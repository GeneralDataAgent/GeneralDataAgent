"""Opt-in, repository-owned structural verification of advisory document evidence.

This is a producer self-check against the supplied corpus, not an independent
semantic verifier. Skill prose never selects code or changes failure policy.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from generaldataagent.business.extensions import ExtensionAdmission, SkillDefinition
from generaldataagent.business.rules.document_context import DocumentContext
from generaldataagent.contracts import ArtifactRef

DOCUMENT_VERIFIER_VERSION = "document-evidence-v1"
DOCUMENT_CHECKS = (
    "document_skill_binding",
    "document_corpus_and_quotation_binding",
    "document_gap_and_truncation_binding",
    "document_evidence_task_binding",
    "document_evidence_exact_readback",
)


@dataclass(frozen=True)
class DocumentEvidenceVerifier:
    """An explicit executable adapter bound to one exact reviewed Skill revision."""

    skill_name: str
    skill_version: str
    skill_hash: str
    adapter: str = DOCUMENT_VERIFIER_VERSION

    def __post_init__(self):
        if self.adapter != DOCUMENT_VERIFIER_VERSION:
            raise ValueError("document_verifier_adapter_unsupported")

    def require_binding(self, skill: SkillDefinition) -> None:
        if (self.skill_name, self.skill_version, self.skill_hash) != (
            skill.name,
            skill.version,
            ExtensionAdmission.skill_hash(skill),
        ):
            raise ValueError("document_verifier_skill_binding_mismatch")

    def verify(
        self,
        *,
        context: DocumentContext,
        skill: SkillDefinition,
        query: str,
        result: dict,
        task_id: UUID,
        read_bytes: Callable[[ArtifactRef], bytes],
    ) -> dict:
        self.require_binding(skill)
        # Re-derive from the explicit snapshot through the repository method,
        # rather than trusting the runner's returned quotations or metadata.
        expected = DocumentContext.search(context, query)
        observed = {key: value for key, value in result.items() if key != "evidence"}
        expected_bytes = _bytes(expected)
        if _bytes(observed) != expected_bytes:
            raise ValueError("document_verifier_result_mismatch")
        ref = ArtifactRef.model_validate(result.get("evidence"))
        if ref.task_id != task_id or ref.artifact_type != "rule.document.evidence":
            raise ValueError("document_verifier_evidence_binding_mismatch")
        if read_bytes(ref) != expected_bytes:
            raise ValueError("document_verifier_evidence_content_mismatch")
        return {
            "adapter": self.adapter,
            "scope": "producer_self_check",
            "passed": True,
            "check_codes": list(DOCUMENT_CHECKS),
            "skill": {
                "name": self.skill_name,
                "version": self.skill_version,
                "content_hash": self.skill_hash,
            },
        }


def _bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
