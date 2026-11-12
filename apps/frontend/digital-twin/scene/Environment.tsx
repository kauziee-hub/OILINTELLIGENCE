"use client";

import { Instance, Instances } from "@react-three/drei";
import { useMemo } from "react";
import type { TankState } from "@/services/types";
import { ATMOSPHERE, useTwinView } from "../store";

const ROAD = { x: 108, z: 80, w: 9 };          // ring road around the tank farm
const FENCE = { x0: -175, x1: 165, z0: -215, z1: 108 };

function Roads({ night }: { night: boolean }) {
  const asphalt = night ? "#2d2c2a" : "#57544e";
  const dashes = useMemo(() => {
    const out: { p: [number, number, number]; ry: number }[] = [];
    for (let x = -ROAD.x + 6; x < ROAD.x - 6; x += 9) {
      out.push({ p: [x, 0.07, ROAD.z], ry: 0 });
      out.push({ p: [x, 0.07, -ROAD.z], ry: 0 });
    }
    for (let z = -ROAD.z + 6; z < ROAD.z - 6; z += 9) {
      out.push({ p: [ROAD.x, 0.07, z], ry: Math.PI / 2 });
      out.push({ p: [-ROAD.x, 0.07, z], ry: Math.PI / 2 });
    }
    for (let z = -ROAD.z - 8; z > -175; z -= 9) out.push({ p: [0, 0.07, z], ry: Math.PI / 2 });
    return out;
  }, []);
  const seg = (x: number, z: number, sx: number, sz: number, k: string) => (
    <mesh key={k} position={[x, 0.04, z]} rotation={[-Math.PI / 2, 0, 0]} receiveShadow>
      <planeGeometry args={[sx, sz]} /><meshStandardMaterial color={asphalt} roughness={0.95} />
    </mesh>
  );
  return (
    <group>
      {seg(0, ROAD.z, 2 * ROAD.x + ROAD.w, ROAD.w, "n")}
      {seg(0, -ROAD.z, 2 * ROAD.x + ROAD.w, ROAD.w, "s")}
      {seg(ROAD.x, 0, ROAD.w, 2 * ROAD.z, "e")}
      {seg(-ROAD.x, 0, ROAD.w, 2 * ROAD.z, "w")}
      {seg(0, -128, ROAD.w, 96, "spur")}
      <Instances limit={dashes.length}>
        <planeGeometry args={[4, 0.3]} />
        <meshStandardMaterial color="#f1ede4" />
        {dashes.map((d, i) => <Instance key={i} position={d.p} rotation={[-Math.PI / 2, 0, d.ry]} />)}
      </Instances>
    </group>
  );
}

/** Secondary containment: concrete bund wall + gravel floor around each tank. */
function Bunds({ tanks }: { tanks: Record<string, TankState> }) {
  return (
    <>
      {Object.entries(tanks).map(([id, t]) => {
        const s = t.diameter_m + 16;
        return (
          <group key={id} position={[t.position[0], 0, t.position[2]]}>
            <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.03, 0]} receiveShadow>
              <planeGeometry args={[s, s]} /><meshStandardMaterial color="#b2a790" roughness={1} />
            </mesh>
            {[[0, s / 2, s, 0.6], [0, -s / 2, s, 0.6], [s / 2, 0, 0.6, s], [-s / 2, 0, 0.6, s]].map(([x, z, w, d], i) => (
              <mesh key={i} position={[x, 0.75, z]} castShadow receiveShadow>
                <boxGeometry args={[w, 1.5, d]} /><meshStandardMaterial color="#d8d2c6" roughness={0.9} />
              </mesh>
            ))}
            {/* access stair over the bund wall */}
            <mesh position={[s / 2 + 0.9, 0.85, 0]} rotation={[0, 0, Math.PI / 5]} castShadow>
              <boxGeometry args={[2.6, 0.12, 1.2]} /><meshStandardMaterial color="#8d897f" metalness={0.5} />
            </mesh>
            {/* drain sump */}
            <mesh position={[-s / 2 + 2.2, 0.05, -s / 2 + 2.2]}>
              <boxGeometry args={[1.6, 0.1, 1.6]} /><meshStandardMaterial color="#77736b" />
            </mesh>
          </group>
        );
      })}
    </>
  );
}

function Fence() {
  const posts = useMemo(() => {
    const out: [number, number, number][] = [];
    for (let x = FENCE.x0; x <= FENCE.x1; x += 7) out.push([x, 1.2, FENCE.z0], [x, 1.2, FENCE.z1]);
    for (let z = FENCE.z0 + 7; z < FENCE.z1; z += 7) out.push([FENCE.x0, 1.2, z], [FENCE.x1, 1.2, z]);
    return out;
  }, []);
  const W = FENCE.x1 - FENCE.x0, D = FENCE.z1 - FENCE.z0;
  return (
    <group>
      <Instances limit={posts.length}>
        <cylinderGeometry args={[0.07, 0.07, 2.4, 5]} /><meshStandardMaterial color="#77736b" metalness={0.5} />
        {posts.map((p, i) => <Instance key={i} position={p} />)}
      </Instances>
      {[[(FENCE.x0 + FENCE.x1) / 2, FENCE.z0, W, 0], [(FENCE.x0 + FENCE.x1) / 2, FENCE.z1, W, 0],
        [FENCE.x0, (FENCE.z0 + FENCE.z1) / 2, D, Math.PI / 2], [FENCE.x1, (FENCE.z0 + FENCE.z1) / 2, D, Math.PI / 2]].map(([x, z, w, r], i) => (
        <mesh key={i} position={[x, 1.25, z]} rotation={[0, r, 0]}>
          <planeGeometry args={[w, 2.2]} /><meshStandardMaterial color="#7d7a74" transparent opacity={0.18} side={2} depthWrite={false} />
        </mesh>
      ))}
    </group>
  );
}

