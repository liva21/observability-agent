"""
test_analysis.py — Test suite for Module 3 (LLM Analysis Agent).

LLM calls and external tool calls (DB, Prometheus, Redis) are all mocked.
Tests verify: prompt construction, JSON parsing, fallback handling,
state graph flow, and result persistence.

Run:
    pytest tests/test_analysis.py -v
"""

import json
import pytest
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.analysis.models import (
    ServiceContext,
    RootCauseAnalysis,
    AnalysisRequest,
    AnalysisResult,
)
from src.analysis.prompts import build_analysis_prompt, SYSTEM_PROMPT
from src.analysis.agent import (
    fetch_context_node,
    llm_analyze_node,
    format_result_node,
    _fallback_analysis,
    AgentState,
)
from src.analysis.tools import get_dependency_map, _compute_depth


# ================================================================== #
#  Fixtures                                                            #
# ================================================================== #

@pytest.fixture
def sample_request():
    return AnalysisRequest(
        anomaly_id="anomaly-abc123",
        service_name="payment-service",
        anomaly_score=0.87,
        severity="high",
        window_features={
            "service_name": "payment-service",
            "window_seconds": 60,
            "error_rate": 0.42,
            "error_count": 252,
            "critical_count": 18,
            "latency_p50": 120.0,
            "latency_p95": 4800.0,
            "latency_p99": 9200.0,
            "logs_per_second": 10.0,
            "unique_traces": 45,
        },
        detector_results=[
            {
                "detector_name": "statistical",
                "score": 0.91,
                "triggered_on": ["error_rate", "latency_p95"],
                "details": {},
            },
            {
                "detector_name": "isolation_forest",
                "score": 0.79,
                "triggered_on": ["error_rate"],
                "details": {},
            },
        ],
    )


@pytest.fixture
def sample_context():
    return ServiceContext(
        service_name="payment-service",
        recent_logs=[
            "[12:00:01] INFO  processing payment",
            "[12:00:02] ERROR DB connection timeout after 5000ms",
            "[12:00:03] ERROR DB connection timeout after 5000ms",
        ],
        error_logs=[
            "[12:00:02] ERROR DB connection timeout after 5000ms",
            "[12:00:03] ERROR DB connection timeout after 5000ms",
        ],
        metrics={"metrics": {"error_rate_5m": 0.41, "latency_p95_5m": 4.8}},
        dependencies=["postgres-primary", "redis-cache", "fraud-service"],
        dependents=["api-gateway", "order-service"],
        fetch_duration_ms=123.4,
    )


@pytest.fixture
def sample_analysis():
    return RootCauseAnalysis(
        root_cause="PostgreSQL primary connection pool exhausted due to slow queries under high load.",
        confidence=0.88,
        severity="high",
        affected_services=["payment-service", "order-service", "api-gateway"],
        affected_users_estimate="~2000 users",
        timeline=[
            "12:00:00 — Latency begins rising on payment-service",
            "12:00:02 — DB connection timeouts start appearing",
            "12:00:10 — Error rate crosses 40%",
        ],
        contributing_factors=[
            "Connection pool size too small for current traffic",
            "Slow DB query (missing index on orders.created_at)",
        ],
        recommended_action="Immediately scale connection pool and kill long-running queries.",
        remediation_steps=[
            "Run: SELECT pid, query FROM pg_stat_activity WHERE state='active';",
            "Kill slow queries: SELECT pg_cancel_backend(pid);",
            "Increase max_connections in postgres config",
            "Add index: CREATE INDEX ON orders(created_at);",
        ],
        escalate_to="database-team",
        llm_model="gpt-4o",
        prompt_tokens=1200,
        completion_tokens=380,
        analysis_duration_ms=3200.0,
    )


@pytest.fixture
def initial_state(sample_request) -> AgentState:
    return AgentState(
        request=sample_request,
        context=None,
        analysis=None,
        result=None,
        error="",
    )


# ================================================================== #
#  Model tests                                                         #
# ================================================================== #

class TestModels:
    def test_analysis_request_auto_timestamp(self, sample_request):
        assert sample_request.requested_at is not None

    def test_root_cause_auto_id(self, sample_analysis):
        assert sample_analysis.id
        assert len(sample_analysis.id) == 36

    def test_root_cause_auto_timestamp(self, sample_analysis):
        assert sample_analysis.analyzed_at is not None

    def test_confidence_clamped_above_1(self):
        a = RootCauseAnalysis(
            root_cause="x", confidence=1.5, severity="high",
            recommended_action="fix it",
        )
        assert a.confidence == 1.0

    def test_confidence_clamped_below_0(self):
        a = RootCauseAnalysis(
            root_cause="x", confidence=-0.3, severity="high",
            recommended_action="fix it",
        )
        assert a.confidence == 0.0

    def test_invalid_severity_defaults_to_medium(self):
        a = RootCauseAnalysis(
            root_cause="x", confidence=0.5, severity="catastrophic",
            recommended_action="fix it",
        )
        assert a.severity == "medium"

    def test_analysis_result_auto_id(self, sample_request, sample_context, sample_analysis):
        r = AnalysisResult(
            request=sample_request,
            context=sample_context,
            analysis=sample_analysis,
        )
        assert r.id
        assert r.success is True

    def test_service_context_defaults(self):
        ctx = ServiceContext(service_name="svc")
        assert ctx.recent_logs == []
        assert ctx.dependencies == []
        assert ctx.fetch_duration_ms == 0.0


