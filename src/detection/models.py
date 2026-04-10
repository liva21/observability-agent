from pydantic import BaseModel, field_validator
from datetime import datetime, timezone
from typing import Optional
import uuid


class WindowFeatures(BaseModel):
    """
    Aggregated metrics computed over a sliding time window
    for a single service. This is the input to all detectors.
    """
    window_id: str = ""
    service_name: str
    window_start: datetime
    window_end: datetime
    window_seconds: int = 60

    # Volume
    total_logs: int = 0
    logs_per_second: float = 0.0

    # Error metrics
    error_count: int = 0
    critical_count: int = 0
    error_rate: float = 0.0          # 0.0 – 1.0

    # Latency (parsed from attributes["latency_ms"])
    latency_p50: float = 0.0
    latency_p95: float = 0.0
    latency_p99: float = 0.0
    latency_mean: float = 0.0

    # Unique traces — high unique-trace-per-log ratio = many short requests
    unique_traces: int = 0

    def model_post_init(self, __context):
        if not self.window_id:
            self.window_id = str(uuid.uuid4())


class DetectorResult(BaseModel):
    """Score (0–1) from a single detector algorithm."""
    detector_name: str
    score: float                      # 0.0 = normal, 1.0 = certain anomaly
    triggered_on: list[str] = []      # which features drove the score
    details: dict = {}

    @field_validator("score")
    @classmethod
    def clamp_score(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


class AnomalyResult(BaseModel):
    """Final ensemble result for one service window."""
    id: str = ""
    detected_at: datetime = None
    service_name: str
    window_id: str
    anomaly_score: float              # ensemble score 0–1
    is_anomaly: bool
    severity: str                     # normal / low / medium / high / critical
    detector_results: list[DetectorResult] = []
    features: WindowFeatures
    recommended_action: str = ""

    def model_post_init(self, __context):
        if not self.id:
            self.id = str(uuid.uuid4())
        if self.detected_at is None:
            self.detected_at = datetime.now(tz=timezone.utc)

    @field_validator("anomaly_score")
    @classmethod
    def clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, v))
