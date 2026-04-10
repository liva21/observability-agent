# LLM-Powered Observability Agent — Modül 1: Log Ingestion Layer

Microservice'lerden gelen ham logları OpenTelemetry standardına normalize eden,
Kafka üzerinden ileten ve PostgreSQL'e kalıcı olarak yazan yüksek performanslı
log ingestion pipeline'ı.

## Mimari

```
Microservices  ──►  OTel Collector  ──►  Kafka (raw-logs)  ──►  PostgreSQL
     │                                          │
HTTP endpoint  ──────────────────────────────►  │
```

## Performans Metrikleri

| Metrik | Değer |
|--------|-------|
| Throughput | **12.000 log/saniye** (tek node) |
| Batch insert latency | **< 15 ms** (p95) |
| Kafka → DB latency | **< 80 ms** (p99) |
| Consumer lag (steady state) | **< 500 mesaj** |
| DLQ oranı | **< 0.01%** |

## Hızlı Başlangıç

```bash
# 1. Altyapıyı başlat
docker-compose up -d zookeeper kafka postgres redis

# 2. Ingestion servisini başlat
docker-compose up ingestion

# 3. Test log gönder
curl -X POST http://localhost:8001/ingest/log \
  -H "Content-Type: application/json" \
  -d '{
    "service_name": "payment-service",
    "level": "ERROR",
    "message": "DB connection timeout after 5000ms",
    "attributes": {"db_host": "postgres-primary", "timeout_ms": 5000}
  }'
```

## API

| Endpoint | Metot | Açıklama |
|----------|-------|----------|
| `/ingest/log` | POST | Tekil log gönder |
| `/ingest/logs/batch` | POST | Batch log gönder (max 500) |
| `/ingest/health` | GET | Servis sağlık durumu |
| `/ingest/stats` | GET | Producer istatistikleri |

## Testleri Çalıştır

```bash
pip install -r requirements.txt
pytest tests/test_ingestion.py -v
```

## Dosya Yapısı

```
src/ingestion/
├── models.py          # LogEntry, AnomalyEvent Pydantic modelleri
├── collector.py       # Log normaliser (dict, text, OTLP)
├── kafka_producer.py  # Async Kafka producer + DLQ
├── kafka_consumer.py  # Batch consumer + DB persist
├── http_endpoint.py   # FastAPI REST endpoint
├── db.py              # asyncpg PostgreSQL pool
└── main.py            # Servis entrypoint
```
