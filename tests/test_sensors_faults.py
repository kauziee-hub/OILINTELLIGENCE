"""Unit tests: sensor generator noise & fault behaviour, fault injection engine."""
import numpy as np
import pytest

from simulation.engine.simulator import Simulator
from simulation.faults.faults import FaultInjector, FaultSpec
from simulation.sensors.sensor import Instrument
from tests.conftest import base_scenario


def make(plant, sid, seed=0):
    return Instrument(sid, plant.sensors[sid], np.random.default_rng(seed))


def test_noise_statistics(plant):
    inst = make(plant, "PT-002")
    inst.sys_abs = 0.0
    vals = np.array([inst.measure(t, 5.0, 1.0) for t in range(4000)])
    assert vals.mean() == pytest.approx(5.0, abs=0.002)
    assert vals.std() == pytest.approx(plant.sensors["PT-002"].noise, rel=0.15)


def test_meter_bias_affects_measurement_not_physics(plant):
    inst = make(plant, "FM-002")
    inst.sys_rel = 0.0
    inst.fault.bias_rel = 0.02
    vals = np.array([inst.measure(t, 100.0, 1.0) for t in range(2000)])
    assert vals.mean() == pytest.approx(102.0, rel=1e-3)


def test_drift_grows_with_time(plant):
    inst = make(plant, "LT-001")
    inst.sys_abs = 0.0
    inst.fault.drift_abs_per_h = 0.01
    inst.fault.drift_start_s = 0.0
    early = np.mean([inst.measure(t, 5.0, 1.0) for t in range(0, 300)])
    late = np.mean([inst.measure(t, 5.0, 1.0) for t in range(36000, 36300)])
    assert late - early == pytest.approx(0.1, abs=0.01)


def test_stuck_and_dropout(plant):
    inst = make(plant, "TT-001")
    inst.measure(0, 30.0, 1.0)
    inst.fault.stuck = True
    frozen = inst.measure(1, 31.0, 1.0)
    assert inst.measure(2, 35.0, 1.0) == frozen
    inst.fault.stuck = False
    inst.fault.dropout = True
    assert not any(inst.due(t) for t in range(10, 100))


def test_low_flow_cutoff_and_quantisation(plant):
    inst = make(plant, "FM-003")
    inst.sys_rel = 0.0
    assert inst.measure(0, 1.0, 1.0) == 0.0              # below 2 m3/h cut-off
    v = inst.measure(1, 87.123456, 1.0)
    assert abs(v * 100 - round(v * 100)) < 1e-6           # 0.01 resolution


def test_fault_injector_targets(plant):
    sim = Simulator(base_scenario(seed=5, hours=0.2))
    fi = FaultInjector([
        FaultSpec(fault_type="VALVE_PASSING", target="XV-003", start_s=0, params={"passing_fraction": 0.003}),
        FaultSpec(fault_type="PUMP_DEGRADATION", target="PU-002", start_s=0, params={"head_loss_frac": 0.1}),
        FaultSpec(fault_type="PIPELINE_LEAK", target="PL-001", start_s=0, params={"position": 0.4}),
        FaultSpec(fault_type="METER_BIAS", target="FM-001", start_s=100, params={"bias_rel": 0.03}),
    ])
    leaks, tank = fi.apply(50.0, sim.instruments, sim.valves, sim.pumps)
    assert sim.valves["XV-003"].passing_fraction == pytest.approx(0.003)
    assert sim.pumps["PU-002"].head_factor == pytest.approx(0.9)
    assert "PL-001" in leaks and leaks["PL-001"][0] == pytest.approx(0.4)
    assert sim.instruments["FM-001"].fault.bias_rel == 0.0       # not yet active
    fi.apply(150.0, sim.instruments, sim.valves, sim.pumps)
    assert sim.instruments["FM-001"].fault.bias_rel == pytest.approx(0.03)


def test_missing_transaction_omits_record_but_moves_oil():
    sc = base_scenario(seed=8, hours=2.0, faults=[FaultSpec(fault_type="MISSING_TRANSACTION", target="T-1",
                                                            start_s=900)])
    sim = Simulator(sc)
    sim.run()
    assert "T-1" not in sim.records.transfers                     # no record...
    assert sim.truth[-1]["TK-002.mass_t"] - sim.truth[0]["TK-002.mass_t"] > 50   # ...but oil moved


def test_unknown_fault_type_rejected():
    with pytest.raises(ValueError):
        FaultInjector([FaultSpec(fault_type="ALIEN_PROBE", target="TK-001")])
