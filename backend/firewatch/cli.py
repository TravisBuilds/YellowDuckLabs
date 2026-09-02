"""Fire Watch command line.

    python -m firewatch initdb
    python -m firewatch ingest --municipality west-vancouver
    python -m firewatch derive --municipality west-vancouver
    python -m firewatch score  --municipality west-vancouver
    python -m firewatch run    --municipality west-vancouver
    python -m firewatch status --municipality west-vancouver
    python -m firewatch backtest -m west-vancouver --date 2023-08-15
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from firewatch.core.municipality import list_municipalities, load_municipality

log = logging.getLogger("firewatch")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def cmd_initdb(args: argparse.Namespace) -> int:
    from firewatch.core.db import drop_all, init_db

    if args.reset:
        log.warning("dropping all Fire Watch tables")
        drop_all()
    init_db()
    log.info("schema ready")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from firewatch.core.ai.documents import register_documents
    from firewatch.core.db import session_scope
    from firewatch.jobs.ingest import ingest_municipality

    with session_scope() as session:
        summary = ingest_municipality(
            session,
            args.municipality,
            only=args.only,
            as_of=_parse_date(args.date),
            skip_boundary=args.skip_boundary,
        )
        documents = register_documents(session, load_municipality(args.municipality))

    print(json.dumps({"ingest": summary, "documents": documents}, indent=2, default=str))
    _print_source_table(summary)
    return 0


def _print_source_table(summary: dict) -> None:
    print("\nSource results")
    print(f"{'source':<32} {'status':<12} {'records':>8}  message")
    print("-" * 100)
    for source_id, info in summary.get("sources", {}).items():
        message = (info.get("message") or "")[:48]
        print(
            f"{source_id:<32} {info['status']:<12} "
            f"{info.get('records', 0):>8}  {message}"
        )


def cmd_derive(args: argparse.Namespace) -> int:
    from firewatch.core.db import session_scope
    from firewatch.core.derive import derive_all
    from firewatch.core.models import Municipality
    from firewatch.jobs.ingest import build_context

    config = load_municipality(args.municipality)
    with session_scope() as session:
        municipality = session.get(Municipality, args.municipality)
        if municipality is None:
            log.error("%s has not been ingested yet", args.municipality)
            return 1
        ctx = build_context(municipality, config, as_of=_parse_date(args.date))
        result = derive_all(
            session, municipality, config, ctx, as_of=_parse_date(args.date)
        )

    print(
        json.dumps(
            {
                "cells": result.cells,
                "metrics_written": result.metrics_written,
                "gaps_recorded": result.gaps_recorded,
                "missing_metrics": result.missing_metrics,
                "notes": result.notes,
            },
            indent=2,
        )
    )
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    from firewatch.core.db import session_scope
    from firewatch.core.models import Municipality
    from firewatch.jobs.score import score_municipality

    with session_scope() as session:
        municipality = session.get(Municipality, args.municipality)
        if municipality is None:
            log.error("%s has not been ingested yet", args.municipality)
            return 1
        summary = score_municipality(session, municipality, as_of=_parse_date(args.date))

    print(json.dumps(summary, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Full pipeline: schema, ingest, derive, score."""
    from firewatch.core.db import init_db

    init_db()
    for command in (cmd_ingest, cmd_derive, cmd_score):
        code = command(args)
        if code != 0:
            return code
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from firewatch.core.db import session_scope
    from firewatch.core.health.report import coverage_summary, dataset_health, overall_status

    with session_scope() as session:
        health = dataset_health(session, args.municipality)
        coverage = coverage_summary(session, args.municipality)

    print(f"\nData health — {args.municipality}")
    print(f"{'source':<32} {'status':<12} {'tier':<14} {'records':>8}  freshness")
    print("-" * 100)
    for record in health:
        print(
            f"{record['source_id']:<32} {record['status']:<12} "
            f"{record['precedence_tier']:<14} {record['records_in_use']:>8}  "
            f"{record['staleness']['description']}"
        )

    print(f"\nMetric coverage over {coverage['total_cells']} cells")
    print(f"{'metric':<36} {'group':<12} {'coverage':>9} {'confidence':>11}")
    print("-" * 100)
    for metric in coverage["metrics"]:
        confidence = metric["mean_confidence"]
        print(
            f"{metric['metric']:<36} {str(metric['group']):<12} "
            f"{metric['coverage_percent']:>8.1f}% "
            f"{(f'{confidence:.2f}' if confidence is not None else '-'):>11}"
        )
    if coverage["missing_metrics"]:
        print("\nMetrics with no data at all:")
        for metric in coverage["missing_metrics"]:
            print(f"  - {metric['metric']} ({metric['label']})")

    status = overall_status(health)
    print(f"\nStatus counts: {status['counts']}")
    if status["municipal_sources_configured"] == 0:
        print(
            "No municipal-authoritative source is configured for this "
            "municipality. Buildings, roads and water assets rest entirely on "
            "provincial, federal and community data."
        )
    elif status["authoritative_gaps"]:
        print(
            "Municipal-authoritative sources NOT in use: "
            + ", ".join(status["authoritative_gaps"])
        )
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from firewatch.backtest import print_report, run_backtest

    date = _parse_date(args.date)
    if date is None:
        log.error("backtest needs an explicit --date")
        return 1

    report = run_backtest(
        args.municipality, date, refetch=not args.no_refetch, top_n=args.top
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    from firewatch.sources.registry import ADAPTERS

    print("Registered adapters:")
    for name in sorted(ADAPTERS):
        print(f"  {name:<28} {ADAPTERS[name].__module__}.{ADAPTERS[name].__name__}")

    print("\nConfigured municipalities:")
    for municipality_id in sorted(list_municipalities()):
        config = load_municipality(municipality_id)
        print(f"\n  {config.id} — {config.name}")
        print(f"    boundary: {config.boundary.adapter}")
        for source in config.sources:
            flag = "x" if source.enabled else " "
            print(
                f"    [{flag}] {source.id:<30} {source.adapter:<26} "
                f"{source.precedence_tier.value}"
            )
    return 0


def cmd_alerts(args: argparse.Namespace) -> int:
    from firewatch.core.alerts.process import process_municipality_alerts
    from firewatch.core.db import session_scope
    from firewatch.core.models import Municipality, PriorityScore
    from firewatch.core.scoring.priority import SCORE_VERSION
    from sqlalchemy import func, select

    with session_scope() as session:
        municipality = session.get(Municipality, args.municipality)
        if municipality is None:
            log.error("%s has not been ingested yet", args.municipality)
            return 1
        as_of_date = args.date
        if args.date:
            parsed = _parse_date(args.date)
            as_of_date = parsed.date().isoformat() if parsed else args.date
        else:
            as_of_date = session.scalar(
                select(func.max(PriorityScore.as_of_date)).where(
                    PriorityScore.municipality_id == args.municipality
                )
            )
        if not as_of_date:
            log.error("no scores found for %s", args.municipality)
            return 1
        summary = process_municipality_alerts(
            session, args.municipality, as_of_date, SCORE_VERSION
        )

    print(json.dumps(summary, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="firewatch", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_municipality(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--municipality", "-m", required=True)
        sub.add_argument(
            "--date",
            help="ISO date for historical mode. Defaults to now.",
        )

    initdb = subparsers.add_parser("initdb", help="create schema")
    initdb.add_argument("--reset", action="store_true", help="drop everything first")
    initdb.set_defaults(func=cmd_initdb)

    ingest = subparsers.add_parser("ingest", help="fetch and store sources")
    add_municipality(ingest)
    ingest.add_argument("--only", nargs="*", help="limit to these source ids")
    ingest.add_argument("--skip-boundary", action="store_true")
    ingest.set_defaults(func=cmd_ingest)

    derive = subparsers.add_parser("derive", help="compute per-cell metrics")
    add_municipality(derive)
    derive.set_defaults(func=cmd_derive)

    score = subparsers.add_parser("score", help="compute priority scores")
    add_municipality(score)
    score.set_defaults(func=cmd_score)

    run = subparsers.add_parser("run", help="ingest, derive and score")
    add_municipality(run)
    run.add_argument("--only", nargs="*")
    run.add_argument("--skip-boundary", action="store_true")
    run.set_defaults(func=cmd_run)

    backtest = subparsers.add_parser(
        "backtest", help="reconstruct the picture for a historical date"
    )
    add_municipality(backtest)
    backtest.add_argument(
        "--no-refetch",
        action="store_true",
        help="use only stored data, do not query sources for the date",
    )
    backtest.add_argument("--top", type=int, default=10)
    backtest.add_argument("--json", action="store_true")
    backtest.set_defaults(func=cmd_backtest)

    status = subparsers.add_parser("status", help="data health report")
    status.add_argument("--municipality", "-m", required=True)
    status.set_defaults(func=cmd_status)

    sources = subparsers.add_parser("sources", help="list adapters and configs")
    sources.set_defaults(func=cmd_sources)

    alerts = subparsers.add_parser("alerts", help="process priority alerts")
    add_municipality(alerts)
    alerts.set_defaults(func=cmd_alerts)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
