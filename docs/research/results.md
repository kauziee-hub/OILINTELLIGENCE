# OILINTELLIGENCE - research results

Generated 2026-11-03 16:00 UTC by `oil-experiments`. Model version `v1-bea418efe9d3-202611031540`; dataset 3075 balance windows, 33825 sensor windows and 205 investigation cases (env A 165, B 28, C 12). All data are synthetic; every number below comes from the simulator and must not be read as plant performance.

Environments: **A** = same distribution as training (held-out runs); **B** = different instrument noise and unseen fault combinations; **C** = fault families never seen in training (density bias, sensor noise, wrong ticket quantity). Splits are by simulation run, never by window.

## 1. KPI scorecard (section 14 targets)

| KPI | Target | Result | Status |
|---|---|---|---|
| Normal mass-balance conservation (simulator) | within numerical tolerance | max error 1.86e-08 kg | MET |
| Physical loss estimation MAE | < 10% of injected loss | 38.1% all cases; 15.2% where reported (median 6.6%) | NOT MET |
| Measurement fault detection recall | >= 90% | 40.8% | NOT MET |
| Root-cause top-3 recall (env A, all test cases) | >= 90% | 80.0% | NOT MET |
| False-positive anomaly rate (xgboost_physics, env A) | <= 5 / 100 normal | 3.8 (chi2 detector 1.0) | MET |
| Data completeness (normal connectivity) | >= 98% | 100.0% | MET |
| Sensor fault localisation | >= 85% | 59.2% | NOT MET |
| Fault attribution macro-F1 (env A) | >= 0.85 | 0.669 | NOT MET |
| Probability calibration | Brier better than uncalibrated | Brier 0.532 vs 0.503; log-loss 1.328 vs 2.007 | NOT MET |
| Diagnostic test selection | EIG better than random | 0.484 vs 0.047 bits | MET |
| Investigation evidence completeness | >= 95% | 100.0% | MET |
| Unsupported confident diagnoses | zero (safety-critical cases) | agent: 0 confident-wrong, 0 wrong resolutions; unseen faults: 0 confident known-cause verdicts | MET |
| Investigation preparation time | 50% better than manual baseline | median 1.79 s per automated investigation | NOT MEASURED (no manual baseline) |
| Standard API latency | P95 <= 500 ms | 28.4 ms | MET |
| Sensor history query | <= 2 s | P95 59.6 ms | MET |
| Dashboard update latency / twin FPS | <= 2 s / >= 30 FPS | browser-side; not measured by this script | NOT MEASURED |

## 2. Experiment 1 & 2 - detection: threshold vs ML, raw vs physics features

Window-level detection against observable-effect labels (a fault counts once its effect is physically visible in the window). FA/100 = false alarms per 100 normal windows; latency in 30-min windows.

**Environment A**

| Detector | Precision | Recall | F1 | PR-AUC | FA/100 | Episode recall | Latency (w) |
|---|---|---|---|---|---|---|---|
| conventional_threshold | 0.50 | 0.22 | 0.31 | 0.39 | 7.5 | 0.56 | 2.05 |
| statistical_chi2 | 0.94 | 0.43 | 0.59 | 0.72 | 1.0 | 0.62 | 0.88 |
| isolation_forest_raw | 0.53 | 0.12 | 0.19 | 0.41 | 3.6 | 0.21 | 1.00 |
| isolation_forest | 0.71 | 0.44 | 0.54 | 0.63 | 5.9 | 0.72 | 1.29 |
| xgboost_raw | 0.74 | 0.23 | 0.35 | 0.55 | 2.8 | 0.36 | 1.21 |
| xgboost_physics | 0.82 | 0.50 | 0.62 | 0.70 | 3.8 | 0.77 | 0.83 |

**Environment B**

| Detector | Precision | Recall | F1 | PR-AUC | FA/100 | Episode recall | Latency (w) |
|---|---|---|---|---|---|---|---|
| conventional_threshold | 0.29 | 0.44 | 0.35 | 0.33 | 23.9 | 0.62 | 1.19 |
| statistical_chi2 | 0.61 | 0.70 | 0.65 | 0.67 | 9.9 | 0.65 | 0.65 |
| isolation_forest_raw | 0.21 | 0.23 | 0.22 | 0.24 | 19.8 | 0.42 | 2.18 |
| isolation_forest | 0.34 | 0.65 | 0.45 | 0.46 | 28.0 | 0.73 | 0.47 |
| xgboost_raw | 0.40 | 0.23 | 0.30 | 0.33 | 7.9 | 0.38 | 2.00 |
| xgboost_physics | 0.53 | 0.77 | 0.63 | 0.65 | 15.2 | 0.73 | 0.58 |

