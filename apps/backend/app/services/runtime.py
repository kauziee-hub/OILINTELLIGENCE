"""Runtime: process-wide wiring of bus, simulator, ingestion, OPC UA, monitoring and agent."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal, init_db
from app.models import orm
from app.services.db_backend import DbBackend, jsonable
from app.services.ingestion import Ingestion, TelemetryBuffer
from app.services.live_state import build_state
from app.services.monitoring import Monitor
from app.services.seed import seed_assets, seed_connectors, seed_users
from app.services.sim_control import LocalSimController, RemoteSimController
from industrial.connectors.bus import InProcessBus, MqttBus
from industrial.schemas.plant import get_plant
from intelligence.service import DiagnosticEngine

log = logging.getLogger("oilintel.runtime")


class Broadcaster:
    """Fan-out of live messages to WebSocket clients (thread-safe enqueue)."""

    def __init__(self):
        self.clients: set[Any] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.sent = 0

    def publish(self, msg: dict[str, Any]) -> None:
        if self.loop is None or not self.clients:
            return
        data = json.dumps(msg, default=str)
        for ws in list(self.clients):
            asyncio.run_coroutine_threadsafe(self._send(ws, data), self.loop)

    async def _send(self, ws, data):
        try:
            await ws.send_text(data)
            self.sent += 1
        except Exception:
            self.clients.discard(ws)


class Runtime:
    def __init__(self, settings: Settings | None = None):
        self.settings = s = settings or get_settings()
        self.plant = get_plant(s.plant_config)
        self.source = "SIMULATION" if s.data_mode == "SIMULATION" else "PLANT"
        self.broker = None
        self.bus = None
        self.sim = None
        self.opcua = None
        self.buffer = TelemetryBuffer(s.telemetry_buffer_hours)
        self.engine = DiagnosticEngine(self.plant, s.models_dir)
        self.backend = DbBackend(self.engine, self.buffer, self.source)
        self.broadcaster = Broadcaster()
        self.ingestion: Ingestion | None = None
        self.monitor: Monitor | None = None
        self.adaptive = None                 # AdaptiveController, created once the database exists
        self.started_at = time.time()
        self._investigator = None
        self._inv_lock = threading.Lock()

    def sim_clock_floor(self):
        """Earliest start time for a new simulation run: one minute after the newest stored
        simulation telemetry (an accelerated run ends ahead of the wall clock)."""
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import func
        with SessionLocal() as db:
            last = db.execute(select(func.max(orm.Telemetry.ts_epoch)).where(orm.Telemetry.source == "SIMULATION")).scalar()
        return datetime.fromtimestamp(last, timezone.utc).replace(microsecond=0) + timedelta(seconds=60) if last else None

    # ------------------------------------------------------------------ boot
    def start(self) -> None:
        s = self.settings
        init_db()
        with SessionLocal() as db:
            seed_assets(db, self.plant)
            seed_users(db, s)
            seed_connectors(db, s)
        if s.bus == "mqtt":
            if s.embedded_broker:
                from industrial.mqtt.broker import EmbeddedBroker
                self.broker = EmbeddedBroker("127.0.0.1", s.mqtt_port)
                if not self.broker.start():
                    log.error("embedded MQTT broker failed: %s", self.broker.error)
            self.bus = MqttBus(s.mqtt_host, s.mqtt_port, s.mqtt_username, s.mqtt_password, tls=s.mqtt_tls,
                               client_id="oilintel-backend")
            self.bus.wait_connected(10)
        else:
            self.bus = InProcessBus()
        self.ingestion = Ingestion(self.bus, self.buffer, s.data_mode)
        self.ingestion.start()
        if s.opcua_enabled and s.data_mode == "SIMULATION":
            from industrial.opcua.server import VirtualOpcUaServer
            self.opcua = VirtualOpcUaServer(self.plant, s.opcua_endpoint)
            if self.opcua.start():
                self.bus.subscribe("oilintelligence/simulation/+/+/+", self.opcua.on_telemetry)
            else:
                log.error("OPC UA server failed to start: %s", self.opcua.error)
        if s.data_mode == "SIMULATION" and s.simulator == "local":
            # an in-process run cannot outlive the process that hosted it; a previous process that
            # was killed never recorded its run as stopped
            from datetime import datetime, timezone
            with SessionLocal() as db:
                for r in db.execute(select(orm.SimulationRun).where(
                        orm.SimulationRun.status.in_(("RUNNING", "PAUSED")))).scalars():
                    r.status, r.ended_at = "INTERRUPTED", r.ended_at or datetime.now(timezone.utc)
                db.commit()
        if s.data_mode == "SIMULATION":
            self.sim = LocalSimController(self.bus, self.plant) if s.simulator == "local" else \
                RemoteSimController(self.bus, self.plant)
            if s.simulator == "local" and s.sim_autostart:
                self.sim.start(speed=s.sim_speed, not_before=self.sim_clock_floor())
        if s.adaptive_enabled:
            from intelligence.adaptation.controller import AdaptiveController
            from intelligence.adaptation.drift import load_reference
            self.adaptive = AdaptiveController(
                self.engine, self.plant, s.adaptive_state_path, load_reference(s.models_dir),
                cases_provider=self.backend.verified_cases, data_provider=self.recent_data,
                overlap_factor=s.monitor_interval_s / s.monitor_window_s, window_s=s.monitor_window_s,
                synthetic_data_available=s.data_mode == "SIMULATION")
        self.monitor = Monitor(self)
        self.monitor.start()
        threading.Thread(target=self._state_pusher, name="ws-state", daemon=True).start()
        log.info("runtime started: mode=%s bus=%s simulator=%s", s.data_mode, s.bus, s.simulator)

    def stop(self) -> None:
        if self.monitor:
            self.monitor.stop()
        if self.sim and self.settings.simulator == "local":
            self.sim.stop()
        if self.ingestion:
            self.ingestion.stop()
        if self.opcua:
            self.opcua.stop()
        if self.bus:
            self.bus.close()
        if self.broker:
            self.broker.stop()

    # ---------------------------------------------------------------- helpers
    def investigator(self):
        with self._inv_lock:
            if self._investigator is None:
                from agents.graph.workflow import Investigator
                self._investigator = Investigator(self.backend)
            return self._investigator

    def recent_data(self, t0: float, t1: float):
        """Telemetry + records for a period (digital-twin replay inputs); None if not buffered."""
        tel = self.buffer.frame(t0, t1 + self.engine.READ_AHEAD_S)
        if not len(tel):
            return None
        return tel, self.backend.records(t1 - 30 * 86400, t1)

    def broadcast(self, msg: dict[str, Any]) -> None:
        self.broadcaster.publish(msg)

    def lab_density(self) -> dict[str, float]:
        out = {}
        with SessionLocal() as s:
            for t in self.plant.tanks:
                r = s.execute(select(orm.LabSample).where(orm.LabSample.tank_id == t)
                              .order_by(orm.LabSample.timestamp.desc()).limit(1)).scalar()
                if r:
                    out[t] = r.density_15c
        return out

    def alerts(self) -> list[dict[str, Any]]:
        q = select(orm.AnomalyEvent).where(orm.AnomalyEvent.status.in_(["OPEN", "INVESTIGATING"]),
                                           orm.AnomalyEvent.source == self.source)
        if self.buffer.epoch is not None:
            # events of an earlier simulation run describe a plant state that no longer exists;
            # their investigations stay open for review but are not live equipment alerts
            q = q.where(orm.AnomalyEvent.t1 >= self.buffer.epoch)
        with SessionLocal() as s:
            evs = s.execute(q.order_by(orm.AnomalyEvent.detected_at.desc()).limit(20)).scalars().all()
            out = []
            for e in evs:
                inv = s.get(orm.Investigation, e.investigation_id) if e.investigation_id else None
                for a in e.assets or []:
                    out.append({"asset_id": a, "severity": e.severity, "event_id": e.event_id,
                                "message": e.summary[:160], "investigation_id": e.investigation_id,
                                "top_hypothesis": inv.top_hypothesis if inv else None})
            return out

    def twin_state(self) -> dict[str, Any]:
        bal = self.monitor.last_result if self.monitor else None
        return build_state(self.plant, dict(self.buffer.latest), self.buffer.last_event_ts, self._lab_cache(),
                           self._alert_cache(), self.sim.status() if self.sim else None,
                           {k: bal.get(k) for k in ("t1", "facility_residual_t", "facility_sigma_t", "chi2", "dof",
                                                    "p_value", "max_abs_z", "z", "anomaly", "route")} if bal else None,
                           self.source)

    _lab: tuple[float, dict] = (0.0, {})
    _al: tuple[float, list] = (0.0, [])

    def _lab_cache(self):
        if time.time() - self._lab[0] > 10:
            self._lab = (time.time(), self.lab_density())
        return self._lab[1]

    def _alert_cache(self):
        if time.time() - self._al[0] > 3:
            self._al = (time.time(), self.alerts())
        return self._al[1]

    def _state_pusher(self) -> None:
        while True:
            time.sleep(1.0)
            if self.broadcaster.clients:
                try:
                    self.broadcast({"type": "state", "data": jsonable(self.twin_state())})
                except Exception:
                    log.exception("state push failed")

    def health(self) -> dict[str, Any]:
        return {"mode": self.settings.data_mode, "uptime_s": round(time.time() - self.started_at),
                "bus": self.bus.status() if self.bus else None,
                "ingestion": self.ingestion.status() if self.ingestion else None,
                "monitor": self.monitor.status() if self.monitor else None,
                "simulation": self.sim.status() if self.sim else None,
                "opcua": self.opcua.status() if self.opcua else {"running": False},
                "websocket_clients": len(self.broadcaster.clients),
                "models": self.engine.model_versions,
                "calibration": {"temperature": self.engine.temperature, "prior_mix": self.engine.prior_mix},
                "adaptive": ({"kind_scale": self.engine.kind_scale, "meter_factors": self.engine.meter_factors,
                              "learned_priors": self.engine.family_prior is not None}
                             if self.adaptive is not None else None)}


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime()
    return _runtime


def set_runtime(rt: Runtime | None) -> None:
    global _runtime
    _runtime = rt
