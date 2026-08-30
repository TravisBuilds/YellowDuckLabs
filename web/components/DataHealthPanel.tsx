"use client";

/**
 * Data health.
 *
 * The brief's hardest requirement is that a stakeholder can always tell how
 * fresh and how trustworthy the picture is. So this panel leads with what is
 * broken or missing rather than with what worked.
 */

import { useEffect, useState } from "react";

import { api, type DatasetHealth } from "@/lib/api";
import { STATUS_STYLES, TIER_LABELS, fmtPercent, relativeTime } from "@/lib/display";
import { Callout, Collapsible, ErrorNote, Panel, Spinner, StatusPill } from "@/components/ui";

interface Health {
  overall: {
    counts: Record<string, number>;
    failed_sources: string[];
    authoritative_gaps: string[];
    municipal_sources_configured: number;
    municipal_sources_in_use: number;
  };
  datasets: DatasetHealth[];
  coverage: {
    total_cells: number;
    metrics: {
      metric: string;
      label: string;
      group: string | null;
      unit: string | null;
      cells_with_value: number;
      coverage_percent: number;
      mean_confidence: number | null;
      expected_incomplete: string | null;
    }[];
    missing_metrics: { metric: string; label: string; group: string }[];
  };
  status_meanings: Record<string, string>;
}

interface Gaps {
  total: number;
  by_type: Record<
    string,
    { severity: string; description: string; resolvable_by: string | null; affects: string[] }[]
  >;
  note: string;
}

const STATUS_ORDER = [
  "FAILED",
  "UNAVAILABLE",
  "PARTIAL",
  "STALE",
  "UNKNOWN",
  "AGING",
  "CURRENT",
];

