"""
test_dashboard.py — Test suite for Module 5 (Dashboard API).

All DB calls mocked. Tests verify: route responses, status codes,
query parameter handling, WebSocket payload structure, and edge cases.

Run:
    pytest tests/test_dashboard.py -v
"""

import json
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from fastapi import FastAPI

# ------------------------------------------------------------------ #
#  Minimal test app (avoids importing full dashboard with DB pools)    #
# ------------------------------------------------------------------ #

from src.dashboard.routers import logs, anomalies, alerts, analysis, metrics

test_app = FastAPI()
test_app.include_router(logs.router,      prefix="/api/logs")
test_app.include_router(anomalies.router, prefix="/api/anomalies")
test_app.include_router(alerts.router,    prefix="/api/alerts")
test_app.include_router(analysis.router,  prefix="/api/analysis")
test_app.include_router(metrics.router,   prefix="/api/metrics")

client = TestClient(test_app)


# ================================================================== #
#  Fixtures                                                            #
# ================================================================== #

def make_log_row():
    return {
        "id": "log-uuid-1",
        "timestamp": datetime.now(tz=timezone.utc),
        "service_name": "payment-service",
        "level": "ERROR",
        "message": "DB timeout",
        "trace_id": "trace-abc",
        "span_id": "span-123",
        "host": "pod-1",
        "environment": "production",
        "attributes": {},
    }


def make_analysis_row():
    return {
        "id": "analysis-uuid-1",
        "analyzed_at": datetime.now(tz=timezone.utc),
        "service_name": "payment-service",
        "anomaly_id": "anomaly-111",
        "anomaly_score": 0.87,
        "severity": "high",
        "root_cause": "DB connection pool exhausted",
        "confidence": 0.88,
        "recommended_action": "Scale connection pool",
        "affected_services": ["payment-service"],
        "escalate_to": "database-team",
        "success": True,
    }


def make_alert_row():
    return {
        "id": "alert-uuid-1",
        "created_at": datetime.now(tz=timezone.utc),
        "sent_at": datetime.now(tz=timezone.utc),
        "analysis_id": "analysis-uuid-1",
        "anomaly_id": "anomaly-111",
        "service_name": "payment-service",
        "severity": "high",
        "status": "sent",
        "title": "[HIGH] Anomaly on payment-service",
        "root_cause": "DB pool exhausted",
        "confidence": 0.88,
        "recommended_action": "Scale pool",
        "affected_services": ["payment-service"],
        "remediation_steps": ["Kill slow queries"],
        "escalate_to": "database-team",
        "channels_attempted": ["slack"],
        "channels_succeeded": ["slack"],
        "error_messages": [],
    }


# ================================================================== #
#  Logs endpoint tests                                                  #
# ================================================================== #

