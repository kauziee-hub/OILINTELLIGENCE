"""API test harness: isolated SQLite databases, generated test credentials, live in-process simulator.

Settings are read once (lru_cache) when the app is imported, so the environment is prepared
before ``app.main`` is imported. Test passwords are generated per session and never printed.
"""
from __future__ import annotations

import os
import secrets
import time

import pytest

ROLES = ("admin", "engineer", "operator", "viewer")


@pytest.fixture(scope="session")
def creds():
    return {r: secrets.token_urlsafe(16) for r in ROLES}


@pytest.fixture(scope="session")
def client(tmp_path_factory, creds):
    d = tmp_path_factory.mktemp("api")
    os.environ.update(
        DATABASE_URL=f"sqlite:///{(d / 'app.db').as_posix()}",
        TRUTH_DATABASE_URL=f"sqlite:///{(d / 'truth.db').as_posix()}",
        ADAPTIVE_STATE_PATH=str(d / "adaptive_state.json"),
        DEMO_HISTORY_PATH=str(d / "demo_history.json"),
        AUTH_REQUIRED="1", BUS="inproc", SIMULATOR="local", DATA_MODE="SIMULATION",
        SIM_AUTOSTART="1", SIM_SPEED="120", MONITOR_INTERVAL_S="300", AUTO_INVESTIGATE="1",
        OPCUA_ENABLED="0", EMBEDDED_BROKER="0", RATE_LIMIT_PER_MINUTE="100000",
        JWT_SECRET=secrets.token_hex(32),
        **{f"SEED_{r.upper()}_PASSWORD": pw for r, pw in creds.items()},
    )
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        deadline = time.time() + 30
        while time.time() < deadline:            # wait for first telemetry to be ingested
            h = c.get("/api/v1/health").json()
            if (h.get("ingestion") or {}).get("written", 0) > 0:
                break
            time.sleep(0.5)
        yield c


@pytest.fixture(scope="session")
def tokens(client, creds):
    out = {}
    for r, pw in creds.items():
        res = client.post("/api/v1/auth/token", data={"username": r, "password": pw})
        assert res.status_code == 200, res.text
        out[r] = {"Authorization": f"Bearer {res.json()['access_token']}"}
    return out
