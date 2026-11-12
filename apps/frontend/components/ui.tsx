"use client";

import clsx from "clsx";
import { AlertOctagon, AlertTriangle, ArrowUpRight, CheckCircle2, Info, Loader2, MinusCircle, MoreHorizontal } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

export type Tone = "normal" | "warning" | "critical" | "offline" | "info";

const toneClass: Record<Tone, string> = {
  normal: "text-ok bg-ok/10 ring-ok/25",
  warning: "text-[#9a6510] bg-warn/12 ring-warn/30",
  critical: "text-crit bg-crit/10 ring-crit/25",
  offline: "text-ink-3 bg-off/12 ring-off/25",
  info: "text-info bg-info/10 ring-info/25",
};
const toneIcon: Record<Tone, typeof CheckCircle2> = {
  normal: CheckCircle2,
  warning: AlertTriangle,
  critical: AlertOctagon,
  offline: MinusCircle,
  info: Info,
};
export const toneHex: Record<Tone, string> = {
  normal: "#3e7b57", warning: "#c98a1d", critical: "#c4432b", offline: "#9a958c", info: "#3f6c9e",
};

/** Status indicator: colour + icon + text (never colour alone). */
export function Badge({ tone, children, className }: { tone: Tone; children: ReactNode; className?: string }) {
  const Icon = toneIcon[tone];
  return (
    <span className={clsx("inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[11.5px] font-medium ring-1 ring-inset",
      toneClass[tone], className)}>
      <Icon size={11.5} aria-hidden />
      {children}
    </span>
  );
}

/** Solid pill (e.g. "+5% vs last month" in the reference): dark-green / dark / orange. */
export function Pill({ children, tone = "dark", className }: {
  children: ReactNode; tone?: "dark" | "green" | "orange" | "light" | "red"; className?: string;
}) {
  const t = { dark: "bg-dark text-white", green: "bg-ok text-white", orange: "bg-accent text-white",
    light: "bg-white text-ink-2 ring-1 ring-line", red: "bg-crit text-white" }[tone];
  return <span className={clsx("inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2.5 py-0.5 text-[11px] font-medium", t, className)}>{children}</span>;
}

export function IconButton({ children, title, onClick, href, className, active }: {
  children: ReactNode; title: string; onClick?: () => void; href?: string; className?: string; active?: boolean;
}) {
  const cls = clsx("inline-flex size-8 items-center justify-center rounded-full ring-1 ring-line transition-colors",
    active ? "bg-dark text-white ring-dark" : "bg-white/80 text-ink-2 hover:bg-white hover:text-ink", className);
  if (href) return <Link href={href} title={title} aria-label={title} className={cls}>{children}</Link>;
  return <button type="button" title={title} aria-label={title} onClick={onClick} className={cls}>{children}</button>;
}

export function Card({ title, actions, children, className, subtitle, href, pad = true }: {
  title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; subtitle?: ReactNode;
  href?: string; pad?: boolean;
}) {
  return (
    <section className={clsx("glass card-shadow rounded-[22px] ring-1 ring-white/70", className)}>
      {(title || actions || href) && (
        <header className="flex items-start justify-between gap-3 px-5 pt-4">
          <div className="min-w-0">
            <h2 className="text-[14.5px] font-semibold tracking-[-0.01em] text-ink">{title}</h2>
            {subtitle && <p className="mt-0.5 text-[12px] text-ink-3">{subtitle}</p>}
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {actions}
            {href && <IconButton title="Open" href={href}><ArrowUpRight size={14} /></IconButton>}
          </div>
        </header>
      )}
      <div className={clsx(pad && "px-5 pb-5", pad && (title || actions || href) ? "pt-3" : pad && "pt-5")}>{children}</div>
    </section>
  );
}

/** KPI tile. */
export function Kpi({ label, value, unit, sub, tone }: {
  label: string; value: ReactNode; unit?: string; sub?: ReactNode; tone?: Tone;
}) {
  return (
    <div className="glass card-shadow rounded-[20px] px-4 py-3.5 ring-1 ring-white/70">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[12px] text-ink-3">{label}</span>
        {tone && tone !== "normal" && <Badge tone={tone}>{tone === "info" ? "info" : tone}</Badge>}
      </div>
      <div className="mt-1 flex items-baseline gap-1">
        <span className="num display text-[26px] font-medium text-ink">{value}</span>
        {unit && <span className="text-[13px] text-ink-3">{unit}</span>}
      </div>
      {sub && <div className="mt-0.5 text-[12px] text-ink-2">{sub}</div>}
    </div>
  );
}

