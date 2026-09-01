"use client";

import maplibregl from "maplibre-gl";
import { useEffect, useRef } from "react";

import { API_BASE } from "@/lib/api";
import { scoreColorExpression } from "@/lib/display";
import { FEATURE_LAYERS, TOP_PRIORITY_MIN, type FeatureLayerSpec } from "@/lib/layers";

interface WmsOverlay {
  source_id: string;
  name: string;
  label: string;
  group: string;
  wms_url: string;
  attribution: string | null;
}

interface Props {
  municipalityId: string;
  date: string | null;
  cellValue: string;
  cellMetric: string | null;
  cellOpacity: number;
  topPrioritiesOnly: boolean;
  visibleFeatures: Record<string, boolean>;
  overlays: WmsOverlay[];
  visibleOverlays: Record<string, boolean>;
  hillshade: boolean;
  selected: { lat: number; lon: number } | null;
  onPick: (lat: number, lon: number) => void;
  onReady?: () => void;
}

const CELL_FILL = "cells-fill";
const CELL_LINE = "cells-line";
const SELECTED_SOURCE = "selected-point";
const HILLSHADE = "hillshade";

/**
 * Keyless dark vector basemap. OpenFreeMap serves OpenMapTiles without an API
 * key or usage cap, which matters because a municipal tool should not depend on
 * someone's personal tile key to draw its own base layer.
 */
const BASEMAP_STYLE_URL = "https://tiles.openfreemap.org/styles/dark";

/**
 * Used only if the remote style cannot be fetched. Deliberately austere: better
 * a blank dark canvas with real data on it than a broken map.
 */
const FALLBACK_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  glyphs: "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf",
  sources: {},
  layers: [
    { id: "background", type: "background", paint: { "background-color": "#070a0f" } },
  ],
};

/**
 * The same Terrarium DEM the backend uses for slope and aspect, so the
 * hillshade on screen is the terrain the score was actually computed from.
 */
const TERRAIN_SOURCE: maplibregl.RasterDEMSourceSpecification = {
  type: "raster-dem",
  tiles: ["https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"],
  encoding: "terrarium",
  tileSize: 256,
  maxzoom: 14,
  attribution:
    '<a href="https://github.com/tilezen/joerd/blob/master/docs/attribution.md">Tilezen Joerd</a> elevation tiles',
};

