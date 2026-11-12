"""Quasi-steady hydraulic network solver (section 4.4).

Formulation
-----------
* Tank nozzles are fixed-head nodes (head = liquid surface elevation).
* Manifolds and leak points are free nodes with unknown piezometric head h.
* Pipe segment flow from head difference (Darcy-Weisbach + local losses):
      dh = (f L/D + sum K + K_valve) * v|v| / 2g ,  Q = v A
* Pump branch: Q from the inverse pump curve with a check valve.
* Leak sink: orifice  Q = Cd A_hole sqrt(2 g (h - z)).
Node mass balances are solved with MINPACK (hybr); friction factors are
updated by fixed-point iteration from the previous flow. Liquid is treated as
incompressible and line-pack / hydraulic transients are neglected (documented
limitation, docs/mathematical_models).

After solving volumetric flows, mass is routed through the network with exact
stream mixing (simulation.process.properties.Stream) so that total mass is
conserved to floating point precision regardless of solver tolerance.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import root

from industrial.schemas.plant import PlantConfig

LEAK_CD = 0.62
BLOCKED_HEAD_ABOVE_ELEVATION = 0.0  # blocked-in liquid line: pressure collapses, no leak flow
REG_EPS = 1e-4


def friction_factor(re: float, rel_rough: float) -> float:
    """Darcy friction factor: laminar 64/Re, Swamee-Jain for turbulent, blended."""
    re = max(re, 1.0)
    if re < 2000.0:
        return 64.0 / re
    turb = 0.25 / (math.log10(rel_rough / 3.7 + 5.74 / max(re, 4000.0) ** 0.9)) ** 2
    if re > 4000.0:
        return turb
    lam = 64.0 / 2000.0
    w = (re - 2000.0) / 2000.0
    return lam * (1 - w) + turb * w


@dataclass
class PipeSegment:
    id: str
    pipe_id: str
    s0: float
    s1: float
    n_from: str
    n_to: str
    length: float
    diameter: float
    area: float
    rel_rough: float
    k_dist: float
    valve_id: str | None = None
    valve_s: float | None = None
    f: float = 0.02


@dataclass
class PumpBranch:
    id: str
    n_from: str
    n_to: str


@dataclass
class HydraulicSolution:
    q: dict[str, float]                 # branch id -> m3/s (positive from->to)
    heads: dict[str, float]             # node -> head (m)
    leak_q: dict[str, float]            # leak node -> m3/s
    blocked: set[str]                   # free nodes isolated from any tank
    segment_k: dict[str, float]         # segment total K used
    converged: bool = True
    streams: dict = field(default_factory=dict)       # branch id -> Stream
    leak_streams: dict = field(default_factory=dict)  # leak node -> Stream
    tank_in: dict = field(default_factory=dict)       # tank -> list[Stream]
    tank_out_mass: dict = field(default_factory=dict) # tank -> kg/s


class HydraulicNetwork:
    def __init__(self, plant: PlantConfig):
        self.plant = plant
        self.g = plant.environment.gravity
        self.fixed_nodes = set(plant.tanks)
        self.elevation = {t: c.nozzle_elevation_m for t, c in plant.tanks.items()}
        self.elevation.update({j: c.elevation_m for j, c in plant.junctions.items()})
        self._leak_key: tuple | None = None
        self._prev_heads: dict[str, float] = {}
        self.segments: dict[str, PipeSegment] = {}
        self.pump_branches: dict[str, PumpBranch] = {}
        self.free_nodes: list[str] = []
        self.leak_nodes: dict[str, tuple[str, float]] = {}
        self._build({})

    # ---------------------------------------------------------------- topology
    def _build(self, leaks: dict[str, float]) -> None:
        """leaks: pipe_id -> fractional position of leak along pipe."""
        self.segments.clear()
        self.leak_nodes.clear()
        self.free_nodes = list(self.plant.junctions)
        valves_by_pipe: dict[str, tuple[str, float]] = {
            v.pipeline: (vid, v.position_on_line) for vid, v in self.plant.valves.items()
        }
        for pid, p in self.plant.pipelines.items():
            cuts = [0.0, 1.0]
            leak_node = None
            if pid in leaks:
                s_leak = min(max(leaks[pid], 0.01), 0.99)
                cuts = [0.0, s_leak, 1.0]
                leak_node = f"LEAK:{pid}"
                self.leak_nodes[leak_node] = (pid, s_leak)
                self.free_nodes.append(leak_node)
                self.elevation[leak_node] = p.elevation_m
            nodes = [p.from_] + ([leak_node] if leak_node else []) + [p.to]
            vinfo = valves_by_pipe.get(pid)
            for i in range(len(cuts) - 1):
                s0, s1 = cuts[i], cuts[i + 1]
                seg = PipeSegment(
                    id=f"{pid}#{i}", pipe_id=pid, s0=s0, s1=s1,
                    n_from=nodes[i], n_to=nodes[i + 1],
                    length=p.length_m * (s1 - s0), diameter=p.internal_diameter_m,
                    area=p.area_m2, rel_rough=p.roughness_m / p.internal_diameter_m,
                    k_dist=p.fittings_k * (s1 - s0),
                )
                if vinfo is not None:
                    vid, vs = vinfo
                    in_seg = (s0 <= vs <= s1) and not (vs == s0 and i > 0)
                    if in_seg:
                        seg.valve_id, seg.valve_s = vid, vs
                self.segments[seg.id] = seg
        self.pump_branches = {
            pid: PumpBranch(id=pid, n_from=p.from_, n_to=p.to) for pid, p in self.plant.pumps.items()
        }
        self._leak_key = tuple(sorted(leaks.items()))

    def ensure_topology(self, leaks: dict[str, float]) -> None:
        key = tuple(sorted(leaks.items()))
        if key != self._leak_key:
            self._build(leaks)

    # ------------------------------------------------------------------- solve
    def solve(self, tank_heads: dict[str, float], valve_k: dict[str, float], pumps: dict,
              leak_areas: dict[str, float], nu: float, rho: float) -> HydraulicSolution:
        """Solve for heads and flows.

        tank_heads: tank -> head (m); valve_k: valve -> K (inf if shut);
        pumps: pump id -> PumpModel; leak_areas: leak node -> hole area (m2);
        nu: kinematic viscosity (m2/s); rho: density for pressure (kg/m3).
        """
        g = self.g
        seg_k_valve: dict[str, float] = {}
        active_segs: list[PipeSegment] = []
        for seg in self.segments.values():
            kv = valve_k.get(seg.valve_id, 0.0) if seg.valve_id else 0.0
            seg_k_valve[seg.id] = kv
            if math.isfinite(kv):
                active_segs.append(seg)
        active_pumps = [pb for pid, pb in self.pump_branches.items() if pumps[pid].active]

        # connected components (union-find)
        parent: dict[str, str] = {}

        def find(a: str) -> str:
            parent.setdefault(a, a)
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for n in list(self.fixed_nodes) + self.free_nodes:
            find(n)
        for seg in active_segs:
            union(seg.n_from, seg.n_to)
        for pb in active_pumps:
            union(pb.n_from, pb.n_to)
        comp_has_fixed = {find(t) for t in self.fixed_nodes}
        unknown = [n for n in self.free_nodes if find(n) in comp_has_fixed]
        blocked = set(self.free_nodes) - set(unknown)
        idx = {n: i for i, n in enumerate(unknown)}

        heads: dict[str, float] = dict(tank_heads)
        for n in blocked:
            heads[n] = self.elevation[n] + BLOCKED_HEAD_ABOVE_ELEVATION

        def seg_ktot(seg: PipeSegment) -> float:
            return seg.f * seg.length / seg.diameter + seg.k_dist + seg_k_valve[seg.id]

        def seg_q(seg: PipeSegment, h: dict[str, float], ktot: float) -> float:
            dh = h[seg.n_from] - h[seg.n_to]
            c = seg.area * math.sqrt(2 * g / ktot)
            return c * dh / math.sqrt(abs(dh) + REG_EPS)

        def pump_q(pb: PumpBranch, h: dict[str, float]) -> float:
            dh = h[pb.n_to] - h[pb.n_from]
            return pumps[pb.id].flow_for_head(dh) / 3600.0

        def leak_q(node: str, h: dict[str, float]) -> float:
            a = leak_areas.get(node, 0.0)
            if a <= 0:
                return 0.0
            ph = h[node] - self.elevation[node]
            sp = 0.01 * math.log1p(math.exp(ph / 0.01)) if ph / 0.01 < 50 else ph
            return LEAK_CD * a * math.sqrt(2 * g * sp)

        relevant_segs = [s for s in active_segs if s.n_from not in blocked and s.n_to not in blocked]
        relevant_pumps = [p for p in active_pumps if p.n_from not in blocked and p.n_to not in blocked]
        converged = True

        if unknown:
            x0 = np.array([self._initial_head(n, tank_heads, relevant_pumps, pumps) for n in unknown])
            for _outer in range(3):
                ktot = {s.id: seg_ktot(s) for s in relevant_segs}

                def residual(x: np.ndarray) -> np.ndarray:
                    h = dict(heads)
                    for n, i in idx.items():
                        h[n] = x[i]
                    r = np.zeros(len(unknown))
                    for s in relevant_segs:
                        q = seg_q(s, h, ktot[s.id])
                        if s.n_from in idx:
                            r[idx[s.n_from]] -= q
                        if s.n_to in idx:
                            r[idx[s.n_to]] += q
                    for pb in relevant_pumps:
                        q = pump_q(pb, h)
                        if pb.n_from in idx:
                            r[idx[pb.n_from]] -= q
                        if pb.n_to in idx:
                            r[idx[pb.n_to]] += q
                    for n in unknown:
                        if n in self.leak_nodes:
                            r[idx[n]] -= leak_q(n, h)
                    return r * 1000.0  # scale to L/s for conditioning

                sol = root(residual, x0, method="hybr", options={"xtol": 1e-12, "maxfev": 4000})
                if not sol.success or np.max(np.abs(sol.fun)) > 1e-6:
                    sol2 = root(residual, x0, method="lm", options={"xtol": 1e-12, "maxiter": 4000})
                    if np.max(np.abs(sol2.fun)) < np.max(np.abs(sol.fun)):
                        sol = sol2
                converged = bool(np.max(np.abs(sol.fun)) < 1e-4)
                x0 = sol.x
                for n, i in idx.items():
                    heads[n] = float(sol.x[i])
                # update friction factors from new flows
                changed = False
                for s in relevant_segs:
                    q = seg_q(s, heads, ktot[s.id])
                    v = abs(q) / s.area
                    f_new = friction_factor(v * s.diameter / nu, s.rel_rough)
                    if abs(f_new - s.f) / s.f > 1e-3:
                        changed = True
                    s.f = f_new
                if not changed:
                    break
            for n in unknown:
                self._prev_heads[n] = heads[n]

        # final flows
        q_out: dict[str, float] = {}
        k_used: dict[str, float] = {}
        relevant_ids = {s.id for s in relevant_segs}
        relevant_pump_ids = {p.id for p in relevant_pumps}
        for s in self.segments.values():
            if s.id in relevant_ids:
                k = seg_ktot(s)
                k_used[s.id] = k
                q_out[s.id] = seg_q(s, heads, k)
            else:
                q_out[s.id] = 0.0
                k_used[s.id] = math.inf if not math.isfinite(seg_k_valve[s.id]) else seg_ktot(s)
        for pb in self.pump_branches.values():
            q_out[pb.id] = pump_q(pb, heads) if pb.id in relevant_pump_ids else 0.0
        leaks = {n: (leak_q(n, heads) if n not in blocked else 0.0) for n in self.leak_nodes}
        return HydraulicSolution(q=q_out, heads=heads, leak_q=leaks, blocked=blocked,
                                 segment_k=k_used, converged=converged)

    def _initial_head(self, node: str, tank_heads: dict[str, float], active_pumps, pumps) -> float:
        if node in self._prev_heads:
            return self._prev_heads[node]
        base = float(np.mean(list(tank_heads.values())))
        is_discharge = any(pb.n_to == node for pb in self.pump_branches.values())
        if is_discharge and active_pumps:
            s = max(pumps[pb.id].speed_ratio for pb in active_pumps)
            return base + 0.8 * s * s * self.plant.pumps[active_pumps[0].id].shutoff_head_m
        if node in self.leak_nodes:
            pid, _ = self.leak_nodes[node]
            p = self.plant.pipelines[pid]
            for end in (p.from_, p.to):
                if end in self._prev_heads:
                    return self._prev_heads[end]
        return base

    # ------------------------------------------------------------- post-solve
    def head_at(self, sol: HydraulicSolution, pipe_id: str, s: float) -> float:
        """Piezometric head at fractional position s along a pipeline."""
        segs = [seg for seg in self.segments.values() if seg.pipe_id == pipe_id]
        segs.sort(key=lambda x: x.s0)
        seg = next((x for x in segs if x.s0 <= s <= x.s1), segs[-1])
        hf, ht = sol.heads[seg.n_from], sol.heads[seg.n_to]
        k = sol.segment_k.get(seg.id, math.inf)
        if not math.isfinite(k):
            # isolated by a shut valve: line side of the valve sits at the free-node head
            if seg.valve_s is not None and seg.valve_s >= s:
                return hf
            return ht
        q = sol.q[seg.id]
        v = q / seg.area
        vh = v * abs(v) / (2 * self.g)
        h = hf
        if seg.valve_id is not None and seg.valve_s is not None and seg.valve_s <= s:
            kv = k - seg.f * seg.length / seg.diameter - seg.k_dist
            h -= kv * vh
        frac = (s - seg.s0) / max(seg.s1 - seg.s0, 1e-9)
        h -= (seg.f * seg.length / seg.diameter + seg.k_dist) * vh * frac
        return h

    def segment_at(self, pipe_id: str, s: float) -> PipeSegment:
        segs = sorted((x for x in self.segments.values() if x.pipe_id == pipe_id), key=lambda x: x.s0)
        return next((x for x in segs if x.s0 <= s <= x.s1), segs[-1])

    def pressure_bar(self, head: float, elevation: float, rho: float) -> float:
        return rho * self.g * (head - elevation) / 1e5

    def route_mass(self, sol: HydraulicSolution, tanks: dict, pumps: dict, min_q: float = 1e-7) -> None:
        """Route mass/energy through the solved network with exact mixing.

        Populates sol.streams, sol.leak_streams, sol.tank_in, sol.tank_out_mass.
        Tank outflow mass = rho_obs(tank) * Q_out. Each free node mixes its inflow
        streams and splits them in proportion to its volumetric outflows.
        """
        from simulation.process.properties import Stream

        edges: list[tuple[str, str, str, float]] = []  # (branch, up, down, |Q|)
        for sid, seg in self.segments.items():
            q = sol.q[sid]
            if abs(q) > min_q:
                up, dn = (seg.n_from, seg.n_to) if q > 0 else (seg.n_to, seg.n_from)
                edges.append((sid, up, dn, abs(q)))
        for pid, pb in self.pump_branches.items():
            q = sol.q[pid]
            if q > min_q:
                edges.append((pid, pb.n_from, pb.n_to, q))

        out_edges: dict[str, list] = {}
        in_count: dict[str, int] = {n: 0 for n in self.free_nodes}
        for e in edges:
            out_edges.setdefault(e[1], []).append(e)
            if e[2] in in_count and e[1] not in tanks:
                in_count[e[2]] += 1
        inbox: dict[str, Stream] = {n: Stream() for n in self.free_nodes}
        sol.tank_in = {t: [] for t in tanks}
        sol.tank_out_mass = {t: 0.0 for t in tanks}
        sol.streams = {}
        sol.leak_streams = {}

        def deliver(edge, stream):
            bid, _up, dn, _q = edge
            if bid in pumps:
                stream = stream.add_heat(pumps[bid].shaft_power_w)
            sol.streams[bid] = stream
            if dn in tanks:
                sol.tank_in[dn].append(stream)
            else:
                inbox[dn] = inbox[dn] + stream

        # sources: tanks
        for t, tank in tanks.items():
            outs = out_edges.get(t, [])
            qtot = sum(e[3] for e in outs)
            if qtot <= 0:
                continue
            m = tank.density_obs * qtot
            sol.tank_out_mass[t] = m
            base = tank.outflow_stream(m)
            for e in outs:
                deliver(e, base.scaled(e[3] / qtot))

        # free nodes in topological order (Kahn)
        ready = [n for n in self.free_nodes if in_count[n] == 0]
        processed: set[str] = set()
        remaining = dict(in_count)
        while ready:
            n = ready.pop()
            if n in processed:
                continue
            processed.add(n)
            s_in = inbox[n]
            outs = out_edges.get(n, [])
            ql = sol.leak_q.get(n, 0.0)
            qtot = sum(e[3] for e in outs) + ql
            if qtot > 0 and not s_in.empty:
                if ql > 0:
                    sol.leak_streams[n] = s_in.scaled(ql / qtot)
                for e in outs:
                    deliver(e, s_in.scaled(e[3] / qtot))
            for e in outs:
                if e[2] in remaining:
                    remaining[e[2]] -= 1
                    if remaining[e[2]] == 0:
                        ready.append(e[2])
