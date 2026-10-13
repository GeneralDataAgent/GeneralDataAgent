"""Deterministic authorization policy evaluated outside the agent loop."""

from collections.abc import Iterable
from dataclasses import dataclass

from generaldataagent.contracts import PolicyDecision, RiskLevel, ToolRequest


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """Static policy metadata for one tool."""

    tool_name: str
    risk_level: RiskLevel
    allowed_actions: frozenset[str]
    allowed_roles: frozenset[str]
    requires_approval: bool = False
    permanently_denied: bool = False
    obligations: tuple[str, ...] = ()


DEFAULT_RULES = (
    PolicyRule(
        tool_name="catalog.search",
        risk_level=RiskLevel.R0,
        allowed_actions=frozenset({"search", "read"}),
        allowed_roles=frozenset({"supervisor", "discovery", "data_analyst", "data_engineer"}),
        obligations=("audit",),
    ),
    PolicyRule(
        tool_name="sql.query",
        risk_level=RiskLevel.R1,
        allowed_actions=frozenset({"explain", "query"}),
        allowed_roles=frozenset({"data_analyst", "data_engineer", "verifier"}),
        obligations=("audit", "read_only", "query_budget"),
    ),
    PolicyRule(
        tool_name="connector.inspect",
        risk_level=RiskLevel.R0,
        allowed_actions=frozenset({"inspect"}),
        allowed_roles=frozenset({"discovery", "data_engineer", "verifier"}),
        obligations=("audit", "read_only"),
    ),
    PolicyRule(
        tool_name="data.profile",
        risk_level=RiskLevel.R1,
        allowed_actions=frozenset({"profile"}),
        allowed_roles=frozenset({"data_engineer", "verifier"}),
        obligations=("audit", "read_only", "query_budget"),
    ),
    PolicyRule(
        tool_name="data.quality",
        risk_level=RiskLevel.R1,
        allowed_actions=frozenset({"check"}),
        allowed_roles=frozenset({"data_engineer", "verifier"}),
        obligations=("audit", "read_only", "query_budget"),
    ),
    PolicyRule(
        tool_name="schema.diff",
        risk_level=RiskLevel.R0,
        allowed_actions=frozenset({"compare"}),
        allowed_roles=frozenset({"data_engineer", "verifier"}),
        obligations=("audit",),
    ),
    PolicyRule(
        tool_name="artifact.write",
        risk_level=RiskLevel.R1,
        allowed_actions=frozenset({"create"}),
        allowed_roles=frozenset(
            {
                "supervisor",
                "discovery",
                "data_analyst",
                "data_engineer",
                "data_scientist",
                "verifier",
                "reporter",
            }
        ),
        obligations=("audit", "content_hash", "classification"),
    ),
    PolicyRule(
        tool_name="dataset.export",
        risk_level=RiskLevel.R2,
        allowed_actions=frozenset({"export"}),
        allowed_roles=frozenset({"reporter"}),
        requires_approval=True,
        obligations=("audit", "dlp_scan", "approval"),
    ),
    PolicyRule(
        tool_name="production.write",
        risk_level=RiskLevel.R3,
        allowed_actions=frozenset({"insert", "update", "delete", "deploy"}),
        allowed_roles=frozenset({"data_engineer"}),
        requires_approval=True,
        obligations=("audit", "fixed_workflow", "approval", "rollback_plan"),
    ),
    PolicyRule(
        tool_name="permission.change",
        risk_level=RiskLevel.R4,
        allowed_actions=frozenset(),
        allowed_roles=frozenset(),
        permanently_denied=True,
    ),
)


class PolicyEngine:
    """A fail-closed policy engine suitable for local and unit-test use."""

    def __init__(self, rules: Iterable[PolicyRule] = DEFAULT_RULES) -> None:
        self._rules = {rule.tool_name: rule for rule in rules}

    def evaluate(self, request: ToolRequest) -> PolicyDecision:
        rule = self._rules.get(request.tool_name)
        if rule is None:
            return self._decision(request, "deny", request.requested_risk, "unknown_tool")

        if rule.permanently_denied:
            return self._decision(request, "deny", rule.risk_level, "permanently_denied")

        if request.agent_role not in rule.allowed_roles:
            return self._decision(request, "deny", rule.risk_level, "role_not_allowed")

        if request.action not in rule.allowed_actions:
            return self._decision(request, "deny", rule.risk_level, "action_not_allowed")

        if not request.data_domains and request.tool_name in {
            "sql.query",
            "data.profile",
            "data.quality",
            "dataset.export",
        }:
            return self._decision(request, "deny", rule.risk_level, "missing_data_domain")

        if rule.requires_approval:
            return PolicyDecision(
                request_id=request.request_id,
                decision="require_approval",
                risk_level=rule.risk_level,
                reason_code="approval_required",
                reason="The requested action requires an external approval workflow.",
                obligations=rule.obligations,
            )

        return PolicyDecision(
            request_id=request.request_id,
            decision="allow",
            risk_level=rule.risk_level,
            reason_code="policy_allowed",
            reason="The request matches an explicit allow rule.",
            obligations=rule.obligations,
        )

    @staticmethod
    def _decision(
        request: ToolRequest,
        decision: str,
        risk_level: RiskLevel,
        reason_code: str,
    ) -> PolicyDecision:
        return PolicyDecision(
            request_id=request.request_id,
            decision=decision,  # type: ignore[arg-type]
            risk_level=risk_level,
            reason_code=reason_code,
            reason=f"Request denied by policy: {reason_code}.",
            obligations=("audit",),
        )
