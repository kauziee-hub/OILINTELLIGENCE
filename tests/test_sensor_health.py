"""Instrument health: an idle meter is not a stuck meter."""
from agents.tools.backend import InMemoryBackend
from industrial.schemas.plant import get_plant
from intelligence.physics.measurement import to_epoch
from simulation.engine.export import run_scenario
from simulation.scenarios.demo import build_demo_scenario


def test_idle_meter_on_a_shut_line_is_not_reported_stuck():
    plant = get_plant()
    sc = build_demo_scenario(plant, "normal", sample_interval_s=60.0)
    sc.duration_s = 1.2 * 3600                          # R-01 transfer running; PL-003 (FM-003) shut
    run = run_scenario(sc, plant)
    t = to_epoch(sc.start_time) + 3600.0
    be = InMemoryBackend(run, plant=plant)
    be.set_now(t)
    de = be.engine
    rec = be.records(t - 86400, t)
    ctx = de.context(be.telemetry(t - 3 * 1800 - 900, t), rec)
    health = {r["sensor_id"]: r for r in de.sensor_health(de.evidence(ctx, t - 3 * 1800, t, 1800), ctx)}
    assert health["FM-003"]["predicted"] == "NORMAL" and health["FM-003"]["p_fault"] < 0.5
