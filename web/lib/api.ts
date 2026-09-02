/**
 * Fire Watch API client.
 *
 * The UI never computes a fire-relevant number. Every value shown comes from
 * the backend together with its provenance, so the map and the AI analyst are
 * always reading the same figures.
 */

function publicApiBase(): string {
  // Undefined means local Next talking to the published API port.
  // An empty string means same-origin (Caddy routes /api to FastAPI).
  const raw = process.env.NEXT_PUBLIC_API_BASE;
  if (raw === undefined) return "http://localhost:8000";
  return raw.replace(/\/$/, "");
}

export const API_BASE = publicApiBase();

export type Unknownable<T> = T & {
  status?: "unknown";
  reason?: string;
};

export interface MetricValue {
  metric: string;
  label: string;
  value: number | null;
  unit: string | null;
  confidence?: number | null;
  as_of_date?: string | null;
  sources?: string[];
  method?: string | null;
  caveat?: string | null;
  status?: string;
  reason?: string;
  name?: string | null;
}

export interface MunicipalitySummary {
  id: string;
  name: string;
  short_name: string;
  province: string;
  primary?: boolean;
  ingested: boolean;
  cells: number;
  source_count: number;
}

export interface MunicipalityDetail {
  id: string;
  name: string;
  short_name: string;
  province: string;
  timezone: string;
  area_km2: number | null;
  analysis: {
    h3_resolution: number;
    metric_crs: string;
    boundary_buffer_m: number;
    cells_total: number;
    cells_within_boundary: number;
  };
  boundary_source_url: string | null;
  known_unknowns: string[];
  scored_dates: string[];
  score_version: string;
  component_definitions: Record<string, string>;
  priority_bands: { min: number; label: string }[];
}

export interface Summary {
  municipality_id: string;
  as_of_date: string | null;
  status?: string;
  message?: string;
  score_version?: string;
  priority?: {
    cells_scored: number;
    mean: number | null;
    max: number | null;
    mean_confidence: number | null;
    mean_completeness: number | null;
    bands: Record<string, number>;
  };
  fire_weather?: Record<string, unknown>;
  recent_hotspots_7d?: number;
  hotspot_caveat?: string;
  data_health?: {
    counts: Record<string, number>;
    failed_sources: string[];
    authoritative_gaps: string[];
    municipal_sources_configured: number;
    municipal_sources_in_use: number;
  };
}

export interface DatasetHealth {
  source_id: string;
  title: string | null;
  adapter: string;
  feature_kind: string | null;
  precedence_tier: string;
  status: string;
  message: string | null;
  licence: string | null;
  licence_url: string | null;
  attribution: string | null;
  source_url: string | null;
  spatial_resolution: string | null;
  temporal_resolution: string | null;
  known_caveats: string[];
  last_observed_at: string | null;
  last_ingested_at: string;
  staleness: { age_hours: number | null; description: string };
  dataset_version: number;
  records_held: number;
  records_in_use: number;
  records_superseded: number;
  records_rejected: number;
  validation_status: string;
  validation_report: Record<string, unknown>;
}

export interface CellProfile {
  error?: string;
  municipality: { id: string; name: string };
  cell: {
    h3_index: string;
    resolution: number;
    centroid: { lat: number; lon: number };
    area_m2: number;
    within_boundary: boolean;
  };
  as_of_date: string;
  priority: {
    status?: string;
    reason?: string;
    overall?: number | null;
    band?: string;
    confidence?: number | null;
    completeness?: number | null;
    score_version?: string;
    components?: Record<string, number | null>;
    separable_views?: Record<string, number | null>;
    explanation?: {
      formula?: string;
      formula_note?: string;
      primary_drivers?: { component: string; value: number; why: string }[];
      factors_reducing_priority?: { component: string; value: number; why: string }[];
      components?: Record<string, ComponentExplanation>;
      unavailable_components?: string[];
    };
  };
  preserve: Record<string, MetricValue | string>;
  threat: Record<string, unknown>;
  existing_defenses: Record<string, unknown>;
  observation: Record<string, unknown>;
  unknown_needs_validation: {
    gap_type: string;
    severity: string;
    description: string;
    resolvable_by: string | null;
    affects: string[];
  }[];
  provenance: {
    source_id: string;
    title: string | null;
    licence: string | null;
    attribution: string | null;
    source_url: string | null;
    caveats: string[];
    dataset_version: number;
    status: string;
    observed_at: string | null;
  }[];
}

export interface ComponentExplanation {
  name: string;
  definition: string;
  value: number | null;
  completeness: number;
  confidence: number;
  rationale: string;
  signals: {
    name: string;
    label: string;
    value: number | null;
    weight: number;
    rationale: string;
    confidence: number;
    inputs_used: {
      metric: string;
      value: number | null;
      unit: string | null;
      confidence: number | null;
      sources: string[];
    }[];
    inputs_missing: string[];
  }[];
}

export interface AnalystResponse {
  answer: string;
  llm_used: boolean;
  model: string | null;
  tool_calls: { tool: string; arguments: unknown; error: string | null; result: unknown }[];
  citations: {
    type: string;
    source_id?: string;
    document_id?: string;
    title?: string | null;
    licence?: string | null;
    attribution?: string | null;
    observed_at?: string | null;
    page?: number | null;
    source_url?: string | null;
  }[];
  notes: string[];
}

