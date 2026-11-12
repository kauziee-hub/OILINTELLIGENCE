"""Integration tests: telemetry contract, bus, MQTT (embedded broker), OPC UA server/client."""
import asyncio
import socket
import threading
import time
from datetime import datetime, timezone

import pytest

from industrial.connectors.bus import InProcessBus, MqttBus
from industrial.mqtt.topics import telemetry_topic, topic_matches
from industrial.schemas.telemetry import TelemetryRecord


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_telemetry_contract_roundtrip():
    r = TelemetryRecord(asset_id="PL-001", sensor_id="FM-001", parameter="volume_flow", value=125.4, unit="m3/h",
                        event_timestamp=datetime(2026, 10, 30, 10, 30, tzinfo=timezone.utc))
    r2 = TelemetryRecord.from_json(r.to_json())
    assert r2.value == 125.4 and r2.schema_version == "1.0" and r2.quality == "GOOD"


def test_topic_design():
    t = telemetry_topic("SIMULATION", "tank", "TK-001", "level")
    assert t == "oilintelligence/simulation/tank/TK-001/level"
    assert topic_matches("oilintelligence/simulation/+/+/+", t)
    assert topic_matches("oilintelligence/#", t)
    assert not topic_matches("oilintelligence/plant/+/+/+", t)


def test_inprocess_bus_and_live_simulation():
    from simulation.engine.live import LiveSimulation
    bus = InProcessBus()
    got = []
    bus.subscribe("oilintelligence/simulation/+/+/+", lambda t, p: got.append(p))
    live = LiveSimulation(bus)
    live.start(speed=120)
    time.sleep(1.5)
    live.stop()
    assert len(got) > 20
    assert {"event_id", "event_timestamp", "quality", "source", "schema_version"} <= set(got[0])
    assert got[0]["source"] == "SIMULATION"


def test_mqtt_publish_subscribe_with_embedded_broker():
    from industrial.mqtt.broker import EmbeddedBroker
    port = free_port()
    broker = EmbeddedBroker("127.0.0.1", port)
    assert broker.start(), broker.error
    try:
        sub, pub = MqttBus("127.0.0.1", port), MqttBus("127.0.0.1", port)
        assert sub.wait_connected(5) and pub.wait_connected(5)
        got = threading.Event()
        sub.subscribe("oilintelligence/simulation/+/+/+", lambda t, p: got.set() if p["value"] == 1.5 else None)
        time.sleep(0.3)
        pub.publish(telemetry_topic("SIMULATION", "tank", "TK-001", "level"), {"value": 1.5})
        assert got.wait(5)
        sub.close()
        pub.close()
    finally:
        broker.stop()


def test_mqtt_buffers_while_disconnected():
    bus = MqttBus("127.0.0.1", free_port())      # nothing listening
    for i in range(5):
        bus.publish("oilintelligence/simulation/tank/TK-001/level", {"i": i})
    assert bus.status()["buffered"] == 5 and not bus.connected
    bus.close()


def test_opcua_server_exposes_values_read_only(plant):
    from industrial.opcua.client import OpcUaReadOnlyConnector, default_tag_map
    from industrial.opcua.server import VirtualOpcUaServer
    port = free_port()
    srv = VirtualOpcUaServer(plant, f"opc.tcp://127.0.0.1:{port}/oi/")
    assert srv.start(), srv.error
    try:
        srv.update("LT-001", 12.345, "GOOD")
        srv.update("FM-001:volume_total", 123456.7, "GOOD")
        time.sleep(0.5)
        tags = default_tag_map(plant, srv.ns_idx)
        conn = OpcUaReadOnlyConnector(f"opc.tcp://127.0.0.1:{port}/oi/",
                                      {k: v for k, v in tags.items() if v["sensor_id"] in ("LT-001", "FM-001")})
        recs = asyncio.run(conn.read_once())
        by = {(r.sensor_id, r.parameter): r for r in recs}
        assert by[("LT-001", "level")].value == pytest.approx(12.345)
        assert by[("FM-001", "volume_total")].value == pytest.approx(123456.7)
        assert all(r.source == "PLANT" for r in recs)
        assert not hasattr(conn, "write") and not hasattr(conn, "write_value")
        assert conn.test()["ok"]
    finally:
        srv.stop()


def test_live_run_clock_never_goes_backwards():
    """An accelerated run ends ahead of the wall clock; the next run (including a generated scenario
    with a fixed historical start date) must start after it, never on top of stored history."""
    from datetime import timedelta

    from industrial.schemas.plant import get_plant
    from simulation.engine.live import LiveSimulation
    from simulation.scenarios.scenario import generate_scenario
    bus = InProcessBus()
    live = LiveSimulation(bus)
    try:
        live.start(speed=120)
        time.sleep(1.0)
        end_first = live.sim.timestamp()
        assert end_first > datetime.now(timezone.utc)            # accelerated: ahead of the wall clock
        sc = generate_scenario(get_plant(), "NORMAL", 3, "A", duration_s=3600, dt_s=1.0, sample_interval_s=1.0)
        assert sc.start_time < end_first                          # fixed default start date (2026-10-30)
        live.start(sc, speed=120)
        assert live.sim.scenario.start_time >= end_first
        floor = live.sim.timestamp() + timedelta(days=2)
        live.start(speed=10, not_before=floor)                    # backend-supplied floor (stored history)
        assert live.sim.scenario.start_time >= floor
    finally:
        live.stop()