# ================================================================== #
#  Prompt builder tests                                                #
# ================================================================== #

class TestPrompts:
    def test_system_prompt_not_empty(self):
        assert len(SYSTEM_PROMPT) > 100
        assert "JSON" in SYSTEM_PROMPT
        assert "root_cause" in SYSTEM_PROMPT

    def test_prompt_contains_service_name(self, sample_request, sample_context):
        prompt = build_analysis_prompt(
            service_name=sample_request.service_name,
            anomaly_score=sample_request.anomaly_score,
            severity=sample_request.severity,
            window_features=sample_request.window_features,
            detector_results=sample_request.detector_results,
            context=sample_context,
        )
        assert "payment-service" in prompt

    def test_prompt_contains_error_rate(self, sample_request, sample_context):
        prompt = build_analysis_prompt(
            service_name=sample_request.service_name,
            anomaly_score=sample_request.anomaly_score,
            severity=sample_request.severity,
            window_features=sample_request.window_features,
            detector_results=sample_request.detector_results,
            context=sample_context,
        )
        assert "42.0%" in prompt   # 0.42 → "42.0%"

    def test_prompt_contains_error_logs(self, sample_request, sample_context):
        prompt = build_analysis_prompt(
            service_name=sample_request.service_name,
            anomaly_score=sample_request.anomaly_score,
            severity=sample_request.severity,
            window_features=sample_request.window_features,
            detector_results=sample_request.detector_results,
            context=sample_context,
        )
        assert "DB connection timeout" in prompt

    def test_prompt_contains_detector_signals(self, sample_request, sample_context):
        prompt = build_analysis_prompt(
            service_name=sample_request.service_name,
            anomaly_score=sample_request.anomaly_score,
            severity=sample_request.severity,
            window_features=sample_request.window_features,
            detector_results=sample_request.detector_results,
            context=sample_context,
        )
        assert "statistical" in prompt
        assert "isolation_forest" in prompt

    def test_prompt_contains_dependencies(self, sample_request, sample_context):
        prompt = build_analysis_prompt(
            service_name=sample_request.service_name,
            anomaly_score=sample_request.anomaly_score,
            severity=sample_request.severity,
            window_features=sample_request.window_features,
            detector_results=sample_request.detector_results,
            context=sample_context,
        )
        assert "postgres-primary" in prompt

    def test_prompt_ends_with_json_instruction(self, sample_request, sample_context):
        prompt = build_analysis_prompt(
            service_name=sample_request.service_name,
            anomaly_score=sample_request.anomaly_score,
            severity=sample_request.severity,
            window_features=sample_request.window_features,
            detector_results=sample_request.detector_results,
            context=sample_context,
        )
        assert "JSON" in prompt[-200:]

    def test_empty_error_logs_handled(self, sample_request):
        ctx = ServiceContext(service_name="svc", error_logs=[])
        prompt = build_analysis_prompt(
            service_name="svc",
            anomaly_score=0.8,
            severity="high",
            window_features=sample_request.window_features,
            detector_results=[],
            context=ctx,
        )
        assert "none captured" in prompt


# ================================================================== #
#  Tool tests                                                          #
# ================================================================== #

class TestTools:
    def test_dependency_map_known_service(self):
        result = asyncio.get_event_loop().run_until_complete(
            get_dependency_map("payment-service")
        )
        assert "postgres-primary" in result["dependencies"]
        assert "api-gateway" in result["dependents"]

    def test_dependency_map_unknown_service(self):
        result = asyncio.get_event_loop().run_until_complete(
            get_dependency_map("unknown-xyz")
        )
        assert result["dependencies"] == []
        assert isinstance(result["dependents"], list)

    def test_depth_api_gateway_is_zero(self):
        assert _compute_depth("api-gateway") == 0

    def test_depth_payment_service(self):
        depth = _compute_depth("payment-service")
        assert depth >= 1   # at least one hop from gateway

    def test_dependency_map_has_impact_radius(self):
        result = asyncio.get_event_loop().run_until_complete(
            get_dependency_map("payment-service")
        )
        assert "impact_radius" in result
        assert result["impact_radius"] >= 0


# ================================================================== #
#  Agent node tests (mocked)                                           #
# ================================================================== #

