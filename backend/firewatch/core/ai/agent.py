"""The Fire Watch AI analyst.

The model is an analyst and copilot. It is not an incident commander, and it has
no authority to make or imply an emergency-response decision.

Grounding is enforced structurally rather than by asking politely:

* every map fact must come from a tool result in this conversation;
* the tools themselves attach provenance, dates and caveats to every value;
* the tools return explicit ``unknown`` markers, so "we don't know" is always an
  available, well-supported answer;
* without an API key the analyst still runs, executing the same tools and
  returning their output verbatim with no natural-language synthesis at all.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from firewatch.config import settings
from firewatch.core.ai.documents import search_documents
from firewatch.core.ai.tools import TOOL_FUNCTIONS

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6

SYSTEM_PROMPT = """\
You are the Fire Watch analyst for Yellow Duck Labs, working with a municipal \
wildfire operating picture.

Your role is analyst and copilot. You are NOT an incident commander. You never \
make, recommend, or imply an emergency-response decision, an evacuation, a \
dispatch, or a suppression action.

HOW YOU MUST ANSWER

1. Every factual claim about the map, the terrain, the weather, the assets or \
the scores MUST come from a tool result in this conversation. If you have not \
called a tool for it, you do not know it.
2. Cite the source datasets and observation dates that the tool results give \
you. Name the dataset, not just "public data".
3. Distinguish observation from inference. Say which is which. A station reading \
applied to a cell is an approximation, and you must say so.
4. Name what is missing. The tools return "unknown" markers and data-gap \
records; surface them rather than skipping past them.
5. Give a confidence level, and explain what limits it.
6. NEVER state a dispatch time, response time, crew availability, apparatus \
access, hydrant flow rate, water pressure, suppression capacity, or official \
fire status. These are not in any dataset you can reach. If asked, say plainly \
that the data do not establish it and name who could confirm it.
7. If a tool returns no data, say so. Do not fill the gap with a plausible \
number, a typical value, or a general statement about wildfire behaviour \
presented as a local fact.

A satellite hotspot is a thermal anomaly, not a confirmed wildfire. A mapped \
road is not evidence of apparatus access. A mapped hydrant carries no flow \
information. The priority score is an unvalidated working hypothesis, not \
scientific truth.

Prefer being useful and specific about what the data DO show over hedging \
everything. Then be exact about the boundary of what they show.

