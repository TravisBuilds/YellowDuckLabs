"""Generic OGC WFS 2.0 adapter, plus the BCGW and CWFIS specialisations.

Both the BC Geographic Warehouse and the Canadian Wildland Fire Information
System publish GeoServer-backed WFS endpoints. One implementation covers both,
which is what allows any BC municipality to be onboarded from config alone.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from firewatch.sources.base import (
    DatasetManifest,
    DataStatus,
    IngestContext,
    NormalizedFeature,
    NormalizedFeatures,
    RawDataset,
    SourceAdapter,
    ValidationReport,
    content_hash,
)
from firewatch.sources.http import SourceUnavailable, get_json, get_text
from firewatch.sources.parsing import (
    clean_properties,
    first_present,
    geometry_from_geojson,
    parse_datetime,
    parse_year,
)

#: Hard cap per request. Prevents a mis-scoped query pulling millions of rows.
DEFAULT_PAGE_SIZE = 5000
MAX_RECORDS = 50000

_GEOMETRY_FIELD_RE = re.compile(
    r'name="(?P<name>[A-Za-z_0-9]+)"[^>]*type="gml:[A-Za-z]*(?:Geometry|Point|Curve|Surface|MultiGeometry)[A-Za-z]*PropertyType"'
)


class WfsAdapter(SourceAdapter):
    """OGC WFS 2.0 GetFeature over a bounding box, returning GeoJSON."""

    base_url: str = ""
    namespace: str = ""
    service_title: str = "OGC WFS"
    #: Fallback geometry column when discovery is unavailable.
    geometry_field: str = "SHAPE"

    def __init__(self, config) -> None:
        super().__init__(config)
        self._discovered_geometry_field: str | None = None

    def geometry_column(self) -> str:
        """Resolve the geometry column name.

        GeoServer layers within a single service disagree on this (CWFIS uses
        ``geometry`` for hotspots and ``the_geom`` for weather stations), and a
        wrong guess produces an ``Illegal property name`` error or, worse, a
        silently empty result. Discovering it from the schema removes an entire
        class of per-layer special-casing.
        """
        if configured := self.params.get("geometry_field"):
            return str(configured)
        if self._discovered_geometry_field:
            return self._discovered_geometry_field

        try:
            xml = get_text(
                self._endpoint(),
                params={
                    "service": "WFS",
                    "version": "2.0.0",
                    "request": "DescribeFeatureType",
                    "typeName": self._layer(),
                },
                retries=1,
            )
            match = _GEOMETRY_FIELD_RE.search(xml)
            if match:
                self._discovered_geometry_field = match.group("name")
                return self._discovered_geometry_field
        except Exception:
            pass

        return self.geometry_field

    def _layer(self) -> str:
        layer = self.params.get("layer")
        if not layer:
            raise ValueError(f"Source '{self.source_id}' requires params.layer")
        return f"{self.namespace}:{layer}" if self.namespace else str(layer)

    def _endpoint(self) -> str:
        return str(self.params.get("base_url") or self.base_url)

    def discover(self, ctx: IngestContext) -> DatasetManifest:
        return DatasetManifest(
            source_id=self.source_id,
            title=f"{self.service_title}: {self.params.get('layer')}",
            source_url=f"{self._endpoint()}?service=WFS&request=GetFeature"
                       f"&typeName={self._layer()}",
            licence=self.params.get("licence"),
            licence_url=self.params.get("licence_url"),
            attribution=self.params.get("attribution"),
            temporal_resolution=self.params.get("temporal_resolution"),
            spatial_resolution=self.params.get("spatial_resolution"),
        )

    def _spatial_filter(self, ctx: IngestContext) -> str:
        """BBOX in longitude/latitude order.

        WFS 2.0 axis order for EPSG:4326 is a well-known source of silent empty
        results. A CQL ``BBOX`` on the geometry column with an explicit CRS is
        unambiguous, so we use that instead of the ``bbox`` parameter.
        """
        west, south, east, north = _pad_bounds(ctx.bounds, self._pad_km())
        return f"BBOX({self.geometry_column()},{west},{south},{east},{north},'EPSG:4326')"

    def _pad_km(self) -> float:
        return max(
            float(self.params.get("search_radius_km", 0) or 0),
            float(self.params.get("search_buffer_km", 0) or 0),
        )

    def _temporal_filter(self, ctx: IngestContext) -> str | None:
        """Restrict fast-moving sources to a useful window.

        In historical mode this anchors on the requested date so the reconstruction
        does not leak observations that had not happened yet.
        """
        date_field = self.params.get("date_field")
        window_days = self.params.get("window_days")
        if not date_field or not window_days:
            return None
        end = ctx.as_of or datetime.now(timezone.utc)
        start = end - timedelta(days=int(window_days))
        return (
            f"{date_field} DURING {start.strftime('%Y-%m-%dT%H:%M:%SZ')}/"
            f"{end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )

    def _filters(self, ctx: IngestContext) -> str:
        clauses = [self._spatial_filter(ctx)]
        temporal = self._temporal_filter(ctx)
        if temporal:
            clauses.append(temporal)
        field = self.params.get("filter_field")
        value = self.params.get("filter_value")
        if field and value is not None:
            clauses.append(f"{field}='{value}'")
        extra = self.params.get("cql_filter")
        if extra:
            clauses.append(str(extra))
        return " AND ".join(clauses)

    def fetch(self, ctx: IngestContext) -> RawDataset:
        endpoint = self._endpoint()
        page_size = int(self.params.get("page_size", DEFAULT_PAGE_SIZE))
        cql = self._filters(ctx)

        collected: list[dict] = []
        reported_total: int | None = None
        truncated = False
        notes: list[str] = []
        start_index = 0
        request_url = ""

        # Several of these layers are views without a primary key. GeoServer
        # refuses a paged request on them unless an explicit sort is supplied,
        # so pagination is only attempted when config names a sort field.
        sort_by = self.params.get("sort_by")

        while True:
            params: dict[str, object] = {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeName": self._layer(),
                "outputFormat": "application/json",
                "srsName": "EPSG:4326",
                "count": page_size,
                "CQL_FILTER": cql,
            }
            if start_index:
                params["startIndex"] = start_index
            if sort_by:
                params["sortBy"] = str(sort_by)

            payload = get_json(endpoint, params=params)
            request_url = request_url or _url_with(endpoint, params)

            features = payload.get("features") or []
            total = payload.get("totalFeatures")
            if isinstance(total, int):
                reported_total = total
            collected.extend(features)

            if len(features) < page_size:
                break
            if not sort_by:
                truncated = True
                notes.append(
                    f"Retrieved the first {len(collected)} records only. This layer "
                    "has no primary key, so paging requires a 'sort_by' field in "
                    "the source config."
                )
                break
            start_index += page_size
            if len(collected) >= MAX_RECORDS:
                truncated = True
                notes.append(
                    f"Stopped at the {MAX_RECORDS} record cap; the source holds more."
                )
                break

        return RawDataset(
            payload={"features": collected},
            request_url=request_url,
            content_hash=content_hash(collected),
            reported_total=reported_total,
            truncated=truncated,
            notes=notes,
        )

    def normalize(self, raw: RawDataset, ctx: IngestContext) -> NormalizedFeatures:
        kind = self.feature_kind or "boundary"
        date_field = self.params.get("date_field")
        out: list[NormalizedFeature] = []
        notes: list[str] = []
        undated = 0

        for index, raw_feature in enumerate(raw.payload.get("features", [])):
            geom = geometry_from_geojson(raw_feature.get("geometry"))
            if geom is None:
                continue
            props = clean_properties(raw_feature.get("properties"))

            observed = None
            if date_field:
                observed = parse_datetime(first_present(props, date_field))
                if observed is None:
                    observed = parse_year(
                        first_present(props, "FIRE_YEAR", "year", "YEAR")
                    )
                if observed is None:
                    undated += 1

            record_id = str(
                raw_feature.get("id")
                or first_present(props, "OBJECTID", "uid", "id", "FIRE_NUMBER")
                or f"{self.source_id}-{index}"
            )
            out.append(
                NormalizedFeature(
                    source_record_id=record_id,
                    feature_kind=kind,
                    geometry=geom,
                    properties=props,
                    observed_at=observed,
                )
            )

        if date_field and undated:
            notes.append(
                f"{undated} records had no parseable value in '{date_field}'."
            )
        return NormalizedFeatures(features=out, notes=notes)


def _url_with(endpoint: str, params: dict) -> str:
    from urllib.parse import urlencode

    return f"{endpoint}?{urlencode({k: v for k, v in params.items() if v != ''})}"


def _pad_bounds(
    bounds: tuple[float, float, float, float], pad_km: float
) -> tuple[float, float, float, float]:
    """Widen a lon/lat envelope by an approximate distance.

    Degree approximation is acceptable here because this only decides how much
    extra area to *query*; all real distance work happens in the metric CRS.
    """
    west, south, east, north = bounds
    if not pad_km:
        return bounds
    import math

    mid_lat = (south + north) / 2.0
    pad_lat = pad_km / 111.0
    pad_lon = pad_km / max(1.0, 111.0 * math.cos(math.radians(mid_lat)))
    return west - pad_lon, south - pad_lat, east + pad_lon, north + pad_lat


class BcgwWfsAdapter(WfsAdapter):
    """BC Geographic Warehouse.

    Covers municipal boundaries, historical fire perimeters and incidents, and
    any other provincial layer named in config.
    """

    adapter_id = "bcgw_wfs"
    base_url = "https://openmaps.gov.bc.ca/geo/pub/wfs"
    namespace = "pub"
    service_title = "BC Geographic Warehouse"
    geometry_field = "SHAPE"
    # Provincial administrative and fire-history layers update episodically;
    # a boundary a year old is not stale.
    aging_after = timedelta(days=365)
    stale_after = timedelta(days=365 * 5)

    def _spatial_filter(self, ctx: IngestContext) -> str:
        # A named-boundary lookup must search the province, not our own bbox.
        if self.params.get("filter_field"):
            return ""
        return super()._spatial_filter(ctx)

    def _filters(self, ctx: IngestContext) -> str:
        return " AND ".join(c for c in super()._filters(ctx).split(" AND ") if c)

    def status_for(self, latest_observed_at, raw, report):
        if self.feature_kind == "boundary":
            # A legal boundary has no meaningful staleness clock.
            if report.accepted:
                return DataStatus.CURRENT, None
            return DataStatus.FAILED, "No boundary polygon matched the configured filter."
        return super().status_for(latest_observed_at, raw, report)


class CwfisWfsAdapter(WfsAdapter):
    """Canadian Wildland Fire Information System GeoServer.

    Fire weather stations carry real FFMC/DMC/DC/ISI/BUI/FWI values, and the
    hotspot archive supports historical replay.
    """

    adapter_id = "cwfis_wfs"
    base_url = "https://cwfis.cfs.nrcan.gc.ca/geoserver/public/wfs"
    namespace = "public"
    service_title = "CWFIS"
    geometry_field = "the_geom"
    aging_after = timedelta(hours=12)
    stale_after = timedelta(days=3)

    @property
    def accumulate(self) -> bool:
        # Hotspots and station observations are time series; retain history so
        # historical mode has something to replay.
        return self.feature_kind in {"satellite_hotspot", "fire_weather_observation"}

    def _temporal_filter(self, ctx: IngestContext) -> str | None:
        date_field = self.params.get("date_field")
        if not date_field:
            return None

        if self.feature_kind == "satellite_hotspot":
            # Historical mode: a window ending on the requested date.
            # Current mode: recent detections plus enough history to be useful.
            window = int(self.params.get("window_days", 14 if ctx.as_of else 3650))
        elif self.feature_kind == "fire_weather_observation":
            window = int(self.params.get("window_days", 3 if ctx.as_of else 2))
        else:
            return None

        end = ctx.as_of or datetime.now(timezone.utc)
        start = end - timedelta(days=window)
        return (
            f"{date_field} >= '{start.strftime('%Y-%m-%dT%H:%M:%SZ')}' AND "
            f"{date_field} <= '{end.strftime('%Y-%m-%dT%H:%M:%SZ')}'"
        )

    def normalize(self, raw: RawDataset, ctx: IngestContext) -> NormalizedFeatures:
        normalized = super().normalize(raw, ctx)
        for feature in normalized.features:
            name = feature.properties.get("name")
            if isinstance(name, str):
                feature.properties["name"] = _tidy_station_name(name)
        return normalized

    def status_for(self, latest_observed_at, raw, report):
        if self.feature_kind == "vegetation_cell":
            # Fire danger polygons carry no date field; the layer name says
            # "current" and we cannot verify more than that.
            if report.accepted:
                return DataStatus.UNKNOWN, (
                    "CWFIS publishes this layer as 'current' but exposes no "
                    "observation timestamp, so its exact age is unverifiable."
                )
            return DataStatus.PARTIAL, "No fire-danger polygons intersected the area."
        return super().status_for(latest_observed_at, raw, report)


def _tidy_station_name(name: str | None) -> str | None:
    """Make a CWFIS station name readable.

    The station table is fixed-width and inconsistently padded: some names
    arrive space-padded, others use '+' in place of every space, so the same
    field yields both "SAANICHTON CFIA" and "WEST+VANCOUVER+AUT". Only the
    padding character is touched; the name itself is left alone.

    An unnamed station is passed through as None. A single null in a station
    table should cost us that station's label, not the whole ingest.
    """
    if name is None:
        return None
    tidied = " ".join(str(name).replace("+", " ").split())
    return tidied or None
