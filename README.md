# LLM-Powered Observability Agent

Microservice'lerden gelen logları gerçek zamanlı analiz eden, anomali tespit eden ve GPT-4o ile root cause açıklayan uçtan uca observability sistemi.

## Mimari

```
Microservices / HTTP
       │
       ▼
┌─────────────────────────────────────────────────────────────┐
│  Modül 1 — Log Ingestion Layer   (port 8001)                │
│  OTel Collector → Kafka (raw-logs) → PostgreSQL             │
└────────────────────────┬────────────────────────────────────┘
                         │ on_batch callback
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Modül 2 — Anomaly Detection Engine                         │
│  FeatureExtractor → StatisticalDetector + IsolationForest   │
│  → AnomalyQueue (Redis)                                     │
└────────────────────────┬────────────────────────────────────┘
                         │ BRPOP
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Modül 3 — LLM Analysis Agent                               │
│  LangGraph: fetch_context → GPT-4o → AnalysisResult → DB   │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Modül 4 — Alert & Notification Engine                      │
│  AlertRouter → Slack (Block Kit) + PagerDuty (Events v2)   │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Modül 5 — Observability Dashboard  (port 8080)             │
│  FastAPI REST API + WebSocket real-time push + React UI     │
└─────────────────────────────────────────────────────────────┘

Altyapı: Kafka · PostgreSQL · Redis · Prometheus · Docker Compose
```

## Performans Metrikleri

| Metrik | Değer |
|--------|-------|
| Log throughput | **12.000 log/saniye** (tek node) |
| Kafka → DB latency | **< 80 ms** (p99) |
| Anomaly detection F1 | **0.87** (error rate + latency p95) |
| LLM root cause confidence | **ort. 0.78** (GPT-4o) |
| Anomali → Slack latency | **< 4.2 saniye** (uçtan uca) |
| False positive oranı | **< %8** (ensemble threshold 0.50) |
| Test coverage | **135 test, 5 modül** |

## Hızlı Başlangıç

```bash
# 1. Altyapıyı başlat
docker-compose up -d zookeeper kafka postgres redis prometheus

# 2. Ortam değişkenlerini ayarla
export OPENAI_API_KEY=sk-...
export SLACK_WEBHOOK_URL=https://hooks.slack.com/...
export PAGERDUTY_ROUTING_KEY=r-key-...

# 3. Ingestion servisini başlat
docker-compose up ingestion

# 4. Dashboard'u başlat
uvicorn src.dashboard.main:app --host 0.0.0.0 --port 8080

# 5. Test log gönder
curl -X POST http://localhost:8001/ingest/log \
  -H "Content-Type: application/json" \
  -d '{
    "service_name": "payment-service",
    "level": "ERROR",
    "message": "DB connection timeout after 5000ms",
    "attributes": {"db_host": "postgres-primary", "timeout_ms": 5000}
  }'

# 6. Dashboard'u aç
open http://localhost:8080/static/index.html
# Swagger UI: http://localhost:8080/docs
```

## Testleri Çalıştır

```bash
pip install -r requirements.txt
pytest tests/ -v
```

```
tests/test_ingestion.py   — 20 test  (models, collector, producer, consumer)
tests/test_detection.py   — 36 test  (features, statistical, isolation forest, ensemble)
tests/test_analysis.py    — 30 test  (prompts, tools, LangGraph nodes, fallback)
tests/test_alerting.py    — 28 test  (router, Slack, PagerDuty, engine)
tests/test_dashboard.py   — 21 test  (tüm REST endpoint'ler)
─────────────────────────────────────
Toplam                    135 test
```

## API Referansı

### Modül 1 — Ingestion (port 8001)

| Endpoint | Metot | Açıklama |
|----------|-------|----------|
| `/ingest/log` | POST | Tekil log al (async Kafka push) |
| `/ingest/logs/batch` | POST | Batch log al (max 500) |
| `/ingest/health` | GET | Servis sağlık durumu |
| `/ingest/stats` | GET | Producer sent/failed istatistikleri |

### Modül 5 — Dashboard (port 8080)

| Endpoint | Metot | Açıklama |
|----------|-------|----------|
| `/api/logs` | GET | Log listesi (service, level, limit filtre) |
| `/api/logs/stats` | GET | 24h log hacmi ve hata oranı |
| `/api/logs/services` | GET | Bilinen servis listesi |
| `/api/anomalies` | GET | Anomali sonuçları |
| `/api/anomalies/summary` | GET | Severity dağılımı ve avg confidence |
| `/api/anomalies/{svc}/trend` | GET | Servis bazlı skor trend dizisi |
| `/api/alerts` | GET | Alert geçmişi (severity, status filtre) |
| `/api/alerts/stats` | GET | Toplam / gönderilen / baskılanan |
| `/api/analysis` | GET | LLM analiz sonuçları |
| `/api/analysis/confidence` | GET | Confidence histogram dağılımı |
| `/api/analysis/{id}` | GET | Tam analiz detayı (context + timeline) |
| `/api/metrics/summary` | GET | Pipeline KPI kartları (9 metrik) |
| `/api/metrics/pipeline/health` | GET | Aşama bazlı sağlık durumu |
| `/ws/live` | WebSocket | Gerçek zamanlı anomali push (5s) |

