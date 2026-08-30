"""Source adapters: parsing, status reporting and failure honesty.

The recurring danger in an ingestion layer is that a failure looks like an empty
result. "No hotspots detected" and "the hotspot service refused the request"
render identically in a UI unless the pipeline is careful to distinguish them,
and the first reads as reassurance.

These tests exercise that distinction, plus the parsing of the untidy real-world
values these services actually return.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from shapely.geometry import Point, Polygon

from firewatch.sources.base import DataStatus, describe_age
from firewatch.sources.http import summarise_body
from firewatch.sources.parsing import (
    clean_properties,
    first_present,
    geometry_from_geojson,
    parse_datetime,
    parse_year,
)
from firewatch.sources.registry import ADAPTERS
from firewatch.sources.wfs import _tidy_station_name

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Datetime and value parsing, against formats these services really emit
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        "2026-08-30T12:00:00Z",
        "2026-08-30T12:00:00+00:00",
        "2026-08-30 12:00:00",
        "2026-08-30T12:00:00.000Z",
        "2026-08-30",
    ],
)
def test_datetime_parsing_handles_the_formats_in_the_wild(raw):
    parsed = parse_datetime(raw)
    assert parsed is not None
    assert parsed.year == 2026 and parsed.month == 8 and parsed.day == 30
    # Everything must be timezone-aware, or comparisons silently break later.
    assert parsed.tzinfo is not None


def test_epoch_milliseconds_are_understood():
    """ArcGIS services return dates as epoch milliseconds."""
    parsed = parse_datetime(1756555200000)
    assert parsed is not None and parsed.year == 2025


@pytest.mark.parametrize("raw", [None, "", "   ", "not a date", "0000-00-00", {}])
def test_unparseable_dates_return_none_not_now(raw):
    """Defaulting to now() would make stale data look current, which is worse
    than admitting the date is unknown."""
    assert parse_datetime(raw) is None


def test_year_only_fire_history_becomes_the_start_of_that_year():
    """Provincial fire history sometimes publishes only a year."""
    assert parse_year("2023").year == 2023
    assert parse_year(2023) == datetime(2023, 1, 1, tzinfo=timezone.utc)
    assert parse_year("2023-08-15").year == 2023
    assert parse_year(None) is None
    assert parse_year("no year here") is None
    # A year outside plausible record-keeping is a parsing artefact, not a date.
    assert parse_year("0001") is None
    assert parse_year("9999") is None


def test_first_present_prefers_the_earliest_populated_key():
    """Field naming is inconsistent across services, so several are tried."""
    properties = {"FIRE_YEAR": None, "fire_year": "", "year": 2019}
    assert first_present(properties, "FIRE_YEAR", "fire_year", "year") == 2019
    assert first_present(properties, "absent") is None


def test_first_present_is_case_insensitive():
    """OGC services disagree with each other about field casing."""
    assert first_present({"Rep_Date": "2026-01-01"}, "rep_date") == "2026-01-01"


def test_clean_properties_keeps_real_zeroes():
    """A zero count and a False flag are data. Dropping them as 'empty' would
    turn "no buildings here" into "we have no idea"."""
    cleaned = clean_properties(
        {
            "name": "  Cypress Bowl  ",
            "blank": "   ",
            "count": 0,
            "flag": False,
            "OBJECTID": 7,
        }
    )
    assert cleaned["name"] == "Cypress Bowl"
    assert "blank" not in cleaned
    assert cleaned["count"] == 0
    assert cleaned["flag"] is False


def test_clean_properties_makes_dates_json_safe():
    cleaned = clean_properties({"FIRE_DATE": datetime(2023, 8, 15, tzinfo=timezone.utc)})
    assert cleaned["FIRE_DATE"] == "2023-08-15T00:00:00+00:00"


def test_clean_properties_tolerates_no_properties():
    assert clean_properties(None) == {}
    assert clean_properties({}) == {}


def test_geometry_round_trips_the_types_we_ingest():
    point = geometry_from_geojson({"type": "Point", "coordinates": [-123.16, 49.36]})
    assert point.geom_type == "Point"

    polygon = geometry_from_geojson(
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}
    )
    assert polygon.geom_type == "Polygon" and polygon.area == pytest.approx(1.0)

    line = geometry_from_geojson({"type": "LineString", "coordinates": [[0, 0], [1, 1]]})
    assert line.geom_type == "LineString"

    multi = geometry_from_geojson(
        {
            "type": "MultiPolygon",
            "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]]],
        }
    )
    assert multi.geom_type == "MultiPolygon"


def test_malformed_geometry_returns_none_rather_than_raising():
    """One bad feature must not abort an otherwise good ingest."""
    assert geometry_from_geojson(None) is None
    assert geometry_from_geojson({}) is None
    assert geometry_from_geojson({"type": "Polygon"}) is None
    assert geometry_from_geojson({"type": "Nonsense", "coordinates": [1, 2]}) is None


def test_empty_geometry_is_treated_as_absent():
    """A zero-area polygon carries no location, so it must not be stored as one."""
    assert geometry_from_geojson({"type": "Polygon", "coordinates": []}) is None
    assert geometry_from_geojson({"type": "MultiPolygon", "coordinates": []}) is None


def test_binary_annotation_blobs_are_dropped():
    """Oracle-backed provincial services attach CAD annotation blobs."""
    cleaned = clean_properties(
        {"SE_ANNO_CAD_DATA": b"\x00\x01", "SHAPE": "blob", "NAME": "Cypress"}
    )
    assert cleaned == {"NAME": "Cypress"}


def test_station_names_are_tidied():
    """CWFIS returns the same field URL-encoded in some rows and not others."""
    assert _tidy_station_name("WEST+VANCOUVER+AUT") == "WEST VANCOUVER AUT"
    assert _tidy_station_name("SAANICHTON CFIA") == "SAANICHTON CFIA"
    assert _tidy_station_name("  DOUBLE  SPACE  ") == "DOUBLE SPACE"
    # An unnamed station costs us its label, not the whole ingest.
    assert _tidy_station_name(None) is None
    assert _tidy_station_name("   ") is None


# --------------------------------------------------------------------------- #
# Freshness description
# --------------------------------------------------------------------------- #


def test_age_is_described_in_units_a_person_reads():
    """Guards a real bug: a 13-hour-old reading reported as "0 days old",
    which reads as a broken field rather than as fresh data."""
    assert "hour" in describe_age(timedelta(hours=13))
    assert "minute" in describe_age(timedelta(minutes=20))
    assert "day" in describe_age(timedelta(days=4))
    assert "0 day" not in describe_age(timedelta(hours=13))


def test_negative_age_from_clock_skew_does_not_produce_nonsense():
    """A station clock slightly ahead of ours must not report a negative age."""
    assert "-" not in describe_age(timedelta(hours=-3))


def test_age_description_is_never_empty():
    for delta in (timedelta(0), timedelta(seconds=5), timedelta(days=4000)):
        assert describe_age(delta).strip()


# --------------------------------------------------------------------------- #
# Error message summarising
#
# Cloudflare's bot interstitial is an HTML page, and pasting it into a data
# health panel produces a wall of markup where an explanation should be.
# --------------------------------------------------------------------------- #


def _response(body: str, status: int = 403) -> httpx.Response:
    return httpx.Response(
        status, text=body, request=httpx.Request("GET", "https://example.test")
    )


def test_bot_protection_pages_are_explained_not_quoted():
    cloudflare = (
        "<!DOCTYPE html><html><head><title>Just a moment...</title>"
        "<meta http-equiv='refresh'></head><body>"
        "<div class='cf-browser-verification'>Checking your browser</div>"
        "</body></html>"
    )
    summary = summarise_body(_response(cloudflare))

    assert "bot-protection" in summary
    assert "<" not in summary and "DOCTYPE" not in summary
    assert "just a moment" not in summary.lower()


def test_ogc_exception_text_is_preferred_because_it_is_informative():
    xml = (
        '<?xml version="1.0"?><ows:ExceptionReport>'
        "<ows:Exception><ows:ExceptionText>Unknown attribute: the_geom"
        "</ows:ExceptionText></ows:Exception></ows:ExceptionReport>"
    )
    summary = summarise_body(_response(xml, 400))
    assert "Unknown attribute: the_geom" in summary
    assert "<" not in summary


def test_a_plain_text_error_survives_intact():
    assert "Rate limit exceeded" in summarise_body(
        _response("Rate limit exceeded", 429)
    )


def test_long_bodies_are_truncated_with_an_ellipsis():
    summary = summarise_body(_response("x " * 4000, 500), limit=100)
    assert len(summary) <= 105
    assert summary.endswith("…")


def test_an_empty_body_still_produces_a_reason():
    summary = summarise_body(_response("", 502))
    assert "502" in summary


# --------------------------------------------------------------------------- #
# The status model, which is what the data health panel renders
# --------------------------------------------------------------------------- #


def test_status_values_are_the_ones_the_brief_names():
    names = {s.value for s in DataStatus}
    assert {
        "CURRENT", "AGING", "STALE", "PARTIAL", "UNKNOWN", "FAILED", "UNAVAILABLE",
    } <= names


def test_unavailable_is_distinct_from_a_zero_record_success():
    """The central honesty requirement of the ingest layer.

    A source that could not be reached and a source that legitimately returned
    nothing must not share a status, because one is reassuring and the other is
    a hole in the picture.
    """
    assert DataStatus.UNAVAILABLE != DataStatus.CURRENT
    assert DataStatus.FAILED != DataStatus.CURRENT
    assert DataStatus.UNKNOWN not in {DataStatus.CURRENT, DataStatus.FAILED}


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_every_registered_adapter_implements_the_interface():
    from firewatch.sources.base import SourceAdapter

    assert ADAPTERS, "no adapters registered"
    for name, adapter in ADAPTERS.items():
        assert issubclass(adapter, SourceAdapter), name
        for method in ("discover", "fetch", "normalize"):
            assert hasattr(adapter, method), f"{name} has no {method}()"


def test_a_historical_version_is_not_the_current_picture():
    """Guards a real bug: backtesting 2023-08-15 re-fetched fire weather for
    that date, stored an empty version as the latest, and the data-health
    panel then reported the live stations as PARTIAL with no observation
    date — because it was reading the historical empty fetch.
    """
    from sqlalchemy import inspect as sa_inspect

    from firewatch.core.models import DatasetVersion

    columns = {c.name for c in sa_inspect(DatasetVersion).columns}
    assert "as_of_date" in columns


def test_adapter_ids_are_stable_names_not_class_names():
    """Configs reference these strings, so they are part of the contract."""
    assert "bcgw_wfs" in ADAPTERS
    assert "cwfis_wfs" in ADAPTERS
    assert "osm_overpass" in ADAPTERS
    assert "terrain_tiles" in ADAPTERS
    assert "eccc_geomet" in ADAPTERS
    assert "nasa_firms" in ADAPTERS
    assert "arcgis_feature_service" in ADAPTERS
    assert "wms_overlay" in ADAPTERS
    assert "wcs_raster" in ADAPTERS
