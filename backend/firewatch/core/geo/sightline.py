"""Terrain line-of-sight, for reasoning about what can actually be seen.

Fire Watch has to answer where a fire would go unseen. Distance to the nearest
road is a poor answer to that question in steep coastal terrain: a gully 80 m
below a highway can be completely invisible from it, while a ridge 3 km away is
in plain view from half the municipality.

So visibility is computed properly, as a line-of-sight test against the DEM,
between a location and the places where people actually are. What is tested is
not the ground surface but a smoke column of a stated height, because that is
the thing a person would notice first.

Everything here is deterministic geometry: terrain plus, when supplied, typical
FBP canopy height. It says nothing about whether anyone is looking, what the
weather is doing, or whether smoke would be recognised and reported. Those
limits are stated with the metric.
"""

from __future__ import annotations

import math

import numpy as np

from firewatch.core.geo.terrain import TerrainModel

EARTH_RADIUS_M = 6371008.8

#: Height of the smoke column being tested for. An early-stage wildfire in
#: timber produces a visible column well above this, but a low, drifting smoke
#: from a smouldering surface fire may not.
DEFAULT_TARGET_HEIGHT_M = 10.0

#: Eye height of an observer standing on, or driving along, the ground.
DEFAULT_OBSERVER_HEIGHT_M = 2.0

#: Sight lines are sampled at roughly this spacing, bounded by the DEM's own
#: resolution. Finer sampling cannot reveal detail the DEM does not hold.
TARGET_SAMPLE_SPACING_M = 30.0
MAX_SAMPLES = 160

#: A road is a clearing. Canopy immediately beside the observer would block
#: every forest-road sightline at the first sample, which is not how a person
#: looking out from a travelled road actually sees.
ROAD_CORRIDOR_M = 50.0


def metres_per_degree(lat: float) -> tuple[float, float]:
    """(metres per degree of longitude, metres per degree of latitude)."""
    lat_rad = math.radians(lat)
    m_per_deg_lat = 111132.92 - 559.82 * math.cos(2 * lat_rad)
    m_per_deg_lon = 111412.84 * math.cos(lat_rad) - 93.5 * math.cos(3 * lat_rad)
    return abs(m_per_deg_lon), abs(m_per_deg_lat)


