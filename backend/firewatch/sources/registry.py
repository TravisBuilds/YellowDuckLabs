"""Adapter registry.

``adapter:`` in a municipality YAML resolves through this table. Adding a
municipality is a config change; adding a *kind* of source is a new adapter
registered here.
"""

from __future__ import annotations

from firewatch.core.municipality import SourceConfig
from firewatch.sources.arcgis import ArcGisFeatureServiceAdapter, WmsOverlayAdapter
from firewatch.sources.base import SourceAdapter
from firewatch.sources.dem.adapter import TerrainTilesAdapter
from firewatch.sources.eccc.adapter import EcccGeoMetAdapter
from firewatch.sources.nasa_firms.adapter import NasaFirmsAdapter
from firewatch.sources.osm.adapter import OsmOverpassAdapter
from firewatch.sources.wcs import WcsRasterAdapter
from firewatch.sources.wfs import BcgwWfsAdapter, CwfisWfsAdapter, WfsAdapter

ADAPTERS: dict[str, type[SourceAdapter]] = {
    cls.adapter_id: cls
    for cls in (
        ArcGisFeatureServiceAdapter,
        BcgwWfsAdapter,
        CwfisWfsAdapter,
        EcccGeoMetAdapter,
        NasaFirmsAdapter,
        OsmOverpassAdapter,
        TerrainTilesAdapter,
        WcsRasterAdapter,
        WmsOverlayAdapter,
    )
}
# Generic WFS is available for provinces other than BC without new code.
ADAPTERS["wfs"] = WfsAdapter


def build_adapter(config: SourceConfig) -> SourceAdapter:
    try:
        cls = ADAPTERS[config.adapter]
    except KeyError:
        raise KeyError(
            f"Unknown adapter '{config.adapter}' for source '{config.id}'. "
            f"Registered adapters: {', '.join(sorted(ADAPTERS))}"
        ) from None
    return cls(config)
