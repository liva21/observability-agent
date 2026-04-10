from pydantic import BaseModel, field_validator
from datetime import datetime, timezone
from typing import Optional
from enum import Enum
import uuid


class AlertStatus(str, Enum):
    PENDING   = "pending"
    SENT      = "sent"
    FAILED    = "failed"
    SUPPRESSED = "suppressed"    # deduplicated or rate-limited


class AlertSeverity(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


class NotificationChannel(str, Enum):
    SLACK      = "slack"
    PAGERDUTY  = "pagerduty"
    EMAIL      = "email"
    WEBHOOK    = "webhook"


class Alert(BaseModel):
    """
    Represents one alert derived from an AnalysisResult.
    Created by AlertRouter, delivered by channel notifiers.
    """
    id: str = ""
    created_at: datetime = None

    # Source
    analysis_id: str
    anomaly_id: str
    service_name: str

    # Content
    severity: AlertSeverity
    title: str
    root_cause: str
    confidence: float
    recommended_action: str
    affected_services: list[str] = []
    remediation_steps: list[str] = []
    escalate_to: str = ""

    # Delivery state
    status: AlertStatus = AlertStatus.PENDING
    channels_attempted: list[str] = []
    channels_succeeded: list[str] = []
    error_messages: list[str] = []
    sent_at: Optional[datetime] = None

    def model_post_init(self, __context):
        if not self.id:
            self.id = str(uuid.uuid4())
        if self.created_at is None:
            self.created_at = datetime.now(tz=timezone.utc)

    @field_validator("confidence")
    @classmethod
    def clamp(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    def mark_sent(self, channel: str):
        self.channels_succeeded.append(channel)
        self.status = AlertStatus.SENT
        self.sent_at = datetime.now(tz=timezone.utc)

    def mark_failed(self, channel: str, error: str):
        self.channels_attempted.append(channel)
        self.error_messages.append(f"{channel}: {error}")
        if not self.channels_succeeded:
            self.status = AlertStatus.FAILED

    def mark_suppressed(self):
        self.status = AlertStatus.SUPPRESSED


class AlertRule(BaseModel):
    """
    Routing rule: which channels fire for which severity levels.
    """
    severity: AlertSeverity
    channels: list[NotificationChannel]
    require_confidence_above: float = 0.0    # skip if LLM confidence too low
    cooldown_seconds: int = 300              # dedup window per (service, severity)


class DeliveryResult(BaseModel):
    """Result of a single channel delivery attempt."""
    channel: NotificationChannel
    success: bool
    status_code: Optional[int] = None
    response_body: str = ""
    duration_ms: float = 0.0
    error: str = ""