export default function MapView({
  municipalityId,
  date,
  cellValue,
  cellMetric,
  cellOpacity,
  topPrioritiesOnly,
  visibleFeatures,
  overlays,
  visibleOverlays,
  hillshade,
  selected,
  onPick,
  onReady,
}: Props) {
  const container = useRef<HTMLDivElement | null>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const popup = useRef<maplibregl.Popup | null>(null);
  const loaded = useRef(false);

  // --- map creation -------------------------------------------------------
  useEffect(() => {
    if (!container.current || map.current) return;

    const instance = new maplibregl.Map({
      container: container.current,
      style: BASEMAP_STYLE_URL,
      center: [-123.16, 49.36],
      zoom: 11.2,
      attributionControl: { compact: true },
    });
    map.current = instance;

    instance.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-right");
    instance.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    const resize = new ResizeObserver(() => instance.resize());
    resize.observe(container.current);

    popup.current = new maplibregl.Popup({
      closeButton: false,
      closeOnClick: false,
      className: "firewatch-popup",
      offset: 8,
    });

    instance.on("error", (event) => {
      // A basemap that fails to load must not take the data layers with it.
      const message = String((event as unknown as { error?: Error }).error?.message || "");
      if (message.includes(BASEMAP_STYLE_URL) && !loaded.current) {
        instance.setStyle(FALLBACK_STYLE);
      }
    });

    instance.on("load", async () => {
      loaded.current = true;
      // Insert our layers beneath the basemap's labels: place names and road
      // shields stay legible on top of the score shading.
      const labelLayer = firstSymbolLayer(instance);

      if (!instance.getSource("terrain")) {
        instance.addSource("terrain", TERRAIN_SOURCE);
      }
      instance.addLayer(
        {
          id: HILLSHADE,
          type: "hillshade",
          source: "terrain",
          layout: { visibility: hillshade ? "visible" : "none" },
          paint: {
            "hillshade-exaggeration": 0.45,
            "hillshade-shadow-color": "#000000",
            "hillshade-highlight-color": "#4a5568",
            "hillshade-accent-color": "#1a202c",
          },
        },
        labelLayer,
      );

      await addBoundary(instance, municipalityId, labelLayer);
      addCellLayers(
        instance,
        municipalityId,
        cellValue,
        date,
        cellOpacity,
        topPrioritiesOnly,
        labelLayer,
      );

      instance.addSource(SELECTED_SOURCE, {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      instance.addLayer({
        id: "selected-ring",
        type: "circle",
        source: SELECTED_SOURCE,
        paint: {
          "circle-radius": 9,
          "circle-color": "transparent",
          "circle-stroke-color": "#f5b301",
          "circle-stroke-width": 2.5,
        },
      });
      onReady?.();
    });

    instance.on("click", (event) => {
      onPick(event.lngLat.lat, event.lngLat.lng);
    });

    instance.on("mousemove", CELL_FILL, (event) => {
      const feature = event.features?.[0];
      if (!feature || !popup.current) return;
      instance.getCanvas().style.cursor = "crosshair";
      const props = feature.properties as Record<string, unknown>;
      const value = props.v === null || props.v === undefined ? "no value" : String(props.v);
      popup.current
        .setLngLat(event.lngLat)
        .setHTML(
          `<div style="font:11px ui-monospace,monospace;background:#0d1117;color:#e6edf3;
            padding:6px 8px;border:1px solid rgba(255,255,255,.12);border-radius:4px">
            <div style="color:#f5b301">${props.band ?? "Unscored"}</div>
            <div>value ${value}</div>
            <div style="color:#8b949e">confidence ${props.conf ?? "—"}</div>
            <div style="color:#6e7681">${props.h3 ?? ""}</div>
          </div>`,
        )
        .addTo(instance);
    });

    instance.on("mouseleave", CELL_FILL, () => {
      instance.getCanvas().style.cursor = "";
      popup.current?.remove();
    });

    return () => {
      resize.disconnect();
      instance.remove();
      map.current = null;
      loaded.current = false;
    };
    // Deliberately created once; subsequent prop changes are applied by the
    // effects below rather than by rebuilding the map.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [municipalityId]);

  // --- cell layer data ----------------------------------------------------
  useEffect(() => {
    const instance = map.current;
    if (!instance || !loaded.current) return;
    const source = instance.getSource("cells") as maplibregl.GeoJSONSource | undefined;
    if (!source) return;
    source.setData(
      cellUrl(municipalityId, cellValue, cellMetric, date, topPrioritiesOnly) as unknown as never,
    );
    // A raw metric is not a 0..1 score, so switch to a relative ramp for it.
    if (instance.getLayer(CELL_FILL)) {
      instance.setPaintProperty(
        CELL_FILL,
        "fill-color",
        cellMetric ? metricColorExpression() : scoreColorExpression("v"),
      );
    }
  }, [municipalityId, cellValue, cellMetric, date, topPrioritiesOnly]);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !loaded.current || !instance.getLayer(CELL_FILL)) return;
    instance.setPaintProperty(CELL_FILL, "fill-opacity", cellOpacity);
    instance.setPaintProperty(CELL_LINE, "line-opacity", cellOpacity * 0.35);
  }, [cellOpacity]);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !loaded.current || !instance.getLayer(HILLSHADE)) return;
    instance.setLayoutProperty(HILLSHADE, "visibility", hillshade ? "visible" : "none");
  }, [hillshade]);

  // --- feature layers -----------------------------------------------------
  useEffect(() => {
    const instance = map.current;
    if (!instance || !loaded.current) return;

    for (const spec of FEATURE_LAYERS) {
      const wanted = visibleFeatures[spec.id];
      const exists = Boolean(instance.getSource(featureSourceId(spec.id)));
      if (wanted && !exists) {
        addFeatureLayer(instance, municipalityId, spec, date);
      } else if (exists) {
        for (const layerId of featureLayerIds(spec)) {
          if (instance.getLayer(layerId)) {
            instance.setLayoutProperty(layerId, "visibility", wanted ? "visible" : "none");
          }
        }
      }
    }
  }, [municipalityId, visibleFeatures, date]);

  // Hotspots are time-aware; refresh their data when the date changes.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !loaded.current) return;
    const source = instance.getSource(featureSourceId("satellite_hotspot")) as
      | maplibregl.GeoJSONSource
      | undefined;
    if (source) {
      source.setData(
        featureUrl(municipalityId, "satellite_hotspot", 4000) as unknown as never,
      );
    }
  }, [municipalityId, date]);

  // --- WMS overlays -------------------------------------------------------
  useEffect(() => {
    const instance = map.current;
    if (!instance || !loaded.current) return;

    for (const overlay of overlays) {
      const sourceId = `wms-${overlay.source_id}-${overlay.name}`;
      const wanted = visibleOverlays[overlay.name];
      if (wanted && !instance.getSource(sourceId)) {
        instance.addSource(sourceId, {
          type: "raster",
          tiles: [wmsTileUrl(overlay)],
          tileSize: 256,
          attribution: overlay.attribution || undefined,
        });
        instance.addLayer(
          {
            id: sourceId,
            type: "raster",
            source: sourceId,
            paint: { "raster-opacity": 0.55 },
          },
          instance.getLayer(CELL_FILL) ? CELL_FILL : undefined,
        );
      } else if (instance.getLayer(sourceId)) {
        instance.setLayoutProperty(sourceId, "visibility", wanted ? "visible" : "none");
      }
    }
  }, [overlays, visibleOverlays]);

  // --- selection marker ---------------------------------------------------
  useEffect(() => {
    const instance = map.current;
    if (!instance || !loaded.current) return;
    const source = instance.getSource(SELECTED_SOURCE) as maplibregl.GeoJSONSource | undefined;
    if (!source) return;
    source.setData({
      type: "FeatureCollection",
      features: selected
        ? [
            {
              type: "Feature",
              geometry: { type: "Point", coordinates: [selected.lon, selected.lat] },
              properties: {},
            },
          ]
        : [],
    });
  }, [selected]);

  return <div ref={container} className="absolute inset-0" />;
}

