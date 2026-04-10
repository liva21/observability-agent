"""
test_ingestion.py — Full test suite for Module 1 (Log Ingestion Layer).

Run:
    pytest tests/test_ingestion.py -v
"""

import json
import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.ingestion.models import LogEntry, LogLevel, AnomalyEvent
from src.ingestion.collector import LogCollector


# ================================================================== #
#  Fixtures                                                            #
# ================================================================== #

@pytest.fixture
def collector():
    return LogCollector(default_service="test-service", default_env="test")


@pytest.fixture
def sample_log_dict():
    return {
        "timestamp": "2024-01-15T12:34:56.789Z",
        "service_name": "payment-service",
        "level": "ERROR",
        "message": "Connection to DB timed out after 5000ms",
        "trace_id": "abc123",
        "span_id": "def456",
        "host": "pod-42",
        "environment": "production",
        "attributes": {"db_host": "postgres-primary", "timeout_ms": 5000},
    }


@pytest.fixture
def sample_log_entry(sample_log_dict):
    return LogEntry(**sample_log_dict)


# ================================================================== #
#  Model tests                                                         #
# ================================================================== #

class TestLogEntry:
    def test_basic_creation(self, sample_log_dict):
        entry = LogEntry(**sample_log_dict)
        assert entry.service_name == "payment-service"
        assert entry.level == LogLevel.ERROR
        assert entry.message == "Connection to DB timed out after 5000ms"

    def test_auto_id_generated(self, sample_log_dict):
        e1 = LogEntry(**sample_log_dict)
        e2 = LogEntry(**sample_log_dict)
        assert e1.id != e2.id
        assert len(e1.id) == 36   # UUID format

    def test_auto_trace_id_when_missing(self):
        entry = LogEntry(
            timestamp=datetime.now(tz=timezone.utc),
            service_name="svc",
            level=LogLevel.INFO,
            message="hello",
        )
        assert len(entry.trace_id) == 36

    def test_service_name_stripped_and_lowercased(self):
        entry = LogEntry(
            timestamp=datetime.now(tz=timezone.utc),
            service_name="  PAYMENT-Service  ",
            level=LogLevel.INFO,
            message="test",
        )
        assert entry.service_name == "payment-service"

    def test_empty_service_name_raises(self):
        with pytest.raises(Exception):
            LogEntry(
                timestamp=datetime.now(tz=timezone.utc),
                service_name="   ",
                level=LogLevel.INFO,
                message="test",
            )

    def test_empty_message_raises(self):
        with pytest.raises(Exception):
            LogEntry(
                timestamp=datetime.now(tz=timezone.utc),
                service_name="svc",
                level=LogLevel.INFO,
                message="",
            )

    def test_serialise_deserialise_roundtrip(self, sample_log_entry):
        json_str = sample_log_entry.model_dump_json()
        data = json.loads(json_str)
        restored = LogEntry(**data)
        assert restored.id == sample_log_entry.id
        assert restored.level == sample_log_entry.level
        assert restored.attributes == sample_log_entry.attributes

    def test_all_log_levels_accepted(self):
        for level in LogLevel:
            entry = LogEntry(
                timestamp=datetime.now(tz=timezone.utc),
                service_name="svc",
                level=level,
                message="test",
            )
            assert entry.level == level


# ================================================================== #
#  Collector tests                                                     #
# ================================================================== #

class TestLogCollector:

    def test_normalise_full_dict(self, collector, sample_log_dict):
        entry = collector.normalise(sample_log_dict)
        assert entry is not None
        assert entry.service_name == "payment-service"
        assert entry.level == LogLevel.ERROR
        assert entry.attributes["timeout_ms"] == 5000

    def test_normalise_minimal_dict(self, collector):
        entry = collector.normalise({
            "service_name": "api",
            "message": "started",
        })
        assert entry is not None
        assert entry.level == LogLevel.INFO
        assert entry.service_name == "api"

    def test_normalise_otlp_style(self, collector):
        otlp = {
            "resource": {"service.name": "order-service"},
            "severity": "WARNING",
            "body": "High memory usage detected",
            "attributes": {"memory_percent": 87.5},
        }
        entry = collector.normalise(otlp)
        assert entry is not None
        assert entry.service_name == "order-service"
        assert entry.level == LogLevel.WARNING
        assert entry.attributes["memory_percent"] == 87.5

    def test_normalise_plain_text(self, collector):
        text = "2024-01-15T12:00:00Z ERROR my-service Database connection refused"
        entry = collector.normalise(text)
        assert entry is not None
        assert entry.level == LogLevel.ERROR
        assert entry.service_name == "my-service"
        assert "Database connection refused" in entry.message

    def test_normalise_plain_text_fallback(self, collector):
        entry = collector.normalise("just some plain text without format")
        assert entry is not None
        assert entry.message == "just some plain text without format"
        assert entry.service_name == "test-service"   # default

    def test_normalise_warn_alias(self, collector):
        entry = collector.normalise({"service_name": "svc", "level": "WARN", "message": "x"})
        assert entry.level == LogLevel.WARNING

    def test_normalise_fatal_alias(self, collector):
        entry = collector.normalise({"service_name": "svc", "level": "FATAL", "message": "x"})
        assert entry.level == LogLevel.CRITICAL

    def test_normalise_timestamp_epoch_ms(self, collector):
        entry = collector.normalise({
            "service_name": "svc",
            "message": "test",
            "timestamp": 1705316096789,   # ms epoch
        })
        assert entry is not None
        assert entry.timestamp.year == 2024

    def test_normalise_unsupported_type_returns_none(self, collector):
        entry = collector.normalise(12345)
        assert entry is None

    def test_normalise_batch(self, collector, sample_log_dict):
        raws = [sample_log_dict, sample_log_dict, "bad input that still works"]
        entries = collector.normalise_batch(raws)
        assert len(entries) == 3     # plain text still produces an entry

    def test_normalise_batch_skips_none(self, collector):
        raws = [
            {"service_name": "svc", "message": "ok"},
            12345,                   # invalid type → None → skipped
            {"service_name": "svc", "message": "ok2"},
        ]
        entries = collector.normalise_batch(raws)
        assert len(entries) == 2


