"""
anomalies.py — Anomaly query endpoints.

GET /api/anomalies                  — recent anomaly results
GET /api/anomalies/summary          — score distribution and severity breakdown
GET /api/anomalies/{service}/trend  — score trend for one service (last N windows)
"""

from typing import Optional

from fastapi import APIRouter, Query, HTTPException

from src.analysis.db import fetch_recent as fetch_analyses, get_pool as get_analysis_pool
from src.ingestion.db import get_pool as get_ingestion_pool

router = APIRouter()


@router.get("")
async def list_anomalies(
    service_name: Optional[str] = Query(None),
    severity: Optional[str] = Query(None, description="low|medium|high|critical"),
    limit: int = Query(20, ge=1, le=200),
):
    """
    Return recent anomaly analysis results, newest first.
    These come from the analysis_results table written by the LLM agent.
    """
    rows = await fetch_analyses(
        limit=limit,
        service_name=service_name,
        severity=severity,
    )
    return {"anomalies": rows, "count": len(rows)}


@router.get("/summary")
async def anomaly_summary():
    """
    High-level anomaly stats for the dashboard summary section.
    Returns counts by severity and average confidence.
    """
    pool = await get_analysis_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM analysis_results")
        by_severity = await conn.fetch(
            """
            SELECT severity, COUNT(*) AS cnt,
                   ROUND(AVG(confidence)::NUMERIC, 3) AS avg_confidence
            FROM analysis_results
            GROUP BY severity
            """
        )
        last_24h = await conn.fetchval(
            "SELECT COUNT(*) FROM analysis_results WHERE analyzed_at > NOW() - INTERVAL '24 hours'"
        )
        top_services = await conn.fetch(
            """
            SELECT service_name, COUNT(*) AS anomaly_count
            FROM analysis_results
            WHERE analyzed_at > NOW() - INTERVAL '7 days'
            GROUP BY service_name
            ORDER BY anomaly_count DESC
            LIMIT 5
            """
        )

    return {
        "total_analyzed": total,
        "last_24h": last_24h,
        "by_severity": [
            {
                "severity": r["severity"],
                "count": r["cnt"],
                "avg_confidence": float(r["avg_confidence"]),
            }
            for r in by_severity
        ],
        "top_affected_services": [
            {"service": r["service_name"], "count": r["anomaly_count"]}
            for r in top_services
        ],
    }


@router.get("/{service_name}/trend")
async def service_trend(
    service_name: str,
    hours: int = Query(6, ge=1, le=72),
):
    """
    Anomaly score trend for one service over the last N hours.
    Returns a time series suitable for a line chart.
    """
    pool = await get_analysis_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT analyzed_at, anomaly_score, severity, confidence
            FROM analysis_results
            WHERE service_name = $1
              AND analyzed_at > NOW() - ($2 || ' hours')::INTERVAL
            ORDER BY analyzed_at ASC
            """,
            service_name, str(hours),
        )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No anomaly data found for '{service_name}' in last {hours}h",
        )
    return {
        "service_name": service_name,
        "hours": hours,
        "data_points": [
            {
                "timestamp": r["analyzed_at"].isoformat(),
                "score": round(float(r["anomaly_score"]), 4),
                "severity": r["severity"],
                "confidence": round(float(r["confidence"]), 3),
            }
            for r in rows
        ],
    }
