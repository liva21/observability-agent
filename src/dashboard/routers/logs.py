"""
logs.py — Log query endpoints.

GET /api/logs              — paginated log list with filters
GET /api/logs/{id}         — single log by ID
GET /api/logs/stats        — log volume and level breakdown
GET /api/logs/services     — list of known services
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query, HTTPException

from src.ingestion.db import fetch_logs, get_pool
from src.dashboard.schemas import LogsResponse, LogStatsResponse, ServicesResponse

router = APIRouter()


@router.get("", response_model=LogsResponse)
async def list_logs(
    service_name: Optional[str] = Query(None, description="Filter by service"),
    level: Optional[str] = Query(None, description="DEBUG|INFO|WARNING|ERROR|CRITICAL"),
    limit: int = Query(100, ge=1, le=1000),
):
    """
    Return recent log entries, newest first.

    Example:
        GET /api/logs?service_name=payment-service&level=ERROR&limit=50
    """
    rows = await fetch_logs(
        service_name=service_name,
        level=level,
        limit=limit,
    )
    return {"logs": rows, "count": len(rows)}


@router.get("/stats", response_model=LogStatsResponse)
async def log_stats():
    """
    Return log volume and level breakdown for the last 24h.
    Used by the dashboard summary cards.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM raw_logs WHERE timestamp > NOW() - INTERVAL '24 hours'"
        )
        by_level = await conn.fetch(
            """
            SELECT level, COUNT(*) AS cnt
            FROM raw_logs
            WHERE timestamp > NOW() - INTERVAL '24 hours'
            GROUP BY level
            ORDER BY cnt DESC
            """
        )
        by_service = await conn.fetch(
            """
            SELECT service_name, COUNT(*) AS cnt
            FROM raw_logs
            WHERE timestamp > NOW() - INTERVAL '24 hours'
            GROUP BY service_name
            ORDER BY cnt DESC
            LIMIT 10
            """
        )
        error_rate = await conn.fetchval(
            """
            SELECT ROUND(
                COUNT(*) FILTER (WHERE level IN ('ERROR','CRITICAL'))::NUMERIC /
                NULLIF(COUNT(*), 0) * 100, 2
            )
            FROM raw_logs
            WHERE timestamp > NOW() - INTERVAL '1 hour'
            """
        )

    return {
        "last_24h": {
            "total": total,
            "error_rate_last_1h_pct": float(error_rate or 0),
            "by_level":   {r["level"]: r["cnt"] for r in by_level},
            "by_service": {r["service_name"]: r["cnt"] for r in by_service},
        }
    }


@router.get("/services", response_model=ServicesResponse)
async def list_services():
    """Return distinct service names seen in last 7 days."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT service_name
            FROM raw_logs
            WHERE timestamp > NOW() - INTERVAL '7 days'
            ORDER BY service_name
            """
        )
    return {"services": [r["service_name"] for r in rows]}


@router.get("/{log_id}")
async def get_log(log_id: str):
    """Return a single log entry by ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM raw_logs WHERE id = $1", log_id
        )
    if not row:
        raise HTTPException(status_code=404, detail="Log not found")
    return dict(row)
