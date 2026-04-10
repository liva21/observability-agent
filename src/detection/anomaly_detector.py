"""
anomaly_detector.py — Ensemble anomaly detector.

Combines StatisticalDetector and MLDetector using a weighted ensemble.
Converts the final score into a severity level and AnomalyResult.

Weights:
  - Statistical: 0.60  (faster, more interpretable, no warm-up penalty)
  - Isolation Forest: 0.40 (catches non-linear patterns)
"""

import logging
from datetime import datetime, timezone

from .models import WindowFeatures, AnomalyResult, DetectorResult
from .statistical_detector import StatisticalDetector
from .ml_detector import MLDetector

logger = logging.getLogger(__name__)

# Score → severity mapping (upper bound exclusive)
SEVERITY_THRESHOLDS = [
    (0.30, "normal"),
    (0.50, "low"),
    (0.70, "medium"),
    (0.85, "high"),
    (1.01, "critical"),
]

# Minimum score to flag as is_anomaly = True
ANOMALY_THRESHOLD = 0.50

# Detector weights
WEIGHTS = {
    "statistical":     0.60,
    "isolation_forest": 0.40,
}

RECOMMENDED_ACTIONS = {
    "normal":   "No action required.",
    "low":      "Monitor closely over the next 5 minutes.",
    "medium":   "Investigate logs; consider scaling or alerting on-call.",
    "high":     "Immediate investigation required — check dependent services.",
    "critical": "Page on-call immediately — potential service outage.",
}


class AnomalyDetector:
    """
    Top-level detector used by the consumer pipeline.

    Usage:
        detector = AnomalyDetector()

        # Called once per window batch (from Kafka consumer callback)
        result = detector.analyze(features)

        if result.is_anomaly:
            await redis_queue.push(result.model_dump_json())
    """

    def __init__(self):
        self.stat_detector = StatisticalDetector()
        self.ml_detector   = MLDetector()
        self._results_history: list[AnomalyResult] = []

    # ------------------------------------------------------------------ #
    #  Public                                                              #
    # ------------------------------------------------------------------ #

    def analyze(self, features: WindowFeatures) -> AnomalyResult:
        """
        Run both detectors and return a fused AnomalyResult.
        Always returns a result (score=0 during warm-up).
        """
        stat_result = self.stat_detector.score(features)
        ml_result   = self.ml_detector.score(features)

        ensemble_score = self._fuse(stat_result, ml_result)
        severity       = self._severity(ensemble_score)
        is_anomaly     = ensemble_score >= ANOMALY_THRESHOLD

        result = AnomalyResult(
            service_name=features.service_name,
            window_id=features.window_id,
            anomaly_score=round(ensemble_score, 4),
            is_anomaly=is_anomaly,
            severity=severity,
            detector_results=[stat_result, ml_result],
            features=features,
            recommended_action=RECOMMENDED_ACTIONS[severity],
        )

        self._results_history.append(result)
        # Keep last 1000 in memory
        if len(self._results_history) > 1000:
            self._results_history.pop(0)

        log_fn = logger.warning if is_anomaly else logger.debug
        log_fn(
            "service=%s score=%.3f severity=%s is_anomaly=%s",
            features.service_name, ensemble_score, severity, is_anomaly,
        )
        return result

    def analyze_batch(
        self, features_by_service: dict[str, WindowFeatures]
    ) -> list[AnomalyResult]:
        """Analyze multiple services in one call. Returns all results."""
        return [self.analyze(f) for f in features_by_service.values()]

    def recent_anomalies(self, limit: int = 20) -> list[AnomalyResult]:
        """Return the most recent flagged anomalies from memory."""
        flagged = [r for r in self._results_history if r.is_anomaly]
        return flagged[-limit:]

    def stats(self) -> dict:
        total   = len(self._results_history)
        flagged = sum(1 for r in self._results_history if r.is_anomaly)
        return {
            "total_windows_analyzed": total,
            "anomalies_detected": flagged,
            "anomaly_rate": round(flagged / total, 4) if total else 0.0,
            "tracked_services": self.stat_detector.tracked_services,
        }

    # ------------------------------------------------------------------ #
    #  Private                                                             #
    # ------------------------------------------------------------------ #

    def _fuse(
        self,
        stat: DetectorResult,
        ml: DetectorResult,
    ) -> float:
        stat_w = WEIGHTS["statistical"]
        ml_w   = WEIGHTS["isolation_forest"]

        # If ML is still warming up, put full weight on statistical
        if ml.details.get("status") == "warming_up":
            stat_w, ml_w = 1.0, 0.0

        return min(stat.score * stat_w + ml.score * ml_w, 1.0)

    def _severity(self, score: float) -> str:
        for upper, label in SEVERITY_THRESHOLDS:
            if score < upper:
                return label
        return "critical"
