"""Synthetic dataset generation pipeline (section 6).

Produces, for every simulated run:
  * window-level features + labels      (Model 1, learned attribution baseline)
  * instrument-window features + labels (Model 2)
  * one investigation case with physics-Bayes attribution likelihoods
    (Model 3 calibration / evaluation, Model 4 test-selection experiments)
  * optionally the raw four data layers (telemetry, truth, records, faults)

Splits are made by complete simulation runs (never by rows). Environments:
  A  known families, training ranges     -> train / val / test by seed
  B  new schedules, noise, severities, unseen fault combinations -> test only
  C  fault mechanisms never seen in training -> test only

Usage:  python -m intelligence.training.dataset --out datasets --workers 6
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from industrial.schemas.plant import get_plant
from intelligence.fault_attribution.bayesian import PhysicsBayesAttributor
from intelligence.fault_attribution.hypotheses import FAULT_TO_FAMILY
from intelligence.features.windows import sensor_window_features, window_features
from intelligence.mass_balance.engine import BalanceEngine
from intelligence.physics.measurement import MeasurementModel, RecordsView, TelemetryStore, to_epoch
from intelligence.sensor_diagnostics.classifier import FAULT_TO_CLASS
from simulation.engine.export import run_scenario
from simulation.faults.faults import KNOWN_FAMILIES, UNSEEN_FAMILIES
from simulation.scenarios.scenario import generate_scenario

WINDOW_S = 1800.0
GRID_OFFSET_S = 600.0
CASE_WINDOWS = 8


def build_plan(seeds_train: int = 6, seeds_val: int = 2, seeds_test: int = 3, seeds_b: int = 2,
               seeds_c: int = 4, normal_factor: int = 2, duration_h: float = 8.0) -> list[dict[str, Any]]:
    plan = []
    fams_a = ["NORMAL"] + KNOWN_FAMILIES + ["MULTI_FAULT"]
    for fi, fam in enumerate(fams_a):
        k = normal_factor if fam == "NORMAL" else 1
        n = (seeds_train + seeds_val + seeds_test) * k
        for i in range(n):
            j = i // k
            split = "train" if j < seeds_train else ("val" if j < seeds_train + seeds_val else "test")
            plan.append({"env": "A", "family": fam, "seed": 10000 + 100 * fi + i, "split": split,
                         "duration_s": duration_h * 3600})
    for fi, fam in enumerate(fams_a):
        for i in range(seeds_b):
            plan.append({"env": "B", "family": fam, "seed": 20000 + 100 * fi + i, "split": "test",
                         "duration_s": duration_h * 3600})
    for fi, fam in enumerate(UNSEEN_FAMILIES):
        for i in range(seeds_c):
            plan.append({"env": "C", "family": fam, "seed": 30000 + 100 * fi + i, "split": "test",
                         "duration_s": duration_h * 3600})
    return plan


def _fault_fraction(f, t0: float, t1: float) -> float:
    a = max(t0, f.start_s)
    b = min(t1, f.end_s if f.end_s is not None else math.inf)
    return max(b - a, 0.0) / (t1 - t0)


def _sensor_class(f) -> str:
    base = FAULT_TO_CLASS.get(f.fault_type)
    if base is None:
        return "NORMAL"
    if base == "BIAS":
        v = f.params.get("bias_rel", f.params.get("offset_m", f.params.get("offset_c", f.params.get("offset_kgm3", 0))))
        return "BIAS_POS" if float(v) >= 0 else "BIAS_NEG"
    return base


def observable(plant, f, r: dict[str, Any], frac: float, a: float, b: float) -> bool:
    """Does fault f have a physically observable effect in window [a, b)?

    Labels that merely say "a fault is active" penalise detectors for faults that
    cannot leave a trace (a constant level offset cancels in an inventory difference;
    a biased meter that carries no flow). Observability follows the measurement
    physics; both label variants are kept and reported.
    """
    ft, tg = f.fault_type, f.target
    onset_in = a <= f.start_s < b
    meters = list(plant.meters())
    route = next((rid for rid in plant.routes if r.get(f"ctx_route_{rid}", 0) > 0), None)
    if ft in ("PIPELINE_LEAK", "TANK_LEAK"):
        return frac > 0 and r["true_loss_t"] >= 0.25
    if frac < 0.5 and not onset_in:
        return False
    if ft in ("METER_BIAS", "METER_DRIFT", "DENSITY_BIAS"):
        ms = [tg] if tg in meters else [m for m in meters if plant.sensors[m].density_sensor == tg]
        return any(r.get(f"raw_mass_{m}", 0.0) >= 1.0 for m in ms)
    if ft == "LEVEL_BIAS":
        return onset_in
    if ft == "LEVEL_DRIFT":
        return frac >= 0.5
    if ft == "TEMPERATURE_BIAS":
        asset = plant.sensors[tg].asset
        if asset in plant.tanks:
            return onset_in or (route is not None and plant.routes[route].source == asset)
        ms = [m for m in meters if plant.sensors[m].temperature_sensor == tg]
        return onset_in or any(r.get(f"raw_mass_{m}", 0.0) >= 1.0 for m in ms)
    if ft in ("SENSOR_STUCK", "SENSOR_DROPOUT", "SENSOR_NOISE"):
        if tg in meters and ft == "SENSOR_STUCK":
            return r.get(f"raw_mass_{tg}", 0.0) >= 1.0 or onset_in
        return frac >= 0.5
    if ft == "VALVE_PASSING":
        return frac >= 0.5 and route is not None and tg not in plant.routes[route].open_valves
    if ft == "PUMP_DEGRADATION":
        return frac >= 0.5 and r.get(f"raw_pump_running_{tg}", 0.0) >= 0.5
    return frac >= 0.5


def process_run(spec: dict[str, Any], save_raw_dir: str | None = None) -> dict[str, Any]:
    plant = get_plant()
    t_start = time.time()
    sc = generate_scenario(plant, spec["family"], spec["seed"], spec["env"], duration_s=spec["duration_s"],
                           dt_s=2.0, sample_interval_s=10.0)
    res = run_scenario(sc)
    run_id = sc.scenario_id
    if save_raw_dir:
        res.save(Path(save_raw_dir) / run_id)
    store = TelemetryStore(res.telemetry)
    rec = RecordsView(transfers=res.transfers, lab_samples=res.lab_samples, calibrations=res.calibrations)
    mm = MeasurementModel(plant, store, rec)
    eng = BalanceEngine(plant, mm)
    st = to_epoch(sc.start_time)
    grid = np.arange(GRID_OFFSET_S, sc.duration_s - 60.0 + 1e-6, WINDOW_S)
    ev = eng.evidence([st + g for g in grid])
    truth = res.truth
    tt = truth["t"].to_numpy()
    loss_cum = truth["physical_loss_cum_t"].to_numpy()
    rmap = {o.transfer_id: o.route for o in sc.operations}

    def true_loss(a_rel: float, b_rel: float) -> float:
        return float(np.interp(b_rel, tt, loss_cum) - np.interp(a_rel, tt, loss_cum))

    # ---------------- window rows
    wrows = window_features(plant, ev)
    for r in wrows:
        a, b = r["t0"] - st, r["t1"] - st
        act = [(f, _fault_fraction(f, a, b)) for f in sc.faults if f.category != "accounting"]
        # accounting faults are "active" during the affected transfer's movement
        for f in sc.faults:
            if f.category == "accounting":
                o = next((o for o in sc.operations if o.transfer_id == f.target), None)
                if o is not None:
                    dur = (o.target_volume_m3 or 0) / o.setpoint_m3h * 3600
                    fake = type("F", (), {"start_s": o.start_s, "end_s": o.start_s + dur + 300})
                    act.append((f, _fault_fraction(fake, a, b)))
        active = [f for f, frac in act if frac >= 0.5]
        r["true_loss_t"] = true_loss(a, b)
        obs = []
        for f, frac in act:
            if f.category == "accounting":
                if f.fault_type == "WRONG_QUANTITY":
                    o = next((o for o in sc.operations if o.transfer_id == f.target), None)
                    end_s = o.start_s + (o.target_volume_m3 or 0) / o.setpoint_m3h * 3600 if o else -1
                    if a <= end_s + 400 < b:
                        obs.append(f)
                elif frac >= 0.5:
                    obs.append(f)
            elif observable(plant, f, r, frac, a, b):
                obs.append(f)
        r["label_observable"] = int(bool(obs))
        r["label_family_observable"] = FAULT_TO_FAMILY[obs[0].fault_type] if obs else "NORMAL"
        r["observable_faults"] = json.dumps([f.fault_id for f in obs])
        r.update({"run_id": run_id, "env": spec["env"], "family": spec["family"], "split": spec["split"],
                  "label_anomaly": int(bool(active)),
                  "label_family": FAULT_TO_FAMILY[active[0].fault_type] if active else "NORMAL",
                  "label_targets": json.dumps([f"{FAULT_TO_FAMILY[f.fault_type]}:{rmap.get(f.target, f.target)}"
                                               for f in active]),
                  "true_loss_t": true_loss(a, b)})
    # ---------------- sensor-window rows
    srows = sensor_window_features(plant, ev, store)
    obs_by_window = {wr["window"]: set(json.loads(wr["observable_faults"])) for wr in wrows}
    for r in srows:
        a, b = r["t0"] - st, r["t1"] - st
        cls, cls_active = "NORMAL", "NORMAL"
        unknown = False
        for f in sc.faults:
            if f.target == r["sensor_id"] and (_fault_fraction(f, a, b) >= 0.5 or a <= f.start_s < b):
                if f.fault_type in ("SENSOR_NOISE",):
                    unknown = True
                if _fault_fraction(f, a, b) >= 0.5:
                    cls_active = _sensor_class(f)
                if f.fault_id in obs_by_window.get(r["window"], set()):
                    cls = _sensor_class(f)
        r.update({"run_id": run_id, "env": spec["env"], "family": spec["family"], "split": spec["split"],
                  "label": "UNKNOWN" if unknown else cls, "label_active": "UNKNOWN" if unknown else cls_active})

    # ---------------- investigation case
    fs = sc.faults[0].start_s if sc.faults else float(np.random.default_rng(spec["seed"]).uniform(3, 5)) * 3600
    if sc.faults and sc.faults[0].category == "accounting":
        o = next((o for o in sc.operations if o.transfer_id == sc.faults[0].target), None)
        fs = o.start_s if o else fs
    hi = [g for g in grid if g <= min(fs + 2.5 * 3600, sc.duration_s - 60)]
    sel = [g for g in hi if g >= hi[-1] - CASE_WINDOWS * WINDOW_S] if hi else list(grid[:CASE_WINDOWS + 1])
    cev = eng.evidence([st + g for g in sel])
    case = {"run_id": run_id, "env": spec["env"], "family": spec["family"], "split": spec["split"],
            "truth": [f"{FAULT_TO_FAMILY[f.fault_type]}:{rmap.get(f.target, f.target)}" for f in sc.faults],
            "truth_families": [FAULT_TO_FAMILY[f.fault_type] for f in sc.faults],
            "true_loss_t": true_loss(sel[0], sel[-1]), "t0": st + sel[0], "t1": st + sel[-1],
            "observable": bool(any(wr["label_observable"] for wr in wrows
                                   if sel[0] <= wr["t0"] - st and wr["t1"] - st <= sel[-1]))}
    variants = {"full": None}
    if spec["env"] == "C":
        variants["restricted"] = {"DENSITY_ERROR"}
    for vname, excl in variants.items():
        att = PhysicsBayesAttributor(plant, exclude_families=excl)
        r = att.attribute(cev)
        case[vname] = {
            "status": r.status, "chi2": r.chi2, "dof": r.dof,
            "singles": [{"hid": s.hypothesis.hid, "family": s.hypothesis.family, "ll": s.loglik,
                         "prior": s.hypothesis.prior} for s in r.scores],
            "pairs": [{"hids": p["hypotheses"], "families": p["families"], "ll": p["loglik"],
                       "logpost": p["logpost"]} for p in r.pairs],
            "ranking": [s.hypothesis.hid for s in r.ranked(10)],
            "hyp_dist": r.hypothesis_distribution(),
            "family_dist": r.family_distribution(),
            "category": r.category_probs,
            "loss_est": r.estimated_physical_loss_t, "loss_cond": r.conditional_loss_t,
            "loss_iv": r.physical_loss_interval_t,
            "loss_param_sd": (math.sqrt(max((r.conditional_loss_sd_t or 0) ** 2
                                            - (0.25 * (r.conditional_loss_t or 0)) ** 2, 0.0))
                              if r.conditional_loss_t is not None else None),
            "p_physical_loss": r.category_probs.get("physical_loss", 0.0),
        }
    # case-level features for the learned (non-physics) attribution baseline: mean/max over windows
    cw = pd.DataFrame(window_features(plant, cev))
    num = cw.select_dtypes("number")
    case["features_mean"] = num.mean().to_dict()
    case["features_max"] = num.abs().max().to_dict()
    return {"run_id": run_id, "windows": wrows, "sensors": srows, "case": case,
            "seconds": round(time.time() - t_start, 1)}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Generate the OILINTELLIGENCE synthetic dataset")
    ap.add_argument("--out", default="datasets")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--quick", action="store_true", help="small plan for smoke testing")
    ap.add_argument("--save-raw", type=int, default=20, help="save full four-layer data for up to N test runs (one per env/family)")
    a = ap.parse_args(argv)
    out = Path(a.out)
    (out / "training").mkdir(parents=True, exist_ok=True)
    plan = build_plan(seeds_train=2, seeds_val=1, seeds_test=1, seeds_b=1, seeds_c=1, normal_factor=1) if a.quick \
        else build_plan()
    plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:12]
    print(f"Generating {len(plan)} runs (plan {plan_hash}) with {a.workers} workers", flush=True)
    windows, sensors, cases = [], [], []
    t0 = time.time()
    raw_dir = str(out / "synthetic")
    # keep the full four-layer data for one held-out (test) run of each environment/family
    keep: set[int] = set()
    seen: set[tuple[str, str]] = set()
    for i, spec in enumerate(plan):
        key = (spec["env"], spec["family"])
        if spec["split"] == "test" and key not in seen and len(keep) < a.save_raw:
            seen.add(key)
            keep.add(i)
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(process_run, spec, raw_dir if i in keep else None): spec for i, spec in enumerate(plan)}
        for k, fut in enumerate(as_completed(futs), 1):
            spec = futs[fut]
            try:
                r = fut.result()
            except Exception as e:  # keep going; record failure
                print(f"  ! run {spec} failed: {e!r}", flush=True)
                continue
            windows += r["windows"]
            sensors += r["sensors"]
            cases.append(r["case"])
            if k % 10 == 0 or k == len(plan):
                print(f"  {k}/{len(plan)} runs  ({time.time() - t0:.0f}s)", flush=True)
    wdf = pd.DataFrame(windows)
    sdf = pd.DataFrame(sensors)
    wdf.to_parquet(out / "training" / "windows.parquet", index=False)
    sdf.to_parquet(out / "training" / "sensor_windows.parquet", index=False)
    with open(out / "training" / "cases.jsonl", "w", encoding="utf-8") as fh:
        for c in cases:
            fh.write(json.dumps(c, default=float) + "\n")
    # the plant definition is hashed separately: models trained on one plant.yaml describe that plant only
    plant_hash = hashlib.sha256(json.dumps(get_plant().model_dump(mode="json"), sort_keys=True).encode()).hexdigest()[:12]
    meta = {"plan_hash": plan_hash, "plant_hash": plant_hash, "runs": len(cases), "windows": len(wdf), "sensor_windows": len(sdf),
            "window_s": WINDOW_S, "generated_s": round(time.time() - t0, 1),
            "envs": wdf.groupby("env")["run_id"].nunique().to_dict() if len(wdf) else {},
            "plan": plan}
    (out / "training" / "dataset_meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"Done: {len(cases)} runs, {len(wdf)} windows, {len(sdf)} sensor-windows in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
