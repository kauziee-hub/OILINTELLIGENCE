"""Train, calibrate and evaluate all diagnostic models; write the model registry.

python -m intelligence.training.train --data datasets/training --models models
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from scipy.special import logsumexp
from xgboost import XGBClassifier

from industrial.schemas.plant import get_plant
from intelligence.active_diagnosis.information_gain import ActiveDiagnosis
from intelligence.active_diagnosis.tests import sample_outcome
from intelligence.anomaly_detection.detectors import (ConventionalThreshold, IsolationForestDetector,
                                                      StatisticalDetector, XGBoostDetector,
                                                      tune_threshold_for_false_alarms)
from intelligence.evaluation.metrics import (binary_metrics, log_loss_brier, multiclass_metrics,
                                             regression_metrics, topk_accuracy)
from intelligence.fault_attribution.hypotheses import Hypothesis, build_hypotheses
from intelligence.sensor_diagnostics.classifier import CLASSES, SensorFaultClassifier

FAMILIES = ["NORMAL", "PIPELINE_LEAK", "TANK_LEAK", "METER_ERROR", "LEVEL_ERROR", "TEMP_ERROR", "DENSITY_ERROR",
            "SENSOR_STUCK", "SENSOR_DROPOUT", "VALVE_PASSING", "PUMP_DEGRADATION", "MISSING_TRANSFER", "UNKNOWN"]


# ---------------------------------------------------------------------------
# Posterior recomputation with calibration temperature
# ---------------------------------------------------------------------------
def posterior(case_var: dict[str, Any], T: float, eps: float = 0.0
              ) -> tuple[dict[str, float], dict[str, float], list[str]]:
    """Recompute the posterior from stored log-likelihoods (temperature T, prior mixing eps)."""
    singles = case_var["singles"]
    pairs = case_var["pairs"]
    lp = [s["ll"] / T + math.log(s["prior"]) for s in singles]
    lpp = [p["ll"] / T + (p["logpost"] - p["ll"]) for p in pairs]
    Z = logsumexp(lp + lpp)
    hyp: dict[str, float] = {}
    fam: dict[str, float] = {}
    marg: dict[str, float] = {}
    for s, v in zip(singles, lp):
        pr = (1 - eps) * math.exp(v - Z) + eps * s["prior"]
        hyp[s["hid"]] = hyp.get(s["hid"], 0) + pr
        fam[s["family"]] = fam.get(s["family"], 0) + pr
        marg[s["hid"]] = marg.get(s["hid"], 0) + pr
    for p, v in zip(pairs, lpp):
        pr = (1 - eps) * math.exp(v - Z)
        for h, f in zip(p["hids"], p["families"]):
            hyp[h] = hyp.get(h, 0) + pr / 2
            fam[f] = fam.get(f, 0) + pr / 2
            marg[h] = marg.get(h, 0) + pr
    ranking = sorted(marg, key=lambda k: -marg[k])
    return hyp, fam, ranking


def case_truth_family(c: dict[str, Any]) -> str:
    return c["truth_families"][0] if c["truth_families"] else "NORMAL"


def fit_calibration(cases: list[dict[str, Any]]) -> tuple[float, float, list[tuple[float, float, float]]]:
    """Grid-fit temperature T and prior-mixing eps by family-level log loss.

    The physics model has no fitted parameters, so the train and validation runs
    (never the test runs) are both available for calibration.
    """
    Ts = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0]
    Es = [0.0, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2]
    single = [c for c in cases if len(c["truth"]) <= 1]
    curve = []
    for T in Ts:
        for e in Es:
            rows = [posterior(c["full"], T, e)[1] for c in single]
            truths = [case_truth_family(c) for c in single]
            curve.append((T, e, log_loss_brier(rows, truths, FAMILIES)["log_loss"]))
    best = min(curve, key=lambda x: x[2])
    return best[0], best[1], curve


def fit_loss_interval(cases: list[dict[str, Any]], target: float = 0.9) -> float:
    """Relative model-error term k so that cond +/- 1.645 sqrt(sd^2 + (k cond)^2) covers ~90%."""
    lc = [c for c in cases if c["true_loss_t"] > 0.5 and c["full"]["loss_cond"] is not None]
    if not lc:
        return 0.25
    for k in np.arange(0.05, 1.51, 0.05):
        cov = []
        for c in lc:
            m = c["full"]["loss_cond"]
            sd = math.sqrt((c["full"].get("loss_param_sd") or 0.0) ** 2 + (k * m) ** 2)
            cov.append(m - 1.645 * sd <= c["true_loss_t"] <= m + 1.645 * sd)
        if np.mean(cov) >= target:
            return float(round(k, 2))
    return 1.5


# ---------------------------------------------------------------------------
def evaluate_detectors(w: pd.DataFrame, out: dict[str, Any], models_dir: Path, version: str) -> dict[str, Any]:
    LBL = "label_observable"
    train = w[(w.env == "A") & (w.split == "train")]
    val = w[(w.env == "A") & (w.split == "val")]
    normal_train = train[(train.label_anomaly == 0) & (train[LBL] == 0)]
    dets: dict[str, Any] = {
        "conventional_threshold": ConventionalThreshold(),
        "statistical_chi2": StatisticalDetector(),
        "isolation_forest_raw": IsolationForestDetector(name="isolation_forest_raw", kind="raw").fit(normal_train),
        "isolation_forest": IsolationForestDetector(kind="all").fit(normal_train),
        "xgboost_raw": XGBoostDetector(name="xgboost_raw", kind="raw").fit(train, train[LBL].to_numpy()),
        "xgboost_physics": XGBoostDetector(name="xgboost_physics", kind="all").fit(train, train[LBL].to_numpy()),
    }
    # threshold tuning on validation normal windows: <= 5 false alarms / 100 normal windows
    vn = val[(val.label_anomaly == 0) & (val[LBL] == 0)]
    for name, d in dets.items():
        if name in ("isolation_forest", "isolation_forest_raw", "xgboost_raw", "xgboost_physics", "statistical_chi2"):
            d.threshold = max(tune_threshold_for_false_alarms(d.score(vn), 3.0), getattr(d, "threshold", 0) * 0
                              ) if name != "statistical_chi2" else max(
                tune_threshold_for_false_alarms(d.score(vn), 3.0), 3.0)
    res: dict[str, Any] = {}
    for env in ("A", "B", "C"):
        te = w[(w.env == env) & (w.split == "test")]
        if not len(te):
            continue
        res[env] = {}
        for name, d in dets.items():
            s = d.score(te)
            m = binary_metrics(te[LBL].to_numpy(), s, d.threshold)
            m_act = binary_metrics(te.label_anomaly.to_numpy(), s, d.threshold)
            m["active_label"] = {k: m_act[k] for k in ("precision", "recall", "f1", "pr_auc") if k in m_act}
            # episode-level: fault run detected if any fault-active window alarms
            det_runs, lat = 0, []
            fault_runs = te[te.family != "NORMAL"].run_id.unique()
            for rid in fault_runs:
                g = te[te.run_id == rid].sort_values("window")
                sg = d.score(g)
                act = g[LBL].to_numpy() == 1
                if act.any():
                    first = int(np.argmax(act))
                    hits = np.flatnonzero((sg >= d.threshold) & (np.arange(len(g)) >= first))
                    if len(hits):
                        det_runs += 1
                        lat.append(int(hits[0] - first))
            m["episode_detection_rate"] = det_runs / max(len(fault_runs), 1)
            m["mean_detection_latency_windows"] = float(np.mean(lat)) if lat else float("nan")
            res[env][name] = m
    out["model1_anomaly_detection"] = res
    out["model1_feature_importance"] = dets["xgboost_physics"].feature_importance()
    for name in ("isolation_forest", "xgboost_physics"):
        save_model(models_dir, name, version, dets[name], {"threshold": dets[name].threshold,
                                                            "columns": dets[name].columns})
    save_model(models_dir, "statistical_chi2", version, dets["statistical_chi2"],
               {"threshold": dets["statistical_chi2"].threshold})
    return dets


SENSOR_FP_BUDGET = 0.01     # share of healthy instrument-windows we accept to flag


def evaluate_sensor_classifier(s: pd.DataFrame, out: dict[str, Any], models_dir: Path, version: str) -> None:
    s = s[s.active > 0]
    tr = s[(s.env == "A") & (s.split == "train") & (s.label != "UNKNOWN")]
    clf = SensorFaultClassifier().fit(tr, tr.label.tolist())
    # decision threshold from env A VALIDATION normals only (never the test runs)
    va = s[(s.env == "A") & (s.split == "val") & (s.label == "NORMAL")]
    thr = clf.calibrate_threshold(va, SENSOR_FP_BUDGET)
    decision = {"rule": "P(fault) >= threshold, then most probable fault class",
                "false_flag_budget": SENSOR_FP_BUDGET, "threshold": thr, "fitted_on": "env A validation normals"}
    res = {}
    for env in ("A", "B"):
        te = s[(s.env == env) & (s.split == "test") & (s.label != "UNKNOWN")]
        if not len(te):
            continue
        pred = clf.predict(te)
        m = multiclass_metrics(te.label.tolist(), pred, CLASSES)
        if "label_active" in te:
            ma = multiclass_metrics(te.label_active.tolist(), pred, CLASSES)
            m["active_label"] = {"macro_f1": ma["macro_f1"], "per_class_recall": ma["per_class_recall"]}
        faulty = te.label != "NORMAL"
        m["fault_detection_recall"] = float(np.mean(np.array(pred)[faulty.to_numpy()] != "NORMAL")) if faulty.any() else None
        m["false_flag_rate"] = float(np.mean(np.array(pred)[~faulty.to_numpy()] != "NORMAL")) if (~faulty).any() else None
        argmax = np.array([clf.classes[i] for i in clf.predict_proba(te).argmax(1)])
        m["fault_detection_recall_argmax"] = float(np.mean(argmax[faulty.to_numpy()] != "NORMAL")) if faulty.any() else None
        # localisation: among fault windows of a run, is the faulty instrument the one ranked most abnormal?
        P = clf.predict_proba(te)
        te2 = te.assign(p_fault=1 - P[:, 0])
        loc = []
        for (rid, win), g in te2.groupby(["run_id", "window"]):
            if (g.label != "NORMAL").any():
                loc.append(g.sort_values("p_fault").iloc[-1].label != "NORMAL")
        m["fault_localisation_accuracy"] = float(np.mean(loc)) if loc else None
        m["decision"] = decision
        res[env] = m
    out["model2_sensor_diagnostics"] = res
    save_model(models_dir, "sensor_classifier", version, clf, {"columns": clf.columns, "classes": clf.classes})


def evaluate_attribution(cases: list[dict[str, Any]], out: dict[str, Any], models_dir: Path, version: str) -> float:
    calib = [c for c in cases if c["env"] == "A" and c["split"] in ("train", "val")]
    T, EPS, curve = fit_calibration(calib)
    K = fit_loss_interval(calib)
    out["model3_calibration"] = {"temperature": T, "prior_mix": EPS, "loss_model_rel_error": K,
                                 "fitted_on": "env A train+val runs", "n_cases": len(calib),
                                 "curve_top5": sorted(curve, key=lambda x: x[2])[:5]}
    # learned attribution baselines on case-level features (trained on env A train)
    train = [c for c in cases if c["env"] == "A" and c["split"] == "train" and len(c["truth"]) <= 1]

    def feat(c, kind):
        keys = sorted(k for k in c["features_mean"] if k.startswith(("raw_", "ctx_", "qual_")) or (
            kind == "phys" and k.startswith("phys_")))
        return [c["features_mean"].get(k, 0.0) for k in keys] + [c["features_max"].get(k, 0.0) for k in keys], keys

    learned = {}
    for kind in ("raw", "phys"):
        X = np.array([feat(c, kind)[0] for c in train])
        y = [case_truth_family(c) for c in train]
        labels = sorted(set(y))
        yi = np.array([labels.index(v) for v in y])
        m = XGBClassifier(n_estimators=250, max_depth=3, learning_rate=0.1, objective="multi:softprob",
                          num_class=len(labels), n_jobs=4, random_state=0)
        m.fit(X, yi)
        learned[kind] = (m, labels)
    res: dict[str, Any] = {}
    for env in ("A", "B", "C", "A_observable", "B_observable"):
        base_env = env.split("_")[0]
        te = [c for c in cases if c["env"] == base_env and c["split"] == "test"]
        if env.endswith("observable"):
            te = [c for c in te if c.get("observable") or not c["truth"]]
        if not te:
            continue
        r: dict[str, Any] = {}
        for label, TT, EE in (("physics_bayes_uncalibrated", 1.0, 0.0), ("physics_bayes_calibrated", T, EPS)):
            fam_rows, truths, rank_h, rank_f, truth_h, truth_f, preds = [], [], [], [], [], [], []
            confident_wrong = 0
            abstain = 0
            for c in te:
                hyp, fam, ranking = posterior(c["full"], TT, EE)
                famrank = sorted(fam, key=lambda k: -fam[k])
                rank_h.append(ranking)
                rank_f.append(famrank)
                truth_h.append(c["truth"] if c["truth"] else ["NORMAL"])
                truth_f.append(c["truth_families"] if c["truth_families"] else ["NORMAL"])
                if len(c["truth"]) <= 1:
                    fam_rows.append(fam)
                    truths.append(case_truth_family(c))
                    preds.append(famrank[0])
                top_p = fam[famrank[0]]
                if base_env == "C":
                    known_wrong = famrank[0] not in ("UNKNOWN",) and famrank[0] not in truth_f[-1]
                    if known_wrong and top_p >= 0.8:
                        confident_wrong += 1
                    if famrank[0] == "UNKNOWN" or top_p < 0.8:
                        abstain += 1
            r[label] = {
                "top1_hypothesis": topk_accuracy(truth_h, rank_h, 1), "top3_hypothesis": topk_accuracy(truth_h, rank_h, 3),
                "top1_family": topk_accuracy(truth_f, rank_f, 1), "top3_family": topk_accuracy(truth_f, rank_f, 3),
                **log_loss_brier(fam_rows, truths, FAMILIES),
                "macro_f1_family": multiclass_metrics(truths, preds, sorted(set(truths)))["macro_f1"],
                "n_cases": len(te),
            }
            if base_env == "C":
                r[label]["confident_wrong_known_cause"] = confident_wrong
                r[label]["abstention_rate"] = abstain / len(te)
        if base_env == "C" and all("restricted" in c for c in te):
            cw, ab = 0, 0
            for c in te:
                _, fam, _ = posterior(c["restricted"], T, EPS)
                top = max(fam, key=fam.get)
                if top != "UNKNOWN" and top not in c["truth_families"] and fam[top] >= 0.8:
                    cw += 1
                if top == "UNKNOWN" or fam[top] < 0.8:
                    ab += 1
            r["physics_bayes_restricted_hypotheses"] = {"confident_wrong_known_cause": cw,
                                                        "abstention_rate": ab / len(te), "n_cases": len(te)}
        for kind, (m, labels) in learned.items():
            single = [c for c in te if len(c["truth"]) <= 1]
            if not single:
                continue
            X = np.array([feat(c, kind)[0] for c in single])
            P = m.predict_proba(X)
            rows = [{labels[j]: float(P[i, j]) for j in range(len(labels))} for i in range(len(single))]
            truths = [case_truth_family(c) for c in single]
            preds = [labels[int(np.argmax(P[i]))] for i in range(len(single))]
            rankf = [[labels[j] for j in np.argsort(-P[i])] for i in range(len(single))]
            r[f"xgboost_{kind}_features"] = {
                "top1_family": topk_accuracy([[t] for t in truths], rankf, 1),
                "top3_family": topk_accuracy([[t] for t in truths], rankf, 3),
                **log_loss_brier(rows, truths, FAMILIES),
                "macro_f1_family": multiclass_metrics(truths, preds, sorted(set(truths)))["macro_f1"],
                "n_cases": len(single),
            }
        res[env] = r
    out["model3_fault_attribution"] = res
    # ---- loss estimation (physical loss scenarios)
    def loss_eval(sel):
        est = [c["full"]["loss_cond"] if c["full"]["loss_cond"] is not None else 0.0 for c in sel]
        tru = [c["true_loss_t"] for c in sel]
        sds = [math.sqrt((c["full"].get("loss_param_sd") or 0.0) ** 2 + (K * e) ** 2) for c, e in zip(sel, est)]
        lo = [max(e - 1.645 * sd, 0.0) for e, sd in zip(est, sds)]
        hi = [e + 1.645 * sd for e, sd in zip(est, sds)]
        m = regression_metrics(tru, est, lo, hi) if sel else {}
        if sel:
            m["mae_pct_of_injected"] = float(np.mean(np.abs(np.array(est) - np.array(tru)) / np.array(tru)) * 100)
            m["median_abs_pct_error"] = float(np.median(np.abs(np.array(est) - np.array(tru)) / np.array(tru)) * 100)
        return m

    lc = [c for c in cases if c["split"] == "test" and c["env"] in ("A", "B") and c["true_loss_t"] > 0.5]
    reported = [c for c in lc if (c["full"].get("p_physical_loss") or 0.0) >= 0.6]
    out["loss_estimation"] = {
        "all_physical_loss_cases": loss_eval(lc),
        "cases_where_loss_is_reported": loss_eval(reported),
        "reported_fraction": len(reported) / len(lc) if lc else None,
        "per_case": [{"run": c["run_id"], "true_t": round(c["true_loss_t"], 3), "est_t": c["full"]["loss_cond"],
                      "p_physical_loss": round(c["full"].get("p_physical_loss") or 0.0, 3)} for c in lc],
    }
    cal = {"temperature": T, "prior_mix": EPS, "loss_model_rel_error": K}
    save_model(models_dir, "physics_bayes", version, cal, dict(cal, type="analytical-calibration"))
    return T, EPS


def evaluate_active_diagnosis(cases: list[dict[str, Any]], T: float, EPS: float, out: dict[str, Any],
                              seed: int = 0) -> None:
    plant = get_plant()
    ad = ActiveDiagnosis(plant)
    allh = {h.hid: h for h in build_hypotheses(plant)}
    rng = np.random.default_rng(seed)
    te = [c for c in cases if c["split"] == "test" and c["env"] in ("A", "B") and c["truth"]]
    fixed_order = ["LEDGER_RECONCILIATION", "INSTRUMENT_DIAGNOSTICS", "RETRIEVE_METER_PROVING_RECORD",
                   "MANUAL_TANK_GAUGING", "REFERENCE_TEMPERATURE_CHECK", "PUMP_PERFORMANCE_TEST",
                   "VALVE_SEAT_TEST", "LINE_INSPECTION", "TANK_ISOLATION_TEST", "DENSITY_LAB_CHECK"]
    strategies = ("information_gain_per_cost", "information_gain", "random", "fixed_order")
    res = {s: {"tests": [], "cost": [], "hours": [], "correct": [], "resolved": []} for s in strategies}
    for c in te:
        hyp, _, _ = posterior(c["full"], T, EPS)
        ids = [h for h in hyp if h in allh and hyp[h] > 1e-5]
        H = [allh[h] for h in ids]
        p0 = np.array([hyp[h] for h in ids])
        p0 = p0 / p0.sum()
        truth = [Hypothesis(t, t.split(":")[0], t.split(":", 1)[1] if ":" in t else None, 1.0)
                 for t in c["truth"]]
        for strat in strategies:
            p = p0.copy()
            done: set[str] = set()
            cost = hours = 0.0
            n = 0
            srng = np.random.default_rng(rng.integers(1 << 31))
            while n < 8 and p.max() < 0.95:
                if strat.startswith("information_gain"):
                    ad.cost_weight = 0.5 if strat == "information_gain_per_cost" else 0.0
                    ad.safety_weight = 1.0 if strat == "information_gain_per_cost" else 0.0
                    ranked = ad.rank(H, p, performed=done, top=1)
                    act = ranked[0].action
                elif strat == "random":
                    pool = [a for a in ad.actions if a.action_id not in done]
                    act = pool[int(srng.integers(len(pool)))]
                else:
                    pool = [a for a in ad.actions if a.action_id not in done]
                    pool.sort(key=lambda a: (fixed_order.index(a.action_type), a.action_id))
                    act = pool[0]
                y = sample_outcome(plant, act, truth, srng)
                p = ad.update(H, p, act.action_id, y)
                done.add(act.action_id)
                cost += act.spec.cost
                hours += act.spec.duration_h
                n += 1
            top = ids[int(np.argmax(p))]
            res[strat]["tests"].append(n)
            res[strat]["cost"].append(cost)
            res[strat]["hours"].append(hours)
            res[strat]["correct"].append(top in c["truth"])
            res[strat]["resolved"].append(bool(p.max() >= 0.95))
    summary = {}
    for s, v in res.items():
        summary[s] = {"mean_tests": float(np.mean(v["tests"])), "mean_cost": float(np.mean(v["cost"])),
                      "mean_hours": float(np.mean(v["hours"])), "final_accuracy": float(np.mean(v["correct"])),
                      "resolution_rate": float(np.mean(v["resolved"])), "n_cases": len(v["tests"])}
    # expected information gain of the first selected test vs random choice
    eig_best, eig_rand = [], []
    for c in te[:60]:
        hyp, _, _ = posterior(c["full"], T, EPS)
        ids = [h for h in hyp if h in allh and hyp[h] > 1e-5]
        H = [allh[h] for h in ids]
        p = np.array([hyp[h] for h in ids])
        ranked = ad.rank(H, p / p.sum(), top=len(ad.actions))
        eig_best.append(max(r.expected_information_gain for r in ranked))
        eig_rand.append(float(np.mean([r.expected_information_gain for r in ranked])))
    summary["first_test_expected_information_gain_bits"] = {"selected": float(np.mean(eig_best)),
                                                            "random_choice": float(np.mean(eig_rand))}
    out["model4_active_diagnosis"] = summary


def save_model(models_dir: Path, name: str, version: str, obj: Any, meta: dict[str, Any]) -> None:
    d = models_dir / "registry" / name / version
    d.mkdir(parents=True, exist_ok=True)
    joblib.dump(obj, d / "model.joblib")
    meta = dict(meta, name=name, version=version, created=datetime.now(timezone.utc).isoformat())
    (d / "meta.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    idx_path = models_dir / "registry" / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    idx[name] = {"latest": version, "path": str(d.relative_to(models_dir))}
    idx_path.write_text(json.dumps(idx, indent=1), encoding="utf-8")


def main(argv: list[str] | None = None) -> dict[str, Any]:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="datasets/training")
    ap.add_argument("--models", default="models")
    ap.add_argument("--results", default="experiments/results")
    a = ap.parse_args(argv)
    data, models_dir, results = Path(a.data), Path(a.models), Path(a.results)
    models_dir.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    meta = json.loads((data / "dataset_meta.json").read_text())
    version = f"v1-{meta['plan_hash']}-{datetime.now(timezone.utc):%Y%m%d%H%M}"
    t0 = time.time()
    w = pd.read_parquet(data / "windows.parquet")
    s = pd.read_parquet(data / "sensor_windows.parquet")
    cases = [json.loads(l) for l in open(data / "cases.jsonl", encoding="utf-8")]
    out: dict[str, Any] = {"version": version, "dataset": {k: v for k, v in meta.items() if k != "plan"},
                           "counts": {"windows": len(w), "sensor_windows": len(s), "cases": len(cases),
                                      "cases_by_env": dict(Counter(c["env"] for c in cases))}}
    print("[1/4] anomaly detection", flush=True)
    evaluate_detectors(w, out, models_dir, version)
    print("[2/4] instrument fault classifier", flush=True)
    evaluate_sensor_classifier(s, out, models_dir, version)
    print("[3/4] fault attribution + calibration + loss estimation", flush=True)
    T, EPS = evaluate_attribution(cases, out, models_dir, version)
    print("[4/4] active diagnosis", flush=True)
    evaluate_active_diagnosis(cases, T, EPS, out)
    # drift reference for the adaptive layer (operating-feature distributions of normal training windows)
    from intelligence.adaptation.drift import build_reference
    (models_dir / "registry").mkdir(parents=True, exist_ok=True)
    (models_dir / "registry" / "drift_reference.json").write_text(json.dumps(build_reference(w)), encoding="utf-8")
    out["training_seconds"] = round(time.time() - t0, 1)
    out["config_hash"] = hashlib.sha256(json.dumps(out["dataset"], sort_keys=True, default=str).encode()).hexdigest()[:12]
    (results / "results.json").write_text(json.dumps(out, indent=1, default=float), encoding="utf-8")
    print(f"results -> {results / 'results.json'}  ({out['training_seconds']}s)")
    return out


if __name__ == "__main__":
    main()
