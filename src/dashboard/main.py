"""
main.py — Dashboard FastAPI application.

Mounts:
  /api/logs       → log query endpoints
  /api/anomalies  → anomaly query + WebSocket live feed
  /api/alerts     → alert history + stats
  /api/analysis   → LLM analysis results
  /api/metrics    → system health summary
  /ws/live        → WebSocket: real-time anomaly push every 5s

Run:
    uvicorn src.dashboard.main:app --host 0.0.0.0 --port 8080 --reload
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import logs, anomalies, alerts, analysis, metrics, websocket
from src.ingestion.db import get_pool as get_ingestion_pool, close_pool as close_ingestion_pool
from src.alerting.db import get_pool as get_alerting_pool
from src.analysis.db import get_pool as get_analysis_pool

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: warm all DB pools
    await get_ingestion_pool()
    await get_alerting_pool()
    await get_analysis_pool()
    logger.info("Dashboard DB pools ready")
    yield
    # Shutdown
    await close_ingestion_pool()
    logger.info("Dashboard shut down")


app = FastAPI(
    title="Observability Agent — Dashboard API",
    version="1.0.0",
    description="Real-time log anomaly detection and root cause analysis dashboard.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers
app.include_router(logs.router,      prefix="/api/logs",     tags=["logs"])
app.include_router(anomalies.router, prefix="/api/anomalies",tags=["anomalies"])
app.include_router(alerts.router,    prefix="/api/alerts",   tags=["alerts"])
app.include_router(analysis.router,  prefix="/api/analysis", tags=["analysis"])
app.include_router(metrics.router,   prefix="/api/metrics",  tags=["metrics"])
app.include_router(websocket.router, prefix="/ws",           tags=["websocket"])


@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok", "service": "observability-dashboard"}


@app.get("/", tags=["system"])
async def root():
    return {
        "service": "Observability Agent Dashboard",
        "version": "1.0.0",
        "docs": "/docs",
        "websocket": "/ws/live",
    }
