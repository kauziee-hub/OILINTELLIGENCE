"use client";

import clsx from "clsx";
import {
  Bell, BrainCircuit, Boxes, Clapperboard, Cpu, FileText, FlaskConical, Gauge, LayoutDashboard, LogOut, Microscope, Search,
  Settings, Share2, TrendingDown, Truck, Workflow,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { connectLive, useLive } from "@/hooks/useLive";
import { ensureSession, getSession, setSession, type Session } from "@/services/api";

const TABS = [
  { href: "/dashboard", label: "Overview", icon: LayoutDashboard },
  { href: "/digital-twin", label: "Digital Twin", icon: Boxes },
  { href: "/investigations", label: "Investigations", icon: Workflow },
  { href: "/loss-analytics", label: "Loss Analytics", icon: TrendingDown },
  { href: "/diagnostics", label: "Diagnostics", icon: Cpu },
  { href: "/adaptive", label: "Adaptive AI", icon: BrainCircuit },
  { href: "/simulation-lab", label: "Simulation Lab", icon: FlaskConical },
  { href: "/demo", label: "Demo", icon: Clapperboard },
];
const DOCK = [
  { href: "/assets", label: "Asset Explorer", icon: Share2 },
  { href: "/sensors", label: "Sensor Explorer", icon: Gauge },
  { href: "/transfers", label: "Transfers", icon: Truck },
  { href: "/reports", label: "Reports", icon: FileText },
  { href: "/model-lab", label: "Model Lab", icon: Microscope },
  { href: "/settings", label: "Settings", icon: Settings },
];

function Logo() {
  return (
    <Link href="/dashboard" className="flex shrink-0 items-center gap-2.5">
      <span className="relative inline-flex size-9 items-center justify-center rounded-[12px] bg-dark text-[15px] font-semibold text-white shadow-sm">
        O<span className="absolute right-1.5 top-1.5 size-1.5 rounded-full bg-accent" />
      </span>
      <span className="hidden text-[17px] font-semibold tracking-[-0.03em] text-ink lg:block">oilintelligence</span>
    </Link>
  );
}

function Tabs({ pathname }: { pathname: string | null }) {
  return (
    <nav className="glass flex min-w-0 items-center gap-0.5 overflow-x-auto rounded-full p-1 ring-1 ring-white/70 card-shadow">
      {[...TABS, ...DOCK.map((d) => ({ ...d, mobileOnly: true }))].map((t) => {
        const active = pathname?.startsWith(t.href);
        return (
          <Link key={t.href} href={t.href}
            className={clsx("shrink-0 whitespace-nowrap rounded-full px-3.5 py-1.5 text-[13px] font-medium transition-colors",
              "mobileOnly" in t && "md:hidden",
              active ? "bg-dark text-white shadow-sm" : "text-ink-2 hover:bg-white/80 hover:text-ink")}>
            {t.label}
          </Link>
        );
      })}
    </nav>
  );
}

function StatusCluster({ session }: { session: Session }) {
  const { state, connected, balance, events } = useLive();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const sim = state?.simulation;
  const mode = state?.mode;
  const alerts = state?.alerts?.length ?? 0;
  const lastAnomaly = events.find((e) => e.type === "anomaly");
  const initials = session.username.slice(0, 2).toUpperCase();
  return (
    <div className="flex shrink-0 items-center gap-2">
      <div className="glass hidden items-center gap-2 rounded-full px-3 py-1.5 text-[12px] ring-1 ring-white/70 xl:flex">
        <span className={clsx("size-2 rounded-full", connected ? "bg-ok pulse" : "bg-crit")} aria-hidden />
        <span className="font-medium text-ink">{mode === "PLANT" ? "Plant data · read-only" : mode === "SIMULATION" ? "Simulation" : "Connecting"}</span>
        {sim?.sim_timestamp && <span className="num text-ink-3">{sim.sim_timestamp.slice(11, 16)} UTC · ×{sim.speed}</span>}
      </div>
      {balance && (
        <span className={clsx("hidden items-center gap-1.5 rounded-full px-3 py-1.5 text-[12px] font-medium lg:inline-flex",
          balance.anomaly ? "bg-crit text-white" : "bg-ok text-white")}>
          {balance.anomaly ? "Balance abnormal" : "Balance within ±2σ"}
        </span>
      )}
      <Link href="/investigations" title={lastAnomaly ? `Latest anomaly ${lastAnomaly.data?.event_id}` : "Investigations"}
        className="relative inline-flex size-9 items-center justify-center rounded-full bg-white/85 text-ink-2 ring-1 ring-line hover:text-ink">
        <Bell size={15} />
        {alerts > 0 && <span className="num absolute -right-0.5 -top-0.5 inline-flex min-w-4 items-center justify-center rounded-full bg-accent px-1 text-[10px] font-semibold text-white">{alerts}</span>}
      </Link>
      <div className="relative">
        <button type="button" onClick={() => setOpen((o) => !o)} aria-label="Account"
          className="inline-flex size-9 items-center justify-center rounded-full bg-[linear-gradient(135deg,#e8885f,#c84b23)] text-[12px] font-semibold text-white ring-2 ring-white">
          {initials}
        </button>
        {open && (
          <div className="glass card-shadow absolute right-0 top-11 z-50 w-56 rounded-2xl p-2 ring-1 ring-white/70">
            <div className="px-2.5 py-2">
              <div className="text-[13px] font-medium">{session.username}</div>
              <div className="text-[11.5px] text-ink-3">role: {session.role} · read-only analytics, no plant control</div>
            </div>
            {session.token ? (
              <button type="button" className="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-[13px] text-ink-2 hover:bg-white"
                onClick={() => { setSession(null); router.push("/login"); }}>
                <LogOut size={14} /> Sign out
              </button>
            ) : (
              <div className="px-2.5 pb-1.5 text-[11.5px] text-ink-3">Open access on the company network: no sign-in needed.</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function Dock({ pathname }: { pathname: string | null }) {
  return (
    <aside className="fixed left-4 top-1/2 z-30 hidden -translate-y-1/2 md:block">
      <nav className="flex flex-col items-center gap-1 rounded-full bg-dark p-1.5 shadow-[0_18px_40px_-16px_rgba(0,0,0,0.55)]">
        <Link href="/sensors" title="Search instruments" className="inline-flex size-10 items-center justify-center rounded-full text-white/60 hover:bg-white/10 hover:text-white">
          <Search size={16} />
        </Link>
        <span className="my-1 h-px w-5 bg-white/15" />
        {DOCK.map(({ href, label, icon: Icon }) => {
          const active = pathname?.startsWith(href);
          return (
            <Link key={href} href={href} title={label} aria-label={label}
              className={clsx("group relative inline-flex size-10 items-center justify-center rounded-full transition-colors",
                active ? "bg-white text-dark" : "text-white/60 hover:bg-white/10 hover:text-white")}>
              <Icon size={16} />
              <span className="pointer-events-none absolute left-12 whitespace-nowrap rounded-full bg-dark px-2.5 py-1 text-[11.5px] text-white opacity-0 shadow transition-opacity group-hover:opacity-100">
                {label}
              </span>
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [session, setSess] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);
  const isLogin = pathname?.startsWith("/login");

  useEffect(() => {
    let alive = true;
    const apply = (s: Session | null) => {
      if (!alive) return;
      setSess(s);
      setReady(true);
      if (!s && !isLogin) router.replace("/login");
      if (s && !isLogin) connectLive();
    };
    // open-access deployments have no sign-in page: the session is created from the server's config
    ensureSession().then(apply).catch(() => apply(getSession()));
    return () => { alive = false; };
  }, [pathname, isLogin, router]);

  if (isLogin) return <>{children}</>;
  if (!ready || !session) return <div className="p-8 text-ink-3">Loading…</div>;

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-40 px-4 pt-3 md:pl-24 md:pr-6">
        <div className="mx-auto flex max-w-[1680px] items-center gap-3">
          <Logo />
          <div className="min-w-0 flex-1"><Tabs pathname={pathname} /></div>
          <StatusCluster session={session} />
        </div>
      </header>
      <Dock pathname={pathname} />
      <main className="mx-auto max-w-[1680px] px-4 pb-10 pt-6 md:pl-24 md:pr-6">{children}</main>
    </div>
  );
}
