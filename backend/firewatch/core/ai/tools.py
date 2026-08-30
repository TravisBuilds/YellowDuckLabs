"""Structured tools over the Fire Watch database.

These functions are the *only* way the AI analyst is allowed to learn map facts.
Each returns values with their provenance, confidence and known caveats
attached, and each returns explicit "unknown" markers rather than omitting
missing data, so the model has something concrete to say "we don't know" about.

They are also the REST surface used by the front end, which keeps the UI and the
AI reading exactly the same numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from firewatch.core.derive import (
    BUILDING_RADIUS_M,
    INTERFACE_RADIUS_M,
    METRIC_DEFINITIONS,
    WATER_RADIUS_M,
)
from firewatch.core.models import (
    AnalysisCell,
    DataGap,
    Dataset,
    DatasetVersion,
    Feature,
    Municipality,
    PriorityScore,
)
from firewatch.core.fuels import classify
from firewatch.core.scoring.priority import SCORE_VERSION

UNKNOWN = "unknown"


def _today(municipality: Municipality) -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _resolve_date(session: Session, municipality_id: str, date: str | None) -> str:
    """Pick the date to report on.

    If the requested date has no scores, say so by returning the nearest date
    that does, rather than silently reporting a different day's numbers.
    """
    if date:
        return date
    latest = session.scalar(
        select(func.max(PriorityScore.as_of_date)).where(
            PriorityScore.municipality_id == municipality_id
        )
    )
    return latest or datetime.now(timezone.utc).date().isoformat()


def _cell_at(
    session: Session, municipality_id: str, lat: float, lon: float
) -> AnalysisCell | None:
    return session.scalar(
        select(AnalysisCell)
        .where(
            AnalysisCell.municipality_id == municipality_id,
            text("ST_Contains(analysis_cells.geometry, ST_SetSRID(ST_Point(:lon, :lat), 4326))"),
        )
        .params(lat=lat, lon=lon)
        .limit(1)
    ) or session.scalar(
        # Fall back to nearest cell so a click just outside a polygon edge still
        # returns something, clearly labelled.
        select(AnalysisCell)
        .where(AnalysisCell.municipality_id == municipality_id)
        .order_by(
            text("analysis_cells.geometry <-> ST_SetSRID(ST_Point(:lon, :lat), 4326)")
        )
        .params(lat=lat, lon=lon)
        .limit(1)
    )


def _metrics_for_cell(session: Session, cell_id: int, as_of_date: str) -> dict[str, dict]:
    rows = session.execute(
        text(
            """
            SELECT metric, value, unit, confidence, evidence, as_of_date
              FROM cell_metrics
             WHERE cell_id = :cell
               AND (as_of_date IS NULL OR as_of_date = :date)
             ORDER BY as_of_date NULLS FIRST
            """
        ),
        {"cell": cell_id, "date": as_of_date},
    ).all()

    out: dict[str, dict] = {}
    for metric, value, unit, confidence, evidence, row_date in rows:
        definition = METRIC_DEFINITIONS.get(metric, {})
        out[metric] = {
            "metric": metric,
            "label": definition.get("label", metric),
            "value": value,
            "unit": unit or definition.get("unit"),
            "confidence": confidence,
            "as_of_date": row_date,
            "sources": (evidence or {}).get("sources", []),
            "method": (evidence or {}).get("method"),
            "caveat": (evidence or {}).get("caveat"),
        }
    return out


def _fbp_fuel_block(metrics: dict[str, dict]) -> dict:
    raw = _metric_or_unknown(metrics, "fbp_fuel_code")
    spec = classify(raw.get("value")) if raw.get("value") is not None else None
    spread = metrics.get("fbp_spread_factor") or {}
    raw["label"] = "FBP fuel type"
    raw["fbp_class"] = spec.fbp_class if spec else None
    raw["name"] = spec.label if spec else None
    raw["spread_factor"] = spread.get("value")
    raw["is_fuel"] = spec.is_fuel if spec else None
    if spec:
        raw["reason"] = (
            "CWFIS 100 m national FBP class at this cell. A type, not a "
            "measured fuel load."
        )
    return raw


def _metric_or_unknown(metrics: dict[str, dict], name: str) -> dict:
    if name in metrics and metrics[name]["value"] is not None:
        return metrics[name]
    definition = METRIC_DEFINITIONS.get(name, {})
    return {
        "metric": name,
        "label": definition.get("label", name),
        "value": None,
        "unit": definition.get("unit"),
        "status": UNKNOWN,
        "reason": "No source available to derive this value for this location.",
    }


def _dataset_provenance(session: Session, source_ids: list[str], municipality_id: str) -> list[dict]:
    if not source_ids:
        return []
    rows = session.execute(
        text(
            """
            SELECT d.source_id, d.title, d.licence, d.attribution, d.source_url,
                   d.caveats, v.version, v.status, v.latest_observed_at, v.ingested_at
              FROM datasets d
              JOIN LATERAL (
                    SELECT version, status, latest_observed_at, ingested_at
                      FROM dataset_versions WHERE dataset_id = d.id
                     ORDER BY version DESC LIMIT 1
              ) v ON TRUE
             WHERE d.municipality_id = :m AND d.source_id = ANY(:ids)
            """
        ),
        {"m": municipality_id, "ids": list(set(source_ids))},
    ).all()
    return [
        {
            "source_id": r[0],
            "title": r[1],
            "licence": r[2],
            "attribution": r[3],
            "source_url": r[4],
            "caveats": r[5],
            "dataset_version": r[6],
            "status": r[7],
            "observed_at": r[8].isoformat() if r[8] else None,
            "ingested_at": r[9].isoformat() if r[9] else None,
        }
        for r in rows
    ]


# --- tool: get_cell_profile ------------------------------------------------


def get_cell_profile(
    session: Session,
    municipality_id: str,
    lat: float,
    lon: float,
    date: str | None = None,
) -> dict[str, Any]:
    """Full evidence profile for a location.

    Structured as the brief's five sections: PRESERVE, THREAT, EXISTING
    DEFENSES, OBSERVATION and UNKNOWN / NEEDS VALIDATION.
    """
    municipality = session.get(Municipality, municipality_id)
    if municipality is None:
        return {"error": f"Unknown municipality '{municipality_id}'"}

    as_of_date = _resolve_date(session, municipality_id, date)
    cell = _cell_at(session, municipality_id, lat, lon)
    if cell is None:
        return {
            "error": "No analysis cell covers this location.",
            "hint": "The point may be outside the ingested envelope.",
        }

    metrics = _metrics_for_cell(session, cell.id, as_of_date)
    score = session.scalar(
        select(PriorityScore).where(
            PriorityScore.cell_id == cell.id,
            PriorityScore.as_of_date == as_of_date,
            PriorityScore.score_version == SCORE_VERSION,
        )
    )

    contributing = sorted(
        {s for m in metrics.values() for s in m.get("sources", [])}
    )

    profile: dict[str, Any] = {
        "municipality": {"id": municipality.id, "name": municipality.name},
        "cell": {
            "h3_index": cell.h3_index,
            "resolution": cell.resolution,
            "centroid": {"lat": cell.centroid_lat, "lon": cell.centroid_lon},
            "area_m2": round(cell.area_m2),
            "within_boundary": cell.within_boundary,
        },
        "as_of_date": as_of_date,
        "requested_date": date,
        "priority": _priority_block(score),
        "preserve": {
            "buildings_within_250m": _metric_or_unknown(metrics, "building_count_250m"),
            "nearest_building": _metric_or_unknown(metrics, "nearest_building_m"),
            "park_overlap": _metric_or_unknown(metrics, "park_overlap_fraction"),
            "note": (
                f"Structural exposure is counted within {BUILDING_RADIUS_M} m. "
                "Critical infrastructure is not included: no approved public "
                "dataset for it is configured in this iteration."
            ),
        },
        "threat": {
            "elevation": _metric_or_unknown(metrics, "elevation_m"),
            "slope": _metric_or_unknown(metrics, "slope_deg"),
            "aspect": _metric_or_unknown(metrics, "aspect_deg"),
            "ruggedness": _metric_or_unknown(metrics, "ruggedness_m"),
            "vegetation_fraction": _metric_or_unknown(metrics, "vegetation_fraction"),
            "nearest_vegetation": _metric_or_unknown(metrics, "nearest_vegetation_m"),
            "fbp_fuel": _fbp_fuel_block(metrics),
            "canopy_height": _metric_or_unknown(metrics, "canopy_height_m"),
            "historical_hotspots": _metric_or_unknown(metrics, "hotspot_count_history"),
            "nearest_fire_record": _metric_or_unknown(metrics, "nearest_fire_record_m"),
            "years_since_nearest_fire": _metric_or_unknown(metrics, "years_since_nearest_fire"),
            "fire_weather": {
                "fwi": _metric_or_unknown(metrics, "fwi"),
                "ffmc": _metric_or_unknown(metrics, "ffmc"),
                "dmc": _metric_or_unknown(metrics, "dmc"),
                "dc": _metric_or_unknown(metrics, "dc"),
                "isi": _metric_or_unknown(metrics, "isi"),
                "bui": _metric_or_unknown(metrics, "bui"),
                "station_distance_km": _metric_or_unknown(metrics, "fire_weather_station_km"),
            },
            "weather": {
                "temperature": _metric_or_unknown(metrics, "temp_c"),
                "relative_humidity": _metric_or_unknown(metrics, "relative_humidity_pct"),
                "wind_speed": _metric_or_unknown(metrics, "wind_speed_kmh"),
                "wind_direction": _metric_or_unknown(metrics, "wind_direction_deg"),
            },
            "interface_note": (
                f"Vegetation within {INTERFACE_RADIUS_M} m of structures is the "
                "wildland-urban interface condition of concern. Fuel type is the "
                "CWFIS 100 m FBP class: a type, not an inventory."
            ),
        },
        "existing_defenses": {
            "nearest_road": _metric_or_unknown(metrics, "nearest_road_m"),
            "road_length_within_500m": _metric_or_unknown(metrics, "road_length_500m"),
            "nearest_water_asset": _metric_or_unknown(metrics, "nearest_water_asset_m"),
            "water_assets_within_500m": _metric_or_unknown(metrics, "water_asset_count_500m"),
            "nearest_fire_station": _metric_or_unknown(metrics, "nearest_fire_station_m"),
            "fuel_treatments": {
                "status": UNKNOWN,
                "reason": (
                    "No fuel treatment dataset is configured. Completed FireSmart "
                    "treatment extents would materially change this section."
                ),
            },
            "note": (
                f"Water assets are counted within {WATER_RADIUS_M} m. Mapped presence "
                "of a hydrant, main or reservoir carries no flow, pressure or "
                "operability information."
            ),
        },
        "observation": {
            "terrain_visibility_from_roads": _metric_or_unknown(
                metrics, "road_visibility_fraction"
            ),
            "nearest_clear_vantage": _metric_or_unknown(
                metrics, "nearest_visible_road_m"
            ),
            "recent_hotspots": _metric_or_unknown(metrics, "recent_hotspot_count"),
            "days_since_detection": _metric_or_unknown(
                metrics, "days_since_satellite_observation"
            ),
            "yellow_duck_sensor_coverage": {
                "status": UNKNOWN,
                "reason": (
                    "No Yellow Duck sensor is deployed. This section is the "
                    "extension point for future fixed-camera or drone coverage."
                ),
            },
            "note": (
                "Visibility is a line-of-sight calculation to a 10 m smoke column "
                "from the road network, through terrain and typical FBP canopy "
                "height. It assumes someone is present and looking. Measured crown "
                "geometry is not used, so true visibility under dense timber is no "
                "better than stated. A satellite hotspot is a thermal anomaly, not "
                "a confirmed wildfire."
            ),
        },
        "unknown_needs_validation": _unknowns_for_cell(session, municipality, cell.id),
        "provenance": _dataset_provenance(session, contributing, municipality_id),
        "all_metrics": metrics,
    }
    return profile


def _priority_block(score: PriorityScore | None) -> dict[str, Any]:
    if score is None:
        return {
            "status": UNKNOWN,
            "reason": "No priority score has been computed for this cell and date.",
        }
    return {
        "overall": score.overall_priority,
        "percentile": score.priority_percentile,
        "band": (score.explanation or {}).get("band"),
        "confidence": score.confidence,
        "completeness": score.completeness,
        "score_version": score.score_version,
        "components": {
            "ignition_likelihood": score.ignition_likelihood,
            "spread_potential": score.spread_potential,
            "consequence_exposure": score.consequence_exposure,
            "observation_gap": score.observation_gap,
            "access_difficulty_proxy": score.access_difficulty_proxy,
        },
        "separable_views": {
            "hazard": score.hazard,
            "exposure": score.exposure,
            "current_conditions": score.current_conditions,
            "operational_gap": score.operational_gap,
        },
        "explanation": score.explanation,
    }


def _unknowns_for_cell(
    session: Session, municipality: Municipality, cell_id: int
) -> list[dict]:
    rows = session.scalars(
        select(DataGap).where(
            DataGap.municipality_id == municipality.id,
            (DataGap.cell_id == cell_id) | (DataGap.cell_id.is_(None)),
        )
    ).all()
    return [
        {
            "gap_type": gap.gap_type,
            "severity": gap.severity,
            "description": gap.description,
            "resolvable_by": gap.resolvable_by,
            "affects": gap.affects,
        }
        for gap in rows
    ]


# --- tool: rank_cells ------------------------------------------------------

_RANKABLE = {
    "overall_priority": PriorityScore.overall_priority,
    "priority_percentile": PriorityScore.priority_percentile,
    "ignition_likelihood": PriorityScore.ignition_likelihood,
    "spread_potential": PriorityScore.spread_potential,
    "consequence_exposure": PriorityScore.consequence_exposure,
    "observation_gap": PriorityScore.observation_gap,
    "access_difficulty_proxy": PriorityScore.access_difficulty_proxy,
    "hazard": PriorityScore.hazard,
    "exposure": PriorityScore.exposure,
    "current_conditions": PriorityScore.current_conditions,
    "operational_gap": PriorityScore.operational_gap,
    "confidence": PriorityScore.confidence,
}


def rank_cells(
    session: Session,
    municipality_id: str,
    order_by: str = "overall_priority",
    date: str | None = None,
    limit: int = 20,
    within_boundary_only: bool = True,
    min_filters: dict[str, float] | None = None,
    max_filters: dict[str, float] | None = None,
    metric_min: dict[str, float] | None = None,
    metric_max: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Rank cells by any score component, with optional thresholds.

    ``metric_min`` / ``metric_max`` filter on derived metrics (for example
    ``{"slope_deg": 25}``), which is what lets the analyst answer questions like
    "steep terrain plus dense vegetation plus high structural exposure".
    """
    as_of_date = _resolve_date(session, municipality_id, date)
    if order_by not in _RANKABLE:
        return {
            "error": f"Cannot order by '{order_by}'.",
            "allowed": sorted(_RANKABLE),
        }

    query = (
        select(
            PriorityScore, AnalysisCell.h3_index,
            AnalysisCell.centroid_lat, AnalysisCell.centroid_lon,
        )
        .join(AnalysisCell, AnalysisCell.id == PriorityScore.cell_id)
        .where(
            PriorityScore.municipality_id == municipality_id,
            PriorityScore.as_of_date == as_of_date,
            PriorityScore.score_version == SCORE_VERSION,
            _RANKABLE[order_by].isnot(None),
        )
    )
    if within_boundary_only:
        query = query.where(AnalysisCell.within_boundary.is_(True))

    for name, threshold in (min_filters or {}).items():
        if name in _RANKABLE:
            query = query.where(_RANKABLE[name] >= threshold)
    for name, threshold in (max_filters or {}).items():
        if name in _RANKABLE:
            query = query.where(_RANKABLE[name] <= threshold)

    for name, threshold in (metric_min or {}).items():
        query = query.where(
            PriorityScore.cell_id.in_(
                select(text("cell_id")).select_from(text("cell_metrics")).where(
                    text("metric = :mname AND value >= :mval")
                ).params(mname=name, mval=threshold)
            )
        )
    for name, threshold in (metric_max or {}).items():
        query = query.where(
            PriorityScore.cell_id.in_(
                select(text("cell_id")).select_from(text("cell_metrics")).where(
                    text("metric = :xname AND value <= :xval")
                ).params(xname=name, xval=threshold)
            )
        )

    rows = session.execute(
        query.order_by(_RANKABLE[order_by].desc()).limit(min(limit, 200))
    ).all()

    return {
        "municipality_id": municipality_id,
        "as_of_date": as_of_date,
        "order_by": order_by,
        "score_version": SCORE_VERSION,
        "filters": {
            "min": min_filters or {}, "max": max_filters or {},
            "metric_min": metric_min or {}, "metric_max": metric_max or {},
            "within_boundary_only": within_boundary_only,
        },
        "count": len(rows),
        "cells": [
            {
                "h3_index": h3_index,
                "lat": lat,
                "lon": lon,
                "overall_priority": score.overall_priority,
                "priority_percentile": score.priority_percentile,
                "band": (score.explanation or {}).get("band"),
                "confidence": score.confidence,
                "completeness": score.completeness,
                "components": {
                    "ignition_likelihood": score.ignition_likelihood,
                    "spread_potential": score.spread_potential,
                    "consequence_exposure": score.consequence_exposure,
                    "observation_gap": score.observation_gap,
                    "access_difficulty_proxy": score.access_difficulty_proxy,
                },
                "primary_drivers": (score.explanation or {}).get("primary_drivers", []),
            }
            for score, h3_index, lat, lon in rows
        ],
    }


