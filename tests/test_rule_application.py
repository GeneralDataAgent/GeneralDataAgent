"""Shared facet behavior has independent expectations and two adapter consumers."""

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from admission_helpers import enroll

from generaldataagent.business.extensions import ExtensionAdmission, SkillDefinition
from generaldataagent.business.governance import CapabilityRegistry
from generaldataagent.business.rules.application import (
    compute_fee_summary,
    resolve_applicability,
    resolve_target_month,
    rule_affected_amounts,
    scope_rule_month,
    select_rule_facets,
)
from generaldataagent.business.rules.engine import load_rule_set
from generaldataagent.business.rules.payments import PaymentsSourceError
from generaldataagent.business.rules.workflow import AnnexWorkflowServices
from generaldataagent.contracts import ActorContext
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest
from generaldataagent.contracts.rules import RuleSemantics, TransactionFacets
from generaldataagent.evaluation.stage_nine import analyze_fee_rules

DOCUMENTS = "null accepts all values\nfee = fixed_amount + rate * transaction_value / 10000"


def annex(prefix):
    return [
        dict(ID=prefix + "-open", card_scheme="Orbit", fixed_amount="0.07", rate="13"),
        dict(
            ID=prefix + "-limited",
            card_scheme="Orbit",
            fixed_amount="0.09",
            rate="17",
            account_type=["S"],
            merchant_category_code=[7001],
            aci=["F"],
            is_credit=False,
            intracountry=True,
        ),
        dict(ID=prefix + "-other", card_scheme="Nova", fixed_amount="0.01", rate="5"),
        dict(
            ID=prefix + "-empty",
            card_scheme="Orbit",
            account_type=[],
            fixed_amount="0.03",
            rate="9",
        ),
    ]


@pytest.mark.parametrize("prefix", ["fresh", "unseen"])
@pytest.mark.parametrize(
    "filters, suffixes",
    [
        ({}, ["open", "limited", "other", "empty"]),
        ({"card_scheme": "Nova"}, ["other"]),
        ({"account_type": "R"}, ["open", "other"]),
        ({"merchant_category_code": 8002}, ["open", "other", "empty"]),
        ({"aci": "B"}, ["open", "other", "empty"]),
        ({"is_credit": True}, ["open", "other", "empty"]),
        ({"intracountry": False}, ["open", "other", "empty"]),
        (
            {
                "card_scheme": "Orbit",
                "account_type": "S",
                "aci": "F",
                "is_credit": False,
                "intracountry": True,
            },
            ["open", "limited"],
        ),
        ({"card_scheme": "Absent"}, []),
    ],
)
def test_shared_selection_and_adapters_match_independent_expectations(
    tmp_path, prefix, filters, suffixes
):
    records = annex(prefix)
    request = RuleAnalysisRequest(
        operation="select", semantics_excerpt="null accepts all values", **filters
    )
    rules = load_rule_set(
        records, source="synthetic-annex", semantics=RuleSemantics(source="synthetic-manual")
    )
    expected = [prefix + "-" + suffix for suffix in suffixes]
    selected = select_rule_facets(rules, request)
    assert [rule.rule_id for rule in selected] == expected
    assert all(any(rule is original for original in rules) for rule in selected)
    workflow = AnnexWorkflowServices(records, documents=DOCUMENTS)
    state = {"request": request.model_dump()}
    assert workflow.services().match_rules(state)["ids"] == expected
    database = tmp_path / "annex.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE fees (payload TEXT)")
        db.executemany("INSERT INTO fees VALUES (?)", [(json.dumps(row),) for row in records])
    result = analyze_fee_rules(
        database, request.model_dump(), allowed_relations={"fees"}, documents=DOCUMENTS
    )
    assert result["items"] == expected
    assert result["total_count"] == len(expected)


@pytest.mark.parametrize("wildcard, expected", [(False, []), (True, ["fresh-empty"])])
def test_shared_selection_respects_loaded_empty_list_semantics(wildcard, expected):
    rules = load_rule_set(
        [annex("fresh")[-1]],
        source="synthetic",
        semantics=RuleSemantics(source="manual", empty_list_is_wildcard=wildcard),
    )
    request = RuleAnalysisRequest(
        operation="select",
        semantics_excerpt="null accepts all values",
        account_type="R",
        empty_list_is_wildcard=wildcard,
    )
    assert [rule.rule_id for rule in select_rule_facets(rules, request)] == expected


