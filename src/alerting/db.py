"""
db.py — Alert persistence layer.

Stores every Alert (sent, failed, suppressed) to PostgreSQL.
Used by the dashboard for alert history and metrics.
"""

import os
import json
import logging
from typing import Optional

import asyncpg

from .models import Alert, AlertStatus

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:password@localhost:5432/observability"
)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS alerts (
    id                  TEXT PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL,
    sent_at             TIMESTAMPTZ,
    analysis_id         TEXT NOT NULL,
    anomaly_id          TEXT NOT NULL,
    service_name        TEXT NOT NULL,
    severity            TEXT NOT NULL,
    status              TEXT NOT NULL,
    title               TEXT NOT NULL,
    root_cause          TEXT NOT NULL,
    confidence          FLOAT NOT NULL,
    recommended_action  TEXT,
    affected_services   JSONB DEFAULT '[]',
    remediation_steps   JSONB DEFAULT '[]',
    escalate_to         TEXT DEFAULT '',
    channels_attempted  JSONB DEFAULT '[]',
    channels_succeeded  JSONB DEFAULT '[]',
    error_messages      JSONB DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_alerts_created_at   ON alerts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_service_name ON alerts (service_name);
CREATE INDEX IF NOT EXISTS idx_alerts_severity     ON alerts (severity);
CREATE INDEX IF NOT EXISTS idx_alerts_status       ON alerts (status);
"""

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with _pool.acquire() as conn:
            await conn.execute(CREATE_TABLE_SQL)
        logger.info("Alert DB pool ready")
    return _pool


async def save_alert(alert: Alert) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO alerts (
                id, created_at, sent_at, analysis_id, anomaly_id,
                service_name, severity, status, title, root_cause,
                confidence, recommended_action, affected_services,
                remediation_steps, escalate_to, channels_attempted,
                channels_succeeded, error_messages
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
                $11,$12,$13,$14,$15,$16,$17,$18
            )
            ON CONFLICT (id) DO UPDATE SET
                status             = EXCLUDED.status,
                sent_at            = EXCLUDED.sent_at,
                channels_succeeded = EXCLUDED.channels_succeeded,
                error_messages     = EXCLUDED.error_messages
            """,
            alert.id,
            alert.created_at,
            alert.sent_at,
            alert.analysis_id,
            alert.anomaly_id,
            alert.service_name,
            alert.severity.value,
            alert.status.value,
            alert.title,
            alert.root_cause,
            alert.confidence,
            alert.recommended_action,
            json.dumps(alert.affected_services),
            json.dumps(alert.remediation_steps),
            alert.escalate_to,
            json.dumps(alert.channels_attempted),
            json.dumps(alert.channels_succeeded),
            json.dumps(alert.error_messages),
        )
    logger.debug("Saved alert %s status=%s", alert.id, alert.status.value)


async def fetch_alerts(
    limit: int = 50,
    service_name: Optional[str] = None,
    severity: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    pool = await get_pool()
    conditions, params = [], []
    i = 1
    for col, val in [
        ("service_name", service_name),
        ("severity", severity),
        ("status", status),
    ]:
        if val:
            conditions.append(f"{col} = ${i}")
            params.append(val)
            i += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    query = f"""
        SELECT * FROM alerts
        {where}
        ORDER BY created_at DESC
        LIMIT ${i}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


async def alert_stats() -> dict:
    """Aggregate stats for the dashboard."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM alerts")
        sent  = await conn.fetchval(
            "SELECT COUNT(*) FROM alerts WHERE status = 'sent'"
        )
        by_sev = await conn.fetch(
            "SELECT severity, COUNT(*) AS cnt FROM alerts GROUP BY severity"
        )
    return {
        "total": total,
        "sent": sent,
        "suppressed": total - sent,
        "by_severity": {r["severity"]: r["cnt"] for r in by_sev},
    }
