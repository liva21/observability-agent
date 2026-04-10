"""
slack_notifier.py — Slack Block Kit alert delivery.

Sends rich, structured Slack messages using the Incoming Webhooks API.
Block Kit layout:
  - Header block  (severity emoji + title)
  - Section block (root cause + confidence)
  - Fields block  (service, affected services, escalate_to)
  - Divider
  - Remediation steps (numbered list)
  - Context block (timestamp + anomaly ID)
"""

import os
import time
import logging
import json
from datetime import timezone

import httpx

from .models import Alert, AlertSeverity, DeliveryResult, NotificationChannel

logger = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_TIMEOUT_SEC = float(os.getenv("SLACK_TIMEOUT_SEC", "10"))

SEVERITY_EMOJI = {
    AlertSeverity.LOW:      ":information_source:",
    AlertSeverity.MEDIUM:   ":warning:",
    AlertSeverity.HIGH:     ":rotating_light:",
    AlertSeverity.CRITICAL: ":fire:",
}

SEVERITY_COLOR = {
    AlertSeverity.LOW:      "#36a64f",   # green
    AlertSeverity.MEDIUM:   "#f0a500",   # amber
    AlertSeverity.HIGH:     "#e01e5a",   # red
    AlertSeverity.CRITICAL: "#800000",   # dark red
}


class SlackNotifier:
    """
    Sends alert notifications to Slack via Incoming Webhook.

    Usage:
        notifier = SlackNotifier(webhook_url="https://hooks.slack.com/...")
        result = await notifier.send(alert)
    """

    def __init__(self, webhook_url: str = SLACK_WEBHOOK_URL):
        self.webhook_url = webhook_url

    async def send(self, alert: Alert) -> DeliveryResult:
        if not self.webhook_url:
            logger.warning("SLACK_WEBHOOK_URL not set — skipping Slack delivery")
            return DeliveryResult(
                channel=NotificationChannel.SLACK,
                success=False,
                error="SLACK_WEBHOOK_URL not configured",
            )

        payload = self._build_payload(alert)
        t0 = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=SLACK_TIMEOUT_SEC) as client:
                resp = await client.post(
                    self.webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )

            duration_ms = round((time.monotonic() - t0) * 1000, 1)

            if resp.status_code == 200 and resp.text == "ok":
                logger.info(
                    "Slack alert sent — service=%s severity=%s",
                    alert.service_name, alert.severity.value,
                )
                return DeliveryResult(
                    channel=NotificationChannel.SLACK,
                    success=True,
                    status_code=resp.status_code,
                    response_body=resp.text,
                    duration_ms=duration_ms,
                )
            else:
                error = f"HTTP {resp.status_code}: {resp.text}"
                logger.error("Slack delivery failed: %s", error)
                return DeliveryResult(
                    channel=NotificationChannel.SLACK,
                    success=False,
                    status_code=resp.status_code,
                    response_body=resp.text,
                    duration_ms=duration_ms,
                    error=error,
                )

        except Exception as exc:
            duration_ms = round((time.monotonic() - t0) * 1000, 1)
            logger.error("Slack request failed: %s", exc)
            return DeliveryResult(
                channel=NotificationChannel.SLACK,
                success=False,
                duration_ms=duration_ms,
                error=str(exc),
            )

    # ------------------------------------------------------------------ #
    #  Block Kit payload builder                                           #
    # ------------------------------------------------------------------ #

    def _build_payload(self, alert: Alert) -> dict:
        emoji = SEVERITY_EMOJI.get(alert.severity, ":bell:")
        color = SEVERITY_COLOR.get(alert.severity, "#888888")

        # Remediation steps as numbered text
        steps_text = "\n".join(
            f"{i+1}. {step}"
            for i, step in enumerate(alert.remediation_steps[:5])
        ) or "_No steps provided_"

        # Affected services
        affected = ", ".join(f"`{s}`" for s in alert.affected_services) or "_unknown_"

        # Escalation
        escalate = f"→ *{alert.escalate_to}*" if alert.escalate_to else "_no escalation_"

        timestamp = alert.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") \
            if alert.created_at else "—"

        blocks = [
            # Header
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} {alert.title}",
                },
            },
            # Root cause
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Root cause:* {alert.root_cause}\n"
                        f"*Confidence:* {alert.confidence:.0%}    "
                        f"*Escalate:* {escalate}"
                    ),
                },
            },
            # Fields
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Service*\n`{alert.service_name}`"},
                    {"type": "mrkdwn", "text": f"*Affected*\n{affected}"},
                    {"type": "mrkdwn", "text": f"*Action*\n{alert.recommended_action}"},
                    {"type": "mrkdwn", "text": f"*Alert ID*\n`{alert.id[:8]}`"},
                ],
            },
            {"type": "divider"},
            # Remediation
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Remediation steps:*\n{steps_text}",
                },
            },
            # Context footer
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Observability Agent  •  {timestamp}  •  anomaly `{alert.anomaly_id[:8]}`",
                    }
                ],
            },
        ]

        return {
            "attachments": [
                {
                    "color": color,
                    "blocks": blocks,
                }
            ]
        }
