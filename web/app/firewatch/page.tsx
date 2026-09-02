"use client";

/**
 * The Fire Watch operating picture.
 *
 * Layout follows the order a fire chief would ask the questions in: what is the
 * state of the whole municipality, then what is at this specific place, then how
 * much of it can be trusted.
 */

import dynamic from "next/dynamic";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import AnalystPanel from "@/components/AnalystPanel";
import AlertsPanel from "@/components/AlertsPanel";
import DataHealthPanel from "@/components/DataHealthPanel";
import EvidenceDrawer from "@/components/EvidenceDrawer";
import LayerControls from "@/components/LayerControls";
import PriorityList from "@/components/PriorityList";
import Timeline from "@/components/Timeline";
import { Callout, ErrorNote, Spinner, Stat } from "@/components/ui";
import { api, type AlertRegion, type CellProfile, type MunicipalityDetail, type Summary } from "@/lib/api";
import { PRIORITY_BAND_LEGEND, PRIORITY_STOPS, fmt, fmtPercent } from "@/lib/display";
import { FEATURE_LAYERS } from "@/lib/layers";

// MapLibre touches window at import time.
const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

type Tab = "evidence" | "priorities" | "analyst" | "health" | "alerts";

const TABS: { id: Tab; label: string; highlight?: boolean }[] = [
  { id: "evidence", label: "Location" },
  { id: "alerts", label: "Email alerts", highlight: true },
  { id: "priorities", label: "Priorities" },
  { id: "analyst", label: "Analyst" },
  { id: "health", label: "Data health" },
];

