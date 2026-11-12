export function fmt(v: number | null | undefined, digits = 2, unit = ""): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const s = Math.abs(v) >= 10000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : v.toFixed(digits);
  return unit ? `${s} ${unit}` : s;
}

export function pct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

export function signed(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}`;
}

export function epochToLocal(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toISOString().replace("T", " ").slice(0, 16) + " UTC";
}

export function shortTime(ts: number): string {
  return new Date(ts * 1000).toISOString().slice(11, 16);
}

/** Map a z-score to a severity level (thresholds of the alarm logic). */
export function zSeverity(z: number | null | undefined): "normal" | "warning" | "critical" {
  const a = Math.abs(z ?? 0);
  if (a >= 4) return "critical";
  if (a >= 2.5) return "warning";
  return "normal";
}

export function humanize(id: string): string {
  return id.replace(/_/g, " ").toLowerCase().replace(/(^|\s)\S/g, (c) => c.toUpperCase());
}

export function statusTone(status: string): "normal" | "warning" | "critical" | "offline" | "info" {
  const s = status.toUpperCase();
  if (["CLOSED", "RUNNING", "GOOD", "OPEN_OK", "NORMAL", "NO_ABNORMALITY_CONFIRMED", "COMPLETED"].includes(s))
    return "normal";
  if (["CRITICAL", "BAD", "REJECTED", "UNKNOWN_CAUSE", "ERROR", "FAILED"].includes(s)) return "critical";
  if (["STOPPED", "STALE", "UNKNOWN", "SUPERSEDED", "OFFLINE", "FINISHED", "RUN_ENDED", "INTERRUPTED"].includes(s)) return "offline";
  if (["SIMULATION", "INFO", "PAUSED", "RECEIVED", "QUEUED"].includes(s)) return "info";
  return "warning";
}
