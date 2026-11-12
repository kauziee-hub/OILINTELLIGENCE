"use client";

import { useState } from "react";
import { Badge, Button, Card, Empty, ErrorNote, PageHeader, ProbBar, Spinner } from "@/components/ui";
import { useLive } from "@/hooks/useLive";
import { api } from "@/services/api";
import { fmt, humanize, signed, statusTone } from "@/lib/format";

export default function Diagnostics() {
  const now = useLive((s) => s.state?.timestamp ?? null);
  const [hours, setHours] = useState(4);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [att, setAtt] = useState<any>(null);
  const [det, setDet] = useState<any>(null);
  const [health, setHealth] = useState<any[] | null>(null);
  const [tests, setTests] = useState<any>(null);

  async function run() {
    if (!now) return;
    setBusy(true);
    setErr(null);
    try {
      const body = { start: now - hours * 3600, end: now, window_s: 1800 };
      const [a, d, h] = await Promise.all([
        api("/ml/fault-attribution", { method: "POST", json: body }),
        api("/ml/anomaly-detection", { method: "POST", json: { end: now, window_s: 1800 } }),
        api("/ml/sensor-health", { method: "POST", json: { end: now, window_s: 1800 } }),
      ]);
      setAtt(a);
      setDet(d);
      setHealth(h);
      const dist = Object.entries<number>(a.hypothesis_distribution).sort((x, y) => y[1] - x[1]).slice(0, 25);
      setTests(await api("/ml/next-best-test", { method: "POST", json: { hypotheses: dist.map((x) => x[0]), probabilities: dist.map((x) => x[1]) } }));
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader title="ML Diagnostics"
        subtitle="Model 1 anomaly detection · Model 2 instrument faults · Model 3 physics-guided Bayesian attribution · Model 4 next-best test"
        actions={
          <>
            <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
              {[2, 4, 6, 8].map((h) => <option key={h} value={h}>evidence: last {h} h</option>)}
            </select>
            <Button variant="primary" onClick={run} disabled={busy || !now}>{busy ? <Spinner label="Running models" /> : "Run diagnostics"}</Button>
          </>
        } />
      <ErrorNote error={err} />
      {!att && !busy && <Empty>Run diagnostics on the most recent evidence period.</Empty>}
      {att && (
        <div className="grid gap-4 xl:grid-cols-3">
          <Card title="Fault attribution (Model 3)" className="xl:col-span-2"
            subtitle={`χ² ${fmt(att.chi2, 1)} on ${att.dof} rows · calibration T=${fmt(att.model?.calibration_temperature, 2)}`}
            actions={<Badge tone={statusTone(att.status)}>{humanize(att.status)}</Badge>}>
            <div className="space-y-2">
              {att.hypotheses.map((h: any) => (
                <div key={h.hypothesis_id}>
                  <div className="flex justify-between text-sm">
                    <span>{h.label}</span>
                    <span className="num text-xs text-ink-3">{Object.entries(h.magnitude ?? {}).filter(([, v]) => v != null).slice(0, 3).map(([k, v]) => `${k}=${v}`).join(" · ")}</span>
                  </div>
                  <ProbBar p={h.calibrated_probability} tone={h.calibrated_probability > 0.8 ? "critical" : h.calibrated_probability > 0.4 ? "warning" : "info"} />
                </div>
              ))}
            </div>
            {att.multi_fault_combinations?.length > 0 && (
              <div className="mt-3 text-xs text-ink-3">Two-fault combinations: {att.multi_fault_combinations.slice(0, 3).map((c: any) =>
                `${c.hypotheses.join(" + ")} (${(c.posterior * 100).toFixed(1)}%)`).join("; ")}</div>
            )}
          </Card>
          <Card title="Residual decomposition" subtitle="R = L + B + U + ε (tonnes)">
            {att.decomposition_tonnes && Object.entries<number>(att.decomposition_tonnes).map(([k, v]) => (
              <div key={k} className="flex justify-between border-b border-line py-1 text-sm"><span className="text-ink-3">{humanize(k)}</span><span className="num">{signed(v, 3)}</span></div>
            ))}
            <div className="mt-3 text-xs">
              {Object.entries<number>(att.category_probabilities ?? {}).map(([k, v]) => (
                <div key={k}><span className="text-ink-2">{humanize(k)}</span><ProbBar p={v} tone="info" /></div>
              ))}
            </div>
          </Card>
          <Card title="Evidence supporting the leading hypothesis" className="xl:col-span-2">
            {(() => {
              const top = att.hypotheses[0]?.hypothesis_id;
              const rows = att.explanation?.hypotheses?.[top] ?? [];
              return rows.length ? (
                <table className="data">
                  <thead><tr><th>Row</th><th className="text-right">Observed</th><th className="text-right">Predicted by {top}</th><th className="text-right">σ</th><th className="text-right">z</th></tr></thead>
                  <tbody>{rows.map((r: any) => (
                    <tr key={r.row}><td className="num text-xs">{r.row}</td><td className="num text-right">{signed(r.observed, 3)}</td>
                      <td className="num text-right">{signed(r.predicted_by_hypothesis, 3)}</td><td className="num text-right">{fmt(r.sigma, 3)}</td>
                      <td className="num text-right">{signed(r.z, 1)}</td></tr>
                  ))}</tbody>
                </table>
              ) : <Empty>No supporting rows (normal operation).</Empty>;
            })()}
          </Card>
          <Card title="Anomaly detectors (Model 1)" subtitle="Latest 30-min window">
            {det && (
              <div className="space-y-1 text-sm">
                {Object.entries<number>(det.scores ?? {}).map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between border-b border-line py-1">
                    <span className="text-ink-2">{humanize(k)}</span>
                    <span className="flex items-center gap-2"><span className="num">{fmt(v, 3)}</span>
                      <Badge tone={det.alarms?.[k] ? "critical" : "normal"}>{det.alarms?.[k] ? "alarm" : "ok"}</Badge></span>
                  </div>
                ))}
                <div className="flex items-center justify-between py-1"><span className="text-ink-2">Conventional threshold (0.5%)</span>
                  <Badge tone={det.conventional_alarm ? "critical" : "normal"}>{det.conventional_alarm ? "alarm" : "ok"}</Badge></div>
              </div>
            )}
          </Card>
          <Card title="Instrument health (Model 2)" className="xl:col-span-2">
            {health?.length ? (
              <table className="data">
                <thead><tr><th>Instrument</th><th>Classification</th><th className="text-right">p(fault)</th><th className="text-right">coverage</th><th className="text-right">bias-direction z</th></tr></thead>
                <tbody>{health.slice(0, 12).map((h) => (
                  <tr key={h.sensor_id}><td className="num">{h.sensor_id}</td>
                    <td><Badge tone={h.predicted === "NORMAL" ? "normal" : "warning"}>{h.predicted}</Badge></td>
                    <td className="num text-right">{fmt(h.p_fault, 3)}</td><td className="num text-right">{fmt(h.q_coverage, 2)}</td>
                    <td className="num text-right">{signed(h.z_bias_dir, 2)}</td></tr>
                ))}</tbody>
              </table>
            ) : <Empty>—</Empty>}
          </Card>
          <Card title="Next-best verification (Model 4)" subtitle={tests ? `current entropy ${fmt(tests.current_entropy_bits, 2)} bits` : ""}>
            {tests?.ranked_actions?.slice(0, 6).map((a: any) => (
              <div key={a.action_id} className="border-b border-line py-1.5 text-xs">
                <div className="flex justify-between"><span className="num">{a.action_id}</span><span className="num">{fmt(a.expected_information_gain_bits, 3)} bits</span></div>
                <div className="text-ink-3">{a.rationale} · cost {a.cost}{a.available ? "" : " · unavailable"}</div>
              </div>
            ))}
          </Card>
        </div>
      )}
    </div>
  );
}