/** Inline headline statistic: round icon, big number, caption, optional pill (reference "Sales Overview" row). */
export function Stat({ icon, value, unit, label, pill, pillTone = "green" }: {
  icon: ReactNode; value: ReactNode; unit?: string; label: ReactNode; pill?: ReactNode;
  pillTone?: "dark" | "green" | "orange" | "light" | "red";
}) {
  return (
    <div className="flex min-w-0 items-center gap-3">
      <div className="flex size-11 shrink-0 items-center justify-center rounded-full bg-white text-ink-2 shadow-sm ring-1 ring-line">{icon}</div>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="num display text-[22px] font-medium text-ink">{value}</span>
          {unit && <span className="-ml-1 text-[13px] text-ink-3">{unit}</span>}
          {pill && <Pill tone={pillTone}>{pill}</Pill>}
        </div>
        <div className="truncate text-[12px] text-ink-3">{label}</div>
      </div>
    </div>
  );
}

export function Segmented<T extends string>({ value, options, onChange, size = "sm" }: {
  value: T; options: { value: T; label: ReactNode }[]; onChange: (v: T) => void; size?: "sm" | "md";
}) {
  return (
    <div className="inline-flex rounded-full bg-panel-2 p-0.5 ring-1 ring-line">
      {options.map((o) => (
        <button key={o.value} type="button" onClick={() => onChange(o.value)}
          className={clsx("rounded-full font-medium transition-colors", size === "sm" ? "px-2.5 py-1 text-[11.5px]" : "px-3.5 py-1.5 text-[12.5px]",
            o.value === value ? "bg-white text-ink shadow-sm ring-1 ring-line" : "text-ink-3 hover:text-ink")}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Button({ children, onClick, variant = "default", disabled, type = "button", title, className }: {
  children: ReactNode; onClick?: () => void; variant?: "default" | "primary" | "danger" | "ghost" | "accent";
  disabled?: boolean; type?: "button" | "submit"; title?: string; className?: string;
}) {
  const v = {
    default: "bg-white text-ink ring-1 ring-line hover:bg-panel-2",
    primary: "bg-dark text-white hover:bg-dark-2",
    accent: "bg-accent text-white hover:brightness-105",
    danger: "bg-crit/10 text-crit ring-1 ring-crit/30 hover:bg-crit/15",
    ghost: "text-ink-2 hover:bg-white/70",
  }[variant];
  return (
    <button type={type} title={title} disabled={disabled} onClick={onClick}
      className={clsx("inline-flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-[13px] font-medium transition-all",
        "disabled:cursor-not-allowed disabled:opacity-40", v, className)}>
      {children}
    </button>
  );
}

export function ProbBar({ p, tone = "info", label }: { p: number; tone?: Tone; label?: string }) {
  const color = { normal: "bg-ok", warning: "bg-warn", critical: "bg-crit", offline: "bg-off", info: "bg-accent" }[tone];
  return (
    <div className="flex items-center gap-2" aria-label={label}>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-sand">
        <div className={clsx("h-full rounded-full", color)} style={{ width: `${Math.max(0, Math.min(1, p)) * 100}%` }} />
      </div>
      <span className="num w-12 text-right text-[12px] text-ink-2">{(p * 100).toFixed(1)}%</span>
    </div>
  );
}

/** Horizontal stacked mix bar (reference "Stage mix"). */
export function StackBar({ parts, height = 34 }: {
  parts: { label: string; value: number; color: string; text?: string }[]; height?: number;
}) {
  const total = parts.reduce((a, p) => a + Math.max(p.value, 0), 0) || 1;
  return (
    <div>
      <div className="flex gap-1.5" style={{ height }}>
        {parts.filter((p) => p.value > 0).map((p) => (
          <div key={p.label} title={`${p.label}: ${p.text ?? p.value}`} className="rounded-[10px]"
            style={{ width: `${(100 * p.value) / total}%`, minWidth: 6, background: p.color }} />
        ))}
      </div>
      <div className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1">
        {parts.map((p) => (
          <span key={p.label} className="inline-flex items-center gap-1.5 text-[11.5px] text-ink-3">
            <span className="size-2 rounded-full" style={{ background: p.color }} /> {p.label}
            <span className="num text-ink-2">{p.text ?? p.value}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

/** Dark / orange / light highlight card (reference "Hot deals"). */
export function HighlightCard({ variant = "dark", caption, title, subtitle, value, foot, chips, href, menu = true }: {
  variant?: "dark" | "orange" | "light"; caption: ReactNode; title: ReactNode; subtitle?: ReactNode; value: ReactNode;
  foot?: ReactNode; chips?: string[]; href?: string; menu?: boolean;
}) {
  const v = {
    dark: "bg-[linear-gradient(160deg,#2f2d2a,#1f1e1c)] text-white",
    orange: "bg-[linear-gradient(160deg,#df6538,#c84b23)] text-white",
    light: "glass text-ink ring-1 ring-white/70",
  }[variant];
  const sub = variant === "light" ? "text-ink-3" : "text-white/60";
  const body = (
    <div className={clsx("card-shadow group relative flex h-full min-h-[172px] flex-col justify-between overflow-hidden rounded-[22px] p-4 transition-transform hover:-translate-y-0.5", v)}>
      <div className="flex items-start justify-between gap-2">
        <span className={clsx("text-[11.5px]", sub)}>{caption}</span>
        {menu && <span className={clsx("inline-flex size-7 items-center justify-center rounded-full",
          variant === "light" ? "bg-white ring-1 ring-line" : "bg-white/10")}><MoreHorizontal size={14} /></span>}
      </div>
      <div className="mt-2">
        <div className="text-[16px] font-medium leading-snug tracking-[-0.01em]">{title}</div>
        {subtitle && <div className={clsx("mt-0.5 text-[12px]", sub)}>{subtitle}</div>}
      </div>
      <div className="mt-3 flex items-end justify-between gap-2">
        <div>
          <div className="num display text-[24px] font-medium">{value}</div>
          {foot && <div className={clsx("text-[11.5px]", sub)}>{foot}</div>}
        </div>
        {chips && chips.length > 0 && (
          <div className="flex -space-x-2">
            {chips.slice(0, 3).map((c) => (
              <span key={c} title={c} className={clsx("inline-flex size-7 items-center justify-center rounded-full text-[8.5px] font-semibold ring-2",
                variant === "light" ? "bg-sand text-ink-2 ring-white" : "bg-white/90 text-dark ring-black/10")}>
                {c.replace(/^[A-Z]{2}-0*/, "").slice(0, 3) || c.slice(0, 3)}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
  return href ? <Link href={href} className="block h-full">{body}</Link> : body;
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-ink-3">
      <Loader2 size={14} className="animate-spin" /> {label}
    </span>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="rounded-2xl bg-crit/8 px-3.5 py-2.5 text-[13px] text-crit ring-1 ring-crit/25">{error}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-8 text-center text-[13px] text-ink-3">{children}</div>;
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  const words = title.split(" ");
  const split = words.length > 1 ? [words.slice(0, Math.ceil(words.length / 2)).join(" "), words.slice(Math.ceil(words.length / 2)).join(" ")] : [title];
  return (
    <div className="rise mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="display text-[34px] font-medium text-ink md:text-[40px]">
          {split.map((w, i) => <span key={i} className="block">{w}</span>)}
        </h1>
        {subtitle && <p className="mt-2 max-w-2xl text-[13px] text-ink-3">{subtitle}</p>}
      </div>
      <div className="flex flex-wrap items-center gap-2">{actions}</div>
    </div>
  );
}

export function JsonView({ value }: { value: unknown }) {
  return (
    <pre className="mono max-h-96 overflow-auto rounded-2xl bg-panel-2 p-3 text-[11.5px] text-ink-2 ring-1 ring-line">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

/** Label for whether a value is measured, estimated (calculated) or simulated ground truth. */
export function Provenance({ kind }: { kind: "measured" | "estimated" | "simulated-truth" | "model" }) {
  const map = {
    measured: ["Measured", "normal"],
    estimated: ["Estimated", "warning"],
    "simulated-truth": ["Simulated ground truth", "info"],
    model: ["Model output", "info"],
  } as const;
  const [t, tone] = map[kind];
  return <Badge tone={tone as Tone}>{t}</Badge>;
}
