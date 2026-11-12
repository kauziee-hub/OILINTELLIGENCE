"use client";

import { Environment as EnvMap, Lightformer } from "@react-three/drei";
import { ATMOSPHERE, useTwinView } from "../store";

/** Sun (shadow caster), sky / ground fill and an in-code studio environment for metal reflections
 *  (Lightformers - no HDRI download needed). */
export function Lighting() {
  const tod = useTwinView((s) => s.timeOfDay);
  const a = ATMOSPHERE[tod];
  return (
    <>
      <ambientLight intensity={a.ambient} />
      <hemisphereLight args={[a.hemiSky, a.hemiGround, a.hemiI]} />
      <directionalLight position={a.sun} intensity={a.sunI} color={a.sunColor} castShadow
        shadow-mapSize={[2048, 2048]} shadow-bias={-0.0004} shadow-normalBias={0.04}
        shadow-camera-left={-190} shadow-camera-right={190} shadow-camera-top={170} shadow-camera-bottom={-210}
        shadow-camera-near={10} shadow-camera-far={520} />
      <EnvMap resolution={256} frames={1} environmentIntensity={tod === "night" ? 0.2 : 0.6}>
        <Lightformer form="rect" intensity={2.2} color="#fff4e6" position={[0, 40, -60]} scale={[160, 40, 1]} />
        <Lightformer form="rect" intensity={1.2} color="#ffe2c8" position={[90, 25, 60]} rotation={[0, -Math.PI / 3, 0]} scale={[90, 30, 1]} />
        <Lightformer form="rect" intensity={0.8} color="#dfe8f5" position={[-90, 30, 40]} rotation={[0, Math.PI / 3, 0]} scale={[90, 30, 1]} />
        <Lightformer form="circle" intensity={3} color="#ffffff" position={[0, 90, 0]} rotation={[Math.PI / 2, 0, 0]} scale={40} />
      </EnvMap>
    </>
  );
}
