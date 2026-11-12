"use client";

import { Activity, BrainCircuit, Database, Droplets, Gauge, Plus, Scale, Workflow } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { ResidualChart, SERIES } from "@/components/charts";
import {
  Badge, Card, Empty, HighlightCard, IconButton, PageHeader, Pill, Segmented, Stat, StackBar,
} from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useLive } from "@/hooks/useLive";
import { epochToLocal, fmt, humanize, signed, zSeverity } from "@/lib/format";
import type { InvestigationSummary, TankState } from "@/services/types";

const OPEN = ["CLOSED", "REJECTED", "SUPERSEDED", "DUPLICATE", "RUN_ENDED"];

function TankGauge({ id, t }: { id: string; t: TankState }) {
  const f = Math.max(0, Math.min(1, t.fill_fraction ?? 0));
  return (
    <div className="flex flex-col items-center gap-2">
      <div className="relative h-36 w-20 overflow-hidden rounded-[18px] bg-white ring-1 ring-line shadow-inner">
        <div className="absolute inset-x-0 bottom-0 bg-[linear-gradient(180deg,#e0874f,#b44a22)] transition-[height] duration-700"
          style={{ height: `${f * 100}%` }} />
        <div className="absolute inset-x-0 border-t border-dashed border-ink/25" style={{ bottom: "90%" }} title="high level" />
        <div className="absolute inset-x-0 border-t border-dashed border-ink/25" style={{ bottom: "10%" }} title="low level" />
        <div className="num absolute inset-x-0 top-2 text-center text-[13px] font-semibold text-ink">{fmt(f * 100, 0)}%</div>
      </div>
      <div className="text-center leading-tight">
        <div className="text-[12.5px] font-semibold">{id}</div>
        <div className="num text-[11.5px] text-ink-3">{fmt(t.level_m, 2)} m · {fmt(t.inventory_t_estimated, 0)} t</div>
        {t.alarm && <Badge tone="warning" className="mt-1">{t.alarm.replace("_", " ").toLowerCase()}</Badge>}
      </div>
    </div>
  );
}