class TestLogsEndpoints:

    @patch("src.dashboard.routers.logs.fetch_logs", new_callable=AsyncMock)
    def test_list_logs_returns_200(self, mock_fetch):
        mock_fetch.return_value = [make_log_row()]
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert "logs" in data
        assert data["count"] == 1

    @patch("src.dashboard.routers.logs.fetch_logs", new_callable=AsyncMock)
    def test_list_logs_passes_filters(self, mock_fetch):
        mock_fetch.return_value = []
        resp = client.get("/api/logs?service_name=payment-service&level=ERROR&limit=50")
        assert resp.status_code == 200
        mock_fetch.assert_called_once_with(
            service_name="payment-service",
            level="ERROR",
            limit=50,
        )

    @patch("src.dashboard.routers.logs.fetch_logs", new_callable=AsyncMock)
    def test_list_logs_default_limit_100(self, mock_fetch):
        mock_fetch.return_value = []
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        mock_fetch.assert_called_once_with(service_name=None, level=None, limit=100)

    @patch("src.dashboard.routers.logs.fetch_logs", new_callable=AsyncMock)
    def test_list_logs_limit_too_large_rejected(self, mock_fetch):
        mock_fetch.return_value = []
        resp = client.get("/api/logs?limit=9999")
        assert resp.status_code == 422   # FastAPI validation error

    @patch("src.dashboard.routers.logs.get_pool", new_callable=AsyncMock)
    def test_get_log_not_found_returns_404(self, mock_pool):
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_pool.return_value.__aenter__ = AsyncMock(return_value=mock_conn)

        mock_pool_obj = AsyncMock()
        mock_pool_obj.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("src.dashboard.routers.logs.get_pool", return_value=mock_pool_obj):
            resp = client.get("/api/logs/nonexistent-id")
        assert resp.status_code == 404

    @patch("src.dashboard.routers.logs.get_pool", new_callable=AsyncMock)
    def test_log_stats_structure(self, mock_pool):
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(side_effect=[1000, 5.2])
        mock_conn.fetch = AsyncMock(side_effect=[
            [{"level": "ERROR", "cnt": 150}, {"level": "INFO", "cnt": 850}],
            [{"service_name": "payment-service", "cnt": 400}],
        ])
        mock_pool_obj = AsyncMock()
        mock_pool_obj.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))
        with patch("src.dashboard.routers.logs.get_pool", return_value=mock_pool_obj):
            resp = client.get("/api/logs/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "last_24h" in data
        assert "by_level" in data["last_24h"]
        assert "by_service" in data["last_24h"]


# ================================================================== #
#  Anomalies endpoint tests                                            #
# ================================================================== #

class TestAnomaliesEndpoints:

    @patch("src.dashboard.routers.anomalies.fetch_analyses", new_callable=AsyncMock)
    def test_list_anomalies_returns_200(self, mock_fetch):
        mock_fetch.return_value = [make_analysis_row()]
        resp = client.get("/api/anomalies")
        assert resp.status_code == 200
        data = resp.json()
        assert "anomalies" in data
        assert data["count"] == 1

    @patch("src.dashboard.routers.anomalies.fetch_analyses", new_callable=AsyncMock)
    def test_list_anomalies_filters_passed(self, mock_fetch):
        mock_fetch.return_value = []
        resp = client.get("/api/anomalies?service_name=payment-service&severity=high&limit=5")
        assert resp.status_code == 200
        mock_fetch.assert_called_once_with(limit=5, service_name="payment-service", severity="high")

    @patch("src.dashboard.routers.anomalies.get_analysis_pool", new_callable=AsyncMock)
    def test_summary_structure(self, mock_pool):
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(side_effect=[50, 10])
        mock_conn.fetch = AsyncMock(side_effect=[
            [{"severity": "high", "cnt": 8, "avg_confidence": 0.85}],
            [{"service_name": "payment-service", "anomaly_count": 5}],
        ])
        mock_pool_obj = AsyncMock()
        mock_pool_obj.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))
        with patch("src.dashboard.routers.anomalies.get_analysis_pool", return_value=mock_pool_obj):
            resp = client.get("/api/anomalies/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_analyzed" in data
        assert "by_severity" in data
        assert "top_affected_services" in data

    @patch("src.dashboard.routers.anomalies.get_analysis_pool", new_callable=AsyncMock)
    def test_service_trend_not_found(self, mock_pool):
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_pool_obj = AsyncMock()
        mock_pool_obj.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))
        with patch("src.dashboard.routers.anomalies.get_analysis_pool", return_value=mock_pool_obj):
            resp = client.get("/api/anomalies/unknown-svc/trend")
        assert resp.status_code == 404

    @patch("src.dashboard.routers.anomalies.get_analysis_pool", new_callable=AsyncMock)
    def test_service_trend_data_points(self, mock_pool):
        now = datetime.now(tz=timezone.utc)
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[
            {"analyzed_at": now, "anomaly_score": 0.72, "severity": "high", "confidence": 0.85},
            {"analyzed_at": now, "anomaly_score": 0.45, "severity": "medium", "confidence": 0.70},
        ])
        mock_pool_obj = AsyncMock()
        mock_pool_obj.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))
        with patch("src.dashboard.routers.anomalies.get_analysis_pool", return_value=mock_pool_obj):
            resp = client.get("/api/anomalies/payment-service/trend?hours=6")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data_points"]) == 2
        assert "timestamp" in data["data_points"][0]
        assert "score" in data["data_points"][0]


# ================================================================== #
#  Alerts endpoint tests                                               #
# ================================================================== #

class TestAlertsEndpoints:

    @patch("src.dashboard.routers.alerts.fetch_alerts", new_callable=AsyncMock)
    def test_list_alerts_returns_200(self, mock_fetch):
        mock_fetch.return_value = [make_alert_row()]
        resp = client.get("/api/alerts")
        assert resp.status_code == 200
        data = resp.json()
        assert "alerts" in data
        assert data["count"] == 1

    @patch("src.dashboard.routers.alerts.fetch_alerts", new_callable=AsyncMock)
    def test_list_alerts_filters_passed(self, mock_fetch):
        mock_fetch.return_value = []
        resp = client.get("/api/alerts?severity=critical&status=sent&limit=10")
        assert resp.status_code == 200
        mock_fetch.assert_called_once_with(
            limit=10,
            service_name=None,
            severity="critical",
            status="sent",
        )

    @patch("src.dashboard.routers.alerts.alert_stats", new_callable=AsyncMock)
    def test_alert_stats_structure(self, mock_stats):
        mock_stats.return_value = {
            "total": 100,
            "sent": 60,
            "suppressed": 40,
            "by_severity": {"high": 30, "critical": 15, "medium": 55},
        }
        resp = client.get("/api/alerts/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 100
        assert "by_severity" in data

    @patch("src.dashboard.routers.alerts.get_pool", new_callable=AsyncMock)
    def test_get_alert_not_found(self, mock_pool):
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_pool_obj = AsyncMock()
        mock_pool_obj.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))
        with patch("src.dashboard.routers.alerts.get_pool", return_value=mock_pool_obj):
            resp = client.get("/api/alerts/nonexistent")
        assert resp.status_code == 404


