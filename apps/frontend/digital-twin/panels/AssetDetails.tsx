"use client";

import Link from "next/link";
import { useMemo } from "react";
import { TimeChart } from "@/components/charts";
import { Badge, Provenance } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useLive } from "@/hooks/useLive";
import { fmt } from "@/lib/format";

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3 border-b border-line py-1.5 text-[13px]">
      <span className="text-ink-3">{k}</span>
      <span className="num text-right text-ink">{v}</span>
    </div>
  );
}

export function SensorHistory({ ids, hours = 2 }: { ids: string[]; hours?: number }) {
  const ts = useLive((s) => s.state?.timestamp ?? null);
  const bucket = ts ? Math.floor(ts / 120) : 0; // refresh every 2 simulated minutes of data
  const qs = ids.map((i) => `sensor_id=${encodeURIComponent(i)}`).join("&");
  const end = ts ?? 0;
  const { data } = useApi<any>(ids.length && ts ? `/telemetry/history?${qs}&start=${end - hours * 3600}&end=${end}&max_points=240&b=${bucket}` : null);
  const series = useMemo(() => data ? Object.entries<any>(data.series).map(([k, v]) => ({
    name: `${k} (${v.unit})`, points: v.points.map((p: any) => [p[0], p[1]] as [number, number | null]),
  })) : [], [data]);
  if (!series.length) return <div className="text-xs text-ink-3">No history yet.</div>;
  return <TimeChart series={series} height={180} />;
}

export function AssetDetails() {
  const sel = useLive((s) => s.selected);
  const st = useLive((s) => s.state);
  if (!sel || !st) {
    return (
      <div className="text-sm text-ink-3">
        Click a tank, pipeline, pump, valve or instrument marker to inspect live values, history and any open
        investigation.
      </div>
    );
  }
  const alerts = st.alerts.filter((a) => a.asset_id === sel.id || st.sensors[a.asset_id]?.asset_id === sel.id);
  const sensorsOf = Object.entries(st.sensors).filter(([, s]) => s.asset_id === sel.id).map(([k]) => k);
  let body: React.ReactNode = null;
  let hist: string[] = [];
  if (sel.kind === "tank") {
    const t = st.tanks[sel.id];
    body = (
      <>
        <Row k="Level" v={<>{fmt(t.level_m, 3)} m <Provenance kind="measured" /></>} />
        <Row k="Temperature" v={`${fmt(t.temperature_c, 2)} °C`} />
        <Row k="Fill" v={`${fmt((t.fill_fraction ?? 0) * 100, 1)} %`} />
        <Row k="Inventory" v={<>{fmt(t.inventory_t_estimated, 1)} t <Provenance kind="estimated" /></>} />
        <Row k="Lab density @15°C" v={`${fmt(t.density_15c_lab, 1)} kg/m³`} />
        <Row k="Nominal capacity" v={`${fmt(t.capacity_m3, 0)} m³`} />
      </>
    );
    hist = sensorsOf.filter((s) => s.startsWith("LT"));
  } else if (sel.kind === "pipeline") {
    const p = st.pipelines[sel.id];
    body = (
      <>
        <Row k="Route" v={`${p.from} → ${p.to}`} />
        <Row k="Flow" v={<>{fmt(p.flow_m3h, 2)} m³/h {p.flow_inferred ? <Provenance kind="estimated" /> : p.meter ? <Provenance kind="measured" /> : null}</>} />
        <Row k="Meter" v={p.meter ?? "none (inferred)"} />
        <Row k="Pressure" v={`${fmt(p.pressure_bar, 3)} bar(g)`} />
        <Row k="Nominal diameter" v={`DN${p.diameter_mm}`} />
      </>
    );
    hist = [p.meter, p.pressure_sensor].filter(Boolean) as string[];
  } else if (sel.kind === "pump") {
    const p = st.pumps[sel.id];
    body = (
      <>
        <Row k="Status" v={<Badge tone={p.status === "RUNNING" ? "normal" : "offline"}>{p.status}</Badge>} />
        <Row k="Speed" v={`${fmt(p.speed_rpm, 0)} rpm`} />
        <Row k="Power" v={`${fmt(p.power_kw, 2)} kW`} />
        <Row k="Discharge" v={`${fmt(p.discharge_bar, 3)} bar(g)`} />
      </>
    );
    hist = [`${sel.id}-PW`, `${sel.id}-PD`];
  } else if (sel.kind === "valve") {
    const v = st.valves[sel.id];
    body = (
      <>
        <Row k="Position (feedback)" v={`${fmt(v.position_pct, 1)} %`} />
        <Row k="Command" v={<Badge tone={v.command === "OPEN" ? "normal" : "offline"}>{v.command}</Badge>} />
        <Row k="Role" v={v.role} />
        <Row k="Pipeline" v={v.pipeline} />
      </>
    );
    hist = [`${sel.id}-ZT`];
  } else if (sel.kind === "sensor") {
    const s = st.sensors[sel.id];
    body = (
      <>
        <Row k="Asset" v={s.asset_id} />
        <Row k="Measurement" v={s.parameter} />
        <Row k="Value" v={<>{fmt(s.value, 3)} {s.unit} <Provenance kind="measured" /></>} />
        <Row k="Quality" v={<Badge tone={s.quality === "GOOD" ? "normal" : s.quality === "STALE" ? "offline" : "warning"}>{s.quality}</Badge>} />
        <Row k="Last update" v={s.ts ? new Date(s.ts * 1000).toISOString().slice(11, 19) + " UTC" : "—"} />
      </>
    );
    hist = [sel.id];
  }
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-[11.5px] capitalize text-ink-3">{sel.kind}</div>
          <div className="display text-[22px] font-medium">{sel.id}</div>
        </div>
        {alerts.length ? <Badge tone="critical">abnormal</Badge> : <Badge tone="normal">no open alert</Badge>}
      </div>
      <div>{body}</div>
      {alerts.map((a, i) => (
        <div key={i} className="rounded-2xl bg-crit/8 p-2.5 text-[12px] ring-1 ring-crit/25">
          <div className="text-crit">{a.severity}: {a.message}</div>
          {a.investigation_id && (
            <Link className="mt-1 inline-block font-medium text-accent underline" href={`/investigations/${a.investigation_id}`}>
              Open investigation {a.investigation_id}
            </Link>
          )}
        </div>
      ))}
      {hist.length > 0 && (
        <div>
          <div className="mb-1 text-[12px] font-medium text-ink-2">History · last 2 h</div>
          <SensorHistory ids={hist} />
        </div>
      )}
      {sensorsOf.length > 0 && sel.kind !== "sensor" && (
        <div className="text-xs text-ink-3">Instruments: {sensorsOf.join(", ")}</div>
      )}
    </div>
  );
}
