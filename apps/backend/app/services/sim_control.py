"""Simulation controllers: in-process (dev) or remote over MQTT (Docker simulator service)."""
from __future__ import annotations

import threading
import uuid
from typing import Any

from industrial.mqtt import topics as T
from simulation.engine.live import LiveSimulation


class LocalSimController:
    kind = "local"

    def __init__(self, bus, plant):
        self.live = LiveSimulation(bus, plant)

    def start(self, scenario=None, speed=None, not_before=None):
        return self.live.start(scenario, speed, not_before)

    def pause(self):
        return self.live.pause()

    def resume(self):
        return self.live.resume()

    def stop(self):
        return self.live.stop()

    def set_speed(self, speed):
        return self.live.set_speed(speed)

    def inject_fault(self, fault):
        return self.live.inject_fault(fault)

    def clear_fault(self, fault_id):
        return self.live.clear_fault(fault_id)

    def add_transfer(self, order):
        return self.live.add_transfer(order)

    def perform_check(self, action_type, target):
        return self.live.perform_check(action_type, target)

    def status(self):
        return self.live.status()

    def truth_snapshot(self):
        return self.live.sim.truth_snapshot() if self.live.sim else None


class RemoteSimController:
    kind = "remote"

    def __init__(self, bus, plant):
        self.bus = bus
        self._status: dict[str, Any] = {"state": "UNKNOWN"}
        self._replies: dict[str, dict[str, Any]] = {}
        self._events: dict[str, threading.Event] = {}
        self.reply_topic = f"{T.ROOT}/simulation/replies/{uuid.uuid4().hex[:8]}"
        bus.subscribe(T.SIM_STATUS, lambda t, p: self._status.update(p))
        bus.subscribe(self.reply_topic, self._on_reply)

    def _on_reply(self, topic, p):
        cid = p.get("correlation_id")
        if cid in self._events:
            self._replies[cid] = p
            self._events[cid].set()

    def _call(self, command: str, timeout: float = 10.0, **kw):
        cid = uuid.uuid4().hex
        ev = threading.Event()
        self._events[cid] = ev
        self.bus.publish(T.SIM_CONTROL, {"command": command, "reply_to": self.reply_topic, "correlation_id": cid, **kw})
        if not ev.wait(timeout):
            self._events.pop(cid, None)
            raise TimeoutError(f"simulator did not answer '{command}'")
        self._events.pop(cid, None)
        r = self._replies.pop(cid)
        if not r.get("ok"):
            raise RuntimeError(r.get("error"))
        return r.get("result")

    def start(self, scenario=None, speed=None, not_before=None):
        return self._call("start", scenario=scenario.model_dump(mode="json") if scenario else None, speed=speed,
                          not_before=not_before.isoformat() if not_before else None)

    def pause(self):
        return self._call("pause")

    def resume(self):
        return self._call("resume")

    def stop(self):
        return self._call("stop")

    def set_speed(self, speed):
        return self._call("speed", speed=speed)

    def inject_fault(self, fault):
        return self._call("inject_fault", fault=fault.model_dump() if hasattr(fault, "model_dump") else fault)

    def clear_fault(self, fault_id):
        return self._call("clear_fault", fault_id=fault_id)

    def add_transfer(self, order):
        return self._call("add_transfer", order=order.model_dump() if hasattr(order, "model_dump") else order)

    def perform_check(self, action_type, target):
        return self._call("perform_check", action_type=action_type, target=target)

    def status(self):
        return dict(self._status)

    def truth_snapshot(self):
        return None