// --- helpers --------------------------------------------------------------

function cellUrl(
  municipalityId: string,
  cellValue: string,
  cellMetric: string | null,
  date: string | null,
  topPrioritiesOnly: boolean,
): string {
  const base = `${API_BASE}/api/municipalities/${municipalityId}`;
  const params = new URLSearchParams();
  if (date) params.set("date", date);
  if (cellMetric) {
    params.set("metric", cellMetric);
    return `${base}/cells/metric?${params}`;
  }
  params.set("value", cellValue);
  if (topPrioritiesOnly) {
    params.set("min_overall_priority", String(TOP_PRIORITY_MIN));
  }
  return `${base}/cells?${params}`;
}

function featureUrl(municipalityId: string, kind: string, limit?: number): string {
  const params = new URLSearchParams({ kind });
  if (limit) params.set("limit", String(limit));
  return `${API_BASE}/api/municipalities/${municipalityId}/features?${params}`;
}

function wmsTileUrl(overlay: WmsOverlay): string {
  const params = new URLSearchParams({
    service: "WMS",
    version: "1.1.1",
    request: "GetMap",
    layers: overlay.name,
    styles: "",
    format: "image/png",
    transparent: "true",
    srs: "EPSG:3857",
    width: "256",
    height: "256",
  });
  return `${overlay.wms_url}?${params}&bbox={bbox-epsg-3857}`;
}

/** The basemap's first label layer, so data can be inserted beneath it. */
function firstSymbolLayer(instance: maplibregl.Map): string | undefined {
  return instance
    .getStyle()
    .layers?.find((layer) => layer.type === "symbol")?.id;
}

async function addBoundary(
  instance: maplibregl.Map,
  municipalityId: string,
  beforeId?: string,
) {
  const url = `${API_BASE}/api/municipalities/${municipalityId}/boundary`;
  instance.addSource("boundary", { type: "geojson", data: url });
  instance.addLayer(
    {
      id: "boundary-envelope",
      type: "line",
      source: "boundary",
      filter: ["==", ["get", "kind"], "analysis_envelope"],
      paint: {
        "line-color": "#3f4653",
        "line-width": 1,
        "line-dasharray": [3, 3],
      },
    },
    beforeId,
  );
  instance.addLayer(
    {
      id: "boundary-legal",
      type: "line",
      source: "boundary",
      filter: ["==", ["get", "kind"], "legal_boundary"],
      paint: { "line-color": "#f5b301", "line-width": 1.8 },
    },
    beforeId,
  );

  // Fit to the legal boundary rather than a hard-coded centre.
  try {
    const response = await fetch(url);
    const collection = await response.json();
    const legal = collection.features?.find(
      (f: GeoJSON.Feature) => f.properties?.kind === "legal_boundary",
    );
    if (legal) {
      const bounds = new maplibregl.LngLatBounds();
      const walk = (coords: unknown): void => {
        if (Array.isArray(coords) && typeof coords[0] === "number") {
          bounds.extend(coords as [number, number]);
        } else if (Array.isArray(coords)) {
          coords.forEach(walk);
        }
      };
      walk((legal.geometry as GeoJSON.Polygon).coordinates);
      instance.fitBounds(bounds, { padding: 48, duration: 0 });
    }
  } catch {
    /* keep the default view */
  }
}

