"""OILINTELLIGENCE backend - FastAPI application.

uvicorn app.main:app --app-dir apps/backend --port 8000
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api import adaptive_routes, core_routes, demo_routes, intel_routes, sim_routes
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.security import Principal, current_user, decode_token, ws_principal
from app.models import orm
from app.services.db_backend import jsonable
from app.services.runtime import get_runtime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("oilintel.api")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    rt = get_runtime()
    rt.broadcaster.loop = asyncio.get_running_loop()
    await asyncio.to_thread(rt.start)
    yield
    await asyncio.to_thread(rt.stop)


app = FastAPI(title="OILINTELLIGENCE API", version="1.0.0", lifespan=lifespan,
              description="Physics-guided, agent-assisted hydrocarbon loss attribution. Read-only analytical "
                          "platform: no endpoint can command plant equipment.")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list, allow_credentials=True,
                   allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type"])

_hits: dict[str, deque] = defaultdict(deque)


@app.middleware("http")
async def guard(request: Request, call_next):
    # --- rate limiting (per client, sliding 60 s window)
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= settings.rate_limit_per_minute:
        return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
    q.append(now)
    t0 = time.perf_counter()
    response = await call_next(request)
    dur = (time.perf_counter() - t0) * 1000
    response.headers["X-Response-Time-ms"] = f"{dur:.1f}"
    _metrics["requests"] += 1
    _metrics["latency_ms"].append(dur)
    # --- audit trail of every state-changing request
    if request.method in ("POST", "PUT", "DELETE") and request.url.path.startswith(settings.api_prefix):
        actor, role = "anonymous", ""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            try:
                p = decode_token(auth[7:])
                actor, role = p.get("sub", "?"), p.get("role", "")
            except Exception:
                pass
        if not request.url.path.endswith("/auth/token"):
            try:
                with SessionLocal() as s:
                    s.add(orm.AuditLog(actor=actor, role=role, action=f"{request.method} {request.url.path}",
                                       resource=request.url.path, status_code=response.status_code, ip=ip))
                    s.commit()
            except Exception:
                log.exception("audit middleware failed")
    return response


_metrics: dict = {"requests": 0, "latency_ms": deque(maxlen=2000)}

P = settings.api_prefix
app.include_router(core_routes.router, prefix=P)
app.include_router(sim_routes.router, prefix=P)
app.include_router(intel_routes.router, prefix=P)
app.include_router(adaptive_routes.router, prefix=P)
app.include_router(demo_routes.router, prefix=P)


@app.get(f"{P}/health", tags=["system"])
def health():
    return jsonable(get_runtime().health())


@app.get(f"{P}/twin/state", tags=["digital-twin"])
def twin_state(_: Principal = Depends(current_user)):
    return jsonable(get_runtime().twin_state())


@app.get(f"{P}/monitoring/latest", tags=["physics"])
def monitoring_latest(_: Principal = Depends(current_user)):
    m = get_runtime().monitor
    return jsonable(m.last_result if m else None)


@app.get("/metrics", tags=["system"], response_class=PlainTextResponse)
def metrics():
    """Prometheus exposition of platform metrics (section 12.4)."""
    rt = get_runtime()
    h = rt.health()
    ing = h.get("ingestion") or {}
    bus = h.get("bus") or {}
    mon = h.get("monitor") or {}
    sim = h.get("simulation") or {}
    lat = sorted(_metrics["latency_ms"])
    p95 = lat[int(0.95 * (len(lat) - 1))] if lat else 0.0
    delay = (time.time() - ing["last_ingest_wall"]) if ing.get("last_ingest_wall") else -1
    lines = [
        f"oilintel_telemetry_received_total {ing.get('received', 0)}",
        f"oilintel_telemetry_duplicates_total {ing.get('duplicates', 0)}",
        f"oilintel_telemetry_written_total {ing.get('written', 0)}",
        f"oilintel_telemetry_write_errors_total {ing.get('write_errors', 0)}",
        f"oilintel_seconds_since_last_message {delay:.2f}",
        f"oilintel_bus_buffered_messages {bus.get('buffered', 0)}",
        f"oilintel_bus_dropped_messages_total {bus.get('dropped', 0)}",
        f"oilintel_bus_connected {1 if bus.get('connected') else 0}",
        f"oilintel_monitor_evaluations_total {mon.get('evaluations', 0)}",
        f"oilintel_monitor_errors_total {mon.get('errors', 0)}",
        f"oilintel_simulation_steps_per_second {sim.get('steps_per_second', 0)}",
        f"oilintel_simulation_step_ms {sim.get('last_step_ms', 0)}",
        f"oilintel_api_requests_total {_metrics['requests']}",
        f"oilintel_api_latency_p95_ms {p95:.2f}",
        f"oilintel_websocket_clients {h.get('websocket_clients', 0)}",
    ]
    return "\n".join(lines) + "\n"


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    principal = ws_principal(ws)
    if principal is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    rt = get_runtime()
    rt.broadcaster.clients.add(ws)
    try:
        await ws.send_text(__import__("json").dumps({"type": "state", "data": jsonable(rt.twin_state())}, default=str))
        while True:
            await ws.receive_text()      # client pings / keeps alive
    except WebSocketDisconnect:
        pass
    finally:
        rt.broadcaster.clients.discard(ws)
