"""Live (real-time / accelerated) simulation service.

Runs the physics simulator in a background thread and publishes:
  * telemetry  -> oilintelligence/simulation/<type>/<asset>/<parameter>  (internal contract JSON)
  * records    -> oilintelligence/events/transfers, oilintelligence/events/records/<kind>
  * truth      -> oilintelligence/truth/state, /truth/faults            (restricted; evaluation only)
  * status     -> oilintelligence/simulation/status (retained)
and accepts commands on oilintelligence/simulation/control (start/pause/resume/stop/speed/
inject_fault/clear_fault/add_transfer/perform_check). The same class is used
in-process by the backend and standalone by `oil-sim live` (Docker service).
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from industrial.connectors.bus import TelemetryBus
from industrial.mqtt import topics as T
from industrial.schemas.plant import PlantConfig, get_plant
from simulation.engine.simulator import Simulator
from simulation.faults.faults import FaultSpec
from simulation.scenarios.scenario import Scenario, TransferOrder, generate_schedule, random_initial

log = logging.getLogger("oilintel.sim.live")


def default_live_scenario(plant: PlantConfig, seed: int = 7, duration_h: float = 48.0,
                          start: datetime | None = None) -> Scenario:
    rng = np.random.default_rng(seed)
    initial = random_initial(plant, rng)
    orders = generate_schedule(plant, rng, duration_h * 3600, initial, prefix="L")
    start = start or datetime.now(timezone.utc).replace(microsecond=0)
    return Scenario(scenario_id=f"LIVE-{start:%Y%m%d%H%M}", family="LIVE", environment="LIVE", seed=seed,
                    description="Live operating schedule (inject faults from the Simulation Lab)",
                    start_time=start, duration_s=duration_h * 3600, dt_s=1.0, sample_interval_s=1.0,
                    gt_interval_s=10.0, initial=initial, operations=orders, faults=[])


class LiveSimulation:
    def __init__(self, bus: TelemetryBus, plant: PlantConfig | None = None):
        self.bus = bus
        self.plant = plant or get_plant()
        self.sim: Simulator | None = None
        self.run_id: str | None = None
        self.speed = 10.0
        self.state = "STOPPED"            # STOPPED | RUNNING | PAUSED | FINISHED
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._published_records: set[str] = set()
        self._published_transfer_state: dict[str, str] = {}
        self.wall_started: float | None = None
        # earliest start time of the next run: data time never goes backwards across runs
        self.clock_floor: datetime | None = None
        self.steps_per_s = 0.0
        self.last_step_ms = 0.0
        self.bus.subscribe(T.SIM_CONTROL, self._on_control)

    # ----------------------------------------------------------------- control
    def start(self, scenario: Scenario | None = None, speed: float | None = None,
              not_before: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            self.stop()
            # An accelerated run runs ahead of the wall clock, so a new run starting "now" (or a
            # generated scenario's fixed start date) would write telemetry over timestamps that
            # history already holds, and time-window queries would mix the two runs.
            floor = max(t for t in (datetime.now(timezone.utc).replace(microsecond=0), self.clock_floor, not_before)
                        if t is not None)
            sc = scenario or default_live_scenario(self.plant, start=floor)
            st = sc.start_time if sc.start_time.tzinfo else sc.start_time.replace(tzinfo=timezone.utc)
            if st < floor:
                sc = sc.model_copy(update={"start_time": floor})
            self.speed = float(speed or self.speed)
            sc = sc.model_copy(update={"sample_interval_s": max(sc.sample_interval_s, self._sample_interval())})
            self.sim = Simulator(sc, self.plant)
            self.run_id = f"RUN-{uuid.uuid4().hex[:8].upper()}"
            self._published_records.clear()
            self._published_transfer_state.clear()
            self.state = "RUNNING"
            self._stop.clear()
            self.wall_started = time.time()
            self.bus.publish(f"{T.ROOT}/events/runs", {"event_type": "RUN_STARTED", "run_id": self.run_id,
                                                       "scenario": sc.model_dump(mode="json"), "speed": self.speed})
            self._publish_records(force=True)
            self._thread = threading.Thread(target=self._loop, name="live-sim", daemon=True)
            self._thread.start()
            self._publish_status()
            return self.status()

    def pause(self):
        if self.state == "RUNNING":
            self.state = "PAUSED"
        self._publish_status()
        return self.status()

    def resume(self):
        if self.state == "PAUSED":
            self.state = "RUNNING"
        self._publish_status()
        return self.status()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=5)
        if self.sim is not None:
            end = self.sim.timestamp() + timedelta(seconds=60)
            self.clock_floor = max(self.clock_floor, end) if self.clock_floor else end
        if self.sim is not None and self.state in ("RUNNING", "PAUSED"):
            self.bus.publish(f"{T.ROOT}/events/runs", {"event_type": "RUN_STOPPED", "run_id": self.run_id,
                                                       "sim_time_s": self.sim.t})
        if self.state != "STOPPED":
            self.state = "STOPPED"
            self._publish_status()
        return self.status()

    def set_speed(self, speed: float):
        self.speed = float(min(max(speed, 0.25), 120.0))
        if self.sim:
            si = self._sample_interval()
            for inst in self.sim.instruments.values():
                inst.set_sample_interval(si)
        self._publish_status()
        return self.status()

    def _sample_interval(self) -> float:
        # keep ~1 publication per instrument per wall-second at any speed (bounded message rate)
        return max(1.0, self.speed)

    def inject_fault(self, fault: FaultSpec | dict[str, Any]) -> dict[str, Any]:
        if self.sim is None:
            raise RuntimeError("simulation not running")
        f = fault if isinstance(fault, FaultSpec) else FaultSpec(**fault)
        if f.start_s <= 0:
            f.start_s = self.sim.t
        self.sim.inject_fault(f)
        self.bus.publish(f"{T.TRUTH_ROOT}/faults", {"event_type": "FAULT_INJECTED", "run_id": self.run_id,
                                                    "fault": f.model_dump(), "sim_t": self.sim.t,
                                                    "timestamp": self.sim.timestamp().isoformat()})
        return f.model_dump()

    def clear_fault(self, fault_id: str) -> dict[str, Any]:
        if self.sim is None:
            raise RuntimeError("simulation not running")
        self.sim.clear_fault(fault_id)
        self.bus.publish(f"{T.TRUTH_ROOT}/faults", {"event_type": "FAULT_CLEARED", "run_id": self.run_id,
                                                    "fault_id": fault_id, "sim_t": self.sim.t,
                                                    "timestamp": self.sim.timestamp().isoformat()})
        return {"cleared": fault_id}

    def add_transfer(self, order: TransferOrder | dict[str, Any]) -> dict[str, Any]:
        if self.sim is None:
            raise RuntimeError("simulation not running")
        o = order if isinstance(order, TransferOrder) else TransferOrder(**order)
        if o.start_s <= 0:
            o.start_s = self.sim.t + 5
        self.sim.add_transfer(o)
        return o.model_dump()

    def perform_check(self, action_type: str, target: str | None) -> dict[str, Any]:
        if self.sim is None:
            raise RuntimeError("simulation not running")
        with self._lock:
            return self.sim.perform_check(action_type, target)

    def _on_control(self, topic: str, msg: dict[str, Any]) -> None:
        """Remote control (standalone simulator service)."""
        cmd = msg.get("command")
        try:
            if cmd == "start":
                sc = Scenario(**msg["scenario"]) if msg.get("scenario") else None
                nb = datetime.fromisoformat(msg["not_before"]) if msg.get("not_before") else None
                res = self.start(sc, msg.get("speed"), nb)
            elif cmd == "pause":
                res = self.pause()
            elif cmd == "resume":
                res = self.resume()
            elif cmd == "stop":
                res = self.stop()
            elif cmd == "speed":
                res = self.set_speed(msg["speed"])
            elif cmd == "inject_fault":
                res = self.inject_fault(msg["fault"])
            elif cmd == "clear_fault":
                res = self.clear_fault(msg["fault_id"])
            elif cmd == "add_transfer":
                res = self.add_transfer(msg["order"])
            elif cmd == "perform_check":
                res = self.perform_check(msg["action_type"], msg.get("target"))
            else:
                return
            if msg.get("reply_to"):
                self.bus.publish(msg["reply_to"], {"ok": True, "result": res, "correlation_id": msg.get("correlation_id")})
        except Exception as e:
            if msg.get("reply_to"):
                self.bus.publish(msg["reply_to"], {"ok": False, "error": repr(e),
                                                   "correlation_id": msg.get("correlation_id")})

    # ----------------------------------------------------------------- loop
    def _loop(self) -> None:
        sim = self.sim
        assert sim is not None
        dt = sim.scenario.dt_s
        next_wall = time.time()
        last_status = 0.0
        count, t_count = 0, time.time()
        while not self._stop.is_set():
            if self.state == "PAUSED":
                time.sleep(0.1)
                next_wall = time.time()
                continue
            if sim.t >= sim.scenario.duration_s:
                self.state = "FINISHED"
                self._publish_status()
                break
            t0 = time.perf_counter()
            with self._lock:
                out = sim.step()
            self.last_step_ms = (time.perf_counter() - t0) * 1000
            self._publish_step(out)
            if sim.step_count % 10 == 0:
                self._publish_records()
            if sim.step_count % max(int(sim.scenario.gt_interval_s / dt), 1) == 0:
                self._publish_truth()
            count += 1
            now = time.time()
            if now - t_count >= 2.0:
                self.steps_per_s = count / (now - t_count)
                count, t_count = 0, now
            if now - last_status >= 1.0:
                self._publish_status()
                last_status = now
            next_wall += dt / self.speed
            delay = next_wall - time.time()
            if delay > 0:
                time.sleep(delay)
            elif delay < -2.0:          # cannot keep up: do not try to catch up unboundedly
                next_wall = time.time()

    def _publish_step(self, out) -> None:
        P = self.plant
        ts = out.timestamp.isoformat()
        ing = datetime.now(timezone.utc).isoformat()
        for (t, sid, asset, par, val, unit, q) in out.telemetry:
            atype = P.asset_type(asset) if asset in P.all_asset_ids() else "facility"
            payload = {"event_id": str(uuid.uuid4()), "asset_id": asset, "sensor_id": sid, "parameter": par,
                       "value": None if val is None else float(val), "unit": unit, "event_timestamp": ts,
                       "ingestion_timestamp": ing, "quality": q, "source": "SIMULATION",
                       "source_tag": f"simulation/{atype}/{asset}/{par}", "run_id": self.run_id,
                       "schema_version": "1.0"}
            self.bus.publish(T.telemetry_topic("SIMULATION", atype, asset, par), payload, qos=1)
        for ev in out.events:
            self.bus.publish(T.EVENTS_TRANSFERS, {"event_type": ev["event_type"], "run_id": self.run_id,
                                                  "timestamp": ev["timestamp"].isoformat(),
                                                  "payload": ev["payload"]}, qos=1)

    def _publish_records(self, force: bool = False) -> None:
        sim = self.sim
        if sim is None:
            return
        rec = sim.records
        for tr in rec.transfers.values():
            key = f"{tr.transfer_id}:{tr.status}"
            if key not in self._published_records:
                self._published_records.add(key)
                self.bus.publish(f"{T.ROOT}/events/records/transfers", {"run_id": self.run_id, "record": tr.as_dict()})
        for kind in ("calibrations", "lab_samples", "maintenance", "operator_log"):
            for r in getattr(rec, kind):
                rid = r.get("record_id") or r.get("sample_id") or r.get("log_id")
                if rid in self._published_records:
                    continue
                if kind == "lab_samples" and r["timestamp"] > sim.timestamp():
                    continue       # lab result not yet available
                self._published_records.add(rid)
                self.bus.publish(f"{T.ROOT}/events/records/{kind}", {"run_id": self.run_id, "record": r})

    def _publish_truth(self) -> None:
        sim = self.sim
        if sim is None or not sim.truth:
            return
        row = dict(sim.truth[-1])
        row["timestamp"] = row["timestamp"].isoformat()
        self.bus.publish(f"{T.TRUTH_ROOT}/state", {"run_id": self.run_id, "row": row}, qos=0)

    def _publish_status(self) -> None:
        self.bus.publish(T.SIM_STATUS, self.status(), qos=1, retain=True)

    def status(self) -> dict[str, Any]:
        sim = self.sim
        d = {"state": self.state, "run_id": self.run_id, "speed": self.speed,
             "steps_per_second": round(self.steps_per_s, 1), "last_step_ms": round(self.last_step_ms, 2)}
        if sim is not None:
            d.update({"scenario_id": sim.scenario.scenario_id, "sim_time_s": sim.t,
                      "sim_timestamp": sim.timestamp().isoformat(), "duration_s": sim.scenario.duration_s,
                      "active_transfer": ({"transfer_id": sim.active.order.transfer_id, "route": sim.active.order.route,
                                           "state": sim.active.state, "setpoint_m3h": sim.active.order.setpoint_m3h}
                                          if sim.active else None),
                      "pending_transfers": [{"transfer_id": e.order.transfer_id, "route": e.order.route,
                                             "start_s": e.order.start_s} for e in sim.pending[:5]],
                      "active_fault_count": len(sim.faults.active(sim.t)),
                      "conservation_error_kg": (sim.truth[-1]["conservation_error_kg"] if sim.truth else 0.0)})
        return d
