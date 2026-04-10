"""
main.py — Observability Agent ingestion service entrypoint.

Starts two things concurrently:
  1. FastAPI HTTP server (uvicorn) for log ingestion via REST
  2. Kafka consumer loop for OTLP / producer-side logs

Run:
    python -m src.ingestion.main
or via Docker:
    docker-compose up ingestion
"""

import asyncio
import logging
import os
import signal

import uvicorn
from fastapi import FastAPI

from .http_endpoint import router as ingestion_router
from .kafka_consumer import LogConsumer
from .db import get_pool, close_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Observability Agent — Ingestion", version="1.0.0")
app.include_router(ingestion_router, prefix="/ingest")


@app.on_event("startup")
async def startup():
    await get_pool()      # warm the DB pool and create tables
    logger.info("DB pool ready")


@app.on_event("shutdown")
async def shutdown():
    await close_pool()
    logger.info("DB pool closed")


async def run_consumer():
    """Background task that runs the Kafka consumer."""
    consumer = LogConsumer()
    loop = asyncio.get_event_loop()

    def _stop(sig, frame):
        logger.info("Signal %s received — stopping consumer", sig)
        asyncio.ensure_future(consumer.stop())

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    await consumer.start()


async def run_all():
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8001")),
        loop="asyncio",
        log_level="info",
    )
    server = uvicorn.Server(config)
    await asyncio.gather(
        server.serve(),
        run_consumer(),
    )


if __name__ == "__main__":
    asyncio.run(run_all())