export interface AlertRegion {
  id: string;
  name: string;
  short_name: string;
  province: string;
  ingested: boolean;
}

export interface AlertSubscription {
  municipality_id: string;
  short_name: string;
  name: string;
  subscribed_at: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* keep the status line */
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

const q = (params: Record<string, string | number | boolean | null | undefined>) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") {
      search.set(key, String(value));
    }
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
};

export const api = {
  municipalities: () =>
    request<{ municipalities: MunicipalitySummary[] }>("/api/municipalities"),

  municipality: (id: string) =>
    request<MunicipalityDetail>(`/api/municipalities/${id}`),

  summary: (id: string, date?: string) =>
    request<Summary>(`/api/municipalities/${id}/summary${q({ date })}`),

  boundary: (id: string) =>
    request<GeoJSON.FeatureCollection>(`/api/municipalities/${id}/boundary`),

  cells: (id: string, value: string, date?: string) =>
    request<GeoJSON.FeatureCollection & { properties?: Record<string, unknown> }>(
      `/api/municipalities/${id}/cells${q({ value, date })}`,
    ),

  cellsByMetric: (id: string, metric: string, date?: string) =>
    request<GeoJSON.FeatureCollection & { properties?: Record<string, unknown> }>(
      `/api/municipalities/${id}/cells/metric${q({ metric, date })}`,
    ),

  features: (id: string, kind: string, limit?: number) =>
    request<GeoJSON.FeatureCollection & { properties?: Record<string, unknown> }>(
      `/api/municipalities/${id}/features${q({ kind, limit })}`,
    ),

  profile: (id: string, lat: number, lon: number, date?: string) =>
    request<CellProfile>(`/api/municipalities/${id}/profile${q({ lat, lon, date })}`),

  dataHealth: (id: string) =>
    request<{
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
    }>(`/api/municipalities/${id}/data-health`),

  dataGaps: (id: string) =>
    request<{
      total: number;
      by_type: Record<string, { severity: string; description: string; resolvable_by: string | null; affects: string[] }[]>;
      note: string;
    }>(`/api/municipalities/${id}/data-gaps`),

  conflicts: (id: string) =>
    request<{
      conflicts: {
        subject: string;
        winner: string | null;
        superseded: string | null;
        description: string;
        detail: Record<string, unknown>;
      }[];
      precedence_order: string[];
    }>(`/api/municipalities/${id}/conflicts`),

  overlays: (id: string) =>
    request<{
      overlays: {
        source_id: string;
        name: string;
        label: string;
        group: string;
        wms_url: string;
        attribution: string | null;
        licence: string | null;
      }[];
    }>(`/api/municipalities/${id}/overlays`),

  dates: (id: string) =>
    request<{ dates: { date: string; cells: number; mean_priority: number | null }[] }>(
      `/api/municipalities/${id}/dates`,
    ),

  metrics: (id: string) =>
    request<{
      metrics: {
        metric: string;
        label: string;
        unit: string | null;
        group: string;
        available: boolean;
        cells_with_value: number;
        mean_confidence: number | null;
      }[];
    }>(`/api/municipalities/${id}/metrics`),

  rank: (id: string, params: Record<string, string | number | undefined>) =>
    request<{
      as_of_date: string;
      order_by: string;
      count: number;
      cells: {
        h3_index: string;
        lat: number;
        lon: number;
        overall_priority: number | null;
        band: string | null;
        confidence: number | null;
        components: Record<string, number | null>;
        primary_drivers: { component: string; value: number; why: string }[];
      }[];
    }>(`/api/municipalities/${id}/rank${q(params)}`),

  aiStatus: (id: string) =>
    request<{
      llm_enabled: boolean;
      model: string | null;
      mode: string;
      mode_explanation: string;
      tools: { name: string; description: string }[];
      documents: {
        document_id: string;
        title: string;
        status: string;
        message: string | null;
        source_url: string | null;
        quotable: boolean;
      }[];
      suggested_questions: string[];
      guardrails: string[];
    }>(`/api/municipalities/${id}/ai/status`),

  ask: (id: string, question: string, context?: Record<string, unknown>) =>
    request<AnalystResponse>(`/api/municipalities/${id}/ask`, {
      method: "POST",
      body: JSON.stringify({ question, context }),
    }),

  alertRegions: () => request<{ regions: AlertRegion[] }>("/api/alerts/regions"),

  alertStatus: () =>
    request<{ email_enabled: boolean; public_web_url: string }>("/api/alerts/status"),

  lookupAlerts: (email: string) =>
    request<{ email: string; subscriptions: AlertSubscription[] }>("/api/alerts/lookup", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),

  updateAlertSubscriptions: (email: string, municipalityIds: string[]) =>
    request<{
      email: string;
      municipality_ids: string[];
      added: string[];
      removed: string[];
      subscriptions: AlertSubscription[];
    }>("/api/alerts/subscriptions", {
      method: "PUT",
      body: JSON.stringify({ email, municipality_ids: municipalityIds }),
    }),

  unsubscribeAlert: (token: string) =>
    request<{
      status: string;
      email: string;
      municipality_id: string;
      short_name: string;
    }>(`/api/alerts/unsubscribe/${encodeURIComponent(token)}`, {
      method: "DELETE",
    }),
};
