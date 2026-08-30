"""Coordinate reference system handling.

Rule: EPSG:4326 is the canonical storage and interchange CRS. Every distance,
area or slope calculation happens in the municipality's configured metric CRS.
No metre-based maths on degrees, ever.
"""

from __future__ import annotations

from functools import lru_cache

from pyproj import CRS, Transformer
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

WGS84 = "EPSG:4326"


@lru_cache(maxsize=64)
def _transformer(src: str, dst: str) -> Transformer:
    return Transformer.from_crs(CRS.from_user_input(src), CRS.from_user_input(dst), always_xy=True)


def project(geom: BaseGeometry, src: str, dst: str) -> BaseGeometry:
    if src == dst:
        return geom
    return transform(_transformer(src, dst).transform, geom)


def to_metric(geom: BaseGeometry, metric_crs: str) -> BaseGeometry:
    return project(geom, WGS84, metric_crs)


def to_wgs84(geom: BaseGeometry, metric_crs: str) -> BaseGeometry:
    return project(geom, metric_crs, WGS84)


def buffer_meters(geom: BaseGeometry, meters: float, metric_crs: str) -> BaseGeometry:
    """Buffer a WGS84 geometry by a true metric distance, returning WGS84."""
    return to_wgs84(to_metric(geom, metric_crs).buffer(meters), metric_crs)


def area_m2(geom: BaseGeometry, metric_crs: str) -> float:
    return float(to_metric(geom, metric_crs).area)


def distance_m(a: BaseGeometry, b: BaseGeometry, metric_crs: str) -> float:
    return float(to_metric(a, metric_crs).distance(to_metric(b, metric_crs)))


def utm_crs_for(lon: float, lat: float) -> str:
    """Pick a sensible UTM zone.

    Lets a municipality config omit ``metric_crs`` and still get correct metric
    maths, which matters for onboarding somewhere new quickly.
    """
    zone = int((lon + 180) // 6) + 1
    return f"EPSG:{32600 + zone if lat >= 0 else 32700 + zone}"
