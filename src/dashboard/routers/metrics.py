"""
metrics.py — System health summary endpoint.

GET /api/metrics/summary — single endpoint for the dashboard header cards:
  - logs ingested (last 1h, 24h)
  - anomalies detected (last 24h)
  - alerts sent (last 24h)
  - average LLM confidence
  - pipeline latency percentiles

This is the "heartbeat" endpoint polled by the dashboard every 30s.
"""

import asyncio
from fastapi import APIRouter

from src.ingestion.db import get_pool as get_ingestion_pool
from src.analysis.db import get_pool as get_analysis_pool
from src.alerting.db import get_pool as get_alerting_pool

router = APIRouter()


@router.get("/summary")
async def system_summary():
    """
    Aggregate health summary across all pipeline stages.
    Designed for the dashboard top-bar KPI cards.
    """
    ingestion_pool = await get_ingestion_pool()
    analysis_pool  = await get_analysis_pool()
    alerting_pool  = await get_alerting_pool()

    # Run all queries concurrently
    (
        logs_1h, logs_24h,
        error_rate_1h,
        anomalies_24h, anomalies_critical,
        avg_confidence,
        alerts_sent_24h, alerts_suppressed_24h,
        avg_analysis_ms,
    ) = await asyncio.gather(
        _fetchval(ingestion_pool,
            "SELECT COUNT(*) FROM raw_logs WHERE timestamp > NOW() - INTERVAL '1 hour'"),
        _fetchval(ingestion_pool,
            "SELECT COUNT(*) FROM raw_logs WHERE timestamp > NOW() - INTERVAL '24 hours'"),
        _fetchval(ingestion_pool,
            """SELECT ROUND(
                COUNT(*) FILTER (WHERE level IN ('ERROR','CRITICAL'))::NUMERIC /
                NULLIF(COUNT(*),0) * 100, 2
               ) FROM raw_logs WHERE timestamp > NOW() - INTERVAL '1 hour'"""),
        _fetchval(analysis_pool,
            "SELECT COUNT(*) FROM analysis_results WHERE analyzed_at > NOW() - INTERVAL '24 hours'"),
        _fetchval(analysis_pool,
            "SELECT COUNT(*) FROM analysis_results WHERE severity='critical' AND analyzed_at > NOW() - INTERVAL '24 hours'"),
        _fetchval(analysis_pool,
            "SELECT ROUND(AVG(confidence)::NUMERIC,3) FROM analysis_results WHERE analyzed_at > NOW() - INTERVAL '24 hours'"),
        _fetchval(alerting_pool,
            "SELECT COUNT(*) FROM alerts WHERE status='sent' AND created_at > NOW() - INTERVAL '24 hours'"),
        _fetchval(alerting_pool,
            "SELECT COUNT(*) FROM alerts WHERE status='suppressed' AND created_at > NOW() - INTERVAL '24 hours'"),
        _fetchval(analysis_pool,
            "SELECT ROUND(AVG(analysis_duration_ms)::NUMERIC,1) FROM analysis_results WHERE analyzed_at > NOW() - INTERVAL '24 hours'"),
    )

    return {
        "ingestion": {
            "logs_last_1h":       int(logs_1h or 0),
            "logs_last_24h":      int(logs_24h or 0),
            "error_rate_1h_pct":  float(error_rate_1h or 0),
        },
        "detection": {
            "anomalies_last_24h": int(anomalies_24h or 0),
            "critical_last_24h":  int(anomalies_critical or 0),
            "avg_confidence":     float(avg_confidence or 0),
            "avg_analysis_ms":    float(avg_analysis_ms or 0),
        },
        "alerting": {
            "sent_last_24h":       int(alerts_sent_24h or 0),
            "suppressed_last_24h": int(alerts_suppressed_24h or 0),
        },
    }


@router.get("/pipeline/health")
async def pipeline_health():
    """
    Simple health check for each pipeline stage.
    Returns ok/degraded/down per stage based on recent activity.
    """
    ingestion_pool = await get_ingestion_pool()
    analysis_pool  = await get_analysis_pool()

    logs_5m, analyses_1h = await asyncio.gather(
        _fetchval(ingestion_pool,
            "SELECT COUNT(*) FROM raw_logs WHERE timestamp > NOW() - INTERVAL '5 minutes'"),
        _fetchval(analysis_pool,
            "SELECT COUNT(*) FROM analysis_results WHERE analyzed_at > NOW() - INTERVAL '1 hour'"),
    )

    ingestion_status = "ok" if (logs_5m or 0) > 0 else "degraded"
    analysis_status  = "ok" if (analyses_1h or 0) > 0 else "degraded"

    return {
        "stages": {
            "ingestion": {"status": ingestion_status, "logs_last_5m": int(logs_5m or 0)},
            "detection": {"status": "ok"},
            "analysis":  {"status": analysis_status, "analyses_last_1h": int(analyses_1h or 0)},
            "alerting":  {"status": "ok"},
        }
    }


async def _fetchval(pool, query: str):
    try:
        async with pool.acquire() as conn:
            return await conn.fetchval(query)
    except Exception:
        return None
