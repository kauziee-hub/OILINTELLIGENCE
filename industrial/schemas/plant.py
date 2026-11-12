"""Plant definition schema and loader.

The plant YAML (config/plant.yaml) is validated into these Pydantic models. The
same object is used by the simulator (physical parameters), the intelligence
layer (measurement topology) and the backend (asset registry / 3D layout).
"""
from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLANT_PATH = REPO_ROOT / "config" / "plant.yaml"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Product(_Base):
    name: str
    density_15c: float
    thermal_expansion_coeff: float
    viscosity_15c_cst: float
    viscosity_temp_coeff: float
    specific_heat_j_kgk: float


class Environment(_Base):
    ambient_mean_c: float = 30.0
    ambient_diurnal_amplitude_c: float = 6.0
    ambient_peak_hour: float = 14.0
    gravity: float = 9.80665


class Tank(_Base):
    name: str
    diameter_m: float
    shell_height_m: float
    max_safe_fill_m: float
    min_operating_level_m: float
    nozzle_elevation_m: float
    base_elevation_m: float = 0.0
    heat_transfer_ua_w_k: float
    initial_product: str
    initial_level_range_m: tuple[float, float]
    initial_temperature_range_c: tuple[float, float]
    position: tuple[float, float, float]

    @property
    def area_m2(self) -> float:
        return math.pi * (self.diameter_m / 2.0) ** 2

    @property
    def nominal_capacity_m3(self) -> float:
        return self.area_m2 * self.shell_height_m


class Junction(_Base):
    name: str
    elevation_m: float
    position: tuple[float, float, float]


class Pipeline(_Base):
    name: str
    from_: str = Field(alias="from")
    to: str
    nominal_diameter_mm: float
    internal_diameter_m: float
    length_m: float
    roughness_m: float
    fittings_k: float
    elevation_m: float
    path: list[tuple[float, float, float]]

    @property
    def area_m2(self) -> float:
        return math.pi * (self.internal_diameter_m / 2.0) ** 2


class Pump(_Base):
    name: str
    from_: str = Field(alias="from")
    to: str
    shutoff_head_m: float
    curve_coeff_a: float
    rated_flow_m3h: float
    rated_speed_rpm: float
    bep_efficiency: float
    motor_efficiency: float
    startup_time_s: float
    position: tuple[float, float, float]


class Valve(_Base):
    name: str
    pipeline: str
    position_on_line: float
    role: Literal["isolation", "control"]
    k_open: float
    rangeability: float
    stroke_time_s: float
    tank_side: str | None = None


class Sensor(BaseModel):
    """Instrument specification. Extra keys are allowed for type-specific options."""

    model_config = ConfigDict(extra="allow")

    type: str
    asset: str
    parameter: str
    unit: str
    location: float | None = None
    range: tuple[float, float] | None = None
    noise: float = 0.0
    noise_rel: float = 0.0
    noise_abs: float = 0.0
    ar_sigma: float = 0.0
    ar_tau_s: float = 600.0
    accuracy: float = 0.0
    accuracy_rel: float = 0.0
    resolution: float = 0.0
    low_flow_cutoff: float = 0.0
    publish_interval_s: float = 1.0
    on_change: bool = False
    temperature_sensor: str | None = None
    density_sensor: str | None = None


class Route(_Base):
    name: str
    source: str
    destination: str
    open_valves: list[str]
    control_valve: str
    control_meter: str
    custody_meter: str
    pipelines: list[str]
    meters: list[str]
    setpoint_range_m3h: tuple[float, float]


class ControlTuning(_Base):
    flow_kp: float = 0.0025
    flow_ti_s: float = 20.0


