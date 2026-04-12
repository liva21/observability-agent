from .models import LogEntry, LogLevel, AnomalyEvent
from .collector import LogCollector
from .db import get_pool, insert_log, insert_logs_bulk, fetch_logs

# kafka_producer ve kafka_consumer confluent_kafka C kütüphanesi gerektirir.
# Docker içinde çalışır. Dashboard/local geliştirme için try/except ile atlanır.
try:
    from .kafka_producer import LogProducer
    from .kafka_consumer import LogConsumer
except ImportError:
    LogProducer = None
    LogConsumer = None

__all__ = [
    "LogEntry",
    "LogLevel",
    "AnomalyEvent",
    "LogCollector",
    "LogProducer",
    "LogConsumer",
    "get_pool",
    "insert_log",
    "insert_logs_bulk",
    "fetch_logs",
]
