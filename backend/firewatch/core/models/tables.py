"""PostGIS schema for Fire Watch.

Design rule: **provenance is never discarded.** Every normalized spatial record
carries the dataset and version it came from, the upstream record id, the source
URL, and the observation time. If we cannot answer "where did this come from and
when was it true", the record does not belong in this database.

Deviation from the build brief: the brief lists one table per feature type
(buildings, roads, parcels, ...). This implementation uses a single ``features``
table discriminated by ``feature_kind``, because core scoring logic must work
identically whether a municipality's buildings arrive from a municipal ArcGIS
service or from OpenStreetMap. Per-kind SQL views recreate the brief's names for
analyst convenience (see ``firewatch/core/db.py``).
"""

from __future__ import annotations

from datetime import date, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from firewatch.core.models.base import Base, utcnow

# JSONB on Postgres, plain JSON elsewhere (keeps unit tests runnable on SQLite).
JsonB = JSON().with_variant(JSONB(), "postgresql")


class Municipality(Base):
    """One configured municipality. Populated from its YAML config."""

    __tablename__ = "municipalities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    short_name: Mapped[str] = mapped_column(String(128), nullable=False)
    province: Mapped[str] = mapped_column(String(8), nullable=False)
    country: Mapped[str] = mapped_column(String(8), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)

    h3_resolution: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_crs: Mapped[str] = mapped_column(String(32), nullable=False)
    boundary_buffer_m: Mapped[float] = mapped_column(Float, nullable=False)

    #: Legal boundary as supplied by the boundary adapter, EPSG:4326.
    boundary: Mapped[object | None] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=True),
        nullable=True,
    )
    #: Boundary buffered by ``boundary_buffer_m``; the true ingest envelope.
    boundary_buffered: Mapped[object | None] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=True),
        nullable=True,
    )

    boundary_source_url: Mapped[str | None] = mapped_column(Text)
    boundary_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    known_unknowns: Mapped[list] = mapped_column(JsonB, default=list)
    config_json: Mapped[dict] = mapped_column(JsonB, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    datasets: Mapped[list[Dataset]] = relationship(back_populates="municipality")


class Dataset(Base):
    """A configured source, one row per (municipality, source id).

    Holds the licence and attribution record the brief requires for every
    dataset.
    """

    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("municipality_id", "source_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(
        ForeignKey("municipalities.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    adapter: Mapped[str] = mapped_column(String(128), nullable=False)
    feature_kind: Mapped[str | None] = mapped_column(String(64), index=True)
    precedence_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    precedence_rank: Mapped[int] = mapped_column(Integer, nullable=False)

    title: Mapped[str | None] = mapped_column(Text)
    licence: Mapped[str | None] = mapped_column(Text)
    licence_url: Mapped[str | None] = mapped_column(Text)
    attribution: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)

    #: Operator-declared limitations, surfaced verbatim in the UI.
    caveats: Mapped[list] = mapped_column(JsonB, default=list)
    params: Mapped[dict] = mapped_column(JsonB, default=dict)

    spatial_resolution: Mapped[str | None] = mapped_column(String(128))
    temporal_resolution: Mapped[str | None] = mapped_column(String(128))

    municipality: Mapped[Municipality] = relationship(back_populates="datasets")
    versions: Mapped[list[DatasetVersion]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )


class DatasetVersion(Base):
    """One ingest attempt of one dataset.

    A version row exists even when the fetch fails, so the data-health panel can
    distinguish "never tried", "tried and failed", and "current".
    """

    __tablename__ = "dataset_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    #: SHA-256 of the raw payload. Identical hash means upstream did not change.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)

    record_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Records the source returned but we rejected (bad geometry, no date, ...).
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)

    #: When the *source* says its data were last updated, where it tells us.
    source_last_updated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Newest observation timestamp actually present in this version.
    latest_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The historical date a backtest asked for. NULL means a live fetch of the
    #: current picture. Without this, a historical re-fetch of an empty window
    #: becomes "the latest version" and the data-health panel reports that the
    #: live source is empty.
    as_of_date: Mapped[date | None] = mapped_column(Date, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    request_url: Mapped[str | None] = mapped_column(Text)
    validation_report: Mapped[dict] = mapped_column(JsonB, default=dict)

    dataset: Mapped[Dataset] = relationship(back_populates="versions")


class Feature(Base):
    """A normalized spatial record with complete provenance."""

    __tablename__ = "features"
    __table_args__ = (
        Index("ix_features_muni_kind", "municipality_id", "feature_kind"),
        Index("ix_features_kind_observed", "feature_kind", "observed_at"),
        Index("ix_features_geom", "geometry", postgresql_using="gist"),
        UniqueConstraint(
            "dataset_id", "dataset_version", "source_record_id",
            name="uq_features_source_record",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(
        ForeignKey("municipalities.id", ondelete="CASCADE"), index=True
    )
    feature_kind: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- provenance (mandatory) ---
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True
    )
    dataset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)

    #: When the real-world observation was made. Null means the source did not
    #: tell us, which is itself reportable information.
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    geometry: Mapped[object] = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False), nullable=False
    )
    properties_json: Mapped[dict] = mapped_column(JsonB, default=dict)

    #: True when a higher-precedence source supersedes this record. Kept, not
    #: deleted, so conflicts stay inspectable.
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class AnalysisCell(Base):
    """An H3 cell covering part of the municipality."""

    __tablename__ = "analysis_cells"
    __table_args__ = (
        UniqueConstraint("municipality_id", "h3_index"),
        Index("ix_cells_geom", "geometry", postgresql_using="gist"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(
        ForeignKey("municipalities.id", ondelete="CASCADE"), index=True
    )
    h3_index: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    resolution: Mapped[int] = mapped_column(Integer, nullable=False)

    geometry: Mapped[object] = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False), nullable=False
    )
    centroid_lat: Mapped[float] = mapped_column(Float, nullable=False)
    centroid_lon: Mapped[float] = mapped_column(Float, nullable=False)
    area_m2: Mapped[float] = mapped_column(Float, nullable=False)

    #: False for cells in the buffer ring outside the legal boundary.
    within_boundary: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class CellMetric(Base):
    """One derived value for one cell.

    Stored long-form rather than as wide columns so that adding a metric never
    requires a migration, and so every value carries its own provenance and
    confidence.
    """

    __tablename__ = "cell_metrics"
    __table_args__ = (
        # NULLS NOT DISTINCT matters here rather than being a detail.
        # Time-invariant metrics carry as_of_date = NULL, and under the default
        # rule Postgres treats every NULL as unique, so the upsert on re-derive
        # would never match and each run would silently duplicate every static
        # metric instead of replacing it.
        UniqueConstraint(
            "cell_id",
            "metric",
            "as_of_date",
            name="uq_cell_metric",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_cell_metrics_metric", "metric", "as_of_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cell_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_cells.id", ondelete="CASCADE"), index=True
    )
    metric: Mapped[str] = mapped_column(String(64), nullable=False)

    value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(32))
    #: Free-form for categorical metrics (e.g. a fuel class label).
    value_text: Mapped[str | None] = mapped_column(Text)

    #: Null for time-invariant metrics such as slope. Set for current/historical
    #: metrics so historical mode can select the right row.
    as_of_date: Mapped[str | None] = mapped_column(String(10), index=True)

    #: 0..1. Reflects source quality and how much was actually measured.
    confidence: Mapped[float | None] = mapped_column(Float)
    #: Dataset ids that contributed, plus supporting detail for the UI.
    evidence: Mapped[dict] = mapped_column(JsonB, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PriorityScore(Base):
    """Fire Watch Priority for one cell on one date, under one score version."""

    __tablename__ = "priority_scores"
    __table_args__ = (
        UniqueConstraint("cell_id", "as_of_date", "score_version", name="uq_priority"),
        Index("ix_priority_lookup", "municipality_id", "as_of_date", "score_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(
        ForeignKey("municipalities.id", ondelete="CASCADE"), index=True
    )
    cell_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_cells.id", ondelete="CASCADE"), index=True
    )
    as_of_date: Mapped[str] = mapped_column(String(10), nullable=False)
    score_version: Mapped[str] = mapped_column(String(32), nullable=False)

    #: The five components of the brief's working formula, each 0..1.
    ignition_likelihood: Mapped[float | None] = mapped_column(Float)
    spread_potential: Mapped[float | None] = mapped_column(Float)
    consequence_exposure: Mapped[float | None] = mapped_column(Float)
    observation_gap: Mapped[float | None] = mapped_column(Float)
    access_difficulty_proxy: Mapped[float | None] = mapped_column(Float)

    #: The brief insists hazard, exposure, current conditions and operational
    #: gap stay separable rather than collapsing into one opaque number.
    hazard: Mapped[float | None] = mapped_column(Float)
    exposure: Mapped[float | None] = mapped_column(Float)
    current_conditions: Mapped[float | None] = mapped_column(Float)
    operational_gap: Mapped[float | None] = mapped_column(Float)

    overall_priority: Mapped[float | None] = mapped_column(Float, index=True)
    #: Percentile of this cell within its municipality on this date, 0..1.
    #: The absolute score is what tracks real severity over time, so on a damp
    #: day nothing should read as high. That makes the absolute value a poor
    #: choice for colouring a map, hence a separate relative rank. Comparable
    #: within one municipality and date only.
    priority_percentile: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    #: Fraction of intended inputs that were actually available.
    completeness: Mapped[float | None] = mapped_column(Float)

    explanation: Mapped[dict] = mapped_column(JsonB, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    components: Mapped[list[ScoreComponent]] = relationship(
        back_populates="score", cascade="all, delete-orphan"
    )


class ScoreComponent(Base):
    """A queryable row per component, so the AI can rank by any single driver."""

    __tablename__ = "score_components"
    __table_args__ = (Index("ix_score_components_name", "component", "value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    score_id: Mapped[int] = mapped_column(
        ForeignKey("priority_scores.id", ondelete="CASCADE"), index=True
    )
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    #: Human-readable reason string shown in the evidence panel.
    rationale: Mapped[str | None] = mapped_column(Text)
    inputs_used: Mapped[list] = mapped_column(JsonB, default=list)
    inputs_missing: Mapped[list] = mapped_column(JsonB, default=list)

    score: Mapped[PriorityScore] = relationship(back_populates="components")


class DataGap(Base):
    """A first-class record of something we do not know.

    The brief requires unknowns to be objects in the product, not prose.
    """

    __tablename__ = "data_gaps"
    __table_args__ = (Index("ix_data_gaps_lookup", "municipality_id", "gap_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(
        ForeignKey("municipalities.id", ondelete="CASCADE"), index=True
    )
    #: Null when the gap is municipality-wide rather than cell-specific.
    cell_id: Mapped[int | None] = mapped_column(
        ForeignKey("analysis_cells.id", ondelete="CASCADE"), index=True
    )

    gap_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    #: Who could close this gap: 'municipal_fire', 'provincial', 'yellow_duck'...
    resolvable_by: Mapped[str | None] = mapped_column(String(64))
    affects: Mapped[list] = mapped_column(JsonB, default=list)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceConflict(Base):
    """Two sources disagree. Recorded rather than resolved silently."""

    __tablename__ = "source_conflicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(
        ForeignKey("municipalities.id", ondelete="CASCADE"), index=True
    )
    subject: Mapped[str] = mapped_column(String(128), nullable=False)
    winning_dataset_id: Mapped[int | None] = mapped_column(ForeignKey("datasets.id"))
    losing_dataset_id: Mapped[int | None] = mapped_column(ForeignKey("datasets.id"))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict] = mapped_column(JsonB, default=dict)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IngestRun(Base):
    """One invocation of the ingest pipeline."""

    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(
        ForeignKey("municipalities.id", ondelete="CASCADE"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="running")
    requested_sources: Mapped[list] = mapped_column(JsonB, default=list)
    summary: Mapped[dict] = mapped_column(JsonB, default=dict)


class Document(Base):
    """A local wildfire document available to the retrieval layer."""

    __tablename__ = "source_documents"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    municipality_id: Mapped[str] = mapped_column(
        ForeignKey("municipalities.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    publisher: Mapped[str | None] = mapped_column(Text)
    publication_date: Mapped[str | None] = mapped_column(String(32))
    source_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="not_ingested")
    message: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentChunk(Base):
    """A retrievable passage. Citations always resolve to page or section."""

    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("source_documents.id", ondelete="CASCADE"), index=True
    )
    page: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: pgvector is the intended home for embeddings. Until an embedding provider
    #: is configured, retrieval is lexical and this stays null.
    embedding: Mapped[list | None] = mapped_column(JsonB, nullable=True)

    document: Mapped[Document] = relationship(back_populates="chunks")


class AlertSubscription(Base):
    """Email alert for a municipality crossing into High priority."""

    __tablename__ = "alert_subscriptions"
    __table_args__ = (
        UniqueConstraint("email", "municipality_id", name="uq_alert_subscription"),
        Index("ix_alert_subscriptions_municipality", "municipality_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    municipality_id: Mapped[str] = mapped_column(
        ForeignKey("municipalities.id", ondelete="CASCADE"), nullable=False
    )
    unsubscribe_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AlertDispatch(Base):
    """Record that subscribers were notified for one score date."""

    __tablename__ = "alert_dispatches"
    __table_args__ = (
        UniqueConstraint(
            "municipality_id", "as_of_date", "score_version", name="uq_alert_dispatch"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    municipality_id: Mapped[str] = mapped_column(
        ForeignKey("municipalities.id", ondelete="CASCADE"), index=True
    )
    as_of_date: Mapped[str] = mapped_column(String(10), nullable=False)
    score_version: Mapped[str] = mapped_column(String(32), nullable=False)
    new_high_cells: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    summary: Mapped[dict] = mapped_column(JsonB, default=dict)
