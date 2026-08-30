"""Elevation tiles -> terrain model.

This adapter does not store vector features. It retrieves and caches the DEM
tiles covering the municipality, records provenance and coverage, and exposes a
loader that the cell-derivation step uses to compute slope, aspect, elevation
and ruggedness.
"""

from __future__ import annotations

from datetime import timedelta

from firewatch.config import settings
from firewatch.core.geo.terrain import TerrainModel, build_terrain_model
from firewatch.core.municipality import SourceConfig
from firewatch.sources.base import (
    DatasetManifest,
    DataStatus,
    IngestContext,
    NormalizedFeatures,
    RawDataset,
    SourceAdapter,
    content_hash,
)
from firewatch.sources.http import SourceUnavailable, get_bytes

DEFAULT_TEMPLATE = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"


def _fetch_tile(url: str) -> bytes:
    return get_bytes(url, retries=1)


def load_terrain_model(
    ctx: IngestContext, config: SourceConfig
) -> tuple[TerrainModel, list[str]]:
    """Build the terrain model, reusing the on-disk tile cache."""
    template = str(config.params.get("url_template", DEFAULT_TEMPLATE))
    zoom = int(config.params.get("zoom", 13))
    return build_terrain_model(
        bounds=ctx.bounds,
        zoom=zoom,
        url_template=template,
        fetch=_fetch_tile,
        cache_dir=settings.firewatch_cache_dir / "terrain",
    )


class TerrainTilesAdapter(SourceAdapter):
    adapter_id = "terrain_tiles"
    produces_features = False
    # Elevation is effectively time-invariant at our resolution.
    aging_after = timedelta(days=365 * 20)
    stale_after = timedelta(days=365 * 50)

    def discover(self, ctx: IngestContext) -> DatasetManifest:
        zoom = int(self.params.get("zoom", 13))
        from firewatch.core.geo.terrain import ground_resolution_m

        _, south, _, north = ctx.bounds
        resolution = ground_resolution_m((south + north) / 2.0, zoom)
        return DatasetManifest(
            source_id=self.source_id,
            title="Elevation tiles (Terrarium-encoded DEM)",
            source_url=str(self.params.get("url_template", DEFAULT_TEMPLATE)),
            licence=self.params.get("licence"),
            licence_url=self.params.get("licence_url"),
            attribution=self.params.get("attribution"),
            spatial_resolution=f"~{resolution:.1f} m per pixel at zoom {zoom}",
            temporal_resolution="Static composite",
            caveats=[
                "This is a global composite DEM, chosen because it is keyless and "
                "works for any municipality. It is not a substitute for local "
                "bare-earth LiDAR where that exists.",
                "Slope and aspect are derived, not observed. They inherit any "
                "error in the underlying elevation composite.",
            ],
        )

    def fetch(self, ctx: IngestContext) -> RawDataset:
        try:
            model, urls = load_terrain_model(ctx, self.config)
        except Exception as exc:
            raise SourceUnavailable(f"Terrain tiles unavailable: {exc}") from exc

        return RawDataset(
            payload={
                "tile_count": model.tile_count,
                "pixel_m": model.pixel_m,
                "zoom": model.zoom,
                "shape": list(model.elevation.shape),
                "elevation_min_m": float(model.elevation.min()),
                "elevation_max_m": float(model.elevation.max()),
            },
            request_url=urls[0] if urls else "",
            content_hash=content_hash(sorted(urls)),
            notes=[
                f"{model.tile_count} tiles stitched to "
                f"{model.elevation.shape[1]}x{model.elevation.shape[0]} px at "
                f"~{model.pixel_m:.1f} m/px. Elevation range "
                f"{model.elevation.min():.0f}-{model.elevation.max():.0f} m."
            ],
        )

    def normalize(self, raw: RawDataset, ctx: IngestContext) -> NormalizedFeatures:
        # Terrain is consumed as a raster by the derivation step, not stored as
        # features. Per-cell terrain values land in cell_metrics with their own
        # provenance.
        return NormalizedFeatures(features=[])

    def status_for(self, latest_observed_at, raw, report):
        payload = raw.payload or {}
        return DataStatus.CURRENT, (
            f"DEM covering the municipality: {payload.get('tile_count')} tiles at "
            f"~{payload.get('pixel_m', 0):.0f} m/px, elevation "
            f"{payload.get('elevation_min_m', 0):.0f}-"
            f"{payload.get('elevation_max_m', 0):.0f} m."
        )
