"""Agent tools and investigation workflow, incl. the agent evaluation cases of section 13.5."""
import numpy as np
import pytest

from agents.evidence.grounding import check_grounding
from agents.graph.workflow import Investigator
from agents.llm.provider import LLMResult, TemplateProvider
from agents.tools.backend import InMemoryBackend
from agents.tools.registry import TOOLS, ToolContext, call_tool
from intelligence.physics.measurement import to_epoch


@pytest.fixture(scope="module")
def leak_backend(leak_run):
    return InMemoryBackend(leak_run)


def t(run, s):
    return to_epoch(run.scenario.start_time) + s


def test_tools_have_schemas_roles_and_no_control_path():
    for name, spec in TOOLS.items():
        assert spec.schema is not None and spec.roles and spec.timeout_s > 0
        assert not name.startswith(("write", "set_", "command", "start", "stop", "open", "close"))
    # the agent's role only grants read/analysis tools plus investigation bookkeeping
    agent_tools = {n for n, s in TOOLS.items() if "agent" in s.roles}
    assert agent_tools == {n for n in TOOLS if n.startswith(("get_", "calculate_", "run_", "evaluate_"))} | \
        {"create_investigation"}


def test_invalid_equipment_id_rejected(leak_backend, leak_run):
    ctx = ToolContext(leak_backend)
    r = call_tool(ctx, "get_sensor_history", {"sensor_ids": ["FM-999"], "start": t(leak_run, 0), "end": t(leak_run, 600)})
    assert not r["ok"] and r["error"]["code"] == "INVALID_ID"
    assert ctx.log[-1]["status"] == "ERROR"


def test_schema_validation_and_permissions(leak_backend, leak_run):
    ctx = ToolContext(leak_backend)
    bad = call_tool(ctx, "calculate_mass_balance", {"start": t(leak_run, 600), "end": t(leak_run, 0)})
    assert bad["error"]["code"] == "INVALID_INPUT"
    forbidden = call_tool(ctx, "submit_engineer_feedback", {"investigation_id": "X", "decision": "CONFIRM"})
    assert forbidden["error"]["code"] == "FORBIDDEN"        # the agent cannot approve its own findings
    unknown = call_tool(ctx, "open_valve", {})
    assert unknown["error"]["code"] == "UNKNOWN_TOOL"


def test_failed_query_reported(leak_run):
    class Broken(InMemoryBackend):
        def telemetry(self, *a, **k):
            raise ConnectionError("historian unreachable")

    ctx = ToolContext(Broken(leak_run))
    r = call_tool(ctx, "calculate_mass_balance", {"start": t(leak_run, 600), "end": t(leak_run, 7200)})
    assert not r["ok"] and r["error"]["code"] == "TOOL_FAILURE"


def test_investigation_end_to_end_with_verification(leak_run):
    be = InMemoryBackend(leak_run)
    inv = Investigator(be, llm=TemplateProvider())
    st = inv.investigate({"event_id": "EVT-T1", "event_type": "BALANCE_ANOMALY", "assets": ["MF-DIS"],
                          "t1": t(leak_run, 3.9 * 3600)})
    assert st["status"] == "AWAITING_VERIFICATION"
    rep = st["report"]
    assert rep["requires_engineer_approval"] is True
    assert rep["observed_residual_tonnes"] > 0
    assert rep["recommended_check"]["action"] in ("LINE_INSPECTION", "RETRIEVE_METER_PROVING_RECORD")
    assert all(e["id"].startswith("E") for e in rep["evidence"]) and "[E1]" in rep["narrative"]
    assert len(st["competing"]) >= 1                         # competing explanations are preserved
    # engineer reports a positive line inspection on the true pipe
    st2 = inv.review(st["investigation_id"], {"decision": "VERIFICATION_RESULT", "action_id": "LINE_INSPECTION:PL-002",
                                              "outcome": "POSITIVE", "reviewer": "eng", "role": "engineer"})
    top = max(st2["posterior"].items(), key=lambda kv: kv[1])
    assert top[0] == "PIPELINE_LEAK:PL-002" and top[1] > 0.9
    st3 = inv.review(st["investigation_id"], {"decision": "CONFIRM", "verified_cause": top[0], "reviewer": "eng",
                                              "role": "engineer"})
    assert st3["status"] == "CLOSED"
    assert be.cases and be.cases[-1]["verified_cause"] == "PIPELINE_LEAK:PL-002"   # feedback -> training data
    with pytest.raises(ValueError):
        inv.review(st["investigation_id"], {"decision": "CONFIRM", "reviewer": "eng", "role": "engineer"})


