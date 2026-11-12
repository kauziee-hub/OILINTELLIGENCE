"use client";

import { create } from "zustand";

export type TimeOfDay = "day" | "dusk" | "night";
export type CameraPreset = "overview" | "tankfarm" | "TK-001" | "TK-002" | "TK-003" | "pumps" | "process";

/** Camera viewpoints: [position, target]. */
export const PRESETS: Record<CameraPreset, { label: string; pos: [number, number, number]; target: [number, number, number] }> = {
  overview: { label: "Overview", pos: [150, 105, 175], target: [0, 4, -20] },
  tankfarm: { label: "Tank farm", pos: [0, 120, 95], target: [0, 0, -4] },
  "TK-001": { label: "TK-001", pos: [-22, 30, 42], target: [-62, 9, 0] },
  "TK-002": { label: "TK-002", pos: [102, 30, -2], target: [62, 9, -36] },
  "TK-003": { label: "TK-003", pos: [100, 28, 72], target: [62, 9, 36] },
  pumps: { label: "Pump house", pos: [22, 14, 26], target: [-2, 2, 0] },
  process: { label: "Process area", pos: [95, 60, -30], target: [10, 18, -125] },
};

interface TwinView {
  preset: CameraPreset;
  presetNonce: number;
  timeOfDay: TimeOfDay;
  xray: boolean;
  flow: boolean;
  setPreset: (p: CameraPreset) => void;
  setTimeOfDay: (t: TimeOfDay) => void;
  setXray: (x: boolean) => void;
  setFlow: (f: boolean) => void;
}

export const useTwinView = create<TwinView>((set) => ({
  preset: "overview",
  presetNonce: 0,
  timeOfDay: "day",
  xray: false,
  flow: true,
  setPreset: (preset) => set((s) => ({ preset, presetNonce: s.presetNonce + 1 })),
  setTimeOfDay: (timeOfDay) => set({ timeOfDay }),
  setXray: (xray) => set({ xray }),
  setFlow: (flow) => set({ flow }),
}));

/** Lighting / atmosphere per time of day. */
export const ATMOSPHERE: Record<TimeOfDay, {
  bg: string; fog: [number, number]; sun: [number, number, number]; sunColor: string; sunI: number;
  hemiSky: string; hemiGround: string; hemiI: number; ambient: number; lamps: boolean; ground: string;
}> = {
  day: { bg: "#e6e0d5", fog: [300, 820], sun: [120, 140, 60], sunColor: "#fff3e0", sunI: 2.6, hemiSky: "#f6efe4",
    hemiGround: "#8f8470", hemiI: 0.55, ambient: 0.16, lamps: false, ground: "#a9a68a" },
  dusk: { bg: "#e7b38e", fog: [220, 640], sun: [-160, 38, 40], sunColor: "#ffb27a", sunI: 1.6, hemiSky: "#f6c8a6",
    hemiGround: "#6d5a4b", hemiI: 0.55, ambient: 0.2, lamps: true, ground: "#b9a690" },
  night: { bg: "#14161b", fog: [180, 560], sun: [-60, 140, 90], sunColor: "#9fb3d9", sunI: 0.5, hemiSky: "#4a5a7a",
    hemiGround: "#1d1f24", hemiI: 0.6, ambient: 0.2, lamps: true, ground: "#3b3934" },
};
