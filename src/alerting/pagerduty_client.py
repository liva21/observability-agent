"""
pagerduty_client.py — PagerDuty Events API v2 integration.

Sends TRIGGER events for new critical/high alerts.
Dedup key = alert.id so re-sends are idempotent.

PagerDuty severity mapping:
  critical → critical
  high     → error
  medium   → warning
  low      → info
"""

import os
import time
import logging
import json
from datetime import timezone

import httpx

from .models import Alert, AlertSeverity, DeliveryResult, NotificationChannel

logger = logging.getLogger(__name__)

PAGERDUTY_ROUTING_KEY = os.getenv("PAGERDUTY_ROUTING_KEY", "")
PAGERDUTY_API_URL     = "https://events.pagerduty.com/v2/enqueue"
PAGERDUTY_TIMEOUT_SEC = float(os.getenv("PAGERDUTY_TIMEOUT_SEC", "10"))

PD_SEVERITY_MAP = {
    AlertSeverity.CRITICAL: "critical",
    AlertSeverity.HIGH:     "error",
    AlertSeverity.MEDIUM:   "warning",
    AlertSeverity.LOW:      "info",
}


class PagerDutyClient:
    """
    Sends alerts to PagerDuty via Events API v2.

    Usage:
        client = PagerDutyClient(routing_key="...")
        result = await client.send(alert)
    """

    def __init__(self, routing_key: str = PAGERDUTY_ROUTING_KEY):
        self.routing_key = routing_key

    async def send(self, alert: Alert) -> DeliveryResult:
        if not self.routing_key:
            logger.warning("PAGERDUTY_ROUTING_KEY not set — skipping PagerDuty delivery")
            return DeliveryResult(
                channel=NotificationChannel.PAGERDUTY,
                success=False,
                error="PAGERDUTY_ROUTING_KEY not configured",
            )

        payload = self._build_payload(alert)
        t0 = time.monotonic()

        try:
            async with httpx.AsyncClient(timeout=PAGERDUTY_TIMEOUT_SEC) as client:
                resp = await client.post(
                    PAGERDUTY_API_URL,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/vnd.pagerduty+json;version=2",
                    },
                )

            duration_ms = round((time.monotonic() - t0) * 1000, 1)
            body = resp.text

            if resp.status_code in (200, 202):
                logger.info(
                    "PagerDuty event sent — service=%s severity=%s dedup_key=%s",
                    alert.service_name, alert.severity.value, alert.id,
                )
                return DeliveryResult(
                    channel=NotificationChannel.PAGERDUTY,
                    success=True,
                    status_code=resp.status_code,
                    response_body=body,
                    duration_ms=duration_ms,
                )
            else:
                error = f"HTTP {resp.status_code}: {body}"
                logger.error("PagerDuty delivery failed: %s", error)
                return DeliveryResult(
                    channel=NotificationChannel.PAGERDUTY,
                    success=False,
                    status_code=resp.status_code,
                    response_body=body,
                    duration_ms=duration_ms,
                    error=error,
                )

        except Exception as exc:
            duration_ms = round((time.monotonic() - t0) * 1000, 1)
            logger.error("PagerDuty request failed: %s", exc)
            return DeliveryResult(
                channel=NotificationChannel.PAGERDUTY,
                success=False,
                duration_ms=duration_ms,
                error=str(exc),
            )

    # ------------------------------------------------------------------ #
    #  Payload builder                                                     #
    # ------------------------------------------------------------------ #

    def _build_payload(self, alert: Alert) -> dict:
        pd_severity = PD_SEVERITY_MAP.get(alert.severity, "error")

        # Custom details for the PD incident
        custom_details = {
            "root_cause":          alert.root_cause,
            "confidence":          f"{alert.confidence:.0%}",
            "affected_services":   alert.affected_services,
            "recommended_action":  alert.recommended_action,
            "remediation_steps":   alert.remediation_steps,
            "escalate_to":         alert.escalate_to,
            "anomaly_id":          alert.anomaly_id,
            "analysis_id":         alert.analysis_id,
        }

        return {
            "routing_key":  self.routing_key,
            "event_action": "trigger",
            "dedup_key":    alert.id,          # idempotent re-sends
            "payload": {
                "summary":   alert.title,
                "source":    alert.service_name,
                "severity":  pd_severity,
                "timestamp": alert.created_at.isoformat() if alert.created_at else None,
                "component": alert.service_name,
                "group":     "observability-agent",
                "class":     "anomaly",
                "custom_details": custom_details,
            },
            "links": [],
            "images": [],
        }
