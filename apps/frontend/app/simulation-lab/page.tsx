"use client";

import { useEffect, useMemo, useState } from "react";
import { Badge, Button, Card, Empty, ErrorNote, JsonView, PageHeader, Provenance } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useLive } from "@/hooks/useLive";
import { api, hasRole } from "@/services/api";
import { fmt, humanize } from "@/lib/format";

export default function SimulationLab() {
  const state = useLive((s) => s.state);
  const sim = state?.simulation;
  const { data: catalog } = useApi<any>("/simulation/catalog");
  const { data: truth, reload: reloadTruth } = useApi<any>(hasRole("engineer") ? "/simulation/truth/latest" : null, 4000);
  const { data: runs } = useApi<any[]>("/simulation/runs", 15000);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const can = hasRole("operator");

  const [ft, setFt] = useState("PIPELINE_LEAK");
  const [target, setTarget] = useState("");
  const [params, setParams] = useState("{}");
  const [duration, setDuration] = useState("");
  const [family, setFamily] = useState("");
  const [seed, setSeed] = useState(7);
  const [speed, setSpeed] = useState(10);
  const [route, setRoute] = useState("R-01");
  const [sp, setSp] = useState(120);
  const [vol, setVol] = useState(250);
  const [pump, setPump] = useState("PU-001");

  const def = catalog?.fault_types?.[ft];
  useEffect(() => {
    if (def) {
      setTarget(def.targets[0] ?? "");
      setParams(JSON.stringify(def.default_params ?? {}));
    }
  }, [ft, def]);
  useEffect(() => {
    if (sim?.speed) setSpeed(sim.speed);
  }, [sim?.speed]);

  async function run(label: string, fn: () => Promise<any>) {
    setErr(null);
    setOk(null);
    try {
      const r = await fn();
      setOk(`${label}: ${typeof r === "object" ? (r.fault_id ?? r.transfer_id ?? r.state ?? "ok") : "ok"}`);
      reloadTruth();
    } catch (e: any) {
      setErr(e.message);
    }
  }

  const activeFaults: any[] = useMemo(() => truth?.truth?.active_faults ?? [], [truth]);

  if (state && state.mode !== "SIMULATION") {
    return <Empty>The Simulation Lab is disabled in PLANT mode.</Empty>;
  }
  return (
    <div className="space-y-4">
      <PageHeader title="Simulation Lab" subtitle="Scenario design, fault injection and simulated ground truth (never visible to diagnostic models)"
        actions={sim && <Badge tone={sim.state === "RUNNING" ? "normal" : "offline"}>{sim.state} ×{sim.speed}</Badge>} />
      <ErrorNote error={err} />
      {ok && <div className="text-sm text-ok">{ok}</div>}
      <div className="grid gap-4 xl:grid-cols-3">
        <Card title="Simulation control">
          <div className="space-y-3 text-sm">
            <div className="num text-xs text-ink-3">
              run {sim?.run_id ?? "—"} · scenario {sim?.scenario_id ?? "—"} · t = {fmt((sim?.sim_time_s ?? 0) / 3600, 2)} h ·
              {" "}{fmt(sim?.steps_per_second, 0)} steps/s · mass conservation error {fmt(sim?.conservation_error_kg, 6)} kg
            </div>
            <div className="flex flex-wrap gap-2">
              <Button disabled={!can} onClick={() => run("pause", () => api("/simulation/pause", { method: "POST" }))}>Pause</Button>
              <Button disabled={!can} onClick={() => run("resume", () => api("/simulation/resume", { method: "POST" }))}>Resume</Button>
              <Button disabled={!can} variant="danger" onClick={() => run("stop", () => api("/simulation/stop", { method: "POST" }))}>Stop</Button>
            </div>
            <label className="block">
              <span className="text-xs text-ink-3">Speed ×{speed}</span>
              <input type="range" min={1} max={120} value={speed} className="w-full" onChange={(e) => setSpeed(Number(e.target.value))}
                onMouseUp={() => can && run("speed", () => api("/simulation/speed", { method: "PUT", json: { speed } }))} />
            </label>
            <div className="border-t border-line pt-3">
              <div className="mb-1 text-xs uppercase text-ink-3">Start a new run</div>
              <div className="flex flex-wrap gap-2">
                <select value={family} onChange={(e) => setFamily(e.target.value)}>
                  <option value="">live schedule (no faults)</option>
                  {["NORMAL", ...(catalog?.known_families ?? []), "MULTI_FAULT", ...(catalog?.unseen_families ?? [])].map((f: string) =>
                    <option key={f} value={f}>{humanize(f)} scenario</option>)}
                </select>
                <input type="number" className="w-20" value={seed} onChange={(e) => setSeed(Number(e.target.value))} title="seed" />
                <Button variant="primary" disabled={!can} onClick={() => run("start", () => api("/simulation/start", { method: "POST",
                  json: { family: family || null, seed, speed, duration_h: 48 } }))}>Start</Button>
              </div>
              <p className="mt-1 text-xs text-ink-3">Same scenario + seed reproduces the identical physical truth trajectory.</p>
            </div>
          </div>
        </Card>

        <Card title="Inject fault" subtitle="Applied at the current simulation time">
          <div className="space-y-2 text-sm">
            <select className="w-full" value={ft} onChange={(e) => setFt(e.target.value)}>
              {Object.entries<any>(catalog?.fault_types ?? {}).map(([k, v]) => <option key={k} value={k}>{humanize(k)} ({v.category})</option>)}
            </select>
            <select className="w-full" value={target} onChange={(e) => setTarget(e.target.value)}>
              {(def?.targets ?? []).map((t: string) => <option key={t}>{t}</option>)}
            </select>
            <label className="block text-xs text-ink-3">Parameters (JSON)
              <input className="num mt-1 w-full" value={params} onChange={(e) => setParams(e.target.value)} />
            </label>
            <label className="block text-xs text-ink-3">Duration (s, empty = until cleared)
              <input className="mt-1 w-full" value={duration} onChange={(e) => setDuration(e.target.value)} />
            </label>
            <Button variant="danger" disabled={!can || !target} onClick={() => run("fault injected", async () => {
              let p = {};
              try {
                p = JSON.parse(params || "{}");
              } catch {
                throw new Error("parameters must be valid JSON");
              }
              return api("/simulation/faults", { method: "POST", json: { fault_type: ft, target, params: p,
                duration_s: duration ? Number(duration) : null } });
            })}>Inject {humanize(ft)}</Button>
          </div>
        </Card>

        <Card title="Schedule a transfer">
          <div className="space-y-2 text-sm">
            <select className="w-full" value={route} onChange={(e) => setRoute(e.target.value)}>
              {Object.entries<any>(catalog?.routes ?? {}).map(([k, r]) => <option key={k} value={k}>{k} — {r.name}</option>)}
            </select>
            <div className="grid grid-cols-3 gap-2">
              <label className="text-xs text-ink-3">m³/h<input className="mt-1 w-full" type="number" value={sp} onChange={(e) => setSp(Number(e.target.value))} /></label>
              <label className="text-xs text-ink-3">volume m³<input className="mt-1 w-full" type="number" value={vol} onChange={(e) => setVol(Number(e.target.value))} /></label>
              <label className="text-xs text-ink-3">pump<select className="mt-1 w-full" value={pump} onChange={(e) => setPump(e.target.value)}>
                {(catalog?.pumps ?? []).map((p: string) => <option key={p}>{p}</option>)}</select></label>
            </div>
            <Button disabled={!can} onClick={() => run("transfer scheduled", () => api("/simulation/transfers", { method: "POST",
              json: { route, setpoint_m3h: sp, target_volume_m3: vol, pumps: [pump] } }))}>Schedule transfer</Button>
            <div className="text-xs text-ink-3">Pending: {(sim?.pending_transfers ?? []).map((p) => `${p.transfer_id} ${p.route}`).join(", ") || "none"}</div>
          </div>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Active injected faults" actions={<Provenance kind="simulated-truth" />}>
          {activeFaults.length ? (
            <table className="data">
              <thead><tr><th>Fault</th><th>Target</th><th>Parameters</th><th /></tr></thead>
              <tbody>{activeFaults.map((f) => (
                <tr key={f.fault_id}>
                  <td className="text-xs">{humanize(f.fault_type)}<div className="num text-ink-3">{f.fault_id}</div></td>
                  <td className="num text-xs">{f.target}</td>
                  <td className="num text-xs text-ink-3">{JSON.stringify(f.params)}</td>
                  <td><Button disabled={!can} onClick={() => run("cleared", () => api(`/simulation/faults/${f.fault_id}`, { method: "DELETE" }))}>Clear</Button></td>
                </tr>
              ))}</tbody>
            </table>
          ) : <Empty>No active faults.</Empty>}
        </Card>
        <Card title="Simulated ground truth" subtitle="Physical truth from the simulator — for evaluation only" actions={<Provenance kind="simulated-truth" />}>
          {truth?.truth ? (
            <div className="space-y-2 text-xs">
              <table className="data">
                <thead><tr><th>Tank</th><th className="text-right">True mass t</th><th className="text-right">True level m</th><th className="text-right">T °C</th></tr></thead>
                <tbody>{Object.entries<any>(truth.truth.tanks).map(([k, t]) => (
                  <tr key={k}><td>{k}</td><td className="num text-right">{fmt(t.mass_t, 2)}</td><td className="num text-right">{fmt(t.level_m, 4)}</td><td className="num text-right">{fmt(t.temperature_c, 2)}</td></tr>
                ))}</tbody>
              </table>
              <div>Physical loss so far: <span className="num">{fmt(truth.truth.physical_loss_cum_t, 3)} t</span></div>
              <div>Active leak rates: <span className="num">{Object.entries<any>(truth.truth.leaks ?? {}).map(([k, v]) => `${k} ${fmt(v, 2)} t/h`).join(", ") || "none"}</span></div>
            </div>
          ) : <Empty>Ground truth requires the engineer role.</Empty>}
        </Card>
      </div>
      <Card title="Simulation runs">
        {runs?.length ? <JsonView value={runs.slice(0, 10)} /> : <Empty>No runs.</Empty>}
      </Card>
    </div>
  );
}
