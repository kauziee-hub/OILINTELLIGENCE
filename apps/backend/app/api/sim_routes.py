"""Simulation lab endpoints (simulation mode only; never available against a real plant)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import TruthSession, get_db
from app.core.security import Principal, current_user, require_role
from app.models import orm
from app.services.db_backend import jsonable
from app.services.runtime import get_runtime
from simulation.faults.faults import FAULT_TYPES, KNOWN_FAMILIES, UNSEEN_FAMILIES, FaultSpec
from simulation.scenarios.scenario import Scenario, TransferOrder, generate_scenario

router = APIRouter(prefix="/simulation", tags=["simulation"])


def sim():
    rt = get_runtime()
    if rt.settings.data_mode != "SIMULATION" or rt.sim is None:
        raise HTTPException(409, "simulation endpoints are disabled in PLANT mode")
    return rt.sim


class StartIn(BaseModel):
    family: str | None = Field(default=None, description="generate a scenario of this fault family")
    seed: int = 7
    duration_h: float = Field(default=48.0, gt=0.5, le=240)
    speed: float = Field(default=10.0, ge=0.25, le=120)
    scenario: dict[str, Any] | None = None


class SpeedIn(BaseModel):
    speed: float = Field(ge=0.25, le=120)


class FaultIn(BaseModel):
    fault_type: str
    target: str
    params: dict[str, Any] = Field(default_factory=dict)
    duration_s: float | None = Field(default=None, gt=0)
    severity: float = 0.5


class TransferIn(BaseModel):
    route: str
    setpoint_m3h: float = Field(gt=10, le=400)
    target_volume_m3: float = Field(gt=10, le=20000)
    pumps: list[str] = Field(default_factory=lambda: ["PU-001"])
    start_in_s: float = Field(default=5.0, ge=0)


class CheckIn(BaseModel):
    action_type: str
    target: str | None = None


@router.post("/start")
def start(body: StartIn, _: Principal = Depends(require_role("operator"))):
    s = sim()
    rt = get_runtime()
    sc = None
    if body.scenario:
        sc = Scenario(**body.scenario)
    elif body.family:
        sc = generate_scenario(rt.plant, body.family, body.seed, "A", duration_s=body.duration_h * 3600, dt_s=1.0,
                               sample_interval_s=1.0)
    return s.start(sc, body.speed, not_before=rt.sim_clock_floor())


@router.post("/scenarios")
def configure_scenario(body: StartIn, _: Principal = Depends(require_role("operator"))):
    return start(body, _)


@router.post("/pause")
def pause(_: Principal = Depends(require_role("operator"))):
    return sim().pause()


@router.post("/resume")
def resume(_: Principal = Depends(require_role("operator"))):
    return sim().resume()


@router.post("/stop")
def stop(_: Principal = Depends(require_role("operator"))):
    return sim().stop()


@router.put("/speed")
def speed(body: SpeedIn, _: Principal = Depends(require_role("operator"))):
    return sim().set_speed(body.speed)


@router.get("/status")
def status(_: Principal = Depends(current_user)):
    return sim().status()


@router.get("/runs")
def runs(db: Session = Depends(get_db), _: Principal = Depends(current_user)):
    rows = db.execute(select(orm.SimulationRun).order_by(orm.SimulationRun.started_at.desc()).limit(50)).scalars()
    return [jsonable({"run_id": r.run_id, "scenario_id": r.scenario_id, "seed": r.seed, "status": r.status,
                      "started_at": r.started_at, "ended_at": r.ended_at, "speed": r.speed}) for r in rows]


@router.get("/catalog")
def catalog(_: Principal = Depends(current_user)):
    P = get_runtime().plant
    targets = {
        "METER_BIAS": list(P.meters()), "METER_DRIFT": list(P.meters()),
        "LEVEL_BIAS": list(P.sensors_of_type("level")), "LEVEL_DRIFT": list(P.sensors_of_type("level")),
        "TEMPERATURE_BIAS": [s for s in P.sensors_of_type("temperature")],
        "DENSITY_BIAS": list(P.sensors_of_type("density")),
        "SENSOR_STUCK": [s for s, c in P.sensors.items() if c.type in ("flow", "level", "temperature", "pressure")],
        "SENSOR_DROPOUT": [s for s, c in P.sensors.items() if c.type in ("flow", "level", "temperature", "pressure")],
        "SENSOR_NOISE": list(P.meters()) + list(P.sensors_of_type("level")),
        "PIPELINE_LEAK": list(P.pipelines), "TANK_LEAK": list(P.tanks), "VALVE_PASSING": list(P.valves),
        "PUMP_DEGRADATION": list(P.pumps), "MISSING_TRANSACTION": list(P.routes), "WRONG_QUANTITY": list(P.routes),
    }
    defaults = {
        "METER_BIAS": {"bias_rel": 0.02}, "METER_DRIFT": {"drift_rel_per_h": 0.006}, "LEVEL_BIAS": {"offset_m": 0.04},
        "LEVEL_DRIFT": {"drift_m_per_h": 0.008}, "TEMPERATURE_BIAS": {"offset_c": 2.5}, "DENSITY_BIAS": {"offset_kgm3": 5},
        "SENSOR_NOISE": {"noise_mult": 8}, "PIPELINE_LEAK": {"position": 0.5, "hole_diameter_mm": 7, "ramp_s": 300},
        "TANK_LEAK": {"hole_diameter_mm": 10}, "VALVE_PASSING": {"passing_fraction": 0.003},
        "PUMP_DEGRADATION": {"head_loss_frac": 0.15, "eff_loss_frac": 0.08}, "WRONG_QUANTITY": {"error_frac": 0.03},
    }
    return {"fault_types": {k: {"category": v, "targets": targets.get(k, []), "default_params": defaults.get(k, {})}
                            for k, v in FAULT_TYPES.items()},
            "known_families": KNOWN_FAMILIES, "unseen_families": UNSEEN_FAMILIES,
            "routes": {k: {"name": r.name, "setpoint_range_m3h": r.setpoint_range_m3h} for k, r in P.routes.items()},
            "pumps": list(P.pumps)}


@router.post("/faults")
def inject_fault(body: FaultIn, _: Principal = Depends(require_role("operator"))):
    s = sim()
    rt = get_runtime()
    if body.fault_type not in FAULT_TYPES:
        raise HTTPException(422, f"unknown fault type {body.fault_type}")
    valid = rt.plant.all_asset_ids() | set(rt.plant.sensors) | set(rt.plant.routes)
    if body.target not in valid:
        raise HTTPException(422, f"unknown target {body.target}")
    st = s.status()
    now = st.get("sim_time_s", 0.0)
    target = body.target
    if body.fault_type in ("MISSING_TRANSACTION", "WRONG_QUANTITY"):
        # applies to the next transfer started on this route
        target = body.target
    f = FaultSpec(fault_type=body.fault_type, target=target, start_s=now, severity=body.severity,
                  end_s=(now + body.duration_s) if body.duration_s else None, params=body.params)
    return s.inject_fault(f)


@router.delete("/faults/{fault_id}")
def clear_fault(fault_id: str, _: Principal = Depends(require_role("operator"))):
    return sim().clear_fault(fault_id)


@router.post("/transfers")
def add_transfer(body: TransferIn, _: Principal = Depends(require_role("operator"))):
    s = sim()
    rt = get_runtime()
    if body.route not in rt.plant.routes:
        raise HTTPException(422, "unknown route")
    bad = [p for p in body.pumps if p not in rt.plant.pumps]
    if bad:
        raise HTTPException(422, f"unknown pumps {bad}")
    now = s.status().get("sim_time_s", 0.0)
    import uuid
    o = TransferOrder(transfer_id=f"M-{uuid.uuid4().hex[:5].upper()}", route=body.route, start_s=now + body.start_in_s,
                      setpoint_m3h=body.setpoint_m3h, pumps=body.pumps, target_volume_m3=body.target_volume_m3,
                      max_duration_s=body.target_volume_m3 / body.setpoint_m3h * 3600 * 1.5)
    return s.add_transfer(o)


@router.post("/verification")
def verification(body: CheckIn, _: Principal = Depends(require_role("engineer"))):
    """Perform a field verification check against simulator ground truth (simulation oracle)."""
    try:
        return sim().perform_check(body.action_type, body.target)
    except KeyError:
        raise HTTPException(422, "unknown verification action / target")


@router.get("/truth/latest")
def truth_latest(_: Principal = Depends(require_role("engineer"))):
    """SIMULATED GROUND TRUTH - for simulation-lab inspection and evaluation only."""
    s = sim()
    snap = s.truth_snapshot()
    if snap is None:
        with TruthSession() as ts:
            r = ts.execute(select(orm.TruthSnapshot).order_by(orm.TruthSnapshot.id.desc()).limit(1)).scalar()
            snap = r.row if r else None
    return {"label": "SIMULATED GROUND TRUTH (not available to diagnostic models)", "truth": jsonable(snap)}


@router.get("/truth/faults")
def truth_faults(all_runs: bool = False, _: Principal = Depends(require_role("engineer"))):
    """Injected faults of the current run (all_runs=true: the last 100 of any run)."""
    run_id = (sim().status() or {}).get("run_id")
    q = select(orm.FaultEvent)
    if run_id and not all_runs:
        q = q.where(orm.FaultEvent.run_id == run_id)
    with TruthSession() as ts:
        rows = ts.execute(q.order_by(orm.FaultEvent.start_time.desc()).limit(100)).scalars().all()
    return [jsonable({c.name: getattr(r, c.name) for c in orm.FaultEvent.__table__.columns}) for r in rows]
