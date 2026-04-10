"""
statistical_detector.py — Rolling statistical anomaly detection.

Three algorithms run in parallel on each numeric feature:
  1. Z-score   — how many std-devs from the rolling mean?
  2. IQR fence — is the value beyond Q3 + 1.5*IQR?
  3. CUSUM     — cumulative sum control chart for drift detection

Each detector maintains a separate rolling window per (service, feature).
The final score is the max across all feature scores.
"""

import logging
import numpy as np
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .models import WindowFeatures, DetectorResult

logger = logging.getLogger(__name__)

# Features we run statistics on, with (weight, higher_is_worse flag)
FEATURE_CONFIG: dict[str, tuple[float, bool]] = {
    "error_rate":    (0.40, True),
    "latency_p95":   (0.30, True),
    "latency_p99":   (0.15, True),
    "logs_per_second": (0.15, True),   # sudden traffic spike
}


@dataclass
class FeatureWindow:
    """Rolling history for one (service, feature) pair."""
    values: deque = field(default_factory=lambda: deque(maxlen=200))
    cusum_pos: float = 0.0
    cusum_neg: float = 0.0
    cusum_target: Optional[float] = None   # initialised from first N samples


class StatisticalDetector:
    """
    Maintains per-service rolling windows and scores incoming
    WindowFeatures objects.

    Usage:
        detector = StatisticalDetector()
        result = detector.score(features)
    """

    def __init__(
        self,
        z_threshold: float = 3.0,
        min_samples: int = 15,
        cusum_k: float = 0.5,     # allowance (slack)
        cusum_h: float = 5.0,     # decision interval
    ):
        self.z_threshold  = z_threshold
        self.min_samples  = min_samples
        self.cusum_k      = cusum_k
        self.cusum_h      = cusum_h
        # _state[service][feature] = FeatureWindow
        self._state: dict[str, dict[str, FeatureWindow]] = {}

    # ------------------------------------------------------------------ #
    #  Public                                                              #
    # ------------------------------------------------------------------ #

    def score(self, features: WindowFeatures) -> DetectorResult:
        svc = features.service_name
        if svc not in self._state:
            self._state[svc] = {}

        feature_scores: dict[str, float] = {}

        for feat_name, (weight, _) in FEATURE_CONFIG.items():
            value = getattr(features, feat_name, None)
            if value is None:
                continue

            win = self._get_window(svc, feat_name)

            if len(win.values) < self.min_samples:
                # Not enough history yet — update window and skip scoring
                win.values.append(value)
                continue

            z_score   = self._z_score(value, win)
            iqr_score = self._iqr_score(value, win)
            cusum_sc  = self._cusum_score(value, win)

            # Take the max across methods, then weight by feature importance
            raw = max(z_score, iqr_score, cusum_sc)
            feature_scores[feat_name] = raw * weight

            # Update window after scoring
            win.values.append(value)

        if not feature_scores:
            return DetectorResult(
                detector_name="statistical",
                score=0.0,
                details={"status": "warming_up"},
            )

        total_score = min(sum(feature_scores.values()), 1.0)
        triggered   = [f for f, s in feature_scores.items() if s > 0.15]

        return DetectorResult(
            detector_name="statistical",
            score=round(total_score, 4),
            triggered_on=triggered,
            details={
                "feature_scores": {k: round(v, 4) for k, v in feature_scores.items()},
                "window_size": len(self._state[svc].get(
                    next(iter(FEATURE_CONFIG)), FeatureWindow()
                ).values),
            },
        )

    # ------------------------------------------------------------------ #
    #  Algorithms                                                          #
    # ------------------------------------------------------------------ #

    def _z_score(self, value: float, win: FeatureWindow) -> float:
        arr  = np.array(win.values)
        mean = np.mean(arr)
        std  = np.std(arr)
        if std < 1e-9:
            return 0.0
        z = abs((value - mean) / std)
        return min(z / self.z_threshold, 1.0)

    def _iqr_score(self, value: float, win: FeatureWindow) -> float:
        arr = np.array(win.values)
        q1, q3 = np.percentile(arr, [25, 75])
        iqr    = q3 - q1
        if iqr < 1e-9:
            return 0.0
        upper_fence = q3 + 1.5 * iqr
        lower_fence = q1 - 1.5 * iqr
        if value > upper_fence:
            return min((value - upper_fence) / (iqr + 1e-9), 1.0)
        if value < lower_fence:
            return min((lower_fence - value) / (iqr + 1e-9), 1.0)
        return 0.0

    def _cusum_score(self, value: float, win: FeatureWindow) -> float:
        if win.cusum_target is None:
            win.cusum_target = float(np.mean(win.values))

        std = float(np.std(win.values)) or 1.0
        k   = self.cusum_k * std

        win.cusum_pos = max(0.0, win.cusum_pos + (value - win.cusum_target) - k)
        win.cusum_neg = max(0.0, win.cusum_neg - (value - win.cusum_target) - k)

        cusum_val = max(win.cusum_pos, win.cusum_neg)
        threshold = self.cusum_h * std
        return min(cusum_val / (threshold + 1e-9), 1.0)

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _get_window(self, service: str, feature: str) -> FeatureWindow:
        if feature not in self._state[service]:
            self._state[service][feature] = FeatureWindow()
        return self._state[service][feature]

    def reset_service(self, service: str):
        """Call this when a service is redeployed / renamed."""
        self._state.pop(service, None)

    @property
    def tracked_services(self) -> list[str]:
        return list(self._state.keys())
