"""Spatial endpoints: boundary, cells, features, evidence profiles."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from firewatch.core.ai.tools import (
    get_cell_profile,
    get_nearby_assets,
    get_recent_hotspots,
    get_weather_profile,
    rank_cells,
)
from firewatch.core.db import get_session
from firewatch.core.derive import METRIC_DEFINITIONS
from firewatch.core.models import AnalysisCell, Municipality, PriorityScore

router = APIRouter(prefix="/api/municipalities", tags=["geo"])

#: Feature kinds the map may request, and a sane cap for each.
FEATURE_LIMITS = {
    "building": 20000,
    "road": 20000,
    "water_asset": 5000,
    "fire_station": 200,
    "park": 2000,
    "vegetation_cell": 5000,
    "satellite_hotspot": 5000,
    "fire_perimeter": 2000,
    "fire_event": 2000,
    "weather_station": 500,
    "fire_weather_observation": 500,
}

#: Score columns the map can colour cells by.
CELL_VALUE_COLUMNS = {
    "overall_priority", "priority_percentile", "ignition_likelihood",
    "spread_potential", "consequence_exposure", "observation_gap",
    "access_difficulty_proxy", "hazard", "exposure", "current_conditions",
    "operational_gap", "confidence", "completeness",
}


def _require(session: Session, municipality_id: str) -> Municipality:
    municipality = session.get(Municipality, municipality_id)
    if municipality is None:
        raise HTTPException(404, detail=f"Municipality '{municipality_id}' not ingested.")
    return municipality


@router.get("/{municipality_id}/boundary")
def boundary(municipality_id: str, session: Session = Depends(get_session)) -> dict:
    _require(session, municipality_id)
    row = session.execute(
        text(
            """
            SELECT ST_AsGeoJSON(boundary, 6), ST_AsGeoJSON(boundary_buffered, 6),
                   boundary_source_url, name
              FROM municipalities WHERE id = :m
            """
        ),
        {"m": municipality_id},
    ).first()
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": json.loads(row[0]),
                "properties": {
                    "kind": "legal_boundary",
                    "name": row[3],
                    "source_url": row[2],
                },
            },
            {
                "type": "Feature",
                "geometry": json.loads(row[1]),
                "properties": {
                    "kind": "analysis_envelope",
                    "note": (
                        "The buffered envelope. Fire does not respect municipal "
                        "boundaries, so terrain and fuel just outside are ingested too."
                    ),
                },
            },
        ],
    }


@router.get("/{municipality_id}/cells")
def cells(
    municipality_id: str,
    date: str | None = None,
    value: str = Query("overall_priority"),
    within_boundary: bool = True,
    min_value: float | None = None,
    min_overall_priority: float | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """Analysis cells as GeoJSON, coloured by one score column.

    Aggregated inside PostGIS so a 10k-cell grid is one round trip.
    """
    _require(session, municipality_id)
    if value not in CELL_VALUE_COLUMNS:
        raise HTTPException(
            400,
            detail=f"'{value}' is not a cell value column. Allowed: {sorted(CELL_VALUE_COLUMNS)}",
        )

    as_of = date or session.scalar(
        select(func.max(PriorityScore.as_of_date)).where(
            PriorityScore.municipality_id == municipality_id
        )
    )
    if as_of is None:
        return {
            "type": "FeatureCollection",
            "features": [],
            "properties": {
                "status": "no_scores",
                "message": "No priority scores computed yet. Run the score job.",
            },
        }

    conditions = ["p.municipality_id = :m", "p.as_of_date = :d"]
    if within_boundary:
        conditions.append("c.within_boundary")
    if min_value is not None:
        conditions.append(f"p.{value} >= :minv")
    if min_overall_priority is not None:
        conditions.append("p.overall_priority >= :minop")

    payload = session.execute(
        text(
            f"""
            SELECT jsonb_build_object(
                     'type', 'FeatureCollection',
                     'features', COALESCE(jsonb_agg(feature), '[]'::jsonb)
                   )
              FROM (
                SELECT jsonb_build_object(
                         'type', 'Feature',
                         'geometry', ST_AsGeoJSON(c.geometry, 5)::jsonb,
                         'properties', jsonb_build_object(
                             'h3', c.h3_index,
                             'lat', round(c.centroid_lat::numeric, 5),
                             'lon', round(c.centroid_lon::numeric, 5),
                             'v', round(p.{value}::numeric, 3),
                             'abs', round(p.overall_priority::numeric, 3),
                             'pct', round(p.priority_percentile::numeric, 3),
                             'band', p.explanation->>'band',
                             'conf', round(p.confidence::numeric, 2),
                             'inside', c.within_boundary
                         )
                       ) AS feature
                  FROM priority_scores p
                  JOIN analysis_cells c ON c.id = p.cell_id
                 WHERE {' AND '.join(conditions)}
              ) AS features
            """
        ),
        {
            "m": municipality_id,
            "d": as_of,
            "minv": min_value,
            "minop": min_overall_priority,
        },
    ).scalar()

    result = payload or {"type": "FeatureCollection", "features": []}
    result["properties"] = {
        "as_of_date": as_of,
        "value_column": value,
        "min_overall_priority": min_overall_priority,
        "count": len(result.get("features", [])),
    }
    return result


@router.get("/{municipality_id}/cells/metric")
def cells_by_metric(
    municipality_id: str,
    metric: str,
    date: str | None = None,
    within_boundary: bool = True,
    session: Session = Depends(get_session),
) -> dict:
    """Cells coloured by a raw derived metric rather than a score component."""
    _require(session, municipality_id)
    if metric not in METRIC_DEFINITIONS:
        raise HTTPException(
            400, detail=f"Unknown metric '{metric}'. Allowed: {sorted(METRIC_DEFINITIONS)}"
        )

    conditions = ["c.municipality_id = :m", "cm.metric = :metric"]
    if within_boundary:
        conditions.append("c.within_boundary")
    if date:
        conditions.append("(cm.as_of_date IS NULL OR cm.as_of_date = :d)")

    payload = session.execute(
        text(
            f"""
            SELECT jsonb_build_object(
                     'type', 'FeatureCollection',
                     'features', COALESCE(jsonb_agg(feature), '[]'::jsonb)
                   )
              FROM (
                SELECT jsonb_build_object(
                         'type', 'Feature',
                         'geometry', ST_AsGeoJSON(c.geometry, 5)::jsonb,
                         'properties', jsonb_build_object(
                             'h3', c.h3_index,
                             'v', round(cm.value::numeric, 3),
                             'unit', cm.unit,
                             'conf', round(cm.confidence::numeric, 2)
                         )
                       ) AS feature
                  FROM cell_metrics cm
                  JOIN analysis_cells c ON c.id = cm.cell_id
                 WHERE {' AND '.join(conditions)}
              ) AS features
            """
        ),
        {"m": municipality_id, "metric": metric, "d": date},
    ).scalar()

    result = payload or {"type": "FeatureCollection", "features": []}
    definition = METRIC_DEFINITIONS[metric]
    result["properties"] = {
        "metric": metric,
        "label": definition["label"],
        "unit": definition["unit"],
        "group": definition["group"],
        "count": len(result.get("features", [])),
    }
    return result


@router.get("/{municipality_id}/features")
def features(
    municipality_id: str,
    kind: str,
    limit: int | None = None,
    include_superseded: bool = False,
    since: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """Ingested features of one kind as GeoJSON, with provenance per feature."""
    _require(session, municipality_id)
    if kind not in FEATURE_LIMITS:
        raise HTTPException(
            400, detail=f"Unknown feature kind '{kind}'. Allowed: {sorted(FEATURE_LIMITS)}"
        )
    cap = min(limit or FEATURE_LIMITS[kind], FEATURE_LIMITS[kind])

    conditions = ["f.municipality_id = :m", "f.feature_kind = :kind"]
    if not include_superseded:
        conditions.append("NOT f.superseded")
    if since:
        conditions.append("f.observed_at >= CAST(:since AS timestamptz)")

    payload = session.execute(
        text(
            f"""
            SELECT jsonb_build_object(
                     'type', 'FeatureCollection',
                     'features', COALESCE(jsonb_agg(feature), '[]'::jsonb)
                   )
              FROM (
                SELECT jsonb_build_object(
                         'type', 'Feature',
                         'geometry', ST_AsGeoJSON(f.geometry, 6)::jsonb,
                         'properties', jsonb_build_object(
                             'source', d.source_id,
                             'record_id', f.source_record_id,
                             'observed_at', f.observed_at,
                             'superseded', f.superseded
                         ) || COALESCE(f.properties_json, '{{}}'::jsonb)
                       ) AS feature
                  FROM features f
                  JOIN datasets d ON d.id = f.dataset_id
                 WHERE {' AND '.join(conditions)}
                 ORDER BY f.observed_at DESC NULLS LAST
                 LIMIT :cap
              ) AS features
            """
        ),
        {"m": municipality_id, "kind": kind, "cap": cap, "since": since},
    ).scalar()

    result = payload or {"type": "FeatureCollection", "features": []}
    total = session.scalar(
        text(
            "SELECT count(*) FROM features WHERE municipality_id = :m "
            "AND feature_kind = :kind AND NOT superseded"
        ),
        {"m": municipality_id, "kind": kind},
    )
    returned = len(result.get("features", []))
    result["properties"] = {
        "kind": kind,
        "returned": returned,
        "total_available": total,
        "truncated": bool(total and returned < total),
    }
    return result


@router.get("/{municipality_id}/profile")
def profile(
    municipality_id: str,
    lat: float,
    lon: float,
    date: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """The location evidence drawer payload."""
    _require(session, municipality_id)
    return get_cell_profile(session, municipality_id, lat, lon, date=date)


@router.get("/{municipality_id}/assets")
def assets(
    municipality_id: str,
    lat: float,
    lon: float,
    radius_m: float = 500,
    session: Session = Depends(get_session),
) -> dict:
    _require(session, municipality_id)
    return get_nearby_assets(session, municipality_id, lat, lon, radius_m=radius_m)


@router.get("/{municipality_id}/weather")
def weather(
    municipality_id: str,
    lat: float,
    lon: float,
    date: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    _require(session, municipality_id)
    return get_weather_profile(session, municipality_id, lat, lon, date=date)


@router.get("/{municipality_id}/hotspots")
def hotspots(
    municipality_id: str,
    window_days: int = 7,
    date: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    _require(session, municipality_id)
    return get_recent_hotspots(
        session, municipality_id, window_days=window_days, date=date
    )


@router.get("/{municipality_id}/rank")
def rank(
    municipality_id: str,
    order_by: str = "overall_priority",
    date: str | None = None,
    limit: int = 20,
    min_slope_deg: float | None = None,
    min_exposure: float | None = None,
    max_history: float | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """Ranked cells, with the filter combinations the brief's questions need."""
    _require(session, municipality_id)
    metric_min: dict[str, float] = {}
    metric_max: dict[str, float] = {}
    min_filters: dict[str, float] = {}

    if min_slope_deg is not None:
        metric_min["slope_deg"] = min_slope_deg
    if min_exposure is not None:
        min_filters["consequence_exposure"] = min_exposure
    if max_history is not None:
        metric_max["hotspot_count_history"] = max_history

    return rank_cells(
        session,
        municipality_id,
        order_by=order_by,
        date=date,
        limit=limit,
        min_filters=min_filters or None,
        metric_min=metric_min or None,
        metric_max=metric_max or None,
    )


@router.get("/{municipality_id}/metrics")
def metrics_catalogue(municipality_id: str, session: Session = Depends(get_session)) -> dict:
    """Which metrics exist and how well covered they are, for layer controls."""
    _require(session, municipality_id)
    rows = session.execute(
        text(
            """
            SELECT cm.metric, count(DISTINCT cm.cell_id), avg(cm.confidence)
              FROM cell_metrics cm
              JOIN analysis_cells c ON c.id = cm.cell_id
             WHERE c.municipality_id = :m
             GROUP BY cm.metric
            """
        ),
        {"m": municipality_id},
    ).all()
    available = {r[0]: (r[1], r[2]) for r in rows}

    return {
        "municipality_id": municipality_id,
        "metrics": [
            {
                "metric": name,
                "label": definition["label"],
                "unit": definition["unit"],
                "group": definition["group"],
                "available": name in available,
                "cells_with_value": available.get(name, (0, None))[0],
                "mean_confidence": (
                    round(available[name][1], 3)
                    if name in available and available[name][1] is not None
                    else None
                ),
            }
            for name, definition in METRIC_DEFINITIONS.items()
        ],
    }
