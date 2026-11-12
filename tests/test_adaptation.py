"""Adaptive layer: drift detection, self-calibration with governance, experience memory, twin replay."""
from types import SimpleNamespace

import numpy as np
import pytest

from intelligence.adaptation.controller import AdaptiveController
from intelligence.adaptation.drift import DriftMonitor, psi
from intelligence.adaptation.experience import ExperienceMemory
from intelligence.uncertainty.propagation import assemble
from tests.conftest import evidence_for

ROWS = ["CV:TK-001", "CV:TK-002", "CV:MANIFOLD"]


def feed(target, n=36, sd=1.0, shift=None, seed=0):
    rng = np.random.default_rng(seed)
    for i in range(n):
        z = {k: float(rng.normal(0.0, sd)) for k in ROWS}
        if shift:
            z[shift[0]] += shift[1]
        target.observe({"t1": 1000.0 * i, "z": z, "anomaly": False, "alarm_confirmed": False, "quality_issues": {},
                        "route": "R-01"}, {"ctx_throughput_t": 100.0})


def fake_engine():
    return SimpleNamespace(kind_scale={}, meter_factors={}, family_prior=None)


def controller(tmp_path, plant, cases=None):
    return AdaptiveController(fake_engine(), plant, tmp_path / "adaptive.json", reference=None,
                              cases_provider=(lambda: cases or []))


# ------------------------------------------------------------------ hooks
def test_kind_scale_inflates_only_that_row_kind(normal_run, plant):
    ev = evidence_for(normal_run, plant, 600, 3 * 3600)
    base = assemble(ev)
    ev.kind_scale = {"CV": 2.0}
    scaled = assemble(ev)
    cv = np.array([r.kind == "CV" for r in base.rows])
    assert np.allclose(scaled.sigma_r[cv], 2.0 * base.sigma_r[cv], rtol=1e-6)
    assert np.allclose(scaled.sigma_r[~cv], base.sigma_r[~cv], rtol=1e-6)


def test_meter_factor_scales_metered_mass(normal_run, plant):
    from intelligence.physics.measurement import MeasurementModel, RecordsView, TelemetryStore, to_epoch
    mm = MeasurementModel(plant, TelemetryStore(normal_run.telemetry), RecordsView(transfers=normal_run.transfers))
    st = to_epoch(normal_run.scenario.start_time)
    m0 = mm.meter_mass("FM-002", st + 1800, st + 5400)["mass_t"]
    mm.meter_factors = {"FM-002": 1.01}
    assert mm.meter_mass("FM-002", st + 1800, st + 5400)["mass_t"] == pytest.approx(1.01 * m0, rel=1e-9)


# ------------------------------------------------------------------ drift
def test_psi_zero_for_same_distribution_and_large_for_shift():
    rng = np.random.default_rng(1)
    ref = rng.normal(0, 1, 5000)
    edges = np.quantile(ref, np.linspace(0, 1, 11))
    p = np.histogram(ref, bins=edges)[0] / len(ref)
    assert psi(edges.tolist(), p.tolist(), rng.normal(0, 1, 2000)) < 0.05
    assert psi(edges.tolist(), p.tolist(), rng.normal(1.5, 1, 2000)) > 0.5


def test_drift_monitor_warms_up_then_reports_green_on_calibrated_residuals():
    m = DriftMonitor(None)
    feed(m, n=5)
    assert m.report()["status"] == "WARMING_UP"
    feed(m, n=36, sd=1.0, seed=3)
    rep = m.report()
    sig = {s["kind"]: s for s in rep["signals"]}
    assert sig["calibration"]["severity"] == "GREEN" and sig["sensor"]["severity"] == "GREEN"


def test_dispersion_increase_is_calibration_drift_not_sensor_drift():
    m = DriftMonitor(None)
    feed(m, sd=1.9, seed=4)
    sig = {s["kind"]: s for s in m.report()["signals"]}
    assert sig["calibration"]["severity"] in ("AMBER", "RED")
    assert sig["calibration"]["detail"]["CV"]["sd_scale"] == pytest.approx(1.9, rel=0.2)
    assert sig["sensor"]["severity"] == "GREEN"


