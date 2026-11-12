"""Print a compact summary of experiments/results/results.json."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(path: str = "experiments/results/results.json") -> None:
    r = json.loads(Path(path).read_text())
    print("version", r["version"], "| counts", r["counts"])
    print("\nModel 1 - anomaly detection (observable-effect labels)")
    for env, d in r["model1_anomaly_detection"].items():
        for k, v in d.items():
            print(f"  [{env}] {k:24s} P={v['precision']:.2f} R={v['recall']:.2f} F1={v['f1']:.2f} "
                  f"PR-AUC={v.get('pr_auc', 0):.2f} FA/100={v['false_alarms_per_100_normal']:.1f} "
                  f"episode={v['episode_detection_rate']:.2f} latency={v['mean_detection_latency_windows']:.2f}w")
    print("\nModel 2 - instrument fault classifier")
    for env, v in r["model2_sensor_diagnostics"].items():
        rec = {k: round(x, 2) for k, x in v["per_class_recall"].items()}
        print(f"  [{env}] macro-F1={v['macro_f1']:.3f} fault-recall={v['fault_detection_recall']:.2f} "
              f"localisation={v['fault_localisation_accuracy']:.2f} recall={rec}")
    c = r["model3_calibration"]
    print(f"\nModel 3 - calibration T={c['temperature']} eps={c['prior_mix']} loss_rel_err={c['loss_model_rel_error']}")
    for env, d in r["model3_fault_attribution"].items():
        for k, x in d.items():
            keys = ("top1_family", "top3_family", "top1_hypothesis", "top3_hypothesis", "macro_f1_family",
                    "log_loss", "brier", "ece", "abstention_rate", "confident_wrong_known_cause", "n_cases")
            print(f"  [{env}] {k:36s} " + " ".join(f"{kk}={x[kk]:.3f}" if isinstance(x.get(kk), float) else
                                                   f"{kk}={x[kk]}" for kk in keys if kk in x))
    L = r["loss_estimation"]
    print("\nLoss estimation", {k: (round(v, 3) if isinstance(v, float) else v)
                                for k, v in L["all_physical_loss_cases"].items()})
    print("  reported cases", {k: (round(v, 3) if isinstance(v, float) else v)
                               for k, v in L["cases_where_loss_is_reported"].items()},
          "reported_fraction", L["reported_fraction"])
    print("\nModel 4 - active diagnosis")
    for k, v in r["model4_active_diagnosis"].items():
        print(f"  {k:28s} {v}")


if __name__ == "__main__":
    main(*sys.argv[1:])
