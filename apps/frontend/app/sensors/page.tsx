"use client";

import { useMemo, useState } from "react";
import { TimeChart } from "@/components/charts";
import { Badge, Card, Empty, PageHeader } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useLive } from "@/hooks/useLive";
import { fmt } from "@/lib/format";

export default function Sensors() {
  const state = useLive((s) => s.state);
  const [sel, setSel] = useState<string[]>(["LT-001", "LT-002", "LT-003"]);
  const [hours, setHours] = useState(4);
  const ts = state?.timestamp ?? null;
  const bucket = ts ? Math.floor(ts / 300) : 0;
  const qs = sel.map((s) => `sensor_id=${s}`).join("&");
  const { data } = useApi<any>(sel.length && ts ? `/telemetry/history?${qs}&start=${ts - hours * 3600}&end=${ts}&max_points=400&b=${bucket}` : null);
  const { data: q } = useApi<any>("/telemetry/quality", 15000);
  const series = useMemo(() => data ? Object.entries<any>(data.series).map(([k, v]) => ({
    name: `${k} (${v.unit})`, points: v.points.map((p: any) => [p[0], p[1]] as [number, number | null]) })) : [], [data]);
  const groups = useMemo(() => {
    const g: Record<string, string[]> = {};
    Object.entries(state?.sensors ?? {}).forEach(([id, s]) => (g[s.type] ??= []).push(id));
    return g;
  }, [state?.sensors]);
  const qmap = Object.fromEntries((q?.sensors ?? []).map((x: any) => [x.sensor_id, x]));
  const units = new Set(sel.map((s) => state?.sensors[s]?.unit));

  return (
    <div className="space-y-4">
      <PageHeader title="Sensor Explorer" subtitle="Live and historical readings with data-quality metadata (raw telemetry is never overwritten)"
        actions={<select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
          {[1, 2, 4, 8, 12, 24].map((h) => <option key={h} value={h}>last {h} h</option>)}</select>} />
      <div className="grid gap-4 xl:grid-cols-[320px_1fr]">
        <Card title="Instruments">
          <div className="max-h-[70vh] space-y-3 overflow-y-auto">
            {Object.entries(groups).map(([type, ids]) => (
              <div key={type}>
                <div className="mb-1 text-xs uppercase text-ink-3">{type.replace("_", " ")}</div>
                {ids.map((id) => {
                  const s = state!.sensors[id];
                  const qq = qmap[id];
                  return (
                    <label key={id} className="flex items-center justify-between gap-2 py-0.5 text-sm">
                      <span className="flex items-center gap-2">
                        <input type="checkbox" checked={sel.includes(id)}
                          onChange={(e) => setSel((p) => e.target.checked ? [...p, id] : p.filter((x) => x !== id))} />
                        <span className="num">{id}</span>
                      </span>
                      <span className="flex items-center gap-1">
                        <span className="num text-xs text-ink-2">{fmt(s.value, 2)} {s.unit}</span>
                        {qq?.flatlined ? <Badge tone="warning">flat</Badge> : s.quality !== "GOOD" ? <Badge tone="offline">{s.quality}</Badge> : null}
                      </span>
                    </label>
                  );
                })}
              </div>
            ))}
          </div>
        </Card>
        <div className="space-y-4">
          <Card title="History" subtitle={units.size > 1 ? "Mixed units selected — compare shapes, not magnitudes" : undefined}>
            {series.length ? <TimeChart series={series} height={360} /> : <Empty>Select instruments.</Empty>}
          </Card>
          <Card title="Data quality (last 30 min)" subtitle={q ? `overall completeness ${(q.overall_completeness * 100).toFixed(1)}%` : ""}>
            <table className="data">
              <thead><tr><th>Instrument</th><th className="text-right">Samples</th><th className="text-right">Completeness</th><th className="text-right">Bad</th><th>State</th></tr></thead>
              <tbody>{(q?.sensors ?? []).filter((x: any) => sel.includes(x.sensor_id) || x.flatlined || x.completeness < 0.9).slice(0, 20).map((x: any) => (
                <tr key={x.sensor_id}><td className="num">{x.sensor_id}</td><td className="num text-right">{x.samples}</td>
                  <td className="num text-right">{(x.completeness * 100).toFixed(0)}%</td><td className="num text-right">{(x.bad_fraction * 100).toFixed(0)}%</td>
                  <td>{x.flatlined ? <Badge tone="warning">flat-lined</Badge> : x.completeness < 0.9 ? <Badge tone="warning">gaps</Badge> : <Badge tone="normal">ok</Badge>}</td></tr>
              ))}</tbody>
            </table>
          </Card>
        </div>
      </div>
    </div>
  );
}