def test_external_document_skill_requires_admission_before_rendering(tmp_path):
    root = Path(__file__).resolve().parents[1]
    skill = SkillDefinition.model_validate_json(
        (root / "examples/skills/payment-document-context.json").read_text()
    )
    assert skill.allowed_tools == ("search_documents",)
    assert set(skill.inputs) == {"question", "documents"}
    assert {item.kind for item in skill.examples} == {"positive", "negative"}
    from datetime import UTC, datetime

    actor = ActorContext(user_id="u", tenant_id="t", project_id="p", data_domains=("payments",))
    registry = CapabilityRegistry(tmp_path / "registry", clock=lambda: datetime.now(UTC))
    admission = ExtensionAdmission(registry)
    context = {
        "question": "Find the domesticity definition",
        "documents": "Issuer equals acquirer.",
    }
    with pytest.raises(PermissionError):
        admission.render_skill(skill, actor, context)
    enroll(registry, actor, "skill", skill.name, admission.skill_hash(skill))
    text, reference = admission.render_skill(skill, actor, context)
    assert context["documents"] in text
    assert "a quoted null rule does not establish empty-list semantics" in text
    assert reference == ("skill", skill.name, skill.version, admission.skill_hash(skill))


def month_rows():
    return [
        {"year": 2026, "day_of_year": 1, "eur_amount": "10.25", "has_fraudulent_dispute": "True"},
        {"year": 2026, "day_of_year": 2, "eur_amount": "4.75", "has_fraudulent_dispute": "False"},
        {"year": 2026, "day_of_year": 32, "eur_amount": "99.00", "has_fraudulent_dispute": "True"},
    ]


@pytest.mark.parametrize(
    ("request_month", "ordinal", "expected"), [(1, None, 1), (None, 32, 2), (7, 32, 2)]
)
def test_target_month_resolution_is_exact(request_month, ordinal, expected):
    assert resolve_target_month(year=2026, month=request_month, day_of_year=ordinal) == expected


@pytest.mark.parametrize("ordinal", [0, 367, 1.5, True])
def test_invalid_ordinal_days_fail_closed(ordinal):
    with pytest.raises(PaymentsSourceError, match="payments_invalid_row"):
        resolve_target_month(year=2026, month=None, day_of_year=ordinal)


def test_missing_month_and_day_is_refused():
    with pytest.raises(ValueError, match="month or day_of_year required"):
        resolve_target_month(year=2026, month=None, day_of_year=None)


def test_month_scope_keeps_month_totals_and_day_subset_separate():
    scope = scope_rule_month(month_rows(), month=1, day_of_year=1)
    assert [row["day_of_year"] for row in scope.monthly_rows] == [1, 2]
    assert [row["day_of_year"] for row in scope.scoped_rows] == [1]
    assert (scope.volume, scope.fraud_volume) == (Decimal("15"), Decimal("10.25"))
    assert (scope.transaction_count, scope.fraud_count) == (2, 1)
    assert float(scope.fraud_ratio) == pytest.approx(0.6833333333333333)
    unscoped = scope_rule_month(month_rows(), month=2)
    assert len(unscoped.scoped_rows) == len(unscoped.monthly_rows) == 1
    assert unscoped.month == 2


def test_empty_month_and_zero_volume_keep_separate_decisions():
    with pytest.raises(PaymentsSourceError, match="payments_month_empty"):
        scope_rule_month(month_rows(), month=4)
    zero = scope_rule_month(
        [{"year": 2026, "day_of_year": 1, "eur_amount": "0", "has_fraudulent_dispute": "False"}],
        month=1,
    )
    assert zero.volume == 0 and zero.fraud_ratio is None


def fee_rules():
    return load_rule_set(
        [
            dict(ID="a", card_scheme="Orbit", fixed_amount="0.07", rate="13"),
            dict(ID="b", card_scheme="Orbit", fixed_amount="0.09", rate="17", aci=["F"]),
        ],
        source="synthetic-annex",
        semantics=RuleSemantics(source="synthetic-manual"),
    )


def test_fee_summary_is_exact_and_leaves_presentation_to_callers():
    rules = fee_rules()
    value = Decimal("100")
    computation = compute_fee_summary(rules, transaction_value=value)
    assert [fee.rule_id for fee in computation.fees] == ["a", "b"]
    # Independent expectation: fixed + rate * value / 10000, unrounded.
    assert [Decimal(fee.fee) for fee in computation.fees] == [
        Decimal("0.07") + Decimal("13") * value / Decimal(10000),
        Decimal("0.09") + Decimal("17") * value / Decimal(10000),
    ]
    assert computation.average == sum(
        (Decimal(fee.fee) for fee in computation.fees), Decimal(0)
    ) / Decimal(2)


def test_fee_summary_reports_no_average_when_no_rule_applies():
    computation = compute_fee_summary((), transaction_value=Decimal("100"))
    assert computation.fees == () and computation.average is None


def test_affected_amounts_follow_the_same_rule_match_both_consumers_use():
    rule_a, rule_b = fee_rules()
    transactions = tuple(
        TransactionFacets(**facet)
        for facet in (
            dict(card_scheme="Orbit", aci="B", is_credit=True, amount="10"),
            dict(card_scheme="Orbit", aci="F", is_credit=True, amount="25.5"),
            dict(card_scheme="Nova", aci="F", is_credit=True, amount="99"),
        )
    )
    assert rule_affected_amounts(rule_a, transactions) == (Decimal("10"), Decimal("25.5"))
    assert rule_affected_amounts(rule_b, transactions) == (Decimal("25.5"),)


