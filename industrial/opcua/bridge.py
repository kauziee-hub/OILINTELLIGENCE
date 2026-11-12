"""Standalone OPC UA service: subscribes to MQTT simulation telemetry and exposes it
through the virtual OPC UA server (Docker service `opcua`).

python -m industrial.opcua.bridge
"""
from __future__ import annotations

import logging
import os
import signal
import time

from industrial.connectors.bus import MqttBus, env_flag
from industrial.opcua.server import VirtualOpcUaServer


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log = logging.getLogger("oilintel.opcua.bridge")
    server = VirtualOpcUaServer(endpoint=os.environ.get("OPCUA_ENDPOINT", "opc.tcp://0.0.0.0:4840/oilintelligence/"),
                                cert_file=os.environ.get("OPCUA_CERT"), key_file=os.environ.get("OPCUA_KEY"))
    if not server.start():
        raise SystemExit(f"OPC UA server failed: {server.error}")
    bus = MqttBus(os.environ.get("MQTT_HOST", "mqtt"), int(os.environ.get("MQTT_PORT", "1883")),
                  os.environ.get("MQTT_USERNAME"), os.environ.get("MQTT_PASSWORD"),
                  tls=env_flag("MQTT_TLS"), client_id="oilintel-opcua")
    bus.subscribe("oilintelligence/simulation/+/+/+", server.on_telemetry)
    log.info("OPC UA bridge running at %s", server.endpoint)
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    try:
        while not stop["flag"]:
            time.sleep(5)
            log.debug("status %s / bus %s", server.status(), bus.status())
    except KeyboardInterrupt:
        pass
    server.stop()
    bus.close()


if __name__ == "__main__":
    main()
