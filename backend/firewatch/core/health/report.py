"""Data-health reporting.

An operational intelligence layer that hides stale data is worse than no layer
at all, so every dataset reports its licence, freshness, coverage and caveats,
and the UI is expected to show it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from firewatch.core.models import Dataset, DatasetVersion, Feature


def _staleness(latest: datetime | None) -> dict[str, Any]:
    if latest is None:
        return {"age_hours": None, "description": "No observation date published"}
    age = datetime.now(timezone.utc) - latest
    hours = age.total_seconds() / 3600.0
    if hours < 48:
        description = f"{hours:.0f} hours old"
    elif age.days < 90:
        description = f"{age.days} days old"
    else:
        description = f"{age.days // 30} months old"
    return {"age_hours": round(hours, 1), "description": description}


def dataset_health(session: Session, municipality_id: str) -> list[dict[str, Any]]:
    """One record per configured dataset, newest version first."""
    # The current picture is the latest *live* version of each dataset. A
    # backtest for a historical date writes its own versions; those must not
    # become the health report of what is knowable today.
    latest_version = (
        select(
            DatasetVersion.dataset_id,
            func.max(DatasetVersion.version).label("version"),
        )
        .where(DatasetVersion.as_of_date.is_(None))
        .group_by(DatasetVersion.dataset_id)
        .subquery()
    )

    rows = session.execute(
        select(Dataset, DatasetVersion)
        .join(latest_version, latest_version.c.dataset_id == Dataset.id)
        .join(
            DatasetVersion,
            (DatasetVersion.dataset_id == Dataset.id)
            & (DatasetVersion.version == latest_version.c.version),
        )
        .where(Dataset.municipality_id == municipality_id)
        .order_by(Dataset.precedence_rank, Dataset.source_id)
    ).all()

    feature_counts = dict(
        session.execute(
            select(Feature.dataset_id, func.count(Feature.id))
            .where(Feature.municipality_id == municipality_id)
            .group_by(Feature.dataset_id)
        ).all()
    )
    active_counts = dict(
        session.execute(
            select(Feature.dataset_id, func.count(Feature.id))
            .where(
                Feature.municipality_id == municipality_id,
                Feature.superseded.is_(False),
            )
            .group_by(Feature.dataset_id)
        ).all()
    )

    out: list[dict[str, Any]] = []
    for dataset, version in rows:
        held = feature_counts.get(dataset.id, 0)
        active = active_counts.get(dataset.id, 0)
        out.append(
            {
                "source_id": dataset.source_id,
                "title": dataset.title,
                "adapter": dataset.adapter,
                "feature_kind": dataset.feature_kind,
                "precedence_tier": dataset.precedence_tier,
                "status": version.status,
                "message": version.message,
                "licence": dataset.licence,
                "licence_url": dataset.licence_url,
                "attribution": dataset.attribution,
                "source_url": dataset.source_url,
                "spatial_resolution": dataset.spatial_resolution,
                "temporal_resolution": dataset.temporal_resolution,
                "known_caveats": dataset.caveats,
                "last_source_update": (
                    version.source_last_updated.isoformat()
                    if version.source_last_updated
                    else None
                ),
                "last_observed_at": (
                    version.latest_observed_at.isoformat()
                    if version.latest_observed_at
                    else None
                ),
                "last_ingested_at": version.ingested_at.isoformat(),
                "staleness": _staleness(version.latest_observed_at),
                "dataset_version": version.version,
                "content_hash": version.content_hash,
                "records_held": held,
                "records_in_use": active,
                "records_superseded": held - active,
                "records_rejected": version.rejected_count,
                "validation_status": (
                    "passed"
                    if version.validation_report.get("ok", True)
                    else "failed"
                ),
                "validation_report": version.validation_report,
            }
        )
    return out


def coverage_summary(session: Session, municipality_id: str) -> dict[str, Any]:
    """How much of the grid actually has each metric.

    ``coverage_percent`` in the brief's data-health model: the share of cells for
    which a value exists.
    """
    total_cells = session.scalar(
        text("SELECT count(*) FROM analysis_cells WHERE municipality_id = :m"),
        {"m": municipality_id},
    ) or 0

    rows = session.execute(
        text(
            """
            SELECT cm.metric,
                   count(DISTINCT cm.cell_id) AS cells,
                   avg(cm.confidence) AS mean_confidence,
                   max(cm.as_of_date) AS latest_date
              FROM cell_metrics cm
              JOIN analysis_cells c ON c.id = cm.cell_id
             WHERE c.municipality_id = :m
             GROUP BY cm.metric
             ORDER BY cm.metric
            """
        ),
        {"m": municipality_id},
    ).all()

    from firewatch.core.derive import METRIC_DEFINITIONS

    metrics = []
    for metric, cells, mean_confidence, latest_date in rows:
        definition = METRIC_DEFINITIONS.get(metric, {})
        metrics.append(
            {
                "metric": metric,
                "label": definition.get("label", metric),
                "group": definition.get("group"),
                "unit": definition.get("unit"),
                "cells_with_value": cells,
                "coverage_percent": round(100.0 * cells / total_cells, 1) if total_cells else 0.0,
                "mean_confidence": round(mean_confidence, 3) if mean_confidence else None,
                "latest_date": latest_date,
                # Some metrics are legitimately undefined for some cells. Saying
                # so beside the percentage stops correct behaviour from reading
                # as a data gap.
                "expected_incomplete": definition.get("expected_incomplete"),
            }
        )

    covered = {m["metric"] for m in metrics}
    missing = [
        {
            "metric": name,
            "label": definition["label"],
            "group": definition["group"],
            "coverage_percent": 0.0,
        }
        for name, definition in METRIC_DEFINITIONS.items()
        if name not in covered
    ]

    return {
        "total_cells": total_cells,
        "metrics": metrics,
        "missing_metrics": missing,
    }


def source_status_summary(session: Session, municipality_id: str) -> dict[str, Any]:
    """Header-grade source counts without scanning the features table."""
    rows = session.execute(
        text(
            """
            SELECT dv.status, d.source_id, d.precedence_tier
              FROM datasets d
              JOIN (
                    SELECT dataset_id, max(version) AS version
                      FROM dataset_versions
                     WHERE as_of_date IS NULL
                     GROUP BY dataset_id
                   ) latest ON latest.dataset_id = d.id
              JOIN dataset_versions dv
                ON dv.dataset_id = d.id AND dv.version = latest.version
             WHERE d.municipality_id = :m
            """
        ),
        {"m": municipality_id},
    ).all()

    counts: dict[str, int] = {}
    failed_sources: list[str] = []
    authoritative_gaps: list[str] = []
    municipal_configured = 0
    municipal_in_use = 0

    for status, source_id, tier in rows:
        counts[status] = counts.get(status, 0) + 1
        if status in {"FAILED", "UNAVAILABLE"}:
            failed_sources.append(source_id)
        if tier == "municipal":
            municipal_configured += 1
            if status in {"FAILED", "UNAVAILABLE"}:
                authoritative_gaps.append(source_id)
            elif status not in {"FAILED", "UNAVAILABLE"}:
                municipal_in_use += 1

    return {
        "counts": counts,
        "failed_sources": failed_sources,
        "authoritative_gaps": authoritative_gaps,
        "municipal_sources_configured": municipal_configured,
        "municipal_sources_in_use": municipal_in_use,
    }


def overall_status(health: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for record in health:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    return {
        "counts": counts,
        "failed_sources": [
            r["source_id"] for r in health if r["status"] in {"FAILED", "UNAVAILABLE"}
        ],
        "authoritative_gaps": [
            r["source_id"]
            for r in health
            if r["precedence_tier"] == "municipal"
            and r["status"] in {"FAILED", "UNAVAILABLE"}
        ],
        # A municipality with no municipal-tier source configured has an empty
        # gap list, which would otherwise read as "nothing missing" when in
        # fact the entire top precedence tier is absent.
        "municipal_sources_configured": sum(
            1 for r in health if r["precedence_tier"] == "municipal"
        ),
        "municipal_sources_in_use": sum(
            1
            for r in health
            if r["precedence_tier"] == "municipal"
            and r["status"] not in {"FAILED", "UNAVAILABLE"}
            and r["records_in_use"] > 0
        ),
    }
