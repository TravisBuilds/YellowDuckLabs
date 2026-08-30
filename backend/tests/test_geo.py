"""Geometry, terrain and line-of-sight.

These are the parts of Fire Watch where a silent error is most dangerous,
because a wrong slope or a wrong visibility answer is still a plausible-looking
number. So they are tested against geometry whose answer is known in advance
rather than against a stored snapshot of previous output.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from PIL import Image
from shapely.geometry import Point

from firewatch.core.geo.crs import area_m2, buffer_meters, distance_m, utm_crs_for
from firewatch.core.geo.grid import approximate_edge_m, cell_for_point, generate_grid
from firewatch.core.geo.sightline import (
    SightlineEngine,
    metres_per_degree,
    sample_along_lines,
)
from firewatch.core.geo.terrain import TerrainModel, decode_terrarium, ground_resolution_m

# West Vancouver, roughly the middle of the municipality.
LAT, LON = 49.36, -123.16
CRS10 = "EPSG:32610"


# --------------------------------------------------------------------------- #
# CRS and measurement
# --------------------------------------------------------------------------- #


def test_utm_zone_selection_follows_longitude():
    """A municipality must not be measured in another zone's grid."""
    assert utm_crs_for(-123.16, 49.36) == "EPSG:32610"  # West Vancouver
    assert utm_crs_for(-119.49, 49.89) == "EPSG:32611"  # Kelowna
    assert utm_crs_for(-79.38, 43.65) == "EPSG:32617"  # Toronto
    # Southern hemisphere uses the 327xx band.
    assert utm_crs_for(151.2, -33.87) == "EPSG:32756"


def test_buffer_is_metric_not_degrees():
    """A 1 km buffer must be 1 km on the ground, not 1 km of longitude.

    At 49 degrees north a degree of longitude is about two thirds of a degree of
    latitude, so a naive degree buffer would be badly wrong east-west. This is
    the single most common silent geospatial error and it would corrupt every
    distance metric in the system.
    """
    centre = Point(LON, LAT)
    buffered = buffer_meters(centre, 1000.0, CRS10)

    north = distance_m(centre, Point(LON, buffered.bounds[3]), CRS10)
    east = distance_m(centre, Point(buffered.bounds[2], LAT), CRS10)

    assert north == pytest.approx(1000.0, rel=0.02)
    assert east == pytest.approx(1000.0, rel=0.02)


def test_area_is_measured_in_true_square_metres():
    """A 5 km-radius disc buffered into a square envelope is 100 km2."""
    square = buffer_meters(Point(LON, LAT), 5000.0, CRS10).envelope
    assert area_m2(square, CRS10) / 1e6 == pytest.approx(100.0, rel=0.02)


def test_distance_matches_known_separation():
    """Lions Gate Bridge to Horseshoe Bay is about 11.7 km straight-line.

    Chosen because it spans nearly the whole municipality east to west, so an
    error in the projection would show up plainly.
    """
    lions_gate = Point(-123.1400, 49.3143)
    horseshoe_bay = Point(-123.2735, 49.3736)
    km = distance_m(lions_gate, horseshoe_bay, CRS10) / 1000.0
    assert km == pytest.approx(11.7, abs=0.4)


# --------------------------------------------------------------------------- #
# H3 grid
# --------------------------------------------------------------------------- #


def test_grid_covers_boundary_and_flags_the_buffer():
    boundary = buffer_meters(Point(LON, LAT), 2000.0, CRS10)
    buffered = buffer_meters(boundary, 1000.0, CRS10)
    cells = generate_grid(boundary, 9, CRS10, buffered)

    assert cells, "a 2 km-radius boundary must produce cells at resolution 9"

    inside = [c for c in cells if c.within_boundary]
    outside = [c for c in cells if not c.within_boundary]

    assert inside, "cells inside the legal boundary must be flagged as such"
    assert outside, "the buffer must produce cells outside the legal boundary"

    # Every cell flagged inside must actually touch the boundary, or the
    # within_boundary distinction is meaningless everywhere downstream.
    for cell in inside:
        assert cell.polygon.intersects(boundary)

    # The union of inside cells must fully cover the boundary. An eroded edge
    # would silently drop the outermost interface, which is exactly where the
    # houses are.
    covered = sum(c.area_m2 for c in inside)
    assert covered >= area_m2(boundary, CRS10)


