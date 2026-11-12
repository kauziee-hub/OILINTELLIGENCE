"""InvestigationBackend implementation over the application database.

Telemetry comes from the in-memory ring buffer when it covers the requested
period, otherwise from the telemetry table. Records come from the records
tables. Investigations, evidence, recommendations, reviews and diagnostic cases
are persisted; every tool call is written to the audit log.
"""
from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import select

from agents.tools.backend import InvestigationBackend
from app.core.db import SessionLocal
from app.models import orm
from intelligence.service import DiagnosticEngine

log = logging.getLogger("oilintel.dbbackend")


def _aw(d: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; all stored times are UTC."""
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _df(rows: list[dict[str, Any]], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)


def jsonable(x):
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, float):
        return None if (math.isnan(x) or math.isinf(x)) else x
    if isinstance(x, (datetime, pd.Timestamp)):
        return x.isoformat()
    if hasattr(x, "item") and not isinstance(x, (str, bytes)):
        try:
            return jsonable(x.item())
        except Exception:
            return str(x)
    return x


class DbBackend(InvestigationBackend):
    def __init__(self, engine: DiagnosticEngine, buffer, source: str):
        self.engine = engine
        self.plant = engine.plant
        self.buffer = buffer
        self.source = source

    def now(self) -> float:
        if self.buffer.last_event_ts:
            return self.buffer.complete_ts() or self.buffer.last_event_ts   # every sensor has reported
        with SessionLocal() as s:
            v = s.execute(select(orm.Telemetry.ts_epoch).where(orm.Telemetry.source == self.source)
                          .order_by(orm.Telemetry.ts_epoch.desc()).limit(1)).scalar()
        return float(v) if v else datetime.now(timezone.utc).timestamp()

    def data_start(self) -> float | None:
        if self.buffer.epoch is not None:       # simulation run: earlier data is a different run
            return self.buffer.epoch
        a, _ = self.buffer.coverage()
        if a is not None:
            with SessionLocal() as s:
                v = s.execute(select(orm.Telemetry.ts_epoch).where(orm.Telemetry.source == self.source,
                                                                   orm.Telemetry.ts_epoch < a)
                              .order_by(orm.Telemetry.ts_epoch).limit(1)).scalar()
            return float(v) if v is not None else a
        return None

    # ------------------------------------------------------------------ data
    def telemetry(self, t0, t1, sensor_ids=None):
        if self.buffer.epoch is not None:       # never mix in an earlier simulation run (different plant state)
            t0 = max(t0, self.buffer.epoch)
        a, b = self.buffer.coverage()
        if a is not None and a <= t0 + 60:
            return self.buffer.frame(t0, t1, sensor_ids)
        with SessionLocal() as s:
            q = select(orm.Telemetry.ts_epoch, orm.Telemetry.sensor_id, orm.Telemetry.asset_id, orm.Telemetry.parameter,
                       orm.Telemetry.value, orm.Telemetry.unit, orm.Telemetry.quality, orm.Telemetry.source).where(
                orm.Telemetry.ts_epoch >= t0, orm.Telemetry.ts_epoch <= t1, orm.Telemetry.source == self.source)
            if sensor_ids:
                q = q.where(orm.Telemetry.sensor_id.in_(sensor_ids))
            rows = s.execute(q).all()
        df = pd.DataFrame(rows, columns=["ts", "sensor_id", "asset_id", "parameter", "value", "unit", "quality", "source"])
        df["timestamp"] = pd.to_datetime(df["ts"], unit="s", utc=True)
        return df.sort_values("ts")

    def records(self, t0, t1):
        tmax = datetime.fromtimestamp(t1, tz=timezone.utc)
        q = select(orm.Transfer).where(orm.Transfer.start_time <= tmax, orm.Transfer.source == self.source)
        if self.buffer.epoch is not None:
            # simulation: transfers of earlier runs never apply. A run replaced mid-transfer leaves its
            # transfer IN_PROGRESS for ever; read as current, it "books" the new run's metered flow on that
            # route and hides unbooked movement (a passing valve looked like a leak).
            q = q.where(orm.Transfer.start_time >= datetime.fromtimestamp(self.buffer.epoch - 1, tz=timezone.utc))
        with SessionLocal() as s:
            tr = s.execute(q).scalars().all()
            cal = s.execute(select(orm.CalibrationRecord).where(orm.CalibrationRecord.timestamp <= tmax)).scalars().all()
            lab = s.execute(select(orm.LabSample).where(orm.LabSample.timestamp <= tmax)).scalars().all()
            mt = s.execute(select(orm.MaintenanceRecord)).scalars().all()
            lg = s.execute(select(orm.OperatorLog).where(orm.OperatorLog.timestamp <= tmax)).scalars().all()
        def done(r):
            return r.end_time is not None and _aw(r.end_time) <= tmax

        transfers = _df([{"transfer_id": r.transfer_id, "route": r.route, "source_tank": r.source_tank,
                          "destination_tank": r.destination_tank, "start_time": _aw(r.start_time),
                          "end_time": (_aw(r.end_time) if done(r) else None),
                          "recorded_quantity": (r.recorded_quantity if done(r) else None),
                          "custody_meter": r.custody_meter,
                          "status": (r.status if done(r) else "IN_PROGRESS"),
                          "quantity_unit": r.quantity_unit} for r in tr],
                        ["transfer_id", "route", "source_tank", "destination_tank", "start_time", "end_time",
                         "recorded_quantity", "custody_meter", "status", "quantity_unit"])
        cals = _df([{"record_id": r.record_id, "sensor_id": r.sensor_id, "record_type": r.record_type,
                     "timestamp": _aw(r.timestamp), "result": r.result, **(r.data or {})} for r in cal],
                   ["record_id", "sensor_id", "record_type", "timestamp", "result"])
        labs = _df([{"sample_id": r.sample_id, "tank_id": r.tank_id, "timestamp": _aw(r.timestamp),
                     "density_15c": r.density_15c} for r in lab], ["sample_id", "tank_id", "timestamp", "density_15c"])
        mts = _df([{"record_id": r.record_id, "asset_id": r.asset_id, "record_type": r.record_type,
                    "timestamp": _aw(r.timestamp), "notes": r.notes} for r in mt],
                  ["record_id", "asset_id", "record_type", "timestamp", "notes"])
        logs = _df([{"log_id": r.log_id, "timestamp": _aw(r.timestamp), "asset_id": r.asset_id, "category": r.category,
                     "text": r.text} for r in lg], ["log_id", "timestamp", "asset_id", "category", "text"])
        return {"transfers": transfers, "calibrations": cals, "lab_samples": labs, "maintenance": mts,
                "operator_log": logs}

    # ------------------------------------------------------------ persistence
    def find_open_investigation(self, assets, t0, t1):
        with SessionLocal() as s:
            rows = s.execute(select(orm.Investigation).where(orm.Investigation.status.notin_(
                ["CLOSED", "REJECTED"]), orm.Investigation.source == self.source)).scalars().all()
        epoch = self.buffer.epoch
        for r in rows:
            if epoch is not None and r.t1 < epoch:
                continue            # earlier simulation run: never the same episode
            if (set(r.assets or []) & set(assets) or not assets) and r.t1 >= t0 - 3600:
                return r.investigation_id
            if r.t1 >= t1 - 5400 and r.status in ("AWAITING_VERIFICATION", "AWAITING_REVIEW", "COLLECTING_EVIDENCE",
                                                  "RECEIVED"):
                return r.investigation_id
        return None

    def create_investigation(self, event):
        iid = f"INV-{uuid.uuid4().hex[:6].upper()}"
        with SessionLocal() as s:
            s.add(orm.Investigation(investigation_id=iid, event_id=event.get("event_id"), status="RECEIVED",
                                    t0=event.get("t0") or 0.0, t1=event.get("t1") or 0.0,
                                    assets=event.get("assets", []), summary=event.get("summary", ""),
                                    state={"event": jsonable(event)}, source=self.source))
            if event.get("event_id"):
                ev = s.get(orm.AnomalyEvent, event["event_id"])
                if ev is not None:
                    ev.investigation_id = iid
                    ev.status = "INVESTIGATING"
            s.commit()
        return iid

    def save_investigation(self, inv_id, state):
        st = jsonable(state)
        rep = st.get("report") or {}
        att = st.get("attribution") or {}
        top = (att.get("hypotheses") or [{}])[0] if att.get("hypotheses") else {}
        with SessionLocal() as s:
            inv = s.get(orm.Investigation, inv_id)
            if inv is None:
                inv = orm.Investigation(investigation_id=inv_id, status="RECEIVED", t0=0, t1=0, source=self.source)
                s.add(inv)
            inv.status = st.get("status", inv.status)
            inv.t0 = st.get("t0", inv.t0) or inv.t0
            inv.t1 = st.get("t1", inv.t1) or inv.t1
            inv.assets = (st.get("event") or {}).get("assets", inv.assets or [])
            inv.top_hypothesis = top.get("hypothesis_id")
            inv.top_probability = top.get("calibrated_probability")
            inv.summary = (rep.get("narrative") or inv.summary or "")[:4000]
            inv.state = {k: v for k, v in st.items() if k != "report"}
            inv.report = rep
            inv.updated_at = datetime.now(timezone.utc)
            # evidence register and recommendation (replace current snapshot)
            s.query(orm.InvestigationEvidence).filter_by(investigation_id=inv_id).delete()
            for e in rep.get("evidence", []):
                s.add(orm.InvestigationEvidence(investigation_id=inv_id, evidence_id=e["id"], evidence_type=e["type"],
                                                source=str(e["source"])[:96], summary=e["summary"],
                                                values=e.get("values", {})))
            rc = st.get("recommended_check")
            if rc:
                exists = s.execute(select(orm.Recommendation).where(
                    orm.Recommendation.investigation_id == inv_id, orm.Recommendation.action_id == rc["action_id"],
                    orm.Recommendation.status == "PENDING")).scalar()
                if exists is None:
                    s.add(orm.Recommendation(investigation_id=inv_id, action_id=rc["action_id"], action=rc["action"],
                                             target=rc.get("target"),
                                             expected_information_gain_bits=rc["expected_information_gain_bits"],
                                             cost=rc["cost"], rationale=rc.get("rationale", "")))
            for v in st.get("verifications", []):
                for r in s.execute(select(orm.Recommendation).where(orm.Recommendation.investigation_id == inv_id,
                                                                    orm.Recommendation.action_id == v["action_id"])
                                   ).scalars():
                    r.status, r.outcome = "DONE", v["outcome"]
            if inv.status in ("CLOSED", "REJECTED") and inv.event_id:
                ev = s.get(orm.AnomalyEvent, inv.event_id)
                if ev:
                    ev.status = "RESOLVED"
            s.commit()

    def load_investigation(self, inv_id):
        with SessionLocal() as s:
            inv = s.get(orm.Investigation, inv_id)
            if inv is None:
                return None
            st = dict(inv.state or {})
            st["report"] = inv.report
            st["investigation_id"] = inv.investigation_id
            st["status"] = inv.status
            return st

    def audit(self, actor, action, details):
        try:
            with SessionLocal() as s:
                s.add(orm.AuditLog(actor=actor, role=details.get("role", "agent") if isinstance(details, dict) else "",
                                   action=action, resource=str(details.get("investigation_id", ""))
                                   if isinstance(details, dict) else "", details=jsonable(details)))
                s.commit()
        except Exception:
            log.exception("audit write failed")

    def record_diagnostic_case(self, case):
        c = jsonable(case)
        with SessionLocal() as s:
            s.merge(orm.DiagnosticCase(case_id=c["case_id"], residual=c.get("residual"),
                                       uncertainty=c.get("uncertainty"), candidate_faults=c.get("candidate_faults", []),
                                       verified_cause=c.get("verified_cause"),
                                       verification_method=c.get("verification_method", "")[:256],
                                       reviewer_status=c.get("reviewer_status", ""), data=c))
            s.commit()

    def verified_cases(self, limit: int = 2000) -> list[dict[str, Any]]:
        """Engineer-confirmed diagnostic cases (experience memory of the adaptive layer)."""
        with SessionLocal() as s:
            rows = s.execute(select(orm.DiagnosticCase).where(orm.DiagnosticCase.verified_cause.is_not(None))
                             .order_by(orm.DiagnosticCase.created_at.desc()).limit(limit)).scalars().all()
            return [dict(r.data or {}, case_id=r.case_id, verified_cause=r.verified_cause,
                         residual=r.residual, candidate_faults=r.candidate_faults) for r in rows]

    def add_review_row(self, inv_id: str, reviewer: str, review: dict[str, Any]) -> None:
        with SessionLocal() as s:
            s.add(orm.EngineerReview(investigation_id=inv_id, reviewer=reviewer, decision=review.get("decision", ""),
                                     verified_cause=review.get("verified_cause"), action_id=review.get("action_id"),
                                     outcome=review.get("outcome"), comments=review.get("comments", "")))
            s.commit()
