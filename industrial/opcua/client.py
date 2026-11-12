"""Read-only OPC UA client adapter for plant integration.

Connects to an external OPC UA server, browses / reads variables and
subscribes to data changes, mapping node ids to platform sensor ids via a tag
map and converting them to the internal telemetry contract with
source=PLANT. This adapter deliberately has NO write path: the platform must
never issue commands to pumps, valves or PLCs (section 12.3).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from industrial.schemas.telemetry import TelemetryRecord, utcnow

log = logging.getLogger("oilintel.opcua.client")


def _quality(status_code) -> str:
    try:
        if status_code.is_good():
            return "GOOD"
        if status_code.is_uncertain():
            return "UNCERTAIN"
    except Exception:
        pass
    return "BAD"


class OpcUaReadOnlyConnector:
    def __init__(self, url: str, tag_map: dict[str, dict[str, str]] | None = None, username: str | None = None,
                 password: str | None = None, security: str | None = None, timeout_s: float = 5.0):
        """tag_map: node_id -> {"sensor_id", "asset_id", "parameter", "unit"}"""
        self.url = url
        self.tag_map = tag_map or {}
        self.username, self.password = username, password
        self.security = security          # e.g. "Basic256Sha256,SignAndEncrypt,cert.der,key.pem"
        self.timeout_s = timeout_s
        self.records = 0
        self.last_error: str | None = None

    def _client(self):
        from asyncua import Client
        c = Client(self.url, timeout=self.timeout_s)
        if self.username:
            c.set_user(self.username)
            c.set_password(self.password or "")
        return c

    async def _test(self, browse_limit: int = 25) -> dict[str, Any]:
        c = self._client()
        if self.security:
            await c.set_security_string(self.security)
        async with c:
            ns = await c.get_namespace_array()
            found: list[dict[str, Any]] = []

            async def walk(node, depth):
                if depth > 5 or len(found) >= browse_limit:
                    return
                for ch in await node.get_children():
                    if len(found) >= browse_limit:
                        return
                    try:
                        nc = await ch.read_node_class()
                    except Exception:
                        continue
                    if nc.name == "Variable":
                        try:
                            dv = await ch.read_data_value()
                            found.append({"node_id": ch.nodeid.to_string(),
                                          "browse_name": (await ch.read_browse_name()).Name,
                                          "value": dv.Value.Value if dv.Value else None,
                                          "quality": _quality(dv.StatusCode)})
                        except Exception:
                            pass
                    else:
                        await walk(ch, depth + 1)

            objs = c.nodes.objects
            for ch in await objs.get_children():
                bn = (await ch.read_browse_name()).Name
                if bn != "Server":
                    await walk(ch, 1)
            return {"ok": True, "url": self.url, "namespaces": ns, "variables_sampled": found}

    def test(self) -> dict[str, Any]:
        try:
            return asyncio.run(asyncio.wait_for(self._test(), self.timeout_s * 4))
        except Exception as e:
            self.last_error = repr(e)
            return {"ok": False, "url": self.url, "error": repr(e)}

    async def read_once(self) -> list[TelemetryRecord]:
        """Read all mapped tags once (polling mode)."""
        out = []
        c = self._client()
        async with c:
            for nid, meta in self.tag_map.items():
                try:
                    dv = await c.get_node(nid).read_data_value()
                    out.append(self._to_record(meta, dv, nid))
                except Exception as e:
                    self.last_error = repr(e)
        self.records += len(out)
        return out

    def _to_record(self, meta: dict[str, str], dv, nid: str) -> TelemetryRecord:
        v = dv.Value.Value if dv.Value is not None else None
        ts = dv.SourceTimestamp or datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return TelemetryRecord(asset_id=meta["asset_id"], sensor_id=meta["sensor_id"], parameter=meta["parameter"],
                               value=float(v) if isinstance(v, (int, float)) else None, unit=meta.get("unit", ""),
                               event_timestamp=ts, ingestion_timestamp=utcnow(), quality=_quality(dv.StatusCode),
                               source="PLANT", source_tag=f"opcua:{nid}")

    async def subscribe(self, callback: Callable[[TelemetryRecord], None], period_ms: int = 1000,
                        stop: asyncio.Event | None = None) -> None:
        """Subscribe to data changes of all mapped tags until `stop` is set."""
        conn = self

        class Handler:
            def datachange_notification(self, node, val, data):
                nid = node.nodeid.to_string()
                meta = conn.tag_map.get(nid)
                if meta:
                    conn.records += 1
                    callback(conn._to_record(meta, data.monitored_item.Value, nid))

        c = self._client()
        async with c:
            sub = await c.create_subscription(period_ms, Handler())
            await sub.subscribe_data_change([c.get_node(n) for n in self.tag_map])
            await (stop or asyncio.Event()).wait()


def default_tag_map(plant, ns_index: int = 2) -> dict[str, dict[str, str]]:
    """Tag map for the platform's own virtual server (used in integration tests)."""
    from industrial.opcua.server import node_map
    out = {}
    for key, path in node_map(plant).items():
        sid, _, par = key.partition(":")
        if sid not in plant.sensors:
            continue
        cfg = plant.sensors[sid]
        out[f"ns={ns_index};s=OilIntelligence.{path}"] = {"sensor_id": sid, "asset_id": cfg.asset,
                                                          "parameter": par or cfg.parameter,
                                                          "unit": "m3" if par == "volume_total" else cfg.unit}
    return out
