"""Security tests: authentication, role-based access, audit trail, read-only plant interface (NFR-002/003)."""
import pytest

API = "/api/v1"


def test_login_rejects_wrong_password(client):
    r = client.post(f"{API}/auth/token", data={"username": "admin", "password": "not-the-password"})
    assert r.status_code == 401


def test_endpoints_require_authentication(client):
    for path in ("/assets", "/telemetry/latest", "/investigations", "/simulation/status", "/twin/state"):
        assert client.get(API + path).status_code == 401, path
    assert client.get(API + "/assets", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_me_reports_role(client, tokens):
    for role, h in tokens.items():
        assert client.get(f"{API}/auth/me", headers=h).json()["role"] == role


@pytest.mark.parametrize("method,path,body,min_role", [
    ("post", "/simulation/faults", {"fault_type": "METER_BIAS", "target": "FM-003", "params": {"bias_rel": 0.0}}, "operator"),
    ("post", "/simulation/verification", {"action_type": "LINE_INSPECTION", "target": "PL-001"}, "engineer"),
    ("get", "/simulation/truth/latest", None, "engineer"),
    ("post", "/investigations", {"assets": ["TK-001"], "run": False}, "engineer"),
    ("get", "/users", None, "admin"),
    ("post", "/adaptive/run", None, "engineer"),
    ("get", "/audit", None, "admin"),
])
def test_role_based_access(client, tokens, method, path, body, min_role):
    rank = ["viewer", "operator", "engineer", "admin"]
    for role in rank:
        kw = {"headers": tokens[role]}
        if body is not None:
            kw["json"] = body
        code = getattr(client, method)(API + path, **kw).status_code
        if rank.index(role) < rank.index(min_role):
            assert code == 403, (role, path, code)
        else:
            assert code in (200, 201), (role, path, code)


def test_state_changes_are_audited(client, tokens):
    client.post(f"{API}/investigations", json={"assets": ["TK-002"], "run": False}, headers=tokens["engineer"])
    rows = client.get(f"{API}/audit", headers=tokens["admin"]).json()
    assert any(r["actor"] == "engineer" and "POST /api/v1/investigations" in r["action"] for r in rows)


def test_no_endpoint_can_command_plant_equipment(client):
    """Outside the simulation lab, every write endpoint is analytical / bookkeeping only."""
    paths = client.get("/openapi.json").json()["paths"]
    allowed_prefixes = (f"{API}/simulation/", f"{API}/auth/", f"{API}/users", f"{API}/investigations",
                        f"{API}/physics/", f"{API}/ml/", f"{API}/integrations/", f"{API}/adaptive/",
                        f"{API}/demo/")            # demo runs drive the simulator only (409 in PLANT mode)
    for path, ops in paths.items():
        writes = {m for m in ops if m in ("post", "put", "delete", "patch")}
        if writes:
            assert path.startswith(allowed_prefixes), path
        assert not any(w in path for w in ("/setpoint", "/command", "/write", "/control")), path


def test_connectors_are_read_only(client, tokens):
    rows = client.get(f"{API}/integrations", headers=tokens["viewer"]).json()
    assert rows and all(r["read_only"] for r in rows)


def test_websocket_requires_token(client, tokens):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/live") as ws:
            ws.receive_text()
    tok = tokens["viewer"]["Authorization"].split()[1]
    with client.websocket_connect(f"/ws/live?token={tok}") as ws:
        assert '"type": "state"' in ws.receive_text()


def test_auth_config_is_public(client):
    r = client.get(f"{API}/auth/config")                    # no token: the UI asks before showing a sign-in page
    assert r.status_code == 200 and r.json() == {"auth_required": True}


def test_open_access_acts_as_the_configured_identity(client, monkeypatch):
    from app.core import security
    from app.core.config import get_settings
    s = get_settings().model_copy(update={"auth_required": False, "open_access_user": "control-room",
                                          "open_access_role": "engineer"})
    monkeypatch.setattr(security, "get_settings", lambda: s)
    p = security.current_user(request=None, token=None)     # no sign-in, no token
    assert p.username == "control-room" and p.role == "engineer"
    with __import__("pytest").raises(Exception):            # roles still apply on an open-access deployment
        security.require_role("admin")(p)
