"""Investigation orchestrator - LangGraph state machine (section 8).

Investigation graph (steps 1-12):
  intake -> create_case -> retrieve_telemetry -> validate_quality
     -(missing critical evidence)-> request_evidence -> report -> END (NEEDS_DATA)
     -> recalculate_balance -> attribute -> retrieve_records -> competing
     -> evaluate_tests -> select_action -> report -> request_review -> END

Review graph (steps 13-14), entered when an engineer submits a verification
result or a decision:
  record_outcome -(resolved / confirmed / rejected)-> finalize -> END
                 -(unresolved)-> evaluate_tests -> select_action -> report -> request_review -> END

The ML/Bayesian model owns the numbers; nodes only call deterministic tools,
assemble evidence and route. The LLM (optional) is used only inside `report`.
"""
from __future__ import annotations

import math
import time
from typing import Any, TypedDict

import numpy as np
from langgraph.graph import END, START, StateGraph

from agents.llm.provider import get_provider
from agents.reports.render import build_report
from agents.tools.backend import InvestigationBackend
from agents.tools.registry import ToolContext, call_tool
from intelligence.active_diagnosis.tests import action_by_id
from intelligence.fault_attribution.hypotheses import build_hypotheses

LOOKBACK_S = 4 * 3600.0
RESOLVED_P = 0.9


class InvState(TypedDict, total=False):
    investigation_id: str
    event: dict[str, Any]
    status: str
    t0: float
    t1: float
    steps: list[dict[str, Any]]
    telemetry_summary: dict[str, Any]
    missing_evidence: list[str]
    balance: dict[str, Any]
    uncertainty: dict[str, Any]
    attribution: dict[str, Any]
    posterior: dict[str, float]
    records_evidence: list[dict[str, Any]]
    conflicts: list[str]
    competing: list[str]
    ranked_tests: list[dict[str, Any]]
    recommended_check: dict[str, Any] | None
    verifications: list[dict[str, Any]]
    performed: list[str]
    reviews: list[dict[str, Any]]
    review_input: dict[str, Any]
    report: dict[str, Any]
    errors: list[dict[str, Any]]
    model_versions: dict[str, str]
    final: dict[str, Any]
    duplicate_of: str | None
    twin_validation: dict[str, Any]          # digital-twin replay results (adaptive layer), kept across reviews


