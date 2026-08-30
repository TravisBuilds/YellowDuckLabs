"""The priority score.

Two things are tested here, and the second matters more than the first.

The first is arithmetic: that the normalisers clamp, that a low component
suppresses the total, that ordering is monotone in each input.

The second is honesty. A score that silently substitutes a default for missing
data is worse than no score, because it looks the same as a real one. So most of
these tests assert on what the model *says about itself*: that absent inputs are
named, that confidence falls when data are missing, and that no component ever
invents a value it was not given.
"""

from __future__ import annotations

import pytest

from firewatch.core.scoring.normalize import (
    aspect_factor,
    geometric_mean,
    inverse_ramp,
    log_ramp,
    ramp,
    slope_factor,
)
from firewatch.core.scoring.priority import (
    COMPONENT_DEFINITIONS,
    COMPONENT_FUNCTIONS,
    SCORE_VERSION,
    CellMetrics,
    band_for,
    observation_gap,
    score_cell,
)

# A cell with every metric present, used as the baseline to perturb.
FULL_METRICS = {
    "slope_deg": 25.0,
    "aspect_deg": 180.0,
    "elevation_m": 400.0,
    "ruggedness_m": 6.0,
    "vegetation_fraction": 0.7,
    "fbp_fuel_code": 7.0,
    "fbp_spread_factor": 0.75,
    "canopy_height_m": 25.0,
    "building_count_250m": 20.0,
    "nearest_building_m": 120.0,
    "park_overlap_fraction": 0.2,
    "nearest_road_m": 300.0,
    "nearest_water_asset_m": 800.0,
    "hotspot_count_history": 3.0,
    "days_since_satellite_observation": 12.0,
    "road_visibility_fraction": 0.2,
    "nearest_visible_road_m": 900.0,
    "fwi": 18.0,
    "ffmc": 89.0,
    "isi": 8.0,
    "bui": 70.0,
    "wind_speed_kmh": 20.0,
}


def metrics(**overrides) -> CellMetrics:
    values = {**FULL_METRICS, **overrides}
    return CellMetrics(
        values=values,
        units={k: None for k in values},
        confidences={k: 0.8 for k in values},
        sources={k: ["test"] for k in values},
    )


# --------------------------------------------------------------------------- #
# Normalisers
# --------------------------------------------------------------------------- #


def test_ramps_clamp_to_the_unit_interval():
    assert ramp(-100.0, 0.0, 10.0) == 0.0
    assert ramp(1000.0, 0.0, 10.0) == 1.0
    assert ramp(5.0, 0.0, 10.0) == pytest.approx(0.5)

    assert inverse_ramp(-100.0, 0.0, 10.0) == 1.0
    assert inverse_ramp(1000.0, 0.0, 10.0) == 0.0
    assert inverse_ramp(5.0, 0.0, 10.0) == pytest.approx(0.5)


def test_ramps_return_no_answer_for_no_input():
    """Absent input must not become zero, which is a real and different claim."""
    assert ramp(None, 0.0, 10.0) is None
    assert inverse_ramp(None, 0.0, 10.0) is None
    assert log_ramp(None, 0, 10) is None
    assert slope_factor(None) is None
    assert aspect_factor(None) is None


def test_log_ramp_is_steep_at_the_bottom():
    """The difference between 0 and 5 buildings matters more than 100 and 105."""
    low = log_ramp(5.0, 0, 120) - log_ramp(0.0, 0, 120)
    high = log_ramp(105.0, 0, 120) - log_ramp(100.0, 0, 120)
    assert low > high * 3


def test_slope_factor_rises_then_saturates():
    assert slope_factor(0.0) < slope_factor(20.0) < slope_factor(35.0)
    # Above about 45 degrees the spread rate stops climbing meaningfully.
    assert slope_factor(50.0) == pytest.approx(slope_factor(80.0), abs=0.05)
    assert 0.0 <= slope_factor(90.0) <= 1.0


def test_aspect_factor_peaks_on_south_facing_ground():
    """In the northern hemisphere south aspects dry fastest."""
    south = aspect_factor(180.0)
    north = aspect_factor(0.0)
    assert south > aspect_factor(135.0) >= aspect_factor(90.0)
    assert south > north
    # Aspect is circular: 0 and 360 are the same bearing.
    assert aspect_factor(0.0) == pytest.approx(aspect_factor(360.0))


