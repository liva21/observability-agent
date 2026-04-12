"""
schemas.py — FastAPI response models for OpenAPI documentation.

These Pydantic models make Swagger UI show real schemas instead of "string".
"""

from pydantic import BaseModel
from datetime import datetime
from typing import Optional, Any


# ── Logs ────────────────────────────────────────────────────────────

class LogRow(BaseModel):
    id: str
    timestamp: datetime
    service_name: str
    level: str
    message: str
    trace_id: str
    span_id: str
    host: str
    environment: str
    attributes: dict

class LogsResponse(BaseModel):
    logs: list[dict]
    count: int

class LevelBreakdown(BaseModel):
    by_level: dict[str, int]
    by_service: dict[str, int]

class LogStats24h(BaseModel):
    total: int
    error_rate_last_1h_pct: float
    by_level: dict[str, int]
    by_service: dict[str, int]

class LogStatsResponse(BaseModel):
    last_24h: LogStats24h

class ServicesResponse(BaseModel):
    services: list[str]


# ── Anomalies ────────────────────────────────────────────────────────

class AnomalyRow(BaseModel):
    id: str
    analyzed_at: datetime
    service_name: str
    anomaly_id: str
    anomaly_score: float
    severity: str
    root_cause: str
    confidence: float
    recommended_action: str
    affected_services: Any
    escalate_to: str
    success: bool

class AnomaliesResponse(BaseModel):
    anomalies: list[dict]
    count: int

class SeverityBucket(BaseModel):
    severity: str
    count: int
    avg_confidence: float

class TopService(BaseModel):
    service: str
    count: int

class AnomalySummaryResponse(BaseModel):
    total_analyzed: int
    last_24h: int
    by_severity: list[SeverityBucket]
    top_affected_services: list[TopService]

class TrendPoint(BaseModel):
    timestamp: str
    score: float
    severity: str
    confidence: float

class TrendResponse(BaseModel):
    service_name: str
    hours: int
    data_points: list[TrendPoint]


# ── Alerts ───────────────────────────────────────────────────────────

class AlertsResponse(BaseModel):
    alerts: list[dict]
    count: int

class AlertStatsResponse(BaseModel):
    total: int
    sent: int
    suppressed: int
    by_severity: dict[str, int]


# ── Analysis ─────────────────────────────────────────────────────────

class AnalysisRow(BaseModel):
    id: str
    analyzed_at: datetime
    service_name: str
    severity: str
    root_cause: str
    confidence: float
    recommended_action: Optional[str]
    affected_services: Any
    escalate_to: str
    success: bool

class AnalysesResponse(BaseModel):
    analyses: list[dict]
    count: int

class ConfidenceBucket(BaseModel):
    bucket: str
    count: int
    avg_confidence: float

class ConfidenceResponse(BaseModel):
    total_analyses: int
    average_confidence: float
    distribution: list[ConfidenceBucket]


# ── Metrics ──────────────────────────────────────────────────────────

class IngestionKPIs(BaseModel):
    logs_last_1h: int
    logs_last_24h: int
    error_rate_1h_pct: float

class DetectionKPIs(BaseModel):
    anomalies_last_24h: int
    critical_last_24h: int
    avg_confidence: float
    avg_analysis_ms: float

class AlertingKPIs(BaseModel):
    sent_last_24h: int
    suppressed_last_24h: int

class MetricsSummaryResponse(BaseModel):
    ingestion: IngestionKPIs
    detection: DetectionKPIs
    alerting: AlertingKPIs

class StageHealth(BaseModel):
    status: str

class PipelineHealthResponse(BaseModel):
    stages: dict[str, dict]


# ── System ───────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    service: str

class RootResponse(BaseModel):
    service: str
    version: str
    docs: str
    websocket: str
