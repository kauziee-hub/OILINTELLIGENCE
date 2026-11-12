"use client";

import { Instance, Instances } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useRef, useState } from "react";
import type * as THREE from "three";
import { useLive } from "@/hooks/useLive";
import { fmt } from "@/lib/format";
import type { PumpState } from "@/services/types";
import { Label } from "../overlays/Label";

const FINS = Array.from({ length: 14 }, (_, i) => (i / 14) * Math.PI * 2);

/** Horizontal end-suction centrifugal pump on a baseplate: volute, coupling guard, TEFC motor.
 *  Status: beacon on the terminal box + the motor fan visibly turning when running. */
export function Pump({ id, pump, alert }: { id: string; pump: PumpState; alert?: "WARNING" | "CRITICAL" }) {
  const fan = useRef<THREE.Group>(null);
  const [hover, setHover] = useState(false);
  const select = useLive((s) => s.setSelected);
  const selected = useLive((s) => s.selected?.kind === "pump" && s.selected.id === id);
  const running = pump.status === "RUNNING" || pump.status === "STARTING";
  useFrame((_, dt) => {
    if (fan.current && running) fan.current.rotation.x += dt * Math.max(2, ((pump.speed_rpm ?? 0) / 2960) * 22);
  });
  const beacon = alert ? (alert === "CRITICAL" ? "#e0452b" : "#e2a93b") : running ? "#37c26e" :
    pump.status === "UNKNOWN" ? "#8e897f" : "#9a958c";
  const tint = alert ? beacon : selected ? "#d4572d" : hover ? "#ffffff" : "#000000";
  const ti = alert ? 0.3 : selected ? 0.25 : hover ? 0.06 : 0;
  const p = pump.position;
  return (
    <group position={[p[0], 0, p[2]]}
      onClick={(e) => {
        e.stopPropagation();
        select({ kind: "pump", id });
      }}
      onPointerOver={(e) => {
        e.stopPropagation();
        setHover(true);
      }}
      onPointerOut={() => setHover(false)}>
      {/* plinth + baseplate */}
      <mesh position={[0, 0.2, 0]} receiveShadow castShadow>
        <boxGeometry args={[6.6, 0.4, 2.6]} /><meshStandardMaterial color="#d3ccbf" roughness={0.95} />
      </mesh>
      <mesh position={[0, 0.5, 0]} castShadow>
        <boxGeometry args={[6.0, 0.2, 1.9]} /><meshStandardMaterial color="#4c4a45" metalness={0.5} roughness={0.5} />
      </mesh>
      {/* volute casing + suction / discharge nozzles */}
      <group position={[-1.6, 1.35, 0]}>
        <mesh rotation={[0, 0, Math.PI / 2]} castShadow>
          <cylinderGeometry args={[0.92, 0.92, 0.8, 32]} />
          <meshStandardMaterial color="#365d86" metalness={0.45} roughness={0.42} emissive={tint} emissiveIntensity={ti} />
        </mesh>
        <mesh position={[-0.9, 0, 0]} rotation={[0, 0, Math.PI / 2]} castShadow>
          <cylinderGeometry args={[0.36, 0.42, 1.0, 20]} /><meshStandardMaterial color="#365d86" metalness={0.45} roughness={0.42} />
        </mesh>
        <mesh position={[-1.45, 0, 0]} rotation={[0, 0, Math.PI / 2]}>
          <cylinderGeometry args={[0.62, 0.62, 0.14, 24]} /><meshStandardMaterial color="#7b766c" metalness={0.6} />
        </mesh>
        <mesh position={[0, 1.05, 0]} castShadow>
          <cylinderGeometry args={[0.3, 0.34, 1.1, 20]} /><meshStandardMaterial color="#365d86" metalness={0.45} roughness={0.42} />
        </mesh>
        <mesh position={[0, 1.62, 0]}>
          <cylinderGeometry args={[0.55, 0.55, 0.12, 24]} /><meshStandardMaterial color="#7b766c" metalness={0.6} />
        </mesh>
        {/* bearing housing */}
        <mesh position={[0.75, -0.15, 0]} rotation={[0, 0, Math.PI / 2]}>
          <cylinderGeometry args={[0.32, 0.38, 0.8, 18]} /><meshStandardMaterial color="#365d86" metalness={0.45} roughness={0.42} />
        </mesh>
      </group>
      {/* coupling guard (safety yellow) */}
      <mesh position={[-0.25, 1.15, 0]} rotation={[0, 0, Math.PI / 2]} castShadow>
        <cylinderGeometry args={[0.42, 0.42, 0.85, 20, 1, false, 0, Math.PI]} />
        <meshStandardMaterial color="#e2b43c" roughness={0.6} side={2} />
      </mesh>
      {/* TEFC motor with cooling fins and fan cowl */}
      <group position={[1.35, 1.2, 0]}>
        <mesh rotation={[0, 0, Math.PI / 2]} castShadow>
          <cylinderGeometry args={[0.74, 0.74, 2.3, 32]} />
          <meshStandardMaterial color="#5d7c6b" metalness={0.35} roughness={0.5} emissive={tint} emissiveIntensity={ti} />
        </mesh>
        <Instances limit={FINS.length}>
          <boxGeometry args={[2.1, 0.06, 0.16]} />
          <meshStandardMaterial color="#526f60" metalness={0.35} roughness={0.5} />
          {FINS.map((a) => <Instance key={a} position={[0, Math.cos(a) * 0.8, Math.sin(a) * 0.8]} rotation={[a, 0, 0]} />)}
        </Instances>
        <mesh position={[1.35, 0, 0]} rotation={[0, 0, Math.PI / 2]}>
          <cylinderGeometry args={[0.66, 0.74, 0.45, 28]} /><meshStandardMaterial color="#4a5f53" metalness={0.4} />
        </mesh>
        <group ref={fan} position={[1.6, 0, 0]}>
          {[0, 1, 2, 3].map((k) => (
            <mesh key={k} rotation={[(k * Math.PI) / 4, 0, 0]}>
              <boxGeometry args={[0.05, 1.1, 0.12]} /><meshStandardMaterial color="#2c2b28" />
            </mesh>
          ))}
        </group>
        {/* terminal box + status beacon */}
        <mesh position={[-0.2, 0.9, 0]} castShadow>
          <boxGeometry args={[0.6, 0.42, 0.6]} /><meshStandardMaterial color="#4a5f53" />
        </mesh>
        <mesh position={[-0.2, 1.23, 0]}>
          <cylinderGeometry args={[0.13, 0.13, 0.24, 14]} />
          <meshStandardMaterial color={beacon} emissive={beacon} emissiveIntensity={running || alert ? 1.6 : 0.15} toneMapped={false} />
        </mesh>
      </group>
      <Label position={[0, 4.3, 0]} center zIndexRange={[10, 0]}>
        <div className="pointer-events-none whitespace-nowrap rounded-full bg-white/90 px-2.5 py-1 text-[11px] text-ink shadow-[0_6px_18px_-8px_rgba(0,0,0,0.35)] ring-1 ring-black/5">
          <span className="font-semibold">{id}</span>{" "}
          <span className={running ? "font-medium text-ok" : "text-ink-3"}>{pump.status.toLowerCase()}</span>{" "}
          <span className="num text-ink-3">{fmt(pump.power_kw, 0)} kW · {fmt(pump.discharge_bar, 1)} bar</span>
        </div>
      </Label>
    </group>
  );
}