function metricColorExpression() {
  // Raw metrics are unbounded, so colour by rank-free quantile-ish breaks that
  // still read as "more is more" without implying a 0..1 score.
  return [
    "interpolate",
    ["linear"],
    ["coalesce", ["get", "v"], -1],
    -1,
    "#30363d",
    0,
    "#1e3a5f",
    1,
    "#2b7a78",
    10,
    "#d9b310",
    100,
    "#e07b39",
    1000,
    "#c53030",
  ] as unknown as maplibregl.ExpressionSpecification;
}

function addCellLayers(
  instance: maplibregl.Map,
  municipalityId: string,
  cellValue: string,
  date: string | null,
  opacity: number,
  topPrioritiesOnly: boolean,
  beforeId?: string,
) {
  instance.addSource("cells", {
    type: "geojson",
    data: cellUrl(municipalityId, cellValue, null, date, topPrioritiesOnly),
  });
  instance.addLayer(
    {
      id: CELL_FILL,
      type: "fill",
      source: "cells",
      paint: {
        "fill-color": scoreColorExpression("v"),
        "fill-opacity": opacity,
      },
    },
    beforeId,
  );
  instance.addLayer(
    {
      id: CELL_LINE,
      type: "line",
      source: "cells",
      paint: {
        "line-color": "#0b0e14",
        "line-width": 0.3,
        "line-opacity": opacity * 0.35,
      },
    },
    beforeId,
  );
}

const featureSourceId = (id: string) => `feat-${id}`;

function featureLayerIds(spec: FeatureLayerSpec): string[] {
  const base = featureSourceId(spec.id);
  return spec.geometry === "polygon" ? [`${base}-fill`, `${base}-line`] : [base];
}

function addFeatureLayer(
  instance: maplibregl.Map,
  municipalityId: string,
  spec: FeatureLayerSpec,
  date: string | null,
) {
  const sourceId = featureSourceId(spec.id);
  const beforeId = firstSymbolLayer(instance);
  instance.addSource(sourceId, {
    type: "geojson",
    data: featureUrl(municipalityId, spec.kind, spec.limit),
  });

  if (spec.geometry === "point") {
    instance.addLayer(
      {
        id: sourceId,
        type: "circle",
        source: sourceId,
        paint: {
          "circle-radius": spec.sizeProperty
            ? ([
                "interpolate",
                ["linear"],
                ["coalesce", ["to-number", ["get", spec.sizeProperty]], 0],
                0,
                3,
                5,
                5,
                50,
                9,
              ] as unknown as maplibregl.ExpressionSpecification)
            : 4,
          "circle-color": spec.color,
          "circle-opacity": 0.85,
          "circle-stroke-color": "#0b0e14",
          "circle-stroke-width": 0.8,
        },
      },
      beforeId,
    );
  } else if (spec.geometry === "line") {
    instance.addLayer(
      {
        id: sourceId,
        type: "line",
        source: sourceId,
        paint: { "line-color": spec.color, "line-width": 0.8, "line-opacity": 0.7 },
      },
      beforeId,
    );
  } else {
    instance.addLayer(
      {
        id: `${sourceId}-fill`,
        type: "fill",
        source: sourceId,
        paint: { "fill-color": spec.color, "fill-opacity": 0.3 },
      },
      beforeId,
    );
    instance.addLayer(
      {
        id: `${sourceId}-line`,
        type: "line",
        source: sourceId,
        paint: { "line-color": spec.color, "line-width": 0.6, "line-opacity": 0.8 },
      },
      beforeId,
    );
  }
}
