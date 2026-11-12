"""Verification action catalogue (Model 4 inputs).

Each field / desk check has a discrete outcome (POSITIVE = check finds a fault
at its target) with a likelihood P(outcome | H) given by its sensitivity for
the hypotheses it can confirm and a false-positive rate otherwise. The
simulation oracle (`simulate_outcome`) samples outcomes from exactly this
model using simulator ground truth, so the information-gain engine is
consistent with the simulated world. In plant mode outcomes are entered by
engineers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from industrial.schemas.plant import PlantConfig
from intelligence.fault_attribution.hypotheses import Hypothesis

OUTCOMES = ("POSITIVE", "NEGATIVE")


@dataclass(frozen=True)
class ActionType:
    name: str
    description: str
    cost: float              # relative cost units (engineering hours equivalent)
    duration_h: float
    safety_risk: float       # 0 (desk work) .. 1 (hazardous field work)
    requires_isolation: bool
    false_positive: float
    confirms: dict[str, float] = field(default_factory=dict)   # family -> sensitivity


ACTION_TYPES: dict[str, ActionType] = {
    "LEDGER_RECONCILIATION": ActionType(
        "LEDGER_RECONCILIATION", "Reconcile SCADA movement log against transfer tickets and operator log",
        cost=1.0, duration_h=0.5, safety_risk=0.0, requires_isolation=False, false_positive=0.03,
        confirms={"MISSING_TRANSFER": 0.95}),
    "INSTRUMENT_DIAGNOSTICS": ActionType(
        "INSTRUMENT_DIAGNOSTICS", "Review transmitter self-diagnostics, comms status and raw signal",
        cost=1.0, duration_h=0.5, safety_risk=0.0, requires_isolation=False, false_positive=0.02,
        confirms={"SENSOR_STUCK": 0.97, "SENSOR_DROPOUT": 0.97}),
    "RETRIEVE_METER_PROVING_RECORD": ActionType(
        "RETRIEVE_METER_PROVING_RECORD", "Retrieve latest meter proving record and run a fresh proving",
        cost=4.0, duration_h=3.0, safety_risk=0.1, requires_isolation=False, false_positive=0.04,
        confirms={"METER_ERROR": 0.93, "SENSOR_STUCK": 0.6}),
    "MANUAL_TANK_GAUGING": ActionType(
        "MANUAL_TANK_GAUGING", "Independent manual dip (innage) and temperature measurement",
        cost=2.0, duration_h=1.0, safety_risk=0.3, requires_isolation=False, false_positive=0.04,
        confirms={"LEVEL_ERROR": 0.92, "SENSOR_STUCK": 0.6, "TEMP_ERROR": 0.5}),
    "REFERENCE_TEMPERATURE_CHECK": ActionType(
        "REFERENCE_TEMPERATURE_CHECK", "Compare transmitter against a certified reference thermometer",
        cost=1.5, duration_h=1.0, safety_risk=0.2, requires_isolation=False, false_positive=0.03,
        confirms={"TEMP_ERROR": 0.95, "SENSOR_STUCK": 0.6}),
    "DENSITY_LAB_CHECK": ActionType(
        "DENSITY_LAB_CHECK", "Draw line sample and compare lab density with the density meter",
        cost=2.0, duration_h=4.0, safety_risk=0.1, requires_isolation=False, false_positive=0.04,
        confirms={"DENSITY_ERROR": 0.9}),
    "VALVE_SEAT_TEST": ActionType(
        "VALVE_SEAT_TEST", "Isolate and perform a seat-leak (pressure hold / acoustic) test",
        cost=5.0, duration_h=2.0, safety_risk=0.4, requires_isolation=True, false_positive=0.05,
        confirms={"VALVE_PASSING": 0.9}),
    "LINE_INSPECTION": ActionType(
        "LINE_INSPECTION", "Pipeline walk-down with leak survey (visual, acoustic, soil probe)",
        cost=6.0, duration_h=3.0, safety_risk=0.4, requires_isolation=False, false_positive=0.02,
        confirms={"PIPELINE_LEAK": 0.85}),
    "TANK_ISOLATION_TEST": ActionType(
        "TANK_ISOLATION_TEST", "Isolate tank and monitor gauged inventory (static leak test)",
        cost=8.0, duration_h=6.0, safety_risk=0.3, requires_isolation=True, false_positive=0.05,
        confirms={"TANK_LEAK": 0.85, "VALVE_PASSING": 0.5, "LEVEL_ERROR": 0.45}),
    "PUMP_PERFORMANCE_TEST": ActionType(
        "PUMP_PERFORMANCE_TEST", "Measure head/flow/power against the certified pump curve",
        cost=3.0, duration_h=1.5, safety_risk=0.2, requires_isolation=False, false_positive=0.03,
        confirms={"PUMP_DEGRADATION": 0.95}),
}


@dataclass(frozen=True)
class VerificationAction:
    action_id: str
    action_type: str
    target: str | None

    @property
    def spec(self) -> ActionType:
        return ACTION_TYPES[self.action_type]

    def relevant(self, plant: PlantConfig, h: Hypothesis) -> bool:
        """Whether a positive result at this action's target confirms hypothesis h."""
        fam, tg = h.family, h.target
        a = self.action_type
        if fam not in self.spec.confirms:
            return False
        if a == "LEDGER_RECONCILIATION":
            return True
        if a in ("INSTRUMENT_DIAGNOSTICS", "RETRIEVE_METER_PROVING_RECORD", "REFERENCE_TEMPERATURE_CHECK",
                 "DENSITY_LAB_CHECK"):
            return tg == self.target
        if a == "MANUAL_TANK_GAUGING":
            if fam in ("LEVEL_ERROR", "SENSOR_STUCK", "TEMP_ERROR") and tg in plant.sensors:
                return plant.sensors[tg].asset == self.target and plant.sensors[tg].type in ("level", "temperature")
            return False
        if a == "TANK_ISOLATION_TEST":
            if fam == "TANK_LEAK":
                return tg == self.target
            if fam == "VALVE_PASSING":
                return plant.valves[tg].tank_side == self.target
            if fam == "LEVEL_ERROR":
                return plant.sensors[tg].asset == self.target
            return False
        return tg == self.target

    def likelihood(self, plant: PlantConfig, h: Hypothesis) -> np.ndarray:
        if self.relevant(plant, h):
            p = self.spec.confirms[h.family]
        else:
            p = self.spec.false_positive
        return np.array([p, 1.0 - p])

    def to_dict(self) -> dict[str, Any]:
        sp = self.spec
        return {"action_id": self.action_id, "action": self.action_type, "target": self.target,
                "description": sp.description, "cost": sp.cost, "duration_h": sp.duration_h,
                "safety_risk": sp.safety_risk, "requires_isolation": sp.requires_isolation}


