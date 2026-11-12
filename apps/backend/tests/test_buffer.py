"""Telemetry buffer: the monitor must never evaluate a half-ingested timestamp."""
from __future__ import annotations

from datetime import datetime, timezone


def _buffer(hours: float):
    # imported lazily: app modules bind settings at import, and conftest prepares the test environment first
    from app.services.ingestion import TelemetryBuffer
    return TelemetryBuffer(hours)


def _msg(i: int, sensor: str, ts: float, value: float) -> dict:
    return {"event_id": f"e{i}", "event_timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "sensor_id": sensor, "asset_id": sensor, "parameter": "totaliser", "value": value}


def test_complete_ts_waits_for_every_live_sensor():
    b = _buffer(1.0)
    t = 1_800_000_000.0
    n = 0
    for k in range(3):                                   # three complete samples, 60 s apart
        for s in ("FM-001", "FM-002", "FM-003"):
            n += 1
            b.add(_msg(n, s, t + 60 * k, 100.0 * k))
    assert b.complete_ts() == t + 120
    n += 1
    b.add(_msg(n, "FM-001", t + 180, 300.0))              # next batch half ingested
    assert b.last_event_ts == t + 180
    assert b.complete_ts() == t + 120                    # evaluation stays on the last complete sample
    for s in ("FM-002", "FM-003"):
        n += 1
        b.add(_msg(n, s, t + 180, 300.0))
    assert b.complete_ts() == t + 180


def test_complete_ts_ignores_dropped_out_sensors():
    b = _buffer(2.0)
    t = 1_800_000_000.0
    b.add(_msg(1, "LT-001", t, 5.0))                     # then silent (dropout fault)
    for k in range(1, 20):
        b.add(_msg(100 + k, "FM-001", t + 60 * k, float(k)))
    assert b.complete_ts() == t + 60 * 19                # a dead sensor must not freeze the monitor
