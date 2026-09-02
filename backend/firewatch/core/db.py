"""Engine, session and schema bootstrap."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from firewatch.config import settings
from firewatch.core.models import Base

log = logging.getLogger(__name__)

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# The build brief names one table per feature type. We store a single
# provenance-complete ``features`` table so core logic is source-agnostic, then
# expose the brief's names as views so an analyst can query them directly.
_FEATURE_VIEWS = {
    "buildings": "building",
    "roads": "road",
    "parcels": "parcel",
    "parks": "park",
    "water_assets": "water_asset",
    "fire_stations": "fire_station",
    "fuel_treatments": "fuel_treatment",
    "fire_events": "fire_event",
    "fire_perimeters": "fire_perimeter",
    "satellite_hotspots": "satellite_hotspot",
    "weather_stations": "weather_station",
    "weather_observations": "weather_observation",
    "fire_weather_observations": "fire_weather_observation",
    "terrain_cells": "terrain_cell",
    "vegetation_cells": "vegetation_cell",
}


def init_db() -> None:
    """Create extensions, tables and convenience views. Idempotent."""
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        # pgvector is provisioned now so document embeddings can land later
        # without a schema change. Absence is not fatal.
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception:
            pass

    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        # GIST on the geometry columns declared with spatial_index=False, plus
        # geography-cast indexes so that the metric ST_DWithin / ST_Distance
        # queries in the derivation step are index-assisted rather than
        # sequential scans.
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_features_geom "
            "ON features USING GIST (geometry)",
            "CREATE INDEX IF NOT EXISTS ix_features_geog "
            "ON features USING GIST ((geometry::geography))",
            "CREATE INDEX IF NOT EXISTS ix_cells_geom "
            "ON analysis_cells USING GIST (geometry)",
            "CREATE INDEX IF NOT EXISTS ix_cells_geog "
            "ON analysis_cells USING GIST ((geometry::geography))",
            "CREATE INDEX IF NOT EXISTS ix_cells_muni_inside "
            "ON analysis_cells (municipality_id) WHERE within_boundary",
            "CREATE INDEX IF NOT EXISTS ix_priority_muni_date "
            "ON priority_scores (municipality_id, as_of_date, score_version)",
            "CREATE INDEX IF NOT EXISTS ix_features_kind_active "
            "ON features (municipality_id, feature_kind) WHERE NOT superseded",
            "CREATE INDEX IF NOT EXISTS ix_features_muni_kind_obs "
            "ON features (municipality_id, feature_kind, observed_at DESC) "
            "WHERE NOT superseded",
            "CREATE INDEX IF NOT EXISTS ix_doc_chunks_fts "
            "ON document_chunks USING GIN (to_tsvector('english', text))",
        ):
            conn.execute(text(statement))

        # A view definition cannot carry bind parameters, and these values come
        # from a fixed internal table rather than user input.
        for view, kind in _FEATURE_VIEWS.items():
            conn.execute(
                text(
                    f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM features "
                    f"WHERE feature_kind = '{kind}' AND NOT superseded"
                )
            )

    with engine.begin() as conn:
        # Added after the first ingest. create_all will not ALTER existing tables.
        conn.execute(
            text(
                "ALTER TABLE dataset_versions "
                "ADD COLUMN IF NOT EXISTS as_of_date date"
            )
        )
        # Historical empty fetches written before as_of_date existed. A later
        # empty version of a source that already has live records is the
        # backtest shadowing the current picture; mark it so the health query
        # can skip it. A source that has never returned records is left alone.
        conn.execute(
            text(
                """
                UPDATE dataset_versions v
                   SET as_of_date = COALESCE(
                       v.latest_observed_at::date, v.fetched_at::date
                   )
                 WHERE v.as_of_date IS NULL
                   AND EXISTS (
                       SELECT 1 FROM dataset_versions earlier
                        WHERE earlier.dataset_id = v.dataset_id
                          AND earlier.version < v.version
                          AND (
                              -- Empty historical fetch after a populated live one.
                              (v.record_count = 0 AND earlier.record_count > 0)
                              -- Historical fetch whose newest observation is
                              -- older than a previous version's. A live refresh
                              -- would be newer or equal, never older.
                              OR (
                                  v.latest_observed_at IS NOT NULL
                                  AND earlier.latest_observed_at IS NOT NULL
                                  AND v.latest_observed_at < earlier.latest_observed_at
                              )
                          )
                   )
                """
            )
        )

    repair_cell_metric_uniqueness()


def repair_cell_metric_uniqueness() -> None:
    """Bring an existing database up to the NULLS NOT DISTINCT constraint.

    ``create_all`` will not alter a constraint that already exists, so a
    database created before the fix keeps the permissive rule and keeps
    accumulating duplicate static metrics. Detect that, drop the duplicates
    keeping the most recently computed row, and rebuild.
    """
    with engine.begin() as conn:
        nulls_distinct = conn.execute(
            text(
                """
                SELECT i.indnullsnotdistinct
                  FROM pg_constraint c
                  JOIN pg_index i ON i.indexrelid = c.conindid
                 WHERE c.conname = 'uq_cell_metric'
                """
            )
        ).scalar()

        if nulls_distinct is None or nulls_distinct is True:
            return

        removed = conn.execute(
            text(
                """
                DELETE FROM cell_metrics cm
                 WHERE cm.id NOT IN (
                       SELECT DISTINCT ON (cell_id, metric, as_of_date) id
                         FROM cell_metrics
                        ORDER BY cell_id, metric, as_of_date, computed_at DESC, id DESC
                       )
                """
            )
        ).rowcount

        conn.execute(text("ALTER TABLE cell_metrics DROP CONSTRAINT uq_cell_metric"))
        conn.execute(
            text(
                "ALTER TABLE cell_metrics ADD CONSTRAINT uq_cell_metric "
                "UNIQUE NULLS NOT DISTINCT (cell_id, metric, as_of_date)"
            )
        )
        if removed:
            log.warning(
                "Removed %d duplicate cell_metrics rows left by the previous "
                "unique constraint, keeping the most recent value for each.",
                removed,
            )


def drop_all() -> None:
    """Tear down. Used by tests and ``make reset``."""
    with engine.begin() as conn:
        for view in _FEATURE_VIEWS:
            conn.execute(text(f"DROP VIEW IF EXISTS {view} CASCADE"))
    Base.metadata.drop_all(engine)
