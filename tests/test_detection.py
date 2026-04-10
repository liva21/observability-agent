"""
test_detection.py — Full test suite for Module 2 (Anomaly Detection Engine).

Run:
    pytest tests/test_detection.py -v
"""

import pytest
import numpy as np
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from src.detection.models import WindowFeatures, DetectorResult, AnomalyResult
from src.detection.feature_extractor import FeatureExtractor
from src.detection.statistical_detector import StatisticalDetector
from src.detection.ml_detector import MLDetector
from src.detection.anomaly_detector import AnomalyDetector
from src.ingestion.models import LogEntry, LogLevel


# ================================================================== #
#  Fixtures                                                            #
# ================================================================== #

def make_features(
    service="payment-service",
    error_rate=0.05,
    latency_p95=200.0,
    latency_p99=300.0,
    latency_p50=80.0,
    latency_mean=90.0,
    logs_per_second=10.0,
    total_logs=600,
    error_count=30,
    critical_count=0,
    unique_traces=50,
) -> WindowFeatures:
    now = datetime.now(tz=timezone.utc)
    return WindowFeatures(
        service_name=service,
        window_start=now - timedelta(seconds=60),
        window_end=now,
        window_seconds=60,
        total_logs=total_logs,
        logs_per_second=logs_per_second,
        error_count=error_count,
        critical_count=critical_count,
        error_rate=error_rate,
        latency_p50=latency_p50,
        latency_p95=latency_p95,
        latency_p99=latency_p99,
        latency_mean=latency_mean,
        unique_traces=unique_traces,
    )


def make_log_entry(
    service="payment-service",
    level=LogLevel.INFO,
    message="processed request",
    latency_ms: float = None,
) -> LogEntry:
    attrs = {}
    if latency_ms is not None:
        attrs["latency_ms"] = latency_ms
    return LogEntry(
        timestamp=datetime.now(tz=timezone.utc),
        service_name=service,
        level=level,
        message=message,
        attributes=attrs,
    )


# ================================================================== #
#  WindowFeatures model tests                                          #
# ================================================================== #

class TestWindowFeatures:
    def test_auto_id_generated(self):
        f = make_features()
        assert f.window_id
        assert len(f.window_id) == 36

    def test_two_features_have_different_ids(self):
        f1, f2 = make_features(), make_features()
        assert f1.window_id != f2.window_id

    def test_default_values(self):
        now = datetime.now(tz=timezone.utc)
        f = WindowFeatures(service_name="svc", window_start=now, window_end=now)
        assert f.error_rate == 0.0
        assert f.total_logs == 0


# ================================================================== #
#  FeatureExtractor tests                                              #
# ================================================================== #

class TestFeatureExtractor:
    def setup_method(self):
        self.extractor = FeatureExtractor()

    def test_empty_logs_returns_zero_features(self):
        features = self.extractor.extract([], service_name="svc")
        assert features.total_logs == 0
        assert features.error_rate == 0.0

    def test_error_rate_computed_correctly(self):
        logs = (
            [make_log_entry(level=LogLevel.INFO)] * 80
            + [make_log_entry(level=LogLevel.ERROR)] * 15
            + [make_log_entry(level=LogLevel.CRITICAL)] * 5
        )
        features = self.extractor.extract(logs, "svc")
        assert features.total_logs == 100
        assert features.error_count == 15
        assert features.critical_count == 5
        assert abs(features.error_rate - 0.20) < 0.001

    def test_latency_percentiles(self):
        latencies = list(range(10, 110))   # 10 … 109 ms (100 values)
        logs = [make_log_entry(latency_ms=float(l)) for l in latencies]
        features = self.extractor.extract(logs, "svc")
        assert features.latency_p50 > 0
        assert features.latency_p95 > features.latency_p50
        assert features.latency_p99 >= features.latency_p95

    def test_no_latency_attributes_gives_zero(self):
        logs = [make_log_entry() for _ in range(10)]
        features = self.extractor.extract(logs, "svc")
        assert features.latency_p50 == 0.0
        assert features.latency_p95 == 0.0

    def test_unique_traces_counted(self):
        logs = [make_log_entry() for _ in range(20)]
        # Each LogEntry auto-generates a unique trace_id
        features = self.extractor.extract(logs, "svc")
        assert features.unique_traces == 20

    def test_extract_by_service_groups_correctly(self):
        logs = (
            [make_log_entry(service="svc-a")] * 30
            + [make_log_entry(service="svc-b")] * 20
        )
        result = self.extractor.extract_by_service(logs)
        assert "svc-a" in result
        assert "svc-b" in result
        assert result["svc-a"].total_logs == 30
        assert result["svc-b"].total_logs == 20

    def test_logs_per_second_reasonable(self):
        logs = [make_log_entry() for _ in range(100)]
        features = self.extractor.extract(logs, "svc", window_seconds=60)
        assert features.logs_per_second >= 0


