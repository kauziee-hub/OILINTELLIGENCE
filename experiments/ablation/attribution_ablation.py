"""Experiment 7 - ablation: remove one component of the physics-guided attribution at a time
and re-score the held-out test cases (environment A test split + unseen-fault environment C).

Each variant uses the uncalibrated posterior (T = 1, no prior mixing) so that differences come
from the evidence model itself, not from the calibration fitted to the full model.
"""
from __future__ import annotations

import math
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import numpy as np

from industrial.schemas.plant import get_plant
from intelligence.fault_attribution.bayesian import PhysicsBayesAttributor
from intelligence.fault_attribution.hypotheses import FAULT_TO_FAMILY
from intelligence.mass_balance.engine import BalanceEngine
from intelligence.physics.measurement import MeasurementModel, RecordsView, TelemetryStore, to_epoch
from intelligence.training.dataset import CASE_WINDOWS, GRID_OFFSET_S, WINDOW_S, build_plan
from simulation.engine.export import run_scenario
from simulation.scenarios.scenario import generate_scenario

ALL_KINDS = {"CV", "LEDGER", "TICKET", "TCONS", "DCONS", "PUMP"}
VARIANTS: dict[str, dict[str, Any]] = {
    "full_model": {},
    "no_two_fault_hypotheses": {"multi_fault": False},
    "no_accounting_records": {"use_kinds": ALL_KINDS - {"LEDGER", "TICKET"}},
    "no_temperature_density_consistency": {"use_kinds": ALL_KINDS - {"TCONS", "DCONS"}},
    "no_pump_curve": {"use_kinds": ALL_KINDS - {"PUMP"}},
    "control_volume_rows_only": {"use_kinds": {"CV"}},
    "no_unknown_catch_all": {"exclude_families": {"UNKNOWN"}},
}


def _case(spec: dict[str, Any]) -> dict[str, Any]:
    """Same case window selection as the training dataset (fault onset + 2.5 h, 8 windows)."""
    plant = get_plant()
    sc = generate_scenario(plant, spec["family"], spec["seed"], spec["env"], duration_s=spec["duration_s"],
                           dt_s=2.0, sample_interval_s=10.0)
    res = run_scenario(sc)
    mm = MeasurementModel(plant, TelemetryStore(res.telemetry),
                          RecordsView(transfers=res.transfers, lab_samples=res.lab_samples, calibrations=res.calibrations))
    st = to_epoch(sc.start_time)
    grid = np.arange(GRID_OFFSET_S, sc.duration_s - 60.0 + 1e-6, WINDOW_S)
    fs = sc.faults[0].start_s if sc.faults else float(np.random.default_rng(spec["seed"]).uniform(3, 5)) * 3600
    if sc.faults and sc.faults[0].category == "accounting":
        o = next((o for o in sc.operations if o.transfer_id == sc.faults[0].target), None)
        fs = o.start_s if o else fs
    hi = [g for g in grid if g <= min(fs + 2.5 * 3600, sc.duration_s - 60)]
    sel = [g for g in hi if g >= hi[-1] - CASE_WINDOWS * WINDOW_S] if hi else list(grid[:CASE_WINDOWS + 1])
    ev = BalanceEngine(plant, mm).evidence([st + g for g in sel])
    rmap = {o.transfer_id: o.route for o in sc.operations}
    truth = [f"{FAULT_TO_FAMILY[f.fault_type]}:{rmap.get(f.target, f.target)}" for f in sc.faults]
    fams = [FAULT_TO_FAMILY[f.fault_type] for f in sc.faults] or ["NORMAL"]
    out = {"run_id": sc.scenario_id, "env": spec["env"], "truth": truth, "families": fams, "variants": {}}
    for name, kw in VARIANTS.items():
        r = PhysicsBayesAttributor(plant, **kw).attribute(ev)
        fd = r.family_distribution()
        hd = r.hypothesis_distribution()
        top_fam = max(fd, key=fd.get)
        ranked = sorted(hd, key=hd.get, reverse=True)
        p_true = max([fd.get(f, 0.0) for f in fams] + [1e-12])
        out["variants"][name] = {"top1_family": top_fam in fams, "top3_hypothesis": bool(set(ranked[:3]) & set(truth))
                                 if truth else top_fam == "NORMAL", "log_loss": -math.log(max(p_true, 1e-12)),
                                 "top_family": top_fam, "top_p": round(fd[top_fam], 3)}
    return out


def run(workers: int = 6, include_env_c: bool = True) -> dict[str, Any]:
    plan = [s for s in build_plan() if s["split"] == "test" and (s["env"] == "A" or (include_env_c and s["env"] == "C"))]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        cases = list(ex.map(_case, plan))
    out: dict[str, Any] = {"n_cases": {"A": sum(c["env"] == "A" for c in cases), "C": sum(c["env"] == "C" for c in cases)},
                           "variants": {}}
    for name in VARIANTS:
        a = [c["variants"][name] for c in cases if c["env"] == "A"]
        cc = [c["variants"][name] for c in cases if c["env"] == "C"]
        out["variants"][name] = {
            "A_top1_family": round(float(np.mean([v["top1_family"] for v in a])), 3),
            "A_top3_hypothesis": round(float(np.mean([v["top3_hypothesis"] for v in a])), 3),
            "A_mean_log_loss": round(float(np.mean([v["log_loss"] for v in a])), 3),
            # unseen faults: a confident (>= 0.9) known-cause verdict is an unsupported diagnosis
            "C_confident_known_cause": int(sum(v["top_family"] not in ("UNKNOWN",) and v["top_p"] >= 0.9 for v in cc)),
            "C_abstained_unknown": int(sum(v["top_family"] == "UNKNOWN" for v in cc)),
        }
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2))
