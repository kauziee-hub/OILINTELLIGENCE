"use client";

import { useMemo, useState } from "react";
import { BarsChart, ResidualChart } from "@/components/charts";
import { Badge, Button, Card, Empty, ErrorNote, PageHeader, Spinner } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useLive } from "@/hooks/useLive";
import { fmt, signed, zSeverity } from "@/lib/format";
import { api } from "@/services/api";

export default function LossAnalytics() {
  const now = useLive((s) => s.state?.timestamp ?? null);
  const { data: resid } = useApi<any[]>("/physics/residuals?limit=500", 20000);
  const [hours, setHours] = useState(4);
  const [rec, setRec] = useState<any>(null);
  const [att, setAtt] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const chart = useMemo(() => (resid ?? []).map((r) => ({
    t: r.t1, residual: r.facility_residual_t, sigma: r.rows?.facility_sigma_t, anomaly: r.anomaly })), [resid]);
  const cum = useMemo(() => {
    // non-overlapping sum of consecutive 30-min windows (approximate cumulative apparent loss)
    let last = -Infinity;
    let acc = 0;
    const out: { name: string; cumulative: number }[] = [];
    for (const r of resid ?? []) {
      if (r.t1 - last >= r.window_s - 1) {
        acc += r.facility_residual_t;
        last = r.t1;
        out.push({ name: new Date(r.t1 * 1000).toISOString().slice(11, 16), cumulative: Number(acc.toFixed(3)) });
      }
    }
    return out;
  }, [resid]);

  async function run() {
    if (!now) return;
    setBusy(true);
    setErr(null);
    try {
      const body = { start: now - hours * 3600, end: now, window_s: 1800 };
      const [r, a] = await Promise.all([api("/physics/reconcile", { method: "POST", json: body }),
        api("/ml/fault-attribution", { method: "POST", json: body })]);
      setRec(r);
      setAtt(a);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  const d = att?.decomposition_tonnes;
  return (
    <div className="space-y-4">
      <PageHeader title="Loss Analytics"
        subtitle="Physical vs. apparent loss: topology-aware reconciliation, uncertainty and residual decomposition R = L + B + U + ε"
        actions={
          <>
            <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
              {[2, 4, 8, 12, 24].map((h) => <option key={h} value={h}>last {h} h</option>)}
            </select>
            <Button variant="primary" onClick={run} disabled={busy || !now}>{busy ? <Spinner label="Reconciling" /> : "Reconcile period"}</Button>
          </>
        } />
      <ErrorNote error={err} />
      <div className="grid gap-4 xl:grid-cols-3">
        <Card title="Facility residual history" subtitle="Rolling windows, ±2σ band, abnormal windows in red" className="xl:col-span-2">
          {chart.length ? <ResidualChart data={chart} height={280} /> : <Empty>No balance history yet.</Empty>}
        </Card>
        <Card title="Cumulative apparent loss" subtitle="Sum of consecutive window residuals (not yet attributed)">
          {cum.length ? <BarsChart data={cum.slice(-24)} keys={["cumulative"]} height={280} /> : <Empty>—</Empty>}
        </Card>
      </div>
      {rec && (
        <div className="grid gap-4 xl:grid-cols-3">
          <Card title="Period balance" subtitle={`${rec.windows} windows · sign: ${rec.sign_convention}`}>
            <div className="space-y-1 text-sm">
              <div className="flex justify-between"><span className="text-ink-3">Facility residual</span><span className="num">{signed(rec.facility_residual_t, 3)} t</span></div>
              <div className="flex justify-between"><span className="text-ink-3">1σ uncertainty</span><span className="num">{fmt(rec.facility_residual_sigma_t, 3)} t</span></div>
              <div className="flex justify-between"><span className="text-ink-3">z</span><span className="num">{fmt(rec.facility_z, 2)}</span></div>
              <div className="flex justify-between"><span className="text-ink-3">χ² / rows</span><span className="num">{fmt(rec.chi2, 1)} / {rec.dof}</span></div>
            </div>
            {d && (
              <div className="mt-4">
                <div className="mb-1 text-xs uppercase text-ink-3">Decomposition (posterior-weighted)</div>
                <BarsChart height={200} keys={["tonnes"]} data={[
                  { name: "L physical", tonnes: d.L_physical_loss }, { name: "B measurement", tonnes: d.B_measurement_bias },
                  { name: "U unrecorded", tonnes: d.U_unrecorded_movement }, { name: "ε residual", tonnes: d.epsilon }]} />
                <div className="mt-2 text-xs text-ink-2">
                  Estimated physical loss:{" "}
                  <span className="num text-ink">{att.estimated_physical_loss_tonnes != null ? `${fmt(att.estimated_physical_loss_tonnes, 2)} t` : "not supported by current evidence"}</span>
                </div>
              </div>
            )}
          </Card>
          <Card title="Most abnormal balance rows" className="xl:col-span-2">
            <table className="data">
              <thead><tr><th>Row</th><th className="text-right">Residual</th><th className="text-right">σ</th><th className="text-right">z</th><th>Dominant uncertainty</th></tr></thead>
              <tbody>
                {rec.rows_most_abnormal.map((r: any) => (
                  <tr key={r.row}>
                    <td className="num text-xs">{r.row}</td>
                    <td className="num text-right">{signed(r.value, 3)} {r.unit}</td>
                    <td className="num text-right">{fmt(r.sigma, 3)}</td>
                    <td className="text-right"><Badge tone={zSeverity(r.z)}>{signed(r.z, 1)}</Badge></td>
                    <td className="num text-xs text-ink-3">{(r.top_uncertainty_sources ?? []).join(", ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </div>
      )}
    </div>
  );
}
