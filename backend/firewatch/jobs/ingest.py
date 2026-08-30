"""Ingest orchestration: boundary, sources, precedence resolution."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from sqlalchemy import select
from sqlalchemy.orm import Session

from firewatch.core.geo.crs import buffer_meters
from firewatch.core.models import (
    Dataset,
    DatasetVersion,
    Feature,
    IngestRun,
    Municipality,
    SourceConflict,
)
from firewatch.core.municipality import MunicipalityConfig, load_municipality
from firewatch.sources.base import DataStatus, IngestContext
from firewatch.sources.pipeline import run_source
from firewatch.sources.registry import build_adapter

log = logging.getLogger(__name__)

#: Feature kinds where two sources genuinely describe the same real-world thing,
#: so precedence must pick a winner. Fire history and observations are additive
#: rather than competing, so they are excluded.
COMPETING_KINDS = {"building", "road", "parcel", "park", "water_asset", "fire_station"}


def _as_multipolygon(geom: BaseGeometry) -> MultiPolygon:
    if isinstance(geom, MultiPolygon):
        return geom
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]
    if not polys:
        raise ValueError(f"Boundary is not polygonal: {geom.geom_type}")
    return MultiPolygon(polys)


def load_boundary(session: Session, config: MunicipalityConfig) -> Municipality:
    """Fetch the legal boundary and upsert the municipality row.

    Nothing else can run until this succeeds, so failure here raises.
    """
    municipality = session.get(Municipality, config.id)
    if municipality is None:
        municipality = Municipality(id=config.id)
        session.add(municipality)

    municipality.name = config.name
    municipality.short_name = config.short_name
    municipality.province = config.province
    municipality.country = config.country
    municipality.timezone = config.timezone
    municipality.h3_resolution = config.analysis.h3_resolution
    municipality.metric_crs = config.analysis.metric_crs
    municipality.boundary_buffer_m = config.analysis.boundary_buffer_m
    municipality.known_unknowns = list(config.known_unknowns)
    municipality.config_json = config.model_dump(mode="json")
    session.flush()

    adapter = build_adapter(config.boundary)
    # The boundary adapter needs a context but has no boundary yet. A
    # province-wide envelope is correct here: we are searching by name.
    placeholder = MultiPolygon([Polygon([(-180, -85), (180, -85), (180, 85), (-180, 85)])])
    bootstrap_ctx = IngestContext(
        municipality=config,
        boundary=placeholder,
        boundary_buffered=placeholder,
        metric_crs=config.analysis.metric_crs,
    )

    manifest = adapter.discover(bootstrap_ctx)
    raw = adapter.fetch(bootstrap_ctx)
    normalized = adapter.normalize(raw, bootstrap_ctx)
    if not normalized.features:
        raise RuntimeError(
            f"Boundary source '{config.boundary.id}' returned no polygon for "
            f"{config.name}. Check boundary.params in the municipality config."
        )

    boundary = _as_multipolygon(
        unary_union([f.geometry for f in normalized.features])
    )
    buffered = _as_multipolygon(
        buffer_meters(boundary, config.analysis.boundary_buffer_m, config.analysis.metric_crs)
    )

    municipality.boundary = from_shape(boundary, srid=4326)
    municipality.boundary_buffered = from_shape(buffered, srid=4326)
    municipality.boundary_source_url = manifest.source_url
    municipality.boundary_observed_at = normalized.features[0].observed_at
    session.flush()

    log.info(
        "Boundary for %s: %.2f km2, bounds %s",
        config.name,
        boundary.area * 111 * 111 * 0.65,  # rough, logging only
        [round(v, 4) for v in boundary.bounds],
    )
    return municipality


def build_context(
    municipality: Municipality, config: MunicipalityConfig, as_of: datetime | None = None
) -> IngestContext:
    if municipality.boundary is None:
        raise RuntimeError(f"{config.id} has no boundary; run boundary ingest first.")
    return IngestContext(
        municipality=config,
        boundary=to_shape(municipality.boundary),
        boundary_buffered=to_shape(municipality.boundary_buffered),
        metric_crs=config.analysis.metric_crs,
        as_of=as_of,
    )


def resolve_precedence(session: Session, municipality_id: str) -> list[dict]:
    """Apply the data-precedence order for competing feature kinds.

    Losing records are flagged ``superseded``, never deleted, and a conflict row
    explains the decision.
    """
    decisions: list[dict] = []

    for kind in sorted(COMPETING_KINDS):
        rows = session.execute(
            select(Dataset, DatasetVersion)
            .join(DatasetVersion, DatasetVersion.dataset_id == Dataset.id)
            .where(
                Dataset.municipality_id == municipality_id,
                Dataset.feature_kind == kind,
            )
            .order_by(Dataset.precedence_rank, DatasetVersion.version.desc())
        ).all()

        # Keep only each dataset's newest version, and only those with data.
        best: dict[int, tuple[Dataset, DatasetVersion]] = {}
        for dataset, version in rows:
            if dataset.id not in best and version.record_count > 0:
                best[dataset.id] = (dataset, version)

        usable = sorted(best.values(), key=lambda pair: pair[0].precedence_rank)
        if len(usable) < 2:
            # Nothing to arbitrate; make sure a lone source is not left flagged.
            for dataset, _ in usable:
                session.query(Feature).filter(
                    Feature.dataset_id == dataset.id
                ).update({"superseded": False})
            continue

        winner, winner_version = usable[0]
        session.query(Feature).filter(Feature.dataset_id == winner.id).update(
            {"superseded": False}
        )

        for loser, loser_version in usable[1:]:
            session.query(Feature).filter(Feature.dataset_id == loser.id).update(
                {"superseded": True}
            )
            description = (
                f"Both '{winner.source_id}' ({winner.precedence_tier}, "
                f"{winner_version.record_count} records) and '{loser.source_id}' "
                f"({loser.precedence_tier}, {loser_version.record_count} records) "
                f"supply {kind} features. The higher-precedence source is used for "
                f"analysis; the other is retained and inspectable."
            )
            session.add(
                SourceConflict(
                    municipality_id=municipality_id,
                    subject=kind,
                    winning_dataset_id=winner.id,
                    losing_dataset_id=loser.id,
                    description=description,
                    detail={
                        "winner_count": winner_version.record_count,
                        "loser_count": loser_version.record_count,
                        "count_difference": (
                            winner_version.record_count - loser_version.record_count
                        ),
                    },
                )
            )
            decisions.append(
                {"kind": kind, "winner": winner.source_id, "superseded": loser.source_id}
            )

    session.flush()
    return decisions


def ingest_municipality(
    session: Session,
    municipality_id: str,
    only: list[str] | None = None,
    as_of: datetime | None = None,
    skip_boundary: bool = False,
) -> dict:
    """Run the full ingest for one municipality."""
    config = load_municipality(municipality_id)
    summary: dict = {"boundary": None, "sources": {}, "precedence": []}

    # The boundary must land first: the municipality row it creates is the
    # foreign-key target for everything else, including the run record.
    if skip_boundary:
        municipality = session.get(Municipality, municipality_id)
        if municipality is None:
            raise RuntimeError("Cannot skip boundary ingest before it has ever run.")
    else:
        municipality = load_boundary(session, config)
        summary["boundary"] = {
            "status": "CURRENT",
            "source": config.boundary.id,
            "url": municipality.boundary_source_url,
        }
    session.commit()

    run = IngestRun(
        municipality_id=municipality_id,
        requested_sources=only or [s.id for s in config.enabled_sources()],
    )
    session.add(run)
    session.commit()

    ctx = build_context(municipality, config, as_of=as_of)

    for source in config.enabled_sources():
        if only and source.id not in only:
            continue
        try:
            adapter = build_adapter(source)
        except KeyError as exc:
            summary["sources"][source.id] = {"status": "FAILED", "message": str(exc)}
            continue

        log.info("Ingesting %s (%s)", source.id, source.adapter)
        version = run_source(session, municipality, source, adapter, ctx)
        session.commit()
        summary["sources"][source.id] = {
            "status": version.status,
            "records": version.record_count,
            "rejected": version.rejected_count,
            "message": version.message,
            "version": version.version,
        }

    summary["precedence"] = resolve_precedence(session, municipality_id)

    run.finished_at = datetime.now(timezone.utc)
    failed = [
        sid for sid, info in summary["sources"].items()
        if info["status"] in {DataStatus.FAILED.value, DataStatus.UNAVAILABLE.value}
    ]
    run.status = "completed_with_failures" if failed else "completed"
    run.summary = summary
    session.commit()
    return summary