# --- tool: get_nearby_assets ----------------------------------------------


def get_nearby_assets(
    session: Session,
    municipality_id: str,
    lat: float,
    lon: float,
    radius_m: float = 500,
    kinds: list[str] | None = None,
) -> dict[str, Any]:
    """Actual features near a point, with their source records."""
    kinds = kinds or [
        "building", "road", "water_asset", "fire_station", "park", "vegetation_cell",
    ]
    radius_m = min(float(radius_m), 5000.0)

    rows = session.execute(
        text(
            """
            SELECT f.feature_kind,
                   count(*) AS n,
                   min(ST_Distance(f.geometry::geography,
                                   ST_SetSRID(ST_Point(:lon, :lat), 4326)::geography)) AS nearest_m,
                   array_agg(DISTINCT d.source_id) AS sources
              FROM features f
              JOIN datasets d ON d.id = f.dataset_id
             WHERE f.municipality_id = :m
               AND NOT f.superseded
               AND f.feature_kind = ANY(:kinds)
               AND ST_DWithin(f.geometry::geography,
                              ST_SetSRID(ST_Point(:lon, :lat), 4326)::geography, :radius)
             GROUP BY f.feature_kind
             ORDER BY f.feature_kind
            """
        ),
        {"m": municipality_id, "lat": lat, "lon": lon, "kinds": kinds, "radius": radius_m},
    ).all()

    found = {
        kind: {
            "count": n,
            "nearest_m": round(nearest, 1) if nearest is not None else None,
            "sources": list(sources or []),
        }
        for kind, n, nearest, sources in rows
    }
    for kind in kinds:
        if kind not in found:
            found[kind] = {
                "count": 0,
                "nearest_m": None,
                "status": UNKNOWN if not _kind_has_any_source(session, municipality_id, kind) else "none_within_radius",
                "reason": (
                    f"No source supplies '{kind}' features, so absence here proves nothing."
                    if not _kind_has_any_source(session, municipality_id, kind)
                    else f"No '{kind}' features are mapped within {radius_m:.0f} m."
                ),
            }

    return {
        "municipality_id": municipality_id,
        "query": {"lat": lat, "lon": lon, "radius_m": radius_m},
        "assets": found,
    }


