"""
agent.py — LangGraph-based LLM Analysis Agent.

Graph topology:
  START
    │
    ▼
  fetch_context   ← calls all three tools concurrently
    │
    ▼
  llm_analyze     ← sends context to GPT-4o, parses JSON output
    │
    ▼
  format_result   ← wraps into AnalysisResult
    │
    ▼
  END

State flows forward only. Each node receives the full state dict
and returns a partial update.
"""

import os
import json
import time
import logging
import asyncio
from datetime import datetime, timezone
from typing import TypedDict, Optional, Any

from openai import AsyncOpenAI

from .models import (
    AnalysisRequest,
    AnalysisResult,
    RootCauseAnalysis,
    ServiceContext,
)
from .tools import fetch_recent_logs, get_service_metrics, get_dependency_map
from .prompts import SYSTEM_PROMPT, build_analysis_prompt

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL      = os.getenv("LLM_MODEL", "gpt-4o")
MAX_TOKENS     = int(os.getenv("LLM_MAX_TOKENS", "1500"))
LLM_TIMEOUT    = float(os.getenv("LLM_TIMEOUT_SEC", "30"))


# ------------------------------------------------------------------ #
#  Agent State                                                         #
# ------------------------------------------------------------------ #

class AgentState(TypedDict):
    request:  AnalysisRequest
    context:  Optional[ServiceContext]
    analysis: Optional[RootCauseAnalysis]
    result:   Optional[AnalysisResult]
    error:    str


# ------------------------------------------------------------------ #
#  Node functions                                                      #
# ------------------------------------------------------------------ #

async def fetch_context_node(state: AgentState) -> dict:
    """
    Node 1 — Run all tool calls concurrently and build ServiceContext.
    """
    req = state["request"]
    svc = req.service_name

    t0 = time.monotonic()
    logs_task    = fetch_recent_logs(svc, minutes=10, limit=50)
    errors_task  = fetch_recent_logs(svc, minutes=10, limit=30, error_only=True)
    metrics_task = get_service_metrics(svc)
    deps_task    = get_dependency_map(svc)

    logs_data, errors_data, metrics_data, deps_data = await asyncio.gather(
        logs_task, errors_task, metrics_task, deps_task,
        return_exceptions=True,
    )

    def safe(data, default):
        return data if not isinstance(data, Exception) else default

    context = ServiceContext(
        service_name=svc,
        recent_logs=safe(logs_data,   {}).get("logs", []),
        error_logs=safe(errors_data,  {}).get("logs", []),
        metrics=safe(metrics_data,    {}),
        dependencies=safe(deps_data,  {}).get("dependencies", []),
        dependents=safe(deps_data,    {}).get("dependents", []),
        fetch_duration_ms=round((time.monotonic() - t0) * 1000, 1),
    )

    logger.info(
        "Context fetched for '%s' — logs=%d errors=%d deps=%d (%.0fms)",
        svc,
        len(context.recent_logs),
        len(context.error_logs),
        len(context.dependencies),
        context.fetch_duration_ms,
    )
    return {"context": context}


async def llm_analyze_node(state: AgentState) -> dict:
    """
    Node 2 — Build prompt and call the LLM. Parse JSON response.
    """
    req     = state["request"]
    context = state["context"]

    user_prompt = build_analysis_prompt(
        service_name=req.service_name,
        anomaly_score=req.anomaly_score,
        severity=req.severity,
        window_features=req.window_features,
        detector_results=req.detector_results,
        context=context,
    )

    t0 = time.monotonic()
    try:
        client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=LLM_TIMEOUT)
        response = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=MAX_TOKENS,
            temperature=0.1,          # low temp = more deterministic JSON
            response_format={"type": "json_object"},
        )

        raw_json = response.choices[0].message.content
        duration_ms = round((time.monotonic() - t0) * 1000, 1)

        data = json.loads(raw_json)
        analysis = RootCauseAnalysis(
            **data,
            llm_model=LLM_MODEL,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            analysis_duration_ms=duration_ms,
        )

        logger.info(
            "LLM analysis done — service=%s confidence=%.2f severity=%s (%.0fms)",
            req.service_name,
            analysis.confidence,
            analysis.severity,
            duration_ms,
        )
        return {"analysis": analysis, "error": ""}

    except json.JSONDecodeError as exc:
        logger.error("LLM returned invalid JSON: %s", exc)
        return {
            "analysis": _fallback_analysis(req, f"JSON parse error: {exc}"),
            "error": str(exc),
        }
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        return {
            "analysis": _fallback_analysis(req, str(exc)),
            "error": str(exc),
        }


async def format_result_node(state: AgentState) -> dict:
    """
    Node 3 — Wrap state into final AnalysisResult.
    """
    result = AnalysisResult(
        request=state["request"],
        context=state["context"],
        analysis=state["analysis"],
        success=not bool(state.get("error")),
        error_message=state.get("error", ""),
    )
    return {"result": result}


# ------------------------------------------------------------------ #
#  Graph builder                                                       #
# ------------------------------------------------------------------ #

def build_analysis_graph():
    """
    Build and compile the LangGraph StateGraph.

    Returns a compiled graph with an async ainvoke() method.

    Usage:
        graph = build_analysis_graph()
        final_state = await graph.ainvoke(initial_state)
        result = final_state["result"]
    """
    try:
        from langgraph.graph import StateGraph, END
    except ImportError:
        raise ImportError(
            "langgraph is required. Install with: pip install langgraph"
        )

    graph = StateGraph(AgentState)

    graph.add_node("fetch_context",  fetch_context_node)
    graph.add_node("llm_analyze",    llm_analyze_node)
    graph.add_node("format_result",  format_result_node)

    graph.set_entry_point("fetch_context")
    graph.add_edge("fetch_context", "llm_analyze")
    graph.add_edge("llm_analyze",   "format_result")
    graph.add_edge("format_result", END)

    return graph.compile()


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def _fallback_analysis(req: AnalysisRequest, reason: str) -> RootCauseAnalysis:
    """Returned when LLM call fails — prevents pipeline from crashing."""
    return RootCauseAnalysis(
        root_cause=f"Analysis unavailable — {reason}",
        confidence=0.0,
        severity=req.severity,
        affected_services=[req.service_name],
        recommended_action="Manual investigation required.",
        llm_model=LLM_MODEL,
    )
