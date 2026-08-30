"use client";

/**
 * Date selector across scored dates.
 *
 * Historical mode is not cosmetic: it is how a stakeholder checks whether the
 * model responds to conditions at all. So the control makes it obvious when you
 * are looking at something other than the latest picture.
 */

import { fmt } from "@/lib/display";

interface DatePoint {
  date: string;
  cells: number;
  mean_priority: number | null;
}

interface Props {
  dates: DatePoint[];
  active: string | null;
  latest: string | null;
  onSelect: (date: string | null) => void;
}

export default function Timeline({ dates, active, latest, onSelect }: Props) {
  if (dates.length === 0) return null;

  const historical = Boolean(active && active !== latest);
  const values = dates.map((d) => d.mean_priority ?? 0);
  const max = Math.max(...values, 0.001);

  return (
    <div className="flex items-center gap-3 border-t border-white/10 bg-black/60 px-4 py-2 backdrop-blur">
      <div className="shrink-0">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500">
          {historical ? "Historical replay" : "Latest picture"}
        </div>
        <div
          className={`font-mono text-[13px] ${historical ? "text-amber-300" : "text-zinc-100"}`}
        >
          {active || latest || "—"}
        </div>
      </div>

      <div className="flex flex-1 items-end gap-0.5 overflow-x-auto">
        {dates.map((point) => {
          const isActive = point.date === (active || latest);
          const height = 6 + ((point.mean_priority ?? 0) / max) * 22;
          return (
            <button
              key={point.date}
              type="button"
              title={`${point.date} · mean priority ${fmt(point.mean_priority, 3)} · ${point.cells.toLocaleString()} cells`}
              onClick={() => onSelect(point.date === latest ? null : point.date)}
              className="group flex w-3 shrink-0 flex-col items-center justify-end"
            >
              <span
                className={`w-2 rounded-sm transition-colors ${
                  isActive ? "bg-duck" : "bg-zinc-700 group-hover:bg-zinc-500"
                }`}
                style={{ height }}
              />
            </button>
          );
        })}
      </div>

      {historical && (
        <button
          type="button"
          onClick={() => onSelect(null)}
          className="shrink-0 rounded border border-amber-500/40 px-2 py-1 text-[10px] text-amber-300 hover:bg-amber-500/10"
        >
          Back to latest
        </button>
      )}

      <div className="shrink-0 text-[10px] leading-snug text-zinc-600">
        Bars show mean priority
        <br />
        across all cells.
      </div>
    </div>
  );
}
