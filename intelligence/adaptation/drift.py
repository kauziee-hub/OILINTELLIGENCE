"""Layer 7 - continuous monitoring & drift detection.

The monitor evaluates a 30-min balance window every few minutes. Each evaluation is observed here
and five kinds of drift are tracked against the training reference:

    data gate     share of windows failing the data-quality gate (stuck / dropped-out instruments)
    data          operating-feature distribution vs training (population stability index, PSI)
    calibration   dispersion of normalised residuals z on normal windows (E[z^2] should be ~1)
    prediction    transient (unconfirmed) alarm rate vs the false-alarm budget
    sensor        persistent mean shift of one balance row (slow instrument drift or small loss)
    regime        operating point outside the training envelope (throughput, route)

Monitoring windows overlap (30-min window every 5 min), so successive z values are correlated;
every test uses an effective sample size n_eff = n * interval / window.

A dispersion change with zero mean is a calibration matter (noisier instruments, model mis-specified);
a mean shift is a fault and is never absorbed by self-calibration - it is handed to the reasoning core.
"""
from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats

DATA_FEATURES = ["ctx_throughput_t", "raw_pressure_PL-001", "raw_pressure_PL-002", "raw_pressure_PL-003",
                 "raw_pressure_PL-004", "raw_mass_FM-001", "raw_mass_FM-002", "raw_mass_FM-003"]
ALARM_BUDGET = 0.05            # section 14 target: <= 5 false alarms per 100 normal windows
# rows whose normalised residual is designed to be ~N(0, 1). LEDGER rows (metered but unbooked
# movement) are one-sided - clipped at zero - so E[z^2] ~ 0 under normal operation: not testable.
CALIBRATION_KINDS = {"CV", "TCONS", "DCONS", "PUMP", "TICKET"}
# PSI needs enough INDEPENDENT windows: 60 overlapping windows ~ 10 independent samples
DISTRIBUTION_MIN_WINDOWS = 60
SEVERITY_ORDER = {"GREEN": 0, "AMBER": 1, "RED": 2}


@dataclass
class DriftSignal:
    kind: str
    severity: str
    statistic: float | None
    threshold: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ reference
def build_reference(windows) -> dict[str, Any]:
    """Training reference from the training-split windows without an observable fault."""
    w = windows
    if "split" in w.columns:
        w = w[w["split"] == "train"]
    if "label_observable" in w.columns:
        w = w[w["label_observable"] == 0]
    ref: dict[str, Any] = {"n_windows": int(len(w)), "features": {}}
    for f in DATA_FEATURES:
        if f not in w.columns:
            continue
        v = w[f].astype(float).to_numpy()
        v = v[np.isfinite(v)]
        if len(v) < 50:
            continue
        edges = np.unique(np.quantile(v, np.linspace(0, 1, 11)))
        if len(edges) < 3:
            continue
        hist = np.histogram(v, bins=edges)[0] / len(v)
        ref["features"][f] = {"edges": edges.tolist(), "p": hist.tolist(), "q01": float(np.quantile(v, 0.01)),
                              "q99": float(np.quantile(v, 0.99)), "mean": float(v.mean()), "sd": float(v.std())}
    routes = [c for c in w.columns if c.startswith("ctx_route_")]
    ref["routes_seen"] = [c.replace("ctx_route_", "") for c in routes if w[c].sum() > 0]
    return ref


def load_reference(models_dir: str | Path, data_dir: str | Path | None = None) -> dict[str, Any] | None:
    """Load models/registry/drift_reference.json; build it from the training windows if missing."""
    path = Path(models_dir) / "registry" / "drift_reference.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    src = Path(data_dir or Path(models_dir).parent / "datasets" / "training") / "windows.parquet"
    if not src.exists():
        return None
    import pandas as pd
    ref = build_reference(pd.read_parquet(src))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ref), encoding="utf-8")
    except OSError:
        pass
    return ref