class SightlineEngine:
    """Line-of-sight tests against a stitched DEM.

    Built once per municipality and reused across every cell, because the
    expensive part is holding the elevation grid, not the ray tests.
    """

    def __init__(
        self,
        model: TerrainModel,
        observer_height_m: float = DEFAULT_OBSERVER_HEIGHT_M,
        target_height_m: float = DEFAULT_TARGET_HEIGHT_M,
        canopy_at=None,
    ) -> None:
        self.model = model
        self.observer_height_m = observer_height_m
        self.target_height_m = target_height_m
        #: Optional (lat, lon) -> canopy height in metres. Typical-for-class
        #: stand height, not measured LiDAR. Applied along the ray except in a
        #: short corridor around the observer, which is assumed to be a road.
        self.canopy_at = canopy_at
        self._elev = model.elevation
        self._rows, self._cols = self._elev.shape

    # -- grid sampling ------------------------------------------------------

    def _elevation_at(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        """Nearest-neighbour DEM lookup for arrays of coordinates."""
        col = np.rint((lon - self.model.west) / self.model.lon_per_px).astype(np.int64)
        row = np.rint((self.model.north - lat) / self.model.lat_per_px).astype(np.int64)
        np.clip(col, 0, self._cols - 1, out=col)
        np.clip(row, 0, self._rows - 1, out=row)
        return self._elev[row, col]

    def elevation_at_point(self, lat: float, lon: float) -> float:
        return float(
            self._elevation_at(np.array([lat]), np.array([lon]))[0]
        )

    # -- visibility ---------------------------------------------------------

    def visible_from(
        self,
        target_lat: float,
        target_lon: float,
        observer_lats: np.ndarray,
        observer_lons: np.ndarray,
    ) -> np.ndarray:
        """Which observers have a clear view of a smoke column at the target.

        Returns a boolean array aligned with the observer arrays. An empty
        observer set returns an empty array rather than an assumption.
        """
        n_obs = len(observer_lats)
        if n_obs == 0:
            return np.zeros(0, dtype=bool)

        m_lon, m_lat = metres_per_degree(target_lat)

        d_east = (observer_lons - target_lon) * m_lon
        d_north = (observer_lats - target_lat) * m_lat
        distances = np.hypot(d_east, d_north)

        target_ground = self.elevation_at_point(target_lat, target_lon)
        target_z = target_ground + self.target_height_m
        observer_z = (
            self._elevation_at(observer_lats, observer_lons) + self.observer_height_m
        )

        # One sample count for the whole batch keeps this a single array op.
        max_distance = float(distances.max())
        n_samples = int(
            min(MAX_SAMPLES, max(8.0, max_distance / TARGET_SAMPLE_SPACING_M))
        )

        # Interior samples only: the endpoints are the observer and the column
        # itself, and including them would let ground at either end block the
        # ray it defines.
        fractions = np.linspace(0.0, 1.0, n_samples + 2)[1:-1]

        # (n_obs, n_samples) grids of sample coordinates along each sight line.
        f = fractions[None, :]
        sample_lat = target_lat + (observer_lats[:, None] - target_lat) * f
        sample_lon = target_lon + (observer_lons[:, None] - target_lon) * f

        terrain = self._elevation_at(sample_lat, sample_lon)

        # The sight ray, linear in the horizontal, from column top to eye.
        ray = target_z + (observer_z[:, None] - target_z) * f

        # Earth curvature. The ray is a straight chord, and the ground between
        # its endpoints bulges *above* that chord by d1*d2/2R. The correction is
        # therefore added to the intervening terrain: it makes distant ground
        # more obstructive, not less, and it is what puts a horizon in the model
        # at all.
        #
        # It is negligible at close range, 0.18 m over 3 km, but reaches 8 m at
        # 15 km, which is comparable to the smoke column being tested for.
        along = distances[:, None] * f
        remaining = distances[:, None] - along
        curvature_bulge = (along * remaining) / (2.0 * EARTH_RADIUS_M)

        canopy = np.zeros_like(terrain)
        if self.canopy_at is not None:
            canopy = np.asarray(self.canopy_at(sample_lat, sample_lon), dtype=float)
            # f=1 is the observer. Drop canopy in the last ROAD_CORRIDOR_M of
            # each ray so the road itself is not treated as a wall of timber.
            near_observer = along >= (distances[:, None] - ROAD_CORRIDOR_M)
            canopy = np.where(near_observer, 0.0, canopy)

        blocked = (terrain + canopy + curvature_bulge) >= ray
        return ~blocked.any(axis=1)

    def observability(
        self,
        target_lat: float,
        target_lon: float,
        observer_lats: np.ndarray,
        observer_lons: np.ndarray,
        max_range_m: float,
    ) -> "Observability":
        """How well observed a location is, from a set of observer points.

        Observers are weighted by distance: a clear view from 300 m away is
        worth far more for early detection than a clear view from 4 km, where a
        small column is a smudge on a hillside among many.
        """
        n_obs = len(observer_lats)
        if n_obs == 0:
            return Observability(
                observers_tested=0,
                observers_visible=0,
                weighted_visibility=None,
                nearest_visible_m=None,
                nearest_observer_m=None,
            )

        m_lon, m_lat = metres_per_degree(target_lat)
        distances = np.hypot(
            (observer_lons - target_lon) * m_lon,
            (observer_lats - target_lat) * m_lat,
        )

        visible = self.visible_from(
            target_lat, target_lon, observer_lats, observer_lons
        )

        # Linear falloff to the stated maximum useful range.
        weights = np.clip(1.0 - distances / max_range_m, 0.0, 1.0)
        total = float(weights.sum())
        weighted = float(weights[visible].sum()) / total if total > 0 else None

        visible_distances = distances[visible]
        return Observability(
            observers_tested=int(n_obs),
            observers_visible=int(visible.sum()),
            weighted_visibility=weighted,
            nearest_visible_m=(
                float(visible_distances.min()) if visible_distances.size else None
            ),
            nearest_observer_m=float(distances.min()),
        )


class Observability:
    """Result of an observability test, kept explicit for the evidence panel."""

    __slots__ = (
        "observers_tested",
        "observers_visible",
        "weighted_visibility",
        "nearest_visible_m",
        "nearest_observer_m",
    )

    def __init__(
        self,
        observers_tested: int,
        observers_visible: int,
        weighted_visibility: float | None,
        nearest_visible_m: float | None,
        nearest_observer_m: float | None,
    ) -> None:
        self.observers_tested = observers_tested
        self.observers_visible = observers_visible
        self.weighted_visibility = weighted_visibility
        self.nearest_visible_m = nearest_visible_m
        self.nearest_observer_m = nearest_observer_m


def sample_along_lines(
    coords: list[list[tuple[float, float]]], spacing_m: float
) -> tuple[np.ndarray, np.ndarray]:
    """Resample line geometries into evenly spaced (lat, lon) observer points.

    Road vertex density reflects how curvy a road is, not how much of it people
    travel, so vertices are resampled to even ground spacing before being used
    as observers.
    """
    lats: list[float] = []
    lons: list[float] = []

    for line in coords:
        if not line:
            continue
        lats.append(line[0][1])
        lons.append(line[0][0])
        carried = 0.0
        for (lon_a, lat_a), (lon_b, lat_b) in zip(line, line[1:]):
            m_lon, m_lat = metres_per_degree((lat_a + lat_b) / 2.0)
            seg_m = math.hypot((lon_b - lon_a) * m_lon, (lat_b - lat_a) * m_lat)
            if seg_m <= 0:
                continue
            walked = spacing_m - carried
            while walked < seg_m:
                t = walked / seg_m
                lats.append(lat_a + (lat_b - lat_a) * t)
                lons.append(lon_a + (lon_b - lon_a) * t)
                walked += spacing_m
            carried = (carried + seg_m) % spacing_m

    return np.asarray(lats, dtype=np.float64), np.asarray(lons, dtype=np.float64)
