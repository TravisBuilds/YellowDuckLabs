"""Municipality configuration model.

A municipality is a YAML file. Onboarding a new one must not require changes in
``firewatch.core``. The only municipality-specific *code* permitted lives in
``firewatch.sources.municipal.<name>``, referenced here by adapter id.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from firewatch.config import settings


class PrecedenceTier(str, Enum):
    """Data precedence, per the brief's section 6.

    Lower ``rank`` wins when two sources describe the same real-world thing.
    Conflicts are recorded, never silently dropped.
    """

    MUNICIPAL = "municipal"
    PROVINCIAL = "provincial"
    FEDERAL = "federal"
    REMOTE_SENSING = "remote_sensing"
    COMMUNITY = "community"
    DERIVED = "derived"

    @property
    def rank(self) -> int:
        return {
            PrecedenceTier.MUNICIPAL: 1,
            PrecedenceTier.PROVINCIAL: 2,
            PrecedenceTier.FEDERAL: 3,
            PrecedenceTier.REMOTE_SENSING: 4,
            PrecedenceTier.COMMUNITY: 5,
            PrecedenceTier.DERIVED: 6,
        }[self]


class FeatureKind(str, Enum):
    """What a normalized feature *is*, independent of which source produced it.

    Scoring and the evidence panel consume feature kinds, so a municipality that
    gets buildings from OSM and one that gets them from a municipal portal are
    handled by identical core logic.
    """

    BOUNDARY = "boundary"
    BUILDING = "building"
    ROAD = "road"
    PARCEL = "parcel"
    PARK = "park"
    WATER_ASSET = "water_asset"
    FIRE_STATION = "fire_station"
    FUEL_TREATMENT = "fuel_treatment"
    FIRE_EVENT = "fire_event"
    FIRE_PERIMETER = "fire_perimeter"
    SATELLITE_HOTSPOT = "satellite_hotspot"
    WEATHER_STATION = "weather_station"
    WEATHER_OBSERVATION = "weather_observation"
    FIRE_WEATHER_OBSERVATION = "fire_weather_observation"
    TERRAIN_CELL = "terrain_cell"
    VEGETATION_CELL = "vegetation_cell"


class AnalysisConfig(BaseModel):
    """Spatial analysis parameters.

    ``h3_resolution`` 10 gives ~15 000 m2 cells (~65 m edge), which is the
    brief's recommended internal unit.
    """

    h3_resolution: int = 10
    # Fires do not respect municipal boundaries. We ingest a buffered area so
    # that a cell on the boundary still sees the fuel and terrain next door.
    boundary_buffer_m: float = 2000.0
    # Metric CRS for all distance/area/slope work. Must be appropriate locally.
    metric_crs: str = "EPSG:3857"

    @field_validator("h3_resolution")
    @classmethod
    def _resolution_in_range(cls, v: int) -> int:
        if not 0 <= v <= 15:
            raise ValueError("h3_resolution must be between 0 and 15")
        return v


class SourceConfig(BaseModel):
    """One configured data source for one municipality."""

    id: str
    adapter: str
    enabled: bool = True
    precedence_tier: PrecedenceTier = PrecedenceTier.COMMUNITY
    # Free-form adapter arguments. Validated by the adapter, not here, so a new
    # adapter never requires a core schema change.
    params: dict[str, Any] = Field(default_factory=dict)
    # Operator-supplied caveats surfaced verbatim in the UI.
    caveats: list[str] = Field(default_factory=list)


class DocumentConfig(BaseModel):
    """A local wildfire document for the retrieval layer."""

    id: str
    title: str
    publisher: str
    url: str
    publication_date: str | None = None
    # Local path, if the operator has downloaded it (some portals block robots).
    local_path: str | None = None


class MunicipalityConfig(BaseModel):
    id: str
    name: str
    short_name: str
    province: str
    country: str
    timezone: str
    #: Which municipality the UI opens on. Without this the selector falls back
    #: to alphabetical order, and adding a municipality whose name sorts earlier
    #: silently changes what the product opens on.
    primary: bool = False
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    boundary: SourceConfig
    sources: list[SourceConfig] = Field(default_factory=list)
    documents: list[DocumentConfig] = Field(default_factory=list)
    # Things a local expert must confirm. First-class content, not a footnote.
    known_unknowns: list[str] = Field(default_factory=list)

    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]

    def source(self, source_id: str) -> SourceConfig | None:
        for s in self.sources:
            if s.id == source_id:
                return s
        return None


def _config_path(municipality_id: str, directory: Path | None = None) -> Path:
    directory = directory or settings.municipalities_dir
    return directory / f"{municipality_id}.yaml"


def load_municipality(municipality_id: str, directory: Path | None = None) -> MunicipalityConfig:
    path = _config_path(municipality_id, directory)
    if not path.exists():
        available = ", ".join(sorted(list_municipalities(directory))) or "none"
        raise FileNotFoundError(
            f"No municipality config at {path}. Available: {available}"
        )
    raw = yaml.safe_load(path.read_text())
    config = MunicipalityConfig.model_validate(raw)
    if config.id != municipality_id:
        raise ValueError(
            f"Municipality id mismatch: file {path.name} declares id '{config.id}'"
        )
    return config


def list_municipalities(directory: Path | None = None) -> list[str]:
    directory = directory or settings.municipalities_dir
    if not directory.exists():
        return []
    return [p.stem for p in directory.glob("*.yaml")]
