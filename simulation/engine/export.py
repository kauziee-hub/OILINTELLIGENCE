"""Batch scenario execution and dataset export (four data layers, section 6.1)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from industrial.schemas.plant import PlantConfig, get_plant
from simulation.engine.simulator import Simulator
from simulation.scenarios.scenario import Scenario


@dataclass
class RunResult:
    scenario: Scenario
    telemetry: pd.DataFrame           # layer 2: sensor measurements (what a plant would see)
    truth: pd.DataFrame               # layer 1 + 4: physical truth & fault state (simulator only)
    transfers: pd.DataFrame           # layer 3: operational records
    calibrations: pd.DataFrame
    lab_samples: pd.DataFrame
    maintenance: pd.DataFrame
    operator_log: pd.DataFrame
    events: list[dict[str, Any]] = field(default_factory=list)
    faults: pd.DataFrame = field(default_factory=pd.DataFrame)
    sim: Simulator | None = None

    def records(self) -> dict[str, pd.DataFrame]:
        return {"transfers": self.transfers, "calibrations": self.calibrations,
                "lab_samples": self.lab_samples, "maintenance": self.maintenance,
                "operator_log": self.operator_log}

    # ------------------------------------------------------------------ io
    def save(self, out_dir: str | Path) -> Path:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "scenario.json").write_text(self.scenario.model_dump_json(indent=2), encoding="utf-8")
        self.telemetry.to_parquet(out / "telemetry.parquet", index=False)
        truth = self.truth.copy()
        truth["active_faults"] = truth["active_faults"].apply(json.dumps)
        truth.to_parquet(out / "ground_truth.parquet", index=False)
        for name, df in self.records().items():
            d = df.copy()
            for c in d.columns:
                if d[c].apply(lambda x: isinstance(x, (dict, list))).any():
                    d[c] = d[c].apply(json.dumps)
            d.to_parquet(out / f"{name}.parquet", index=False)
        self.faults.to_parquet(out / "fault_injections.parquet", index=False)
        (out / "events.json").write_text(json.dumps(self.events, default=str, indent=1), encoding="utf-8")
        return out

    @classmethod
    def load(cls, run_dir: str | Path) -> "RunResult":
        d = Path(run_dir)
        sc = Scenario.model_validate_json((d / "scenario.json").read_text(encoding="utf-8"))
        truth = pd.read_parquet(d / "ground_truth.parquet")
        truth["active_faults"] = truth["active_faults"].apply(json.loads)

        def rd(name):
            df = pd.read_parquet(d / f"{name}.parquet")
            for c in ("opening_gauges", "closing_gauges"):
                if c in df.columns:
                    df[c] = df[c].apply(lambda s: json.loads(s) if isinstance(s, str) else s)
            return df

        return cls(
            scenario=sc, telemetry=pd.read_parquet(d / "telemetry.parquet"), truth=truth,
            transfers=rd("transfers"), calibrations=rd("calibrations"), lab_samples=rd("lab_samples"),
            maintenance=rd("maintenance"), operator_log=rd("operator_log"),
            events=json.loads((d / "events.json").read_text(encoding="utf-8")),
            faults=pd.read_parquet(d / "fault_injections.parquet"),
        )


TELEMETRY_COLUMNS = ["t", "sensor_id", "asset_id", "parameter", "value", "unit", "quality"]


def run_scenario(scenario: Scenario, plant: PlantConfig | None = None, keep_sim: bool = False) -> RunResult:
    plant = plant or get_plant()
    sim = Simulator(scenario, plant)
    rows: list[tuple] = []
    events: list[dict[str, Any]] = []

    def collect(o):
        rows.extend(o.telemetry)
        events.extend(o.events)

    sim.run(on_step=collect)
    tel = pd.DataFrame(rows, columns=TELEMETRY_COLUMNS)
    tel["timestamp"] = pd.to_datetime(scenario.start_time) + pd.to_timedelta(tel["t"], unit="s")
    truth = pd.DataFrame(sim.truth)
    transfers = pd.DataFrame([r.as_dict() for r in sim.records.transfers.values()])
    faults = pd.DataFrame([
        {"fault_id": f.fault_id, "scenario_id": scenario.scenario_id, "fault_type": f.fault_type,
         "affected_asset": f.target, "start_s": f.start_s, "end_s": f.end_s,
         "start_time": scenario.start_time + pd.Timedelta(seconds=f.start_s),
         "end_time": (scenario.start_time + pd.Timedelta(seconds=f.end_s)) if f.end_s is not None else None,
         "severity": f.severity, "fault_parameters": json.dumps(f.params)}
        for f in sim.faults.faults])
    return RunResult(
        scenario=scenario, telemetry=tel, truth=truth, transfers=transfers,
        calibrations=pd.DataFrame(sim.records.calibrations), lab_samples=pd.DataFrame(sim.records.lab_samples),
        maintenance=pd.DataFrame(sim.records.maintenance), operator_log=pd.DataFrame(sim.records.operator_log),
        events=events, faults=faults, sim=sim if keep_sim else None,
    )