**Environment C**

| Detector | Precision | Recall | F1 | PR-AUC | FA/100 | Episode recall | Latency (w) |
|---|---|---|---|---|---|---|---|
| conventional_threshold | 0.38 | 0.14 | 0.20 | 0.36 | 7.4 | 0.33 | 0.75 |
| statistical_chi2 | 0.91 | 0.45 | 0.61 | 0.65 | 1.5 | 0.67 | 0.38 |
| isolation_forest_raw | 0.11 | 0.02 | 0.04 | 0.20 | 5.9 | 0.25 | 5.00 |
| isolation_forest | 0.70 | 0.16 | 0.26 | 0.43 | 2.2 | 0.50 | 1.67 |
| xgboost_raw | 0.00 | 0.00 | 0.00 | 0.24 | 2.9 | 0.00 | nan |
| xgboost_physics | 0.74 | 0.45 | 0.56 | 0.64 | 5.1 | 0.67 | 0.38 |

Physics-derived features raise the F1 of both learned detectors in 3/3 environments (A, B, C). Most precise detector per environment: A statistical_chi2, B statistical_chi2, C statistical_chi2; best recall: A xgboost_physics, B xgboost_physics, C statistical_chi2; best episode coverage: A xgboost_physics, B isolation_forest, C statistical_chi2.

## 3. Instrument fault classifier (Model 2)

| Env | Macro-F1 | Fault recall | Localisation | Recall by class |
|---|---|---|---|---|
| A | 0.497 | 0.41 | 0.59 | NORMAL 1.00, BIAS_POS 0.08, BIAS_NEG 0.05, DRIFT 0.41, STUCK 0.25, DROPOUT 1.00 |
| B | 0.643 | 0.67 | 0.65 | NORMAL 0.98, BIAS_POS 1.00, BIAS_NEG 0.27, DRIFT 0.57, STUCK 1.00, DROPOUT 1.00 |

## 4. Experiment 3 - fault attribution (Model 3)

Calibration fitted on train+validation cases: temperature T = 0.75, prior mixing eps = 0.1, loss model relative error 0.15.

| Env | Model | Top-1 family | Top-3 family | Top-1 cause | Top-3 cause | Macro-F1 | Log-loss | Brier | ECE |
|---|---|---|---|---|---|---|---|---|---|
| A | physics_bayes_uncalibrated | 0.644 | 0.822 | 0.622 | 0.822 | 0.669 | 2.007 | 0.503 | 0.118 |
| A | physics_bayes_calibrated | 0.644 | 0.844 | 0.600 | 0.800 | 0.669 | 1.328 | 0.532 | 0.140 |
| A | xgboost_raw_features | 0.357 | 0.619 | n/a | n/a | 0.342 | 2.306 | 0.922 | 0.256 |
| A | xgboost_phys_features | 0.429 | 0.762 | n/a | n/a | 0.414 | 1.605 | 0.707 | 0.160 |
| B | physics_bayes_uncalibrated | 0.500 | 0.679 | 0.464 | 0.571 | 0.445 | 2.834 | 0.710 | 0.179 |
| B | physics_bayes_calibrated | 0.500 | 0.679 | 0.500 | 0.571 | 0.451 | 1.864 | 0.708 | 0.182 |
| B | xgboost_raw_features | 0.269 | 0.538 | n/a | n/a | 0.268 | 2.419 | 0.958 | 0.350 |
| B | xgboost_phys_features | 0.462 | 0.615 | n/a | n/a | 0.429 | 2.048 | 0.772 | 0.242 |
| C | physics_bayes_uncalibrated | 0.583 | 0.583 | 0.333 | 0.333 | 0.773 | 11.633 | 0.614 | 0.315 |
| C | physics_bayes_calibrated | 0.583 | 0.583 | 0.333 | 0.333 | 0.773 | 2.504 | 0.635 | 0.186 |
| C | xgboost_raw_features | 0.000 | 0.000 | n/a | n/a | 0.000 | 27.631 | 1.358 | 0.518 |
| C | xgboost_phys_features | 0.000 | 0.000 | n/a | n/a | 0.000 | 27.631 | 1.404 | 0.537 |
| A_observable | physics_bayes_uncalibrated | 0.725 | 0.850 | 0.700 | 0.850 | 0.748 | 1.971 | 0.430 | 0.196 |
| A_observable | physics_bayes_calibrated | 0.725 | 0.875 | 0.675 | 0.850 | 0.748 | 1.196 | 0.462 | 0.222 |
| A_observable | xgboost_raw_features | 0.351 | 0.595 | n/a | n/a | 0.317 | 2.311 | 0.906 | 0.236 |
| A_observable | xgboost_phys_features | 0.486 | 0.784 | n/a | n/a | 0.444 | 1.449 | 0.627 | 0.098 |
| B_observable | physics_bayes_uncalibrated | 0.650 | 0.800 | 0.650 | 0.750 | 0.611 | 2.093 | 0.539 | 0.112 |
| B_observable | physics_bayes_calibrated | 0.650 | 0.800 | 0.700 | 0.750 | 0.630 | 1.403 | 0.549 | 0.225 |
| B_observable | xgboost_raw_features | 0.333 | 0.556 | n/a | n/a | 0.319 | 2.092 | 0.847 | 0.304 |
| B_observable | xgboost_phys_features | 0.667 | 0.778 | n/a | n/a | 0.607 | 1.310 | 0.500 | 0.214 |