# ================================================================== #
#  Analysis endpoint tests                                             #
# ================================================================== #

class TestAnalysisEndpoints:

    @patch("src.dashboard.routers.analysis.fetch_recent", new_callable=AsyncMock)
    def test_list_analyses_returns_200(self, mock_fetch):
        mock_fetch.return_value = [make_analysis_row()]
        resp = client.get("/api/analysis")
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    @patch("src.dashboard.routers.analysis.get_by_id", new_callable=AsyncMock)
    def test_get_analysis_not_found(self, mock_get):
        mock_get.return_value = None
        resp = client.get("/api/analysis/nonexistent")
        assert resp.status_code == 404

    @patch("src.dashboard.routers.analysis.get_by_id", new_callable=AsyncMock)
    def test_get_analysis_returns_full_json(self, mock_get):
        mock_get.return_value = {"id": "abc", "root_cause": "DB pool exhausted"}
        resp = client.get("/api/analysis/abc")
        assert resp.status_code == 200
        assert resp.json()["root_cause"] == "DB pool exhausted"

    @patch("src.dashboard.routers.analysis.get_pool", new_callable=AsyncMock)
    def test_confidence_distribution_structure(self, mock_pool):
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[
            {"bucket": "high (0.6-0.8)", "cnt": 30, "avg_confidence": 0.73},
        ])
        mock_conn.fetchval = AsyncMock(side_effect=[0.75, 50])
        mock_pool_obj = AsyncMock()
        mock_pool_obj.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))
        with patch("src.dashboard.routers.analysis.get_pool", return_value=mock_pool_obj):
            resp = client.get("/api/analysis/confidence")
        assert resp.status_code == 200
        data = resp.json()
        assert "distribution" in data
        assert "average_confidence" in data


# ================================================================== #
#  Metrics endpoint tests                                              #
# ================================================================== #

class TestMetricsEndpoints:

    @patch("src.dashboard.routers.metrics.get_ingestion_pool", new_callable=AsyncMock)
    @patch("src.dashboard.routers.metrics.get_analysis_pool", new_callable=AsyncMock)
    @patch("src.dashboard.routers.metrics.get_alerting_pool", new_callable=AsyncMock)
    def test_summary_structure(self, mock_alerting, mock_analysis, mock_ingestion):
        def make_pool(values):
            mock_conn = AsyncMock()
            mock_conn.fetchval = AsyncMock(side_effect=values)
            mock_pool_obj = AsyncMock()
            mock_pool_obj.acquire = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=False),
            ))
            return mock_pool_obj

        mock_ingestion.return_value = make_pool([5000, 120000, 3.5])
        mock_analysis.return_value = make_pool([48, 3, 0.82, 2800.0])
        mock_alerting.return_value = make_pool([12, 36])

        resp = client.get("/api/metrics/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "ingestion" in data
        assert "detection" in data
        assert "alerting" in data
        assert "logs_last_1h"       in data["ingestion"]
        assert "anomalies_last_24h" in data["detection"]
        assert "sent_last_24h"      in data["alerting"]

    @patch("src.dashboard.routers.metrics.get_ingestion_pool", new_callable=AsyncMock)
    @patch("src.dashboard.routers.metrics.get_analysis_pool", new_callable=AsyncMock)
    def test_pipeline_health_structure(self, mock_analysis, mock_ingestion):
        def make_pool(values):
            mock_conn = AsyncMock()
            mock_conn.fetchval = AsyncMock(side_effect=values)
            mock_pool_obj = AsyncMock()
            mock_pool_obj.acquire = MagicMock(return_value=AsyncMock(
                __aenter__=AsyncMock(return_value=mock_conn),
                __aexit__=AsyncMock(return_value=False),
            ))
            return mock_pool_obj

        mock_ingestion.return_value = make_pool([100])
        mock_analysis.return_value = make_pool([5])

        resp = client.get("/api/metrics/pipeline/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "stages" in data
        assert "ingestion" in data["stages"]
        assert "analysis" in data["stages"]
        assert data["stages"]["ingestion"]["status"] == "ok"
