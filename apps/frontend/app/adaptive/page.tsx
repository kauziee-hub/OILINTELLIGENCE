"use client";

import clsx from "clsx";
import { ArrowRight, BrainCircuit, Play, RotateCcw } from "lucide-react";
import { useState } from "react";
import { BarsChart } from "@/components/charts";
import { Badge, Button, Card, Empty, ErrorNote, PageHeader, Pill, Spinner, type Tone } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { fmt, humanize } from "@/lib/format";
import { api, hasRole } from "@/services/api";

const SEV: Record<string, Tone> = { GREEN: "normal", AMBER: "warning", RED: "critical" };
const STEP_STYLE: Record<string, string> = {
  ok: "bg-dark text-white", alert: "bg-accent text-white", error: "bg-crit text-white",
  skipped: "bg-white text-ink-3 ring-1 ring-line", waiting: "bg-white text-ink-3 ring-1 ring-line",
};
const LAYER_COLORS = ["#2f6fb3", "#2e7d4f", "#6b3fa0", "#c4621f", "#127a7a", "#2a3f7a", "#b8861b", "#5b2c91", "#2e7d4f",
  "#3c4a5e", "#7a2d55", "#127a4f"];
const CONVENTIONAL = ["Detect drift", "Collect data", "Retrain", "Deploy"];

function DriftTile({ s }: { s: any }) {
  const tone = SEV[s.severity] ?? "offline";
  return (
    <div className="rounded-[18px] bg-white/75 p-3.5 ring-1 ring-line">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[13px] font-semibold">{humanize(s.kind)} drift</span>
        <Badge tone={tone}>{s.severity.toLowerCase()}</Badge>
      </div>
      <div className="num display mt-1 text-[22px] font-medium">{s.statistic != null ? fmt(s.statistic, 2) : "—"}</div>
      <p className="mt-1 text-[12px] leading-snug text-ink-2">{s.message}</p>
      <p className="mt-1.5 text-[11px] text-ink-3">{s.threshold}</p>
    </div>
  );
}

/** Engineering Intelligence Core (Layer 8): reasoning engine at the centre, subsystems around it. */
function Core({ status }: { status: any }) {
  const exp = status?.experience;
  const cal = status?.calibration;
  const nodes: [string, string][] = [
    ["Behaviour Understanding", "drift characterisation: mean shift vs dispersion, step vs gradual"],
    ["Hypothesis Generation", "53 single-fault hypotheses (13 families) + two-fault pairs, UNKNOWN catch-all"],
    ["Optimisation Interaction", "next-best check: information gain per cost, safety guardrails"],
    ["Recommendation Interaction", "plain-language recommendation, engineer approval"],
    ["Confidence Engine", `calibration T=${fmt(cal?.temperature, 2)} · ε=${fmt(cal?.prior_mix, 2)} · abstention`],
    ["Digital Twin Orchestrator", `${status?.twin_validations ?? 0} twin replays of hypotheses`],
    ["Engineering Experience Memory", `${exp?.verified_cases ?? 0} verified cases → learned priors`],
    ["Engineering Knowledge Repository", "plant topology, instrument specs, verification catalogue"],
    ["Plant State Estimation", "reconciled inventories, leak rates, bias estimates"],
    ["Physics Constraint Engine", "topology-aware mass balance + uncertainty propagation"],
  ];
  const N = nodes.length;
  return (
    <div className="relative mx-auto aspect-[16/10] w-full max-w-[980px]">
      <svg className="absolute inset-0 h-full w-full" viewBox="0 0 160 100" preserveAspectRatio="none" aria-hidden>
        {nodes.map((_, i) => {
          const a = (i / N) * Math.PI * 2 - Math.PI / 2;
          return <line key={i} x1="80" y1="50" x2={80 + Math.cos(a) * 62} y2={50 + Math.sin(a) * 38} stroke="#cdbfe3" strokeWidth="0.25" />;
        })}
        <ellipse cx="80" cy="50" rx="62" ry="38" fill="none" stroke="#e3dbef" strokeWidth="0.2" strokeDasharray="1 1" />
      </svg>
      <div className="absolute left-1/2 top-1/2 w-[30%] -translate-x-1/2 -translate-y-1/2 rounded-[20px] bg-[linear-gradient(150deg,#6b3fa0,#4b2580)] p-4 text-center text-white shadow-[0_20px_50px_-20px_rgba(75,37,128,0.7)]">
        <BrainCircuit className="mx-auto mb-1" size={22} />
        <div className="text-[13px] font-semibold tracking-wide">ENGINEERING REASONING ENGINE</div>
        <div className="mt-1 text-[11px] text-white/75">LangGraph investigation agent around the physics-guided Bayesian attribution</div>
      </div>
      {nodes.map(([name, impl], i) => {
        const a = (i / N) * Math.PI * 2 - Math.PI / 2;
        const x = 50 + Math.cos(a) * 38.75;
        const y = 50 + Math.sin(a) * 38;
        return (
          <div key={name} className="absolute w-[21%] -translate-x-1/2 -translate-y-1/2 rounded-[14px] bg-white/90 px-2.5 py-2 text-center ring-1 ring-[#d9cdea] shadow-sm"
            style={{ left: `${x}%`, top: `${y}%` }}>
            <div className="text-[11.5px] font-semibold text-[#4b2580]">{name}</div>
            <div className="mt-0.5 text-[10.5px] leading-snug text-ink-3">{impl}</div>
          </div>
        );
      })}
    </div>
  );
}

