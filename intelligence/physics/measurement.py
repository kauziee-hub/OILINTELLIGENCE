"""Measurement model: raw telemetry -> engineering quantities.

This layer only uses information a real plant has (telemetry + operational
records). It never touches simulator ground truth.

* Tank inventory:  level -> observed volume (strapping, flat-bottom cylinder)
                   -> standard volume V15 = V_obs * VCF(T_tank)
                   -> mass = V15 * rho15_est(t)
  rho15_est(t) starts from the latest lab sample and is updated by mixing with
  metered receipts (DT density), because receipts of a different crude change
  the tank density between lab samples.
* Meter mass: gross volume increments from the device totalizer (robust to
  communication dropouts), converted with CTL(T_line) and base density from the
  meter's associated TT and DT instruments (flow-computer method).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from industrial.schemas.plant import PlantConfig

EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


def to_epoch(ts) -> np.ndarray | float:
    if isinstance(ts, (pd.Series, pd.Index, np.ndarray, list)):
        s = pd.to_datetime(pd.Series(ts), utc=True)
        return ((s - EPOCH).dt.total_seconds()).to_numpy(dtype=float)
    p = pd.Timestamp(ts)
    p = p.tz_convert("UTC") if p.tzinfo else p.tz_localize("UTC")
    return (p - EPOCH).total_seconds()


def from_epoch(x: float) -> pd.Timestamp:
    return EPOCH + pd.Timedelta(seconds=float(x))


@dataclass
class Series:
    t: np.ndarray
    v: np.ndarray
    good: np.ndarray

    def __len__(self) -> int:
        return len(self.t)


class TelemetryStore:
    """In-memory columnar index of telemetry: (sensor_id, parameter) -> Series."""

    def __init__(self, df: pd.DataFrame):
        if "ts" not in df.columns:
            df = df.copy()
            df["ts"] = to_epoch(df["timestamp"]) if "timestamp" in df.columns else df["t"].astype(float)
        df = df.sort_values("ts", kind="stable")
        self.series: dict[tuple[str, str], Series] = {}
        for (sid, par), g in df.groupby(["sensor_id", "parameter"], sort=False):
            v = pd.to_numeric(g["value"], errors="coerce").to_numpy(dtype=float)
            good = (g["quality"].to_numpy() != "BAD") & np.isfinite(v)
            self.series[(sid, par)] = Series(g["ts"].to_numpy(dtype=float), v, good)
        self.t_min = float(df["ts"].min()) if len(df) else 0.0
        self.t_max = float(df["ts"].max()) if len(df) else 0.0

    def get(self, sensor: str, parameter: str) -> Series | None:
        return self.series.get((sensor, parameter))

    def window(self, sensor: str, parameter: str, t0: float, t1: float) -> Series:
        s = self.get(sensor, parameter)
        if s is None:
            return Series(np.array([]), np.array([]), np.array([], dtype=bool))
        i0, i1 = np.searchsorted(s.t, [t0, t1], side="left")
        return Series(s.t[i0:i1], s.v[i0:i1], s.good[i0:i1])

    def value_at(self, sensor: str, parameter: str, t: float, half_window: float = 60.0,
                 max_gap: float = 900.0) -> float:
        """Robust (mean) value around t; falls back to last good value within max_gap."""
        s = self.get(sensor, parameter)
        if s is None or len(s) == 0:
            return math.nan
        i0, i1 = np.searchsorted(s.t, [t - half_window, t + half_window], side="left")
        sel = s.good[i0:i1]
        if sel.any():
            return float(np.mean(s.v[i0:i1][sel]))
        j = np.searchsorted(s.t, t, side="right") - 1
        while j >= 0 and t - s.t[j] <= max_gap:
            if s.good[j]:
                return float(s.v[j])
            j -= 1
        return math.nan

    def value_at_linear(self, sensor: str, parameter: str, t: float, half_window: float = 60.0,
                        max_gap: float = 900.0) -> float:
        """Value at exactly t from a local straight-line fit over +-half_window. While a tank fills or
        empties, a plain mean of unevenly spaced samples is biased toward where the samples happen to
        sit; the fit's intercept at t is not. Falls back to `value_at` with fewer than 2 samples."""
        s = self.get(sensor, parameter)
        if s is None or len(s) == 0:
            return math.nan
        i0, i1 = np.searchsorted(s.t, [t - half_window, t + half_window], side="left")
        sel = s.good[i0:i1]
        if int(sel.sum()) < 2:
            return self.value_at(sensor, parameter, t, half_window, max_gap)
        x = s.t[i0:i1][sel] - t
        y = s.v[i0:i1][sel]
        if float(np.ptp(x)) < 1e-6:
            return float(np.mean(y))
        b = float(np.polyfit(x, y, 1)[0])
        return float(np.mean(y) - b * np.mean(x))

    def last_before(self, sensor: str, parameter: str, t: float) -> float:
        s = self.get(sensor, parameter)
        if s is None or len(s) == 0:
            return math.nan
        j = np.searchsorted(s.t, t, side="right") - 1
        return float(s.v[j]) if j >= 0 else math.nan

    def any_above(self, sensor: str, parameter: str, t0: float, t1: float, thr: float) -> bool:
        w = self.window(sensor, parameter, t0, t1)
        prev = self.last_before(sensor, parameter, t0)
        return bool((w.v[w.good] > thr).any()) or (not math.isnan(prev) and prev > thr)

    def mean(self, sensor: str, parameter: str, t0: float, t1: float) -> float:
        w = self.window(sensor, parameter, t0, t1)
        return float(np.mean(w.v[w.good])) if w.good.any() else math.nan

    def median(self, sensor: str, parameter: str, t0: float, t1: float) -> float:
        w = self.window(sensor, parameter, t0, t1)
        return float(np.median(w.v[w.good])) if w.good.any() else math.nan

    def coverage(self, sensor: str, parameter: str, t0: float, t1: float, expected_interval: float) -> float:
        w = self.window(sensor, parameter, t0, t1)
        n_exp = max((t1 - t0) / max(expected_interval, 1e-6), 1.0)
        return float(min(w.good.sum() / n_exp, 1.0))