export default function Page() {
  const [municipalityId, setMunicipalityId] = useState<string | null>(null);
  const [available, setAvailable] = useState<{ id: string; short_name: string }[]>([]);
  const [detail, setDetail] = useState<MunicipalityDetail | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [dates, setDates] = useState<{ date: string; cells: number; mean_priority: number | null }[]>([]);
  const [overlays, setOverlays] = useState<
    { source_id: string; name: string; label: string; group: string; wms_url: string; attribution: string | null }[]
  >([]);
  const [metrics, setMetrics] = useState<
    { metric: string; label: string; unit: string | null; group: string; available: boolean; cells_with_value: number }[]
  >([]);
  const [alertRegions, setAlertRegions] = useState<AlertRegion[]>([]);
  const [bootError, setBootError] = useState<string | null>(null);

  const [date, setDate] = useState<string | null>(null);
  const [cellValue, setCellValue] = useState("priority_percentile");
  const [cellMetric, setCellMetric] = useState<string | null>(null);
  const [cellOpacity, setCellOpacity] = useState(0.6);
  const [hillshade, setHillshade] = useState(true);
  const [topPrioritiesOnly, setTopPrioritiesOnly] = useState(false);
  const [showLayers, setShowLayers] = useState(false);

  const [visibleFeatures, setVisibleFeatures] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(FEATURE_LAYERS.map((spec) => [spec.id, spec.defaultVisible])),
  );
  const [visibleOverlays, setVisibleOverlays] = useState<Record<string, boolean>>({});

  const [selected, setSelected] = useState<{ lat: number; lon: number } | null>(null);
  const [profile, setProfile] = useState<CellProfile | null>(null);
  const [profileLoading, setProfileLoading] = useState(false);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("evidence");

  useEffect(() => {
    const query = window.matchMedia("(min-width: 1024px)");
    const sync = () => setShowLayers(query.matches);
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    api
      .alertRegions()
      .then(({ regions }) => setAlertRegions(regions))
      .catch(() => setAlertRegions([]));
  }, []);

  // --- boot ---------------------------------------------------------------
  useEffect(() => {
    api
      .municipalities()
      .then(({ municipalities }) => {
        const ingested = municipalities.filter((m) => m.ingested);
        setAvailable(municipalities.map((m) => ({ id: m.id, short_name: m.short_name })));
        // The config declares which municipality to open on. Falling back to
        // list order would mean a newly added municipality could displace the
        // one the product is actually for, purely on how its name sorts.
        const first =
          (ingested.find((m) => m.primary) ||
            ingested[0] ||
            municipalities[0])?.id ?? null;
        setMunicipalityId(first);
        if (!first) {
          setBootError(
            "No municipality is configured. Add a YAML config and run the ingest job.",
          );
        }
      })
      .catch((error) =>
        setBootError(
          `Cannot reach the Fire Watch API. ${(error as Error).message}`,
        ),
      );
  }, []);

  useEffect(() => {
    if (!municipalityId) return;
    setDetail(null);
    Promise.all([
      api.municipality(municipalityId),
      api.dates(municipalityId),
      api.overlays(municipalityId),
      api.metrics(municipalityId),
    ])
      .then(([d, dt, ov, mt]) => {
        setDetail(d);
        setDates(dt.dates);
        setOverlays(ov.overlays);
        setMetrics(mt.metrics);
      })
      .catch((error) => setBootError((error as Error).message));
  }, [municipalityId]);

  useEffect(() => {
    if (!municipalityId) return;
    api
      .summary(municipalityId, date || undefined)
      .then(setSummary)
      .catch(() => setSummary(null));
  }, [municipalityId, date]);

  // --- location inspection ------------------------------------------------
  const pick = useCallback(
    async (lat: number, lon: number) => {
      if (!municipalityId) return;
      setSelected({ lat, lon });
      setTab("evidence");
      setProfileLoading(true);
      setProfileError(null);
      try {
        setProfile(await api.profile(municipalityId, lat, lon, date || undefined));
      } catch (error) {
        setProfile(null);
        setProfileError((error as Error).message);
      } finally {
        setProfileLoading(false);
      }
    },
    [municipalityId, date],
  );

  const openAlerts = useCallback(() => {
    setTab("alerts");
  }, []);

  // Re-read the selected location when the date changes.
  useEffect(() => {
    if (selected) pick(selected.lat, selected.lon);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [date]);

  const latestDate = dates.length ? dates[dates.length - 1].date : null;
  const bands = summary?.priority?.bands || {};
  const fireWeather = (summary?.fire_weather || {}) as Record<string, { value?: number | null } | number | null>;

  const weatherValue = (key: string): number | null => {
    const raw = fireWeather[key];
    if (raw === null || raw === undefined) return null;
    if (typeof raw === "number") return raw;
    return (raw as { value?: number | null }).value ?? null;
  };

  const failed = summary?.data_health?.failed_sources || [];

  const legend = useMemo(
    () =>
      PRIORITY_STOPS.map(([stop, color]) => ({
        stop,
        color,
      })),
    [],
  );

  if (bootError) {
    return (
      <main className="flex h-dvh items-center justify-center bg-ink p-8">
        <div className="max-w-md space-y-3">
          <div className="text-[13px] font-semibold uppercase tracking-[0.2em] text-duck">
            Yellow Duck Labs · Fire Watch
          </div>
          <ErrorNote error={bootError} />
          <Callout>
            Bring the stack up with <code>docker compose up</code>, then run{" "}
            <code>python -m firewatch run -m west-vancouver</code> to ingest, derive
            and score.
          </Callout>
        </div>
      </main>
    );
  }

  if (!municipalityId || !detail) {
    return (
      <main className="flex h-dvh items-center justify-center bg-ink">
        <Spinner label="Loading the operating picture" />
      </main>
    );
  }

  const stats = (
    <>
      <Stat
        label="Cells scored"
        value={(summary?.priority?.cells_scored ?? 0).toLocaleString()}
        hint={`H3 res ${detail.analysis.h3_resolution}`}
      />
      <Stat
        label="Mean priority"
        value={fmt(summary?.priority?.mean, 3)}
        hint={`max ${fmt(summary?.priority?.max, 3)}`}
      />
      <Stat
        label="FWI"
        value={fmt(weatherValue("fwi"), 1)}
        hint={`FFMC ${fmt(weatherValue("ffmc"), 0)} · ISI ${fmt(weatherValue("isi"), 1)}`}
      />
      <Stat
        label="Hotspots 7d"
        value={summary?.recent_hotspots_7d ?? "—"}
        hint="thermal anomalies"
      />
      <Stat
        label="Confidence"
        value={fmtPercent(summary?.priority?.mean_confidence)}
        hint={`inputs ${fmtPercent(summary?.priority?.mean_completeness)}`}
      />
    </>
  );

  const bandsRow = (
    <div className="flex gap-1">
      {["Very high", "High", "Moderate", "Low", "Very low"].map((band) =>
        bands[band] ? (
          <span
            key={band}
            className="rounded bg-white/5 px-1.5 py-0.5 text-[10px] text-zinc-400"
          >
            {bands[band].toLocaleString()} {band.toLowerCase()}
          </span>
        ) : null,
      )}
    </div>
  );

  return (
    <main className="flex h-dvh flex-col overflow-hidden overscroll-none bg-ink text-zinc-200">
      <header className="shrink-0 border-b border-white/10 bg-black/50 pt-[max(0.4rem,env(safe-area-inset-top))]">
        <div className="flex items-center gap-3 px-3 py-2 lg:gap-5 lg:px-4">
          <Link href="/" className="shrink-0 hover:opacity-90">
            <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-duck lg:text-[10px]">
              Yellow Duck Labs
            </div>
            <div className="text-[12px] leading-tight text-zinc-400 lg:text-[11px] lg:text-zinc-500">
              Fire Watch · silent sentry
            </div>
          </Link>

          <select
            value={municipalityId}
            onChange={(event) => {
              setMunicipalityId(event.target.value);
              setSelected(null);
              setProfile(null);
              setDate(null);
            }}
            className="min-w-0 flex-1 rounded border border-white/10 bg-black/40 px-2 py-2 text-[14px] text-zinc-200 outline-none focus:border-duck/60 lg:flex-none lg:py-1 lg:text-[12px]"
          >
            {available.map((m) => (
              <option key={m.id} value={m.id}>
                {m.short_name}
              </option>
            ))}
          </select>

          <button
            type="button"
            onClick={openAlerts}
            className={`shrink-0 rounded px-3 py-2 text-[13px] font-semibold lg:py-1.5 lg:text-[12px] ${
              tab === "alerts"
                ? "bg-duck/20 text-duck ring-1 ring-duck/50"
                : "bg-duck text-black hover:opacity-90"
            }`}
          >
            Priority alerts
          </button>

          <div className="hidden flex-1 items-center gap-6 lg:flex">
            {stats}
            {bandsRow}
            {failed.length > 0 && (
              <button
                type="button"
                onClick={() => setTab("health")}
                className="shrink-0 rounded border border-red-500/40 bg-red-500/10 px-2 py-1 text-[10px] text-red-300 hover:bg-red-500/20"
              >
                {failed.length} source{failed.length === 1 ? "" : "s"} unavailable
              </button>
            )}
          </div>
        </div>

        <div className="flex items-end gap-5 overflow-x-auto px-3 pb-2.5 lg:hidden">
          {failed.length > 0 && (
            <button
              type="button"
              onClick={() => setTab("health")}
              className="shrink-0 rounded border border-red-500/40 bg-red-500/10 px-2 py-1.5 text-[11px] text-red-300"
            >
              {failed.length} down
            </button>
          )}
          {stats}
        </div>
      </header>

      <div className="relative flex min-h-0 flex-1 flex-col lg:flex-row">
        {showLayers && (
          <button
            type="button"
            aria-label="Close layers"
            onClick={() => setShowLayers(false)}
            className="absolute inset-0 z-20 bg-black/55 lg:hidden"
          />
        )}

        <aside
          className={`z-30 flex shrink-0 flex-col border-white/10 bg-black/95 transition-all lg:relative lg:bg-black/40 ${
            showLayers
              ? "absolute inset-y-0 left-0 w-[min(20rem,88vw)] border-r lg:static lg:w-72"
              : "pointer-events-none absolute inset-y-0 left-0 w-[min(20rem,88vw)] -translate-x-full border-r lg:pointer-events-auto lg:static lg:w-0 lg:overflow-hidden lg:border-0 lg:translate-x-0"
          }`}
        >
          <div className="flex items-center justify-between border-b border-white/10 px-4 py-3 lg:hidden">
            <span className="text-[12px] font-semibold uppercase tracking-[0.16em] text-zinc-400">
              Layers
            </span>
            <button
              type="button"
              onClick={() => setShowLayers(false)}
              className="rounded px-2 py-1 text-[13px] text-zinc-300 hover:text-white"
            >
              Close
            </button>
          </div>
          <div className="min-h-0 flex-1">
            <LayerControls
              cellValue={cellValue}
              cellMetric={cellMetric}
              cellOpacity={cellOpacity}
              hillshade={hillshade}
              topPrioritiesOnly={topPrioritiesOnly}
              visibleFeatures={visibleFeatures}
              visibleOverlays={visibleOverlays}
              overlays={overlays}
              metrics={metrics}
              onCellValue={setCellValue}
              onCellMetric={setCellMetric}
              onCellOpacity={setCellOpacity}
              onHillshade={setHillshade}
              onTopPrioritiesOnly={setTopPrioritiesOnly}
              onFeature={(id, visible) =>
                setVisibleFeatures((prev) => ({ ...prev, [id]: visible }))
              }
              onOverlay={(name, visible) =>
                setVisibleOverlays((prev) => ({ ...prev, [name]: visible }))
              }
            />
          </div>
        </aside>

        <div className="relative min-h-0 min-w-0 flex-1">
          <MapView
            municipalityId={municipalityId}
            date={date}
            cellValue={cellValue}
            cellMetric={cellMetric}
            cellOpacity={cellOpacity}
            topPrioritiesOnly={topPrioritiesOnly}
            visibleFeatures={visibleFeatures}
            overlays={overlays}
            visibleOverlays={visibleOverlays}
            hillshade={hillshade}
            selected={selected}
            onPick={pick}
          />

          <button
            type="button"
            onClick={() => setShowLayers(!showLayers)}
            className="absolute left-3 top-3 z-10 min-h-9 rounded border border-white/15 bg-black/70 px-3 py-1.5 text-[13px] text-zinc-200 backdrop-blur hover:text-white lg:min-h-0 lg:px-2 lg:py-1 lg:text-[11px] lg:text-zinc-300"
          >
            {showLayers ? "Hide layers" : "Layers"}
          </button>

          {!cellMetric && (
            <div className="absolute bottom-3 left-3 z-10 max-w-[15rem] rounded border border-white/10 bg-black/75 px-2.5 py-2 backdrop-blur lg:bottom-9">
              <div className="text-[9px] uppercase tracking-wider text-zinc-500">
                {topPrioritiesOnly
                  ? "High & very high only"
                  : cellValue === "priority_percentile"
                    ? "Rank within municipality"
                    : "Absolute priority"}
              </div>
              {cellValue === "priority_percentile" && !topPrioritiesOnly ? (
                <>
                  <div className="mt-1 flex items-center gap-1">
                    <span className="flex flex-col items-center gap-0.5">
                      <span
                        className="h-2.5 w-10 rounded-sm"
                        style={{
                          background:
                            "linear-gradient(to right, #1e3a5f, #d9b310, #c53030)",
                        }}
                      />
                      <span className="flex w-10 justify-between font-mono text-[8px] text-zinc-500">
                        <span>0</span>
                        <span>1</span>
                      </span>
                    </span>
                    <span className="text-[8px] leading-snug text-zinc-600">
                      lowest → highest today. Band labels use absolute score.
                    </span>
                  </div>
                </>
              ) : (
                <div className="mt-1 flex items-center gap-1">
                  {legend.map(({ stop, color }) => (
                    <span key={stop} className="flex flex-col items-center gap-0.5">
                      <span
                        className="h-2.5 w-6 rounded-sm"
                        style={{ background: color }}
                      />
                      <span className="font-mono text-[8px] text-zinc-500">{stop}</span>
                    </span>
                  ))}
                  <span className="ml-1.5 flex flex-col items-center gap-0.5">
                    <span className="h-2.5 w-6 rounded-sm bg-[#30363d]" />
                    <span className="text-[8px] text-zinc-500">n/a</span>
                  </span>
                </div>
              )}
              {cellValue === "overall_priority" && !topPrioritiesOnly && (
                <div className="mt-1.5 flex flex-wrap gap-x-2 gap-y-0.5 text-[8px] text-zinc-600">
                  {PRIORITY_BAND_LEGEND.map(({ stop, label }) => (
                    <span key={label}>
                      ≥{stop} {label.toLowerCase()}
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {summary?.status === "no_scores" && (
            <div className="absolute inset-x-0 top-14 z-10 mx-auto w-fit px-3">
              <Callout tone="warn">{summary.message}</Callout>
            </div>
          )}
        </div>

        <aside className="flex max-h-[46dvh] min-h-0 shrink-0 flex-col border-t border-white/10 bg-black/70 lg:max-h-none lg:w-[27rem] lg:border-l lg:border-t-0 lg:bg-black/40">
          <nav className="flex shrink-0 border-b border-white/10">
            {TABS.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => setTab(item.id)}
                className={`min-h-11 flex-1 px-1.5 py-2.5 text-[12px] font-medium transition-colors lg:min-h-0 lg:px-2 lg:py-2 lg:text-[11px] ${
                  tab === item.id
                    ? "border-b-2 border-duck bg-white/[0.03] text-duck"
                    : item.highlight
                      ? "bg-duck/10 text-duck hover:bg-duck/15"
                      : "text-zinc-500 hover:text-zinc-300"
                }`}
              >
                {item.label}
              </button>
            ))}
          </nav>

          <div className="min-h-0 flex-1 overflow-hidden">
            {tab === "evidence" && (
              <EvidenceDrawer
                profile={profile}
                loading={profileLoading}
                error={profileError}
              />
            )}
            {tab === "priorities" && (
              <PriorityList
                municipalityId={municipalityId}
                date={date}
                onSelect={pick}
              />
            )}
            {tab === "analyst" && (
              <AnalystPanel
                municipalityId={municipalityId}
                date={date}
                selected={selected}
              />
            )}
            {tab === "health" && <DataHealthPanel municipalityId={municipalityId} />}
            {tab === "alerts" && <AlertsPanel regions={alertRegions} />}
          </div>
        </aside>
      </div>

      <Timeline dates={dates} active={date} latest={latestDate} onSelect={setDate} />
    </main>
  );
}
