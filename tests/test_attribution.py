"""Attribution, active diagnosis and learned-model tests."""
import numpy as np
import pandas as pd
import pytest

from intelligence.active_diagnosis.information_gain import ActiveDiagnosis, entropy, hypotheses_from_attribution
from intelligence.active_diagnosis.tests import action_by_id, build_actions
from intelligence.anomaly_detection.detectors import IsolationForestDetector, XGBoostDetector, \
    tune_threshold_for_false_alarms
from intelligence.evaluation.metrics import binary_metrics, log_loss_brier, regression_metrics, topk_accuracy
from intelligence.fault_attribution.bayesian import PhysicsBayesAttributor
from intelligence.fault_attribution.hypotheses import build_hypotheses
from intelligence.features.windows import window_features
from tests.conftest import evidence_for


def test_hypothesis_priors_normalised(plant):
    hs = build_hypotheses(plant)
    assert sum(h.prior for h in hs) == pytest.approx(1.0)
    assert {"NORMAL", "UNKNOWN", "PIPELINE_LEAK", "METER_ERROR", "MISSING_TRANSFER"} <= {h.family for h in hs}
    hs2 = build_hypotheses(plant, exclude_families={"DENSITY_ERROR"})
    assert "DENSITY_ERROR" not in {h.family for h in hs2}


def test_normal_run_attributed_normal(normal_run, plant):
    r = PhysicsBayesAttributor(plant).attribute(evidence_for(normal_run, plant, 600, 3.5 * 3600))
    assert r.ranked(1)[0].hypothesis.family == "NORMAL"
    assert r.estimated_physical_loss_t is None             # no defensible loss estimate
    assert sum(r.family_distribution().values()) == pytest.approx(1.0)


def test_leak_attributed_to_physical_loss(leak_run, plant):
    r = PhysicsBayesAttributor(plant).attribute(evidence_for(leak_run, plant, 1800, 3.9 * 3600))
    top3 = [s.hypothesis.hid for s in r.ranked(3)]
    assert "PIPELINE_LEAK:PL-002" in top3
    assert r.category_probs["physical_loss"] > 0.6
    true_loss = leak_run.truth["physical_loss_cum_t"].iloc[-1]
    assert r.conditional_loss_t == pytest.approx(true_loss, rel=0.5)
    d = r.decomposition
    assert d["total_residual_R"] == pytest.approx(d["L_physical_loss"] + d["B_measurement_bias"] +
                                                  d["U_unrecorded_movement"] + d["epsilon"], abs=2e-3)  # 3-dp rounding


def test_information_gain_prefers_discriminating_check(plant):
    hs = {h.hid: h for h in build_hypotheses(plant)}
    H = [hs["PIPELINE_LEAK:PL-003"], hs["PIPELINE_LEAK:PL-002"], hs["NORMAL"]]
    p = np.array([0.45, 0.45, 0.10])
    ad = ActiveDiagnosis(plant)
    ranked = ad.rank(H, p, top=5)
    assert ranked[0].action.action_type == "LINE_INSPECTION"
    assert ranked[0].expected_information_gain > 0.4
    post = ad.update(H, p, "LINE_INSPECTION:PL-003", "POSITIVE")
    assert post[0] > 0.9 and entropy(post) < entropy(p)


def test_isolation_tests_unavailable_during_transfer(plant):
    hs = {h.hid: h for h in build_hypotheses(plant)}
    H = [hs["TANK_LEAK:TK-001"], hs["NORMAL"]]
    ranked = ActiveDiagnosis(plant).rank(H, np.array([0.5, 0.5]), transfer_active=True, top=50)
    iso = [r for r in ranked if r.action.spec.requires_isolation]
    assert iso and all(not r.available for r in iso)


def test_action_catalogue_ids(plant):
    acts = build_actions(plant)
    assert len({a.action_id for a in acts}) == len(acts)
    assert action_by_id(plant, "RETRIEVE_METER_PROVING_RECORD:FM-002").target == "FM-002"


def test_learned_detectors_train_and_score(normal_run, leak_run, plant):
    fn = pd.DataFrame(window_features(plant, evidence_for(normal_run, plant, 600, 3.5 * 3600)))
    fl = pd.DataFrame(window_features(plant, evidence_for(leak_run, plant, 600, 3.5 * 3600)))
    df = pd.concat([fn.assign(y=0), fl.assign(y=(fl.t0 >= fl.t0.min() + 3000).astype(int))])
    iso = IsolationForestDetector().fit(df[df.y == 0])
    assert len(iso.score(df)) == len(df)
    xgb = XGBoostDetector().fit(df, df.y.to_numpy())
    s = xgb.score(df)
    assert s[df.y.to_numpy() == 1].mean() > s[df.y.to_numpy() == 0].mean()
    assert np.isfinite(tune_threshold_for_false_alarms(s, 5.0))


def test_metrics():
    m = binary_metrics([0, 0, 1, 1], [0.1, 0.6, 0.7, 0.9], 0.5)
    assert m["tp"] == 2 and m["fp"] == 1 and m["false_alarms_per_100_normal"] == 50.0
    assert topk_accuracy([["A"], ["B"]], [["A", "C"], ["C", "B"]], 1) == 0.5
    ll = log_loss_brier([{"A": 0.9, "B": 0.1}, {"A": 0.2, "B": 0.8}], ["A", "B"], ["A", "B"])
    assert ll["brier"] < 0.1
    r = regression_metrics([1, 2], [1.1, 1.8], [0.5, 1.5], [1.5, 2.5])
    assert r["interval_coverage"] == 1.0 and r["mae"] == pytest.approx(0.15)


def test_bounded_magnitude_correction():
    """[0, inf) reduces to the half-normal correction; a bound below the fitted magnitude penalises."""
    import math

    from scipy.special import log_ndtr

    from intelligence.fault_attribution.bayesian import bounded_magnitude_correction as corr
    assert corr(1.3, 0.2, 4.0) == pytest.approx(math.log(2) + float(log_ndtr(1.3 / math.sqrt(0.2))))
    assert corr(1.3, 0.2, 4.0, upper=1e6) == pytest.approx(corr(1.3, 0.2, 4.0), abs=1e-9)
    # data say theta ~ 3 +- 0.1, physics says theta <= 1: the hypothesis loses most of its support
    assert corr(3.0, 0.01, 4.0, upper=1.0) < corr(3.0, 0.01, 4.0) - 50
    # a bound well above the fitted magnitude costs nothing on the data side and gains prior mass
    assert corr(0.5, 0.01, 4.0, upper=5.0) > corr(0.5, 0.01, 4.0)


def test_silent_meter_bounds_passing_valve(plant, leak_run):
    """PL-002 leak on route R-01: XV-003 is shut and FM-003 (upstream, in series) reads nothing, so
    flow passing XV-003 is below FM-003's low-flow cut-off and cannot explain the manifold deficit."""
    ev = evidence_for(leak_run, plant, 1800, 3.9 * 3600)
    win = ev.windows[-1]
    assert "FM-003" in win.silent_meters
    from intelligence.fault_attribution.bayesian import PhysicsBayesAttributor
    att = PhysicsBayesAttributor(plant)
    assert att._valve_rate_bound("XV-003", win) < 10.0
    hd = att.attribute(ev).hypothesis_distribution()
    assert hd.get("VALVE_PASSING:XV-003", 0.0) < hd["PIPELINE_LEAK:PL-002"]
