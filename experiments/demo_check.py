"""Offline proof of the demo scenarios: each one is simulated and pushed through the same pipeline the
live platform runs (monitor alarm rule with on-delay -> investigation -> auto-refresh -> field checks
sampled from each check's sensitivity), then scored against the injected truth.

    python -m experiments.demo_check            # all scenarios
    python -m experiments.demo_check pipeline_leak meter_bias
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

RESULTS = Path(__file__).resolve().parent / "results" / "demo_check.json"


def _monitor_flags(de, be, t: float, window: float = 1800.0) -> tuple[bool, bool]:
    """(anomaly, immediate, row z-scores) exactly as the runtime Monitor.evaluate decides them."""
    from intelligence.features.windows import window_features
    from intelligence.uncertainty.propagation import window_statistics
    rec = be.records(t - 30 * 86400, t)
    tel = be.telemetry(de.telemetry_start(rec, t - window, t), t + de.READ_AHEAD_S)
    ctx = de.context(tel, rec)
    ev = ctx.engine.evidence([t - window, t])
    stats = window_statistics(ev)[0]
    feats = pd.DataFrame(window_features(de.plant, ev))
    chi = float(max(feats["phys_log_p"].iloc[0], feats["phys_max_abs_z_cv"].iloc[0] / 4 * 3))
    xgb = de.models.get("xgboost_physics")
    xgb_alarm = bool(xgb is not None and float(xgb.score(feats)[0]) >= xgb.threshold)
    quality = {k: v for k, v in ev.windows[0].quality.items() if v["stuck"] or v["dropout"]}
    anomaly = bool(chi >= 3.0 or (xgb_alarm and stats["max_abs_z"] > 2.5) or quality)
    immediate = bool(quality) or stats["max_abs_z"] >= 6.0 or chi >= 8.0
    return anomaly, immediate, stats["z"]


def check(key: str, sample_s: float = 60.0, seed: int = 4242) -> dict[str, Any]:
    from agents.graph.workflow import Investigator
    from agents.llm.provider import TemplateProvider
    from agents.tools.backend import InMemoryBackend
    from industrial.schemas.plant import get_plant
    from intelligence.active_diagnosis.tests import action_by_id, sample_outcome, true_hypotheses
    from intelligence.physics.measurement import to_epoch
    from simulation.engine.export import run_scenario
    from simulation.scenarios.demo import BY_KEY, build_demo_scenario, inject_s

    t_wall = time.time()
    plant = get_plant()
    spec = BY_KEY[key]
    sc = build_demo_scenario(plant, key, seed=seed, dt_s=1.0, sample_interval_s=sample_s)
    run = run_scenario(sc, plant)
    st = to_epoch(sc.start_time)
    inj = inject_s(key)
    be = InMemoryBackend(run, plant=plant)
    de = be.engine
    from intelligence.alarm import AlarmOnDelay, SlowShiftCusum, instrument_memory_s
    events, on_delay, cusum = [], AlarmOnDelay(2, instrument_memory_s(plant)), SlowShiftCusum()
    for t in np.arange(st + 2100.0, st + sc.duration_s - 60, 300.0):
        be.set_now(float(t))
        anomaly, immediate, z = _monitor_flags(de, be, float(t))
        slow = cusum.update(z, float(t))
        if on_delay.update(anomaly or bool(slow), immediate or bool(slow), float(t)):
            events.append(float(t))
    out: dict[str, Any] = {"key": key, "title": spec["title"], "expected": spec["expected"]["hypothesis"],
                           "events": len(events)}
    if inj is None:                                  # normal scenario: any confirmed event is a false alarm
        out.update(false_alarm_events=len(events), passed=len(events) == 0)
        if events:
            be.set_now(events[0])
            s = Investigator(be, llm=TemplateProvider()).investigate(
                {"event_id": "DEMO", "event_type": "BALANCE_ANOMALY", "assets": ["MF-DIS"], "t1": events[0]})
            post = s.get("posterior") or {}
            top = max(post, key=post.get) if post else None
            out.update(investigation_top=top, passed=top in (None, "NORMAL"))
        out["seconds"] = round(time.time() - t_wall, 1)
        return out
    before = [e for e in events if e < st + inj]
    after = [e for e in events if e >= st + inj]
    out["false_alarm_events"] = len(before)
    if not after:
        out.update(detected=False, passed=False, seconds=round(time.time() - t_wall, 1))
        return out
    t_det = after[0]
    out["detected"] = True
    out["latency_min"] = round((t_det - st - inj) / 60.0, 1)
    be.set_now(t_det)
    inv = Investigator(be, llm=TemplateProvider())
    s = inv.investigate({"event_id": "DEMO", "event_type": "BALANCE_ANOMALY", "assets": ["MF-DIS"], "t1": t_det})
    iid = s["investigation_id"]
    out["first_status"] = s.get("status")
    # the live monitor re-analyses an open case after 30 min of new evidence; a presenter can also press Re-analyse
    t_ref = min(t_det + 3600.0, st + sc.duration_s - 60)
    be.set_now(t_ref)
    s = inv.refresh(iid, t_ref)
    cands = [h["hypothesis_id"] for h in (s.get("report") or {}).get("candidate_hypotheses", [])]
    post = s.get("posterior") or {}
    exp = spec["expected"]["hypothesis"]
    out.update(status_after_refresh=s.get("status"), top3=cands[:3],
               top3_p=[round(post.get(h, 0.0), 3) for h in cands[:3]], top1_correct=bool(cands[:1] == [exp]),
               top3_correct=exp in cands[:3],
               estimated_loss_t=(s.get("report") or {}).get("estimated_physical_loss_tonnes"),
               true_loss_t=round(float(np.interp(t_ref - st, run.truth["t"], run.truth["physical_loss_cum_t"])), 3))
    if spec["expected"]["kind"] == "unknown":
        known = {h: p for h, p in post.items() if h not in ("UNKNOWN", "NORMAL")}
        top_known = max(known.values(), default=0.0)
        out.update(top_known_p=round(top_known, 3), passed=top_known < 0.6)
        out["seconds"] = round(time.time() - t_wall, 1)
        return out
    rmap = {o.transfer_id: o.route for o in sc.operations}
    truth = true_hypotheses(plant, sc.faults, rmap)
    rng = np.random.default_rng(seed)
    checks = []
    # checks back to back, as the Demo page's "Run field checks" does
    while s.get("status") == "AWAITING_VERIFICATION" and s.get("recommended_check") and len(checks) < 5:
        aid = s["recommended_check"]["action_id"]
        y = sample_outcome(plant, action_by_id(plant, aid), truth, rng)
        s = inv.review(iid, {"decision": "VERIFICATION_RESULT", "action_id": aid, "outcome": y, "reviewer": "demo",
                             "role": "engineer"})
        checks.append(f"{aid}={y}")
    post = s.get("posterior") or {}
    final = max(post, key=post.get) if post else None
    out.update(checks=checks, final_status=s.get("status"), final_top=final,
               final_p=round(post.get(final, 0.0), 3) if final else None,
               resolved_correct=bool(s.get("status") == "RESOLVED_PENDING_APPROVAL" and final == exp))
    out["passed"] = bool(out["top3_correct"] and (out["resolved_correct"] or out["top1_correct"]))
    out["seconds"] = round(time.time() - t_wall, 1)
    return out


def main(argv: list[str] | None = None) -> list[dict[str, Any]]:
    from simulation.scenarios.demo import DEMO_SCENARIOS
    keys = (argv if argv is not None else sys.argv[1:]) or [s["key"] for s in DEMO_SCENARIOS]
    with ProcessPoolExecutor(max_workers=min(3, len(keys))) as ex:
        res = list(ex.map(check, keys))
    for r in res:
        print(json.dumps(r, default=str))
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    old.update({r["key"]: r for r in res})
    RESULTS.write_text(json.dumps(old, indent=1, default=str), encoding="utf-8")
    return res


if __name__ == "__main__":
    main()
