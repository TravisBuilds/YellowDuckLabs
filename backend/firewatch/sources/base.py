"""The source adapter contract and the ingestion pipeline.

Every dataset enters Fire Watch through one adapter implementing
``discover / fetch / normalize``. Validation, reprojection, clipping,
persistence and health recording are handled once, here, so adapters stay small
and consistent.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from shapely.geometry.base import BaseGeometry

from firewatch.core.geo.crs import buffer_meters
from firewatch.core.municipality import MunicipalityConfig, SourceConfig


class DataStatus(str, Enum):
    """Data-health status shown in the UI.

    ``UNAVAILABLE`` extends the brief's list: a source that is correctly
    configured but missing a credential is operationally different from one that
    failed, and both are different from one returning stale data.
    """

    CURRENT = "CURRENT"
    AGING = "AGING"
    STALE = "STALE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class DatasetManifest:
    """What an adapter says about its source before fetching anything."""

    source_id: str
    title: str
    source_url: str
    licence: str | None = None
    licence_url: str | None = None
    attribution: str | None = None
    spatial_resolution: str | None = None
    temporal_resolution: str | None = None
    caveats: list[str] = field(default_factory=list)
    #: False when a prerequisite is missing (e.g. no API key configured).
    available: bool = True
    message: str | None = None


@dataclass
class RawDataset:
    """Raw payload plus everything needed to version it."""

    payload: Any
    request_url: str
    content_hash: str
    source_last_updated: datetime | None = None
    #: Total the source claims to hold, when it tells us. Enables PARTIAL
    #: detection when we paginate or cap.
    reported_total: int | None = None
    truncated: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class NormalizedFeature:
    """One record, already in EPSG:4326, ready for provenance-complete storage."""

    source_record_id: str
    feature_kind: str
    geometry: BaseGeometry
    properties: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_url: str | None = None


@dataclass
class NormalizedFeatures:
    features: list[NormalizedFeature]
    notes: list[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    received: int = 0
    accepted: int = 0
    rejected: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    missing_observed_at: int = 0
    duplicate_ids: int = 0
    clipped_out: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "received": self.received,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "rejection_reasons": self.rejection_reasons,
            "missing_observed_at": self.missing_observed_at,
            "duplicate_ids": self.duplicate_ids,
            "clipped_out": self.clipped_out,
        }


@dataclass
class IngestContext:
    """Everything an adapter may know about where it is ingesting.

    Adapters receive geometry and CRS through this object. None of them read a
    municipality name to decide behaviour.
    """

    municipality: MunicipalityConfig
    boundary: BaseGeometry
    boundary_buffered: BaseGeometry
    metric_crs: str
    #: Historical mode: reconstruct the picture as of this date.
    as_of: datetime | None = None
    _envelopes: dict[float, BaseGeometry] = field(default_factory=dict, repr=False)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) of the buffered ingest envelope."""
        return self.boundary_buffered.bounds

    def envelope_km(self, buffer_km: float | None) -> BaseGeometry:
        """The boundary grown by ``buffer_km``.

        Some sources are deliberately collected well beyond the municipality:
        a satellite hotspot 30 km upwind is not inside town, but it is exactly
        the thing a fire chief wants to see. Those sources declare their own
        search radius, and features are kept against that radius rather than
        the default ingest envelope.
        """
        if not buffer_km or buffer_km <= 0:
            return self.boundary_buffered
        cached = self._envelopes.get(float(buffer_km))
        if cached is None:
            grown = buffer_meters(self.boundary, buffer_km * 1000.0, self.metric_crs)
            # Never return less coverage than the default envelope.
            cached = grown.union(self.boundary_buffered)
            self._envelopes[float(buffer_km)] = cached
        return cached

    def bbox_string(self, order: str = "lonlat") -> str:
        west, south, east, north = self.bounds
        if order == "latlon":
            return f"{south},{west},{north},{east}"
        return f"{west},{south},{east},{north}"