def test_mean_shift_is_sensor_drift():
    m = DriftMonitor(None)
    feed(m, shift=("CV:TK-002", 3.0), seed=5)
    sensors = [s for s in m.report()["signals"] if s["kind"] == "sensor"]
    assert sensors[0]["severity"] in ("AMBER", "RED") and sensors[0]["detail"]["row"] == "CV:TK-002"


# ------------------------------------------------------------------ controller
def test_small_dispersion_change_is_self_calibrated_automatically(tmp_path, plant):
    c = controller(tmp_path, plant)
    feed(c, sd=1.3, n=72, seed=6)          # 72 overlapping windows ~ 36 independent samples
    cyc = c.run_cycle(use_twin=False)
    steps = {s["key"]: s for s in cyc["steps"]}
    assert steps["self_calibrate"]["status"] == "ok"
    assert 1.15 < c.engine.kind_scale["CV"] < 1.35               # applied to the engine, inside the auto band
    assert c.state["adaptations"][0]["status"] == "APPLIED"
    assert steps["retrain_if_necessary"]["detail"]["gate"]["status"] == "NOT_NEEDED"


def test_large_change_needs_approval_and_can_be_reverted(tmp_path, plant):
    c = controller(tmp_path, plant)
    feed(c, sd=1.8, n=60, seed=7)
    c.run_cycle(use_twin=False)
    a = c.state["adaptations"][0]
    assert a["status"] == "PROPOSED" and "CV" not in c.engine.kind_scale
    c.decide(a["id"], "APPROVE", "eng")
    assert c.engine.kind_scale["CV"] == pytest.approx(a["new"])
    c.decide(a["id"], "REVERT", "eng")
    assert "CV" not in c.engine.kind_scale
    with pytest.raises(ValueError):
        c.decide(a["id"], "REVERT", "eng")
    # state survives a restart
    c2 = controller(tmp_path, plant)
    assert c2.state["adaptations"][0]["status"] == "REVERTED"


def test_mean_shift_opens_investigation_instead_of_calibrating(tmp_path, plant):
    c = controller(tmp_path, plant)
    feed(c, shift=("CV:TK-002", 3.2), n=60, seed=8)
    cyc = c.run_cycle(use_twin=False)
    steps = {s["key"]: s for s in cyc["steps"]}
    chosen = steps["run_optimisation"]["summary"]
    assert "OPEN_INVESTIGATION" in chosen
    assert not c.engine.kind_scale                               # a fault signature is never absorbed


def test_meter_proving_proposes_meter_factor(tmp_path, plant):
    c = controller(tmp_path, plant)
    assert c.on_verification("RETRIEVE_METER_PROVING_RECORD:FM-002", "NEGATIVE", {"meter_factor": 1.0005}) is None
    a = c.on_verification("RETRIEVE_METER_PROVING_RECORD:FM-002", "POSITIVE",
                          {"meter_factor": 1.012, "as_found_error_pct": -1.2})
    assert a["status"] == "PROPOSED" and not c.engine.meter_factors
    c.decide(a["id"], "APPROVE", "eng")
    assert c.engine.meter_factors["FM-002"] == pytest.approx(1.012)


# ------------------------------------------------------------------ experience
def test_experience_priors_are_bounded_and_normalised():
    mem = ExperienceMemory()
    cases = [{"case_id": f"C{i}", "verified_cause": "PIPELINE_LEAK:PL-002", "residual": 4.0,
              "candidate_faults": [{"hypothesis_id": "PIPELINE_LEAK:PL-002", "calibrated_probability": 0.6},
                                   {"hypothesis_id": "PIPELINE_LEAK:PL-003", "calibrated_probability": 0.4}]}
             for i in range(60)]
    mem.update(cases)
    pr = mem.priors()
    assert sum(pr.values()) == pytest.approx(1.0)
    assert pr["PIPELINE_LEAK"] > mem.base["PIPELINE_LEAK"]
    assert pr["PIPELINE_LEAK"] <= 3.0 * mem.base["PIPELINE_LEAK"] + 1e-6    # experience nudges, never dominates
    assert all(v >= mem.base[f] / 3.0 - 1e-6 for f, v in pr.items())
    sim = mem.similar({"PIPELINE_LEAK:PL-002": 0.55, "PIPELINE_LEAK:PL-003": 0.45}, residual_t=3.8)
    assert sim and sim[0]["verified_cause"] == "PIPELINE_LEAK:PL-002" and sim[0]["similarity"] > 0.9