export default function AdaptivePage() {
  const { data: s, reload, error } = useApi<any>("/adaptive/status", 10000);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const canAct = hasRole("engineer");

  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    setErr(null);
    try {
      await fn();
      await reload();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(null);
    }
  }

  if (error && !s) return <ErrorNote error={error} />;
  if (!s) return <Spinner />;
  const cycle = s.last_cycle;
  const steps: any[] = cycle?.steps ?? s.steps.map((x: any) => ({ ...x, status: "waiting", summary: "no cycle yet" }));
  const drift = s.drift ?? {};
  const signals: any[] = drift.signals ?? [];
  const gate = s.retrain_gate ?? {};
  const priors = (s.experience?.families ?? []).filter((f: any) => !["UNKNOWN"].includes(f.family));

  return (
    <div className="space-y-5">
      <PageHeader title="Adaptive Intelligence"
        subtitle="Adapt first, retrain last. The adaptive AI architecture (12 layers, Engineering Intelligence Core) running on the loss-attribution platform: drift is understood, explained, validated in the digital twin and absorbed by bounded, governed self-calibration; retraining is the final fallback."
        actions={<>
          <Pill tone={drift.overall === "RED" ? "red" : drift.overall === "AMBER" ? "orange" : "green"}>drift {String(drift.overall ?? "—").toLowerCase()}</Pill>
          <Pill tone={gate.status === "RECOMMENDED" ? "orange" : "dark"}>retrain gate · {humanize(gate.status ?? "—")}</Pill>
          {canAct && (
            <Button variant="accent" disabled={!!busy} onClick={() => act("run", () => api("/adaptive/run", { method: "POST" }))}>
              {busy === "run" ? <Spinner label="Running cycle" /> : <><Play size={14} /> Run adaptive cycle</>}
            </Button>
          )}
        </>} />
      <ErrorNote error={err} />

      <Card title="Architecture philosophy" subtitle={cycle ? `Last cycle ${cycle.id} · ${cycle.trigger} · ${new Date(cycle.at * 1000).toISOString().slice(11, 19)} UTC · ${cycle.seconds}s` : "Cycles run every six monitoring evaluations, or on demand"}>
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="w-28 shrink-0 text-[11.5px] font-medium uppercase tracking-wide text-ink-3">Current systems</span>
            {CONVENTIONAL.map((c, i) => (
              <span key={c} className="flex items-center gap-2">
                <span className="rounded-full bg-[#8e8a84] px-3.5 py-1.5 text-[12px] font-medium text-white">{c}</span>
                {i < CONVENTIONAL.length - 1 && <ArrowRight size={13} className="text-ink-3" />}
              </span>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="w-28 shrink-0 text-[11.5px] font-medium uppercase tracking-wide text-[#5b2c91]">New architecture</span>
            {steps.map((st, i) => (
              <span key={st.key} className="flex items-center gap-2">
                <span title={st.summary} className={clsx("rounded-full px-3.5 py-1.5 text-[12px] font-medium", STEP_STYLE[st.status] ?? STEP_STYLE.waiting)}>{st.label}</span>
                {i < steps.length - 1 && <ArrowRight size={13} className="text-ink-3" />}
              </span>
            ))}
          </div>
          <div className="grid gap-x-6 gap-y-1.5 pt-2 md:grid-cols-2">
            {steps.map((st, i) => (
              <div key={st.key} className="flex gap-2 text-[12px]">
                <span className="num w-5 shrink-0 text-ink-3">{i + 1}</span>
                <span className="w-44 shrink-0 font-medium text-ink-2">{st.label}</span>
                <span className="text-ink-3">{st.summary || "—"}</span>
              </div>
            ))}
          </div>
          <div className="flex flex-wrap gap-3 pt-1 text-[11px] text-ink-3">
            <span className="inline-flex items-center gap-1"><span className="size-2 rounded-full bg-dark" /> done</span>
            <span className="inline-flex items-center gap-1"><span className="size-2 rounded-full bg-accent" /> action / alert</span>
            <span className="inline-flex items-center gap-1"><span className="size-2 rounded-full bg-white ring-1 ring-line" /> not needed this cycle</span>
          </div>
        </div>
      </Card>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card className="xl:col-span-2" title="Layer 7 · Continuous monitoring & drift detection"
          subtitle={drift.status === "WARMING_UP" ? `Collecting windows (${drift.n_windows}/${drift.required})` :
            `${drift.n_windows} recent windows · ${drift.normal_windows} normal · effective sample factor ${fmt(drift.effective_sample_factor, 2)} (overlapping windows)`}>
          {signals.length ? (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{signals.map((x, i) => <DriftTile key={`${x.kind}-${i}`} s={x} />)}</div>
          ) : <Empty>Drift statistics need at least {drift.required ?? 12} monitoring windows of the current run.</Empty>}
        </Card>
        <Card title="Layer 9 · Retrain gate" subtitle="Retraining is the final fallback, never the default response">
          <div className={clsx("rounded-[18px] p-4 text-white", gate.status === "RECOMMENDED" ? "bg-[linear-gradient(160deg,#df6538,#c84b23)]" : "bg-[linear-gradient(160deg,#2f2d2a,#1f1e1c)]")}>
            <div className="text-[11.5px] text-white/60">Status</div>
            <div className="display text-[26px] font-medium">{humanize(gate.status ?? "—")}</div>
            <div className="mt-2 grid grid-cols-2 gap-2 text-[12px]">
              <div><div className="text-white/55">regime-red streak</div><div className="num text-[16px]">{gate.streaks?.regime_red ?? 0}</div></div>
              <div><div className="text-white/55">drift after calibration</div><div className="num text-[16px]">{gate.streaks?.drift_after_calibration ?? 0}</div></div>
            </div>
          </div>
          <ul className="mt-3 space-y-1.5 text-[12px] text-ink-2">
            <li>• Recommended only when drift persists {s.bounds?.persist_cycles ?? 3} cycles <i>after</i> self-calibration, or the operating regime stays outside the training envelope.</li>
            <li>• Needs data for the new regime: ≥10 verified cases, or simulator-generated scenarios.</li>
            <li>• A challenger is trained offline and promoted only if it beats the champion on held-out runs and passes engineer review (Layer 10).</li>
          </ul>
        </Card>
      </div>

      <Card title="Layer 8 · Engineering Intelligence Core" subtitle="All subsystems exchange information through the central reasoning engine">
        <Core status={s} />
      </Card>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card className="xl:col-span-2" title="Self-calibration & governance (Layer 10)"
          subtitle={`Uncertainty re-scaling is applied automatically inside ×${s.bounds?.auto_band?.[0]}–×${s.bounds?.auto_band?.[1]}; larger changes and meter factors need engineer approval. Every change is logged and reversible.`}>
          <div className="mb-3 flex flex-wrap gap-2">
            {Object.keys(s.kind_scale ?? {}).length === 0 && Object.keys(s.meter_factors ?? {}).length === 0 && (
              <Pill tone="light">uncertainty model and meter factors at calibrated defaults</Pill>
            )}
            {Object.entries(s.kind_scale ?? {}).map(([k, v]) => <Pill key={k} tone="dark">{k} rows σ ×{fmt(v as number, 3)}</Pill>)}
            {Object.entries(s.meter_factors ?? {}).map(([k, v]) => <Pill key={k} tone="orange">{k} meter factor {fmt(v as number, 5)}</Pill>)}
          </div>
          {(s.adaptations ?? []).length ? (
            <table className="data">
              <thead><tr><th>ID</th><th>Adaptation</th><th className="text-right">old → new</th><th>Status</th><th>Reason</th><th /></tr></thead>
              <tbody>
                {s.adaptations.slice(0, 12).map((a: any) => (
                  <tr key={a.id}>
                    <td className="mono text-[11.5px]">{a.id}</td>
                    <td className="text-[12px]">{humanize(a.type)} · <span className="font-medium">{a.target}</span></td>
                    <td className="num text-right text-[12px]">{fmt(a.old, 4)} → {fmt(a.new, 4)}</td>
                    <td><Badge tone={a.status === "APPLIED" ? "normal" : a.status === "PROPOSED" ? "warning" : "offline"}>{a.status.toLowerCase()}</Badge></td>
                    <td className="max-w-[320px] text-[11.5px] text-ink-3">{a.reason}</td>
                    <td className="whitespace-nowrap text-right">
                      {canAct && a.status === "PROPOSED" && <>
                        <Button className="mr-1" variant="primary" disabled={!!busy} onClick={() => act(a.id, () => api(`/adaptive/adaptations/${a.id}/approve`, { method: "POST" }))}>Approve</Button>
                        <Button variant="ghost" disabled={!!busy} onClick={() => act(a.id, () => api(`/adaptive/adaptations/${a.id}/reject`, { method: "POST" }))}>Reject</Button>
                      </>}
                      {canAct && a.status === "APPLIED" && (
                        <Button variant="ghost" disabled={!!busy} onClick={() => act(a.id, () => api(`/adaptive/adaptations/${a.id}/revert`, { method: "POST" }))}><RotateCcw size={13} /> Revert</Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <Empty>No adaptations yet — the models are operating inside their calibrated envelope.</Empty>}
        </Card>
        <Card title="Experience memory" subtitle={`${s.experience?.verified_cases ?? 0} verified cases · prior strength ${s.experience?.prior_strength ?? "—"} pseudo-cases · shift bounded ×3`}>
          {priors.length ? (
            <BarsChart height={250} keys={["default", "learned"]} colors={["#cfc7bb", "#d4572d"]}
              data={priors.slice(0, 8).map((f: any) => ({ name: humanize(f.family).replace(" Error", ""), default: f.default_prior, learned: f.learned_prior }))} />
          ) : <Empty>No experience yet.</Empty>}
          <p className="mt-2 text-[11.5px] text-ink-3">Confirmed investigations nudge the fault-family prior (Dirichlet update) — learning from experience without retraining any model.</p>
        </Card>
      </div>

      <Card title="Architecture map · 12 layers" subtitle="Where each layer of the adaptive AI architecture lives in OILINTELLIGENCE">
        <div className="grid gap-2.5 md:grid-cols-2 xl:grid-cols-3">
          {(s.architecture ?? []).map((l: any) => (
            <div key={l.layer} className="flex overflow-hidden rounded-[16px] bg-white/75 ring-1 ring-line">
              <div className="w-28 shrink-0 p-3 text-white" style={{ background: LAYER_COLORS[(l.layer - 1) % LAYER_COLORS.length] }}>
                <div className="text-[10px] uppercase tracking-wider text-white/70">Layer {l.layer}</div>
                <div className="text-[12px] font-semibold leading-tight">{l.name}</div>
              </div>
              <div className="p-3 text-[12px] leading-snug text-ink-2">{l.implementation}</div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
