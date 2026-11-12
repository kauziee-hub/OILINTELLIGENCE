"""Scenario definitions and the randomised scenario generator.

A scenario = initial conditions + operating schedule (transfer orders) + fault
injections + instrument/noise configuration + seed. Identical scenario JSON and
seed reproduce the identical physical truth trajectory.

Evaluation environments (section 13.4):
  A - in-distribution: known fault families, training severity ranges
  B - generalisation : new schedules, noise levels, severities, fault combinations
  C - unknown faults : fault mechanisms never seen in training
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from industrial.schemas.plant import PlantConfig
from simulation.faults.faults import KNOWN_FAMILIES, UNSEEN_FAMILIES, FaultSpec

DEFAULT_START = datetime(2026, 10, 30, 0, 0, 0, tzinfo=timezone.utc)


class TransferOrder(BaseModel):
    transfer_id: str
    route: str
    start_s: float
    setpoint_m3h: float
    pumps: list[str] = Field(default_factory=lambda: ["PU-001"])
    target_volume_m3: float | None = None
    max_duration_s: float | None = None


class TankInitial(BaseModel):
    level_m: float
    temperature_c: float
    product: str | None = None


class Scenario(BaseModel):
    scenario_id: str
    family: str = "NORMAL"
    environment: str = "A"
    seed: int = 0
    description: str = ""
    start_time: datetime = DEFAULT_START
    duration_s: float = 8 * 3600.0
    dt_s: float = 1.0
    sample_interval_s: float = 0.0
    gt_interval_s: float = 10.0
    noise_scale: float = 1.0
    ambient_mean_c: float | None = None
    initial: dict[str, TankInitial] = Field(default_factory=dict)
    operations: list[TransferOrder] = Field(default_factory=list)
    faults: list[FaultSpec] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Schedule generation
# ---------------------------------------------------------------------------
ROUTE_WEIGHTS = {"R-01": 0.4, "R-02": 0.35, "R-03": 0.25}


def generate_schedule(plant: PlantConfig, rng: np.random.Generator, duration_s: float,
                      initial: dict[str, TankInitial], env: str = "A",
                      prefix: str = "T") -> list[TransferOrder]:
    """Random but physically feasible transfer schedule."""
    vol = {t: initial[t].level_m * plant.tanks[t].area_m2 for t in plant.tanks}
    t = float(rng.uniform(15, 35)) * 60.0
    orders: list[TransferOrder] = []
    k = 1
    while t < duration_s - 45 * 60:
        routes = list(ROUTE_WEIGHTS)
        w = np.array([ROUTE_WEIGHTS[r] for r in routes])
        feasible = []
        for r in routes:
            src, dst = plant.routes[r].source, plant.routes[r].destination
            src_min = (plant.tanks[src].min_operating_level_m + 1.0) * plant.tanks[src].area_m2
            dst_max = (plant.tanks[dst].max_safe_fill_m - 1.0) * plant.tanks[dst].area_m2
            feasible.append(vol[src] - src_min > 150 and dst_max - vol[dst] > 150)
        w = w * np.array(feasible, dtype=float)
        if w.sum() <= 0:
            break
        r = str(rng.choice(routes, p=w / w.sum()))
        route = plant.routes[r]
        lo, hi = route.setpoint_range_m3h
        if env == "B":  # generalisation: rates near the edges of the operating envelope
            lo, hi = (lo * 0.75, lo * 0.95) if rng.random() < 0.5 else (hi * 0.95, hi * 1.08)
        sp = float(rng.uniform(lo, hi))
        dur = float(rng.uniform(60, 140)) * 60.0
        dur = min(dur, duration_s - t - 20 * 60)
        if dur < 30 * 60:
            break
        src, dst = route.source, route.destination
        src_min = (plant.tanks[src].min_operating_level_m + 1.0) * plant.tanks[src].area_m2
        dst_max = (plant.tanks[dst].max_safe_fill_m - 1.0) * plant.tanks[dst].area_m2
        target = min(sp * dur / 3600.0, vol[src] - src_min, dst_max - vol[dst])
        if target < 100:
            break
        pumps = [str(rng.choice(["PU-001", "PU-002"]))]
        if sp > 140 and rng.random() < 0.5:
            pumps = ["PU-001", "PU-002"]
        orders.append(TransferOrder(
            transfer_id=f"{prefix}-{k:03d}", route=r, start_s=round(t, 1), setpoint_m3h=round(sp, 1),
            pumps=pumps, target_volume_m3=round(target, 1), max_duration_s=round(dur * 1.4, 1),
        ))
        vol[src] -= target
        vol[dst] += target
        t += target / sp * 3600.0 + float(rng.uniform(15, 45)) * 60.0 + 120.0
        k += 1
    return orders


def random_initial(plant: PlantConfig, rng: np.random.Generator) -> dict[str, TankInitial]:
    out = {}
    for tid, tk in plant.tanks.items():
        out[tid] = TankInitial(
            level_m=round(float(rng.uniform(*tk.initial_level_range_m)), 3),
            temperature_c=round(float(rng.uniform(*tk.initial_temperature_range_c)), 2),
            product=tk.initial_product,
        )
    return out


# ---------------------------------------------------------------------------
# Fault generation
# ---------------------------------------------------------------------------
def _sign(rng: np.random.Generator) -> float:
    return 1.0 if rng.random() < 0.5 else -1.0


def make_fault(family: str, plant: PlantConfig, rng: np.random.Generator, start_s: float,
               orders: list[TransferOrder], env: str = "A") -> FaultSpec | None:
    meters = list(plant.meters())
    levels = list(plant.sensors_of_type("level"))
    temps = [s for s in plant.sensors_of_type("temperature") if s != "TT-006"]
    B = env == "B"

    def sev(lo, hi, b_lo=None, b_hi=None):
        if B and b_lo is not None:
            return float(rng.uniform(b_lo, b_hi))
        return float(rng.uniform(lo, hi))

    # target the assets that are actually exercised by the schedule after start_s
    later = [o for o in orders if o.start_s + 600 >= start_s] or orders
    used_routes = [o.route for o in later] or ["R-01"]
    used_meters = sorted({m for r in used_routes for m in plant.routes[r].meters}) or meters
    used_pipes = sorted({p for r in used_routes for p in plant.routes[r].pipelines})
    used_pumps = sorted({p for o in later for p in o.pumps}) or ["PU-001"]

    if family == "METER_BIAS":
        b = _sign(rng) * sev(0.008, 0.03, 0.035, 0.05)
        return FaultSpec(fault_type=family, target=str(rng.choice(used_meters)), start_s=start_s,
                         severity=abs(b) / 0.05, params={"bias_rel": round(b, 4)})
    if family == "METER_DRIFT":
        d = _sign(rng) * sev(0.003, 0.009, 0.010, 0.015)
        return FaultSpec(fault_type=family, target=str(rng.choice(used_meters)), start_s=start_s,
                         severity=abs(d) / 0.015, params={"drift_rel_per_h": round(d, 5)})
    if family == "LEVEL_BIAS":
        o = _sign(rng) * sev(0.02, 0.06, 0.07, 0.10)
        return FaultSpec(fault_type=family, target=str(rng.choice(levels)), start_s=start_s,
                         severity=abs(o) / 0.1, params={"offset_m": round(o, 4)})
    if family == "LEVEL_DRIFT":
        d = _sign(rng) * sev(0.004, 0.012, 0.013, 0.02)
        return FaultSpec(fault_type=family, target=str(rng.choice(levels)), start_s=start_s,
                         severity=abs(d) / 0.02, params={"drift_m_per_h": round(d, 5)})
    if family == "TEMPERATURE_BIAS":
        o = _sign(rng) * sev(1.5, 4.0, 4.5, 6.0)
        return FaultSpec(fault_type=family, target=str(rng.choice(temps)), start_s=start_s,
                         severity=abs(o) / 6.0, params={"offset_c": round(o, 3)})
    if family == "SENSOR_STUCK":
        tgt = str(rng.choice(used_meters + levels + temps))
        return FaultSpec(fault_type=family, target=tgt, start_s=start_s, severity=0.5,
                         end_s=start_s + sev(1800, 5400), params={})
    if family == "SENSOR_DROPOUT":
        tgt = str(rng.choice(used_meters + levels + temps))
        return FaultSpec(fault_type=family, target=tgt, start_s=start_s, severity=0.5,
                         end_s=start_s + sev(900, 3600), params={})
    if family == "PIPELINE_LEAK":
        d = sev(4.0, 10.0, 2.5, 3.5)
        return FaultSpec(fault_type=family, target=str(rng.choice(used_pipes)), start_s=start_s,
                         severity=d / 12.0, params={"position": round(float(rng.uniform(0.15, 0.85)), 3),
                                                    "hole_diameter_mm": round(d, 2),
                                                    "ramp_s": round(float(rng.uniform(0, 900)), 0)})
    if family == "TANK_LEAK":
        d = sev(7.0, 14.0, 5.0, 6.5)
        return FaultSpec(fault_type=family, target=str(rng.choice(list(plant.tanks))), start_s=start_s,
                         severity=d / 15.0, params={"hole_diameter_mm": round(d, 2)})
    if family == "VALVE_PASSING":
        # passing only matters on valves that are shut while a route is active
        candidates = []
        for o in later:
            open_v = set(plant.routes[o.route].open_valves)
            candidates += [v for v in plant.valves if v not in open_v]
        tgt = str(rng.choice(candidates or list(plant.valves)))
        pf = sev(0.0012, 0.005, 0.0006, 0.0011)
        if plant.valves[tgt].role == "isolation":
            pf *= 0.45
        return FaultSpec(fault_type=family, target=tgt, start_s=start_s, severity=pf / 0.005,
                         params={"passing_fraction": round(pf, 6)})
    if family == "PUMP_DEGRADATION":
        h = sev(0.08, 0.2, 0.22, 0.3)
        return FaultSpec(fault_type=family, target=str(rng.choice(used_pumps)), start_s=start_s,
                         severity=h / 0.3, params={"head_loss_frac": round(h, 3),
                                                   "eff_loss_frac": round(h * float(rng.uniform(0.3, 0.7)), 3)})
    if family in ("MISSING_TRANSACTION", "WRONG_QUANTITY"):
        cand = [o for o in orders if o.start_s >= start_s] or orders[-1:]
        if not cand:
            return None
        o = cand[0]
        params = {} if family == "MISSING_TRANSACTION" else {"error_frac": round(_sign(rng) * sev(0.01, 0.05), 4)}
        return FaultSpec(fault_type=family, target=o.transfer_id, start_s=o.start_s, severity=0.5, params=params)
    if family == "DENSITY_BIAS":
        o = _sign(rng) * sev(4.0, 9.0)
        return FaultSpec(fault_type=family, target=str(rng.choice(list(plant.sensors_of_type("density")))),
                         start_s=start_s, severity=abs(o) / 9.0, params={"offset_kgm3": round(o, 3)})
    if family == "SENSOR_NOISE":
        tgt = str(rng.choice(used_meters + levels))
        return FaultSpec(fault_type=family, target=tgt, start_s=start_s, severity=0.6,
                         params={"noise_mult": round(sev(6.0, 15.0), 2)})
    raise ValueError(f"Unknown family {family}")


TRAIN_MULTI_PAIRS = [
    ("METER_BIAS", "PIPELINE_LEAK"), ("LEVEL_DRIFT", "VALVE_PASSING"),
    ("TEMPERATURE_BIAS", "TANK_LEAK"), ("METER_DRIFT", "PUMP_DEGRADATION"),
]
UNSEEN_MULTI_PAIRS = [
    ("PIPELINE_LEAK", "LEVEL_BIAS"), ("VALVE_PASSING", "METER_BIAS"),
    ("TANK_LEAK", "METER_DRIFT"), ("MISSING_TRANSACTION", "PIPELINE_LEAK"),
]


def generate_scenario(plant: PlantConfig, family: str, seed: int, env: str = "A",
                      duration_s: float = 8 * 3600.0, dt_s: float = 2.0,
                      sample_interval_s: float = 10.0) -> Scenario:
    rng = np.random.default_rng(seed)
    initial = random_initial(plant, rng)
    orders = generate_schedule(plant, rng, duration_s, initial, env=env, prefix=f"T{seed % 1000:03d}")
    faults: list[FaultSpec] = []
    noise_scale = 1.0 if env != "B" else float(rng.choice([0.6, 1.6, 2.2]))
    # anchor fault onset to the schedule: after the first transfer (normal baseline) and
    # before / during a later transfer so that the fault is actually exercised
    if len(orders) >= 2:
        k = int(rng.integers(1, len(orders)))
        prev_end = orders[k - 1].start_s + (orders[k - 1].target_volume_m3 or 0) / orders[k - 1].setpoint_m3h * 3600
        o = orders[k]
        o_dur = (o.target_volume_m3 or 0) / o.setpoint_m3h * 3600
        start = float(rng.uniform(min(prev_end + 300, o.start_s), o.start_s + 0.3 * o_dur))
    else:
        start = float(rng.uniform(0.25, 0.5)) * duration_s
    if family == "MULTI_FAULT":
        pairs = UNSEEN_MULTI_PAIRS if env == "B" else TRAIN_MULTI_PAIRS
        a, b = pairs[int(rng.integers(len(pairs)))]
        for fam, st in ((a, start), (b, start + float(rng.uniform(0, 0.15)) * duration_s)):
            f = make_fault(fam, plant, rng, st, orders, env)
            if f:
                faults.append(f)
    elif family != "NORMAL":
        f = make_fault(family, plant, rng, start, orders, env)
        if f:
            faults.append(f)
    return Scenario(
        scenario_id=f"{env}-{family}-{seed}", family=family, environment=env, seed=seed,
        description=f"{family} scenario (env {env}, seed {seed})",
        duration_s=duration_s, dt_s=dt_s, sample_interval_s=sample_interval_s,
        noise_scale=noise_scale, initial=initial, operations=orders, faults=faults,
    )


__all__ = [
    "Scenario", "TransferOrder", "TankInitial", "generate_scenario", "generate_schedule",
    "make_fault", "KNOWN_FAMILIES", "UNSEEN_FAMILIES",
]
