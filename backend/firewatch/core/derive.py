"""Derive per-cell metrics from ingested features.

Everything here is deterministic geospatial computation. No model, no LLM. Each
metric row records the datasets that produced it and a confidence reflecting the
quality of those datasets, so the evidence panel can always answer "how do you
know that".

Metric names are the contract between this module, the scoring module and the
UI. They are stable strings, listed in ``METRIC_DEFINITIONS``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
from geoalchemy2.shape import from_shape, to_shape
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from firewatch.core.geo.grid import approximate_edge_m, generate_grid
from firewatch.core.geo.sightline import (
    DEFAULT_TARGET_HEIGHT_M,
    SightlineEngine,
    metres_per_degree,
    sample_along_lines,
)
from firewatch.core.models import AnalysisCell, CellMetric, DataGap, Dataset, Municipality
from firewatch.core.municipality import MunicipalityConfig
from firewatch.sources.base import IngestContext

log = logging.getLogger(__name__)

# --- radii, in metres -------------------------------------------------------
#: Structural exposure search. Wide enough to capture a cluster of homes around
#: a cell rather than only those it directly touches.
BUILDING_RADIUS_M = 250
#: FireSmart's outermost priority zone. Vegetation inside this distance of a
#: structure is the wildland-urban interface condition the CWRP is concerned with.
INTERFACE_RADIUS_M = 100
WATER_RADIUS_M = 500
ROAD_RADIUS_M = 500
HOTSPOT_HISTORY_RADIUS_M = 1000
RECENT_HOTSPOT_RADIUS_M = 5000

HISTORY_YEARS = 10
RECENT_HOTSPOT_DAYS = 7


METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    "elevation_m": {"unit": "m", "label": "Elevation", "group": "terrain"},
    "slope_deg": {"unit": "deg", "label": "Maximum slope in cell", "group": "terrain"},
    "aspect_deg": {
        "unit": "deg",
        "label": "Aspect (downslope bearing)",
        "group": "terrain",
        # Level ground has no downslope direction, so partial coverage here is
        # correct rather than a gap. Said on the metric so the coverage table
        # explains itself instead of showing a bare percentage.
        "expected_incomplete": (
            "Undefined on level ground, which in the buffered grid is mostly "
            "open water. Partial coverage here is correct, not missing data."
        ),
    },
    "ruggedness_m": {"unit": "m", "label": "Terrain ruggedness index", "group": "terrain"},
    "building_count_250m": {"unit": "count", "label": f"Buildings within {BUILDING_RADIUS_M} m", "group": "exposure"},
    "nearest_building_m": {"unit": "m", "label": "Distance to nearest building", "group": "exposure"},
    "park_overlap_fraction": {"unit": "fraction", "label": "Share of cell in a park", "group": "exposure"},
    "nearest_road_m": {"unit": "m", "label": "Distance to nearest mapped road", "group": "access"},
    "road_length_500m": {"unit": "m", "label": f"Mapped road length within {ROAD_RADIUS_M} m", "group": "access"},
    "nearest_water_asset_m": {"unit": "m", "label": "Distance to nearest mapped water asset", "group": "defenses"},
    "water_asset_count_500m": {"unit": "count", "label": f"Water assets within {WATER_RADIUS_M} m", "group": "defenses"},
    "nearest_fire_station_m": {"unit": "m", "label": "Distance to nearest mapped fire station", "group": "defenses"},
    "vegetation_fraction": {"unit": "fraction", "label": "Share of cell mapped as vegetation", "group": "fuels"},
    "nearest_vegetation_m": {"unit": "m", "label": "Distance to mapped vegetation", "group": "fuels"},
    "fbp_fuel_code": {
        "unit": "code",
        "label": "CWFIS FBP fuel type (majority)",
        "group": "fuels",
    },
    "fbp_spread_factor": {
        "unit": "factor",
        "label": "FBP relative spread potential",
        "group": "fuels",
    },
    "canopy_height_m": {
        "unit": "m",
        "label": "Typical canopy height for the FBP type",
        "group": "fuels",
        "expected_incomplete": (
            "Zero over water, non-fuel and grass. That is a classification, "
            "not a missing measurement."
        ),
    },
    "hotspot_count_history": {"unit": "count", "label": f"Satellite hotspots within {HOTSPOT_HISTORY_RADIUS_M} m ({HISTORY_YEARS} y)", "group": "history"},
    "years_since_nearest_fire": {"unit": "years", "label": "Years since nearest recorded fire", "group": "history"},
    "nearest_fire_record_m": {"unit": "m", "label": "Distance to nearest recorded fire", "group": "history"},
    "fwi": {"unit": "index", "label": "Fire Weather Index", "group": "current"},
    "ffmc": {"unit": "index", "label": "Fine Fuel Moisture Code", "group": "current"},
    "dmc": {"unit": "index", "label": "Duff Moisture Code", "group": "current"},
    "dc": {"unit": "index", "label": "Drought Code", "group": "current"},
    "isi": {"unit": "index", "label": "Initial Spread Index", "group": "current"},
    "bui": {"unit": "index", "label": "Buildup Index", "group": "current"},
    "fire_weather_station_km": {"unit": "km", "label": "Distance to fire weather station used", "group": "current"},
    "wind_speed_kmh": {"unit": "km/h", "label": "Wind speed at station", "group": "current"},
    "wind_direction_deg": {"unit": "deg", "label": "Wind direction at station", "group": "current"},
    "temp_c": {"unit": "C", "label": "Temperature at station", "group": "current"},
    "relative_humidity_pct": {"unit": "%", "label": "Relative humidity at station", "group": "current"},
    "recent_hotspot_count": {"unit": "count", "label": f"Hotspots within {RECENT_HOTSPOT_RADIUS_M} m in {RECENT_HOTSPOT_DAYS} d", "group": "observation"},
    "days_since_satellite_observation": {"unit": "days", "label": "Days since a satellite detection nearby", "group": "observation"},
    "road_visibility_fraction": {"unit": "fraction", "label": "Visibility from the road network", "group": "observation"},
    "nearest_visible_road_m": {
        "unit": "m",
        "label": "Distance to nearest road with a clear view",
        "group": "observation",
        # The absent cells are the finding, not a hole in it: no road within
        # range can see them at all. They are the largest observation gaps in
        # the municipality, and the score treats them as such.
        "expected_incomplete": (
            "Absent where no road within 4 km has a clear line of sight. Those "
            "cells are the most concealed in the municipality, not unmeasured."
        ),
    },
}


@dataclass
class DerivationResult:
    cells: int = 0
    metrics_written: int = 0
    gaps_recorded: int = 0
    notes: list[str] = field(default_factory=list)
    missing_metrics: list[str] = field(default_factory=list)


def _active_sources(session: Session, municipality_id: str, kind: str) -> list[str]:
    """Source ids currently supplying a feature kind, best precedence first."""
    rows = session.execute(
        text(
            """
            SELECT d.source_id
            FROM datasets d
            WHERE d.municipality_id = :m
              AND d.feature_kind = :k
              AND EXISTS (
                  SELECT 1 FROM features f
                  WHERE f.dataset_id = d.id AND NOT f.superseded
              )
            ORDER BY d.precedence_rank
            """
        ),
        {"m": municipality_id, "k": kind},
    ).all()
    return [r[0] for r in rows]


def ensure_grid(
    session: Session, municipality: Municipality, config: MunicipalityConfig
) -> int:
    """Create the H3 grid if the resolution or boundary has changed."""
    existing = session.scalar(
        select(AnalysisCell)
        .where(AnalysisCell.municipality_id == municipality.id)
        .limit(1)
    )
    if existing is not None and existing.resolution == config.analysis.h3_resolution:
        return session.scalar(
            select(text("count(*)")).select_from(AnalysisCell).where(
                AnalysisCell.municipality_id == municipality.id
            )
        ) or 0

    session.execute(
        delete(AnalysisCell).where(AnalysisCell.municipality_id == municipality.id)
    )
    boundary = to_shape(municipality.boundary)
    buffered = to_shape(municipality.boundary_buffered)

    cells = generate_grid(
        boundary_wgs84=boundary,
        resolution=config.analysis.h3_resolution,
        metric_crs=config.analysis.metric_crs,
        buffered_wgs84=buffered,
    )
    for cell in cells:
        session.add(
            AnalysisCell(
                municipality_id=municipality.id,
                h3_index=cell.h3_index,
                resolution=cell.resolution,
                geometry=from_shape(cell.polygon, srid=4326),
                centroid_lat=cell.centroid_lat,
                centroid_lon=cell.centroid_lon,
                area_m2=cell.area_m2,
                within_boundary=cell.within_boundary,
            )
        )
    session.commit()
    log.info(
        "Generated %d cells at H3 resolution %d (~%.0f m edge)",
        len(cells), config.analysis.h3_resolution,
        approximate_edge_m(config.analysis.h3_resolution),
    )
    return len(cells)


def _write_metric_sql(
    session: Session,
    municipality_id: str,
    metric: str,
    value_sql: str,
    *,
    as_of_date: str | None,
    confidence: float,
    evidence: dict,
    unit: str | None = None,
) -> int:
    """Insert or update one metric for every cell, from a single SQL expression.

    ``value_sql`` must be a SELECT producing ``(cell_id, value)``.
    """
    unit = unit or METRIC_DEFINITIONS.get(metric, {}).get("unit")
    statement = text(
        f"""
        INSERT INTO cell_metrics
            (cell_id, metric, value, unit, as_of_date, confidence, evidence, computed_at)
        SELECT src.cell_id, :metric, src.value, :unit, :as_of, :confidence,
               CAST(:evidence AS jsonb), now()
        FROM ({value_sql}) AS src
        ON CONFLICT (cell_id, metric, as_of_date) DO UPDATE
            SET value = EXCLUDED.value,
                confidence = EXCLUDED.confidence,
                evidence = EXCLUDED.evidence,
                computed_at = now()
        """
    )
    import json

    result = session.execute(
        statement,
        {
            "m": municipality_id,
            "metric": metric,
            "unit": unit,
            "as_of": as_of_date,
            "confidence": confidence,
            "evidence": json.dumps(evidence),
        },
    )
    return result.rowcount or 0


# --- terrain ---------------------------------------------------------------


def derive_terrain(
    session: Session,
    municipality: Municipality,
    config: MunicipalityConfig,
    ctx: IngestContext,
) -> tuple[int, list[str]]:
    """Sample the DEM at every cell."""
    from firewatch.sources.dem.adapter import load_terrain_model

    terrain_source = next(
        (s for s in config.enabled_sources() if s.adapter == "terrain_tiles"), None
    )
    if terrain_source is None:
        return 0, ["No terrain source is configured; slope and aspect are unavailable."]

    try:
        model, urls = load_terrain_model(ctx, terrain_source)
    except Exception as exc:
        return 0, [f"Terrain model could not be built: {exc}"]

    cells = session.execute(
        select(AnalysisCell.id, AnalysisCell.centroid_lat, AnalysisCell.centroid_lon)
        .where(AnalysisCell.municipality_id == municipality.id)
    ).all()

    # A resolution-10 cell spans only a few DEM pixels, so sample a window and
    # take the maximum slope: the steep part of a cell is what matters for spread.
    edge_m = approximate_edge_m(config.analysis.h3_resolution)
    radius_px = max(1, int(edge_m / max(1.0, model.pixel_m)))

    evidence = {
        "sources": [terrain_source.id],
        "method": (
            f"Horn gradient on a {model.pixel_m:.1f} m/px DEM, aggregated over a "
            f"{2 * radius_px + 1}px window (~{(2 * radius_px + 1) * model.pixel_m:.0f} m)"
        ),
        "dem_zoom": model.zoom,
        "tile_urls_sample": urls[:3],
    }

    rows = {"elevation_m": [], "slope_deg": [], "aspect_deg": [], "ruggedness_m": []}
    for cell_id, lat, lon in cells:
        sample = model.sample_window(lat, lon, radius_px=radius_px)
        rows["elevation_m"].append((cell_id, sample.elevation_m))
        rows["slope_deg"].append((cell_id, sample.slope_deg))
        rows["aspect_deg"].append((cell_id, sample.aspect_deg))
        rows["ruggedness_m"].append((cell_id, sample.ruggedness_m))

    import json

    written = 0
    for metric, values in rows.items():
        payload = [
            {"cell_id": cid, "value": val} for cid, val in values if val is not None
        ]
        if not payload:
            continue
        session.execute(
            text(
                """
                INSERT INTO cell_metrics
                    (cell_id, metric, value, unit, as_of_date, confidence, evidence, computed_at)
                SELECT (elem->>'cell_id')::int, :metric, (elem->>'value')::float,
                       :unit, NULL, :confidence, CAST(:evidence AS jsonb), now()
                FROM jsonb_array_elements(CAST(:payload AS jsonb)) AS elem
                ON CONFLICT (cell_id, metric, as_of_date) DO UPDATE
                    SET value = EXCLUDED.value,
                        confidence = EXCLUDED.confidence,
                        evidence = EXCLUDED.evidence,
                        computed_at = now()
                """
            ),
            {
                "metric": metric,
                "unit": METRIC_DEFINITIONS[metric]["unit"],
                # Derived from a global composite DEM rather than local LiDAR.
                "confidence": 0.7,
                "evidence": json.dumps(evidence),
                "payload": json.dumps(payload),
            },
        )
        written += len(payload)

    session.commit()

    # Aspect is undefined on level ground, which is correct but shows up in the
    # coverage report as a gap. Over Burrard Inlet and Howe Sound the DEM is
    # uniformly at sea level, so a large share of the buffered grid legitimately
    # has no downslope bearing. Say so, rather than leaving a bare percentage
    # that reads as missing data.
    flat_cells = sum(1 for _, value in rows["aspect_deg"] if value is None)
    notes: list[str] = []
    if flat_cells:
        notes.append(
            f"{flat_cells} of {len(cells)} cells have no aspect because the "
            "terrain there is level, mostly open water in the buffered grid. "
            "Aspect is undefined without a downslope direction; this is not "
            "missing data."
        )

    return written, notes


# --- static spatial metrics ------------------------------------------------


_KNN_DISTANCE = """
    SELECT c.id AS cell_id,
           (SELECT ST_Distance(c.geometry::geography, f.geometry::geography)
              FROM features f
             WHERE f.municipality_id = c.municipality_id
               AND f.feature_kind = '{kind}'
               AND NOT f.superseded
             ORDER BY c.geometry <-> f.geometry
             LIMIT 1) AS value
      FROM analysis_cells c
     WHERE c.municipality_id = :m
