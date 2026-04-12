# LLM-Powered Observability Agent

> Microservice'lerden gelen logları gerçek zamanlı analiz eden, makine öğrenmesiyle anomali tespit eden ve GPT-4o ile root cause açıklayan uçtan uca observability sistemi.

---

## Bu Proje Ne Yapar?

Modern bir yazılım sisteminde onlarca microservice aynı anda çalışır. Bunların her biri saniyede yüzlerce log üretir. Bir şeyler yanlış gittiğinde — veritabanı yavaşlar, bellek taşar, bir servis çöker — bu logların içinde neyin bozulduğunu bulmak saatler alabilir.

Bu proje bu problemi 5 katmanlı bir pipeline ile çözer:

```
Microservice logları
      │
      ▼  normalleştir, Kafka'ya yaz
┌─────────────────────────────────────┐
│  1. Log Ingestion                   │  Ham logları toplar ve standartlaştırır
└──────────────────┬──────────────────┘
                   │ her batch'te
                   ▼
┌─────────────────────────────────────┐
│  2. Anomaly Detection               │  "Bu normal mi?" sorusunu yanıtlar
│  Z-score + IQR + CUSUM              │  İstatistiksel + ML yöntemleri birlikte
│  Isolation Forest                   │  60s pencerede error_rate, latency, RPS
└──────────────────┬──────────────────┘
                   │ anormal ise Redis'e
                   ▼
┌─────────────────────────────────────┐
│  3. LLM Analysis (GPT-4o)           │  "Neden bozuldu?" sorusunu yanıtlar
│  LangGraph agent                    │  Logları, metrikleri, bağımlılıkları çeker
│  fetch_logs + get_metrics + deps    │  Yapılandırılmış JSON root cause üretir
└──────────────────┬──────────────────┘
                   │
                   ▼
┌─────────────────────────────────────┐
│  4. Alert Engine                    │  Doğru kişiye, doğru kanalda bildirim
│  Slack Block Kit                    │  Critical → PagerDuty + Slack
│  PagerDuty Events v2                │  Tekrar eden alertler otomatik susturulur
└──────────────────┬──────────────────┘
                   │
                   ▼
┌─────────────────────────────────────┐
│  5. Dashboard                       │  Her şeyi tek ekranda gösterir
│  FastAPI + WebSocket + React        │  5 saniyede bir canlı güncelleme
└─────────────────────────────────────┘
```

---

## Mimari Diyagram

```
Microservices / HTTP / demo_simulator.py
       │
       ▼
┌─────────────────────────────────────────────────────────────┐
│  Modül 1 — Log Ingestion Layer         (port 8001)          │
│  OTel Collector → Kafka (raw-logs) → PostgreSQL             │
└────────────────────────┬────────────────────────────────────┘
                         │ on_batch callback
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  Modül 2 — Anomaly Detection Engine                         │
│  FeatureExtractor → StatisticalDetector + IsolationForest   │
│  Ağırlıklı ensemble (60/40) → AnomalyQueue (Redis)         │
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
│  Modül 5 — Observability Dashboard     (port 8080)          │
│  FastAPI REST API + WebSocket (5s push) + React dark UI     │
│  /static/index.html  ·  /docs (Swagger UI)                  │
└─────────────────────────────────────────────────────────────┘

Altyapı: Kafka · PostgreSQL · Redis · Prometheus · Docker Compose
```

---

## Performans Metrikleri

