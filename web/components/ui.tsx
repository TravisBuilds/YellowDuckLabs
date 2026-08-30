"use client";

/**
 * Small shared presentation pieces.
 *
 * The recurring job in this UI is showing a number next to how much it should
 * be trusted, so those pairings live here rather than being reinvented per
 * panel.
 */

import { type ReactNode, useState } from "react";

import { STATUS_STYLES, fmtPercent, fmtValue, relativeTime } from "@/lib/display";
import type { MetricValue } from "@/lib/api";

export function Panel({
  title,
  subtitle,
  right,
  children,
}: {
  title: string;
  subtitle?: string;
  right?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="border-b border-white/5 last:border-0">
      <header className="flex items-baseline justify-between gap-3 px-4 pt-4 pb-2">
        <div>
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-duck">
            {title}
          </h3>
          {subtitle && <p className="mt-1 text-[11px] leading-snug text-zinc-500">{subtitle}</p>}
        </div>
        {right}
      </header>
      <div className="px-4 pb-4">{children}</div>
    </section>
  );
}

export function Collapsible({
  title,
  count,
  defaultOpen = false,
  children,
}: {
  title: string;
  count?: number | string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="border-t border-white/5 first:border-0">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center justify-between py-2 text-left text-[12px] text-zinc-300 hover:text-white"
      >
        <span className="flex items-center gap-2">
          <span className={`text-zinc-600 transition-transform ${open ? "rotate-90" : ""}`}>
            ▸
          </span>
          {title}
        </span>
        {count !== undefined && (
          <span className="font-mono text-[10px] text-zinc-500">{count}</span>
        )}
      </button>
      {open && <div className="pb-3 pl-4">{children}</div>}
    </div>
  );
}

export function StatusPill({ status }: { status: string }) {
  const style = STATUS_STYLES[status] || STATUS_STYLES.UNKNOWN;
  return (
    <span
      className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${style.bg} ${style.text}`}
    >
      {style.label}
    </span>
  );
}

/** A 0..1 bar. Never green: a low number here is not good news, just low. */
export function ScoreBar({ value, muted = false }: { value: number | null; muted?: boolean }) {
  const pct = value === null ? 0 : Math.max(0, Math.min(1, value)) * 100;
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-white/5">
      <div
        className={`h-full rounded-full ${muted ? "bg-zinc-500" : "bg-gradient-to-r from-sky-700 via-amber-500 to-red-500"}`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

/** A metric with its unit, provenance and caveat, or an explicit unknown. */
export function MetricRow({ label, metric }: { label: string; metric: MetricValue | unknown }) {
  const m = metric as MetricValue;
  const unknown = !m || m.status === "unknown" || m.value === null || m.value === undefined;

  return (
    <div className="flex items-start justify-between gap-3 py-1.5">
      <div className="min-w-0 flex-1">
        <div className="text-[12px] text-zinc-300">{label}</div>
        {unknown ? (
          <div className="mt-0.5 text-[10px] leading-snug text-purple-300/80">
            {m?.reason || "No data available."}
          </div>
        ) : (
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[10px] text-zinc-500">
            {m.sources?.length ? <span>{m.sources.join(", ")}</span> : null}
            {m.confidence !== null && m.confidence !== undefined && (
              <span>confidence {fmtPercent(m.confidence)}</span>
            )}
          </div>
        )}
        {!unknown && m.caveat && (
          <div className="mt-1 text-[10px] leading-snug text-amber-300/70">{m.caveat}</div>
        )}
      </div>
      <div className="shrink-0 font-mono text-[13px] tabular-nums">
        {unknown ? (
          <span className="text-purple-300/70">unknown</span>
        ) : (
          <span className="text-zinc-100">
            {m.name ? m.name : fmtValue(m.value, m.unit)}
          </span>
        )}
      </div>
    </div>
  );
}

export function Callout({
  tone = "note",
  children,
}: {
  tone?: "note" | "warn" | "gap";
  children: ReactNode;
}) {
  const styles = {
    note: "border-white/10 bg-white/[0.03] text-zinc-400",
    warn: "border-amber-500/25 bg-amber-500/[0.06] text-amber-200/90",
    gap: "border-purple-500/25 bg-purple-500/[0.06] text-purple-200/90",
  }[tone];
  return (
    <div className={`rounded border px-2.5 py-2 text-[11px] leading-relaxed ${styles}`}>
      {children}
    </div>
  );
}

export function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</div>
      <div className="mt-0.5 font-mono text-[15px] tabular-nums text-zinc-100">{value}</div>
      {hint && <div className="text-[10px] text-zinc-600">{hint}</div>}
    </div>
  );
}

export function Freshness({ iso }: { iso: string | null | undefined }) {
  return <span className="text-[10px] text-zinc-500">{relativeTime(iso)}</span>;
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-6 text-[11px] text-zinc-500">
      <span className="inline-block h-3 w-3 animate-spin rounded-full border border-zinc-600 border-t-duck" />
      {label || "Loading"}
    </div>
  );
}

export function ErrorNote({ error }: { error: string }) {
  return (
    <div className="rounded border border-red-500/30 bg-red-500/[0.07] px-2.5 py-2 text-[11px] text-red-200">
      {error}
    </div>
  );
}
