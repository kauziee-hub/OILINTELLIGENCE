"""Digital-twin state built exclusively from the latest measured telemetry.

The twin never shows simulator truth: tank inventories are *estimated* from
measured level/temperature and the latest lab density; flows on unmetered
pipes are marked as inferred.
"""
from __future__ import annotations

import math
from typing import Any

from industrial.schemas.plant import PlantConfig

STALE_S = 120.0


def _v(latest, sid, par=None, plant=None):
    if plant is not None and par is None:
        par = plant.sensors[sid].parameter
    r = latest.get((sid, par))
    return (r.get("value"), r.get("quality", "GOOD"), r.get("ts")) if r else (None, "BAD", None)


def build_state(plant: PlantConfig, latest: dict, now: float | None, lab_density: dict[str, float],
                alerts: list[dict[str, Any]], sim_status: dict[str, Any] | None, balance: dict[str, Any] | None,
                mode: str) -> dict[str, Any]:
    def val(sid, par=None):
        v, q, ts = _v(latest, sid, par, plant)
        stale = ts is None or (now is not None and now - ts > max(STALE_S, 6 * plant.sensors[sid].publish_interval_s))
        return v, ("STALE" if stale and v is not None else q), ts

    tanks = {}
    for tid, t in plant.tanks.items():
        lvl, ql, _ = val(plant.tank_level_sensor(tid))
        temp, qt, _ = val(plant.tank_temperature_sensor(tid))
        rho15 = lab_density.get(tid) or plant.products[t.initial_product].density_15c
        alpha = plant.products[t.initial_product].thermal_expansion_coeff
        inv = None
        if lvl is not None and temp is not None:
            inv = t.area_m2 * lvl / (1 + alpha * (temp - 15.0)) * rho15 / 1000.0
        alarm = None
        if lvl is not None and lvl >= t.max_safe_fill_m - 0.2:
            alarm = "HIGH_LEVEL"
        elif lvl is not None and lvl <= t.min_operating_level_m + 0.2:
            alarm = "LOW_LEVEL"
        tanks[tid] = {"name": t.name, "level_m": lvl, "temperature_c": temp, "inventory_t_estimated": inv,
                      "fill_fraction": (lvl / t.shell_height_m) if lvl is not None else None,
                      "capacity_m3": t.nominal_capacity_m3, "diameter_m": t.diameter_m, "height_m": t.shell_height_m,
                      "position": t.position, "quality": "BAD" if "BAD" in (ql, qt) else (
                          "STALE" if "STALE" in (ql, qt) else ql), "alarm": alarm, "density_15c_lab": rho15}
    meter_on = {s.asset: sid for sid, s in plant.meters().items()}
    pt_on = {s.asset: sid for sid, s in plant.sensors.items() if s.type == "pressure" and s.asset in plant.pipelines}
    xv4, _, _ = val("XV-004-ZT")
    running = any((val(f"{p}-ST")[0] or 0) >= 0.5 for p in plant.pumps)
    pipes = {}
    for pid, p in plant.pipelines.items():
        flow, qf, inferred = None, "GOOD", False
        if pid in meter_on:
            flow, qf, _ = val(meter_on[pid])
        elif pid == "PL-004":
            f3, qf, _ = val("FM-003")
            flow = f3 if (xv4 or 0) > 5 and running else 0.0
            inferred = True
        pres, qp, _ = val(pt_on[pid]) if pid in pt_on else (None, "BAD", None)
        pipes[pid] = {"name": p.name, "from": p.from_, "to": p.to, "flow_m3h": flow, "flow_inferred": inferred,
                      "flowing": bool(flow and flow > 1.0), "pressure_bar": pres, "path": p.path,
                      "diameter_mm": p.nominal_diameter_mm, "quality": qf if qp == "GOOD" else qp,
                      "meter": meter_on.get(pid), "pressure_sensor": pt_on.get(pid)}
    pumps = {}
    for pid, p in plant.pumps.items():
        st, qs, _ = val(f"{pid}-ST")
        status = {1.0: "RUNNING", 0.5: "STARTING", 0.25: "STOPPING", 0.0: "STOPPED"}.get(st, "UNKNOWN") \
            if st is not None else "UNKNOWN"
        pumps[pid] = {"name": p.name, "status": status, "speed_rpm": val(f"{pid}-SP")[0],
                      "power_kw": val(f"{pid}-PW")[0], "discharge_bar": val(f"{pid}-PD")[0],
                      "position": p.position, "quality": qs}
    valves = {}
    for vid, v in plant.valves.items():
        pos, qv, _ = val(f"{vid}-ZT")
        cmd, _, _ = val(f"{vid}-ZC")
        pipe = plant.pipelines[v.pipeline]
        pt = pipe.path[0] if v.position_on_line < 0.5 else pipe.path[-1]
        valves[vid] = {"name": v.name, "position_pct": pos, "command": "OPEN" if (cmd or 0) >= 0.5 else "CLOSED",
                       "role": v.role, "pipeline": v.pipeline, "position": pt, "quality": qv}
    sensors = {}
    for sid, s in plant.sensors.items():
        v, q, ts = val(sid)
        sensors[sid] = {"asset_id": s.asset, "type": s.type, "parameter": s.parameter, "value": v, "unit": s.unit,
                        "quality": q, "ts": ts}
    for t, d in tanks.items():
        if d["alarm"]:
            alerts = alerts + [{"asset_id": t, "severity": "WARNING", "message": d["alarm"].replace("_", " ").title()}]
    return {"mode": mode, "timestamp": now, "simulation": sim_status, "tanks": tanks, "pipelines": pipes,
            "pumps": pumps, "valves": valves, "sensors": sensors, "alerts": alerts, "balance": balance,
            "junctions": {j: {"position": c.position, "name": c.name} for j, c in plant.junctions.items()}}