# ---------------------------------------------------------------------------
# Records view
# ---------------------------------------------------------------------------
@dataclass
class RecordsView:
    transfers: pd.DataFrame = field(default_factory=pd.DataFrame)
    lab_samples: pd.DataFrame = field(default_factory=pd.DataFrame)
    calibrations: pd.DataFrame = field(default_factory=pd.DataFrame)
    maintenance: pd.DataFrame = field(default_factory=pd.DataFrame)
    operator_log: pd.DataFrame = field(default_factory=pd.DataFrame)

    def __post_init__(self):
        if len(self.transfers):
            tr = self.transfers.copy()
            tr["start_ts"] = to_epoch(tr["start_time"])
            tr["end_ts"] = [to_epoch(x) if (x is not None and not pd.isna(x)) else math.inf
                            for x in tr["end_time"]]
            self.transfers = tr
        if len(self.lab_samples):
            ls = self.lab_samples.copy()
            ls["ts"] = to_epoch(ls["timestamp"])
            self.lab_samples = ls.sort_values("ts")

    def lab_density(self, tank: str, t: float) -> tuple[float, float]:
        """Latest lab density at or before t -> (rho15, sample_ts)."""
        if not len(self.lab_samples):
            return math.nan, math.nan
        d = self.lab_samples[(self.lab_samples["tank_id"] == tank) & (self.lab_samples["ts"] <= t)]
        if not len(d):
            d = self.lab_samples[self.lab_samples["tank_id"] == tank]
            if not len(d):
                return math.nan, math.nan
            r = d.iloc[0]
        else:
            r = d.iloc[-1]
        return float(r["density_15c"]), float(r["ts"])

    def covered_intervals(self, meter: str, plant: PlantConfig, t0: float, t1: float) -> list[tuple[float, float]]:
        """Time intervals in [t0,t1] covered by a transfer record whose route uses `meter`."""
        out = []
        if not len(self.transfers):
            return out
        for _, r in self.transfers.iterrows():
            route = plant.routes.get(r["route"])
            if route is None or meter not in route.meters:
                continue
            a, b = max(t0, r["start_ts"]), min(t1, r["end_ts"])
            if b > a:
                out.append((a, b))
        return out


