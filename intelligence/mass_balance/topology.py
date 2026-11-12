"""Measurement-graph topology: control volumes induced by meters and valves.

The facility graph G = (V, E) is cut at (a) every flow meter that registered
flow in the window and (b) every valve that was shut for the whole window.
Each connected component is a control volume (CV) with an explicit
measurement boundary:

    R_cv = sum(metered inflow) - sum(metered outflow) - sum(dM_tanks in cv)

Sum of all CV residuals equals the facility residual (-sum dM_tanks), so the
elementary CVs form a non-redundant basis of all valid balances.
A meter that registered no flow carries no information and is not a cut.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from industrial.schemas.plant import PlantConfig


@dataclass(frozen=True)
class SubSegment:
    pipe_id: str
    s0: float
    s1: float

    @property
    def id(self) -> str:
        return f"{self.pipe_id}[{self.s0:.2f}-{self.s1:.2f}]"


@dataclass
class ControlVolume:
    cv_id: str
    tanks: list[str] = field(default_factory=list)
    junctions: list[str] = field(default_factory=list)
    segments: list[SubSegment] = field(default_factory=list)
    meters_in: list[str] = field(default_factory=list)
    meters_out: list[str] = field(default_factory=list)

    @property
    def observable(self) -> bool:
        return bool(self.tanks or self.meters_in or self.meters_out)

    def meter_sign(self, meter: str) -> int:
        if meter in self.meters_in:
            return 1
        if meter in self.meters_out:
            return -1
        return 0


@dataclass
class ValveCut:
    valve_id: str
    pipe_id: str
    line_cv: str | None
    tank_cv: str | None
    tank_id: str | None


@dataclass
class Topology:
    cvs: dict[str, ControlVolume]
    element_cv: dict[str, str]          # element id -> cv id
    valve_cuts: list[ValveCut]
    active_meters: list[str]
    open_valves: list[str]

    def cv_of_tank(self, tank: str) -> str | None:
        return self.element_cv.get(tank)

    def cv_of_segment(self, seg: SubSegment) -> str | None:
        return self.element_cv.get(seg.id)


class TopologyBuilder:
    def __init__(self, plant: PlantConfig):
        self.plant = plant
        self.pipe_points: dict[str, list[tuple[float, str, str]]] = {}
        for pid in plant.pipelines:
            pts: list[tuple[float, str, str]] = []
            for sid, s in plant.sensors.items():
                if s.type == "flow" and s.asset == pid:
                    pts.append((float(s.location or 0.5), "meter", sid))
            for vid, v in plant.valves.items():
                if v.pipeline == pid:
                    pts.append((float(v.position_on_line), "valve", vid))
            pts.sort()
            self.pipe_points[pid] = pts
        self.segments: dict[str, list[SubSegment]] = {}
        for pid, pts in self.pipe_points.items():
            cuts = sorted({0.0, 1.0} | {p[0] for p in pts})
            self.segments[pid] = [SubSegment(pid, a, b) for a, b in zip(cuts[:-1], cuts[1:]) if b > a]

    def build(self, open_valves: set[str], active_meters: set[str]) -> Topology:
        P = self.plant
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

        for t in P.tanks:
            find(t)
        for j in P.junctions:
            find(j)
        meter_sides: dict[str, tuple[str, str]] = {}
        valve_sides: dict[str, tuple[str, str, str | None, str]] = {}
        for pid, segs in self.segments.items():
            pipe = P.pipelines[pid]
            for sg in segs:
                find(sg.id)
            pts = self.pipe_points[pid]

            def seg_before(s):
                c = [x for x in segs if abs(x.s1 - s) < 1e-9]
                return c[0].id if c else None

            def seg_after(s):
                c = [x for x in segs if abs(x.s0 - s) < 1e-9]
                return c[0].id if c else None

            # connections at each point position (endpoints included)
            positions = sorted({0.0, 1.0} | {p[0] for p in pts})
            for s in positions:
                before = seg_before(s) if s > 0 else pipe.from_
                after = seg_after(s) if s < 1 else pipe.to
                items = [p for p in pts if abs(p[0] - s) < 1e-9]
                cut = False
                for _, kind, ident in items:
                    if kind == "meter" and ident in active_meters:
                        cut = True
                        meter_sides[ident] = (before, after)
                    if kind == "valve" and ident not in open_valves:
                        cut = True
                        tank_side = P.valves[ident].tank_side
                        valve_sides[ident] = (before, after, tank_side, pid)
                if not cut and before and after:
                    union(before, after)
        # pump station connects suction and discharge manifolds
        for pu in P.pumps.values():
            union(pu.from_, pu.to)

        groups: dict[str, list[str]] = {}
        for el in list(parent):
            groups.setdefault(find(el), []).append(el)
        cvs: dict[str, ControlVolume] = {}
        element_cv: dict[str, str] = {}
        seg_lookup = {sg.id: sg for segs in self.segments.values() for sg in segs}
        for root, els in groups.items():
            tanks = sorted(e for e in els if e in P.tanks)
            juncs = sorted(e for e in els if e in P.junctions)
            segs = sorted((seg_lookup[e] for e in els if e in seg_lookup), key=lambda s: s.id)
            if tanks:
                name = "CV:" + "+".join(tanks) + ("+MANIFOLD" if juncs else "")
            elif juncs:
                name = "CV:MANIFOLD"
            else:
                name = "CV:" + segs[0].id
            cv = ControlVolume(name, tanks, juncs, segs)
            cvs[name] = cv
            for e in els:
                element_cv[e] = name
        for m, (up, dn) in meter_sides.items():
            cvs[element_cv[up]].meters_out.append(m)
            cvs[element_cv[dn]].meters_in.append(m)
        valve_cuts = []
        for v, (before, after, tank_side, pid) in valve_sides.items():
            sides = [element_cv.get(before), element_cv.get(after)]
            tank_cv = element_cv.get(tank_side) if tank_side else None
            line_cv = next((s for s in sides if s != tank_cv), None)
            valve_cuts.append(ValveCut(v, pid, line_cv, tank_cv, tank_side))
        return Topology(cvs=cvs, element_cv=element_cv, valve_cuts=valve_cuts,
                        active_meters=sorted(active_meters), open_valves=sorted(open_valves))
