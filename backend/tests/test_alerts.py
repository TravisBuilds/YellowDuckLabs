"""Tests for email alert subscriptions."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from firewatch.core.alerts.subscriptions import (
    list_subscriptions,
    sync_subscriptions,
    unsubscribe_token,
    validate_email,
)
from firewatch.core.models import AlertSubscription, Municipality


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Municipality.__table__.create(engine)
    AlertSubscription.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    db = factory()
    db.add(
        Municipality(
            id="west-vancouver",
            name="District of West Vancouver",
            short_name="West Vancouver",
            province="BC",
            country="CA",
            timezone="America/Vancouver",
            h3_resolution=10,
            metric_crs="EPSG:32610",
            boundary_buffer_m=2000.0,
        )
    )
    db.commit()
    yield db
    db.close()


def test_validate_email_normalizes():
    assert validate_email("  Chief@Example.ORG ") == "chief@example.org"


def test_validate_email_rejects_invalid():
    with pytest.raises(ValueError):
        validate_email("not-an-email")


def test_sync_subscriptions_add_and_remove(session: Session, monkeypatch):
    monkeypatch.setattr(
        "firewatch.core.alerts.subscriptions.list_municipalities",
        lambda: ["west-vancouver"],
    )

    result = sync_subscriptions(session, "chief@example.org", ["west-vancouver"])
    assert result.added == ["west-vancouver"]
    assert len(list_subscriptions(session, "chief@example.org")) == 1

    result = sync_subscriptions(session, "chief@example.org", [])
    assert result.removed == ["west-vancouver"]
    assert list_subscriptions(session, "chief@example.org") == []


def test_unsubscribe_token(session: Session, monkeypatch):
    monkeypatch.setattr(
        "firewatch.core.alerts.subscriptions.list_municipalities",
        lambda: ["west-vancouver"],
    )
    sync_subscriptions(session, "chief@example.org", ["west-vancouver"])
    token = session.scalars(
        __import__("sqlalchemy").select(AlertSubscription.unsubscribe_token)
    ).first()
    payload = unsubscribe_token(session, token)
    assert payload is not None
    assert payload["email"] == "chief@example.org"
    assert payload["municipality_id"] == "west-vancouver"