# ---------------------------------------------------------------------------
# Measurement model
# ---------------------------------------------------------------------------
REF_T = 15.0


class MeasurementModel:
    def __init__(self, plant: PlantConfig, store: TelemetryStore, records: RecordsView):
        self.plant = plant
        self.store = store
        self.records = records
        self.alpha = {tid: plant.products[t.initial_product].thermal_expansion_coeff
                      for tid, t in plant.tanks.items()}
        self.receipt_meter = {"TK-002": "FM-002", "TK-003": "FM-003"}
        self._rho_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        # meter factors from approved proving records (flow-computer K-factor correction), 1.0 = none
        self.meter_factors: dict[str, float] = {}

    # -------------------------------------------------------------- tanks
    def tank_level(self, tank: str, t: float) -> float:
        return self.store.value_at_linear(self.plant.tank_level_sensor(tank), "level", t, half_window=60.0)

    def tank_temperature(self, tank: str, t: float) -> float:
        return self.store.value_at(self.plant.tank_temperature_sensor(tank), "temperature", t, half_window=90.0)

    def _rho_track(self, tank: str) -> tuple[np.ndarray, np.ndarray]:
        """Piecewise tank base-density estimate driven by lab samples and metered receipts."""
        if tank in self._rho_cache:
            return self._rho_cache[tank]
        st = self.store
        rho0, _ = self.records.lab_density(tank, st.t_min)
        if math.isnan(rho0):
            rho0 = self.plant.products[self.plant.tanks[tank].initial_product].density_15c
        meter = self.receipt_meter.get(tank)
        grid = np.arange(st.t_min, st.t_max + 60.0, 60.0)
        rho = np.full(len(grid), rho0)
        if meter is not None:
            cfg = self.plant.sensors[meter]
            area = self.plant.tanks[tank].area_m2
            cur = rho0
            labs = self.records.lab_samples
            lab_t = labs[labs["tank_id"] == tank][["ts", "density_15c"]].to_numpy() if len(labs) else np.zeros((0, 2))
            li = 0
            while li < len(lab_t) and lab_t[li][0] <= grid[0]:
                li += 1
            for k in range(1, len(grid)):
                a, b = grid[k - 1], grid[k]
                while li < len(lab_t) and lab_t[li][0] <= b:
                    cur = float(lab_t[li][1])
                    li += 1
                dv = self._gross_increment(meter, a, b)
                if dv > 1e-6:
                    tl = st.value_at(cfg.temperature_sensor, "temperature", b, 120)
                    rin = st.value_at(cfg.density_sensor, "density_15c", b, 120)
                    alpha = self.alpha[tank]
                    v15_in = dv / (1.0 + alpha * (tl - REF_T)) if not math.isnan(tl) else dv
                    lvl = st.value_at(self.plant.tank_level_sensor(tank), "level", a, 60)
                    tt = st.value_at(self.plant.tank_temperature_sensor(tank), "temperature", a, 120)
                    if not (math.isnan(lvl) or math.isnan(rin)):
                        v15_tank = area * lvl / (1.0 + alpha * ((tt if not math.isnan(tt) else 25.0) - REF_T))
                        cur = (cur * v15_tank + rin * v15_in) / (v15_tank + v15_in)
                rho[k] = cur
        self._rho_cache[tank] = (grid, rho)
        return grid, rho

    def tank_rho15(self, tank: str, t: float) -> float:
        grid, rho = self._rho_track(tank)
        i = int(np.clip(np.searchsorted(grid, t, side="right") - 1, 0, len(grid) - 1))
        return float(rho[i])

    def rho_end_by_mixing(self, tank: str, t0: float, t1: float, rho_start: float) -> float:
        """Density at t1 propagated from rho_start using only metered receipts in [t0, t1].

        Lab samples arriving inside the window are information updates, not physical
        changes, so they must not create an inventory step inside the window.
        """
        meter = self.receipt_meter.get(tank)
        if meter is None or not self.meter_active(meter, t0, t1):
            return rho_start
        cfg = self.plant.sensors[meter]
        alpha = self.alpha[tank]
        lvl = self.tank_level(tank, t0)
        tt = self.tank_temperature(tank, t0)
        if math.isnan(lvl):
            return rho_start
        v15_tank = self.plant.tanks[tank].area_m2 * lvl / (1.0 + alpha * ((tt if not math.isnan(tt) else 25.0) - REF_T))
        num, den = rho_start * v15_tank, v15_tank
        edges = np.arange(t0, t1 + 1e-6, 120.0)
        if edges[-1] < t1:
            edges = np.append(edges, t1)
        for a, b in zip(edges[:-1], edges[1:]):
            dv = self._gross_increment(meter, a, b)
            if dv <= 1e-6:
                continue
            tm = 0.5 * (a + b)
            tl = self.store.value_at(cfg.temperature_sensor, "temperature", tm, 120)
            rin = self.store.value_at(cfg.density_sensor, "density_15c", tm, 120)
            if math.isnan(rin):
                rin = self.store.last_before(cfg.density_sensor, "density_15c", tm)
            if math.isnan(rin):
                continue
            v15_in = dv / (1.0 + alpha * ((tl if not math.isnan(tl) else 25.0) - REF_T))
            num += rin * v15_in
            den += v15_in
        return num / den if den > 0 else rho_start

    def tank_inventory(self, tank: str, t: float, rho15: float | None = None) -> dict[str, float]:
        cfg = self.plant.tanks[tank]
        lvl = self.tank_level(tank, t)
        temp = self.tank_temperature(tank, t)
        rho15 = self.tank_rho15(tank, t) if rho15 is None else rho15
        alpha = self.alpha[tank]
        v_obs = cfg.area_m2 * lvl
        vcf = 1.0 / (1.0 + alpha * (temp - REF_T))
        v15 = v_obs * vcf
        mass_t = v15 * rho15 / 1000.0
        rho_obs = rho15 * vcf
        return {"level_m": lvl, "temperature_c": temp, "rho15": rho15, "v_obs_m3": v_obs, "v15_m3": v15,
                "mass_t": mass_t, "rho_obs": rho_obs, "area_m2": cfg.area_m2, "alpha": alpha}

    # -------------------------------------------------------------- meters
    def _gross_increment(self, meter: str, t0: float, t1: float) -> float:
        return self._raw_gross_increment(meter, t0, t1) * self.meter_factors.get(meter, 1.0)

    def _raw_gross_increment(self, meter: str, t0: float, t1: float) -> float:
        tot = self.store.get(meter, "volume_total")
        if tot is not None and len(tot) > 1:
            a = np.interp(t0, tot.t, tot.v) if tot.t[0] <= t0 else tot.v[0]
            b = np.interp(t1, tot.t, tot.v) if tot.t[-1] >= t1 else tot.v[-1]
            if tot.t[0] <= t1 and tot.t[-1] >= t0:
                return max(float(b - a), 0.0)
        w = self.store.window(meter, "volume_flow", t0, t1)
        if len(w) < 2:
            return 0.0
        return float(np.trapezoid(np.where(w.good, w.v, 0.0), w.t) / 3600.0)

    def boundary_sampling_sd(self, meter: str, times: list[float], t0: float, t1: float,
                             reference_spacing_s: float = 10.0) -> float:
        """1-sigma totalizer interpolation error (t) at abrupt flow changes (transfer start/stop).

        A record boundary at fraction f of a historian interval (h seconds) splits that interval's
        gross increment dV by linear interpolation (f * dV), but when flow started or stopped inside
        the interval is unknown, so the true split can be anywhere in [0, dV]. Taking it as uniform,
        the interpolation error has mean (1/2 - f) dV and sd dV/sqrt 12; both enter as an RMS error,
        because the bias is systematic at start/stop ramps. dV is the increment observed across the
        bracketing samples, not a typical flow rate, so a start-up ramp that no flow sample inside
        the window has seen yet is still covered.
        Only the spacing beyond the calibration data's own spacing is counted - that part of the
        effect is already contained in the empirically calibrated ticket / ledger terms."""
        s = self.store.get(meter, "volume_total") or self.store.get(meter, "volume_flow")
        if s is None or len(s) < 2 or not times:
            return 0.0
        var = 0.0
        for tb in times:
            i = int(np.searchsorted(s.t, tb))
            if not 0 < i < len(s.t):
                continue
            a, b = float(s.t[i - 1]), float(s.t[i])
            h = b - a
            if h <= reference_spacing_s:
                continue
            dm = self._gross_increment(meter, a, b) * 0.86            # m3 -> t (crude density ~0.86 t/m3)
            f = (tb - a) / h
            var += (dm * (h - reference_spacing_s) / h) ** 2 * ((0.5 - f) ** 2 + 1.0 / 12.0)
        return math.sqrt(var)

    def meter_covers(self, meter: str, t0: float, t1: float, tol: float = 120.0) -> bool:
        """True when the meter's totalizer (or flow) series spans [t0, t1]."""
        for par in ("volume_total", "volume_flow"):
            s = self.store.get(meter, par)
            if s is not None and len(s) > 1:
                return s.t[0] <= t0 + tol and s.t[-1] >= t1 - tol
        return False

    def meter_mass(self, meter: str, t0: float, t1: float, step: float = 60.0) -> dict[str, float]:
        """Net mass through a meter over [t0, t1] (flow-computer method), tonnes."""
        cfg = self.plant.sensors[meter]
        alpha = self.alpha["TK-001"]
        n = max(int(math.ceil((t1 - t0) / step)), 1)
        edges = np.linspace(t0, t1, n + 1)
        mass = 0.0
        gross = 0.0
        tw = 0.0
        rw = 0.0
        for a, b in zip(edges[:-1], edges[1:]):
            dv = self._gross_increment(meter, a, b)
            if dv <= 0:
                continue
            tm = 0.5 * (a + b)
            tl = self.store.value_at(cfg.temperature_sensor, "temperature", tm, 90)
            rho15 = self.store.value_at(cfg.density_sensor, "density_15c", tm, 120)
            # instrument unavailable (dropout): hold the last known value, as a flow computer would
            if math.isnan(tl):
                tl = self.store.last_before(cfg.temperature_sensor, "temperature", tm)
            if math.isnan(rho15):
                rho15 = self.store.last_before(cfg.density_sensor, "density_15c", tm)
            if math.isnan(tl) or math.isnan(rho15):
                continue
            ctl = 1.0 / (1.0 + alpha * (tl - REF_T))
            mass += dv * ctl * rho15 / 1000.0
            gross += dv
            tw += dv * tl
            rw += dv * rho15
        return {"mass_t": mass, "gross_m3": gross,
                "t_line_c": tw / gross if gross > 0 else math.nan,
                "rho15": rw / gross if gross > 0 else math.nan}

    def meter_mass_uncovered(self, meter: str, t0: float, t1: float) -> float:
        """Metered mass during times NOT covered by a transfer record (unbooked movement)."""
        covered = self.records.covered_intervals(meter, self.plant, t0, t1)
        total = self.meter_mass(meter, t0, t1)["mass_t"]
        cov = sum(self.meter_mass(meter, a, b)["mass_t"] for a, b in covered)
        return max(total - cov, 0.0)

    def meter_active(self, meter: str, t0: float, t1: float) -> bool:
        return self._gross_increment(meter, t0, t1) > 0.05

    # -------------------------------------------------------------- states
    def valve_open(self, valve: str, t0: float, t1: float) -> bool:
        return self.store.any_above(f"{valve}-ZC", "command", t0, t1, 0.5) or \
            self.store.any_above(f"{valve}-ZT", "position", t0, t1, 1.0)

    def pump_running_fraction(self, pump: str, t0: float, t1: float) -> float:
        w = self.store.window(f"{pump}-ST", "status", t0, t1)
        prev = self.store.last_before(f"{pump}-ST", "status", t0)
        vals = np.concatenate([[prev] if not math.isnan(prev) else [], w.v])
        if not len(vals):
            return 0.0
        sp = self.store.window(f"{pump}-SP", "speed", t0, t1)
        if len(sp):
            return float(np.mean(sp.v > 0.9 * self.plant.pumps[pump].rated_speed_rpm))
        return float(np.mean(vals >= 0.99))

    def pressure_mean(self, sensor: str, t0: float, t1: float) -> float:
        par = self.plant.sensors[sensor].parameter
        return self.store.mean(sensor, par, t0, t1)
