"use client";

import { Plus } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { Badge, Button, Card, Empty, ErrorNote, HighlightCard, PageHeader, Segmented, StackBar } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { api, hasRole } from "@/services/api";
import { epochToLocal, humanize, statusTone } from "@/lib/format";
import type { InvestigationSummary } from "@/services/types";

const CLOSED = ["CLOSED", "REJECTED", "SUPERSEDED", "DUPLICATE", "RUN_ENDED"];
const STATUS_COLOR: Record<string, string> = {
  AWAITING_VERIFICATION: "#d4572d", RESOLVED_PENDING_APPROVAL: "#e3b341", NEEDS_DATA: "#9a958c", AWAITING_REVIEW: "#3f6c9e",
  CLOSED: "#3e7b57", REJECTED: "#2b2a27", SUPERSEDED: "#cfc7bb", RUN_ENDED: "#cfc7bb",
};

export default function Investigations() {
  const [view, setView] = useState<"open" | "closed" | "all">("open");
  const { data, reload } = useApi<InvestigationSummary[]>("/investigations?limit=200", 6000);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const canRun = hasRole("engineer");

  const all = useMemo(() => data ?? [], [data]);
  const shown = all.filter((i) => view === "all" || (view === "open" ? !CLOSED.includes(i.status) : CLOSED.includes(i.status)));
  const mix = useMemo(() => {
    const c: Record<string, number> = {};
    for (const i of all) c[i.status] = (c[i.status] ?? 0) + 1;
    return Object.entries(c).sort((a, b) => b[1] - a[1]).map(([k, v]) => ({ label: humanize(k), value: v, color: STATUS_COLOR[k] ?? "#8e897f" }));
  }, [all]);

  async function manual() {
    setErr(null);
    try {
      const r = await api("/investigations", { method: "POST", json: { assets: [], summary: "Manual facility review", run: true } });
      setMsg(`Investigation queued (${r.event_id}).`);
      setTimeout(reload, 4000);
    } catch (e: any) {
      setErr(e.message);
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader title="AI Investigation Center"
        subtitle="Evidence-seeking investigations orchestrated by the LangGraph agent. The physics-guided Bayesian model owns the numbers; engineers own the decision."
        actions={<>
          <Segmented value={view} onChange={setView} size="md"
            options={[{ value: "open", label: "Open" }, { value: "closed", label: "Closed" }, { value: "all", label: "All" }]} />
          {canRun && <Button variant="primary" onClick={manual}><Plus size={14} /> Manual investigation</Button>}
        </>} />
      {msg && <div className="text-[13px] text-ok">{msg}</div>}
      <ErrorNote error={err} />

      <Card title="Case mix" subtitle={`${all.length} investigations · ${all.filter((i) => !CLOSED.includes(i.status)).length} open`}>
        {mix.length ? <StackBar parts={mix} /> : <Empty>No investigations yet.</Empty>}
      </Card>

      {view !== "closed" && shown.length > 0 && (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {shown.slice(0, 8).map((i) => {
            const [fam, target] = (i.top_hypothesis ?? "").split(":");
            return (
              <HighlightCard key={i.investigation_id} href={`/investigations/${i.investigation_id}`}
                variant={(i.status === "AWAITING_VERIFICATION" && (i.top_probability ?? 0) >= 0.6) || i.status === "RESOLVED_PENDING_APPROVAL"
                  ? "orange" : CLOSED.includes(i.status) || i.status === "NEEDS_DATA" ? "light" : "dark"}
                caption={`${i.investigation_id} · ${epochToLocal(i.t1).slice(5)}`}
                title={fam ? `${humanize(fam)}${target ? ` · ${target}` : ""}` : "Collecting evidence"}
                subtitle={(i.assets ?? []).join(", ") || "facility"}
                value={i.top_probability != null ? `${Math.round(i.top_probability * 100)}%` : "—"}
                foot={humanize(i.status)} chips={i.assets} />
            );
          })}
        </div>
      )}

      <Card title={view === "open" ? "All open cases" : view === "closed" ? "Closed cases" : "All cases"}>
        {shown.length ? (
          <table className="data">
            <thead>
              <tr><th>ID</th><th>Status</th><th>Most probable cause</th><th className="text-right">p</th><th>Assets</th><th>Analysis period</th><th>Updated</th></tr>
            </thead>
            <tbody>
              {shown.map((i) => (
                <tr key={i.investigation_id}>
                  <td><Link className="mono font-medium text-accent" href={`/investigations/${i.investigation_id}`}>{i.investigation_id}</Link></td>
                  <td><Badge tone={statusTone(i.status)}>{humanize(i.status).toLowerCase()}</Badge></td>
                  <td className="text-[12.5px]">{i.top_hypothesis ?? "—"}</td>
                  <td className="num text-right text-[12.5px]">{i.top_probability != null ? (i.top_probability * 100).toFixed(1) + "%" : "—"}</td>
                  <td className="text-[12px] text-ink-2">{i.assets.join(", ") || "facility"}</td>
                  <td className="num text-[12px] text-ink-3">{epochToLocal(i.t0)} → {epochToLocal(i.t1).slice(11)}</td>
                  <td className="num text-[12px] text-ink-3">{i.updated_at?.slice(0, 19).replace("T", " ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <Empty>No {view === "all" ? "" : view} investigations. They open automatically when the balance becomes abnormal.</Empty>}
      </Card>
    </div>
  );
}
