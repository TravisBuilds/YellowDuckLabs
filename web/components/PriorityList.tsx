"use client";

/**
 * Ranked cells, with the filters the brief's questions imply.
 *
 * The most operationally interesting query is not "what scores highest" but
 * "what is exposed, steep, and has no recorded fire history" — the places a
 * history-based intuition would miss. That is a preset here.
 */

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { bandColor, componentLabel, fmt } from "@/lib/display";
import { CELL_VALUES } from "@/lib/layers";
import { Callout, ErrorNote, Panel, Spinner } from "@/components/ui";

interface RankedCell {
  h3_index: string;
  lat: number;
  lon: number;
  overall_priority: number | null;
  band: string | null;
  confidence: number | null;
  components: Record<string, number | null>;
  primary_drivers: { component: string; value: number; why: string }[];
}

interface Props {
  municipalityId: string;
  date: string | null;
  onSelect: (lat: number, lon: number) => void;
}

const PRESETS = [
  {
    id: "priority",
    label: "Highest priority",
    params: { order_by: "overall_priority" },
    note: "Straight ranking by the combined score.",
  },
  {
    id: "unseen",
    label: "Least observed",
    params: { order_by: "observation_gap" },
    note: "Where terrain hides a fire from the road network for longest.",
  },
  {
    id: "blind_interface",
    label: "Exposed, steep, no fire history",
    params: { order_by: "overall_priority", min_slope_deg: 25, min_exposure: 0.4, max_history: 0 },
    note: "Structures nearby, steep ground, and not one recorded hotspot in ten years. These are the places a history-based intuition misses.",
  },
  {
    id: "exposure",
    label: "Most exposed",
    params: { order_by: "consequence_exposure" },
    note: "Most structures and community land at stake.",
  },
  {
    id: "access",
    label: "Hardest to reach",
    params: { order_by: "access_difficulty_proxy" },
    note: "A proxy from roads, terrain and water distance. Not response time.",
  },
];

export default function PriorityList({ municipalityId, date, onSelect }: Props) {
  const [preset, setPreset] = useState(PRESETS[0]);
  const [cells, setCells] = useState<RankedCell[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setCells(null);
    setError(null);
    try {
      const result = await api.rank(municipalityId, {
        ...preset.params,
        date: date || undefined,
        limit: 25,
      });
      setCells(result.cells as RankedCell[]);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [municipalityId, date, preset]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="h-full min-h-0 overflow-y-auto overscroll-contain">
      <Panel title="Priorities" subtitle={preset.note}>
        <div className="flex flex-wrap gap-1">
          {PRESETS.map((option) => (
            <button
              key={option.id}
              type="button"
              onClick={() => setPreset(option)}
              className={`min-h-9 rounded px-2.5 py-2 text-[13px] lg:min-h-0 lg:px-2 lg:py-1 lg:text-[11px] ${
                preset.id === option.id
                  ? "bg-duck text-black"
                  : "border border-white/10 text-zinc-400 hover:text-white"
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>

        {error && (
          <div className="mt-3">
            <ErrorNote error={error} />
          </div>
        )}
        {!cells && !error && <Spinner label="Ranking cells" />}

        {cells && cells.length === 0 && (
          <div className="mt-3">
            <Callout tone="gap">
              No cell meets these conditions. That is a finding, not an error: the
              filter combination does not occur in this municipality with the data
              currently held.
            </Callout>
          </div>
        )}

        {cells && cells.length > 0 && (
          <ol className="mt-3 space-y-1.5">
            {cells.map((cell, index) => (
              <li key={cell.h3_index}>
                <button
                  type="button"
                  onClick={() => onSelect(cell.lat, cell.lon)}
                  className="w-full rounded border border-white/5 bg-white/[0.02] px-2.5 py-2.5 text-left hover:border-duck/40 lg:py-2"
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-[11px] text-zinc-500">
                      #{index + 1} · {cell.lat.toFixed(4)}, {cell.lon.toFixed(4)}
                    </span>
                    <span className="flex items-baseline gap-2">
                      <span className={`text-[10px] ${bandColor(cell.band)}`}>
                        {cell.band}
                      </span>
                      <span className="font-mono text-[12px] tabular-nums text-zinc-100">
                        {fmt(
                          preset.params.order_by === "overall_priority"
                            ? cell.overall_priority
                            : cell.components[preset.params.order_by],
                          3,
                        )}
                      </span>
                    </span>
                  </div>
                  {preset.params.order_by !== "overall_priority" && (
                    <div className="mt-0.5 text-[10px] text-zinc-600">
                      {componentLabel(preset.params.order_by)} · overall{" "}
                      {fmt(cell.overall_priority, 3)}
                    </div>
                  )}
                  {cell.primary_drivers[0] && (
                    <p className="mt-1 text-[10px] leading-snug text-zinc-500">
                      {cell.primary_drivers[0].why}
                    </p>
                  )}
                </button>
              </li>
            ))}
          </ol>
        )}
      </Panel>
    </div>
  );
}

export const RANKABLE_LABELS = CELL_VALUES;
