"use client";

import Link from "next/link";
import { Badge, Button, Card, Empty, Kpi, PageHeader } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { API_URL, getSession } from "@/services/api";
import { fmt, statusTone } from "@/lib/format";
import type { InvestigationSummary } from "@/services/types";

async function download(id: string, format: string) {
  const res = await fetch(`${API_URL}/api/v1/investigations/${id}/report?format=${format}`,
    { headers: { Authorization: `Bearer ${getSession()?.token}` } });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(await res.blob());
  a.download = `${id}.${format === "markdown" ? "md" : format}`;
  a.click();
}

export default function Reports() {
  const { data: k } = useApi<any>("/reports/kpis", 15000);
  const { data: invs } = useApi<InvestigationSummary[]>("/investigations?limit=200", 15000);
  return (
    <div className="space-y-4">
      <PageHeader title="Reports" subtitle="Engineering investigation reports, KPIs and exports" />
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <Kpi label="Anomaly events" value={k?.anomaly_events ?? "—"} />
        <Kpi label="Investigations" value={Object.values<number>(k?.investigations_by_status ?? {}).reduce((a, b) => a + b, 0)} />
        <Kpi label="Engineer reviews" value={k?.engineer_reviews ?? "—"} />
        <Kpi label="Mean preparation time" value={k?.mean_investigation_preparation_s != null ? fmt(k.mean_investigation_preparation_s / 60, 1) : "—"} unit="min" />
        <Kpi label="Transferred" value={fmt(k?.transferred_t, 0)} unit="t" sub={`${k?.transfers_completed ?? 0} completed transfers`} />
      </div>
      <Card title="Investigation reports">
        {invs?.length ? (
          <table className="data">
            <thead><tr><th>Investigation</th><th>Status</th><th>Cause</th><th>Period end</th><th>Export</th></tr></thead>
            <tbody>{invs.map((i) => (
              <tr key={i.investigation_id}>
                <td><Link className="num text-info" href={`/investigations/${i.investigation_id}`}>{i.investigation_id}</Link></td>
                <td><Badge tone={statusTone(i.status)}>{i.status.replace(/_/g, " ").toLowerCase()}</Badge></td>
                <td className="text-xs">{i.top_hypothesis ?? "—"}</td>
                <td className="num text-xs">{new Date(i.t1 * 1000).toISOString().slice(0, 16).replace("T", " ")}</td>
                <td className="flex gap-1">
                  <Button variant="ghost" onClick={() => download(i.investigation_id, "html")}>HTML</Button>
                  <Button variant="ghost" onClick={() => download(i.investigation_id, "markdown")}>MD</Button>
                  <Button variant="ghost" onClick={() => download(i.investigation_id, "json")}>JSON</Button>
                </td>
              </tr>
            ))}</tbody>
          </table>
        ) : <Empty>No reports yet.</Empty>}
      </Card>
    </div>
  );
}