# ================================================================== #
#  Kafka producer tests (mocked)                                       #
# ================================================================== #

class TestLogProducer:

    @patch("src.ingestion.kafka_producer.Producer")
    def test_send_success(self, MockProducer, sample_log_entry):
        from src.ingestion.kafka_producer import LogProducer
        mock_producer = MagicMock()
        MockProducer.return_value = mock_producer

        producer = LogProducer()
        result = asyncio.get_event_loop().run_until_complete(
            producer.send(sample_log_entry)
        )
        assert result is True
        assert mock_producer.produce.called

    @patch("src.ingestion.kafka_producer.Producer")
    def test_send_batch_returns_stats(self, MockProducer, sample_log_entry):
        from src.ingestion.kafka_producer import LogProducer
        MockProducer.return_value = MagicMock()

        producer = LogProducer()
        logs = [sample_log_entry] * 5
        result = asyncio.get_event_loop().run_until_complete(
            producer.send_batch(logs)
        )
        assert result["sent"] == 5
        assert result["failed"] == 0

    @patch("src.ingestion.kafka_producer.Producer")
    def test_dlq_written_on_permanent_failure(self, MockProducer, sample_log_entry, tmp_path):
        import src.ingestion.kafka_producer as kp
        from src.ingestion.kafka_producer import LogProducer
        from confluent_kafka import KafkaException

        mock_producer = MagicMock()
        mock_producer.produce.side_effect = KafkaException("broker unavailable")
        MockProducer.return_value = mock_producer
        kp.DLQ_PATH = tmp_path / "dlq.jsonl"
        kp.MAX_RETRIES = 1

        producer = LogProducer()
        result = asyncio.get_event_loop().run_until_complete(
            producer.send(sample_log_entry, retries=1)
        )
        assert result is False
        assert kp.DLQ_PATH.exists()
        lines = kp.DLQ_PATH.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["id"] == sample_log_entry.id


# ================================================================== #
#  Kafka consumer tests (mocked)                                       #
# ================================================================== #

class TestLogConsumer:

    def _make_kafka_message(self, log_entry: LogEntry):
        msg = MagicMock()
        msg.value.return_value = log_entry.model_dump_json().encode()
        msg.offset.return_value = 0
        msg.error.return_value = None
        return msg

    @patch("src.ingestion.kafka_consumer.Consumer")
    @patch("src.ingestion.kafka_consumer.insert_logs_bulk", new_callable=AsyncMock)
    def test_process_batch_inserts_to_db(self, mock_insert, MockConsumer, sample_log_entry):
        from src.ingestion.kafka_consumer import LogConsumer
        mock_insert.return_value = 1
        MockConsumer.return_value = MagicMock()

        consumer = LogConsumer()
        msgs = [self._make_kafka_message(sample_log_entry)]
        asyncio.get_event_loop().run_until_complete(consumer._process_batch(msgs))

        mock_insert.assert_called_once()
        inserted_entries = mock_insert.call_args[0][0]
        assert len(inserted_entries) == 1
        assert inserted_entries[0].id == sample_log_entry.id

    @patch("src.ingestion.kafka_consumer.Consumer")
    @patch("src.ingestion.kafka_consumer.insert_logs_bulk", new_callable=AsyncMock)
    def test_on_batch_callback_fired(self, mock_insert, MockConsumer, sample_log_entry):
        from src.ingestion.kafka_consumer import LogConsumer
        mock_insert.return_value = 1
        MockConsumer.return_value = MagicMock()

        received = []

        async def callback(entries):
            received.extend(entries)

        consumer = LogConsumer(on_batch=callback)
        msgs = [self._make_kafka_message(sample_log_entry)]
        asyncio.get_event_loop().run_until_complete(consumer._process_batch(msgs))

        assert len(received) == 1
        assert received[0].service_name == sample_log_entry.service_name

    @patch("src.ingestion.kafka_consumer.Consumer")
    def test_deserialise_bad_json_skipped(self, MockConsumer):
        from src.ingestion.kafka_consumer import LogConsumer
        MockConsumer.return_value = MagicMock()

        bad_msg = MagicMock()
        bad_msg.value.return_value = b"not json at all"
        bad_msg.offset.return_value = 99
        bad_msg.error.return_value = None

        consumer = LogConsumer()
        result = consumer._deserialise(bad_msg)
        assert result is None
        assert consumer.stats["errors"] == 1
