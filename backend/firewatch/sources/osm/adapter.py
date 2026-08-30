"""OpenStreetMap via Overpass.

The cross-municipal baseline. When a municipality's own open-data portal is weak
or unreachable, OSM is what still lets Fire Watch produce a useful picture. It
is always the lowest-precedence vector source, and its records are marked
superseded when authoritative data for the same feature kind arrive.
"""

from __future__ import annotations

import time
from datetime import timedelta

from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from firewatch.sources.base import (
    DatasetManifest,
    DataStatus,
    IngestContext,
    NormalizedFeature,
    NormalizedFeatures,
    RawDataset,
    SourceAdapter,
    content_hash,
)
from firewatch.sources.http import SourceUnavailable, post_form
from firewatch.sources.parsing import parse_datetime

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

#: Tag values that make a closed way an area rather than a line.
_AREA_TAGS = {
    "building", "landuse", "leisure", "natural", "amenity", "area",
    "man_made", "boundary", "place", "waterway",
}

#: Overpass is a donated public service with a small number of query slots. A
#: municipality needs several queries, so they are spaced out to stay within
#: what the instance is willing to serve.
_MIN_SECONDS_BETWEEN_QUERIES = 4.0
_last_query_at = 0.0


def _wait_turn() -> None:
    global _last_query_at
    elapsed = time.monotonic() - _last_query_at
    if 0 < elapsed < _MIN_SECONDS_BETWEEN_QUERIES:
        time.sleep(_MIN_SECONDS_BETWEEN_QUERIES - elapsed)
    _last_query_at = time.monotonic()


class OsmOverpassAdapter(SourceAdapter):
    adapter_id = "osm_overpass"
    # OSM has no single observation date; per-element timestamps come from the
    # last edit, which reflects mapping activity rather than ground truth.
    aging_after = timedelta(days=365 * 3)
    stale_after = timedelta(days=365 * 10)

    def discover(self, ctx: IngestContext) -> DatasetManifest:
        return DatasetManifest(
            source_id=self.source_id,
            title=f"OpenStreetMap ({self.feature_kind})",
            source_url="https://www.openstreetmap.org/",
            licence=self.params.get("licence", "Open Database License (ODbL) 1.0"),
            licence_url=self.params.get(
                "licence_url", "https://opendatacommons.org/licenses/odbl/1-0/"
            ),
            attribution=self.params.get("attribution", "© OpenStreetMap contributors"),
            temporal_resolution="Continuously edited; no survey date",
            caveats=[
                "OpenStreetMap completeness varies by area and by feature type. "
                "Absence of a feature in OSM is not evidence that it does not exist.",
            ],
        )

    def _query(self, ctx: IngestContext) -> str:
        template = self.params.get("query")
        if not template:
            raise ValueError(f"Source '{self.source_id}' requires params.query")
        # Overpass bbox order is (south, west, north, east).
        west, south, east, north = ctx.bounds
        bbox = f"{south},{west},{north},{east}"
        body = template.format(bbox=bbox).strip()
        timeout = int(self.params.get("timeout", 180))
        return f"[out:json][timeout:{timeout}];\n(\n{body}\n);\nout geom meta;"

    def fetch(self, ctx: IngestContext) -> RawDataset:
        query = self._query(ctx)
        errors: list[str] = []
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                _wait_turn()
                response = post_form(
                    endpoint,
                    data={"data": query},
                    retries=2,
                    timeout=float(self.params.get("timeout", 180)) + 60,
                )
                payload = response.json()
            except Exception as exc:
                errors.append(f"{endpoint}: {exc}")
                continue
            elements = payload.get("elements", [])
            return RawDataset(
                payload={"elements": elements, "query": query},
                request_url=endpoint,
                content_hash=content_hash(elements),
                notes=[f"Mirror {endpoint} used."] if endpoint != OVERPASS_ENDPOINTS[0] else [],
            )
        raise SourceUnavailable("All Overpass mirrors failed: " + "; ".join(errors))

    @staticmethod
    def _is_area(tags: dict) -> bool:
        return any(tag in tags for tag in _AREA_TAGS)

    def _geometry_for(self, element: dict) -> BaseGeometry | None:
        etype = element.get("type")
        tags = element.get("tags") or {}

        if etype == "node":
            if element.get("lat") is None:
                return None
            return Point(element["lon"], element["lat"])

        if etype == "way":
            coords = [(p["lon"], p["lat"]) for p in element.get("geometry") or [] if p]
            if len(coords) < 2:
                return None
            closed = len(coords) >= 4 and coords[0] == coords[-1]
            if closed and self._is_area(tags):
                poly = Polygon(coords)
                return poly if poly.is_valid else poly.buffer(0)
            return LineString(coords)

        if etype == "relation":
            polygons: list[Polygon] = []
            lines: list[LineString] = []
            for member in element.get("members") or []:
                coords = [(p["lon"], p["lat"]) for p in member.get("geometry") or [] if p]
                if len(coords) < 2:
                    continue
                if len(coords) >= 4 and coords[0] == coords[-1]:
                    poly = Polygon(coords)
                    polygons.append(poly if poly.is_valid else poly.buffer(0))
                else:
                    lines.append(LineString(coords))
            if polygons:
                merged = unary_union(polygons)
                if isinstance(merged, (Polygon, MultiPolygon)):
                    return merged
            if lines:
                merged = unary_union(lines)
                # Try to close an open multipolygon ring set.
                try:
                    from shapely.ops import polygonize

                    built = list(polygonize(merged))
                    if built:
                        return unary_union(built)
                except Exception:
                    pass
                return merged
        return None

    def normalize(self, raw: RawDataset, ctx: IngestContext) -> NormalizedFeatures:
        kind = self.feature_kind or "building"
        out: list[NormalizedFeature] = []
        skipped = 0

        for element in raw.payload.get("elements", []):
            geom = self._geometry_for(element)
            if geom is None or geom.is_empty:
                skipped += 1
                continue
            tags = element.get("tags") or {}
            out.append(
                NormalizedFeature(
                    source_record_id=f"{element.get('type')}/{element.get('id')}",
                    feature_kind=kind,
                    geometry=geom,
                    properties={
                        "osm_type": element.get("type"),
                        "osm_id": element.get("id"),
                        "osm_version": element.get("version"),
                        **tags,
                    },
                    # OSM timestamps are last-edit times, so they describe the
                    # map, not the world. Recorded, but treated as weak evidence.
                    observed_at=parse_datetime(element.get("timestamp")),
                    source_url=(
                        f"https://www.openstreetmap.org/"
                        f"{element.get('type')}/{element.get('id')}"
                    ),
                )
            )

        notes = []
        if skipped:
            notes.append(f"{skipped} OSM elements had geometry we could not reconstruct.")
        return NormalizedFeatures(features=out, notes=notes)

    def status_for(self, latest_observed_at, raw, report):
        if not report.accepted:
            return DataStatus.PARTIAL, (
                "OpenStreetMap returned no features of this kind for the area."
            )
        # An OSM edit timestamp is not a survey date, so freshness is reported
        # as unknown rather than implying the data were verified recently.
        return DataStatus.UNKNOWN, (
            "OpenStreetMap has no survey date. The newest edit timestamp is "
            f"{latest_observed_at.date().isoformat() if latest_observed_at else 'unknown'}, "
            "which reflects mapping activity rather than ground verification."
        )