`*_observable` rows restrict scoring to cases where the injected fault had a physically observable effect inside the case window.
The topology-aware physics model scores higher than both non-topological XGBoost classifiers on top-1, top-3 and macro-F1 in every environment, except: B_observable top1_family vs xgboost_phys_features (0.650 < 0.667). On unseen fault families (env C) the XGBoost classifiers score zero by construction; the physics model's top family is right in 58% of cases and it gives 0 confident wrong known-cause verdicts.

**Loss estimation** (physical-loss cases):

| Subset | n | MAE (t) | Bias (t) | MAPE | Median abs. % error | 90% interval coverage |
|---|---|---|---|---|---|---|
| all physical-loss cases | 11 | 0.59 | 0.37 | 38.1% | 41.2% | 91% |
| cases where a loss is reported | 4 | 0.52 | 0.07 | 15.2% | 6.6% | 100% |

A loss figure is reported only when P(physical loss) >= 0.6 (fraction reported: 0.36).

## 5. Experiment 4 - active diagnosis (Model 4)

| Policy | Mean checks | Mean cost | Mean hours | Final accuracy | Resolution rate |
|---|---|---|---|---|---|
| information_gain_per_cost | 4.71 | 15.47 | 10.09 | 0.815 | 0.538 |
| information_gain | 4.86 | 17.85 | 11.07 | 0.800 | 0.508 |
| random | 7.82 | 23.30 | 14.22 | 0.662 | 0.077 |
| fixed_order | 7.40 | 7.40 | 3.70 | 0.538 | 0.108 |

First-check expected information gain: 0.484 bits (selected) vs 0.047 bits (random). The information-gain-per-cost policy resolves 0.54 of cases vs 0.08 (random, 7.0x) and 0.11 (fixed order, 5.0x) with fewer checks; the fixed order is cheaper only because it never escalates to the expensive, decisive checks.

## 6. Uncertainty calibration (normal operation)

12 fresh normal runs x 8.0 h. z = residual / propagated sigma should be ~N(0, 1). The 120 s case thins the historian to one sample per 2 minutes per tag.

| Sampling | Row kind | n | mean z | sd z | within 2 sigma | within 3 sigma | max abs z |
|---|---|---|---|---|---|---|---|
| 10s | CV | 644 | -0.02 | 0.88 | 97.4% | 100.0% | 2.91 |
| 10s | DCONS | 151 | -0.18 | 1.08 | 94.7% | 96.7% | 3.67 |
| 10s | LEDGER | 242 | 0.00 | 0.01 | 100.0% | 100.0% | 0.04 |
| 10s | PUMP | 82 | -0.22 | 1.22 | 91.5% | 100.0% | 2.51 |
| 10s | TCONS | 148 | 0.23 | 0.84 | 100.0% | 100.0% | 1.62 |
| 10s | TICKET | 30 | 0.32 | 0.80 | 96.7% | 100.0% | 2.06 |
| 10s | all | 1297 | -0.01 | 0.85 | 97.5% | 99.6% | 3.67 |
| 120s | CV | 645 | -0.01 | 0.93 | 97.4% | 99.8% | 3.34 |
| 120s | DCONS | 155 | -0.15 | 1.12 | 94.2% | 96.1% | 3.74 |
| 120s | LEDGER | 244 | 0.38 | 0.70 | 91.4% | 100.0% | 2.53 |
| 120s | PUMP | 84 | -0.25 | 1.22 | 89.3% | 100.0% | 2.45 |
| 120s | TCONS | 150 | 0.23 | 0.83 | 100.0% | 100.0% | 1.69 |
| 120s | TICKET | 30 | 0.98 | 0.90 | 83.3% | 96.7% | 3.02 |
| 120s | all | 1308 | 0.08 | 0.95 | 95.3% | 99.4% | 3.74 |

