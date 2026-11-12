"""Virtual industrial instruments.

Measurement model for a continuous instrument:

    y = (x_true * (1 + e_rel_sys + b_rel(t)) + e_abs_sys + b_abs(t))
        + noise_white + noise_AR(1)
    then low-flow cut-off, range saturation and quantisation.

e_*_sys : calibration error drawn once per run (instrument accuracy)
b_*(t)  : injected fault bias (constant or drifting)
Faults (stuck, dropout, noise increase) are applied here; the fault state is
NEVER published - it only exists in simulator ground truth.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from industrial.schemas.plant import Sensor as SensorCfg

DISCRETE_TYPES = {"pump_status", "valve_command"}


@dataclass
class SensorFaultState:
    bias_abs: float = 0.0
    bias_rel: float = 0.0
    drift_abs_per_h: float = 0.0
    drift_rel_per_h: float = 0.0
    drift_start_s: float = 0.0
    stuck: bool = False
    dropout: bool = False
    noise_mult: float = 1.0
    active_fault_ids: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.bias_abs = self.bias_rel = 0.0
        self.drift_abs_per_h = self.drift_rel_per_h = 0.0
        self.stuck = self.dropout = False
        self.noise_mult = 1.0
        self.active_fault_ids = []

    def total_bias(self, t: float, x_true: float) -> float:
        hours = max(t - self.drift_start_s, 0.0) / 3600.0
        rel = self.bias_rel + self.drift_rel_per_h * hours
        ab = self.bias_abs + self.drift_abs_per_h * hours
        return x_true * rel + ab


class Instrument:
    def __init__(self, sensor_id: str, cfg: SensorCfg, rng: np.random.Generator,
                 sample_interval_s: float = 0.0):
        self.id = sensor_id
        self.cfg = cfg
        self.rng = rng
        self.discrete = cfg.type in DISCRETE_TYPES or cfg.type == "valve_position"
        # calibration (systematic) errors for this run
        self.sys_abs = float(rng.normal(0.0, cfg.accuracy)) if cfg.accuracy else 0.0
        self.sys_rel = float(rng.normal(0.0, cfg.accuracy_rel)) if cfg.accuracy_rel else 0.0
        self.ar_state = 0.0
        self.fault = SensorFaultState()
        self._stuck_value: float | None = None
        self.last_value: float | None = None
        self.last_published: float | None = None
        self.interval = max(cfg.publish_interval_s, sample_interval_s)
        self.next_publish_t = float(rng.uniform(0.0, min(self.interval, 5.0)))
        # flow-meter totalizer (gross volume, m3) computed by the device from its own reading
        self.totalizer = float(rng.uniform(1.0e5, 9.0e5)) if cfg.type == "flow" else None
        self.last_total_published: float | None = None

    def set_sample_interval(self, sample_interval_s: float) -> None:
        self.interval = max(self.cfg.publish_interval_s, sample_interval_s)

    # ------------------------------------------------------------------ measure
    def measure(self, t: float, x_true: float, dt: float) -> float:
        """Return the instrument's current reading (before publication logic)."""
        cfg = self.cfg
        if self.discrete:
            y = x_true
        else:
            sigma = cfg.noise + cfg.noise_abs + cfg.noise_rel * abs(x_true)
            sigma *= self.fault.noise_mult
            if cfg.ar_sigma > 0:
                phi = math.exp(-dt / max(cfg.ar_tau_s, 1e-6))
                self.ar_state = phi * self.ar_state + math.sqrt(1 - phi * phi) * cfg.ar_sigma * float(
                    self.rng.normal())
            noise = float(self.rng.normal(0.0, sigma)) if sigma > 0 else 0.0
            y = x_true * (1.0 + self.sys_rel) + self.sys_abs + self.fault.total_bias(t, x_true)
            y += noise + self.ar_state
            if cfg.type == "flow":
                if y < cfg.low_flow_cutoff:
                    y = 0.0
            if cfg.resolution > 0:
                y = round(y / cfg.resolution) * cfg.resolution
        if self.fault.stuck:
            if self._stuck_value is None:
                self._stuck_value = self.last_value if self.last_value is not None else y
            y = self._stuck_value
        else:
            self._stuck_value = None
        self.last_value = y
        if self.totalizer is not None:
            self.totalizer += max(y, 0.0) * dt / 3600.0
        return y

    def quality(self, y: float) -> tuple[float, str]:
        if self.cfg.range is None or self.discrete:
            return y, "GOOD"
        lo, hi = self.cfg.range
        if y < lo or y > hi:
            return min(max(y, lo), hi), "BAD"
        span = hi - lo
        if self.cfg.type != "flow" and (y - lo < 0.01 * span or hi - y < 0.01 * span):
            return y, "UNCERTAIN"
        return y, "GOOD"

    def due(self, t: float) -> bool:
        if self.fault.dropout:
            if t >= self.next_publish_t:
                self.next_publish_t = t + self.interval
            return False
        if t + 1e-9 >= self.next_publish_t:
            self.next_publish_t += self.interval
            if self.next_publish_t <= t:
                self.next_publish_t = t + self.interval
            return True
        if self.cfg.on_change and self.last_value is not None and self.last_published is not None:
            if abs(self.last_value - self.last_published) > 1e-9:
                return True
        return False