def test_geometric_mean_is_suppressed_by_one_low_term():
    """The 'product' behaviour the brief asks for: one near-zero drags it down."""
    assert geometric_mean([0.5] * 5) == pytest.approx(0.5)

    lopsided = geometric_mean([0.95, 0.95, 0.95, 0.95, 0.02])
    arithmetic = sum([0.95, 0.95, 0.95, 0.95, 0.02]) / 5
    # An arithmetic mean would call this cell high priority. The geometric mean
    # will not, because one component says the fire cannot get going here.
    assert arithmetic > 0.75
    assert lopsided < 0.5


def test_geometric_mean_preserves_ordering():
    assert geometric_mean([0.6, 0.6]) > geometric_mean([0.5, 0.6])


def test_geometric_mean_of_nothing_is_no_answer():
    assert geometric_mean([]) is None


# --------------------------------------------------------------------------- #
# Bands
# --------------------------------------------------------------------------- #


def test_bands_cover_the_range_and_report_unknown():
    assert band_for(None) == "Unknown"
    assert band_for(0.0) == "Very low"
    assert band_for(0.35) == "Low"
    assert band_for(0.50) == "Moderate"
    assert band_for(0.65) == "High"
    assert band_for(0.90) == "Very high"


def test_band_thresholds_are_monotone():
    previous = None
    for value in [0.0, 0.29, 0.30, 0.44, 0.45, 0.59, 0.60, 0.74, 0.75, 1.0]:
        band = band_for(value)
        order = ["Very low", "Low", "Moderate", "High", "Very high"]
        if previous is not None:
            assert order.index(band) >= order.index(previous)
        previous = band


# --------------------------------------------------------------------------- #
# Monotonicity: the score must move the direction a responder would expect
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "metric, low, high, component",
    [
        ("slope_deg", 2.0, 40.0, "spread_potential"),
        ("vegetation_fraction", 0.0, 0.9, "spread_potential"),
        ("fbp_spread_factor", 0.1, 0.9, "spread_potential"),
        ("isi", 1.0, 25.0, "spread_potential"),
        ("wind_speed_kmh", 2.0, 60.0, "spread_potential"),
        ("ffmc", 60.0, 95.0, "ignition_likelihood"),
        ("hotspot_count_history", 0.0, 40.0, "ignition_likelihood"),
        ("building_count_250m", 0.0, 200.0, "consequence_exposure"),
        ("ruggedness_m", 0.5, 25.0, "access_difficulty_proxy"),
        ("nearest_water_asset_m", 50.0, 3000.0, "access_difficulty_proxy"),
        ("nearest_visible_road_m", 100.0, 3000.0, "observation_gap"),
    ],
)
def test_component_rises_with_its_driver(metric, low, high, component):
    fn = COMPONENT_FUNCTIONS[component]
    assert fn(metrics(**{metric: low})).value < fn(metrics(**{metric: high})).value


@pytest.mark.parametrize(
    "metric, near, far, component",
    [
        ("nearest_building_m", 10.0, 2000.0, "consequence_exposure"),
        ("nearest_road_m", 20.0, 3000.0, "ignition_likelihood"),
    ],
)
def test_component_falls_as_distance_grows(metric, near, far, component):
    """Closer buildings mean more to lose; closer roads mean more ignitions."""
    fn = COMPONENT_FUNCTIONS[component]
    assert fn(metrics(**{metric: near})).value > fn(metrics(**{metric: far})).value


def test_overall_score_rises_with_a_worse_cell():
    calm = score_cell(
        metrics(slope_deg=2.0, ffmc=60.0, wind_speed_kmh=1.0, isi=0.5,
                vegetation_fraction=0.0, fbp_spread_factor=0.05,
                road_visibility_fraction=0.95),
        "2026-08-30",
    )
    severe = score_cell(
        metrics(slope_deg=40.0, ffmc=94.0, wind_speed_kmh=55.0, isi=25.0,
                vegetation_fraction=0.95, fbp_spread_factor=0.9,
                road_visibility_fraction=0.0),
        "2026-08-30",
    )
    assert severe.overall_priority > calm.overall_priority


def test_all_component_values_stay_in_the_unit_interval():
    """Extreme inputs must not produce a score above 1 or below 0."""
    extremes = metrics(
        slope_deg=90.0, ffmc=101.0, isi=200.0, bui=500.0, fwi=200.0,
        wind_speed_kmh=300.0, vegetation_fraction=1.0,
        building_count_250m=5000.0, nearest_building_m=0.0,
        nearest_road_m=0.0, hotspot_count_history=10000.0,
        road_visibility_fraction=0.0, nearest_visible_road_m=0.0,
    )
    result = score_cell(extremes, "2026-08-30")
    assert 0.0 <= result.overall_priority <= 1.0
    for component in result.components.values():
        assert 0.0 <= component.value <= 1.0


