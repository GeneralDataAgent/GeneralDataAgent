"""Prepare the explicit document treatment for the real-model validation run.

The admission records created here carry synthetic development evidence hashes
and repository-owned reviewers. They authorize *this development measurement*
only; they are not independent quality/security review, release evidence, or
production admission.
"""

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from generaldataagent.business.extensions import ExtensionAdmission, SkillDefinition
from generaldataagent.business.governance import Capability, CapabilityRegistry
from generaldataagent.business.rules.document_context import MarkdownDocument
from generaldataagent.contracts import ActorContext

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "examples/skills/rule-document-discovery.json"
DOCUMENTS = (
    ("data/stage6/dabstep/manual.md", "manual.md"),
    ("data/stage6/dabstep/payments-readme.md", "payments-readme.md"),
)
ACTOR = {
    "tenant_id": "evaluation",
    "project_id": "dabstep-rules",
    "data_domains": ("payments",),
}
SYNTHETIC_EVIDENCE = "sha256:" + "b" * 64


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        raise ValueError("prepared admission must stay inside the implementation repository")
    if output.exists():
        raise FileExistsError("prepared admission already exists")
    output.mkdir(parents=True)

    skill = SkillDefinition.model_validate_json(SKILL.read_bytes())
    admission = ExtensionAdmission(
        CapabilityRegistry(output / "admission.sqlite", clock=lambda: datetime.now(UTC))
    )
    skill_hash = admission.skill_hash(skill)
    actor = ActorContext(
        user_id="stage10-evaluation",
        tenant_id=ACTOR["tenant_id"],
        project_id=ACTOR["project_id"],
        data_domains=ACTOR["data_domains"],
    )
    registry = admission.registry
    registry.propose(
        Capability(
            name=skill.name,
            version=skill.version,
            kind="skill",
            content_hash=skill_hash,
            owner="rules-team",
            tenant_id=actor.tenant_id,
            project_id=actor.project_id,
            data_domains=actor.data_domains,
            regression_evidence_hash=SYNTHETIC_EVIDENCE,
            security_evidence_hash=SYNTHETIC_EVIDENCE,
            rollback_version="disabled",
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    registry.review(
        skill.name, skill.version, reviewer_id="dev-quality-review", discipline="quality"
    )
    registry.review(
        skill.name, skill.version, reviewer_id="dev-security-review", discipline="security"
    )
    registry.activate(skill.name, skill.version)
    admission.require("skill", skill.name, skill.version, skill_hash, actor)

    documents = []
    for relative, source in DOCUMENTS:
        path = ROOT / relative
        document = MarkdownDocument(
            source=source, version=digest(path), text=path.read_bytes().decode("utf-8")
        )
        documents.append(
            {
                "path": relative,
                "source": source,
                "version": document.version,
                "content_hash": document.content_hash,
            }
        )
    config = {
        "registry_path": str((output / "admission.sqlite").relative_to(ROOT)),
        "skill_path": str(SKILL.relative_to(ROOT)),
        "skill_hash": skill_hash,
        "documents": documents,
    }
    (output / "document-extension.json").write_text(json.dumps(config, indent=2) + "\n")
    print(
        json.dumps(
            {
                "skill": skill.name,
                "skill_version": skill.version,
                "skill_hash": skill_hash,
                "admission_evidence": "synthetic_development_only",
                "documents": [{k: v for k, v in item.items() if k != "path"} for item in documents],
                "configuration": str((output / "document-extension.json").relative_to(ROOT)),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
