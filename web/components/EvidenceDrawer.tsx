"use client";

/**
 * Location inspection, organised as the brief's four questions.
 *
 * The score is shown, but it is never the last word: every component can be
 * opened down to the individual signals, the metrics behind them, the datasets
 * behind those, and what was missing.
 */

import type { CellProfile, ComponentExplanation, MetricValue } from "@/lib/api";
import {
  bandColor,
  componentLabel,
  fmt,
  fmtPercent,
  fmtValue,
} from "@/lib/display";
import {
  Callout,
  Collapsible,
  ErrorNote,
  MetricRow,
  Panel,
  ScoreBar,
  Spinner,
  StatusPill,
} from "@/components/ui";

interface Props {
  profile: CellProfile | null;
  loading: boolean;
  error: string | null;
}

const COMPONENT_ORDER = [
  "ignition_likelihood",
  "spread_potential",
  "consequence_exposure",
  "observation_gap",
  "access_difficulty_proxy",
];

export default function EvidenceDrawer({ profile, loading, error }: Props) {
  if (loading) return <Spinner label="Reading this location" />;
  if (error) {
    return (
      <div className="p-4">
        <ErrorNote error={error} />
      </div>
    );
  }
  if (!profile) {
    return (
      <div className="p-5 text-[14px] leading-relaxed text-zinc-400 lg:p-6 lg:text-[12px] lg:text-zinc-500">
        Tap anywhere on the map to inspect that location: what is there to lose,
        what threat it faces, what already protects it, and what nobody knows.
      </div>
    );
  }
  if (profile.error) {
    return (
      <div className="p-4">
        <ErrorNote error={profile.error} />
      </div>
    );
  }

  const priority = profile.priority;
  const explanation = priority?.explanation;
  const components = explanation?.components || {};

  return (
    <div className="h-full min-h-0 overflow-y-auto overscroll-contain">
      <Panel
        title="Fire Watch Priority"
        subtitle={`Cell ${profile.cell.h3_index} · ${profile.cell.centroid.lat.toFixed(4)}, ${profile.cell.centroid.lon.toFixed(4)} · as of ${profile.as_of_date}`}
      >
        {priority?.status === "unknown" ? (
          <Callout tone="gap">{priority.reason}</Callout>
        ) : (
          <>
            <div className="flex items-end justify-between gap-4">
              <div>
                <div className="font-mono text-3xl tabular-nums text-zinc-50">
                  {fmt(priority?.overall, 3)}
                </div>
                <div className={`text-[12px] font-medium ${bandColor(priority?.band)}`}>
                  {priority?.band}
                  {profile.priority && "percentile" in profile.priority && (
                    <span className="ml-2 text-zinc-500">
                      ranks above{" "}
                      {fmtPercent(
                        (profile.priority as { percentile?: number | null })
                          .percentile,
                      )}{" "}
                      of this municipality
                    </span>
                  )}
                </div>
              </div>
              <div className="text-right text-[10px] text-zinc-500">
                <div>confidence {fmtPercent(priority?.confidence)}</div>
                <div>inputs present {fmtPercent(priority?.completeness)}</div>
                <div>{priority?.score_version}</div>
              </div>
            </div>
            <div className="mt-3">
              <ScoreBar value={priority?.overall ?? null} />
            </div>

            <div className="mt-4 space-y-2">
              {COMPONENT_ORDER.map((name) => {
                const value = priority?.components?.[name] ?? null;
                const detail = components[name];
                return (
                  <ComponentRow
                    key={name}
                    name={name}
                    value={value}
                    detail={detail}
                  />
                );
              })}
            </div>

            {explanation?.formula_note && (
              <div className="mt-4">
                <Callout tone="warn">{explanation.formula_note}</Callout>
              </div>
            )}
          </>
        )}
      </Panel>

      <Panel
        title="What we preserve here"
        subtitle="What would be lost. Property value is deliberately not used."
      >
        {renderMetrics(profile.preserve)}
      </Panel>

      <Panel
        title="What threat exists here"
        subtitle="Terrain, fuel and recorded fire history at this location."
      >
        {renderMetrics(profile.threat)}
      </Panel>

      <Panel
        title="What already protects here"
        subtitle="Access, water and response assets that exist today."
      >
        {renderMetrics(profile.existing_defenses)}
      </Panel>

      <Panel
        title="Who is watching here"
        subtitle="Observation coverage, and where it runs out."
      >
        {renderMetrics(profile.observation)}
      </Panel>

      <Panel
        title="What nobody knows"
        subtitle={`${profile.unknown_needs_validation.length} recorded gaps affecting this location.`}
      >
        <div className="space-y-2">
          {profile.unknown_needs_validation.slice(0, 8).map((gap, index) => (
            <div key={index} className="rounded border border-purple-500/20 bg-purple-500/[0.05] px-2.5 py-2">
              <div className="flex items-start justify-between gap-2">
                <span className="text-[11px] leading-snug text-purple-100/90">
                  {gap.description}
                </span>
                <span className="shrink-0 text-[9px] uppercase tracking-wide text-purple-300/60">
                  {gap.severity}
                </span>
              </div>
              {gap.resolvable_by && (
                <div className="mt-1 text-[10px] text-zinc-500">
                  Resolvable by: {gap.resolvable_by.replace(/_/g, " ")}
                </div>
              )}
            </div>
          ))}
          {profile.unknown_needs_validation.length > 8 && (
            <div className="text-[10px] text-zinc-600">
              {profile.unknown_needs_validation.length - 8} more in the Data health tab.
            </div>
          )}
        </div>
      </Panel>

      <Panel
        title="Where these numbers came from"
        subtitle={`${profile.provenance.length} datasets contributed to this location.`}
      >
        <div className="space-y-1">
          {profile.provenance.map((source) => (
            <Collapsible key={source.source_id} title={source.source_id}>
              <div className="space-y-1.5 text-[11px] text-zinc-400">
                <div className="flex items-center gap-2">
                  <StatusPill status={source.status} />
                  <span className="text-[10px] text-zinc-600">
                    v{source.dataset_version}
                  </span>
                </div>
                {source.title && <div className="text-zinc-300">{source.title}</div>}
                {source.attribution && <div>{source.attribution}</div>}
                {source.licence && (
                  <div className="text-[10px] text-zinc-500">Licence: {source.licence}</div>
                )}
                {source.observed_at && (
                  <div className="text-[10px] text-zinc-500">
                    Observed {source.observed_at.slice(0, 10)}
                  </div>
                )}
                {source.caveats.map((caveat, i) => (
                  <div key={i} className="text-[10px] leading-snug text-amber-300/70">
                    {caveat}
                  </div>
                ))}
                {source.source_url && (
                  <a
                    href={source.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="block truncate text-[10px] text-sky-400 hover:underline"
                  >
                    {source.source_url}
                  </a>
                )}
              </div>
            </Collapsible>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function ComponentRow({
  name,
  value,
  detail,
}: {
  name: string;
  value: number | null;
  detail?: ComponentExplanation;
}) {
  return (
    <div className="rounded border border-white/5 bg-white/[0.02] px-2.5 py-2">
      <div className="flex items-center justify-between gap-3">
        <span className="text-[12px] text-zinc-300">{componentLabel(name)}</span>
        <span className="font-mono text-[12px] tabular-nums text-zinc-100">
          {fmt(value, 3)}
        </span>
      </div>
      <div className="mt-1.5">
        <ScoreBar value={value} />
      </div>
      {detail && (
        <>
          <p className="mt-2 text-[11px] leading-relaxed text-zinc-500">
            {detail.rationale}
          </p>
          <Collapsible
            title="Signals and inputs"
            count={`${detail.signals.length}`}
          >
            <div className="space-y-2.5">
              {detail.signals.map((signal) => (
                <div key={signal.name}>
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-[11px] text-zinc-300">{signal.label}</span>
                    <span className="font-mono text-[10px] text-zinc-500">
                      {fmt(signal.value, 2)} · weight {signal.weight}
                    </span>
                  </div>
                  <p className="mt-0.5 text-[10px] leading-snug text-zinc-500">
                    {signal.rationale}
                  </p>
                  {signal.inputs_used.map((input) => (
                    <div
                      key={input.metric}
                      className="mt-1 flex items-baseline justify-between gap-2 border-l border-white/10 pl-2 text-[10px]"
                    >
                      <span className="text-zinc-600">
                        {input.metric}
                        {input.sources.length ? ` · ${input.sources.join(", ")}` : ""}
                      </span>
                      <span className="font-mono text-zinc-400">
                        {fmtValue(input.value, input.unit)}
                      </span>
                    </div>
                  ))}
                  {signal.inputs_missing.length > 0 && (
                    <div className="mt-1 border-l border-purple-500/30 pl-2 text-[10px] text-purple-300/70">
                      missing: {signal.inputs_missing.join(", ")}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </Collapsible>
          <p className="mt-1 text-[10px] leading-snug text-zinc-600">
            {detail.definition}
          </p>
        </>
      )}
    </div>
  );
}

/** Render a profile section: metric rows, plus its trailing prose note. */
function renderMetrics(section: Record<string, unknown>) {
  const note = typeof section.note === "string" ? section.note : null;
  const entries = Object.entries(section).filter(([key]) => key !== "note");

  return (
    <>
      <div className="divide-y divide-white/5">
        {entries.map(([key, value]) => (
          <MetricRow
            key={key}
            label={labelFor(key, value as MetricValue)}
            metric={value}
          />
        ))}
      </div>
      {note && (
        <div className="mt-2">
          <Callout>{note}</Callout>
        </div>
      )}
    </>
  );
}

function labelFor(key: string, value: MetricValue | undefined): string {
  if (value && typeof value === "object" && value.label) return value.label;
  return key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}
