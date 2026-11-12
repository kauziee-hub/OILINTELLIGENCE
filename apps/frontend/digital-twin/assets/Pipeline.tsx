"use client";

import { Instance, Instances } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useState } from "react";
import * as THREE from "three";
import { useLive } from "@/hooks/useLive";
import type { PipeState } from "@/services/types";
import { alongQuat, pathCurve, pipePath, PIPE_Y, pointAt, SENSOR_LOCATION, tangentAt } from "../geometry";
import { useTwinView } from "../store";

let chevrons: THREE.CanvasTexture | null = null;
/** Chevron strip drawn once on a canvas; scrolled along the tube's u coordinate to show flow direction. */
function chevronTexture(): THREE.CanvasTexture {
  if (chevrons) return chevrons;
  const c = document.createElement("canvas");
  c.width = 128;
  c.height = 32;
  const g = c.getContext("2d")!;
  g.clearRect(0, 0, 128, 32);
  g.fillStyle = "#ffffff";
  for (const x0 of [8, 72]) {
    g.beginPath();
    g.moveTo(x0, 4);
    g.lineTo(x0 + 22, 16);
    g.lineTo(x0, 28);
    g.lineTo(x0 + 12, 28);
    g.lineTo(x0 + 34, 16);
    g.lineTo(x0 + 12, 4);
    g.closePath();
    g.fill();
  }
  chevrons = new THREE.CanvasTexture(c);
  chevrons.wrapS = THREE.RepeatWrapping;
  chevrons.wrapT = THREE.RepeatWrapping;
  return chevrons;
}

function FlowRibbon({ curve, radius, flow, inferred }: {
  curve: THREE.CurvePath<THREE.Vector3>; radius: number; flow: number; inferred: boolean;
}) {
  const len = curve.getLength();
  const tex = useMemo(() => {
    const t = chevronTexture().clone();
    t.needsUpdate = true;
    t.repeat.set(Math.max(1, len / 4.5), 3);
    return t;
  }, [len]);
  useEffect(() => () => tex.dispose(), [tex]);
  useFrame((_, dt) => {
    tex.offset.x -= dt * (0.25 + (flow / 150) * 0.9);
  });
  return (
    <mesh>
      <tubeGeometry args={[curve as unknown as THREE.Curve<THREE.Vector3>, Math.max(48, Math.round(len * 2)), radius * 1.07, 16, false]} />
      <meshBasicMaterial map={tex} color={inferred ? "#e2ddd4" : "#ff9a62"} transparent opacity={0.95} depthWrite={false} toneMapped={false} />
    </mesh>
  );
}

export function Pipeline({ id, pipe, alert }: { id: string; pipe: PipeState; alert?: "WARNING" | "CRITICAL" }) {
  const [hover, setHover] = useState(false);
  const select = useLive((s) => s.setSelected);
  const selected = useLive((s) => s.selected?.kind === "pipeline" && s.selected.id === id);
  const showFlow = useTwinView((s) => s.flow);
  const pts = useMemo(() => pipePath(pipe), [pipe]);
  const curve = useMemo(() => pathCurve(pts), [pts]);
  const radius = 0.34 * (pipe.diameter_mm / 200);
  const len = curve.getLength();

  // concrete sleepers every ~7 m along straight runs, perpendicular to the pipe
  const sleepers = useMemo(() => {
    const out: { p: [number, number, number]; ry: number }[] = [];
    const n = Math.max(2, Math.floor(len / 7));
    for (let i = 1; i < n; i++) {
      const u = i / n;
      const p = curve.getPointAt(u);
      const t = curve.getTangentAt(u);
      out.push({ p: [p.x, (PIPE_Y - radius) / 2, p.z], ry: Math.atan2(t.x, t.z) });
    }
    return out;
  }, [curve, len, radius]);

  // flanges at both ends and at the meter / instrument spool
  const flanges = useMemo(() => {
    const at = [0.012, 0.988, ...(pipe.meter ? [SENSOR_LOCATION[pipe.meter] ?? 0.9] : [])];
    return at.map((s) => ({ p: pointAt(pts, s), q: alongQuat(tangentAt(pts, s)) }));
  }, [pts, pipe.meter]);

  const color = alert === "CRITICAL" ? "#c4432b" : alert === "WARNING" ? "#c98a1d" : "#b9b4ab";
  const emissive = alert ? color : selected ? "#d4572d" : hover ? "#ffffff" : "#000000";
  return (
    <group>
      <mesh castShadow receiveShadow
        onClick={(e) => {
          e.stopPropagation();
          select({ kind: "pipeline", id });
        }}
        onPointerOver={(e) => {
          e.stopPropagation();
          setHover(true);
        }}
        onPointerOut={() => setHover(false)}>
        <tubeGeometry args={[curve as unknown as THREE.Curve<THREE.Vector3>, Math.max(48, Math.round(len * 1.5)), radius, 18, false]} />
        <meshStandardMaterial color={color} metalness={0.62} roughness={0.32} emissive={emissive}
          emissiveIntensity={alert ? 0.35 : selected ? 0.3 : hover ? 0.08 : 0} />
      </mesh>
      {flanges.map((f, i) => (
        <mesh key={i} position={f.p} quaternion={f.q} castShadow>
          <cylinderGeometry args={[radius * 1.65, radius * 1.65, 0.28, 20]} />
          <meshStandardMaterial color="#8f8a80" metalness={0.65} roughness={0.35} />
        </mesh>
      ))}
      <Instances limit={sleepers.length} castShadow receiveShadow>
        <boxGeometry args={[1.9, PIPE_Y - radius, 0.55]} />
        <meshStandardMaterial color="#cdc6ba" roughness={0.95} />
        {sleepers.map((s, i) => <Instance key={i} position={s.p} rotation={[0, s.ry, 0]} />)}
      </Instances>
      {showFlow && pipe.flowing && <FlowRibbon curve={curve} radius={radius} flow={pipe.flow_m3h ?? 0} inferred={pipe.flow_inferred} />}
    </group>
  );
}
