"""Fault hypothesis catalogue derived from the plant topology.

Each hypothesis H_i is a cause that can explain the observed evidence. The
`category` maps each cause onto the residual decomposition R = L + B + U + eps
(section 4.6):  L = physical loss, B = measurement bias, U = unrecorded /
incorrectly accounted movement.
"""
from __future__ import annotations

from dataclasses import dataclass

from industrial.schemas.plant import PlantConfig

CATEGORY = {
    "NORMAL": "normal",
    "PIPELINE_LEAK": "physical_loss",
    "TANK_LEAK": "physical_loss",
    "METER_ERROR": "measurement",
    "LEVEL_ERROR": "measurement",
    "TEMP_ERROR": "measurement",
    "DENSITY_ERROR": "measurement",
    "SENSOR_STUCK": "measurement",
    "SENSOR_DROPOUT": "measurement",
    "VALVE_PASSING": "unrecorded_movement",
    "MISSING_TRANSFER": "unrecorded_movement",
    "PUMP_DEGRADATION": "operational",
    "UNKNOWN": "unknown",
}

# Family prior mass (shared equally among targets of the family).
DEFAULT_FAMILY_PRIOR = {
    "NORMAL": 0.30,
    "PIPELINE_LEAK": 0.07,
    "TANK_LEAK": 0.05,
    "METER_ERROR": 0.09,
    "LEVEL_ERROR": 0.07,
    "TEMP_ERROR": 0.06,
    "DENSITY_ERROR": 0.03,
    "SENSOR_STUCK": 0.05,
    "SENSOR_DROPOUT": 0.05,
    "VALVE_PASSING": 0.07,
    "PUMP_DEGRADATION": 0.05,
    "MISSING_TRANSFER": 0.06,
    "UNKNOWN": 0.04,
}

LABELS = {
    "NORMAL": "Normal operating variation",
    "PIPELINE_LEAK": "Physical leak on pipeline",
    "TANK_LEAK": "Physical leak from tank (floor / shell)",
    "METER_ERROR": "Flow meter bias / drift",
    "LEVEL_ERROR": "Level gauge error",
    "TEMP_ERROR": "Temperature measurement error",
    "DENSITY_ERROR": "Density meter error",
    "SENSOR_STUCK": "Frozen (stuck) instrument",
    "SENSOR_DROPOUT": "Instrument communication dropout",
    "VALVE_PASSING": "Valve passing while closed (unmetered movement)",
    "MISSING_TRANSFER": "Movement without transfer record (accounting)",
    "PUMP_DEGRADATION": "Pump performance degradation",
    "UNKNOWN": "Unexplained abnormality (no known cause fits)",
}

# Map simulator fault types -> attribution families (evaluation only)
FAULT_TO_FAMILY = {
    "METER_BIAS": "METER_ERROR", "METER_DRIFT": "METER_ERROR",
    "LEVEL_BIAS": "LEVEL_ERROR", "LEVEL_DRIFT": "LEVEL_ERROR",
    "TEMPERATURE_BIAS": "TEMP_ERROR", "DENSITY_BIAS": "DENSITY_ERROR",
    "SENSOR_STUCK": "SENSOR_STUCK", "SENSOR_DROPOUT": "SENSOR_DROPOUT", "SENSOR_NOISE": "UNKNOWN",
    "PIPELINE_LEAK": "PIPELINE_LEAK", "TANK_LEAK": "TANK_LEAK", "VALVE_PASSING": "VALVE_PASSING",
    "PUMP_DEGRADATION": "PUMP_DEGRADATION", "MISSING_TRANSACTION": "MISSING_TRANSFER",
    "WRONG_QUANTITY": "UNKNOWN",
}


@dataclass(frozen=True)
class Hypothesis:
    hid: str
    family: str
    target: str | None
    prior: float

    @property
    def category(self) -> str:
        return CATEGORY[self.family]

    @property
    def label(self) -> str:
        base = LABELS[self.family]
        return f"{base} ({self.target})" if self.target else base


def build_hypotheses(plant: PlantConfig, family_prior: dict[str, float] | None = None,
                     exclude_families: set[str] | None = None) -> list[Hypothesis]:
    fp = dict(DEFAULT_FAMILY_PRIOR)
    if family_prior:
        fp.update(family_prior)
    ex = exclude_families or set()
    targets: dict[str, list[str | None]] = {
        "NORMAL": [None],
        "PIPELINE_LEAK": list(plant.pipelines),
        "TANK_LEAK": list(plant.tanks),
        "METER_ERROR": list(plant.meters()),
        "LEVEL_ERROR": list(plant.sensors_of_type("level")),
        "TEMP_ERROR": [s for s in plant.sensors_of_type("temperature") if plant.sensors[s].asset != plant.plant_id],
        "DENSITY_ERROR": list(plant.sensors_of_type("density")),
        "SENSOR_STUCK": list(plant.meters()) + list(plant.sensors_of_type("level")) +
        [s for s in plant.sensors_of_type("temperature") if plant.sensors[s].asset != plant.plant_id],
        "SENSOR_DROPOUT": list(plant.meters()) + list(plant.sensors_of_type("level")) +
        [s for s in plant.sensors_of_type("temperature") if plant.sensors[s].asset != plant.plant_id],
        "VALVE_PASSING": list(plant.valves),
        "PUMP_DEGRADATION": list(plant.pumps),
        "MISSING_TRANSFER": list(plant.routes),
        "UNKNOWN": [None],
    }
    out: list[Hypothesis] = []
    for fam, tg in targets.items():
        if fam in ex:
            continue
        for t in tg:
            out.append(Hypothesis(hid=f"{fam}:{t}" if t else fam, family=fam, target=t,
                                  prior=fp.get(fam, 0.01) / len(tg)))
    z = sum(h.prior for h in out)
    return [Hypothesis(h.hid, h.family, h.target, h.prior / z) for h in out]