# --------------------------------------------------------------------------- #
# Honesty about missing data
# --------------------------------------------------------------------------- #


def test_a_cell_with_no_data_gets_no_score():
    """Not a zero, and not a middling default. No score, and a stated reason.

    Guards a real bug. Three observation-gap signals correctly treat absence of
    evidence as evidence of a gap: "no road has a clear view of here" and "no
    detection has ever been recorded nearby" are findings, not blanks. But those
    signals consumed no metric, so a cell with no data at all satisfied all of
    them, scored a maximal observation gap, and came out at 1.0, "Very high" —
    ranked top of the municipality precisely because nothing was known about it.
    """
    empty = CellMetrics(values={}, units={}, confidences={}, sources={})
    result = score_cell(empty, "2026-08-30")

    assert result.overall_priority is None
    assert result.band == "Unknown"
    assert result.confidence == 0.0
    assert "No data" in result.explanation["summary"]


def test_absence_of_analysis_is_not_reported_as_an_observation_gap():
    """The specific conflation behind that bug, tested directly.

    "We looked and nothing can see this place" and "we never looked" must not
    produce the same number.
    """
    never_computed = observation_gap(metrics(road_visibility_fraction=None))
    assert never_computed.value is None
    assert "not computed" in never_computed.rationale
    assert "absence of analysis" in never_computed.rationale

    computed_and_blind = observation_gap(
        metrics(road_visibility_fraction=0.0, nearest_visible_road_m=None)
    )
    assert computed_and_blind.value > 0.9


def test_an_overall_score_needs_a_minimum_basis():
    """One component out of five is not a priority score, however it prints."""
    thin = score_cell(
        CellMetrics(
            values={"road_visibility_fraction": 0.1},
            units={}, confidences={}, sources={},
        ),
        "2026-08-30",
    )
    assert thin.overall_priority is None
    assert thin.band == "Unknown"
    assert "no overall priority is reported" in thin.explanation["summary"]
    # The components that *were* computed remain readable on their own.
    assert thin.components["observation_gap"].value is not None


def test_missing_inputs_are_named_not_silently_dropped():
    result = COMPONENT_FUNCTIONS["spread_potential"](
        metrics(slope_deg=None, aspect_deg=None)
    )
    missing = {m for s in result.signals for m in s.inputs_missing}
    assert "slope_deg" in missing
    assert "aspect_deg" in missing
    assert result.completeness < 1.0
    assert "missing inputs" in result.rationale.lower()


def test_confidence_falls_when_data_are_missing():
    full = COMPONENT_FUNCTIONS["spread_potential"](metrics())
    partial = COMPONENT_FUNCTIONS["spread_potential"](
        metrics(slope_deg=None, isi=None, wind_speed_kmh=None)
    )
    assert partial.confidence < full.confidence
    assert partial.completeness < full.completeness


def test_completeness_is_reported_per_component_and_overall():
    result = score_cell(metrics(slope_deg=None, ffmc=None), "2026-08-30")
    assert 0.0 < result.completeness < 1.0
    assert result.explanation["completeness"] == pytest.approx(
        round(result.completeness, 3)
    )


def test_unavailable_components_are_listed():
    """A cell with no exposure inputs must say so, not report zero exposure."""
    result = score_cell(
        metrics(
            building_count_250m=None,
            nearest_building_m=None,
            park_overlap_fraction=None,
        ),
        "2026-08-30",
    )
    assert "consequence_exposure" in result.explanation["unavailable_components"]
    assert result.components["consequence_exposure"].value is None
    # And it must not be quietly folded into the total as a zero.
    assert result.overall_priority is not None


def test_every_signal_reports_the_metrics_it_used():
    """Provenance: a number in the UI must be traceable to its inputs."""
    result = score_cell(metrics(), "2026-08-30")
    for component in result.components.values():
        for signal in component.signals:
            if signal.value is None:
                continue
            # The dedicated-observation signal is an explicit assumption of
            # absence and consumes no metric, which is stated in its rationale.
            if signal.name == "dedicated_observation":
                assert "no fixed camera" in signal.rationale.lower()
                continue
            assert signal.inputs_used, f"{signal.name} cited no inputs"
            for used in signal.inputs_used:
                assert used.sources, f"{signal.name} used a metric with no source"


def test_every_component_has_a_definition_and_a_rationale():
    result = score_cell(metrics(), "2026-08-30")
    for name, component in result.components.items():
        assert COMPONENT_DEFINITIONS[name], f"{name} has no definition"
        assert component.rationale, f"{name} has no rationale"
        payload = component.as_dict()
        assert payload["definition"]
        assert payload["signals"]