class PlantConfig(_Base):
    schema_version: str
    plant_id: str
    name: str
    reference_temperature_c: float = 15.0
    products: dict[str, Product]
    environment: Environment
    tanks: dict[str, Tank]
    junctions: dict[str, Junction]
    pipelines: dict[str, Pipeline]
    pumps: dict[str, Pump]
    valves: dict[str, Valve]
    sensors: dict[str, Sensor]
    routes: dict[str, Route]
    control: ControlTuning = ControlTuning()

    @model_validator(mode="after")
    def _validate_references(self) -> "PlantConfig":
        nodes = set(self.tanks) | set(self.junctions)
        assets = self.all_asset_ids()
        for pid, p in self.pipelines.items():
            if p.from_ not in nodes or p.to not in nodes:
                raise ValueError(f"Pipeline {pid} references unknown node")
        for pid, p in self.pumps.items():
            if p.from_ not in nodes or p.to not in nodes:
                raise ValueError(f"Pump {pid} references unknown node")
        for vid, v in self.valves.items():
            if v.pipeline not in self.pipelines:
                raise ValueError(f"Valve {vid} references unknown pipeline {v.pipeline}")
        for sid, s in self.sensors.items():
            if s.asset not in assets:
                raise ValueError(f"Sensor {sid} references unknown asset {s.asset}")
            for ref in (s.temperature_sensor, s.density_sensor):
                if ref and ref not in self.sensors:
                    raise ValueError(f"Sensor {sid} references unknown sensor {ref}")
        for rid, r in self.routes.items():
            for v in r.open_valves + [r.control_valve]:
                if v not in self.valves:
                    raise ValueError(f"Route {rid} references unknown valve {v}")
            for m in r.meters + [r.control_meter, r.custody_meter]:
                if m not in self.sensors or self.sensors[m].type != "flow":
                    raise ValueError(f"Route {rid} references invalid meter {m}")
            if r.source not in self.tanks or r.destination not in self.tanks:
                raise ValueError(f"Route {rid} has invalid source/destination")
        for t in self.tanks.values():
            if t.initial_product not in self.products:
                raise ValueError(f"Unknown product {t.initial_product}")
        return self

    # ------------------------------------------------------------------ helpers
    def all_asset_ids(self) -> set[str]:
        return (
            {self.plant_id}
            | set(self.tanks)
            | set(self.junctions)
            | set(self.pipelines)
            | set(self.pumps)
            | set(self.valves)
        )

    def asset_type(self, asset_id: str) -> str:
        if asset_id in self.tanks:
            return "tank"
        if asset_id in self.junctions:
            return "manifold"
        if asset_id in self.pipelines:
            return "pipeline"
        if asset_id in self.pumps:
            return "pump"
        if asset_id in self.valves:
            return "valve"
        if asset_id == self.plant_id:
            return "facility"
        if asset_id in self.sensors:
            return "sensor"
        raise KeyError(asset_id)

    def sensors_of_type(self, sensor_type: str) -> dict[str, Sensor]:
        return {k: v for k, v in self.sensors.items() if v.type == sensor_type}

    def tank_level_sensor(self, tank_id: str) -> str:
        return next(k for k, s in self.sensors.items() if s.type == "level" and s.asset == tank_id)

    def tank_temperature_sensor(self, tank_id: str) -> str:
        return next(
            k for k, s in self.sensors.items()
            if s.type == "temperature" and s.asset == tank_id
        )

    def meters(self) -> dict[str, Sensor]:
        return self.sensors_of_type("flow")

    def valve_on_tank(self, tank_id: str) -> list[str]:
        return [vid for vid, v in self.valves.items() if v.tank_side == tank_id]

    def pipeline_of_tank_valve(self, valve_id: str) -> str:
        return self.valves[valve_id].pipeline

    def sensor_tag(self, sensor_id: str) -> str:
        """MQTT/OPC-style source tag for a sensor."""
        s = self.sensors[sensor_id]
        kind = self.asset_type(s.asset)
        return f"{kind}/{s.asset}/{s.parameter}"


def load_plant(path: str | os.PathLike | None = None) -> PlantConfig:
    p = Path(path or os.environ.get("PLANT_CONFIG", DEFAULT_PLANT_PATH))
    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return PlantConfig.model_validate(raw)


@lru_cache(maxsize=4)
def get_plant(path: str | None = None) -> PlantConfig:
    return load_plant(path)
