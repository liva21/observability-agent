"""
demo_simulator.py — Canlı demo simülatörü.

Gerçek bir microservice ortamını simüle eder:
  - Her servis için düzenli aralıklarla normal log akışı
  - Rastgele anomali senaryoları (DB spike, memory leak, cascade failure)
  - Anomali sonrası otomatik "recovery" dönemi
  - Terminal'de renkli gerçek zamanlı özet

Çalıştır:
    python demo_simulator.py

Durdurmak için Ctrl+C.
"""

import asyncio
import httpx
import random
import time
import signal
import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field

INGEST_URL  = "http://localhost:8001/ingest/log"
BATCH_URL   = "http://localhost:8001/ingest/logs/batch"
INTERVAL_MS = 300          # normal log aralığı (ms)
ANOMALY_EVERY_SEC = 45     # kaç saniyede bir anomali senaryosu tetikle

# ANSI renk kodları
R = "\033[91m"; Y = "\033[93m"; G = "\033[92m"
B = "\033[94m"; M = "\033[95m"; C = "\033[96m"
W = "\033[97m"; DIM = "\033[2m"; RESET = "\033[0m"; BOLD = "\033[1m"

SERVICES = [
    "payment-service",
    "order-service",
    "auth-service",
    "api-gateway",
    "fraud-service",
    "inventory-service",
]

# ── Normal log templates ─────────────────────────────────────────────

NORMAL_TEMPLATES = [
    ("INFO",  "Request processed successfully",                    {}),
    ("INFO",  "Payment authorized",                                {"latency_ms": None}),
    ("INFO",  "Cache hit for session token",                       {"cache_hit": True}),
    ("INFO",  "Order placed successfully",                         {"order_id": None}),
    ("INFO",  "User authenticated via JWT",                        {"latency_ms": None}),
    ("DEBUG", "Health check passed",                               {}),
    ("DEBUG", "Metrics flushed to Prometheus",                     {}),
    ("WARNING", "Slow DB query detected",                          {"query_ms": None}),
    ("WARNING", "Memory usage at 72%",                             {"memory_pct": 72}),
]

# ── Anomaly scenarios ────────────────────────────────────────────────

@dataclass
class Scenario:
    name: str
    service: str
    color: str
    duration_sec: int
    logs: list[tuple]           # (level, message, attributes_template)
    recovery_logs: list[tuple]

SCENARIOS = [
    Scenario(
        name="DB Connection Pool Exhausted",
        service="payment-service",
        color=R,
        duration_sec=20,
        logs=[
            ("ERROR",    "DB connection timeout after 5000ms",          {"latency_ms": 5000, "db_host": "postgres-primary"}),
            ("ERROR",    "Connection pool exhausted: max_connections=20",{"pool_size": 20, "waiting": 15}),
            ("CRITICAL", "Circuit breaker OPEN for postgres-primary",    {"failures": 10}),
            ("ERROR",    "Payment processing failed: DB unavailable",    {"latency_ms": 5001}),
            ("ERROR",    "DB connection timeout after 5000ms",           {"latency_ms": 5000}),
        ],
        recovery_logs=[
            ("INFO", "DB connection restored",                 {"latency_ms": 45}),
            ("INFO", "Circuit breaker CLOSED",                 {}),
            ("INFO", "Connection pool healthy: 18/20 free",   {"pool_free": 18}),
        ],
    ),
    Scenario(
        name="Memory Leak → OOM",
        service="order-service",
        color=Y,
        duration_sec=25,
        logs=[
            ("WARNING", "Memory usage at 85%",           {"memory_pct": 85}),
            ("WARNING", "Memory usage at 91%",           {"memory_pct": 91}),
            ("ERROR",   "GC pause 4200ms — heap full",   {"gc_pause_ms": 4200}),
            ("CRITICAL","OOM kill — pod restarting",     {"memory_pct": 99}),
            ("ERROR",   "Service unavailable during restart", {"latency_ms": 9999}),
        ],
        recovery_logs=[
            ("INFO", "Pod restarted successfully",        {}),
            ("INFO", "Memory usage at 41% (fresh start)", {"memory_pct": 41}),
            ("INFO", "Service healthy after restart",     {}),
        ],
    ),
    Scenario(
        name="Cascade Failure: fraud-service",
        service="api-gateway",
        color=M,
        duration_sec=20,
        logs=[
            ("ERROR",   "HTTP 503 from fraud-service (attempt 1/3)",  {"upstream": "fraud-service", "latency_ms": 3000}),
            ("ERROR",   "HTTP 503 from fraud-service (attempt 2/3)",  {"upstream": "fraud-service", "latency_ms": 3001}),
            ("ERROR",   "HTTP 503 from fraud-service (attempt 3/3)",  {"upstream": "fraud-service", "latency_ms": 3002}),
            ("CRITICAL","All retries exhausted — returning 503 to client", {"error": "upstream_unavailable"}),
            ("ERROR",   "Request queue growing: 847 pending",         {"queue_depth": 847}),
        ],
        recovery_logs=[
            ("INFO", "fraud-service responding again",   {"latency_ms": 120}),
            ("INFO", "Request queue draining: 120 left", {"queue_depth": 120}),
            ("INFO", "All upstream services healthy",    {}),
        ],
    ),
    Scenario(
        name="Redis Cache Stampede",
        service="auth-service",
        color=C,
        duration_sec=15,
        logs=[
            ("WARNING", "Cache miss rate 94% — stampede detected",  {"miss_rate": 0.94}),
            ("ERROR",   "Redis TIMEOUT after 2000ms",               {"latency_ms": 2000}),
            ("ERROR",   "Redis TIMEOUT after 2000ms",               {"latency_ms": 2001}),
            ("WARNING", "Fallback to DB: 340 req/s hitting postgres",{"db_rps": 340}),
            ("ERROR",   "DB connection pool 95% utilised",          {"pool_utilised": 0.95}),
        ],
        recovery_logs=[
            ("INFO", "Cache warming complete — hit rate 89%",  {"hit_rate": 0.89}),
            ("INFO", "Redis latency back to normal: 3ms",      {"latency_ms": 3}),
            ("INFO", "DB load returning to baseline",          {}),
        ],
    ),
    Scenario(
        name="High Latency Spike",
        service="inventory-service",
        color=B,
        duration_sec=18,
        logs=[
            ("WARNING", "p95 latency 1800ms — threshold 500ms",  {"latency_ms": 1800, "threshold_ms": 500}),
            ("ERROR",   "Request timeout: 30s exceeded",          {"latency_ms": 30001}),
            ("ERROR",   "Request timeout: 30s exceeded",          {"latency_ms": 30001}),
            ("WARNING", "Slow SQL: SELECT * FROM inventory ORDER BY...", {"query_ms": 4500}),
            ("ERROR",   "p99 latency 8200ms",                    {"latency_ms": 8200}),
        ],
        recovery_logs=[
            ("INFO", "DB index rebuild complete",              {}),
            ("INFO", "p95 latency back to 120ms",             {"latency_ms": 120}),
            ("INFO", "All queries within SLA",                {}),
        ],
    ),
]


