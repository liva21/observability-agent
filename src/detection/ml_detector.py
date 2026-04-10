"""
ml_detector.py — Isolation Forest anomaly detector with online retraining.

Isolation Forest is well-suited for log anomaly detection:
  - Works without labelled data (unsupervised)
  - Handles high-dimensional feature vectors
  - Efficient on small-to-medium datasets
  - Naturally produces anomaly scores in [-1, 1]

Strategy:
  - Collect features in a buffer until MIN_TRAIN_SAMPLES reached
  - Train an initial model; retrain every RETRAIN_INTERVAL windows
  - Score each incoming WindowFeatures vector
  - Normalise IForest scores to 0–1 range
"""

import logging
import numpy as np
from dataclasses import dataclass, field
from typing import Optional

from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from .models import WindowFeatures, DetectorResult

logger = logging.getLogger(__name__)

MIN_TRAIN_SAMPLES = 50       # windows before first model fit
RETRAIN_INTERVAL  = 200      # retrain every N windows
CONTAMINATION     = 0.05     # expected anomaly fraction (5%)

# Feature vector column order — must stay consistent
FEATURE_COLUMNS = [
    "error_rate",
    "critical_count",
    "latency_p50",
    "latency_p95",
    "latency_p99",
    "logs_per_second",
    "unique_traces",
]


@dataclass
class ServiceMLState:
    buffer: list[list[float]] = field(default_factory=list)
    model: Optional[IsolationForest] = None
    scaler: Optional[StandardScaler] = None
    windows_seen: int = 0
    last_train_at: int = 0


class MLDetector:
    """
    Per-service Isolation Forest detector.

    Usage:
        detector = MLDetector()
        result = detector.score(features)   # returns DetectorResult
    """

    def __init__(
        self,
        contamination: float = CONTAMINATION,
        n_estimators: int = 100,
        random_state: int = 42,
    ):
        self.contamination  = contamination
        self.n_estimators   = n_estimators
        self.random_state   = random_state
        self._state: dict[str, ServiceMLState] = {}

    # ------------------------------------------------------------------ #
    #  Public                                                              #
    # ------------------------------------------------------------------ #

    def score(self, features: WindowFeatures) -> DetectorResult:
        svc   = features.service_name
        state = self._get_state(svc)
        vec   = self._to_vector(features)

        state.buffer.append(vec)
        state.windows_seen += 1

        # Not enough data yet
        if len(state.buffer) < MIN_TRAIN_SAMPLES:
            return DetectorResult(
                detector_name="isolation_forest",
                score=0.0,
                details={
                    "status": "warming_up",
                    "samples_collected": len(state.buffer),
                    "samples_needed": MIN_TRAIN_SAMPLES,
                },
            )

        # Retrain if needed
        should_retrain = (
            state.model is None
            or (state.windows_seen - state.last_train_at) >= RETRAIN_INTERVAL
        )
        if should_retrain:
            self._train(svc, state)

        # Score
        raw_score = self._predict_score(vec, state)

        return DetectorResult(
            detector_name="isolation_forest",
            score=round(raw_score, 4),
            triggered_on=self._identify_drivers(vec, state) if raw_score > 0.5 else [],
            details={
                "windows_seen": state.windows_seen,
                "buffer_size": len(state.buffer),
                "last_trained_at_window": state.last_train_at,
                "raw_if_score": round(raw_score, 4),
            },
        )

    @property
    def tracked_services(self) -> list[str]:
        return list(self._state.keys())

    def reset_service(self, service: str):
        self._state.pop(service, None)

    # ------------------------------------------------------------------ #
    #  Private                                                             #
    # ------------------------------------------------------------------ #

    def _get_state(self, service: str) -> ServiceMLState:
        if service not in self._state:
            self._state[service] = ServiceMLState()
        return self._state[service]

    def _to_vector(self, features: WindowFeatures) -> list[float]:
        return [float(getattr(features, col, 0.0)) for col in FEATURE_COLUMNS]

    def _train(self, service: str, state: ServiceMLState):
        logger.info(
            "Training Isolation Forest for '%s' on %d samples",
            service, len(state.buffer),
        )
        X = np.array(state.buffer)

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        model = IsolationForest(
            n_estimators=self.n_estimators,
            contamination=self.contamination,
            random_state=self.random_state,
            n_jobs=-1,
        )
        model.fit(X_scaled)

        state.model  = model
        state.scaler = scaler
        state.last_train_at = state.windows_seen
        logger.info("Isolation Forest trained for '%s'", service)

    def _predict_score(self, vec: list[float], state: ServiceMLState) -> float:
        """
        IsolationForest.decision_function() returns higher values for
        normal samples (positive = inlier, negative = outlier).
        We invert and normalise to 0–1.
        """
        X = np.array(vec).reshape(1, -1)
        X_scaled = state.scaler.transform(X)

        # decision_function: more negative = more anomalous
        raw = float(state.model.decision_function(X_scaled)[0])

        # Empirical normalisation: typical range is roughly [-0.5, 0.5]
        # Map so that 0 = normal boundary, >0.5 decision = score ~1
        score = max(0.0, -raw * 2.0)
        return min(score, 1.0)

    def _identify_drivers(
        self, vec: list[float], state: ServiceMLState
    ) -> list[str]:
        """
        Identify which features most contributed to the anomaly score
        by computing per-feature z-scores against the training buffer.
        """
        buffer_arr = np.array(state.buffer)
        means = np.mean(buffer_arr, axis=0)
        stds  = np.std(buffer_arr, axis=0) + 1e-9
        z_scores = np.abs((np.array(vec) - means) / stds)

        # Return feature names where z > 2.0
        drivers = [
            FEATURE_COLUMNS[i]
            for i, z in enumerate(z_scores)
            if z > 2.0
        ]
        return drivers
