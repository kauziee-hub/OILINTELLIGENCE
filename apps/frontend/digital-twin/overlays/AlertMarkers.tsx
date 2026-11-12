"use client";

import { useFrame } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import type { Alert, TwinState } from "@/services/types";
import { pipePath, pointAt, valveSpot } from "../geometry";
import { Label } from "./Label";

function anchor(assetId: string, state: TwinState): THREE.Vector3 | null {
  if (state.tanks[assetId]) {
    const t = state.tanks[assetId];
    return new THREE.Vector3(t.position[0], t.height_m + 9, t.position[2]);
  }
  if (state.pipelines[assetId]) return pointAt(pipePath(state.pipelines[assetId]), 0.5).add(new THREE.Vector3(0, 5, 0));
  if (state.pumps[assetId]) {
    const p = state.pumps[assetId].position;
    return new THREE.Vector3(p[0], 6.2, p[2]);
  }
  if (state.valves[assetId]) {
    const s = valveSpot(state, assetId);
    return s ? s.pos.clone().add(new THREE.Vector3(0, 5.5, 0)) : null;
  }
  if (state.junctions[assetId]) {
    const j = state.junctions[assetId].position;
    return new THREE.Vector3(j[0], 5.5, j[2]);
  }
  if (state.sensors[assetId]) return anchor(state.sensors[assetId].asset_id, state);
  return null;
}

/** Pulsing beacon: ring + light shaft down to the asset. */
function Beacon({ pos, color }: { pos: THREE.Vector3; color: string }) {
  const ring = useRef<THREE.Mesh>(null);
  useFrame(({ clock }) => {
    if (!ring.current) return;
    const k = 1 + 0.3 * Math.sin(clock.elapsedTime * 4);
    ring.current.scale.set(k, k, k);
  });
  return (
    <group position={pos}>
      <mesh ref={ring} rotation={[Math.PI / 2, 0, 0]}>
        <torusGeometry args={[1.8, 0.24, 10, 40]} />
        <meshBasicMaterial color={color} toneMapped={false} />
      </mesh>
      <mesh position={[0, -pos.y / 2, 0]}>
        <cylinderGeometry args={[0.35, 1.4, pos.y, 16, 1, true]} />
        <meshBasicMaterial color={color} transparent opacity={0.16} depthWrite={false} side={2} toneMapped={false} />
      </mesh>
    </group>
  );
}

/** Diagnosed abnormalities (from the monitoring / investigation pipeline). */
export function AlertMarkers({ alerts, state }: { alerts: Alert[]; state: TwinState }) {
  const seen = new Set<string>();
  return (
    <>
      {alerts.map((a) => {
        if (seen.has(a.asset_id)) return null;
        seen.add(a.asset_id);
        const p = anchor(a.asset_id, state);
        if (!p) return null;
        const color = a.severity === "CRITICAL" ? "#e0452b" : "#e2a93b";
        return (
          <group key={a.asset_id}>
            <Beacon pos={p} color={color} />
            <Label position={[p.x, p.y + 2.6, p.z]} center zIndexRange={[30, 0]}>
              <div className="pointer-events-none whitespace-nowrap rounded-full px-2.5 py-1 text-[10.5px] font-medium text-white shadow-lg"
                style={{ background: color }}>
                ⚠ {a.asset_id} · {a.top_hypothesis && !["NORMAL", "UNKNOWN"].includes(a.top_hypothesis)
                  ? a.top_hypothesis.replace(/_/g, " ").toLowerCase() : "under investigation"}
              </div>
            </Label>
          </group>
        );
      })}
    </>
  );
}

const dropGeo = new THREE.SphereGeometry(0.11, 6, 6);
const dropMat = new THREE.MeshStandardMaterial({ color: "#2b1806", roughness: 0.2 });

/** Simulated leak locations from SIMULATOR GROUND TRUTH (simulation lab view only): droplet spray + pool. */
function Spray({ at }: { at: THREE.Vector3 }) {
  const N = 36;
  const drops = useMemo(() => Array.from({ length: N }, (_, i) => ({
    a: (i / N) * Math.PI * 2 + Math.random() * 0.3, v: 2.5 + Math.random() * 2, phase: Math.random(),
  })), []);
  const group = useRef<THREE.Group>(null);
  const pool = useRef<THREE.Mesh>(null);
  useFrame(({ clock }) => {
    const t = clock.elapsedTime;
    group.current?.children.forEach((c, i) => {
      const d = drops[i];
      const u = (t * 0.9 + d.phase) % 1;
      c.position.set(Math.cos(d.a) * d.v * u * 0.6, d.v * u - 4.5 * u * u, Math.sin(d.a) * d.v * u * 0.6);
    });
    if (pool.current) pool.current.scale.setScalar(1 + 0.06 * Math.sin(t * 2));
  });
  return (
    <group position={at}>
      <group ref={group}>
        {drops.map((_, i) => <mesh key={i} geometry={dropGeo} material={dropMat} />)}
      </group>
      <mesh ref={pool} position={[0, -at.y + 0.08, 0]} rotation={[-Math.PI / 2, 0, 0]}>
        <circleGeometry args={[3.2, 32]} />
        <meshStandardMaterial color="#1d1208" roughness={0.15} metalness={0.2} transparent opacity={0.85} />
      </mesh>
    </group>
  );
}

export function TruthLeakIndicator({ leaks, state }: {
  leaks: { pipe: string; position: number; rate_th: number }[]; state: TwinState;
}) {
  return (
    <>
      {leaks.map((l) => {
        const pl = state.pipelines[l.pipe];
        const tk = state.tanks[l.pipe];
        const p = pl ? pointAt(pipePath(pl), l.position) : tk ? new THREE.Vector3(tk.position[0], 0.8, tk.position[2] + tk.diameter_m / 2 + 0.3) : null;
        if (!p) return null;
        return (
          <group key={l.pipe}>
            <Spray at={p} />
            <Label position={[p.x, p.y + 4, p.z]} center zIndexRange={[30, 0]}>
              <div className="pointer-events-none whitespace-nowrap rounded-full bg-info px-2.5 py-1 text-[10.5px] font-medium text-white shadow-lg">
                Simulated leak (ground truth) · {l.pipe} · {l.rate_th.toFixed(2)} t/h
              </div>
            </Label>
          </group>
        );
      })}
    </>
  );
}
