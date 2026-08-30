"""CWFIS FBP fuel types: classification, sampling, and scoring use."""

from __future__ import annotations

import numpy as np
import pytest
from pyproj import Transformer

from firewatch.core.fuels import FBP_TYPES, FuelModel, classify
from firewatch.core.scoring.priority import CellMetrics, spread_potential


def _metrics(**overrides) -> CellMetrics:
    values = {"fbp_spread_factor": 0.75, "fbp_fuel_code": 7.0, "slope_deg": 25.0,
              "aspect_deg": 180.0, "vegetation_fraction": 0.7, "isi": 8.0,
              "wind_speed_kmh": 20.0}
    values.update(overrides)
    return CellMetrics(values=values, units={}, confidences={k: 0.8 for k in values},
                       sources={k: ["test"] for k in values})

LAT, LON = 49.36, -123.16


def test_known_codes_resolve_to_their_fbp_class():
    spruce = classify(2)
    assert spruce is not None
    assert spruce.fbp_class == "C-2"
    assert spruce.is_fuel
    assert spruce.spread_factor > classify(5).spread_factor  # C-5 is slower

    water = classify(102)
    assert water is not None
    assert water.is_fuel is False
    assert water.spread_factor == 0.0
    assert water.canopy_height_m == 0.0


def test_nodata_and_unknown_codes_are_not_invented():
    assert classify(None) is None
    assert classify(-9999) is None
    assert classify(9999) is None


def test_unlisted_mixedwood_uses_the_percent_conifer_convention():
    """415 is listed; 440 is the same convention at 40% conifer."""
    spec = classify(440)
    assert spec is not None
    assert spec.is_fuel
    assert spec.fbp_class == "M"
    assert FBP_TYPES[415].spread_factor < spec.spread_factor < FBP_TYPES[650].spread_factor


def _fuel_grid(code: int, size: int = 5, pixel_m: float = 100.0) -> FuelModel:
    east, north = Transformer.from_crs(
        "EPSG:4326", "EPSG:3978", always_xy=True
    ).transform(LON, LAT)
    return FuelModel(
        codes=np.full((size, size), code, dtype=np.int32),
        west_m=east - (size / 2) * pixel_m,
        north_m=north + (size / 2) * pixel_m,
        pixel_m=pixel_m,
        native_crs="EPSG:3978",
    )


def test_fuel_model_samples_the_majority_class_at_a_known_point():
    model = _fuel_grid(7)
    spec, spread, canopy = model.sample(LAT, LON)
    assert spec is not None
    assert spec.fbp_class == "C-7"
    assert spread == pytest.approx(0.75)
    assert canopy == pytest.approx(25.0)


def test_canopy_at_returns_an_array_aligned_with_the_request():
    model = _fuel_grid(7)
    heights = model.canopy_at(np.array([LAT, LAT]), np.array([LON, LON]))
    assert heights.shape == (2,)
    assert np.allclose(heights, 25.0)


def test_spread_potential_rises_with_fbp_spread_factor():
    low = spread_potential(_metrics(fbp_spread_factor=0.1))
    high = spread_potential(_metrics(fbp_spread_factor=0.9))
    assert low.value < high.value
    used = {s.name for s in high.signals}
    assert "fbp_fuel" in used


def test_missing_fbp_type_is_named_rather_than_substituted():
    result = spread_potential(_metrics(fbp_spread_factor=None, fbp_fuel_code=None))
    signal = next(s for s in result.signals if s.name == "fbp_fuel")
    assert signal.value is None
    assert "fbp_spread_factor" in signal.inputs_missing