"""

_COUNT_WITHIN = """
    SELECT c.id AS cell_id,
           (SELECT count(*)
              FROM features f
             WHERE f.municipality_id = c.municipality_id
               AND f.feature_kind = '{kind}'
               AND NOT f.superseded
               AND ST_DWithin(c.geometry::geography, f.geometry::geography, {radius})
           ) AS value
      FROM analysis_cells c
     WHERE c.municipality_id = :m
"""

_OVERLAP_FRACTION = """
    SELECT c.id AS cell_id,
           COALESCE((
             SELECT LEAST(1.0, SUM(ST_Area(ST_Intersection(c.geometry, f.geometry)::geography))
                                / NULLIF(ST_Area(c.geometry::geography), 0))
               FROM features f
              WHERE f.municipality_id = c.municipality_id
                AND f.feature_kind = '{kind}'
                AND NOT f.superseded
                AND ST_Intersects(c.geometry, f.geometry)
                AND ST_Dimension(f.geometry) = 2
           ), 0) AS value
      FROM analysis_cells c
     WHERE c.municipality_id = :m
"""

_LINE_LENGTH_WITHIN = """
    SELECT c.id AS cell_id,
           COALESCE((
             SELECT SUM(ST_Length(f.geometry::geography))
               FROM features f
              WHERE f.municipality_id = c.municipality_id
                AND f.feature_kind = '{kind}'
                AND NOT f.superseded
                AND ST_DWithin(c.geometry::geography, f.geometry::geography, {radius})
           ), 0) AS value
      FROM analysis_cells c
     WHERE c.municipality_id = :m
