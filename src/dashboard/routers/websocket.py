"""
websocket.py — Real-time WebSocket feed for the dashboard.

WS /ws/live — Pushes a JSON payload every PUSH_INTERVAL_SEC seconds:
  {
    "type":      "live_update",
    "timestamp": "2024-01-15T12:00:00Z",
    "anomalies": [...],   # last 10 anomalies
    "alerts":    [...],   # last 5 alerts
    "summary":   {...},   # pipeline KPIs
  }

The frontend subscribes once on load and receives continuous updates
without polling. On disconnect the loop stops cleanly.

Connection management:
  - Each client gets its own push loop task
  - Slow clients: payload is sent with a timeout; skipped if client lags
  - Max 50 concurrent WebSocket clients (configurable)
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.analysis.db import fetch_recent as fetch_analyses
from src.alerting.db import fetch_alerts
from src.ingestion.db import get_pool as get_ingestion_pool

logger = logging.getLogger(__name__)

router = APIRouter()

PUSH_INTERVAL_SEC = float(os.getenv("WS_PUSH_INTERVAL", "5"))
MAX_CLIENTS       = int(os.getenv("WS_MAX_CLIENTS", "50"))
SEND_TIMEOUT_SEC  = 3.0

# Global client registry
_connected_clients: set[WebSocket] = set()


@router.websocket("/live")
async def websocket_live(ws: WebSocket):
    """
    Real-time anomaly dashboard feed.

    Connect:
        ws://localhost:8080/ws/live

    Receives JSON every PUSH_INTERVAL_SEC seconds.
    Send any message to get an immediate update.
    """
    if len(_connected_clients) >= MAX_CLIENTS:
        await ws.close(code=1013, reason="Server at capacity")
        return

    await ws.accept()
    _connected_clients.add(ws)
    client_id = id(ws)
    logger.info("WS client connected — id=%d total=%d", client_id, len(_connected_clients))

    try:
        # Send initial payload immediately on connect
        payload = await _build_payload()
        await asyncio.wait_for(ws.send_json(payload), timeout=SEND_TIMEOUT_SEC)

        # Push loop + receive loop run concurrently
        push_task = asyncio.create_task(_push_loop(ws, client_id))
        recv_task = asyncio.create_task(_receive_loop(ws, client_id))

        # Wait for either to finish (disconnect or error)
        done, pending = await asyncio.wait(
            [push_task, recv_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

    except WebSocketDisconnect:
        logger.info("WS client disconnected — id=%d", client_id)
    except Exception as exc:
        logger.error("WS error for client %d: %s", client_id, exc)
    finally:
        _connected_clients.discard(ws)
        logger.info("WS client removed — id=%d remaining=%d", client_id, len(_connected_clients))


async def _push_loop(ws: WebSocket, client_id: int):
    """Periodically push fresh data to the client."""
    while True:
        await asyncio.sleep(PUSH_INTERVAL_SEC)
        try:
            payload = await _build_payload()
            await asyncio.wait_for(ws.send_json(payload), timeout=SEND_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logger.warning("WS send timeout for client %d — skipping", client_id)
        except WebSocketDisconnect:
            break
        except Exception as exc:
            logger.error("WS push error for client %d: %s", client_id, exc)
            break


async def _receive_loop(ws: WebSocket, client_id: int):
    """
    Listen for client messages.
    Any message triggers an immediate update (manual refresh).
    """
    while True:
        try:
            _ = await ws.receive_text()
            payload = await _build_payload()
            await asyncio.wait_for(ws.send_json(payload), timeout=SEND_TIMEOUT_SEC)
        except WebSocketDisconnect:
            break
        except Exception:
            break


async def _build_payload() -> dict:
    """Fetch fresh data and build the push payload."""
    anomalies, alerts, log_count = await asyncio.gather(
        fetch_analyses(limit=10),
        fetch_alerts(limit=5),
        _log_count_1h(),
        return_exceptions=True,
    )

    def safe(v, default):
        return v if not isinstance(v, Exception) else default

    return {
        "type":      "live_update",
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "connected_clients": len(_connected_clients),
        "anomalies": _serialise(safe(anomalies, [])),
        "alerts":    _serialise(safe(alerts, [])),
        "summary": {
            "logs_last_1h": int(safe(log_count, 0) or 0),
        },
    }


async def _log_count_1h() -> int:
    pool = await get_ingestion_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM raw_logs WHERE timestamp > NOW() - INTERVAL '1 hour'"
        )


def _serialise(rows) -> list:
    """Convert asyncpg Records or dicts to JSON-safe list."""
    result = []
    for row in rows:
        d = dict(row) if not isinstance(row, dict) else row
        # Convert datetime to ISO string
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        result.append(d)
    return result


# ------------------------------------------------------------------ #
#  Broadcast helper (used by alerting engine to push immediately)      #
# ------------------------------------------------------------------ #

async def broadcast_anomaly(anomaly_data: dict):
    """
    Push an anomaly event to ALL connected WebSocket clients immediately.
    Called by the analysis worker when a new anomaly is processed.
    """
    if not _connected_clients:
        return
    payload = {
        "type":      "anomaly_alert",
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "anomaly":   anomaly_data,
    }
    dead = set()
    for ws in list(_connected_clients):
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=SEND_TIMEOUT_SEC)
        except Exception:
            dead.add(ws)
    _connected_clients -= dead
