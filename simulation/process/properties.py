"""Crude oil physical properties and stream mixing algebra.

Density/temperature model (simplified volume correction, section 4.3):

    rho_T = rho_15 / (1 + alpha (T - T_ref))      V_15 = V_T / (1 + alpha (T - T_ref))

A future upgrade should implement API MPMS Ch. 11.1 VCF tables; the interface
(`density_at`, `vcf`) is designed so that the correlation can be swapped.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

T_REF = 15.0


@dataclass(frozen=True)
class CrudeProperties:
    density_15c: float
    alpha: float
    nu15_cst: float
    nu_b: float
    cp: float

    def density_at(self, temperature_c: float) -> float:
        return self.density_15c / (1.0 + self.alpha * (temperature_c - T_REF))

    def vcf(self, temperature_c: float) -> float:
        """Volume correction factor: V_15 = V_obs * vcf(T)."""
        return 1.0 / (1.0 + self.alpha * (temperature_c - T_REF))

    def kinematic_viscosity(self, temperature_c: float) -> float:
        """Kinematic viscosity in m2/s."""
        return self.nu15_cst * 1e-6 * math.exp(-self.nu_b * (temperature_c - T_REF))


def density_at(rho15: float, alpha: float, temperature_c: float) -> float:
    return rho15 / (1.0 + alpha * (temperature_c - T_REF))


def vcf(alpha: float, temperature_c: float) -> float:
    return 1.0 / (1.0 + alpha * (temperature_c - T_REF))


@dataclass
class Stream:
    """Extensive stream quantities (rates) that mix additively.

    m   : mass rate, kg/s
    v15 : standard volume rate at 15 degC, m3/s
    h   : enthalpy rate relative to 0 degC, W  (= m cp T)
    a_v : sum(alpha * v15)            -> mixture alpha = a_v / v15
    lnnu_m : sum(ln(nu15) * m)        -> mixture viscosity (log mixing)
    b_m : sum(viscosity temp coeff * m)
    cp_m: sum(cp * m)
    """

    m: float = 0.0
    v15: float = 0.0
    h: float = 0.0
    a_v: float = 0.0
    lnnu_m: float = 0.0
    b_m: float = 0.0
    cp_m: float = 0.0

    @classmethod
    def from_props(cls, props: CrudeProperties, temperature_c: float, mass_rate: float) -> "Stream":
        v15 = mass_rate / props.density_15c
        return cls(
            m=mass_rate,
            v15=v15,
            h=mass_rate * props.cp * temperature_c,
            a_v=props.alpha * v15,
            lnnu_m=math.log(props.nu15_cst) * mass_rate,
            b_m=props.nu_b * mass_rate,
            cp_m=props.cp * mass_rate,
        )

    def __add__(self, o: "Stream") -> "Stream":
        return Stream(
            self.m + o.m, self.v15 + o.v15, self.h + o.h, self.a_v + o.a_v,
            self.lnnu_m + o.lnnu_m, self.b_m + o.b_m, self.cp_m + o.cp_m,
        )

    def scaled(self, f: float) -> "Stream":
        return Stream(
            self.m * f, self.v15 * f, self.h * f, self.a_v * f,
            self.lnnu_m * f, self.b_m * f, self.cp_m * f,
        )

    def add_heat(self, watts: float) -> "Stream":
        s = Stream(**self.__dict__)
        s.h += watts
        return s

    # ---------------------------------------------------------------- intensive
    @property
    def empty(self) -> bool:
        return self.m <= 1e-12

    @property
    def rho15(self) -> float:
        return self.m / self.v15 if self.v15 > 0 else float("nan")

    @property
    def alpha(self) -> float:
        return self.a_v / self.v15 if self.v15 > 0 else float("nan")

    @property
    def cp(self) -> float:
        return self.cp_m / self.m if self.m > 0 else float("nan")

    @property
    def temperature(self) -> float:
        return self.h / self.cp_m if self.cp_m > 0 else float("nan")

    def props(self) -> CrudeProperties:
        return CrudeProperties(
            density_15c=self.rho15,
            alpha=self.alpha,
            nu15_cst=math.exp(self.lnnu_m / self.m),
            nu_b=self.b_m / self.m,
            cp=self.cp,
        )

    def line_density(self) -> float:
        return density_at(self.rho15, self.alpha, self.temperature)

    def line_volume_rate(self) -> float:
        """Volumetric flow at line conditions, m3/s."""
        if self.empty:
            return 0.0
        return self.m / self.line_density()
