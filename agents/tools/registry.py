"""Agent tool registry (section 8.3).

Every tool has: a Pydantic input schema (validated before execution), an
allowed-roles list, a timeout, and an execution log entry (tool, arguments,
status, duration, result summary) written to the audit trail. There are no
tools that write to the process: the agent can read data, run deterministic
calculations and models, and manage investigation records - nothing else.
"""
from __future__ import annotations

import math
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from agents.tools.backend import InvestigationBackend
from intelligence.fault_attribution.hypotheses import build_hypotheses
from intelligence.physics.measurement import to_epoch

MAX_RANGE_S = 7 * 24 * 3600


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ToolContext:
    backend: InvestigationBackend
    actor: str = "agent"
    role: str = "agent"
    investigation_id: str | None = None
    cache: dict[str, Any] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------- schemas
class _Range(BaseModel):
    start: float = Field(description="epoch seconds")
    end: float = Field(description="epoch seconds")

    @model_validator(mode="after")
    def _check(self):
        if not (math.isfinite(self.start) and math.isfinite(self.end)) or self.end <= self.start:
            raise ValueError("end must be after start")
        if self.end - self.start > MAX_RANGE_S:
            raise ValueError("range exceeds 7 days")
        return self


class SensorHistoryIn(_Range):
    sensor_ids: list[str] = Field(min_length=1, max_length=40)
    max_points: int = Field(default=120, ge=10, le=2000)


class AssetIn(BaseModel):
    asset_id: str


class SensorIn(BaseModel):
    sensor_id: str


class BalanceIn(_Range):
    window_s: float = Field(default=1800.0, ge=600.0, le=7200.0)


class AttributionIn(BalanceIn):
    exclude_families: list[str] = Field(default_factory=list)


class TestsIn(BaseModel):
    hypotheses: list[str]
    probabilities: list[float]
    transfer_active: bool = False
    performed: list[str] = Field(default_factory=list)
    top: int = Field(default=6, ge=1, le=20)

    @model_validator(mode="after")
    def _check(self):
        if len(self.hypotheses) != len(self.probabilities) or not self.hypotheses:
            raise ValueError("hypotheses and probabilities must be non-empty and the same length")
        if any(p < 0 for p in self.probabilities) or sum(self.probabilities) <= 0:
            raise ValueError("probabilities must be non-negative and not all zero")
        return self


class CreateInvestigationIn(BaseModel):
    event_id: str
    event_type: str
    assets: list[str] = Field(default_factory=list)
    t0: float
    t1: float
    severity: str = "WARNING"
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class FeedbackIn(BaseModel):
    investigation_id: str
    decision: str = Field(pattern="^(CONFIRM|REJECT|REQUEST_MORE_EVIDENCE|VERIFICATION_RESULT)$")
    verified_cause: str | None = None
    action_id: str | None = None
    outcome: str | None = Field(default=None, pattern="^(POSITIVE|NEGATIVE)$")
    comments: str = ""
    reviewer: str = ""

    @field_validator("comments")
    @classmethod
    def _len(cls, v):
        if len(v) > 4000:
            raise ValueError("comment too long")
        return v


# ---------------------------------------------------------------- helpers
def _analysis_ctx(ctx: ToolContext, start: float, end: float):
    key = ("analysis", round(start), round(end))
    if key not in ctx.cache:
        b = ctx.backend
        pad = 900.0
        rec = b.records(start - 30 * 86400, end)
        # transfers completing inside the period are reconciled against their tickets: fetch meter data
        # back to each such transfer's start
        tel = b.telemetry(b.engine.telemetry_start(rec, start, end, pad), end + b.engine.READ_AHEAD_S)
        if not len(tel):
            raise ToolError("NO_DATA", "no telemetry available for the requested period")
        ctx.cache[key] = b.engine.context(tel, rec)
    return ctx.cache[key]


def _evidence(ctx: ToolContext, start: float, end: float, window_s: float):
    key = ("evidence", round(start), round(end), window_s)
    if key not in ctx.cache:
        a = _analysis_ctx(ctx, start, end)
        ev = ctx.backend.engine.evidence(a, start, end, window_s)
        if ev is None:
            raise ToolError("INSUFFICIENT_PERIOD", "period shorter than one balance window")
        ctx.cache[key] = ev
    return ctx.cache[key]


