from pydantic import BaseModel, field_validator
from datetime import datetime, timezone
from typing import Optional
import uuid


class ServiceContext(BaseModel):
    """
    Runtime context fetched by the agent tools before calling the LLM.
    Passed as structured input to the analysis prompt.
    """
    service_name: str
    recent_logs: list[str] = []          # last N log messages
    error_logs: list[str] = []           # only ERROR/CRITICAL messages
    metrics: dict = {}                   # Prometheus metric values
    dependencies: list[str] = []        # services this one calls
    dependents: list[str] = []          # services that call this one
    recent_deployments: list[str] = []  # recent deploy events if known
    fetch_duration_ms: float = 0.0      # how long tool calls took


class RootCauseAnalysis(BaseModel):
    """
    Structured output produced by the LLM after analyzing a ServiceContext.
    """
    id: str = ""
    analyzed_at: datetime = None

    # Core findings
    root_cause: str                      # one-sentence root cause
    confidence: float                    # 0.0 – 1.0
    severity: str                        # low / medium / high / critical

    # Impact
    affected_services: list[str] = []
    affected_users_estimate: str = ""    # "~500 users", "unknown", etc.

    # What happened
    timeline: list[str] = []            # ordered list of events
    contributing_factors: list[str] = []

    # What to do
    recommended_action: str
    remediation_steps: list[str] = []
    escalate_to: str = ""               # "on-call", "database-team", etc.

    # Meta
    llm_model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    analysis_duration_ms: float = 0.0

    def model_post_init(self, __context):
        if not self.id:
            self.id = str(uuid.uuid4())
        if self.analyzed_at is None:
            self.analyzed_at = datetime.now(tz=timezone.utc)

    @field_validator("confidence")
    @classmethod
    def clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @field_validator("severity")
    @classmethod
    def valid_severity(cls, v: str) -> str:
        allowed = {"low", "medium", "high", "critical"}
        if v.lower() not in allowed:
            return "medium"
        return v.lower()


class AnalysisRequest(BaseModel):
    """Input to the agent — comes from AnomalyResult via Redis queue."""
    anomaly_id: str
    service_name: str
    anomaly_score: float
    severity: str
    window_features: dict          # WindowFeatures.model_dump()
    detector_results: list[dict]  # DetectorResult list
    requested_at: datetime = None

    def model_post_init(self, __context):
        if self.requested_at is None:
            self.requested_at = datetime.now(tz=timezone.utc)


class AnalysisResult(BaseModel):
    """Complete output stored to DB and forwarded to alerting."""
    id: str = ""
    request: AnalysisRequest
    context: ServiceContext
    analysis: RootCauseAnalysis
    success: bool = True
    error_message: str = ""

    def model_post_init(self, __context):
        if not self.id:
            self.id = str(uuid.uuid4())
