"""Physics-based tank farm simulator (digital twin process engine).

Each step (dt):
  1. ambient conditions          5. hydraulic network solve + exact mass routing
  2. fault injection             6. tank mass/energy balances (incl. floor leaks)
  3. operations sequencing       7. line temperature / density states
  4. actuator dynamics           8. instruments -> measurements -> publication
                                 9. flow control (PI on *measured* flow)
                                10. records (flow computer tickets) + ground truth
The simulator never depends on a frontend and is fully deterministic for a
given (scenario, seed).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

import numpy as np

from industrial.schemas.plant import PlantConfig, get_plant
from simulation.faults.faults import FaultInjector, FaultSpec
from simulation.hydraulics.network import HydraulicNetwork, HydraulicSolution
from simulation.process.properties import CrudeProperties, Stream
from simulation.pumps.pump import STATUS_CODE, PumpModel
from simulation.records.records import FlowComputer, RecordsLedger, TransferRecord
from simulation.scenarios.scenario import Scenario, TransferOrder, random_initial
from simulation.sensors.sensor import Instrument
from simulation.tanks.tank import TankModel
from simulation.valves.valve import ValveModel

# Transfer execution states
PENDING, OPENING, STARTING, RUNNING, STOPPING, CLOSING, COMPLETE, ABORTED = (
    "PENDING", "OPENING", "STARTING", "RUNNING", "STOPPING", "CLOSING", "COMPLETE", "ABORTED")


@dataclass
class TransferExecution:
    order: TransferOrder
    state: str = PENDING
    t_state: float = 0.0
    t_running: float | None = None
    integral: float = 0.0
    u0: float = 0.25
    record: TransferRecord | None = None
    flow_computer: FlowComputer | None = None
    stop_reason: str | None = None


@dataclass
class StepOutput:
    t: float
    timestamp: datetime
    telemetry: list[tuple] = field(default_factory=list)   # (t, sensor, asset, parameter, value, unit, quality)
    events: list[dict[str, Any]] = field(default_factory=list)


class Simulator:
    def __init__(self, scenario: Scenario, plant: PlantConfig | None = None):
        self.plant = plant or get_plant()
        self.scenario = scenario
        P = self.plant
        ss = np.random.SeedSequence(scenario.seed)
        kids = ss.spawn(4 + len(P.sensors))
        self.rng = np.random.default_rng(kids[0])
        self.oracle_rng = np.random.default_rng(kids[2])
        self.g = P.environment.gravity
        self.t = 0.0
        self.step_count = 0
        self.start_time = scenario.start_time
        self.products = {
            pid: CrudeProperties(p.density_15c, p.thermal_expansion_coeff, p.viscosity_15c_cst,
                                 p.viscosity_temp_coeff, p.specific_heat_j_kgk)
            for pid, p in P.products.items()
        }
        init = scenario.initial or random_initial(P, self.rng)
        self.tanks: dict[str, TankModel] = {}
        for tid, cfg in P.tanks.items():
            ini = init[tid]
            prod_id = ini.product or cfg.initial_product
            self.tanks[tid] = TankModel(tid, cfg, self.products[prod_id], ini.level_m, ini.temperature_c, prod_id)
        self.pumps = {pid: PumpModel(pid, cfg) for pid, cfg in P.pumps.items()}
        self.valves = {vid: ValveModel(vid, cfg) for vid, cfg in P.valves.items()}
        self.network = HydraulicNetwork(P)
        self.instruments: dict[str, Instrument] = {}
        for k, (sid, cfg) in enumerate(P.sensors.items()):
            c = cfg.model_copy()
            if scenario.noise_scale != 1.0:
                c = cfg.model_copy(update={
                    "noise": cfg.noise * scenario.noise_scale,
                    "noise_rel": cfg.noise_rel * scenario.noise_scale,
                    "noise_abs": cfg.noise_abs * scenario.noise_scale,
                    "ar_sigma": cfg.ar_sigma * scenario.noise_scale,
                })
            self.instruments[sid] = Instrument(sid, c, np.random.default_rng(kids[4 + k]),
                                               scenario.sample_interval_s)
        self.faults = FaultInjector([f.model_copy() for f in scenario.faults])
        self.records = RecordsLedger(self.start_time, np.random.default_rng(kids[1]))
        self.records.seed_history(P, self.instruments, self.tanks)
        self.pending: list[TransferExecution] = [
            TransferExecution(order=o) for o in sorted(scenario.operations, key=lambda o: o.start_s)]
        self.active: TransferExecution | None = None
        self.completed: list[TransferExecution] = []
        amb = self.ambient(0.0)
        self.line_temp: dict[str, float] = {k: amb for k in list(P.pipelines) + list(P.junctions)}
        self.line_temp["PL-001"] = self.tanks["TK-001"].temperature
        self.line_rho15: dict[str, float] = {
            "PL-001": self.tanks["TK-001"].rho15,
            "PL-002": self.tanks["TK-002"].rho15,
            "PL-003": self.tanks["TK-003"].rho15,
            "PL-004": self.tanks["TK-002"].rho15,
            "MF-DIS": self.tanks["TK-001"].rho15,
            "MF-SUC": self.tanks["TK-001"].rho15,
        }
        self.sol: HydraulicSolution | None = None
        self.cum_leak_kg: dict[str, float] = {}
        self.leak_rate_kgs: dict[str, float] = {}
        self.true_values: dict[str, float] = {}
        self.truth: list[dict[str, Any]] = []
        self.event_log: list[dict[str, Any]] = []
        self._next_truth_t = 0.0
        self._initial_total_mass = self.total_mass()
        self.listeners: list[Callable[[StepOutput], None]] = []
        self.paused = False

    # ------------------------------------------------------------------ utils
    def timestamp(self, t: float | None = None) -> datetime:
        return self.start_time + timedelta(seconds=self.t if t is None else t)

    def ambient(self, t: float) -> float:
        env = self.plant.environment
        mean = self.scenario.ambient_mean_c if self.scenario.ambient_mean_c is not None else env.ambient_mean_c
        hour = (self.start_time.hour + self.start_time.minute / 60.0 + t / 3600.0) % 24.0
        return mean + env.ambient_diurnal_amplitude_c * math.cos(2 * math.pi * (hour - env.ambient_peak_hour) / 24.0)

    def total_mass(self) -> float:
        return sum(tk.mass for tk in self.tanks.values())

    def total_physical_loss_kg(self) -> float:
        return sum(self.cum_leak_kg.values()) + sum(tk.cum_loss_kg for tk in self.tanks.values())

    def _event(self, etype: str, asset: str | None = None, **payload) -> dict[str, Any]:
        ev = {"event_type": etype, "asset_id": asset, "t": self.t, "timestamp": self.timestamp(),
              "payload": payload}
        self.event_log.append(ev)
        return ev

    # ------------------------------------------------------------- operations
    def add_transfer(self, order: TransferOrder) -> None:
        self.pending.append(TransferExecution(order=order))
        self.pending.sort(key=lambda e: e.order.start_s)

    def inject_fault(self, fault: FaultSpec) -> None:
        self.faults.add(fault)

    def clear_fault(self, fault_id: str) -> None:
        self.faults.clear(fault_id, self.t)

    def _measured(self, sid: str) -> float:
        v = self.instruments[sid].last_value
        return float("nan") if v is None else float(v)

    def _operations(self, dt: float, events: list) -> None:
        P = self.plant
        if self.active is None and self.pending and self.pending[0].order.start_s <= self.t:
            ex = self.pending.pop(0)
            route = P.routes[ex.order.route]
            dst_lvl = self._measured(P.tank_level_sensor(route.destination))
            src_lvl = self._measured(P.tank_level_sensor(route.source))
            if (not math.isnan(dst_lvl) and dst_lvl > P.tanks[route.destination].max_safe_fill_m - 0.3) or (
                    not math.isnan(src_lvl) and src_lvl < P.tanks[route.source].min_operating_level_m + 0.3):
                ex.state = ABORTED
                ex.stop_reason = "PERMISSIVE_NOT_MET"
                self.completed.append(ex)
                self.records.log(self.timestamp(), f"Transfer {ex.order.transfer_id} not started: level permissive",
                                 route.destination)
            else:
                self.active = ex
                self._set_state(ex, OPENING)
                for vid in route.open_valves:
                    self.valves[vid].command(True, ex.u0 if vid == route.control_valve else 1.0)
                fault = self.faults.transaction_fault_for(ex.order.transfer_id, ex.order.route,
                                                          max(self.t, ex.order.start_s))
                gauges = {route.source: round(src_lvl, 3), route.destination: round(dst_lvl, 3)}
                ex.flow_computer = FlowComputer(alpha=self.tanks[route.source].alpha)
                if fault is None or fault.fault_type != "MISSING_TRANSACTION":
                    ex.record = TransferRecord(
                        transfer_id=ex.order.transfer_id, route=ex.order.route, source_tank=route.source,
                        destination_tank=route.destination, start_time=self.timestamp(),
                        custody_meter=route.custody_meter, product_id=self.tanks[route.source].product_id,
                        opening_gauges=gauges)
                    self.records.transfers[ex.order.transfer_id] = ex.record
                    self.records.log(self.timestamp(), f"Transfer {ex.order.transfer_id} started on {ex.order.route} "
                                     f"({route.name}) at {ex.order.setpoint_m3h:.0f} m3/h", route.source)
                    events.append(self._event("TRANSFER_STARTED", route.source, **ex.record.as_dict()))
                else:
                    if self.oracle_rng.random() < 0.6:
                        self.records.log(self.timestamp(), f"Manual line-up of {ex.order.route} opened by shift "
                                         "operator (no ticket reference)", route.source)
        ex = self.active
        if ex is None:
            return
        route = P.routes[ex.order.route]
        o = ex.order
        if ex.state == OPENING:
            ok = all(self.valves[v].position >= self.valves[v].target - 1e-3 for v in route.open_valves)
            if ok:
                for pid in o.pumps:
                    self.pumps[pid].commanded_on = True
                self._set_state(ex, STARTING)
        elif ex.state == STARTING:
            if all(self.pumps[p].speed_ratio >= 0.999 for p in o.pumps):
                ex.t_running = self.t
                ex.integral = 0.0
                ex.u0 = self.valves[route.control_valve].position
                self._set_state(ex, RUNNING)
        elif ex.state == RUNNING:
            reason = None
            if o.target_volume_m3 is not None and ex.flow_computer.gross_m3 >= o.target_volume_m3:
                reason = "TARGET_REACHED"
            elif o.max_duration_s is not None and self.t - ex.t_running >= o.max_duration_s:
                reason = "MAX_DURATION"
            dst_lvl = self._measured(P.tank_level_sensor(route.destination))
            src_lvl = self._measured(P.tank_level_sensor(route.source))
            if dst_lvl >= P.tanks[route.destination].max_safe_fill_m:
                reason = "INTERLOCK_HIGH_LEVEL"
            if src_lvl <= P.tanks[route.source].min_operating_level_m:
                reason = "INTERLOCK_LOW_LEVEL"
            suction_pt = "PT-004" if route.source == "TK-002" else "PT-001"
            if self._measured(suction_pt) < -0.6:
                reason = "INTERLOCK_LOW_SUCTION_PRESSURE"
            if reason:
                ex.stop_reason = reason
                for pid in o.pumps:
                    self.pumps[pid].commanded_on = False
                self._set_state(ex, STOPPING)
            else:
                self._flow_control(ex, dt)
        elif ex.state == STOPPING:
            if all(not self.pumps[p].active for p in o.pumps):
                for vid in route.open_valves:
                    self.valves[vid].command(False)
                self._set_state(ex, CLOSING)
        elif ex.state == CLOSING:
            if all(self.valves[v].position <= 1e-6 for v in route.open_valves):
                self._complete(ex, events)

    def _set_state(self, ex: TransferExecution, state: str) -> None:
        ex.state = state
        ex.t_state = self.t

    def _flow_control(self, ex: TransferExecution, dt: float) -> None:
        route = self.plant.routes[ex.order.route]
        y = self._measured(route.control_meter)
        if math.isnan(y):
            return
        kp, ti = self.plant.control.flow_kp, self.plant.control.flow_ti_s
        e = ex.order.setpoint_m3h - y
        u_unsat = ex.u0 + kp * (e + ex.integral / ti)
        if not ((u_unsat >= 1.0 and e > 0) or (u_unsat <= 0.03 and e < 0)):
            ex.integral += e * dt
        u = min(max(ex.u0 + kp * (e + ex.integral / ti), 0.03), 1.0)
        self.valves[route.control_valve].set_target(u)

    def _complete(self, ex: TransferExecution, events: list) -> None:
        P = self.plant
        route = P.routes[ex.order.route]
        self._set_state(ex, COMPLETE)
        self.active = None
        self.completed.append(ex)
        if ex.record is not None:
            fault = self.faults.transaction_fault_for(ex.order.transfer_id, ex.order.route, ex.order.start_s)
            qty = ex.flow_computer.net_mass_t
            if fault is not None and fault.fault_type == "WRONG_QUANTITY":
                qty *= 1.0 + float(fault.params.get("error_frac", 0.02))
            ex.record.end_time = self.timestamp()
            ex.record.recorded_quantity = round(qty, 3)
            ex.record.recorded_volume_m3 = round(ex.flow_computer.gross_m3, 3)
            ex.record.status = "COMPLETED"
            ex.record.closing_gauges = {
                route.source: round(self._measured(P.tank_level_sensor(route.source)), 3),
                route.destination: round(self._measured(P.tank_level_sensor(route.destination)), 3),
            }
            self.records.log(self.timestamp(), f"Transfer {ex.order.transfer_id} completed "
                             f"({ex.stop_reason}); ticket {ex.record.recorded_quantity:.3f} t", route.destination)
            events.append(self._event("TRANSFER_COMPLETED", route.destination, **ex.record.as_dict()))
            self.records.add_lab_sample(self.timestamp() + timedelta(minutes=40), route.destination,
                                        self.tanks[route.destination].rho15)

    # ------------------------------------------------------------------- step
    def step(self) -> StepOutput:
        P = self.plant
        dt = self.scenario.dt_s
        t = self.t
        out = StepOutput(t=t, timestamp=self.timestamp())
        t_amb = self.ambient(t)

        pipe_leaks, tank_leaks = self.faults.apply(t, self.instruments, self.valves, self.pumps)
        self._operations(dt, out.events)
        for v in self.valves.values():
            v.step(dt)
        for p in self.pumps.values():
            p.step(dt)

        # ---- hydraulics
        self.network.ensure_topology({pid: pos for pid, (pos, _a) in pipe_leaks.items()})
        leak_areas = {f"LEAK:{pid}": a for pid, (_pos, a) in pipe_leaks.items()}
        src_tank = self.tanks[P.routes[self.active.order.route].source] if self.active else self.tanks["TK-001"]
        props = src_tank.props()
        nu = props.kinematic_viscosity(src_tank.temperature)
        rho = src_tank.density_obs
        sol = self.network.solve(
            tank_heads={tid: tk.liquid_head for tid, tk in self.tanks.items()},
            valve_k={vid: v.k_value() for vid, v in self.valves.items()},
            pumps=self.pumps, leak_areas=leak_areas, nu=nu, rho=rho)
        for pid, pump in self.pumps.items():
            pump.update_operating_point(sol.q[pid] * 3600.0, rho, self.g)
        self.network.route_mass(sol, self.tanks, self.pumps)
        self.sol = sol
        self._rho_line = rho

        # ---- tanks
        for tid, tk in self.tanks.items():
            loss = tk.floor_leak_rate(tank_leaks.get(tid, 0.0))
            tk.step(dt, sol.tank_in[tid], sol.tank_out_mass[tid], loss, t_amb)
            self.leak_rate_kgs[f"TANK:{tid}"] = loss
        self.leak_rate_kgs.update({n.split(":", 1)[1]: 0.0 for n in self.network.leak_nodes})
        for node, st in sol.leak_streams.items():
            pid = node.split(":", 1)[1]
            self.cum_leak_kg[pid] = self.cum_leak_kg.get(pid, 0.0) + st.m * dt
            self.leak_rate_kgs[pid] = st.m

        # ---- line states
        self._update_line_states(sol, dt, t_amb)

        # ---- instruments
        self._measure_and_publish(out, sol, dt, t_amb, rho)

        # ---- flow computer (custody ticket) integration
        if self.active is not None and self.active.flow_computer is not None:
            route = P.routes[self.active.order.route]
            m = route.custody_meter
            cfg = P.sensors[m]
            self.active.flow_computer.accumulate(
                self._measured(m), self._measured(cfg.temperature_sensor), self._measured(cfg.density_sensor), dt)

        # ---- ground truth
        if t + 1e-9 >= self._next_truth_t:
            self._record_truth(t)
            self._next_truth_t += self.scenario.gt_interval_s
        self.t += dt
        self.step_count += 1
        for cb in self.listeners:
            cb(out)
        return out

    def _segment_stream(self, sol: HydraulicSolution, pipe_id: str, s: float) -> tuple[Stream | None, float]:
        seg = self.network.segment_at(pipe_id, s)
        return sol.streams.get(seg.id), sol.q.get(seg.id, 0.0)

    def _update_line_states(self, sol: HydraulicSolution, dt: float, t_amb: float) -> None:
        P = self.plant
        for pid in P.pipelines:
            st, q = self._segment_stream(sol, pid, 0.9)
            if st is not None and abs(q) * 3600 > 1.0:
                a = 1 - math.exp(-dt / 60.0)
                self.line_temp[pid] += a * (st.temperature - self.line_temp[pid])
                self.line_rho15[pid] = st.rho15
            else:
                a = 1 - math.exp(-dt / 7200.0)
                self.line_temp[pid] += a * (t_amb - self.line_temp[pid])
        dis = Stream()
        for pid in P.pumps:
            if pid in sol.streams:
                dis = dis + sol.streams[pid]
        if not dis.empty and dis.line_volume_rate() * 3600 > 1.0:
            a = 1 - math.exp(-dt / 30.0)
            self.line_temp["MF-DIS"] += a * (dis.temperature - self.line_temp["MF-DIS"])
            self.line_rho15["MF-DIS"] = dis.rho15
        else:
            a = 1 - math.exp(-dt / 7200.0)
            self.line_temp["MF-DIS"] += a * (t_amb - self.line_temp["MF-DIS"])

    def true_value(self, sid: str, sol: HydraulicSolution, t_amb: float, rho: float) -> float:
        P = self.plant
        cfg = P.sensors[sid]
        typ, asset = cfg.type, cfg.asset
        if typ == "level":
            return self.tanks[asset].level
        if typ == "temperature":
            if asset in self.tanks:
                return self.tanks[asset].temperature
            if asset == P.plant_id:
                return t_amb
            return self.line_temp[asset]
        if typ == "pressure":
            if asset in P.pipelines:
                h = self.network.head_at(sol, asset, cfg.location or 0.5)
                return self.network.pressure_bar(h, P.pipelines[asset].elevation_m, rho)
            if asset in P.pumps:
                node = P.pumps[asset].to
                return self.network.pressure_bar(sol.heads[node], self.network.elevation[node], rho)
        if typ == "flow":
            st, q = self._segment_stream(sol, asset, cfg.location or 0.5)
            if st is None or st.empty:
                return 0.0
            v = st.line_volume_rate() * 3600.0
            return v if q >= 0 else -v
        if typ == "density":
            return self.line_rho15[asset]
        if typ == "pump_status":
            return STATUS_CODE[self.pumps[asset].status]
        if typ == "pump_speed":
            return self.pumps[asset].speed_ratio * self.pumps[asset].cfg.rated_speed_rpm
        if typ == "pump_power":
            return self.pumps[asset].elec_power_kw
        if typ == "valve_position":
            return self.valves[asset].position * 100.0
        if typ == "valve_command":
            return 1.0 if self.valves[asset].command_open else 0.0
        raise ValueError(f"No true-value model for {sid}")

    def _measure_and_publish(self, out: StepOutput, sol, dt, t_amb, rho) -> None:
        P = self.plant
        t = self.t
        for sid, inst in self.instruments.items():
            x = self.true_value(sid, sol, t_amb, rho)
            self.true_values[sid] = x
            y = inst.measure(t, x, dt)
            if inst.due(t):
                yq, q = inst.quality(y)
                cfg = inst.cfg
                out.telemetry.append((t, sid, cfg.asset, cfg.parameter, yq, cfg.unit, q))
                inst.last_published = y
                if inst.totalizer is not None:
                    out.telemetry.append((t, sid, cfg.asset, "volume_total", round(inst.totalizer, 3), "m3", "GOOD"))

    def _record_truth(self, t: float) -> None:
        sol = self.sol
        row: dict[str, Any] = {"t": t, "timestamp": self.timestamp(t)}
        for tid, tk in self.tanks.items():
            row[f"{tid}.mass_t"] = tk.mass / 1000.0
            row[f"{tid}.level_m"] = tk.level
            row[f"{tid}.temperature_c"] = tk.temperature
            row[f"{tid}.rho15"] = tk.rho15
            row[f"{tid}.floor_leak_cum_t"] = tk.cum_loss_kg / 1000.0
            row[f"{tid}.floor_leak_rate_th"] = self.leak_rate_kgs.get(f"TANK:{tid}", 0.0) * 3.6
        for pid in self.plant.pipelines:
            row[f"{pid}.leak_rate_th"] = self.leak_rate_kgs.get(pid, 0.0) * 3.6
            row[f"{pid}.leak_cum_t"] = self.cum_leak_kg.get(pid, 0.0) / 1000.0
        for pid, p in self.pumps.items():
            row[f"{pid}.flow_m3h"] = p.flow_m3h
            row[f"{pid}.head_m"] = p.head_m
            row[f"{pid}.power_kw"] = p.elec_power_kw
            row[f"{pid}.head_factor"] = p.head_factor
        for vid, v in self.valves.items():
            row[f"{vid}.position"] = v.position
            row[f"{vid}.passing_fraction"] = v.passing_fraction
        for sid, inst in self.instruments.items():
            row[f"{sid}.true"] = self.true_values.get(sid)
            row[f"{sid}.measured"] = inst.last_value
        row["total_mass_t"] = self.total_mass() / 1000.0
        row["physical_loss_cum_t"] = self.total_physical_loss_kg() / 1000.0
        row["conservation_error_kg"] = (self._initial_total_mass - self.total_mass()
                                        - self.total_physical_loss_kg())
        act = self.faults.active(t)
        row["active_faults"] = [f"{f.fault_type}@{f.target}" for f in act]
        row["active_transfer"] = self.active.order.transfer_id if self.active else None
        row["active_route"] = self.active.order.route if self.active else None
        row["transfer_state"] = self.active.state if self.active else None
        row["hydraulics_converged"] = bool(sol.converged) if sol else True
        self.truth.append(row)

    # ---------------------------------------------------------------- running
    def run(self, until_s: float | None = None, on_step: Callable[[StepOutput], None] | None = None) -> None:
        end = self.scenario.duration_s if until_s is None else until_s
        while self.t < end - 1e-9:
            o = self.step()
            if on_step:
                on_step(o)

    # ------------------------------------------------------------- snapshots
    def truth_snapshot(self) -> dict[str, Any]:
        """Ground-truth state (simulation lab only - never fed to inference)."""
        return {
            "t": self.t, "timestamp": self.timestamp().isoformat(),
            "tanks": {tid: {"mass_t": tk.mass / 1000, "level_m": tk.level, "temperature_c": tk.temperature,
                            "rho15": tk.rho15} for tid, tk in self.tanks.items()},
            "pumps": {pid: {"status": p.status, "flow_m3h": p.flow_m3h, "head_m": p.head_m,
                            "power_kw": p.elec_power_kw} for pid, p in self.pumps.items()},
            "valves": {vid: {"position": v.position, "command_open": v.command_open,
                             "passing_fraction": v.passing_fraction} for vid, v in self.valves.items()},
            "leaks": {k: v * 3.6 for k, v in self.leak_rate_kgs.items() if v > 0},
            "physical_loss_cum_t": self.total_physical_loss_kg() / 1000,
            "active_faults": [f.model_dump() for f in self.faults.active(self.t)],
            "active_transfer": (self.active.order.model_dump() | {"state": self.active.state}) if self.active else None,
        }

    # ------------------------------------------------------- verification oracle
    def perform_check(self, action: str, target: str) -> dict[str, Any]:
        """Simulated field verification (only available in simulation mode).

        Outcome distributions match intelligence.active_diagnosis.tests so the
        information-gain model is consistent with the simulated world.
        """
        from intelligence.active_diagnosis.tests import simulate_outcome
        return simulate_outcome(self, action, target, self.oracle_rng)
