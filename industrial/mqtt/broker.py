"""Embedded MQTT broker for local development without Docker (amqtt).

Production / Docker deployments use Eclipse Mosquitto with TLS, authentication
and topic ACLs (infrastructure/mqtt). This broker is for single-machine dev only.
"""
from __future__ import annotations

import asyncio
import logging
import threading

log = logging.getLogger("oilintel.broker")


class EmbeddedBroker:
    def __init__(self, host: str = "127.0.0.1", port: int = 1883):
        self.host, self.port = host, port
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._broker = None
        self.started = threading.Event()
        self.error: str | None = None

    def start(self, timeout: float = 10.0) -> bool:
        def run():
            from amqtt.broker import Broker
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            cfg = {"listeners": {"default": {"type": "tcp", "bind": f"{self.host}:{self.port}"}},
                   "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}}

            async def main():
                self._broker = Broker(cfg)
                await self._broker.start()
                self.started.set()

            try:
                self._loop.run_until_complete(main())
                self._loop.run_forever()
            except Exception as e:  # port in use etc.
                self.error = repr(e)
                self.started.set()

        logging.getLogger("amqtt").setLevel(logging.WARNING)
        self._thread = threading.Thread(target=run, name="embedded-mqtt", daemon=True)
        self._thread.start()
        self.started.wait(timeout)
        return self.error is None and self.started.is_set()

    def stop(self) -> None:
        if self._loop and self._broker:
            fut = asyncio.run_coroutine_threadsafe(self._broker.shutdown(), self._loop)
            try:
                fut.result(5)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