def test_controller_feeds_learned_priors_to_engine(tmp_path, plant):
    cases = [{"case_id": "C1", "verified_cause": "METER_ERROR:FM-002"}]
    c = controller(tmp_path, plant, cases)
    assert c.engine.family_prior is not None and c.engine.family_prior["METER_ERROR"] > \
        c.memory.base["METER_ERROR"]


# ------------------------------------------------------------------ twin
def test_twin_replay_supports_leak_and_rejects_normal(leak_run, plant):
    from intelligence.adaptation.twin import TwinValidator
    from intelligence.physics.measurement import to_epoch
    from intelligence.service import DiagnosticEngine
    de = DiagnosticEngine(plant)
    rec = {"transfers": leak_run.transfers, "lab_samples": leak_run.lab_samples}
    ctx = de.context(leak_run.telemetry, rec)
    st = to_epoch(leak_run.scenario.start_time)
    ev = de.evidence(ctx, st + 3600, st + 3.5 * 3600)
    hyps = de.attribute(ev).to_dict(top=5)["hypotheses"]
    out = TwinValidator(plant, de).validate(leak_run.telemetry, rec, ev.t_edges, hyps, max_hypotheses=2)
    res = {r["hypothesis_id"]: r for r in out["results"] if r.get("replayed")}
    assert res["NORMAL"]["chi2_per_dof"] > 3.0 and not res["NORMAL"]["consistent"]
    assert out["best_supported"].startswith("PIPELINE_LEAK:")
    assert res[out["best_supported"]]["consistent"]


def test_one_sided_ledger_rows_are_not_a_calibration_signal():
    """Unbooked-movement rows are clipped at zero (z ~ 0 normally) - never 'over-conservative'."""
    m = DriftMonitor(None)
    rng = np.random.default_rng(9)
    for i in range(60):
        z = {k: float(rng.normal(0, 1)) for k in ROWS}
        z["LEDGER:FM-001"] = 0.001
        m.observe({"t1": i, "z": z, "anomaly": False, "alarm_confirmed": False, "quality_issues": {}}, {})
    cal = next(s for s in m.report()["signals"] if s["kind"] == "calibration")
    assert "LEDGER" not in cal["detail"] and cal["severity"] == "GREEN"


def test_distribution_drift_waits_for_enough_independent_windows():
    ref = {"features": {"ctx_throughput_t": {"edges": [0, 50, 100, 150], "p": [0.3, 0.4, 0.3], "q01": 1, "q99": 149,
                                             "mean": 75, "sd": 30}}, "routes_seen": ["R-01"]}
    m = DriftMonitor(ref)
    feed(m, n=30)                                     # all at 100 t: a narrow slice of the reference
    data = next(s for s in m.report()["signals"] if s["kind"] == "data")
    assert data["severity"] == "GREEN" and data["statistic"] is None


def test_repeated_cycles_do_not_duplicate_pending_proposals(tmp_path, plant):
    c = controller(tmp_path, plant)
    for k in range(3):
        feed(c, sd=1.8, n=72, seed=20 + k)
        c.run_cycle(use_twin=False)
    pending = [a for a in c.state["adaptations"] if a["status"] == "PROPOSED" and a["target"] == "CV"]
    assert len(pending) == 1


def test_persistent_systematic_offset_counts_as_one_draw():
    """A reference error repeats unchanged in every window (z ~ -1.3 here): one systematic draw,
    not 18 independent ones, so it is neither calibration drift nor sensor drift."""
    m = DriftMonitor(None)
    rng = np.random.default_rng(11)
    for i in range(60):
        z = {k: float(rng.normal(0, 1)) for k in ROWS}
        z["DCONS:DT-002"] = 2.0 if i < 4 else -1.3
        m.observe({"t1": i, "z": z, "anomaly": False, "alarm_confirmed": False, "quality_issues": {}}, {})
    sig = {s["kind"]: s for s in m.report()["signals"]}
    assert sig["calibration"]["severity"] == "GREEN"
    assert sig["sensor"]["severity"] == "GREEN"
