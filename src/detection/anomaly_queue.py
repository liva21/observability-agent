"""
anomaly_queue.py — Redis-backed queue for anomaly events.

AnomalyResult objects with is_anomaly=True are pushed here.
The LLM Analysis Agent (Module 3) pops from this queue for deep analysis.

Features:
  - Deduplication: same (service, severity) within TTL_SECONDS won't re-queue
  - Priority: critical > high > medium > low (via sorted set score)
  - Stats endpoint for dashboard visibility
"""

import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

from .models import AnomalyResult

logger = logging.getLogger(__name__)

REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379/0")
QUEUE_KEY       = "anomaly:queue"           # Redis list (LPUSH / BRPOP)
DEDUP_PREFIX    = "anomaly:dedup:"          # key prefix for dedup locks
TTL_SECONDS     = int(os.getenv("ANOMALY_DEDUP_TTL", "300"))   # 5 minutes
MAX_QUEUE_SIZE  = int(os.getenv("ANOMALY_MAX_QUEUE", "1000"))

SEVERITY_PRIORITY = {"critical": 4, "high": 3, "medium": 2, "low": 1, "normal": 0}


class AnomalyQueue:
    """
    Async Redis queue for anomaly events.

    Usage:
        queue = AnomalyQueue()
        await queue.connect()

        pushed = await queue.push(anomaly_result)
        result = await queue.pop(timeout=5)

        await queue.close()
    """

    def __init__(self, redis_url: str = REDIS_URL):
        self._url    = redis_url
        self._redis: Optional[aioredis.Redis] = None

    async def connect(self):
        self._redis = await aioredis.from_url(
            self._url,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        logger.info("AnomalyQueue connected to Redis")

    async def close(self):
        if self._redis:
            await self._redis.aclose()

    # ------------------------------------------------------------------ #
    #  Core operations                                                     #
    # ------------------------------------------------------------------ #

    async def push(self, result: AnomalyResult) -> bool:
        """
        Push an AnomalyResult to the queue.
        Returns False if deduplicated (same event seen recently).
        Only pushes if is_anomaly=True and severity != normal/low.
        """
        if not result.is_anomaly:
            return False
        if result.severity in ("normal", "low"):
            return False

        redis = await self._get_redis()

        # Deduplication check
        dedup_key = f"{DEDUP_PREFIX}{result.service_name}:{result.severity}"
        if await redis.exists(dedup_key):
            logger.debug("Deduplicated anomaly for %s", result.service_name)
            return False

        # Queue size guard
        queue_size = await redis.llen(QUEUE_KEY)
        if queue_size >= MAX_QUEUE_SIZE:
            logger.warning("Anomaly queue full (%d items) — dropping event", queue_size)
            return False

        payload = result.model_dump_json()
        await redis.lpush(QUEUE_KEY, payload)
        await redis.setex(dedup_key, TTL_SECONDS, "1")

        logger.info(
            "Queued anomaly — service=%s severity=%s score=%.3f",
            result.service_name, result.severity, result.anomaly_score,
        )
        return True

    async def pop(self, timeout: int = 5) -> Optional[AnomalyResult]:
        """
        Blocking pop from the queue. Returns None on timeout.
        Used by the LLM Analysis Agent consumer loop.
        """
        redis = await self._get_redis()
        item = await redis.brpop(QUEUE_KEY, timeout=timeout)
        if item is None:
            return None
        _, payload = item
        try:
            data = json.loads(payload)
            return AnomalyResult(**data)
        except Exception as exc:
            logger.error("Failed to deserialise anomaly from queue: %s", exc)
            return None

    async def push_batch(self, results: list[AnomalyResult]) -> dict:
        """Push multiple results. Returns {"pushed": N, "deduplicated": M}."""
        pushed = 0
        deduped = 0
        for r in results:
            ok = await self.push(r)
            if ok:
                pushed += 1
            else:
                deduped += 1
        return {"pushed": pushed, "deduplicated": deduped}

    # ------------------------------------------------------------------ #
    #  Stats & inspection                                                  #
    # ------------------------------------------------------------------ #

    async def stats(self) -> dict:
        redis = await self._get_redis()
        queue_size = await redis.llen(QUEUE_KEY)
        dedup_keys = await redis.keys(f"{DEDUP_PREFIX}*")
        return {
            "queue_depth": queue_size,
            "active_dedup_locks": len(dedup_keys),
            "dedup_ttl_seconds": TTL_SECONDS,
            "max_queue_size": MAX_QUEUE_SIZE,
        }

    async def drain(self) -> list[AnomalyResult]:
        """Return all items currently in queue without blocking. For testing."""
        redis = await self._get_redis()
        items = await redis.lrange(QUEUE_KEY, 0, -1)
        results = []
        for payload in items:
            try:
                results.append(AnomalyResult(**json.loads(payload)))
            except Exception:
                pass
        return results

    # ------------------------------------------------------------------ #
    #  Private                                                             #
    # ------------------------------------------------------------------ #

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            await self.connect()
        return self._redis
