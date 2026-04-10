"""
tools.py — LangGraph tool definitions for the LLM Analysis Agent.

Three tools the agent can call before producing a root cause analysis:

  1. fetch_recent_logs  — query PostgreSQL for recent log messages
  2. get_service_metrics — query Prometheus for numeric metrics
  3. get_dependency_map  — look up which services talk to which

Each tool is a plain async function. LangGraph calls them automatically
when the LLM requests them via tool_use.
"""

import os
import logging
import time
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import asyncpg
import httpx

logger = logging.getLogger(__name__)

DATABASE_URL  = os.getenv("DATABASE_URL",   "postgresql://postgres:password@localhost:5432/observability")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

# Hard-coded dependency graph (in production: load from service mesh / Consul)
# Format: service → list of services it calls
SERVICE_DEPENDENCIES: dict[str, list[str]] = {
    "api-gateway":       ["auth-service", "payment-service", "order-service"],
    "payment-service":   ["postgres-primary", "redis-cache", "fraud-service"],
    "order-service":     ["payment-service", "inventory-service", "postgres-primary"],
    "auth-service":      ["postgres-primary", "redis-cache"],
    "fraud-service":     ["ml-model-service", "postgres-primary"],
    "inventory-service": ["postgres-replica"],
}


# ------------------------------------------------------------------ #
#  Tool 1: fetch_recent_logs                                          #
# ------------------------------------------------------------------ #

async def fetch_recent_logs(
    service_name: str,
    minutes: int = 10,
    limit: int = 50,
    error_only: bool = False,
) -> dict:
    """
    Fetch recent log messages for a service from PostgreSQL.

    Args:
        service_name: Target service (exact match, lowercased)
        minutes: Look-back window in minutes
        limit: Max rows to return
        error_only: If True, return only ERROR and CRITICAL logs

    Returns:
        {"logs": [...], "total": N, "error_count": M, "duration_ms": X}
    """
    t0 = time.monotonic()
    since = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)

    level_filter = "AND level IN ('ERROR', 'CRITICAL')" if error_only else ""

    query = f"""
        SELECT timestamp, level, message, attributes
        FROM raw_logs
        WHERE service_name = $1
          AND timestamp >= $2
          {level_filter}
        ORDER BY timestamp DESC
        LIMIT $3
    """

    try:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            rows = await conn.fetch(query, service_name.lower(), since, limit)
        finally:
            await conn.close()

        logs = [
            f"[{r['timestamp'].strftime('%H:%M:%S')}] {r['level']} {r['message']}"
            for r in rows
        ]
        error_count = sum(1 for r in rows if r["level"] in ("ERROR", "CRITICAL"))

        return {
            "logs": logs,
            "total": len(logs),
            "error_count": error_count,
            "duration_ms": round((time.monotonic() - t0) * 1000, 1),
        }

    except Exception as exc:
        logger.error("fetch_recent_logs failed: %s", exc)
        return {
            "logs": [],
            "total": 0,
            "error_count": 0,
            "error": str(exc),
            "duration_ms": round((time.monotonic() - t0) * 1000, 1),
        }


# ------------------------------------------------------------------ #
#  Tool 2: get_service_metrics                                         #
# ------------------------------------------------------------------ #

# Prometheus queries per metric name
_PROM_QUERIES: dict[str, str] = {
    "error_rate_5m":     'rate(http_requests_total{{service="{svc}",status=~"5.."}}'
                         "[5m]) / rate(http_requests_total{{service=\"{svc}\"}}[5m])",
    "request_rate_5m":   'rate(http_requests_total{{service="{svc}"}}[5m])',
    "latency_p95_5m":    'histogram_quantile(0.95, rate(http_request_duration_seconds_bucket'
                         '{{service="{svc}"}}[5m]))',
    "latency_p99_5m":    'histogram_quantile(0.99, rate(http_request_duration_seconds_bucket'
                         '{{service="{svc}"}}[5m]))',
    "cpu_usage_pct":     'avg(rate(container_cpu_usage_seconds_total{{pod=~"{svc}-.*"}}[5m])) * 100',
    "memory_usage_mb":   'avg(container_memory_usage_bytes{{pod=~"{svc}-.*"}}) / 1048576',
    "active_connections": 'pg_stat_activity_count{{datname="observability"}}',
}


async def get_service_metrics(service_name: str) -> dict:
    """
    Query Prometheus for key metrics of a service.

    Returns:
        {"metrics": {name: value}, "duration_ms": X}
    """
    t0 = time.monotonic()
    metrics: dict[str, float] = {}

    async with httpx.AsyncClient(timeout=5.0) as client:
        tasks = {
            name: _query_prometheus(client, query.format(svc=service_name))
            for name, query in _PROM_QUERIES.items()
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    for name, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            logger.debug("Prometheus query '%s' failed: %s", name, result)
            metrics[name] = -1.0     # sentinel for "unavailable"
        elif result is not None:
            metrics[name] = result

    return {
        "metrics": metrics,
        "available": sum(1 for v in metrics.values() if v >= 0),
        "duration_ms": round((time.monotonic() - t0) * 1000, 1),
    }


async def _query_prometheus(client: httpx.AsyncClient, query: str) -> Optional[float]:
    try:
        resp = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
        )
        data = resp.json()
        result = data.get("data", {}).get("result", [])
        if result:
            return float(result[0]["value"][1])
        return None
    except Exception:
        return None


# ------------------------------------------------------------------ #
#  Tool 3: get_dependency_map                                          #
# ------------------------------------------------------------------ #

async def get_dependency_map(service_name: str) -> dict:
    """
    Return the dependency graph for a service:
      - dependencies: services this service calls
      - dependents: services that call this service

    Args:
        service_name: Target service name

    Returns:
        {"dependencies": [...], "dependents": [...], "depth": N}
    """
    svc = service_name.lower()

    dependencies = SERVICE_DEPENDENCIES.get(svc, [])

    dependents = [
        caller
        for caller, callees in SERVICE_DEPENDENCIES.items()
        if svc in callees
    ]

    # Transitive depth (how many hops from api-gateway)
    depth = _compute_depth(svc)

    return {
        "service": svc,
        "dependencies": dependencies,
        "dependents": dependents,
        "dependency_count": len(dependencies),
        "dependent_count": len(dependents),
        "depth_from_gateway": depth,
        "impact_radius": len(dependents),   # how many services are affected upstream
    }


def _compute_depth(service: str, visited: Optional[set] = None, depth: int = 0) -> int:
    """BFS depth from api-gateway to this service."""
    if service == "api-gateway":
        return 0
    if visited is None:
        visited = set()
    visited.add(service)
    for caller, callees in SERVICE_DEPENDENCIES.items():
        if service in callees and caller not in visited:
            return _compute_depth(caller, visited, depth + 1) + 1
    return depth
