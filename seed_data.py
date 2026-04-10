"""
seed_data.py — Demo verisi oluşturucu.

Tüm tabloları gerçekçi test verisiyle doldurur:
  - raw_logs          (500 log, 4 servis, çeşitli level'lar)
  - analysis_results  (20 anomali analizi, LLM sonuçları)
  - alerts            (15 alert, farklı severity ve status)

Çalıştır:
    python seed_data.py
"""

import asyncio
import asyncpg
import json
import uuid
import random
from datetime import datetime, timezone, timedelta

DATABASE_URL = "postgresql://postgres:password@localhost:5432/observability"

SERVICES = ["payment-service", "order-service", "auth-service", "api-gateway"]

ERROR_MESSAGES = [
    "DB connection timeout after 5000ms",
    "Connection pool exhausted: max_connections=20 reached",
    "HTTP 503 from downstream fraud-service",
    "Redis ECONNREFUSED 127.0.0.1:6379",
    "JWT verification failed: token expired",
    "Unhandled exception in payment processor",
    "Request timeout after 30s",
    "Circuit breaker OPEN for postgres-primary",
]

INFO_MESSAGES = [
    "Request processed successfully",
    "Payment authorized in 120ms",
    "User session created",
    "Cache hit for user:1234",
    "Order placed successfully",
    "Health check passed",
    "Metrics flushed to Prometheus",
]

ROOT_CAUSES = [
    ("DB connection pool exhausted due to slow queries under high load.",
     "high", 0.88, "database-team",
     ["Kill slow queries", "Increase max_connections to 50", "Add connection pool monitoring"]),
    ("Downstream fraud-service latency spike causing payment-service timeout cascade.",
     "high", 0.82, "on-call",
     ["Check fraud-service pod health", "Scale fraud-service replicas", "Enable circuit breaker"]),
    ("Redis cache eviction causing cache stampede on auth-service.",
     "medium", 0.74, "",
     ["Increase Redis maxmemory", "Implement cache warming", "Add jitter to TTL"]),
    ("Memory leak in order-service causing OOM kills every ~2 hours.",
     "critical", 0.91, "backend-team",
     ["Restart affected pods immediately", "Review recent memory changes", "Enable heap profiling"]),
    ("Increased error rate due to invalid JWT tokens after secret rotation.",
     "medium", 0.79, "",
     ["Verify secret rotation propagated to all pods", "Restart auth pods if needed"]),
]


