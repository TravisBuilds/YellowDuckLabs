"""Run alert detection and notify subscribers."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from firewatch.core.alerts.detect import (
    already_dispatched,
    count_newly_high_cells,
    newly_high_cells,
)
from firewatch.core.alerts.notify import build_alert_email, send_email
from firewatch.core.alerts.subscriptions import subscribers_for_municipality
from firewatch.core.models import AlertDispatch, Municipality
from firewatch.core.scoring.priority import SCORE_VERSION

log = logging.getLogger(__name__)


def process_municipality_alerts(
    session: Session,
    municipality_id: str,
    as_of_date: str,
    score_version: str = SCORE_VERSION,
) -> dict:
    """Notify subscribers when new cells cross into High priority."""
    if already_dispatched(session, municipality_id, as_of_date, score_version):
        return {
            "municipality_id": municipality_id,
            "as_of_date": as_of_date,
            "status": "skipped",
            "reason": "already_dispatched",
        }

    new_high_count = count_newly_high_cells(
        session, municipality_id, as_of_date, score_version
    )
    if new_high_count == 0:
        return {
            "municipality_id": municipality_id,
            "as_of_date": as_of_date,
            "status": "skipped",
            "reason": "no_new_high_cells",
            "new_high_cells": 0,
        }

    subscribers = subscribers_for_municipality(session, municipality_id)
    if not subscribers:
        return {
            "municipality_id": municipality_id,
            "as_of_date": as_of_date,
            "status": "skipped",
            "reason": "no_subscribers",
            "new_high_cells": new_high_count,
        }

    municipality = session.get(Municipality, municipality_id)
    municipality_name = municipality.name if municipality else municipality_id
    sample_cells = newly_high_cells(
        session, municipality_id, as_of_date, score_version, limit=8
    )

    sent = 0
    for subscription in subscribers:
        message = build_alert_email(
            municipality_name=municipality_name,
            as_of_date=as_of_date,
            new_high_count=new_high_count,
            sample_cells=sample_cells,
            subscription=subscription,
        )
        try:
            send_email(message)
            sent += 1
        except Exception:
            log.exception(
                "Failed to send alert to %s for %s", subscription.email, municipality_id
            )

    dispatch = AlertDispatch(
        municipality_id=municipality_id,
        as_of_date=as_of_date,
        score_version=score_version,
        new_high_cells=new_high_count,
        recipients=sent,
        sent_at=datetime.now(timezone.utc),
        summary={
            "sample": [
                {
                    "h3": cell.h3_index,
                    "lat": cell.lat,
                    "lon": cell.lon,
                    "band": cell.band,
                    "priority": cell.overall_priority,
                }
                for cell in sample_cells
            ]
        },
    )
    session.add(dispatch)
    session.commit()

    log.info(
        "Alert dispatch for %s on %s: %d new high cells, %d/%d emails sent",
        municipality_id,
        as_of_date,
        new_high_count,
        sent,
        len(subscribers),
    )

    return {
        "municipality_id": municipality_id,
        "as_of_date": as_of_date,
        "status": "sent",
        "new_high_cells": new_high_count,
        "subscribers": len(subscribers),
        "emails_sent": sent,
    }
