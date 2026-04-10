"""
kafka_consumer.py — Async Kafka consumer with batch processing.

Reads LogEntry JSON messages from Kafka, deserialises them,
persists to PostgreSQL in bulk, and hands off to downstream
processors (anomaly detection queue hook).
"""

import os
import json
import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import Callable, Optional

from confluent_kafka import Consumer, KafkaError, KafkaException
from .models import LogEntry, LogLevel
from .db import insert_logs_bulk

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP   = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC       = os.getenv("KAFKA_LOG_TOPIC", "raw-logs")
KAFKA_GROUP_ID    = os.getenv("KAFKA_GROUP_ID", "observability-agent")
BATCH_SIZE        = int(os.getenv("CONSUMER_BATCH_SIZE", "50"))
BATCH_TIMEOUT_SEC = float(os.getenv("CONSUMER_BATCH_TIMEOUT", "2.0"))
POLL_TIMEOUT_SEC  = float(os.getenv("CONSUMER_POLL_TIMEOUT", "0.5"))


class LogConsumer:
    """
    Async-friendly Kafka consumer.

    Pulls messages in micro-batches, deserialises them to LogEntry,
    bulk-inserts into PostgreSQL, and optionally calls an on_batch
    callback (e.g. to push to the anomaly detection pipeline).

    Usage:
        consumer = LogConsumer(on_batch=my_callback)
        await consumer.start()          # blocks until stopped
        # or run as a task:
        task = asyncio.create_task(consumer.start())
        ...
        await consumer.stop()
    """

    def __init__(
        self,
        bootstrap_servers: str = KAFKA_BOOTSTRAP,
        topic: str = KAFKA_TOPIC,
        group_id: str = KAFKA_GROUP_ID,
        on_batch: Optional[Callable[[list[LogEntry]], None]] = None,
    ):
        self.topic    = topic
        self.on_batch = on_batch
        self._running = False
        self._consumer = Consumer(
            {
                "bootstrap.servers":  bootstrap_servers,
                "group.id":           group_id,
                "auto.offset.reset":  "earliest",
                "enable.auto.commit": False,    # manual commit after DB write
                "max.poll.interval.ms": 300_000,
                "session.timeout.ms":   30_000,
            }
        )
        self._processed = 0
        self._errors    = 0
        logger.info(
            "LogConsumer ready — topic: %s, group: %s, batch: %d",
            topic, group_id, BATCH_SIZE,
        )

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def start(self):
        """Subscribe and consume until stop() is called."""
        self._consumer.subscribe([self.topic])
        self._running = True
        logger.info("LogConsumer started — listening on '%s'", self.topic)

        loop = asyncio.get_event_loop()
        try:
            while self._running:
                batch = await loop.run_in_executor(None, self._poll_batch)
                if batch:
                    await self._process_batch(batch)
                else:
                    # No messages — short yield so other tasks can run
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            logger.info("LogConsumer task cancelled")
        finally:
            self._consumer.close()
            logger.info(
                "LogConsumer stopped — processed: %d, errors: %d",
                self._processed, self._errors,
            )

    async def stop(self):
        self._running = False

    @property
    def stats(self) -> dict:
        return {"processed": self._processed, "errors": self._errors}

    # ------------------------------------------------------------------ #
    #  Private helpers                                                     #
    # ------------------------------------------------------------------ #

    def _poll_batch(self) -> list:
        """
        Blocking poll loop that runs in a thread-pool executor.
        Accumulates up to BATCH_SIZE messages or BATCH_TIMEOUT_SEC seconds.
        Returns raw Kafka messages.
        """
        import time
        messages = []
        deadline = time.monotonic() + BATCH_TIMEOUT_SEC

        while len(messages) < BATCH_SIZE and time.monotonic() < deadline:
            msg = self._consumer.poll(POLL_TIMEOUT_SEC)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue   # end of partition — not an error
                logger.error("Kafka error: %s", msg.error())
                self._errors += 1
                continue
            messages.append(msg)

        return messages

    async def _process_batch(self, raw_messages: list):
        """Deserialise → persist → callback → commit."""
        entries: list[LogEntry] = []
        for msg in raw_messages:
            entry = self._deserialise(msg)
            if entry:
                entries.append(entry)

        if not entries:
            return

        # 1. Persist to PostgreSQL
        try:
            inserted = await insert_logs_bulk(entries)
            self._processed += inserted
            logger.debug("Inserted %d logs into DB", inserted)
        except Exception as exc:
            logger.error("DB insert failed: %s", exc)
            self._errors += len(entries)
            return

        # 2. Fire downstream callback (non-blocking)
        if self.on_batch:
            try:
                await self.on_batch(entries)
            except Exception as exc:
                logger.error("on_batch callback raised: %s", exc)

        # 3. Commit offsets only after successful processing
        self._consumer.commit(asynchronous=False)

    def _deserialise(self, msg) -> Optional[LogEntry]:
        try:
            data = json.loads(msg.value().decode())
            return LogEntry(**data)
        except Exception as exc:
            logger.warning(
                "Failed to deserialise message offset=%d: %s",
                msg.offset(), exc,
            )
            self._errors += 1
            return None
