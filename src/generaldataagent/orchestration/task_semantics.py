"""Accept public task semantics independently of model or evaluation transport."""

from generaldataagent.contracts.analysis import AnalysisTaskSpec


def accept_contract(proposal: dict, *, question: str, instructions: str) -> AnalysisTaskSpec:
    spec = AnalysisTaskSpec.model_validate(proposal)
    if spec.question != question or spec.public_requirements != instructions:
        raise ValueError("contract must preserve both public task texts exactly")
    from generaldataagent.tools.semantic_requirements import validate_requirement_sources

    validate_requirement_sources(spec.requirements, question, instructions)
    if spec.conflicts:
        raise ValueError("public requirement conflicts require clarification")
    from generaldataagent.tools.answer_fields import (
        infer_public_field_rules,
        public_precisions,
        validate_field_sources,
    )

    if spec.answer.fields and set(spec.answer.fields) != set(spec.answer.columns):
        raise ValueError("per-field rules must cover exactly the declared answer columns")
    public_text = question + "\n" + instructions
    if any(
        field.source_excerpt and field.source_excerpt not in public_text
        for field in spec.answer.fields.values()
    ):
        raise ValueError("field rule needs an exact public source excerpt")
    public_fields = infer_public_field_rules(instructions, spec.answer.columns)
    if public_fields:
        fields = dict(spec.answer.fields or public_fields)
        for key, expected in public_fields.items():
            field = fields[key]
            if field.kind != expected.kind:
                raise ValueError("field type contradicts the explicit public format")
            updates = {}
            for attribute in ("minimum", "maximum", "choices"):
                known = getattr(expected, attribute)
                if known is not None:
                    if getattr(field, attribute) not in (None, known):
                        raise ValueError(
                            "field value domain contradicts the explicit public format"
                        )
                    updates[attribute] = known
                    updates["source_excerpt"] = expected.source_excerpt
            for attribute in ("item_kind", "order"):
                known = getattr(expected, attribute)
                if known is not None:
                    if getattr(field, attribute) not in (None, known):
                        raise ValueError(
                            "collection elements contradict the explicit public format"
                        )
                    updates[attribute] = known
            if not field.source_excerpt:
                updates["source_excerpt"] = expected.source_excerpt
            if expected.nullable is not None:
                if field.nullable not in (None, expected.nullable):
                    raise ValueError("field nullability contradicts the explicit public format")
                updates["nullable"] = expected.nullable
            if expected.decimals is not None:
                if field.decimals is not None and field.decimals != expected.decimals:
                    raise ValueError("field precision contradicts the explicit public format")
                updates["decimals"] = expected.decimals
                if public_precisions(field.source_excerpt) != {expected.decimals}:
                    updates["source_excerpt"] = expected.source_excerpt
            if expected.collection_separator is not None:
                if field.collection_separator not in (None, expected.collection_separator):
                    raise ValueError(
                        "collection presentation contradicts the explicit public format"
                    )
                updates["collection_separator"] = expected.collection_separator
                updates["source_excerpt"] = expected.source_excerpt
            fields[key] = field.model_copy(update=updates)
        spec = spec.model_copy(update={"answer": spec.answer.model_copy(update={"fields": fields})})
    validate_field_sources(spec.answer.fields, spec.answer.columns, question, instructions)
    digits = public_precisions(question + "\n" + instructions)
    if len(digits) > 1:
        declared = {
            field.decimals for field in spec.answer.fields.values() if field.decimals is not None
        }
        if not spec.answer.fields or spec.answer.decimals is not None or declared != digits:
            raise ValueError(
                "conflicting public rounding requirements need complete per-field rules"
            )
        return spec
    if not digits and spec.answer.decimals is not None:
        raise ValueError("precision requires an explicit public rounding rule")
    if digits:
        precision = next(iter(digits))
        if spec.answer.decimals is not None and spec.answer.decimals != precision:
            raise ValueError("answer precision contradicts public rounding requirement")
        spec = AnalysisTaskSpec.model_validate(
            spec.model_dump() | {"answer": spec.answer.model_dump() | {"decimals": precision}}
        )
    return spec
