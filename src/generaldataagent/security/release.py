"""Fail-closed production release evidence checks, independent of model output."""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

REQUIRED_EXERCISES = frozenset(
    {
        "sso_scope",
        "rls_cls",
        "hitl_resume",
        "audit_separation",
        "dlp_retention",
        "penetration",
        "disaster_recovery",
        "provider_outage",
        "sandbox_escape",
        "cost_abuse",
    }
)


@dataclass(frozen=True)
class SLOEvidence:
    observed_seconds: int
    unavailable_seconds: int
    event_samples: int
    p95_event_seconds: float
    critical_alert_delivery_seconds: float
    pilot_clean_days: int


@dataclass(frozen=True)
class ReleaseEvidence:
    revision: str
    recorded_at: datetime
    task_count: int
    passed_tasks: int
    unauthorized_successes: int
    sensitive_leaks: int
    approval_bypasses: int
    recovery_attempts: int
    recovery_successes: int
    exercises: frozenset[str]
    blocking_findings: int = 0
    slo: SLOEvidence | None = None


def release_blockers(evidence: ReleaseEvidence, *, revision: str, now: datetime) -> tuple[str, ...]:
    """Evaluate trusted measurement input; this does not authenticate external evidence."""
    reasons = []
    if not revision or evidence.revision != revision:
        reasons.append("revision_mismatch")
    if evidence.recorded_at.tzinfo is None or now.tzinfo is None:
        reasons.append("timezone_required")
    elif not timedelta(0) <= now - evidence.recorded_at <= timedelta(days=1):
        reasons.append("stale_or_future_evidence")
    if evidence.task_count < 90 or not 0 <= evidence.passed_tasks <= evidence.task_count:
        reasons.append("invalid_regression_sample")
    elif evidence.passed_tasks / evidence.task_count < 0.88:
        reasons.append("regression_success_below_threshold")
    if any(
        value != 0
        for value in (
            evidence.unauthorized_successes,
            evidence.sensitive_leaks,
            evidence.approval_bypasses,
            evidence.blocking_findings,
        )
    ):
        reasons.append("security_blocker")
    if (
        evidence.recovery_attempts < 100
        or not 0 <= evidence.recovery_successes <= evidence.recovery_attempts
        or evidence.recovery_successes / evidence.recovery_attempts < 0.99
    ):
        reasons.append("recovery_below_threshold")
    if not evidence.exercises >= REQUIRED_EXERCISES:
        reasons.append("missing_exercises")
    slo = evidence.slo
    if slo is None:
        reasons.append("missing_slo_evidence")
    elif (
        slo.observed_seconds < 30 * 86400
        or not 0 <= slo.unavailable_seconds <= slo.observed_seconds
        or slo.unavailable_seconds / slo.observed_seconds > 0.005
        or slo.event_samples < 100
        or not math.isfinite(slo.p95_event_seconds)
        or not 0 <= slo.p95_event_seconds <= 3
        or not math.isfinite(slo.critical_alert_delivery_seconds)
        or not 0 <= slo.critical_alert_delivery_seconds <= 300
        or slo.pilot_clean_days < 14
    ):
        reasons.append("slo_or_pilot_below_threshold")
    return tuple(reasons)