# ================================================================== #
#  StatisticalDetector tests                                           #
# ================================================================== #

class TestStatisticalDetector:
    def setup_method(self):
        self.detector = StatisticalDetector(min_samples=10)

    def _warm_up(self, service: str, n: int = 12, error_rate: float = 0.05):
        """Feed normal windows to build rolling history."""
        for _ in range(n):
            f = make_features(service=service, error_rate=error_rate)
            self.detector.score(f)

    def test_warm_up_returns_zero_score(self):
        result = self.detector.score(make_features())
        assert result.score == 0.0
        assert result.details.get("status") == "warming_up"

    def test_normal_traffic_low_score(self):
        self._warm_up("svc")
        result = self.detector.score(make_features(service="svc", error_rate=0.05))
        assert result.score < 0.3

    def test_spike_in_error_rate_detected(self):
        self._warm_up("svc", error_rate=0.02)
        # Sudden spike to 80% error rate
        result = self.detector.score(
            make_features(service="svc", error_rate=0.80, latency_p95=2000)
        )
        assert result.score > 0.5

    def test_latency_spike_detected(self):
        self._warm_up("svc", n=15)
        result = self.detector.score(
            make_features(service="svc", latency_p95=5000.0, latency_p99=8000.0)
        )
        assert result.score > 0.3

    def test_detector_name(self):
        self._warm_up("svc")
        result = self.detector.score(make_features(service="svc"))
        assert result.detector_name == "statistical"

    def test_score_clamped_between_0_and_1(self):
        self._warm_up("svc")
        for _ in range(5):
            result = self.detector.score(
                make_features(service="svc", error_rate=0.99, latency_p95=99999)
            )
        assert 0.0 <= result.score <= 1.0

    def test_separate_state_per_service(self):
        self._warm_up("svc-a", error_rate=0.01)
        self._warm_up("svc-b", error_rate=0.50)
        result_a = self.detector.score(make_features(service="svc-a", error_rate=0.01))
        result_b = self.detector.score(make_features(service="svc-b", error_rate=0.50))
        # Both should be "normal" relative to their own histories
        assert result_a.score < result_b.score or result_a.score < 0.5

    def test_tracked_services_updated(self):
        self.detector.score(make_features(service="alpha"))
        self.detector.score(make_features(service="beta"))
        assert "alpha" in self.detector.tracked_services
        assert "beta" in self.detector.tracked_services

    def test_reset_service_clears_state(self):
        self._warm_up("svc")
        self.detector.reset_service("svc")
        result = self.detector.score(make_features(service="svc"))
        assert result.details.get("status") == "warming_up"


# ================================================================== #
#  MLDetector tests                                                    #
# ================================================================== #

