"""
test_alerting.py — Full test suite for Module 4 (Alert & Notification Engine).

All external calls (Redis, Slack webhook, PagerDuty API, PostgreSQL) are mocked.

Run:
    pytest tests/test_alerting.py -v
"""

import json
import pytest
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.alerting.models import (
    Alert,
    AlertRule,
    AlertStatus,
    AlertSeverity,
    NotificationChannel,
    DeliveryResult,
)
from src.alerting.router import AlertRouter
from src.alerting.slack_notifier import SlackNotifier
from src.alerting.pagerduty_client import PagerDutyClient
from src.alerting.engine import AlertEngine
from src.analysis.models import (
    AnalysisResult,
    AnalysisRequest,
    ServiceContext,
    RootCauseAnalysis,
)


# ================================================================== #
#  Fixtures                                                            #
# ================================================================== #

def make_analysis_result(
    service="payment-service",
    severity="high",
    confidence=0.88,
    anomaly_score=0.82,
) -> AnalysisResult:
    request = AnalysisRequest(
        anomaly_id="anomaly-111",
        service_name=service,
        anomaly_score=anomaly_score,
        severity=severity,
        window_features={"error_rate": 0.4, "window_seconds": 60},
        detector_results=[],
    )
    context = ServiceContext(service_name=service)
    analysis = RootCauseAnalysis(
        root_cause="DB connection pool exhausted",
        confidence=confidence,
        severity=severity,
        affected_services=[service, "api-gateway"],
        affected_users_estimate="~500 users",
        timeline=["12:00 latency rises", "12:01 errors start"],
        contributing_factors=["slow queries", "missing index"],
        recommended_action="Scale connection pool immediately",
        remediation_steps=["Kill slow queries", "Increase max_connections"],
        escalate_to="database-team",
        llm_model="gpt-4o",
    )
    return AnalysisResult(
        request=request,
        context=context,
        analysis=analysis,
    )


@pytest.fixture
def analysis_result():
    return make_analysis_result()


@pytest.fixture
def critical_result():
    return make_analysis_result(severity="critical", confidence=0.95)


@pytest.fixture
def low_confidence_result():
    return make_analysis_result(severity="high", confidence=0.10)


# ================================================================== #
#  Alert model tests                                                   #
# ================================================================== #

class TestAlertModel:
    def test_auto_id_generated(self, analysis_result):
        alert = Alert(
            analysis_id=analysis_result.id,
            anomaly_id="anomaly-111",
            service_name="payment-service",
            severity=AlertSeverity.HIGH,
            title="Test alert",
            root_cause="DB timeout",
            confidence=0.88,
            recommended_action="Fix it",
        )
        assert alert.id
        assert len(alert.id) == 36

    def test_auto_created_at(self):
        alert = Alert(
            analysis_id="a1", anomaly_id="b1", service_name="svc",
            severity=AlertSeverity.LOW, title="t", root_cause="r",
            confidence=0.5, recommended_action="x",
        )
        assert alert.created_at is not None

    def test_confidence_clamped(self):
        alert = Alert(
            analysis_id="a1", anomaly_id="b1", service_name="svc",
            severity=AlertSeverity.HIGH, title="t", root_cause="r",
            confidence=1.9, recommended_action="x",
        )
        assert alert.confidence == 1.0

    def test_mark_sent_updates_status(self):
        alert = Alert(
            analysis_id="a1", anomaly_id="b1", service_name="svc",
            severity=AlertSeverity.HIGH, title="t", root_cause="r",
            confidence=0.8, recommended_action="x",
        )
        alert.mark_sent("slack")
        assert alert.status == AlertStatus.SENT
        assert "slack" in alert.channels_succeeded
        assert alert.sent_at is not None

    def test_mark_failed_keeps_pending_if_no_success(self):
        alert = Alert(
            analysis_id="a1", anomaly_id="b1", service_name="svc",
            severity=AlertSeverity.HIGH, title="t", root_cause="r",
            confidence=0.8, recommended_action="x",
        )
        alert.mark_failed("slack", "connection refused")
        assert alert.status == AlertStatus.FAILED
        assert "slack: connection refused" in alert.error_messages

    def test_mark_suppressed(self):
        alert = Alert(
            analysis_id="a1", anomaly_id="b1", service_name="svc",
            severity=AlertSeverity.HIGH, title="t", root_cause="r",
            confidence=0.8, recommended_action="x",
        )
        alert.mark_suppressed()
        assert alert.status == AlertStatus.SUPPRESSED


# ================================================================== #
#  AlertRouter tests                                                   #
# ================================================================== #

