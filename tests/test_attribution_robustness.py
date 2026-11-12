"""Pairs must pay for their onset choice, as singles do: otherwise one noisy gauge read becomes a "second fault"."""
from agents.tools.backend import InMemoryBackend
from industrial.schemas.plant import get_plant
from intelligence.physics.measurement import to_epoch
from simulation.engine.export import run_scenario
from simulation.scenarios.demo import build_demo_scenario


def test_one_noisy_read_does_not_become_a_second_fault():
    # tank leak on TK-001; the 3.5-7.5 h window starts on a ~3 sigma LT-003 read (seed 4242). Before the fix
    # the pairs "TK-001 cause + TEMP_ERROR:TT-003" took 70 % and TT-003 out-ranked the real cause.
    plant = get_plant()
    sc = build_demo_scenario(plant, "tank_leak", sample_interval_s=60.0)
    sc.duration_s = 7.6 * 3600
    run = run_scenario(sc, plant)
    t = to_epoch(sc.start_time) + 7.5 * 3600
    be = InMemoryBackend(run, plant=plant)
    be.set_now(t)
    de = be.engine
    rec = be.records(t - 30 * 86400, t)
    ctx = de.context(be.telemetry(de.telemetry_start(rec, t - 4 * 3600, t), t), rec)
    res = de.attribute(de.evidence(ctx, t - 4 * 3600, t, 1800))
    top = max(res.scores, key=lambda s: s.marginal)
    assert top.hypothesis.hid in ("TANK_LEAK:TK-001", "LEVEL_ERROR:LT-001")
    assert all(p["posterior"] < 0.2 for p in res.pairs)