def test_repeated_investigation_not_duplicated(leak_run):
    be = InMemoryBackend(leak_run)
    inv = Investigator(be, llm=TemplateProvider())
    ev = {"event_id": "EVT-A", "event_type": "BALANCE_ANOMALY", "assets": ["MF-DIS"], "t1": t(leak_run, 3.5 * 3600)}
    first = inv.investigate(ev)
    second = inv.investigate({**ev, "event_id": "EVT-B"})
    assert second["status"] == "DUPLICATE" and second["duplicate_of"] == first["investigation_id"]
    assert len(be.investigations) == 1


def test_missing_evidence_requests_data(leak_run):
    run = leak_run
    tel = run.telemetry
    stripped = type(run)(**{**run.__dict__, "telemetry": tel[tel.sensor_id != "LT-002"]})
    inv = Investigator(InMemoryBackend(stripped), llm=TemplateProvider())
    st = inv.investigate({"event_id": "EVT-M", "event_type": "BALANCE_ANOMALY", "assets": ["TK-002"],
                          "t1": t(run, 3.5 * 3600)})
    assert st["status"] == "NEEDS_DATA"
    assert any("LT-002" in m for m in st["missing_evidence"])
    assert not st["report"]["candidate_hypotheses"]          # no confident diagnosis without evidence


def test_grounding_check_rejects_invented_numbers():
    evidence = {"observed_residual_tonnes": 8.03, "candidate_hypotheses": [{"label": "leak PL-003",
                                                                           "calibrated_probability": 0.62}]}
    ok, _ = check_grounding("Residual 8.03 t; leak on PL-003 with probability 62%.", evidence)
    assert ok
    ok2, problems = check_grounding("The leak on PL-003 lost 14.7 t through FM-009.", evidence)
    assert not ok2 and any("14.7" in p for p in problems) and any("FM-009" in p for p in problems)


def test_ungrounded_llm_narrative_is_discarded(leak_run):
    class Hallucinating:
        name = "fake"

        def narrate(self, payload):
            return LLMResult(text="Pump PU-009 leaked 999.9 tonnes.", provider="fake", model="fake")

    inv = Investigator(InMemoryBackend(leak_run), llm=Hallucinating())
    st = inv.investigate({"event_id": "EVT-H", "event_type": "BALANCE_ANOMALY", "assets": ["MF-DIS"],
                          "t1": t(leak_run, 3.9 * 3600)})
    assert st["report"]["narrative_source"] == "template"
    assert st["report"]["grounding"]["passed"] is False


def test_review_keeps_twin_validation(leak_run):
    """State written outside the graph (twin replay) must survive later verification reviews."""
    be = InMemoryBackend(leak_run)
    inv = Investigator(be, llm=TemplateProvider())
    st = inv.investigate({"event_id": "EVT-TW", "event_type": "BALANCE_ANOMALY", "assets": ["MF-DIS"],
                          "t1": t(leak_run, 3.9 * 3600)})
    saved = be.load_investigation(st["investigation_id"])
    saved["twin_validation"] = {"best_supported": "PIPELINE_LEAK:PL-002"}
    be.save_investigation(st["investigation_id"], saved)
    st2 = inv.review(st["investigation_id"], {"decision": "VERIFICATION_RESULT", "action_id": "LINE_INSPECTION:PL-003",
                                              "outcome": "NEGATIVE", "reviewer": "eng", "role": "engineer"})
    assert (st2.get("twin_validation") or {}).get("best_supported") == "PIPELINE_LEAK:PL-002"
