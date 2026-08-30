/**
 * Declarative layer catalogue.
 *
 * Grouped as the brief specifies: FIRE NOW, TERRAIN & FUELS, WHAT WE PRESERVE,
 * EXISTING DEFENSES, FIRE HISTORY, YELLOW DUCK. Only a small default set is
 * visible on load, so the first screen is readable rather than exhaustive.
 */

export type LayerGroup =
  | "yellow_duck"
  | "fire_now"
  | "terrain_fuels"
  | "preserve"
  | "defenses"
  | "fire_history";

export const GROUP_LABELS: Record<LayerGroup, string> = {
  yellow_duck: "Yellow Duck",
  fire_now: "Fire now",
  terrain_fuels: "Terrain & fuels",
  preserve: "What we preserve",
  defenses: "Existing defenses",
  fire_history: "Fire history",
};

export const GROUP_ORDER: LayerGroup[] = [
  "yellow_duck",
  "fire_now",
  "terrain_fuels",
  "preserve",
  "defenses",
  "fire_history",
];

export interface FeatureLayerSpec {
  id: string;
  label: string;
  group: LayerGroup;
  /** Backend feature_kind. */
  kind: string;
  geometry: "point" | "line" | "polygon";
  color: string;
  defaultVisible: boolean;
  /** Shown in layer controls so a user knows what they are trusting. */
  note?: string;
  limit?: number;
  /** Draw as a graduated circle using this numeric property. */
  sizeProperty?: string;
}

export const FEATURE_LAYERS: FeatureLayerSpec[] = [
  {
    id: "satellite_hotspot",
    label: "Satellite hotspots",
    group: "fire_now",
    kind: "satellite_hotspot",
    geometry: "point",
    color: "#ff6b35",
    defaultVisible: true,
    note: "Thermal anomalies, not confirmed wildfires.",
    limit: 4000,
    sizeProperty: "frp",
  },
  {
    id: "fire_weather_observation",
    label: "Fire weather stations",
    group: "fire_now",
    kind: "fire_weather_observation",
    geometry: "point",
    color: "#7dd3fc",
    defaultVisible: false,
    note: "CWFIS stations with FFMC/DMC/DC/ISI/BUI/FWI readings.",
    limit: 400,
  },
  {
    id: "weather_station",
    label: "ECCC climate stations",
    group: "fire_now",
    kind: "weather_station",
    geometry: "point",
    color: "#a5b4fc",
    defaultVisible: false,
    limit: 400,
  },
  {
    id: "vegetation_cell",
    label: "Mapped vegetation",
    group: "terrain_fuels",
    kind: "vegetation_cell",
    geometry: "polygon",
    color: "#2f6f4f",
    defaultVisible: false,
    note: "OSM presence proxy. Fire behaviour uses the CWFIS FBP fuel grid, shown as the WMS overlay.",
    limit: 4000,
  },
  {
    id: "building",
    label: "Buildings",
    group: "preserve",
    kind: "building",
    geometry: "polygon",
    color: "#c9d1d9",
    defaultVisible: false,
    note: "Structural exposure. Coverage may be incomplete.",
    limit: 15000,
  },
  {
    id: "park",
    label: "Parks & reserves",
    group: "preserve",
    kind: "park",
    geometry: "polygon",
    color: "#4ade80",
    defaultVisible: false,
    limit: 2000,
  },
  {
    id: "road",
    label: "Roads",
    group: "defenses",
    kind: "road",
    geometry: "line",
    color: "#8b949e",
    defaultVisible: false,
    note: "A mapped road is not evidence of apparatus access.",
    limit: 15000,
  },
  {
    id: "water_asset",
    label: "Water assets",
    group: "defenses",
    kind: "water_asset",
    geometry: "point",
    color: "#38bdf8",
    defaultVisible: true,
    note: "Mapped location only. No flow, pressure or operability data.",
    limit: 3000,
  },
  {
    id: "fire_station",
    label: "Fire stations",
    group: "defenses",
    kind: "fire_station",
    geometry: "point",
    color: "#f87171",
    defaultVisible: true,
    note: "Location only. Staffing and response time are not modelled.",
    limit: 100,
  },
  {
    id: "fire_perimeter",
    label: "Historical fire perimeters",
    group: "fire_history",
    kind: "fire_perimeter",
    geometry: "polygon",
    color: "#b45309",
    defaultVisible: false,
    note: "Larger mapped fires only.",
    limit: 2000,
  },
  {
    id: "fire_event",
    label: "Recorded fire incidents",
    group: "fire_history",
    kind: "fire_event",
    geometry: "point",
    color: "#fb923c",
    defaultVisible: false,
    limit: 2000,
  },
];

/** Score columns available for the priority cell layer. */
export interface CellValueSpec {
  value: string;
  label: string;
  /** Shown under the selector so the number on screen is never unexplained. */
  note?: string;
}

export const CELL_VALUES: CellValueSpec[] = [
  {
    value: "priority_percentile",
    label: "Priority rank (relative)",
    note: "Each cell's rank against the rest of this municipality on this date. Use this to read the map; comparable within one date only.",
  },
  {
    value: "overall_priority",
    label: "Fire Watch Priority (absolute)",
    note: "The absolute score. It tracks real severity, so on a damp day the whole map is legitimately low.",
  },
  { value: "ignition_likelihood", label: "Ignition likelihood" },
  { value: "spread_potential", label: "Spread potential" },
  { value: "consequence_exposure", label: "Consequence / exposure" },
  {
    value: "observation_gap",
    label: "Observation gap",
    note: "Line-of-sight from the road network to a 10 m smoke column, through terrain and typical FBP canopy.",
  },
  { value: "access_difficulty_proxy", label: "Access difficulty (proxy)" },
  { value: "hazard", label: "Hazard (ignition x spread)" },
  { value: "exposure", label: "Exposure" },
  { value: "current_conditions", label: "Current fire weather" },
  { value: "operational_gap", label: "Operational gap" },
  { value: "confidence", label: "Confidence in the score" },
  { value: "completeness", label: "Input completeness" },
];
