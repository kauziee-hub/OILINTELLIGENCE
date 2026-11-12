"use client";

import { Stats } from "@react-three/drei";
import { Canvas } from "@react-three/fiber";
import { useMemo } from "react";
import * as THREE from "three";
import { useLive } from "@/hooks/useLive";
import type { TwinState, Vec3 } from "@/services/types";
import { Pipeline } from "../assets/Pipeline";
import { Pump } from "../assets/Pump";
import { SensorMarker } from "../assets/Sensor";
import { Tank } from "../assets/Tank";
import { Valve } from "../assets/Valve";
import { alongQuat, pathCurve, pipePath, PIPE_Y, pointAt, SENSOR_LOCATION, sensorPosition, tangentAt } from "../geometry";
import { CameraControls } from "../interactions/CameraControls";
import { AlertMarkers, TruthLeakIndicator } from "../overlays/AlertMarkers";
import { ATMOSPHERE, PRESETS, useTwinView } from "../store";
import { Environment } from "./Environment";
import { Lighting } from "./Lighting";
import { ProcessArea } from "./ProcessArea";
import { LabelGate, Label } from "../overlays/Label";

export interface SceneOptions {
  showSensors: boolean;
  showSensorLabels: boolean;
  showStats: boolean;
  truthLeaks?: { pipe: string; position: number; rate_th: number }[];
}

function alertFor(state: TwinState, asset: string) {
  const a = state.alerts.filter((x) => x.asset_id === asset);
  if (!a.length) return undefined;
  return a.some((x) => x.severity === "CRITICAL") ? "CRITICAL" : "WARNING";
}

function Pipe({ pts, r, color = "#b9b4ab" }: { pts: Vec3[]; r: number; color?: string }) {
  const curve = useMemo(() => pathCurve(pts, 0.9), [pts]);
  return (
    <mesh castShadow receiveShadow>
      <tubeGeometry args={[curve as unknown as THREE.Curve<THREE.Vector3>, 64, r, 14, false]} />
      <meshStandardMaterial color={color} metalness={0.62} roughness={0.32} />
    </mesh>
  );
}

/** Suction / discharge headers and the pump station shelter. */
function PumpStation({ state }: { state: TwinState }) {
  const suc = state.junctions["MF-SUC"]?.position;
  const dis = state.junctions["MF-DIS"]?.position;
  if (!suc || !dis) return null;
  const pumps = Object.entries(state.pumps);
  return (
    <group>
      {[["MF-SUC", suc], ["MF-DIS", dis]].map(([id, p]) => {
        const pos = p as Vec3;
        return (
          <group key={id as string} position={[pos[0], PIPE_Y, pos[2]]}>
            <mesh rotation={[Math.PI / 2, 0, 0]} castShadow>
              <cylinderGeometry args={[0.5, 0.5, 16, 20]} /><meshStandardMaterial color="#a9a399" metalness={0.6} roughness={0.35} />
            </mesh>
            {[-8, 8].map((z) => (
              <mesh key={z} position={[0, 0, z]}><sphereGeometry args={[0.5, 16, 10]} /><meshStandardMaterial color="#a9a399" metalness={0.6} /></mesh>
            ))}
            <mesh position={[0, -(PIPE_Y - 0.2) / 2, 0]}><boxGeometry args={[1.6, PIPE_Y - 0.4, 16]} /><meshStandardMaterial color="#cdc6ba" /></mesh>
            <Label position={[0, 2.2, -8.6]} center zIndexRange={[10, 0]}>
              <div className="pointer-events-none whitespace-nowrap rounded-full bg-white/85 px-2 py-0.5 text-[10px] font-medium text-ink-2 ring-1 ring-black/5">{id as string}</div>
            </Label>
          </group>
        );
      })}
      {pumps.map(([id, p]) => {
        const [x, , z] = p.position;
        const running = p.status === "RUNNING";
        return (
          <group key={id}>
            <Pipe pts={[[suc[0], PIPE_Y, z], [x - 3.1, PIPE_Y, z]]} r={0.36} />
            <Pipe pts={[[x - 1.6, 2.95, z], [x - 1.6, 3.9, z], [dis[0], 3.9, z], [dis[0], PIPE_Y + 0.4, z]]} r={0.3}
              color={running ? "#c2b29d" : "#b9b4ab"} />
          </group>
        );
      })}
      {/* pump house: steel frame + roof, open sides */}
      <group position={[-2, 0, 0]}>
        {[[-6.5, -9.5], [6.5, -9.5], [-6.5, 9.5], [6.5, 9.5], [0, -9.5], [0, 9.5]].map(([x, z], i) => (
          <mesh key={i} position={[x, 3.7, z]} castShadow><boxGeometry args={[0.4, 7.4, 0.4]} /><meshStandardMaterial color="#6f6a62" metalness={0.5} /></mesh>
        ))}
        <mesh position={[0, 7.55, 0]} rotation={[0, 0, 0.05]} castShadow receiveShadow>
          <boxGeometry args={[15, 0.3, 21]} /><meshStandardMaterial color="#a39d93" metalness={0.45} roughness={0.5} />
        </mesh>
        <Label position={[0, 9.3, 0]} center zIndexRange={[10, 0]}>
          <div className="pointer-events-none whitespace-nowrap rounded-full bg-dark/75 px-2 py-0.5 text-[10px] text-white">Pump house</div>
        </Label>
      </group>
    </group>
  );
}

