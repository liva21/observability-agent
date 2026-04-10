"""
collector.py — Raw log normaliser (OpenTelemetry-style pipeline).

Accepts logs from three sources:
  1. Python loguru / standard logging dicts
  2. Raw OTLP-like JSON dicts
  3. Plain text lines (best-effort parse)

Returns a normalised LogEntry ready for Kafka / DB.
"""

import re
import logging
from datetime import datetime, timezone
from typing import Optional

from .models import LogEntry, LogLevel

logger = logging.getLogger(__name__)

# Regex for common plain-text log lines:
# "2024-01-15T12:34:56.789Z ERROR payment-service Connection refused"
_TEXT_PATTERN = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)"
    r"\s+(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)"
    r"\s+(?P<service>\S+)"
    r"\s+(?P<msg>.+)",
    re.IGNORECASE,
)

_LEVEL_ALIASES = {
    "WARN": LogLevel.WARNING,
    "FATAL": LogLevel.CRITICAL,
}


def _parse_level(raw: str) -> LogLevel:
    upper = raw.upper()
    if upper in _LEVEL_ALIASES:
        return _LEVEL_ALIASES[upper]
    try:
        return LogLevel(upper)
    except ValueError:
        return LogLevel.INFO


def _parse_timestamp(raw) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        # epoch seconds or milliseconds
        ts = raw / 1000 if raw > 1e10 else raw
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(raw, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                dt = datetime.strptime(raw, fmt)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return datetime.now(tz=timezone.utc)


class LogCollector:
    """
    Normalise raw log data from any source into LogEntry objects.

    Usage:
        collector = LogCollector(default_service="api-gateway")
        entry = collector.normalise(raw_dict)
    """

    def __init__(self, default_service: str = "unknown", default_env: str = "production"):
        self.default_service = default_service
        self.default_env = default_env

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def normalise(self, raw) -> Optional[LogEntry]:
        """
        Accept a dict (OTLP / loguru / arbitrary) or a plain string.
        Returns a LogEntry or None if the input is unparseable.
        """
        try:
            if isinstance(raw, str):
                return self._from_text(raw)
            if isinstance(raw, dict):
                return self._from_dict(raw)
            logger.warning("Unsupported log type: %s", type(raw))
            return None
        except Exception as exc:
            logger.error("Failed to normalise log: %s — %s", raw, exc)
            return None

    def normalise_batch(self, raws: list) -> list[LogEntry]:
        """Normalise a list of raw inputs, silently dropping failures."""
        result = []
        for raw in raws:
            entry = self.normalise(raw)
            if entry:
                result.append(entry)
        return result

    # ------------------------------------------------------------------ #
    #  Private helpers                                                     #
    # ------------------------------------------------------------------ #

    def _from_dict(self, d: dict) -> LogEntry:
        # Support OTLP-style nested resource attributes
        resource = d.get("resource", {})
        service = (
            d.get("service_name")
            or d.get("service")
            or resource.get("service.name")
            or self.default_service
        )

        # Build attributes: merge everything not already a top-level field
        reserved = {"timestamp", "time", "ts", "service_name", "service",
                    "level", "severity", "message", "msg", "trace_id",
                    "span_id", "host", "environment", "env", "resource"}
        attributes = {k: v for k, v in d.items() if k not in reserved}
        # Merge OTLP body attributes if present
        attributes.update(d.get("attributes", {}))

        return LogEntry(
            timestamp=_parse_timestamp(
                d.get("timestamp") or d.get("time") or d.get("ts")
                or datetime.now(tz=timezone.utc)
            ),
            service_name=service,
            level=_parse_level(d.get("level") or d.get("severity", "INFO")),
            message=str(d.get("message") or d.get("msg") or d.get("body", "")),
            trace_id=d.get("trace_id", ""),
            span_id=d.get("span_id", ""),
            host=d.get("host", ""),
            environment=d.get("environment") or d.get("env") or self.default_env,
            attributes=attributes,
        )

    def _from_text(self, text: str) -> Optional[LogEntry]:
        m = _TEXT_PATTERN.match(text.strip())
        if not m:
            # Fall back: treat entire line as message, level=INFO
            return LogEntry(
                timestamp=datetime.now(tz=timezone.utc),
                service_name=self.default_service,
                level=LogLevel.INFO,
                message=text.strip(),
                environment=self.default_env,
            )
        return LogEntry(
            timestamp=_parse_timestamp(m.group("ts")),
            service_name=m.group("service"),
            level=_parse_level(m.group("level")),
            message=m.group("msg").strip(),
            environment=self.default_env,
        )