class SourceAdapter(ABC):
    """Base class for all data sources.

    Subclasses implement the three source-specific steps. Everything else is
    provided by :func:`run_source`.
    """

    #: Registry key referenced by ``adapter:`` in a municipality YAML.
    adapter_id: str = ""
    #: Adapters that only register provenance (e.g. WMS overlays) set this so
    #: the pipeline does not treat zero features as a failure.
    produces_features: bool = True
    #: How long before data are considered aging, then stale.
    aging_after: timedelta = timedelta(days=90)
    stale_after: timedelta = timedelta(days=365)

    def __init__(self, config: SourceConfig) -> None:
        self.config = config
        self.params = config.params

    @property
    def source_id(self) -> str:
        return self.config.id

    @property
    def feature_kind(self) -> str | None:
        kind = self.params.get("feature_kind")
        return str(kind) if kind else None

    @property
    def search_buffer_km(self) -> float | None:
        """How far beyond the boundary this source's features stay relevant."""
        value = self.params.get("search_buffer_km")
        return float(value) if value else None

    @abstractmethod
    def discover(self, ctx: IngestContext) -> DatasetManifest:
        """Describe the source. Must not raise for an unavailable source."""

    @abstractmethod
    def fetch(self, ctx: IngestContext) -> RawDataset:
        """Retrieve raw data. Raise ``SourceUnavailable`` on failure."""

    @abstractmethod
    def normalize(self, raw: RawDataset, ctx: IngestContext) -> NormalizedFeatures:
        """Convert raw payload to EPSG:4326 features with observation times."""

    # --- shared helpers -----------------------------------------------------

    def validate(self, normalized: NormalizedFeatures, ctx: IngestContext) -> ValidationReport:
        """Structural validation. Adapters may extend but rarely need to."""
        report = ValidationReport(received=len(normalized.features))
        report.warnings.extend(normalized.notes)
        seen: set[str] = set()
        kept: list[NormalizedFeature] = []

        for feature in normalized.features:
            reason = None
            if feature.geometry is None or feature.geometry.is_empty:
                reason = "empty_geometry"
            elif not feature.geometry.is_valid:
                repaired = feature.geometry.buffer(0)
                if repaired.is_valid and not repaired.is_empty:
                    feature.geometry = repaired
                else:
                    reason = "invalid_geometry"
            if reason is None and feature.source_record_id in seen:
                reason = "duplicate_source_record_id"
                report.duplicate_ids += 1

            if reason:
                report.rejected += 1
                report.rejection_reasons[reason] = report.rejection_reasons.get(reason, 0) + 1
                continue

            if feature.observed_at is None:
                report.missing_observed_at += 1
            seen.add(feature.source_record_id)
            kept.append(feature)

        normalized.features = kept
        report.accepted = len(kept)

        if report.missing_observed_at:
            report.warnings.append(
                f"{report.missing_observed_at} of {report.received} records carry no "
                "observation date from the source; freshness for those is unknown."
            )
        if report.received and report.accepted == 0 and self.produces_features:
            report.ok = False
            report.errors.append("Every record from this source was rejected.")
        return report

    def clip(self, normalized: NormalizedFeatures, ctx: IngestContext) -> int:
        """Drop features outside this source's collection envelope."""
        envelope = ctx.envelope_km(self.search_buffer_km)
        before = len(normalized.features)
        normalized.features = [
            f for f in normalized.features if f.geometry.intersects(envelope)
        ]
        return before - len(normalized.features)

    def status_for(
        self, latest_observed_at: datetime | None, raw: RawDataset, report: ValidationReport
    ) -> tuple[DataStatus, str | None]:
        """Classify freshness. Overridden by fast-moving sources."""
        if raw.truncated:
            return DataStatus.PARTIAL, "Result set was capped; coverage is incomplete."
        if latest_observed_at is None:
            return DataStatus.UNKNOWN, "The source publishes no observation date."

        age = datetime.now(timezone.utc) - latest_observed_at
        if age <= self.aging_after:
            return DataStatus.CURRENT, None
        message = f"Newest observation is {describe_age(age)} old."
        if age <= self.stale_after:
            return DataStatus.AGING, message
        return DataStatus.STALE, message


def describe_age(age: timedelta) -> str:
    """Human-readable age, at a resolution that matches the interval.

    Fire weather moves hourly, so reporting a six-hour-old observation as
    "0 days old" would read as an error rather than as information.
    """
    seconds = max(age.total_seconds(), 0)
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes"
    if seconds < 2 * 86400:
        return f"{int(seconds // 3600)} hours"
    return f"{int(seconds // 86400)} days"


def content_hash(payload: Any) -> str:
    """Stable SHA-256 over a payload, for change detection."""
    if isinstance(payload, bytes):
        return hashlib.sha256(payload).hexdigest()
    if isinstance(payload, str):
        return hashlib.sha256(payload.encode()).hexdigest()
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