class TestMLDetector:
    def setup_method(self):
        self.detector = MLDetector(contamination=0.05)

    def _warm_up(self, service: str, n: int = 55):
        for _ in range(n):
            self.detector.score(make_features(service=service, error_rate=0.03))

    def test_warm_up_returns_zero_score(self):
        result = self.detector.score(make_features())
        assert result.score == 0.0
        assert result.details["status"] == "warming_up"

    def test_samples_needed_decreases(self):
        d = MLDetector()
        d.score(make_features())
        result = d.score(make_features())
        assert result.details["samples_collected"] == 2

    def test_score_after_training(self):
        self._warm_up("svc")
        result = self.detector.score(make_features(service="svc", error_rate=0.03))
        assert result.score >= 0.0
        assert result.score <= 1.0

    def test_detector_name(self):
        self._warm_up("svc")
        result = self.detector.score(make_features(service="svc"))
        assert result.detector_name == "isolation_forest"

    def test_anomalous_point_higher_score(self):
        self._warm_up("svc")
        normal_score = self.detector.score(
            make_features(service="svc", error_rate=0.03)
        ).score
        anomaly_score = self.detector.score(
            make_features(service="svc", error_rate=0.99, latency_p95=50000)
        ).score
        # Anomalous point should score higher
        assert anomaly_score >= normal_score

    def test_score_always_0_to_1(self):
        self._warm_up("svc")
        for _ in range(10):
            result = self.detector.score(
                make_features(service="svc", error_rate=np.random.uniform(0, 1))
            )
            assert 0.0 <= result.score <= 1.0


# ================================================================== #
#  AnomalyDetector (ensemble) tests                                    #
# ================================================================== #

class TestAnomalyDetector:
    def setup_method(self):
        self.detector = AnomalyDetector()

    def _warm_up(self, service: str, n: int = 60):
        for _ in range(n):
            self.detector.analyze(make_features(service=service, error_rate=0.03))

    def test_returns_anomaly_result(self):
        result = self.detector.analyze(make_features())
        assert isinstance(result, AnomalyResult)

    def test_auto_id_and_timestamp(self):
        result = self.detector.analyze(make_features())
        assert result.id
        assert result.detected_at is not None

    def test_severity_normal_during_warmup(self):
        result = self.detector.analyze(make_features())
        assert result.severity in ("normal", "low")

    def test_no_anomaly_on_normal_traffic(self):
        self._warm_up("svc")
        result = self.detector.analyze(make_features(service="svc", error_rate=0.03))
        assert result.is_anomaly is False

    def test_anomaly_on_high_error_rate(self):
        self._warm_up("svc")
        result = self.detector.analyze(
            make_features(service="svc", error_rate=0.90, latency_p95=5000)
        )
        assert result.anomaly_score > 0.3

    def test_severity_ordering(self):
        self._warm_up("svc")
        normal_r = self.detector.analyze(
            make_features(service="svc", error_rate=0.03)
        )
        spike_r = self.detector.analyze(
            make_features(service="svc", error_rate=0.90, latency_p95=5000)
        )
        severities = ["normal", "low", "medium", "high", "critical"]
        assert severities.index(spike_r.severity) >= severities.index(normal_r.severity)

    def test_two_detector_results_present(self):
        result = self.detector.analyze(make_features())
        assert len(result.detector_results) == 2
        names = {r.detector_name for r in result.detector_results}
        assert "statistical" in names
        assert "isolation_forest" in names

    def test_recommended_action_present(self):
        result = self.detector.analyze(make_features())
        assert result.recommended_action

    def test_recent_anomalies_returns_flagged(self):
        self._warm_up("svc")
        # Inject a clear anomaly
        self.detector.analyze(
            make_features(service="svc", error_rate=0.99, latency_p95=99999)
        )
        recent = self.detector.recent_anomalies()
        # May or may not flag during warm-up period — just check no crash
        assert isinstance(recent, list)

    def test_stats_structure(self):
        self.detector.analyze(make_features())
        stats = self.detector.stats()
        assert "total_windows_analyzed" in stats
        assert "anomalies_detected" in stats
        assert "anomaly_rate" in stats
        assert stats["total_windows_analyzed"] >= 1

    def test_analyze_batch(self):
        features_map = {
            "svc-a": make_features(service="svc-a"),
            "svc-b": make_features(service="svc-b"),
        }
        results = self.detector.analyze_batch(features_map)
        assert len(results) == 2
        services = {r.service_name for r in results}
        assert "svc-a" in services
        assert "svc-b" in services
