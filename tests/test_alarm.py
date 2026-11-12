"""Alarm on-delay: confirmation must outlast the instruments' slow error, not just count windows."""
from industrial.schemas.plant import get_plant
from intelligence.alarm import AlarmOnDelay, instrument_memory_s


def test_instrument_memory_comes_from_the_plant_specification():
    assert instrument_memory_s(get_plant()) == 600.0        # level / flow gauges: AR noise, tau 600 s


def test_gauge_excursion_seen_twice_is_not_confirmed():
    # the live normal-operation false alarm: abnormal at 5.40 h and 5.48 h (5 min apart), normal at 5.57 h
    od = AlarmOnDelay(2, 600.0)
    t = 19_440.0
    assert not od.update(True, False, t)
    assert not od.update(True, False, t + 290)               # same slow gauge error, not new evidence
    assert not od.update(False, False, t + 600)
    assert od.count == 0 and od.since is None


def test_persistent_fault_is_confirmed_once_it_outlasts_the_gauge_memory():
    od = AlarmOnDelay(2, 600.0)
    assert [od.update(True, False, 300.0 * k) for k in range(4)] == [False, False, True, True]


def test_overwhelming_evidence_alarms_immediately():
    assert AlarmOnDelay(2, 600.0).update(True, True, 0.0)


def test_cusum_accumulates_a_slow_drift_that_per_window_tests_miss():
    from intelligence.alarm import SlowShiftCusum
    cs = SlowShiftCusum()
    hits = [cs.update({"CV:TK-002": 2.2, "CV:TK-001": 0.3}, 1800.0 * k) for k in range(1, 6)]
    assert hits[:3] == [[], [], []] and hits[3] == ["CV:TK-002"]        # z ~2.2 every window: flagged at the 4th


def test_cusum_uses_only_non_overlapping_windows_and_cv_rows():
    from intelligence.alarm import SlowShiftCusum
    cs = SlowShiftCusum()
    # evaluations every 5 min; the overlapping ones (z = 50) must be ignored, so the first hit is the 4th
    # non-overlapping window (k = 18), not the first overlapping evaluation
    hits = [k for k in range(40)
            if cs.update({"CV:TK-002": 2.2 if k % 6 == 0 else 50.0, "TCONS:TT-005": -9.0}, 300.0 * k)]
    assert hits[0] == 18
    assert cs.statistic().get("TCONS:TT-005-") is None                 # persistent-offset rows never accumulate


def test_cusum_stays_quiet_on_unit_variance_noise():
    import numpy as np
    from intelligence.alarm import SlowShiftCusum
    rng = np.random.default_rng(7)
    cs = SlowShiftCusum()
    alarms = sum(bool(cs.update({f"CV:R{i}": float(z) for i, z in enumerate(rng.normal(size=5))}, 1800.0 * k))
                 for k in range(1, 1001))                              # 500 plant-hours, 5 rows
    assert alarms == 0
