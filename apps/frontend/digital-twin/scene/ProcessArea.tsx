"use client";

import { Instance, Instances } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import { useTwinView } from "../store";
import { Label } from "../overlays/Label";

const PAINT = "#dedad3";
const STEEL = "#7c776e";

/** Pipe-rack spine linking the tank farm to the process units (two tiers, portal frames every 8 m). */
function PipeRack() {
  const frames = useMemo(() => {
    const out: number[] = [];
    for (let x = -120; x <= 125; x += 8) out.push(x);
    return out;
  }, []);
  const z = -100;
  const pipes: [number, number, string, number][] = [
    [4.5, -1.8, "#b9b4ab", 0.38], [4.5, 0, "#8f8a80", 0.3], [4.5, 1.8, "#c9a46b", 0.42],
    [7.6, -1.6, "#6f7d8c", 0.3], [7.6, 0.4, "#b9b4ab", 0.26], [7.6, 2.0, "#9d4b2d", 0.22],
  ];
  return (
    <group>
      <Instances limit={frames.length * 2} castShadow>
        <boxGeometry args={[0.45, 8.2, 0.45]} /><meshStandardMaterial color={STEEL} metalness={0.5} roughness={0.55} />
        {frames.flatMap((x) => [<Instance key={`a${x}`} position={[x, 4.1, z - 3]} />, <Instance key={`b${x}`} position={[x, 4.1, z + 3]} />])}
      </Instances>
      <Instances limit={frames.length * 2} castShadow>
        <boxGeometry args={[0.4, 0.4, 6.6]} /><meshStandardMaterial color={STEEL} metalness={0.5} roughness={0.55} />
        {frames.flatMap((x) => [<Instance key={`c${x}`} position={[x, 4.0, z]} />, <Instance key={`d${x}`} position={[x, 7.1, z]} />])}
      </Instances>
      {pipes.map(([y, dz, c, r], i) => (
        <mesh key={i} position={[2.5, y + r, z + dz]} rotation={[0, 0, Math.PI / 2]} castShadow>
          <cylinderGeometry args={[r, r, 245, 14]} /><meshStandardMaterial color={c} metalness={0.55} roughness={0.38} />
        </mesh>
      ))}
    </group>
  );
}

function Column({ x, z, r, h, label }: { x: number; z: number; r: number; h: number; label: string }) {
  const levels = useMemo(() => Array.from({ length: Math.floor((h - 6) / 8) }, (_, i) => 7 + i * 8), [h]);
  const vapour = useMemo(() => new THREE.CatmullRomCurve3([
    new THREE.Vector3(0, h + r * 0.7, 0), new THREE.Vector3(0, h + r + 2.5, 0), new THREE.Vector3(r + 3, h + r + 2, 0),
    new THREE.Vector3(r + 3.5, h * 0.6, 0), new THREE.Vector3(r + 3.5, 6, 0)]), [h, r]);
  return (
    <group position={[x, 0, z]}>
      <mesh position={[0, 2.5, 0]} castShadow receiveShadow>
        <cylinderGeometry args={[r * 1.08, r * 1.15, 5, 28]} /><meshStandardMaterial color="#a59d90" roughness={0.9} />
      </mesh>
      <mesh position={[0, 5 + (h - 5) / 2, 0]} castShadow receiveShadow>
        <cylinderGeometry args={[r, r, h - 5, 32]} /><meshStandardMaterial color={PAINT} metalness={0.35} roughness={0.45} />
      </mesh>
      <mesh position={[0, h, 0]} castShadow>
        <sphereGeometry args={[r, 32, 16, 0, Math.PI * 2, 0, Math.PI / 2]} /><meshStandardMaterial color={PAINT} metalness={0.35} roughness={0.45} />
      </mesh>
      {levels.map((y) => (
        <group key={y} position={[0, y, 0]}>
          <mesh rotation={[-Math.PI / 2, 0, 0]} castShadow>
            <ringGeometry args={[r, r + 1.4, 32, 1, 0, Math.PI * 1.3]} /><meshStandardMaterial color={STEEL} side={2} metalness={0.5} />
          </mesh>
          <mesh position={[0, 1.05, 0]} rotation={[Math.PI / 2, 0, 0]}>
            <torusGeometry args={[r + 1.35, 0.04, 4, 48, Math.PI * 1.3]} /><meshStandardMaterial color="#e2b43c" />
          </mesh>
        </group>
      ))}
      <mesh position={[-(r + 0.5), h / 2, 0]}><boxGeometry args={[0.12, h - 4, 0.8]} /><meshStandardMaterial color={STEEL} /></mesh>
      <mesh castShadow>
        <tubeGeometry args={[vapour, 64, Math.max(0.35, r * 0.2), 10, false]} /><meshStandardMaterial color="#b9b4ab" metalness={0.5} />
      </mesh>
      <Label position={[0, h + r + 4.5, 0]} center zIndexRange={[5, 0]}>
        <div className="pointer-events-none whitespace-nowrap rounded-full bg-white/70 px-2 py-0.5 text-[10px] text-ink-3 ring-1 ring-black/5">{label}</div>
      </Label>
    </group>
  );
}

