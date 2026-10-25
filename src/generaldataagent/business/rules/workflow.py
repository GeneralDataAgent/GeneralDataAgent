"""Deterministic facet-selection services for the development rule workflow.

Two execution scopes are supported and never mixed:

* ``select`` / ``average_fee`` filter only the facets the caller supplied, so
  they need nothing beyond the immutable annex snapshot.
* ``applicable`` / ``counterfactual`` need real merchant and transaction
  evidence, so they additionally require an injected read-only payments source
  and are refused outright when one is absent.
"""

import hashlib
import json
from decimal import Decimal, InvalidOperation

from generaldataagent.business.rules.application import (
    compute_fee_summary,
    resolve_target_month,
    rule_affected_amounts,
    scope_rule_month,
    select_rule_facets,
)
from generaldataagent.business.rules.engine import (
    applicable_rules,
    counterfactual_delta,
    load_rule_set,
    match_rules,
)
from generaldataagent.business.rules.payments import (
    PaymentsSourceError,
    transaction_facets,
)
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.contracts.rules import (
    CounterfactualResult,
    FeeResult,
    RuleSemantics,
    TransactionFacets,
)
from generaldataagent.orchestration.rule_workflow import RuleWorkflowServices

FACET_OPERATIONS = frozenset({"select", "average_fee"})
CONTEXT_OPERATIONS = frozenset({"applicable", "counterfactual"})
FORMULA = "fee = fixed_amount + rate * transaction_value / 10000"


def _canonical(value: Decimal) -> str:
    """Plain decimal text without binary-float noise or trailing zeros."""
    return format(value.normalize(), "f")


