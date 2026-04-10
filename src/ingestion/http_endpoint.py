"""
http_endpoint.py — FastAPI HTTP log ingestion endpoint.

Services that cannot use OTLP or Kafka directly can POST logs
here. The endpoint normalises, validates, and pushes to Kafka.

Mount this router in your main FastAPI app:
    from src.ingestion.http_endpoint import router
    app.include_router(router, prefix="/ingest")
"""

import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, HTTPException, BackgroundTasks, status
from pydantic import BaseModel

from .collector import LogCollector
from .kafka_producer import LogProducer
from .models import LogEntry, LogLevel

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ingestion"])

# Module-level singletons (initialised on first request)
_collector: LogCollector | None = None
_producer:  LogProducer  | None = None


def _get_collector() -> LogCollector:
    global _collector
    if _collector is None:
        _collector = LogCollector()
    return _collector


def _get_producer() -> LogProducer:
    global _producer
    if _producer is None:
        _producer = LogProducer()
    return _producer


# ------------------------------------------------------------------ #
#  Request schemas                                                     #
# ------------------------------------------------------------------ #

class RawLogRequest(BaseModel):
    timestamp:    datetime | None = None
    service_name: str
    level:        str = "INFO"
    message:      str
    trace_id:     str = ""
    span_id:      str = ""
    host:         str = ""
    environment:  str = "production"
    attributes:   dict = {}


class BatchLogRequest(BaseModel):
    logs: List[RawLogRequest]


# ------------------------------------------------------------------ #
#  Routes                                                              #
# ------------------------------------------------------------------ #

@router.post("/log", status_code=status.HTTP_202_ACCEPTED)
async def ingest_single_log(
    payload: RawLogRequest,
    background_tasks: BackgroundTasks,
):
    """
    Accept a single log entry and forward to Kafka asynchronously.

    Example:
        POST /ingest/log
        {
          "service_name": "payment-service",
          "level": "ERROR",
          "message": "Connection to DB timed out after 5000ms",
          "attributes": {"db_host": "postgres-primary", "timeout_ms": 5000}
        }
    """
    entry = _build_entry(payload)
    background_tasks.add_task(_send_to_kafka, entry)
    return {"accepted": True, "log_id": entry.id}


@router.post("/logs/batch", status_code=status.HTTP_202_ACCEPTED)
async def ingest_batch_logs(
    payload: BatchLogRequest,
    background_tasks: BackgroundTasks,
):
    """
    Accept up to 500 log entries in one request.
    All entries are forwarded to Kafka asynchronously.
    """
    if len(payload.logs) > 500:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Batch size exceeds maximum of 500",
        )
    entries = [_build_entry(log) for log in payload.logs]
    background_tasks.add_task(_send_batch_to_kafka, entries)
    return {"accepted": True, "count": len(entries)}


@router.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(tz=timezone.utc).isoformat()}


@router.get("/stats")
async def producer_stats():
    producer = _get_producer()
    return producer.stats


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def _build_entry(payload: RawLogRequest) -> LogEntry:
    collector = _get_collector()
    raw = payload.model_dump()
    if raw.get("timestamp") is None:
        raw["timestamp"] = datetime.now(tz=timezone.utc)
    entry = collector.normalise(raw)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Could not parse log entry",
        )
    return entry


async def _send_to_kafka(entry: LogEntry):
    producer = _get_producer()
    ok = await producer.send(entry)
    if not ok:
        logger.error("Failed to send log %s to Kafka", entry.id)


async def _send_batch_to_kafka(entries: list[LogEntry]):
    producer = _get_producer()
    result = await producer.send_batch(entries)
    logger.info("Batch kafka result: %s", result)