function Heater({ x, z }: { x: number; z: number }) {
  return (
    <group position={[x, 0, z]}>
      <mesh position={[0, 7, 0]} castShadow receiveShadow><boxGeometry args={[18, 14, 12]} /><meshStandardMaterial color="#b9ad9b" roughness={0.85} /></mesh>
      <mesh position={[0, 17, 0]} castShadow><boxGeometry args={[10, 6, 8]} /><meshStandardMaterial color="#a89c8a" roughness={0.85} /></mesh>
      {[-3, 3].map((dx) => (
        <group key={dx} position={[dx, 0, 0]}>
          <mesh position={[0, 34, 0]} castShadow><cylinderGeometry args={[1.1, 1.4, 34, 18]} /><meshStandardMaterial color="#c7c1b6" metalness={0.3} /></mesh>
          {[46, 49].map((y) => (
            <mesh key={y} position={[0, y, 0]}><cylinderGeometry args={[1.12, 1.12, 1.5, 18]} /><meshStandardMaterial color="#b8402a" /></mesh>
          ))}
        </group>
      ))}
      <Label position={[0, 54, 0]} center zIndexRange={[5, 0]}>
        <div className="pointer-events-none whitespace-nowrap rounded-full bg-white/70 px-2 py-0.5 text-[10px] text-ink-3 ring-1 ring-black/5">Crude heater</div>
      </Label>
    </group>
  );
}

function Sphere({ x, z, r }: { x: number; z: number; r: number }) {
  const legs = useMemo(() => Array.from({ length: 10 }, (_, i) => (i / 10) * Math.PI * 2), []);
  const cy = r + 3;
  return (
    <group position={[x, 0, z]}>
      <mesh position={[0, cy, 0]} castShadow receiveShadow>
        <sphereGeometry args={[r, 40, 28]} /><meshStandardMaterial color="#f0eee9" metalness={0.3} roughness={0.38} />
      </mesh>
      <mesh position={[0, cy, 0]} rotation={[Math.PI / 2, 0, 0]}><torusGeometry args={[r * 1.002, 0.12, 6, 64]} /><meshStandardMaterial color="#cfcac2" /></mesh>
      <Instances limit={legs.length} castShadow>
        <cylinderGeometry args={[0.32, 0.38, cy, 10]} /><meshStandardMaterial color="#d9d4cb" />
        {legs.map((a) => <Instance key={a} position={[Math.cos(a) * r * 0.97, cy / 2, Math.sin(a) * r * 0.97]} />)}
      </Instances>
      <mesh position={[0, cy + r + 0.3, 0]}><cylinderGeometry args={[1.8, 1.8, 0.25, 20]} /><meshStandardMaterial color={STEEL} /></mesh>
    </group>
  );
}