def build_actions(plant: PlantConfig) -> list[VerificationAction]:
    acts = [VerificationAction("LEDGER_RECONCILIATION", "LEDGER_RECONCILIATION", None)]
    instr = list(plant.meters()) + list(plant.sensors_of_type("level")) + [
        s for s in plant.sensors_of_type("temperature") if plant.sensors[s].asset != plant.plant_id]
    acts += [VerificationAction(f"INSTRUMENT_DIAGNOSTICS:{s}", "INSTRUMENT_DIAGNOSTICS", s) for s in instr]
    acts += [VerificationAction(f"RETRIEVE_METER_PROVING_RECORD:{m}", "RETRIEVE_METER_PROVING_RECORD", m)
             for m in plant.meters()]
    acts += [VerificationAction(f"MANUAL_TANK_GAUGING:{t}", "MANUAL_TANK_GAUGING", t) for t in plant.tanks]
    acts += [VerificationAction(f"REFERENCE_TEMPERATURE_CHECK:{s}", "REFERENCE_TEMPERATURE_CHECK", s)
             for s in plant.sensors_of_type("temperature") if plant.sensors[s].asset != plant.plant_id]
    acts += [VerificationAction(f"DENSITY_LAB_CHECK:{d}", "DENSITY_LAB_CHECK", d)
             for d in plant.sensors_of_type("density")]
    acts += [VerificationAction(f"VALVE_SEAT_TEST:{v}", "VALVE_SEAT_TEST", v) for v in plant.valves]
    acts += [VerificationAction(f"LINE_INSPECTION:{p}", "LINE_INSPECTION", p) for p in plant.pipelines]
    acts += [VerificationAction(f"TANK_ISOLATION_TEST:{t}", "TANK_ISOLATION_TEST", t) for t in plant.tanks]
    acts += [VerificationAction(f"PUMP_PERFORMANCE_TEST:{p}", "PUMP_PERFORMANCE_TEST", p) for p in plant.pumps]
    return acts


def action_by_id(plant: PlantConfig, action_id: str) -> VerificationAction:
    for a in build_actions(plant):
        if a.action_id == action_id:
            return a
    raise KeyError(action_id)


