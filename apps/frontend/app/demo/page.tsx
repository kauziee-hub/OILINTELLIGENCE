"use client";

import clsx from "clsx";
import { Check, ClipboardCheck, ExternalLink, Flag, Play, RefreshCw, Square, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Badge, Button, Card, Empty, ErrorNote, PageHeader, Pill, ProbBar, Segmented, Spinner } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { fmt, humanize, pct } from "@/lib/format";
import { api, hasRole } from "@/services/api";

type Verdict = "PASS" | "FAIL" | "RUNNING";
const VERDICT_TONE: Record<Verdict, "green" | "red" | "dark"> = { PASS: "green", FAIL: "red", RUNNING: "dark" };
const CATEGORY_COLOR: Record<string, string> = {
  Baseline: "#2e7d4f", "Physical loss": "#c84b23", "Measurement error": "#2f6fb3", "Physical movement": "#127a7a",
  Accounting: "#b8861b", Robustness: "#6b3fa0", Equipment: "#3c4a5e",
};

function hm(s: number | null | undefined) {
  if (s == null) return "—";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}:${String(m).padStart(2, "0")}`;
}

/** Fault parameter in words: hole_diameter_mm 9 -> "hole diameter 9 mm", bias_rel 0.025 -> "bias 2.5%". */
function param(k: string, v: unknown) {
  const units: [RegExp, string, number][] = [
    [/_mm$/, " mm", 1], [/_s$/, " s", 1], [/_m_per_h$/, " m/h", 1], [/_(rel|frac|fraction)$/, "%", 100],
  ];
  for (const [re, unit, f] of units) {
    if (re.test(k)) return `${k.replace(re, "").replace(/_/g, " ")} ${typeof v === "number" ? +(v * f).toFixed(3) : v}${unit}`;
  }
  return `${k.replace(/_/g, " ")} ${v}`;
}

function StepDot({ state }: { state: string }) {
  const base = "flex size-8 shrink-0 items-center justify-center rounded-full";
  if (state === "done") return <span className={clsx(base, "bg-dark text-white")}><Check size={15} /></span>;
  if (state === "failed") return <span className={clsx(base, "bg-crit text-white")}><X size={15} /></span>;
  if (state === "active") return <span className={clsx(base, "bg-accent text-white")}><span className="size-2.5 animate-pulse rounded-full bg-white" /></span>;
  return <span className={clsx(base, "bg-white ring-1 ring-line")}><span className="size-2 rounded-full bg-sand" /></span>;
}

function Steps({ steps }: { steps: any[] }) {
  return (
    <ol className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
      {steps.map((s, i) => (
        <li key={s.key} className="flex gap-2.5 lg:flex-col">
          <div className="flex items-center gap-2">
            <StepDot state={s.state} />
            {i < steps.length - 1 && <span className="hidden h-px flex-1 bg-line lg:block" />}
          </div>
          <div className="min-w-0">
            <div className={clsx("text-[13px] font-semibold", s.state === "pending" ? "text-ink-3" : "text-ink")}>{s.label}</div>
            <div className="text-[11.5px] leading-snug text-ink-3">{s.detail || "—"}</div>
          </div>
        </li>
      ))}
    </ol>
  );
}

function Progress({ st }: { st: any }) {
  const dur = st.session.duration_s;
  const inj = st.session.inject_s;
  const p = Math.min(1, st.progress || 0);
  return (
    <div>
      <div className="relative h-3 overflow-hidden rounded-full bg-white/15">
        <div className="h-full rounded-full bg-[linear-gradient(90deg,#f0a27f,#df6538)] transition-[width] duration-700" style={{ width: `${p * 100}%` }} />
        {inj != null && <span className="absolute top-0 h-full w-0.5 bg-white" style={{ left: `${(inj / dur) * 100}%` }} />}
      </div>
      <div className="mt-1.5 flex justify-between text-[11px] text-white/60">
        <span>0:00</span>
        {inj != null && <span>fault at {hm(inj)}</span>}
        <span>{hm(dur)} plant time</span>
      </div>
    </div>
  );
}

function LiveRun({ st, onAct, busy }: { st: any; onAct: (label: string, path: string) => void; busy: string | null }) {
  const s = st.session;
  const inv = st.investigation;
  const exp = s.expected;
  const truth = st.truth ?? {};
  const verdict = st.verdict as Verdict;
  const canEng = hasRole("engineer");
  const leaks = Object.entries(truth.leaks_t_h ?? {}) as [string, number][];
  const waitingCheck = inv?.status === "AWAITING_VERIFICATION" && inv?.recommended_check;
  return (
    <div className="space-y-5">
      <section className="card-shadow rounded-[24px] bg-[linear-gradient(160deg,#2f2d2a,#1c1b19)] p-5 text-white">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-[11.5px] uppercase tracking-wide text-white/50">Live demo · {s.id} · {s.category} · {s.speed}× speed</div>
            <div className="display mt-0.5 text-[26px] font-medium">{s.title}</div>
            <div className="mt-0.5 text-[12.5px] text-white/65">Expected answer: <b className="text-white">{exp.label}</b> ({exp.hypothesis})</div>
          </div>
          <div className="text-right">
            <Pill tone={VERDICT_TONE[verdict]} className="text-[13px]">{verdict === "RUNNING" ? (st.ended ? "scenario ended" : "running") : verdict}</Pill>
            <div className="num display mt-1 text-[24px]">{hm(st.sim_time_s)}</div>
            <div className="text-[11px] text-white/50">plant time elapsed{st.sim_state && st.sim_state !== "RUNNING" ? ` · ${st.sim_state.toLowerCase()}` : ""}</div>
          </div>
        </div>
        <div className="mt-4"><Progress st={st} /></div>
        {!st.same_run && <p className="mt-3 text-[12px] text-[#f0a27f]">The simulator was restarted outside this demo; start the scenario again to keep scoring it.</p>}
      </section>

      <Card title="What the platform is doing" subtitle="Each step turns dark when the platform completes it on its own; it is never told what was injected.">
        <Steps steps={st.steps} />
      </Card>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card title="The platform's answer" subtitle={inv ? `${inv.investigation_id} · ${humanize(inv.status)}` : "No investigation yet"}
          actions={inv && <Link href={`/investigations/${inv.investigation_id}`} className="inline-flex items-center gap-1 text-[12px] font-medium text-accent">Open case <ExternalLink size={12} /></Link>}>
          {inv ? (
            <div className="space-y-3">
              <ul className="space-y-2">
                {inv.candidates.map((c: any) => (
                  <li key={c.hypothesis_id} className={clsx("rounded-[14px] px-3 py-2", c.hypothesis_id === exp.hypothesis ? "bg-ok/10 ring-1 ring-ok/40" : "bg-white/70 ring-1 ring-line")}>
                    <div className="truncate text-[12.5px] font-medium">{c.label || c.hypothesis_id}</div>
                    <ProbBar p={c.p} tone={c.hypothesis_id === exp.hypothesis ? "normal" : "info"} />
                    {c.hypothesis_id === exp.hypothesis && <div className="mt-0.5 text-[10.5px] font-medium text-ok">← the injected truth</div>}
                  </li>
                ))}
              </ul>
              {inv.recommended_check && (
                <div className="rounded-[14px] bg-panel-2 px-3 py-2 text-[12px] ring-1 ring-line">
                  <div className="font-semibold">Next recommended check</div>
                  <div className="text-ink-2">{inv.recommended_check.description}</div>
                  <div className="text-ink-3">cost {fmt(inv.recommended_check.cost, 0)} · {fmt(inv.recommended_check.duration_h, 1)} h · {fmt(inv.recommended_check.expected_information_gain_bits, 2)} bits expected</div>
                </div>
              )}
              {inv.verifications?.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {inv.verifications.map((v: any, i: number) => (
                    <Badge key={i} tone={v.outcome === "POSITIVE" ? "warning" : "offline"}>{v.action_id} → {String(v.outcome).toLowerCase()}</Badge>
                  ))}
                </div>
              )}
              {canEng && (
                <div className="flex flex-wrap gap-2">
                  <Button variant="accent" disabled={!!busy || !waitingCheck} onClick={() => onAct("verify", "/demo/verify")}
                    title="Plays the field engineer: each recommended check is carried out in the simulator, with realistic sensitivity">
                    {busy === "verify" ? <Spinner label="Checking" /> : <><ClipboardCheck size={14} /> Run field checks</>}
                  </Button>
                  <Button disabled={!!busy} onClick={() => onAct("reanalyse", "/demo/reanalyse")} title="Re-run the attribution with all evidence collected so far">
                    {busy === "reanalyse" ? <Spinner label="Analysing" /> : <><RefreshCw size={14} /> Re-analyse</>}
                  </Button>
                </div>
              )}
            </div>
          ) : <Empty>{exp.kind === "normal" ? "No investigation, which is exactly what should happen." : "Waiting for the monitor to raise an anomaly event."}</Empty>}
        </Card>

        <Card title="Ground truth vs estimate" subtitle="Truth comes from the simulator and is hidden from the platform">
          <div className="space-y-3 text-[12.5px]">
            <div className="rounded-[14px] bg-white/70 p-3 ring-1 ring-line">
              <div className="text-[11px] uppercase tracking-wide text-ink-3">Injected fault</div>
              {s.fault.length ? s.fault.map((f: any, i: number) => (
                <div key={i} className="mt-1">
                  <b>{humanize(f.fault_type)}</b> on <b>{f.target}</b>
                  {Object.keys(f.params ?? {}).length > 0 && (
                    <div className="text-ink-3">{Object.entries(f.params).map(([k, v]) => param(k, v)).join(" · ")}</div>
                  )}
                </div>
              )) : <div className="mt-1">none (normal operation)</div>}
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div className="rounded-[14px] bg-white/70 p-3 ring-1 ring-line">
                <div className="text-[11px] text-ink-3">True oil lost so far</div>
                <div className="num display text-[20px]">{fmt(truth.physical_loss_cum_t, 2)} <span className="text-[12px] text-ink-3">t</span></div>
                <div className="text-[11px] text-ink-3">{leaks.length ? leaks.map(([k, v]) => `${k} ${fmt(v, 1)} t/h`).join(", ") : "no leak flowing now"}</div>
              </div>
              <div className="rounded-[14px] bg-white/70 p-3 ring-1 ring-line">
                <div className="text-[11px] text-ink-3">Platform&apos;s loss estimate</div>
                <div className="num display text-[20px]">{fmt(inv?.estimated_loss_t, 2)} <span className="text-[12px] text-ink-3">t</span></div>
                <div className="text-[11px] text-ink-3">
                  {inv?.loss_interval_t ? `90% interval ${fmt(inv.loss_interval_t[0], 2)}–${fmt(inv.loss_interval_t[1], 2)} t` : "for the analysed window"}
                </div>
              </div>
            </div>
            <div className="grid grid-cols-3 gap-2 text-center">
              <div className="rounded-[14px] bg-white/70 p-2 ring-1 ring-line"><div className="num text-[17px]">{st.latency_min != null ? fmt(st.latency_min, 0) : "—"}</div><div className="text-[10.5px] text-ink-3">min to detect</div></div>
              <div className="rounded-[14px] bg-white/70 p-2 ring-1 ring-line"><div className="num text-[17px]">{st.false_alarms}</div><div className="text-[10.5px] text-ink-3">false alarms</div></div>
              <div className="rounded-[14px] bg-white/70 p-2 ring-1 ring-line"><div className="num text-[17px]">{st.expected_rank ? `#${st.expected_rank}` : "—"}</div><div className="text-[10.5px] text-ink-3">rank of truth</div></div>
            </div>
            <p className="text-[11px] text-ink-3">The estimate covers the window the case analysed, so it trails the running total while the leak continues.</p>
          </div>
        </Card>

        <Card title="What to say" subtitle="Talking points for this scenario">
          <ul className="space-y-2 text-[12.5px] text-ink-2">
            {(st.talk ?? []).map((t: string, i: number) => (
              <li key={i} className="flex gap-2"><span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-accent" />{t}</li>
            ))}
          </ul>
          {st.events?.length > 0 && (
            <div className="mt-4">
              <div className="mb-1.5 text-[11px] uppercase tracking-wide text-ink-3">Anomaly events in this run</div>
              <ul className="space-y-1 text-[11.5px]">
                {st.events.map((e: any) => (
                  <li key={e.event_id} className="flex items-center justify-between gap-2">
                    <span className="truncate">{e.event_id} · {(e.assets ?? []).slice(0, 3).join(", ")}</span>
                    <Badge tone={e.severity === "CRITICAL" ? "critical" : "warning"}>{String(e.severity).toLowerCase()}</Badge>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

export default function DemoPage() {
  const { data: cat, error: catErr } = useApi<any[]>("/demo/scenarios");
  const { data: st, reload } = useApi<any>("/demo/status", 2000);
  const { data: hist, reload: reloadHist } = useApi<any[]>("/demo/history", 15000);
  const [speed, setSpeed] = useState<"30" | "60" | "120">("60");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const canRun = hasRole("operator");

  async function act(label: string, path: string, json?: unknown) {
    setBusy(label);
    setErr(null);
    try {
      await api(path, { method: "POST", json });
      await Promise.all([reload(), reloadHist()]);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(null);
    }
  }

  if (catErr && !cat) return <ErrorNote error={catErr} />;
  if (!cat) return <Spinner />;
  const activeKey = st?.active ? st.session.key : null;

  return (
    <div className="space-y-5">
      <PageHeader title="Demo Scenarios"
        subtitle="Each scenario replays the same plant with one known fault injected at a known time. The platform is not told what was injected: watch it detect the problem, rank the causes, recommend a check and confirm the answer, then compare with the ground truth."
        actions={<>
          <span className="text-[12px] text-ink-3">Speed</span>
          <Segmented value={speed} onChange={setSpeed} size="md"
            options={[{ value: "30", label: "30×" }, { value: "60", label: "60×" }, { value: "120", label: "120×" }]} />
          {st?.active && canRun && (
            <Button disabled={!!busy} onClick={() => act("finish", "/demo/finish")}><Square size={13} /> Finish &amp; save result</Button>
          )}
        </>} />
      <ErrorNote error={err} />

      {st?.active ? <LiveRun st={st} onAct={act} busy={busy} /> : (
        <Card>
          <div className="flex flex-wrap items-center gap-3 text-[13px] text-ink-2">
            <Flag size={16} className="text-accent" />
            Pick a scenario below. At 60× one minute of demo is one hour of plant time: the fault starts after about 1.5 minutes and an
            8-hour scenario takes 8 minutes. Keep this page open; the steps update every two seconds.
          </div>
        </Card>
      )}

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {cat.map((s) => {
          const color = CATEGORY_COLOR[s.category] ?? "#3c4a5e";
          const last = (hist ?? []).find((h) => h.session?.key === s.key);
          return (
            <section key={s.key} className={clsx("glass card-shadow flex flex-col rounded-[22px] p-4 ring-1",
              activeKey === s.key ? "ring-2 ring-accent" : "ring-white/70")}>
              <div className="flex items-center justify-between gap-2">
                <span className="rounded-full px-2.5 py-0.5 text-[11px] font-semibold text-white" style={{ background: color }}>{s.category}</span>
                {last && <Pill tone={VERDICT_TONE[last.verdict as Verdict] ?? "light"}>last run · {String(last.verdict ?? "—").toLowerCase()}</Pill>}
              </div>
              <h3 className="mt-2 text-[15px] font-semibold">{s.title}</h3>
              <p className="mt-1 text-[12.5px] leading-snug text-ink-2">{s.story}</p>
              <p className="mt-2 text-[12px] leading-snug text-ink-3"><b className="text-ink-2">Proves:</b> {s.proves}</p>
              <div className="mt-auto flex items-center justify-between gap-2 pt-3">
                <span className="text-[11.5px] text-ink-3">Expected: <b className="text-ink-2">{s.expected.label}</b></span>
                {canRun && (
                  <Button variant={activeKey === s.key ? "default" : "primary"} disabled={!!busy}
                    onClick={() => act(`start:${s.key}`, `/demo/scenarios/${s.key}/start`, { speed: Number(speed) })}>
                    {busy === `start:${s.key}` ? <Spinner label="Starting" /> : <><Play size={13} /> {activeKey === s.key ? "Restart" : "Run"}</>}
                  </Button>
                )}
              </div>
            </section>
          );
        })}
      </div>

      <Card title="Scoreboard" subtitle="Saved results of finished demo runs (most recent first)">
        {hist && hist.length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-left text-[12.5px]">
              <thead className="text-[11px] uppercase tracking-wide text-ink-3">
                <tr><th className="py-1.5">Scenario</th><th>Result</th><th>Detected after</th><th>Platform&apos;s answer</th><th>False alarms</th><th>Plant time</th></tr>
              </thead>
              <tbody>
                {hist.slice(0, 15).map((h, i) => (
                  <tr key={i} className="border-t border-line/70">
                    <td className="py-2 font-medium">{h.session?.title ?? "—"}</td>
                    <td><Pill tone={VERDICT_TONE[h.verdict as Verdict] ?? "light"}>{String(h.verdict ?? "—").toLowerCase()}</Pill></td>
                    <td className="num">{h.latency_min != null ? `${fmt(h.latency_min, 0)} min` : "—"}</td>
                    <td>{h.investigation ? `${h.investigation.top ?? "—"} (${pct(h.investigation.top_p, 0)})` : "no case"}</td>
                    <td className="num">{h.false_alarms ?? "—"}</td>
                    <td className="num">{hm(h.sim_time_s)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>No finished demo runs yet. Results are saved when you press “Finish &amp; save result” or start another scenario.</Empty>}
      </Card>
    </div>
  );
}
