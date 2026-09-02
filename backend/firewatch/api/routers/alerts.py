"""Email alert subscriptions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from firewatch.core.alerts.subscriptions import (
    alertable_regions,
    list_subscriptions,
    sync_subscriptions,
    unsubscribe_token,
    validate_email,
)
from firewatch.core.db import get_session
from firewatch.config import settings

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class LookupRequest(BaseModel):
    email: str


class SyncRequest(BaseModel):
    email: str
    municipality_ids: list[str] = Field(default_factory=list)


@router.get("/regions")
def regions(session: Session = Depends(get_session)) -> dict:
    """Regions available for alert subscription."""
    return {"regions": alertable_regions(session)}


@router.get("/status")
def status() -> dict:
    return {
        "email_enabled": settings.email_enabled,
        "public_web_url": settings.public_web_url,
    }


@router.post("/lookup")
def lookup(body: LookupRequest, session: Session = Depends(get_session)) -> dict:
    try:
        email = validate_email(body.email)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return {"email": email, "subscriptions": list_subscriptions(session, email)}


@router.put("/subscriptions")
def update_subscriptions(body: SyncRequest, session: Session = Depends(get_session)) -> dict:
    try:
        result = sync_subscriptions(session, body.email, body.municipality_ids)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return {
        "email": result.email,
        "municipality_ids": result.municipality_ids,
        "added": result.added,
        "removed": result.removed,
        "subscriptions": list_subscriptions(session, result.email),
    }


@router.delete("/unsubscribe/{token}")
def unsubscribe(token: str, session: Session = Depends(get_session)) -> dict:
    payload = unsubscribe_token(session, token)
    if payload is None:
        raise HTTPException(404, detail="This unsubscribe link is invalid or already used.")
    return {"status": "unsubscribed", **payload}
