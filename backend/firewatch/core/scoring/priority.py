"""Fire Watch Priority score, version 0.1.

Deterministic. No LLM is involved in producing any number here.

The brief's formula is treated as a working hypothesis:

    Fire Watch Priority
        = ignition likelihood
        x spread potential
        x consequence/exposure
        x observation gap
        x response/access difficulty

Hazard, exposure, current conditions and operational gap are also reported
separately, because collapsing them into one number is exactly the failure mode
the brief warns against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from firewatch.core.scoring.normalize import (
    MetricInput,
    Signal,
    aspect_factor,
    combine,
    geometric_mean,
    inverse_ramp,
    log_ramp,
    ramp,
    slope_factor,
)

SCORE_VERSION = "v0.1"

#: What each component means, shown in the UI beside its value.
COMPONENT_DEFINITIONS = {
    "ignition_likelihood": (
        "How likely an ignition is here, from recorded fire and hotspot history, "
        "current fine-fuel dryness, and proximity to human activity corridors."
    ),
    "spread_potential": (
        "How readily fire would spread if it started, from slope, aspect, the "
        "CWFIS FBP fuel type, mapped vegetation continuity and current "
        "fire-weather spread indices."
    ),
    "consequence_exposure": (
        "What is nearby that could be harmed: structures, their density, and "
        "community assets. Property value is deliberately not used."
    ),
    "observation_gap": (
        "How poorly observed this location is. Mostly line-of-sight from the "
        "road network to a 10 m smoke column, with intervening terrain and "
        "FBP-typical canopy height. Detection recency is secondary. It is not a "
        "satellite blind-interval calculation, and no camera or patrol coverage "
        "data exist to include."
    ),
    "access_difficulty_proxy": (
        "ACCESS_DIFFICULTY_PROXY. Distance to mapped roads, terrain ruggedness, "
        "slope and distance to mapped water assets. This is NOT response time."
    ),
}

PRIORITY_BANDS = (
    (0.75, "Very high"),
    (0.60, "High"),
    (0.45, "Moderate"),
    (0.30, "Low"),
    (0.00, "Very low"),
)


def band_for(value: float | None) -> str:
    if value is None:
        return "Unknown"
    for threshold, label in PRIORITY_BANDS:
        if value >= threshold:
            return label
    return "Very low"


@dataclass
class CellMetrics:
    """Metric values for one cell, keyed by metric name."""

    values: dict[str, float | None] = field(default_factory=dict)
    units: dict[str, str | None] = field(default_factory=dict)
    confidences: dict[str, float | None] = field(default_factory=dict)
    sources: dict[str, list[str]] = field(default_factory=dict)

    def get(self, metric: str) -> float | None:
        return self.values.get(metric)

    def input_for(self, metric: str) -> MetricInput:
        return MetricInput(
            metric=metric,
            value=self.values.get(metric),
            unit=self.units.get(metric),
            confidence=self.confidences.get(metric),
            sources=self.sources.get(metric, []),
        )


@dataclass
class ComponentResult:
    name: str
    value: float | None
    completeness: float
    confidence: float
    signals: list[Signal]
    rationale: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "definition": COMPONENT_DEFINITIONS.get(self.name, ""),
            "value": None if self.value is None else round(self.value, 4),
            "completeness": round(self.completeness, 3),
            "confidence": round(self.confidence, 3),
            "rationale": self.rationale,
            "signals": [s.as_dict() for s in self.signals],
        }


@dataclass
class ScoreResult:
    overall_priority: float | None
    band: str
    confidence: float
    completeness: float
    components: dict[str, ComponentResult]
    hazard: float | None
    exposure: float | None
    current_conditions: float | None
    operational_gap: float | None
    score_version: str
    explanation: dict


def _signal(
    name: str,
    label: str,
    weight: float,
    metrics: CellMetrics,
    metric_names: list[str],
    transform: Callable[[CellMetrics], float | None],
    rationale: Callable[[float | None, CellMetrics], str],
) -> Signal:
    value = transform(metrics)
    used = [metrics.input_for(m) for m in metric_names if metrics.get(m) is not None]
    missing = [m for m in metric_names if metrics.get(m) is None]
    return Signal(
        name=name,
        label=label,
        value=value,
        weight=weight,
        rationale=rationale(value, metrics),
        inputs_used=used,
        inputs_missing=missing,
    )


def _fmt(value: float | None, digits: int = 1, suffix: str = "") -> str:
    return "unknown" if value is None else f"{value:.{digits}f}{suffix}"


# --- components ------------------------------------------------------------


def ignition_likelihood(m: CellMetrics) -> ComponentResult:
    signals = [
        _signal(
            "hotspot_history", "Recorded hotspot history nearby", 0.30, m,
            ["hotspot_count_history"],
            lambda mm: log_ramp(mm.get("hotspot_count_history"), 0, 20),
            lambda v, mm: (
                f"{_fmt(mm.get('hotspot_count_history'), 0)} satellite hotspots have "
                "been recorded within 1 km over the last 10 years."
            ),
        ),
        _signal(
            "fine_fuel_dryness", "Fine fuel moisture code", 0.30, m,
            ["ffmc"],
            # FFMC below ~70 rarely supports ignition; above 92 it is very easy.
            lambda mm: ramp(mm.get("ffmc"), 70.0, 92.0),
            lambda v, mm: (
                f"Fine Fuel Moisture Code is {_fmt(mm.get('ffmc'))}; fine fuels "
                "ignite readily above roughly 88."
            ),
        ),
        _signal(
            "human_activity", "Proximity to roads and trails", 0.25, m,
            ["nearest_road_m"],
            # Most wildfire ignitions in populated coastal BC are human-caused
            # and cluster near access.
            lambda mm: inverse_ramp(mm.get("nearest_road_m"), 20.0, 600.0),
            lambda v, mm: (
                f"Nearest mapped road is {_fmt(mm.get('nearest_road_m'), 0, ' m')} away. "
                "Human-caused ignitions concentrate near access."
            ),
        ),
        _signal(
            "receptive_fuel", "Fuel that can carry an ignition", 0.15, m,
            ["fbp_spread_factor", "vegetation_fraction"],
            lambda mm: (
                mm.get("fbp_spread_factor")
                if mm.get("fbp_spread_factor") is not None
                else ramp(mm.get("vegetation_fraction"), 0.0, 0.6)
            ),
            lambda v, mm: (
                f"FBP relative spread factor is {_fmt(mm.get('fbp_spread_factor'), 2)}. "
                "This is a fuel type, not a measured fuel load."
                if mm.get("fbp_spread_factor") is not None
                else (
                    f"{_fmt((mm.get('vegetation_fraction') or 0) * 100, 0, '%')} of "
                    "the cell is mapped as vegetation. This is a presence proxy; "
                    "no FBP type was available."
                )
            ),
        ),
    ]
    return _assemble("ignition_likelihood", signals)


def spread_potential(m: CellMetrics) -> ComponentResult:
    signals = [
        _signal(
            "slope", "Slope", 0.25, m, ["slope_deg"],
            lambda mm: slope_factor(mm.get("slope_deg")),
            lambda v, mm: (
                f"Maximum slope in the cell is {_fmt(mm.get('slope_deg'), 0, ' degrees')}. "
                "Spread rate rises steeply with slope and saturates above about 45."
            ),
        ),
        _signal(
            "aspect", "Aspect", 0.10, m, ["aspect_deg"],
            lambda mm: aspect_factor(mm.get("aspect_deg")),
            lambda v, mm: (
                f"Aspect is {_fmt(mm.get('aspect_deg'), 0, ' degrees')}. South-facing "
                "slopes dry fastest in this hemisphere."
            ),
        ),
        _signal(
            "fbp_fuel", "FBP fuel type", 0.25, m, ["fbp_spread_factor", "fbp_fuel_code"],
            lambda mm: mm.get("fbp_spread_factor"),
            lambda v, mm: (
                f"FBP relative spread factor is {_fmt(mm.get('fbp_spread_factor'), 2)} "
                f"(type code {_fmt(mm.get('fbp_fuel_code'), 0)}). This ranks fire "
                "behaviour potential of the national fuel class, not a local fuel load."
            ),
        ),
        _signal(
            "fuel_continuity", "Vegetation continuity", 0.10, m, ["vegetation_fraction"],
            lambda mm: ramp(mm.get("vegetation_fraction"), 0.05, 0.8),
            lambda v, mm: (
                f"{_fmt((mm.get('vegetation_fraction') or 0) * 100, 0, '%')} vegetation "
                "cover from mapped polygons. A continuity check only; the FBP type "
                "carries the fire-behaviour ranking."
            ),
        ),
        _signal(
            "spread_index", "Initial Spread Index", 0.20, m, ["isi"],
            lambda mm: ramp(mm.get("isi"), 2.0, 20.0),
            lambda v, mm: (
                f"Initial Spread Index is {_fmt(mm.get('isi'))} at the nearest fire "
                "weather station."
            ),
        ),
        _signal(
            "wind", "Wind speed", 0.10, m, ["wind_speed_kmh"],
            lambda mm: ramp(mm.get("wind_speed_kmh"), 5.0, 50.0),
            lambda v, mm: (
                f"Wind at the nearest station is "
                f"{_fmt(mm.get('wind_speed_kmh'), 0, ' km/h')}. Local drainage winds "
                "are not represented."
            ),
        ),
    ]
    return _assemble("spread_potential", signals)


def consequence_exposure(m: CellMetrics) -> ComponentResult:
    signals = [
        _signal(
            "structure_density", "Nearby structure count", 0.50, m,
            ["building_count_250m"],
            lambda mm: log_ramp(mm.get("building_count_250m"), 0, 120),
            lambda v, mm: (
                f"{_fmt(mm.get('building_count_250m'), 0)} buildings are mapped within "
                "250 m."
            ),
        ),
        _signal(
            "structure_proximity", "Distance to nearest structure", 0.35, m,
            ["nearest_building_m"],
            lambda mm: inverse_ramp(mm.get("nearest_building_m"), 0.0, 500.0),
            lambda v, mm: (
                f"Nearest mapped building is "
                f"{_fmt(mm.get('nearest_building_m'), 0, ' m')} away."
            ),
        ),
        _signal(
            "community_asset", "Park or community land", 0.15, m,
            ["park_overlap_fraction"],
            lambda mm: ramp(mm.get("park_overlap_fraction"), 0.0, 0.5),
            lambda v, mm: (
                f"{_fmt((mm.get('park_overlap_fraction') or 0) * 100, 0, '%')} of the "
                "cell lies within a mapped park or reserve."
            ),
        ),
    ]
    return _assemble("consequence_exposure", signals)


def observation_gap(m: CellMetrics) -> ComponentResult:
    # Several signals below treat an absent value as a full gap: "no road has a
    # clear view" and "no detection has ever been recorded nearby" are real
    # findings, and reporting them as unknown would understate the gap.
    #
    # But that reading is only valid if the visibility computation actually ran
    # for this cell. Otherwise "no clear vantage found" is indistinguishable
    # from "never looked", and a cell with no data at all would score a maximal
    # observation gap and rank top of the municipality on an absence of
    # evidence. So the component is gated on the one metric that proves the
    # computation happened.
    visibility_computed = m.get("road_visibility_fraction") is not None
    if not visibility_computed:
        return ComponentResult(
            name="observation_gap",
            value=None,
            completeness=0.0,
            confidence=0.0,
            signals=[],
            rationale=(
                "Terrain visibility was not computed for this location, so how "
                "well observed it is cannot be assessed. This is not a finding "
                "of good or poor observation; it is an absence of analysis."
            ),
        )

    signals = [
        _signal(
            "terrain_visibility", "Terrain screening from the road network", 0.55, m,
            ["road_visibility_fraction"],
            # The dominant measurable term: terrain either blocks the view or it
            # does not. A concealed draw is where a fire grows unnoticed.
            lambda mm: inverse_ramp(mm.get("road_visibility_fraction"), 0.0, 0.45),
            lambda v, mm: (
                f"{_fmt((mm.get('road_visibility_fraction') or 0) * 100, 0, '%')} "
                "distance-weighted visibility of a 10 m smoke column here from the "
                "surrounding road network. Terrain plus typical FBP canopy height; "
                "measured crown geometry is not used, so real visibility under "
                "dense timber is still no better than this."
            ),
        ),
        _signal(
            "sightline_distance", "Distance to the nearest clear vantage", 0.20, m,
            ["nearest_visible_road_m"],
            lambda mm: (
                1.0 if mm.get("nearest_visible_road_m") is None
                else ramp(mm.get("nearest_visible_road_m"), 150.0, 2500.0)
            ),
            lambda v, mm: (
                "No road within 4 km has a clear line of sight to this location. "
                "A fire here would not be seen from the road network at all."
                if mm.get("nearest_visible_road_m") is None
                else (
                    f"Nearest road with a clear view is "
                    f"{_fmt(mm.get('nearest_visible_road_m'), 0, ' m')} away. "
                    "Detection at distance depends on column size and air clarity."
                )
            ),
        ),
        _signal(
            "detection_recency", "Time since a nearby detection", 0.10, m,
            ["days_since_satellite_observation"],
            lambda mm: (
                1.0 if mm.get("days_since_satellite_observation") is None
                else ramp(mm.get("days_since_satellite_observation"), 1.0, 30.0)
            ),
            lambda v, mm: (
                "No satellite detection has ever been recorded within 5 km, so we "
                "have no detection-based evidence about this location."
                if mm.get("days_since_satellite_observation") is None
                else (
                    f"Most recent nearby detection was "
                    f"{_fmt(mm.get('days_since_satellite_observation'), 0)} days ago. "
                    "This measures detections, not observation attempts."
                )
            ),
        ),
        _signal(
            "dedicated_observation", "Dedicated ground or aerial observation", 0.15, m,
            [],
            # Structurally unavailable: no camera network, no drone coverage, no
            # patrol data is ingested. Reporting a full gap is the honest answer,
            # but it is held to a small weight so it cannot flatten the component.
            lambda mm: 1.0,
            lambda v, mm: (
                "No fixed camera, patrol, lookout or drone coverage data are "
                "available for any part of this municipality, so dedicated "
                "observation is treated as entirely absent. This is uniform across "
                "every cell and therefore ranks nothing; it is the section a "
                "Yellow Duck sensor network would populate."
            ),
        ),
    ]
    result = _assemble("observation_gap", signals)
    # One of four signals is an assumption of absence rather than a measurement,
    # and canopy height is typical-for-class rather than measured, so confidence
    # is capped.
    result.confidence = min(result.confidence, 0.55)
    return result


def access_difficulty_proxy(m: CellMetrics) -> ComponentResult:
    signals = [
        _signal(
            "road_distance", "Distance to mapped road", 0.35, m, ["nearest_road_m"],
            lambda mm: ramp(mm.get("nearest_road_m"), 30.0, 800.0),
            lambda v, mm: (
                f"Nearest mapped road is {_fmt(mm.get('nearest_road_m'), 0, ' m')} away. "
                "Road presence does not establish apparatus access."
            ),
        ),
        _signal(
            "terrain_ruggedness", "Terrain ruggedness", 0.25, m, ["ruggedness_m"],
            lambda mm: ramp(mm.get("ruggedness_m"), 1.0, 15.0),
            lambda v, mm: (
                f"Terrain ruggedness index is {_fmt(mm.get('ruggedness_m'))} m mean "
                "elevation change to neighbouring ground."
            ),
        ),
        _signal(
            "slope_access", "Slope", 0.15, m, ["slope_deg"],
            lambda mm: ramp(mm.get("slope_deg"), 10.0, 40.0),
            lambda v, mm: (
                f"Slope is {_fmt(mm.get('slope_deg'), 0, ' degrees')}, which constrains "
                "both vehicle and crew movement."
            ),
        ),
        _signal(
            "water_distance", "Distance to mapped water asset", 0.25, m,
            ["nearest_water_asset_m"],
            lambda mm: ramp(mm.get("nearest_water_asset_m"), 100.0, 1500.0),
            lambda v, mm: (
                f"Nearest mapped water asset is "
                f"{_fmt(mm.get('nearest_water_asset_m'), 0, ' m')} away. Mapped presence "
                "says nothing about flow or pressure."
            ),
        ),
    ]
    return _assemble("access_difficulty_proxy", signals)


def _assemble(name: str, signals: list[Signal]) -> ComponentResult:
    value, completeness, signals = combine(signals)
    available = [s for s in signals if s.available]
    confidence = (
        sum(s.confidence * s.weight for s in available)
        / sum(s.weight for s in available)
        if available
        else 0.0
    ) * completeness

    if value is None:
        rationale = "No inputs were available for this component."
    else:
        ranked = sorted(available, key=lambda s: (s.value or 0) * s.weight, reverse=True)
        top = ranked[0]
        rationale = f"Driven mainly by {top.label.lower()}. {top.rationale}"
        if completeness < 1.0:
            missing = [s.label.lower() for s in signals if not s.available]
            rationale += f" Missing inputs: {', '.join(missing)}."

    return ComponentResult(
        name=name,
        value=value,
        completeness=completeness,
        confidence=confidence,
        signals=signals,
        rationale=rationale,
    )


# --- assembly --------------------------------------------------------------

COMPONENT_FUNCTIONS = {
    "ignition_likelihood": ignition_likelihood,
    "spread_potential": spread_potential,
    "consequence_exposure": consequence_exposure,
    "observation_gap": observation_gap,
    "access_difficulty_proxy": access_difficulty_proxy,
}


#: An overall priority is a comparison between places, so it needs enough of a
#: basis to be a comparison at all. A geometric mean over one component out of
#: five is not a priority score, however confidently it prints.
MINIMUM_COMPONENTS_FOR_OVERALL = 3


def score_cell(metrics: CellMetrics, as_of_date: str) -> ScoreResult:
    components = {name: fn(metrics) for name, fn in COMPONENT_FUNCTIONS.items()}

    available = [c for c in components.values() if c.value is not None]
    if len(available) < MINIMUM_COMPONENTS_FOR_OVERALL:
        missing = [n for n, c in components.items() if c.value is None]
        if available:
            summary = (
                f"Only {len(available)} of {len(components)} components could be "
                f"computed here, so no overall priority is reported. Missing: "
                f"{', '.join(missing)}. The components that were computed are "
                "shown below and can be read on their own."
            )
        else:
            summary = "No data were available for this location."

        return ScoreResult(
            overall_priority=None,
            band="Unknown",
            confidence=0.0,
            completeness=(
                sum(c.completeness for c in components.values()) / len(components)
            ),
            components=components,
            hazard=_mean_of(components, ["ignition_likelihood", "spread_potential"]),
            exposure=components["consequence_exposure"].value,
            current_conditions=_current_conditions_value(metrics),
            operational_gap=_mean_of(
                components, ["observation_gap", "access_difficulty_proxy"]
            ),
            score_version=SCORE_VERSION,
            explanation={
                "as_of_date": as_of_date,
                "score_version": SCORE_VERSION,
                "band": "Unknown",
                "summary": summary,
                "unavailable_components": missing,
                "components": {n: c.as_dict() for n, c in components.items()},
            },
        )

    overall = geometric_mean([c.value for c in available])
    completeness = sum(c.completeness for c in components.values()) / len(components)
    confidence = sum(c.confidence for c in available) / len(available)

    # The four separable views the brief insists on keeping distinct.
    hazard = _mean_of(components, ["ignition_likelihood", "spread_potential"])
    exposure = components["consequence_exposure"].value
    current_conditions = _current_conditions_value(metrics)
    operational_gap = _mean_of(components, ["observation_gap", "access_difficulty_proxy"])

    drivers = sorted(available, key=lambda c: c.value or 0, reverse=True)
    reducers = [c for c in available if (c.value or 0) < 0.4]

    explanation = {
        "as_of_date": as_of_date,
        "score_version": SCORE_VERSION,
        "formula": (
            "geometric mean of ignition_likelihood, spread_potential, "
            "consequence_exposure, observation_gap and access_difficulty_proxy"
        ),
        "formula_note": (
            "The build brief specifies the product of the five components. The "
            "geometric mean is that product raised to the power 1/5: it keeps the "
            "same ordering and the same 'one low component suppresses the score' "
            "behaviour, on a readable 0-1 scale. This formula is a working "
            "hypothesis and has not been validated against outcomes."
        ),
        "band": band_for(overall),
        "primary_drivers": [
            {"component": c.name, "value": round(c.value, 3), "why": c.rationale}
            for c in drivers[:3]
        ],
        "factors_reducing_priority": [
            {"component": c.name, "value": round(c.value, 3), "why": c.rationale}
            for c in reducers
        ],
        "components": {n: c.as_dict() for n, c in components.items()},
        "separable_views": {
            "hazard": _round(hazard),
            "exposure": _round(exposure),
            "current_conditions": _round(current_conditions),
            "operational_gap": _round(operational_gap),
        },
        "completeness": round(completeness, 3),
        "confidence": round(confidence, 3),
        "unavailable_components": [
            n for n, c in components.items() if c.value is None
        ],
    }

    return ScoreResult(
        overall_priority=overall,
        band=band_for(overall),
        confidence=confidence,
        completeness=completeness,
        components=components,
        hazard=hazard,
        exposure=exposure,
        current_conditions=current_conditions,
        operational_gap=operational_gap,
        score_version=SCORE_VERSION,
        explanation=explanation,
    )


def _mean_of(components: dict[str, ComponentResult], names: list[str]) -> float | None:
    values = [components[n].value for n in names if components[n].value is not None]
    return sum(values) / len(values) if values else None


def _current_conditions_value(m: CellMetrics) -> float | None:
    """Fire-weather severity alone, independent of terrain or exposure."""
    parts = [
        ramp(m.get("fwi"), 0.0, 30.0),
        ramp(m.get("ffmc"), 70.0, 95.0),
        ramp(m.get("bui"), 0.0, 100.0),
    ]
    present = [p for p in parts if p is not None]
    return sum(present) / len(present) if present else None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)
