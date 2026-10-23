from uuid import uuid4

import pytest

from generaldataagent.agents.rule_supervisor import RuleSupervisorRuntime
from generaldataagent.business.rules.semantics import (
    AnnexSemanticsDeclaration,
    SemanticsDeclarationError,
)
from generaldataagent.contracts.rule_tools import RuleAnalysisRequest

DOCUMENTS = (
    "Some rules use an empty list.\n"
    "If a field is set to null it means that it applies to all possible values of that field."
)
NULL_SENTENCE = (
    "If a field is set to null it means that it applies to all possible values of that field."
)


def declaration(**changes):
    values = dict(
        empty_list_is_wildcard=True,
        source_excerpt=NULL_SENTENCE,
    )
    return AnnexSemanticsDeclaration(**(values | changes))


def request(**changes):
    values = dict(
        operation="average_fee",
        semantics_excerpt=NULL_SENTENCE,
        empty_list_is_wildcard=True,
    )
    return RuleAnalysisRequest(**(values | changes))


def test_a_declaration_must_quote_a_passage_that_is_actually_supplied():
    assert declaration().validated_against(DOCUMENTS)
    with pytest.raises(SemanticsDeclarationError) as denied:
        declaration(source_excerpt="the manual never says this").validated_against(DOCUMENTS)
    assert denied.value.code == "semantics_declaration_source_missing"


def test_null_wildcard_needs_the_null_sentence_in_the_excerpt():
    with pytest.raises(SemanticsDeclarationError) as denied:
        declaration(
            null_is_wildcard=True, source_excerpt="Some rules use an empty list."
        ).validated_against(DOCUMENTS)
    assert denied.value.code == "semantics_declaration_null_source_missing"
    # Declaring null as *not* a wildcard needs no null sentence.
    assert declaration(
        null_is_wildcard=False, source_excerpt="Some rules use an empty list."
    ).validated_against(DOCUMENTS)


def test_empty_list_reading_has_no_default():
    with pytest.raises(ValueError):
        AnnexSemanticsDeclaration(source_excerpt=NULL_SENTENCE)


def test_fingerprint_is_stable_and_summary_hides_the_excerpt():
    first, second = declaration(), declaration()
    assert first.fingerprint() == second.fingerprint()
    assert first.fingerprint().startswith("sha256:")
    assert declaration(empty_list_is_wildcard=False).fingerprint() != first.fingerprint()
    summary = first.summary()
    assert summary["empty_list_is_wildcard"] is True
    assert NULL_SENTENCE not in str(summary)


def test_a_conflicting_request_is_refused_rather_than_overridden():
    with pytest.raises(SemanticsDeclarationError) as denied:
        declaration(empty_list_is_wildcard=True).apply_to(request(empty_list_is_wildcard=False))
    assert denied.value.code == "semantics_declaration_request_conflict"
    # A consistent request passes through unchanged.
    consistent = request(empty_list_is_wildcard=True)
    assert declaration().apply_to(consistent) is consistent


def test_supervisor_refuses_a_declaration_that_contradicts_its_request():
    with pytest.raises(SemanticsDeclarationError):
        RuleSupervisorRuntime(
            model=None,
            request=request(empty_list_is_wildcard=False),
            workflow=None,
            delivery=None,
            task_id=uuid4(),
            semantics=declaration(empty_list_is_wildcard=True),
        )


def test_supervisor_rejects_an_unvalidated_declaration_object():
    with pytest.raises(ValueError, match="validated annex declaration"):
        RuleSupervisorRuntime(
            model=None,
            request=request(),
            workflow=None,
            delivery=None,
            task_id=uuid4(),
            semantics={"empty_list_is_wildcard": True},
        )


def test_supervisor_reports_the_reading_it_relied_on():
    runtime = RuleSupervisorRuntime(
        model=None,
        request=request(),
        workflow=None,
        delivery=None,
        task_id=uuid4(),
        semantics=declaration(),
    )
    assert runtime.semantics_summary() == declaration().summary()
    without = RuleSupervisorRuntime(
        model=None, request=request(), workflow=None, delivery=None, task_id=uuid4()
    )
    # No declaration is reported as absent, never as an implicit default.
    assert without.semantics_summary() is None