/** Flow-meter spools on the metered lines (the transmitter head is the instrument marker above). */
function Meters({ state }: { state: TwinState }) {
  return (
    <>
      {Object.values(state.pipelines).filter((p) => p.meter).map((p) => {
        const pts = pipePath(p);
        const s = SENSOR_LOCATION[p.meter!] ?? 0.9;
        const pos = pointAt(pts, s);
        const q = alongQuat(tangentAt(pts, s));
        const r = 0.34 * (p.diameter_mm / 200);
        return (
          <group key={p.meter ?? p.name} position={pos}>
            <mesh quaternion={q} castShadow><cylinderGeometry args={[r * 1.5, r * 1.5, 2.2, 22]} /><meshStandardMaterial color="#56606c" metalness={0.5} roughness={0.4} /></mesh>
            <mesh position={[0, r * 1.5 + 0.2, 0]}><boxGeometry args={[0.5, 0.4, 0.5]} /><meshStandardMaterial color="#3f4852" /></mesh>
          </group>
        );
      })}
    </>
  );
}

function SceneContent({ state, opts }: { state: TwinState; opts: SceneOptions }) {
  const sensorPos = useMemo(() => {
    const out: [string, THREE.Vector3][] = [];
    for (const id of Object.keys(state.sensors)) {
      const p = sensorPosition(id, state);
      if (p) out.push([id, p]);
    }
    return out;
    // positions only depend on the static layout
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [Object.keys(state.sensors).length]);
  return (
    <>
      <LabelGate />
      <Lighting />
      <Environment tanks={state.tanks} />
      <ProcessArea />
      {Object.entries(state.tanks).map(([id, t]) => <Tank key={id} id={id} tank={t} alert={alertFor(state, id)} />)}
      {Object.entries(state.pipelines).map(([id, p]) => <Pipeline key={id} id={id} pipe={p} alert={alertFor(state, id)} />)}
      <PumpStation state={state} />
      <Meters state={state} />
      {Object.entries(state.pumps).map(([id, p]) => <Pump key={id} id={id} pump={p} alert={alertFor(state, id)} />)}
      {Object.entries(state.valves).map(([id, v]) => <Valve key={id} id={id} valve={v} state={state} alert={alertFor(state, id)} />)}
      {opts.showSensors && sensorPos.map(([id, p]) => (
        <SensorMarker key={id} id={id} sensor={state.sensors[id]} position={p} showLabel={opts.showSensorLabels} />
      ))}
      <AlertMarkers alerts={state.alerts} state={state} />
      {opts.truthLeaks && <TruthLeakIndicator leaks={opts.truthLeaks} state={state} />}
      <CameraControls />
      {opts.showStats && <Stats className="!left-auto !right-3 !top-3" />}
    </>
  );
}

export default function TankFarmScene({ opts }: { opts: SceneOptions }) {
  const state = useLive((s) => s.state);
  const setSelected = useLive((s) => s.setSelected);
  const tod = useTwinView((s) => s.timeOfDay);
  const atm = ATMOSPHERE[tod];
  if (!state) {
    return <div className="flex h-full items-center justify-center text-ink-3">Waiting for live telemetry…</div>;
  }
  return (
    <Canvas shadows="percentage" camera={{ position: PRESETS.overview.pos, fov: 40, near: 0.5, far: 3000 }} dpr={[1, 1.75]}
      onPointerMissed={() => setSelected(null)} gl={{ antialias: true, preserveDrawingBuffer: true }}>
      <color attach="background" args={[atm.bg]} />
      <fog attach="fog" args={[atm.bg, atm.fog[0], atm.fog[1]]} />
      <SceneContent state={state} opts={opts} />
    </Canvas>
  );
}
