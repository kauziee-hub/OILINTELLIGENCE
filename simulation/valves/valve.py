"""Valve model: rate-limited actuator with equal-percentage characteristic.

Cv(x) = Cv_max * R^(x-1)  for x >= x_min, linear to zero below x_min (tight shut-off).
K(x)  = k_open * (Cv_max / Cv(x))^2   (referenced to pipe velocity head)
A passing (leaking) seat is modelled as a minimum effective Cv fraction.
"""
from __future__ import annotations

import math

from industrial.schemas.plant import Valve as ValveCfg

X_MIN = 0.02


class ValveModel:
    def __init__(self, valve_id: str, cfg: ValveCfg):
        self.id = valve_id
        self.cfg = cfg
        self.position = 0.0          # true stem position, 0..1
        self.target = 0.0            # actuator target, 0..1
        self.command_open = False    # discrete command (OPEN/CLOSED)
        self.passing_fraction = 0.0  # fault: Cv fraction passing when seated

    def command(self, open_: bool, target: float | None = None) -> None:
        self.command_open = open_
        if not open_:
            self.target = 0.0
        else:
            self.target = 1.0 if target is None else min(max(target, 0.0), 1.0)

    def set_target(self, x: float) -> None:
        if self.command_open:
            self.target = min(max(x, 0.0), 1.0)

    def step(self, dt: float) -> bool:
        """Move stem toward target. Returns True if the position changed."""
        max_move = dt / max(self.cfg.stroke_time_s, 1e-6)
        d = self.target - self.position
        if abs(d) < 1e-9:
            return False
        self.position += max(-max_move, min(max_move, d))
        return True

    def cv_fraction(self, x: float | None = None) -> float:
        x = self.position if x is None else x
        R = self.cfg.rangeability
        if x >= X_MIN:
            c = R ** (x - 1.0)
        else:
            c = R ** (X_MIN - 1.0) * (x / X_MIN)
        return max(c, self.passing_fraction)

    def k_value(self) -> float:
        c = self.cv_fraction()
        if c <= 0:
            return math.inf
        return self.cfg.k_open / (c * c)

    def position_for_k(self, k: float) -> float:
        """Inverse characteristic (used for initialisation)."""
        if k <= self.cfg.k_open:
            return 1.0
        c = math.sqrt(self.cfg.k_open / k)
        return 1.0 + math.log(c) / math.log(self.cfg.rangeability)
