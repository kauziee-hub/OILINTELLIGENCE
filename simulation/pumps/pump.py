"""Centrifugal pump model (section 4.5).

    H(Q) = hf * (s^2 H0 - a Q^2)       s = n / n_rated, hf = head degradation factor
    eta(Q) = ef * eta_bep * (2r - r^2), r = Q / (s Q_bep)
    P_shaft = rho g Q H / eta          P_elec = P_shaft / eta_motor
"""
from __future__ import annotations

import math

from industrial.schemas.plant import Pump as PumpCfg

STOPPED, STARTING, RUNNING, STOPPING = "STOPPED", "STARTING", "RUNNING", "STOPPING"
STATUS_CODE = {STOPPED: 0.0, STARTING: 0.5, RUNNING: 1.0, STOPPING: 0.25}


class PumpModel:
    def __init__(self, pump_id: str, cfg: PumpCfg):
        self.id = pump_id
        self.cfg = cfg
        self.commanded_on = False
        self.speed_ratio = 0.0
        self.head_factor = 1.0      # fault: degradation reduces developed head
        self.eff_factor = 1.0       # fault: degradation reduces efficiency
        self.flow_m3h = 0.0
        self.head_m = 0.0
        self.shaft_power_w = 0.0
        self.elec_power_kw = 0.0
        self.run_hours = 0.0

    @property
    def status(self) -> str:
        if self.commanded_on:
            return RUNNING if self.speed_ratio >= 0.999 else STARTING
        return STOPPED if self.speed_ratio <= 1e-3 else STOPPING

    @property
    def active(self) -> bool:
        return self.speed_ratio > 1e-3

    def step(self, dt: float) -> None:
        rate = dt / max(self.cfg.startup_time_s, 1e-6)
        if self.commanded_on:
            self.speed_ratio = min(1.0, self.speed_ratio + rate)
            self.run_hours += dt / 3600.0
        else:
            self.speed_ratio = max(0.0, self.speed_ratio - rate * 1.5)

    # ---------------------------------------------------------------- hydraulics
    def head_at(self, q_m3h: float) -> float:
        s = self.speed_ratio
        return self.head_factor * (s * s * self.cfg.shutoff_head_m - self.cfg.curve_coeff_a * q_m3h * q_m3h)

    def flow_for_head(self, dh_m: float, eps: float = 0.05) -> float:
        """Inverse curve with smooth check-valve cut-off. Returns m3/h (>= 0)."""
        s = self.speed_ratio
        if s <= 1e-3:
            return 0.0
        x = s * s * self.cfg.shutoff_head_m - dh_m / self.head_factor
        sp = eps * math.log1p(math.exp(x / eps)) if x / eps < 50 else x
        return math.sqrt(sp / self.cfg.curve_coeff_a)

    def efficiency(self, q_m3h: float) -> float:
        s = max(self.speed_ratio, 1e-3)
        r = q_m3h / (s * self.cfg.rated_flow_m3h)
        eta = self.cfg.bep_efficiency * (2 * r - r * r)
        return max(eta, 0.08) * self.eff_factor

    def update_operating_point(self, q_m3h: float, rho: float, g: float) -> None:
        self.flow_m3h = q_m3h if self.active else 0.0
        if not self.active:
            self.head_m = 0.0
            self.shaft_power_w = 0.0
            self.elec_power_kw = 0.0
            return
        self.head_m = self.head_at(q_m3h)
        q = q_m3h / 3600.0
        s = self.speed_ratio
        if q_m3h < 1e-3:
            # deadhead limit of rho g Q H / eta as Q -> 0
            p_shaft = rho * g * self.cfg.shutoff_head_m * s ** 3 * (self.cfg.rated_flow_m3h / 3600.0) / (
                2 * self.cfg.bep_efficiency * self.eff_factor)
        else:
            p_shaft = rho * g * q * max(self.head_m, 0.0) / self.efficiency(q_m3h)
        self.shaft_power_w = p_shaft
        self.elec_power_kw = p_shaft / self.cfg.motor_efficiency / 1000.0
