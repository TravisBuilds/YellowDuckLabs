"""Compute and persist Fire Watch Priority scores."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from firewatch.core.models import (
    AnalysisCell,
    CellMetric,
    Municipality,
    PriorityScore,
    ScoreComponent,
)
from firewatch.core.scoring.priority import SCORE_VERSION, CellMetrics, score_cell

log = logging.getLogger(__name__)


def load_cell_metrics(
    session: Session, municipality_id: str, as_of_date: str
) -> dict[int, CellMetrics]:
    """Gather metrics per cell for one date.

    Time-invariant metrics (``as_of_date IS NULL``) are combined with the
    date-specific ones, which is what makes historical mode work: change the
    date and the current-conditions layer changes while terrain does not.
    """
    rows = session.execute(
        select(
            CellMetric.cell_id,
            CellMetric.metric,
            CellMetric.value,
            CellMetric.unit,
            CellMetric.confidence,
            CellMetric.evidence,
            CellMetric.as_of_date,
        )
        .join(AnalysisCell, AnalysisCell.id == CellMetric.cell_id)
        .where(
            AnalysisCell.municipality_id == municipality_id,
            (CellMetric.as_of_date.is_(None)) | (CellMetric.as_of_date == as_of_date),
        )
    ).all()

    per_cell: dict[int, CellMetrics] = defaultdict(CellMetrics)
    for cell_id, metric, value, unit, confidence, evidence, row_date in rows:
        bucket = per_cell[cell_id]
        # A date-specific value always wins over a time-invariant one.
        if metric in bucket.values and row_date is None:
            continue
        bucket.values[metric] = value
        bucket.units[metric] = unit
        bucket.confidences[metric] = confidence
        bucket.sources[metric] = list((evidence or {}).get("sources", []))
    return per_cell


def score_municipality(
    session: Session,
    municipality: Municipality,
    as_of: datetime | None = None,
    score_version: str = SCORE_VERSION,
) -> dict:
    as_of = as_of or datetime.now(timezone.utc)
    as_of_date = as_of.date().isoformat()

    cell_ids = session.scalars(
        select(AnalysisCell.id).where(AnalysisCell.municipality_id == municipality.id)
    ).all()
    metrics_by_cell = load_cell_metrics(session, municipality.id, as_of_date)

    # Recompute cleanly for this (date, version) pair.
    existing = session.scalars(
        select(PriorityScore.id).where(
            PriorityScore.municipality_id == municipality.id,
            PriorityScore.as_of_date == as_of_date,
            PriorityScore.score_version == score_version,
        )
    ).all()
    if existing:
        session.execute(
            delete(ScoreComponent).where(ScoreComponent.score_id.in_(existing))
        )
        session.execute(delete(PriorityScore).where(PriorityScore.id.in_(existing)))
        session.commit()

    scored = 0
    unscorable = 0
    distribution: dict[str, int] = defaultdict(int)

    for cell_id in cell_ids:
        metrics = metrics_by_cell.get(cell_id, CellMetrics())
        result = score_cell(metrics, as_of_date)

        if result.overall_priority is None:
            unscorable += 1

        score = PriorityScore(
            municipality_id=municipality.id,
            cell_id=cell_id,
            as_of_date=as_of_date,
            score_version=score_version,
            ignition_likelihood=result.components["ignition_likelihood"].value,
            spread_potential=result.components["spread_potential"].value,
            consequence_exposure=result.components["consequence_exposure"].value,
            observation_gap=result.components["observation_gap"].value,
            access_difficulty_proxy=result.components["access_difficulty_proxy"].value,
            hazard=result.hazard,
            exposure=result.exposure,
            current_conditions=result.current_conditions,
            operational_gap=result.operational_gap,
            overall_priority=result.overall_priority,
            confidence=result.confidence,
            completeness=result.completeness,
            explanation=result.explanation,
        )
        session.add(score)
        session.flush()

        for name, component in result.components.items():
            session.add(
                ScoreComponent(
                    score_id=score.id,
                    component=name,
                    value=component.value,
                    confidence=component.confidence,
                    rationale=component.rationale,
                    inputs_used=[
                        i.metric for s in component.signals for i in s.inputs_used
                    ],
                    inputs_missing=[
                        m for s in component.signals for m in s.inputs_missing
                    ],
                )
            )

        distribution[result.band] += 1
        scored += 1
        if scored % 2000 == 0:
            session.commit()
            log.info("scored %d cells", scored)

    session.commit()
    ranked = _assign_percentiles(session, municipality.id, as_of_date, score_version)

    alert_summary = None
    try:
        from firewatch.core.alerts.process import process_municipality_alerts

        alert_summary = process_municipality_alerts(
            session, municipality.id, as_of_date, score_version
        )
    except Exception:
        log.exception("alert processing failed for %s", municipality.id)

    return {
        "as_of_date": as_of_date,
        "score_version": score_version,
        "cells_scored": scored,
        "cells_without_data": unscorable,
        "cells_ranked": ranked,
        "band_distribution": dict(distribution),
        "alerts": alert_summary,
    }


#: Resolution of the percentile lookup. 1000 buckets is finer than anyone reads
#: off a map and keeps the ranking a single indexed binary search.
_PERCENTILE_BUCKETS = 1000


def _assign_percentiles(
    session: Session, municipality_id: str, as_of_date: str, score_version: str
) -> int:
    """Rank every cell against the municipality's own cells for this date.

    The reference population is deliberately the cells inside the legal
    boundary. Cells in the buffer ring are mostly ocean and uninhabited
    mountainside, and including them would push the whole municipality into the
    top of its own distribution and paint the map red.

    Buffer cells still receive a percentile, expressing where they would fall
    within the municipality's distribution, so they remain comparable.
    """
    thresholds = session.execute(
        text(
            f"""
            SELECT percentile_cont(
                       (SELECT array_agg(i / {_PERCENTILE_BUCKETS}::float)
                          FROM generate_series(1, {_PERCENTILE_BUCKETS} - 1) AS i)
                   ) WITHIN GROUP (ORDER BY p.overall_priority)
              FROM priority_scores p
              JOIN analysis_cells c ON c.id = p.cell_id
             WHERE p.municipality_id = :m
               AND p.as_of_date = :d
               AND p.score_version = :v
               AND p.overall_priority IS NOT NULL
               AND c.within_boundary
            """
        ),
        {"m": municipality_id, "d": as_of_date, "v": score_version},
    ).scalar()

    if not thresholds:
        return 0

    result = session.execute(
        text(
            f"""
            UPDATE priority_scores p
               SET priority_percentile =
                     width_bucket(p.overall_priority, CAST(:bounds AS float[]))
                     / {_PERCENTILE_BUCKETS}::float
             WHERE p.municipality_id = :m
               AND p.as_of_date = :d
               AND p.score_version = :v
               AND p.overall_priority IS NOT NULL
            """
        ),
        {
            "m": municipality_id,
            "d": as_of_date,
            "v": score_version,
            "bounds": list(thresholds),
        },
    )
    session.commit()
    return result.rowcount or 0
