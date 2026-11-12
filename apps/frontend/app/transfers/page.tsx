"use client";

import { Badge, Card, Empty, PageHeader } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { fmt, statusTone } from "@/lib/format";

export default function Transfers() {
  const { data } = useApi<any[]>("/transfers?limit=200", 10000);
  const { data: log } = useApi<any[]>("/records/operator-log?limit=60", 10000);
  return (
    <div className="space-y-4">
      <PageHeader title="Transfer Management" subtitle="Transfer tickets (custody-meter net mass via flow-computer method) and operator log" />
      <Card title="Transfers">
        {data?.length ? (
          <table className="data">
            <thead><tr><th>Transfer</th><th>Route</th><th>From → To</th><th>Start</th><th>End</th><th className="text-right">Ticket (net t)</th><th className="text-right">Gross m³</th><th>Custody meter</th><th>Gauges (open → close)</th><th>Status</th></tr></thead>
            <tbody>{data.map((t) => (
              <tr key={t.transfer_id}>
                <td className="num">{t.transfer_id}</td><td className="num">{t.route}</td>
                <td className="text-xs">{t.source_tank} → {t.destination_tank}</td>
                <td className="num text-xs">{t.start_time?.slice(0, 16).replace("T", " ")}</td>
                <td className="num text-xs">{t.end_time?.slice(0, 16).replace("T", " ") ?? "—"}</td>
                <td className="num text-right">{fmt(t.recorded_quantity, 3)}</td>
                <td className="num text-right">{fmt(t.recorded_volume_m3, 2)}</td>
                <td className="num text-xs">{t.custody_meter}</td>
                <td className="num text-xs text-ink-3">{Object.entries(t.opening_gauges ?? {}).map(([k, v]) => `${k} ${v}`).join(", ")}
                  {t.closing_gauges && Object.keys(t.closing_gauges).length ? ` → ${Object.entries(t.closing_gauges).map(([k, v]) => `${k} ${v}`).join(", ")}` : ""}</td>
                <td><Badge tone={statusTone(t.status === "IN_PROGRESS" ? "INFO" : t.status)}>{t.status}</Badge></td>
              </tr>
            ))}</tbody>
          </table>
        ) : <Empty>No transfers recorded yet.</Empty>}
      </Card>
      <Card title="Operator log">
        {log?.length ? (
          <ul className="space-y-1 text-sm">{log.map((l, i) => (
            <li key={i}><span className="num text-xs text-ink-3">{String(l.timestamp).slice(0, 16).replace("T", " ")}</span>{" "}
              <span className="num text-xs text-ink-2">{l.asset_id}</span> {l.text}</li>
          ))}</ul>
        ) : <Empty>No entries.</Empty>}
      </Card>
    </div>
  );
}
