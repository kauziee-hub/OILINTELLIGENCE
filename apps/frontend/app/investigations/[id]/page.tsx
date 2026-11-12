"use client";

import { useParams } from "next/navigation";
import { useState } from "react";
import { BarsChart } from "@/components/charts";
import { Badge, Button, Card, Empty, ErrorNote, HighlightCard, JsonView, PageHeader, ProbBar, Segmented, Spinner } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useLive } from "@/hooks/useLive";
import { API_URL, api, getSession, hasRole } from "@/services/api";
import { fmt, humanize, signed, statusTone } from "@/lib/format";
import type { Check, InvestigationDetail, TwinValidation } from "@/services/types";

function probTone(p: number) {
  return p >= 0.8 ? "critical" : p >= 0.4 ? "warning" : "info";
}

export default function InvestigationPage() {
  const { id } = useParams<{ id: string }>();
  const { data: d, reload, error } = useApi<InvestigationDetail>(`/investigations/${id}`, 8000);
  const mode = useLive((s) => s.state?.mode);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [cause, setCause] = useState("");
  const [comments, setComments] = useState("");
  const [tab, setTab] = useState<"report" | "evidence" | "tools" | "posterior">("report");
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

  // a failed background refresh keeps the last good record on screen (see the note below the header)
  if (error && !d) return <ErrorNote error={error} />;
  if (!d) return <Spinner />;
  const rep = d.report;
  const rc: Check | null = d.recommended_check;
  const open = !["CLOSED", "REJECTED", "SUPERSEDED", "RUN_ENDED"].includes(d.status);
  const top = Object.entries(d.posterior ?? {}).sort((a, b) => b[1] - a[1])[0];
  const twin: TwinValidation | null | undefined = d.twin_validation;
  const download = async (fmt_: string) => {
    const res = await fetch(`${API_URL}/api/v1/investigations/${id}/report?format=${fmt_}`,
      { headers: { Authorization: `Bearer ${getSession()?.token}` } });
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${id}.${fmt_ === "markdown" ? "md" : fmt_}`;
    a.click();
  };

  return (
    <div className="space-y-4">
      <PageHeader
        title={`Investigation ${d.investigation_id}`}
        subtitle={<>Analysis period {rep?.period.start} → {rep?.period.end} · assets {d.assets.join(", ") || "facility"}</>}
        actions={
          <>
            <Badge tone={statusTone(d.status)}>{d.status.replace(/_/g, " ")}</Badge>
            {/* the model's diagnosis before any field check; verification results then drive the case status */}
            {rep?.diagnosis_status && (
              <Badge tone={d.verifications?.length ? "info" : statusTone(rep.diagnosis_status)}>
                {d.verifications?.length ? `model before checks: ${humanize(rep.diagnosis_status)}` : humanize(rep.diagnosis_status)}
              </Badge>
            )}
            {canAct && open && (
              <Button onClick={() => act("refresh", () => api(`/investigations/${id}/refresh`, { method: "POST" }))} disabled={!!busy}>
                {busy === "refresh" ? <Spinner label="Re-analysing" /> : "Re-analyse with latest data"}
              </Button>
            )}
            <Button onClick={() => download("html")}>HTML</Button>
            <Button onClick={() => download("markdown")}>Markdown</Button>
            <Button onClick={() => download("json")}>JSON</Button>
          </>
        } />
      <ErrorNote error={err} />
      {error && <ErrorNote error={`Live refresh failed (${error}); showing the last record received.`} />}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <HighlightCard variant={open ? "orange" : "dark"} menu={false}
          caption={`Leading explanation · ${humanize(d.status)}`}
          title={top ? humanize(top[0].split(":")[0]) + (top[0].includes(":") ? ` · ${top[0].split(":")[1]}` : "") : "Collecting evidence"}
          subtitle={d.final ? `Final cause: ${String(d.final.verified_cause)}` : "posterior after all verification results"}
          value={top ? `${Math.round(top[1] * 100)}%` : "—"} foot={`${d.verifications?.length ?? 0} field check(s) recorded`} chips={d.assets} />
        <Card title="Observed residual">
          <div className="num display text-[26px] font-medium">{signed(rep?.observed_residual_tonnes, 3)} t</div>
          <div className="text-xs text-ink-3">1σ {fmt(rep?.residual_uncertainty_tonnes, 3)} t · R &gt; 0 = apparent loss</div>
        </Card>
        <Card title="Estimated physical loss">
          <div className="num display text-[26px] font-medium">{rep?.estimated_physical_loss_tonnes != null ? `${fmt(rep.estimated_physical_loss_tonnes, 2)} t` : "—"}</div>
          <div className="text-xs text-ink-3">
            {rep?.estimated_physical_loss_tonnes != null && rep.physical_loss_interval_tonnes
              ? `90% interval ${fmt(rep.physical_loss_interval_tonnes[0], 2)}–${fmt(rep.physical_loss_interval_tonnes[1], 2)} t`
              : "withheld until evidence supports a defensible estimate"}
          </div>
        </Card>
        <Card title="Cause category">
          {rep?.category_probabilities ? Object.entries(rep.category_probabilities).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([k, v]) => (
            <div key={k} className="text-xs"><span className="text-ink-2">{humanize(k)}</span><ProbBar p={v} tone={k === "physical_loss" ? "critical" : "info"} /></div>
          )) : "—"}
        </Card>
        <Card title="Approval">
          <Badge tone={rep?.requires_engineer_approval ? "warning" : "normal"}>engineer approval required</Badge>
          <div className="mt-2 text-xs text-ink-3">Narrative: {rep?.narrative_source}</div>
          {d.final && <div className="mt-1 text-xs text-ok">Final cause: {String(d.final.verified_cause)}</div>}
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1fr_380px]">
        <div className="space-y-4">
          <Card title="Competing explanations" subtitle="Calibrated probabilities from the physics-guided Bayesian model (updated by verification results)">
            {rep?.candidate_hypotheses?.length ? (
              <div className="space-y-2">
                {rep.candidate_hypotheses.map((h) => (
                  <div key={h.hypothesis_id}>
                    <div className="flex justify-between text-sm">
                      <span>{h.label}</span>
                      <span className="num text-xs text-ink-3">{Object.entries(h.magnitude ?? {}).filter(([, v]) => v != null && v !== "")
                        .slice(0, 3).map(([k, v]) => `${k}=${v}`).join(" · ")}</span>
                    </div>
                    <ProbBar p={h.calibrated_probability} tone={probTone(h.calibrated_probability)} />
                  </div>
                ))}
              </div>
            ) : <Empty>{d.missing_evidence?.length ? "Diagnosis withheld — evidence missing." : "No hypotheses."}</Empty>}
            {d.missing_evidence?.length > 0 && (
              <div className="mt-3 rounded-2xl bg-warn/10 p-2.5 text-[12px] text-[#9a6510] ring-1 ring-warn/30">
                Missing evidence: {d.missing_evidence.join("; ")}
              </div>
            )}
            {rep?.conflicts?.map((c, i) => <div key={i} className="mt-2 rounded-2xl bg-warn/10 p-2.5 text-[12px] text-[#9a6510] ring-1 ring-warn/30">{c}</div>)}
          </Card>

          <Card title="Validate via digital twin"
            subtitle="Each leading cause is replayed through the full nonlinear simulator from the measured initial state and the recorded transfers; the replayed balance residuals are compared with the observed ones."
            actions={canAct ? (
              <Button variant="primary" disabled={!!busy}
                onClick={() => act("twin", () => api(`/investigations/${id}/twin-validate`, { method: "POST" }))}>
                {busy === "twin" ? <Spinner label="Replaying in the twin" /> : twin ? "Replay again" : "Run twin replay"}
              </Button>
            ) : undefined}>
            {twin ? (
              <div className="space-y-3">
                <table className="data">
                  <thead><tr><th>Hypothesis replayed</th><th className="text-right">χ²/dof</th><th className="text-right">p</th>
                    <th className="text-right">observed</th><th className="text-right">twin</th><th>verdict</th></tr></thead>
                  <tbody>
                    {twin.results.map((r) => (
                      <tr key={r.hypothesis_id}>
                        <td className="text-[12.5px]">{r.hypothesis_id}{r.fault && <div className="mono text-[10.5px] text-ink-3">{r.fault.fault_type} {Object.entries(r.fault.params).map(([k, v]) => `${k}=${typeof v === "number" ? v.toFixed(3) : v}`).join(" ")}</div>}</td>
                        {r.replayed ? (<>
                          <td className="num text-right">{fmt(r.chi2_per_dof, 2)}</td>
                          <td className="num text-right">{r.p_value != null ? r.p_value.toExponential(1) : "—"}</td>
                          <td className="num text-right">{fmt(r.observed_facility_t, 2)} t</td>
                          <td className="num text-right">{fmt(r.twin_facility_t, 2)} t</td>
                          <td><Badge tone={r.consistent ? "normal" : "critical"}>{r.consistent ? "consistent" : "rejected"}</Badge>
                            {twin.best_supported === r.hypothesis_id && <Badge tone="info" className="ml-1">best</Badge>}</td>
                        </>) : <td colSpan={5} className="text-[12px] text-ink-3">{r.reason}</td>}
                      </tr>
                    ))}
                  </tbody>
                </table>
                {(() => {
                  const best = twin.results.find((r) => r.hypothesis_id === twin.best_supported);
                  return best?.per_window?.length ? (
                    <BarsChart height={190} keys={["observed", "twin replay"]} colors={["#2b2a27", "#d4572d"]}
                      data={best.per_window.map((w) => ({ name: `w${w.window}`, observed: w.observed_t, "twin replay": w.twin_t }))} />
                  ) : null;
                })()}
                <p className="text-[11.5px] text-ink-3">Replayed transfers: {twin.transfers_replayed.join(", ") || "none"} · {twin.windows} windows · {twin.seconds} s · uses telemetry and records only, never simulator ground truth.</p>
              </div>
            ) : <Empty>No twin replay yet{canAct ? " — run one to test the leading causes against the full physics." : "."}</Empty>}
          </Card>

          <Card title="Investigation record" actions={
            <Segmented value={tab} onChange={setTab} options={(["report", "evidence", "posterior", "tools"] as const).map((t) => ({ value: t, label: t }))} />
          }>
            {tab === "report" && <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink-2">{rep?.narrative}</p>}
            {tab === "evidence" && (
              <table className="data">
                <thead><tr><th>ID</th><th>Type</th><th>Source</th><th>Summary</th></tr></thead>
                <tbody>{rep?.evidence.map((e) => (
                  <tr key={e.id}><td className="num">{e.id}</td><td className="text-xs">{e.type}</td><td className="text-xs text-ink-3">{e.source}</td><td className="text-xs">{e.summary}</td></tr>
                ))}</tbody>
              </table>
            )}
            {tab === "posterior" && (
              <BarsChart height={260} keys={["probability"]}
                data={Object.entries(d.posterior ?? {}).slice(0, 10).map(([k, v]) => ({ name: k.replace("PIPELINE_LEAK", "LEAK"), probability: Number(v.toFixed(4)) }))} />
            )}
            {tab === "tools" && (
              <table className="data">
                <thead><tr><th>#</th><th>Tool</th><th>Status</th><th className="text-right">ms</th><th>Note</th></tr></thead>
                <tbody>{(d.tool_calls ?? []).map((t, i) => (
                  <tr key={i}><td className="num text-ink-3">{i + 1}</td><td className="num text-xs">{t.tool}</td>
                    <td><Badge tone={t.status === "OK" ? "normal" : "critical"}>{t.status}</Badge></td>
                    <td className="num text-right text-xs">{fmt(t.duration_ms, 0)}</td><td className="text-xs text-crit">{t.error ?? ""}</td></tr>
                ))}</tbody>
              </table>
            )}
          </Card>
        </div>

        <div className="space-y-4">
          <Card title="Recommended next check" subtitle="Highest expected information gain per unit cost & safety exposure">
            {rc ? (
              <div className="space-y-2 text-sm">
                <div className="font-semibold">{rc.description}</div>
                <div className="num text-xs text-ink-3">{rc.action_id}</div>
                <div className="text-xs text-ink-2">{rc.rationale ?? rc.reason}</div>
                <div className="grid grid-cols-3 gap-2 text-xs">
                  <div><div className="text-ink-3">Info gain</div><div className="num">{fmt(rc.expected_information_gain_bits, 3)} bits</div></div>
                  <div><div className="text-ink-3">Cost</div><div className="num">{rc.cost}</div></div>
                  <div><div className="text-ink-3">Duration</div><div className="num">{rc.duration_h} h</div></div>
                </div>
                {canAct && open && (
                  <div className="space-y-2 pt-2">
                    {mode === "SIMULATION" && (
                      <Button variant="primary" className="w-full justify-center" disabled={!!busy}
                        onClick={() => act("oracle", () => api(`/investigations/${id}/verify`, { method: "POST",
                          json: { action_id: rc.action_id, use_simulation_oracle: true } }))}>
                        {busy === "oracle" ? <Spinner label="Performing" /> : "Perform check (simulation oracle)"}
                      </Button>
                    )}
                    <div className="flex gap-2">
                      {(["POSITIVE", "NEGATIVE"] as const).map((o) => (
                        <Button key={o} className="flex-1 justify-center" disabled={!!busy}
                          onClick={() => act(o, () => api(`/investigations/${id}/verify`, { method: "POST",
                            json: { action_id: rc.action_id, use_simulation_oracle: false, outcome: o } }))}>
                          Record {o.toLowerCase()}
                        </Button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ) : <Empty>No further check recommended.</Empty>}
            {rep?.alternative_checks?.length > 0 && (
              <div className="mt-3 border-t border-line pt-2 text-xs text-ink-3">
                Alternatives: {rep.alternative_checks.map((a) => `${a.action_id} (${fmt(a.expected_information_gain_bits, 2)} bits${a.available ? "" : ", unavailable"})`).join("; ")}
              </div>
            )}
          </Card>

          <Card title="Similar verified cases" subtitle="Experience memory · engineer-confirmed investigations">
            {d.similar_cases?.length ? (
              <ul className="space-y-2">
                {d.similar_cases.map((c) => (
                  <li key={c.case_id} className="rounded-2xl bg-white/70 p-2.5 ring-1 ring-line">
                    <div className="flex items-center justify-between text-[12.5px]">
                      <span className="mono font-medium">{c.case_id}</span>
                      <span className="num text-ink-3">similarity {(c.similarity * 100).toFixed(0)}%</span>
                    </div>
                    <div className="mt-0.5 text-[12px] text-ink-2">confirmed: {c.verified_cause}</div>
                    {c.verification_method && <div className="text-[11.5px] text-ink-3">via {c.verification_method}</div>}
                  </li>
                ))}
              </ul>
            ) : <Empty>No similar verified case in memory yet.</Empty>}
          </Card>

          <Card title="Verification history">
            {d.verifications?.length ? (
              <ul className="space-y-1 text-xs">
                {d.verifications.map((v, i) => (
                  <li key={i} className="flex items-center justify-between gap-2">
                    <span className="num">{v.action_id}</span>
                    <Badge tone={v.outcome === "POSITIVE" ? "critical" : "normal"}>{v.outcome}</Badge>
                  </li>
                ))}
              </ul>
            ) : <Empty>No checks performed yet.</Empty>}
          </Card>

          {canAct && open && (
            <Card title="Engineer review">
              <div className="space-y-2 text-sm">
                <select className="w-full" value={cause} onChange={(e) => setCause(e.target.value)}>
                  <option value="">verified cause…</option>
                  {Object.keys(d.posterior ?? {}).map((h) => <option key={h} value={h}>{h}</option>)}
                </select>
                <textarea className="w-full" rows={3} placeholder="Comments (field findings, references)" value={comments}
                  onChange={(e) => setComments(e.target.value)} />
                <div className="flex gap-2">
                  <Button variant="primary" className="flex-1 justify-center" disabled={!!busy}
                    onClick={() => act("confirm", () => api(`/investigations/${id}/review`, { method: "POST",
                      json: { decision: "CONFIRM", verified_cause: cause || Object.keys(d.posterior ?? {})[0], comments } }))}>
                    Confirm & close
                  </Button>
                  <Button variant="danger" className="flex-1 justify-center" disabled={!!busy}
                    onClick={() => act("reject", () => api(`/investigations/${id}/review`, { method: "POST",
                      json: { decision: "REJECT", verified_cause: cause || null, comments } }))}>
                    Reject
                  </Button>
                </div>
              </div>
            </Card>
          )}

          <Card title="Reviews & audit">
            {d.reviews?.length ? d.reviews.map((r, i) => (
              <div key={i} className="border-b border-line py-1 text-xs">
                <span className="text-ink-2">{r.reviewer}</span> · {r.decision} {r.action_id ? `· ${r.action_id} → ${r.outcome}` : ""}
                {r.verified_cause ? ` · ${r.verified_cause}` : ""} <span className="text-ink-3">{r.created_at?.slice(0, 19)}</span>
                {r.comments && <div className="text-ink-3">{r.comments}</div>}
              </div>
            )) : <Empty>No reviews.</Empty>}
          </Card>
        </div>
      </div>
      <details className="text-xs text-ink-3">
        <summary>Raw balance evidence</summary>
        <JsonView value={d.balance} />
      </details>
    </div>
  );
}
