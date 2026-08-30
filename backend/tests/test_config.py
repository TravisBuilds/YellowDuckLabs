"""Municipality configuration and portability.

The claim that Fire Watch scales to another municipality is only true if
onboarding one is a configuration change. These tests hold that line: every
shipped config must load, every adapter it names must exist, and nothing under
``firewatch/core`` may hard-code a municipality.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from firewatch.core.municipality import (
    MunicipalityConfig,
    PrecedenceTier,
    list_municipalities,
    load_municipality,
)
from firewatch.sources.registry import ADAPTERS

CONFIG_DIR = Path(__file__).resolve().parents[1] / "firewatch" / "municipalities"
PACKAGE_DIR = Path(__file__).resolve().parents[1] / "firewatch"
ALL_IDS = sorted(list_municipalities())


def test_the_shipped_municipalities_are_present():
    """West Vancouver is the mission. Kelowna exists to prove portability."""
    assert "west-vancouver" in ALL_IDS
    assert "kelowna" in ALL_IDS


@pytest.mark.parametrize("municipality_id", ALL_IDS)
def test_config_loads_and_validates(municipality_id):
    config = load_municipality(municipality_id)
    assert isinstance(config, MunicipalityConfig)
    assert config.id == municipality_id
    assert config.name and config.short_name
    assert config.timezone


@pytest.mark.parametrize("municipality_id", ALL_IDS)
def test_every_adapter_named_in_config_is_registered(municipality_id):
    """A typo in an adapter name must fail here, not halfway through an ingest."""
    config = load_municipality(municipality_id)
    assert config.boundary.adapter in ADAPTERS, config.boundary.adapter
    for source in config.sources:
        assert source.adapter in ADAPTERS, f"{source.id} -> {source.adapter}"


@pytest.mark.parametrize("municipality_id", ALL_IDS)
def test_source_ids_are_unique(municipality_id):
    ids = [s.id for s in load_municipality(municipality_id).sources]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("municipality_id", ALL_IDS)
def test_every_source_declares_a_precedence_tier_and_a_licence(municipality_id):
    """Provenance is not optional. A source with no licence cannot be shown."""
    config = load_municipality(municipality_id)
    for source in [config.boundary, *config.sources]:
        assert isinstance(source.precedence_tier, PrecedenceTier)
        params = source.params or {}
        assert params.get("licence"), f"{source.id} declares no licence"
        assert params.get("attribution"), f"{source.id} declares no attribution"


@pytest.mark.parametrize("municipality_id", ALL_IDS)
def test_analysis_settings_are_sane(municipality_id):
    config = load_municipality(municipality_id)
    # Resolution 10 is about 100-150 m across, which is the brief's target.
    # Anything outside 7-11 is either uselessly coarse or ruinously expensive.
    assert 7 <= config.analysis.h3_resolution <= 11
    assert config.analysis.boundary_buffer_m > 0
    assert re.fullmatch(r"EPSG:\d{4,5}", config.analysis.metric_crs)


@pytest.mark.parametrize("municipality_id", ALL_IDS)
def test_metric_crs_is_a_projected_crs_not_degrees(municipality_id):
    """Measuring metres in EPSG:4326 is the error this guards against."""
    from pyproj import CRS

    crs = CRS.from_user_input(load_municipality(municipality_id).analysis.metric_crs)
    assert crs.is_projected
    assert crs.axis_info[0].unit_name == "metre"


@pytest.mark.parametrize("municipality_id", ALL_IDS)
def test_known_unknowns_are_declared(municipality_id):
    """The brief requires the system to state what it does not know."""
    config = load_municipality(municipality_id)
    assert config.known_unknowns, f"{municipality_id} declares no known unknowns"
    for entry in config.known_unknowns:
        assert len(entry) > 20, f"unhelpfully terse unknown: {entry!r}"


@pytest.mark.parametrize("municipality_id", ALL_IDS)
def test_community_tier_sources_carry_caveats(municipality_id):
    """Community data standing in for authoritative data must say so somewhere."""
    config = load_municipality(municipality_id)
    community = [
        s for s in config.sources if s.precedence_tier == PrecedenceTier.COMMUNITY
    ]
    assert community, "every municipality falls back to OSM for something"
    assert any(s.caveats for s in community), (
        "at least the vegetation and road fallbacks must carry caveats"
    )


def test_exactly_one_municipality_is_primary():
    """Which municipality the product opens on must be a declared decision.

    Falling back to list order meant adding Kelowna silently changed the default
    view to Kelowna, because it sorts before West Vancouver.
    """
    primaries = [m for m in ALL_IDS if load_municipality(m).primary]
    assert len(primaries) == 1, f"expected one primary, found {primaries}"
    assert primaries == ["west-vancouver"]


def test_kelowna_uses_a_different_utm_zone_than_west_vancouver():
    """Portability is not proven by a second config in the same zone."""
    wv = load_municipality("west-vancouver").analysis.metric_crs
    kl = load_municipality("kelowna").analysis.metric_crs
    assert wv != kl


def test_kelowna_has_no_municipal_tier_which_is_the_point():
    """The second municipality deliberately tests the missing-top-tier path."""
    config = load_municipality("kelowna")
    municipal = [
        s for s in config.sources if s.precedence_tier == PrecedenceTier.MUNICIPAL
    ]
    assert municipal == []


def test_west_vancouver_configures_its_municipal_sources():
    config = load_municipality("west-vancouver")
    municipal = [
        s for s in config.sources if s.precedence_tier == PrecedenceTier.MUNICIPAL
    ]
    assert len(municipal) >= 3


# --------------------------------------------------------------------------- #
# The portability claim itself
# --------------------------------------------------------------------------- #

#: Places allowed to name a municipality.
#:
#: ``cli.py`` and ``backtest.py`` carry example commands in their help text.
#: ``sources/municipal/`` is the designated home for genuinely place-specific
#: adapter code, and exists precisely so such code has somewhere to live that
#: is not the engine.
_ALLOWED_FILES = {"cli.py", "backtest.py"}
_ALLOWED_DIRS = {"municipal"}

_PLACE_NAMES = re.compile(r"west.vancouver|kelowna|dwvmaps|okanagan", re.I)


def _non_docstring_strings(tree: ast.AST) -> set[ast.Constant]:
    """Every string literal in a module except its docstrings.

    Comments and docstrings are exempt from the portability rule: naming a real
    example is how a caveat gets explained, and the wildfire domain is full of
    caveats that only make sense with a concrete case attached. What is not
    exempt is a literal that can reach a user or steer a query.
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    return {
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


def test_no_municipality_name_is_hard_coded_in_the_engine():
    """The load-bearing portability test.

    If a place name appears in a live string literal in engine code, the second
    municipality works by luck rather than by design.

    This test found a real leak: the analyst's suggested questions asked what
    "West Vancouver Fire & Rescue" needed to validate, and offered that question
    to Kelowna users too.
    """
    offenders: list[str] = []

    for path in PACKAGE_DIR.rglob("*.py"):
        if path.name in _ALLOWED_FILES:
            continue
        if _ALLOWED_DIRS & set(path.relative_to(PACKAGE_DIR).parts):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in _non_docstring_strings(tree):
            if _PLACE_NAMES.search(node.value):
                offenders.append(
                    f"{path.relative_to(PACKAGE_DIR)}:{node.lineno}: {node.value[:80]!r}"
                )

    assert not offenders, "municipality-specific literals in engine code:\n" + "\n".join(
        sorted(offenders)
    )


def test_place_specific_adapter_code_is_quarantined():
    """Municipal-specific code is allowed, but only in one place."""
    municipal = PACKAGE_DIR / "sources" / "municipal"
    assert municipal.is_dir(), (
        "the quarantine directory must exist, or place-specific code has nowhere "
        "to go except the engine"
    )


def test_suggested_questions_are_built_from_the_municipality():
    """Guards the leak directly, without needing a database."""
    from firewatch.api.routers.ai import SUGGESTED_QUESTIONS, suggested_questions

    class FakeMunicipality:
        short_name = "Kelowna"
        name = "City of Kelowna"

    questions = suggested_questions(FakeMunicipality())
    assert any("Kelowna fire service" in q for q in questions)
    assert not any(_PLACE_NAMES.search(q) for q in SUGGESTED_QUESTIONS)


def test_adding_a_municipality_needs_only_a_yaml_file():
    """A config referencing only portable sources must load with no code change."""
    portable = {
        "bcgw_wfs", "cwfis_wfs", "eccc_geomet", "osm_overpass",
        "terrain_tiles", "nasa_firms", "wms_overlay", "wcs_raster",
    }
    for municipality_id in ALL_IDS:
        config = load_municipality(municipality_id)
        adapters = {s.adapter for s in config.sources} | {config.boundary.adapter}
        # Only the municipal ArcGIS adapter is place-specific, and it is optional.
        assert adapters - portable <= {"arcgis_feature_service"}


@pytest.mark.parametrize("municipality_id", ALL_IDS)
def test_yaml_is_parseable_without_the_pydantic_layer(municipality_id):
    """Guards against a config that only loads because a default masked a typo."""
    raw = yaml.safe_load((CONFIG_DIR / f"{municipality_id}.yaml").read_text())
    assert raw["id"] == municipality_id
    assert isinstance(raw["sources"], list) and raw["sources"]
    assert "boundary" in raw
    for source in raw["sources"]:
        assert set(source) <= {
            "id", "adapter", "precedence_tier", "params", "caveats", "enabled",
        }, f"unrecognised key in {source.get('id')}"
    assert set(raw) <= {
        "id", "name", "short_name", "province", "country", "timezone", "primary",
        "analysis", "boundary", "sources", "documents", "known_unknowns",
    }, f"unrecognised top-level key in {municipality_id}.yaml"


@pytest.mark.parametrize("municipality_id", ALL_IDS)
def test_enabled_sources_defaults_to_all(municipality_id):
    config = load_municipality(municipality_id)
    enabled = config.enabled_sources()
    assert len(enabled) == len([s for s in config.sources if s.enabled])
    assert enabled, "a municipality with no enabled sources cannot be analysed"
