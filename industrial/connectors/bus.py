"""Telemetry bus abstraction: in-process (single-process dev) or MQTT.

Both deliver the same payloads (JSON of the internal telemetry contract), so
the simulator, ingestion, OPC UA bridge and backend are agnostic to the
transport. The MQTT implementation provides QoS 1, automatic reconnection with
back-off and a bounded offline buffer so telemetry produced while the broker is
unreachable is published after reconnection (non-functional requirement:
"automatic reconnection and buffered telemetry handling").
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable

from industrial.mqtt.topics import topic_matches

log = logging.getLogger("oilintel.bus")
Callback = Callable[[str, dict[str, Any]], None]


def env_flag(name: str, default: bool = False) -> bool:
    """Boolean environment variable, parsed the same way as the backend settings (1/true/yes/on)."""
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


class TelemetryBus:
    name = "abstract"

    def publish(self, topic: str, payload: dict[str, Any], qos: int = 1, retain: bool = False) -> None:
        raise NotImplementedError

    def subscribe(self, pattern: str, callback: Callback) -> None:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        return {"type": self.name}

    def close(self) -> None:
        pass


class InProcessBus(TelemetryBus):
    name = "inproc"

    def __init__(self):
        self._subs: list[tuple[str, Callback]] = []
        self._lock = threading.Lock()
        self.published = 0
        self.retained: dict[str, dict[str, Any]] = {}

    def publish(self, topic, payload, qos=1, retain=False):
        self.published += 1
        if retain:
            self.retained[topic] = payload
        with self._lock:
            subs = list(self._subs)
        for pat, cb in subs:
            if topic_matches(pat, topic):
                try:
                    cb(topic, payload)
                except Exception:  # a failing consumer must not break the producer
                    log.exception("subscriber failed for %s", topic)

    def subscribe(self, pattern, callback):
        with self._lock:
            self._subs.append((pattern, callback))
        for t, p in list(self.retained.items()):
            if topic_matches(pattern, t):
                callback(t, p)

    def status(self):
        return {"type": self.name, "connected": True, "published": self.published, "subscriptions": len(self._subs)}


class MqttBus(TelemetryBus):
    name = "mqtt"

    def __init__(self, host: str = "localhost", port: int = 1883, username: str | None = None,
                 password: str | None = None, tls: bool = False, ca_certs: str | None = None,
                 client_id: str | None = None, buffer_size: int = 200_000):
        import paho.mqtt.client as mqtt

        self._mqtt = mqtt
        self.host, self.port = host, port
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=client_id or f"oilintel-{uuid.uuid4().hex[:8]}",
                                  clean_session=False)
        if username:
            self.client.username_pw_set(username, password)
        if tls:
            self.client.tls_set(ca_certs=ca_certs)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self._subs: list[tuple[str, Callback]] = []
        self._buffer: deque[tuple[str, str, int, bool]] = deque(maxlen=buffer_size)
        self.connected = False
        self.published = 0
        self.received = 0
        self.dropped = 0
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self.client.connect_async(host, port, keepalive=30)
        self.client.loop_start()

    # ----------------------------------------------------------- callbacks
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        ok = not getattr(reason_code, "is_failure", False)
        self.connected = ok
        if not ok:
            self.last_error = str(reason_code)
            return
        for pat, _ in self._subs:
            client.subscribe(pat, qos=1)
        self._flush()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        self.last_error = str(reason_code)

    def _on_message(self, client, userdata, msg):
        self.received += 1
        try:
            payload = json.loads(msg.payload)
        except Exception:
            return
        for pat, cb in list(self._subs):
            if topic_matches(pat, msg.topic):
                try:
                    cb(msg.topic, payload)
                except Exception:
                    log.exception("subscriber failed for %s", msg.topic)

    def _flush(self):
        with self._lock:
            while self._buffer and self.connected:
                t, p, q, r = self._buffer.popleft()
                info = self.client.publish(t, p, qos=q, retain=r)
                if info.rc != self._mqtt.MQTT_ERR_SUCCESS:
                    self._buffer.appendleft((t, p, q, r))
                    break
                self.published += 1

    # ----------------------------------------------------------- api
    def publish(self, topic, payload, qos=1, retain=False):
        data = json.dumps(payload, default=str, separators=(",", ":"))
        if not self.connected:
            if len(self._buffer) == self._buffer.maxlen:
                self.dropped += 1
            self._buffer.append((topic, data, qos, retain))
            return
        if self._buffer:
            self._flush()
        info = self.client.publish(topic, data, qos=qos, retain=retain)
        if info.rc == self._mqtt.MQTT_ERR_SUCCESS:
            self.published += 1
        else:
            self._buffer.append((topic, data, qos, retain))

    def subscribe(self, pattern, callback):
        self._subs.append((pattern, callback))
        if self.connected:
            self.client.subscribe(pattern, qos=1)

    def wait_connected(self, timeout: float = 5.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.connected:
                return True
            time.sleep(0.05)
        return self.connected

    def status(self):
        return {"type": self.name, "host": self.host, "port": self.port, "connected": self.connected,
                "published": self.published, "received": self.received, "buffered": len(self._buffer),
                "dropped": self.dropped, "last_error": self.last_error}

    def close(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass


def test_mqtt_connection(host: str, port: int, username: str | None = None, password: str | None = None,
                         timeout: float = 3.0) -> dict[str, Any]:
    """Connect, publish and receive a probe message (connector test endpoint)."""
    try:
        bus = MqttBus(host, port, username, password, client_id=f"oilintel-probe-{uuid.uuid4().hex[:6]}")
    except Exception as e:
        return {"ok": False, "error": repr(e)}
    try:
        if not bus.wait_connected(timeout):
            return {"ok": False, "error": f"could not connect to {host}:{port}", "detail": bus.last_error}
        got = threading.Event()
        topic = f"oilintelligence/system/probe/{uuid.uuid4().hex[:6]}"
        bus.subscribe(topic, lambda t, p: got.set())
        time.sleep(0.2)
        t0 = time.time()
        bus.publish(topic, {"probe": True})
        ok = got.wait(timeout)
        return {"ok": ok, "round_trip_ms": round((time.time() - t0) * 1000, 1) if ok else None,
                "broker": f"{host}:{port}"}
    finally:
        bus.close()
