import * as THREE from "three";
import type { PipeState, TwinState, Vec3 } from "@/services/types";

/** Pipes run on concrete sleepers; the plant definition gives the schematic route at grade. */
export const PIPE_Y = 1.35;

/** Polyline helpers: pipes are drawn along the schematic path from the plant definition. */
export function polylineLength(pts: Vec3[]): number {
  let L = 0;
  for (let i = 1; i < pts.length; i++) L += new THREE.Vector3(...pts[i]).distanceTo(new THREE.Vector3(...pts[i - 1]));
  return L;
}

/** 3D route of a pipeline as rendered (elevated onto the sleepers). */
export function pipePath(pipe: Pick<PipeState, "path">): Vec3[] {
  return pipe.path.map(([x, , z]) => [x, PIPE_Y, z] as Vec3);
}

export function pointAt(pts: Vec3[], s: number): THREE.Vector3 {
  const L = polylineLength(pts);
  let target = Math.max(0, Math.min(1, s)) * L;
  for (let i = 1; i < pts.length; i++) {
    const a = new THREE.Vector3(...pts[i - 1]);
    const b = new THREE.Vector3(...pts[i]);
    const d = a.distanceTo(b);
    if (target <= d || i === pts.length - 1) return a.lerp(b, d > 0 ? Math.min(target / d, 1) : 0);
    target -= d;
  }
  return new THREE.Vector3(...pts[pts.length - 1]);
}

export function tangentAt(pts: Vec3[], s: number): THREE.Vector3 {
  const a = pointAt(pts, Math.max(0, s - 0.002));
  const b = pointAt(pts, Math.min(1, s + 0.002));
  const t = b.sub(a);
  return t.lengthSq() > 0 ? t.normalize() : new THREE.Vector3(1, 0, 0);
}

/** Polyline with rounded bends (piping elbows) as a continuous curve. */
export function pathCurve(pts: Vec3[], bend = 2.2): THREE.CurvePath<THREE.Vector3> {
  const cp = new THREE.CurvePath<THREE.Vector3>();
  const v = pts.map((p) => new THREE.Vector3(...p));
  if (v.length < 3) {
    cp.add(new THREE.LineCurve3(v[0], v[v.length - 1]));
    return cp;
  }
  let start = v[0].clone();
  for (let i = 1; i < v.length - 1; i++) {
    const prev = v[i - 1], cur = v[i], next = v[i + 1];
    const dIn = cur.clone().sub(prev), dOut = next.clone().sub(cur);
    const r = Math.min(bend, dIn.length() / 2, dOut.length() / 2);
    const a = cur.clone().sub(dIn.normalize().multiplyScalar(r));
    const b = cur.clone().add(dOut.normalize().multiplyScalar(r));
    if (a.distanceTo(start) > 1e-3) cp.add(new THREE.LineCurve3(start, a));
    cp.add(new THREE.QuadraticBezierCurve3(a, cur.clone(), b));
    start = b;
  }
  cp.add(new THREE.LineCurve3(start, v[v.length - 1]));
  return cp;
}

/** Quaternion turning +Y onto a direction (orients cylinders along a pipe). */
export function alongQuat(dir: THREE.Vector3): THREE.Quaternion {
  return new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir.clone().normalize());
}

export const SENSOR_LOCATION: Record<string, number> = {
  "FM-001": 0.92, "FM-002": 0.95, "FM-003": 0.95, "PT-001": 0.97, "PT-002": 0.05, "PT-003": 0.05,
  "PT-004": 0.95, "TT-004": 0.93, "DT-001": 0.9,
};

/** Where a valve sits: a few metres from the pipeline end it isolates (tank nozzle side). */
export function valveSpot(state: TwinState, valveId: string): { pos: THREE.Vector3; dir: THREE.Vector3 } | null {
  const v = state.valves[valveId];
  const pipe = v && state.pipelines[v.pipeline];
  if (!pipe) return null;
  const pts = pipePath(pipe);
  const L = polylineLength(pts);
  const s = v.position[0] === pipe.path[0][0] && v.position[2] === pipe.path[0][2] ? 3.5 / L : 1 - 3.5 / L;
  return { pos: pointAt(pts, s), dir: tangentAt(pts, s) };
}

/** World position of an instrument marker. */
export function sensorPosition(id: string, state: TwinState): THREE.Vector3 | null {
  const s = state.sensors[id];
  if (!s) return null;
  const a = s.asset_id;
  if (state.tanks[a]) {
    const t = state.tanks[a];
    const r = t.diameter_m / 2;
    const ang = s.type === "level" ? 0.55 : s.type === "temperature" ? 1.05 : 1.6;
    const y = s.type === "level" ? t.height_m + 1.2 : 2.6;
    return new THREE.Vector3(t.position[0] - (r + 0.6) * Math.cos(ang), y, t.position[2] - (r + 0.6) * Math.sin(ang));
  }
  if (state.pipelines[a]) {
    const p = pointAt(pipePath(state.pipelines[a]), SENSOR_LOCATION[id] ?? 0.5);
    return p.add(new THREE.Vector3(0, 1.25, 0));
  }
  if (state.junctions[a]) {
    const j = state.junctions[a].position;
    const off = s.type === "density" ? 2.4 : 1.0;
    return new THREE.Vector3(j[0] + off, PIPE_Y + 1.6, j[2] + 2.6);
  }
  if (state.pumps[a]) {
    const p = state.pumps[a].position;
    const off = { pump_power: 1.6, pump_speed: 0.4, pressure: -1.0, pump_status: 2.6 }[s.type] ?? 0;
    return new THREE.Vector3(p[0] + off, 2.9, p[2] + 1.3);
  }
  if (state.valves[a]) {
    const spot = valveSpot(state, a);
    if (!spot) return null;
    return spot.pos.clone().add(new THREE.Vector3(0, 3.3 + (s.type === "valve_command" ? 0.7 : 0), 0));
  }
  if (a === "TF-01") return new THREE.Vector3(-20, 8.5, 64);
  return null;
}
