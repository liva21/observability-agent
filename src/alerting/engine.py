"""
engine.py — AlertEngine: top-level orchestrator for Module 4.

Wires together:
  AlertRouter   → decides channels
  SlackNotifier → delivers to Slack
  PagerDutyClient → delivers to PagerDuty
  DB            → persists every alert

Consumed by the analysis worker (Module 3) after each LLM analysis:
    engine = AlertEngine()
    await engine.connect()
    await engine.process(analysis_result)
"""

import asyncio
import logging

from src.analysis.models import AnalysisResult
from .models import Alert, AlertStatus, NotificationChannel
from .router import AlertRouter
from .slack_notifier import SlackNotifier
from .pagerduty_client import PagerDutyClient
from .db import save_alert

logger = logging.getLogger(__name__)


class AlertEngine:
    """
    End-to-end alert pipeline:
      AnalysisResult → route → deliver → persist

    Usage:
        engine = AlertEngine()
        await engine.connect()
        await engine.process(analysis_result)
        await engine.close()
    """

    def __init__(
        self,
        slack_webhook_url: str = "",
        pagerduty_routing_key: str = "",
    ):
        self.router     = AlertRouter()
        self.slack      = SlackNotifier(webhook_url=slack_webhook_url)
        self.pagerduty  = PagerDutyClient(routing_key=pagerduty_routing_key)
        self._processed = 0
        self._sent      = 0
        self._suppressed = 0
        self._failed    = 0

    async def connect(self):
        await self.router.connect()
        logger.info("AlertEngine ready")

    async def close(self):
        await self.router.close()

    # ------------------------------------------------------------------ #
    #  Public                                                              #
    # ------------------------------------------------------------------ #

    async def process(self, result: AnalysisResult) -> Alert:
        """
        Full pipeline: route → deliver → persist.
        Always returns the Alert regardless of delivery outcome.
        """
        self._processed += 1

        # Step 1 — Route
        alert = await self.router.route(result)

        # Step 2 — Deliver (skip if suppressed or no channels)
        if alert.status != AlertStatus.SUPPRESSED and alert.channels_attempted:
            await self._deliver(alert)

        # Step 3 — Persist
        try:
            await save_alert(alert)
        except Exception as exc:
            logger.error("Failed to persist alert %s: %s", alert.id, exc)

        # Update stats
        if alert.status == AlertStatus.SUPPRESSED:
            self._suppressed += 1
        elif alert.status == AlertStatus.SENT:
            self._sent += 1
        else:
            self._failed += 1

        return alert

    @property
    def stats(self) -> dict:
        return {
            "processed":  self._processed,
            "sent":       self._sent,
            "suppressed": self._suppressed,
            "failed":     self._failed,
        }

    # ------------------------------------------------------------------ #
    #  Private                                                             #
    # ------------------------------------------------------------------ #

    async def _deliver(self, alert: Alert):
        """
        Attempt delivery on all channels concurrently.
        Updates alert state based on results.
        """
        tasks = {}
        for channel_name in alert.channels_attempted:
            channel = NotificationChannel(channel_name)
            if channel == NotificationChannel.SLACK:
                tasks[channel_name] = self.slack.send(alert)
            elif channel == NotificationChannel.PAGERDUTY:
                tasks[channel_name] = self.pagerduty.send(alert)
            else:
                logger.warning("Unknown channel: %s — skipping", channel_name)

        if not tasks:
            return

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        for channel_name, delivery_result in zip(tasks.keys(), results):
            if isinstance(delivery_result, Exception):
                alert.mark_failed(channel_name, str(delivery_result))
                logger.error(
                    "Channel %s raised exception for alert %s: %s",
                    channel_name, alert.id, delivery_result,
                )
            elif delivery_result.success:
                alert.mark_sent(channel_name)
                logger.info(
                    "Alert %s delivered via %s (%.0fms)",
                    alert.id[:8], channel_name, delivery_result.duration_ms,
                )
            else:
                alert.mark_failed(channel_name, delivery_result.error)
