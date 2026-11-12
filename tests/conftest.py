"""Shared fixtures: simulated runs are generated once per test session."""
from __future__ import annotations

import numpy as np
import pytest

from industrial.schemas.plant import get_plant
from intelligence.mass_balance.engine import BalanceEngine
from intelligence.physics.measurement import MeasurementModel, RecordsView, TelemetryStore, to_epoch
from simulation.engine.export import run_scenario
from simulation.faults.faults import FaultSpec
from simulation.scenarios.scenario import Scenario, TankInitial, TransferOrder


@pytest.fixture(scope="session")
def plant():
    return get_plant()


def base_scenario(seed: int = 3, faults=None, hours: float = 4.0, route: str = "R-01") -> Scenario:
    return Scenario(
        scenario_id=f"TEST-{seed}", seed=seed, duration_s=hours * 3600, dt_s=2.0, sample_interval_s=10.0,
        initial={"TK-001": TankInitial(level_m=12.0, temperature_c=36.0),
                 "TK-002": TankInitial(level_m=6.0, temperature_c=32.0),
                 "TK-003": TankInitial(level_m=4.0, temperature_c=30.0)},
        operations=[TransferOrder(transfer_id="T-1", route=route, start_s=900, setpoint_m3h=130,
                                  pumps=["PU-001"], target_volume_m3=420)],
        faults=faults or [])


@pytest.fixture(scope="session")
def normal_run():
    return run_scenario(base_scenario(), keep_sim=True)


@pytest.fixture(scope="session")
def leak_run():
    f = FaultSpec(fault_id="F-LEAK", fault_type="PIPELINE_LEAK", target="PL-002", start_s=3600,
                  params={"position": 0.5, "hole_diameter_mm": 8.0})
    return run_scenario(base_scenario(faults=[f]), keep_sim=True)


def evidence_for(run, plant, t_from: float, t_to: float, window: float = 1800.0):
    mm = MeasurementModel(plant, TelemetryStore(run.telemetry),
                          RecordsView(transfers=run.transfers, lab_samples=run.lab_samples))
    st = to_epoch(run.scenario.start_time)
    edges = [st + t for t in np.arange(t_from, t_to + 1e-6, window)]
    return BalanceEngine(plant, mm).evidence(edges)