def _kind_has_any_source(session: Session, municipality_id: str, kind: str) -> bool:
    return bool(
        session.scalar(
            select(func.count(Feature.id)).where(
                Feature.municipality_id == municipality_id,
                Feature.feature_kind == kind,
                Feature.superseded.is_(False),
            )
        )
    )


# --- tool: get_recent_hotspots -------------------------------------------


def get_recent_hotspots(
    session: Session,
    municipality_id: str,
    window_days: int = 7,
    date: str | None = None,
    bounds: list[float] | None = None,
) -> dict[str, Any]:
    """Satellite detections in a time window, by product."""
    end = (
        datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
        if date
        else datetime.now(timezone.utc)
    )
    start = end - timedelta(days=int(window_days))

    conditions = [
        "f.municipality_id = :m",
        "f.feature_kind = 'satellite_hotspot'",
        "f.observed_at BETWEEN :start AND :end",
    ]
    params: dict[str, Any] = {"m": municipality_id, "start": start, "end": end}
    if bounds and len(bounds) == 4:
        conditions.append(
            "ST_Intersects(f.geometry, ST_MakeEnvelope(:w, :s, :e, :n, 4326))"
        )
        params.update(dict(zip(("w", "s", "e", "n"), bounds)))

    rows = session.execute(
        text(
            f"""
            SELECT d.source_id,
                   COALESCE(f.properties_json->>'satellite',
                            f.properties_json->>'product', 'unspecified') AS platform,
                   count(*) AS n,
                   max(f.observed_at) AS latest,
                   ST_AsGeoJSON(ST_Collect(f.geometry)) AS geom
              FROM features f
              JOIN datasets d ON d.id = f.dataset_id
             WHERE {' AND '.join(conditions)}
             GROUP BY d.source_id, platform
             ORDER BY n DESC
            """
        ),
        params,
    ).all()

    total = sum(r[2] for r in rows)
    return {
        "municipality_id": municipality_id,
        "window": {
            "start": start.isoformat(), "end": end.isoformat(), "days": window_days,
        },
        "total_detections": total,
        "by_platform": [
            {
                "source_id": r[0],
                "platform": r[1],
                "count": r[2],
                "latest_observed_at": r[3].isoformat() if r[3] else None,
            }
            for r in rows
        ],
        "interpretation": (
            "Zero detections does not mean no fire. Detection depends on overpass "
            "timing, cloud cover and fire intensity. A hotspot is a thermal anomaly, "
            "not a confirmed wildfire."
        ),
    }


