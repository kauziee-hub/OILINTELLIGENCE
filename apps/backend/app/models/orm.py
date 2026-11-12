"""SQLAlchemy ORM schema (section 9.4).

Raw telemetry is append-only at the application level: there is no update path
for telemetry rows; corrections / reprocessing create derived records
(balance_results, model_predictions) and never overwrite measurements.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TruthBase(DeclarativeBase):
    pass


TS = DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[str] = mapped_column(String(16))
    password_hash: Mapped[str] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Asset(Base):
    __tablename__ = "assets"
    asset_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset_type: Mapped[str] = mapped_column(String(32))
    asset_name: Mapped[str] = mapped_column(String(128))
    location: Mapped[str] = mapped_column(String(64), default="TF-01")
    parent_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    operating_status: Mapped[str] = mapped_column(String(32), default="IN_SERVICE")
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)


class AssetRelationship(Base):
    __tablename__ = "asset_relationships"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_asset: Mapped[str] = mapped_column(String(32))
    to_asset: Mapped[str] = mapped_column(String(32))
    relation_type: Mapped[str] = mapped_column(String(32))
    properties: Mapped[dict] = mapped_column(JSON, default=dict)


class Sensor(Base):
    __tablename__ = "sensors"
    sensor_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(32), index=True)
    sensor_type: Mapped[str] = mapped_column(String(32))
    parameter: Mapped[str] = mapped_column(String(32))
    unit: Mapped[str] = mapped_column(String(16))
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)


class Telemetry(Base):
    __tablename__ = "telemetry"
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_timestamp: Mapped[datetime] = mapped_column(TS, primary_key=True)
    ts_epoch: Mapped[float] = mapped_column(Float)
    ingestion_timestamp: Mapped[datetime] = mapped_column(TS, default=utcnow)
    asset_id: Mapped[str] = mapped_column(String(32))
    sensor_id: Mapped[str] = mapped_column(String(32))
    parameter: Mapped[str] = mapped_column(String(32))
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(16))
    quality: Mapped[str] = mapped_column(String(12))
    source: Mapped[str] = mapped_column(String(12))
    source_tag: Mapped[str] = mapped_column(String(128), default="")
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    schema_version: Mapped[str] = mapped_column(String(8), default="1.0")
    __table_args__ = (Index("ix_telemetry_sensor_ts", "sensor_id", "ts_epoch"),
                      Index("ix_telemetry_source_ts", "source", "ts_epoch"))


class Transfer(Base):
    __tablename__ = "transfers"
    transfer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    route: Mapped[str] = mapped_column(String(16))
    source_tank: Mapped[str] = mapped_column(String(16))
    destination_tank: Mapped[str] = mapped_column(String(16))
    start_time: Mapped[datetime] = mapped_column(TS)
    end_time: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    recorded_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    recorded_volume_m3: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity_unit: Mapped[str] = mapped_column(String(8), default="t")
    product_id: Mapped[str] = mapped_column(String(32), default="")
    custody_meter: Mapped[str] = mapped_column(String(16), default="")
    status: Mapped[str] = mapped_column(String(16))
    opening_gauges: Mapped[dict] = mapped_column(JSON, default=dict)
    closing_gauges: Mapped[dict] = mapped_column(JSON, default=dict)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(12), default="SIMULATION")


class CalibrationRecord(Base):
    __tablename__ = "calibration_records"
    record_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sensor_id: Mapped[str] = mapped_column(String(32), index=True)
    record_type: Mapped[str] = mapped_column(String(48))
    timestamp: Mapped[datetime] = mapped_column(TS)
    result: Mapped[str] = mapped_column(String(16))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class LabSample(Base):
    __tablename__ = "lab_samples"
    sample_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tank_id: Mapped[str] = mapped_column(String(16), index=True)
    timestamp: Mapped[datetime] = mapped_column(TS)
    density_15c: Mapped[float] = mapped_column(Float)
    bsw_pct: Mapped[float] = mapped_column(Float, default=0.0)
    method: Mapped[str] = mapped_column(String(64), default="")
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class MaintenanceRecord(Base):
    __tablename__ = "maintenance_records"
    record_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(32), index=True)
    record_type: Mapped[str] = mapped_column(String(48))
    timestamp: Mapped[datetime] = mapped_column(TS)
    notes: Mapped[str] = mapped_column(Text, default="")
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class OperatorLog(Base):
    __tablename__ = "operator_logs"
    log_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(TS, index=True)
    asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    category: Mapped[str] = mapped_column(String(32), default="OPERATIONS")
    text: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class SimulationRun(Base):
    __tablename__ = "simulation_runs"
    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(64))
    seed: Mapped[int] = mapped_column(Integer, default=0)
    scenario: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")
    started_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    speed: Mapped[float] = mapped_column(Float, default=1.0)


class BalanceResult(Base):
    __tablename__ = "balance_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    t1: Mapped[datetime] = mapped_column(TS, primary_key=True)
    t0: Mapped[datetime] = mapped_column(TS)
    t1_epoch: Mapped[float] = mapped_column(Float, index=True)
    window_s: Mapped[float] = mapped_column(Float)
    route: Mapped[str | None] = mapped_column(String(16), nullable=True)
    facility_residual_t: Mapped[float] = mapped_column(Float)
    chi2: Mapped[float] = mapped_column(Float)
    dof: Mapped[int] = mapped_column(Integer)
    p_value: Mapped[float] = mapped_column(Float)
    max_abs_z: Mapped[float] = mapped_column(Float)
    anomaly: Mapped[bool] = mapped_column(Boolean, default=False)
    scores: Mapped[dict] = mapped_column(JSON, default=dict)
    rows: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(12), default="SIMULATION")


class ModelPrediction(Base):
    __tablename__ = "model_predictions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    model_name: Mapped[str] = mapped_column(String(48))
    model_version: Mapped[str] = mapped_column(String(64), default="")
    t0: Mapped[float] = mapped_column(Float)
    t1: Mapped[float] = mapped_column(Float)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    investigation_id: Mapped[str | None] = mapped_column(String(16), nullable=True)


class AnomalyEvent(Base):
    __tablename__ = "anomaly_events"
    event_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    detected_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    t0: Mapped[float] = mapped_column(Float)
    t1: Mapped[float] = mapped_column(Float)
    event_type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    assets: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    scores: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    investigation_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str] = mapped_column(String(12), default="SIMULATION")


class Investigation(Base):
    __tablename__ = "investigations"
    investigation_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    event_id: Mapped[str | None] = mapped_column(String(24), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    t0: Mapped[float] = mapped_column(Float)
    t1: Mapped[float] = mapped_column(Float)
    assets: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    top_hypothesis: Mapped[str | None] = mapped_column(String(64), nullable=True)
    top_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(12), default="SIMULATION")


class InvestigationEvidence(Base):
    __tablename__ = "investigation_evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(String(16), index=True)
    evidence_id: Mapped[str] = mapped_column(String(8))
    evidence_type: Mapped[str] = mapped_column(String(24))
    source: Mapped[str] = mapped_column(String(96))
    summary: Mapped[str] = mapped_column(Text)
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Recommendation(Base):
    __tablename__ = "recommendations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(String(16), index=True)
    action_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(48))
    target: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expected_information_gain_bits: Mapped[float] = mapped_column(Float)
    cost: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class EngineerReview(Base):
    __tablename__ = "engineer_reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(String(16), index=True)
    reviewer: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32))
    verified_cause: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    comments: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class DiagnosticCase(Base):
    __tablename__ = "diagnostic_cases"
    case_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    residual: Mapped[float | None] = mapped_column(Float, nullable=True)
    uncertainty: Mapped[float | None] = mapped_column(Float, nullable=True)
    candidate_faults: Mapped[list] = mapped_column(JSON, default=list)
    verified_cause: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verification_method: Mapped[str] = mapped_column(String(256), default="")
    reviewer_status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(TS, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), default="")
    action: Mapped[str] = mapped_column(String(96))
    resource: Mapped[str] = mapped_column(String(128), default="")
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")


class Connector(Base):
    __tablename__ = "connectors"
    connector_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    connector_type: Mapped[str] = mapped_column(String(16))     # mqtt | opcua | historian
    name: Mapped[str] = mapped_column(String(128))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    last_status: Mapped[dict] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------- truth database
class GroundTruth(TruthBase):
    __tablename__ = "ground_truth"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(TS, index=True)
    sim_t: Mapped[float] = mapped_column(Float)
    asset_id: Mapped[str] = mapped_column(String(32))
    true_mass: Mapped[float | None] = mapped_column(Float, nullable=True)
    true_flow: Mapped[float | None] = mapped_column(Float, nullable=True)
    true_physical_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    true_sensor_bias: Mapped[float | None] = mapped_column(Float, nullable=True)
    true_fault_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    true_fault_location: Mapped[str | None] = mapped_column(String(32), nullable=True)
    known_fault_state: Mapped[bool] = mapped_column(Boolean, default=False)


class FaultEvent(TruthBase):
    __tablename__ = "fault_events"
    fault_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    fault_type: Mapped[str] = mapped_column(String(32))
    affected_asset: Mapped[str] = mapped_column(String(32))
    start_time: Mapped[datetime] = mapped_column(TS)
    end_time: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    severity: Mapped[float] = mapped_column(Float, default=0.5)
    fault_parameters: Mapped[dict] = mapped_column(JSON, default=dict)


class TruthSnapshot(TruthBase):
    __tablename__ = "truth_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    sim_t: Mapped[float] = mapped_column(Float)
    timestamp: Mapped[datetime] = mapped_column(TS)
    row: Mapped[dict] = mapped_column(JSON)
