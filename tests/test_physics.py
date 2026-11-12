"""Unit tests: tank inventory, density model, hydraulics, pumps, valves (section 13.1)."""
import math

import numpy as np
import pytest

from simulation.engine.simulator import Simulator
from simulation.hydraulics.network import friction_factor
from simulation.process.properties import CrudeProperties, Stream
from simulation.pumps.pump import PumpModel
from simulation.tanks.tank import TankModel
from simulation.valves.valve import ValveModel
from tests.conftest import base_scenario

CRUDE = CrudeProperties(850.0, 0.00085, 25.0, 0.035, 1950.0)


def test_density_temperature_correction():
    assert CRUDE.density_at(15.0) == pytest.approx(850.0)
    # rho_T = rho_15 / (1 + alpha (T - 15))
    assert CRUDE.density_at(35.0) == pytest.approx(850.0 / (1 + 0.00085 * 20))
    assert CRUDE.vcf(35.0) * (1 + 0.00085 * 20) == pytest.approx(1.0)


def test_stream_mixing_conserves_extensive_quantities():
    a = Stream.from_props(CRUDE, 30.0, 10.0)
    b = Stream.from_props(CrudeProperties(862.0, 0.00082, 38.0, 0.038, 1930.0), 40.0, 5.0)
    m = a + b
    assert m.m == pytest.approx(15.0)
    assert m.rho15 == pytest.approx(15.0 / (10 / 850 + 5 / 862))
    assert 30.0 < m.temperature < 40.0


def test_tank_volume_level_mass_consistency(plant):
    t = TankModel("TK-002", plant.tanks["TK-002"], CRUDE, level_m=5.0, temperature_c=30.0, product_id="CRUDE-A")
    assert t.level == pytest.approx(5.0, abs=1e-9)
    v_obs = plant.tanks["TK-002"].area_m2 * 5.0
    assert t.mass == pytest.approx(v_obs * CRUDE.vcf(30.0) * 850.0)


def test_tank_mass_balance_step(plant):
    t = TankModel("TK-002", plant.tanks["TK-002"], CRUDE, 5.0, 30.0, "CRUDE-A")
    m0 = t.mass
    inflow = Stream.from_props(CRUDE, 30.0, 20.0)       # kg/s
    t.step(10.0, [inflow], outflow_mass_rate=5.0, loss_mass_rate=1.0, ambient_c=30.0)
    # dM = (m_in - m_out - m_loss) dt
    assert t.mass - m0 == pytest.approx((20.0 - 5.0 - 1.0) * 10.0, rel=1e-12)


def test_thermal_expansion_changes_level_not_mass(plant):
    t = TankModel("TK-003", plant.tanks["TK-003"], CRUDE, 6.0, 20.0, "CRUDE-A")
    m0, l0 = t.mass, t.level
    t.contents.h += t.contents.cp_m * 10.0          # heat by 10 degC, no flow
    assert t.mass == pytest.approx(m0)
    assert t.level > l0                              # expansion: level rises with constant mass


def test_friction_factor_regimes():
    assert friction_factor(1000, 2e-4) == pytest.approx(0.064)
    f_turb = friction_factor(1e5, 2e-4)
    assert 0.015 < f_turb < 0.03


def test_pump_curve_and_inverse(plant):
    p = PumpModel("PU-001", plant.pumps["PU-001"])
    p.commanded_on = True
    for _ in range(10):
        p.step(1.0)
    assert p.head_at(150.0) == pytest.approx(85.0 - 6.6667e-4 * 150 ** 2, rel=1e-4)
    q = p.flow_for_head(p.head_at(120.0))
    assert q == pytest.approx(120.0, rel=1e-3)
    p.head_factor = 0.85                              # degradation lowers the curve
    assert p.head_at(120.0) < 0.86 * (85.0 - 6.6667e-4 * 120 ** 2) + 1e-6


def test_valve_characteristic_and_passing(plant):
    v = ValveModel("XV-002", plant.valves["XV-002"])
    assert math.isinf(v.k_value())                   # shut and tight
    v.passing_fraction = 0.002
    assert v.k_value() == pytest.approx(4.0 / 0.002 ** 2)
    v.command(True, 1.0)
    for _ in range(40):
        v.step(1.0)
    assert v.position == pytest.approx(1.0)
    assert v.k_value() == pytest.approx(4.0)


def test_simulator_conserves_mass_exactly(normal_run):
    err = normal_run.truth["conservation_error_kg"].abs().max()
    assert err < 1e-3


def test_simulator_conservation_with_leak(leak_run):
    tr = leak_run.truth
    assert tr["conservation_error_kg"].abs().max() < 1e-3
    assert tr["physical_loss_cum_t"].iloc[-1] > 0.5   # leak removed mass from the system


def test_pressure_flow_consistency_during_transfer(normal_run):
    tr = normal_run.truth
    running = tr[tr["transfer_state"] == "RUNNING"]
    assert len(running) > 10
    # flow controller holds the setpoint, discharge pressure plausible (3-8 bar band)
    assert running["PU-001.flow_m3h"].median() == pytest.approx(130.0, rel=0.03)
    assert 3.0 < running["PU-001-PD.true"].median() < 8.0


def test_deterministic_replay():
    a = Simulator(base_scenario(seed=11, hours=0.5))
    b = Simulator(base_scenario(seed=11, hours=0.5))
    a.run()
    b.run()
    ma = np.array([r["TK-001.mass_t"] for r in a.truth])
    mb = np.array([r["TK-001.mass_t"] for r in b.truth])
    assert np.array_equal(ma, mb)
    assert a.instruments["LT-001"].last_value == b.instruments["LT-001"].last_value
