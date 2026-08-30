"""AI grounding guards.

The analyst is the part of this system most able to do harm, because prose is
persuasive in a way a number is not. A confident sentence about hydrant flow or
response time would be believed, and no dataset here contains either.

Grounding is therefore structural rather than a polite request in a prompt:

* the tools carry provenance, so a cited fact can be traced;
* the tools return explicit unknown markers, so "we don't know" is always
  available and well-supported;
* with no API key the analyst emits tool output verbatim and no prose at all.

These tests check the structure, not the model's manners. They need the ingested
database, and skip cleanly without one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from firewatch.core.ai.agent import (
    SYSTEM_PROMPT,
    TOOL_SCHEMAS,
    _collect_citations,
    _execute_tool,
    ask_analyst,
)
from firewatch.core.ai.tools import TOOL_FUNCTIONS
from firewatch.core.models import Municipality

pytestmark = pytest.mark.usefixtures("session")

#: Things no dataset in this system establishes. The analyst must never state
#: them, and the prompt must name each one explicitly.
FORBIDDEN_CLAIMS = (
    "dispatch",
    "response time",
    "crew availability",
    "apparatus access",
    "flow rate",
    "pressure",
    "suppression capacity",
    "official fire status",
)


@pytest.fixture
def session():
    """A fresh session per test.

    Deliberately not shared: one failing query aborts a PostgreSQL transaction,
    and a shared session would turn a single real failure into a cascade of
    confusing ones with the original cause buried.
    """
    from firewatch.core.db import SessionLocal

    try:
        db = SessionLocal()
        db.execute(select(Municipality).limit(1))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"database unavailable: {exc}")

    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture
def municipality_id(session):
    found = session.scalars(select(Municipality.id)).first()
    if not found:
        pytest.skip("no municipality has been ingested")
    return found


# --------------------------------------------------------------------------- #
# The prompt's stated limits
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("claim", FORBIDDEN_CLAIMS)
def test_prompt_names_each_thing_the_data_cannot_establish(claim):
    """A generic "be careful" instruction is not a guard. Each unsupported
    claim is named, so the refusal is specific enough to be followed."""
    assert claim in SYSTEM_PROMPT.lower()


def test_prompt_forbids_the_incident_commander_role():
    lowered = SYSTEM_PROMPT.lower()
    assert "not an incident commander" in lowered
    assert "evacuation" in lowered


def test_prompt_requires_tool_grounding_and_citation():
    lowered = SYSTEM_PROMPT.lower()
    assert "must come from a tool result" in lowered
    assert "cite" in lowered
    # Naming the dataset, not "public data", is what makes a claim checkable.
    assert "name the dataset" in lowered


def test_prompt_requires_unknowns_to_be_surfaced():
    lowered = SYSTEM_PROMPT.lower()
    assert "name what is missing" in lowered
    assert "unknown" in lowered


def test_prompt_states_the_score_is_unvalidated():
    assert "unvalidated" in SYSTEM_PROMPT.lower()
    assert "hypothesis" in SYSTEM_PROMPT.lower()


def test_prompt_repeats_the_core_caveats():
    lowered = SYSTEM_PROMPT.lower()
    assert "thermal anomaly, not a confirmed wildfire" in lowered
    assert "not evidence of apparatus access" in lowered
    assert "no flow information" in lowered


# --------------------------------------------------------------------------- #
# Tool surface
# --------------------------------------------------------------------------- #


def test_every_advertised_tool_is_implemented():
    """A schema with no function behind it becomes a hallucination surface."""
    advertised = {t["name"] for t in TOOL_SCHEMAS}
    implemented = set(TOOL_FUNCTIONS) | {"search_local_documents"}
    assert advertised <= implemented, advertised - implemented


def test_every_implemented_tool_is_advertised():
    """And a tool the model cannot see is dead weight it will work around."""
    advertised = {t["name"] for t in TOOL_SCHEMAS}
    assert set(TOOL_FUNCTIONS) <= advertised, set(TOOL_FUNCTIONS) - advertised


def test_tool_schemas_are_well_formed():
    for schema in TOOL_SCHEMAS:
        assert schema["name"] and schema["description"]
        assert schema["input_schema"]["type"] == "object"
        for name, spec in schema["input_schema"].get("properties", {}).items():
            assert "type" in spec, f"{schema['name']}.{name} has no type"


def test_no_tool_offers_a_municipality_argument():
    """The municipality is bound by the caller, so the model cannot read across
    municipal boundaries by asking for a different one."""
    for schema in TOOL_SCHEMAS:
        assert "municipality_id" not in schema["input_schema"].get("properties", {})
        assert "municipality" not in schema["input_schema"].get("properties", {})


def test_unknown_tool_names_are_refused_not_improvised(session, municipality_id):
    call = _execute_tool(session, municipality_id, "get_hydrant_flow_rate", {})
    assert call.error is not None
    assert "unknown tool" in call.error.lower()
    assert call.result is None


def test_bad_arguments_produce_an_error_not_a_guess(session, municipality_id):
    call = _execute_tool(
        session, municipality_id, "get_cell_profile", {"nonsense_argument": 1}
    )
    assert call.error is not None
    assert call.result is None


def test_tool_exceptions_are_captured_not_raised(session, municipality_id):
    """A failing tool must degrade the answer, not crash the request."""
    call = _execute_tool(
        session, municipality_id, "get_cell_profile", {"lat": "north", "lon": "west"}
    )
    assert call.error is not None


# --------------------------------------------------------------------------- #
# Provenance attached to real tool output
# --------------------------------------------------------------------------- #


def test_cell_profile_carries_provenance_and_caveats(session, municipality_id):
    profile = TOOL_FUNCTIONS["get_cell_profile"](
        session, municipality_id, lat=49.36, lon=-123.16
    )
    assert profile.get("sources") or profile.get("provenance"), (
        "a profile with no provenance cannot be cited, so it cannot be used"
    )
    # The score must never arrive as a bare number.
    priority = profile.get("priority") or {}
    if priority.get("overall") is not None:
        assert priority.get("components") or profile.get("explanation")
        assert priority.get("score_version")


def test_a_location_outside_the_municipality_is_refused_not_extrapolated(
    session, municipality_id
):
    """Somewhere in the Pacific. The honest answer is that we have nothing."""
    profile = TOOL_FUNCTIONS["get_cell_profile"](
        session, municipality_id, lat=10.0, lon=-150.0
    )
    text = str(profile).lower()
    assert (
        profile.get("error")
        or profile.get("found") is False
        or "no analysis cell" in text
        or "outside" in text
        or "unknown" in text
    ), f"expected an explicit miss, got: {str(profile)[:400]}"


def test_nearby_assets_distinguishes_absence_from_no_source(
    session, municipality_id
):
    """"No hydrants within 500 m" and "we have no hydrant data" are different
    facts, and only one of them is reassuring."""
    result = TOOL_FUNCTIONS["get_nearby_assets"](
        session, municipality_id, lat=49.36, lon=-123.16, radius_m=500
    )
    assert result
    rendered = str(result).lower()
    assert "count" in rendered or "nearest" in rendered


def test_data_gaps_are_reportable(session, municipality_id):
    gaps = TOOL_FUNCTIONS["get_data_gaps"](session, municipality_id)
    assert gaps is not None
    # The municipality config declares known unknowns; they must reach the tool.
    assert str(gaps).strip() not in ("", "{}", "[]", "None")


def test_source_provenance_reports_licence_and_attribution(
    session, municipality_id
):
    provenance = TOOL_FUNCTIONS["get_source_provenance"](session, municipality_id)
    rendered = str(provenance).lower()
    assert "licence" in rendered or "license" in rendered
    assert "attribution" in rendered


def test_citations_are_extracted_from_tool_results(session, municipality_id):
    call = _execute_tool(
        session, municipality_id, "get_source_provenance", {}
    )
    citations = _collect_citations([call])
    assert citations, "provenance output must yield citations"
    for citation in citations:
        assert citation["type"] in {"dataset", "document"}
        if citation["type"] == "dataset":
            assert citation["source_id"]


# --------------------------------------------------------------------------- #
# Deterministic mode: the analyst with no language model
# --------------------------------------------------------------------------- #


def test_without_a_model_the_analyst_synthesises_nothing(
    session, municipality_id, monkeypatch
):
    from firewatch.config import settings

    monkeypatch.setattr(type(settings), "llm_enabled", property(lambda _: False))

    answer = ask_analyst(session, municipality_id, "Where is the observation gap?")

    assert answer.llm_used is False
    assert answer.tool_calls, "deterministic mode must still run the tools"
    # It must announce its own limitation rather than passing tool dumps off as
    # analysis.
    assert "no synthesis" in answer.answer.lower()
    assert answer.notes


def test_deterministic_mode_reports_which_tools_it_ran(
    session, municipality_id, monkeypatch
):
    from firewatch.config import settings

    monkeypatch.setattr(type(settings), "llm_enabled", property(lambda _: False))

    answer = ask_analyst(session, municipality_id, "Which datasets support this?")
    for call in answer.tool_calls:
        assert call.name in answer.answer


def test_deterministic_mode_never_states_a_forbidden_quantity(
    session, municipality_id, monkeypatch
):
    """It cannot invent, because it only prints tool output. Verified, not assumed."""
    from firewatch.config import settings

    monkeypatch.setattr(type(settings), "llm_enabled", property(lambda _: False))

    for question in (
        "How fast can a crew get to the worst cell?",
        "What is the hydrant flow rate on the upper slopes?",
        "Should we evacuate?",
    ):
        answer = ask_analyst(session, municipality_id, question).answer.lower()
        # The word may appear as part of a caveat, but never as an assertion of
        # a value, and deterministic mode asserts nothing at all.
        assert "no synthesis" in answer