def test_grid_area_is_close_to_the_h3_nominal_area():
    boundary = buffer_meters(Point(LON, LAT), 3000.0, CRS10)
    cells = generate_grid(boundary, 9, CRS10, None)
    mean_area = sum(c.area_m2 for c in cells) / len(cells)
    edge = approximate_edge_m(9)
    # A regular hexagon of edge e has area 3*sqrt(3)/2 * e^2.
    nominal = 1.5 * math.sqrt(3.0) * edge**2
    assert mean_area == pytest.approx(nominal, rel=0.35)


def test_grid_resolution_changes_cell_count_as_expected():
    """Each H3 resolution step is roughly a 7x change in cell count."""
    boundary = buffer_meters(Point(LON, LAT), 3000.0, CRS10)
    coarse = generate_grid(boundary, 8, CRS10, None)
    fine = generate_grid(boundary, 9, CRS10, None)
    assert 4.0 < len(fine) / len(coarse) < 10.0


def test_grid_cells_are_unique():
    boundary = buffer_meters(Point(LON, LAT), 2000.0, CRS10)
    buffered = buffer_meters(boundary, 500.0, CRS10)
    cells = generate_grid(boundary, 9, CRS10, buffered)
    indexes = [c.h3_index for c in cells]
    assert len(indexes) == len(set(indexes))


def test_point_lookup_agrees_with_the_generated_grid():
    """The cell a click resolves to must be a cell the grid actually holds."""
    boundary = buffer_meters(Point(LON, LAT), 2000.0, CRS10)
    cells = {c.h3_index for c in generate_grid(boundary, 9, CRS10, None)}
    assert cell_for_point(LAT, LON, 9) in cells


# --------------------------------------------------------------------------- #
# Terrain
# --------------------------------------------------------------------------- #


def _terrarium_image(elevation: np.ndarray) -> Image.Image:
    packed = elevation + 32768.0
    r = np.floor(packed / 256.0)
    g = np.floor(packed - r * 256.0)
    b = np.round((packed - r * 256.0 - g) * 256.0)
    rgb = np.stack([r, g, b], axis=-1).astype(np.uint8)
    return Image.fromarray(rgb, mode="RGB")


def test_terrarium_decoding_round_trips_known_elevations():
    """Terrarium packs elevation as (r * 256 + g + b / 256) - 32768."""
    values = np.array([[-100.0, 0.0, 1.5, 500.0, 1200.0, 3000.0]])
    decoded = decode_terrarium(_terrarium_image(values))
    assert np.allclose(decoded, values, atol=0.02)


def test_ground_resolution_shrinks_with_latitude_and_zoom():
    """Zoom 13 near West Vancouver is about 12 m per pixel."""
    assert ground_resolution_m(LAT, 13) == pytest.approx(12.4, rel=0.1)
    # Each zoom level halves the ground size of a pixel.
    assert ground_resolution_m(LAT, 14) == pytest.approx(
        ground_resolution_m(LAT, 13) / 2, rel=1e-6
    )
    # A pixel covers more ground at the equator than at high latitude.
    assert ground_resolution_m(0.0, 13) > ground_resolution_m(60.0, 13)


def _model(elevation: np.ndarray, pixel_m: float = 30.0) -> TerrainModel:
    rows, cols = elevation.shape
    lat_per_px = pixel_m / 111320.0
    lon_per_px = pixel_m / (111320.0 * math.cos(math.radians(LAT)))
    return TerrainModel(
        elevation=elevation,
        west=LON - cols * lon_per_px / 2,
        north=LAT + rows * lat_per_px / 2,
        lon_per_px=lon_per_px,
        lat_per_px=lat_per_px,
        pixel_m=pixel_m,
        zoom=13,
        tile_count=1,
    )


def test_slope_of_a_flat_surface_is_zero():
    model = _model(np.full((16, 16), 300.0))
    assert np.allclose(model.slope_deg, 0.0, atol=1e-6)
    # A flat surface has no downslope direction, so aspect must be undefined
    # rather than an arbitrary bearing.
    assert np.isnan(model.aspect_deg).all()


