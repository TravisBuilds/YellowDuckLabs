"""Municipality metadata, data health and provenance endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from firewatch.core.ai.tools import get_data_gaps, get_source_provenance
from firewatch.core.db import get_session
from firewatch.core.health.report import (
    coverage_summary,
    dataset_health,
    overall_status,
)
from firewatch.core.models import (
    AnalysisCell,
    Dataset,
    Municipality,
    PriorityScore,
    SourceConflict,
)
from firewatch.core.municipality import list_municipalities, load_municipality
from firewatch.core.scoring.priority import (
    COMPONENT_DEFINITIONS,
    PRIORITY_BANDS,
    SCORE_VERSION,
)

router = APIRouter(prefix="/api/municipalities", tags=["municipalities"])


def _require(session: Session, municipality_id: str) -> Municipality:
    municipality = session.get(Municipality, municipality_id)
    if municipality is None:
        configured = list_municipalities()
        raise HTTPException(
            404,
            detail=(
                f"Municipality '{municipality_id}' has not been ingested. "
                f"Configured municipalities: {', '.join(configured) or 'none'}. "
                "Run the ingest job first."
            ),
        )
    return municipality


@router.get("")
def list_all(session: Session = Depends(get_session)) -> dict:
    """Configured municipalities and whether each has been ingested."""
    ingested = {
        m.id: m
        for m in session.scalars(select(Municipality)).all()
    }
    out = []
    for municipality_id in sorted(list_municipalities()):
        try:
            config = load_municipality(municipality_id)
        except Exception as exc:
            out.append({"id": municipality_id, "error": str(exc)})
            continue
        row = ingested.get(municipality_id)
        out.append(
            {
                "id": config.id,
                "name": config.name,
                "short_name": config.short_name,
                "province": config.province,
                "country": config.country,
                "timezone": config.timezone,
                "primary": config.primary,
                "h3_resolution": config.analysis.h3_resolution,
                "source_count": len(config.enabled_sources()),
                "ingested": row is not None,
                "cells": (
                    session.scalar(
                        select(func.count(AnalysisCell.id)).where(
                            AnalysisCell.municipality_id == municipality_id
                        )
                    )
                    if row
                    else 0
                ),
            }
        )
    return {"municipalities": out}


@router.get("/{municipality_id}")
def detail(municipality_id: str, session: Session = Depends(get_session)) -> dict:
    municipality = _require(session, municipality_id)

    cells_total = session.scalar(
        select(func.count(AnalysisCell.id)).where(
            AnalysisCell.municipality_id == municipality_id
        )
    )
    cells_inside = session.scalar(
        select(func.count(AnalysisCell.id)).where(
            AnalysisCell.municipality_id == municipality_id,
            AnalysisCell.within_boundary.is_(True),
        )
    )
    area_km2 = session.scalar(
        text(
            "SELECT ST_Area(boundary::geography) / 1e6 FROM municipalities WHERE id = :m"
        ),
        {"m": municipality_id},
    )
    dates = session.scalars(
        select(PriorityScore.as_of_date)
        .where(PriorityScore.municipality_id == municipality_id)
        .distinct()
        .order_by(PriorityScore.as_of_date.desc())
    ).all()

    return {
        "id": municipality.id,
        "name": municipality.name,
        "short_name": municipality.short_name,
        "province": municipality.province,
        "country": municipality.country,
        "timezone": municipality.timezone,
        "area_km2": round(area_km2, 2) if area_km2 else None,
        "analysis": {
            "h3_resolution": municipality.h3_resolution,
            "metric_crs": municipality.metric_crs,
            "boundary_buffer_m": municipality.boundary_buffer_m,
            "cells_total": cells_total,
            "cells_within_boundary": cells_inside,
        },
        "boundary_source_url": municipality.boundary_source_url,
        "known_unknowns": municipality.known_unknowns,
        "scored_dates": dates,
        "score_version": SCORE_VERSION,
        "component_definitions": COMPONENT_DEFINITIONS,
        "priority_bands": [
            {"min": threshold, "label": label} for threshold, label in PRIORITY_BANDS
        ],
    }


@router.get("/{municipality_id}/summary")
def summary(
    municipality_id: str,
    date: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """The default-screen summary: conditions, priority distribution, freshness."""
    _require(session, municipality_id)

    as_of = date or session.scalar(
        select(func.max(PriorityScore.as_of_date)).where(
            PriorityScore.municipality_id == municipality_id
        )
    )
    if as_of is None:
        return {
            "municipality_id": municipality_id,
            "as_of_date": None,
            "status": "no_scores",
            "message": "No priority scores have been computed yet.",
        }

    stats = session.execute(
        text(
            """
            SELECT count(*) AS n,
                   avg(overall_priority) AS mean_priority,
                   max(overall_priority) AS max_priority,
                   avg(confidence) AS mean_confidence,
                   avg(completeness) AS mean_completeness
              FROM priority_scores p
              JOIN analysis_cells c ON c.id = p.cell_id
             WHERE p.municipality_id = :m AND p.as_of_date = :d
               AND c.within_boundary AND p.overall_priority IS NOT NULL
            """
        ),
        {"m": municipality_id, "d": as_of},
    ).first()

    bands = session.execute(
        text(
            """
            SELECT p.explanation->>'band' AS band, count(*)
              FROM priority_scores p
              JOIN analysis_cells c ON c.id = p.cell_id
             WHERE p.municipality_id = :m AND p.as_of_date = :d AND c.within_boundary
             GROUP BY band ORDER BY count(*) DESC
            """
        ),
        {"m": municipality_id, "d": as_of},
    ).all()

    # Municipality-wide fire weather: the modal nearest-station reading.
    weather = session.execute(
        text(
            """
            SELECT f.properties_json->>'name' AS station,
                   (f.properties_json->>'fwi')::float AS fwi,
                   (f.properties_json->>'ffmc')::float AS ffmc,
                   (f.properties_json->>'dc')::float AS dc,
                   (f.properties_json->>'bui')::float AS bui,
                   (f.properties_json->>'isi')::float AS isi,
                   (f.properties_json->>'temp')::float AS temp,
                   (f.properties_json->>'rh')::float AS rh,
                   (f.properties_json->>'ws')::float AS ws,
                   f.observed_at,
                   ST_Distance(f.geometry::geography,
                               ST_Centroid((SELECT boundary FROM municipalities
                                             WHERE id = :m))::geography) / 1000.0 AS km
              FROM features f
             WHERE f.municipality_id = :m
               AND f.feature_kind = 'fire_weather_observation'
               AND f.observed_at <= (CAST(:d AS date) + interval '1 day')
             ORDER BY km ASC, f.observed_at DESC
             LIMIT 1
            """
        ),
        {"m": municipality_id, "d": as_of},
    ).first()

    hotspots = session.execute(
        text(
            """
            SELECT count(*) FROM features
             WHERE municipality_id = :m AND feature_kind = 'satellite_hotspot'
               AND observed_at > (CAST(:d AS date) - interval '7 days')
               AND observed_at <= (CAST(:d AS date) + interval '1 day')
            """
        ),
        {"m": municipality_id, "d": as_of},
    ).scalar()

    health = dataset_health(session, municipality_id)

    return {
        "municipality_id": municipality_id,
        "as_of_date": as_of,
        "score_version": SCORE_VERSION,
        "priority": {
            "cells_scored": stats[0] if stats else 0,
            "mean": round(stats[1], 4) if stats and stats[1] is not None else None,
            "max": round(stats[2], 4) if stats and stats[2] is not None else None,
            "mean_confidence": round(stats[3], 3) if stats and stats[3] is not None else None,
            "mean_completeness": round(stats[4], 3) if stats and stats[4] is not None else None,
            "bands": {band or "Unknown": count for band, count in bands},
        },
        "fire_weather": (
            {
                "station": (weather[0] or "").strip(),
                "distance_km": round(weather[10], 1) if weather[10] is not None else None,
                "observed_at": weather[9].isoformat() if weather[9] else None,
                "fwi": weather[1], "ffmc": weather[2], "dc": weather[3],
                "bui": weather[4], "isi": weather[5],
                "temp_c": weather[6], "relative_humidity_pct": weather[7],
                "wind_speed_kmh": weather[8],
                "caveat": (
                    "Observed at the nearest reporting fire weather station, not "
                    "within the municipality's terrain."
                ),
            }
            if weather
            else {
                "status": "unknown",
                "reason": "No fire weather observation is available for this date.",
            }
        ),
        "recent_hotspots_7d": hotspots or 0,
        "hotspot_caveat": (
            "A satellite hotspot is a thermal anomaly, not a confirmed wildfire."
        ),
        "data_health": overall_status(health),
    }


@router.get("/{municipality_id}/data-health")
def data_health(municipality_id: str, session: Session = Depends(get_session)) -> dict:
    _require(session, municipality_id)
    health = dataset_health(session, municipality_id)
    return {
        "municipality_id": municipality_id,
        "overall": overall_status(health),
        "datasets": health,
        "coverage": coverage_summary(session, municipality_id),
        "status_meanings": {
            "CURRENT": "Fresh enough for its update cadence.",
            "AGING": "Older than expected but still usable.",
            "STALE": "Too old to rely on.",
            "PARTIAL": "Responded, but coverage is incomplete or empty.",
            "UNKNOWN": "The source publishes no usable observation date.",
            "FAILED": "Could not be retrieved.",
            "UNAVAILABLE": "Not configured, e.g. a missing credential.",
        },
    }


@router.get("/{municipality_id}/data-gaps")
def data_gaps(
    municipality_id: str,
    h3_index: str | None = None,
    gap_type: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    _require(session, municipality_id)
    return get_data_gaps(session, municipality_id, h3_index=h3_index, gap_type=gap_type)


@router.get("/{municipality_id}/provenance")
def provenance(
    municipality_id: str,
    source_id: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    _require(session, municipality_id)
    return get_source_provenance(session, municipality_id, source_id=source_id)


@router.get("/{municipality_id}/conflicts")
def conflicts(municipality_id: str, session: Session = Depends(get_session)) -> dict:
    """Where two sources disagreed and which one won."""
    _require(session, municipality_id)
    names = {
        d.id: d.source_id
        for d in session.scalars(
            select(Dataset).where(Dataset.municipality_id == municipality_id)
        ).all()
    }
    rows = session.scalars(
        select(SourceConflict)
        .where(SourceConflict.municipality_id == municipality_id)
        .order_by(SourceConflict.detected_at.desc())
    ).all()
    return {
        "municipality_id": municipality_id,
        "conflicts": [
            {
                "subject": c.subject,
                "winner": names.get(c.winning_dataset_id),
                "superseded": names.get(c.losing_dataset_id),
                "description": c.description,
                "detail": c.detail,
                "detected_at": c.detected_at.isoformat(),
            }
            for c in rows
        ],
        "precedence_order": [
            "municipality-authoritative", "province-authoritative",
            "federal-authoritative", "remote sensing", "community (OSM)",
            "model-derived",
        ],
    }


@router.get("/{municipality_id}/overlays")
def overlays(municipality_id: str, session: Session = Depends(get_session)) -> dict:
    """WMS overlays available to the map, with attribution."""
    _require(session, municipality_id)
    rows = session.scalars(
        select(Dataset).where(
            Dataset.municipality_id == municipality_id,
            Dataset.adapter == "wms_overlay",
        )
    ).all()
    out = []
    for dataset in rows:
        base_url = dataset.params.get("base_url")
        for layer in dataset.params.get("layers", []):
            out.append(
                {
                    "source_id": dataset.source_id,
                    "name": layer.get("name"),
                    "label": layer.get("label"),
                    "group": layer.get("group"),
                    "wms_url": base_url,
                    "attribution": dataset.attribution,
                    "licence": dataset.licence,
                }
            )
    return {"municipality_id": municipality_id, "overlays": out}


@router.get("/{municipality_id}/dates")
def dates(municipality_id: str, session: Session = Depends(get_session)) -> dict:
    """Dates with computed scores, for the timeline control."""
    _require(session, municipality_id)
    rows = session.execute(
        text(
            """
            SELECT as_of_date, count(*) AS cells, avg(overall_priority) AS mean
              FROM priority_scores
             WHERE municipality_id = :m
             GROUP BY as_of_date ORDER BY as_of_date DESC
            """
        ),
        {"m": municipality_id},
    ).all()
    return {
        "municipality_id": municipality_id,
        "dates": [
            {
                "date": r[0],
                "cells": r[1],
                "mean_priority": round(r[2], 4) if r[2] is not None else None,
            }
            for r in rows
        ],
    }
