"""Adaptive intelligence endpoints: drift, adaptive cycles, governed self-calibration, experience
memory and digital-twin hypothesis validation."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import Principal, current_user, require_role
from app.services.db_backend import jsonable
from app.services.runtime import get_runtime

router = APIRouter(tags=["adaptive"])


def _ad():
    ad = get_runtime().adaptive
    if ad is None:
        raise HTTPException(409, "adaptive layer is disabled (ADAPTIVE_ENABLED=0)")
    return ad


@router.get("/adaptive/status")
def adaptive_status(_: Principal = Depends(current_user)):
    rt = get_runtime()
    out = _ad().status()
    out["models"] = rt.engine.model_versions
    out["calibration"] = {"temperature": rt.engine.temperature, "prior_mix": rt.engine.prior_mix}
    out["monitor"] = rt.monitor.status() if rt.monitor else None
    return jsonable(out)


@router.post("/adaptive/run")
def adaptive_run(user: Principal = Depends(require_role("engineer"))):
    rt = get_runtime()
    cyc = _ad().run_cycle(f"manual:{user.username}", rt.buffer.last_event_ts)
    rt.backend.audit(user.username, "adaptive:cycle", {"cycle": cyc["id"], "drift": cyc["drift_overall"]})
    return jsonable(cyc)


class DecisionIn(BaseModel):
    comments: str = Field(default="", max_length=2000)


@router.post("/adaptive/adaptations/{adaptation_id}/{decision}")
def adaptive_decide(adaptation_id: str, decision: str, body: DecisionIn | None = None,
                    user: Principal = Depends(require_role("engineer"))):
    d = decision.upper()
    if d not in ("APPROVE", "REJECT", "REVERT"):
        raise HTTPException(422, "decision must be approve, reject or revert")
    try:
        a = _ad().decide(adaptation_id, d, user.username)
    except KeyError:
        raise HTTPException(404, "adaptation not found")
    except ValueError as e:
        raise HTTPException(409, str(e))
    get_runtime().backend.audit(user.username, f"adaptive:{d.lower()}", {"adaptation": a,
                                                                          "comments": (body.comments if body else "")})
    return jsonable(a)


@router.get("/adaptive/experience")
def adaptive_experience(_: Principal = Depends(current_user)):
    return jsonable(_ad().learn_from_experience())


def twin_validate_investigation(inv_id: str, user: str) -> dict[str, Any]:
    rt = get_runtime()
    st = rt.backend.load_investigation(inv_id)
    if st is None:
        raise HTTPException(404, "investigation not found")
    att = st.get("attribution") or {}
    hyps = att.get("hypotheses") or []
    if not hyps or not st.get("t0") or not st.get("t1"):
        raise HTTPException(409, "investigation has no attribution to validate yet")
    t0, t1, W = float(st["t0"]), float(st["t1"]), 1800.0
    n = int((t1 - t0) // W)
    if n < 1:
        raise HTTPException(409, "investigation period is shorter than one balance window")
    edges = [t1 - k * W for k in range(n, -1, -1)]
    tel = rt.backend.telemetry(edges[0] - 900 - 120, t1 + 60)
    if not len(tel):
        raise HTTPException(409, "telemetry for the investigation period is no longer buffered")
    rec = rt.backend.records(t1 - 30 * 86400, t1)
    res = _ad().twin_validate(tel, rec, edges, hyps)
    st["twin_validation"] = jsonable(res)
    rt.backend.save_investigation(inv_id, st)
    rt.backend.audit(user, "investigation:twin_validation", {"investigation_id": inv_id,
                                                             "best_supported": res.get("best_supported")})
    return res


@router.post("/investigations/{inv_id}/twin-validate", tags=["investigations"])
def twin_validate(inv_id: str, user: Principal = Depends(require_role("engineer"))):
    """Replay the leading hypotheses through the nonlinear simulator (digital twin) from the measured
    initial state and recorded operations, and compare with the observed balance residuals."""
    return jsonable(twin_validate_investigation(inv_id, user.username))