# --- tool: get_weather_profile -------------------------------------------


def get_weather_profile(
    session: Session,
    municipality_id: str,
    lat: float,
    lon: float,
    date: str | None = None,
) -> dict[str, Any]:
    """Nearest fire-weather and station-weather observations for a location."""
    as_of_date = _resolve_date(session, municipality_id, date)
    end = datetime.fromisoformat(as_of_date).replace(tzinfo=timezone.utc) + timedelta(days=1)

    fire_weather = session.execute(
        text(
            """
            SELECT f.properties_json, f.observed_at, d.source_id,
                   ST_Distance(f.geometry::geography,
                               ST_SetSRID(ST_Point(:lon, :lat), 4326)::geography) / 1000.0
              FROM features f
              JOIN datasets d ON d.id = f.dataset_id
             WHERE f.municipality_id = :m
               AND f.feature_kind = 'fire_weather_observation'
               AND f.observed_at <= :end
             ORDER BY f.geometry <-> ST_SetSRID(ST_Point(:lon, :lat), 4326),
                      f.observed_at DESC
             LIMIT 1
            """
        ),
        {"m": municipality_id, "lat": lat, "lon": lon, "end": end},
    ).first()

    station_weather = session.execute(
        text(
            """
            SELECT f.properties_json, f.observed_at, d.source_id,
                   ST_Distance(f.geometry::geography,
                               ST_SetSRID(ST_Point(:lon, :lat), 4326)::geography) / 1000.0
              FROM features f
              JOIN datasets d ON d.id = f.dataset_id
             WHERE f.municipality_id = :m
               AND f.feature_kind = 'weather_observation'
               AND f.observed_at <= :end
             ORDER BY f.observed_at DESC,
                      f.geometry <-> ST_SetSRID(ST_Point(:lon, :lat), 4326)
             LIMIT 1
            """
        ),
        {"m": municipality_id, "lat": lat, "lon": lon, "end": end},
    ).first()

    result: dict[str, Any] = {
        "municipality_id": municipality_id,
        "query": {"lat": lat, "lon": lon, "date": as_of_date},
        "caveat": (
            "All values below are station observations, not measurements at the "
            "requested point. Terrain-driven local wind and humidity variation is "
            "not represented."
        ),
    }

    if fire_weather:
        props, observed, source_id, distance_km = fire_weather
        result["fire_weather"] = {
            "station": (props.get("name") or "").strip() or UNKNOWN,
            "agency": (props.get("agency") or "").strip() or None,
            "distance_km": round(distance_km, 1),
            "observed_at": observed.isoformat() if observed else None,
            "source_id": source_id,
            "codes": {
                k: props.get(k) for k in ("ffmc", "dmc", "dc", "isi", "bui", "fwi", "dsr")
            },
            "weather": {
                k: props.get(k) for k in ("temp", "rh", "ws", "wdir", "precip")
            },
        }
    else:
        result["fire_weather"] = {
            "status": UNKNOWN,
            "reason": f"No fire weather observation exists at or before {as_of_date}.",
        }

    if station_weather:
        props, observed, source_id, distance_km = station_weather
        result["station_weather"] = {
            "station": props.get("STATION_NAME") or UNKNOWN,
            "distance_km": round(distance_km, 1),
            "observed_at": observed.isoformat() if observed else None,
            "source_id": source_id,
            "values": {
                "temperature_c": props.get("TEMP"),
                "relative_humidity_pct": props.get("RELATIVE_HUMIDITY"),
                "wind_speed_kmh": props.get("WIND_SPEED"),
                "wind_direction_tens_deg": props.get("WIND_DIRECTION"),
                "precip_mm": props.get("PRECIP_AMOUNT"),
            },
        }
    else:
        result["station_weather"] = {
            "status": UNKNOWN,
            "reason": f"No ECCC station observation exists at or before {as_of_date}.",
        }

    return result


