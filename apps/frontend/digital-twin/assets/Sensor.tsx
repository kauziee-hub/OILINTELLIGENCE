"use client";

import { useState } from "react";
import type * as THREE from "three";
import { useLive } from "@/hooks/useLive";
import { fmt } from "@/lib/format";
import type { SensorState } from "@/services/types";
import { Label } from "../overlays/Label";

const Q_COLOR: Record<string, string> = { GOOD: "#3fa7ff", UNCERTAIN: "#e2a93b", BAD: "#e0452b", STALE: "#8e897f" };

/** Field transmitter: stem, electronics head and a quality status light (blue good, amber uncertain,
 *  red bad, grey stale). */
export function SensorMarker({ id, sensor, position, showLabel }: {
  id: string; sensor: SensorState; position: THREE.Vector3; showLabel: boolean;
}) {
  const [hover, setHover] = useState(false);
  const select = useLive((s) => s.setSelected);
  const selected = useLive((s) => s.selected?.kind === "sensor" && s.selected.id === id);
  const c = Q_COLOR[sensor.quality] ?? Q_COLOR.GOOD;
  const k = hover || selected ? 1.35 : 1;
  return (
    <group position={position} scale={k}
      onClick={(e) => {
        e.stopPropagation();
        select({ kind: "sensor", id });
      }}
      onPointerOver={(e) => {
        e.stopPropagation();
        setHover(true);
      }}
      onPointerOut={() => setHover(false)}>
      <mesh position={[0, -0.55, 0]}>
        <cylinderGeometry args={[0.06, 0.06, 0.9, 6]} /><meshStandardMaterial color="#8f8a80" metalness={0.6} />
      </mesh>
      <mesh rotation={[Math.PI / 2, 0, 0]}>
        <cylinderGeometry args={[0.26, 0.26, 0.34, 18]} /><meshStandardMaterial color="#efece6" metalness={0.25} roughness={0.35} />
      </mesh>
      <mesh position={[0, 0, 0.19]} rotation={[Math.PI / 2, 0, 0]}>
        <cylinderGeometry args={[0.17, 0.17, 0.05, 18]} />
        <meshStandardMaterial color={c} emissive={c} emissiveIntensity={selected ? 2.2 : 1.2} toneMapped={false} />
      </mesh>
      <Label position={[0, 0.85, 0]} center zIndexRange={[20, 0]}>
        {/* label stays mounted (visibility toggled) so overlay roots are never torn down mid-render */}
        <div style={{ display: hover || selected || showLabel ? "block" : "none" }}
          className="pointer-events-none whitespace-nowrap rounded-full bg-white/95 px-2 py-0.5 text-[10.5px] text-ink shadow-sm ring-1 ring-black/5">
          <span className="font-semibold">{id}</span>{" "}
          <span className="num text-ink-2">{fmt(sensor.value, sensor.type === "level" ? 3 : 2)} {sensor.unit}</span>
          {sensor.quality !== "GOOD" && <span className="ml-1 font-medium text-warn">{sensor.quality.toLowerCase()}</span>}
        </div>
      </Label>
    </group>
  );
}
