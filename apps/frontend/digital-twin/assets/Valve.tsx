"use client";

import { useMemo, useState } from "react";
import { useLive } from "@/hooks/useLive";
import type { TwinState, ValveState } from "@/services/types";
import { alongQuat, valveSpot } from "../geometry";
import { Label } from "../overlays/Label";

/** Motor-operated gate valve (MOV) in line with its pipeline. The rising stem height follows the
 *  measured position feedback (ZT); the cap colour gives open / throttling / closed at a glance. */
export function Valve({ id, valve, state, alert }: {
  id: string; valve: ValveState; state: TwinState; alert?: "WARNING" | "CRITICAL";
}) {
  const [hover, setHover] = useState(false);
  const select = useLive((s) => s.setSelected);
  const selected = useLive((s) => s.selected?.kind === "valve" && s.selected.id === id);
  const spot = useMemo(() => valveSpot(state, id), [state, id]);
  if (!spot) return null;
  const pos = valve.position_pct ?? 0;
  const status = pos >= 95 ? "OPEN" : pos <= 1 ? "CLOSED" : "PARTIAL";
  const cap = alert ? (alert === "CRITICAL" ? "#e0452b" : "#e2a93b") : status === "OPEN" ? "#37c26e" :
    status === "PARTIAL" ? "#e2a93b" : "#8e897f";
  const q = alongQuat(spot.dir);
  const pipeR = 0.34 * ((state.pipelines[valve.pipeline]?.diameter_mm ?? 200) / 200);
  const stem = 0.15 + 0.85 * (pos / 100);
  const tint = selected ? "#d4572d" : hover ? "#ffffff" : "#000000";
  return (
    <group position={spot.pos}
      onClick={(e) => {
        e.stopPropagation();
        select({ kind: "valve", id });
      }}
      onPointerOver={(e) => {
        e.stopPropagation();
        setHover(true);
      }}
      onPointerOut={() => setHover(false)}>
      {/* body + flanges, aligned with the pipe */}
      <group quaternion={q}>
        <mesh castShadow>
          <cylinderGeometry args={[pipeR * 1.9, pipeR * 1.9, 1.1, 24]} />
          <meshStandardMaterial color="#4f5e6e" metalness={0.5} roughness={0.4} emissive={tint} emissiveIntensity={selected ? 0.3 : hover ? 0.08 : 0} />
        </mesh>
        {[-0.62, 0.62].map((y) => (
          <mesh key={y} position={[0, y, 0]}>
            <cylinderGeometry args={[pipeR * 2.15, pipeR * 2.15, 0.16, 24]} /><meshStandardMaterial color="#7b766c" metalness={0.6} />
          </mesh>
        ))}
      </group>
      {/* bonnet, yoke, actuator, handwheel */}
      <mesh position={[0, 0.85, 0]} castShadow>
        <cylinderGeometry args={[0.3, 0.38, 1.0, 16]} /><meshStandardMaterial color="#4f5e6e" metalness={0.5} roughness={0.4} />
      </mesh>
      <mesh position={[0, 1.65, 0]} castShadow>
        <boxGeometry args={[0.95, 0.75, 0.85]} /><meshStandardMaterial color="#2f4f73" metalness={0.35} roughness={0.5} />
      </mesh>
      <mesh position={[0.62, 1.65, 0]} rotation={[0, 0, Math.PI / 2]}>
        <cylinderGeometry args={[0.24, 0.24, 0.55, 16]} /><meshStandardMaterial color="#2f4f73" metalness={0.35} />
      </mesh>
      <mesh position={[0, 1.65, 0.55]}>
        <torusGeometry args={[0.38, 0.05, 6, 24]} /><meshStandardMaterial color="#c4432b" />
      </mesh>
      {/* rising stem = measured opening */}
      <mesh position={[0, 2.05 + stem / 2, 0]}>
        <cylinderGeometry args={[0.07, 0.07, stem, 8]} /><meshStandardMaterial color="#d8d3ca" metalness={0.8} roughness={0.2} />
      </mesh>
      <mesh position={[0, 2.12 + stem, 0]}>
        <sphereGeometry args={[0.17, 14, 10]} />
        <meshStandardMaterial color={cap} emissive={cap} emissiveIntensity={status === "CLOSED" && !alert ? 0.1 : 1.3} toneMapped={false} />
      </mesh>
      <Label position={[0, 3.6, 0]} center zIndexRange={[10, 0]}>
        <div className="pointer-events-none whitespace-nowrap rounded-full bg-white/90 px-2 py-0.5 text-[10.5px] text-ink-2 shadow-sm ring-1 ring-black/5">
          <span className="font-semibold text-ink">{id}</span> <span className="num">{Math.round(pos)}%</span>
        </div>
      </Label>
    </group>
  );
}
