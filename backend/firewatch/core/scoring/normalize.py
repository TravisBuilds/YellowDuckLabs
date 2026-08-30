"""Normalization primitives for the priority score.

Every transform here is an explicit, inspectable ramp with named breakpoints.
Breakpoints are engineering judgement, not fitted parameters, and they are
surfaced in the UI so a wildfire specialist can disagree with a specific number
rather than with a black box.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class MetricInput:
    """One derived metric feeding a signal."""

    metric: str
    value: float | None
    unit: str | None = None
    confidence: float | None = None
    sources: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.value is not None


@dataclass
class Signal:
    """A normalized 0..1 contribution to one score component."""

    name: str
    label: str
    value: float | None
    weight: float
    rationale: str
    inputs_used: list[MetricInput] = field(default_factory=list)
    inputs_missing: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.value is not None

    @property
    def confidence(self) -> float:
        confidences = [i.confidence for i in self.inputs_used if i.confidence is not None]
        return sum(confidences) / len(confidences) if confidences else 0.0

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "value": None if self.value is None else round(self.value, 4),
            "weight": self.weight,
            "rationale": self.rationale,
            "confidence": round(self.confidence, 3),
            "inputs_used": [
                {
                    "metric": i.metric,
                    "value": i.value,
                    "unit": i.unit,
                    "confidence": i.confidence,
                    "sources": i.sources,
                }
                for i in self.inputs_used
            ],
            "inputs_missing": self.inputs_missing,
        }


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def ramp(value: float | None, low: float, high: float) -> float | None:
    """Linear 0..1 ramp. ``low`` maps to 0, ``high`` to 1."""
    if value is None:
        return None
    if high == low:
        return None
    return clamp01((value - low) / (high - low))


def inverse_ramp(value: float | None, near: float, far: float) -> float | None:
    """1 when at or below ``near``, falling to 0 at ``far``.

    Used for "closer is worse" relationships such as distance to structures.
    """
    if value is None:
        return None
    if far == near:
        return None
    return clamp01((far - value) / (far - near))


def log_ramp(value: float | None, low: float, high: float) -> float | None:
    """Ramp on a log scale, for counts with long tails."""
    if value is None:
        return None
    lo = math.log1p(max(0.0, low))
    hi = math.log1p(max(0.0, high))
    if hi == lo:
        return None
    return clamp01((math.log1p(max(0.0, value)) - lo) / (hi - lo))


def slope_factor(slope_deg: float | None) -> float | None:
    """Slope contribution to upslope spread rate.

    Follows the shape used in the Canadian FBP system, where the slope effect
    rises steeply and saturates: negligible below about 10 degrees, strong by
    30, saturated around 45+.
    """
    if slope_deg is None:
        return None
    return clamp01((math.exp(0.0693 * min(slope_deg, 45.0)) - 1.0) / (math.exp(0.0693 * 45.0) - 1.0))


def aspect_factor(aspect_deg: float | None, hemisphere: str = "north") -> float | None:
    """Solar exposure by aspect.

    In the northern hemisphere south-facing slopes dry out fastest and carry the
    most aggressive fire behaviour; north-facing slopes hold moisture.
    """
    if aspect_deg is None:
        return None
    target = 180.0 if hemisphere == "north" else 0.0
    difference = abs((aspect_deg - target + 180.0) % 360.0 - 180.0)
    return clamp01(1.0 - difference / 180.0)


def combine(signals: list[Signal]) -> tuple[float | None, float, list[Signal]]:
    """Weighted mean over available signals.

    Returns (value, completeness, signals). Weights are renormalized across the
    signals that actually have data, so a missing input lowers completeness and
    confidence rather than silently pulling the component toward zero.
    """
    available = [s for s in signals if s.available]
    total_weight = sum(s.weight for s in signals)
    if not available or total_weight == 0:
        return None, 0.0, signals

    available_weight = sum(s.weight for s in available)
    value = sum(s.value * s.weight for s in available) / available_weight
    return clamp01(value), available_weight / total_weight, signals


def geometric_mean(values: list[float], floor: float = 0.02) -> float | None:
    """Geometric mean, the monotone form of the brief's multiplicative formula.

    The brief specifies ``priority = ignition x spread x consequence x
    observation_gap x access``. Taken literally, a five-way product of values in
    0..1 collapses almost everything toward zero and stops being readable. The
    geometric mean is that product raised to 1/n: it preserves the important
    property that one very low component suppresses the whole score, while
    keeping the result on a 0..1 scale a person can reason about.

    Values are floored at a small epsilon so that a single genuine zero does not
    annihilate the score entirely, which would hide the other components.

    An empty input returns None, not zero. Zero is a claim that priority here is
    minimal; None is the absence of a claim.
    """
    if not values:
        return None
    floored = [max(floor, min(1.0, v)) for v in values]
    return math.exp(sum(math.log(v) for v in floored) / len(floored))
