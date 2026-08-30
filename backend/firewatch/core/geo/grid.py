"""H3 analysis grid generation.

The grid is the spine of the product: every derived metric, score component and
data gap hangs off a cell. Generation is driven entirely by a boundary polygon
and a resolution, so it works for any municipality.
"""

from __future__ import annotations

from dataclasses import dataclass

import h3
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry

from firewatch.core.geo.crs import area_m2


@dataclass(frozen=True)
class GridCell:
    h3_index: str
    resolution: int
    polygon: Polygon
    centroid_lat: float
    centroid_lon: float
    area_m2: float
    within_boundary: bool


def _as_polygons(geom: BaseGeometry) -> list[Polygon]:
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    raise TypeError(f"Expected polygonal geometry, got {geom.geom_type}")


def _cells_for_polygon(poly: Polygon, resolution: int) -> set[str]:
    """H3 cells covering one polygon.

    ``h3.geo_to_cells`` fills the interior. Boundary cells whose centre falls
    just outside are added explicitly so the grid fully covers the polygon
    rather than eroding its edge.
    """
    geo = mapping(poly)
    cells = set(h3.geo_to_cells(geo, resolution))

    # Ring out from the exterior so no part of the polygon is uncovered.
    edge_cells: set[str] = set()
    for ring in [poly.exterior, *poly.interiors]:
        for lon, lat in ring.coords:
            edge_cells.add(h3.latlng_to_cell(lat, lon, resolution))
    for cell in list(edge_cells):
        edge_cells.update(h3.grid_disk(cell, 1))

    return cells | edge_cells


def generate_grid(
    boundary_wgs84: BaseGeometry,
    resolution: int,
    metric_crs: str,
    buffered_wgs84: BaseGeometry | None = None,
) -> list[GridCell]:
    """Build the cell set.

    ``buffered_wgs84`` extends the grid beyond the legal boundary so that a
    cell at the municipal edge can still see the fuel, terrain and hotspots
    immediately outside it. Those cells are flagged ``within_boundary=False``.
    """
    extent = buffered_wgs84 if buffered_wgs84 is not None else boundary_wgs84

    indexes: set[str] = set()
    for poly in _as_polygons(extent):
        indexes |= _cells_for_polygon(poly, resolution)

    cells: list[GridCell] = []
    for idx in sorted(indexes):
        ring = h3.cell_to_boundary(idx)
        # h3 v4 returns (lat, lng); shapely wants (x=lon, y=lat).
        poly = Polygon([(lng, lat) for lat, lng in ring])
        if not poly.intersects(extent):
            continue
        lat, lng = h3.cell_to_latlng(idx)
        cells.append(
            GridCell(
                h3_index=idx,
                resolution=resolution,
                polygon=poly,
                centroid_lat=lat,
                centroid_lon=lng,
                area_m2=area_m2(poly, metric_crs),
                within_boundary=poly.intersects(boundary_wgs84),
            )
        )
    return cells


def cell_for_point(lat: float, lon: float, resolution: int) -> str:
    return h3.latlng_to_cell(lat, lon, resolution)


def cell_polygon(h3_index: str) -> Polygon:
    ring = h3.cell_to_boundary(h3_index)
    return Polygon([(lng, lat) for lat, lng in ring])


def approximate_edge_m(resolution: int) -> float:
    """Nominal cell edge length in metres, for UI copy about resolution."""
    return float(h3.average_hexagon_edge_length(resolution, unit="m"))


def geojson_to_shape(geojson: dict) -> BaseGeometry:
    return shape(geojson)