# ── State ────────────────────────────────────────────────────────────

@dataclass
class SimState:
    total_sent:    int = 0
    total_errors:  int = 0
    anomalies_triggered: int = 0
    start_time:    float = field(default_factory=time.monotonic)
    active_scenario: str = ""
    running:       bool = True

    def elapsed(self) -> str:
        s = int(time.monotonic() - self.start_time)
        return f"{s//60:02d}:{s%60:02d}"

    def rps(self) -> float:
        elapsed = max(time.monotonic() - self.start_time, 1)
        return round(self.total_sent / elapsed, 1)


# ── HTTP helpers ─────────────────────────────────────────────────────

async def send_log(client: httpx.AsyncClient, state: SimState, payload: dict):
    try:
        r = await client.post(INGEST_URL, json=payload, timeout=3)
        if r.status_code == 202:
            state.total_sent += 1
        else:
            state.total_errors += 1
    except Exception:
        state.total_errors += 1


async def send_batch(client: httpx.AsyncClient, state: SimState, logs: list[dict]):
    try:
        r = await client.post(BATCH_URL, json={"logs": logs}, timeout=5)
        if r.status_code == 202:
            state.total_sent += len(logs)
        else:
            state.total_errors += len(logs)
    except Exception:
        state.total_errors += len(logs)


# ── Log builders ─────────────────────────────────────────────────────

def normal_log(service: str) -> dict:
    level, msg, attrs = random.choice(NORMAL_TEMPLATES)
    attrs = dict(attrs)
    if "latency_ms" in attrs and attrs["latency_ms"] is None:
        attrs["latency_ms"] = round(random.uniform(20, 350), 1)
    if "query_ms" in attrs and attrs["query_ms"] is None:
        attrs["query_ms"] = round(random.uniform(800, 1500), 1)
    if "order_id" in attrs and attrs["order_id"] is None:
        attrs["order_id"] = f"ORD-{random.randint(10000,99999)}"
    return {
        "service_name": service,
        "level": level,
        "message": msg,
        "environment": "production",
        "host": f"pod-{random.randint(1,6)}",
        "attributes": attrs,
    }


def anomaly_log(service: str, level: str, message: str, attrs: dict) -> dict:
    return {
        "service_name": service,
        "level": level,
        "message": message,
        "environment": "production",
        "host": f"pod-{random.randint(1,3)}",
        "attributes": attrs,
    }


# ── Producers ────────────────────────────────────────────────────────

async def normal_traffic_producer(client: httpx.AsyncClient, state: SimState):
    """Continuously sends normal logs for all services."""
    while state.running:
        # Send a small batch every INTERVAL_MS
        batch = [normal_log(random.choice(SERVICES)) for _ in range(3)]
        await send_batch(client, state, batch)
        await asyncio.sleep(INTERVAL_MS / 1000)