function Lamps({ on }: { on: boolean }) {
  const spots = useMemo(() => {
    const out: [number, number][] = [];
    for (let x = -ROAD.x; x <= ROAD.x; x += 36) out.push([x, ROAD.z + 6], [x, -ROAD.z - 6]);
    for (let z = -ROAD.z + 30; z < ROAD.z; z += 36) out.push([ROAD.x + 6, z], [-ROAD.x - 6, z]);
    return out;
  }, []);
  return (
    <group>
      <Instances limit={spots.length} castShadow>
        <cylinderGeometry args={[0.13, 0.18, 9, 6]} /><meshStandardMaterial color="#6c6862" metalness={0.5} />
        {spots.map(([x, z], i) => <Instance key={i} position={[x, 4.5, z]} />)}
      </Instances>
      <Instances limit={spots.length}>
        <boxGeometry args={[1.0, 0.25, 0.5]} />
        <meshStandardMaterial color={on ? "#ffd59a" : "#d8d3c9"} emissive={on ? "#ffc06a" : "#000000"} emissiveIntensity={on ? 2.5 : 0} toneMapped={!on} />
        {spots.map(([x, z], i) => <Instance key={i} position={[x, 9.05, z]} />)}
      </Instances>
      {on && spots.filter((_, i) => i % 3 === 0).map(([x, z], i) => (
        <pointLight key={i} position={[x, 8.5, z]} intensity={60} distance={38} decay={1.6} color="#ffc88a" />
      ))}
    </group>
  );
}

function ControlRoom({ night }: { night: boolean }) {
  return (
    <group position={[0, 0, 63]}>
      <mesh position={[0, 3, 0]} castShadow receiveShadow>
        <boxGeometry args={[26, 6, 9]} /><meshStandardMaterial color="#ddd7cc" roughness={0.8} />
      </mesh>
      <mesh position={[0, 3.6, -4.52]}>
        <planeGeometry args={[23, 1.4]} />
        <meshStandardMaterial color={night ? "#ffd9a0" : "#4a5563"} emissive={night ? "#ffbf70" : "#000000"} emissiveIntensity={night ? 1.4 : 0} />
      </mesh>
      <mesh position={[0, 6.2, 0]} castShadow><boxGeometry args={[26.6, 0.4, 9.6]} /><meshStandardMaterial color="#a39b8e" /></mesh>
      <mesh position={[9, 7.6, 1]} castShadow><boxGeometry args={[4, 2.6, 3]} /><meshStandardMaterial color="#9b9488" /></mesh>
      <mesh position={[-11, 8.5, -2]}><cylinderGeometry args={[0.08, 0.08, 5, 6]} /><meshStandardMaterial color="#77736b" /></mesh>
    </group>
  );
}

function Trees() {
  const trees = useMemo(() => {
    const out: [number, number, number][] = [];
    let s = 7;
    const rnd = () => ((s = (s * 9301 + 49297) % 233280) / 233280);
    for (let x = -170; x < 160; x += 9) out.push([x + rnd() * 4, 0, 118 + rnd() * 14]);
    for (let z = -200; z < 100; z += 10) out.push([178 + rnd() * 10, 0, z + rnd() * 5], [-188 - rnd() * 10, 0, z + rnd() * 5]);
    return out.map(([x, y, z]) => ({ p: [x, y, z] as [number, number, number], k: 0.8 + rnd() * 0.6 }));
  }, []);
  return (
    <group>
      <Instances limit={trees.length} castShadow>
        <coneGeometry args={[2.6, 8, 7]} /><meshStandardMaterial color="#7d8a62" roughness={1} />
        {trees.map((t, i) => <Instance key={i} position={[t.p[0], 5.2 * t.k, t.p[2]]} scale={t.k} />)}
      </Instances>
      <Instances limit={trees.length}>
        <cylinderGeometry args={[0.3, 0.4, 2.4, 5]} /><meshStandardMaterial color="#6e5a44" />
        {trees.map((t, i) => <Instance key={i} position={[t.p[0], 1.2, t.p[2]]} scale={t.k} />)}
      </Instances>
    </group>
  );
}

/** Ground, roads, bunds, fence, lighting columns, control building and landscaping. */
export function Environment({ tanks }: { tanks: Record<string, TankState> }) {
  const tod = useTwinView((s) => s.timeOfDay);
  const atm = ATMOSPHERE[tod];
  const night = tod === "night";
  return (
    <group>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.02, 0]} receiveShadow>
        <planeGeometry args={[1600, 1600]} /><meshStandardMaterial color={atm.ground} roughness={1} />
      </mesh>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[-5, 0.0, -53]} receiveShadow>
        <planeGeometry args={[FENCE.x1 - FENCE.x0 - 4, FENCE.z1 - FENCE.z0 - 4]} />
        <meshStandardMaterial color={night ? "#45423c" : "#c8bfac"} roughness={1} />
      </mesh>
      {/* manifold / pump-station concrete pad */}
      <mesh position={[-2, 0.06, 0]} receiveShadow>
        <boxGeometry args={[34, 0.12, 22]} /><meshStandardMaterial color={night ? "#5a5650" : "#dcd5c8"} roughness={0.9} />
      </mesh>
      <Roads night={night} />
      <Bunds tanks={tanks} />
      <Fence />
      <Lamps on={atm.lamps} />
      <ControlRoom night={night || tod === "dusk"} />
      <Trees />
    </group>
  );
}