class AnnexWorkflowServices:
    """Own an immutable annex snapshot and an optional scoped payments source.

    Merchant/month operations are rejected unless a payments source was
    supplied at construction. No caller-built fee coefficients are trusted and
    no rule context is accepted from the request.
    """

    def __init__(self, annex: list[dict], *, documents: str, payments=None):
        self._annex_json = json.dumps(annex, sort_keys=True, allow_nan=False)
        self._documents = documents
        self._payments = payments

    def source_hashes(self):
        sources = {
            "annex": "sha256:" + hashlib.sha256(self._annex_json.encode()).hexdigest(),
            "documents": "sha256:" + hashlib.sha256(self._documents.encode()).hexdigest(),
        }
        if self._payments is not None:
            sources["payments"] = self._payments.snapshot_hash()
        return sources

    def services(self):
        return RuleWorkflowServices(
            monthly_context=self._context,
            match_rules=self._match,
            compute=self._compute,
            verify=self._verify,
            deliver=self._deliver,
        )

    # -- request validation -------------------------------------------------

    def _request(self, state) -> RuleAnalysisRequest:
        return RuleAnalysisRequest.model_validate(state["request"])

    def _context(self, state):
        request = self._request(state)
        if request.semantics_excerpt not in self._documents:
            raise ValueError("annex_workflow_semantics_source_missing")
        if "null" not in request.semantics_excerpt.casefold():
            raise ValueError("annex_workflow_null_semantics_required")
        if FORMULA not in self._documents:
            raise ValueError("annex_workflow_formula_source_missing")
        context = {
            "scope": "supplied_facets_only",
            "semantics_excerpt": request.semantics_excerpt,
            "formula": FORMULA,
        }
        if request.operation in CONTEXT_OPERATIONS:
            if self._payments is None:
                raise ValueError("annex_workflow_requires_payments_source")
            context = self._monthly_context(request)
            context["scope"] = "merchant_and_transactions"
            context["formula"] = FORMULA
        elif request.operation not in FACET_OPERATIONS:
            raise ValueError("annex_workflow_unsupported_operation")
        return context

    def _monthly_context(self, request):
        try:
            target_month = resolve_target_month(
                year=request.year, month=request.month, day_of_year=request.day_of_year
            )
        except PaymentsSourceError as exc:
            raise ValueError("annex_workflow_day_outside_year") from exc
        source = self._payments.merchant_year(request.merchant, request.year)
        scope = scope_rule_month(source.rows, month=target_month, day_of_year=request.day_of_year)
        if not scope.volume:
            raise ValueError("annex_workflow_zero_monthly_volume")
        return {
            "year": request.year,
            "month": target_month,
            # Canonical text: SQLite REAL columns carry binary-float tails.
            "volume": _canonical(scope.volume),
            "fraud_volume": _canonical(scope.fraud_volume),
            "fraud_ratio": _canonical(scope.fraud_ratio),
            "transaction_count": scope.transaction_count,
            "fraud_count": scope.fraud_count,
            "scoped_transactions": len(scope.scoped_rows),
        }

    # -- stages -------------------------------------------------------------

    def _rules(self, state):
        request = self._request(state)
        return load_rule_set(
            json.loads(self._annex_json),
            source=self.source_hashes()["annex"],
            semantics=RuleSemantics(
                empty_list_is_wildcard=request.empty_list_is_wildcard,
                source=self.source_hashes()["documents"],
            ),
        )

    def _facet_matches(self, request, rules):
        return list(select_rule_facets(rules, request))

    def _transactions(self, state) -> tuple[TransactionFacets, ...]:
        request = self._request(state)
        source = self._payments.merchant_year(request.merchant, request.year)
        scope = scope_rule_month(
            source.rows,
            month=state["monthly_context"]["month"],
            day_of_year=request.day_of_year,
        )
        return tuple(TransactionFacets(**facet) for facet in transaction_facets(scope.scoped_rows))

    def _match(self, state):
        request = self._request(state)
        rules = self._rules(state)
        if request.operation in FACET_OPERATIONS:
            selected = self._facet_matches(request, rules)
            ids = [rule.rule_id for rule in selected]
            return {"ids": ids, "complete": True, "total_count": len(ids)}
        context = state["monthly_context"]
        source = self._payments.merchant_year(request.merchant, request.year)
        merchant_level = match_rules(
            self._facet_matches(request, rules),
            merchant=source.merchant,
            monthly_volume=Decimal(context["volume"]),
            monthly_fraud_level=Decimal(context["fraud_ratio"]),
        )
        report = applicable_rules(merchant_level, self._transactions(state))
        if report.indeterminate:
            raise ValueError("annex_workflow_indeterminate_facets")
        ids = [rule.rule_id for rule in merchant_level if rule.rule_id in report.matched]
        return {"ids": ids, "complete": True, "total_count": len(ids)}

    def _selected(self, state):
        ids = state["matched_rules"]["ids"]
        return tuple(rule for rule in self._rules(state) if rule.rule_id in ids)

    def _compute(self, state):
        request = self._request(state)
        if request.operation == "applicable":
            return {
                "items": list(state["matched_rules"]["ids"]),
                "total_count": state["matched_rules"]["total_count"],
            }
        if request.operation == "counterfactual":
            return self._compute_counterfactual(request, state)
        selected = self._selected(state)
        value = Decimal(request.transaction_value)
        computation = compute_fee_summary(selected, transaction_value=value)
        return {
            "fees": [fee.model_dump(mode="json") for fee in computation.fees],
            "average_fee": str(computation.average) if computation.average is not None else None,
        }

    def _compute_counterfactual(self, request, state):
        targets = [rule for rule in self._selected(state) if rule.rule_id == request.rule_id]
        if len(targets) != 1:
            raise ValueError("annex_workflow_counterfactual_not_applicable")
        rule = targets[0]
        affected = rule_affected_amounts(rule, self._transactions(state))
        change = counterfactual_delta(
            rule, affected, alternate_rate=Decimal(request.alternate_rate)
        )
        return change.model_dump(mode="json")

    # -- verification -------------------------------------------------------

    def _verify(self, state):
        """Re-derive the answer from the annex and payments source, not the state.

        The checkpoint is treated as a claim: matching, totals and arithmetic
        are recomputed here from authoritative inputs. This is an independent
        re-derivation of the same deterministic code path, not an independent
        semantic oracle.
        """
        request = self._request(state)
        expected_context = self._context(state)
        if json.dumps(state.get("monthly_context"), sort_keys=True, allow_nan=False) != json.dumps(
            expected_context, sort_keys=True, allow_nan=False
        ):
            return {
                "passed": False,
                "failures": ["rule_context_mismatch"],
                "checks": ["authoritative_context"],
            }
        failures = []
        selection = state["matched_rules"]
        if (
            selection.get("complete") is not True
            or type(selection.get("total_count")) is not int
            or selection != self._match(state)
        ):
            return {
                "passed": False,
                "failures": ["rule_selection_mismatch"],
                "checks": ["authoritative_context", "selection_consistency"],
            }
        if request.operation == "applicable":
            expected = state["matched_rules"]["ids"]
            computation = state["computation"]
            items = computation.get("items")
            if (
                set(computation) != {"items", "total_count"}
                or not isinstance(items, list)
                or any(not isinstance(item, str) for item in items)
            ):
                failures.append("rule_applicable_record_invalid")
            if items != expected:
                failures.append("rule_items_mismatch")
            if type(computation.get("total_count")) is not int or computation["total_count"] != len(
                expected
            ):
                failures.append("rule_count_mismatch")
        elif request.operation == "counterfactual":
            failures.extend(self._verify_counterfactual(request, state))
        else:
            failures.extend(self._verify_fees(request, state))
        return {
            "passed": not failures,
            "failures": failures,
            "checks": [
                "authoritative_context",
                "selection_consistency",
                "authoritative_fee_records",
                "decimal_average",
            ],
        }

    def _verify_fees(self, request, state):
        failures = []
        rules = self._selected(state)
        computation = state["computation"]
        if set(computation) != {"fees", "average_fee"}:
            failures.append("rule_fee_computation_invalid")
        results = computation.get("fees")
        if not isinstance(results, list) or any(not isinstance(row, dict) for row in results):
            return failures + ["rule_fee_records_invalid"]
        if [row.get("rule_id") for row in results] != [rule.rule_id for rule in rules]:
            failures.append("rule_result_ids_mismatch")
        value = Decimal(request.transaction_value)
        expected_fees = [
            Decimal(rule.fixed_amount) + Decimal(rule.rate) * value / Decimal(10000)
            for rule in rules
        ]
        for rule, row, expected in zip(rules, results, expected_fees, strict=False):
            if (
                set(row) != set(FeeResult.model_fields)
                or row.get("schema_version") != FeeResult.model_fields["schema_version"].default
                or row.get("fixed_amount") != rule.fixed_amount
                or row.get("rate") != rule.rate
                or row.get("source") != rule.source
                or not self._decimal_matches(row.get("fee"), expected)
            ):
                failures.append("rule_fee_record_mismatch")
        expected_average = (
            sum(expected_fees, Decimal(0)) / len(expected_fees) if expected_fees else None
        )
        average = computation.get("average_fee")
        if (
            average is not None
            if expected_average is None
            else not self._decimal_matches(average, expected_average)
        ):
            failures.append("rule_average_mismatch")
        return failures

    @staticmethod
    def _decimal_matches(value, expected):
        if not isinstance(value, str):
            return False
        try:
            actual = Decimal(value)
        except InvalidOperation:
            return False
        return actual.is_finite() and actual == expected

    def _verify_counterfactual(self, request, state):
        failures = []
        targets = [rule for rule in self._selected(state) if rule.rule_id == request.rule_id]
        if len(targets) != 1:
            return ["rule_counterfactual_target_missing"]
        rule = targets[0]
        amounts = rule_affected_amounts(rule, self._transactions(state))
        total = sum(amounts, Decimal(0))
        alternate = Decimal(request.alternate_rate)
        baseline = Decimal(rule.fixed_amount) * len(amounts) + Decimal(rule.rate) * total / Decimal(
            10000
        )
        expected_alternate = Decimal(rule.fixed_amount) * len(
            amounts
        ) + alternate * total / Decimal(10000)
        computation = state["computation"]
        if (
            set(computation) != set(CounterfactualResult.model_fields)
            or computation.get("schema_version")
            != CounterfactualResult.model_fields["schema_version"].default
        ):
            failures.append("rule_counterfactual_record_invalid")
        if computation.get("rule_id") != rule.rule_id:
            failures.append("rule_counterfactual_rule_mismatch")
        for field, expected, code in (
            ("delta", expected_alternate - baseline, "rule_delta_mismatch"),
            ("baseline_fee", baseline, "rule_baseline_mismatch"),
            ("alternate_fee", expected_alternate, "rule_alternate_mismatch"),
        ):
            if not self._decimal_matches(computation.get(field), expected):
                failures.append(code)
        count = computation.get("transaction_count")
        if type(count) is not int or count != len(amounts):
            failures.append("rule_transaction_count_mismatch")
        return failures

    # -- delivery -----------------------------------------------------------

    def _deliver(self, state):
        request = self._request(state)
        delivery = {
            "operation": request.operation,
            "source_hashes": self.source_hashes(),
            "formula": state["monthly_context"]["formula"],
        }
        if request.operation == "applicable":
            delivery["items"] = list(state["computation"]["items"])
            delivery["total_count"] = state["computation"]["total_count"]
        elif request.operation == "counterfactual":
            delivery.update(state["computation"])
        else:
            delivery["items"] = state["matched_rules"]["ids"]
            delivery["average_fee"] = state["computation"]["average_fee"]
        if state["monthly_context"]["scope"] == "merchant_and_transactions":
            delivery["context"] = {
                key: value
                for key, value in state["monthly_context"].items()
                if key not in {"formula", "scope", "semantics_excerpt"}
            }
        return delivery


__all__ = [
    "AnnexWorkflowServices",
    "CONTEXT_OPERATIONS",
    "FACET_OPERATIONS",
    "FORMULA",
    "PaymentsSourceError",
]
