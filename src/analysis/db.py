"""
db.py — Persistence layer for AnalysisResult objects.

Creates and manages the analysis_results table.
Results are written here after each LLM analysis and
read by the dashboard API (Module 5).
"""

import os
import json
import logging
from typing import Optional

import asyncpg

from .models import AnalysisResult

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:password@localhost:5432/observability"
)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS analysis_results (
    id                  TEXT PRIMARY KEY,
    analyzed_at         TIMESTAMPTZ NOT NULL,
    service_name        TEXT NOT NULL,
    anomaly_id          TEXT NOT NULL,
    anomaly_score       FLOAT NOT NULL,
    severity            TEXT NOT NULL,
    root_cause          TEXT NOT NULL,
    confidence          FLOAT NOT NULL,
    affected_services   JSONB DEFAULT '[]',
    recommended_action  TEXT,
    remediation_steps   JSONB DEFAULT '[]',
    timeline            JSONB DEFAULT '[]',
    contributing_factors JSONB DEFAULT '[]',
    escalate_to         TEXT DEFAULT '',
    llm_model           TEXT,
    prompt_tokens       INT DEFAULT 0,
    completion_tokens   INT DEFAULT 0,
    analysis_duration_ms FLOAT DEFAULT 0,
    success             BOOLEAN DEFAULT TRUE,
    error_message       TEXT DEFAULT '',
    raw_result          JSONB
);

CREATE INDEX IF NOT EXISTS idx_analysis_service  ON analysis_results (service_name);
CREATE INDEX IF NOT EXISTS idx_analysis_at       ON analysis_results (analyzed_at DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_severity ON analysis_results (severity);
CREATE INDEX IF NOT EXISTS idx_analysis_anomaly  ON analysis_results (anomaly_id);
"""

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with _pool.acquire() as conn:
            await conn.execute(CREATE_TABLE_SQL)
        logger.info("Analysis DB pool ready")
    return _pool


async def save_result(result: AnalysisResult) -> None:
    """Persist an AnalysisResult to the database."""
    pool = await get_pool()
    a = result.analysis
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analysis_results (
                id, analyzed_at, service_name, anomaly_id, anomaly_score,
                severity, root_cause, confidence, affected_services,
                recommended_action, remediation_steps, timeline,
                contributing_factors, escalate_to, llm_model,
                prompt_tokens, completion_tokens, analysis_duration_ms,
                success, error_message, raw_result
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
                $11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21
            )
            ON CONFLICT (id) DO NOTHING
            """,
            result.id,
            a.analyzed_at,
            result.request.service_name,
            result.request.anomaly_id,
            result.request.anomaly_score,
            a.severity,
            a.root_cause,
            a.confidence,
            json.dumps(a.affected_services),
            a.recommended_action,
            json.dumps(a.remediation_steps),
            json.dumps(a.timeline),
            json.dumps(a.contributing_factors),
            a.escalate_to,
            a.llm_model,
            a.prompt_tokens,
            a.completion_tokens,
            a.analysis_duration_ms,
            result.success,
            result.error_message,
            result.model_dump_json(),
        )
    logger.debug("Saved analysis result %s", result.id)


async def fetch_recent(
    limit: int = 20,
    service_name: Optional[str] = None,
    severity: Optional[str] = None,
) -> list[dict]:
    """Fetch recent analysis results for the dashboard."""
    pool = await get_pool()
    conditions = []
    params: list = []
    i = 1
    if service_name:
        conditions.append(f"service_name = ${i}")
        params.append(service_name)
        i += 1
    if severity:
        conditions.append(f"severity = ${i}")
        params.append(severity)
        i += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    query = f"""
        SELECT id, analyzed_at, service_name, severity,
               root_cause, confidence, recommended_action,
               affected_services, escalate_to, success
        FROM analysis_results
        {where}
        ORDER BY analyzed_at DESC
        LIMIT ${i}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


async def get_by_id(result_id: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT raw_result FROM analysis_results WHERE id = $1",
            result_id,
        )
    if row:
        return json.loads(row["raw_result"])
    return None
