"use client";

import { OrbitControls } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useRef } from "react";
import * as THREE from "three";
import type { OrbitControls as OrbitImpl } from "three-stdlib";
import { PRESETS, useTwinView } from "../store";

/** Orbit (left drag), pan (right drag), zoom (wheel); named viewpoints fly the camera smoothly and
 *  hand control back as soon as the user grabs the view. */
export function CameraControls() {
  const controls = useRef<OrbitImpl>(null);
  const { camera } = useThree();
  const preset = useTwinView((s) => s.preset);
  const nonce = useTwinView((s) => s.presetNonce);
  const flying = useRef(false);
  const goal = useRef({ pos: new THREE.Vector3(...PRESETS.overview.pos), target: new THREE.Vector3(...PRESETS.overview.target) });

  useEffect(() => {
    const p = PRESETS[preset];
    goal.current = { pos: new THREE.Vector3(...p.pos), target: new THREE.Vector3(...p.target) };
    flying.current = true;
  }, [preset, nonce]);

  useEffect(() => {
    const c = controls.current;
    if (!c) return;
    const stop = () => (flying.current = false);
    c.addEventListener("start", stop);
    return () => c.removeEventListener("start", stop);
  }, []);

  useFrame((_, dt) => {
    const c = controls.current;
    if (!flying.current || !c) return;
    const k = 1 - Math.exp(-dt * 2.6);
    camera.position.lerp(goal.current.pos, k);
    c.target.lerp(goal.current.target, k);
    c.update();
    if (camera.position.distanceTo(goal.current.pos) < 0.3 && c.target.distanceTo(goal.current.target) < 0.3) flying.current = false;
  });

  return (
    <OrbitControls ref={controls} makeDefault enableDamping dampingFactor={0.08} minDistance={12} maxDistance={420}
      maxPolarAngle={Math.PI / 2.08} target={PRESETS.overview.target} />
  );
}
