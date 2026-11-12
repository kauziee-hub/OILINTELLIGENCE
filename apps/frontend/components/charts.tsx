"use client";

import {
  Area, Bar, BarChart, CartesianGrid, ComposedChart, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";
import { shortTime } from "@/lib/format";

/** Warm categorical palette (orange, ink, green, amber, slate blue, plum, grey, teal). */
export const SERIES = ["#d4572d", "#2b2a27", "#3e7b57", "#c98a1d", "#3f6c9e", "#8e6cb5", "#9a958c", "#4f9a92"];
const axis = { stroke: "#a39d93", fontSize: 11, tickLine: false, axisLine: false };
const grid = <CartesianGrid stroke="#e4ded4" strokeDasharray="2 4" vertical={false} />;
const tip = {
  contentStyle: { background: "rgba(251,250,247,0.96)", border: "1px solid #e5dfd5", borderRadius: 14, fontSize: 12,
    boxShadow: "0 10px 30px -12px rgba(60,40,20,.25)" },
  labelStyle: { color: "#8e897f" },
  cursor: { stroke: "#cfc7bb", strokeDasharray: "3 3" },
};
const legend = { wrapperStyle: { fontSize: 11.5, color: "#56524b" }, iconType: "circle" as const, iconSize: 7 };

export interface Series {
  name: string;
  points: [number, number | null][];
  unit?: string;
}

/** Multi-series time chart (epoch seconds on x). Series are merged on timestamps. */
export function TimeChart({ series, height = 240, yLabel }: { series: Series[]; height?: number; yLabel?: string }) {
  const map = new Map<number, Record<string, number | null>>();
  for (const s of series) {
    for (const [t, v] of s.points) {
      const k = Math.round(t);
      const row = map.get(k) ?? { t: k };
      row[s.name] = v;
      map.set(k, row);
    }
  }
  const data = [...map.values()].sort((a, b) => (a.t as number) - (b.t as number));
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 8, left: -6, bottom: 0 }}>
        {grid}
        <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={shortTime} {...axis} />
        <YAxis {...axis} width={54} label={yLabel ? { value: yLabel, angle: -90, position: "insideLeft", fill: "#a39d93",
          fontSize: 11 } : undefined} domain={["auto", "auto"]} />
        <Tooltip {...tip} labelFormatter={(v) => shortTime(Number(v))} />
        {series.length > 1 && <Legend {...legend} />}
        {series.map((s, i) => (
          <Line key={s.name} type="monotone" dataKey={s.name} stroke={SERIES[i % SERIES.length]} dot={false}
            strokeWidth={2} connectNulls isAnimationActive={false} />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}

/** Facility residual with the calibrated +/- 2 sigma band (dashed bounds) and abnormal-window markers. */
export function ResidualChart({ data, height = 260 }: {
  data: { t: number; residual: number; sigma?: number; anomaly?: boolean }[]; height?: number;
}) {
  const rows = data.map((d) => ({ ...d, band: d.sigma ? [-2 * d.sigma, 2 * d.sigma] : undefined,
    up: d.sigma ? 2 * d.sigma : undefined, lo: d.sigma ? -2 * d.sigma : undefined, alarm: d.anomaly ? d.residual : null }));
  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={rows} margin={{ top: 10, right: 8, left: -6, bottom: 0 }}>
        <defs>
          <linearGradient id="bandFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#d4572d" stopOpacity={0.10} />
            <stop offset="100%" stopColor="#d4572d" stopOpacity={0.03} />
          </linearGradient>
        </defs>
        {grid}
        <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} tickFormatter={shortTime} {...axis} />
        <YAxis {...axis} width={54} tickFormatter={(v) => `${v} t`} />
        <Tooltip {...tip} labelFormatter={(v) => shortTime(Number(v))}
          formatter={(v: unknown, n?: unknown) => [Array.isArray(v) ? `±${Number(v[1]).toFixed(2)} t` : `${Number(v).toFixed(3)} t`, String(n ?? "")]} />
        <Area dataKey="band" stroke="none" fill="url(#bandFill)" name="±2σ uncertainty" isAnimationActive={false} />
        <Line dataKey="up" stroke="#2b2a27" strokeDasharray="5 5" strokeWidth={1} dot={false} legendType="none" tooltipType="none" isAnimationActive={false} name="+2σ" />
        <Line dataKey="lo" stroke="#2b2a27" strokeDasharray="5 5" strokeWidth={1} dot={false} legendType="none" tooltipType="none" isAnimationActive={false} name="−2σ" />
        <ReferenceLine y={0} stroke="#cfc7bb" />
        <Line dataKey="residual" type="monotone" stroke="#d4572d" dot={false} strokeWidth={2.2} name="facility residual" isAnimationActive={false} />
        <Line dataKey="alarm" stroke="#c4432b" strokeWidth={0} dot={{ r: 3.5, fill: "#c4432b", stroke: "#fff", strokeWidth: 1.5 }}
          name="abnormal window" isAnimationActive={false} />
        <Legend {...legend} />
      </ComposedChart>
    </ResponsiveContainer>
  );
}

export function BarsChart({ data, keys, height = 220, xKey = "name", colors }: {
  data: Record<string, any>[]; keys: string[]; height?: number; xKey?: string; colors?: string[];
}) {
  const pal = colors ?? SERIES;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ top: 8, right: 8, left: -6, bottom: 0 }} barGap={3}>
        {grid}
        <XAxis dataKey={xKey} {...axis} interval={0} angle={data.length > 5 ? -20 : 0} textAnchor={data.length > 5 ? "end" : "middle"}
          height={data.length > 5 ? 54 : 30} />
        <YAxis {...axis} width={44} />
        <Tooltip {...tip} cursor={{ fill: "rgba(212,87,45,0.06)" }} />
        {keys.length > 1 && <Legend {...legend} />}
        {keys.map((k, i) => <Bar key={k} dataKey={k} fill={pal[i % pal.length]} radius={[6, 6, 2, 2]} isAnimationActive={false} />)}
      </BarChart>
    </ResponsiveContainer>
  );
}