async def seed(conn: asyncpg.Connection):
    now = datetime.now(tz=timezone.utc)

    # ── 1. raw_logs ──────────────────────────────────────────────────
    print("Seeding raw_logs...")
    log_rows = []
    for i in range(500):
        svc = random.choice(SERVICES)
        # Introduce a spike of errors in the last 30 minutes
        if i > 400:
            level = random.choices(
                ["ERROR", "CRITICAL", "WARNING", "INFO"],
                weights=[40, 10, 20, 30]
            )[0]
        else:
            level = random.choices(
                ["INFO", "WARNING", "ERROR", "DEBUG"],
                weights=[65, 15, 15, 5]
            )[0]

        msg = random.choice(ERROR_MESSAGES if level in ("ERROR", "CRITICAL") else INFO_MESSAGES)
        ts  = now - timedelta(seconds=random.randint(0, 3600))
        lat = random.uniform(50, 5000) if level in ("ERROR", "CRITICAL") else random.uniform(20, 300)

        log_rows.append((
            str(uuid.uuid4()), ts, svc, level, msg,
            str(uuid.uuid4()), str(uuid.uuid4())[:16],
            f"pod-{random.randint(1,5)}", "production",
            json.dumps({"latency_ms": round(lat, 1), "http_status": 500 if level == "ERROR" else 200}),
        ))

    await conn.executemany(
        """
        INSERT INTO raw_logs
          (id, timestamp, service_name, level, message,
           trace_id, span_id, host, environment, attributes)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT DO NOTHING
        """,
        log_rows,
    )
    print(f"  ✓ {len(log_rows)} logs inserted")

    # ── 2. analysis_results ──────────────────────────────────────────
    print("Seeding analysis_results...")
    analysis_rows = []
    for i in range(20):
        svc = random.choice(SERVICES)
        rc_idx = i % len(ROOT_CAUSES)
        rc, sev, conf, escalate, steps = ROOT_CAUSES[rc_idx]
        score = round(random.uniform(0.55, 0.95), 3)
        ts = now - timedelta(minutes=random.randint(0, 360))
        aid = str(uuid.uuid4())

        raw = {
            "id": aid,
            "request": {
                "anomaly_id": str(uuid.uuid4()),
                "service_name": svc,
                "anomaly_score": score,
                "severity": sev,
                "window_features": {
                    "error_rate": round(random.uniform(0.2, 0.9), 3),
                    "latency_p95": round(random.uniform(500, 8000), 1),
                    "window_seconds": 60,
                },
                "detector_results": [
                    {"detector_name": "statistical", "score": round(score - 0.05, 3), "triggered_on": ["error_rate"]},
                    {"detector_name": "isolation_forest", "score": round(score - 0.1, 3), "triggered_on": []},
                ],
            },
            "analysis": {
                "root_cause": rc,
                "confidence": conf,
                "severity": sev,
                "affected_services": [svc, "api-gateway"],
                "recommended_action": steps[0],
                "remediation_steps": steps,
                "escalate_to": escalate,
                "timeline": [
                    f"{(ts - timedelta(minutes=5)).strftime('%H:%M')} — Latency begins rising",
                    f"{(ts - timedelta(minutes=3)).strftime('%H:%M')} — Error rate crosses 20%",
                    f"{ts.strftime('%H:%M')} — Anomaly detected",
                ],
                "contributing_factors": ["high traffic", "missing index"],
                "llm_model": "gpt-4o",
                "prompt_tokens": random.randint(800, 1400),
                "completion_tokens": random.randint(200, 450),
                "analysis_duration_ms": round(random.uniform(1800, 4500), 1),
            },
        }

        analysis_rows.append((
            aid, ts, svc, raw["request"]["anomaly_id"], score,
            sev, rc, conf,
            json.dumps([svc, "api-gateway"]),
            steps[0],
            json.dumps(steps),
            json.dumps(raw["analysis"]["timeline"]),
            json.dumps(["high traffic", "missing index"]),
            escalate, "gpt-4o",
            raw["analysis"]["prompt_tokens"],
            raw["analysis"]["completion_tokens"],
            raw["analysis"]["analysis_duration_ms"],
            True, "",
            json.dumps(raw),
        ))

    await conn.executemany(
        """
        INSERT INTO analysis_results (
            id, analyzed_at, service_name, anomaly_id, anomaly_score,
            severity, root_cause, confidence, affected_services,
            recommended_action, remediation_steps, timeline,
            contributing_factors, escalate_to, llm_model,
            prompt_tokens, completion_tokens, analysis_duration_ms,
            success, error_message, raw_result
        ) VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
            $11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21
        )
        ON CONFLICT DO NOTHING
        """,
        analysis_rows,
    )
    print(f"  ✓ {len(analysis_rows)} analysis results inserted")

    # ── 3. alerts ────────────────────────────────────────────────────
    print("Seeding alerts...")
    statuses   = ["sent", "sent", "sent", "suppressed", "failed"]
    severities = ["critical", "high", "high", "medium", "medium", "low"]
    alert_rows = []

    for i in range(15):
        svc    = random.choice(SERVICES)
        sev    = random.choice(severities)
        status = random.choice(statuses)
        ts     = now - timedelta(minutes=random.randint(0, 480))
        sent_at = ts + timedelta(seconds=random.randint(2, 8)) if status == "sent" else None
        channels_done = ["slack"] if status == "sent" else []
        if sev == "critical" and status == "sent":
            channels_done = ["slack", "pagerduty"]
        rc, _, conf, escalate, steps = ROOT_CAUSES[i % len(ROOT_CAUSES)]

        alert_rows.append((
            str(uuid.uuid4()), ts, sent_at,
            str(uuid.uuid4()), str(uuid.uuid4()),
            svc, sev, status,
            f"[{sev.upper()}] Anomaly detected on {svc} (score={round(random.uniform(0.55,0.95),2)})",
            rc, conf, steps[0],
            json.dumps([svc, "api-gateway"]),
            json.dumps(steps),
            escalate,
            json.dumps(channels_done),
            json.dumps(channels_done),
            json.dumps([] if status != "failed" else ["slack: connection refused"]),
        ))

    await conn.executemany(
        """
        INSERT INTO alerts (
            id, created_at, sent_at, analysis_id, anomaly_id,
            service_name, severity, status, title, root_cause,
            confidence, recommended_action, affected_services,
            remediation_steps, escalate_to, channels_attempted,
            channels_succeeded, error_messages
        ) VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
            $11,$12,$13,$14,$15,$16,$17,$18
        )
        ON CONFLICT DO NOTHING
        """,
        alert_rows,
    )
    print(f"  ✓ {len(alert_rows)} alerts inserted")


async def main():
    print(f"Connecting to {DATABASE_URL}...")
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await seed(conn)
        print("\nSeed tamamlandı. Dashboard'u yenile: http://localhost:8080/static/index.html")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