class TestAlertRouter:

    def _make_router_no_redis(self) -> AlertRouter:
        router = AlertRouter()
        router._redis = None
        return router

    @patch("src.alerting.router.aioredis.from_url", new_callable=AsyncMock)
    def test_route_high_severity_uses_slack(self, mock_redis, analysis_result):
        router = self._make_router_no_redis()
        alert = asyncio.get_event_loop().run_until_complete(
            router.route(analysis_result)
        )
        assert NotificationChannel.SLACK.value in alert.channels_attempted

    @patch("src.alerting.router.aioredis.from_url", new_callable=AsyncMock)
    def test_route_critical_uses_pagerduty_and_slack(self, mock_redis, critical_result):
        router = self._make_router_no_redis()
        alert = asyncio.get_event_loop().run_until_complete(
            router.route(critical_result)
        )
        assert NotificationChannel.PAGERDUTY.value in alert.channels_attempted
        assert NotificationChannel.SLACK.value in alert.channels_attempted

    @patch("src.alerting.router.aioredis.from_url", new_callable=AsyncMock)
    def test_low_confidence_suppressed(self, mock_redis, low_confidence_result):
        router = self._make_router_no_redis()
        alert = asyncio.get_event_loop().run_until_complete(
            router.route(low_confidence_result)
        )
        assert alert.status == AlertStatus.SUPPRESSED

    @patch("src.alerting.router.aioredis.from_url", new_callable=AsyncMock)
    def test_title_contains_service_and_severity(self, mock_redis, analysis_result):
        router = self._make_router_no_redis()
        alert = asyncio.get_event_loop().run_until_complete(
            router.route(analysis_result)
        )
        assert "payment-service" in alert.title
        assert "HIGH" in alert.title

    @patch("src.alerting.router.aioredis.from_url", new_callable=AsyncMock)
    def test_dedup_suppresses_duplicate(self, mock_redis, analysis_result):
        router = self._make_router_no_redis()

        # Manually set dedup lock
        mock_redis_client = AsyncMock()
        mock_redis_client.get = AsyncMock(return_value="1")  # already locked
        mock_redis_client.setex = AsyncMock()
        router._redis = mock_redis_client

        alert = asyncio.get_event_loop().run_until_complete(
            router.route(analysis_result)
        )
        assert alert.status == AlertStatus.SUPPRESSED

    @patch("src.alerting.router.aioredis.from_url", new_callable=AsyncMock)
    def test_dedup_sets_lock_on_first_alert(self, mock_redis, analysis_result):
        router = self._make_router_no_redis()

        mock_redis_client = AsyncMock()
        mock_redis_client.get = AsyncMock(return_value=None)  # no lock yet
        mock_redis_client.setex = AsyncMock()
        router._redis = mock_redis_client

        asyncio.get_event_loop().run_until_complete(router.route(analysis_result))
        mock_redis_client.setex.assert_called_once()

    @patch("src.alerting.router.aioredis.from_url", new_callable=AsyncMock)
    def test_low_severity_no_channels(self, mock_redis):
        router = self._make_router_no_redis()
        result = make_analysis_result(severity="low", confidence=0.5)
        alert = asyncio.get_event_loop().run_until_complete(router.route(result))
        assert alert.channels_attempted == []


# ================================================================== #
#  SlackNotifier tests                                                 #
# ================================================================== #

class TestSlackNotifier:

    def _make_alert(self) -> Alert:
        return Alert(
            analysis_id="a1", anomaly_id="b1",
            service_name="payment-service",
            severity=AlertSeverity.HIGH,
            title="[HIGH] Anomaly on payment-service",
            root_cause="DB pool exhausted",
            confidence=0.88,
            recommended_action="Scale pool",
            affected_services=["payment-service", "api-gateway"],
            remediation_steps=["Kill slow queries", "Increase max_connections"],
            escalate_to="database-team",
            channels_attempted=["slack"],
        )

    def test_no_webhook_returns_failure(self):
        notifier = SlackNotifier(webhook_url="")
        result = asyncio.get_event_loop().run_until_complete(
            notifier.send(self._make_alert())
        )
        assert result.success is False
        assert "not configured" in result.error

    @patch("src.alerting.slack_notifier.httpx.AsyncClient")
    def test_successful_delivery(self, MockClient):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        MockClient.return_value = mock_client

        notifier = SlackNotifier(webhook_url="https://hooks.slack.com/test")
        result = asyncio.get_event_loop().run_until_complete(
            notifier.send(self._make_alert())
        )
        assert result.success is True
        assert result.status_code == 200

    @patch("src.alerting.slack_notifier.httpx.AsyncClient")
    def test_http_error_returns_failure(self, MockClient):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "invalid_payload"
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        MockClient.return_value = mock_client

        notifier = SlackNotifier(webhook_url="https://hooks.slack.com/test")
        result = asyncio.get_event_loop().run_until_complete(
            notifier.send(self._make_alert())
        )
        assert result.success is False
        assert "400" in result.error

    def test_payload_contains_root_cause(self):
        notifier = SlackNotifier(webhook_url="https://hooks.slack.com/test")
        alert = self._make_alert()
        payload = notifier._build_payload(alert)
        payload_str = json.dumps(payload)
        assert "DB pool exhausted" in payload_str

    def test_payload_contains_remediation_steps(self):
        notifier = SlackNotifier(webhook_url="https://hooks.slack.com/test")
        payload = notifier._build_payload(self._make_alert())
        payload_str = json.dumps(payload)
        assert "Kill slow queries" in payload_str

    def test_payload_has_color_for_severity(self):
        notifier = SlackNotifier(webhook_url="https://hooks.slack.com/test")
        payload = notifier._build_payload(self._make_alert())
        assert "color" in payload["attachments"][0]


