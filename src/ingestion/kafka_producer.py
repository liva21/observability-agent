"""
kafka_producer.py — Async Kafka producer with retry & dead-letter queue.

Writes normalised LogEntry objects to a Kafka topic.
Failed messages are written to a dead-letter JSON file for debugging.
"""

import os
import json
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from confluent_kafka import Producer, KafkaException
from .models import LogEntry

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC     = os.getenv("KAFKA_LOG_TOPIC", "raw-logs")
DLQ_PATH        = Path(os.getenv("DLQ_PATH", "/tmp/dlq_logs.jsonl"))
MAX_RETRIES     = int(os.getenv("KAFKA_MAX_RETRIES", "3"))


class LogProducer:
    """
    Thread-safe Kafka producer for LogEntry objects.

    Usage:
        producer = LogProducer()
        await producer.send(log_entry)
        await producer.send_batch(log_entries)
        producer.flush()
    """

    def __init__(
        self,
        bootstrap_servers: str = KAFKA_BOOTSTRAP,
        topic: str = KAFKA_TOPIC,
    ):
        self.topic = topic
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap_servers,
                "acks": "all",                   # strongest durability
                "retries": MAX_RETRIES,
                "retry.backoff.ms": 300,
                "linger.ms": 5,                  # small batching window
                "compression.type": "snappy",
                "message.max.bytes": 1_048_576,  # 1 MB
            }
        )
        self._sent = 0
        self._failed = 0
        logger.info("LogProducer connected to %s → topic '%s'", bootstrap_servers, topic)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def send(self, log: LogEntry, retries: int = MAX_RETRIES) -> bool:
        """
        Async-friendly send. Runs the blocking produce() in a thread pool
        so it doesn't block the event loop.
        Returns True on success, False on permanent failure.
        """
        loop = asyncio.get_event_loop()
        for attempt in range(1, retries + 1):
            try:
                await loop.run_in_executor(None, self._produce_one, log)
                self._sent += 1
                return True
            except KafkaException as exc:
                logger.warning(
                    "Kafka send attempt %d/%d failed for %s: %s",
                    attempt, retries, log.id, exc,
                )
                if attempt < retries:
                    await asyncio.sleep(0.3 * attempt)

        self._failed += 1
        self._write_dlq(log)
        return False

    async def send_batch(self, logs: list[LogEntry]) -> dict:
        """
        Send a batch of LogEntry objects concurrently.
        Returns {"sent": N, "failed": M}.
        """
        results = await asyncio.gather(*[self.send(log) for log in logs])
        sent   = sum(results)
        failed = len(results) - sent
        logger.info("Batch complete — sent: %d, failed: %d", sent, failed)
        return {"sent": sent, "failed": failed}

    def flush(self, timeout: float = 10.0):
        """Block until all in-flight messages are delivered or timeout."""
        remaining = self._producer.flush(timeout)
        if remaining > 0:
            logger.warning("%d messages still in queue after flush", remaining)

    @property
    def stats(self) -> dict:
        return {"sent": self._sent, "failed": self._failed}

    # ------------------------------------------------------------------ #
    #  Private helpers                                                     #
    # ------------------------------------------------------------------ #

    def _produce_one(self, log: LogEntry):
        self._producer.produce(
            topic=self.topic,
            key=log.service_name.encode(),
            value=log.model_dump_json().encode(),
            headers={
                "content-type": b"application/json",
                "log-level":    log.level.value.encode(),
                "environment":  log.environment.encode(),
            },
            on_delivery=self._delivery_report,
        )
        self._producer.poll(0)   # trigger callbacks without blocking

    def _delivery_report(self, err, msg):
        if err:
            logger.error("Delivery failed — topic=%s, err=%s", msg.topic(), err)
        else:
            logger.debug(
                "Delivered — topic=%s partition=%d offset=%d",
                msg.topic(), msg.partition(), msg.offset(),
            )

    def _write_dlq(self, log: LogEntry):
        """Append failed log to dead-letter queue file for later replay."""
        try:
            DLQ_PATH.parent.mkdir(parents=True, exist_ok=True)
            with DLQ_PATH.open("a") as f:
                f.write(log.model_dump_json() + "\n")
            logger.info("Written to DLQ: %s", log.id)
        except Exception as exc:
            logger.error("DLQ write failed: %s", exc)
