"""Operational records layer (data layer 3).

Generates what a real terminal's business systems would contain: transfer
tickets (from the flow computer, i.e. from *measured* values), meter proving
history, instrument calibration checks, tank lab density samples, maintenance
history and operator log entries. Accounting faults (missing / wrong
transactions) corrupt this layer only - never the physics.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np


@dataclass
class TransferRecord:
    transfer_id: str
    route: str
    source_tank: str
    destination_tank: str
    start_time: datetime
    end_time: datetime | None = None
    recorded_quantity: float | None = None     # tonnes (net mass from custody meter ticket)
    recorded_volume_m3: float | None = None    # gross volume (totalizer)
    quantity_unit: str = "t"
    product_id: str = "CRUDE"
    custody_meter: str = ""
    status: str = "IN_PROGRESS"
    opening_gauges: dict[str, float] = field(default_factory=dict)
    closing_gauges: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "transfer_id": self.transfer_id, "route": self.route, "source_tank": self.source_tank,
            "destination_tank": self.destination_tank, "start_time": self.start_time,
            "end_time": self.end_time, "recorded_quantity": self.recorded_quantity,
            "recorded_volume_m3": self.recorded_volume_m3, "quantity_unit": self.quantity_unit,
            "product_id": self.product_id, "custody_meter": self.custody_meter, "status": self.status,
            "opening_gauges": self.opening_gauges, "closing_gauges": self.closing_gauges,
        }


class FlowComputer:
    """Integrates a custody meter's measured gross flow into net mass (tonnes).

    Net mass = sum( Q_gross * CTL(T_line) * rho_15 * dt ), using the meter's
    associated temperature and density instruments - exactly the measured
    quantities, so instrument faults propagate into the ticket.
    """

    def __init__(self, alpha: float = 0.00085):
        self.alpha = alpha
        self.net_mass_t = 0.0
        self.gross_m3 = 0.0

    def accumulate(self, q_m3h: float, t_line_c: float, rho15: float, dt: float) -> None:
        if q_m3h <= 0:
            return
        v = q_m3h * dt / 3600.0
        ctl = 1.0 / (1.0 + self.alpha * (t_line_c - 15.0))
        self.gross_m3 += v
        self.net_mass_t += v * ctl * rho15 / 1000.0


class RecordsLedger:
    def __init__(self, start_time: datetime, rng: np.random.Generator):
        self.start_time = start_time
        self.rng = rng
        self.transfers: dict[str, TransferRecord] = {}
        self.calibrations: list[dict[str, Any]] = []
        self.lab_samples: list[dict[str, Any]] = []
        self.maintenance: list[dict[str, Any]] = []
        self.operator_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------ history
    def seed_history(self, plant, instruments: dict, tanks: dict) -> None:
        """Generate pre-run history consistent with each instrument's calibration error."""
        rng = self.rng
        for sid, inst in instruments.items():
            cfg = inst.cfg
            if cfg.type == "flow":
                for k in range(3):
                    days = int(rng.integers(25, 35)) * (k + 1)
                    # meter factor reflects the systematic error at proving time (+ proving repeatability)
                    mf = 1.0 / (1.0 + inst.sys_rel) + float(rng.normal(0, 0.0004))
                    self.calibrations.append({
                        "record_id": str(uuid.uuid4()), "sensor_id": sid, "record_type": "METER_PROVING",
                        "timestamp": self.start_time - timedelta(days=days),
                        "result": "PASS" if abs(mf - 1) < 0.0025 else "ADJUSTED",
                        "meter_factor": round(mf, 5), "as_found_error_pct": round((1 / mf - 1) * 100, 3),
                        "technician": "Metrology team", "notes": "Prover loop, 5 consecutive runs",
                    })
            elif cfg.type in ("level", "temperature", "density", "pressure"):
                days = int(rng.integers(20, 120))
                err = inst.sys_abs + float(rng.normal(0, max(cfg.accuracy, 1e-6) * 0.3))
                tol = 3.0 * max(cfg.accuracy, 1e-6)
                self.calibrations.append({
                    "record_id": str(uuid.uuid4()), "sensor_id": sid,
                    "record_type": {"level": "MANUAL_GAUGE_COMPARISON", "temperature": "REFERENCE_CHECK",
                                    "density": "LAB_COMPARISON", "pressure": "LOOP_CALIBRATION"}[cfg.type],
                    "timestamp": self.start_time - timedelta(days=days),
                    "result": "PASS" if abs(err) < tol else "FAIL",
                    "as_found_error": round(err, 5), "unit": cfg.unit, "tolerance": round(tol, 5),
                    "technician": "Instrument team", "notes": "",
                })
        for tid, tank in tanks.items():
            self.lab_samples.append({
                "sample_id": str(uuid.uuid4()), "tank_id": tid,
                "timestamp": self.start_time - timedelta(hours=float(rng.uniform(2, 20))),
                "density_15c": round(tank.rho15 + float(rng.normal(0, 0.3)), 2),
                "bsw_pct": round(float(rng.uniform(0.05, 0.3)), 3), "method": "ASTM D5002 (synthetic)",
            })
        for pid in plant.pumps:
            self.maintenance.append({
                "record_id": str(uuid.uuid4()), "asset_id": pid, "record_type": "PUMP_OVERHAUL",
                "timestamp": self.start_time - timedelta(days=int(rng.integers(90, 400))),
                "notes": "Mechanical seal replaced; performance test within 3% of curve",
            })
        for vid in plant.valves:
            self.maintenance.append({
                "record_id": str(uuid.uuid4()), "asset_id": vid, "record_type": "VALVE_SEAT_TEST",
                "timestamp": self.start_time - timedelta(days=int(rng.integers(60, 500))),
                "notes": "Seat leakage class IV verified",
            })
        for plid in plant.pipelines:
            self.maintenance.append({
                "record_id": str(uuid.uuid4()), "asset_id": plid, "record_type": "LINE_INSPECTION",
                "timestamp": self.start_time - timedelta(days=int(rng.integers(30, 365))),
                "notes": "Visual walk-down, no anomalies",
            })

    # ------------------------------------------------------------ live logging
    def log(self, ts: datetime, text: str, asset_id: str | None = None, category: str = "OPERATIONS") -> None:
        self.operator_log.append({"log_id": str(uuid.uuid4()), "timestamp": ts, "asset_id": asset_id,
                                  "category": category, "text": text})

    def add_lab_sample(self, ts: datetime, tank_id: str, true_rho15: float) -> dict[str, Any]:
        rec = {"sample_id": str(uuid.uuid4()), "tank_id": tank_id, "timestamp": ts,
               "density_15c": round(true_rho15 + float(self.rng.normal(0, 0.3)), 2),
               "bsw_pct": round(float(self.rng.uniform(0.05, 0.3)), 3), "method": "ASTM D5002 (synthetic)"}
        self.lab_samples.append(rec)
        return rec
