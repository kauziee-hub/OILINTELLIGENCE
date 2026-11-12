"use client";

import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useRef, type ComponentProps } from "react";
import { create } from "zustand";

/** drei <Html> mounts its own React root into the canvas container. While the canvas is still
 *  connecting its event layer the container changes once, which tears every label root down and
 *  re-creates it (React warns about a synchronous unmount). Labels therefore mount only after the
 *  scene has rendered a few frames. */
const useLabelGate = create<{ ready: boolean; set: (r: boolean) => void }>((set) => ({
  ready: false,
  set: (ready) => set({ ready }),
}));

export function LabelGate() {
  const frames = useRef(0);
  const set = useLabelGate((s) => s.set);
  useEffect(() => () => set(false), [set]);
  useFrame(() => {
    if (frames.current < 0) return;
    frames.current += 1;
    if (frames.current > 20) {
      frames.current = -1;
      set(true);
    }
  });
  return null;
}

export function Label(props: ComponentProps<typeof Html>) {
  const ready = useLabelGate((s) => s.ready);
  return ready ? <Html {...props} /> : null;
}
