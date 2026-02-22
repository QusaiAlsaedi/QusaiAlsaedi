"""
SQLAlchemy ORM models for Integration Full-Body Monitor.
All PHI is masked before reaching this layer.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional, List

from sqlalchemy import (
    String, Integer, Boolean, Float, BigInteger, Text, DateTime,
    ForeignKey, UniqueConstraint, CheckConstraint, ARRAY, Index
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    alerts_acked: Mapped[List["Alert"]] = relationship(
        "Alert", foreign_keys="Alert.acknowledged_by", back_populates="acker"
    )
    alerts_resolved: Mapped[List["Alert"]] = relationship(
        "Alert", foreign_keys="Alert.resolved_by", back_populates="resolver"
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (
        UniqueConstraint("host", "port", name="uq_endpoint_host_port"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol: Mapped[str] = mapped_column(String(10), nullable=False, default="tcp")
    check_interval_seconds: Mapped[int] = mapped_column(Integer, default=30)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=5)
    tags: Mapped[dict] = mapped_column(JSONB, default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    probes: Mapped[List["Probe"]] = relationship("Probe", back_populates="endpoint", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------
class Probe(Base):
    __tablename__ = "probes"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False
    )
    probed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    probe_source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    endpoint: Mapped["Endpoint"] = relationship("Endpoint", back_populates="probes")

    __table_args__ = (
        Index("ix_probes_endpoint_probed", "endpoint_id", "probed_at"),
        Index("ix_probes_probed_at", "probed_at"),
    )


# ---------------------------------------------------------------------------
# Server Metrics
# ---------------------------------------------------------------------------
class ServerMetric(Base):
    __tablename__ = "server_metrics"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    server_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    cpu_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    memory_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    memory_used_mb: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    memory_total_mb: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    disk_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    disk_used_gb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    disk_total_gb: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    load_avg_1m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    load_avg_5m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    load_avg_15m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    services: Mapped[dict] = mapped_column(JSONB, default=dict)
    agent_version: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    platform: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)


# ---------------------------------------------------------------------------
# HL7 Flows
# ---------------------------------------------------------------------------
class HL7Flow(Base):
    __tablename__ = "hl7_flows"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sending_application: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    sending_facility: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    receiving_application: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    receiving_facility: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    message_types: Mapped[list] = mapped_column(JSONB, default=list)
    ack_timeout_seconds: Mapped[int] = mapped_column(Integer, default=60)
    transport: Mapped[str] = mapped_column(String(20), default="mllp")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    log_patterns: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    messages: Mapped[List["HL7Message"]] = relationship("HL7Message", back_populates="flow")


# ---------------------------------------------------------------------------
# HL7 Messages
# ---------------------------------------------------------------------------
class HL7Message(Base):
    __tablename__ = "hl7_messages"
    __table_args__ = (
        UniqueConstraint(
            "message_control_id", "sending_application", "sending_facility",
            name="uq_hl7_ctrl_send"
        ),
        Index("ix_hl7_status_detected", "status", "detected_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    message_control_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    message_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    event_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    sending_application: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    sending_facility: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    receiving_application: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    receiving_facility: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    message_datetime: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), default="outbound")
    raw_header_masked: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    patient_id_masked: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    ack_received_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ack_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    flow_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("hl7_flows.id"), nullable=True
    )
    log_source_file: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    log_line_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    server_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)

    flow: Mapped[Optional["HL7Flow"]] = relationship("HL7Flow", back_populates="messages")


# ---------------------------------------------------------------------------
# HL7 ACKs
# ---------------------------------------------------------------------------
class HL7Ack(Base):
    __tablename__ = "hl7_acks"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    message_control_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    ack_code: Mapped[str] = mapped_column(String(10), nullable=False)
    ack_datetime: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    source: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    error_condition: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_header_masked: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    server_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    alert_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    bite_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    resolved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    target_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    evidence: Mapped[List["Evidence"]] = relationship(
        "Evidence", back_populates="alert", cascade="all, delete-orphan"
    )
    acker: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[acknowledged_by], back_populates="alerts_acked"
    )
    resolver: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[resolved_by], back_populates="alerts_resolved"
    )


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------
class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    alert_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(50), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    alert: Mapped["Alert"] = relationship("Alert", back_populates="evidence")


# ---------------------------------------------------------------------------
# Alert Rules
# ---------------------------------------------------------------------------
class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(50), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    threshold_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    threshold_unit: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    window_seconds: Mapped[int] = mapped_column(Integer, default=300)
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ---------------------------------------------------------------------------
# Log Parse Rules
# ---------------------------------------------------------------------------
class LogParseRule(Base):
    __tablename__ = "log_parse_rules"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    engine_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    log_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    message_type_group: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    control_id_group: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ack_code_group: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    direction_group: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    direction_default: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    timestamp_group: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    timestamp_format: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    example_line: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
