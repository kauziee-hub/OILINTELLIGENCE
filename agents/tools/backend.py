"""Data-access interface the investigation tools run against.

The FastAPI application implements it on top of the database (plant or
simulation data); `InMemoryBackend` implements it over a simulated run for
tests, evaluation and the CLI. Neither exposes simulator ground truth or any
process-control capability.
"""
from __future__ import annotations

import abc
import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from industrial.schemas.plant import PlantConfig, get_plant
from intelligence.physics.measurement import to_epoch
from intelligence.service import DiagnosticEngine


class InvestigationBackend(abc.ABC):
    plant: PlantConfig
    engine: DiagnosticEngine

    # ------------------------------------------------------------- read data
    @abc.abstractmethod
    def telemetry(self, t0: float, t1: float, sensor_ids: list[str] | None = None) -> pd.DataFrame:
        """Telemetry in the internal contract (sensor_id, parameter, value, quality, timestamp)."""

    @abc.abstractmethod
    def records(self, t0: float, t1: float) -> dict[str, pd.DataFrame]:
        """Operational records: transfers, lab_samples, calibrations, maintenance, operator_log."""

    def asset(self, asset_id: str) -> dict[str, Any] | None:
        P = self.plant
        if asset_id not in P.all_asset_ids() and asset_id not in P.sensors:
            return None
        if asset_id in P.sensors:
            return {"asset_id": asset_id, "asset_type": "sensor", **P.sensors[asset_id].model_dump()}
        kind = P.asset_type(asset_id)
        cfg = {"tank": P.tanks, "pipeline": P.pipelines, "pump": P.pumps, "valve": P.valves,
               "manifold": P.junctions}.get(kind, {}).get(asset_id)
        sensors = [s for s, c in P.sensors.items() if c.asset == asset_id]
        return {"asset_id": asset_id, "asset_type": kind,
                "configuration": cfg.model_dump(by_alias=True) if cfg is not None else {}, "sensors": sensors}

    def transfer_active(self, t: float) -> bool:
        tel = self.telemetry(t - 120, t, [f"{p}-ST" for p in self.plant.pumps])
        if not len(tel):
            return False
        return bool((pd.to_numeric(tel["value"], errors="coerce") >= 0.5).any())

    @abc.abstractmethod
    def now(self) -> float:
        """Current analysis time (epoch seconds)."""

    def data_start(self) -> float | None:
        """Earliest available telemetry time (epoch seconds), if known."""
        return None

    # ----------------------------------------------------------- persistence
    @abc.abstractmethod
    def find_open_investigation(self, assets: list[str], t0: float, t1: float) -> str | None: ...

    @abc.abstractmethod
    def create_investigation(self, event: dict[str, Any]) -> str: ...

    @abc.abstractmethod
    def save_investigation(self, inv_id: str, state: dict[str, Any]) -> None: ...

    @abc.abstractmethod
    def load_investigation(self, inv_id: str) -> dict[str, Any] | None: ...

    @abc.abstractmethod
    def audit(self, actor: str, action: str, details: dict[str, Any]) -> None: ...

    def record_diagnostic_case(self, case: dict[str, Any]) -> None:  # feedback -> future training data
        pass


class InMemoryBackend(InvestigationBackend):
    """Backend over a completed simulation run (telemetry + records only)."""

    def __init__(self, run, engine: DiagnosticEngine | None = None, plant: PlantConfig | None = None):
        self.plant = plant or get_plant()
        self.engine = engine or DiagnosticEngine(self.plant)
        tel = run.telemetry.copy()
        tel["ts"] = to_epoch(tel["timestamp"])
        self._tel = tel
        self._records = run.records()
        self._now = float(tel["ts"].max())
        self.investigations: dict[str, dict[str, Any]] = {}
        self.audit_log: list[dict[str, Any]] = []
        self.cases: list[dict[str, Any]] = []

    def set_now(self, t: float) -> None:
        self._now = t

    def data_start(self) -> float | None:
        return float(self._tel["ts"].min()) if len(self._tel) else None

    def now(self) -> float:
        return self._now

    def telemetry(self, t0, t1, sensor_ids=None):
        t1 = min(t1, self._now)
        m = (self._tel["ts"] >= t0) & (self._tel["ts"] <= t1)
        if sensor_ids:
            m &= self._tel["sensor_id"].isin(sensor_ids)
        return self._tel[m]

    def records(self, t0, t1):
        out = {}
        for name, df in self._records.items():
            if not len(df):
                out[name] = df
                continue
            col = "start_time" if name == "transfers" else "timestamp"
            ts = to_epoch(df[col])
            keep = ts <= t1
            if name == "transfers":
                ends = [to_epoch(x) if x is not None and not pd.isna(x) else None for x in df["end_time"]]
                d = df[keep].copy()
                # records not yet completed at analysis time appear IN_PROGRESS
                for i, (idx, e) in enumerate(zip(d.index, [e for e, k in zip(ends, keep) if k])):
                    if e is not None and e > t1:
                        d.loc[idx, ["end_time", "recorded_quantity", "status"]] = [None, None, "IN_PROGRESS"]
                out[name] = d
            else:
                out[name] = df[keep]
        return out

    def find_open_investigation(self, assets, t0, t1):
        for iid, st in self.investigations.items():
            if st.get("status") in ("CLOSED", "REJECTED"):
                continue
            ev = st.get("event", {})
            if set(ev.get("assets", [])) & set(assets) and ev.get("t1", 0) >= t0 - 3600:
                return iid
        return None

    def create_investigation(self, event):
        iid = f"INV-{uuid.uuid4().hex[:6].upper()}"
        self.investigations[iid] = {"investigation_id": iid, "event": event, "status": "RECEIVED",
                                    "created_at": datetime.now(timezone.utc).isoformat()}
        return iid

    def save_investigation(self, inv_id, state):
        self.investigations[inv_id] = dict(state)

    def load_investigation(self, inv_id):
        return self.investigations.get(inv_id)

    def audit(self, actor, action, details):
        self.audit_log.append({"actor": actor, "action": action, "details": details,
                               "timestamp": datetime.now(timezone.utc).isoformat()})

    def record_diagnostic_case(self, case):
        self.cases.append(case)
