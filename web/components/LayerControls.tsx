"use client";

/**
 * Layer and cell-value controls.
 *
 * Every toggle carries the caveat that belongs with the data it shows, because
 * a layer called "Water assets" quietly implies a great deal that the data do
 * not actually support.
 */

import { CELL_VALUES, FEATURE_LAYERS, GROUP_LABELS, GROUP_ORDER } from "@/lib/layers";
import type { LayerGroup } from "@/lib/layers";
import { Callout, Collapsible, Panel } from "@/components/ui";

interface Overlay {
  source_id: string;
  name: string;
  label: string;
  group: string;
  attribution: string | null;
}

interface MetricOption {
  metric: string;
  label: string;
  unit: string | null;
  group: string;
  available: boolean;
  cells_with_value: number;
}

interface Props {
  cellValue: string;
  cellMetric: string | null;
  cellOpacity: number;
  hillshade: boolean;
  visibleFeatures: Record<string, boolean>;
  visibleOverlays: Record<string, boolean>;
  overlays: Overlay[];
  metrics: MetricOption[];
  onCellValue: (value: string) => void;
  onCellMetric: (metric: string | null) => void;
  onCellOpacity: (value: number) => void;
  onHillshade: (value: boolean) => void;
  onFeature: (id: string, visible: boolean) => void;
  onOverlay: (name: string, visible: boolean) => void;
}

export default function LayerControls({
  cellValue,
  cellMetric,
  cellOpacity,
  hillshade,
  visibleFeatures,
  visibleOverlays,
  overlays,
  metrics,
  onCellValue,
  onCellMetric,
  onCellOpacity,
  onHillshade,
  onFeature,
  onOverlay,
}: Props) {
  const selectedSpec = CELL_VALUES.find((v) => v.value === cellValue);
  const availableMetrics = metrics.filter((m) => m.available);

  const layersByGroup = (group: LayerGroup) =>
    FEATURE_LAYERS.filter((spec) => spec.group === group);

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <Panel title="Cell shading" subtitle="What the hexagons are showing.">
        <select
          value={cellMetric ? `metric:${cellMetric}` : `score:${cellValue}`}
          onChange={(event) => {
            const raw = event.target.value;
            if (raw.startsWith("metric:")) {
              onCellMetric(raw.slice(7));
            } else {
              onCellMetric(null);
              onCellValue(raw.slice(6));
            }
          }}
          className="w-full rounded border border-white/10 bg-black/40 px-2 py-1.5 text-[12px] text-zinc-200 outline-none focus:border-duck/60"
        >
          <optgroup label="Priority score">
            {CELL_VALUES.map((option) => (
              <option key={option.value} value={`score:${option.value}`}>
                {option.label}
              </option>
            ))}
          </optgroup>
          <optgroup label="Raw measured metric">
            {availableMetrics.map((option) => (
              <option key={option.metric} value={`metric:${option.metric}`}>
                {option.label}
                {option.unit ? ` (${option.unit})` : ""}
              </option>
            ))}
          </optgroup>
        </select>

        {!cellMetric && selectedSpec?.note && (
          <div className="mt-2">
            <Callout>{selectedSpec.note}</Callout>
          </div>
        )}
        {cellMetric && (
          <div className="mt-2">
            <Callout>
              A raw measurement, not a score. Colour runs low to high on its own scale.
            </Callout>
          </div>
        )}

        <label className="mt-3 block text-[10px] uppercase tracking-wider text-zinc-500">
          Cell opacity
        </label>
        <input
          type="range"
          min={0}
          max={0.95}
          step={0.05}
          value={cellOpacity}
          onChange={(event) => onCellOpacity(Number(event.target.value))}
          className="mt-1 w-full accent-duck"
        />

        <label className="mt-2 flex cursor-pointer items-center gap-2 text-[12px] text-zinc-300">
          <input
            type="checkbox"
            checked={hillshade}
            onChange={(event) => onHillshade(event.target.checked)}
            className="accent-duck"
          />
          Terrain hillshade
          <span className="text-[10px] text-zinc-600">(same DEM as the score)</span>
        </label>
      </Panel>

      <Panel title="Layers" subtitle="Grouped by the question they answer.">
        {GROUP_ORDER.map((group) => {
          const specs = layersByGroup(group);
          const groupOverlays = overlays.filter((o) => o.group === group);
          if (!specs.length && !groupOverlays.length) {
            if (group !== "yellow_duck") return null;
          }
          const activeCount =
            specs.filter((s) => visibleFeatures[s.id]).length +
            groupOverlays.filter((o) => visibleOverlays[o.name]).length;

          return (
            <Collapsible
              key={group}
              title={GROUP_LABELS[group]}
              count={activeCount ? `${activeCount} on` : ""}
              defaultOpen={group === "fire_now" || group === "defenses"}
            >
              {group === "yellow_duck" && (
                <Callout tone="gap">
                  No Yellow Duck sensor is deployed yet. This group is where camera,
                  drone and ground-sensor coverage will appear, and its emptiness is
                  the point: nothing here is watching.
                </Callout>
              )}

              {specs.map((spec) => (
                <label
                  key={spec.id}
                  className="flex cursor-pointer items-start gap-2 py-1.5 text-[12px]"
                >
                  <input
                    type="checkbox"
                    checked={Boolean(visibleFeatures[spec.id])}
                    onChange={(event) => onFeature(spec.id, event.target.checked)}
                    className="mt-0.5 accent-duck"
                  />
                  <span
                    className="mt-1 h-2 w-2 shrink-0 rounded-full"
                    style={{ background: spec.color }}
                  />
                  <span className="min-w-0">
                    <span className="text-zinc-300">{spec.label}</span>
                    {spec.note && (
                      <span className="block text-[10px] leading-snug text-zinc-600">
                        {spec.note}
                      </span>
                    )}
                  </span>
                </label>
              ))}

              {groupOverlays.map((overlay) => (
                <label
                  key={overlay.name}
                  className="flex cursor-pointer items-start gap-2 py-1.5 text-[12px]"
                >
                  <input
                    type="checkbox"
                    checked={Boolean(visibleOverlays[overlay.name])}
                    onChange={(event) => onOverlay(overlay.name, event.target.checked)}
                    className="mt-0.5 accent-duck"
                  />
                  <span className="min-w-0">
                    <span className="text-zinc-300">{overlay.label}</span>
                    <span className="block text-[10px] leading-snug text-zinc-600">
                      Live WMS overlay. {overlay.attribution}
                    </span>
                  </span>
                </label>
              ))}
            </Collapsible>
          );
        })}
      </Panel>
    </div>
  );
}
