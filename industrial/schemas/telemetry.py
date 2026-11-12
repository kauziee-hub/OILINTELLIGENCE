"""Internal telemetry data contract (schema_version 1.0).

Both the synthetic telemetry adapter (simulator) and industrial adapters
(OPC UA / historian / MQTT plant gateways) MUST emit this exact structure.
Event time and ingestion time are kept separate because industrial data may
arrive late or out of order.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"


class Quality(str, Enum):
    GOOD = "GOOD"
    UNCERTAIN = "UNCERTAIN"
    BAD = "BAD"


class Source(str, Enum):
    SIMULATION = "SIMULATION"
    PLANT = "PLANT"
    HISTORIAN = "HISTORIAN"
    MANUAL = "MANUAL"


class TelemetryRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    asset_id: str
    sensor_id: str
    parameter: str
    value: float | None
    unit: str
    event_timestamp: datetime
    ingestion_timestamp: datetime | None = None
    quality: Quality = Quality.GOOD
    source: Source = Source.SIMULATION
    source_tag: str = ""
    run_id: str | None = None
    schema_version: str = SCHEMA_VERSION

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, payload: str | bytes) -> "TelemetryRecord":
        return cls.model_validate_json(payload)


class EventRecord(BaseModel):
    """Discrete operational events (transfers, valve moves, alarms)."""

    model_config = ConfigDict(use_enum_values=True)

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str
    asset_id: str | None = None
    event_timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    source: Source = Source.SIMULATION
    run_id: str | None = None
    schema_version: str = SCHEMA_VERSION


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))
