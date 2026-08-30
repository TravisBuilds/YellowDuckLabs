"""Parsing helpers shared by adapters."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

_ISO_CLEAN = re.compile(r"Z$", re.IGNORECASE)

_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y%m%d",
)


def parse_datetime(value: Any) -> datetime | None:
    """Best-effort parse to an aware UTC datetime.

    Returns ``None`` rather than guessing. A missing observation date is
    reported as unknown, never defaulted to "now".
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Epoch milliseconds is the ArcGIS convention.
        seconds = value / 1000.0 if abs(value) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    cleaned = _ISO_CLEAN.sub("", text)
    if "." in cleaned:
        cleaned = cleaned.split(".", 1)[0]
    cleaned = cleaned.replace("+00:00", "")

    for fmt in _FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_year(value: Any) -> datetime | None:
    """Some fire-history datasets only publish a year."""
    if value is None:
        return None
    try:
        year = int(str(value).strip()[:4])
    except (TypeError, ValueError):
        return None
    if 1800 <= year <= 2200:
        return datetime(year, 1, 1, tzinfo=timezone.utc)
    return None


def geometry_from_geojson(geojson: dict | None) -> BaseGeometry | None:
    if not geojson:
        return None
    try:
        geom = shape(geojson)
    except Exception:
        return None
    return geom if geom and not geom.is_empty else None


def clean_properties(props: dict[str, Any] | None) -> dict[str, Any]:
    """Drop noise fields and make values JSON-safe."""
    if not props:
        return {}
    out: dict[str, Any] = {}
    for key, value in props.items():
        # Binary annotation blobs from Oracle-backed provincial services.
        if key in {"SE_ANNO_CAD_DATA", "Shape", "SHAPE"}:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        out[key] = value
    return out


def first_present(props: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in props and props[key] not in (None, ""):
            return props[key]
    # Case-insensitive second pass; OGC services vary on field casing.
    lowered = {k.lower(): v for k, v in props.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None
