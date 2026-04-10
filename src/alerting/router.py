"""
router.py — AlertRouter: converts AnalysisResult → Alert and decides
which notification channels to use.

Routing table (default):
  critical → PagerDuty + Slack
  high     → Slack
  medium   → Slack
  low      → (stored to DB only, no notification)

Deduplication: same (service_name, severity) suppressed for cooldown_seconds.
Uses Redis for distributed dedup (same lock as AnomalyQueue uses, different prefix).
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

from src.analysis.models import AnalysisResult
from .models import (
    Alert, AlertRule, AlertSeverity, AlertStatus, NotificationChannel
)

logger = logging.getLogger(__name__)

REDIS_URL      = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DEDUP_PREFIX   = "alert:dedup:"

# Default routing rules — ordered by severity
DEFAULT_RULES: list[AlertRule] = [
    AlertRule(
        severity=AlertSeverity.CRITICAL,
        channels=[NotificationChannel.PAGERDUTY, NotificationChannel.SLACK],
        require_confidence_above=0.50,
        cooldown_seconds=300,
    ),
    AlertRule(
        severity=AlertSeverity.HIGH,
        channels=[NotificationChannel.SLACK],
        require_confidence_above=0.40,
        cooldown_seconds=300,
    ),
    AlertRule(
        severity=AlertSeverity.MEDIUM,
        channels=[NotificationChannel.SLACK],
        require_confidence_above=0.30,
        cooldown_seconds=600,
    ),
    AlertRule(
        severity=AlertSeverity.LOW,
        channels=[],            # DB only
        require_confidence_above=0.0,
        cooldown_seconds=3600,
    ),
]


class AlertRouter:
    """
    Converts AnalysisResult objects into Alert objects and
    decides which channels to notify.

    Usage:
        router = AlertRouter()
        await router.connect()

        alert = await router.route(analysis_result)
        # alert.channels_attempted tells caller what to deliver
    """

    def __init__(
        self,
        rules: Optional[list[AlertRule]] = None,
        redis_url: str = REDIS_URL,
    ):
        self._rules     = {r.severity: r for r in (rules or DEFAULT_RULES)}
        self._redis_url = redis_url
        self._redis: Optional[aioredis.Redis] = None

    async def connect(self):
        self._redis = await aioredis.from_url(
            self._redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        logger.info("AlertRouter connected to Redis")

    async def close(self):
        if self._redis:
            await self._redis.aclose()

    # ------------------------------------------------------------------ #
    #  Public                                                              #
    # ------------------------------------------------------------------ #

    async def route(self, result: AnalysisResult) -> Alert:
        """
        Build an Alert from an AnalysisResult and apply routing rules.
        Returns the Alert (possibly with status=SUPPRESSED if deduped).
        """
        a = result.analysis

        try:
            sev = AlertSeverity(a.severity)
        except ValueError:
            sev = AlertSeverity.MEDIUM

        alert = Alert(
            analysis_id=result.id,
            anomaly_id=result.request.anomaly_id,
            service_name=result.request.service_name,
            severity=sev,
            title=self._make_title(result),
            root_cause=a.root_cause,
            confidence=a.confidence,
            recommended_action=a.recommended_action,
            affected_services=a.affected_services,
            remediation_steps=a.remediation_steps,
            escalate_to=a.escalate_to,
        )

        rule = self._rules.get(sev)
        if rule is None:
            logger.debug("No rule for severity %s — suppressing", sev)
            alert.mark_suppressed()
            return alert

        # Confidence gate
        if a.confidence < rule.require_confidence_above:
            logger.info(
                "Alert suppressed — confidence %.2f < threshold %.2f (service=%s)",
                a.confidence, rule.require_confidence_above,
                result.request.service_name,
            )
            alert.mark_suppressed()
            return alert

        # Deduplication
        if await self._is_duplicate(result.request.service_name, sev, rule.cooldown_seconds):
            logger.info(
                "Alert deduplicated — service=%s severity=%s",
                result.request.service_name, sev.value,
            )
            alert.mark_suppressed()
            return alert

        # Mark which channels should be attempted
        alert.channels_attempted = [c.value for c in rule.channels]
        return alert

    # ------------------------------------------------------------------ #
    #  Private                                                             #
    # ------------------------------------------------------------------ #

    async def _is_duplicate(
        self, service: str, severity: AlertSeverity, cooldown: int
    ) -> bool:
        if self._redis is None:
            return False
        key = f"{DEDUP_PREFIX}{service}:{severity.value}"
        existing = await self._redis.get(key)
        if existing:
            return True
        await self._redis.setex(key, cooldown, "1")
        return False

    def _make_title(self, result: AnalysisResult) -> str:
        sev   = result.analysis.severity.upper()
        svc   = result.request.service_name
        score = result.request.anomaly_score
        return f"[{sev}] Anomaly detected on {svc} (score={score:.2f})"