def test_slope_of_a_known_ramp():
    """A 30 m rise over a 30 m pixel is 45 degrees."""
    ramp = np.tile(np.arange(16, dtype=float) * 30.0, (16, 1)).T
    model = _model(ramp)
    assert np.allclose(model.slope_deg[2:-2, 2:-2], 45.0, atol=0.5)
    # Elevation increases with row index, i.e. southward, so downhill is north.
    assert np.allclose(model.aspect_deg[2:-2, 2:-2], 0.0, atol=1.0)


def test_aspect_of_a_south_facing_slope():
    """South-facing slopes carry the driest fuels, so this must be right."""
    # Elevation falling with increasing row index means downhill is south.
    ramp = np.tile(np.arange(16, dtype=float)[::-1] * 30.0, (16, 1)).T
    model = _model(ramp)
    assert np.allclose(model.aspect_deg[2:-2, 2:-2], 180.0, atol=1.0)


def test_aspect_of_a_west_facing_slope():
    ramp = np.tile(np.arange(16, dtype=float) * 30.0, (16, 1))
    model = _model(ramp)
    assert np.allclose(model.aspect_deg[2:-2, 2:-2], 270.0, atol=1.0)


def test_aspect_of_an_east_facing_slope():
    ramp = np.tile(np.arange(16, dtype=float)[::-1] * 30.0, (16, 1))
    model = _model(ramp)
    assert np.allclose(model.aspect_deg[2:-2, 2:-2], 90.0, atol=1.0)


def test_north_and_south_aspects_are_not_swapped():
    """Guards a real bug: the northing gradient was double-negated.

    The symptom was silent. Aspect was correct east-west and inverted
    north-south, so every south-facing slope was reported as north-facing. The
    aspect dryness factor feeds ignition likelihood, so the effect was to
    systematically credit the driest slopes to the wettest ones.
    """
    rising_southward = np.tile(np.arange(16, dtype=float) * 30.0, (16, 1)).T
    rising_northward = rising_southward[::-1].copy()

    downhill_north = _model(rising_southward).aspect_deg[2:-2, 2:-2]
    downhill_south = _model(rising_northward).aspect_deg[2:-2, 2:-2]

    assert np.allclose(downhill_north, 0.0, atol=1.0)
    assert np.allclose(downhill_south, 180.0, atol=1.0)


def test_ruggedness_separates_smooth_from_broken_ground():
    smooth = _model(np.tile(np.arange(32, dtype=float) * 5.0, (32, 1)))
    broken = _model(
        np.tile(np.arange(32, dtype=float) * 5.0, (32, 1))
        + np.random.default_rng(0).normal(0.0, 40.0, (32, 32))
    )
    assert broken.ruggedness_m[4:-4, 4:-4].mean() > (
        smooth.ruggedness_m[4:-4, 4:-4].mean() * 3
    )


def test_window_sampling_reports_the_steepest_slope_in_the_cell():
    """A cell is several pixels across, and the conservative read is the max."""
    elevation = np.full((64, 64), 100.0)
    elevation[30:34, 30:34] = 400.0  # a small steep knob
    model = _model(elevation)

    single = model.sample(LAT, LON)
    window = model.sample_window(LAT, LON, radius_px=4)
    assert window.slope_deg >= single.slope_deg


def test_sampling_outside_the_dem_returns_no_answer():
    """Off-DEM must be None, never a default that reads as flat ground."""
    model = _model(np.full((16, 16), 100.0))
    sample = model.sample(0.0, 0.0)
    assert sample.elevation_m is None
    assert sample.slope_deg is None
    assert sample.ruggedness_m is None


# --------------------------------------------------------------------------- #
# Line of sight
#
# The whole observation-gap argument rests on this, so it is tested against
# cases whose answers are not in doubt: a flat plain, a ridge in the way, and a
# gully below a road.
# --------------------------------------------------------------------------- #


def test_flat_ground_is_always_visible():
    engine = SightlineEngine(_model(np.full((200, 200), 100.0)))
    lats = np.array([LAT + 0.005, LAT - 0.005])
    lons = np.array([LON, LON + 0.005])
    assert engine.visible_from(LAT, LON, lats, lons).all()


