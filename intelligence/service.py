"""DiagnosticEngine - single entry point to the intelligence layer.

Used by the FastAPI backend, the monitoring loop and the investigation agent.
It works identically on simulated and plant telemetry: inputs are telemetry
records (internal contract) and operational records, never simulator truth.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from industrial.schemas.plant import PlantConfig, get_plant
from intelligence.active_diagnosis.information_gain import ActiveDiagnosis, hypotheses_from_attribution
from intelligence.explainability.explain import explain_hypothesis, most_abnormal_rows
from intelligence.fault_attribution.bayesian import AttributionResult, PhysicsBayesAttributor
from intelligence.features.windows import sensor_window_features, window_features
from intelligence.mass_balance.engine import BalanceEngine, Evidence
from intelligence.physics.measurement import MeasurementModel, RecordsView, TelemetryStore
from intelligence.uncertainty.propagation import assemble, window_statistics

log = logging.getLogger("oilintel.intelligence")
REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class AnalysisContext:
    store: TelemetryStore
    records: RecordsView
    mm: MeasurementModel
    engine: BalanceEngine


class DiagnosticEngine:
    def __init__(self, plant: PlantConfig | None = None, models_dir: str | Path | None = None):
        self.plant = plant or get_plant()
        self.models_dir = Path(models_dir or REPO_ROOT / "models")
        self.temperature = 1.0
        self.prior_mix = 0.0
        self.loss_model_rel_error = 0.25
        # adaptive layer state (intelligence.adaptation): applied without retraining any model
        self.kind_scale: dict[str, float] = {}           # self-calibrated uncertainty scale per row kind
        self.meter_factors: dict[str, float] = {}        # approved meter-proving factors
        self.family_prior: dict[str, float] | None = None  # experience-memory prior over fault families
        self.models: dict[str, Any] = {}
        self.model_versions: dict[str, str] = {}
        self.active = ActiveDiagnosis(self.plant)
        self.load_models()

    # ------------------------------------------------------------------ models
    def load_models(self) -> None:
        idx_path = self.models_dir / "registry" / "index.json"
        if not idx_path.exists():
            log.info("No trained model registry found at %s - physics/statistical models only", idx_path)
            return
        idx = json.loads(idx_path.read_text())
        for name, info in idx.items():
            try:
                obj = joblib.load(self.models_dir / info["path"] / "model.joblib")
                self.models[name] = obj
                self.model_versions[name] = info["latest"]
            except Exception as e:  # pragma: no cover - corrupt artifact
                log.warning("could not load model %s: %s", name, e)
        cal = self.models.get("physics_bayes")
        if isinstance(cal, dict):
            self.temperature = float(cal.get("temperature", 1.0))
            self.prior_mix = float(cal.get("prior_mix", 0.0))
            self.loss_model_rel_error = float(cal.get("loss_model_rel_error", 0.25))

    # ----------------------------------------------------------------- context
    def context(self, telemetry: pd.DataFrame, records: dict[str, pd.DataFrame] | None = None) -> AnalysisContext:
        rec = RecordsView(**(records or {}))
        store = TelemetryStore(telemetry)
        mm = MeasurementModel(self.plant, store, rec)
        mm.meter_factors = dict(self.meter_factors)
        eng = BalanceEngine(self.plant, mm)
        eng.kind_scale = dict(self.kind_scale)
        return AnalysisContext(store, rec, mm, eng)

    # Telemetry read past a window's end. Totalisers are interpolated at the boundary, which needs a sample
    # after it; live sensors sample at different phases, so a frame cut exactly at the end holds some meters
    # at an earlier reading (up to one sample interval of flow short: a z~10 phantom at transfer starts
    # and stops). Two sample intervals at 120x.
    READ_AHEAD_S = 300.0

    @staticmethod
    def telemetry_start(records: dict[str, Any] | None, t0: float, t1: float, pad: float = 900.0,
                        max_back_s: float = 48 * 3600.0) -> float:
        """Earliest telemetry needed to analyse [t0, t1]: a transfer that completes inside the period is
        reconciled against its ticket, which needs meter data back to the transfer's start."""
        start = t0 - pad
        tr = (records or {}).get("transfers")
        if tr is not None and len(tr) and "end_time" in tr.columns:
            done = tr[tr["end_time"].notna()]
            if len(done):
                from intelligence.physics.measurement import to_epoch
                ends = np.asarray(to_epoch(done["end_time"]), dtype=float)
                starts = np.asarray(to_epoch(done["start_time"]), dtype=float)
                sel = (ends > t0) & (ends <= t1)
                if sel.any():
                    start = min(start, max(float(starts[sel].min()) - 120.0, t0 - max_back_s))
        return start

    @staticmethod
    def window_edges(t0: float, t1: float, window_s: float = 1800.0, align: float = 600.0) -> list[float]:
        """Window boundaries on a fixed grid (identical for live and offline analysis)."""
        start = math.ceil((t0 - align) / window_s) * window_s + align
        edges = list(np.arange(start, t1 + 1e-6, window_s))
        return edges

    def evidence(self, ctx: AnalysisContext, t0: float, t1: float, window_s: float = 1800.0,
                 anchor: str = "end") -> Evidence | None:
        """Balance evidence over [t0, t1]. anchor="end" places the last window boundary at t1
        so the most recent (often most informative) data is never dropped; "grid" aligns to
        the fixed monitoring grid."""
        if anchor == "grid":
            edges = self.window_edges(t0, t1, window_s)
        else:
            n = int((t1 - t0) // window_s)
            edges = [t1 - k * window_s for k in range(n, -1, -1)]
        if len(edges) < 2:
            return None
        return ctx.engine.evidence(edges)

    # --------------------------------------------------------------- analytics
    def monitor(self, ctx: AnalysisContext, t_end: float, window_s: float = 1800.0) -> dict[str, Any]:
        """Evaluate the most recent complete window: balances, uncertainty, detectors."""
        edges = self.window_edges(t_end - window_s * 1.999, t_end, window_s)
        if len(edges) < 2:
            return {"status": "INSUFFICIENT_DATA"}
        ev = ctx.engine.evidence(edges[-2:])
        stats = window_statistics(ev)[-1]
        feats = pd.DataFrame(window_features(self.plant, ev))
        scores: dict[str, float] = {"statistical_chi2": float(max(feats["phys_log_p"].iloc[0],
                                                                  feats["phys_max_abs_z_cv"].iloc[0] / 4 * 3))}
        alarms: dict[str, bool] = {"statistical_chi2": scores["statistical_chi2"] >= 3.0}
        for name in ("isolation_forest", "xgboost_physics"):
            m = self.models.get(name)
            if m is not None:
                try:
                    sc = float(m.score(feats)[0])
                    scores[name] = sc
                    alarms[name] = sc >= m.threshold
                except Exception as e:  # feature mismatch etc.
                    log.warning("detector %s failed: %s", name, e)
        conventional = abs(stats["facility_residual_t"]) > max(0.005 * feats["ctx_throughput_t"].iloc[0], 2.0)
        win = ev.windows[0]
        q = {k: v for k, v in win.quality.items() if v["stuck"] or v["dropout"]}
        return {"t0": win.t0, "t1": win.t1, "route": win.route_guess, "statistics": stats, "scores": scores,
                "alarms": alarms, "conventional_alarm": bool(conventional), "quality_issues": q,
                "anomaly": bool(alarms.get("statistical_chi2") or alarms.get("xgboost_physics") or q),
                "features": feats.iloc[0].to_dict()}

    def attribute(self, ev: Evidence, exclude_families: set[str] | None = None) -> AttributionResult:
        return PhysicsBayesAttributor(self.plant, family_prior=self.family_prior, exclude_families=exclude_families,
                                      temperature=self.temperature, prior_mix=self.prior_mix,
                                      loss_model_rel_error=self.loss_model_rel_error).attribute(ev)

    def sensor_health(self, ev: Evidence, ctx: AnalysisContext) -> list[dict[str, Any]]:
        clf = self.models.get("sensor_classifier")
        df = pd.DataFrame(sensor_window_features(self.plant, ev, ctx.store))
        if not len(df):
            return []
        last = df[df.window == df.window.max()].copy()
        if clf is not None:
            P = clf.predict_proba(last)
            last["predicted"] = clf.decide(P)
            last["p_fault"] = 1 - P[:, 0]
        else:
            last["predicted"] = np.where(last.q_flat_run_frac > 0.33, "STUCK",
                                         np.where(last.q_max_gap_frac > 0.33, "DROPOUT", "NORMAL"))
            last["p_fault"] = (last.predicted != "NORMAL").astype(float)
        last["note"] = ""
        # A flat reading only indicates a stuck meter when it contradicts the process. A flow meter reading
        # exactly zero on a line whose valves are all shut is what a healthy meter shows (the classifier,
        # seeing only the flat signal, called an idle FM-003 "stuck" at p ~ 0.5).
        win = ev.windows[-1]
        for i in last.index[last.predicted == "STUCK"]:
            sid = last.at[i, "sensor_id"]
            cfg = self.plant.sensors[sid]
            if cfg.type != "flow":
                continue
            valves = [v for v, vc in self.plant.valves.items() if vc.pipeline == cfg.asset]
            w = ctx.store.window(sid, cfg.parameter, win.t0, win.t1)
            vals = np.asarray(w.v)[np.asarray(w.good, dtype=bool)] if len(w) else np.array([])
            if valves and len(vals) and np.all(vals == 0.0) and not any(ctx.mm.valve_open(v, win.t0, win.t1) for v in valves):
                last.at[i, "predicted"] = "NORMAL"
                last.at[i, "p_fault"] = 0.0
                last.at[i, "note"] = "idle line: valves shut, zero flow expected"
        return last[["sensor_id", "predicted", "p_fault", "q_coverage", "q_flat_run_frac", "z_bias_dir",
                     "z_tank_cv", "z_consistency", "note"]].sort_values("p_fault", ascending=False).to_dict("records")

    def rank_tests(self, result: AttributionResult, transfer_active: bool = False,
                   performed: set[str] | None = None, top: int = 6,
                   posterior: tuple[list, np.ndarray] | None = None) -> list[dict[str, Any]]:
        hyps, p = posterior if posterior is not None else hypotheses_from_attribution(result)
        return [r.to_dict() for r in self.active.rank(hyps, p, transfer_active, performed, top)]

    def explain(self, result: AttributionResult, top: int = 3) -> dict[str, Any]:
        return {"most_abnormal_rows": most_abnormal_rows(result),
                "hypotheses": {s.hypothesis.hid: explain_hypothesis(result, s.hypothesis.hid)
                               for s in result.ranked(top)}}

    def balance_table(self, ev: Evidence) -> list[dict[str, Any]]:
        A = assemble(ev)
        out = []
        for r, z, s in zip(A.rows, A.z, A.sigma_r):
            out.append({"row": r.row_id, "window": r.window, "kind": r.kind, "key": r.key, "value": round(r.value, 4),
                        "unit": r.unit, "sigma": round(float(s), 4), "z": round(float(z), 2),
                        "top_uncertainty_sources": [k for k, _ in A.contributions(A.rows.index(r))[:3]]})
        return out
