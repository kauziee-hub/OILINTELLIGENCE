"""Layer 11 - digital-twin hypothesis validation ("validate via digital twin").

The Bayesian attributor scores hypotheses with linearised signatures. The twin replay checks the
leading hypotheses against the full nonlinear physics: the analysis period is rebuilt in the
simulator from MEASURED data only (initial tank levels / temperatures, the transfers in the records,
the measured flow rates and running pumps), the hypothesised fault is injected with the magnitude
the attributor estimated, and the simulated telemetry goes through the same balance engine. The
replayed control-volume residuals are compared with the observed ones:

    d = r_obs - r_twin,   chi2 = d^T (2 C_obs)^-1 d

(2 C_obs because the replay draws its own, independent instrument errors). A hypothesis whose
estimated magnitude is physically implausible - e.g. a leak rate the measured line pressure cannot
drive, or an onset that the transfer schedule contradicts - fails the replay even when the linear
signature fit looked good. Simulator ground truth of the live plant is never used.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np
from scipy import stats

from industrial.schemas.plant import PlantConfig
from intelligence.uncertainty.propagation import assemble
from simulation.engine.export import run_scenario
from simulation.faults.faults import FaultSpec
from simulation.hydraulics.network import LEAK_CD
from simulation.scenarios.scenario import Scenario, TankInitial, TransferOrder

G = 9.81
RHO = 850.0
LEAD_IN_S = 900.0               # replay starts 15 min before the first window (transients settle)
REPLAYABLE = {"NORMAL", "PIPELINE_LEAK", "TANK_LEAK", "METER_ERROR", "LEVEL_ERROR", "TEMP_ERROR", "DENSITY_ERROR"}


def hole_diameter_mm(rate_t_h: float, *, dp_pa: float | None = None, head_m: float | None = None) -> float:
    """Orifice diameter that drives `rate_t_h` (Q = Cd A sqrt(2 dp / rho), as in the simulator)."""
    m = max(rate_t_h, 0.0) * 1000.0 / 3600.0
    v = math.sqrt(2.0 * dp_pa / RHO) if dp_pa is not None else math.sqrt(2.0 * G * (head_m or 10.0))
    area = m / (RHO * LEAK_CD * v) if v > 0 else 0.0
    return math.sqrt(4.0 * area / math.pi) * 1000.0


class TwinValidator:
    def __init__(self, plant: PlantConfig, engine):
        self.plant = plant
        self.engine = engine

    # ------------------------------------------------------------ scenario
    def build_base(self, ctx, t_start: float, t_end: float) -> Scenario:
        P, mm = self.plant, ctx.mm
        initial = {}
        for tk, cfg in P.tanks.items():
            lvl = mm.tank_level(tk, t_start)
            tmp = mm.tank_temperature(tk, t_start)
            initial[tk] = TankInitial(level_m=float(lvl) if math.isfinite(lvl) else 0.5 * cfg.shell_height_m,
                                      temperature_c=float(tmp) if math.isfinite(tmp) else 30.0)
        ops = []
        tr = ctx.records.transfers
        for _, rec in (tr.iterrows() if len(tr) else []):
            s, e = float(rec["start_ts"]), float(rec["end_ts"])
            route = P.routes.get(rec["route"])
            if route is None or e <= t_start or s >= t_end:
                continue
            a, b = max(s, t_start), min(e, t_end)
            w = ctx.store.window(route.control_meter, "volume_flow", a, b)
            flowing = w.v[w.good & (w.v > 5.0)] if len(w) else np.array([])
            sp = float(np.median(flowing)) if len(flowing) else float(np.mean(route.setpoint_range_m3h))
            if math.isinf(e):
                vol = sp * ((t_end - a) / 3600.0 + 1.0)          # still running at the end of the period
            else:
                vol = max(mm._raw_gross_increment(route.control_meter, a, b), sp * 0.05)
            pumps = [p for p in P.pumps if np.nanmean(ctx.store.window(f"{p}-SP", "speed", a, b).v
                                                       if len(ctx.store.window(f"{p}-SP", "speed", a, b)) else [0.0]) > 100]
            ops.append(TransferOrder(transfer_id=f"TW-{rec['transfer_id']}", route=rec["route"], start_s=a - t_start,
                                     setpoint_m3h=sp, pumps=pumps or ["PU-001"], target_volume_m3=vol,
                                     max_duration_s=(b - a) + 3600.0))
        return Scenario(scenario_id="TWIN-BASE", seed=977, description="digital-twin replay",
                        start_time=datetime.fromtimestamp(t_start, tz=timezone.utc), duration_s=t_end - t_start,
                        dt_s=2.0, sample_interval_s=10.0, initial=initial, operations=ops, faults=[])

    def fault_for(self, h: dict[str, Any], edges: list[float], t_start: float) -> tuple[FaultSpec | None, str]:
        fam, tg, mag = h.get("family"), h.get("target"), h.get("magnitude") or {}
        if fam not in REPLAYABLE:
            return None, f"{fam} is not replayable in the twin (accounting / dynamic-state hypothesis)"
        if fam == "NORMAL":
            return None, ""
        w = mag.get("onset_window")
        onset = max(edges[w] - t_start, 0.0) if isinstance(w, int) and 0 <= w < len(edges) else 0.0
        span_h = max((edges[-1] - (t_start + onset)) / 3600.0, 0.5)
        if fam == "PIPELINE_LEAK":
            rate = float(mag.get("rate_t_per_h_ref") or 0.0)
            pos = 0.5
            m = re.search(r"\[([\d.]+)-([\d.]+)\]", str(mag.get("segment") or ""))
            if m:
                pos = 0.5 * (float(m.group(1)) + float(m.group(2)))
            return FaultSpec(fault_type="PIPELINE_LEAK", target=tg, start_s=onset,
                             params={"position": pos, "hole_diameter_mm": hole_diameter_mm(rate, dp_pa=5e5),
                                     "ramp_s": 60.0}), ""
        if fam == "TANK_LEAK":
            rate = float(mag.get("rate_t_per_h_ref") or 0.0)
            return FaultSpec(fault_type="TANK_LEAK", target=tg, start_s=onset,
                             params={"hole_diameter_mm": hole_diameter_mm(rate, head_m=10.0)}), ""
        if fam == "METER_ERROR":
            last, first = float(mag.get("bias_percent_latest") or 0.0), float(mag.get("bias_percent_first") or 0.0)
            if mag.get("mechanism") == "drift":
                return FaultSpec(fault_type="METER_DRIFT", target=tg, start_s=onset,
                                 params={"drift_rel_per_h": (last - first) / 100.0 / span_h}), ""
            return FaultSpec(fault_type="METER_BIAS", target=tg, start_s=onset, params={"bias_rel": last / 100.0}), ""
        if fam == "LEVEL_ERROR":
            change = float(mag.get("level_error_change_mm") or 0.0) / 1000.0
            if mag.get("mechanism") == "drift":
                return FaultSpec(fault_type="LEVEL_DRIFT", target=tg, start_s=onset,
                                 params={"drift_m_per_h": change / span_h}), ""
            return FaultSpec(fault_type="LEVEL_BIAS", target=tg, start_s=onset, params={"offset_m": change}), ""
        if fam == "TEMP_ERROR":
            return FaultSpec(fault_type="TEMPERATURE_BIAS", target=tg, start_s=onset,
                             params={"offset_c": float(mag.get("temperature_bias_c_latest") or 0.0)}), ""
        return FaultSpec(fault_type="DENSITY_BIAS", target=tg, start_s=onset,
                         params={"offset_kgm3": float(mag.get("density_bias_kgm3_latest") or 0.0)}), ""

    def normal_dispersion(self, telemetry, records: dict[str, Any], edges: list[float]) -> dict[str, float]:
        """E[z^2] per row kind when the recent operations are replayed WITHOUT any fault. The twin's
        instruments behave exactly to specification, so a calibrated model gives ~1 here; comparing
        with the plant's own dispersion separates 'instruments degraded' from 'model mis-specified'."""
        ctx = self.engine.context(telemetry, records)
        base = self.build_base(ctx, edges[0] - LEAD_IN_S, edges[-1])
        res = run_scenario(base.model_copy(update={"scenario_id": "TWIN-NORMAL"}), self.plant)
        sctx = self.engine.context(res.telemetry, {"transfers": res.transfers, "lab_samples": res.lab_samples,
                                                   "calibrations": res.calibrations})
        B = assemble(sctx.engine.evidence(edges))
        out: dict[str, list[float]] = {}
        for r, z in zip(B.rows, B.z):
            if np.isfinite(z):
                out.setdefault(r.kind, []).append(float(z) ** 2)
        return {k: round(float(np.mean(v)), 3) for k, v in out.items()}

    # ------------------------------------------------------------ replay
    def validate(self, telemetry, records: dict[str, Any], edges: list[float], hypotheses: list[dict[str, Any]],
                 max_hypotheses: int = 3) -> dict[str, Any]:
        t0 = time.time()
        ctx = self.engine.context(telemetry, records)
        ev_obs = ctx.engine.evidence(edges)
        A = assemble(ev_obs, kinds={"CV"})
        obs_idx = {r.row_id: i for i, r in enumerate(A.rows)}
        r_obs = A.r
        observed_total = round(float(np.sum(r_obs)), 3)        # a property of the data, not of any replay
        t_start, t_end = edges[0] - LEAD_IN_S, edges[-1]
        base = self.build_base(ctx, t_start, t_end)
        cands = [{"hypothesis_id": "NORMAL", "family": "NORMAL", "target": None, "magnitude": {}}]
        cands += [h for h in hypotheses if h.get("family") not in ("NORMAL", "UNKNOWN")][:max_hypotheses]
        out = []
        for h in cands:
            fault, reason = self.fault_for(h, edges, t_start)
            if fault is None and h["family"] != "NORMAL":
                out.append({"hypothesis_id": h["hypothesis_id"], "replayed": False, "reason": reason})
                continue
            sc = base.model_copy(update={"scenario_id": f"TWIN-{h['hypothesis_id']}", "faults": [fault] if fault else []})
            res = run_scenario(sc, self.plant)
            sctx = self.engine.context(res.telemetry, {"transfers": res.transfers, "lab_samples": res.lab_samples,
                                                       "calibrations": res.calibrations})
            B = assemble(sctx.engine.evidence(edges), kinds={"CV"})
            sim_val = {r.row_id: v for r, v in zip(B.rows, B.r)}
            common = [rid for rid in obs_idx if rid in sim_val]
            if not common:
                out.append({"hypothesis_id": h["hypothesis_id"], "replayed": False,
                            "reason": "replayed topology shares no control volume with the observed period"})
                continue
            ii = [obs_idx[c] for c in common]
            d = r_obs[ii] - np.array([sim_val[c] for c in common])
            C = 2.0 * A.cov[np.ix_(ii, ii)]
            chi2 = float(d @ np.linalg.solve(C + 1e-9 * np.eye(len(ii)), d))
            ss_obs = float(np.sum(r_obs[ii] ** 2))
            per_window = []
            for w in range(len(edges) - 1):
                ok_ = [i for rid, i in obs_idx.items() if rid.startswith(f"w{w}|")]
                tk_ = [v for rid, v in sim_val.items() if rid.startswith(f"w{w}|")]
                per_window.append({"window": w, "observed_t": round(float(sum(r_obs[i] for i in ok_)), 3),
                                   "twin_t": round(float(sum(tk_)), 3)})
            out.append({"hypothesis_id": h["hypothesis_id"], "replayed": True,
                        "fault": fault.model_dump(include={"fault_type", "target", "start_s", "params"}) if fault else None,
                        "rows_compared": len(common), "chi2": round(chi2, 2), "chi2_per_dof": round(chi2 / len(common), 3),
                        "p_value": float(stats.chi2.sf(chi2, len(common))),
                        "explained_fraction": round(1.0 - float(np.sum(d ** 2)) / ss_obs, 3) if ss_obs > 1e-9 else None,
                        "rows_observed": len(obs_idx), "rows_twin": len(sim_val),
                        "observed_facility_t": observed_total,
                        "twin_facility_t": round(float(sum(sim_val.values())), 3),
                        "per_window": per_window})
        replayed = [r for r in out if r.get("replayed")]
        best = min(replayed, key=lambda r: r["chi2_per_dof"], default=None)
        for r in replayed:
            r["consistent"] = bool(r["p_value"] > 0.01)
        return {"method": "nonlinear simulator replay from measured initial state and recorded operations; "
                          "chi2 = d^T (2 C_obs)^-1 d over the control-volume rows both share",
                "observed_facility_t": observed_total,
                "period": [edges[0], edges[-1]], "windows": len(edges) - 1,
                "transfers_replayed": [o.transfer_id for o in base.operations],
                "results": out, "best_supported": best["hypothesis_id"] if best else None,
                "seconds": round(time.time() - t0, 1)}
