"use client";

import { useState } from "react";
import { Badge, Button, Card, Empty, ErrorNote, JsonView, PageHeader } from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { api, hasRole } from "@/services/api";

export default function SettingsPage() {
  const admin = hasRole("admin");
  const { data: conns, reload } = useApi<any[]>("/integrations", 10000);
  const { data: health } = useApi<any>("/health", 5000);
  const { data: users, reload: reloadUsers } = useApi<any[]>(admin ? "/users" : null);
  const { data: audit } = useApi<any[]>(admin ? "/audit?limit=60" : null, 10000);
  const [result, setResult] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [opcUrl, setOpcUrl] = useState("opc.tcp://127.0.0.1:4840/oilintelligence/");
  const [nu, setNu] = useState({ username: "", full_name: "", role: "viewer", password: "" });

  async function test(path: string, body: any) {
    setErr(null);
    setResult(null);
    try {
      setResult(await api(path, { method: "POST", json: body }));
      reload();
    } catch (e: any) {
      setErr(e.message);
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Settings" subtitle="Connectors (read-only), users & permissions, platform health and audit trail" />
      <ErrorNote error={err} />
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Industrial connectors" subtitle="All plant connectors are read-only by design — there is no write path to PLCs, pumps or valves">
          <table className="data">
            <thead><tr><th>Connector</th><th>Type</th><th>State</th><th /></tr></thead>
            <tbody>{(conns ?? []).map((c) => (
              <tr key={c.connector_id}>
                <td><div className="text-sm">{c.name}</div><div className="num text-xs text-ink-3">{c.connector_id}</div></td>
                <td className="text-xs">{c.type}{c.read_only && <> · <Badge tone="info">read-only</Badge></>}</td>
                <td className="text-xs">{c.live ? <Badge tone={c.live.connected || c.live.running ? "normal" : "offline"}>{c.live.connected || c.live.running ? "connected" : "down"}</Badge>
                  : c.last_status?.ok === true ? <Badge tone="normal">last test ok</Badge> : c.last_status?.ok === false ? <Badge tone="critical">last test failed</Badge> : <Badge tone="offline">{c.enabled ? "enabled" : "disabled"}</Badge>}</td>
                <td />
              </tr>
            ))}</tbody>
          </table>
          {admin && (
            <div className="mt-3 space-y-2 border-t border-line pt-3 text-sm">
              <div className="flex gap-2">
                <input className="flex-1" value={opcUrl} onChange={(e) => setOpcUrl(e.target.value)} />
                <Button onClick={() => test("/integrations/opcua/test", { url: opcUrl })}>Test OPC UA</Button>
              </div>
              <div className="flex gap-2">
                <Button onClick={() => test("/integrations/mqtt/test", { host: "localhost", port: 1883 })}>Test MQTT (localhost:1883)</Button>
                <Button onClick={() => test("/integrations/historian/test", { kind: "file", path: "datasets/historian/export.csv" })}>Test historian file</Button>
              </div>
              {result && <JsonView value={result} />}
            </div>
          )}
        </Card>
        <Card title="Platform health">
          {health ? <JsonView value={health} /> : <Empty>—</Empty>}
        </Card>
        {admin && (
          <Card title="Users & roles" subtitle="viewer < operator (simulation control) < engineer (investigations) < admin">
            <table className="data"><tbody>{(users ?? []).map((u) => (
              <tr key={u.username}><td className="num">{u.username}</td><td className="text-xs">{u.full_name}</td><td><Badge tone="info">{u.role}</Badge></td></tr>
            ))}</tbody></table>
            <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
              <input placeholder="username" value={nu.username} onChange={(e) => setNu({ ...nu, username: e.target.value })} />
              <input placeholder="full name" value={nu.full_name} onChange={(e) => setNu({ ...nu, full_name: e.target.value })} />
              <select value={nu.role} onChange={(e) => setNu({ ...nu, role: e.target.value })}>
                {["viewer", "operator", "engineer", "admin"].map((r) => <option key={r}>{r}</option>)}
              </select>
              <input type="password" placeholder="password (min 10)" value={nu.password} onChange={(e) => setNu({ ...nu, password: e.target.value })} />
              <Button variant="primary" onClick={async () => {
                try {
                  await api("/users", { method: "POST", json: nu });
                  setNu({ username: "", full_name: "", role: "viewer", password: "" });
                  reloadUsers();
                } catch (e: any) {
                  setErr(e.message);
                }
              }}>Create user</Button>
            </div>
          </Card>
        )}
        {admin && (
          <Card title="Audit trail" subtitle="Every state-changing request and every agent tool call">
            <div className="max-h-96 overflow-y-auto">
              <table className="data"><tbody>{(audit ?? []).map((a, i) => (
                <tr key={i}><td className="num text-xs text-ink-3">{String(a.timestamp).slice(11, 19)}</td><td className="num text-xs">{a.actor}</td>
                  <td className="num text-xs">{a.action}</td><td className="num text-xs">{a.status_code ?? a.details?.status ?? ""}</td></tr>
              ))}</tbody></table>
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}
