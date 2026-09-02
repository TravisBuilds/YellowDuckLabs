"""Send priority alert emails."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from firewatch.config import settings
from firewatch.core.alerts.detect import NewHighCell
from firewatch.core.models import AlertSubscription

log = logging.getLogger(__name__)


def _firewatch_url(path: str) -> str:
    base = settings.public_web_url.rstrip("/")
    return f"{base}{path}"


def build_alert_email(
    *,
    municipality_name: str,
    as_of_date: str,
    new_high_count: int,
    sample_cells: list[NewHighCell],
    subscription: AlertSubscription,
) -> EmailMessage:
    unsubscribe_url = _firewatch_url(
        f"/firewatch/alerts/unsubscribe?token={subscription.unsubscribe_token}"
    )
    map_url = _firewatch_url("/firewatch")

    lines = [
        f"Fire Watch alert — {municipality_name}",
        "",
        f"As of {as_of_date}, {new_high_count} analysis cell"
        f"{'s' if new_high_count != 1 else ''} in {municipality_name} "
        "newly reached High or Very high priority.",
        "",
        "This means terrain, fuels, exposure, or current fire weather crossed "
        "the threshold since the last score. Open the map to inspect:",
        map_url,
        "",
    ]

    if sample_cells:
        lines.append("Highest-priority new locations:")
        for cell in sample_cells[:8]:
            lines.append(
                f"  • {cell.band} ({cell.overall_priority:.2f}) — "
                f"{cell.lat:.4f}, {cell.lon:.4f}"
            )
        if new_high_count > len(sample_cells):
            lines.append(f"  … and {new_high_count - len(sample_cells)} more")
        lines.append("")

    lines.extend(
        [
            "Scores are model outputs, not confirmed incidents. Verify on the ground.",
            "",
            f"Manage alerts: {_firewatch_url('/firewatch')}",
            f"Unsubscribe from {municipality_name}: {unsubscribe_url}",
            "",
            "— Yellow Duck Labs · Fire Watch",
        ]
    )

    message = EmailMessage()
    message["Subject"] = (
        f"Fire Watch: {municipality_name} — {new_high_count} new high-priority "
        f"{'areas' if new_high_count != 1 else 'area'}"
    )
    message["From"] = settings.smtp_from or settings.firewatch_contact
    message["To"] = subscription.email
    message.set_content("\n".join(lines))
    return message


def send_email(message: EmailMessage) -> None:
    if not settings.email_enabled:
        log.warning(
            "SMTP not configured; alert for %s not sent (subject: %s)",
            message["To"],
            message["Subject"],
        )
        return

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)

    log.info("Sent alert email to %s", message["To"])
