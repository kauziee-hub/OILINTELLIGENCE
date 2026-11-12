"""Run the research experiments (section 17) and write the results report.

    oil-experiments                      # reuse datasets/training if present
    oil-experiments --regenerate         # regenerate the synthetic dataset first
    oil-experiments --quick              # small smoke-test configuration
    oil-experiments --report-only        # rewrite docs/research/results.md from the saved JSON

Outputs: experiments/results/results.json (training / Models 1-4), experiments/results/experiments.json
(coverage, ablation, agent evaluation, simulator and API checks) and docs/research/results.md.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "experiments" / "results"


def simulator_checks(hours: float = 6.0) -> dict[str, Any]:
    """Conservation in the simulator and telemetry completeness under normal connectivity."""
    from industrial.schemas.plant import get_plant
    from simulation.engine.export import run_scenario
    from simulation.scenarios.scenario import generate_scenario
    plant = get_plant()
    sc = generate_scenario(plant, "NORMAL", 4242, "A", duration_s=hours * 3600, dt_s=2.0, sample_interval_s=10.0)
    res = run_scenario(sc)
    tel = res.telemetry
    expected = len(plant.sensors) * int(sc.duration_s // sc.sample_interval_s)
    good = int((tel["quality"] == "GOOD").sum())
    present = tel.groupby("sensor_id").size()
    return {"max_abs_conservation_error_kg": float(res.truth["conservation_error_kg"].abs().max()),
            "total_mass_t": float(res.truth[[c for c in res.truth.columns if c.endswith(".mass_t")]].iloc[0].sum()),
            "telemetry_completeness": round(min(good / max(expected, 1), 1.0), 4),
            "sensors_reporting": int(len(present)), "sensors_configured": len(plant.sensors)}


def api_latency(seconds: float = 20.0) -> dict[str, Any]:
    """P95 latency of standard read endpoints and of a sensor-history query (in-process server)."""
    import os
    import secrets
    import sys
    import tempfile
    d = tempfile.mkdtemp()
    os.environ.update(DATABASE_URL=f"sqlite:///{d}/a.db", TRUTH_DATABASE_URL=f"sqlite:///{d}/t.db", AUTH_REQUIRED="0",
                      SIM_SPEED="60", RATE_LIMIT_PER_MINUTE="1000000",
                      **{f"SEED_{r}_PASSWORD": secrets.token_urlsafe(12) for r in ("ADMIN", "ENGINEER", "OPERATOR", "VIEWER")})
    sys.path.insert(0, str(ROOT / "apps" / "backend"))
    from fastapi.testclient import TestClient

    from app.main import app
    lat: dict[str, list[float]] = {}
    with TestClient(app) as c:
        time.sleep(5)
        paths = ["/api/v1/twin/state", "/api/v1/telemetry/latest", "/api/v1/assets", "/api/v1/investigations",
                 "/api/v1/events/anomalies", "/api/v1/simulation/status"]
        hist = "/api/v1/telemetry/history?sensor_id=LT-001&sensor_id=FM-001&sensor_id=PT-002"
        t_end = time.time() + seconds
        while time.time() < t_end:
            for p in paths + [hist]:
                t0 = time.perf_counter()
                r = c.get(p)
                if r.status_code == 200:
                    lat.setdefault("history" if p == hist else "standard", []).append((time.perf_counter() - t0) * 1000)
    q = lambda v, p: round(float(np.percentile(v, p)), 1) if v else None  # noqa: E731
    return {"standard_p50_ms": q(lat.get("standard", []), 50), "standard_p95_ms": q(lat.get("standard", []), 95),
            "history_p95_ms": q(lat.get("history", []), 95), "requests": sum(len(v) for v in lat.values()),
            "note": "in-process TestClient, SQLite, live simulation at 60x; excludes network"}


# ------------------------------------------------------------------ report
def _fmt(v: Any, pct: bool = False) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    if pct:
        return f"{100 * v:.1f}%"
    return f"{v:.3f}" if isinstance(v, float) else str(v)


def kpi_rows(res: dict[str, Any], ex: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    m1, m2, m3 = res["model1_anomaly_detection"], res["model2_sensor_diagnostics"], res["model3_fault_attribution"]
    loss = res["loss_estimation"]
    sim, ag = ex.get("simulator", {}), (ex.get("agent_evaluation") or {}).get("summary", {})
    abl = (ex.get("ablation") or {}).get("variants", {}).get("full_model", {})
    a_cal, a_unc = m3["A"]["physics_bayes_calibrated"], m3["A"]["physics_bayes_uncalibrated"]
    md4 = res["model4_active_diagnosis"]
    api = ex.get("api") or {}

    def ok(b):
        return "MET" if b else "NOT MET"
    rows = [
        ("Normal mass-balance conservation (simulator)", "within numerical tolerance",
         f"max error {sim.get('max_abs_conservation_error_kg', float('nan')):.2e} kg", ok(sim.get("max_abs_conservation_error_kg", 1) < 1e-2)),
        ("Physical loss estimation MAE", "< 10% of injected loss",
         f"{loss['all_physical_loss_cases']['mape_pct']:.1f}% all cases; "
         f"{loss['cases_where_loss_is_reported']['mape_pct']:.1f}% where reported (median "
         f"{loss['cases_where_loss_is_reported']['median_abs_pct_error']:.1f}%)", ok(loss["all_physical_loss_cases"]["mape_pct"] < 10)),
        ("Measurement fault detection recall", ">= 90%", _fmt(m2["A"]["fault_detection_recall"], True), ok(m2["A"]["fault_detection_recall"] >= 0.9)),
        ("Root-cause top-3 recall (env A, all test cases)", ">= 90%", _fmt(a_cal["top3_hypothesis"], True),
         ok(a_cal["top3_hypothesis"] >= 0.9)),
        ("False-positive anomaly rate (xgboost_physics, env A)", "<= 5 / 100 normal",
         f"{m1['A']['xgboost_physics']['false_alarms_per_100_normal']:.1f} (chi2 detector "
         f"{m1['A']['statistical_chi2']['false_alarms_per_100_normal']:.1f})",
         ok(m1["A"]["xgboost_physics"]["false_alarms_per_100_normal"] <= 5)),
        ("Data completeness (normal connectivity)", ">= 98%", _fmt(sim.get("telemetry_completeness"), True),
         ok((sim.get("telemetry_completeness") or 0) >= 0.98)),
        ("Sensor fault localisation", ">= 85%", _fmt(m2["A"]["fault_localisation_accuracy"], True),
         ok(m2["A"]["fault_localisation_accuracy"] >= 0.85)),
        ("Fault attribution macro-F1 (env A)", ">= 0.85", _fmt(a_cal["macro_f1_family"]), ok(a_cal["macro_f1_family"] >= 0.85)),
        ("Probability calibration", "Brier better than uncalibrated",
         f"Brier {a_cal['brier']:.3f} vs {a_unc['brier']:.3f}; log-loss {a_cal['log_loss']:.3f} vs {a_unc['log_loss']:.3f}",
         ok(a_cal["brier"] < a_unc["brier"])),
        ("Diagnostic test selection", "EIG better than random",
         f"{md4['first_test_expected_information_gain_bits']['selected']:.3f} vs "
         f"{md4['first_test_expected_information_gain_bits']['random_choice']:.3f} bits",
         ok(md4["first_test_expected_information_gain_bits"]["selected"] > md4["first_test_expected_information_gain_bits"]["random_choice"])),
        ("Investigation evidence completeness", ">= 95%", _fmt(ag.get("evidence_completeness_mean"), True),
         ok((ag.get("evidence_completeness_mean") or 0) >= 0.95)),
        ("Unsupported confident diagnoses", "zero (safety-critical cases)",
         f"agent: {ag.get('confident_wrong_cases', 'n/a')} confident-wrong, {ag.get('wrong_resolutions', 'n/a')} wrong "
         f"resolutions; unseen faults: {abl.get('C_confident_known_cause', 'n/a')} confident known-cause verdicts",
         ok(ag.get("confident_wrong_cases", 1) == 0 and ag.get("wrong_resolutions", 1) == 0
            and abl.get("C_confident_known_cause", 1) == 0)),
        ("Investigation preparation time", "50% better than manual baseline",
         f"median {ag.get('investigation_wall_s_median', 'n/a')} s per automated investigation", "NOT MEASURED (no manual baseline)"),
        ("Standard API latency", "P95 <= 500 ms", f"{api.get('standard_p95_ms', 'n/a')} ms",
         ok((api.get("standard_p95_ms") or 1e9) <= 500) if api else "NOT RUN"),
        ("Sensor history query", "<= 2 s", f"P95 {api.get('history_p95_ms', 'n/a')} ms",
         ok((api.get("history_p95_ms") or 1e9) <= 2000) if api else "NOT RUN"),
        ("Dashboard update latency / twin FPS", "<= 2 s / >= 30 FPS", "browser-side; not measured by this script",
         "NOT MEASURED"),
    ]
    return rows


def write_report(res: dict[str, Any], ex: dict[str, Any], path: Path) -> None:
    L: list[str] = []
    a = L.append
    a("# OILINTELLIGENCE - research results")
    a("")
    a(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by `oil-experiments`. Model version "
      f"`{res['version']}`; dataset {res['counts']['windows']} balance windows, {res['counts']['sensor_windows']} "
      f"sensor windows and {res['counts']['cases']} investigation cases (env A {res['counts']['cases_by_env']['A']}, "
      f"B {res['counts']['cases_by_env']['B']}, C {res['counts']['cases_by_env']['C']}). All data are synthetic; "
      "every number below comes from the simulator and must not be read as plant performance.")
    a("")
    a("Environments: **A** = same distribution as training (held-out runs); **B** = different instrument noise and "
      "unseen fault combinations; **C** = fault families never seen in training (density bias, sensor noise, "
      "wrong ticket quantity). Splits are by simulation run, never by window.")
    a("")
    a("## 1. KPI scorecard (section 14 targets)")
    a("")
    a("| KPI | Target | Result | Status |")
    a("|---|---|---|---|")
    for k, t, r, s in kpi_rows(res, ex):
        a(f"| {k} | {t} | {r} | {s} |")
    a("")
    a("## 2. Experiment 1 & 2 - detection: threshold vs ML, raw vs physics features")
    a("")
    a("Window-level detection against observable-effect labels (a fault counts once its effect is physically "
      "visible in the window). FA/100 = false alarms per 100 normal windows; latency in 30-min windows.")
    a("")
    for env in ("A", "B", "C"):
        a(f"**Environment {env}**")
        a("")
        a("| Detector | Precision | Recall | F1 | PR-AUC | FA/100 | Episode recall | Latency (w) |")
        a("|---|---|---|---|---|---|---|---|")
        for name, m in res["model1_anomaly_detection"][env].items():
            a(f"| {name} | {m['precision']:.2f} | {m['recall']:.2f} | {m['f1']:.2f} | {m['pr_auc']:.2f} | "
              f"{m['false_alarms_per_100_normal']:.1f} | {m.get('episode_detection_rate', float('nan')):.2f} | "
              f"{m.get('mean_detection_latency_windows', float('nan')):.2f} |")
        a("")
    m1 = res["model1_anomaly_detection"]
    better = [env for env, d in m1.items() if d["xgboost_physics"]["f1"] > d["xgboost_raw"]["f1"]
              and d["isolation_forest"]["f1"] > d["isolation_forest_raw"]["f1"]]
    best = lambda d, k, f=max: f(d, key=lambda n: d[n][k])  # noqa: E731
    a(f"Physics-derived features raise the F1 of both learned detectors in {len(better)}/{len(m1)} environments "
      f"({', '.join(better)}). Most precise detector per environment: "
      + ", ".join(f"{env} {best(d, 'precision')}" for env, d in m1.items()) + "; best recall: "
      + ", ".join(f"{env} {best(d, 'recall')}" for env, d in m1.items()) + "; best episode coverage: "
      + ", ".join(f"{env} {best(d, 'episode_detection_rate')}" for env, d in m1.items()) + ".")
    a("")
    a("## 3. Instrument fault classifier (Model 2)")
    a("")
    a("| Env | Macro-F1 | Fault recall | Localisation | Recall by class |")
    a("|---|---|---|---|---|")
    for env, m in res["model2_sensor_diagnostics"].items():
        a(f"| {env} | {m['macro_f1']:.3f} | {m['fault_detection_recall']:.2f} | {m['fault_localisation_accuracy']:.2f} | "
          + ", ".join(f"{k} {v:.2f}" for k, v in m["per_class_recall"].items()) + " |")
    a("")
    a("## 4. Experiment 3 - fault attribution (Model 3)")
    a("")
    cal = res["model3_calibration"]
    a(f"Calibration fitted on train+validation cases: temperature T = {cal['temperature']}, prior mixing "
      f"eps = {cal['prior_mix']}, loss model relative error {cal['loss_model_rel_error']}.")
    a("")
    a("| Env | Model | Top-1 family | Top-3 family | Top-1 cause | Top-3 cause | Macro-F1 | Log-loss | Brier | ECE |")
    a("|---|---|---|---|---|---|---|---|---|---|")
    for env, models in res["model3_fault_attribution"].items():
        for name, m in models.items():
            if "top1_family" not in m:
                continue
            a(f"| {env} | {name} | {m['top1_family']:.3f} | {m['top3_family']:.3f} | "
              f"{_fmt(m.get('top1_hypothesis'))} | {_fmt(m.get('top3_hypothesis'))} | {m['macro_f1_family']:.3f} | "
              f"{m['log_loss']:.3f} | {m['brier']:.3f} | {m['ece']:.3f} |")
    a("")
    a("`*_observable` rows restrict scoring to cases where the injected fault had a physically observable effect "
      "inside the case window.")
    m3 = res["model3_fault_attribution"]
    losses = []
    for env, d in m3.items():
        pb = d.get("physics_bayes_calibrated", {})
        for xn in ("xgboost_raw_features", "xgboost_phys_features"):
            for k in ("top1_family", "top3_family", "macro_f1_family"):
                if k in d.get(xn, {}) and k in pb and d[xn][k] > pb[k]:
                    losses.append(f"{env} {k} vs {xn} ({pb[k]:.3f} < {d[xn][k]:.3f})")
    a("The topology-aware physics model scores higher than both non-topological XGBoost classifiers on top-1, "
      "top-3 and macro-F1 in every environment" + (", except: " + "; ".join(losses) + "." if losses else ".")
      + (f" On unseen fault families (env C) the XGBoost classifiers score zero by construction; the physics model's "
         f"top family is right in {m3['C']['physics_bayes_calibrated']['top1_family']:.0%} of cases and it gives "
         f"{m3['C']['physics_bayes_calibrated'].get('confident_wrong_known_cause', 'n/a')} confident wrong "
         "known-cause verdicts." if "C" in m3 else ""))
    a("")
    le = res["loss_estimation"]
    a("**Loss estimation** (physical-loss cases):")
    a("")
    a("| Subset | n | MAE (t) | Bias (t) | MAPE | Median abs. % error | 90% interval coverage |")
    a("|---|---|---|---|---|---|---|")
    for k, lab in (("all_physical_loss_cases", "all physical-loss cases"), ("cases_where_loss_is_reported", "cases where a loss is reported")):
        m = le[k]
        a(f"| {lab} | {m['n']} | {m['mae']:.2f} | {m['bias']:.2f} | {m['mape_pct']:.1f}% | {m['median_abs_pct_error']:.1f}% | "
          f"{100 * m['interval_coverage']:.0f}% |")
    a("")
    a(f"A loss figure is reported only when P(physical loss) >= 0.6 (fraction reported: {le['reported_fraction']:.2f}).")
    a("")
    a("## 5. Experiment 4 - active diagnosis (Model 4)")
    a("")
    a("| Policy | Mean checks | Mean cost | Mean hours | Final accuracy | Resolution rate |")
    a("|---|---|---|---|---|---|")
    for name, m in res["model4_active_diagnosis"].items():
        if isinstance(m, dict) and "mean_tests" in m:
            a(f"| {name} | {m['mean_tests']:.2f} | {m['mean_cost']:.2f} | {m['mean_hours']:.2f} | "
              f"{m['final_accuracy']:.3f} | {m['resolution_rate']:.3f} |")
    eig = res["model4_active_diagnosis"]["first_test_expected_information_gain_bits"]
    a("")
    md4 = res["model4_active_diagnosis"]
    rr = {k: md4[k]["resolution_rate"] for k in ("information_gain_per_cost", "random", "fixed_order")}
    a(f"First-check expected information gain: {eig['selected']:.3f} bits (selected) vs {eig['random_choice']:.3f} "
      f"bits (random). The information-gain-per-cost policy resolves {rr['information_gain_per_cost']:.2f} of cases vs "
      f"{rr['random']:.2f} (random, {rr['information_gain_per_cost'] / max(rr['random'], 1e-9):.1f}x) and "
      f"{rr['fixed_order']:.2f} (fixed order, {rr['information_gain_per_cost'] / max(rr['fixed_order'], 1e-9):.1f}x) "
      "with fewer checks; the fixed order is cheaper only because it never escalates to the expensive, decisive checks.")
    a("")
    cov = ex.get("coverage")
    if cov:
        a("## 6. Uncertainty calibration (normal operation)")
        a("")
        a(f"{cov['n_runs']} fresh normal runs x {cov['hours_per_run']} h. z = residual / propagated sigma should be "
          "~N(0, 1). The 120 s case thins the historian to one sample per 2 minutes per tag.")
        a("")
        a("| Sampling | Row kind | n | mean z | sd z | within 2 sigma | within 3 sigma | max abs z |")
        a("|---|---|---|---|---|---|---|---|")
        for samp in ("sampling_10s", "sampling_120s"):
            for kind, v in list(cov[samp]["by_kind"].items()) + [("all", cov[samp]["all"])]:
                if v.get("n"):
                    a(f"| {samp[9:]} | {kind} | {v['n']} | {v['mean']:.2f} | {v['sd']:.2f} | {100 * v['within_2sigma']:.1f}% | "
                      f"{100 * v['within_3sigma']:.1f}% | {v['max_abs']:.2f} |")
        a("")
        a(f"chi2/dof: {cov['sampling_10s']['chi2_per_dof']} (10 s), {cov['sampling_120s']['chi2_per_dof']} (120 s). "
          "Sparse sampling is handled by sample-count-aware gauge read terms and a totalizer interpolation term at "
          "transfer boundaries (tickets / ledger), sized from the totalizer increment across the bracketing samples. "
          "At 120 s the ticket rows keep a positive bias (linear interpolation across a start-up ramp books part of "
          "the ramp before the ticket starts); the term bounds it to about 3 sigma but does not remove it.")
        a("")
    abl = ex.get("ablation")
    if abl:
        a("## 7. Experiment 7 - ablation (uncalibrated posterior, held-out test cases)")
        a("")
        a(f"Cases: env A {abl['n_cases']['A']}, env C {abl['n_cases']['C']}.")
        a("")
        a("| Variant | A top-1 family | A top-3 cause | A mean log-loss | C confident known-cause | C abstained (UNKNOWN) |")
        a("|---|---|---|---|---|---|")
        for name, v in abl["variants"].items():
            a(f"| {name} | {v['A_top1_family']:.3f} | {v['A_top3_hypothesis']:.3f} | {v['A_mean_log_loss']:.3f} | "
              f"{v['C_confident_known_cause']} | {v['C_abstained_unknown']} |")
        a("")
    ag = ex.get("agent_evaluation")
    if ag:
        s = ag["summary"]
        a("## 8. Experiment 5 - agentic investigation (section 13.5)")
        a("")
        a(f"{s['n_cases']} full LangGraph investigations ({s['known_fault_cases']} known-fault cases), template "
          "narrator (no external LLM), verification outcomes sampled from each check's sensitivity / false-positive "
          "rate given the true cause.")
        a("")
        a("| Metric | Value |")
        a("|---|---|")
        for k in ("initial_top1_rate", "initial_top3_rate", "resolved_rate", "resolved_correct_rate", "wrong_resolutions",
                  "confident_wrong_cases", "mean_checks_known", "grounding_pass_rate", "evidence_completeness_mean",
                  "tool_error_rate", "investigation_wall_s_median"):
            a(f"| {k} | {s[k]} |")
        a(f"| final status counts | {s['final_status_counts']} |")
        a(f"| normal runs final status | {s['normal_cases_final_status']} |")
        a("")
        un = s["unseen_cases"]
        if un:
            ok_n = sum(bool(u.get("correct")) for u in un)
            a(f"**Fault family absent from training (density-meter bias):** {ok_n}/{len(un)} investigations ended on "
              "the true instrument (" + "; ".join(f"{u['scenario']}: {u['final_top']} p={u['final_top_p']}, truth "
                                                  f"{', '.join(u.get('truth', []))}" for u in un) + "). No learned model "
              "saw a density fault, but the physics hypothesis catalogue derives a density-meter signature from the "
              "topology, so these cases generalise rather than abstain.")
        a("")
        a("Behavioural checks:")
        a("")
        for k, v in ag["behaviour"].items():
            a(f"- {k}: {'PASS' if v else 'FAIL'}")
        a("")
        a("Further section 13.5 behaviours (invalid IDs, failed queries, unauthorised actions, competing hypotheses, "
          "ungrounded LLM output, audit history) are covered by `tests/test_agent.py` and `apps/backend/tests`.")
        a("")
    a("## 9. Known limitations (honest gaps)")
    a("")
    a("- Root-cause top-3 recall and attribution macro-F1 are below the section 14 targets. Most misses are "
      "physically low-SNR cases (small drifts, faults on idle equipment) whose effect is within measurement "
      "uncertainty in the case window; scoring on observable cases narrows but does not close the gap.")
    a("- Temperature/prior calibration improves log-loss but not the Brier score.")
    a("- Loss MAE over all physical-loss cases is far above 10%: small or ambiguous leaks are estimated under "
      "competing hypotheses. Where the model reports a loss (P(physical loss) >= 0.6) the median error is small.")
    a("- The instrument classifier misses small biases (bias recall is low in env A): biases below the "
      "reconciliation noise floor are not distinguishable from normal operation in a single window.")
    a("- Under different noise levels (env B) the learned detectors raise more false alarms; the chi-square "
      "detector, which uses the propagated uncertainty, is the most robust.")
    a("- Indistinguishable pairs (e.g. PL-002 vs PL-003 leaks while both drain into the same control volume) are "
      "reported as tied; only a field check separates them, which the active-diagnosis step recommends.")
    a("- At coarse historian resolution (2 min) transfer-ticket reconciliation rows are biased by linear totalizer "
      "interpolation across start-up / shut-down ramps (mean z about 1). The uncertainty term keeps them within "
      "about 3 sigma; a ramp-aware interpolation of the point estimate would remove the bias.")
    a("- No manual-investigation baseline was timed, so the preparation-time KPI is not assessed; dashboard "
      "latency and 3D frame rate are browser-side and not measured here.")
    a("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--regenerate", action="store_true", help="regenerate the synthetic dataset")
    ap.add_argument("--skip-train", action="store_true", help="reuse experiments/results/results.json")
    ap.add_argument("--quick", action="store_true", help="small configuration for smoke testing")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-api", action="store_true", help="skip the API latency probe")
    ap.add_argument("--report", default=str(ROOT / "docs" / "research" / "results.md"))
    ap.add_argument("--report-only", action="store_true", help="rewrite the report from the saved JSON results")
    a = ap.parse_args(argv)
    if a.report_only:
        res = json.loads((RESULTS / "results.json").read_text(encoding="utf-8"))
        ex = json.loads((RESULTS / "experiments.json").read_text(encoding="utf-8"))
        write_report(res, ex, Path(a.report))
        print(f"report -> {a.report}")
        return
    t0 = time.time()
    from experiments.ablation import attribution_ablation
    from experiments.attribution import agent_eval
    from experiments.uncertainty import coverage
    from intelligence.training import dataset, train

    data_dir = ROOT / "datasets"
    if a.regenerate or not (data_dir / "training" / "windows.parquet").exists():
        print("[dataset] generating synthetic runs ...", flush=True)
        dataset.main(["--out", str(data_dir), "--workers", str(a.workers)] + (["--quick"] if a.quick else []))
    if not a.skip_train or not (RESULTS / "results.json").exists():
        print("[train] Models 1-4 ...", flush=True)
        train.main(["--data", str(data_dir / "training"), "--models", str(ROOT / "models"), "--results", str(RESULTS)])
    res = json.loads((RESULTS / "results.json").read_text(encoding="utf-8"))
    ex: dict[str, Any] = {}
    print("[simulator] conservation & completeness ...", flush=True)
    ex["simulator"] = simulator_checks(2.0 if a.quick else 6.0)
    print("[coverage] uncertainty calibration ...", flush=True)
    ex["coverage"] = coverage.run(n_runs=3 if a.quick else 12)
    print("[ablation] component ablation ...", flush=True)
    ex["ablation"] = attribution_ablation.run(workers=a.workers, include_env_c=True) if not a.quick else None
    print("[agent] investigation evaluation ...", flush=True)
    ex["agent_evaluation"] = agent_eval.run(seeds=(11,) if a.quick else (11, 12))
    if not a.no_api:
        print("[api] latency probe ...", flush=True)
        try:
            ex["api"] = api_latency(10.0 if a.quick else 20.0)
        except Exception as e:      # report, do not hide
            ex["api"] = {"error": repr(e)}
    ex["generated_at"] = datetime.now(timezone.utc).isoformat()
    ex["seconds"] = round(time.time() - t0, 1)
    (RESULTS / "experiments.json").write_text(json.dumps(ex, indent=2, default=str), encoding="utf-8")
    write_report(res, ex, Path(a.report))
    print(f"done in {ex['seconds']} s -> {RESULTS / 'experiments.json'}, {a.report}")


if __name__ == "__main__":
    main()
