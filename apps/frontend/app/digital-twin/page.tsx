"use client";

import clsx from "clsx";
import { Eye, Layers, Moon, ScanLine, Sun, Sunset, Waves } from "lucide-react";
import dynamic from "next/dynamic";
import { useEffect, useState, type ReactNode } from "react";
import { Badge, Pill } from "@/components/ui";
import { AssetDetails } from "@/digital-twin/panels/AssetDetails";
import { PRESETS, useTwinView, type CameraPreset, type TimeOfDay } from "@/digital-twin/store";
import { useLive } from "@/hooks/useLive";
import { fmt, signed } from "@/lib/format";
import { api, hasRole } from "@/services/api";

const TankFarmScene = dynamic(() => import("@/digital-twin/scene/TankFarmScene"), {
  ssr: false,
  loading: () => <div className="flex h-full items-center justify-center text-ink-3">Loading 3D scene…</div>,
});

function Toggle({ on, onClick, children, title }: { on: boolean; onClick: () => void; children: ReactNode; title: string }) {
  return (
    <button type="button" title={title} onClick={onClick}
      className={clsx("inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11.5px] font-medium transition-colors",
        on ? "bg-dark text-white" : "bg-white/70 text-ink-2 hover:bg-white")}>
      {children}
    </button>
  );
}

const LEGEND: [string, string][] = [
  ["#ff9a62", "measured flow (chevrons move from → to)"],
  ["#e2ddd4", "inferred flow (no meter on the line)"],
  ["#37c26e", "pump running · valve open"],
  ["#e2a93b", "valve throttling · warning"],
  ["#e0452b", "diagnosed abnormality (beacon)"],
  ["#3fa7ff", "instrument good (amber uncertain, red bad, grey stale)"],
  ["#e07a45", "tank level board = measured gauge level"],
];