def test_forest_canopy_blocks_a_sightline_that_terrain_alone_would_pass():
    """A 25 m stand on flat ground hides a 10 m column from a distant road."""
    model = _model(np.full((200, 200), 100.0))
    observer_lat = LAT + 0.004  # ~450 m, well beyond the road corridor
    observer_lon = LON

    open_air = SightlineEngine(model)
    assert open_air.visible_from(
        LAT, LON, np.array([observer_lat]), np.array([observer_lon])
    )[0]

    forested = SightlineEngine(
        model, canopy_at=lambda lat, lon: np.full(lat.shape, 25.0)
    )
    assert not forested.visible_from(
        LAT, LON, np.array([observer_lat]), np.array([observer_lon])
    )[0]


def test_canopy_beside_the_road_does_not_wall_off_the_observer():
    """The first tens of metres from a travelled road are treated as a clearing."""
    model = _model(np.full((200, 200), 100.0))
    # ~30 m: entirely inside ROAD_CORRIDOR_M, so canopy is dropped.
    observer_lat = LAT + 0.00027
    forested = SightlineEngine(
        model, canopy_at=lambda lat, lon: np.full(lat.shape, 25.0)
    )
    assert forested.visible_from(
        LAT, LON, np.array([observer_lat]), np.array([LON])
    )[0]


def test_a_ridge_blocks_the_view_behind_it():
    """A 300 m ridge between observer and target hides a 10 m smoke column."""
    elevation = np.full((200, 200), 100.0)
    elevation[:, 98:102] = 400.0  # a north-south wall of terrain
    model = _model(elevation)
    engine = SightlineEngine(model)

    lat = model.north - 100 * model.lat_per_px
    target_lon = model.west + 60 * model.lon_per_px
    behind_lon = model.west + 140 * model.lon_per_px
    same_side_lon = model.west + 75 * model.lon_per_px

    assert not engine.visible_from(
        lat, target_lon, np.array([lat]), np.array([behind_lon])
    )[0], "a 300 m ridge must block the view"

    assert engine.visible_from(
        lat, target_lon, np.array([lat]), np.array([same_side_lon])
    )[0], "an observer on the same side of the ridge must see the column"


def test_a_gully_is_hidden_from_the_road_above_it():
    """The case that motivates the metric: a draw below a highway.

    Distance to the nearest road would call this well observed. It is not.
    """
    rows, cols = 200, 200
    elevation = np.zeros((rows, cols))
    for col in range(cols):
        if col < 90:
            elevation[:, col] = 50.0  # valley floor
        elif col < 100:
            elevation[:, col] = 50.0 + (col - 90) * 30.0  # steep rise to the lip
        else:
            elevation[:, col] = 350.0  # bench, with the road on it

    model = _model(elevation)
    engine = SightlineEngine(model)

    lat = model.north - 100 * model.lat_per_px
    valley_lon = model.west + 60 * model.lon_per_px
    road_lon = model.west + 150 * model.lon_per_px

    assert not engine.visible_from(
        lat, valley_lon, np.array([lat]), np.array([road_lon])
    )[0], "the valley floor must be hidden by the lip of the bench"

    # And the distance-to-road proxy would have disagreed, which is the point.
    m_lon, _ = metres_per_degree(lat)
    assert (road_lon - valley_lon) * m_lon < 3000.0


def test_observability_reports_no_answer_when_there_are_no_observers():
    """An absent observer set must not be scored as 'fully visible'."""
    engine = SightlineEngine(_model(np.full((64, 64), 100.0)))
    result = engine.observability(LAT, LON, np.zeros(0), np.zeros(0), 5000.0)

    assert result.observers_tested == 0
    assert result.weighted_visibility is None
    assert result.nearest_observer_m is None
    assert result.nearest_visible_m is None


