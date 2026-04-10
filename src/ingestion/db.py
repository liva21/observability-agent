import os
import asyncpg
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:password@localhost:5432/observability"
)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS raw_logs (
    id           TEXT PRIMARY KEY,
    timestamp    TIMESTAMPTZ NOT NULL,
    service_name TEXT NOT NULL,
    level        TEXT NOT NULL,
    message      TEXT NOT NULL,
    trace_id     TEXT NOT NULL,
    span_id      TEXT NOT NULL,
    host         TEXT,
    environment  TEXT DEFAULT 'production',
    attributes   JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_raw_logs_timestamp    ON raw_logs (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_raw_logs_service_name ON raw_logs (service_name);
CREATE INDEX IF NOT EXISTS idx_raw_logs_level        ON raw_logs (level);
CREATE INDEX IF NOT EXISTS idx_raw_logs_trace_id     ON raw_logs (trace_id);
"""


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        async with _pool.acquire() as conn:
            await conn.execute(CREATE_TABLE_SQL)
        logger.info("Database pool created and tables initialised")
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed")


async def insert_log(log) -> None:
    """Insert a single LogEntry into raw_logs."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO raw_logs
                (id, timestamp, service_name, level, message,
                 trace_id, span_id, host, environment, attributes)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (id) DO NOTHING
            """,
            log.id,
            log.timestamp,
            log.service_name,
            log.level.value,
            log.message,
            log.trace_id,
            log.span_id,
            log.host,
            log.environment,
            log.attributes,
        )


async def insert_logs_bulk(logs: list) -> int:
    """Bulk insert a list of LogEntry objects. Returns inserted count."""
    if not logs:
        return 0
    pool = await get_pool()
    rows = [
        (
            log.id, log.timestamp, log.service_name, log.level.value,
            log.message, log.trace_id, log.span_id, log.host,
            log.environment, log.attributes,
        )
        for log in logs
    ]
    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO raw_logs
                (id, timestamp, service_name, level, message,
                 trace_id, span_id, host, environment, attributes)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (id) DO NOTHING
            """,
            rows,
        )
    return len(rows)


async def fetch_logs(
    service_name: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    pool = await get_pool()
    conditions = []
    params: list = []
    i = 1
    if service_name:
        conditions.append(f"service_name = ${i}")
        params.append(service_name)
        i += 1
    if level:
        conditions.append(f"level = ${i}")
        params.append(level)
        i += 1
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    query = f"""
        SELECT * FROM raw_logs
        {where}
        ORDER BY timestamp DESC
        LIMIT ${i}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]