export default function Dashboard() {
  const state = useLive((s) => s.state);
  const balance = useLive((s) => s.balance);
  const [range, setRange] = useState<"6h" | "24h" | "all">("6h");
  const { data: resid } = useApi<any[]>("/physics/residuals?limit=300", 15000);
  const { data: invs } = useApi<InvestigationSummary[]>("/investigations?limit=30", 8000);
  const { data: anomalies } = useApi<any[]>("/events/anomalies?limit=8", 8000);
  const { data: quality } = useApi<any>("/telemetry/quality", 20000);
  const { data: adaptive } = useApi<any>("/adaptive/status", 20000);

  const chart = useMemo(() => {
    const rows = (resid ?? []).map((r) => ({ t: r.t1, residual: r.facility_residual_t, sigma: r.rows?.facility_sigma_t, anomaly: r.anomaly }));
    if (!rows.length || range === "all") return rows;
    const tmax = Math.max(...rows.map((r) => r.t));
    const span = range === "6h" ? 6 * 3600 : 24 * 3600;
    return rows.filter((r) => r.t >= tmax - span);
  }, [resid, range]);

  const tanks = state ? Object.entries(state.tanks) : [];
  const totalInv = tanks.reduce((a, [, t]) => a + (t.inventory_t_estimated ?? 0), 0);
  const open = (invs ?? []).filter((i) => !OPEN.includes(i.status));
  const awaiting = open.filter((i) => i.status === "AWAITING_VERIFICATION").length;
  const zRows = balance ? Object.entries(balance.z).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])) : [];
  const sim = state?.simulation;
  const flow = state ? Math.max(0, ...Object.values(state.pipelines).map((p) => p.flow_m3h ?? 0)) : 0;
  const abnormal = chart.filter((c) => c.anomaly).length;
  const mix = zRows.slice(0, 5).map(([k, z], i) => ({ label: k, value: Math.abs(z), color: SERIES[i], text: signed(z, 1) }));
  const drift = adaptive?.drift?.overall ?? "—";

  return (
    <div className="space-y-5">
      <PageHeader title="Operations Overview"
        subtitle="Tank farm TF-01 · live inventory, mass-balance health and AI investigations. Every number is measured or calculated from measurements — the simulator's ground truth never reaches this view."
        actions={<>
          {sim?.active_transfer ? <Pill tone="dark"><Activity size={12} /> {sim.active_transfer.route} running</Pill> : <Pill tone="light">No transfer</Pill>}
          <Segmented value={range} onChange={setRange} options={[{ value: "6h", label: "6H" }, { value: "24h", label: "24H" }, { value: "all", label: "All" }]} />
        </>} />

      <div className="glass card-shadow rise grid gap-x-6 gap-y-4 rounded-[22px] px-5 py-4 ring-1 ring-white/70 sm:grid-cols-2 xl:grid-cols-5">
        <Stat icon={<Scale size={17} />} value={signed(balance?.facility_residual_t, 2)} unit="t"
          label={balance?.facility_sigma_t ? `Facility residual · ±${fmt(2 * balance.facility_sigma_t, 2)} t (2σ)` : "Facility residual (R > 0 = apparent loss)"}
          pill={balance ? (balance.anomaly ? "abnormal" : "within ±2σ") : undefined} pillTone={balance?.anomaly ? "red" : "green"} />
        <Stat icon={<Droplets size={17} />} value={fmt(totalInv, 0)} unit="t" label="Total inventory · gauges + lab density" />
        <Stat icon={<Gauge size={17} />} value={fmt(flow, 0)} unit="m³/h"
          label={sim?.active_transfer ? `${sim.active_transfer.transfer_id} on ${sim.active_transfer.route}` : "No movement"} />
        <Stat icon={<Workflow size={17} />} value={open.length} label="Open investigations"
          pill={awaiting ? `${awaiting} awaiting check` : undefined} pillTone="orange" />
        <Stat icon={<Database size={17} />} value={quality ? fmt(quality.overall_completeness * 100, 1) : "—"} unit="%"
          label={`Data completeness · last ${Math.max(1, Math.round((quality?.window_s ?? 1800) / 60))} min`} pill={quality && quality.overall_completeness < 0.98 ? "below 98%" : undefined} pillTone="red" />
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card className="xl:col-span-2" title="Facility mass balance" href="/loss-analytics"
          actions={<Segmented value={range} onChange={setRange} options={[{ value: "6h", label: "6H" }, { value: "24h", label: "24H" }]} />}>
          <div className="mb-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="num display text-[34px] font-medium">{signed(balance?.facility_residual_t, 2)} t</span>
            <span className="text-[12px] text-ink-3">
              latest 30-min window{balance ? ` · χ² ${fmt(balance.chi2, 1)}/${balance.dof} · max|z| ${fmt(balance.max_abs_z, 1)}` : ""}
              {` · ${abnormal} abnormal window${abnormal === 1 ? "" : "s"} in view`}
            </span>
          </div>
          {chart.length ? <ResidualChart data={chart} height={270} /> : <Empty>Waiting for the first balance window…</Empty>}
        </Card>
        <Card title="Tank inventory" subtitle="Measured gauge level · estimated mass" href="/digital-twin">
          {tanks.length ? (
            <div className="flex justify-around pt-2">{tanks.map(([id, t]) => <TankGauge key={id} id={id} t={t} />)}</div>
          ) : <Empty>Waiting for telemetry…</Empty>}
        </Card>
      </div>

      <div>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-[15px] font-semibold tracking-[-0.01em]">Open investigations</h2>
          <div className="flex items-center gap-2 text-[12px] text-ink-3">
            by posterior probability
            <IconButton title="New investigation" href="/investigations"><Plus size={14} /></IconButton>
          </div>
        </div>
        {open.length ? (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {open.slice(0, 8).map((i) => {
              const [fam, target] = (i.top_hypothesis ?? "").split(":");
              return (
                <HighlightCard key={i.investigation_id} href={`/investigations/${i.investigation_id}`}
                  variant={(i.status === "AWAITING_VERIFICATION" && (i.top_probability ?? 0) >= 0.6) || i.status === "RESOLVED_PENDING_APPROVAL" ? "orange" : i.status === "NEEDS_DATA" ? "light" : "dark"}
                  caption={`${i.investigation_id} · ${epochToLocal(i.t1).slice(11)}`}
                  title={fam ? `${humanize(fam)}${target ? ` · ${target}` : ""}` : "Collecting evidence"}
                  subtitle={(i.assets ?? []).join(", ") || "facility"}
                  value={i.top_probability != null ? `${Math.round(i.top_probability * 100)}%` : "—"}
                  foot={humanize(i.status)} chips={i.assets} />
              );
            })}
          </div>
        ) : (
          <Card><Empty>No open investigations — the balance is within its calibrated uncertainty.</Empty></Card>
        )}
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card title="Balance row mix" subtitle="Share of |z| per balance row in the latest window" href="/loss-analytics">
          {mix.length ? (
            <>
              <StackBar parts={mix} />
              <table className="data mt-4">
                <tbody>
                  {zRows.slice(0, 6).map(([k, z]) => (
                    <tr key={k}>
                      <td className="mono text-[12px]">{k}</td>
                      <td className="num text-right">{signed(z, 2)}</td>
                      <td className="text-right"><Badge tone={zSeverity(z)}>{zSeverity(z)}</Badge></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : <Empty>No balance evaluated yet.</Empty>}
        </Card>
        <Card title="Anomaly events" subtitle="Confirmed by the alarm on-delay" href="/loss-analytics">
          {(anomalies ?? []).length ? (
            <ul className="space-y-2">
              {(anomalies ?? []).slice(0, 5).map((a) => (
                <li key={a.event_id} className="rounded-2xl bg-white/70 p-3 ring-1 ring-line">
                  <div className="flex items-center justify-between gap-2">
                    <span className="mono text-[12px] font-medium">{a.event_id}</span>
                    <Badge tone={a.severity === "CRITICAL" ? "critical" : a.severity === "INFO" ? "info" : "warning"}>{a.severity.toLowerCase()}</Badge>
                  </div>
                  <div className="mt-1 line-clamp-2 text-[12px] text-ink-2">{a.summary}</div>
                  <div className="mt-1 text-[11.5px] text-ink-3">{epochToLocal(a.t1)} · {(a.assets ?? []).join(", ")}</div>
                </li>
              ))}
            </ul>
          ) : <Empty>No anomalies detected.</Empty>}
        </Card>
        <Card title="Adaptive intelligence" subtitle="Adapt first, retrain last" href="/adaptive">
          <div className="flex items-center gap-3">
            <div className="flex size-12 items-center justify-center rounded-2xl bg-dark text-white"><BrainCircuit size={20} /></div>
            <div>
              <div className="text-[13px] text-ink-3">Drift status</div>
              <Badge tone={drift === "RED" ? "critical" : drift === "AMBER" ? "warning" : drift === "GREEN" ? "normal" : "offline"}>{String(drift).toLowerCase()}</Badge>
            </div>
            <div className="ml-auto text-right">
              <div className="text-[13px] text-ink-3">Retrain gate</div>
              <div className="text-[13px] font-medium">{humanize(adaptive?.retrain_gate?.status ?? "—")}</div>
            </div>
          </div>
          <div className="mt-4 space-y-1.5">
            {(adaptive?.last_cycle?.steps ?? []).slice(0, 10).map((s: any) => (
              <div key={s.key} className="flex items-center gap-2 text-[12px]">
                <span className={`size-2 rounded-full ${s.status === "alert" ? "bg-accent" : s.status === "ok" ? "bg-ok" : s.status === "error" ? "bg-crit" : "bg-off/50"}`} />
                <span className="w-44 shrink-0 text-ink-2">{s.label}</span>
                <span className="truncate text-ink-3">{s.summary}</span>
              </div>
            ))}
            {!adaptive?.last_cycle && <Empty>The first adaptive cycle runs after six monitoring evaluations.</Empty>}
          </div>
          <div className="mt-3 text-[11.5px] text-ink-3">{(adaptive?.experience?.verified_cases ?? 0)} verified cases in experience memory · {adaptive?.twin_validations ?? 0} twin replays</div>
        </Card>
      </div>
      <p className="text-center text-[11.5px] text-ink-3">Recommendations require engineer approval · this platform has no write path to plant control systems · <Link className="underline" href="/settings">settings</Link></p>
    </div>
  );
}