def psi(ref_edges: list[float], ref_p: list[float], values: np.ndarray) -> float:
    """Population stability index of `values` against the reference decile histogram."""
    edges = np.asarray(ref_edges, dtype=float)
    v = np.clip(values, edges[0], edges[-1])
    q = np.histogram(v, bins=edges)[0] / max(len(v), 1)
    p = np.asarray(ref_p, dtype=float)
    eps = 1e-3
    p, q = np.clip(p, eps, None), np.clip(q, eps, None)
    return float(np.sum((q - p) * np.log(q / p)))


def _sev(value: float, amber: float, red: float) -> str:
    return "RED" if value >= red else ("AMBER" if value >= amber else "GREEN")


# ------------------------------------------------------------------ monitor
class DriftMonitor:
    def __init__(self, reference: dict[str, Any] | None, history: int = 72, overlap_factor: float = 300.0 / 1800.0,
                 min_windows: int = 12):
        self.reference = reference or {"features": {}, "routes_seen": []}
        self.buf: deque[dict[str, Any]] = deque(maxlen=history)
        self.overlap = max(min(overlap_factor, 1.0), 0.05)
        self.min_windows = min_windows

    # one entry per monitoring evaluation
    def observe(self, result: dict[str, Any], features: dict[str, Any] | None = None) -> None:
        z = {k: float(v) for k, v in (result.get("z") or {}).items() if v is not None and math.isfinite(v)}
        feats = {k: float(v) for k, v in (features or {}).items()
                 if isinstance(v, (int, float)) and math.isfinite(float(v))}
        self.buf.append({"t1": result.get("t1"), "anomaly": bool(result.get("anomaly")),
                         "confirmed": bool(result.get("alarm_confirmed")),
                         "quality_ok": not result.get("quality_issues"), "route": result.get("route"),
                         "z": z, "features": feats})

    def reset(self) -> None:
        self.buf.clear()

    def n_eff(self, n: int) -> float:
        return max(n * self.overlap, 1.0)

    def n_eff_series(self, z) -> float:
        """Effective number of independent samples in one row's z series. Overlapping windows AND
        shared systematic errors (a meter calibration, a lab sample) make successive values
        correlated; with lag-1 autocorrelation rho, n_eff = n (1 - rho) / (1 + rho). A value that
        repeats unchanged is ~one draw, however many windows show it."""
        z = np.asarray(z, dtype=float)
        n = len(z)
        if n < 3:
            return max(n * self.overlap, 1.0)
        a, b = z[:-1] - z[:-1].mean(), z[1:] - z[1:].mean()
        den = math.sqrt(float(np.sum(a * a) * np.sum(b * b)))
        rho = 1.0 if den < 1e-9 else float(np.clip(np.sum(a * b) / den, 0.0, 0.999))
        return float(min(max(n * (1 - rho) / (1 + rho), 1.0), max(n * self.overlap, 1.0)))

    def report(self) -> dict[str, Any]:
        rows = list(self.buf)
        n = len(rows)
        if n < self.min_windows:
            return {"status": "WARMING_UP", "n_windows": n, "required": self.min_windows, "overall": "GREEN",
                    "signals": []}
        ok = [r for r in rows if r["quality_ok"]]
        normal = [r for r in ok if not r["anomaly"]]
        signals = [self._data_gate(rows, ok), self._data_drift(ok), self._calibration(normal),
                   self._prediction(ok), *self._sensor(ok), self._regime(ok)]
        signals = [s for s in signals if s is not None]
        overall = max((s.severity for s in signals), key=lambda x: SEVERITY_ORDER[x], default="GREEN")
        return {"status": "ACTIVE", "n_windows": n, "normal_windows": len(normal), "overall": overall,
                "effective_sample_factor": self.overlap, "signals": [s.to_dict() for s in signals]}

    # -------------------------------------------------------------- detectors
    def _data_gate(self, rows, ok) -> DriftSignal:
        fail = 1.0 - len(ok) / max(len(rows), 1)
        return DriftSignal("data_gate", _sev(fail, 0.10, 0.30), round(fail, 3), "AMBER >= 10%, RED >= 30% of windows",
                           f"{100 * fail:.0f}% of recent windows failed the data-quality gate (stuck / dropped-out "
                           "instruments); affected windows are excluded from every drift statistic.",
                           {"failed_windows": len(rows) - len(ok)})

    def _data_drift(self, ok) -> DriftSignal | None:
        feats = self.reference.get("features", {})
        if not feats:
            return None
        if len(ok) < DISTRIBUTION_MIN_WINDOWS:
            return DriftSignal("data", "GREEN", None, f"needs >= {DISTRIBUTION_MIN_WINDOWS} windows",
                               f"Collecting windows for a stable distribution comparison ({len(ok)}/"
                               f"{DISTRIBUTION_MIN_WINDOWS}; overlapping windows carry little independent information).")
        scores = {}
        for f, ref in feats.items():
            v = np.array([r["features"][f] for r in ok if f in r["features"]])
            if len(v) >= self.min_windows:
                scores[f] = round(psi(ref["edges"], ref["p"], v), 3)
        if not scores:
            return None
        worst = max(scores, key=scores.get)
        return DriftSignal("data", _sev(scores[worst], 0.25, 0.5), scores[worst], "PSI: AMBER >= 0.25, RED >= 0.5",
                           f"Largest distribution shift vs training on {worst} (PSI {scores[worst]:.2f}).",
                           {"psi": scores})

    def _calibration(self, normal) -> DriftSignal | None:
        by_row: dict[str, list[float]] = {}
        for r in normal:
            for k, z in r["z"].items():
                if k.split(":", 1)[0] in CALIBRATION_KINDS:
                    by_row.setdefault(k, []).append(z)
        by_kind: dict[str, list[float]] = {}
        neff_kind: dict[str, float] = {}
        for k, zs in by_row.items():
            kind = k.split(":", 1)[0]
            by_kind.setdefault(kind, []).extend(zs)
            neff_kind[kind] = neff_kind.get(kind, 0.0) + self.n_eff_series(zs)
        detail, worst, worst_kind = {}, 0.0, None
        for kind, zs in by_kind.items():
            z = np.asarray(zs)
            if len(z) < 8:
                continue
            ratio = float(np.mean(z ** 2))
            neff = max(neff_kind[kind], 1.0)
            # two-sided chi-square test of E[z^2] = 1 with the effective sample size
            p = 2 * min(stats.chi2.cdf(neff * ratio, neff), stats.chi2.sf(neff * ratio, neff))
            mean = float(z.mean())
            detail[kind] = {"n": len(z), "n_eff": round(neff, 1), "mean_z2": round(ratio, 3), "sd_scale": round(math.sqrt(ratio), 3),
                            "mean_z": round(mean, 3), "p_value": float(p)}
            dev = abs(math.log(max(ratio, 1e-6)))
            if p < 0.01 and dev > worst:
                worst, worst_kind = dev, kind
        if not detail:
            return None
        if worst_kind is None:
            return DriftSignal("calibration", "GREEN", 1.0, "sd scale in [0.8, 1.2] or not significant",
                               "Normalised residuals on normal windows are consistent with the uncertainty model.",
                               detail)
        r = detail[worst_kind]["mean_z2"]
        sd = math.sqrt(r)
        sev = "RED" if (sd > 1.6 or sd < 0.6) else ("AMBER" if (sd > 1.2 or sd < 0.8) else "GREEN")
        return DriftSignal("calibration", sev, r, "sd scale sqrt(E[z^2]): AMBER outside [0.8, 1.2], RED outside [0.6, 1.6]",
                           f"{worst_kind} residual dispersion is {math.sqrt(r):.2f}x the modelled uncertainty on "
                           "normal windows.", detail | {"worst_kind": worst_kind})

    def _prediction(self, ok) -> DriftSignal | None:
        if len(ok) < self.min_windows:
            return None
        transient = sum(1 for r in ok if r["anomaly"] and not r["confirmed"])
        rate = transient / len(ok)
        neff = self.n_eff(len(ok))
        k = rate * neff
        p = float(stats.binom.sf(max(math.ceil(k) - 1, 0), max(int(round(neff)), 1), ALARM_BUDGET))
        sev = "RED" if rate > 0.30 else ("AMBER" if rate > 2 * ALARM_BUDGET and p < 0.05 else "GREEN")
        return DriftSignal("prediction", sev, round(rate, 3), f"transient alarm rate vs budget {ALARM_BUDGET:.0%}",
                           f"{100 * rate:.0f}% of recent windows raised an alarm that did not persist "
                           f"(budget {100 * ALARM_BUDGET:.0f}%).", {"transient_alarms": transient, "windows": len(ok),
                                                                      "p_value": p})

    def _sensor(self, ok) -> list[DriftSignal]:
        series: dict[str, list[float]] = {}
        for r in ok:
            for k, z in r["z"].items():
                series.setdefault(k, []).append(z)
        out = []
        for key, zs in series.items():
            z = np.asarray(zs)
            if len(z) < self.min_windows:
                continue
            mean = float(z.mean())
            neff = self.n_eff_series(z)
            t = mean * math.sqrt(neff) / max(float(z.std(ddof=1)), 0.3)
            # z already includes the systematic uncertainty, so a persistent offset is ONE draw of it:
            # |mean z| >= 2.5 happens to a healthy row ~1% of the time
            if abs(mean) >= 2.5 and abs(t) >= 3.0:
                half = len(z) // 2
                shape = "gradual (growing)" if abs(z[half:].mean()) > 1.5 * abs(z[:half].mean()) + 0.3 else "step / steady"
                out.append(DriftSignal("sensor", "RED" if abs(mean) >= 3.5 else "AMBER", round(mean, 2),
                                       "|mean z| >= 2.5 (AMBER), >= 3.5 (RED)",
                                       f"Persistent {'positive' if mean > 0 else 'negative'} residual on {key} "
                                       f"(mean z {mean:+.2f} over {len(z)} windows, {shape}).",
                                       {"row": key, "mean_z": round(mean, 3), "t": round(t, 2), "shape": shape,
                                        "n": len(z), "n_eff": round(neff, 1)}))
        out.sort(key=lambda s: -abs(s.statistic or 0))
        return out[:4] or [DriftSignal("sensor", "GREEN", 0.0, "|mean z| < 2.5",
                                       "No balance row shows a persistent mean shift.")]

    def _regime(self, ok) -> DriftSignal | None:
        ref = self.reference.get("features", {}).get("ctx_throughput_t")
        if not ref or len(ok) < DISTRIBUTION_MIN_WINDOWS:
            return None
        v = np.array([r["features"].get("ctx_throughput_t", np.nan) for r in ok])
        v = v[np.isfinite(v)]
        if not len(v):
            return None
        outside = float(np.mean((v < ref["q01"]) | (v > ref["q99"])))
        seen = set(self.reference.get("routes_seen") or [])
        unseen = sorted({r["route"] for r in ok if r.get("route") and seen and r["route"] not in seen})
        sev = "RED" if outside > 0.5 or unseen else _sev(outside, 0.2, 0.5)
        return DriftSignal("regime", sev, round(outside, 3), "share outside training envelope: AMBER > 20%, RED > 50%",
                           f"{100 * outside:.0f}% of recent windows run outside the training throughput envelope"
                           + (f"; routes never seen in training: {', '.join(unseen)}" if unseen else "."),
                           {"throughput_envelope_t": [round(ref["q01"], 1), round(ref["q99"], 1)], "unseen_routes": unseen})
