"""Shared enumerations for versioned contracts."""

from enum import StrEnum


class AgentRole(StrEnum):
    SUPERVISOR = "supervisor"
    DISCOVERY = "discovery"
    DATA_ENGINEER = "data_engineer"
    DATA_ANALYST = "data_analyst"
    DATA_SCIENTIST = "data_scientist"
    VERIFIER = "verifier"
    REPORTER = "reporter"


class ArtifactClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class RiskLevel(StrEnum):
    R0 = "r0"
    R1 = "r1"
    R2 = "r2"
    R3 = "r3"
    R4 = "r4"


class TaskStatus(StrEnum):
    CREATED = "created"
    CLARIFYING = "clarifying"
    PLANNED = "planned"
    RUNNING = "running"
    VALIDATING = "validating"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    DELIVERING = "delivering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    NEEDS_HUMAN = "needs_human"


class ValidationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_HUMAN = "needs_human"
