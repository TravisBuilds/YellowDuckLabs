"""Detect cells that newly crossed into High priority."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from firewatch.core.alerts.constants import HIGH_PRIORITY_THRESHOLD
from firewatch.core.models import AlertDispatch, PriorityScore
from firewatch.core.scoring.priority import SCORE_VERSION


@dataclass
class NewHighCell:
    h3_index: str
    lat: float
    lon: float
    overall_priority: float
    band: str


def previous_score_date(
    session: Session, municipality_id: str, as_of_date: str, score_version: str
) -> str | None:
    return session.scalar(
        select(func.max(PriorityScore.as_of_date)).where(
            PriorityScore.municipality_id == municipality_id,
            PriorityScore.as_of_date < as_of_date,
            PriorityScore.score_version == score_version,
        )
    )


def newly_high_cells(
    session: Session,
    municipality_id: str,
    as_of_date: str,
    score_version: str = SCORE_VERSION,
    *,
    limit: int = 25,
) -> list[NewHighCell]:
    """Cells inside the legal boundary that just crossed into High or Very high."""
    prev_date = previous_score_date(session, municipality_id, as_of_date, score_version)
    if prev_date is None:
        return []

    rows = session.execute(
        text(
            """
            SELECT c.h3_index,
                   c.centroid_lat,
                   c.centroid_lon,
                   p_now.overall_priority,
                   COALESCE(p_now.explanation->>'band', 'Unknown') AS band
              FROM priority_scores p_now
              JOIN analysis_cells c ON c.id = p_now.cell_id
              LEFT JOIN priority_scores p_prev
                ON p_prev.cell_id = p_now.cell_id
               AND p_prev.municipality_id = p_now.municipality_id
               AND p_prev.as_of_date = :prev_date
               AND p_prev.score_version = p_now.score_version
             WHERE p_now.municipality_id = :m
               AND p_now.as_of_date = :as_of_date
               AND p_now.score_version = :score_version
               AND p_now.overall_priority >= :threshold
               AND c.within_boundary
               AND (
                     p_prev.overall_priority IS NULL
                     OR p_prev.overall_priority < :threshold
                   )
             ORDER BY p_now.overall_priority DESC
             LIMIT :limit
            """
        ),
        {
            "m": municipality_id,
            "as_of_date": as_of_date,
            "prev_date": prev_date,
            "score_version": score_version,
            "threshold": HIGH_PRIORITY_THRESHOLD,
            "limit": limit,
        },
    ).all()

    return [
        NewHighCell(
            h3_index=row[0],
            lat=float(row[1]),
            lon=float(row[2]),
            overall_priority=float(row[3]),
            band=row[4],
        )
        for row in rows
    ]


def count_newly_high_cells(
    session: Session,
    municipality_id: str,
    as_of_date: str,
    score_version: str = SCORE_VERSION,
) -> int:
    prev_date = previous_score_date(session, municipality_id, as_of_date, score_version)
    if prev_date is None:
        return 0

    return int(
        session.scalar(
            text(
                """
                SELECT count(*)
                  FROM priority_scores p_now
                  JOIN analysis_cells c ON c.id = p_now.cell_id
                  LEFT JOIN priority_scores p_prev
                    ON p_prev.cell_id = p_now.cell_id
                   AND p_prev.municipality_id = p_now.municipality_id
                   AND p_prev.as_of_date = :prev_date
                   AND p_prev.score_version = p_now.score_version
                 WHERE p_now.municipality_id = :m
                   AND p_now.as_of_date = :as_of_date
                   AND p_now.score_version = :score_version
                   AND p_now.overall_priority >= :threshold
                   AND c.within_boundary
                   AND (
                         p_prev.overall_priority IS NULL
                         OR p_prev.overall_priority < :threshold
                       )
                """
            ),
            {
                "m": municipality_id,
                "as_of_date": as_of_date,
                "prev_date": prev_date,
                "score_version": score_version,
                "threshold": HIGH_PRIORITY_THRESHOLD,
            },
        )
        or 0
    )


def already_dispatched(
    session: Session, municipality_id: str, as_of_date: str, score_version: str
) -> bool:
    return (
        session.scalar(
            select(AlertDispatch.id).where(
                AlertDispatch.municipality_id == municipality_id,
                AlertDispatch.as_of_date == as_of_date,
                AlertDispatch.score_version == score_version,
            )
        )
        is not None
    )
