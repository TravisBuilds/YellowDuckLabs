"""Terrain derivation from Terrarium-encoded elevation tiles.

Slope and aspect drive fire spread more than almost any other static factor, so
this needs to be real rather than assumed. Terrarium PNG tiles are used because
they are keyless and globally available, which keeps terrain derivation portable
to any municipality. Where a municipality publishes LiDAR bare-earth (West
Vancouver does), substituting it is a meaningful accuracy upgrade and is
recorded as a data gap.

Terrarium encoding: ``elevation_m = R * 256 + G + B / 256 - 32768``.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

TILE_PX = 256
EARTH_CIRCUMFERENCE_M = 2 * math.pi * 6378137.0


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tile_to_lonlat(x: int, y: int, z: int) -> tuple[float, float]:
    """North-west corner of a tile."""
    n = 2**z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


def ground_resolution_m(lat: float, z: int) -> float:
    """Metres per pixel for 256 px web-mercator tiles at a given latitude."""
    return EARTH_CIRCUMFERENCE_M * math.cos(math.radians(lat)) / (TILE_PX * 2**z)


def decode_terrarium(image: Image.Image) -> np.ndarray:
    arr = np.asarray(image.convert("RGB")).astype(np.float64)
    return arr[:, :, 0] * 256.0 + arr[:, :, 1] + arr[:, :, 2] / 256.0 - 32768.0


@dataclass
class TerrainSample:
    elevation_m: float | None
    slope_deg: float | None
    aspect_deg: float | None
    #: Terrain Ruggedness Index in metres: mean absolute elevation difference to
    #: the eight neighbours. Higher means rougher, harder ground to work.
    ruggedness_m: float | None


class TerrainModel:
    """A stitched DEM over one municipality, with derived slope and aspect."""

    def __init__(
        self,
        elevation: np.ndarray,
        west: float,
        north: float,
        lon_per_px: float,
        lat_per_px: float,
        pixel_m: float,
        zoom: int,
        tile_count: int,
    ) -> None:
        self.elevation = elevation
        self.west = west
        self.north = north
        self.lon_per_px = lon_per_px
        self.lat_per_px = lat_per_px
        self.pixel_m = pixel_m
        self.zoom = zoom
        self.tile_count = tile_count

        # Gradients in metres per metre. np.gradient's first result is along
        # axis 0, the row axis, which increases southward, so negating it gives
        # the true northing gradient.
        dz_drow, dz_deast = np.gradient(elevation, pixel_m, pixel_m)
        dz_dnorth = -dz_drow

        self.slope_deg = np.degrees(np.arctan(np.hypot(dz_deast, dz_dnorth)))

        # Aspect is the compass bearing of the downslope direction, which is the
        # negative gradient vector (-dz/dE, -dz/dN). A bearing is measured from
        # north and turns clockwise, so it is atan2(east, north), not the usual
        # atan2(y, x).
        aspect = np.degrees(np.arctan2(-dz_deast, -dz_dnorth)) % 360.0
        # Flat ground has no downslope direction. Leaving it as an arbitrary
        # bearing would let the aspect dryness factor fire on level terrain.
        aspect[np.hypot(dz_deast, dz_dnorth) < 1e-9] = np.nan
        self.aspect_deg = aspect

        self.ruggedness_m = self._ruggedness(elevation)

    @staticmethod
    def _ruggedness(elevation: np.ndarray) -> np.ndarray:
        padded = np.pad(elevation, 1, mode="edge")
        total = np.zeros_like(elevation, dtype=np.float64)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                shifted = padded[1 + dy : 1 + dy + elevation.shape[0],
                                 1 + dx : 1 + dx + elevation.shape[1]]
                total += np.abs(shifted - elevation)
        return total / 8.0

    def _pixel_for(self, lat: float, lon: float) -> tuple[int, int] | None:
        col = int((lon - self.west) / self.lon_per_px)
        row = int((self.north - lat) / self.lat_per_px)
        if 0 <= row < self.elevation.shape[0] and 0 <= col < self.elevation.shape[1]:
            return row, col
        return None

    def sample(self, lat: float, lon: float) -> TerrainSample:
        px = self._pixel_for(lat, lon)
        if px is None:
            return TerrainSample(None, None, None, None)
        row, col = px
        aspect = self.aspect_deg[row, col]
        return TerrainSample(
            elevation_m=float(self.elevation[row, col]),
            slope_deg=float(self.slope_deg[row, col]),
            aspect_deg=None if math.isnan(aspect) else float(aspect),
            ruggedness_m=float(self.ruggedness_m[row, col]),
        )

    def sample_window(self, lat: float, lon: float, radius_px: int = 2) -> TerrainSample:
        """Aggregate over a small window.

        An H3 resolution-10 cell is a few pixels across at zoom 13, so a single
        pixel would under-report the slope actually present in the cell. We take
        the maximum slope and mean ruggedness, which is the conservative
        reading for spread potential.
        """
        px = self._pixel_for(lat, lon)
        if px is None:
            return TerrainSample(None, None, None, None)
        row, col = px
        r0, r1 = max(0, row - radius_px), min(self.elevation.shape[0], row + radius_px + 1)
        c0, c1 = max(0, col - radius_px), min(self.elevation.shape[1], col + radius_px + 1)

        slope_win = self.slope_deg[r0:r1, c0:c1]
        aspect_win = self.aspect_deg[r0:r1, c0:c1]

        # Circular mean, so aspects either side of north do not average to south.
        finite = aspect_win[np.isfinite(aspect_win)]
        if finite.size:
            rad = np.radians(finite)
            aspect_mean = float(
                (math.degrees(math.atan2(np.sin(rad).mean(), np.cos(rad).mean()))) % 360.0
            )
        else:
            aspect_mean = None

        return TerrainSample(
            elevation_m=float(self.elevation[r0:r1, c0:c1].mean()),
            slope_deg=float(slope_win.max()),
            aspect_deg=aspect_mean,
            ruggedness_m=float(self.ruggedness_m[r0:r1, c0:c1].mean()),
        )


def build_terrain_model(
    bounds: tuple[float, float, float, float],
    zoom: int,
    url_template: str,
    fetch: "callable[[str], bytes]",
    cache_dir: Path | None = None,
) -> tuple[TerrainModel, list[str]]:
    """Fetch and stitch tiles covering ``bounds`` = (west, south, east, north).

    Returns the model plus the list of tile URLs used, for provenance.
    """
    west, south, east, north = bounds
    x0, y0 = lonlat_to_tile(west, north, zoom)
    x1, y1 = lonlat_to_tile(east, south, zoom)
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)

    n_tiles = (x1 - x0 + 1) * (y1 - y0 + 1)
    if n_tiles > 400:
        raise ValueError(
            f"Refusing to fetch {n_tiles} terrain tiles at zoom {zoom}; "
            "lower the zoom or reduce the area."
        )

    rows: list[list[np.ndarray]] = []
    urls: list[str] = []
    for ty in range(y0, y1 + 1):
        row_tiles: list[np.ndarray] = []
        for tx in range(x0, x1 + 1):
            url = url_template.format(z=zoom, x=tx, y=ty)
            urls.append(url)
            data = _cached_fetch(url, fetch, cache_dir)
            if data is None:
                row_tiles.append(np.full((TILE_PX, TILE_PX), np.nan))
            else:
                row_tiles.append(decode_terrarium(Image.open(BytesIO(data))))
        rows.append(row_tiles)

    elevation = np.vstack([np.hstack(r) for r in rows])

    # Fill any failed tiles so gradients stay finite; such areas are reported as
    # missing terrain rather than silently treated as flat.
    if np.isnan(elevation).any():
        elevation = np.nan_to_num(elevation, nan=float(np.nanmin(elevation)))

    nw_lon, nw_lat = tile_to_lonlat(x0, y0, zoom)
    se_lon, se_lat = tile_to_lonlat(x1 + 1, y1 + 1, zoom)
    lon_per_px = (se_lon - nw_lon) / elevation.shape[1]
    lat_per_px = (nw_lat - se_lat) / elevation.shape[0]
    pixel_m = ground_resolution_m((south + north) / 2.0, zoom)

    model = TerrainModel(
        elevation=elevation,
        west=nw_lon,
        north=nw_lat,
        lon_per_px=lon_per_px,
        lat_per_px=lat_per_px,
        pixel_m=pixel_m,
        zoom=zoom,
        tile_count=n_tiles,
    )
    return model, urls


def _cached_fetch(url: str, fetch, cache_dir: Path | None) -> bytes | None:
    if cache_dir is None:
        try:
            return fetch(url)
        except Exception:
            return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()[:32]
    path = cache_dir / f"terrain-{key}.png"
    if path.exists():
        return path.read_bytes()
    try:
        data = fetch(url)
    except Exception:
        return None
    path.write_bytes(data)
    return data
