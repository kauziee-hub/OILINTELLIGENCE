"""Unit tests: topology control volumes, residual calculation, uncertainty propagation."""
import numpy as np
import pytest

from intelligence.mass_balance.topology import TopologyBuilder
from intelligence.uncertainty.propagation import assemble, window_statistics
from tests.conftest import evidence_for


def test_topology_route_r01(plant):
    topo = TopologyBuilder(plant).build({"XV-001", "XV-002"}, {"FM-001", "FM-002"})
    cvs = topo.cvs
    assert "CV:TK-001" in cvs and cvs["CV:TK-001"].meters_out == ["FM-001"]
    assert "CV:MANIFOLD" in cvs
    assert sorted(cvs["CV:MANIFOLD"].meters_in) == ["FM-001"]
    assert sorted(cvs["CV:MANIFOLD"].meters_out) == ["FM-002"]
    assert cvs["CV:TK-002"].meters_in == ["FM-002"]
    cut_valves = {c.valve_id for c in topo.valve_cuts}
    assert cut_valves == {"XV-003", "XV-004"}


def test_topology_route_r03_merges_tank_and_manifold(plant):
    topo = TopologyBuilder(plant).build({"XV-004", "XV-003"}, {"FM-003"})
    assert "CV:TK-002+MANIFOLD" in topo.cvs
    assert topo.cvs["CV:TK-002+MANIFOLD"].meters_out == ["FM-003"]


def test_inactive_meter_is_not_a_cut(plant):
    topo = TopologyBuilder(plant).build({"XV-001", "XV-002"}, {"FM-001"})
    # FM-002 registered no flow -> manifold and TK-002 merge into one control volume
    assert any("TK-002" in k and "MANIFOLD" in k for k in topo.cvs)


def test_sum_of_cv_residuals_equals_facility_residual(normal_run, plant):
    ev = evidence_for(normal_run, plant, 600, 3 * 3600)
    for w in ev.windows:
        cv_sum = sum(r.value for r in w.rows if r.kind == "CV")
        inv0, inv1 = ev.inventories[w.index], ev.inventories[w.index + 1]
        fac = -sum(inv1[t]["mass_t"] - inv0[t]["mass_t"] for t in plant.tanks)
        assert cv_sum == pytest.approx(fac, abs=0.6)   # equal up to the in-window density propagation


def test_normal_operation_residuals_within_uncertainty(normal_run, plant):
    ev = evidence_for(normal_run, plant, 600, 3.5 * 3600)
    A = assemble(ev)
    c2, n, p = A.chi2()
    assert p > 0.01
    assert np.max(np.abs(A.z)) < 4.0


def test_leak_produces_significant_residual(leak_run, plant):
    ev = evidence_for(leak_run, plant, 3600 + 600, 3.5 * 3600)
    stats = window_statistics(ev)
    assert any(s["max_abs_z"] > 4 for s in stats)
    assert sum(s["facility_residual_t"] for s in stats) > 0.5   # positive = apparent loss


def test_uncertainty_propagation_matches_monte_carlo(normal_run, plant):
    ev = evidence_for(normal_run, plant, 1800, 3 * 1800)
    A = assemble(ev)
    rng = np.random.default_rng(0)
    e = rng.normal(0.0, 1.0, (20000, len(A.sigma_e))) * A.sigma_e
    sim = e @ A.J.T
    assert np.allclose(sim.std(0), A.sigma_r, rtol=0.05)


def test_ticket_skipped_when_transfer_starts_before_data(normal_run, plant):
    """Regression: a ticket must not be reconciled against a partial meter total."""
    from intelligence.mass_balance.engine import BalanceEngine
    from intelligence.physics.measurement import MeasurementModel, RecordsView, TelemetryStore, to_epoch
    st = to_epoch(normal_run.scenario.start_time)
    tel = normal_run.telemetry
    late = tel[to_epoch(tel["timestamp"]) >= st + 2 * 3600]      # data begins mid-transfer
    mm = MeasurementModel(plant, TelemetryStore(late),
                          RecordsView(transfers=normal_run.transfers, lab_samples=normal_run.lab_samples))
    ev = BalanceEngine(plant, mm).evidence([st + t for t in np.arange(2.25 * 3600, 4 * 3600 - 60, 1800)])
    assert not [r for r in ev.rows if r.kind == "TICKET"]
    assert not mm.meter_covers("FM-002", st + 900, st + 3 * 3600)


def test_sparse_sampling_widens_boundary_uncertainty(normal_run, plant):
    from intelligence.physics.measurement import MeasurementModel, RecordsView, TelemetryStore, to_epoch
    st = to_epoch(normal_run.scenario.start_time)
    tel = normal_run.telemetry
    rv = RecordsView(transfers=normal_run.transfers)
    dense = MeasurementModel(plant, TelemetryStore(tel), rv)
    keep = tel.groupby(["sensor_id", "parameter"]).cumcount() % 12 == 0             # 10 s -> 2-minute historian
    sparse = MeasurementModel(plant, TelemetryStore(tel[keep]), rv)
    edge = [st + 900]
    assert dense.boundary_sampling_sd("FM-002", edge, st + 600, st + 3600) == 0.0      # calibration spacing
    assert sparse.boundary_sampling_sd("FM-002", edge, st + 600, st + 3600) > 0.3      # ~q*110s/sqrt(12)


def test_ticket_reconciliation_rows_present(normal_run, plant):
    ev = evidence_for(normal_run, plant, 600, 4 * 3600 - 60)
    tickets = [r for r in ev.rows if r.kind == "TICKET"]
    assert tickets and all(abs(r.value) < 0.3 for r in tickets)