async def anomaly_producer(client: httpx.AsyncClient, state: SimState):
    """Periodically triggers an anomaly scenario."""
    await asyncio.sleep(10)   # warm-up pause

    while state.running:
        await asyncio.sleep(ANOMALY_EVERY_SEC)
        if not state.running:
            break

        scenario = random.choice(SCENARIOS)
        state.active_scenario = scenario.name
        state.anomalies_triggered += 1

        print(f"\n{BOLD}{scenario.color}{'─'*60}{RESET}")
        print(f"{BOLD}{scenario.color}  ANOMALY: {scenario.name}{RESET}")
        print(f"{scenario.color}  Service: {scenario.service}  |  Duration: {scenario.duration_sec}s{RESET}")
        print(f"{scenario.color}{'─'*60}{RESET}\n")

        # Send anomaly logs spaced over the scenario duration
        step = scenario.duration_sec / len(scenario.logs)
        for level, msg, attrs in scenario.logs:
            if not state.running:
                break
            log = anomaly_log(scenario.service, level, msg, attrs)
            await send_log(client, state, log)
            color = R if level == "CRITICAL" else Y if level == "ERROR" else W
            print(f"  {color}[{level}]{RESET} {scenario.service}: {msg}")
            await asyncio.sleep(step)

        # Recovery
        print(f"\n  {G}↺ Recovery starting...{RESET}")
        await asyncio.sleep(3)
        for level, msg, attrs in scenario.recovery_logs:
            if not state.running:
                break
            log = anomaly_log(scenario.service, level, msg, attrs)
            await send_log(client, state, log)
            print(f"  {G}[{level}]{RESET} {scenario.service}: {msg}")
            await asyncio.sleep(1)

        state.active_scenario = ""
        print(f"\n  {G}✓ Scenario resolved{RESET}\n")


async def stats_printer(state: SimState):
    """Prints a live stats line every 5 seconds."""
    while state.running:
        await asyncio.sleep(5)
        scenario_info = f" | {Y}ANOMALY: {state.active_scenario}{RESET}" if state.active_scenario else ""
        print(
            f"{DIM}[{state.elapsed()}]{RESET} "
            f"{G}Sent: {state.total_sent}{RESET}  "
            f"{R}Errors: {state.total_errors}{RESET}  "
            f"{B}RPS: {state.rps()}{RESET}  "
            f"{M}Anomalies: {state.anomalies_triggered}{RESET}"
            f"{scenario_info}"
        )


# ── Main ─────────────────────────────────────────────────────────────

async def main():
    print(f"\n{BOLD}{W}{'═'*60}{RESET}")
    print(f"{BOLD}{W}  Observability Agent — Canlı Demo Simülatörü{RESET}")
    print(f"{W}{'═'*60}{RESET}")
    print(f"  {G}►{RESET} Hedef: {INGEST_URL}")
    print(f"  {G}►{RESET} Servisler: {', '.join(SERVICES)}")
    print(f"  {G}►{RESET} Normal log aralığı: {INTERVAL_MS}ms")
    print(f"  {G}►{RESET} Anomali senaryosu: her ~{ANOMALY_EVERY_SEC}s")
    print(f"  {G}►{RESET} Dashboard: http://localhost:8080/static/index.html")
    print(f"\n  {DIM}Durdurmak için Ctrl+C{RESET}\n")
    print(f"{'─'*60}\n")

    state = SimState()

    async with httpx.AsyncClient() as client:
        # Connectivity check
        try:
            r = await client.get("http://localhost:8001/ingest/health", timeout=3)
            if r.status_code == 200:
                print(f"{G}✓ Ingestion servisi bağlantısı OK{RESET}\n")
            else:
                print(f"{R}✗ Ingestion servisi HTTP {r.status_code}{RESET}")
                print("  docker-compose up ingestion çalıştırdığından emin ol.\n")
        except Exception as e:
            print(f"{R}✗ Ingestion servisine bağlanılamadı: {e}{RESET}")
            print(f"  {Y}→ Önce şunu çalıştır: docker-compose up ingestion{RESET}\n")
            return

        # Run all producers concurrently
        tasks = [
            asyncio.create_task(normal_traffic_producer(client, state)),
            asyncio.create_task(anomaly_producer(client, state)),
            asyncio.create_task(stats_printer(state)),
        ]

        def _stop(sig, frame):
            state.running = False
            print(f"\n\n{Y}Simülatör durduruluyor...{RESET}")
            for t in tasks:
                t.cancel()

        signal.signal(signal.SIGINT,  _stop)
        signal.signal(signal.SIGTERM, _stop)

        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            elapsed = int(time.monotonic() - state.start_time)
            print(f"\n{BOLD}{W}{'─'*60}{RESET}")
            print(f"{BOLD}  Demo Özeti{RESET}")
            print(f"{'─'*60}")
            print(f"  Süre:           {elapsed//60}dk {elapsed%60}s")
            print(f"  Toplam log:     {state.total_sent}")
            print(f"  Hata:           {state.total_errors}")
            print(f"  Ort. RPS:       {state.rps()}")
            print(f"  Anomali sayısı: {state.anomalies_triggered}")
            print(f"{'─'*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