function CoolingTower({ x, z }: { x: number; z: number }) {
  const geo = useMemo(() => {
    const pts: THREE.Vector2[] = [];
    for (let i = 0; i <= 24; i++) {
      const y = (i / 24) * 52;
      const t = (y - 38) / 38;
      pts.push(new THREE.Vector2(14.5 * Math.sqrt(1 + 1.25 * t * t) - 0.2, y));
    }
    return new THREE.LatheGeometry(pts, 48);
  }, []);
  return (
    <mesh position={[x, 0, z]} geometry={geo} castShadow receiveShadow>
      <meshStandardMaterial color="#d3cdc2" roughness={0.95} side={2} />
    </mesh>
  );
}

function Flare({ x, z, h }: { x: number; z: number; h: number }) {
  const flame = useRef<THREE.Group>(null);
  const tod = useTwinView((s) => s.timeOfDay);
  useFrame(({ clock }) => {
    if (!flame.current) return;
    const t = clock.elapsedTime;
    const k = 1 + 0.12 * Math.sin(t * 9.1) + 0.08 * Math.sin(t * 17.3);
    flame.current.scale.set(1 + 0.08 * Math.sin(t * 6.3), k, 1 + 0.08 * Math.cos(t * 5.1));
    flame.current.rotation.z = 0.12 * Math.sin(t * 1.3);
  });
  return (
    <group position={[x, 0, z]}>
      <mesh position={[0, h / 2, 0]} castShadow><cylinderGeometry args={[0.7, 1.1, h, 14]} /><meshStandardMaterial color="#b9b4ab" metalness={0.4} /></mesh>
      {[0, 2.09, 4.19].map((a) => (
        <mesh key={a} position={[Math.cos(a) * 5, h * 0.35, Math.sin(a) * 5]} rotation={[Math.sin(a) * 0.17, 0, -Math.cos(a) * 0.17]}>
          <cylinderGeometry args={[0.18, 0.25, h * 0.7, 6]} /><meshStandardMaterial color="#9d4b2d" />
        </mesh>
      ))}
      <mesh position={[0, h + 0.6, 0]}><cylinderGeometry args={[1.1, 0.8, 1.2, 14]} /><meshStandardMaterial color="#6f6a62" /></mesh>
      <group ref={flame} position={[0, h + 1.2, 0]}>
        <mesh position={[0, 3.2, 0]}>
          <coneGeometry args={[1.7, 6.5, 16]} />
          <meshBasicMaterial color="#ffb347" transparent opacity={0.9} toneMapped={false} blending={THREE.AdditiveBlending} depthWrite={false} />
        </mesh>
        <mesh position={[0, 2.3, 0]}>
          <coneGeometry args={[0.9, 4.2, 14]} />
          <meshBasicMaterial color="#fff2c4" transparent opacity={0.95} toneMapped={false} blending={THREE.AdditiveBlending} depthWrite={false} />
        </mesh>
      </group>
      {tod !== "day" && <pointLight position={[0, h + 4, 0]} intensity={tod === "night" ? 2600 : 900} distance={200} decay={1.6} color="#ff9a4a" />}
    </group>
  );
}

/** Decorative refinery process area behind the modelled tank farm (context only - not simulated). */
export function ProcessArea() {
  return (
    <group>
      <PipeRack />
      <mesh position={[-15, 0.07, -142]} receiveShadow><boxGeometry args={[110, 0.14, 50]} /><meshStandardMaterial color="#d9d3c7" roughness={0.9} /></mesh>
      <Column x={-48} z={-138} r={3.4} h={58} label="Crude distillation" />
      <Column x={-28} z={-150} r={2.5} h={44} label="Stabiliser" />
      <Column x={-11} z={-134} r={1.9} h={34} label="Naphtha splitter" />
      <Heater x={22} z={-142} />
      <Sphere x={-108} z={-138} r={8} />
      <Sphere x={-108} z={-170} r={8} />
      <CoolingTower x={70} z={-176} />
      <Flare x={138} z={-190} h={72} />
      <Label position={[-15, 0.5, -112]} center zIndexRange={[5, 0]}>
        <div className="pointer-events-none whitespace-nowrap rounded-full bg-dark/70 px-2.5 py-1 text-[10px] text-white/80">Refinery process area · context only, not simulated</div>
      </Label>
    </group>
  );
}