def _check_ids(plant, ids: list[str], kind: str) -> None:
    valid = set(plant.sensors) if kind == "sensor" else (plant.all_asset_ids() | set(plant.sensors))
    bad = [i for i in ids if i not in valid]
    if bad:
        raise ToolError("INVALID_ID", f"unknown {kind} id(s): {', '.join(bad)}")


# ---------------------------------------------------------------- tools
def get_sensor_history(ctx: ToolContext, a: SensorHistoryIn) -> dict[str, Any]:
    P = ctx.backend.plant
    _check_ids(P, a.sensor_ids, "sensor")
    tel = ctx.backend.telemetry(a.start, a.end, a.sensor_ids)
    out = {}
    for sid in a.sensor_ids:
        par = P.sensors[sid].parameter
        d = tel[(tel.sensor_id == sid) & (tel.parameter == par)]
        v = pd.to_numeric(d["value"], errors="coerce")
        if not len(d):
            out[sid] = {"samples": 0, "unit": P.sensors[sid].unit, "note": "no data in period"}
            continue
        step = max(len(d) // a.max_points, 1)
        ts = d["ts"].to_numpy() if "ts" in d else pd.to_datetime(d["timestamp"]).astype("int64").to_numpy() / 1e9
        out[sid] = {"samples": int(len(d)), "unit": P.sensors[sid].unit, "parameter": par,
                    "min": float(v.min()), "max": float(v.max()), "mean": float(v.mean()), "last": float(v.iloc[-1]),
                    "bad_quality_fraction": float((d["quality"] == "BAD").mean()),
                    "series": [[float(t), float(x)] for t, x in zip(ts[::step], v.to_numpy()[::step])]}
    return {"period": [a.start, a.end], "sensors": out}


def get_asset_details(ctx: ToolContext, a: AssetIn) -> dict[str, Any]:
    d = ctx.backend.asset(a.asset_id)
    if d is None:
        raise ToolError("INVALID_ID", f"unknown asset id: {a.asset_id}")
    return d


def get_transfer_history(ctx: ToolContext, a: _Range) -> dict[str, Any]:
    tr = ctx.backend.records(a.start, a.end).get("transfers", pd.DataFrame())
    rows = []
    if len(tr):
        for _, r in tr.iterrows():
            rows.append({k: (None if (isinstance(v, float) and math.isnan(v)) else v) for k, v in r.items()
                         if k in ("transfer_id", "route", "source_tank", "destination_tank", "start_time", "end_time",
                                  "recorded_quantity", "quantity_unit", "custody_meter", "status")})
    return {"transfers": rows, "count": len(rows)}


def get_meter_calibration(ctx: ToolContext, a: SensorIn) -> dict[str, Any]:
    _check_ids(ctx.backend.plant, [a.sensor_id], "sensor")
    cal = ctx.backend.records(0, ctx.backend.now()).get("calibrations", pd.DataFrame())
    if not len(cal):
        return {"sensor_id": a.sensor_id, "records": [], "note": "no calibration records available"}
    d = cal[cal["sensor_id"] == a.sensor_id].sort_values("timestamp", ascending=False)
    return {"sensor_id": a.sensor_id, "records": d.head(5).to_dict("records")}


def get_maintenance_records(ctx: ToolContext, a: AssetIn) -> dict[str, Any]:
    _check_ids(ctx.backend.plant, [a.asset_id], "asset")
    m = ctx.backend.records(0, ctx.backend.now()).get("maintenance", pd.DataFrame())
    if not len(m):
        return {"asset_id": a.asset_id, "records": []}
    return {"asset_id": a.asset_id, "records": m[m["asset_id"] == a.asset_id].to_dict("records")}


def get_operator_log(ctx: ToolContext, a: _Range) -> dict[str, Any]:
    lg = ctx.backend.records(a.start, a.end).get("operator_log", pd.DataFrame())
    if not len(lg):
        return {"entries": []}
    from intelligence.physics.measurement import to_epoch
    ts = to_epoch(lg["timestamp"])
    d = lg[(ts >= a.start) & (ts <= a.end)]
    return {"entries": d[["timestamp", "asset_id", "category", "text"]].to_dict("records")}


def calculate_mass_balance(ctx: ToolContext, a: BalanceIn) -> dict[str, Any]:
    ev = _evidence(ctx, a.start, a.end, a.window_s)
    from intelligence.uncertainty.propagation import assemble, window_statistics
    A = assemble(ev)
    c2, n, p = A.chi2()
    cv = np.array([r.kind == "CV" for r in A.rows], dtype=float)
    fac = float(A.r @ cv)
    fac_sigma = float(math.sqrt(max(cv @ A.cov @ cv, 0.0)))
    per_window = [{"window": s["window"], "t0": s["t0"], "t1": s["t1"], "chi2": s["chi2"], "dof": s["dof"],
                   "facility_residual_t": s["facility_residual_t"], "max_abs_z": s["max_abs_z"],
                   "route": ev.windows[s["window"]].route_guess} for s in window_statistics(ev)]
    rows = ctx.backend.engine.balance_table(ev)
    rows.sort(key=lambda r: -abs(r["z"]))
    quality = {}
    for w in ev.windows:
        for sid, q in w.quality.items():
            if q["stuck"] or q["dropout"]:
                quality.setdefault(sid, []).append({"window": w.index, **q})
    return {"period": [a.start, a.end], "windows": len(ev.windows), "facility_residual_t": round(fac, 3),
            "facility_residual_sigma_t": round(fac_sigma, 3),
            "facility_z": round(fac / fac_sigma, 2) if fac_sigma > 0 else None,
            "chi2": round(c2, 2), "dof": n, "p_value": p, "per_window": per_window,
            "rows_most_abnormal": rows[:12], "data_quality_issues": quality,
            "sign_convention": "R = metered in - metered out - inventory change; R > 0 is an apparent loss"}


def calculate_uncertainty(ctx: ToolContext, a: BalanceIn) -> dict[str, Any]:
    ev = _evidence(ctx, a.start, a.end, a.window_s)
    from intelligence.uncertainty.propagation import assemble
    A = assemble(ev)
    out = []
    for i, r in enumerate(A.rows):
        if r.kind != "CV":
            continue
        out.append({"row": r.row_id, "sigma": round(float(A.sigma_r[i]), 4), "unit": r.unit,
                    "contributions": [{"source": k, "variance_share": round(v, 3)} for k, v in A.contributions(i)]})
    return {"method": "first-order propagation sigma_R^2 = J Sigma J^T with shared systematic terms",
            "rows": out}


def run_fault_attribution(ctx: ToolContext, a: AttributionIn) -> dict[str, Any]:
    ev = _evidence(ctx, a.start, a.end, a.window_s)
    eng = ctx.backend.engine
    res = eng.attribute(ev, set(a.exclude_families) or None)
    ctx.cache["last_attribution"] = res
    d = res.to_dict(top=8)
    d["explanation"] = eng.explain(res)
    d["model"] = {"name": "physics_bayes", "version": eng.model_versions.get("physics_bayes", "uncalibrated"),
                  "calibration_temperature": eng.temperature}
    d["hypothesis_distribution"] = res.hypothesis_distribution()
    return d


def evaluate_diagnostic_tests(ctx: ToolContext, a: TestsIn) -> dict[str, Any]:
    P = ctx.backend.plant
    allh = {h.hid: h for h in build_hypotheses(P)}
    bad = [h for h in a.hypotheses if h not in allh]
    if bad:
        raise ToolError("INVALID_ID", f"unknown hypothesis id(s): {', '.join(bad)}")
    hyps = [allh[h] for h in a.hypotheses]
    p = np.array(a.probabilities, dtype=float)
    ranked = ctx.backend.engine.active.rank(hyps, p / p.sum(), a.transfer_active, set(a.performed), a.top)
    from intelligence.active_diagnosis.information_gain import entropy
    return {"current_entropy_bits": round(entropy(p / p.sum()), 4), "ranked_actions": [r.to_dict() for r in ranked]}


def create_investigation(ctx: ToolContext, a: CreateInvestigationIn) -> dict[str, Any]:
    b = ctx.backend
    _check_ids(b.plant, a.assets, "asset")
    existing = b.find_open_investigation(a.assets, a.t0, a.t1)
    if existing:
        return {"investigation_id": existing, "created": False,
                "note": "open investigation already covers these assets and period; event attached"}
    iid = b.create_investigation(a.model_dump())
    return {"investigation_id": iid, "created": True}


def submit_engineer_feedback(ctx: ToolContext, a: FeedbackIn) -> dict[str, Any]:
    st = ctx.backend.load_investigation(a.investigation_id)
    if st is None:
        raise ToolError("INVALID_ID", f"unknown investigation {a.investigation_id}")
    reviews = list(st.get("reviews", []))
    reviews.append(a.model_dump() | {"role": ctx.role, "actor": ctx.actor, "timestamp": time.time()})
    st["reviews"] = reviews
    ctx.backend.save_investigation(a.investigation_id, st)
    return {"investigation_id": a.investigation_id, "reviews": len(reviews)}


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: type[BaseModel]
    fn: Callable[[ToolContext, Any], dict[str, Any]]
    roles: tuple[str, ...]
    timeout_s: float = 30.0


READ_ROLES = ("agent", "engineer", "admin", "operator", "viewer")
TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("get_sensor_history", "Retrieve historical sensor readings", SensorHistoryIn, get_sensor_history, READ_ROLES),
    ToolSpec("get_asset_details", "Retrieve equipment configuration", AssetIn, get_asset_details, READ_ROLES),
    ToolSpec("get_transfer_history", "Retrieve transfer events", _Range, get_transfer_history, READ_ROLES),
    ToolSpec("get_meter_calibration", "Retrieve calibration / proving records", SensorIn, get_meter_calibration, READ_ROLES),
    ToolSpec("get_maintenance_records", "Retrieve maintenance history of an asset", AssetIn, get_maintenance_records, READ_ROLES),
    ToolSpec("get_operator_log", "Retrieve operator log entries", _Range, get_operator_log, READ_ROLES),
    ToolSpec("calculate_mass_balance", "Execute deterministic topology-aware balance calculation", BalanceIn,
             calculate_mass_balance, READ_ROLES, 60.0),
    ToolSpec("calculate_uncertainty", "Estimate measurement uncertainty of balances", BalanceIn,
             calculate_uncertainty, READ_ROLES, 60.0),
    ToolSpec("run_fault_attribution", "Query the physics-guided probabilistic attribution model", AttributionIn,
             run_fault_attribution, READ_ROLES, 90.0),
    ToolSpec("evaluate_diagnostic_tests", "Rank verification options by expected information gain", TestsIn,
             evaluate_diagnostic_tests, READ_ROLES),
    ToolSpec("create_investigation", "Create (or attach to) an investigation record", CreateInvestigationIn,
             create_investigation, ("agent", "engineer", "admin")),
    ToolSpec("submit_engineer_feedback", "Record a reviewed finding (engineers only)", FeedbackIn,
             submit_engineer_feedback, ("engineer", "admin")),
]}

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-tool")