class Investigator:
    def __init__(self, backend: InvestigationBackend, llm=None, actor: str = "investigation-agent"):
        self.backend = backend
        self.llm = llm if llm is not None else get_provider()
        self.actor = actor
        self.graph = self._build_investigation_graph()
        self.review_graph = self._build_review_graph()

    # ============================================================== helpers
    def _ctx(self, s: InvState) -> ToolContext:
        if not hasattr(self, "_tool_ctx") or self._tool_ctx.investigation_id != s.get("investigation_id"):
            self._tool_ctx = ToolContext(self.backend, actor=self.actor, role="agent",
                                         investigation_id=s.get("investigation_id"))
        return self._tool_ctx

    def _tool(self, s: InvState, name: str, args: dict[str, Any]) -> dict[str, Any]:
        r = call_tool(self._ctx(s), name, args)
        step = {"step": name, "ok": r["ok"], "t": time.time()}
        if not r["ok"]:
            step["error"] = r["error"]
            s.setdefault("errors", []).append({"tool": name, **r["error"]})
        s.setdefault("steps", []).append(step)
        return r

    @staticmethod
    def _note(s: InvState, step: str, **kw) -> None:
        s.setdefault("steps", []).append({"step": step, "ok": True, "t": time.time(), **kw})

    # ===================================================== investigation graph
    def n_intake(self, s: InvState) -> InvState:
        ev = s["event"]
        t1 = float(ev.get("t1") or self.backend.now())
        t0 = float(ev.get("t0") or t1 - LOOKBACK_S)
        if t1 - t0 < LOOKBACK_S:            # give the balance enough windows of context
            t0 = t1 - LOOKBACK_S
        start = self.backend.data_start()
        if start is not None:
            t0 = max(t0, start + 300.0)     # never analyse before the data begins
        s.update(t0=t0, t1=t1, status="RECEIVED", verifications=s.get("verifications", []),
                 performed=s.get("performed", []), conflicts=[], records_evidence=[], errors=[])
        self._note(s, "intake", window=[t0, t1])
        return s

    def n_create_case(self, s: InvState) -> InvState:
        ev = s["event"]
        r = self._tool(s, "create_investigation", {
            "event_id": ev.get("event_id", "EVT"), "event_type": ev.get("event_type", "ANOMALY"),
            "assets": ev.get("assets", []), "t0": s["t0"], "t1": s["t1"], "severity": ev.get("severity", "WARNING"),
            "summary": ev.get("summary", ""), "payload": ev.get("payload", {})})
        if not r["ok"]:
            s["status"] = "REJECTED"
            s["investigation_id"] = s.get("investigation_id") or "INVALID"
            return s
        res = r["result"]
        s["investigation_id"] = res["investigation_id"]
        if not res["created"]:
            s["status"] = "DUPLICATE"
            s["duplicate_of"] = res["investigation_id"]
        else:
            s["status"] = "COLLECTING_EVIDENCE"
        return s

    def n_retrieve_telemetry(self, s: InvState) -> InvState:
        P = self.backend.plant
        key = list(P.meters()) + list(P.sensors_of_type("level")) + [
            x for x in P.sensors_of_type("temperature") if P.sensors[x].asset != P.plant_id]
        r = self._tool(s, "get_sensor_history", {"sensor_ids": key, "start": s["t0"], "end": s["t1"], "max_points": 60})
        if r["ok"]:
            s["telemetry_summary"] = {k: {kk: v[kk] for kk in ("samples", "min", "max", "mean", "last") if kk in v}
                                      for k, v in r["result"]["sensors"].items()}
        else:
            s["telemetry_summary"] = {}
        for a in s["event"].get("assets", [])[:4]:
            self._tool(s, "get_asset_details", {"asset_id": a})
        return s

    def n_validate_quality(self, s: InvState) -> InvState:
        P = self.backend.plant
        summ = s.get("telemetry_summary", {})
        missing = []
        if not summ:
            missing.append("no telemetry could be retrieved for the analysis period")
        if s["t1"] - s["t0"] < 3600.0:
            missing.append("less than one hour of telemetry is available; at least two balance windows are required")
        # gauges are compared with each other (sampling rates differ between historians / sim speeds)
        counts = {tk: summ.get(P.tank_level_sensor(tk), {}).get("samples", 0) for tk in P.tanks}
        ref = sorted(counts.values())[len(counts) // 2] if counts else 0
        for tk, n in counts.items():
            if n == 0 or n < 0.25 * ref:
                missing.append(f"level gauge {P.tank_level_sensor(tk)} ({tk}) has {n} samples in the period "
                               "(tank inventory unavailable)")
        s["missing_evidence"] = missing
        if missing:
            s["status"] = "NEEDS_DATA"
        self._note(s, "validate_quality", missing=len(missing))
        return s

    def n_request_evidence(self, s: InvState) -> InvState:
        self._note(s, "request_evidence", requested=s.get("missing_evidence", []))
        return s

    def n_recalculate_balance(self, s: InvState) -> InvState:
        args = {"start": s["t0"], "end": s["t1"], "window_s": 1800.0}
        r = self._tool(s, "calculate_mass_balance", args)
        if r["ok"]:
            s["balance"] = r["result"]
        else:
            s["status"] = "NEEDS_DATA"
            s.setdefault("missing_evidence", []).append(f"mass balance could not be computed: {r['error']['message']}")
        u = self._tool(s, "calculate_uncertainty", args)
        if u["ok"]:
            s["uncertainty"] = u["result"]
        return s

    def n_attribute(self, s: InvState) -> InvState:
        r = self._tool(s, "run_fault_attribution", {"start": s["t0"], "end": s["t1"], "window_s": 1800.0})
        if not r["ok"]:
            s["status"] = "NEEDS_DATA"
            s.setdefault("missing_evidence", []).append(f"attribution failed: {r['error']['message']}")
            return s
        s["attribution"] = r["result"]
        post = dict(r["result"]["hypothesis_distribution"])
        # sequential Bayes: outcomes of checks already performed remain evidence after a refresh
        if s.get("verifications"):
            allh = {h.hid: h for h in build_hypotheses(self.backend.plant)}
            ids = [h for h in post if h in allh]
            p = np.array([post[h] for h in ids])
            for v in s["verifications"]:
                act = action_by_id(self.backend.plant, v["action_id"])
                y = 0 if v["outcome"] == "POSITIVE" else 1
                p = p * np.array([act.likelihood(self.backend.plant, allh[h])[y] for h in ids])
            if p.sum() > 0:
                post = {h: float(x) for h, x in zip(ids, p / p.sum())}
                for h in s["attribution"].get("hypotheses", []):
                    h["calibrated_probability"] = round(post.get(h["hypothesis_id"], 0.0), 4)
                s["attribution"]["hypotheses"].sort(key=lambda h: -h["calibrated_probability"])
        s["posterior"] = post
        s["model_versions"] = dict(self.backend.engine.model_versions)
        return s

    def n_retrieve_records(self, s: InvState) -> InvState:
        P = self.backend.plant
        att = s.get("attribution", {})
        top = att.get("hypotheses", [])[:4]
        recs = s.setdefault("records_evidence", [])
        tr = self._tool(s, "get_transfer_history", {"start": s["t0"] - 3600, "end": s["t1"]})
        if tr["ok"]:
            t = tr["result"]["transfers"]
            recs.append({"source": "get_transfer_history",
                         "summary": f"{len(t)} transfer record(s) overlap the period: " +
                                    ", ".join(f"{x['transfer_id']} {x['route']} {x['status']}" for x in t[:5]),
                         "values": {"count": len(t)}})
        log = self._tool(s, "get_operator_log", {"start": s["t0"], "end": s["t1"]})
        if log["ok"]:
            manual = [e for e in log["result"]["entries"] if "no ticket" in str(e.get("text", "")).lower()]
            if manual:
                recs.append({"source": "get_operator_log", "summary": "; ".join(e["text"] for e in manual[:3]),
                             "values": {"entries": len(manual)}})
        for h in top:
            tg = h.get("target")
            if not tg:
                continue
            if tg in P.sensors and P.sensors[tg].type == "flow":
                c = self._tool(s, "get_meter_calibration", {"sensor_id": tg})
                if c["ok"] and c["result"]["records"]:
                    last = c["result"]["records"][0]
                    recs.append({"source": "get_meter_calibration",
                                 "summary": f"Last proving of {tg}: {last.get('result')} meter factor "
                                            f"{last.get('meter_factor')} on {str(last.get('timestamp'))[:10]}",
                                 "values": {"sensor": tg, "meter_factor": last.get("meter_factor")}})
                    if h["calibrated_probability"] > 0.4 and last.get("result") == "PASS":
                        s.setdefault("conflicts", []).append(
                            f"{tg} passed its last proving ({str(last.get('timestamp'))[:10]}); a bias would have "
                            f"developed since then - a fresh proving is needed to confirm.")
            elif tg in P.sensors and P.sensors[tg].type in ("level", "temperature", "density"):
                c = self._tool(s, "get_meter_calibration", {"sensor_id": tg})
                if c["ok"] and c["result"]["records"]:
                    last = c["result"]["records"][0]
                    recs.append({"source": "get_meter_calibration",
                                 "summary": f"Last check of {tg}: {last.get('record_type')} {last.get('result')} "
                                            f"as-found {last.get('as_found_error')} {last.get('unit', '')}",
                                 "values": {"sensor": tg}})
            elif tg in P.all_asset_ids():
                m = self._tool(s, "get_maintenance_records", {"asset_id": tg})
                if m["ok"] and m["result"]["records"]:
                    last = m["result"]["records"][-1]
                    recs.append({"source": "get_maintenance_records",
                                 "summary": f"{tg}: last {last.get('record_type')} on {str(last.get('timestamp'))[:10]}"
                                            f" - {last.get('notes', '')}", "values": {"asset": tg}})
        return s

    def n_competing(self, s: InvState) -> InvState:
        att = s.get("attribution", {})
        hyps = att.get("hypotheses", [])
        if hyps:
            p0 = hyps[0]["calibrated_probability"]
            s["competing"] = [h["hypothesis_id"] for h in hyps[1:] if h["calibrated_probability"] >= p0 / 4]
        q = (s.get("balance") or {}).get("data_quality_issues") or {}
        if q and hyps and hyps[0]["family"] not in ("SENSOR_STUCK", "SENSOR_DROPOUT"):
            s.setdefault("conflicts", []).append(
                "Instrument data-quality issues were detected (" + ", ".join(q) + "); balances that depend on these "
                "instruments are less reliable.")
        self._note(s, "competing", competing=s.get("competing", []))
        return s

    def n_evaluate_tests(self, s: InvState) -> InvState:
        post = s.get("posterior") or {}
        if not post:
            s["ranked_tests"] = []
            return s
        items = sorted(post.items(), key=lambda kv: -kv[1])[:30]
        z = sum(p for _, p in items)
        r = self._tool(s, "evaluate_diagnostic_tests", {
            "hypotheses": [h for h, _ in items], "probabilities": [p / z for _, p in items],
            "transfer_active": self.backend.transfer_active(s["t1"]), "performed": s.get("performed", []), "top": 6})
        s["ranked_tests"] = r["result"]["ranked_actions"] if r["ok"] else []
        return s

    def n_select_action(self, s: InvState) -> InvState:
        avail = [a for a in s.get("ranked_tests", []) if a["available"] and a["expected_information_gain_bits"] > 0.01]
        s["recommended_check"] = avail[0] if avail else None
        return s

    def n_report(self, s: InvState) -> InvState:
        att = s.get("attribution") or {}
        if s["status"] not in ("NEEDS_DATA", "DUPLICATE", "REJECTED", "CLOSED"):
            if att.get("status") == "NO_ABNORMALITY_CONFIRMED":
                s["status"] = "AWAITING_REVIEW"
            elif s.get("recommended_check"):
                s["status"] = "AWAITING_VERIFICATION"
            else:
                s["status"] = "AWAITING_REVIEW"
        s["report"] = build_report(s, self.llm)
        self._note(s, "report", narrative_source=s["report"]["narrative_source"])
        return s

    def n_request_review(self, s: InvState) -> InvState:
        self._persist(s)
        self.backend.audit(self.actor, "investigation:awaiting_review",
                           {"investigation_id": s["investigation_id"], "status": s["status"]})
        return s

    def _persist(self, s: InvState) -> None:
        st = {k: v for k, v in s.items() if k not in ("review_input",)}
        st["tool_calls"] = list(self._ctx(s).log) if hasattr(self, "_tool_ctx") else []
        self.backend.save_investigation(s["investigation_id"], st)

    @staticmethod
    def _route_after_case(s: InvState) -> str:
        return "end" if s["status"] in ("DUPLICATE", "REJECTED") else "continue"

    @staticmethod
    def _route_after_quality(s: InvState) -> str:
        return "missing" if s["status"] == "NEEDS_DATA" else "ok"

    @staticmethod
    def _route_after_balance(s: InvState) -> str:
        return "missing" if s["status"] == "NEEDS_DATA" else "ok"

    def _build_investigation_graph(self):
        g = StateGraph(InvState)
        for name in ("intake", "create_case", "retrieve_telemetry", "validate_quality", "request_evidence",
                     "recalculate_balance", "attribute", "retrieve_records", "competing", "evaluate_tests",
                     "select_action", "report", "request_review"):
            g.add_node(name, getattr(self, f"n_{name}"))
        g.add_edge(START, "intake")
        g.add_edge("intake", "create_case")
        g.add_conditional_edges("create_case", self._route_after_case, {"end": END, "continue": "retrieve_telemetry"})
        g.add_edge("retrieve_telemetry", "validate_quality")
        g.add_conditional_edges("validate_quality", self._route_after_quality,
                                {"missing": "request_evidence", "ok": "recalculate_balance"})
        g.add_conditional_edges("recalculate_balance", self._route_after_balance,
                                {"missing": "request_evidence", "ok": "attribute"})
        g.add_conditional_edges("attribute", self._route_after_balance,
                                {"missing": "request_evidence", "ok": "retrieve_records"})
        g.add_edge("request_evidence", "report")
        g.add_edge("retrieve_records", "competing")
        g.add_edge("competing", "evaluate_tests")
        g.add_edge("evaluate_tests", "select_action")
        g.add_edge("select_action", "report")
        g.add_edge("report", "request_review")
        g.add_edge("request_review", END)
        return g.compile()

    # =========================================================== review graph
    def n_record_outcome(self, s: InvState) -> InvState:
        ri = s.get("review_input") or {}
        ctx = ToolContext(self.backend, actor=ri.get("reviewer", "engineer"), role=ri.get("role", "engineer"),
                          investigation_id=s["investigation_id"])
        r = call_tool(ctx, "submit_engineer_feedback", {
            "investigation_id": s["investigation_id"], "decision": ri.get("decision", "VERIFICATION_RESULT"),
            "verified_cause": ri.get("verified_cause"), "action_id": ri.get("action_id"), "outcome": ri.get("outcome"),
            "comments": ri.get("comments", ""), "reviewer": ri.get("reviewer", "")})
        s.setdefault("steps", []).append({"step": "submit_engineer_feedback", "ok": r["ok"], "t": time.time(),
                                          **({"error": r["error"]} if not r["ok"] else {})})
        if not r["ok"]:
            s.setdefault("errors", []).append(r["error"])
            s["review_input"] = {**ri, "decision": "INVALID"}
            return s
        s.setdefault("reviews", []).append({k: ri.get(k) for k in ("decision", "verified_cause", "action_id",
                                                                   "outcome", "comments", "reviewer")})
        if ri.get("decision") == "VERIFICATION_RESULT" and ri.get("action_id") and ri.get("outcome"):
            post = s.get("posterior") or {}
            allh = {h.hid: h for h in build_hypotheses(self.backend.plant)}
            ids = [h for h in post if h in allh]
            act = action_by_id(self.backend.plant, ri["action_id"])
            y = 0 if ri["outcome"] == "POSITIVE" else 1
            p = np.array([post[h] for h in ids]) * np.array([act.likelihood(self.backend.plant, allh[h])[y] for h in ids])
            p = p / p.sum() if p.sum() > 0 else p
            s["posterior"] = {h: float(v) for h, v in zip(ids, p)}
            s.setdefault("verifications", []).append({"action_id": ri["action_id"], "outcome": ri["outcome"],
                                                      "detail": ri.get("detail"), "reviewer": ri.get("reviewer")})
            s.setdefault("performed", []).append(ri["action_id"])
            # refresh displayed hypothesis probabilities from the updated posterior
            att = s.get("attribution") or {}
            for h in att.get("hypotheses", []):
                h["calibrated_probability"] = round(s["posterior"].get(h["hypothesis_id"], 0.0), 4)
            att["hypotheses"] = sorted(att.get("hypotheses", []), key=lambda h: -h["calibrated_probability"])
            top = max(s["posterior"].items(), key=lambda kv: kv[1])
            if top[0] not in [h["hypothesis_id"] for h in att.get("hypotheses", [])]:
                hh = allh[top[0]]
                att.setdefault("hypotheses", []).insert(0, {
                    "hypothesis_id": hh.hid, "family": hh.family, "target": hh.target, "category": hh.category,
                    "label": hh.label, "calibrated_probability": round(top[1], 4), "magnitude": {}})
            s["attribution"] = att
        return s

    @staticmethod
    def _route_after_outcome(s: InvState) -> str:
        ri = s.get("review_input") or {}
        if ri.get("decision") in ("CONFIRM", "REJECT"):
            return "finalize"
        if ri.get("decision") == "INVALID":
            return "end"
        post = s.get("posterior") or {}
        if post and max(post.values()) >= RESOLVED_P:
            return "finalize"
        return "continue"

    def n_finalize(self, s: InvState) -> InvState:
        ri = s.get("review_input") or {}
        post = s.get("posterior") or {}
        top = max(post.items(), key=lambda kv: kv[1]) if post else (None, 0.0)
        decision = ri.get("decision")
        if decision == "REJECT":
            s["status"] = "REJECTED"
            cause = ri.get("verified_cause") or "NOT_CONFIRMED"
        elif decision == "CONFIRM":
            s["status"] = "CLOSED"
            cause = ri.get("verified_cause") or top[0]
        else:
            s["status"] = "RESOLVED_PENDING_APPROVAL"   # model is confident; engineer must still confirm
            cause = top[0]
        s["final"] = {"verified_cause": cause, "posterior_top": top[0], "posterior_top_p": top[1],
                      "verifications": s.get("verifications", []), "decision": decision}
        if s["status"] == "CLOSED":
            # reviewed outcome becomes a labelled diagnostic case for future training (step 14)
            self.backend.record_diagnostic_case({
                "case_id": s["investigation_id"], "residual": (s.get("balance") or {}).get("facility_residual_t"),
                "uncertainty": (s.get("balance") or {}).get("facility_residual_sigma_t"),
                "candidate_faults": (s.get("attribution") or {}).get("hypotheses", [])[:5],
                "verified_cause": cause,
                "verification_method": ", ".join(v["action_id"] for v in s.get("verifications", [])) or "engineer review",
                "reviewer_status": "CONFIRMED", "t0": s.get("t0"), "t1": s.get("t1")})
        # resolved (or closed) cases need an engineer decision, not further checks
        s["recommended_check"] = None
        s["report"] = build_report(s, self.llm)
        self._persist(s)
        self.backend.audit(ri.get("reviewer", "engineer"), "investigation:finalized",
                           {"investigation_id": s["investigation_id"], "status": s["status"], "cause": cause})
        return s

    def _build_review_graph(self):
        g = StateGraph(InvState)
        for name in ("record_outcome", "finalize", "evaluate_tests", "select_action", "report", "request_review"):
            g.add_node(name, getattr(self, f"n_{name}"))
        g.add_edge(START, "record_outcome")
        g.add_conditional_edges("record_outcome", self._route_after_outcome,
                                {"finalize": "finalize", "continue": "evaluate_tests", "end": END})
        g.add_edge("finalize", END)
        g.add_edge("evaluate_tests", "select_action")
        g.add_edge("select_action", "report")
        g.add_edge("report", "request_review")
        g.add_edge("request_review", END)
        return g.compile()

    # ================================================================== API
    def investigate(self, event: dict[str, Any]) -> InvState:
        return self.graph.invoke({"event": event})

    def refresh(self, investigation_id: str, t1: float | None = None) -> InvState:
        """Re-run the analysis of an open investigation on newly available evidence.

        Verification outcomes already recorded are re-applied to the new posterior."""
        st = self.backend.load_investigation(investigation_id)
        if st is None:
            raise KeyError(investigation_id)
        if st.get("status") in ("CLOSED", "REJECTED", "SUPERSEDED", "RUN_ENDED"):
            raise ValueError(f"investigation {investigation_id} is {st['status']}")
        s: InvState = {k: v for k, v in st.items() if k not in ("tool_calls", "review_input")}
        s["t1"] = float(t1 or self.backend.now())
        start = self.backend.data_start()
        s["t0"] = max(s["t1"] - LOOKBACK_S, (start + 300.0) if start else s["t1"] - LOOKBACK_S)
        s["status"] = "COLLECTING_EVIDENCE"
        s["conflicts"], s["records_evidence"], s["errors"], s["missing_evidence"] = [], [], [], []
        self._note(s, "refresh", window=[s["t0"], s["t1"]])
        for node in (self.n_retrieve_telemetry, self.n_validate_quality):
            s = node(s)
        if s["status"] != "NEEDS_DATA":
            for node in (self.n_recalculate_balance, self.n_attribute):
                s = node(s)
                if s["status"] == "NEEDS_DATA":
                    break
        if s["status"] != "NEEDS_DATA":
            for node in (self.n_retrieve_records, self.n_competing, self.n_evaluate_tests, self.n_select_action):
                s = node(s)
        s = self.n_report(s)
        return self.n_request_review(s)

    def review(self, investigation_id: str, review_input: dict[str, Any]) -> InvState:
        st = self.backend.load_investigation(investigation_id)
        if st is None:
            raise KeyError(investigation_id)
        if st.get("status") in ("CLOSED", "REJECTED", "RUN_ENDED"):
            raise ValueError(f"investigation {investigation_id} is already {st['status']}")
        state = {k: v for k, v in st.items() if k != "tool_calls"}
        state["review_input"] = review_input
        return self.review_graph.invoke(state)