def applicability_rules():
    return load_rule_set(
        [
            dict(ID="open", card_scheme="Orbit", fixed_amount="0.07", rate="13"),
            dict(
                ID="high-volume",
                card_scheme="Orbit",
                fixed_amount="0.09",
                rate="17",
                monthly_volume=">10",
            ),
            dict(
                ID="foreign",
                card_scheme="Orbit",
                fixed_amount="0.05",
                rate="11",
                intracountry=False,
            ),
            dict(ID="other", card_scheme="Nova", fixed_amount="0.01", rate="5"),
        ],
        source="synthetic-annex",
        semantics=RuleSemantics(source="synthetic-manual"),
    )


@pytest.mark.parametrize(
    ("volume", "expected"),
    [
        (Decimal("5"), ["open", "foreign"]),
        (Decimal("50"), ["open", "high-volume", "foreign"]),
    ],
)
def test_applicability_applies_thresholds_before_transactions(volume, expected):
    transactions = (
        TransactionFacets(
            card_scheme="Orbit", aci="B", is_credit=True, intracountry=False, amount="10"
        ),
    )
    result = resolve_applicability(
        applicability_rules(),
        merchant={"merchant": "Shop"},
        volume=volume,
        fraud_ratio=Decimal("0.1"),
        transactions=transactions,
    )
    assert [rule.rule_id for rule in result.matched] == expected
    # The threshold decides merchant-level membership before transactions are
    # considered, so a rule that fails it never reaches transaction matching.
    merchant_level = [rule.rule_id for rule in result.merchant_level]
    assert merchant_level == [
        rule_id
        for rule_id in ("open", "high-volume", "foreign", "other")
        if rule_id not in {"high-volume"} or volume > Decimal("10")
    ]
    assert result.indeterminate is False


def test_applicability_reports_indeterminate_facets_instead_of_dropping_rules():
    # No countries on the transaction: domesticity cannot be decided.
    transactions = (TransactionFacets(card_scheme="Orbit", aci="B", is_credit=True, amount="10"),)
    result = resolve_applicability(
        applicability_rules(),
        merchant={"merchant": "Shop"},
        volume=Decimal("50"),
        fraud_ratio=None,
        transactions=transactions,
    )
    assert result.indeterminate is True
    # The undecidable rule stays a merchant-level survivor but is not matched,
    # so a caller that ignored `indeterminate` would under-report rather than
    # silently guess.
    assert "foreign" in [rule.rule_id for rule in result.merchant_level]
    assert "foreign" not in result.report.matched


def test_applicability_keeps_source_order_and_returns_no_match_for_other_scheme():
    transactions = (
        TransactionFacets(
            card_scheme="Nova", aci="F", is_credit=False, intracountry=True, amount="10"
        ),
    )
    result = resolve_applicability(
        applicability_rules(),
        merchant={"merchant": "Shop"},
        volume=Decimal("50"),
        fraud_ratio=None,
        transactions=transactions,
    )
    assert [rule.rule_id for rule in result.matched] == ["other"]
    assert result.indeterminate is False


def test_local_demo_reuses_shared_operations_without_evaluation():
    """The non-benchmark caller must import no evaluation, model, or graph code.

    A clean interpreter is required: this test session has already imported
    those packages for other tests, so only a subprocess can show what the demo
    itself pulls in.
    """
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    program = (
        "import runpy, sys\n"
        f"runpy.run_path({str(root / 'examples/rule_selection_demo.py')!r}, run_name='__main__')\n"
        "banned = sorted(\n"
        "    name\n"
        "    for name in sys.modules\n"
        "    if name.startswith(('generaldataagent.evaluation', 'langchain', 'langgraph'))\n"
        ")\n"
        "print('BANNED', banned)\n"
        "raise SystemExit(1 if banned else 0)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, cwd=root, timeout=120
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    lines = completed.stdout.splitlines()
    assert lines[0] == "selected: ['demo-retail', 'demo-foreign']"
    assert "month: 1" in lines[1]
    assert "scoped_transactions: 1" in lines[2]
    assert lines[3] == "matched: ['demo-retail', 'demo-foreign']"
    assert lines[4] == "indeterminate: False"
    assert lines[5] == "average_fee: 0.18"
    assert lines[6] == "affected_by_demo_foreign: (Decimal('1200.00'),)"
    assert lines[-1] == "BANNED []"


def test_declared_refusal_codes_match_every_raise_site():
    """A code the handlers raise must be declared, or the supervisor would call it
    a model failure; a declared code nobody raises is dead policy."""
    import inspect
    import re

    from generaldataagent.business.rules import workflow as module
    from generaldataagent.business.rules.workflow import REFUSAL_CODES

    # Locate the module from its own import, not the process working directory.
    source = inspect.getsource(module)
    raised = set(re.findall(r'raise ValueError\("(annex_workflow_[a-z_]+)"', source))
    assert raised == set(REFUSAL_CODES)