# --------------------------------------------------------------------------- #
# The observation gap, which is the product's central claim
# --------------------------------------------------------------------------- #


def test_a_concealed_cell_scores_a_larger_observation_gap_than_a_visible_one():
    hidden = observation_gap(
        metrics(road_visibility_fraction=0.0, nearest_visible_road_m=None)
    )
    open_view = observation_gap(
        metrics(road_visibility_fraction=0.8, nearest_visible_road_m=100.0)
    )
    assert hidden.value > open_view.value
    # Not exactly 1.0: a recent nearby detection still counts for something.
    assert hidden.value > 0.9
    assert open_view.value < 0.4


def test_no_clear_vantage_is_treated_as_a_full_gap_and_says_why():
    result = observation_gap(metrics(nearest_visible_road_m=None))
    signal = next(s for s in result.signals if s.name == "sightline_distance")
    assert signal.value == 1.0
    assert "not be seen from the road network" in signal.rationale


def test_observation_gap_confidence_is_capped():
    """Typical canopy and no patrol data, so the component cannot be certain."""
    result = observation_gap(metrics())
    assert result.confidence <= 0.55


def test_dedicated_observation_is_uniform_and_therefore_ranks_nothing():
    """It must not be able to create apparent differences between cells."""
    a = observation_gap(metrics(road_visibility_fraction=0.1))
    b = observation_gap(metrics(road_visibility_fraction=0.9))
    signal_a = next(s for s in a.signals if s.name == "dedicated_observation")
    signal_b = next(s for s in b.signals if s.name == "dedicated_observation")
    assert signal_a.value == signal_b.value == 1.0
    # The cells still differ, because the measurable terms carry the ranking.
    assert a.value != b.value


def test_terrain_visibility_dominates_the_observation_gap():
    """The measured term must outweigh the assumed one, or ranking is fiction."""
    result = observation_gap(metrics())
    weights = {s.name: s.weight for s in result.signals}
    assert weights["terrain_visibility"] > weights["dedicated_observation"]
    assert weights["terrain_visibility"] > weights["detection_recency"]


# --------------------------------------------------------------------------- #
# The explanation payload, which is what the UI and the AI both read
# --------------------------------------------------------------------------- #


def test_explanation_carries_everything_the_ui_needs():
    result = score_cell(metrics(), "2026-08-30")
    payload = result.explanation

    assert payload["as_of_date"] == "2026-08-30"
    assert payload["score_version"] == SCORE_VERSION
    assert payload["band"] == result.band
    assert payload["formula"]
    # The deviation from the brief's literal product must be disclosed.
    assert "geometric mean" in payload["formula_note"]
    assert "hypothesis" in payload["formula_note"]
    assert payload["primary_drivers"]
    assert set(payload["components"]) == set(COMPONENT_FUNCTIONS)
    assert set(payload["separable_views"]) == {
        "hazard",
        "exposure",
        "current_conditions",
        "operational_gap",
    }


def test_separable_views_are_not_collapsed_into_the_total():
    """The brief insists these stay readable on their own."""
    result = score_cell(metrics(), "2026-08-30")
    assert result.hazard is not None
    assert result.exposure is not None
    assert result.current_conditions is not None
    assert result.operational_gap is not None
    # Exposure is exactly its component, not a blend.
    assert result.exposure == result.components["consequence_exposure"].value


def test_current_conditions_depend_only_on_fire_weather():
    """Terrain must not leak into a reading labelled 'current conditions'."""
    a = score_cell(metrics(slope_deg=2.0, vegetation_fraction=0.0), "2026-08-30")
    b = score_cell(metrics(slope_deg=45.0, vegetation_fraction=1.0), "2026-08-30")
    assert a.current_conditions == pytest.approx(b.current_conditions)

    wetter = score_cell(metrics(fwi=1.0, ffmc=70.0, bui=5.0), "2026-08-30")
    assert wetter.current_conditions < a.current_conditions


def test_primary_drivers_are_ordered_by_contribution():
    result = score_cell(metrics(), "2026-08-30")
    values = [d["value"] for d in result.explanation["primary_drivers"]]
    assert values == sorted(values, reverse=True)


def test_scores_are_reproducible():
    """Deterministic: no LLM, no randomness, no wall-clock dependence."""
    first = score_cell(metrics(), "2026-08-30")
    second = score_cell(metrics(), "2026-08-30")
    assert first.overall_priority == second.overall_priority
    assert first.explanation == second.explanation
