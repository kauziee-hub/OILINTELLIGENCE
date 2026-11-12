"""Demo scenario endpoints: start a catalogued fault scenario live and watch the platform prove itself."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import Principal, current_user, require_role
from app.services.demo import DemoManager
from app.services.runtime import get_runtime

router = APIRouter(tags=["demo"])
_manager: DemoManager | None = None


def _demo() -> DemoManager:
    global _manager
    if _manager is None:
        rt = get_runtime()
        _manager = DemoManager(rt, rt.settings.demo_history_path)
    return _manager


class StartIn(BaseModel):
    speed: float = Field(60.0, gt=0, le=600)


@router.get("/demo/scenarios")
def scenarios(_: Principal = Depends(current_user)):
    return DemoManager.catalogue()


@router.post("/demo/scenarios/{key}/start")
def start(key: str, body: StartIn, p: Principal = Depends(require_role("operator"))):
    try:
        return _demo().start(key, body.speed, p.username)
    except KeyError:
        raise HTTPException(404, f"unknown demo scenario '{key}'")
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@router.get("/demo/status")
def status(_: Principal = Depends(current_user)):
    return _demo().status()


@router.post("/demo/finish")
def finish(_: Principal = Depends(require_role("operator"))):
    return _demo().finish() or {"active": False}


@router.get("/demo/history")
def history(_: Principal = Depends(current_user)):
    return _demo().history


def _case(m: DemoManager) -> str:
    inv_id = m.investigation_id()
    if not inv_id:
        raise HTTPException(409, "no investigation has been opened for this demo yet")
    return inv_id


@router.post("/demo/verify")
def verify(p: Principal = Depends(require_role("engineer"))):
    """Play the field engineer: run each check the system recommends (outcomes come from the simulator's
    ground truth with the check's realistic sensitivity) until the case resolves or no check is left."""
    from app.api.intel_routes import VerifyIn, verify as verify_case
    m = _demo()
    inv_id = _case(m)
    done: list[dict] = []
    for _ in range(5):
        inv = m._investigation(inv_id) or {}
        chk = inv.get("recommended_check")
        if inv.get("status") != "AWAITING_VERIFICATION" or not chk:
            break
        r = verify_case(inv_id, VerifyIn(action_id=chk["action_id"]), p)
        after = m._investigation(inv_id) or {}
        last = (after.get("verifications") or [{}])[-1]
        done.append({"action_id": chk["action_id"], "description": chk.get("description"),
                     "outcome": last.get("outcome"), "status_after": r.get("status")})
    if not done:
        raise HTTPException(409, "the case is not waiting for a field check (re-analyse it, or it is already resolved)")
    return {"checks": done, "status": m.status()}


@router.post("/demo/reanalyse")
def reanalyse(p: Principal = Depends(require_role("engineer"))):
    """Re-run the attribution on the demo's case with all evidence collected so far."""
    from app.api.intel_routes import refresh_investigation
    m = _demo()
    refresh_investigation(_case(m), p)
    return m.status()