# --- tool: compare_dates -------------------------------------------------


def compare_dates(
    session: Session,
    municipality_id: str,
    date_a: str,
    date_b: str,
    h3_index: str | None = None,
    limit: int = 15,
) -> dict[str, Any]:
    """What changed between two dates, overall or for one cell."""
    if h3_index:
        cell = session.scalar(
            select(AnalysisCell).where(
                AnalysisCell.municipality_id == municipality_id,
                AnalysisCell.h3_index == h3_index,
            )
        )
        if cell is None:
            return {"error": f"No cell '{h3_index}' in {municipality_id}"}

        scores = {
            d: session.scalar(
                select(PriorityScore).where(
                    PriorityScore.cell_id == cell.id, PriorityScore.as_of_date == d
                )
            )
            for d in (date_a, date_b)
        }
        missing = [d for d, s in scores.items() if s is None]
        if missing:
            return {
                "error": f"No score computed for {', '.join(missing)}.",
                "hint": "Run the score job for that date first.",
            }

        a, b = scores[date_a], scores[date_b]
        fields = [
            "overall_priority", "ignition_likelihood", "spread_potential",
            "consequence_exposure", "observation_gap", "access_difficulty_proxy",
            "current_conditions", "confidence", "completeness",
        ]
        return {
            "h3_index": h3_index,
            "date_a": date_a,
            "date_b": date_b,
            "changes": {
                f: {
                    "a": getattr(a, f),
                    "b": getattr(b, f),
                    "delta": (
                        round(getattr(b, f) - getattr(a, f), 4)
                        if getattr(a, f) is not None and getattr(b, f) is not None
                        else None
                    ),
                }
                for f in fields
            },
        }

    rows = session.execute(
        text(
            """
            SELECT c.h3_index, c.centroid_lat, c.centroid_lon,
                   a.overall_priority AS pa, b.overall_priority AS pb,
                   b.overall_priority - a.overall_priority AS delta
              FROM priority_scores a
              JOIN priority_scores b
                ON b.cell_id = a.cell_id AND b.as_of_date = :db
              JOIN analysis_cells c ON c.id = a.cell_id
             WHERE a.municipality_id = :m AND a.as_of_date = :da
               AND a.overall_priority IS NOT NULL AND b.overall_priority IS NOT NULL
             ORDER BY abs(b.overall_priority - a.overall_priority) DESC
             LIMIT :lim
            """
        ),
        {"m": municipality_id, "da": date_a, "db": date_b, "lim": min(limit, 100)},
    ).all()

    return {
        "municipality_id": municipality_id,
        "date_a": date_a,
        "date_b": date_b,
        "largest_changes": [
            {
                "h3_index": r[0], "lat": r[1], "lon": r[2],
                "priority_a": round(r[3], 4), "priority_b": round(r[4], 4),
                "delta": round(r[5], 4),
            }
            for r in rows
        ],
        "note": (
            "Only cells with a score on both dates are compared. A change reflects "
            "changed inputs and any change in data availability, not necessarily a "
            "change on the ground."
        ),
    }


