"""Lumped-parameter vertical cylindrical tank (flat bottom).

State: oil mass M, standard volume V15, enthalpy H and mixing composition.
    dM/dt = sum(m_in) - sum(m_out) - m_loss                         (4.1)
    V_obs = V15 * (1 + alpha (T - 15))  ;  h = V_obs / A            (4.2, 4.3)
    d(M cp T)/dt = sum(m_in cp_in T_in) - m_out cp T + UA (T_amb - T)

Outflows always leave at the tank's (well-mixed) composition and temperature.
"""
from __future__ import annotations

import math

from industrial.schemas.plant import Tank as TankCfg
from simulation.process.properties import CrudeProperties, Stream


class TankModel:
    def __init__(self, tank_id: str, cfg: TankCfg, product: CrudeProperties,
                 level_m: float, temperature_c: float, product_id: str):
        self.id = tank_id
        self.cfg = cfg
        self.area = cfg.area_m2
        self.product_id = product_id
        v_obs = self.area * level_m
        v15 = v_obs * product.vcf(temperature_c)
        m = v15 * product.density_15c
        # contents represented as a Stream "inventory" (extensive amounts, not rates)
        self.contents = Stream.from_props(product, temperature_c, m)
        self.cum_in_kg = 0.0
        self.cum_out_kg = 0.0
        self.cum_loss_kg = 0.0

    # --------------------------------------------------------------- properties
    @property
    def mass(self) -> float:
        return self.contents.m

    @property
    def temperature(self) -> float:
        return self.contents.temperature

    @property
    def rho15(self) -> float:
        return self.contents.rho15

    @property
    def alpha(self) -> float:
        return self.contents.alpha

    @property
    def v15(self) -> float:
        return self.contents.v15

    @property
    def volume_obs(self) -> float:
        return self.v15 * (1.0 + self.alpha * (self.temperature - 15.0))

    @property
    def level(self) -> float:
        return self.volume_obs / self.area

    @property
    def liquid_head(self) -> float:
        """Piezometric head at the tank nozzle (liquid surface elevation), m."""
        return self.cfg.base_elevation_m + self.level

    @property
    def density_obs(self) -> float:
        return self.contents.line_density()

    def props(self) -> CrudeProperties:
        return self.contents.props()

    def outflow_stream(self, mass_rate: float) -> Stream:
        """Stream leaving the tank at its current composition."""
        if self.contents.m <= 0:
            return Stream()
        return self.contents.scaled(mass_rate / self.contents.m)

    # ------------------------------------------------------------------- update
    def step(self, dt: float, inflows: list[Stream], outflow_mass_rate: float,
             loss_mass_rate: float, ambient_c: float) -> None:
        """Advance tank state by dt seconds.

        inflows: list of incoming stream rates
        outflow_mass_rate: total transfer outflow (kg/s, >= 0)
        loss_mass_rate: physical loss (floor leak) (kg/s, >= 0)
        """
        total_out = max(outflow_mass_rate, 0.0) + max(loss_mass_rate, 0.0)
        max_out = self.contents.m / dt * 0.999
        if total_out > max_out:  # never drain below empty
            f = max_out / total_out
            outflow_mass_rate *= f
            loss_mass_rate *= f
            total_out = max_out
        delta = Stream()
        for s in inflows:
            delta = delta + s.scaled(dt)
            self.cum_in_kg += s.m * dt
        if total_out > 0:
            delta = delta + self.contents.scaled(-total_out * dt / self.contents.m)
        self.cum_out_kg += max(outflow_mass_rate, 0.0) * dt
        self.cum_loss_kg += max(loss_mass_rate, 0.0) * dt
        T = self.contents.temperature
        new = self.contents + delta
        # heat exchange with ambient (implicit-ish explicit Euler, stable for tank time constants)
        q_amb = self.cfg.heat_transfer_ua_w_k * (ambient_c - T) * dt
        new.h += q_amb
        self.contents = new

    def floor_leak_rate(self, hole_area_m2: float, cd: float = 0.62, g: float = 9.80665) -> float:
        """Mass rate through a floor leak (Torricelli orifice), kg/s."""
        if hole_area_m2 <= 0 or self.level <= 0:
            return 0.0
        return cd * hole_area_m2 * math.sqrt(2.0 * g * self.level) * self.density_obs