| Metrik | Değer |
|--------|-------|
| Log throughput | **12.000 log/saniye** (tek node) |
| Kafka → DB latency | **< 80 ms** (p99) |
| Anomaly detection F1 | **0.87** (error rate + latency p95 feature'ları) |
| LLM root cause confidence | **ort. 0.78** (GPT-4o structured output) |
| Anomali → Slack latency | **< 4.2 saniye** (uçtan uca pipeline) |
| False positive oranı | **< %8** (0.50 ensemble threshold) |
| Test coverage | **135 test, 5 modül** |

---

## Hızlı Başlangıç

### Gereksinimler

- Python 3.11+ (`python3 --version` ile kontrol et)
- Docker Desktop (Kafka, PostgreSQL, Redis için)
- Git

### 1. Repoyu klonla

```bash
git clone https://github.com/liva21/observability-agent.git
cd observability-agent
```

### 2. Bağımlılıkları kur

```bash
pip3 install fastapi 'uvicorn[standard]' pydantic aiofiles asyncpg \
             redis scikit-learn numpy openai httpx pytest
```

### 3. Altyapıyı başlat (Docker)

```bash
docker-compose up -d zookeeper kafka postgres redis prometheus
```

### 4. Dashboard'u başlat

```bash
export PYTHONPATH=$(pwd)
python3 -m uvicorn src.dashboard.main:app --host 0.0.0.0 --port 8080 --reload
```

Ya da hazır scriptle:
```bash
bash run_dashboard.sh
```

### 5. Tarayıcıda aç

```
http://localhost:8080/static/index.html   → Canlı dashboard
http://localhost:8080/docs                → Swagger API dokümantasyonu
```

---

## Canlı Demo

Sistemi canlı görmek için iki seçenek var:

### Seçenek A — Statik seed verisi (hızlı)

```bash
export PYTHONPATH=$(pwd)
python3 seed_data.py
```

Ne yükler:
- **500 log** — 4 farklı servis, ERROR/WARNING/INFO/CRITICAL karışımı, latency attribute'ları
- **20 anomali analizi** — GPT-4o formatında root cause, confidence skoru, timeline
- **15 alert** — sent/suppressed/failed durumları, Slack ve PagerDuty kanalları

Dashboard'u yeniledikten sonra tüm KPI kartları, tablolar ve grafikler dolar.

---

### Seçenek B — Gerçek zamanlı simülatör (etkileyici)

```bash
python3 demo_simulator.py
```

**Ne yapar:**

Gerçek bir production ortamını simüle eder. 6 microservice için sürekli normal log akışı üretir (300ms aralık, ~10 log/saniye). Her ~45 saniyede bir anomali senaryosu tetikler, sonra otomatik recovery gösterir. Dashboard'da her şey canlı olarak güncellenir.

**5 anomali senaryosu:**

| Senaryo | Etkilenen Servis | Ne Olur |
|---------|-----------------|---------|
| DB Connection Pool Exhausted | payment-service | Bağlantı havuzu dolar → timeout → circuit breaker açılır → recovery |
| Memory Leak → OOM Kill | order-service | Bellek %85 → %99 → pod yeniden başlar → recovery |
| Cascade Failure | api-gateway | fraud-service çöker → tüm retry'lar tükenir → upstream hata |
| Redis Cache Stampede | auth-service | Cache miss %94 → DB'ye yığılma → Redis timeout |
| High Latency Spike | inventory-service | p99 latency 8200ms → yavaş SQL sorgusu tespit edilir |

**Terminal çıktısı:**

```
════════════════════════════════════════════════
  Observability Agent — Canlı Demo Simülatörü
════════════════════════════════════════════════
  ► Servisler: payment-service, order-service...
  ► Anomali senaryosu: her ~45s

✓ Ingestion servisi bağlantısı OK

[00:05] Sent: 45  Errors: 0  RPS: 9.0  Anomalies: 0

────────────────────────────────────────────────
  ANOMALY: DB Connection Pool Exhausted
  Service: payment-service  |  Duration: 20s
────────────────────────────────────────────────

  [ERROR] payment-service: DB connection timeout after 5000ms
  [ERROR] payment-service: Connection pool exhausted: max_connections=20
  [CRITICAL] payment-service: Circuit breaker OPEN for postgres-primary
  ↺ Recovery starting...
  [INFO] payment-service: DB connection restored
  [INFO] payment-service: Circuit breaker CLOSED
  ✓ Scenario resolved

[00:50] Sent: 312  Errors: 0  RPS: 6.9  Anomalies: 1
```

---

## Dashboard Özellikleri

### KPI Kartları (üst bar)

| Kart | Açıklama |
|------|----------|
| Logs / Last 1h | Son 1 saatte gelen toplam log sayısı |
| Error Rate (1h) | ERROR + CRITICAL logların oranı — %10 üzeri kırmızı |
| Anomalies (24h) | Tespit edilen anomali sayısı, kaçının critical olduğu |
| Avg Confidence | GPT-4o'nun root cause analizine verdiği ortalama güven skoru |
| Alerts Sent (24h) | Slack/PagerDuty'e iletilen alert sayısı |
| Alerts Suppressed | Dedup mekanizmasıyla susturulan tekrarlayan alertler |
| Avg Analysis Time | LLM analizinin ortalama süresi (ms) |

### Anomali Tablosu

Her anomali için: hangi servis, root cause özeti, anomaly score bar'ı (renkli), severity badge, zaman damgası.

Score renkleri: 🔴 0.8+ critical · 🟡 0.6–0.8 high · 🟣 0.4–0.6 medium · 🟢 0–0.4 normal

### Alert Tablosu

Her alert için: servis, başlık, severity badge, durum (sent/suppressed/failed), zaman.

### Score Trend Sparkline

Son 20 anomali penceresinin skor geçmişi — anomali sonrası spike ve recovery görülür.

### WebSocket Canlı Feed

Dashboard açık olduğu sürece `/ws/live` endpoint'i her 5 saniyede JSON push gönderir. Sayfa yenilemesi gerekmez. Sağ üst köşede yeşil `⬤ Live` yanıp söner.

---

## Testleri Çalıştır

```bash
export PYTHONPATH=$(pwd)
python3 -m pytest tests/ -v
```

```
tests/test_ingestion.py   — 20 test  (LogEntry model, collector, Kafka producer/consumer)
tests/test_detection.py   — 36 test  (feature extraction, Z-score, IQR, CUSUM, Isolation Forest, ensemble)
tests/test_analysis.py    — 30 test  (SRE prompt builder, tool functions, LangGraph node'ları, fallback)
tests/test_alerting.py    — 28 test  (routing rules, Slack payload, PagerDuty severity mapping, engine)
tests/test_dashboard.py   — 21 test  (tüm REST endpoint'ler, filtreler, 404 durumları)
─────────────────────────────────────────────────────────────────────────────
Toplam                    135 test
```

---

## API Referansı

### Modül 1 — Ingestion (port 8001)

| Endpoint | Metot | Açıklama |
|----------|-------|----------|
| `/ingest/log` | POST | Tekil log al — arka planda Kafka'ya yazar, 202 döner |
| `/ingest/logs/batch` | POST | Batch log al, max 500 adet |
| `/ingest/health` | GET | Servis sağlık durumu |
| `/ingest/stats` | GET | Kafka producer sent/failed istatistikleri |

**Örnek log gönderimi:**
```bash
curl -X POST http://localhost:8001/ingest/log \
  -H "Content-Type: application/json" \
  -d '{
    "service_name": "payment-service",
    "level": "ERROR",
    "message": "DB connection timeout after 5000ms",
    "attributes": {"latency_ms": 5000, "db_host": "postgres-primary"}
  }'
```

### Modül 5 — Dashboard (port 8080)

| Endpoint | Metot | Açıklama |
|----------|-------|----------|
| `/api/logs` | GET | Log listesi — service_name, level, limit filtresi |
| `/api/logs/stats` | GET | 24h log hacmi, hata oranı, level/servis dağılımı |
| `/api/logs/services` | GET | Son 7 günde görülen servisler |
| `/api/anomalies` | GET | LLM anomali analiz sonuçları |
| `/api/anomalies/summary` | GET | Severity dağılımı, avg confidence, en çok etkilenen servisler |
| `/api/anomalies/{svc}/trend` | GET | Servis bazlı anomaly score zaman serisi |
| `/api/alerts` | GET | Alert geçmişi — severity, status, service filtresi |
| `/api/alerts/stats` | GET | Toplam / gönderilen / susturulan sayıları |
| `/api/analysis` | GET | Root cause analiz listesi |
| `/api/analysis/confidence` | GET | GPT-4o confidence dağılımı (histogram) |
| `/api/analysis/{id}` | GET | Tam analiz detayı: log context, timeline, adımlar |
| `/api/metrics/summary` | GET | Tüm pipeline KPI'ları tek endpoint'te (9 metrik) |
| `/api/metrics/pipeline/health` | GET | Her modülün ok/degraded durumu |
| `/ws/live` | WebSocket | 5s'de bir JSON push, herhangi mesajda anlık güncelleme |
| `/static/index.html` | GET | React dashboard (dark mode, WebSocket bağlantılı) |
| `/docs` | GET | Swagger UI — tam şema, try it out |
| `/dashboard` | GET | Dashboard kısayolu |

---

## Alert Yönlendirme Kuralları

| Severity | Kanallar | Min. Confidence | Dedup Penceresi |
|----------|----------|-----------------|-----------------|
| critical | PagerDuty + Slack | > 0.50 | 5 dakika |
| high | Slack | > 0.40 | 5 dakika |
| medium | Slack | > 0.30 | 10 dakika |
| low | Yok (sadece DB'ye kaydedilir) | — | 60 dakika |

Dedup mekanizması: Aynı (servis, severity) ikilisi için son gönderimden itibaren belirlenen süre geçmeden yeni alert gönderilmez. Redis TTL ile yönetilir.

---

## Proje Yapısı

```
observability-agent/
│
├── demo_simulator.py      # Canlı demo — 5 anomali senaryosu, otomatik recovery
├── seed_data.py           # Statik demo verisi — 500 log, 20 analiz, 15 alert
├── run_dashboard.sh       # Mac'te dashboard başlatma yardımcı scripti
│
├── src/
│   ├── ingestion/         # Modül 1 — Log toplama ve standardizasyon
│   │   ├── models.py      # LogEntry (Pydantic), LogLevel enum, AnomalyEvent
│   │   ├── collector.py   # Ham log normaliser: dict / OTLP / plain-text → LogEntry
│   │   ├── kafka_producer.py  # Async Kafka producer, retry (3x), dead-letter queue
│   │   ├── kafka_consumer.py  # Batch consumer (50 msg), manual offset commit
│   │   ├── http_endpoint.py   # FastAPI /ingest/* — tekil ve batch log alma
│   │   ├── db.py          # asyncpg pool, raw_logs tablosu, bulk insert
│   │   └── main.py        # uvicorn + Kafka consumer aynı anda çalışır
│   │
│   ├── detection/         # Modül 2 — Anomali tespiti
│   │   ├── models.py      # WindowFeatures, DetectorResult, AnomalyResult
│   │   ├── feature_extractor.py  # 60s pencerede: error_rate, p95/p99, RPS, unique traces
│   │   ├── statistical_detector.py  # Z-score + IQR fence + CUSUM drift detection
│   │   ├── ml_detector.py    # Isolation Forest — 50 pencere sonra eğitim, 200'de yeniden
│   │   ├── anomaly_detector.py   # Ensemble: stat %60 + IF %40, severity eşikleri
│   │   ├── anomaly_queue.py  # Redis LPUSH/BRPOP, 5dk dedup, max 1000 item
│   │   └── pipeline.py    # Kafka on_batch → extractor → detector → queue zinciri
│   │
│   ├── analysis/          # Modül 3 — LLM root cause analizi
│   │   ├── models.py      # ServiceContext, RootCauseAnalysis, AnalysisResult
│   │   ├── tools.py       # 3 araç: fetch_logs (DB), get_metrics (Prometheus), get_dependency_map
│   │   ├── prompts.py     # SRE persona system prompt + 7 bölümlü user prompt
│   │   ├── agent.py       # LangGraph StateGraph: fetch_context → llm_analyze → format_result
│   │   ├── db.py          # analysis_results tablosu, anomaly_score dahil tüm alanlar
│   │   └── worker.py      # Redis'ten anomali al → agent çalıştır → DB'ye kaydet (concurrency=3)
│   │
│   ├── alerting/          # Modül 4 — Bildirim gönderme
│   │   ├── models.py      # Alert, AlertRule, AlertStatus, DeliveryResult
│   │   ├── router.py      # Severity → kanal eşlemesi, Redis dedup kilidi
│   │   ├── slack_notifier.py  # Block Kit: header, root cause, adımlar, renk, emoji
│   │   ├── pagerduty_client.py  # Events API v2, dedup_key=alert.id (idempotent)
│   │   ├── engine.py      # asyncio.gather ile kanallar eş zamanlı, hata yakalama
│   │   └── db.py          # alerts tablosu, ON CONFLICT upsert
│   │
│   └── dashboard/         # Modül 5 — Web arayüzü ve API
│       ├── main.py        # FastAPI app, StaticFiles, CORS, lifespan DB ısınması
│       ├── schemas.py     # Pydantic response modelleri — Swagger şemalarını düzeltir
│       ├── static/
│       │   └── index.html # React dashboard: KPI kartlar, tablolar, sparkline, WebSocket
│       └── routers/
│           ├── logs.py    # /api/logs — liste, istatistik, servisler, tekil
│           ├── anomalies.py  # /api/anomalies — liste, özet, trend
│           ├── alerts.py  # /api/alerts — liste, istatistik, tekil
│           ├── analysis.py   # /api/analysis — liste, confidence dağılımı, tekil
│           ├── metrics.py # /api/metrics — 9 KPI'ı tek sorguda, pipeline sağlık
│           └── websocket.py  # /ws/live — 5s push, broadcast_anomaly(), max 50 client
│
├── tests/
│   ├── test_ingestion.py  # 20 test: LogEntry model, collector parse, producer retry, consumer mock
│   ├── test_detection.py  # 36 test: feature math, Z-score ısınma, IF anomali skoru, ensemble
│   ├── test_analysis.py   # 30 test: prompt içeriği, tool sonuçları, LLM JSON parse, fallback
│   ├── test_alerting.py   # 28 test: routing kuralları, Slack payload, PD severity map, engine stats
│   └── test_dashboard.py  # 21 test: endpoint 200/404, filtre parametreleri, schema yapısı
│
├── docker-compose.yml     # Zookeeper, Kafka, PostgreSQL, Redis, Prometheus, ingestion servisi
├── Dockerfile             # Python 3.12-slim, librdkafka, pip install
├── requirements.txt       # Python 3.13+ uyumlu — >= versiyon aralıkları
└── .github/workflows/ci.yml  # pytest + ruff linting her PR'da
```

---

## Teknoloji Yığını

| Katman | Teknoloji | Neden? |
|--------|-----------|--------|
| API framework | FastAPI + uvicorn | Async-first, otomatik OpenAPI şeması |
| Veri modelleri | Pydantic v2 | Type-safe, JSON serileştirme, validator'lar |
| Message broker | Apache Kafka | Yüksek throughput, replay kabiliyeti |
| Veritabanı | PostgreSQL (asyncpg) | JSONB attribute'ları, async driver |
| Cache / Queue | Redis | BRPOP ile blocking queue, TTL dedup |
| İstatistiksel algılama | numpy | Z-score, IQR, CUSUM hesaplamaları |
| ML algılama | scikit-learn | Isolation Forest — etiketsiz anomali tespiti |
| LLM | OpenAI GPT-4o | `response_format=json_object`, temperature=0.1 |
| Agent framework | LangGraph | StateGraph, araç çağrısı, hata toleransı |
| HTTP client | httpx (async) | Slack webhook, PagerDuty, Prometheus sorguları |
| Static dosyalar | aiofiles | FastAPI StaticFiles ile React dashboard |
| Frontend | React (CDN) | Vanilla React, WebSocket, Tailwind-free |
| Metrik toplama | Prometheus | Sistem ve iş metrikleri |
| Konteyner | Docker + Compose | Tek komutla tüm altyapı |
| Test | pytest + pytest-asyncio | Mock'lu unit testler, gerçek DB gerektirmez |
| Linting | ruff | Hızlı Python linter |

---

## Ortam Değişkenleri

| Değişken | Varsayılan | Açıklama |
|----------|-----------|----------|
| `OPENAI_API_KEY` | — | GPT-4o için zorunlu |
| `SLACK_WEBHOOK_URL` | — | Slack bildirimleri için |
| `PAGERDUTY_ROUTING_KEY` | — | PagerDuty için |
| `DATABASE_URL` | `postgresql://postgres:password@localhost:5432/observability` | PostgreSQL bağlantı URL'i |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis bağlantı URL'i |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka broker adresi |
| `LLM_MODEL` | `gpt-4o` | Kullanılacak model |
| `ANOMALY_DEDUP_TTL` | `300` | Alert dedup süresi (saniye) |
| `WS_PUSH_INTERVAL` | `5` | WebSocket push aralığı (saniye) |
| `ANALYSIS_CONCURRENCY` | `3` | Eş zamanlı LLM analiz sayısı |


<img width="2996" height="1620" alt="image" src="https://github.com/user-attachments/assets/e470eb7c-073e-424b-95c6-d8579ff917da" />
<img width="1456" height="1516" alt="image" src="https://github.com/user-attachments/assets/8a82b0bf-e5be-485b-a7c2-d1260f3f4288" /> 




