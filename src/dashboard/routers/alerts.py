"""
alerts.py — Alert history and statistics endpoints.

GET /api/alerts         — alert list with filters
GET /api/alerts/stats   — counts by status and severity
GET /api/alerts/{id}    — single alert detail
"""

from typing import Optional

from fastapi import APIRouter, Query, HTTPException

from src.alerting.db import fetch_alerts, alert_stats, get_pool

router = APIRouter()


@router.get("")
async def list_alerts(
    service_name: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="pending|sent|failed|suppressed"),
    limit: int = Query(50, ge=1, le=500),
):
    """
    Return alert history, newest first.

    Example:
        GET /api/alerts?severity=critical&status=sent&limit=10
    """
    rows = await fetch_alerts(
        limit=limit,
        service_name=service_name,
        severity=severity,
        status=status,
    )
    return {"alerts": rows, "count": len(rows)}


@router.get("/stats")
async def get_alert_stats():
    """
    Aggregate alert statistics for the dashboard summary cards.
    Returns total, sent, suppressed, and breakdown by severity.
    """
    stats = await alert_stats()
    return stats


@router.get("/{alert_id}")
async def get_alert(alert_id: str):
    """Return full alert detail by ID."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM alerts WHERE id = $1", alert_id
        )
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    return dict(row)