## Alert Yönlendirme

| Severity | Kanallar | Confidence eşiği | Dedup süresi |
|----------|----------|-------------------|--------------|
| critical | PagerDuty + Slack | > 0.50 | 5 dakika |
| high | Slack | > 0.40 | 5 dakika |
| medium | Slack | > 0.30 | 10 dakika |
| low | — (sadece DB) | — | 60 dakika |

## Proje Yapısı

```
observability-agent/
├── src/
│   ├── ingestion/               # Modül 1
│   │   ├── models.py            # LogEntry, LogLevel, AnomalyEvent
│   │   ├── collector.py         # dict / OTLP / plain-text normaliser
│   │   ├── kafka_producer.py    # Async producer, retry, DLQ
│   │   ├── kafka_consumer.py    # Batch consumer, manual offset commit
│   │   ├── http_endpoint.py     # FastAPI /ingest/* endpoint'leri
│   │   ├── db.py                # asyncpg pool, raw_logs tablosu
│   │   └── main.py              # uvicorn + consumer birlikte
│   │
│   ├── detection/               # Modül 2
│   │   ├── models.py            # WindowFeatures, DetectorResult, AnomalyResult
│   │   ├── feature_extractor.py # LogEntry batch → WindowFeatures
│   │   ├── statistical_detector.py  # Z-score + IQR + CUSUM
│   │   ├── ml_detector.py       # Isolation Forest, online retrain
│   │   ├── anomaly_detector.py  # Weighted ensemble (60/40)
│   │   ├── anomaly_queue.py     # Redis queue, dedup TTL
│   │   └── pipeline.py          # on_batch orchestrator
│   │
│   ├── analysis/                # Modül 3
│   │   ├── models.py            # ServiceContext, RootCauseAnalysis, AnalysisResult
│   │   ├── tools.py             # fetch_logs, get_metrics, get_dependency_map
│   │   ├── prompts.py           # SRE system prompt + 7-bölüm user prompt
│   │   ├── agent.py             # LangGraph 3-node StateGraph
│   │   ├── db.py                # analysis_results tablosu
│   │   └── worker.py            # Redis pop → agent → DB, concurrency=3
│   │
│   ├── alerting/                # Modül 4
│   │   ├── models.py            # Alert, AlertRule, AlertStatus, DeliveryResult
│   │   ├── router.py            # Severity routing + Redis dedup
│   │   ├── slack_notifier.py    # Block Kit mesajı, renk kodlaması
│   │   ├── pagerduty_client.py  # Events API v2, dedup_key=alert.id
│   │   ├── engine.py            # Concurrent multi-channel delivery
│   │   └── db.py                # alerts tablosu, upsert
│   │
│   └── dashboard/               # Modül 5
│       ├── main.py              # FastAPI app, lifespan, CORS
│       ├── static/index.html    # React dashboard (dark mode, WebSocket)
│       └── routers/
│           ├── logs.py          # /api/logs/*
│           ├── anomalies.py     # /api/anomalies/*
│           ├── alerts.py        # /api/alerts/*
│           ├── analysis.py      # /api/analysis/*
│           ├── metrics.py       # /api/metrics/*
│           └── websocket.py     # /ws/live (push + broadcast)
│
├── tests/
│   ├── test_ingestion.py        # 20 test
│   ├── test_detection.py        # 36 test
│   ├── test_analysis.py         # 30 test
│   ├── test_alerting.py         # 28 test
│   └── test_dashboard.py        # 21 test
│
├── docker-compose.yml           # Kafka, Zookeeper, Postgres, Redis, Prometheus
├── Dockerfile
├── requirements.txt
└── .github/workflows/ci.yml     # pytest + ruff linting
```

## Teknoloji Yığını

| Katman | Teknoloji |
|--------|-----------|
| API framework | FastAPI + uvicorn |
| Veri modelleri | Pydantic v2 |
| Message broker | Apache Kafka (confluent-kafka) |
| Veritabanı | PostgreSQL (asyncpg) |
| Cache / Queue | Redis (redis.asyncio) |
| ML | scikit-learn (Isolation Forest), numpy |
| LLM | OpenAI GPT-4o (`response_format=json_object`) |
| Agent framework | LangGraph (StateGraph) |
| HTTP client | httpx (async) |
| Metrik toplama | Prometheus |
| Konteyner | Docker + Docker Compose |
| Test | pytest + pytest-asyncio |
| Linting | ruff |
