"""
analysis.py — LLM analysis result endpoints.

GET /api/analysis           — recent analysis results
GET /api/analysis/{id}      — full analysis detail (raw JSON)
GET /api/analysis/confidence — confidence score distribution
"""

from typing import Optional

from fastapi import APIRouter, Query, HTTPException

from src.analysis.db import fetch_recent, get_by_id, get_pool
from src.dashboard.schemas import AnalysesResponse, ConfidenceResponse

router = APIRouter()


@router.get("", response_model=AnalysesResponse)
async def list_analyses(
    service_name: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=200),
):
    """
    Return recent LLM root cause analysis results, newest first.
    Each row includes root_cause, confidence, severity, recommended_action.
    """
    rows = await fetch_recent(
        limit=limit,
        service_name=service_name,
        severity=severity,
    )
    return {"analyses": rows, "count": len(rows)}


@router.get("/confidence", response_model=ConfidenceResponse)
async def confidence_distribution():
    """
    Return confidence score distribution as histogram buckets.
    Useful for evaluating LLM analysis quality over time.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                CASE
                    WHEN confidence < 0.3 THEN 'low (< 0.3)'
                    WHEN confidence < 0.6 THEN 'medium (0.3-0.6)'
                    WHEN confidence < 0.8 THEN 'high (0.6-0.8)'
                    ELSE 'very_high (>= 0.8)'
                END AS bucket,
                COUNT(*) AS cnt,
                ROUND(AVG(confidence)::NUMERIC, 3) AS avg_confidence
            FROM analysis_results
            GROUP BY bucket
            ORDER BY avg_confidence
            """
        )
        avg_overall = await conn.fetchval(
            "SELECT ROUND(AVG(confidence)::NUMERIC, 3) FROM analysis_results"
        )
        total = await conn.fetchval("SELECT COUNT(*) FROM analysis_results")

    return {
        "total_analyses": total,
        "average_confidence": float(avg_overall or 0),
        "distribution": [
            {
                "bucket": r["bucket"],
                "count": r["cnt"],
                "avg_confidence": float(r["avg_confidence"]),
            }
            for r in rows
        ],
    }


@router.get("/{analysis_id}")
async def get_analysis(analysis_id: str):
    """
    Return the full raw AnalysisResult JSON for one analysis.
    Includes ServiceContext (logs, metrics, dependencies) and
    RootCauseAnalysis (timeline, contributing_factors, steps).
    """
    result = await get_by_id(analysis_id)
    if not result:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return result
