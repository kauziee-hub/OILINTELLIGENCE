"""Agent evaluation (section 13.5): run the full LangGraph investigation on seeded fault
scenarios, then close the loop with sampled field-verification outcomes.

Measured per case: initial top-1 / top-3 inclusion of the true cause, the final state after
verification, number of checks, grounding of the narrative, tool failures, and - the safety
metric - whether any case was resolved on a wrong cause. Behavioural checks cover duplicate
events and missing critical evidence.
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Any

import numpy as np

from agents.graph.workflow import Investigator
from agents.llm.provider import TemplateProvider
from agents.tools.backend import InMemoryBackend
from industrial.schemas.plant import get_plant
from intelligence.active_diagnosis.tests import action_by_id, sample_outcome, true_hypotheses
from intelligence.physics.measurement import to_epoch
from simulation.engine.export import run_scenario
from simulation.scenarios.scenario import generate_scenario

FAMILIES = ["PIPELINE_LEAK", "TANK_LEAK", "METER_BIAS", "LEVEL_DRIFT", "VALVE_PASSING", "MISSING_TRANSACTION",
            "NORMAL", "DENSITY_BIAS"]       # DENSITY_BIAS is a family the models never saw
RESOLVED = ("RESOLVED_PENDING_APPROVAL", "CLOSED")


def _case_time(sc) -> float:
    if not sc.faults:
        return min(5 * 3600.0, sc.duration_s - 60)
    f = sc.faults[0]
    start = f.start_s
    if f.category == "accounting":
        o = next((o for o in sc.operations if o.transfer_id == f.target), None)
        start = o.start_s if o else start
    return min(start + 2.5 * 3600.0, sc.duration_s - 60)


def evaluate_case(family: str, seed: int, env: str = "A", max_checks: int = 5) -> dict[str, Any]:
    plant = get_plant()
    sc = generate_scenario(plant, family, seed, env, duration_s=10 * 3600, dt_s=2.0, sample_interval_s=10.0)
    run = run_scenario(sc)
    st = to_epoch(sc.start_time)
    t1 = st + _case_time(sc)
    be = InMemoryBackend(run, plant=plant)
    be.set_now(t1)
    rmap = {o.transfer_id: o.route for o in sc.operations}
    truth = true_hypotheses(plant, sc.faults, rmap)
    truth_ids = [h.hid for h in truth]
    inv = Investigator(be, llm=TemplateProvider())
    w0 = time.perf_counter()
    s = inv.investigate({"event_id": f"EVAL-{sc.scenario_id}", "event_type": "BALANCE_ANOMALY",
                         "assets": ["MF-DIS"], "t1": t1})
    wall_s = time.perf_counter() - w0
    rep = s.get("report") or {}
    # evidence completeness: the items an engineer needs before reviewing a loss case
    required = {"balance_residual": rep.get("observed_residual_tonnes") is not None,
                "residual_uncertainty": rep.get("residual_uncertainty_tonnes") is not None,
                "ranked_causes": bool(rep.get("candidate_hypotheses")),
                "evidence_register": bool(rep.get("evidence")),
                "telemetry_summary": bool(s.get("telemetry_summary")),
                "next_check_or_resolution": bool(rep.get("recommended_check")) or s.get("status") in RESOLVED,
                "cited_narrative": "[E1]" in (rep.get("narrative") or "")}
    cands = [h["hypothesis_id"] for h in rep.get("candidate_hypotheses", [])]
    rec = {"scenario": sc.scenario_id, "family": family, "env": env, "truth": truth_ids,
           "initial_status": s.get("status"), "initial_top": cands[:3],
           "initial_top1": bool(cands[:1] and cands[0] in truth_ids) if truth_ids else (not cands or cands[0] == "NORMAL"),
           "initial_top3": bool(set(cands[:3]) & set(truth_ids)) if truth_ids else None,
           "grounded": bool((rep.get("grounding") or {}).get("passed", True)),
           "evidence_completeness": round(sum(required.values()) / len(required), 3),
           "missing_evidence_items": [k for k, v in required.items() if not v],
           "investigation_wall_s": round(wall_s, 2),
           "tool_calls": len(s.get("tool_calls", [])),
           "tool_errors": sum(1 for c in s.get("tool_calls", []) if c.get("status") == "ERROR")}
    rng = np.random.default_rng(seed)
    checks = 0
    while s.get("status") == "AWAITING_VERIFICATION" and s.get("recommended_check") and checks < max_checks:
        aid = s["recommended_check"]["action_id"]
        outcome = sample_outcome(plant, action_by_id(plant, aid), truth, rng)
        s = inv.review(s["investigation_id"], {"decision": "VERIFICATION_RESULT", "action_id": aid, "outcome": outcome,
                                               "reviewer": "eval", "role": "engineer"})
        checks += 1
    post = sorted((s.get("posterior") or {}).items(), key=lambda kv: -kv[1])
    top, ptop = (post[0] if post else (None, 0.0))
    resolved = s.get("status") in RESOLVED
    rec.update(final_status=s.get("status"), checks=checks, final_top=top, final_top_p=round(float(ptop), 3),
               resolved=resolved, resolved_correct=bool(resolved and top in truth_ids),
               wrong_resolution=bool(resolved and truth_ids and top not in truth_ids and family != "DENSITY_BIAS"),
               confident_wrong=bool(truth_ids and top not in truth_ids and ptop >= 0.9))
    return rec


def behaviour_checks(seed: int = 41) -> dict[str, bool]:
    plant = get_plant()
    sc = generate_scenario(plant, "PIPELINE_LEAK", seed, "A", duration_s=8 * 3600, dt_s=2.0, sample_interval_s=10.0)
    run = run_scenario(sc)
    st = to_epoch(sc.start_time)
    t1 = st + _case_time(sc)
    be = InMemoryBackend(run, plant=plant)
    be.set_now(t1)
    inv = Investigator(be, llm=TemplateProvider())
    ev = {"event_id": "B1", "event_type": "BALANCE_ANOMALY", "assets": ["MF-DIS"], "t1": t1}
    first = inv.investigate(ev)
    dup = inv.investigate({**ev, "event_id": "B2"})
    tel = run.telemetry
    stripped = type(run)(**{**run.__dict__, "telemetry": tel[tel.sensor_id != "LT-002"]})
    be2 = InMemoryBackend(stripped, plant=plant)
    be2.set_now(t1)
    nd = Investigator(be2, llm=TemplateProvider()).investigate({**ev, "event_id": "B3"})
    return {"duplicate_event_attached_to_open_case": dup.get("status") == "DUPLICATE"
            and dup.get("duplicate_of") == first.get("investigation_id"),
            "missing_level_gauge_requests_data": nd.get("status") == "NEEDS_DATA"
            and not (nd.get("report") or {}).get("candidate_hypotheses"),
            "report_requires_engineer_approval": bool((first.get("report") or {}).get("requires_engineer_approval"))}


def run(seeds: tuple[int, ...] = (11, 12)) -> dict[str, Any]:
    cases = [evaluate_case(f, s) for f in FAMILIES for s in seeds]
    known = [c for c in cases if c["family"] not in ("NORMAL", "DENSITY_BIAS")]
    n = max(len(known), 1)
    summary = {
        "n_cases": len(cases),
        "known_fault_cases": len(known),
        "initial_top1_rate": round(sum(c["initial_top1"] for c in known) / n, 3),
        "initial_top3_rate": round(sum(bool(c["initial_top3"]) for c in known) / n, 3),
        "resolved_rate": round(sum(c["resolved"] for c in known) / n, 3),
        "resolved_correct_rate": round(sum(c["resolved_correct"] for c in known) / n, 3),
        "wrong_resolutions": int(sum(c["wrong_resolution"] for c in cases)),
        "confident_wrong_cases": int(sum(c["confident_wrong"] for c in cases)),
        "mean_checks_known": round(float(np.mean([c["checks"] for c in known])) if known else 0.0, 2),
        "grounding_pass_rate": round(sum(c["grounded"] for c in cases) / len(cases), 3),
        "evidence_completeness_mean": round(float(np.mean([c["evidence_completeness"] for c in cases])), 3),
        "investigation_wall_s_median": round(float(np.median([c["investigation_wall_s"] for c in cases])), 2),
        "tool_error_rate": round(sum(c["tool_errors"] for c in cases) / max(sum(c["tool_calls"] for c in cases), 1), 4),
        "final_status_counts": dict(Counter(c["final_status"] for c in cases)),
        "normal_cases_final_status": [c["final_status"] for c in cases if c["family"] == "NORMAL"],
        # no training run contains a density-meter fault; the physics catalogue still has the hypothesis
        "unseen_cases": [{**{k: c[k] for k in ("scenario", "truth", "final_status", "final_top", "final_top_p")},
                          "correct": c["final_top"] in c["truth"]}
                         for c in cases if c["family"] == "DENSITY_BIAS"],
    }
    return {"summary": summary, "behaviour": behaviour_checks(), "cases": cases}


if __name__ == "__main__":
    import json
    print(json.dumps(run(seeds=(11,))["summary"], indent=2))