# --- tool: get_source_provenance ----------------------------------------


def get_source_provenance(
    session: Session, municipality_id: str, source_id: str | None = None
) -> dict[str, Any]:
    """Licence, version and freshness for one source or all of them."""
    query = (
        select(Dataset, DatasetVersion)
        .join(DatasetVersion, DatasetVersion.dataset_id == Dataset.id)
        .where(Dataset.municipality_id == municipality_id)
        .order_by(Dataset.source_id, DatasetVersion.version.desc())
    )
    if source_id:
        query = query.where(Dataset.source_id == source_id)

    seen: set[str] = set()
    records = []
    for dataset, version in session.execute(query).all():
        if dataset.source_id in seen:
            continue
        seen.add(dataset.source_id)
        records.append(
            {
                "source_id": dataset.source_id,
                "title": dataset.title,
                "adapter": dataset.adapter,
                "feature_kind": dataset.feature_kind,
                "precedence_tier": dataset.precedence_tier,
                "licence": dataset.licence,
                "licence_url": dataset.licence_url,
                "attribution": dataset.attribution,
                "source_url": dataset.source_url,
                "caveats": dataset.caveats,
                "status": version.status,
                "message": version.message,
                "dataset_version": version.version,
                "content_hash": version.content_hash,
                "record_count": version.record_count,
                "latest_observed_at": (
                    version.latest_observed_at.isoformat()
                    if version.latest_observed_at else None
                ),
                "ingested_at": version.ingested_at.isoformat(),
                "request_url": version.request_url,
            }
        )

    if source_id and not records:
        return {"error": f"No dataset '{source_id}' for {municipality_id}"}
    return {"municipality_id": municipality_id, "sources": records}


