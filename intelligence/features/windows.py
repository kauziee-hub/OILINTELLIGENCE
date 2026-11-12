"""Feature engineering for the learned models (Models 1 and 2).

Two feature families are produced per window:

* physics features   : uncertainty-normalised residuals of the topology-derived
                       control volumes, ledger/consistency/pump rows, chi-square.
* raw-signal features: conventional statistics of the measurements (flows,
                       meter disagreement, level rates, pressures, power) that a
                       non-topological model would use.

Experiment 2/3 compare classifiers trained on raw-only vs. raw+physics features.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from industrial.schemas.plant import PlantConfig
from intelligence.mass_balance.engine import Evidence, WindowEvidence
from intelligence.uncertainty.propagation import assemble

ROUTES = ["R-01", "R-02", "R-03"]


def _z_by(win_rows, A):
    out = {}
    for r, z, s in zip(A.rows, A.z, A.sigma_r):
        out[(r.kind, r.key)] = (float(r.value), float(z), float(s))
    return out


def window_features(plant: PlantConfig, ev: Evidence) -> list[dict[str, Any]]:
    feats = []
    mm_store = None
    for win in ev.windows:
        A = assemble(ev, rows=win.rows)
        zb = _z_by(win.rows, A)
        c2, n, p = A.chi2()
        f: dict[str, Any] = {"t0": win.t0, "t1": win.t1, "window": win.index}
        # ---------------- physics (topology-aware) features
        f["phys_chi2_per_dof"] = c2 / max(n, 1)
        f["phys_log_p"] = float(-math.log10(max(p, 1e-300)))
        cvz = [z for (k, _), (_, z, _) in zb.items() if k == "CV"]
        f["phys_max_abs_z_cv"] = max((abs(z) for z in cvz), default=0.0)
        for tk in plant.tanks:
            cv = win.topology.cv_of_tank(tk)
            v, z, s = zb.get(("CV", cv), (0.0, 0.0, 1.0))
            f[f"phys_z_cv_{tk}"] = z
            f[f"phys_r_cv_{tk}"] = v
        man = [k for (kind, k) in zb if kind == "CV" and k.startswith("CV:MANIFOLD")]
        f["phys_z_cv_manifold"] = zb[("CV", man[0])][1] if man else 0.0
        other = [z for (kind, k), (_, z, _) in zb.items() if kind == "CV" and "TK-" not in k and "MANIFOLD" not in k]
        f["phys_max_abs_z_cv_line"] = max((abs(z) for z in other), default=0.0)
        cv_rows = [r for r in win.rows if r.kind == "CV"]
        fac = sum(r.value for r in cv_rows)
        mask = np.array([r.kind == "CV" for r in A.rows], dtype=float)
        var_fac = float(mask @ A.cov @ mask) if len(mask) else 1.0
        f["phys_facility_residual_t"] = fac
        f["phys_z_facility"] = fac / math.sqrt(max(var_fac, 1e-9))
        led = [(v, z) for (k, _), (v, z, _) in zb.items() if k == "LEDGER"]
        f["phys_ledger_max_z"] = max((z for _, z in led), default=0.0)
        f["phys_unbooked_t"] = sum(v for v, _ in led)
        for key in ("TT-004", "TT-005"):
            f[f"phys_z_tcons_{key}"] = zb.get(("TCONS", key), (0, 0, 1))[1]
        for key in ("DT-001", "DT-002"):
            f[f"phys_z_dcons_{key}"] = zb.get(("DCONS", key), (0, 0, 1))[1]
        for pu in plant.pumps:
            f[f"phys_z_pump_{pu}"] = zb.get(("PUMP", pu), (0, 0, 1))[1]
        tick = [abs(z) for (k, _), (_, z, _) in zb.items() if k == "TICKET"]
        f["phys_ticket_max_abs_z"] = max(tick, default=0.0)
        # ---------------- operating context
        for r in ROUTES:
            f[f"ctx_route_{r}"] = 1.0 if win.route_guess == r else 0.0
        f["ctx_idle"] = 1.0 if not win.topology.active_meters else 0.0
        f["ctx_throughput_t"] = sum(m["mass_t"] for m in win.meter_mass.values())
        # ---------------- raw-signal (non-topological) features
        for m in plant.meters():
            f[f"raw_mass_{m}"] = win.meter_mass.get(m, {}).get("mass_t", 0.0)
        m1, m2, m3 = (f["raw_mass_FM-001"], f["raw_mass_FM-002"], f["raw_mass_FM-003"])
        f["raw_rel_diff_fm1_fm2"] = (m1 - m2) / max(m1, 1.0) if m1 > 1 and m2 > 1 else 0.0
        f["raw_rel_diff_fm1_fm3"] = (m1 - m3) / max(m1, 1.0) if m1 > 1 and m3 > 1 else 0.0
        inv0 = ev.inventories[win.index]
        inv1 = ev.inventories[win.index + 1]
        for tk in plant.tanks:
            f[f"raw_dmass_{tk}"] = inv1[tk]["mass_t"] - inv0[tk]["mass_t"]
            f[f"raw_level_rate_{tk}"] = (inv1[tk]["level_m"] - inv0[tk]["level_m"]) / max(win.duration_h, 1e-6)
        f["raw_dmass_total"] = sum(f[f"raw_dmass_{tk}"] for tk in plant.tanks)
        f["raw_dmass_total_rel"] = f["raw_dmass_total"] / max(f["ctx_throughput_t"], 1.0)
        for pid, pv in win.pipe_pressure.items():
            f[f"raw_pressure_{pid}"] = pv if not math.isnan(pv) else 0.0
        for pu in plant.pumps:
            f[f"raw_pump_running_{pu}"] = win.pump_running.get(pu, 0.0)
        q = win.quality
        f["qual_min_coverage"] = min((v["coverage"] for v in q.values()), default=1.0)
        f["qual_max_flat_run_frac"] = max((v["flat_run_s"] for v in q.values()), default=0.0) / max(
            win.t1 - win.t0, 1.0)
        f["qual_max_gap_frac"] = max((v["max_gap_s"] for v in q.values()), default=0.0) / max(win.t1 - win.t0, 1.0)
        f["qual_any_stuck"] = float(any(v["stuck"] for v in q.values()))
        f["qual_any_dropout"] = float(any(v["dropout"] for v in q.values()))
        feats.append(f)
    return feats


SENSOR_TYPES = ["flow", "level", "temperature"]


def sensor_window_features(plant: PlantConfig, ev: Evidence, store=None) -> list[dict[str, Any]]:
    """One row per (instrument, window) for the instrument-fault classifier."""
    out = []
    sensors = [s for s, c in plant.sensors.items() if c.type in SENSOR_TYPES and c.asset != plant.plant_id]
    prev_z: dict[str, list[float]] = {s: [] for s in sensors}
    for win in ev.windows:
        A = assemble(ev, rows=win.rows)
        zb = _z_by(win.rows, A)
        for sid in sensors:
            cfg = plant.sensors[sid]
            q = win.quality.get(sid, {"coverage": 1, "flat_run_s": 0, "max_gap_s": 0, "bad_fraction": 0,
                                      "stuck": False, "dropout": False})
            f: dict[str, Any] = {"sensor_id": sid, "window": win.index, "t0": win.t0, "t1": win.t1}
            for t in SENSOR_TYPES:
                f[f"type_{t}"] = 1.0 if cfg.type == t else 0.0
            dur = max(win.t1 - win.t0, 1.0)
            f["q_coverage"] = q["coverage"]
            f["q_flat_run_frac"] = q["flat_run_s"] / dur
            f["q_max_gap_frac"] = q["max_gap_s"] / dur
            f["q_bad_fraction"] = q["bad_fraction"]
            z_in = z_out = z_tank = z_cons = 0.0
            if cfg.type == "flow":
                for cv, signs in win.meter_sign.items():
                    if sid in signs:
                        z = zb.get(("CV", cv), (0, 0, 1))[1]
                        if signs[sid] > 0:
                            z_in += z
                        else:
                            z_out += z
                z_cons = zb.get(("LEDGER", sid), (0, 0, 1))[1]
            elif cfg.type == "level":
                z_tank = zb.get(("CV", win.topology.cv_of_tank(cfg.asset)), (0, 0, 1))[1]
            else:
                if cfg.asset in plant.tanks:
                    z_tank = zb.get(("CV", win.topology.cv_of_tank(cfg.asset)), (0, 0, 1))[1]
                    for (kind, key), (_, z, _) in zb.items():
                        if kind == "TCONS":
                            row = next((r for r in win.rows if r.kind == "TCONS" and r.key == key), None)
                            if row and row.meta.get("source_tank") == cfg.asset:
                                z_cons -= z
                else:
                    z_cons = zb.get(("TCONS", sid), (0, 0, 1))[1]
            # signed "bias direction" score: positive meter bias -> inflow CV z>0 & outflow CV z<0
            f["z_in_cv"] = z_in
            f["z_out_cv"] = z_out
            f["z_bias_dir"] = z_in - z_out
            f["z_tank_cv"] = z_tank
            f["z_consistency"] = z_cons
            key_z = z_in - z_out + z_tank + z_cons
            hist = prev_z[sid]
            f["z_delta_prev"] = key_z - (hist[-1] if hist else 0.0)
            f["z_trend3"] = key_z - (hist[-3] if len(hist) >= 3 else (hist[0] if hist else 0.0))
            f["z_cum_mean"] = float(np.mean(hist + [key_z]))
            hist.append(key_z)
            f["active"] = 1.0 if (cfg.type != "flow" or sid in win.meter_mass) else 0.0
            if store is not None:
                w = store.window(sid, cfg.parameter, win.t0, win.t1)
                v = w.v[w.good]
                if len(v) > 5:
                    d = np.diff(v)
                    expected = (cfg.noise + cfg.noise_abs + cfg.noise_rel * abs(float(np.mean(v))) + 1e-9)
                    f["sig_noise_ratio"] = float(np.std(d) / (math.sqrt(2) * expected))
                    mad = float(np.median(np.abs(d - np.median(d)))) + 1e-9
                    f["sig_step_score"] = float(np.max(np.abs(d)) / (1.4826 * mad)) if mad > 1e-6 else 0.0
                else:
                    f["sig_noise_ratio"] = 0.0
                    f["sig_step_score"] = 0.0
            out.append(f)
    return out