# ---------------------------------------------------------------------------
# Simulation oracle
# ---------------------------------------------------------------------------
_FAULT_FAMILY = {
    "METER_BIAS": "METER_ERROR", "METER_DRIFT": "METER_ERROR", "LEVEL_BIAS": "LEVEL_ERROR",
    "LEVEL_DRIFT": "LEVEL_ERROR", "TEMPERATURE_BIAS": "TEMP_ERROR", "DENSITY_BIAS": "DENSITY_ERROR",
    "SENSOR_STUCK": "SENSOR_STUCK", "SENSOR_DROPOUT": "SENSOR_DROPOUT", "PIPELINE_LEAK": "PIPELINE_LEAK",
    "TANK_LEAK": "TANK_LEAK", "VALVE_PASSING": "VALVE_PASSING", "PUMP_DEGRADATION": "PUMP_DEGRADATION",
    "MISSING_TRANSACTION": "MISSING_TRANSFER", "WRONG_QUANTITY": "MISSING_TRANSFER", "SENSOR_NOISE": "UNKNOWN",
}


def true_hypotheses(plant: PlantConfig, faults, routes_of_transfer: dict[str, str] | None = None) -> list[Hypothesis]:
    out = []
    for f in faults:
        fam = _FAULT_FAMILY[f.fault_type]
        tg = f.target
        if fam == "MISSING_TRANSFER" and routes_of_transfer:
            tg = routes_of_transfer.get(tg, tg)
        out.append(Hypothesis(f"{fam}:{tg}", fam, tg, 1.0))
    return out


def sample_outcome(plant: PlantConfig, action: VerificationAction, truth: list[Hypothesis],
                   rng: np.random.Generator) -> str:
    if truth:
        p_pos = 1.0 - np.prod([1.0 - action.likelihood(plant, h)[0] for h in truth])
    else:
        p_pos = action.spec.false_positive
    return "POSITIVE" if rng.random() < p_pos else "NEGATIVE"


def simulate_outcome(sim, action_type: str, target: str | None, rng: np.random.Generator) -> dict[str, Any]:
    """Field check performed against simulator ground truth (simulation mode only)."""
    plant = sim.plant
    aid = f"{action_type}:{target}" if target else action_type
    action = action_by_id(plant, aid)
    active = sim.faults.active(sim.t) + [f for f in sim.faults.faults if f.category == "accounting"]
    rmap = {e.order.transfer_id: e.order.route for e in sim.completed + sim.pending + ([sim.active] if sim.active else [])}
    truth = true_hypotheses(plant, active, rmap)
    outcome = sample_outcome(plant, action, truth, rng)
    detail: dict[str, Any] = {}
    if action_type == "RETRIEVE_METER_PROVING_RECORD":
        inst = sim.instruments[target]
        bias = inst.sys_rel + inst.fault.bias_rel + inst.fault.drift_rel_per_h * max(
            sim.t - inst.fault.drift_start_s, 0) / 3600
        mf = 1.0 / (1.0 + bias) + float(rng.normal(0, 0.0004))
        detail = {"meter_factor": round(mf, 5), "as_found_error_pct": round((1 / mf - 1) * 100, 3)}
    elif action_type == "MANUAL_TANK_GAUGING":
        tk = sim.tanks[target]
        dip = tk.level + float(rng.normal(0, 0.003))
        lt = sim.instruments[plant.tank_level_sensor(target)].last_value
        detail = {"manual_dip_m": round(dip, 4), "gauge_reading_m": round(lt, 4) if lt is not None else None,
                  "difference_mm": round((lt - dip) * 1000, 1) if lt is not None else None}
    elif action_type == "REFERENCE_TEMPERATURE_CHECK":
        ref = sim.true_values.get(target, float("nan")) + float(rng.normal(0, 0.1))
        rd = sim.instruments[target].last_value
        detail = {"reference_c": round(ref, 2), "transmitter_c": round(rd, 2) if rd is not None else None}
    elif action_type == "VALVE_SEAT_TEST":
        detail = {"seat_leak_detected": outcome == "POSITIVE"}
    elif action_type == "LINE_INSPECTION":
        detail = {"leak_found": outcome == "POSITIVE"}
    elif action_type == "PUMP_PERFORMANCE_TEST":
        p = sim.pumps[target]
        detail = {"head_factor_measured": round(p.head_factor + float(rng.normal(0, 0.01)), 3)}
    return {"action_id": aid, "outcome": outcome, "detail": detail, "source": "SIMULATION_ORACLE",
            "timestamp": sim.timestamp().isoformat()}
