from pydantic import BaseModel, field_validator
from datetime import datetime
from typing import Optional
from enum import Enum
import uuid


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogEntry(BaseModel):
    id: str = ""
    timestamp: datetime
    service_name: str
    level: LogLevel
    message: str
    trace_id: str = ""
    span_id: str = ""
    host: str = ""
    environment: str = "production"
    attributes: dict = {}

    def model_post_init(self, __context):
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.trace_id:
            self.trace_id = str(uuid.uuid4())
        if not self.span_id:
            self.span_id = str(uuid.uuid4())[:16]

    @field_validator("service_name")
    @classmethod
    def service_name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("service_name cannot be empty")
        return v.strip().lower()

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message cannot be empty")
        return v.strip()


class AnomalyEvent(BaseModel):
    id: str = ""
    detected_at: datetime
    service_name: str
    anomaly_score: float
    log_entry: LogEntry
    window_stats: dict = {}

    def model_post_init(self, __context):
        if not self.id:
            self.id = str(uuid.uuid4())
