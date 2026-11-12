"""API integration tests and the end-to-end MVP acceptance sequence (section 19.1)."""
import time

import pytest

API = "/api/v1"


def test_health_and_metrics(client):
    h = client.get(f"{API}/health").json()
    assert h["mode"] == "SIMULATION" and h["ingestion"]["written"] > 0
    m = client.get("/metrics").text
    assert "oilintel_telemetry_written_total" in m and "oilintel_api_latency_p95_ms" in m


def test_asset_registry_and_topology(client, tokens):
    h = tokens["viewer"]
    assets = client.get(f"{API}/assets", headers=h).json()
    ids = {a["asset_id"] for a in assets}
    assert {"TK-001", "TK-002", "TK-003", "PL-001", "PL-004", "PU-001", "PU-002", "XV-004"} <= ids
    topo = client.get(f"{API}/topology", headers=h).json()
    assert topo["edges"]
    assert client.get(f"{API}/assets/XX-999", headers=h).status_code == 404


def test_live_telemetry_flows(client, tokens):
    h = tokens["viewer"]
    latest = client.get(f"{API}/telemetry/latest", headers=h).json()
    assert latest["mode"] == "SIMULATION" and len(latest["values"]) >= 20
    assert {"sensor_id", "value", "quality", "event_timestamp"} <= set(latest["values"][0])
    hist = client.get(f"{API}/telemetry/history", params={"sensor_id": ["LT-001", "FM-001"]}, headers=h).json()
    assert hist["series"]["LT-001"]["points"] and hist["series"]["LT-001"]["unit"] == "m"
    assert client.get(f"{API}/telemetry/history", params={"sensor_id": "XX-1"}, headers=h).status_code == 404


def test_simulation_controls(client, tokens):
    h = tokens["operator"]
    st = client.get(f"{API}/simulation/status", headers=h).json()
    assert st["state"] == "RUNNING"
    assert client.put(f"{API}/simulation/speed", json={"speed": 1000}, headers=h).status_code == 422
    assert client.put(f"{API}/simulation/speed", json={"speed": 120}, headers=h).status_code == 200
    bad = client.post(f"{API}/simulation/faults", json={"fault_type": "PIPELINE_LEAK", "target": "PL-999"}, headers=h)
    assert bad.status_code == 422
    cat = client.get(f"{API}/simulation/catalog", headers=h).json()
    assert "PIPELINE_LEAK" in cat["fault_types"] and cat["routes"]



def test_analysis_never_spans_a_run_boundary(client, tokens):
    """A new run starts from a new initial tank state; investigations and balances must only
    use data from the current run (data_start = run start), never earlier stored history."""
    from datetime import datetime

    from app.services.runtime import get_runtime
    rt = get_runtime()
    deadline = time.time() + 10
    while time.time() < deadline:
        st = rt.sim.status()
        run_start = datetime.fromisoformat(st["sim_timestamp"]).timestamp() - st["sim_time_s"]
        if rt.buffer.epoch is not None and abs(rt.buffer.epoch - run_start) < 2:
            break
        time.sleep(0.2)
    assert rt.buffer.epoch is not None and abs(rt.buffer.epoch - run_start) < 2
    assert rt.backend.data_start() == rt.buffer.epoch

def test_twin_state_shape(client, tokens):
    s = client.get(f"{API}/twin/state", headers=tokens["viewer"]).json()
    assert {"tanks", "pipelines", "pumps", "valves", "sensors"} <= set(s)
    assert "truth" not in s                      # ground truth is never mixed into the operational view


def test_manual_investigation_validation(client, tokens):
    h = tokens["engineer"]
    r = client.post(f"{API}/investigations", json={"assets": ["TK-999"]}, headers=h)
    assert r.status_code == 422
    assert client.get(f"{API}/investigations/INV-NOPE", headers=h).status_code == 404


