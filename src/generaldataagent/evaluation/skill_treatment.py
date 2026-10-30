"""Pinned Skill definitions with a live, read-only admission authority."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from generaldataagent.business.actions import content_hash
from generaldataagent.business.extensions import ExtensionAdmission, SkillDefinition
from generaldataagent.business.governance import CapabilityRegistry
from generaldataagent.evaluation.skill_extension import BoundedAgentSkill, action_capabilities
from generaldataagent.evaluation.stage_six_analysis import document_outline

VERSION = "bounded-agent-skill-v1"
MAX_FILE_BYTES = 128_000
MAX_SNAPSHOT_BYTES = 2_000_000
TREATMENT_SOURCE_PATHS = (
    "evaluation/skill_treatment.py",
    "business/governance.py",
    "business/extensions.py",
    "business/skill_bundle.py",
)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("skill treatment input exceeds byte limit")
    return data


@dataclass(frozen=True)
class SkillTreatment:
    """Canonical serialized definitions; the mutable registry is never snapshotted."""

    snapshot_json: str

    @classmethod
    def from_payload(cls, payload: dict) -> "SkillTreatment":
        if not isinstance(payload, dict) or set(payload) != {"version", "body", "content_hash"}:
            raise ValueError("invalid skill treatment envelope")
        if (
            payload["version"] != VERSION
            or content_hash(payload["body"]) != payload["content_hash"]
        ):
            raise ValueError("skill treatment snapshot hash mismatch")
        body = payload["body"]
        if not isinstance(body, dict) or set(body) != {
            "registry_path",
            "actor",
            "skill",
            "dependencies",
        }:
            raise ValueError("invalid skill treatment body")
        if (
            not isinstance(body["registry_path"], str)
            or not Path(body["registry_path"]).is_absolute()
        ):
            raise ValueError("skill treatment registry path must be absolute")
        from generaldataagent.contracts import ActorContext

        ActorContext.model_validate(body["actor"])
        SkillDefinition.model_validate(body["skill"])
        if not isinstance(body["dependencies"], dict) or len(body["dependencies"]) > 64:
            raise ValueError("invalid skill treatment dependencies")
        for key, value in body["dependencies"].items():
            skill = SkillDefinition.model_validate(value)
            if key != f"{skill.name}@{skill.version}":
                raise ValueError("skill dependency definition mismatch")
        encoded = _canonical(payload)
        if len(encoded.encode()) > MAX_SNAPSHOT_BYTES:
            raise ValueError("skill treatment snapshot exceeds byte limit")
        return cls(encoded)

    def payload(self) -> dict:
        return json.loads(self.snapshot_json)

    def extension(self) -> BoundedAgentSkill:
        from generaldataagent.contracts import ActorContext

        body = self.payload()["body"]
        return BoundedAgentSkill(
            ExtensionAdmission(
                CapabilityRegistry(
                    Path(body["registry_path"]), clock=lambda: datetime.now(UTC), read_only=True
                )
            ),
            ActorContext.model_validate(body["actor"]),
            SkillDefinition.model_validate(body["skill"]),
            {
                key: SkillDefinition.model_validate(value)
                for key, value in body["dependencies"].items()
            },
        )

    def preflight(self, *, profile=None, budget=None, task=None, documents="") -> BoundedAgentSkill:
        extension = self.extension()
        context = {
            "question": task.question if task is not None else "",
            "instructions": task.instructions if task is not None else "",
            "document_outline": document_outline(documents),
        }
        if profile is None:
            extension.admission.render_skill_bundle(
                extension.skill, extension.actor, context, dependencies=extension.dependencies
            )
        else:
            extension.bind(context, available=action_capabilities(profile), task_budget=budget)
        return extension

    def summary(self) -> dict:
        payload = self.payload()
        body = payload["body"]
        return {
            "version": VERSION,
            "configuration_hash": payload["content_hash"],
            "admission_mode": "live_read_only_registry",
            "actor_hash": content_hash(body["actor"]),
            "skill": {
                "name": body["skill"]["name"],
                "version": body["skill"]["version"],
                "content_hash": ExtensionAdmission.skill_hash(
                    SkillDefinition.model_validate(body["skill"])
                ),
            },
            "dependency_count": len(body["dependencies"]),
        }


def load_skill_treatment(path: Path, *, root: Path) -> SkillTreatment:
    root = root.resolve()

    def inside(value):
        if not isinstance(value, str):
            raise ValueError("skill treatment paths must be strings")
        file = (root / value).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            raise ValueError("skill treatment files must exist inside the repository")
        return file

    config = json.loads(_read(inside(str(path)), MAX_FILE_BYTES))
    required = {"registry_path", "actor", "skill_path", "skill_hash"}
    if (
        not isinstance(config, dict)
        or not required <= set(config)
        or set(config) - required - {"dependencies"}
    ):
        raise ValueError("invalid skill treatment configuration")

    def definition(path, digest):
        skill = SkillDefinition.model_validate_json(_read(inside(path), MAX_FILE_BYTES))
        if ExtensionAdmission.skill_hash(skill) != digest:
            raise ValueError("skill definition differs from pinned hash")
        return skill.model_dump(mode="json")

    entries = config.get("dependencies", [])
    if not isinstance(entries, list) or len(entries) > 64:
        raise ValueError("invalid skill treatment dependency list")
    dependencies = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "skill_hash"}:
            raise ValueError("invalid pinned skill dependency entry")
        value = definition(entry["path"], entry["skill_hash"])
        key = f"{value['name']}@{value['version']}"
        if key in dependencies:
            raise ValueError("duplicate skill dependency definition")
        dependencies[key] = value
    body = {
        "registry_path": str(inside(config["registry_path"])),
        "actor": config["actor"],
        "skill": definition(config["skill_path"], config["skill_hash"]),
        "dependencies": dependencies,
    }
    treatment = SkillTreatment.from_payload(
        {"version": VERSION, "body": body, "content_hash": content_hash(body)}
    )
    treatment.preflight()
    return treatment


def worker_skill(payload: dict) -> BoundedAgentSkill | None:
    if "skill_treatment" not in payload:
        return None
    treatment = SkillTreatment.from_payload(payload["skill_treatment"])
    task = SimpleNamespace(**{key: payload["task"][key] for key in ("question", "instructions")})
    return treatment.preflight(
        profile=payload.get("tool_profile", "sql"),
        budget=SimpleNamespace(
            max_tool_calls=payload.get("max_steps", 6),
            max_tokens=payload.get("max_tokens"),
            max_runtime_seconds=payload.get("max_seconds", 120),
        ),
        task=task,
        documents=payload["documents"],
    )