export default function DataHealthPanel({ municipalityId }: { municipalityId: string }) {
  const [health, setHealth] = useState<Health | null>(null);
  const [gaps, setGaps] = useState<Gaps | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setHealth(null);
    setError(null);
    Promise.all([api.dataHealth(municipalityId), api.dataGaps(municipalityId)])
      .then(([h, g]) => {
        setHealth(h as Health);
        setGaps(g);
      })
      .catch((e) => setError((e as Error).message));
  }, [municipalityId]);

  if (error) {
    return (
      <div className="p-4">
        <ErrorNote error={error} />
      </div>
    );
  }
  if (!health) return <Spinner label="Checking every source" />;

  const sorted = [...health.datasets].sort(
    (a, b) => STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status),
  );
  const problems = sorted.filter((d) =>
    ["FAILED", "UNAVAILABLE", "PARTIAL"].includes(d.status),
  );

  return (
    <div className="overflow-y-auto">
      <Panel title="Source status" subtitle={`${health.datasets.length} configured sources.`}>
        <div className="flex flex-wrap gap-1.5">
          {STATUS_ORDER.filter((s) => health.overall.counts[s]).map((status) => (
            <span
              key={status}
              className={`rounded px-2 py-1 text-[10px] ${STATUS_STYLES[status]?.bg} ${STATUS_STYLES[status]?.text}`}
            >
              {health.overall.counts[status]} {STATUS_STYLES[status]?.label.toLowerCase()}
            </span>
          ))}
        </div>

        {problems.length > 0 && (
          <div className="mt-3 space-y-2">
            {problems.map((dataset) => (
              <div
                key={dataset.source_id}
                className="rounded border border-red-500/20 bg-red-500/[0.05] px-2.5 py-2"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11px] text-zinc-200">{dataset.source_id}</span>
                  <StatusPill status={dataset.status} />
                </div>
                {dataset.message && (
                  <p className="mt-1 text-[10px] leading-snug text-zinc-400">
                    {dataset.message}
                  </p>
                )}
              </div>
            ))}
          </div>
        )}

        {health.overall.municipal_sources_configured === 0 ? (
          <div className="mt-3">
            <Callout tone="gap">
              No municipal-authoritative source is configured here. Buildings,
              roads and water assets rest entirely on provincial, federal and
              community data, so counts are lower bounds of unknown tightness.
            </Callout>
          </div>
        ) : (
          health.overall.authoritative_gaps.length > 0 && (
            <div className="mt-3">
              <Callout tone="gap">
                Authoritative data missing for:{" "}
                {health.overall.authoritative_gaps.join(", ")}. Community-mapped data are
                standing in, which means incompleteness is likely rather than
                hypothetical.
              </Callout>
            </div>
          )
        )}
      </Panel>

      <Panel title="Every source" subtitle="Licence, attribution, freshness and caveats.">
        <div className="space-y-0.5">
          {sorted.map((dataset) => (
            <Collapsible
              key={dataset.source_id}
              title={dataset.source_id}
              count={dataset.records_in_use.toLocaleString()}
            >
              <div className="space-y-1.5 text-[11px] text-zinc-400">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusPill status={dataset.status} />
                  <span className="text-[10px] text-zinc-500">
                    {TIER_LABELS[dataset.precedence_tier] || dataset.precedence_tier}
                  </span>
                  <span className="text-[10px] text-zinc-600">
                    v{dataset.dataset_version}
                  </span>
                </div>
                {dataset.title && <div className="text-zinc-300">{dataset.title}</div>}
                {dataset.message && (
                  <div className="leading-snug text-zinc-400">{dataset.message}</div>
                )}
                <div className="text-[10px] text-zinc-500">
                  {dataset.staleness.description}
                </div>
                <div className="grid grid-cols-2 gap-x-3 text-[10px] text-zinc-500">
                  <span>in use: {dataset.records_in_use.toLocaleString()}</span>
                  <span>held: {dataset.records_held.toLocaleString()}</span>
                  <span>superseded: {dataset.records_superseded.toLocaleString()}</span>
                  <span>rejected: {dataset.records_rejected.toLocaleString()}</span>
                </div>
                {dataset.last_observed_at && (
                  <div className="text-[10px] text-zinc-500">
                    Newest observation {relativeTime(dataset.last_observed_at)}
                  </div>
                )}
                {dataset.attribution && (
                  <div className="text-[10px] text-zinc-500">{dataset.attribution}</div>
                )}
                {dataset.licence && (
                  <div className="text-[10px] text-zinc-500">
                    Licence: {dataset.licence}
                  </div>
                )}
                {dataset.known_caveats.map((caveat, index) => (
                  <div key={index} className="leading-snug text-amber-300/70">
                    {caveat}
                  </div>
                ))}
                {dataset.source_url && (
                  <a
                    href={dataset.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="block truncate text-[10px] text-sky-400 hover:underline"
                  >
                    {dataset.source_url}
                  </a>
                )}
              </div>
            </Collapsible>
          ))}
        </div>
      </Panel>

      <Panel
        title="Metric coverage"
        subtitle={`Across ${health.coverage.total_cells.toLocaleString()} analysis cells.`}
      >
        <div className="space-y-1">
          {health.coverage.metrics.map((metric) => (
            <div key={metric.metric}>
              <div className="flex items-center gap-2">
                <span className="w-40 shrink-0 truncate text-[11px] text-zinc-400">
                  {metric.label}
                </span>
                <div className="h-1 flex-1 overflow-hidden rounded-full bg-white/5">
                  <div
                    className="h-full rounded-full bg-teal-500/70"
                    style={{ width: `${metric.coverage_percent}%` }}
                  />
                </div>
                <span className="w-9 shrink-0 text-right font-mono text-[10px] text-zinc-500">
                  {Math.round(metric.coverage_percent)}%
                </span>
                <span className="w-9 shrink-0 text-right font-mono text-[10px] text-zinc-600">
                  {fmtPercent(metric.mean_confidence)}
                </span>
              </div>
              {metric.expected_incomplete && metric.coverage_percent < 100 && (
                <p className="ml-40 pl-2 text-[10px] leading-snug text-zinc-600">
                  {metric.expected_incomplete}
                </p>
              )}
            </div>
          ))}
        </div>
        {health.coverage.missing_metrics.length > 0 && (
          <div className="mt-3">
            <Callout tone="gap">
              Never computed:{" "}
              {health.coverage.missing_metrics.map((m) => m.label).join(", ")}.
            </Callout>
          </div>
        )}
      </Panel>

      {gaps && (
        <Panel
          title="Recorded gaps"
          subtitle={`${gaps.total} known unknowns. ${gaps.note}`}
        >
          {Object.entries(gaps.by_type).map(([type, items]) => (
            <Collapsible key={type} title={type.replace(/_/g, " ")} count={items.length}>
              <div className="space-y-1.5">
                {items.map((gap, index) => (
                  <div key={index} className="text-[11px] leading-snug text-zinc-400">
                    <span className="text-purple-300/60">[{gap.severity}]</span>{" "}
                    {gap.description}
                    {gap.resolvable_by && (
                      <span className="block text-[10px] text-zinc-600">
                        Resolvable by {gap.resolvable_by.replace(/_/g, " ")}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </Collapsible>
          ))}
        </Panel>
      )}

      <Panel title="What the statuses mean" subtitle="Same vocabulary everywhere.">
        <div className="space-y-1.5">
          {Object.entries(health.status_meanings).map(([status, meaning]) => (
            <div key={status} className="flex items-start gap-2">
              <span className="shrink-0">
                <StatusPill status={status} />
              </span>
              <span className="text-[10px] leading-snug text-zinc-500">{meaning}</span>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}
