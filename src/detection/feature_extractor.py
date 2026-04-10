"""
feature_extractor.py — Converts a batch of LogEntry objects (one
sliding window) into a WindowFeatures vector for the detectors.

Called by the consumer on_batch callback after each window closes.
"""

import logging
import numpy as np
from datetime import datetime, timezone
from collections import defaultdict

from src.ingestion.models import LogEntry, LogLevel
from .models import WindowFeatures

logger = logging.getLogger(__name__)

# Latency key names we look for inside LogEntry.attributes
_LATENCY_KEYS = ("latency_ms", "duration_ms", "response_time_ms", "elapsed_ms")


class FeatureExtractor:
    """
    Stateless transformer: list[LogEntry] → WindowFeatures.

    Usage:
        extractor = FeatureExtractor()
        features = extractor.extract(logs, service_name="payment-service")
    """

    def extract(
        self,
        logs: list[LogEntry],
        service_name: str,
        window_seconds: int = 60,
    ) -> WindowFeatures:
        if not logs:
            now = datetime.now(tz=timezone.utc)
            return WindowFeatures(
                service_name=service_name,
                window_start=now,
                window_end=now,
                window_seconds=window_seconds,
            )

        # Sort by timestamp for accurate window bounds
        sorted_logs = sorted(logs, key=lambda l: l.timestamp)
        window_start = sorted_logs[0].timestamp
        window_end   = sorted_logs[-1].timestamp
        total        = len(sorted_logs)
        elapsed_sec  = max(
            (window_end - window_start).total_seconds(), 1.0
        )

        # --- Error counts ---
        error_count    = sum(1 for l in sorted_logs if l.level == LogLevel.ERROR)
        critical_count = sum(1 for l in sorted_logs if l.level == LogLevel.CRITICAL)
        error_rate     = (error_count + critical_count) / total

        # --- Latency ---
        latencies = self._extract_latencies(sorted_logs)
        if latencies:
            arr = np.array(latencies, dtype=float)
            p50  = float(np.percentile(arr, 50))
            p95  = float(np.percentile(arr, 95))
            p99  = float(np.percentile(arr, 99))
            mean = float(np.mean(arr))
        else:
            p50 = p95 = p99 = mean = 0.0

        # --- Unique traces ---
        unique_traces = len({l.trace_id for l in sorted_logs if l.trace_id})

        return WindowFeatures(
            service_name=service_name,
            window_start=window_start,
            window_end=window_end,
            window_seconds=window_seconds,
            total_logs=total,
            logs_per_second=round(total / elapsed_sec, 2),
            error_count=error_count,
            critical_count=critical_count,
            error_rate=round(error_rate, 4),
            latency_p50=round(p50, 2),
            latency_p95=round(p95, 2),
            latency_p99=round(p99, 2),
            latency_mean=round(mean, 2),
            unique_traces=unique_traces,
        )

    def extract_by_service(
        self,
        logs: list[LogEntry],
        window_seconds: int = 60,
    ) -> dict[str, WindowFeatures]:
        """
        Group logs by service_name and extract features per service.
        Returns {service_name: WindowFeatures}.
        """
        grouped: dict[str, list[LogEntry]] = defaultdict(list)
        for log in logs:
            grouped[log.service_name].append(log)

        return {
            svc: self.extract(svc_logs, svc, window_seconds)
            for svc, svc_logs in grouped.items()
        }

    # ------------------------------------------------------------------ #
    #  Private                                                             #
    # ------------------------------------------------------------------ #

    def _extract_latencies(self, logs: list[LogEntry]) -> list[float]:
        latencies = []
        for log in logs:
            for key in _LATENCY_KEYS:
                val = log.attributes.get(key)
                if val is not None:
                    try:
                        latencies.append(float(val))
                        break
                    except (TypeError, ValueError):
                        pass
        return latencies
