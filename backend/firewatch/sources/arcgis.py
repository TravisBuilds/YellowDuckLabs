"""Generic ArcGIS REST Feature Service adapter.

Most Canadian municipalities that publish GIS data publish it through ArcGIS
Server, so this one adapter covers the municipal-authoritative tier for a large
number of potential deployments. It discovers layers by name pattern rather than
by hard-coded layer id, so a service reorganisation does not require code
changes.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from firewatch.sources.base import (
    DatasetManifest,
    IngestContext,
    NormalizedFeature,
    NormalizedFeatures,
    RawDataset,
    SourceAdapter,
    content_hash,
)
from firewatch.sources.http import SourceUnavailable, get_json
from firewatch.sources.parsing import (
    clean_properties,
    first_present,
    geometry_from_geojson,
    parse_datetime,
)

PAGE_SIZE = 2000
MAX_RECORDS = 60000


class ArcGisFeatureServiceAdapter(SourceAdapter):
    adapter_id = "arcgis_feature_service"
    aging_after = timedelta(days=365)
    stale_after = timedelta(days=365 * 5)

    def __init__(self, config) -> None:
        super().__init__(config)
        self._layers: list[dict[str, Any]] | None = None

    def _base_url(self) -> str:
        base = self.params.get("base_url")
        if not base:
            raise ValueError(f"Source '{self.source_id}' requires params.base_url")
        return str(base).rstrip("/")

    def _patterns(self) -> list[str]:
        return [str(p).lower() for p in self.params.get("layer_name_patterns", [])]

    def _walk_catalogue(self) -> list[dict[str, Any]]:
        """Enumerate matching layers across the service catalogue."""
        base = self._base_url()
        matches: list[dict[str, Any]] = []
        patterns = self._patterns()

        root = get_json(base, params={"f": "json"}, browser_shaped=True, retries=1)
        folders = ["", *[str(f) for f in root.get("folders", [])]]

        for folder in folders:
            folder_url = f"{base}/{folder}" if folder else base
            try:
                listing = get_json(
                    folder_url, params={"f": "json"}, browser_shaped=True, retries=1
                )
            except Exception:
                continue
            for service in listing.get("services", []):
                if service.get("type") not in {"FeatureServer", "MapServer"}:
                    continue
                service_url = f"{base}/{service['name']}/{service['type']}"
                try:
                    meta = get_json(
                        service_url, params={"f": "json"}, browser_shaped=True, retries=1
                    )
                except Exception:
                    continue
                for layer in meta.get("layers", []):
                    name = str(layer.get("name", "")).lower()
                    if patterns and not any(p in name for p in patterns):
                        continue
                    matches.append(
                        {
                            "url": f"{service_url}/{layer['id']}",
                            "name": layer.get("name"),
                            "service": service.get("name"),
                        }
                    )
        return matches

    def discover(self, ctx: IngestContext) -> DatasetManifest:
        manifest = DatasetManifest(
            source_id=self.source_id,
            title=f"ArcGIS Feature Service ({self.feature_kind})",
            source_url=self._base_url(),
            licence=self.params.get("licence"),
            licence_url=self.params.get("licence_url"),
            attribution=self.params.get("attribution"),
        )
        try:
            self._layers = self._walk_catalogue()
        except Exception as exc:
            manifest.available = False
            manifest.message = (
                f"Could not read the ArcGIS service catalogue at {self._base_url()}: "
                f"{exc} — the municipal authoritative source for "
                f"'{self.feature_kind}' is therefore NOT in use, and lower-precedence "
                "data are standing in for it."
            )
            return manifest

        if not self._layers:
            manifest.available = False
            manifest.message = (
                f"The service catalogue was readable but no layer name matched "
                f"{self._patterns()}."
            )
            return manifest

        manifest.title = (
            f"{self.params.get('attribution', 'ArcGIS')}: "
            + ", ".join(str(layer["name"]) for layer in self._layers[:6])
        )
        return manifest

    def fetch(self, ctx: IngestContext) -> RawDataset:
        if self._layers is None:
            self._layers = self._walk_catalogue()
        if not self._layers:
            raise SourceUnavailable("No matching ArcGIS layers.")

        west, south, east, north = ctx.bounds
        collected: list[dict] = []
        request_urls: list[str] = []
        truncated = False

        for layer in self._layers:
            offset = 0
            while True:
                params = {
                    "f": "geojson",
                    "where": "1=1",
                    "outFields": "*",
                    "geometry": f"{west},{south},{east},{north}",
                    "geometryType": "esriGeometryEnvelope",
                    "inSR": 4326,
                    "outSR": 4326,
                    "spatialRel": "esriSpatialRelIntersects",
                    "resultOffset": offset,
                    "resultRecordCount": PAGE_SIZE,
                }
                payload = get_json(
                    f"{layer['url']}/query",
                    params=params,
                    browser_shaped=True,
                    retries=1,
                )
                request_urls.append(f"{layer['url']}/query")
                page = payload.get("features") or []
                for feature in page:
                    feature.setdefault("properties", {})
                    feature["properties"]["_layer_name"] = layer["name"]
                collected.extend(page)

                if len(page) < PAGE_SIZE or not payload.get("properties", {}).get(
                    "exceededTransferLimit", len(page) == PAGE_SIZE
                ):
                    break
                offset += PAGE_SIZE
                if len(collected) >= MAX_RECORDS:
                    truncated = True
                    break
            if truncated:
                break

        return RawDataset(
            payload={"features": collected},
            request_url=request_urls[0] if request_urls else self._base_url(),
            content_hash=content_hash(collected),
            truncated=truncated,
            notes=[f"Layers used: {', '.join(str(l['name']) for l in self._layers)}"],
        )

    def normalize(self, raw: RawDataset, ctx: IngestContext) -> NormalizedFeatures:
        kind = self.feature_kind or "building"
        date_field = self.params.get("date_field")
        out: list[NormalizedFeature] = []

        for index, feature in enumerate(raw.payload.get("features", [])):
            geom = geometry_from_geojson(feature.get("geometry"))
            if geom is None:
                continue
            props = clean_properties(feature.get("properties"))
            observed = parse_datetime(first_present(props, date_field or "", "LAST_EDITED_DATE"))
            record_id = str(
                feature.get("id")
                or first_present(props, "OBJECTID", "objectid", "FID", "GlobalID")
                or f"{self.source_id}-{index}"
            )
            out.append(
                NormalizedFeature(
                    source_record_id=f"{props.get('_layer_name', '')}:{record_id}",
                    feature_kind=kind,
                    geometry=geom,
                    properties=props,
                    observed_at=observed,
                )
            )
        return NormalizedFeatures(features=out)


class WmsOverlayAdapter(SourceAdapter):
    """Registers WMS layers for the map without ingesting features.

    Raster products such as the national fuel-type grid and the Fire Weather
    Index are best shown as authoritative overlays rather than re-derived. The
    provenance record still applies, so the data-health panel can list them.
    """

    adapter_id = "wms_overlay"
    produces_features = False

    def discover(self, ctx: IngestContext) -> DatasetManifest:
        layers = self.params.get("layers") or []
        return DatasetManifest(
            source_id=self.source_id,
            title="WMS overlays: " + ", ".join(str(l.get("label")) for l in layers),
            source_url=str(self.params.get("base_url", "")),
            licence=self.params.get("licence"),
            licence_url=self.params.get("licence_url"),
            attribution=self.params.get("attribution"),
            caveats=[
                "These are visual overlays served live from the publisher. Values "
                "shown on the map are not ingested and are not used in scoring.",
            ],
        )

    def fetch(self, ctx: IngestContext) -> RawDataset:
        base = str(self.params.get("base_url", ""))
        layers = self.params.get("layers") or []
        if not base or not layers:
            raise SourceUnavailable("wms_overlay requires base_url and layers.")
        # Confirm the service answers before advertising overlays to the UI.
        get_json  # noqa: B018 - referenced to keep the import meaningful
        from firewatch.sources.http import get_text

        get_text(
            base,
            params={"service": "WMS", "version": "1.3.0", "request": "GetCapabilities"},
            retries=1,
        )
        return RawDataset(
            payload={"layers": layers, "base_url": base},
            request_url=f"{base}?service=WMS&request=GetCapabilities",
            content_hash=content_hash(layers),
            notes=[f"{len(layers)} overlay layers available."],
        )

    def normalize(self, raw: RawDataset, ctx: IngestContext) -> NormalizedFeatures:
        return NormalizedFeatures(features=[])