export default function DigitalTwinPage() {
  const state = useLive((s) => s.state);
  const selected = useLive((s) => s.selected);
  const setSelected = useLive((s) => s.setSelected);
  const view = useTwinView();
  const [showSensors, setShowSensors] = useState(true);
  const [labels, setLabels] = useState(false);
  const [stats, setStats] = useState(false);
  const [truth, setTruth] = useState(false);
  const [legend, setLegend] = useState(true);
  const [leaks, setLeaks] = useState<{ pipe: string; position: number; rate_th: number }[]>([]);
  const canTruth = state?.mode === "SIMULATION" && hasRole("engineer");

  useEffect(() => {
    if (!truth || !canTruth) {
      setLeaks([]);
      return;
    }
    const load = async () => {
      try {
        const t = await api<any>("/simulation/truth/latest");
        const faults = t.truth?.active_faults ?? [];
        setLeaks(faults.filter((f: any) => f.fault_type === "PIPELINE_LEAK" || f.fault_type === "TANK_LEAK").map((f: any) => ({
          pipe: f.target, position: f.params?.position ?? 0.5, rate_th: t.truth?.leaks?.[f.target] ?? t.truth?.leaks?.[`TANK:${f.target}`] ?? 0,
        })));
      } catch {
        setLeaks([]);
      }
    };
    load();
    const h = setInterval(load, 3000);
    return () => clearInterval(h);
  }, [truth, canTruth]);

  const b = state?.balance;
  const sim = state?.simulation;
  const tod: { v: TimeOfDay; icon: ReactNode }[] = [
    { v: "day", icon: <Sun size={13} /> }, { v: "dusk", icon: <Sunset size={13} /> }, { v: "night", icon: <Moon size={13} /> },
  ];
  return (
    <div className="relative -mt-2 h-[calc(100vh-96px)] min-h-[600px] overflow-hidden rounded-[28px] ring-1 ring-white/70 card-shadow">
      <TankFarmScene opts={{ showSensors, showSensorLabels: labels, showStats: stats, truthLeaks: truth ? leaks : undefined }} />

      {/* title + live status */}
      <div className="glass pointer-events-auto absolute left-4 top-4 z-40 max-w-sm rounded-[20px] px-4 py-3 ring-1 ring-white/70 card-shadow">
        <div className="text-[11.5px] text-ink-3">Digital twin · TF-01 crude terminal</div>
        <div className="display text-[22px] font-medium">Tank farm, live</div>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <Pill tone={state?.mode === "PLANT" ? "orange" : "dark"}>{state?.mode === "PLANT" ? "Plant data · read-only" : "Simulation data"}</Pill>
          {sim?.active_transfer && <Pill tone="light">{sim.active_transfer.route} · {fmt(sim.active_transfer.setpoint_m3h, 0)} m³/h</Pill>}
        </div>
        {b && (
          <div className="mt-2.5 border-t border-line pt-2 text-[12px]">
            <div className="text-ink-3">Latest 30-min facility balance</div>
            <div className="num text-ink">R = {signed(b.facility_residual_t, 2)} t · χ² {fmt(b.chi2, 1)}/{b.dof} · max|z| {fmt(b.max_abs_z, 1)}</div>
            <Badge tone={b.anomaly ? "critical" : "normal"} className="mt-1">{b.anomaly ? "abnormal" : "within uncertainty"}</Badge>
          </div>
        )}
      </div>

      {/* camera + atmosphere + layers */}
      <div className="absolute right-4 top-4 z-40 flex max-w-[calc(100%-26rem)] flex-col items-end gap-2">
        <div className="glass flex flex-wrap justify-end gap-1 rounded-full p-1 ring-1 ring-white/70 card-shadow">
          {(Object.keys(PRESETS) as CameraPreset[]).map((k) => (
            <Toggle key={k} title={`Fly to ${PRESETS[k].label}`} on={view.preset === k} onClick={() => view.setPreset(k)}>{PRESETS[k].label}</Toggle>
          ))}
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <div className="glass flex gap-1 rounded-full p-1 ring-1 ring-white/70 card-shadow">
            {tod.map((t) => (
              <Toggle key={t.v} title={`Lighting: ${t.v}`} on={view.timeOfDay === t.v} onClick={() => view.setTimeOfDay(t.v)}>{t.icon}{t.v}</Toggle>
            ))}
          </div>
          <div className="glass flex flex-wrap gap-1 rounded-full p-1 ring-1 ring-white/70 card-shadow">
            <Toggle title="Instrument markers" on={showSensors} onClick={() => setShowSensors(!showSensors)}><ScanLine size={13} />instruments</Toggle>
            <Toggle title="Instrument values" on={labels} onClick={() => setLabels(!labels)}>values</Toggle>
            <Toggle title="Animated flow" on={view.flow} onClick={() => view.setFlow(!view.flow)}><Waves size={13} />flow</Toggle>
            <Toggle title="See-through tank shells" on={view.xray} onClick={() => view.setXray(!view.xray)}><Eye size={13} />x-ray</Toggle>
            <Toggle title="Frame rate" on={stats} onClick={() => setStats(!stats)}>FPS</Toggle>
            {canTruth && <Toggle title="Simulated ground truth (lab only)" on={truth} onClick={() => setTruth(!truth)}>ground truth</Toggle>}
          </div>
        </div>
      </div>

      {/* selection panel */}
      {selected ? (
        <div className="glass absolute bottom-4 right-4 top-[132px] z-40 hidden w-[360px] overflow-y-auto rounded-[22px] p-4 ring-1 ring-white/70 card-shadow lg:block">
          <button type="button" onClick={() => setSelected(null)} className="absolute right-3 top-3 rounded-full bg-white px-2 py-0.5 text-[11px] text-ink-3 ring-1 ring-line hover:text-ink">close</button>
          <AssetDetails />
        </div>
      ) : (
        <div className="glass absolute bottom-4 right-4 z-40 hidden rounded-full px-3.5 py-1.5 text-[11.5px] text-ink-2 ring-1 ring-white/70 card-shadow lg:block">
          Click a tank, pipe, pump, valve or instrument to inspect it
        </div>
      )}

      {/* legend */}
      <div className="absolute bottom-4 left-4 z-40">
        {legend ? (
          <div className="glass max-w-xs rounded-[18px] px-3.5 py-3 ring-1 ring-white/70 card-shadow">
            <div className="mb-1.5 flex items-center justify-between text-[11.5px] font-medium text-ink-2">
              <span className="inline-flex items-center gap-1"><Layers size={12} /> Legend</span>
              <button type="button" className="text-ink-3 hover:text-ink" onClick={() => setLegend(false)}>hide</button>
            </div>
            <ul className="space-y-1">
              {LEGEND.map(([c, t]) => (
                <li key={t} className="flex items-center gap-2 text-[11px] text-ink-2">
                  <span className="h-2 w-4 shrink-0 rounded-full" style={{ background: c }} /> {t}
                </li>
              ))}
            </ul>
            <div className="mt-1.5 text-[10.5px] text-ink-3">Drag to orbit · right-drag to pan · wheel to zoom · click any asset</div>
          </div>
        ) : (
          <button type="button" onClick={() => setLegend(true)} className="glass rounded-full px-3 py-1.5 text-[11.5px] ring-1 ring-white/70">Legend</button>
        )}
      </div>
    </div>
  );
}
