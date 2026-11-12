"""Demo scenario runner: catalogue, permissions, live start, scored status and history."""
from __future__ import annotations

import time

API = "/api/v1"


def test_demo_catalogue_and_permissions(client, tokens):
    cat = client.get(f"{API}/demo/scenarios", headers=tokens["viewer"]).json()
    keys = {s["key"] for s in cat}
    assert {"normal", "pipeline_leak", "meter_bias", "unknown_fault"} <= keys and len(cat) == 9
    assert all(s["expected"]["hypothesis"] and s["talk"] for s in cat)
    assert client.post(f"{API}/demo/scenarios/normal/start", json={}, headers=tokens["viewer"]).status_code == 403
    assert client.post(f"{API}/demo/scenarios/nope/start", json={}, headers=tokens["operator"]).status_code == 404
    assert client.post(f"{API}/demo/scenarios/normal/start", json={"speed": 5000},
                       headers=tokens["operator"]).status_code == 422


def test_demo_run_is_scored_and_archived(client, tokens):
    op, eng = tokens["operator"], tokens["engineer"]
    st = client.post(f"{API}/demo/scenarios/pipeline_leak/start", json={"speed": 120}, headers=op).json()
    assert st["active"] and st["session"]["key"] == "pipeline_leak" and st["session"]["inject_s"] == 5400
    assert [s["key"] for s in st["steps"]][:3] == ["baseline", "fault", "detect"]
    assert st["verdict"] == "RUNNING"
    deadline = time.time() + 20
    while time.time() < deadline:
        st = client.get(f"{API}/demo/status", headers=tokens["viewer"]).json()
        if st["sim_time_s"] and st["sim_time_s"] > 30:
            break
        time.sleep(0.5)
    assert st["same_run"] and st["sim_time_s"] > 0 and st["truth"] is not None
    # analyses right after a run change must not reach back into the previous run's plant state
    from app.services.runtime import get_runtime
    rt = get_runtime()
    tel = rt.backend.telemetry(rt.buffer.epoch - 6 * 3600, rt.backend.now())
    assert len(tel) and tel["ts"].min() >= rt.buffer.epoch
    tr = rt.backend.records(rt.buffer.epoch - 30 * 86400, rt.backend.now())["transfers"]
    assert all(t.timestamp() >= rt.buffer.epoch - 1 for t in tr["start_time"])   # no transfers of earlier runs
    # a new run closes what earlier runs left open: no transfer of another run stays IN_PROGRESS for ever
    from app.core.db import SessionLocal
    from app.models import orm
    with SessionLocal() as db:
        stale = db.query(orm.Transfer).filter(orm.Transfer.status == "IN_PROGRESS",
                                              orm.Transfer.run_id != st["session"]["run_id"]).count()
        old_open = db.query(orm.Investigation).filter(
            orm.Investigation.t1 < rt.buffer.epoch,
            orm.Investigation.status.in_(("AWAITING_VERIFICATION", "NEEDS_DATA", "RECEIVED"))).count()
    assert stale == 0 and old_open == 0
    hist = client.get(f"{API}/physics/residuals", headers=tokens["viewer"]).json()
    assert all(r["t1"] >= rt.buffer.epoch for r in hist)          # charts never sum across runs
    fa = client.post(f"{API}/ml/fault-attribution", json={}, headers=tokens["viewer"])
    assert fa.status_code in (200, 409, 422)
    assert st["steps"][1]["state"] == "pending"                     # fault not injected yet
    assert client.post(f"{API}/demo/verify", headers=eng).status_code == 409     # no case opened yet
    assert client.post(f"{API}/demo/verify", headers=op).status_code == 403      # engineers verify
    fin = client.post(f"{API}/demo/finish", headers=op).json()
    assert fin["session"]["key"] == "pipeline_leak"
    assert client.get(f"{API}/demo/status", headers=op).json() == {"active": False}
    hist = client.get(f"{API}/demo/history", headers=op).json()
    assert hist and hist[0]["session"]["key"] == "pipeline_leak"