def _summarise(result: dict[str, Any]) -> str:
    keys = list(result)[:6]
    return ", ".join(keys)


def call_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate, authorise, execute with timeout and log a tool call. Never raises."""
    t0 = time.time()
    entry: dict[str, Any] = {"tool": name, "arguments": args, "actor": ctx.actor, "role": ctx.role,
                             "investigation_id": ctx.investigation_id, "started": t0}
    spec = TOOLS.get(name)
    try:
        if spec is None:
            raise ToolError("UNKNOWN_TOOL", f"tool {name} is not registered")
        if ctx.role not in spec.roles:
            raise ToolError("FORBIDDEN", f"role '{ctx.role}' may not call {name}")
        try:
            parsed = spec.schema.model_validate(args)
        except ValidationError as e:
            raise ToolError("INVALID_INPUT", e.errors(include_url=False).__repr__()[:500])
        fut = _pool.submit(spec.fn, ctx, parsed)
        try:
            result = fut.result(timeout=spec.timeout_s)
        except FutTimeout:
            raise ToolError("TIMEOUT", f"{name} exceeded {spec.timeout_s}s")
        entry.update(status="OK", duration_ms=round((time.time() - t0) * 1000, 1), result_summary=_summarise(result))
        out = {"ok": True, "result": result}
    except ToolError as e:
        entry.update(status="ERROR", error_code=e.code, error=e.message, duration_ms=round((time.time() - t0) * 1000, 1))
        out = {"ok": False, "error": {"code": e.code, "message": e.message}}
    except Exception as e:  # tool bug / data failure -> reported, not hidden
        entry.update(status="ERROR", error_code="TOOL_FAILURE", error=repr(e)[:400],
                     trace=traceback.format_exc()[-1500:], duration_ms=round((time.time() - t0) * 1000, 1))
        out = {"ok": False, "error": {"code": "TOOL_FAILURE", "message": repr(e)[:400]}}
    ctx.log.append(entry)
    try:
        ctx.backend.audit(ctx.actor, f"tool:{name}", {k: v for k, v in entry.items() if k != "trace"})
    except Exception:
        pass
    return out
