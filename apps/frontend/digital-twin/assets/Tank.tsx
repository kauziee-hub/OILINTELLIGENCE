"use client";

import { Instance, Instances } from "@react-three/drei";
import { useMemo, useState } from "react";
import * as THREE from "three";
import { useLive } from "@/hooks/useLive";
import { fmt } from "@/lib/format";
import type { TankState } from "@/services/types";
import { useTwinView } from "../store";
import { Label } from "../overlays/Label";

const SHELL = "#efede8";
const STEEL = "#8d897f";
const YELLOW = "#e2b43c";

/** External floating-roof crude tank. The roof rides on the MEASURED level (gauge LT-xxx). */
export function Tank({ id, tank, alert }: { id: string; tank: TankState; alert?: "WARNING" | "CRITICAL" }) {
  const [hover, setHover] = useState(false);
  const select = useLive((s) => s.setSelected);
  const selected = useLive((s) => s.selected?.kind === "tank" && s.selected.id === id);
  const xray = useTwinView((s) => s.xray);
  const r = tank.diameter_m / 2;
  const H = tank.height_m;
  const fill = Math.max(0.02, Math.min(1, tank.fill_fraction ?? 0));
  const roofY = Math.max(1.4, H * fill);
  const stale = tank.quality !== "GOOD";

  // spiral stairway: half a turn from grade to the top platform
  const a0 = -Math.PI * 0.15;
  const a1 = a0 + Math.PI * 0.85;
  const stair = useMemo(() => {
    const n = Math.round(H / 0.24);
    const steps: { p: [number, number, number]; ry: number }[] = [];
    const rail: THREE.Vector3[] = [];
    const inner: THREE.Vector3[] = [];
    for (let i = 0; i <= n; i++) {
      const u = i / n;
      const a = a0 + (a1 - a0) * u;
      const y = u * H;
      steps.push({ p: [Math.cos(a) * (r + 0.75), y, Math.sin(a) * (r + 0.75)], ry: -a });
      rail.push(new THREE.Vector3(Math.cos(a) * (r + 1.32), y + 1.05, Math.sin(a) * (r + 1.32)));
      inner.push(new THREE.Vector3(Math.cos(a) * (r + 0.22), y + 0.05, Math.sin(a) * (r + 0.22)));
    }
    return { steps, rail: new THREE.CatmullRomCurve3(rail), inner: new THREE.CatmullRomCurve3(inner) };
  }, [H, r, a0, a1]);

  const posts = useMemo(() => {
    const n = Math.round((2 * Math.PI * r) / 2.4);
    return Array.from({ length: n }, (_, i) => {
      const a = (i / n) * Math.PI * 2;
      return [Math.cos(a) * (r + 0.05), H + 0.55, Math.sin(a) * (r + 0.05)] as [number, number, number];
    });
  }, [r, H]);

  // rolling ladder from the top platform down onto the floating roof
  const ladder = useMemo(() => {
    const top = new THREE.Vector3(Math.cos(a1) * (r - 0.4), H + 0.2, Math.sin(a1) * (r - 0.4));
    const bottom = new THREE.Vector3(Math.cos(a1) * (r * 0.05), roofY + 0.4, Math.sin(a1) * (r * 0.05));
    const mid = top.clone().add(bottom).multiplyScalar(0.5);
    const dir = bottom.clone().sub(top);
    const q = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir.clone().normalize());
    return { mid, len: dir.length(), q };
  }, [a1, r, H, roofY]);

  const gaugeA = Math.PI / 4 + 0.32;
  const emissive = alert === "CRITICAL" ? "#c4432b" : alert === "WARNING" || tank.alarm ? "#c98a1d" : selected ? "#d4572d" : "#000000";
  const emissiveI = alert || tank.alarm ? 0.22 : selected ? 0.16 : hover ? 0.06 : 0;
  const handlers = {
    onClick: (e: { stopPropagation: () => void }) => {
      e.stopPropagation();
      select({ kind: "tank", id });
    },
    onPointerOver: (e: { stopPropagation: () => void }) => {
      e.stopPropagation();
      setHover(true);
    },
    onPointerOut: () => setHover(false),
  };

  return (
    <group position={[tank.position[0], 0, tank.position[2]]}>
      {/* ring-wall foundation */}
      <mesh position={[0, 0.25, 0]} receiveShadow castShadow>
        <cylinderGeometry args={[r + 1.1, r + 1.35, 0.5, 72]} />
        <meshStandardMaterial color="#d6d0c5" roughness={0.95} />
      </mesh>
      {/* shell */}
      <mesh position={[0, 0.5 + H / 2, 0]} castShadow={!xray} receiveShadow {...handlers}>
        <cylinderGeometry args={[r, r, H, 72, 1, true]} />
        <meshStandardMaterial color={stale ? "#c9c6c0" : SHELL} metalness={0.35} roughness={0.42} side={THREE.DoubleSide}
          transparent={xray} opacity={xray ? 0.22 : 1} depthWrite={!xray} emissive={emissive} emissiveIntensity={emissiveI} />
      </mesh>
      {/* weld seams of the shell courses */}
      {Array.from({ length: 7 }, (_, k) => (
        <mesh key={k} position={[0, 0.5 + ((k + 1) * H) / 8, 0]} rotation={[Math.PI / 2, 0, 0]}>
          <torusGeometry args={[r + 0.012, 0.03, 4, 96]} />
          <meshStandardMaterial color="#d2cec6" />
        </mesh>
      ))}
      {/* top angle + wind girder */}
      <mesh position={[0, 0.5 + H, 0]} rotation={[Math.PI / 2, 0, 0]}>
        <torusGeometry args={[r, 0.11, 6, 96]} />
        <meshStandardMaterial color="#dcd8d1" metalness={0.4} roughness={0.4} />
      </mesh>
      <mesh position={[0, 0.5 + H - 1.1, 0]} rotation={[-Math.PI / 2, 0, 0]} castShadow>
        <ringGeometry args={[r, r + 1.0, 96]} />
        <meshStandardMaterial color="#d3cfc8" side={THREE.DoubleSide} metalness={0.3} roughness={0.55} />
      </mesh>
      {/* top handrail */}
      <Instances limit={posts.length}>
        <cylinderGeometry args={[0.04, 0.04, 1.1, 5]} />
        <meshStandardMaterial color="#cfcac2" />
        {posts.map((p, i) => <Instance key={i} position={[p[0], p[1] + 0.5, p[2]]} />)}
      </Instances>
      <mesh position={[0, 0.5 + H + 1.1, 0]} rotation={[Math.PI / 2, 0, 0]}>
        <torusGeometry args={[r + 0.05, 0.045, 4, 96]} />
        <meshStandardMaterial color="#cfcac2" />
      </mesh>
      {/* spiral stairway */}
      <group position={[0, 0.5, 0]}>
        <Instances limit={stair.steps.length} castShadow>
          <boxGeometry args={[0.32, 0.06, 1.05]} />
          <meshStandardMaterial color={STEEL} metalness={0.5} roughness={0.5} />
          {stair.steps.map((s, i) => <Instance key={i} position={s.p} rotation={[0, s.ry, 0]} />)}
        </Instances>
        <mesh>
          <tubeGeometry args={[stair.rail, 160, 0.05, 5, false]} />
          <meshStandardMaterial color={YELLOW} />
        </mesh>
        <mesh>
          <tubeGeometry args={[stair.inner, 160, 0.07, 5, false]} />
          <meshStandardMaterial color={STEEL} />
        </mesh>
        <mesh position={[Math.cos(a1) * (r + 0.9), H + 0.02, Math.sin(a1) * (r + 0.9)]} rotation={[0, -a1, 0]} castShadow>
          <boxGeometry args={[2.2, 0.12, 2.4]} />
          <meshStandardMaterial color={STEEL} metalness={0.5} />
        </mesh>
      </group>
      {/* foam chambers (fire protection) */}
      {[0.6, 2.2, 3.8, 5.3].map((a) => (
        <mesh key={a} position={[Math.cos(a) * (r + 0.45), 0.5 + H - 0.6, Math.sin(a) * (r + 0.45)]} rotation={[0, -a, 0]}>
          <boxGeometry args={[0.6, 0.9, 0.55]} />
          <meshStandardMaterial color="#b8402a" roughness={0.6} />
        </mesh>
      ))}
      {/* floating roof: deck, pontoon, rim seal */}
      <group position={[0, 0.5 + roofY, 0]} {...handlers}>
        <mesh castShadow receiveShadow>
          <cylinderGeometry args={[r - 0.35, r - 0.35, 0.3, 72]} />
          <meshStandardMaterial color="#b6b1a7" metalness={0.55} roughness={0.38} />
        </mesh>
        <mesh position={[0, 0.25, 0]} rotation={[Math.PI / 2, 0, 0]}>
          <torusGeometry args={[r - 1.1, 0.4, 8, 72]} />
          <meshStandardMaterial color="#a39e94" metalness={0.5} roughness={0.45} />
        </mesh>
        <mesh position={[0, 0.2, 0]} rotation={[Math.PI / 2, 0, 0]}>
          <torusGeometry args={[r - 0.28, 0.14, 6, 96]} />
          <meshStandardMaterial color="#3e3a34" roughness={0.9} />
        </mesh>
        {/* roof drain sump + vents */}
        <mesh position={[0, 0.2, 0]}><cylinderGeometry args={[0.9, 0.9, 0.2, 20]} /><meshStandardMaterial color="#8c877d" /></mesh>
        {[1.3, 3.1, 4.9].map((a) => (
          <mesh key={a} position={[Math.cos(a) * r * 0.55, 0.45, Math.sin(a) * r * 0.55]}>
            <cylinderGeometry args={[0.25, 0.25, 0.6, 10]} /><meshStandardMaterial color="#9c968b" />
          </mesh>
        ))}
      </group>
      {/* rolling ladder + gauge pole */}
      <mesh position={[ladder.mid.x, ladder.mid.y + 0.5, ladder.mid.z]} quaternion={ladder.q} castShadow>
        <boxGeometry args={[0.7, ladder.len, 0.08]} />
        <meshStandardMaterial color={STEEL} metalness={0.5} />
      </mesh>
      <mesh position={[Math.cos(a1 + 0.2) * (r - 0.9), 0.5 + (H + 1.5) / 2, Math.sin(a1 + 0.2) * (r - 0.9)]}>
        <cylinderGeometry args={[0.12, 0.12, H + 1.5, 8]} />
        <meshStandardMaterial color="#9a9488" metalness={0.6} />
      </mesh>
      {/* external level gauge board: orange bar = measured level */}
      <group position={[Math.cos(gaugeA) * (r + 0.06), 0.5, Math.sin(gaugeA) * (r + 0.06)]} rotation={[0, -gaugeA + Math.PI / 2, 0]}>
        <mesh position={[0, H / 2, 0]}><boxGeometry args={[0.55, H, 0.05]} /><meshStandardMaterial color="#3a3833" /></mesh>
        <mesh position={[0, (H * fill) / 2, 0.03]}><boxGeometry args={[0.38, H * fill, 0.05]} />
          <meshStandardMaterial color="#e07a45" emissive="#d4572d" emissiveIntensity={0.35} /></mesh>
      </group>
      {/* crude column (x-ray view) */}
      {xray && (
        <mesh position={[0, 0.5 + roofY / 2, 0]}>
          <cylinderGeometry args={[r - 0.25, r - 0.25, roofY, 64]} />
          <meshStandardMaterial color={stale ? "#7a756c" : "#4a2a10"} roughness={0.25} metalness={0.1} transparent opacity={0.92} />
        </mesh>
      )}
      <Label position={[0, H + 5.2, 0]} center zIndexRange={[10, 0]}>
        <div className="pointer-events-none select-none whitespace-nowrap rounded-xl bg-white/92 px-2.5 py-1 text-center leading-tight text-ink shadow-[0_8px_24px_-10px_rgba(0,0,0,0.35)] ring-1 ring-black/5 backdrop-blur">
          <div className="text-[11.5px] font-semibold">{id} <span className="font-normal text-ink-3">{tank.name.split("—")[1]?.trim() ?? ""}</span></div>
          <div className="num text-[10.5px] text-ink-2">{fmt(tank.level_m, 2)} m · {fmt((tank.fill_fraction ?? 0) * 100, 0)}% · {fmt(tank.inventory_t_estimated, 0)} t</div>
          {tank.alarm && <div className="text-[11px] font-medium text-warn">⚠ {tank.alarm.replace("_", " ").toLowerCase()}</div>}
        </div>
      </Label>
    </group>
  );
}