# ================================================================== #
#  PagerDutyClient tests                                               #
# ================================================================== #

class TestPagerDutyClient:

    def _make_critical_alert(self) -> Alert:
        return Alert(
            analysis_id="a1", anomaly_id="b1",
            service_name="payment-service",
            severity=AlertSeverity.CRITICAL,
            title="[CRITICAL] Anomaly on payment-service",
            root_cause="Full service outage",
            confidence=0.95,
            recommended_action="Page on-call immediately",
            channels_attempted=["pagerduty"],
        )

    def test_no_routing_key_returns_failure(self):
        client = PagerDutyClient(routing_key="")
        result = asyncio.get_event_loop().run_until_complete(
            client.send(self._make_critical_alert())
        )
        assert result.success is False
        assert "not configured" in result.error

    @patch("src.alerting.pagerduty_client.httpx.AsyncClient")
    def test_successful_trigger(self, MockClient):
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_resp.text = '{"status":"success","dedup_key":"abc"}'
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        MockClient.return_value = mock_client

        client = PagerDutyClient(routing_key="r-key-123")
        result = asyncio.get_event_loop().run_until_complete(
            client.send(self._make_critical_alert())
        )
        assert result.success is True
        assert result.status_code == 202

    def test_payload_severity_mapping_critical(self):
        client = PagerDutyClient(routing_key="key")
        alert = self._make_critical_alert()
        payload = client._build_payload(alert)
        assert payload["payload"]["severity"] == "critical"

    def test_payload_severity_mapping_high(self):
        client = PagerDutyClient(routing_key="key")
        alert = self._make_critical_alert()
        alert.severity = AlertSeverity.HIGH
        payload = client._build_payload(alert)
        assert payload["payload"]["severity"] == "error"

    def test_payload_dedup_key_is_alert_id(self):
        client = PagerDutyClient(routing_key="key")
        alert = self._make_critical_alert()
        payload = client._build_payload(alert)
        assert payload["dedup_key"] == alert.id

    def test_payload_event_action_is_trigger(self):
        client = PagerDutyClient(routing_key="key")
        payload = client._build_payload(self._make_critical_alert())
        assert payload["event_action"] == "trigger"


# ================================================================== #
#  AlertEngine integration tests                                       #
# ================================================================== #

class TestAlertEngine:

    @patch("src.alerting.engine.save_alert", new_callable=AsyncMock)
    @patch("src.alerting.router.aioredis.from_url", new_callable=AsyncMock)
    @patch("src.alerting.slack_notifier.httpx.AsyncClient")
    def test_process_high_severity_sends_slack(
        self, MockHTTP, mock_redis_url, mock_save, analysis_result
    ):
        # Redis returns no dedup lock
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.setex = AsyncMock()
        mock_redis_url.return_value = mock_redis

        # Slack returns 200
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"
        mock_http = AsyncMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_http.post = AsyncMock(return_value=mock_resp)
        MockHTTP.return_value = mock_http

        engine = AlertEngine(slack_webhook_url="https://hooks.slack.com/test")
        engine.router._redis = mock_redis

        alert = asyncio.get_event_loop().run_until_complete(
            engine.process(analysis_result)
        )
        assert alert.status == AlertStatus.SENT
        assert "slack" in alert.channels_succeeded
        mock_save.assert_called_once()

    @patch("src.alerting.engine.save_alert", new_callable=AsyncMock)
    @patch("src.alerting.router.aioredis.from_url", new_callable=AsyncMock)
    def test_process_suppressed_alert_still_persisted(
        self, mock_redis_url, mock_save
    ):
        # Redis returns existing dedup lock → suppressed
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value="1")
        mock_redis_url.return_value = mock_redis

        engine = AlertEngine()
        engine.router._redis = mock_redis

        alert = asyncio.get_event_loop().run_until_complete(
            engine.process(make_analysis_result())
        )
        assert alert.status == AlertStatus.SUPPRESSED
        mock_save.assert_called_once()   # suppressed alerts still persisted

    @patch("src.alerting.engine.save_alert", new_callable=AsyncMock)
    @patch("src.alerting.router.aioredis.from_url", new_callable=AsyncMock)
    def test_engine_stats_update(self, mock_redis_url, mock_save):
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value="1")  # always suppressed
        engine = AlertEngine()
        engine.router._redis = mock_redis

        for _ in range(3):
            asyncio.get_event_loop().run_until_complete(
                engine.process(make_analysis_result())
            )

        stats = engine.stats
        assert stats["processed"] == 3
        assert stats["suppressed"] == 3