chi2/dof: 0.723 (10 s), 0.918 (120 s). Sparse sampling is handled by sample-count-aware gauge read terms and a totalizer interpolation term at transfer boundaries (tickets / ledger), sized from the totalizer increment across the bracketing samples. At 120 s the ticket rows keep a positive bias (linear interpolation across a start-up ramp books part of the ramp before the ticket starts); the term bounds it to about 3 sigma but does not remove it.

## 7. Experiment 7 - ablation (uncalibrated posterior, held-out test cases)

Cases: env A 45, env C 12.

| Variant | A top-1 family | A top-3 cause | A mean log-loss | C confident known-cause | C abstained (UNKNOWN) |
|---|---|---|---|---|---|
| full_model | 0.644 | 0.800 | 1.960 | 0 | 3 |
| no_two_fault_hypotheses | 0.622 | 0.800 | 1.825 | 4 | 3 |
| no_accounting_records | 0.578 | 0.733 | 2.115 | 0 | 0 |
| no_temperature_density_consistency | 0.622 | 0.778 | 1.985 | 0 | 3 |
| no_pump_curve | 0.600 | 0.778 | 2.073 | 0 | 3 |
| control_volume_rows_only | 0.511 | 0.689 | 2.248 | 0 | 0 |
| no_unknown_catch_all | 0.644 | 0.800 | 1.960 | 0 | 0 |

## 8. Experiment 5 - agentic investigation (section 13.5)

16 full LangGraph investigations (12 known-fault cases), template narrator (no external LLM), verification outcomes sampled from each check's sensitivity / false-positive rate given the true cause.

| Metric | Value |
|---|---|
| initial_top1_rate | 0.833 |
| initial_top3_rate | 1.0 |
| resolved_rate | 0.75 |
| resolved_correct_rate | 0.75 |
| wrong_resolutions | 0 |
| confident_wrong_cases | 0 |
| mean_checks_known | 2.25 |
| grounding_pass_rate | 1.0 |
| evidence_completeness_mean | 1.0 |
| tool_error_rate | 0.0 |
| investigation_wall_s_median | 1.79 |
| final status counts | {'RESOLVED_PENDING_APPROVAL': 11, 'AWAITING_VERIFICATION': 3, 'AWAITING_REVIEW': 2} |
| normal runs final status | ['AWAITING_REVIEW', 'AWAITING_VERIFICATION'] |

**Fault family absent from training (density-meter bias):** 2/2 investigations ended on the true instrument (A-DENSITY_BIAS-11: DENSITY_ERROR:DT-001 p=0.951, truth DENSITY_ERROR:DT-001; A-DENSITY_BIAS-12: DENSITY_ERROR:DT-002 p=0.969, truth DENSITY_ERROR:DT-002). No learned model saw a density fault, but the physics hypothesis catalogue derives a density-meter signature from the topology, so these cases generalise rather than abstain.

Behavioural checks:

- duplicate_event_attached_to_open_case: PASS
- missing_level_gauge_requests_data: PASS
- report_requires_engineer_approval: PASS

Further section 13.5 behaviours (invalid IDs, failed queries, unauthorised actions, competing hypotheses, ungrounded LLM output, audit history) are covered by `tests/test_agent.py` and `apps/backend/tests`.

## 9. Known limitations (honest gaps)

- Root-cause top-3 recall and attribution macro-F1 are below the section 14 targets. Most misses are physically low-SNR cases (small drifts, faults on idle equipment) whose effect is within measurement uncertainty in the case window; scoring on observable cases narrows but does not close the gap.
- Temperature/prior calibration improves log-loss but not the Brier score.
- Loss MAE over all physical-loss cases is far above 10%: small or ambiguous leaks are estimated under competing hypotheses. Where the model reports a loss (P(physical loss) >= 0.6) the median error is small.
- The instrument classifier misses small biases (bias recall is low in env A): biases below the reconciliation noise floor are not distinguishable from normal operation in a single window.
- Under different noise levels (env B) the learned detectors raise more false alarms; the chi-square detector, which uses the propagated uncertainty, is the most robust.
- Indistinguishable pairs (e.g. PL-002 vs PL-003 leaks while both drain into the same control volume) are reported as tied; only a field check separates them, which the active-diagnosis step recommends.
- At coarse historian resolution (2 min) transfer-ticket reconciliation rows are biased by linear totalizer interpolation across start-up / shut-down ramps (mean z about 1). The uncertainty term keeps them within about 3 sigma; a ramp-aware interpolation of the point estimate would remove the bias.
- No manual-investigation baseline was timed, so the preparation-time KPI is not assessed; dashboard latency and 3D frame rate are browser-side and not measured here.

