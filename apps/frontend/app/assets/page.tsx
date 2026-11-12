"use client";

import { useState } from "react";
import { Badge, Card, Empty, JsonView, PageHeader } from "@/components/ui";
import { useApi } from "@/hooks/useApi";

export default function Assets() {
  const { data: assets } = useApi<any[]>("/assets");
  const { data: topo } = useApi<any>("/topology");
  const [sel, setSel] = useState<string | null>("TK-001");
  const { data: detail } = useApi<any>(sel ? `/assets/${sel}` : null);
  const { data: sensors } = useApi<any[]>(sel ? `/assets/${sel}/sensors` : null, 5000);
  const types = [...new Set((assets ?? []).map((a) => a.asset_type))];
  return (
    <div className="space-y-4">
      <PageHeader title="Asset Explorer" subtitle="Equipment registry, configuration and process topology (from the frozen process design)" />
      <div className="grid gap-4 xl:grid-cols-[300px_1fr]">
        <Card title="Assets">
          {types.map((t) => (
            <div key={t} className="mb-2">
              <div className="text-xs uppercase text-ink-3">{t}</div>
              {(assets ?? []).filter((a) => a.asset_type === t).map((a) => (
                <button key={a.asset_id} onClick={() => setSel(a.asset_id)}
                  className={`block w-full rounded px-2 py-1 text-left text-sm ${sel === a.asset_id ? "bg-info/15" : "hover:bg-panel-2"}`}>
                  <span className="num">{a.asset_id}</span> <span className="text-ink-3">{a.asset_name}</span>
                </button>
              ))}
            </div>
          ))}
        </Card>
        <div className="space-y-4">
          {detail ? (
            <Card title={`${detail.asset_id} — ${detail.asset_name}`} actions={<Badge tone="normal">{detail.operating_status}</Badge>}>
              <div className="grid gap-4 lg:grid-cols-2">
                <div>
                  <div className="mb-1 text-xs uppercase text-ink-3">Configuration</div>
                  <JsonView value={detail.configuration} />
                </div>
                <div className="space-y-3">
                  <div>
                    <div className="mb-1 text-xs uppercase text-ink-3">Instruments</div>
                    <table className="data"><tbody>{(sensors ?? []).map((s) => (
                      <tr key={s.sensor_id}><td className="num">{s.sensor_id}</td><td className="text-xs">{s.parameter}</td>
                        <td className="num text-right text-xs">{s.latest?.value?.toFixed?.(3) ?? "—"} {s.unit}</td></tr>
                    ))}</tbody></table>
                  </div>
                  <div>
                    <div className="mb-1 text-xs uppercase text-ink-3">Topology relationships</div>
                    <ul className="space-y-0.5 text-xs">{detail.relationships.map((r: any, i: number) => (
                      <li key={i} className="num text-ink-2">{r.from} —{r.type}→ {r.to}</li>
                    ))}</ul>
                  </div>
                </div>
              </div>
            </Card>
          ) : <Empty>Select an asset.</Empty>}
          {topo && (
            <Card title="Valid transfer routes and measurement configuration">
              <table className="data">
                <thead><tr><th>Route</th><th>Path</th><th>Open valves</th><th>Meters</th><th>Custody meter</th><th>Setpoint range</th></tr></thead>
                <tbody>{Object.entries<any>(topo.routes).map(([k, r]) => (
                  <tr key={k}><td className="num">{k}</td><td className="text-xs">{r.source} → {r.pipelines.join(" → ")} → {r.destination}</td>
                    <td className="num text-xs">{r.open_valves.join(", ")}</td><td className="num text-xs">{r.meters.join(", ")}</td>
                    <td className="num text-xs">{r.custody_meter}</td><td className="num text-xs">{r.setpoint_range_m3h.join("–")} m³/h</td></tr>
                ))}</tbody>
              </table>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