"""


#: Plain-language description of each template, for the evidence panel. A user
#: asking "how do you know that" needs the method, not the SQL.
_METHOD_DESCRIPTIONS = {
    _KNN_DISTANCE: (
        "Geodesic distance from the cell to the nearest mapped {kind}, over all "
        "non-superseded records."
    ),
    _COUNT_WITHIN: "Count of mapped {kind} features within {radius} m of the cell.",
    _OVERLAP_FRACTION: (
        "Share of the cell's area covered by mapped {kind} polygons, capped at 100%."
    ),
    _LINE_LENGTH_WITHIN: (
        "Total geodesic length of mapped {kind} lines within {radius} m of the cell."
    ),
}


def _confidence_for(sources: list[str], tier_ranks: dict[str, int]) -> float:
    """Confidence from the precedence of the best contributing source.

    Municipal-authoritative data earn high confidence; community-mapped data are
    explicitly lower, because incompleteness is likely rather than hypothetical.
    """
    if not sources:
        return 0.0
    best = min(tier_ranks.get(s, 6) for s in sources)
    return {1: 0.9, 2: 0.85, 3: 0.8, 4: 0.75, 5: 0.5, 6: 0.4}.get(best, 0.4)


def derive_static_metrics(
    session: Session, municipality: Municipality
) -> tuple[int, list[str]]:
    tier_ranks = {
        row[0]: row[1]
        for row in session.execute(
            select(Dataset.source_id, Dataset.precedence_rank).where(
                Dataset.municipality_id == municipality.id
            )
        ).all()
    }

    written = 0
    notes: list[str] = []

    plan = [
        ("building", "nearest_building_m", _KNN_DISTANCE, {}),
        ("building", "building_count_250m", _COUNT_WITHIN, {"radius": BUILDING_RADIUS_M}),
        ("road", "nearest_road_m", _KNN_DISTANCE, {}),
        ("road", "road_length_500m", _LINE_LENGTH_WITHIN, {"radius": ROAD_RADIUS_M}),
        ("water_asset", "nearest_water_asset_m", _KNN_DISTANCE, {}),
        ("water_asset", "water_asset_count_500m", _COUNT_WITHIN, {"radius": WATER_RADIUS_M}),
        ("fire_station", "nearest_fire_station_m", _KNN_DISTANCE, {}),
        ("park", "park_overlap_fraction", _OVERLAP_FRACTION, {}),
        ("vegetation_cell", "vegetation_fraction", _OVERLAP_FRACTION, {}),
        ("vegetation_cell", "nearest_vegetation_m", _KNN_DISTANCE, {}),
    ]

    for kind, metric, template, params in plan:
        sources = _active_sources(session, municipality.id, kind)
        if not sources:
            notes.append(
                f"No source is supplying '{kind}' features, so '{metric}' could not "
                "be derived."
            )
            continue
        sql = template.format(kind=kind, **params)
        written += _write_metric_sql(
            session,
            municipality.id,
            metric,
            sql,
            as_of_date=None,
            confidence=_confidence_for(sources, tier_ranks),
            evidence={
                "sources": sources,
                "radius_m": params.get("radius"),
                "method": _METHOD_DESCRIPTIONS[template].format(
                    kind=kind.replace("_", " "), radius=params.get("radius")
                ),
            },
        )
        session.commit()

    return written, notes


# --- fire history ---------------------------------------------------------


def derive_history(
    session: Session, municipality: Municipality, as_of: datetime
) -> tuple[int, list[str]]:
    written = 0
    notes: list[str] = []
    horizon = as_of - timedelta(days=365 * HISTORY_YEARS)

    hotspot_sources = _active_sources(session, municipality.id, "satellite_hotspot")
    if hotspot_sources:
        sql = f"""
            SELECT c.id AS cell_id,
                   (SELECT count(*)
                      FROM features f
                     WHERE f.municipality_id = c.municipality_id
                       AND f.feature_kind = 'satellite_hotspot'
                       AND f.observed_at BETWEEN TIMESTAMPTZ '{horizon.isoformat()}'
                                            AND TIMESTAMPTZ '{as_of.isoformat()}'
                       AND ST_DWithin(c.geometry::geography, f.geometry::geography,
                                      {HOTSPOT_HISTORY_RADIUS_M})
                   ) AS value
              FROM analysis_cells c
             WHERE c.municipality_id = :m
        """
        written += _write_metric_sql(
            session, municipality.id, "hotspot_count_history", sql,
            as_of_date=as_of.date().isoformat(), confidence=0.75,
            evidence={
                "sources": hotspot_sources,
                "window": f"{horizon.date().isoformat()} to {as_of.date().isoformat()}",
                "radius_m": HOTSPOT_HISTORY_RADIUS_M,
                "caveat": (
                    "Hotspot detections are a proxy for fire occurrence. They "
                    "over-report industrial heat and under-report small fires."
                ),
            },
        )
        session.commit()
    else:
        notes.append("No satellite hotspot history is available.")

    fire_sources = (
        _active_sources(session, municipality.id, "fire_event")
        + _active_sources(session, municipality.id, "fire_perimeter")
    )
    if fire_sources:
        sql = """
            SELECT c.id AS cell_id,
                   (SELECT ST_Distance(c.geometry::geography, f.geometry::geography)
                      FROM features f
                     WHERE f.municipality_id = c.municipality_id
                       AND f.feature_kind IN ('fire_event', 'fire_perimeter')
                       AND NOT f.superseded
                     ORDER BY c.geometry <-> f.geometry
                     LIMIT 1) AS value
              FROM analysis_cells c
             WHERE c.municipality_id = :m
        """
        written += _write_metric_sql(
            session, municipality.id, "nearest_fire_record_m", sql,
            as_of_date=None, confidence=0.8,
            evidence={
                "sources": fire_sources,
                "caveat": (
                    "Official fire records map larger incidents. Absence of a "
                    "record does not mean no fire occurred."
                ),
            },
        )

        sql_years = f"""
            SELECT c.id AS cell_id,
                   (SELECT EXTRACT(EPOCH FROM (TIMESTAMPTZ '{as_of.isoformat()}'
                                               - f.observed_at)) / 31557600.0
                      FROM features f
                     WHERE f.municipality_id = c.municipality_id
                       AND f.feature_kind IN ('fire_event', 'fire_perimeter')
                       AND NOT f.superseded
                       AND f.observed_at IS NOT NULL
                       AND f.observed_at <= TIMESTAMPTZ '{as_of.isoformat()}'
                     ORDER BY c.geometry <-> f.geometry
                     LIMIT 1) AS value
              FROM analysis_cells c
             WHERE c.municipality_id = :m
        """
        written += _write_metric_sql(
            session, municipality.id, "years_since_nearest_fire", sql_years,
            as_of_date=as_of.date().isoformat(), confidence=0.8,
            evidence={"sources": fire_sources},
        )
        session.commit()
    else:
        notes.append(
            "No official fire event or perimeter records are available for this area."
        )

    return written, notes


# --- current conditions ---------------------------------------------------

#: Fire weather codes we lift verbatim from the nearest CWFIS station.
_FIRE_WEATHER_FIELDS = {
    "fwi": "fwi", "ffmc": "ffmc", "dmc": "dmc",
    "dc": "dc", "isi": "isi", "bui": "bui",
}
_STATION_WEATHER_FIELDS = {
    "wind_speed_kmh": "ws", "wind_direction_deg": "wdir",
    "temp_c": "temp", "relative_humidity_pct": "rh",
}


def derive_current_conditions(
    session: Session, municipality: Municipality, as_of: datetime
) -> tuple[int, list[str]]:
    """Attach the nearest fire-weather station reading to every cell.

    Fire weather is a regional field, so a single nearby station is a defensible
    approximation. The station identity and distance travel with the value, and
    the evidence panel shows both, so nobody mistakes it for a cell measurement.
    """
    written = 0
    notes: list[str] = []
    as_of_date = as_of.date().isoformat()

    sources = _active_sources(session, municipality.id, "fire_weather_observation")
    if not sources:
        return 0, [
            "No fire weather observations are available; current fire-weather "
            "conditions are unknown."
        ]

    # Most recent observation per station at or before as_of.
    session.execute(text("DROP TABLE IF EXISTS tmp_fwx"))
    session.execute(
        text(
            f"""
            CREATE TEMP TABLE tmp_fwx AS
            SELECT DISTINCT ON (f.properties_json->>'aes')
                   f.geometry, f.observed_at, f.properties_json
              FROM features f
             WHERE f.municipality_id = :m
               AND f.feature_kind = 'fire_weather_observation'
               AND f.observed_at <= TIMESTAMPTZ '{as_of.isoformat()}'
             ORDER BY f.properties_json->>'aes', f.observed_at DESC
            """
        ),
        {"m": municipality.id},
    )
    session.execute(text("CREATE INDEX ON tmp_fwx USING GIST (geometry)"))

    station_count = session.scalar(text("SELECT count(*) FROM tmp_fwx")) or 0
    if station_count == 0:
        return 0, [
            f"No fire weather observations exist at or before {as_of_date}; "
            "current conditions for that date are unknown."
        ]

    for metric, field_name in {**_FIRE_WEATHER_FIELDS, **_STATION_WEATHER_FIELDS}.items():
        sql = f"""
            SELECT c.id AS cell_id,
                   (SELECT (s.properties_json->>'{field_name}')::float
                      FROM tmp_fwx s
                     WHERE (s.properties_json->>'{field_name}') IS NOT NULL
                     ORDER BY c.geometry <-> s.geometry
                     LIMIT 1) AS value
              FROM analysis_cells c
             WHERE c.municipality_id = :m
        """
        written += _write_metric_sql(
            session, municipality.id, metric, sql,
            as_of_date=as_of_date,
            # Observed at a station, applied to a cell. Real observation,
            # spatially approximate.
            confidence=0.65,
            evidence={
                "sources": sources,
                "method": "Nearest CWFIS fire weather station with a value for this field",
                "as_of": as_of.isoformat(),
                "caveat": (
                    "Observed at a station, not in this cell. Terrain-driven local "
                    "variation is not represented."
                ),
            },
        )

    # Distance to the station actually used, so the approximation is visible.
    sql_distance = """
        SELECT c.id AS cell_id,
               (SELECT ST_Distance(c.geometry::geography, s.geometry::geography) / 1000.0
                  FROM tmp_fwx s
                 WHERE (s.properties_json->>'fwi') IS NOT NULL
                 ORDER BY c.geometry <-> s.geometry
                 LIMIT 1) AS value
          FROM analysis_cells c
         WHERE c.municipality_id = :m
    """
    written += _write_metric_sql(
        session, municipality.id, "fire_weather_station_km", sql_distance,
        as_of_date=as_of_date, confidence=0.95,
        evidence={"sources": sources, "method": "Great-circle distance to the station used"},
    )
    session.commit()

    # Fall back to ECCC station weather where CWFIS lacks a field.
    eccc_sources = _active_sources(session, municipality.id, "weather_observation")
    if not eccc_sources:
        notes.append(
            "No ECCC station observations are available to corroborate station weather."
        )

    notes.append(
        f"Fire weather derived from {station_count} CWFIS stations reporting at or "
        f"before {as_of_date}."
    )
    return written, notes


def derive_observation(
    session: Session, municipality: Municipality, as_of: datetime
) -> tuple[int, list[str]]:
    """Observation recency, the MVP proxy for observation gap."""
    written = 0
    notes: list[str] = []
    as_of_date = as_of.date().isoformat()
    sources = _active_sources(session, municipality.id, "satellite_hotspot")

    if not sources:
        return 0, [
            "No satellite detection source is reporting, so observation coverage "
            "cannot be assessed at all."
        ]

    recent_start = as_of - timedelta(days=RECENT_HOTSPOT_DAYS)
    sql_recent = f"""
        SELECT c.id AS cell_id,
               (SELECT count(*)
                  FROM features f
                 WHERE f.municipality_id = c.municipality_id
                   AND f.feature_kind = 'satellite_hotspot'
                   AND f.observed_at BETWEEN TIMESTAMPTZ '{recent_start.isoformat()}'
                                        AND TIMESTAMPTZ '{as_of.isoformat()}'
                   AND ST_DWithin(c.geometry::geography, f.geometry::geography,
                                  {RECENT_HOTSPOT_RADIUS_M})
               ) AS value
          FROM analysis_cells c
         WHERE c.municipality_id = :m
    """
    written += _write_metric_sql(
        session, municipality.id, "recent_hotspot_count", sql_recent,
        as_of_date=as_of_date, confidence=0.75,
        evidence={
            "sources": sources,
            "window_days": RECENT_HOTSPOT_DAYS,
            "radius_m": RECENT_HOTSPOT_RADIUS_M,
        },
    )

    sql_age = f"""
        SELECT c.id AS cell_id,
               (SELECT EXTRACT(EPOCH FROM (TIMESTAMPTZ '{as_of.isoformat()}'
                                            - max(f.observed_at))) / 86400.0
                  FROM features f
                 WHERE f.municipality_id = c.municipality_id
                   AND f.feature_kind = 'satellite_hotspot'
                   AND f.observed_at <= TIMESTAMPTZ '{as_of.isoformat()}'
                   AND ST_DWithin(c.geometry::geography, f.geometry::geography,
                                  {RECENT_HOTSPOT_RADIUS_M})
               ) AS value
          FROM analysis_cells c
         WHERE c.municipality_id = :m
    """
    written += _write_metric_sql(
        session, municipality.id, "days_since_satellite_observation", sql_age,
        as_of_date=as_of_date, confidence=0.4,
        evidence={
            "sources": sources,
            "method": (
                "Days since the most recent satellite *detection* within "
                f"{RECENT_HOTSPOT_RADIUS_M} m"
            ),
            "caveat": (
                "This measures time since something was detected, not time since "
                "the area was observed. A satellite may have looked many times and "
                "seen nothing. True revisit intervals require orbital and cloud "
                "coverage analysis, which this iteration does not perform."
            ),
        },
    )
    session.commit()
    notes.append(
        "Observation recency is a detection-based proxy. It is not a satellite "
        "blind-interval calculation."
    )
    return written, notes


# --- visibility ----------------------------------------------------------

#: How far away an observer is still counted. Beyond a few kilometres an
#: early-stage column is a smudge on a hillside, and crediting it as coverage
#: would overstate how well watched a place is.
VISIBILITY_MAX_RANGE_M = 4000.0

#: Even ground spacing for observer points resampled along the road network.
OBSERVER_SPACING_M = 120.0

#: Observers actually ray-traced per cell, nearest first. Enough to distinguish
#: "seen from a whole neighbourhood" from "seen from one bend in one road".
OBSERVERS_PER_CELL = 24


def derive_fuels(
    session: Session,
    municipality: Municipality,
    config: MunicipalityConfig,
    ctx: IngestContext,
) -> tuple[int, list[str]]:
    """Per-cell FBP type, spread factor and typical canopy height."""
    from firewatch.sources.wcs import load_fuel_model

    fuel_source = next(
        (s for s in config.enabled_sources() if s.adapter == "wcs_raster"), None
    )
    if fuel_source is None:
        return 0, [
            "No FBP fuel raster is configured, so spread potential still uses "
            "the vegetation-presence proxy and canopy is not modelled."
        ]

    try:
        model = load_fuel_model(ctx, fuel_source)
    except Exception as exc:
        return 0, [f"FBP fuel grid could not be loaded: {exc}"]

    cells = session.execute(
        select(AnalysisCell.id, AnalysisCell.centroid_lat, AnalysisCell.centroid_lon)
        .where(AnalysisCell.municipality_id == municipality.id)
    ).all()

    codes: list[tuple[int, float]] = []
    spreads: list[tuple[int, float]] = []
    canopies: list[tuple[int, float]] = []
    class_counts: dict[str, int] = {}
    for cell_id, lat, lon in cells:
        spec, spread, canopy = model.sample(lat, lon)
        if spec is None:
            continue
        codes.append((cell_id, float(spec.code)))
        spreads.append((cell_id, round(spread, 4)))
        canopies.append((cell_id, round(canopy, 2)))
        class_counts[spec.fbp_class] = class_counts.get(spec.fbp_class, 0) + 1

    evidence = {
        "sources": [fuel_source.id],
        "method": (
            "Majority FBP class in a 3x3 window of the CWFIS 100 m national "
            "fuel grid. Spread factor and canopy height are typical-for-class, "
            "not measured at the cell."
        ),
        "caveat": (
            "This is a fuel type, not a fuel inventory. Stand age, crown "
            "closure, surface load and ladder fuels are unknown. Canopy height "
            "is a class typical, not LiDAR."
        ),
    }
    written = _write_point_metrics(session, "fbp_fuel_code", codes, evidence, 0.75)
    written += _write_point_metrics(
        session, "fbp_spread_factor", spreads, evidence, 0.7
    )
    written += _write_point_metrics(
        session, "canopy_height_m", canopies, evidence, 0.45
    )
    session.commit()

    ranked = sorted(class_counts.items(), key=lambda kv: -kv[1])
    summary = ", ".join(f"{name}={n}" for name, n in ranked[:6])
    return written, [
        f"FBP fuel type assigned for {len(codes)} cells. Dominant: {summary}."
    ]


def derive_visibility(
    session: Session,
    municipality: Municipality,
    config: MunicipalityConfig,
    ctx: IngestContext,
) -> tuple[int, list[str]]:
    """How visible each cell is from where people actually are.

    This is the part of the observation gap that is genuinely measurable from
    open data: terrain either does or does not block the view from the road
    network to a smoke column above a given location. When the FBP fuel grid
    is available, typical canopy height is added to intervening terrain so a
    forested draw is not treated as open air.
    """
    from firewatch.sources.dem.adapter import load_terrain_model
    from firewatch.sources.wcs import load_fuel_model

    terrain_source = next(
        (s for s in config.enabled_sources() if s.adapter == "terrain_tiles"), None
    )
    if terrain_source is None:
        return 0, [
            "No terrain source is configured, so terrain visibility could not be "
            "computed and the observation gap falls back to detection recency alone."
        ]

    try:
        model, _urls = load_terrain_model(ctx, terrain_source)
    except Exception as exc:
        return 0, [f"Visibility could not be computed; no terrain model: {exc}"]

    canopy_fn = None
    fuel_source_id = None
    fuel_source = next(
        (s for s in config.enabled_sources() if s.adapter == "wcs_raster"), None
    )
    if fuel_source is not None:
        try:
            fuel_model = load_fuel_model(ctx, fuel_source)
            canopy_fn = fuel_model.canopy_at
            fuel_source_id = fuel_source.id
        except Exception:
            canopy_fn = None

    road_rows = session.execute(
        text(
            """
            SELECT ST_AsGeoJSON(ST_Force2D(geometry)) AS gj
              FROM features
             WHERE municipality_id = :m
               AND feature_kind = 'road'
               AND NOT superseded
            """
        ),
        {"m": municipality.id},
    ).all()

    lines: list[list[tuple[float, float]]] = []
    for (gj,) in road_rows:
        geom = json.loads(gj)
        gtype = geom.get("type")
        if gtype == "LineString":
            lines.append([(c[0], c[1]) for c in geom["coordinates"]])
        elif gtype == "MultiLineString":
            lines.extend([(c[0], c[1]) for c in part] for part in geom["coordinates"])

    obs_lat, obs_lon = sample_along_lines(lines, OBSERVER_SPACING_M)
    if obs_lat.size == 0:
        return 0, [
            "No road geometry is available, so there are no observer positions "
            "from which to compute terrain visibility."
        ]

    engine = SightlineEngine(model, canopy_at=canopy_fn)
    cells = session.execute(
        select(AnalysisCell.id, AnalysisCell.centroid_lat, AnalysisCell.centroid_lon)
        .where(AnalysisCell.municipality_id == municipality.id)
    ).all()

    # A coarse spatial bucket index over observers, so each cell ray-traces
    # against its own neighbourhood rather than all of them.
    deg = VISIBILITY_MAX_RANGE_M / 111_000.0
    buckets: dict[tuple[int, int], list[int]] = {}
    for idx in range(obs_lat.size):
        key = (int(obs_lat[idx] / deg), int(obs_lon[idx] / deg))
        buckets.setdefault(key, []).append(idx)

    visibility: list[tuple[int, float]] = []
    visible_distance: list[tuple[int, float]] = []
    no_observer_cells = 0

    for cell_id, lat, lon in cells:
        key_lat, key_lon = int(lat / deg), int(lon / deg)
        candidates: list[int] = []
        for d_lat in (-1, 0, 1):
            for d_lon in (-1, 0, 1):
                candidates.extend(buckets.get((key_lat + d_lat, key_lon + d_lon), ()))
        if not candidates:
            no_observer_cells += 1
            visibility.append((cell_id, 0.0))
            continue

        idx = np.asarray(candidates)
        cand_lat, cand_lon = obs_lat[idx], obs_lon[idx]
        m_lon, m_lat = metres_per_degree(lat)
        dist = np.hypot((cand_lon - lon) * m_lon, (cand_lat - lat) * m_lat)

        keep = dist <= VISIBILITY_MAX_RANGE_M
        if not keep.any():
            no_observer_cells += 1
            visibility.append((cell_id, 0.0))
            continue

        cand_lat, cand_lon, dist = cand_lat[keep], cand_lon[keep], dist[keep]
        if dist.size > OBSERVERS_PER_CELL:
            nearest = np.argpartition(dist, OBSERVERS_PER_CELL)[:OBSERVERS_PER_CELL]
            cand_lat, cand_lon = cand_lat[nearest], cand_lon[nearest]

        result = engine.observability(
            lat, lon, cand_lat, cand_lon, max_range_m=VISIBILITY_MAX_RANGE_M
        )
        if result.weighted_visibility is not None:
            visibility.append((cell_id, result.weighted_visibility))
        if result.nearest_visible_m is not None:
            visible_distance.append((cell_id, result.nearest_visible_m))

    sources = [terrain_source.id] + _active_sources(session, municipality.id, "road")
    if fuel_source_id:
        sources.append(fuel_source_id)
    evidence = {
        "sources": sources,
        "method": (
            f"Line-of-sight against a {model.pixel_m:.1f} m/px DEM from up to "
            f"{OBSERVERS_PER_CELL} road-network observer points within "
            f"{VISIBILITY_MAX_RANGE_M:.0f} m, testing a "
            f"{DEFAULT_TARGET_HEIGHT_M:.0f} m smoke column, weighted by distance."
            + (
                " Intervening FBP-typical canopy height is added to the terrain, "
                "except in a 50 m corridor around each road observer."
                if canopy_fn
                else " Canopy is not modelled: the fuel grid was unavailable."
            )
        ),
        "observer_points": int(obs_lat.size),
        "observer_spacing_m": OBSERVER_SPACING_M,
        "canopy_modelled": bool(canopy_fn),
        "caveat": (
            "Assumes someone is present and looking, in daylight, in clear air, "
            "and that they would recognise and report smoke. Canopy height is a "
            "typical value for the FBP fuel type, not measured crown geometry, "
            "so real visibility under dense timber is still no better than this."
        ),
    }

    vis_confidence = 0.65 if canopy_fn else 0.55
    written = _write_point_metrics(
        session, "road_visibility_fraction", visibility, evidence, vis_confidence
    )
    written += _write_point_metrics(
        session, "nearest_visible_road_m", visible_distance, evidence, vis_confidence
    )
    session.commit()

    notes = [
        f"Terrain visibility computed from {obs_lat.size} road observer points"
        + (" with FBP-typical canopy screening." if canopy_fn else ".")
    ]
    if no_observer_cells:
        notes.append(
            f"{no_observer_cells} cells have no road within "
            f"{VISIBILITY_MAX_RANGE_M / 1000:.0f} km and are treated as unobserved "
            "from the road network."
        )
    return written, notes


def _write_point_metrics(
    session: Session,
    metric: str,
    values: list[tuple[int, float]],
    evidence: dict,
    confidence: float,
) -> int:
    """Bulk-write one metric from an in-memory list of (cell_id, value)."""
    if not values:
        return 0
    payload = [{"cell_id": cid, "value": val} for cid, val in values]
    result = session.execute(
        text(
            """
            INSERT INTO cell_metrics
                (cell_id, metric, value, unit, as_of_date, confidence, evidence, computed_at)
            SELECT (elem->>'cell_id')::int, :metric, (elem->>'value')::float,
                   :unit, NULL, :confidence, CAST(:evidence AS jsonb), now()
            FROM jsonb_array_elements(CAST(:payload AS jsonb)) AS elem
            ON CONFLICT (cell_id, metric, as_of_date) DO UPDATE
                SET value = EXCLUDED.value,
                    confidence = EXCLUDED.confidence,
                    evidence = EXCLUDED.evidence,
                    computed_at = now()
            """
        ),
        {
            "metric": metric,
            "unit": METRIC_DEFINITIONS.get(metric, {}).get("unit"),
            "confidence": confidence,
            "evidence": json.dumps(evidence),
            "payload": json.dumps(payload),
        },
    )
    return result.rowcount or 0


# --- data gaps -----------------------------------------------------------


def record_data_gaps(
    session: Session,
    municipality: Municipality,
    config: MunicipalityConfig,
    notes: list[str],
) -> int:
    """Turn missing inputs and declared unknowns into first-class records."""
    session.execute(
        delete(DataGap).where(DataGap.municipality_id == municipality.id)
    )
    recorded = 0

    for unknown in config.known_unknowns:
        session.add(
            DataGap(
                municipality_id=municipality.id,
                gap_type="operational_unknown",
                severity="high",
                description=unknown,
                resolvable_by="municipal_fire",
                affects=["access_difficulty_proxy", "response"],
            )
        )
        recorded += 1

    # Any metric we intended to compute but could not.
    present = set(
        session.scalars(
            select(CellMetric.metric)
            .join(AnalysisCell, AnalysisCell.id == CellMetric.cell_id)
            .where(AnalysisCell.municipality_id == municipality.id)
            .distinct()
        ).all()
    )
    for metric in sorted(set(METRIC_DEFINITIONS) - present):
        definition = METRIC_DEFINITIONS[metric]
        session.add(
            DataGap(
                municipality_id=municipality.id,
                gap_type="missing_metric",
                severity="medium",
                description=(
                    f"'{definition['label']}' ({metric}) could not be derived for any "
                    "cell, so every score component that depends on it is weaker."
                ),
                resolvable_by="data_source",
                affects=[definition["group"]],
            )
        )
        recorded += 1

    for note in notes:
        session.add(
            DataGap(
                municipality_id=municipality.id,
                gap_type="derivation_note",
                severity="low",
                description=note,
                resolvable_by="data_source",
                affects=[],
            )
        )
        recorded += 1

    # Sources that failed or are unavailable are gaps in their own right.
    failures = session.execute(
        text(
            """
            SELECT d.source_id, d.feature_kind, v.status, v.message
              FROM datasets d
              JOIN LATERAL (
                    SELECT status, message FROM dataset_versions
                     WHERE dataset_id = d.id ORDER BY version DESC LIMIT 1
              ) v ON TRUE
             WHERE d.municipality_id = :m
               AND v.status IN ('FAILED', 'UNAVAILABLE')
            """
        ),
        {"m": municipality.id},
    ).all()
    for source_id, feature_kind, status, message in failures:
        session.add(
            DataGap(
                municipality_id=municipality.id,
                gap_type="source_unavailable",
                severity="high" if status == "FAILED" else "medium",
                description=(
                    f"Source '{source_id}' is {status}: {message or 'no detail'}"
                ),
                resolvable_by="data_source",
                affects=[feature_kind] if feature_kind else [],
            )
        )
        recorded += 1

    session.commit()
    return recorded


def derive_all(
    session: Session,
    municipality: Municipality,
    config: MunicipalityConfig,
    ctx: IngestContext,
    as_of: datetime | None = None,
) -> DerivationResult:
    as_of = as_of or datetime.now(timezone.utc)
    result = DerivationResult()

    result.cells = ensure_grid(session, municipality, config)

    for step in (
        lambda: derive_terrain(session, municipality, config, ctx),
        lambda: derive_static_metrics(session, municipality),
        lambda: derive_history(session, municipality, as_of),
        lambda: derive_current_conditions(session, municipality, as_of),
        lambda: derive_observation(session, municipality, as_of),
        lambda: derive_fuels(session, municipality, config, ctx),
        lambda: derive_visibility(session, municipality, config, ctx),
    ):
        written, notes = step()
        result.metrics_written += written
        result.notes.extend(notes)

    result.gaps_recorded = record_data_gaps(session, municipality, config, result.notes)

    present = set(
        session.scalars(
            select(CellMetric.metric)
            .join(AnalysisCell, AnalysisCell.id == CellMetric.cell_id)
            .where(AnalysisCell.municipality_id == municipality.id)
            .distinct()
        ).all()
    )
    result.missing_metrics = sorted(set(METRIC_DEFINITIONS) - present)
    return result
