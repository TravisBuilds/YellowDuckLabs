"""AI analyst endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from firewatch.config import settings
from firewatch.core.ai.agent import TOOL_SCHEMAS, ask_analyst
from firewatch.core.ai.documents import search_documents
from firewatch.core.db import get_session
from firewatch.core.models import Document, Municipality

router = APIRouter(prefix="/api/municipalities", tags=["ai"])

#: The questions from the brief's section 1.5, offered as starting points. The
#: last one names the local fire service, so it is built from the municipality
#: rather than written in: suggesting West Vancouver Fire & Rescue to a Kelowna
#: user would be a small tell that the product is really only built for one place.
SUGGESTED_QUESTIONS = [
    "Why is this area high priority today?",
    "Which neighbourhoods combine steep terrain, dense vegetation, and high structural exposure?",
    "Where are we least certain about response accessibility?",
    "Show me locations with high consequence but little historical fire activity.",
    "What changed in this area over the last 48 hours?",
    "Which public datasets support this conclusion?",
]


def suggested_questions(municipality: Municipality) -> list[str]:
    name = municipality.short_name or municipality.name
    return [
        *SUGGESTED_QUESTIONS,
        f"What do we still need the {name} fire service to validate?",
    ]


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    #: What the user is looking at, e.g. {"lat": .., "lon": .., "date": ".."}.
    context: dict | None = None


@router.post("/{municipality_id}/ask")
def ask(
    municipality_id: str,
    request: AskRequest,
    session: Session = Depends(get_session),
) -> dict:
    if session.get(Municipality, municipality_id) is None:
        raise HTTPException(404, detail=f"Municipality '{municipality_id}' not ingested.")

    answer = ask_analyst(
        session, municipality_id, request.question, context=request.context
    )
    return answer.as_dict()


@router.get("/{municipality_id}/ai/status")
def ai_status(municipality_id: str, session: Session = Depends(get_session)) -> dict:
    """Whether a model is configured, and which documents are quotable."""
    municipality = session.get(Municipality, municipality_id)
    if municipality is None:
        raise HTTPException(404, detail=f"Municipality '{municipality_id}' not ingested.")

    documents = session.scalars(
        select(Document).where(Document.municipality_id == municipality_id)
    ).all()
    return {
        "llm_enabled": settings.llm_enabled,
        "model": settings.firewatch_llm_model if settings.llm_enabled else None,
        "mode": "analysis" if settings.llm_enabled else "deterministic_tool_output",
        "mode_explanation": (
            "A language model synthesises answers from structured tool results."
            if settings.llm_enabled
            else (
                "No API key is configured. The analyst still executes the same "
                "structured tools and returns their output verbatim, with no "
                "generated prose. No facts differ between modes."
            )
        ),
        "tools": [
            {"name": t["name"], "description": t["description"]} for t in TOOL_SCHEMAS
        ],
        "documents": [
            {
                "document_id": d.id,
                "title": d.title,
                "status": d.status,
                "message": d.message,
                "source_url": d.source_url,
                "quotable": d.status == "ingested",
            }
            for d in documents
        ],
        "suggested_questions": suggested_questions(municipality),
        "guardrails": [
            "Map facts must come from structured tools, never from model memory.",
            "Dispatch time, crew availability, hydrant flow, apparatus access and "
            "official fire status are never stated: no dataset here contains them.",
            "The analyst returns 'unknown' rather than a plausible substitute.",
            "The analyst does not make emergency-response decisions.",
        ],
    }


@router.get("/{municipality_id}/documents/search")
def documents_search(
    municipality_id: str,
    q: str,
    limit: int = 5,
    session: Session = Depends(get_session),
) -> dict:
    return search_documents(session, municipality_id, q, limit=limit)
