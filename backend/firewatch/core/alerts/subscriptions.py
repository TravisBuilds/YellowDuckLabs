"""Alert subscription storage."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from firewatch.core.models import AlertSubscription, Municipality
from firewatch.core.municipality import list_municipalities, load_municipality

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_email(email: str) -> str:
    normalized = normalize_email(email)
    if not _EMAIL_RE.match(normalized):
        raise ValueError("Enter a valid email address.")
    return normalized


def _new_token() -> str:
    return secrets.token_urlsafe(24)


@dataclass
class SubscriptionSyncResult:
    email: str
    municipality_ids: list[str]
    added: list[str]
    removed: list[str]


def list_subscriptions(session: Session, email: str) -> list[dict]:
    """Return active subscriptions for an email."""
    email = validate_email(email)
    rows = session.scalars(
        select(AlertSubscription)
        .where(AlertSubscription.email == email)
        .order_by(AlertSubscription.municipality_id)
    ).all()
    out = []
    for row in rows:
        municipality = session.get(Municipality, row.municipality_id)
        out.append(
            {
                "municipality_id": row.municipality_id,
                "short_name": municipality.short_name if municipality else row.municipality_id,
                "name": municipality.name if municipality else row.municipality_id,
                "subscribed_at": row.created_at.isoformat(),
            }
        )
    return out


def sync_subscriptions(
    session: Session, email: str, municipality_ids: list[str]
) -> SubscriptionSyncResult:
    """Replace this email's subscriptions with the requested municipality set."""
    email = validate_email(email)
    requested = sorted({m for m in municipality_ids if m})

    configured = set(list_municipalities())
    unknown = [m for m in requested if m not in configured]
    if unknown:
        raise ValueError(f"Unknown region(s): {', '.join(unknown)}")

    ingested = {
        m.id
        for m in session.scalars(select(Municipality)).all()
    }
    not_ready = [m for m in requested if m not in ingested]
    if not_ready:
        raise ValueError(
            f"Region(s) not ready for alerts yet: {', '.join(not_ready)}. "
            "Run ingest and score first."
        )

    existing = session.scalars(
        select(AlertSubscription).where(AlertSubscription.email == email)
    ).all()
    existing_by_municipality = {row.municipality_id: row for row in existing}

    added: list[str] = []
    removed: list[str] = []

    for municipality_id, row in existing_by_municipality.items():
        if municipality_id not in requested:
            session.delete(row)
            removed.append(municipality_id)

    for municipality_id in requested:
        if municipality_id not in existing_by_municipality:
            session.add(
                AlertSubscription(
                    email=email,
                    municipality_id=municipality_id,
                    unsubscribe_token=_new_token(),
                )
            )
            added.append(municipality_id)

    session.commit()
    return SubscriptionSyncResult(
        email=email,
        municipality_ids=requested,
        added=added,
        removed=removed,
    )


def unsubscribe_token(session: Session, token: str) -> dict | None:
    row = session.scalar(
        select(AlertSubscription).where(AlertSubscription.unsubscribe_token == token)
    )
    if row is None:
        return None
    municipality = session.get(Municipality, row.municipality_id)
    payload = {
        "email": row.email,
        "municipality_id": row.municipality_id,
        "short_name": municipality.short_name if municipality else row.municipality_id,
    }
    session.delete(row)
    session.commit()
    return payload


def subscribers_for_municipality(session: Session, municipality_id: str) -> list[AlertSubscription]:
    return list(
        session.scalars(
            select(AlertSubscription)
            .where(AlertSubscription.municipality_id == municipality_id)
            .order_by(AlertSubscription.email)
        ).all()
    )


def alertable_regions(session: Session) -> list[dict]:
    """Regions a user can subscribe to (configured and ingested)."""
    ingested = {m.id: m for m in session.scalars(select(Municipality)).all()}
    regions = []
    for municipality_id in sorted(list_municipalities()):
        config = load_municipality(municipality_id)
        municipality = ingested.get(municipality_id)
        regions.append(
            {
                "id": config.id,
                "name": config.name,
                "short_name": config.short_name,
                "province": config.province,
                "ingested": municipality is not None,
            }
        )
    return regions