class TestAgentNodes:

    @patch("src.analysis.agent.fetch_recent_logs", new_callable=AsyncMock)
    @patch("src.analysis.agent.get_service_metrics", new_callable=AsyncMock)
    @patch("src.analysis.agent.get_dependency_map", new_callable=AsyncMock)
    def test_fetch_context_node_builds_context(
        self, mock_deps, mock_metrics, mock_logs, initial_state
    ):
        mock_logs.return_value = {"logs": ["log1", "log2"], "error_count": 1}
        mock_metrics.return_value = {"metrics": {"error_rate_5m": 0.4}}
        mock_deps.return_value = {
            "dependencies": ["postgres-primary"],
            "dependents": ["api-gateway"],
        }

        result = asyncio.get_event_loop().run_until_complete(
            fetch_context_node(initial_state)
        )
        ctx = result["context"]
        assert isinstance(ctx, ServiceContext)
        assert ctx.service_name == "payment-service"
        assert ctx.recent_logs == ["log1", "log2"]
        assert "postgres-primary" in ctx.dependencies

    @patch("src.analysis.agent.fetch_recent_logs", new_callable=AsyncMock)
    @patch("src.analysis.agent.get_service_metrics", new_callable=AsyncMock)
    @patch("src.analysis.agent.get_dependency_map", new_callable=AsyncMock)
    def test_fetch_context_tool_failure_handled(
        self, mock_deps, mock_metrics, mock_logs, initial_state
    ):
        mock_logs.side_effect = Exception("DB down")
        mock_metrics.return_value = {"metrics": {}}
        mock_deps.return_value = {"dependencies": [], "dependents": []}

        result = asyncio.get_event_loop().run_until_complete(
            fetch_context_node(initial_state)
        )
        # Should not raise — context with empty logs
        assert result["context"].recent_logs == []

    @patch("src.analysis.agent.AsyncOpenAI")
    def test_llm_analyze_node_parses_json(
        self, MockOpenAI, initial_state, sample_context, sample_analysis
    ):
        state_with_ctx = {**initial_state, "context": sample_context}

        llm_json = json.dumps({
            "root_cause": "DB pool exhausted",
            "confidence": 0.88,
            "severity": "high",
            "affected_services": ["payment-service"],
            "affected_users_estimate": "~2000",
            "timeline": ["event 1", "event 2"],
            "contributing_factors": ["slow queries"],
            "recommended_action": "Scale connection pool",
            "remediation_steps": ["step 1"],
            "escalate_to": "database-team",
        })

        mock_response = MagicMock()
        mock_response.choices[0].message.content = llm_json
        mock_response.usage.prompt_tokens = 800
        mock_response.usage.completion_tokens = 200

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        MockOpenAI.return_value = mock_client

        result = asyncio.get_event_loop().run_until_complete(
            llm_analyze_node(state_with_ctx)
        )
        analysis = result["analysis"]
        assert isinstance(analysis, RootCauseAnalysis)
        assert analysis.root_cause == "DB pool exhausted"
        assert analysis.confidence == 0.88
        assert analysis.severity == "high"
        assert analysis.prompt_tokens == 800

    @patch("src.analysis.agent.AsyncOpenAI")
    def test_llm_analyze_node_handles_invalid_json(
        self, MockOpenAI, initial_state, sample_context
    ):
        state_with_ctx = {**initial_state, "context": sample_context}

        mock_response = MagicMock()
        mock_response.choices[0].message.content = "not json at all"
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 10

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        MockOpenAI.return_value = mock_client

        result = asyncio.get_event_loop().run_until_complete(
            llm_analyze_node(state_with_ctx)
        )
        # Fallback analysis returned — no exception raised
        assert result["analysis"].confidence == 0.0
        assert "error" in result and result["error"]

    @patch("src.analysis.agent.AsyncOpenAI")
    def test_llm_analyze_node_handles_api_error(
        self, MockOpenAI, initial_state, sample_context
    ):
        state_with_ctx = {**initial_state, "context": sample_context}
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=Exception("Rate limit exceeded")
        )
        MockOpenAI.return_value = mock_client

        result = asyncio.get_event_loop().run_until_complete(
            llm_analyze_node(state_with_ctx)
        )
        assert result["analysis"].root_cause.startswith("Analysis unavailable")
        assert "Rate limit" in result["error"]

    def test_format_result_node(
        self, initial_state, sample_context, sample_analysis
    ):
        state = {
            **initial_state,
            "context": sample_context,
            "analysis": sample_analysis,
            "error": "",
        }
        result = asyncio.get_event_loop().run_until_complete(
            format_result_node(state)
        )
        r = result["result"]
        assert isinstance(r, AnalysisResult)
        assert r.success is True
        assert r.analysis.root_cause == sample_analysis.root_cause

    def test_format_result_node_marks_failure(
        self, initial_state, sample_context, sample_analysis
    ):
        state = {
            **initial_state,
            "context": sample_context,
            "analysis": sample_analysis,
            "error": "LLM timeout",
        }
        result = asyncio.get_event_loop().run_until_complete(
            format_result_node(state)
        )
        assert result["result"].success is False
        assert "timeout" in result["result"].error_message


# ================================================================== #
#  Fallback tests                                                      #
# ================================================================== #

class TestFallback:
    def test_fallback_analysis_uses_request_severity(self, sample_request):
        fallback = _fallback_analysis(sample_request, "test error")
        assert fallback.severity == sample_request.severity
        assert fallback.confidence == 0.0
        assert "test error" in fallback.root_cause

    def test_fallback_analysis_has_service_in_affected(self, sample_request):
        fallback = _fallback_analysis(sample_request, "timeout")
        assert sample_request.service_name in fallback.affected_services
