"""Historical validation.

    python -m firewatch.backtest --municipality west-vancouver --date 2023-08-18

Reconstructs what was knowable on a chosen date and reports the priority
distribution, the fire activity around that date, the top-ranked cells and, most
importantly, what data were missing.

The point is not to show that the model is right. It is to find out where it is
wrong and what it could not see. Tuning the model against West Vancouver
incidents alone would produce an overfit that fails everywhere else, so this
command reports rather than optimises.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from firewatch.core.db import session_scope
from firewatch.core.derive import derive_all
from firewatch.core.health.report import coverage_summary, dataset_health
from firewatch.core.models import Municipality
from firewatch.core.municipality import load_municipality
from firewatch.jobs.ingest import build_context, ingest_municipality
from firewatch.jobs.score import score_municipality

log = logging.getLogger("firewatch.backtest")

#: Sources worth re-fetching for a historical date. Static municipal layers do
#: not change usefully with the date, so they are not re-pulled.
HISTORICAL_SOURCES = (
    "cwfis_hotspots",
    "cwfis_fire_weather_stations",
    "eccc_hourly_observations",
    "nasa_firms",
)


def run_backtest(
    municipality_id: str,
    date: datetime,
    refetch: bool = True,
    top_n: int = 10,
) -> dict:
    config = load_municipality(municipality_id)
    as_of_date = date.date().isoformat()

    with session_scope() as session:
        municipality = session.get(Municipality, municipality_id)
        if municipality is None:
            raise SystemExit(
                f"{municipality_id} has not been ingested. Run "
                f"'python -m firewatch run -m {municipality_id}' first."
            )

        fetched: dict = {}
        if refetch:
            available = {s.id for s in config.enabled_sources()}
            targets = [s for s in HISTORICAL_SOURCES if s in available]
            log.info("re-fetching %s for %s", targets, as_of_date)
            fetched = ingest_municipality(
                session, municipality_id, only=targets, as_of=date, skip_boundary=True
            ).get("sources", {})

        ctx = build_context(municipality, config, as_of=date)
        derivation = derive_all(session, municipality, config, ctx, as_of=date)
        scores = score_municipality(session, municipality, as_of=date)

        # --- what was actually knowable on that date ---
        availability = session.execute(
            text(
                """
                SELECT d.source_id, d.feature_kind, count(f.id) AS held,
                       count(f.id) FILTER (WHERE f.observed_at <= :end) AS by_date,
                       min(f.observed_at) AS earliest, max(f.observed_at) AS latest
                  FROM datasets d
                  LEFT JOIN features f ON f.dataset_id = d.id
                 WHERE d.municipality_id = :m
                 GROUP BY d.source_id, d.feature_kind
                 ORDER BY d.source_id
                """
            ),
            {"m": municipality_id, "end": date},
        ).all()

        window_start = date - timedelta(days=14)
        fire_activity = session.execute(
            text(
                """
                SELECT d.source_id, count(*) AS n,
                       min(f.observed_at) AS first_seen, max(f.observed_at) AS last_seen
                  FROM features f JOIN datasets d ON d.id = f.dataset_id
                 WHERE f.municipality_id = :m
                   AND f.feature_kind IN ('satellite_hotspot', 'fire_event',
                                          'fire_perimeter')
                   AND f.observed_at BETWEEN :start AND :end
                 GROUP BY d.source_id
                """
            ),
            {"m": municipality_id, "start": window_start, "end": date},
        ).all()

        distribution = session.execute(
            text(
                """
                SELECT explanation->>'band' AS band, count(*),
                       round(avg(overall_priority)::numeric, 4)
                  FROM priority_scores p
                  JOIN analysis_cells c ON c.id = p.cell_id
                 WHERE p.municipality_id = :m AND p.as_of_date = :d
                   AND c.within_boundary
                 GROUP BY band ORDER BY 2 DESC
                """
            ),
            {"m": municipality_id, "d": as_of_date},
        ).all()

        top_cells = session.execute(
            text(
                """
                SELECT c.h3_index, c.centroid_lat, c.centroid_lon,
                       round(p.overall_priority::numeric, 4),
                       round(p.ignition_likelihood::numeric, 3),
                       round(p.spread_potential::numeric, 3),
                       round(p.consequence_exposure::numeric, 3),
                       round(p.observation_gap::numeric, 3),
                       round(p.access_difficulty_proxy::numeric, 3),
                       round(p.confidence::numeric, 3),
                       round(p.completeness::numeric, 3)
                  FROM priority_scores p
                  JOIN analysis_cells c ON c.id = p.cell_id
                 WHERE p.municipality_id = :m AND p.as_of_date = :d
                   AND c.within_boundary AND p.overall_priority IS NOT NULL
                 ORDER BY p.overall_priority DESC
                 LIMIT :n
                """
            ),
            {"m": municipality_id, "d": as_of_date, "n": top_n},
        ).all()

        health = dataset_health(session, municipality_id)
        coverage = coverage_summary(session, municipality_id)

    return {
        "municipality": municipality_id,
        "date": as_of_date,
        "refetched_sources": fetched,
        "data_available_on_date": [
            {
                "source_id": r[0],
                "feature_kind": r[1],
                "records_held": r[2],
                "records_at_or_before_date": r[3],
                "earliest_observation": r[4].isoformat() if r[4] else None,
                "latest_observation": r[5].isoformat() if r[5] else None,
            }
            for r in availability
        ],
        "fire_activity_14d_before": [
            {
                "source_id": r[0],
                "records": r[1],
                "first_seen": r[2].isoformat() if r[2] else None,
                "last_seen": r[3].isoformat() if r[3] else None,
            }
            for r in fire_activity
        ],
        "priority_distribution": [
            {"band": r[0] or "Unknown", "cells": r[1], "mean_priority": float(r[2])}
            for r in distribution
        ],
        "top_cells": [
            {
                "h3_index": r[0], "lat": r[1], "lon": r[2],
                "overall_priority": float(r[3]),
                "components": {
                    "ignition_likelihood": _f(r[4]),
                    "spread_potential": _f(r[5]),
                    "consequence_exposure": _f(r[6]),
                    "observation_gap": _f(r[7]),
                    "access_difficulty_proxy": _f(r[8]),
                },
                "confidence": _f(r[9]),
                "completeness": _f(r[10]),
            }
            for r in top_cells
        ],
        "missing_data_report": {
            "metrics_with_no_data": derivation.missing_metrics,
            "derivation_notes": derivation.notes,
            "low_coverage_metrics": [
                {
                    "metric": m["metric"],
                    "coverage_percent": m["coverage_percent"],
                }
                for m in coverage["metrics"]
                if m["coverage_percent"] < 90
            ],
            "failed_sources": [
                {"source_id": h["source_id"], "status": h["status"], "message": h["message"]}
                for h in health
                if h["status"] in {"FAILED", "UNAVAILABLE", "STALE"}
            ],
        },
        "score_summary": scores,
        "interpretation_warning": (
            "This reconstruction uses today's static municipal and terrain data with "
            "the fire weather and detections available for the chosen date. It is "
            "not a true point-in-time snapshot: buildings, roads and vegetation "
            "extents are as mapped now, not as they were then."
        ),
    }


def _f(value) -> float | None:
    return None if value is None else float(value)


def print_report(report: dict) -> None:
    print(f"\n{'=' * 78}")
    print(f"BACKTEST  {report['municipality']}  {report['date']}")
    print(f"{'=' * 78}")

    print("\nData available at or before this date")
    print(f"  {'source':<32} {'kind':<26} {'held':>8} {'by date':>8}")
    for record in report["data_available_on_date"]:
        print(
            f"  {record['source_id']:<32} {str(record['feature_kind']):<26} "
            f"{record['records_held']:>8} {record['records_at_or_before_date']:>8}"
        )

    print("\nFire activity in the 14 days before this date")
    if report["fire_activity_14d_before"]:
        for record in report["fire_activity_14d_before"]:
            print(
                f"  {record['source_id']:<32} {record['records']:>6} records, "
                f"last {record['last_seen']}"
            )
    else:
        print("  none recorded")

    print("\nPriority distribution (within boundary)")
    for band in report["priority_distribution"]:
        print(f"  {band['band']:<12} {band['cells']:>6} cells  mean {band['mean_priority']}")

    print(f"\nTop {len(report['top_cells'])} cells")
    print(
        f"  {'h3':<17} {'pri':>6} {'ign':>6} {'spr':>6} {'exp':>6} {'obs':>6} "
        f"{'acc':>6} {'conf':>6}"
    )
    for cell in report["top_cells"]:
        c = cell["components"]
        print(
            f"  {cell['h3_index']:<17} {cell['overall_priority']:>6.3f} "
            f"{_s(c['ignition_likelihood']):>6} {_s(c['spread_potential']):>6} "
            f"{_s(c['consequence_exposure']):>6} {_s(c['observation_gap']):>6} "
            f"{_s(c['access_difficulty_proxy']):>6} {_s(cell['confidence']):>6}"
        )

    missing = report["missing_data_report"]
    print("\nMissing data")
    for metric in missing["metrics_with_no_data"]:
        print(f"  no data at all: {metric}")
    for metric in missing["low_coverage_metrics"]:
        print(f"  low coverage:   {metric['metric']} ({metric['coverage_percent']}%)")
    for source in missing["failed_sources"]:
        print(f"  source {source['status']}: {source['source_id']}")
    for note in missing["derivation_notes"]:
        print(f"  note: {note}")

    print(f"\n{report['interpretation_warning']}\n")


def _s(value) -> str:
    return "-" if value is None else f"{value:.3f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--municipality", "-m", required=True)
    parser.add_argument("--date", "-d", required=True, help="ISO date, e.g. 2023-08-18")
    parser.add_argument(
        "--no-refetch",
        action="store_true",
        help="use only data already stored, do not query sources for the date",
    )
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    date = datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc)
    report = run_backtest(
        args.municipality, date, refetch=not args.no_refetch, top_n=args.top
    )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
