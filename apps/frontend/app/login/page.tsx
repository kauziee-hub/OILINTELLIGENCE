"use client";

import { ShieldCheck } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Button, ErrorNote } from "@/components/ui";
import { ensureSession, login } from "@/services/api";

const POINTS = [
  ["Physics first", "Topology-aware mass balance with propagated measurement uncertainty"],
  ["Explainable AI", "Bayesian attribution over 53 fault hypotheses + two-fault pairs, calibrated and auditable"],
  ["Adaptive", "Drift-aware self-calibration and a digital twin that replays every hypothesis"],
];

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // open-access deployment (no sign-in): go straight to the app
  useEffect(() => {
    ensureSession().then((s) => { if (s) router.replace("/dashboard"); }).catch(() => undefined);
  }, [router]);
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="grid w-full max-w-5xl overflow-hidden rounded-[30px] card-shadow ring-1 ring-white/70 md:grid-cols-[1.1fr_1fr]">
        <div className="relative hidden flex-col justify-between bg-[linear-gradient(160deg,#2f2d2a,#1b1a18)] p-9 text-white md:flex">
          <div className="absolute -right-24 -top-24 size-72 rounded-full bg-accent/30 blur-3xl" aria-hidden />
          <div className="relative flex items-center gap-2.5">
            <span className="relative inline-flex size-10 items-center justify-center rounded-[13px] bg-white text-[16px] font-semibold text-dark">
              O<span className="absolute right-1.5 top-1.5 size-1.5 rounded-full bg-accent" />
            </span>
            <span className="text-[19px] font-semibold tracking-[-0.03em]">oilintelligence</span>
          </div>
          <div className="relative">
            <h1 className="display text-[42px] font-medium">Find every tonne.<br /><span className="text-accent-2">Explain every loss.</span></h1>
            <p className="mt-3 max-w-md text-[13.5px] text-white/65">
              Agentic, physics-guided hydrocarbon loss attribution for crude storage and transfer — from residual to verified root cause.
            </p>
          </div>
          <ul className="relative grid gap-3">
            {POINTS.map(([t, d]) => (
              <li key={t} className="rounded-2xl bg-white/[0.06] px-4 py-3 ring-1 ring-white/10">
                <div className="text-[13px] font-semibold">{t}</div>
                <div className="text-[12px] text-white/60">{d}</div>
              </li>
            ))}
          </ul>
        </div>
        <form
          className="glass flex flex-col justify-center gap-4 p-8 md:p-10"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError(null);
            try {
              await login(username, password);
              router.push("/dashboard");
            } catch (err: any) {
              setError(err.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <div>
            <div className="text-[12px] text-ink-3">Welcome back</div>
            <h2 className="display text-[30px] font-medium">Sign in</h2>
          </div>
          <label className="block text-[13px]">
            <span className="text-ink-2">Username</span>
            <input className="mt-1.5 w-full" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
          </label>
          <label className="block text-[13px]">
            <span className="text-ink-2">Password</span>
            <input className="mt-1.5 w-full" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password" />
          </label>
          <ErrorNote error={error} />
          <Button type="submit" variant="primary" disabled={busy || !username || !password} className="w-full justify-center py-2.5">
            {busy ? "Signing in…" : "Sign in"}
          </Button>
          <p className="flex items-start gap-2 text-[11.5px] text-ink-3">
            <ShieldCheck size={14} className="mt-0.5 shrink-0" />
            Read-only analytics: this platform cannot command plant equipment. Accounts are seeded on first start
            (data/initial_credentials.txt or the SEED_* variables in .env).
          </p>
        </form>
      </div>
    </div>
  );
}
