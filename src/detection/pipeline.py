"""
pipeline.py — Detection pipeline orchestrator.

This is the on_batch callback registered with the Kafka consumer (Module 1).
For every batch of LogEntry objects it:

  1. Extracts WindowFeatures per service (FeatureExtractor)
  2. Runs ensemble anomaly detection (AnomalyDetector)
  3. Pushes anomalies to Redis queue (AnomalyQueue) for LLM agent

Usage:
    pipeline = DetectionPipeline()
    await pipeline.connect()

    # Register as Kafka consumer callback:
    consumer = LogConsumer(on_batch=pipeline.process_batch)
    await consumer.start()
"""

import logging
from datetime import datetime

from src.ingestion.models import LogEntry
from .feature_extractor import FeatureExtractor
from .anomaly_detector import AnomalyDetector
from .anomaly_queue import AnomalyQueue
from .models import AnomalyResult

logger = logging.getLogger(__name__)


class DetectionPipeline:
    """
    Stateful pipeline that accepts log batches and produces anomaly events.

    Thread-safety: designed for single asyncio event loop.
    """

    def __init__(self, window_seconds: int = 60):
        self.window_seconds  = window_seconds
        self.extractor       = FeatureExtractor()
        self.detector        = AnomalyDetector()
        self.queue           = AnomalyQueue()
        self._batches_seen   = 0
        self._anomalies_found = 0

    async def connect(self):
        """Call once before starting the consumer."""
        await self.queue.connect()
        logger.info("DetectionPipeline ready")

    async def close(self):
        await self.queue.close()

    # ------------------------------------------------------------------ #
    #  Main entry point — registered as on_batch callback                  #
    # ------------------------------------------------------------------ #

    async def process_batch(self, logs: list[LogEntry]):
        """
        Called by LogConsumer for every micro-batch.
        Runs the full detection pipeline and queues anomalies.
        """
        if not logs:
            return

        self._batches_seen += 1
        batch_id = self._batches_seen

        logger.debug("Pipeline batch #%d — %d logs", batch_id, len(logs))

        # Step 1: Feature extraction per service
        features_by_service = self.extractor.extract_by_service(
            logs, window_seconds=self.window_seconds
        )

        # Step 2: Anomaly detection
        results: list[AnomalyResult] = self.detector.analyze_batch(
            features_by_service
        )

        # Step 3: Queue anomalies
        anomalies = [r for r in results if r.is_anomaly]
        if anomalies:
            push_stats = await self.queue.push_batch(anomalies)
            self._anomalies_found += push_stats["pushed"]
            logger.info(
                "Batch #%d — anomalies: %d queued, %d deduplicated",
                batch_id, push_stats["pushed"], push_stats["deduplicated"],
            )

    # ------------------------------------------------------------------ #
    #  Observability                                                        #
    # ------------------------------------------------------------------ #

    async def stats(self) -> dict:
        queue_stats = await self.queue.stats()
        detector_stats = self.detector.stats()
        return {
            "pipeline": {
                "batches_processed": self._batches_seen,
                "anomalies_found": self._anomalies_found,
            },
            "detector": detector_stats,
            "queue": queue_stats,
        }
