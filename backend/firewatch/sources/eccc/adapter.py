"""Environment and Climate Change Canada via MSC GeoMet (OGC API - Features).

Used for station weather: temperature, humidity, wind and precipitation, current
and historical. Wildfire-specific derived indices are taken from CWFIS rather
than recomputed here; reimplementing the Canadian Forest Fire Danger Rating
System would be both wasteful and less trustworthy than the official product.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
from firewatch.sources.http import get_json
from firewatch.sources.parsing import (
    clean_properties,
    first_present,
    geometry_from_geojson,
    parse_datetime,
)

GEOMET_BASE = "https://api.weather.gc.ca"
PAGE_LIMIT = 2000
MAX_PAGES = 10


class EcccGeoMetAdapter(SourceAdapter):
    adapter_id = "eccc_geomet"
    aging_after = timedelta(hours=6)
    stale_after = timedelta(days=2)

    @property
    def accumulate(self) -> bool:
        return self.feature_kind == "weather_observation"

    def _collection(self) -> str:
        collection = self.params.get("collection")
        if not collection:
            raise ValueError(f"Source '{self.source_id}' requires params.collection")
        return str(collection)

    def discover(self, ctx: IngestContext) -> DatasetManifest:
        return DatasetManifest(
            source_id=self.source_id,
            title=f"ECCC MSC GeoMet: {self._collection()}",
            source_url=f"{GEOMET_BASE}/collections/{self._collection()}",
            licence=self.params.get("licence", "Open Government Licence - Canada"),
            licence_url=self.params.get(
                "licence_url", "https://open.canada.ca/en/open-government-licence-canada"
            ),
            attribution=self.params.get(
                "attribution", "Environment and Climate Change Canada, MSC GeoMet"
            ),
            temporal_resolution="Hourly" if "hourly" in self._collection() else None,
            spatial_resolution="Point observations at station locations",
            caveats=[
                "Station observations are point measurements and do not resolve "
                "terrain-driven local variation in wind, temperature or humidity.",
            ],
        )

    def _bbox(self, ctx: IngestContext) -> str:
        west, south, east, north = ctx.bounds
        radius_km = float(self.params.get("search_radius_km", 0) or 0)
        if radius_km:
            pad_lat = radius_km / 111.0
            pad_lon = radius_km / 78.0
            west, south, east, north = (
                west - pad_lon, south - pad_lat, east + pad_lon, north + pad_lat,
            )
        return f"{west:.5f},{south:.5f},{east:.5f},{north:.5f}"

    def _datetime_filter(self, ctx: IngestContext) -> str | None:
        if self.feature_kind != "weather_observation":
            return None
        end = ctx.as_of or datetime.now(timezone.utc)
        start = end - timedelta(days=int(self.params.get("lookback_days", 14)))
        return (
            f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/"
            f"{end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )

    def fetch(self, ctx: IngestContext) -> RawDataset:
        url = f"{GEOMET_BASE}/collections/{self._collection()}/items"
        base_params: dict[str, object] = {
            "f": "json",
            "bbox": self._bbox(ctx),
            "limit": PAGE_LIMIT,
        }
        window = self._datetime_filter(ctx)
        if window:
            base_params["datetime"] = window

        features: list[dict] = []
        reported_total: int | None = None
        truncated = False
        offset = 0
        request_url = ""

        for _ in range(MAX_PAGES):
            params = {**base_params, "offset": offset}
            payload = get_json(url, params=params)
            if not request_url:
                from urllib.parse import urlencode

                request_url = f"{url}?{urlencode(params)}"

            page = payload.get("features") or []
            features.extend(page)
            total = payload.get("numberMatched")
            if isinstance(total, int):
                reported_total = total
            if len(page) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT
        else:
            truncated = True

        return RawDataset(
            payload={"features": features},
            request_url=request_url,
            content_hash=content_hash(features),
            reported_total=reported_total,
            truncated=truncated,
        )

    def normalize(self, raw: RawDataset, ctx: IngestContext) -> NormalizedFeatures:
        kind = self.feature_kind or "weather_observation"
        date_field = self.params.get("date_field")
        out: list[NormalizedFeature] = []
        undated = 0

        for index, feature in enumerate(raw.payload.get("features", [])):
            geom = geometry_from_geojson(feature.get("geometry"))
            if geom is None:
                continue
            props = clean_properties(feature.get("properties"))

            observed = None
            if kind == "weather_observation":
                observed = parse_datetime(
                    first_present(props, date_field or "", "LOCAL_DATE", "UTC_DATE", "DATE")
                )
                if observed is None:
                    undated += 1

            out.append(
                NormalizedFeature(
                    source_record_id=str(feature.get("id") or f"{self.source_id}-{index}"),
                    feature_kind=kind,
                    geometry=geom,
                    properties=props,
                    observed_at=observed,
                )
            )

        notes = []
        if undated:
            notes.append(f"{undated} observations had no parseable timestamp.")
        return NormalizedFeatures(features=out, notes=notes)

    def status_for(self, latest_observed_at, raw, report):
        if self.feature_kind == "weather_station":
            if report.accepted:
                return DataStatus.CURRENT, (
                    f"{report.accepted} climate stations are registered within the "
                    "search radius. Station metadata, not observations."
                )
            return DataStatus.PARTIAL, "No ECCC climate stations found near this area."
        if report.accepted == 0:
            return DataStatus.PARTIAL, (
                "No station observations were returned for the requested window. "
                "Nearby stations may report on a delay or may not be reporting."
            )
        return super().status_for(latest_observed_at, raw, report)
