"""Window mass-balance engine with a linearised measurement-error model.

For a set of consecutive windows [t_0, t_1, ..., t_W] the engine produces
evidence rows, each linear in a set of measurement-error sources e:

    r = r_true + J e ,   e ~ N(0, diag(sigma^2))        (section 4.7)

Row kinds
  CV     : control-volume mass balance (tonnes)            R = M_in - M_out - dM
  LEDGER : metered mass not covered by any transfer record (tonnes)
  TCONS  : line temperature vs. source-tank temperature (degC)
  DCONS  : line density meter vs. source-tank density estimate (kg/m3)
  PUMP   : measured pump head - curve head at measured flow (m)

Error sources are keyed so that systematic errors are shared across windows
(e.g. "FM-002:sys") while reading errors are keyed by time point (e.g.
"LT-002:read@3", shared by the two windows adjacent to boundary 3). The
resulting covariance J Sigma J^T therefore carries the correct cross-window
correlation, which the attribution engine relies on.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from industrial.schemas.plant import PlantConfig
from intelligence.mass_balance.topology import SubSegment, Topology, TopologyBuilder
from intelligence.physics.measurement import MeasurementModel

G = 9.80665
CP = 1950.0


@dataclass
class UncertaintyConfig:
    """1-sigma error model. Values calibrated on 24 normal-operation runs so that
    per-row-kind z-scores have unit variance (experiments/uncertainty)."""
    meter_rep_rel: float = 0.0006
    level_read_m: float = 0.00102
    tank_temp_read_c: float = 0.02
    strap_rel: float = 0.0004
    tank_rho_base: float = 0.005
    cv_model_abs_t: float = 0.05
    cv_model_rel: float = 1.2e-4
    cv_tank_model_t: float = 0.05
    ledger_model_t: float = 0.02
    ledger_model_rel: float = 0.0005
    tcons_model_c: float = 0.08
    # density consistency: a short-term term per window plus the reference-density error, which is
    # SHARED by every window until the next lab sample (lab repeatability ~0.3 kg/m3 + mixing model)
    dcons_model_kgm3: float = 0.12
    dcons_reference_kgm3: float = 0.32
    pump_model_m: float = 0.38
    ticket_model_t: float = 0.012
    ticket_model_rel: float = 0.0002
    # sample spacing of the calibration data; coarser historian sampling adds totalizer
    # interpolation error at transfer start/stop (see MeasurementModel.boundary_sampling_sd)
    calibration_sample_s: float = 10.0


@dataclass
class Row:
    row_id: str
    window: int
    kind: str
    key: str
    value: float
    unit: str
    sens: dict[str, float] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class WindowEvidence:
    index: int
    t0: float
    t1: float
    topology: Topology
    rows: list[Row]
    meter_mass: dict[str, dict[str, float]]
    unbooked: dict[str, float]
    meter_sign: dict[str, dict[str, int]]          # cv -> meter -> sign
    pipe_pressure: dict[str, float]
    tank_level: dict[str, float]
    pump_running: dict[str, float]
    pump_curve_head: dict[str, float]
    pipe_sqrtp: dict[str, float]      # mean sqrt(max(p,0)/5 bar) over window (orifice-law leak scaling)
    valve_dp_term: dict[str, float]   # mean sign(dp) sqrt(|dp|/5 bar) across shut valves (line - tank side)
    source_tank: str | None
    route_guess: str | None
    quality: dict[str, dict[str, Any]]
    # meters with data for the whole window that registered no flow -> low-flow cut-off (m3/h).
    # A silent meter is not a cut, but it bounds any unmetered flow through it.
    silent_meters: dict[str, float] = field(default_factory=dict)

    @property
    def duration_h(self) -> float:
        return (self.t1 - self.t0) / 3600.0


@dataclass
class Evidence:
    t_edges: list[float]
    windows: list[WindowEvidence]
    inventories: list[dict[str, dict[str, float]]]   # per boundary k: tank -> inventory
    sigma: dict[str, float]
    # self-calibration (adaptive layer): per row-kind standard-deviation scale, 1.0 = as calibrated
    kind_scale: dict[str, float] = field(default_factory=dict)

    @property
    def rows(self) -> list[Row]:
        return [r for w in self.windows for r in w.rows]

    def summary(self) -> dict[str, Any]:
        cv = [r for r in self.rows if r.kind == "CV"]
        fac = sum(r.value for r in cv)
        return {"windows": len(self.windows), "t0": self.t_edges[0], "t1": self.t_edges[-1],
                "facility_residual_t": fac, "rows": len(self.rows)}


class BalanceEngine:
    def __init__(self, plant: PlantConfig, mm: MeasurementModel, ucfg: UncertaintyConfig | None = None):
        self.plant = plant
        self.mm = mm
        self.u = ucfg or UncertaintyConfig()
        self.kind_scale: dict[str, float] = {}
        self.topo = TopologyBuilder(plant)
        self.level_sensor = {t: plant.tank_level_sensor(t) for t in plant.tanks}
        self.temp_sensor = {t: plant.tank_temperature_sensor(t) for t in plant.tanks}
        self.pipe_pt = {s.asset: sid for sid, s in plant.sensors.items()
                        if s.type == "pressure" and s.asset in plant.pipelines}
        self.stuck_min_s = 600.0     # frozen value for >= 10 min
        self.dropout_min_s = 600.0   # no samples for >= 10 min

    def _read_sd(self, mm, sensor: str, parameter: str, t: float, half_window: float, calibrated_sd: float) -> float:
        """1-sigma of an averaged gauge read at t. The calibrated value holds for the calibration
        sample spacing; fewer samples in the averaging window add white-noise variance
        noise^2 (1/n - 1/n_ref) (correlated AR noise does not average and is already included)."""
        n = int(np.sum(mm.store.window(sensor, parameter, t - half_window, t + half_window).good))
        n_ref = 2.0 * half_window / self.u.calibration_sample_s + 1.0
        white = float(getattr(self.plant.sensors[sensor], "noise", 0.0) or 0.0)
        return math.sqrt(calibrated_sd ** 2 + white ** 2 * max(1.0 / max(n, 1) - 1.0 / n_ref, 0.0))

    # ------------------------------------------------------------------ main
    def evidence(self, t_edges: list[float]) -> Evidence:
        P = self.plant
        mm = self.mm
        inv = [{t: mm.tank_inventory(t, tk) for t in P.tanks} for tk in t_edges]
        sigma: dict[str, float] = {}
        for sid, s in P.sensors.items():
            if s.type == "flow":
                sigma[f"{sid}:sys"] = s.accuracy_rel or 0.0025
            elif s.type == "temperature":
                sigma[f"{sid}:sys"] = s.accuracy or 0.2
            elif s.type == "density":
                sigma[f"{sid}:sys"] = s.accuracy or 0.5
        for t in P.tanks:
            sigma[f"{t}:strap"] = self.u.strap_rel
        windows = []
        for w, (a, b) in enumerate(zip(t_edges[:-1], t_edges[1:])):
            # end-of-window inventory uses density propagated by physical mixing only
            end = {}
            for t in P.tanks:
                rho_end = mm.rho_end_by_mixing(t, a, b, inv[w][t]["rho15"])
                end[t] = inv[w + 1][t] if abs(rho_end - inv[w + 1][t]["rho15"]) < 1e-9 else \
                    mm.tank_inventory(t, b, rho15=rho_end)
            windows.append(self._window(w, a, b, inv[w], end, sigma))
        return Evidence(t_edges=list(t_edges), windows=windows, inventories=inv, sigma=sigma,
                        kind_scale=dict(self.kind_scale))

    # ---------------------------------------------------------------- window
    def _window(self, w: int, t0: float, t1: float, inv0: dict, inv1: dict, sigma: dict) -> WindowEvidence:
        P, mm, u = self.plant, self.mm, self.u
        meters = list(P.meters())
        active = {m for m in meters if mm.meter_active(m, t0, t1)}
        open_v = {v for v in P.valves if mm.valve_open(v, t0, t1)}
        topo = self.topo.build(open_v, active)
        mmass = {m: mm.meter_mass(m, t0, t1) for m in active}
        unbooked = {m: mm.meter_mass_uncovered(m, t0, t1) for m in active}
        rows: list[Row] = []
        meter_sign: dict[str, dict[str, int]] = {}
        quality = self._quality(t0, t1, active)

        # ---------------- CV rows
        for cv in topo.cvs.values():
            if not cv.observable:
                continue
            sens: dict[str, float] = {}
            val = 0.0
            thr = 0.0
            ms = {}
            for m in sorted(set(cv.meters_in + cv.meters_out)):
                sg = cv.meters_in.count(m) - cv.meters_out.count(m)
                if sg == 0:
                    continue  # both sides of the meter lie inside this CV: internal, cancels
                ms[m] = sg
                M = mmass[m]["mass_t"]
                rho = mmass[m]["rho15"]
                val += sg * M
                thr += abs(M)
                cfg = P.sensors[m]
                alpha = mm.alpha["TK-001"]
                sens[f"{m}:sys"] = sens.get(f"{m}:sys", 0.0) + sg * M
                key = f"{m}:rep:w{w}"
                sens[key] = sg * M
                sigma[key] = u.meter_rep_rel
                sens[f"{cfg.temperature_sensor}:sys"] = sens.get(f"{cfg.temperature_sensor}:sys", 0.0) - sg * alpha * M
                if rho and not math.isnan(rho):
                    sens[f"{cfg.density_sensor}:sys"] = sens.get(f"{cfg.density_sensor}:sys", 0.0) + sg * M / rho
            meter_sign[cv.cv_id] = ms
            for tk in cv.tanks:
                a, b = inv0[tk], inv1[tk]
                dM = b["mass_t"] - a["mass_t"]
                val -= dM
                thr += abs(dM)
                lt, tt = self.level_sensor[tk], self.temp_sensor[tk]
                k0, k1 = f"{lt}:read@{w}", f"{lt}:read@{w + 1}"
                sens[k1] = sens.get(k1, 0.0) - b["area_m2"] * b["rho_obs"] / 1000.0
                sens[k0] = sens.get(k0, 0.0) + a["area_m2"] * a["rho_obs"] / 1000.0
                sigma[k0] = self._read_sd(mm, lt, "level", t0, 60.0, u.level_read_m)
                sigma[k1] = self._read_sd(mm, lt, "level", t1, 60.0, u.level_read_m)
                j0, j1 = f"{tt}:read@{w}", f"{tt}:read@{w + 1}"
                vcf1 = 1.0 / (1.0 + b["alpha"] * (b["temperature_c"] - 15.0))
                vcf0 = 1.0 / (1.0 + a["alpha"] * (a["temperature_c"] - 15.0))
                sens[j1] = sens.get(j1, 0.0) + b["alpha"] * b["mass_t"] * vcf1
                sens[j0] = sens.get(j0, 0.0) - a["alpha"] * a["mass_t"] * vcf0
                sigma[j0] = self._read_sd(mm, tt, "temperature", t0, 90.0, u.tank_temp_read_c)
                sigma[j1] = self._read_sd(mm, tt, "temperature", t1, 90.0, u.tank_temp_read_c)
                sens[f"{tk}:strap"] = -dM
                rk = f"{tk}:rho:w{w}"
                sens[rk] = -b["v15_m3"] / 1000.0
                sigma[rk] = u.tank_rho_base + 0.1 * abs(b["rho15"] - a["rho15"])
            mk = f"{cv.cv_id}:model:w{w}"
            sens[mk] = 1.0
            sigma[mk] = u.cv_model_abs_t + u.cv_model_rel * thr + u.cv_tank_model_t * len(cv.tanks)
            if not math.isnan(val):
                rows.append(Row(f"w{w}|{cv.cv_id}", w, "CV", cv.cv_id, val, "t", sens,
                                {"tanks": cv.tanks, "meters_in": cv.meters_in, "meters_out": cv.meters_out,
                                 "segments": [s.id for s in cv.segments], "throughput_t": thr}))

        # ---------------- ledger rows (metered but unbooked movement)
        for m in sorted(active):
            M = mmass[m]["mass_t"]
            if M < 0.05:
                continue
            L = unbooked[m]
            key = f"LEDGER:{m}:model:w{w}"
            edges = [x for ab in mm.records.covered_intervals(m, P, t0, t1) for x in ab if t0 < x < t1]
            samp = mm.boundary_sampling_sd(m, edges, t0, t1, u.calibration_sample_s)
            sigma[key] = math.hypot(u.ledger_model_t + u.ledger_model_rel * M, samp)
            rows.append(Row(f"w{w}|LEDGER:{m}", w, "LEDGER", m, L, "t", {f"{m}:sys": L, key: 1.0},
                            {"metered_t": M}))

        # ---------------- ticket rows (transfer reconciliation: booked vs custody meter)
        tr = mm.records.transfers
        if len(tr):
            for _, rec in tr.iterrows():
                if rec.get("status") != "COMPLETED" or not (t0 < rec["end_ts"] <= t1):
                    continue
                qty, meter = rec.get("recorded_quantity"), rec.get("custody_meter")
                if qty is None or pd.isna(qty) or meter not in P.sensors:
                    continue
                # a ticket covers the whole transfer: reconcile it only when the meter data
                # spans the full movement, otherwise the partial total is a spurious mismatch
                if not mm.meter_covers(meter, rec["start_ts"], rec["end_ts"]):
                    continue
                metered = mm.meter_mass(meter, rec["start_ts"], rec["end_ts"])["mass_t"]
                key = f"TICKET:{rec['transfer_id']}:model"
                samp = mm.boundary_sampling_sd(meter, [rec["start_ts"], rec["end_ts"]], rec["start_ts"], rec["end_ts"],
                                               u.calibration_sample_s)
                sigma[key] = math.hypot(u.ticket_model_t + u.ticket_model_rel * metered, samp)
                rows.append(Row(f"w{w}|TICKET:{rec['transfer_id']}", w, "TICKET", str(rec["transfer_id"]),
                                float(qty) - metered, "t", {key: 1.0},
                                {"recorded_t": float(qty), "metered_t": metered, "custody_meter": meter}))

        # ---------------- source / route inference (from SCADA states only)
        # route identification uses the explicit route measurement configuration; a meter
        # registering flow on its own (e.g. through a passing valve) does not define a route
        source = None
        route_guess = None
        for rid, r in P.routes.items():
            if set(r.open_valves) <= open_v and all(m in active for m in r.meters) and all(
                    self._flow_fraction(m, t0, t1) > 0.3 for m in r.meters):
                route_guess = rid
        if route_guess is not None:
            source = P.routes[route_guess].source

        # ---------------- temperature consistency rows
        dur = t1 - t0
        if source == "TK-001" and "FM-001" in active:
            frac = self._flow_fraction("FM-001", t0, t1)
            if frac > 0.8:
                tl = mm.store.median("TT-004", "temperature", t0, t1)
                ts = mm.store.median(self.temp_sensor["TK-001"], "temperature", t0, t1)
                if not (math.isnan(tl) or math.isnan(ts)):
                    key = f"TCONS:TT-004:model:w{w}"
                    sigma[key] = u.tcons_model_c
                    rows.append(Row(f"w{w}|TCONS:TT-004", w, "TCONS", "TT-004", tl - ts, "degC",
                                    {"TT-004:sys": 1.0, "TT-001:sys": -1.0, key: 1.0},
                                    {"source_tank": "TK-001", "expected_offset_c": 0.0}))
        pump_frac = {p: mm.pump_running_fraction(p, t0, t1) for p in P.pumps}
        running = [p for p, f in pump_frac.items() if f > 0.9]
        q_meter = P.routes[route_guess].meters[0] if route_guess else None  # meter on the pump path
        q_mean = mm.store.median(q_meter, "volume_flow", t0, t1) if q_meter else math.nan
        if running and source and q_meter and not math.isnan(q_mean) and q_mean > 5:
            src_tt = self.temp_sensor[source]
            tl = mm.store.median("TT-005", "temperature", t0, t1)
            ts = mm.store.median(src_tt, "temperature", t0, t1)
            pw = sum(mm.store.median(f"{p}-PW", "power", t0, t1) for p in running)
            mdot = q_mean / 3600.0 * inv0[source]["rho_obs"]
            dT = (pw * 1000.0 * P.pumps[running[0]].motor_efficiency) / (mdot * CP) if mdot > 0 else 0.0
            if not (math.isnan(tl) or math.isnan(ts) or math.isnan(dT)):
                key = f"TCONS:TT-005:model:w{w}"
                sigma[key] = u.tcons_model_c
                rows.append(Row(f"w{w}|TCONS:TT-005", w, "TCONS", "TT-005", tl - ts - dT, "degC",
                                {"TT-005:sys": 1.0, f"{src_tt}:sys": -1.0, key: 1.0},
                                {"source_tank": source, "expected_offset_c": dT}))

        # ---------------- density consistency rows
        for dt_id, (meter, tank_guess) in {"DT-001": ("FM-001", "TK-001"), "DT-002": (q_meter, source)}.items():
            if meter is None or tank_guess is None or meter not in active:
                continue
            if self._flow_fraction(meter, t0, t1) < 0.8:
                continue
            dmeas = mm.store.median(dt_id, "density_15c", t0, t1)
            dref = mm.tank_rho15(tank_guess, t0)
            if math.isnan(dmeas) or math.isnan(dref):
                continue
            key = f"DCONS:{dt_id}:model:w{w}"
            sigma[key] = u.dcons_model_kgm3
            _, lab_ts = mm.records.lab_density(tank_guess, t0)
            ref_key = f"{tank_guess}:rho_ref@{int(lab_ts) if math.isfinite(lab_ts) else 'default'}"
            sigma[ref_key] = u.dcons_reference_kgm3
            rows.append(Row(f"w{w}|DCONS:{dt_id}", w, "DCONS", dt_id, dmeas - dref, "kg/m3",
                            {f"{dt_id}:sys": 1.0, key: 1.0, ref_key: -1.0},
                            {"source_tank": tank_guess, "reference_sample_ts": lab_ts}))

        # ---------------- pump performance rows
        curve_head: dict[str, float] = {}
        if running and not math.isnan(q_mean) and q_mean > 5:
            q_each = q_mean / len(running)
            psuc = mm.store.median("PT-001", "pressure", t0, t1)
            rho = inv0[source]["rho_obs"] if source else 850.0
            for p in running:
                pd_ = mm.store.median(f"{p}-PD", "discharge_pressure", t0, t1)
                if math.isnan(pd_) or math.isnan(psuc):
                    continue
                cfg = P.pumps[p]
                h_curve = cfg.shutoff_head_m - cfg.curve_coeff_a * q_each ** 2
                h_meas = (pd_ - psuc) * 1e5 / (rho * G)
                curve_head[p] = h_curve
                key = f"PUMP:{p}:model:w{w}"
                sigma[key] = u.pump_model_m
                rows.append(Row(f"w{w}|PUMP:{p}", w, "PUMP", p, h_meas - h_curve, "m", {key: 1.0},
                                {"q_m3h": q_each, "h_curve_m": h_curve, "h_meas_m": h_meas}))

        pipe_p = {pid: mm.pressure_mean(pt, t0, t1) for pid, pt in self.pipe_pt.items()}
        lvl = {t: 0.5 * (inv0[t]["level_m"] + inv1[t]["level_m"]) for t in P.tanks}
        pipe_sqrtp: dict[str, float] = {}
        for pid, pt in self.pipe_pt.items():
            ws = mm.store.window(pt, "pressure", t0, t1)
            v = ws.v[ws.good]
            pipe_sqrtp[pid] = float(np.mean(np.sqrt(np.clip(v, 0, None) / 5.0))) if len(v) else math.nan
        valve_term: dict[str, float] = {}
        for cut in topo.valve_cuts:
            pt = self.pipe_pt.get(cut.pipe_id)
            if not cut.tank_id or pt is None:
                continue
            ws = mm.store.window(pt, "pressure", t0, t1)
            v = ws.v[ws.good]
            if not len(v):
                continue
            p_tank = inv0[cut.tank_id]["rho_obs"] * G * max(
                lvl[cut.tank_id] - P.tanks[cut.tank_id].nozzle_elevation_m, 0.0) / 1e5
            dp = v - p_tank
            valve_term[cut.valve_id] = float(np.mean(np.sign(dp) * np.sqrt(np.abs(dp) / 5.0)))
        return WindowEvidence(
            index=w, t0=t0, t1=t1, topology=topo, rows=rows, meter_mass=mmass, unbooked=unbooked,
            meter_sign=meter_sign, pipe_pressure=pipe_p, tank_level=lvl, pump_running=pump_frac,
            pump_curve_head=curve_head, pipe_sqrtp=pipe_sqrtp, valve_dp_term=valve_term,
            source_tank=source, route_guess=route_guess, quality=quality,
            silent_meters={m: P.sensors[m].low_flow_cutoff for m in meters
                           if m not in active and mm.meter_covers(m, t0, t1)})

    def _flow_fraction(self, meter: str, t0: float, t1: float) -> float:
        w = self.mm.store.window(meter, "volume_flow", t0, t1)
        if not len(w):
            return 0.0
        return float(np.mean(w.v > 1.0))

    # -------------------------------------------------------------- quality
    def _quality(self, t0: float, t1: float, active_meters: set[str]) -> dict[str, dict[str, Any]]:
        P, st = self.plant, self.mm.store
        out: dict[str, dict[str, Any]] = {}
        for sid, s in P.sensors.items():
            if s.type not in ("level", "temperature", "flow", "density", "pressure"):
                continue
            par = s.parameter
            w = st.window(sid, par, t0, t1)
            expected = max(s.publish_interval_s, 1.0)
            ser = st.get(sid, par)
            if ser is not None and len(ser) > 3:
                dts = np.diff(ser.t[: min(len(ser), 200)])
                if len(dts):
                    expected = max(float(np.median(dts)), expected)
            cov = min(len(w) / max((t1 - t0) / expected, 1.0), 1.0)
            # longest gap between samples (dropout) and longest run of identical values (frozen)
            ts = np.concatenate([[t0], w.t, [t1]])
            max_gap = float(np.max(np.diff(ts))) if len(ts) > 1 else t1 - t0
            stuck = False
            run_s = 0.0
            vals = w.v[w.good]
            tv = w.t[w.good]
            if len(vals) >= 6:
                change = np.flatnonzero(np.diff(vals) != 0.0)
                bounds = np.concatenate([[0], change + 1, [len(vals)]])
                k = int(np.argmax(np.diff(bounds)))
                i0, i1 = bounds[k], bounds[k + 1] - 1
                run_s = float(tv[i1] - tv[i0])
                if run_s >= self.stuck_min_s and (i1 - i0) >= 6:
                    stuck = not (s.type == "flow" and vals[i0] == 0.0)
            bad = float(np.mean(~w.good)) if len(w) else 0.0
            dropout = max_gap >= self.dropout_min_s or cov < 0.5
            out[sid] = {"coverage": round(cov, 3), "stuck": bool(stuck), "bad_fraction": round(bad, 3),
                        "samples": int(len(w)), "max_gap_s": round(max_gap, 1), "flat_run_s": round(run_s, 1),
                        "dropout": bool(dropout)}
        return out


def segment_cv(topo: Topology, seg: SubSegment) -> str | None:
    return topo.cv_of_segment(seg)
