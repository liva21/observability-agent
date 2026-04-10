"""
worker.py — Analysis worker: pops AnomalyResult from Redis queue,
runs the LangGraph agent, persists the AnalysisResult.

Run standalone:
    python -m src.analysis.worker

Or integrated (started alongside ingestion + detection in docker-compose).
"""

import asyncio
import logging
import os
import signal

from src.detection.anomaly_queue import AnomalyQueue
from src.detection.models import AnomalyResult
from .models import AnalysisRequest
from .agent import build_analysis_graph, AgentState
from .db import save_result, get_pool

logger = logging.getLogger(__name__)

WORKER_CONCURRENCY = int(os.getenv("ANALYSIS_CONCURRENCY", "3"))
POP_TIMEOUT_SEC    = int(os.getenv("ANALYSIS_POP_TIMEOUT", "5"))


class AnalysisWorker:
    """
    Continuously pops anomalies from Redis and runs the LLM agent.

    Concurrency: up to WORKER_CONCURRENCY parallel analyses.
    Each analysis is independent; failures are logged and skipped.

    Usage:
        worker = AnalysisWorker()
        await worker.start()    # blocks until stopped
    """

    def __init__(self):
        self.queue   = AnomalyQueue()
        self.graph   = build_analysis_graph()
        self._sem    = asyncio.Semaphore(WORKER_CONCURRENCY)
        self._running = False
        self._processed = 0
        self._failed    = 0

    async def start(self):
        await self.queue.connect()
        await get_pool()   # warm DB pool
        self._running = True
        logger.info(
            "AnalysisWorker started — concurrency=%d", WORKER_CONCURRENCY
        )

        tasks = set()
        try:
            while self._running:
                anomaly = await self.queue.pop(timeout=POP_TIMEOUT_SEC)
                if anomaly is None:
                    continue   # timeout — loop back

                task = asyncio.create_task(self._process(anomaly))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

                # Prevent unbounded task growth
                if len(tasks) >= WORKER_CONCURRENCY * 2:
                    await asyncio.gather(*list(tasks)[:WORKER_CONCURRENCY])

        except asyncio.CancelledError:
            logger.info("AnalysisWorker cancelled")
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.queue.close()
            logger.info(
                "AnalysisWorker stopped — processed=%d failed=%d",
                self._processed, self._failed,
            )

    async def stop(self):
        self._running = False

    @property
    def stats(self) -> dict:
        return {
            "processed": self._processed,
            "failed": self._failed,
        }

    # ------------------------------------------------------------------ #
    #  Private                                                             #
    # ------------------------------------------------------------------ #

    async def _process(self, anomaly: AnomalyResult):
        async with self._sem:
            try:
                request = _to_request(anomaly)
                initial_state: AgentState = {
                    "request":  request,
                    "context":  None,
                    "analysis": None,
                    "result":   None,
                    "error":    "",
                }
                final_state = await self.graph.ainvoke(initial_state)
                result = final_state["result"]

                await save_result(result)
                self._processed += 1

                logger.info(
                    "Analysis complete — service=%s root_cause='%s' confidence=%.2f",
                    anomaly.service_name,
                    result.analysis.root_cause[:80],
                    result.analysis.confidence,
                )

            except Exception as exc:
                self._failed += 1
                logger.error(
                    "Analysis failed for anomaly %s: %s",
                    anomaly.id, exc,
                )


def _to_request(anomaly: AnomalyResult) -> AnalysisRequest:
    return AnalysisRequest(
        anomaly_id=anomaly.id,
        service_name=anomaly.service_name,
        anomaly_score=anomaly.anomaly_score,
        severity=anomaly.severity,
        window_features=anomaly.features.model_dump(),
        detector_results=[d.model_dump() for d in anomaly.detector_results],
    )


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    worker = AnalysisWorker()

    loop = asyncio.get_event_loop()

    def _stop(sig, frame):
        logger.info("Signal %s — stopping worker", sig)
        asyncio.ensure_future(worker.stop())

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    await worker.start()


if __name__ == "__main__":
    asyncio.run(main())
