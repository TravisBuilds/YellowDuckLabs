"""The ingestion pipeline.

    DISCOVER -> FETCH -> HASH/VERSION -> NORMALIZE -> VALIDATE -> CLIP
             -> STORE METADATA -> STORE FEATURES -> RECORD HEALTH

Every branch of this function ends with a ``dataset_versions`` row. A source that
was never reachable, returned nothing, or returned rubbish all leave an audit
trail, because "we don't have this" is information the product must surface.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from geoalchemy2.shape import from_shape
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from firewatch.core.models import Dataset, DatasetVersion, Feature, Municipality
from firewatch.core.municipality import SourceConfig
from firewatch.sources.base import (
    DatasetManifest,
    DataStatus,
    IngestContext,
    SourceAdapter,
    ValidationReport,
)
from firewatch.sources.http import SourceUnavailable

log = logging.getLogger(__name__)


def upsert_dataset(
    session: Session,
    municipality_id: str,
    config: SourceConfig,
    manifest: DatasetManifest,
    feature_kind: str | None,
) -> Dataset:
    dataset = session.scalar(
        select(Dataset).where(
            Dataset.municipality_id == municipality_id, Dataset.source_id == config.id
        )
    )
    if dataset is None:
        dataset = Dataset(municipality_id=municipality_id, source_id=config.id)
        session.add(dataset)

    dataset.adapter = config.adapter
    dataset.feature_kind = feature_kind
    dataset.precedence_tier = config.precedence_tier.value
    dataset.precedence_rank = config.precedence_tier.rank
    dataset.title = manifest.title
    dataset.source_url = manifest.source_url
    dataset.licence = manifest.licence or config.params.get("licence")
    dataset.licence_url = manifest.licence_url or config.params.get("licence_url")
    dataset.attribution = manifest.attribution or config.params.get("attribution")
    dataset.spatial_resolution = manifest.spatial_resolution
    dataset.temporal_resolution = manifest.temporal_resolution
    # Adapter-declared caveats and operator-declared caveats both matter.
    dataset.caveats = list(dict.fromkeys([*manifest.caveats, *config.caveats]))
    dataset.params = {k: v for k, v in config.params.items() if k != "licence"}

    session.flush()
    return dataset


def _next_version(session: Session, dataset_id: int) -> int:
    latest = session.scalar(
        select(DatasetVersion.version)
        .where(DatasetVersion.dataset_id == dataset_id)
        .order_by(DatasetVersion.version.desc())
        .limit(1)
    )
    return (latest or 0) + 1


def _record_version(
    session: Session,
    dataset: Dataset,
    status: DataStatus,
    message: str | None,
    *,
    request_url: str | None = None,
    content_hash: str | None = None,
    record_count: int = 0,
    rejected_count: int = 0,
    source_last_updated: datetime | None = None,
    latest_observed_at: datetime | None = None,
    as_of_date=None,
    validation: dict | None = None,
) -> DatasetVersion:
    version = DatasetVersion(
        dataset_id=dataset.id,
        version=_next_version(session, dataset.id),
        status=status.value,
        message=message,
        request_url=request_url,
        content_hash=content_hash,
        record_count=record_count,
        rejected_count=rejected_count,
        source_last_updated=source_last_updated,
        latest_observed_at=latest_observed_at,
        as_of_date=as_of_date,
        validation_report=validation or {},
    )
    session.add(version)
    session.flush()
    return version


def run_source(
    session: Session,
    municipality: Municipality,
    config: SourceConfig,
    adapter: SourceAdapter,
    ctx: IngestContext,
) -> DatasetVersion:
    """Ingest one source. Never raises for source-side problems."""

    # --- DISCOVER ---------------------------------------------------------
    try:
        manifest = adapter.discover(ctx)
    except Exception as exc:  # a broken adapter must not abort the whole run
        log.exception("discover failed for %s", config.id)
        manifest = DatasetManifest(
            source_id=config.id,
            title=config.id,
            source_url=str(config.params.get("base_url", "")),
            available=False,
            message=f"discover() raised {type(exc).__name__}: {exc}",
        )

    dataset = upsert_dataset(
        session, municipality.id, config, manifest, adapter.feature_kind
    )

    # Live fetches leave as_of_date NULL so they remain the current picture.
    # A backtest for a historical date must not become that picture.
    historical_date = ctx.as_of.date() if ctx.as_of else None

    def record(status, message, **kwargs):
        return _record_version(
            session, dataset, status, message, as_of_date=historical_date, **kwargs
        )

    if not manifest.available:
        return record(
            DataStatus.UNAVAILABLE,
            manifest.message or "Source reported itself unavailable.",
            request_url=manifest.source_url,
        )

    # --- FETCH ------------------------------------------------------------
    try:
        raw = adapter.fetch(ctx)
    except SourceUnavailable as exc:
        return record(
            DataStatus.FAILED, str(exc), request_url=manifest.source_url
        )
    except Exception as exc:
        log.exception("fetch failed for %s", config.id)
        return record(
            DataStatus.FAILED,
            f"{type(exc).__name__}: {exc}",
            request_url=manifest.source_url,
        )

    # --- NORMALIZE / VALIDATE / CLIP -------------------------------------
    try:
        normalized = adapter.normalize(raw, ctx)
    except Exception as exc:
        log.exception("normalize failed for %s", config.id)
        return record(
            DataStatus.FAILED,
            f"normalize() raised {type(exc).__name__}: {exc}",
            request_url=raw.request_url,
            content_hash=raw.content_hash,
        )

    report: ValidationReport = adapter.validate(normalized, ctx)
    report.clipped_out = adapter.clip(normalized, ctx)
    for note in raw.notes:
        report.warnings.append(note)

    if raw.reported_total is not None and raw.reported_total > report.received:
        report.warnings.append(
            f"Source reports {raw.reported_total} records but only {report.received} "
            "were retrieved."
        )

    observed = [f.observed_at for f in normalized.features if f.observed_at]
    latest_observed_at = max(observed) if observed else None

    # --- STORE ------------------------------------------------------------
    version_no = _next_version(session, dataset.id)
    accumulate = bool(adapter.config.params.get("accumulate", False)) or getattr(
        adapter, "accumulate", False
    )

    existing_ids: set[str] = set()
    if accumulate:
        # Time series: keep history, skip records we already hold.
        existing_ids = set(
            session.scalars(
                select(Feature.source_record_id).where(Feature.dataset_id == dataset.id)
            ).all()
        )
    else:
        # Snapshot: the newest fetch replaces the previous features. Version
        # history in dataset_versions retains the full audit trail.
        session.execute(delete(Feature).where(Feature.dataset_id == dataset.id))

    stored = 0
    skipped_existing = 0
    now = datetime.now(timezone.utc)
    for feature in normalized.features:
        if accumulate and feature.source_record_id in existing_ids:
            skipped_existing += 1
            continue
        session.add(
            Feature(
                municipality_id=municipality.id,
                feature_kind=feature.feature_kind,
                dataset_id=dataset.id,
                dataset_version=version_no,
                source_record_id=feature.source_record_id,
                source_url=feature.source_url or raw.request_url,
                observed_at=feature.observed_at,
                valid_from=feature.valid_from,
                valid_to=feature.valid_to,
                ingested_at=now,
                geometry=from_shape(feature.geometry, srid=4326),
                properties_json=feature.properties,
            )
        )
        existing_ids.add(feature.source_record_id)
        stored += 1

    if skipped_existing:
        report.warnings.append(
            f"{skipped_existing} records were already held from earlier ingests."
        )

    status, message = adapter.status_for(latest_observed_at, raw, report)
    if adapter.produces_features and stored == 0 and not skipped_existing:
        status = DataStatus.PARTIAL
        message = (
            "The source responded successfully but no records fell inside the "
            "municipality envelope. This may be correct (genuinely nothing here) "
            "or may indicate a query or projection problem."
        )
    if not report.ok:
        status = DataStatus.FAILED
        message = "; ".join(report.errors) or message

    version = record(
        status,
        message,
        request_url=raw.request_url,
        content_hash=raw.content_hash,
        record_count=stored,
        rejected_count=report.rejected,
        source_last_updated=raw.source_last_updated,
        latest_observed_at=latest_observed_at,
        validation=report.as_dict(),
    )
    log.info(
        "%s: %s (%d stored, %d rejected, %d clipped out)",
        config.id, status.value, stored, report.rejected, report.clipped_out,
    )
    return version
