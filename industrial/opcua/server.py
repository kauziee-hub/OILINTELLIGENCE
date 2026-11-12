"""Virtual OPC UA server exposing the simulated instruments (section 9.2).

Objects/OilIntelligence/{TankFarm,Pipelines,Pumps,Valves}/<asset>/<variable>
Node ids: ns=<idx>;s=OilIntelligence.<Group>.<Asset>.<Variable>
Variables are read-only for clients; values, source timestamps and status
codes (Good / Uncertain / Bad) follow the telemetry stream. Security policies:
None for local development; Basic256Sha256 SignAndEncrypt when a server
certificate/key is configured (infrastructure/opcua/generate_certs.py).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from industrial.schemas.plant import PlantConfig, get_plant

log = logging.getLogger("oilintel.opcua")

# sensor parameter -> (group, variable)
TANK_VARS = {"level": "Level", "temperature": "Temperature"}
PIPE_VARS = {"volume_flow": "FlowRate", "pressure": "Pressure", "temperature": "Temperature",
             "density_15c": "Density"}
PUMP_VARS = {"status": "Status", "speed": "Speed", "power": "Power", "discharge_pressure": "DischargePressure"}
VALVE_VARS = {"position": "Position", "command": "Status"}


def node_map(plant: PlantConfig) -> dict[str, str]:
    """sensor_id[:parameter] -> browse path 'Group.Asset.Variable'."""
    m: dict[str, str] = {}
    for sid, s in plant.sensors.items():
        a = s.asset
        if a in plant.tanks and s.parameter in TANK_VARS:
            m[sid] = f"TankFarm.{a}.{TANK_VARS[s.parameter]}"
        elif a in plant.pipelines and s.parameter in PIPE_VARS:
            m[sid] = f"Pipelines.{a}.{PIPE_VARS[s.parameter]}"
            if s.type == "flow":
                m[f"{sid}:volume_total"] = f"Pipelines.{a}.VolumeTotal"
        elif a in plant.junctions and s.parameter in PIPE_VARS:
            m[sid] = f"Pipelines.{a}.{PIPE_VARS[s.parameter]}"
        elif a in plant.pumps and s.parameter in PUMP_VARS:
            m[sid] = f"Pumps.{a}.{PUMP_VARS[s.parameter]}"
        elif a in plant.valves and s.parameter in VALVE_VARS:
            m[sid] = f"Valves.{a}.{VALVE_VARS[s.parameter]}"
        elif a == plant.plant_id:
            m[sid] = f"TankFarm.Ambient.Temperature"
    for t in plant.tanks:
        m[f"{t}:inventory"] = f"TankFarm.{t}.Inventory"
        m[f"{t}:density"] = f"TankFarm.{t}.Density"
    return m


class VirtualOpcUaServer:
    def __init__(self, plant: PlantConfig | None = None, endpoint: str = "opc.tcp://0.0.0.0:4840/oilintelligence/",
                 cert_file: str | None = None, key_file: str | None = None):
        self.plant = plant or get_plant()
        self.endpoint = endpoint
        self.cert_file, self.key_file = cert_file, key_file
        self.paths = node_map(self.plant)
        self.vars: dict[str, Any] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self.ns_idx: int | None = None
        self.error: str | None = None
        self.updates = 0

    async def _build(self):
        from asyncua import Server, ua
        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name("OILINTELLIGENCE Virtual Tank Farm (simulation)")
        policies = [ua.SecurityPolicyType.NoSecurity]
        if self.cert_file and self.key_file:
            await server.load_certificate(self.cert_file)
            await server.load_private_key(self.key_file)
            policies = [ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt]
        server.set_security_policy(policies)
        idx = await server.register_namespace("urn:oilintelligence:tankfarm")
        self.ns_idx = idx
        root = await server.nodes.objects.add_object(idx, "OilIntelligence")
        groups: dict[str, Any] = {}
        assets: dict[str, Any] = {}
        for key, path in sorted(set((k, v) for k, v in self.paths.items()), key=lambda kv: kv[1]):
            g, a, var = path.split(".")
            if g not in groups:
                groups[g] = await root.add_object(idx, g)
            if (g, a) not in assets:
                assets[(g, a)] = await groups[g].add_object(idx, a)
            if path not in self.vars:
                nid = ua.NodeId(f"OilIntelligence.{path}", idx)
                self.vars[path] = await assets[(g, a)].add_variable(nid, var, ua.Variant(0.0, ua.VariantType.Double))
        return server

    def start(self, timeout: float = 15.0) -> bool:
        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def main():
                try:
                    server = await self._build()
                    self._stop = asyncio.Event()
                    async with server:
                        self._ready.set()
                        await self._stop.wait()
                except Exception as e:
                    self.error = repr(e)
                    self._ready.set()

            self._loop.run_until_complete(main())

        logging.getLogger("asyncua").setLevel(logging.WARNING)
        self._thread = threading.Thread(target=run, name="opcua-server", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.error is None and self._ready.is_set()

    def update(self, key: str, value: float | None, quality: str = "GOOD", ts: datetime | None = None) -> None:
        """Thread-safe value update (key = sensor id, 'FM-001:volume_total', 'TK-001:inventory'...)."""
        path = self.paths.get(key)
        if path is None or self._loop is None or path not in self.vars or value is None:
            return
        from asyncua import ua
        sc = {"GOOD": ua.StatusCodes.Good, "UNCERTAIN": ua.StatusCodes.Uncertain}.get(quality, ua.StatusCodes.Bad)
        dv = ua.DataValue(Value=ua.Variant(float(value), ua.VariantType.Double), StatusCode=ua.StatusCode(sc),
                          SourceTimestamp=ts or datetime.now(timezone.utc), ServerTimestamp=datetime.now(timezone.utc))
        node = self.vars[path]
        asyncio.run_coroutine_threadsafe(node.write_value(dv), self._loop)
        self.updates += 1

    def on_telemetry(self, topic: str, payload: dict[str, Any]) -> None:
        sid, par = payload.get("sensor_id"), payload.get("parameter")
        key = f"{sid}:{par}" if par == "volume_total" else sid
        ts = payload.get("event_timestamp")
        try:
            dt = datetime.fromisoformat(ts) if isinstance(ts, str) else None
        except ValueError:
            dt = None
        self.update(key, payload.get("value"), payload.get("quality", "GOOD"), dt)

    def stop(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)

    def status(self) -> dict[str, Any]:
        return {"endpoint": self.endpoint, "running": self._ready.is_set() and self.error is None,
                "variables": len(self.vars), "updates": self.updates, "namespace_index": self.ns_idx,
                "error": self.error}