def test_observability_distinguishes_near_from_far_vantage():
    engine = SightlineEngine(_model(np.full((600, 600), 100.0), pixel_m=30.0))
    _, m_lat = metres_per_degree(LAT)

    near = engine.observability(
        LAT, LON, np.array([LAT + 200.0 / m_lat]), np.array([LON]), 5000.0
    )
    far = engine.observability(
        LAT, LON, np.array([LAT + 4500.0 / m_lat]), np.array([LON]), 5000.0
    )

    # Both have clear sight lines over flat ground.
    assert near.observers_visible == 1 and far.observers_visible == 1
    # But the near one is the one that would actually catch a small column.
    assert near.nearest_visible_m == pytest.approx(200.0, rel=0.05)
    assert far.nearest_visible_m == pytest.approx(4500.0, rel=0.05)


def test_a_blocked_location_scores_zero_weighted_visibility():
    elevation = np.full((300, 300), 100.0)
    elevation[:, 148:152] = 600.0
    model = _model(elevation)
    engine = SightlineEngine(model)

    lat = model.north - 150 * model.lat_per_px
    target_lon = model.west + 100 * model.lon_per_px
    observer_lons = model.west + np.array([200, 220, 240]) * model.lon_per_px

    result = engine.observability(
        lat, target_lon, np.full(3, lat), observer_lons, 8000.0
    )
    assert result.observers_tested == 3
    assert result.observers_visible == 0
    assert result.weighted_visibility == pytest.approx(0.0)
    # Nearest observer exists, but none of them can see it. Both facts matter.
    assert result.nearest_observer_m is not None
    assert result.nearest_visible_m is None


def test_earth_curvature_puts_a_horizon_in_the_model():
    """Over flat ground a 10 m column is visible to about 16 km, not forever.

    A 2 m observer's horizon is 5 km and a 10 m column's is 11.3 km, so the
    limit is their sum. Guards a real bug: the curvature term was subtracted
    from the intervening terrain instead of added, which removed the horizon
    from the model entirely and made distant ground *less* obstructive than
    flat geometry would imply.
    """
    engine = SightlineEngine(_model(np.zeros((1600, 1600)), pixel_m=60.0))
    _, m_lat = metres_per_degree(LAT)

    def visible_at(metres: float) -> bool:
        return bool(
            engine.visible_from(
                LAT, LON, np.array([LAT + metres / m_lat]), np.array([LON])
            )[0]
        )

    assert visible_at(10_000.0), "well inside the horizon"
    assert not visible_at(30_000.0), "well beyond the horizon"


def test_curvature_is_negligible_at_municipal_range():
    """The correction must not distort the answers it was not meant to affect.

    Over 3 km the earth bulge is 0.18 m, far below DEM noise, so a clear
    municipal-range sight line must stay clear.
    """
    engine = SightlineEngine(_model(np.zeros((400, 400))))
    _, m_lat = metres_per_degree(LAT)
    assert engine.visible_from(
        LAT, LON, np.array([LAT + 3000.0 / m_lat]), np.array([LON])
    )[0]


# --------------------------------------------------------------------------- #
# Observer sampling
# --------------------------------------------------------------------------- #


def test_road_sampling_is_evenly_spaced():
    """A straight 1 km road sampled at 100 m gives about 11 observers."""
    _, m_lat = metres_per_degree(LAT)
    line = [(LON, LAT), (LON, LAT + 1000.0 / m_lat)]

    lats, _ = sample_along_lines([line], spacing_m=100.0)
    assert 9 <= len(lats) <= 13
    assert np.allclose(np.diff(np.sort(lats)) * m_lat, 100.0, rtol=0.15)


def test_dense_vertices_do_not_inflate_observer_count():
    """Road vertex density reflects curviness, not how much road there is.

    Without resampling, a winding mountain road would be credited with far more
    observers than a straight one of the same length, and would look better
    watched purely because it bends.
    """
    _, m_lat = metres_per_degree(LAT)
    end = LAT + 1000.0 / m_lat

    straight = [(LON, LAT), (LON, end)]
    dense = [(LON, LAT + (end - LAT) * i / 200) for i in range(201)]

    n_straight = len(sample_along_lines([straight], 100.0)[0])
    n_dense = len(sample_along_lines([dense], 100.0)[0])
    assert abs(n_straight - n_dense) <= 2


def test_empty_geometry_yields_no_observers():
    lats, lons = sample_along_lines([[], []], spacing_m=100.0)
    assert len(lats) == 0 and len(lons) == 0
