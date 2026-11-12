"""Fault injection engine (section 5.4).

Faults are defined declaratively (FaultSpec) and applied every simulation
step to the physical models (leaks, valve seats, pumps), to instruments
(bias, drift, stuck, dropout, noise) or to the operational records layer
(missing / wrong transactions). The physical process and the instruments are
affected independently: a meter bias changes only what is reported, never
what is physically transferred.
"""
from __future__ import annotations

import math
import uuid
from typing import Any

from pydantic import BaseModel, Field

# Fault families. `KNOWN` families are used for model training (environments A/B);
# `UNSEEN` families are reserved for unknown-fault testing (environment C).
FAULT_TYPES = {
    "METER_BIAS": "sensor",
    "METER_DRIFT": "sensor",
    "LEVEL_BIAS": "sensor",
    "LEVEL_DRIFT": "sensor",
    "TEMPERATURE_BIAS": "sensor",
    "DENSITY_BIAS": "sensor",
    "SENSOR_STUCK": "sensor",
    "SENSOR_DROPOUT": "sensor",
    "SENSOR_NOISE": "sensor",
    "PIPELINE_LEAK": "physical",
    "TANK_LEAK": "physical",
    "VALVE_PASSING": "physical",
    "PUMP_DEGRADATION": "physical",
    "MISSING_TRANSACTION": "accounting",
    "WRONG_QUANTITY": "accounting",
}
KNOWN_FAMILIES = [
    "METER_BIAS", "METER_DRIFT", "LEVEL_BIAS", "LEVEL_DRIFT", "TEMPERATURE_BIAS",
    "SENSOR_STUCK", "SENSOR_DROPOUT", "PIPELINE_LEAK", "TANK_LEAK", "VALVE_PASSING",
    "PUMP_DEGRADATION", "MISSING_TRANSACTION",
]
UNSEEN_FAMILIES = ["DENSITY_BIAS", "SENSOR_NOISE", "WRONG_QUANTITY"]


class FaultSpec(BaseModel):
    fault_id: str = Field(default_factory=lambda: f"F-{uuid.uuid4().hex[:8]}")
    fault_type: str
    target: str
    start_s: float = 0.0
    end_s: float | None = None
    severity: float = 0.5
    params: dict[str, Any] = Field(default_factory=dict)

    def active(self, t: float) -> bool:
        return t >= self.start_s and (self.end_s is None or t < self.end_s)

    @property
    def category(self) -> str:
        return FAULT_TYPES[self.fault_type]


def hole_area(diameter_mm: float) -> float:
    return math.pi * (diameter_mm / 1000.0 / 2.0) ** 2


class FaultInjector:
    def __init__(self, faults: list[FaultSpec] | None = None):
        self.faults: list[FaultSpec] = list(faults or [])
        for f in self.faults:
            if f.fault_type not in FAULT_TYPES:
                raise ValueError(f"Unknown fault type {f.fault_type}")

    def add(self, fault: FaultSpec) -> None:
        if fault.fault_type not in FAULT_TYPES:
            raise ValueError(f"Unknown fault type {fault.fault_type}")
        self.faults.append(fault)

    def clear(self, fault_id: str, t: float) -> None:
        for f in self.faults:
            if f.fault_id == fault_id and (f.end_s is None or f.end_s > t):
                f.end_s = t

    def active(self, t: float) -> list[FaultSpec]:
        return [f for f in self.faults if f.active(t)]

    # ----------------------------------------------------------------- apply
    def apply(self, t: float, sensors: dict, valves: dict, pumps: dict) -> tuple[dict, dict]:
        """Apply faults for time t.

        Returns (pipeline_leaks, tank_leak_areas):
          pipeline_leaks: pipe_id -> (position, hole_area_m2)
          tank_leak_areas: tank_id -> hole_area_m2
        """
        for s in sensors.values():
            s.fault.reset()
        for v in valves.values():
            v.passing_fraction = 0.0
        for p in pumps.values():
            p.head_factor = 1.0
            p.eff_factor = 1.0
        pipe_leaks: dict[str, tuple[float, float]] = {}
        tank_leaks: dict[str, float] = {}

        for f in self.active(t):
            P = f.params
            ft = f.fault_type
            if ft in ("METER_BIAS", "METER_DRIFT", "LEVEL_BIAS", "LEVEL_DRIFT", "TEMPERATURE_BIAS",
                      "DENSITY_BIAS", "SENSOR_STUCK", "SENSOR_DROPOUT", "SENSOR_NOISE"):
                s = sensors.get(f.target)
                if s is None:
                    continue
                st = s.fault
                st.active_fault_ids.append(f.fault_id)
                if ft == "METER_BIAS":
                    st.bias_rel += float(P.get("bias_rel", 0.02))
                    st.bias_abs += float(P.get("bias_abs", 0.0))
                elif ft == "METER_DRIFT":
                    st.drift_rel_per_h += float(P.get("drift_rel_per_h", 0.005))
                    st.drift_start_s = f.start_s
                elif ft == "LEVEL_BIAS":
                    st.bias_abs += float(P.get("offset_m", 0.03))
                elif ft == "LEVEL_DRIFT":
                    st.drift_abs_per_h += float(P.get("drift_m_per_h", 0.004))
                    st.drift_start_s = f.start_s
                elif ft == "TEMPERATURE_BIAS":
                    st.bias_abs += float(P.get("offset_c", 1.5))
                elif ft == "DENSITY_BIAS":
                    st.bias_abs += float(P.get("offset_kgm3", 4.0))
                elif ft == "SENSOR_STUCK":
                    st.stuck = True
                elif ft == "SENSOR_DROPOUT":
                    st.dropout = True
                elif ft == "SENSOR_NOISE":
                    st.noise_mult *= float(P.get("noise_mult", 8.0))
            elif ft == "PIPELINE_LEAK":
                ramp = float(P.get("ramp_s", 0.0))
                grow = 1.0 if ramp <= 0 else min(1.0, (t - f.start_s) / ramp)
                area = hole_area(float(P.get("hole_diameter_mm", 6.0))) * grow
                pipe_leaks[f.target] = (float(P.get("position", 0.5)), area)
            elif ft == "TANK_LEAK":
                tank_leaks[f.target] = tank_leaks.get(f.target, 0.0) + hole_area(
                    float(P.get("hole_diameter_mm", 4.0)))
            elif ft == "VALVE_PASSING":
                v = valves.get(f.target)
                if v is not None:
                    v.passing_fraction = max(v.passing_fraction, float(P.get("passing_fraction", 0.002)))
            elif ft == "PUMP_DEGRADATION":
                p = pumps.get(f.target)
                if p is not None:
                    p.head_factor *= 1.0 - float(P.get("head_loss_frac", 0.12))
                    p.eff_factor *= 1.0 - float(P.get("eff_loss_frac", 0.08))
        return pipe_leaks, tank_leaks

    def record_faults(self, t: float) -> list[FaultSpec]:
        return [f for f in self.active(t) if f.category == "accounting"]

    def transaction_fault_for(self, transfer_id: str, route: str, t_start: float) -> FaultSpec | None:
        """Accounting fault that applies to a transfer starting at t_start."""
        for f in self.faults:
            if f.category != "accounting":
                continue
            if f.target in (transfer_id, route) and f.active(t_start):
                return f
        return None
