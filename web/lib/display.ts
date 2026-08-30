/** Shared formatting and colour scales. */

export const PRIORITY_STOPS: [number, string][] = [
  [0.0, "#1e3a5f"],
  [0.3, "#2b7a78"],
  [0.45, "#d9b310"],
  [0.6, "#e07b39"],
  [0.75, "#c53030"],
];

/** MapLibre interpolate expression for a 0..1 score column. */
export function scoreColorExpression(property = "v") {
  return [
    "interpolate",
    ["linear"],
    ["coalesce", ["get", property], -1],
    -1,
    "#30363d", // no value: grey, never a reassuring green
    ...PRIORITY_STOPS.flatMap(([stop, color]) => [stop, color]),
  ] as unknown as maplibregl.ExpressionSpecification;
}

export const STATUS_STYLES: Record<string, { bg: string; text: string; label: string }> = {
  CURRENT: { bg: "bg-emerald-500/15", text: "text-emerald-300", label: "Current" },
  AGING: { bg: "bg-amber-500/15", text: "text-amber-300", label: "Aging" },
  STALE: { bg: "bg-orange-500/15", text: "text-orange-300", label: "Stale" },
  PARTIAL: { bg: "bg-sky-500/15", text: "text-sky-300", label: "Partial" },
  UNKNOWN: { bg: "bg-zinc-500/15", text: "text-zinc-300", label: "Unknown age" },
  FAILED: { bg: "bg-red-500/15", text: "text-red-300", label: "Failed" },
  UNAVAILABLE: { bg: "bg-purple-500/15", text: "text-purple-300", label: "Unavailable" },
};

export const TIER_LABELS: Record<string, string> = {
  municipal: "Municipal",
  provincial: "Provincial",
  federal: "Federal",
  remote_sensing: "Remote sensing",
  community: "Community (OSM)",
  derived: "Derived",
};

export function fmt(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

export function fmtValue(value: number | null | undefined, unit?: string | null): string {
  if (value === null || value === undefined) return "unknown";
  const digits = unit === "count" ? 0 : Math.abs(value) >= 100 ? 0 : Math.abs(value) >= 10 ? 1 : 2;
  const rendered = value.toFixed(digits);
  if (!unit || unit === "count") return unit === "count" ? rendered : rendered;
  if (unit === "fraction") return `${(value * 100).toFixed(0)}%`;
  if (unit === "deg") return `${rendered}\u00b0`;
  return `${rendered} ${unit}`;
}

export function fmtPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${Math.round(value * 100)}%`;
}

export function bandColor(band: string | null | undefined): string {
  switch (band) {
    case "Very high":
      return "text-red-300";
    case "High":
      return "text-orange-300";
    case "Moderate":
      return "text-amber-300";
    case "Low":
      return "text-teal-300";
    case "Very low":
      return "text-sky-300";
    default:
      return "text-zinc-400";
  }
}

export function componentLabel(name: string): string {
  return (
    {
      ignition_likelihood: "Ignition likelihood",
      spread_potential: "Spread potential",
      consequence_exposure: "Consequence / exposure",
      observation_gap: "Observation gap",
      access_difficulty_proxy: "Access difficulty (proxy)",
      hazard: "Hazard",
      exposure: "Exposure",
      current_conditions: "Current conditions",
      operational_gap: "Operational gap",
      confidence: "Confidence",
      completeness: "Completeness",
    }[name] || name
  );
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "unknown";
  const hours = (Date.now() - then) / 3_600_000;
  if (hours < 1) return "under an hour ago";
  if (hours < 48) return `${Math.round(hours)} h ago`;
  const days = hours / 24;
  if (days < 60) return `${Math.round(days)} days ago`;
  if (days < 730) return `${Math.round(days / 30)} months ago`;
  return `${(days / 365).toFixed(1)} years ago`;
}