When you report a priority score, include its component breakdown and the \
score version. Never give a single risk number with no explanation.
"""


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_cell_profile",
        "description": (
            "Full evidence profile for one location: priority score with component "
            "breakdown, what is nearby to preserve, terrain and fire-weather threat, "
            "existing defenses, observation status, recorded unknowns, and dataset "
            "provenance. Use this for any question about a specific place."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "date": {
                    "type": "string",
                    "description": "ISO date (YYYY-MM-DD). Omit for the latest scored date.",
                },
            },
            "required": ["lat", "lon"],
        },
    },
    {
        "name": "rank_cells",
        "description": (
            "Rank analysis cells by a score component, with optional thresholds on "
            "other components and on derived metrics. Use for questions like which "
            "neighbourhoods combine steep terrain with high structural exposure, or "
            "where we are least certain."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_by": {
                    "type": "string",
                    "enum": [
                        "overall_priority", "ignition_likelihood", "spread_potential",
                        "consequence_exposure", "observation_gap",
                        "access_difficulty_proxy", "hazard", "exposure",
                        "current_conditions", "operational_gap", "confidence",
                    ],
                },
                "date": {"type": "string"},
                "limit": {"type": "integer"},
                "min_filters": {
                    "type": "object",
                    "description": "Component name to minimum value, e.g. {\"consequence_exposure\": 0.6}",
                    "additionalProperties": {"type": "number"},
                },
                "max_filters": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                },
                "metric_min": {
                    "type": "object",
                    "description": "Derived metric to minimum value, e.g. {\"slope_deg\": 25}",
                    "additionalProperties": {"type": "number"},
                },
                "metric_max": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                },
            },
        },
    },
    {
        "name": "get_nearby_assets",
        "description": (
            "Count and nearest distance for real mapped features around a point: "
            "buildings, roads, water assets, fire stations, parks, vegetation. "
            "Distinguishes 'none within radius' from 'no source for this at all'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "radius_m": {"type": "number"},
                "kinds": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["lat", "lon"],
        },
    },
    {
        "name": "get_recent_hotspots",
        "description": (
            "Satellite thermal-anomaly detections in a time window, grouped by "
            "satellite platform and source."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "window_days": {"type": "integer"},
                "date": {"type": "string"},
                "bounds": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "[west, south, east, north]",
                },
            },
        },
    },
    {
        "name": "get_weather_profile",
        "description": (
            "Nearest fire-weather station reading (FFMC, DMC, DC, ISI, BUI, FWI) and "
            "nearest ECCC station weather for a location, with station distance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "date": {"type": "string"},
            },
            "required": ["lat", "lon"],
        },
    },
    {
        "name": "compare_dates",
        "description": (
            "Compare priority and components between two dates, either for one cell "
            "(by h3_index) or the largest changes across the municipality. Use for "
            "'what changed' questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_a": {"type": "string"},
                "date_b": {"type": "string"},
                "h3_index": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["date_a", "date_b"],
        },
    },
    {
        "name": "get_source_provenance",
        "description": (
            "Licence, attribution, version, status, record count and freshness for "
            "one dataset or all of them. Use when asked which datasets support a "
            "conclusion."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"source_id": {"type": "string"}},
        },
    },
    {
        "name": "get_data_gaps",
        "description": (
            "Recorded data gaps and operational unknowns, optionally for one cell. "
            "Use when asked what needs validation or what we do not know."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "h3_index": {"type": "string"},
                "gap_type": {
                    "type": "string",
                    "enum": [
                        "operational_unknown", "missing_metric",
                        "source_unavailable", "derivation_note",
                    ],
                },
            },
        },
    },
    {
        "name": "search_local_documents",
        "description": (
            "Search ingested local wildfire documents, such as the municipality's "
            "Community Wildfire Resiliency Plan. Returns passages with document "
            "title and page. Use when asked what the municipality's own plan says."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
]


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    result: Any = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "tool": self.name,
            "arguments": self.arguments,
            "error": self.error,
            "result": self.result,
        }


@dataclass
class AnalystAnswer:
    answer: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    llm_used: bool = False
    model: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "answer": self.answer,
            "llm_used": self.llm_used,
            "model": self.model,
            "tool_calls": [t.as_dict() for t in self.tool_calls],
            "citations": self.citations,
            "notes": self.notes,
        }


def _execute_tool(
    session: Session, municipality_id: str, name: str, arguments: dict
) -> ToolCall:
    call = ToolCall(name=name, arguments=arguments)
    try:
        if name == "search_local_documents":
            call.result = search_documents(
                session,
                municipality_id,
                str(arguments.get("query", "")),
                limit=int(arguments.get("limit", 5)),
            )
        else:
            function = TOOL_FUNCTIONS.get(name)
            if function is None:
                call.error = f"Unknown tool '{name}'"
                return call
            call.result = function(session, municipality_id, **arguments)
    except TypeError as exc:
        call.error = f"Invalid arguments for {name}: {exc}"
    except Exception as exc:
        log.exception("tool %s failed", name)
        call.error = f"{type(exc).__name__}: {exc}"
    return call


def _collect_citations(tool_calls: list[ToolCall]) -> list[dict]:
    """Pull dataset and document citations out of tool results."""
    citations: dict[str, dict] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "source_id" in node and ("licence" in node or "attribution" in node):
                key = str(node["source_id"])
                citations.setdefault(
                    key,
                    {
                        "type": "dataset",
                        "source_id": key,
                        "title": node.get("title"),
                        "licence": node.get("licence"),
                        "attribution": node.get("attribution"),
                        "observed_at": node.get("observed_at")
                        or node.get("latest_observed_at"),
                        "dataset_version": node.get("dataset_version"),
                        "source_url": node.get("source_url"),
                    },
                )
            if "document_id" in node:
                key = f"doc:{node['document_id']}:{node.get('page')}"
                citations.setdefault(
                    key,
                    {
                        "type": "document",
                        "document_id": node.get("document_id"),
                        "title": node.get("title"),
                        "page": node.get("page"),
                        "section": node.get("section"),
                        "source_url": node.get("source_url"),
                    },
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for call in tool_calls:
        walk(call.result)
    return list(citations.values())


# --- deterministic mode (no API key) --------------------------------------

_COORD_RE = re.compile(r"(-?\d{1,3}\.\d{3,})[,\s]+(-?\d{1,3}\.\d{3,})")

_INTENT_RULES: list[tuple[re.Pattern, str, dict]] = [
    (re.compile(r"\b(gap|unknown|validate|don'?t know|uncertain)\b", re.I),
     "get_data_gaps", {}),
    (re.compile(r"\b(source|dataset|licence|license|provenance|support)\b", re.I),
     "get_source_provenance", {}),
    (re.compile(r"\b(hotspot|detection|satellite|firms)\b", re.I),
     "get_recent_hotspots", {"window_days": 7}),
    (re.compile(r"\b(weather|wind|humidity|fwi|fire weather|temperature)\b", re.I),
     "get_weather_profile", {}),
    (re.compile(r"\b(steep|slope|terrain|vegetation|exposure|dense|combine)\b", re.I),
     "rank_cells", {"order_by": "overall_priority", "limit": 10}),
    (re.compile(r"\b(chang|last 48|yesterday|compare)\b", re.I),
     "compare_dates", {}),
]


def _deterministic_answer(
    session: Session, municipality_id: str, question: str
) -> AnalystAnswer:
    """Run tools and return their output without synthesis.

    This mode exists so the product is honest when no language model is
    configured: it shows exactly what the data say and nothing more.
    """
    calls: list[ToolCall] = []
    coords = _COORD_RE.search(question)

    if coords:
        lat, lon = float(coords.group(1)), float(coords.group(2))
        calls.append(_execute_tool(session, municipality_id, "get_cell_profile",
                                   {"lat": lat, "lon": lon}))

    matched = False
    for pattern, tool, arguments in _INTENT_RULES:
        if pattern.search(question):
            args = dict(arguments)
            if tool in {"get_weather_profile"} and coords:
                args.update({"lat": float(coords.group(1)), "lon": float(coords.group(2))})
            elif tool == "get_weather_profile":
                continue
            if tool == "compare_dates":
                continue
            calls.append(_execute_tool(session, municipality_id, tool, args))
            matched = True
            break

    if not calls or (not matched and not coords):
        calls.append(
            _execute_tool(
                session, municipality_id, "rank_cells",
                {"order_by": "overall_priority", "limit": 10},
            )
        )

    lines = [
        "No language model is configured, so this answer contains no synthesis: "
        "it is the raw output of the structured tools, which is the only "
        "information the analyst actually has.",
        "",
        f"Question: {question}",
        "",
    ]
    for call in calls:
        lines.append(f"### {call.name}({json.dumps(call.arguments)})")
        if call.error:
            lines.append(f"ERROR: {call.error}")
        else:
            lines.append("```json")
            lines.append(json.dumps(call.result, indent=2, default=str)[:6000])
            lines.append("```")
        lines.append("")

    lines.append(
        "Set ANTHROPIC_API_KEY to enable natural-language analysis over these same "
        "tool results. The tools, and therefore the facts available, are identical "
        "in both modes."
    )

    return AnalystAnswer(
        answer="\n".join(lines),
        tool_calls=calls,
        citations=_collect_citations(calls),
        llm_used=False,
        notes=["Deterministic mode: tool output only, no generated prose."],
    )


# --- LLM mode -----------------------------------------------------------


def ask_analyst(
    session: Session,
    municipality_id: str,
    question: str,
    context: dict | None = None,
) -> AnalystAnswer:
    """Answer a question using structured tools, and a language model if available."""
    if not settings.llm_enabled:
        return _deterministic_answer(session, municipality_id, question)

    try:
        from anthropic import Anthropic
    except ImportError:
        return _deterministic_answer(session, municipality_id, question)

    client = Anthropic(api_key=settings.anthropic_api_key)
    municipality_note = (
        f"You are working on municipality '{municipality_id}'. "
        "All tool calls apply to it automatically; do not pass a municipality argument."
    )
    if context:
        municipality_note += (
            f"\nThe user is currently looking at: {json.dumps(context, default=str)}"
        )

    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    all_calls: list[ToolCall] = []

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=settings.firewatch_llm_model,
            max_tokens=4096,
            system=f"{SYSTEM_PROMPT}\n\n{municipality_note}",
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

        tool_uses = [b for b in response.content if getattr(b, "type", "") == "tool_use"]
        if not tool_uses:
            text = "\n".join(
                b.text for b in response.content if getattr(b, "type", "") == "text"
            )
            return AnalystAnswer(
                answer=text.strip(),
                tool_calls=all_calls,
                citations=_collect_citations(all_calls),
                llm_used=True,
                model=settings.firewatch_llm_model,
            )

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for use in tool_uses:
            call = _execute_tool(session, municipality_id, use.name, dict(use.input))
            all_calls.append(call)
            payload = (
                {"error": call.error} if call.error else call.result
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": json.dumps(payload, default=str)[:100000],
                    "is_error": bool(call.error),
                }
            )
        messages.append({"role": "user", "content": results})

    return AnalystAnswer(
        answer=(
            "I reached the tool-call limit before completing this analysis. The "
            "tool results gathered so far are attached; I am not going to "
            "speculate beyond them."
        ),
        tool_calls=all_calls,
        citations=_collect_citations(all_calls),
        llm_used=True,
        model=settings.firewatch_llm_model,
        notes=[f"Stopped after {MAX_TOOL_ROUNDS} tool rounds."],
    )
