"""Simulator command-line interface.

  oil-sim run      --family PIPELINE_LEAK --seed 3 [--env A] [--hours 8] --out datasets/synthetic/x
  oil-sim run      --scenario config/scenarios/demo_leak.json --out ...
  oil-sim live     --mqtt-host localhost --mqtt-port 1883 [--speed 10] [--autostart]
  oil-sim validate                       # physics conservation self-check
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from industrial.connectors.bus import env_flag
from industrial.schemas.plant import get_plant


def cmd_run(a) -> None:
    from simulation.engine.export import run_scenario
    from simulation.scenarios.scenario import Scenario, generate_scenario
    plant = get_plant()
    if a.scenario:
        sc = Scenario.model_validate_json(Path(a.scenario).read_text(encoding="utf-8"))
    else:
        sc = generate_scenario(plant, a.family, a.seed, a.env, duration_s=a.hours * 3600, dt_s=a.dt,
                               sample_interval_s=a.sample)
    t0 = time.time()
    res = run_scenario(sc, plant)
    out = res.save(a.out)
    tr = res.truth.iloc[-1]
    print(json.dumps({"scenario": sc.scenario_id, "out": str(out), "telemetry_rows": len(res.telemetry),
                      "transfers": len(res.transfers), "faults": [f.fault_type + "@" + f.target for f in sc.faults],
                      "physical_loss_t": round(float(tr["physical_loss_cum_t"]), 3),
                      "conservation_error_kg": float(tr["conservation_error_kg"]),
                      "seconds": round(time.time() - t0, 1)}, indent=1))


def cmd_live(a) -> None:
    from industrial.connectors.bus import MqttBus
    from simulation.engine.live import LiveSimulation
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    bus = MqttBus(a.mqtt_host, a.mqtt_port, os.environ.get("MQTT_USERNAME"), os.environ.get("MQTT_PASSWORD"),
                  tls=env_flag("MQTT_TLS"), client_id="oilintel-simulator")
    if not bus.wait_connected(30):
        print(f"cannot reach MQTT broker {a.mqtt_host}:{a.mqtt_port}", file=sys.stderr)
    live = LiveSimulation(bus)
    live.speed = a.speed
    if a.autostart:
        live.start()
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    try:
        while not stop["flag"]:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    live.stop()
    bus.close()


def cmd_validate(a) -> None:
    from simulation.engine.export import run_scenario
    from simulation.scenarios.scenario import generate_scenario
    plant = get_plant()
    worst = 0.0
    for fam in ("NORMAL", "PIPELINE_LEAK", "VALVE_PASSING", "TANK_LEAK"):
        sc = generate_scenario(plant, fam, 1, "A", duration_s=4 * 3600, dt_s=2.0, sample_interval_s=10.0)
        res = run_scenario(sc, plant)
        err = float(res.truth["conservation_error_kg"].abs().max())
        worst = max(worst, err)
        print(f"{fam:16s} max |mass conservation error| = {err:.3e} kg")
    print("PASS" if worst < 1e-3 else "FAIL")
    sys.exit(0 if worst < 1e-3 else 1)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="oil-sim")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--family", default="NORMAL")
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--env", default="A")
    r.add_argument("--hours", type=float, default=8.0)
    r.add_argument("--dt", type=float, default=2.0)
    r.add_argument("--sample", type=float, default=10.0)
    r.add_argument("--scenario")
    r.add_argument("--out", required=True)
    lv = sub.add_parser("live")
    lv.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "localhost"))
    lv.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    lv.add_argument("--speed", type=float, default=float(os.environ.get("SIM_SPEED", "10")))
    lv.add_argument("--autostart", action="store_true", default=env_flag("SIM_AUTOSTART", True))
    sub.add_parser("validate")
    a = ap.parse_args(argv)
    {"run": cmd_run, "live": cmd_live, "validate": cmd_validate}[a.cmd](a)


if __name__ == "__main__":
    main()