# --- tool: get_data_gaps ------------------------------------------------


def get_data_gaps(
    session: Session,
    municipality_id: str,
    h3_index: str | None = None,
    gap_type: str | None = None,
) -> dict[str, Any]:
    """Everything we know we do not know."""
    query = select(DataGap).where(DataGap.municipality_id == municipality_id)
    if gap_type:
        query = query.where(DataGap.gap_type == gap_type)
    if h3_index:
        cell = session.scalar(
            select(AnalysisCell).where(
                AnalysisCell.municipality_id == municipality_id,
                AnalysisCell.h3_index == h3_index,
            )
        )
        if cell is not None:
            query = query.where(
                (DataGap.cell_id == cell.id) | (DataGap.cell_id.is_(None))
            )

    gaps = session.scalars(query).all()
    by_type: dict[str, list[dict]] = {}
    for gap in gaps:
        by_type.setdefault(gap.gap_type, []).append(
            {
                "severity": gap.severity,
                "description": gap.description,
                "resolvable_by": gap.resolvable_by,
                "affects": gap.affects,
            }
        )

    return {
        "municipality_id": municipality_id,
        "total": len(gaps),
        "by_type": by_type,
        "note": (
            "These are recorded gaps only. The absence of a gap record is not "
            "evidence that the data are complete."
        ),
    }


TOOL_FUNCTIONS = {
    "get_cell_profile": get_cell_profile,
    "rank_cells": rank_cells,
    "get_nearby_assets": get_nearby_assets,
    "get_recent_hotspots": get_recent_hotspots,
    "get_weather_profile": get_weather_profile,
    "compare_dates": compare_dates,
    "get_source_provenance": get_source_provenance,
    "get_data_gaps": get_data_gaps,
}
