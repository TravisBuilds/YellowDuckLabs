"""OGC WCS 2.0 raster extracts.

Used for the CWFIS national FBP fuel grid: a 100 m GeoTIFF in Canada Atlas
Lambert (EPSG:3978). The adapter does not store vector features. It caches the
municipal extract and exposes a ``FuelModel`` for derivation, the same pattern
as the terrain tiles.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import numpy as np
from PIL import Image
from pyproj import Transformer

from firewatch.config import settings
from firewatch.core.fuels import FuelModel, classify
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

DEFAULT_WCS = "https://cwfis.cfs.nrcan.gc.ca/geoserver/public/wcs"
DEFAULT_COVERAGE = "public__cffdrs_fbp_fuel_types_100m"
DEFAULT_CRS = "EPSG:3978"
#: Axis labels as advertised by DescribeCoverage for this grid.
DEFAULT_X_AXIS = "E"
DEFAULT_Y_AXIS = "N"
PAD_M = 2000.0


def _cache_path(municipality_id: str, coverage_id: str) -> Path:
    safe = coverage_id.replace(":", "_")
    directory = settings.firewatch_cache_dir / "fuels"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{municipality_id}_{safe}.tif"


def _bbox_native(bounds: tuple[float, float, float, float], crs: str) -> tuple[float, float, float, float]:
    west, south, east, north = bounds
    to_native = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    corners = [
        to_native.transform(lon, lat)
        for lon, lat in (
            (west, south),
            (west, north),
            (east, south),
            (east, north),
        )
    ]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return min(xs) - PAD_M, min(ys) - PAD_M, max(xs) + PAD_M, max(ys) + PAD_M


def _read_geotiff(path: Path, native_crs: str) -> FuelModel:
    image = Image.open(path)
    codes = np.array(image)
    # Pillow promotes 16-bit signed to int32; nodata is -9999.
    transform = image.tag_v2.get(34264)
    if transform is None:
        raise SourceUnavailable(f"{path} has no ModelTransformation tag")
    # Affine: E = a*col + x0, N = e*row + y0 for a north-up 3978 grid.
    pixel_e = float(transform[0])
    pixel_n = float(transform[5])
    west_m = float(transform[3])
    north_m = float(transform[7])
    pixel_m = abs(pixel_e)
    if abs(pixel_n) and abs(abs(pixel_n) - pixel_m) > 1.0:
        # Non-square pixels would silently distort sampling.
        pixel_m = (abs(pixel_e) + abs(pixel_n)) / 2.0
    return FuelModel(
        codes=codes,
        west_m=west_m,
        north_m=north_m,
        pixel_m=pixel_m,
        native_crs=native_crs,
    )


def load_fuel_model(ctx: IngestContext, config: SourceConfig) -> FuelModel:
    coverage = str(config.params.get("coverage_id", DEFAULT_COVERAGE))
    path = _cache_path(ctx.municipality.id, coverage)
    if not path.exists():
        raise SourceUnavailable(
            f"No cached fuel extract at {path}. Ingest the fuel source first."
        )
    return _read_geotiff(path, str(config.params.get("native_crs", DEFAULT_CRS)))


class WcsRasterAdapter(SourceAdapter):
    adapter_id = "wcs_raster"
    produces_features = False
    aging_after = timedelta(days=365)
    stale_after = timedelta(days=365 * 4)

    def discover(self, ctx: IngestContext) -> DatasetManifest:
        return DatasetManifest(
            source_id=self.source_id,
            title=str(self.params.get("title") or "WCS raster"),
            source_url=str(self.params.get("base_url", DEFAULT_WCS)),
            licence=self.params.get("licence"),
            licence_url=self.params.get("licence_url"),
            attribution=self.params.get("attribution"),
            spatial_resolution=str(self.params.get("spatial_resolution", "100 m")),
            temporal_resolution=str(self.params.get("temporal_resolution", "Static national grid")),
            caveats=list(self.config.caveats or []),
        )

    def fetch(self, ctx: IngestContext) -> RawDataset:
        base = str(self.params.get("base_url", DEFAULT_WCS))
        coverage = str(self.params.get("coverage_id", DEFAULT_COVERAGE))
        crs = str(self.params.get("native_crs", DEFAULT_CRS))
        x_axis = str(self.params.get("x_axis", DEFAULT_X_AXIS))
        y_axis = str(self.params.get("y_axis", DEFAULT_Y_AXIS))

        xmin, ymin, xmax, ymax = _bbox_native(ctx.bounds, crs)
        params = {
            "service": "WCS",
            "version": "2.0.1",
            "request": "GetCoverage",
            "coverageId": coverage,
            "format": "image/tiff",
            "subset": [
                f"{x_axis}({xmin:.1f},{xmax:.1f})",
                f"{y_axis}({ymin:.1f},{ymax:.1f})",
            ],
        }
        try:
            payload = get_bytes(base, params=params, timeout=120.0, retries=1)
        except Exception as exc:
            raise SourceUnavailable(f"WCS GetCoverage failed for {coverage}: {exc}") from exc

        if payload[:4] not in (b"II*\x00", b"MM\x00*") and b"<" in payload[:80]:
            raise SourceUnavailable(
                f"WCS returned markup instead of a GeoTIFF for {coverage}"
            )

        path = _cache_path(ctx.municipality.id, coverage)
        path.write_bytes(payload)
        model = _read_geotiff(path, crs)

        valid = model.codes[model.codes != -9999]
        present: dict[str, int] = {}
        if valid.size:
            values, counts = np.unique(valid, return_counts=True)
            for code, count in zip(values.tolist(), counts.tolist()):
                spec = classify(int(code))
                key = spec.fbp_class if spec else str(int(code))
                present[key] = present.get(key, 0) + int(count)

        request_url = (
            f"{base}?service=WCS&version=2.0.1&request=GetCoverage"
            f"&coverageId={coverage}"
        )
        return RawDataset(
            payload={
                "path": str(path),
                "shape": list(model.codes.shape),
                "pixel_m": model.pixel_m,
                "classes": present,
                "valid_pixels": int(valid.size),
            },
            request_url=request_url,
            content_hash=content_hash(payload),
            notes=[
                f"Cached {model.codes.shape[1]}x{model.codes.shape[0]} px extract "
                f"at {model.pixel_m:.0f} m. Classes present: "
                + ", ".join(f"{k}={v}" for k, v in sorted(present.items()))
            ],
        )

    def normalize(self, raw: RawDataset, ctx: IngestContext) -> NormalizedFeatures:
        return NormalizedFeatures(features=[])

    def status_for(self, latest_observed_at, raw, report):
        payload = raw.payload or {}
        classes = payload.get("classes") or {}
        return DataStatus.CURRENT, (
            f"National FBP fuel extract: {payload.get('valid_pixels', 0)} cells at "
            f"~{payload.get('pixel_m', 100):.0f} m. "
            + (
                "Dominant classes: "
                + ", ".join(
                    f"{k}"
                    for k, _ in sorted(classes.items(), key=lambda kv: -kv[1])[:5]
                )
                if classes
                else "No classified pixels."
            )
        )
