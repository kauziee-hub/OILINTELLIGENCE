"""Physics, ML, investigation, reporting and integration endpoints."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents.reports.render import to_html
from agents.tools.registry import ToolContext, call_tool
from app.api.core_routes import parse_time
from app.core.config import REPO_ROOT
from app.core.db import SessionLocal, get_db
from app.core.security import Principal, current_user, require_role
from app.models import orm
from app.services.db_backend import jsonable
from app.services.runtime import get_runtime

router = APIRouter()


def _ctx(user: Principal) -> ToolContext:
    role = "engineer" if user.role in ("engineer", "admin") else "viewer"
    return ToolContext(get_runtime().backend, actor=user.username, role=role)


def _tool(user, name, args):
    r = call_tool(_ctx(user), name, args)
    if not r["ok"]:
        code = {"INVALID_ID": 404, "INVALID_INPUT": 422, "NO_DATA": 409, "INSUFFICIENT_PERIOD": 422,
                "FORBIDDEN": 403, "TIMEOUT": 504}.get(r["error"]["code"], 500)
        raise HTTPException(code, r["error"]["message"])
    return r["result"]


class PeriodIn(BaseModel):
    start: str | float | None = None
    end: str | float | None = None
    window_s: float = Field(default=1800.0, ge=600, le=7200)
    exclude_families: list[str] = Field(default_factory=list)


def _period(body: PeriodIn, default_hours: float = 4.0) -> tuple[float, float]:
    rt = get_runtime()
    t1 = parse_time(body.end, rt.backend.now())
    t0 = parse_time(body.start, t1 - default_hours * 3600)
    ds = rt.backend.data_start()
    if ds is not None and t0 < ds:              # the current run (or plant history) starts later
        t0 = ds
    return t0, t1


# --------------------------------------------------------------- physics
@router.post("/physics/reconcile", tags=["physics"])
def reconcile(body: PeriodIn, user: Principal = Depends(current_user)):
    t0, t1 = _period(body)
    res = _tool(user, "calculate_mass_balance", {"start": t0, "end": t1, "window_s": body.window_s})
    res["uncertainty"] = _tool(user, "calculate_uncertainty", {"start": t0, "end": t1, "window_s": body.window_s})
    return jsonable(res)


@router.get("/physics/residuals", tags=["physics"])
def residuals(start: str | None = None, end: str | None = None, limit: int = Query(500, le=5000),
              db: Session = Depends(get_db), _: Principal = Depends(current_user)):
    rt = get_runtime()
    t1 = parse_time(end, rt.backend.now() + 1)
    t0 = parse_time(start, t1 - 24 * 3600)
    if rt.buffer.epoch is not None:             # simulation: earlier rows belong to other runs (other plant states)
        t0 = max(t0, rt.buffer.epoch)
    rows = db.execute(select(orm.BalanceResult).where(orm.BalanceResult.t1_epoch >= t0, orm.BalanceResult.t1_epoch <= t1,
                                                      orm.BalanceResult.source == rt.source)
                      .order_by(orm.BalanceResult.t1_epoch.desc()).limit(limit)).scalars().all()[::-1]
    # limit keeps the most recent results; returned oldest -> newest
    return [jsonable({"t1": r.t1_epoch, "window_s": r.window_s, "route": r.route,
                      "facility_residual_t": r.facility_residual_t, "chi2": r.chi2, "dof": r.dof, "p_value": r.p_value,
                      "max_abs_z": r.max_abs_z, "anomaly": r.anomaly, "scores": r.scores, "rows": r.rows}) for r in rows]


# ------------------------------------------------------------------- ML
@router.post("/ml/anomaly-detection", tags=["ml"])
def anomaly_detection(body: PeriodIn, _: Principal = Depends(current_user)):
    rt = get_runtime()
    t1 = parse_time(body.end, rt.backend.now())
    tel = rt.backend.telemetry(t1 - body.window_s * 2 - 900, t1)
    if not len(tel):
        raise HTTPException(409, "no telemetry in period")
    ctx = rt.engine.context(tel, rt.backend.records(t1 - 30 * 86400, t1))
    res = rt.engine.monitor(ctx, t1, body.window_s)
    res.pop("features", None)
    return jsonable(res)


@router.post("/ml/fault-attribution", tags=["ml"])
def fault_attribution(body: PeriodIn, user: Principal = Depends(current_user)):
    t0, t1 = _period(body)
    res = _tool(user, "run_fault_attribution", {"start": t0, "end": t1, "window_s": body.window_s,
                                                "exclude_families": body.exclude_families})
    with SessionLocal() as s:
        s.add(orm.ModelPrediction(id=str(uuid.uuid4()), model_name="physics_bayes",
                                  model_version=res["model"]["version"], t0=t0, t1=t1,
                                  output=jsonable({k: res[k] for k in ("status", "hypotheses", "category_probabilities",
                                                                       "estimated_physical_loss_tonnes")})))
        s.commit()
    return jsonable(res)


class NbtIn(BaseModel):
    investigation_id: str | None = None
    hypotheses: list[str] | None = None
    probabilities: list[float] | None = None
    transfer_active: bool | None = None


@router.post("/ml/next-best-test", tags=["ml"])
def next_best_test(body: NbtIn, user: Principal = Depends(current_user)):
    rt = get_runtime()
    hyps, probs, performed = body.hypotheses, body.probabilities, []
    if body.investigation_id:
        st = rt.backend.load_investigation(body.investigation_id)
        if st is None:
            raise HTTPException(404, "investigation not found")
        post = st.get("posterior") or {}
        items = sorted(post.items(), key=lambda kv: -kv[1])[:30]
        hyps, probs = [h for h, _ in items], [p for _, p in items]
        performed = st.get("performed", [])
    if not hyps:
        raise HTTPException(422, "provide investigation_id or hypotheses/probabilities")
    active = body.transfer_active if body.transfer_active is not None else rt.backend.transfer_active(rt.backend.now())
    return _tool(user, "evaluate_diagnostic_tests", {"hypotheses": hyps, "probabilities": probs,
                                                     "transfer_active": active, "performed": performed, "top": 10})


@router.post("/ml/sensor-health", tags=["ml"])
def sensor_health(body: PeriodIn, _: Principal = Depends(current_user)):
    rt = get_runtime()
    t1 = parse_time(body.end, rt.backend.now())
    t0 = t1 - 3 * body.window_s
    tel = rt.backend.telemetry(t0 - 900, t1)
    ctx = rt.engine.context(tel, rt.backend.records(t1 - 30 * 86400, t1))
    ev = rt.engine.evidence(ctx, t0, t1, body.window_s)
    if ev is None:
        raise HTTPException(422, "period too short")
    return jsonable(rt.engine.sensor_health(ev, ctx))


@router.get("/ml/model-performance", tags=["ml"])
def model_performance(_: Principal = Depends(current_user)):
    p = REPO_ROOT / "experiments" / "results" / "results.json"
    idx = Path(get_runtime().settings.models_dir) / "registry" / "index.json"
    # result files may hold NaN (e.g. a latency with no detections), which JSON cannot carry
    return jsonable({"results": json.loads(p.read_text()) if p.exists() else None,
                     "registry": json.loads(idx.read_text()) if idx.exists() else {},
                     "loaded_models": get_runtime().engine.model_versions})


# -------------------------------------------------------- investigations
class InvestigationIn(BaseModel):
    assets: list[str] = Field(default_factory=list)
    start: str | float | None = None
    end: str | float | None = None
    summary: str = "Manual investigation request"
    run: bool = True
    wait: bool = False


def _inv_row(r: orm.Investigation) -> dict[str, Any]:
    return jsonable({"investigation_id": r.investigation_id, "event_id": r.event_id, "status": r.status,
                     "created_at": r.created_at, "updated_at": r.updated_at, "t0": r.t0, "t1": r.t1, "assets": r.assets,
                     "top_hypothesis": r.top_hypothesis, "top_probability": r.top_probability,
                     "summary": (r.summary or "")[:400]})


@router.post("/investigations", tags=["investigations"])
def create_investigation(body: InvestigationIn, background: BackgroundTasks,
                         user: Principal = Depends(require_role("engineer"))):
    rt = get_runtime()
    bad = [a for a in body.assets if a not in rt.plant.all_asset_ids() and a not in rt.plant.sensors]
    if bad:
        raise HTTPException(422, f"unknown assets {bad}")
    t1 = parse_time(body.end, rt.backend.now())
    t0 = parse_time(body.start, t1 - 4 * 3600)
    event = {"event_id": f"EVT-{uuid.uuid4().hex[:6].upper()}", "event_type": "MANUAL_REQUEST", "assets": body.assets,
             "t0": t0, "t1": t1, "severity": "INFO", "summary": body.summary, "requested_by": user.username}
    with SessionLocal() as s:
        s.add(orm.AnomalyEvent(event_id=event["event_id"], t0=t0, t1=t1, event_type="MANUAL_REQUEST", severity="INFO",
                               assets=body.assets, summary=body.summary, source=rt.source))
        s.commit()
    if not body.run:
        iid = rt.backend.create_investigation(event)
        return {"investigation_id": iid, "status": "RECEIVED"}
    if body.wait:
        st = rt.investigator().investigate(event)
        return jsonable({"investigation_id": st.get("investigation_id"), "status": st.get("status"),
                         "report": st.get("report")})
    fut = rt.monitor.launch_investigation(event)
    return {"event_id": event["event_id"], "status": "QUEUED", "queued": fut is not None}


@router.get("/investigations", tags=["investigations"])
def list_investigations(status: str | None = None, limit: int = Query(100, le=500), db: Session = Depends(get_db),
                        _: Principal = Depends(current_user)):
    rt = get_runtime()
    q = select(orm.Investigation).where(orm.Investigation.source == rt.source)
    if status:
        q = q.where(orm.Investigation.status == status)
    return [_inv_row(r) for r in db.execute(q.order_by(orm.Investigation.created_at.desc()).limit(limit)).scalars()]


def _similar(inv_id: str, st: dict[str, Any]) -> list[dict[str, Any]]:
    ad = get_runtime().adaptive
    if ad is None:
        return []
    dist = st.get("posterior") or (st.get("attribution") or {}).get("hypothesis_distribution") or {}
    return ad.memory.similar(dist, (st.get("balance") or {}).get("facility_residual_t"), exclude=inv_id)


@router.get("/investigations/{inv_id}", tags=["investigations"])
def get_investigation(inv_id: str, db: Session = Depends(get_db), _: Principal = Depends(current_user)):
    r = db.get(orm.Investigation, inv_id)
    if r is None:
        raise HTTPException(404, "investigation not found")
    st = dict(r.state or {})
    reviews = db.execute(select(orm.EngineerReview).where(orm.EngineerReview.investigation_id == inv_id)
                         .order_by(orm.EngineerReview.created_at)).scalars().all()
    recs = db.execute(select(orm.Recommendation).where(orm.Recommendation.investigation_id == inv_id)
                      .order_by(orm.Recommendation.created_at)).scalars().all()
    out = _inv_row(r)
    out.update(jsonable({
        "report": r.report, "steps": st.get("steps", []), "tool_calls": st.get("tool_calls", []),
        "balance": st.get("balance"), "attribution": st.get("attribution"), "ranked_tests": st.get("ranked_tests"),
        "recommended_check": st.get("recommended_check"), "verifications": st.get("verifications", []),
        "posterior": dict(sorted((st.get("posterior") or {}).items(), key=lambda kv: -kv[1])[:15]),
        "final": st.get("final"), "missing_evidence": st.get("missing_evidence", []), "errors": st.get("errors", []),
        "twin_validation": st.get("twin_validation"), "similar_cases": _similar(inv_id, st),
        "reviews": [{"reviewer": v.reviewer, "decision": v.decision, "verified_cause": v.verified_cause,
                     "action_id": v.action_id, "outcome": v.outcome, "comments": v.comments,
                     "created_at": v.created_at} for v in reviews],
        "recommendations": [{"action_id": x.action_id, "status": x.status, "outcome": x.outcome,
                             "expected_information_gain_bits": x.expected_information_gain_bits, "cost": x.cost,
                             "created_at": x.created_at} for x in recs]}))
    return out


@router.post("/investigations/{inv_id}/run", tags=["investigations"])
def rerun_investigation(inv_id: str, user: Principal = Depends(require_role("engineer"))):
    rt = get_runtime()
    st = rt.backend.load_investigation(inv_id)
    if st is None:
        raise HTTPException(404, "investigation not found")
    ev = dict(st.get("event") or {})
    ev["event_id"] = f"EVT-{uuid.uuid4().hex[:6].upper()}"
    ev["t1"] = rt.backend.now()
    ev["summary"] = f"Re-run of {inv_id} requested by {user.username}"
    with SessionLocal() as s:
        inv = s.get(orm.Investigation, inv_id)
        inv.status = "SUPERSEDED"
        s.commit()
    new = rt.investigator().investigate(ev)
    return jsonable({"investigation_id": new.get("investigation_id"), "status": new.get("status"), "supersedes": inv_id})


@router.post("/investigations/{inv_id}/refresh", tags=["investigations"])
def refresh_investigation(inv_id: str, user: Principal = Depends(require_role("engineer"))):
    """Re-analyse an open investigation with all evidence available now (verifications are kept)."""
    rt = get_runtime()
    try:
        st = rt.investigator().refresh(inv_id)
    except KeyError:
        raise HTTPException(404, "investigation not found")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return jsonable({"investigation_id": inv_id, "status": st.get("status"), "report": st.get("report")})


class ReviewIn(BaseModel):
    decision: str = Field(pattern="^(CONFIRM|REJECT|REQUEST_MORE_EVIDENCE|VERIFICATION_RESULT)$")
    verified_cause: str | None = None
    action_id: str | None = None
    outcome: str | None = Field(default=None, pattern="^(POSITIVE|NEGATIVE)$")
    detail: dict[str, Any] | None = None
    comments: str = Field(default="", max_length=4000)


@router.post("/investigations/{inv_id}/review", tags=["investigations"])
def review(inv_id: str, body: ReviewIn, user: Principal = Depends(require_role("engineer"))):
    rt = get_runtime()
    if body.decision == "VERIFICATION_RESULT" and not (body.action_id and body.outcome):
        raise HTTPException(422, "verification results need action_id and outcome")
    try:
        st = rt.investigator().review(inv_id, {**body.model_dump(), "reviewer": user.username, "role": user.role})
    except KeyError:
        raise HTTPException(404, "investigation not found")
    except ValueError as e:
        raise HTTPException(409, str(e))
    rt.backend.add_review_row(inv_id, user.username, body.model_dump())
    if rt.adaptive is not None:
        if body.decision == "VERIFICATION_RESULT" and body.action_id:
            rt.adaptive.on_verification(body.action_id, body.outcome or "", body.detail, user.username)
        elif body.decision == "CONFIRM":
            rt.adaptive.learn_from_experience()       # a verified case updates the experience memory
    rt.broadcast({"type": "investigation", "data": {"investigation_id": inv_id, "status": st.get("status")}})
    return jsonable({"investigation_id": inv_id, "status": st.get("status"), "report": st.get("report"),
                     "recommended_check": st.get("recommended_check"), "final": st.get("final")})


class VerifyIn(BaseModel):
    action_id: str
    use_simulation_oracle: bool = True
    outcome: str | None = Field(default=None, pattern="^(POSITIVE|NEGATIVE)$")
    comments: str = ""


@router.post("/investigations/{inv_id}/verify", tags=["investigations"])
def verify(inv_id: str, body: VerifyIn, user: Principal = Depends(require_role("engineer"))):
    """Record the result of a verification check. In simulation mode the check can be
    performed by the simulation oracle (sampled from ground truth with realistic sensitivity)."""
    rt = get_runtime()
    detail = None
    outcome = body.outcome
    if body.use_simulation_oracle:
        if rt.settings.data_mode != "SIMULATION" or rt.sim is None:
            raise HTTPException(409, "simulation oracle only available in SIMULATION mode")
        at, _, tg = body.action_id.partition(":")
        res = rt.sim.perform_check(at, tg or None)
        outcome, detail = res["outcome"], res.get("detail")
    if outcome is None:
        raise HTTPException(422, "outcome required when the oracle is not used")
    return review(inv_id, ReviewIn(decision="VERIFICATION_RESULT", action_id=body.action_id, outcome=outcome,
                                   detail=detail, comments=body.comments), user)


@router.get("/investigations/{inv_id}/evidence", tags=["investigations"])
def evidence(inv_id: str, db: Session = Depends(get_db), _: Principal = Depends(current_user)):
    rows = db.execute(select(orm.InvestigationEvidence).where(orm.InvestigationEvidence.investigation_id == inv_id)
                      .order_by(orm.InvestigationEvidence.id)).scalars().all()
    return [jsonable({"id": r.evidence_id, "type": r.evidence_type, "source": r.source, "summary": r.summary,
                      "values": r.values}) for r in rows]


@router.get("/investigations/{inv_id}/report", tags=["reports"])
def report(inv_id: str, format: str = Query("json", pattern="^(json|markdown|html)$"), db: Session = Depends(get_db),
           _: Principal = Depends(current_user)):
    r = db.get(orm.Investigation, inv_id)
    if r is None or not r.report:
        raise HTTPException(404, "report not available")
    if format == "markdown":
        return PlainTextResponse(r.report.get("markdown", ""), media_type="text/markdown")
    if format == "html":
        return HTMLResponse(to_html(r.report))
    return r.report


# ----------------------------------------------------------------- reports
@router.get("/reports/kpis", tags=["reports"])
def kpis(db: Session = Depends(get_db), _: Principal = Depends(current_user)):
    rt = get_runtime()
    src = rt.source
    inv = db.execute(select(orm.Investigation.status, func.count()).where(orm.Investigation.source == src)
                     .group_by(orm.Investigation.status)).all()
    an = db.execute(select(func.count()).select_from(orm.AnomalyEvent).where(orm.AnomalyEvent.source == src)).scalar()
    bal = db.execute(select(orm.BalanceResult).where(orm.BalanceResult.source == src)
                     .order_by(orm.BalanceResult.t1_epoch.desc()).limit(48)).scalars().all()
    tr = db.execute(select(orm.Transfer).where(orm.Transfer.source == src, orm.Transfer.status == "COMPLETED")).scalars().all()
    invs = db.execute(select(orm.Investigation).where(orm.Investigation.source == src)).scalars().all()
    prep = [(i.updated_at - i.created_at).total_seconds() for i in invs if i.updated_at and i.created_at]
    loss = [((i.report or {}).get("estimated_physical_loss_tonnes")) for i in invs]
    loss = [x for x in loss if x is not None]
    reviewed = db.execute(select(func.count()).select_from(orm.EngineerReview)).scalar()
    return jsonable({
        "mode": src, "anomaly_events": an, "investigations_by_status": {k: v for k, v in inv},
        "engineer_reviews": reviewed,
        "mean_investigation_preparation_s": (sum(prep) / len(prep)) if prep else None,
        "estimated_physical_loss_reported_t": sum(loss) if loss else 0.0,
        "transfers_completed": len(tr), "transferred_t": sum(t.recorded_quantity or 0 for t in tr),
        "recent_balance": [{"t1": b.t1_epoch, "facility_residual_t": b.facility_residual_t, "chi2": b.chi2,
                            "dof": b.dof, "anomaly": b.anomaly} for b in reversed(bal)],
        "data_completeness": None})


# ------------------------------------------------------------- integrations
@router.get("/integrations", tags=["integrations"])
def integrations(db: Session = Depends(get_db), _: Principal = Depends(current_user)):
    rt = get_runtime()
    out = []
    for c in db.execute(select(orm.Connector)).scalars():
        live = None
        if c.connector_id == "mqtt-internal" and rt.bus is not None:
            live = rt.bus.status()
        elif c.connector_id == "opcua-virtual":
            live = rt.opcua.status() if rt.opcua else {"running": False}
        out.append(jsonable({"connector_id": c.connector_id, "type": c.connector_type, "name": c.name,
                             "enabled": c.enabled, "read_only": c.read_only, "config": c.config,
                             "last_status": c.last_status, "live": live}))
    return out


class OpcTestIn(BaseModel):
    url: str = "opc.tcp://127.0.0.1:4840/oilintelligence/"
    username: str | None = None
    password: str | None = None


class MqttTestIn(BaseModel):
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None


class HistTestIn(BaseModel):
    kind: str = Field(default="file", pattern="^(file|rest)$")
    path: str | None = None
    base_url: str | None = None


def _save_status(cid: str, res: dict[str, Any]):
    with SessionLocal() as s:
        c = s.get(orm.Connector, cid)
        if c:
            c.last_status = jsonable(res | {"tested_at": datetime.now(timezone.utc).isoformat()})
            s.commit()


@router.post("/integrations/opcua/test", tags=["integrations"])
def opcua_test(body: OpcTestIn, _: Principal = Depends(require_role("admin"))):
    from industrial.opcua.client import OpcUaReadOnlyConnector
    res = OpcUaReadOnlyConnector(body.url, username=body.username, password=body.password).test()
    _save_status("opcua-virtual" if "127.0.0.1" in body.url or "localhost" in body.url else "opcua-plant", res)
    return jsonable(res)


@router.post("/integrations/mqtt/test", tags=["integrations"])
def mqtt_test(body: MqttTestIn, _: Principal = Depends(require_role("admin"))):
    from industrial.connectors.bus import test_mqtt_connection
    res = test_mqtt_connection(body.host, body.port, body.username, body.password)
    _save_status("mqtt-internal", res)
    return res


@router.post("/integrations/historian/test", tags=["integrations"])
def historian_test(body: HistTestIn, _: Principal = Depends(require_role("admin"))):
    from industrial.historian.adapter import FileHistorianAdapter, RestHistorianAdapter
    if body.kind == "file":
        res = FileHistorianAdapter(body.path or "datasets/historian/export.csv", {}).test()
        _save_status("historian-file", res)
    else:
        if not body.base_url:
            raise HTTPException(422, "base_url required")
        res = RestHistorianAdapter(body.base_url, {}).test()
        _save_status("historian-rest", res)
    return res


@router.get("/integrations/{cid}/status", tags=["integrations"])
def integration_status(cid: str, db: Session = Depends(get_db), _: Principal = Depends(current_user)):
    c = db.get(orm.Connector, cid)
    if c is None:
        raise HTTPException(404, "connector not found")
    return integrations(db, _)[[x["connector_id"] for x in integrations(db, _)].index(cid)]