@pytest.mark.slow
def test_end_to_end_leak_investigation(client, tokens):
    """Start simulation -> normal baseline -> inject leak -> anomaly -> investigation -> evidence ->
    ranked hypotheses -> verification -> engineer confirmation -> report. Runs at 120x real time."""
    eng, op = tokens["engineer"], tokens["operator"]
    st = client.get(f"{API}/simulation/status", headers=op).json()
    route = (st.get("active_transfer") or {}).get("route") or st["pending_transfers"][0]["route"]
    pipe = {"R-01": "PL-002", "R-02": "PL-003", "R-03": "PL-003"}[route]
    # let a normal baseline accumulate (>= 1.5 simulated hours)
    deadline = time.time() + 120
    while client.get(f"{API}/simulation/status", headers=op).json()["sim_time_s"] < 5400 and time.time() < deadline:
        time.sleep(2)
    r = client.post(f"{API}/simulation/faults", headers=op,
                    json={"fault_type": "PIPELINE_LEAK", "target": pipe,
                          "params": {"position": 0.45, "hole_diameter_mm": 9, "ramp_s": 60}})
    assert r.status_code == 200
    inv = None
    deadline = time.time() + 240
    while time.time() < deadline and inv is None:
        time.sleep(5)
        invs = client.get(f"{API}/investigations", headers=eng).json()
        done = [i for i in invs if i["status"] not in ("RECEIVED", "COLLECTING_EVIDENCE")]
        inv = done[0] if done else None
    assert inv, "no investigation was completed after the leak"
    assert client.get(f"{API}/events/anomalies", headers=eng).json()
    iid = inv["investigation_id"]
    time.sleep(45)                                  # more post-onset evidence, then re-analyse
    assert client.post(f"{API}/investigations/{iid}/refresh", headers=eng).status_code == 200
    d = client.get(f"{API}/investigations/{iid}", headers=eng).json()
    rep = d["report"]
    assert rep["observed_residual_tonnes"] > 0 and rep["requires_engineer_approval"]
    assert rep["candidate_hypotheses"] and d["tool_calls"]
    top3 = [h["hypothesis_id"] for h in rep["candidate_hypotheses"][:3]]
    assert f"PIPELINE_LEAK:{pipe}" in top3, top3
    for _ in range(5):
        rc = d.get("recommended_check")
        if not rc or d["status"] in ("CLOSED", "REJECTED", "RESOLVED_PENDING_APPROVAL"):
            break
        v = client.post(f"{API}/investigations/{iid}/verify", headers=eng,
                        json={"action_id": rc["action_id"], "use_simulation_oracle": True})
        assert v.status_code == 200
        d = client.get(f"{API}/investigations/{iid}", headers=eng).json()
    top = next(iter(d["posterior"]))
    if d["status"] == "RESOLVED_PENDING_APPROVAL":     # a resolved case must name the true cause
        assert top == f"PIPELINE_LEAK:{pipe}" and d["posterior"][top] > 0.9, d["posterior"]
    else:                                            # otherwise no wrong cause may be confidently resolved
        assert top == f"PIPELINE_LEAK:{pipe}" or d["posterior"][top] < 0.9, d["posterior"]
    r = client.post(f"{API}/investigations/{iid}/review", headers=eng,
                    json={"decision": "CONFIRM", "verified_cause": f"PIPELINE_LEAK:{pipe}", "comments": "field-confirmed"})
    assert r.status_code == 200 and r.json()["status"] == "CLOSED"
    html = client.get(f"{API}/investigations/{iid}/report", params={"format": "html"}, headers=eng)
    assert html.status_code == 200 and "<h2>Candidate causes</h2>" in html.text and "Evidence register" in html.text
    md = client.get(f"{API}/investigations/{iid}/report", params={"format": "markdown"}, headers=eng)
    assert "## Recommended verification" in md.text and "## Verification history" in md.text
    truth = client.get(f"{API}/simulation/truth/latest", headers=eng).json()
    assert truth["truth"]["physical_loss_cum_t"] > 0
    k = client.get(f"{API}/reports/kpis", headers=eng).json()
    assert k["engineer_reviews"] >= 1


def test_adaptive_layer_endpoints(client, tokens):
    s = client.get(f"{API}/adaptive/status", headers=tokens["viewer"]).json()
    assert len(s["architecture"]) == 12 and len(s["steps"]) == 10
    assert s["drift"]["status"] in ("WARMING_UP", "ACTIVE") and s["retrain_gate"]["status"] in ("NOT_NEEDED", "WATCH")
    cyc = client.post(f"{API}/adaptive/run", headers=tokens["engineer"]).json()
    assert [st["key"] for st in cyc["steps"]][0] == "detect_drift" and len(cyc["steps"]) == 10
    assert client.get(f"{API}/adaptive/status", headers=tokens["viewer"]).json()["last_cycle"]["id"] == cyc["id"]
    assert client.post(f"{API}/adaptive/adaptations/AD-NOPE/approve", headers=tokens["engineer"]).status_code == 404
    assert client.post(f"{API}/adaptive/adaptations/AD-NOPE/explode", headers=tokens["engineer"]).status_code == 422
    exp = client.get(f"{API}/adaptive/experience", headers=tokens["viewer"]).json()
    assert abs(sum(f["learned_prior"] for f in exp["families"]) - 1.0) < 1e-3
    assert client.post(f"{API}/investigations/INV-NOPE/twin-validate", headers=tokens["engineer"]).status_code == 404
