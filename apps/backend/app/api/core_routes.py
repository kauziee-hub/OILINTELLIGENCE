"""Auth, users, assets/topology, telemetry, transfers, events endpoints."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.security import ROLE_RANK, Principal, create_token, current_user, hash_password, require_role, \
    verify_password
from app.models import orm
from app.services.db_backend import jsonable
from app.services.runtime import get_runtime

router = APIRouter()


def parse_time(v: str | float | None, default: float | None = None) -> float | None:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


# ------------------------------------------------------------------- auth
@router.post("/auth/token", tags=["auth"])
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    u = db.get(orm.User, form.username)
    if u is None or not u.active or not verify_password(form.password, u.password_hash):
        raise HTTPException(401, "invalid credentials")
    db.add(orm.AuditLog(actor=u.username, role=u.role, action="auth:login", resource="session"))
    db.commit()
    return {"access_token": create_token(u.username, u.role), "token_type": "bearer", "role": u.role,
            "username": u.username}


@router.get("/auth/config", tags=["auth"])
def auth_config():
    """Public: whether the UI needs a sign-in page (False on an open-access company deployment)."""
    s = get_settings()
    if s.auth_required:
        return {"auth_required": True}
    return {"auth_required": False, "username": s.open_access_user, "role": s.open_access_role}


@router.get("/auth/me", tags=["auth"])
def me(user: Principal = Depends(current_user)):
    return {"username": user.username, "role": user.role}


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    full_name: str = ""
    role: str = Field(pattern="^(admin|engineer|operator|viewer)$")
    password: str = Field(min_length=10, max_length=128)


@router.get("/users", tags=["auth"])
def list_users(db: Session = Depends(get_db), _: Principal = Depends(require_role("admin"))):
    return [{"username": u.username, "full_name": u.full_name, "role": u.role, "active": u.active}
            for u in db.execute(select(orm.User)).scalars()]


@router.post("/users", tags=["auth"])
def create_user(body: UserIn, db: Session = Depends(get_db), _: Principal = Depends(require_role("admin"))):
    if db.get(orm.User, body.username):
        raise HTTPException(409, "user exists")
    db.add(orm.User(username=body.username, full_name=body.full_name, role=body.role,
                    password_hash=hash_password(body.password)))
    db.commit()
    return {"created": body.username}


# ----------------------------------------------------------------- assets
@router.get("/assets", tags=["assets"])
def list_assets(asset_type: str | None = None, db: Session = Depends(get_db), _: Principal = Depends(current_user)):
    q = select(orm.Asset)
    if asset_type:
        q = q.where(orm.Asset.asset_type == asset_type)
    return [{"asset_id": a.asset_id, "asset_type": a.asset_type, "asset_name": a.asset_name, "location": a.location,
             "parent_asset_id": a.parent_asset_id, "operating_status": a.operating_status} for a in db.execute(q).scalars()]


@router.get("/assets/{asset_id}", tags=["assets"])
def get_asset(asset_id: str, db: Session = Depends(get_db), _: Principal = Depends(current_user)):
    a = db.get(orm.Asset, asset_id)
    if a is None:
        raise HTTPException(404, "asset not found")
    rel = db.execute(select(orm.AssetRelationship).where(
        (orm.AssetRelationship.from_asset == asset_id) | (orm.AssetRelationship.to_asset == asset_id))).scalars().all()
    return {"asset_id": a.asset_id, "asset_type": a.asset_type, "asset_name": a.asset_name,
            "parent_asset_id": a.parent_asset_id, "operating_status": a.operating_status,
            "configuration": a.configuration,
            "relationships": [{"from": r.from_asset, "to": r.to_asset, "type": r.relation_type} for r in rel]}


@router.get("/assets/{asset_id}/sensors", tags=["assets"])
def asset_sensors(asset_id: str, db: Session = Depends(get_db), _: Principal = Depends(current_user)):
    rt = get_runtime()
    out = []
    for s in db.execute(select(orm.Sensor).where(orm.Sensor.asset_id == asset_id)).scalars():
        lv = rt.buffer.latest.get((s.sensor_id, s.parameter))
        out.append({"sensor_id": s.sensor_id, "sensor_type": s.sensor_type, "parameter": s.parameter, "unit": s.unit,
                    "latest": {k: lv.get(k) for k in ("value", "quality", "event_timestamp")} if lv else None})
    return out


@router.get("/topology", tags=["assets"])
def topology(_: Principal = Depends(current_user)):
    P = get_runtime().plant
    nodes = ([{"id": t, "type": "tank", "position": c.position, "label": c.name} for t, c in P.tanks.items()]
             + [{"id": j, "type": "manifold", "position": c.position, "label": c.name} for j, c in P.junctions.items()]
             + [{"id": p, "type": "pump", "position": c.position, "label": c.name} for p, c in P.pumps.items()])
    edges = ([{"id": k, "type": "pipeline", "from": p.from_, "to": p.to, "path": p.path, "diameter_mm":
               p.nominal_diameter_mm, "length_m": p.length_m} for k, p in P.pipelines.items()]
             + [{"id": f"{k}-link", "type": "pump", "from": p.from_, "to": p.to, "pump": k} for k, p in P.pumps.items()])
    valves = [{"id": k, "pipeline": v.pipeline, "position_on_line": v.position_on_line, "role": v.role,
               "tank_side": v.tank_side} for k, v in P.valves.items()]
    sensors = [{"id": k, "type": s.type, "asset": s.asset, "location": s.location, "parameter": s.parameter,
                "unit": s.unit} for k, s in P.sensors.items()]
    routes = {k: r.model_dump() for k, r in P.routes.items()}
    return {"plant_id": P.plant_id, "name": P.name, "nodes": nodes, "edges": edges, "valves": valves,
            "sensors": sensors, "routes": routes}


# -------------------------------------------------------------- telemetry
@router.get("/telemetry/latest", tags=["telemetry"])
def telemetry_latest(_: Principal = Depends(current_user)):
    rt = get_runtime()
    return {"timestamp": rt.buffer.last_event_ts, "mode": rt.source,
            "values": [{k: v.get(k) for k in ("sensor_id", "asset_id", "parameter", "value", "unit", "quality",
                                              "event_timestamp", "source")} for v in rt.buffer.latest.values()]}


@router.get("/telemetry/history", tags=["telemetry"])
def telemetry_history(sensor_id: list[str] = Query(...), start: str | None = None, end: str | None = None,
                      parameter: str | None = None, max_points: int = Query(500, ge=10, le=10000),
                      _: Principal = Depends(current_user)):
    rt = get_runtime()
    P = rt.plant
    bad = [s for s in sensor_id if s not in P.sensors]
    if bad:
        raise HTTPException(404, f"unknown sensor(s): {bad}")
    now = rt.backend.now()
    t1 = parse_time(end, now)
    t0 = parse_time(start, t1 - 4 * 3600)
    if t1 - t0 > 31 * 86400:
        raise HTTPException(422, "range too large (max 31 days)")
    df = rt.backend.telemetry(t0, t1, sensor_id)
    out = {}
    for sid in sensor_id:
        par = parameter or P.sensors[sid].parameter
        d = df[(df.sensor_id == sid) & (df.parameter == par)]
        if len(d) > max_points:
            bins = np.linspace(t0, t1, max_points + 1)
            d = d.assign(b=np.digitize(d["ts"], bins)).groupby("b").agg(
                ts=("ts", "mean"), value=("value", "mean"), quality=("quality", "last")).reset_index(drop=True)
        out[sid] = {"parameter": par, "unit": P.sensors[sid].unit,
                    "points": [[float(t), (None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)), q]
                               for t, v, q in zip(d["ts"], d["value"], d["quality"])]}
    return {"start": t0, "end": t1, "series": out}


@router.get("/telemetry/quality", tags=["telemetry"])
def telemetry_quality(window_s: float = 1800, _: Principal = Depends(current_user)):
    rt = get_runtime()
    now = rt.backend.now()
    span = window_s
    if rt.buffer.epoch is not None:            # a run that started minutes ago cannot have 30 min of data
        span = max(min(window_s, now - rt.buffer.epoch), 1.0)
    df = rt.buffer.frame(now - span, now)
    P = rt.plant
    res = []
    speed = rt.sim.status().get("speed", 1) if rt.sim else 1
    for sid, s in P.sensors.items():
        d = df[(df.sensor_id == sid) & (df.parameter == s.parameter)]
        expected = max(span / max(s.publish_interval_s, speed), 1)
        vals = pd.to_numeric(d["value"], errors="coerce")
        flat = len(d) >= 6 and vals.nunique() == 1 and not (s.type in ("flow",) and (vals == 0).all()) and \
            s.type in ("level", "temperature", "pressure", "density", "flow")
        res.append({"sensor_id": sid, "asset_id": s.asset, "samples": int(len(d)),
                    "completeness": round(min(len(d) / expected, 1.0), 3),
                    "bad_fraction": round(float((d["quality"] == "BAD").mean()) if len(d) else 0.0, 3),
                    "flatlined": bool(flat),
                    "last_ts": float(d["ts"].max()) if len(d) else None})
    comp = float(np.mean([r["completeness"] for r in res])) if res else 0.0
    return {"window_s": span, "overall_completeness": round(comp, 3), "sensors": res}


@router.get("/sensors/{sensor_id}", tags=["telemetry"])
def sensor_detail(sensor_id: str, db: Session = Depends(get_db), _: Principal = Depends(current_user)):
    rt = get_runtime()
    s = db.get(orm.Sensor, sensor_id)
    if s is None:
        raise HTTPException(404, "sensor not found")
    cal = db.execute(select(orm.CalibrationRecord).where(orm.CalibrationRecord.sensor_id == sensor_id)
                     .order_by(orm.CalibrationRecord.timestamp.desc()).limit(5)).scalars().all()
    lv = rt.buffer.latest.get((sensor_id, s.parameter))
    return {"sensor_id": s.sensor_id, "asset_id": s.asset_id, "sensor_type": s.sensor_type, "parameter": s.parameter,
            "unit": s.unit, "configuration": s.configuration,
            "latest": jsonable({k: lv.get(k) for k in ("value", "quality", "event_timestamp")}) if lv else None,
            "calibration_history": [jsonable({"record_type": c.record_type, "timestamp": c.timestamp,
                                              "result": c.result, **(c.data or {})}) for c in cal]}


# --------------------------------------------------------------- records
@router.get("/transfers", tags=["transfers"])
def list_transfers(limit: int = Query(100, le=1000), db: Session = Depends(get_db), _: Principal = Depends(current_user)):
    rt = get_runtime()
    rows = db.execute(select(orm.Transfer).where(orm.Transfer.source == rt.source)
                      .order_by(orm.Transfer.start_time.desc()).limit(limit)).scalars().all()
    return [jsonable({c.name: getattr(r, c.name) for c in orm.Transfer.__table__.columns}) for r in rows]


@router.get("/events/anomalies", tags=["events"])
def list_anomalies(status: str | None = None, limit: int = Query(100, le=1000), db: Session = Depends(get_db),
                   _: Principal = Depends(current_user)):
    rt = get_runtime()
    q = select(orm.AnomalyEvent).where(orm.AnomalyEvent.source == rt.source)
    if status:
        q = q.where(orm.AnomalyEvent.status == status)
    rows = db.execute(q.order_by(orm.AnomalyEvent.detected_at.desc()).limit(limit)).scalars().all()
    return [jsonable({c.name: getattr(r, c.name) for c in orm.AnomalyEvent.__table__.columns}) for r in rows]


@router.get("/records/operator-log", tags=["events"])
def operator_log(limit: int = Query(100, le=1000), db: Session = Depends(get_db), _: Principal = Depends(current_user)):
    rows = db.execute(select(orm.OperatorLog).order_by(orm.OperatorLog.timestamp.desc()).limit(limit)).scalars().all()
    return [jsonable({"timestamp": r.timestamp, "asset_id": r.asset_id, "category": r.category, "text": r.text})
            for r in rows]


@router.get("/audit", tags=["system"])
def audit(limit: int = Query(200, le=2000), action: str | None = None, db: Session = Depends(get_db),
          _: Principal = Depends(require_role("admin"))):
    q = select(orm.AuditLog)
    if action:
        q = q.where(orm.AuditLog.action.like(f"{action}%"))
    rows = db.execute(q.order_by(orm.AuditLog.id.desc()).limit(limit)).scalars().all()
    return [jsonable({"timestamp": r.timestamp, "actor": r.actor, "role": r.role, "action": r.action,
                      "resource": r.resource, "status_code": r.status_code, "details": r.details}) for r in rows]
