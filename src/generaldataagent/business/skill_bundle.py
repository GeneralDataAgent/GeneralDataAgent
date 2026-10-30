"""Bounded composition of explicitly supplied, admitted Skill instructions."""

from collections.abc import Mapping

from generaldataagent.business.extensions import ExtensionAdmission, SkillDefinition
from generaldataagent.contracts import ActorContext

MAX_DEFINITIONS = 64
MAX_SKILLS = 32
MAX_DEPTH = 8
MAX_RENDERED_CHARS = 16000


def render_skill_bundle(
    admission: ExtensionAdmission,
    root: SkillDefinition,
    actor: ActorContext,
    context: Mapping[str, str],
    *,
    dependencies: Mapping[str, SkillDefinition] | None = None,
    task_budget=None,
) -> tuple[str, tuple[tuple[str, str, str, str], ...]]:
    """Resolve supplied name@version definitions without loading code or granting tools.

    Instructions share one literal context and one caller-owned task budget.
    Dependency ceilings are compatibility declarations, not additive execution
    allocations. The existing gateway/model controls still meter actual work.
    """
    literal_context = dict(context)
    definitions = _definitions(root, dependencies)
    # Preserve the legacy single-Skill text and admission behavior, while never
    # letting an unadmitted root borrow an admitted dependency's authority.
    root_text, root_reference = admission._render_skill_leaf(
        root, actor, literal_context, task_budget=task_budget
    )
    ordered = _dependency_order(root, definitions)
    sections, references = [], []
    for skill in ordered:
        text, reference = (
            (root_text, root_reference)
            if skill is root
            else admission._render_skill_leaf(
                skill, actor, literal_context, task_budget=task_budget
            )
        )
        sections.append(f"Skill {skill.name}@{skill.version}\n{text}")
        references.append(reference)
    rendered = root_text if len(ordered) == 1 else "\n\n".join(sections)
    if len(rendered) > MAX_RENDERED_CHARS:
        raise ValueError("rendered_skill_bundle_too_large")
    # A revocation during rendering must refuse the assembled bundle too.
    for reference in references:
        admission.require(*reference, actor)
    return rendered, tuple(references)


def ordered_skill_definitions(
    root: SkillDefinition, dependencies: Mapping[str, SkillDefinition] | None = None
) -> tuple[SkillDefinition, ...]:
    """Pure declaration checks and dependency order; never admission or rendering."""
    return _dependency_order(root, _definitions(root, dependencies))


def _definitions(root, dependencies):
    if dependencies is not None and not isinstance(dependencies, Mapping):
        raise ValueError("skill_dependency_definitions_invalid")
    definitions = dict(dependencies or {})
    if len(definitions) > MAX_DEFINITIONS:
        raise ValueError("skill_dependency_definitions_limit")
    for key, value in definitions.items():
        if not isinstance(value, SkillDefinition) or key != f"{value.name}@{value.version}":
            raise ValueError("skill_dependency_definition_mismatch")
    root_key = f"{root.name}@{root.version}"
    if root_key in definitions and definitions[root_key] != root:
        raise ValueError("skill_dependency_root_definition_mismatch")
    definitions[root_key] = root
    return definitions


def _dependency_order(root, definitions):
    ordered = []
    visiting, complete = set(), set()
    versions = {}

    def visit(skill, depth):
        key = f"{skill.name}@{skill.version}"
        if key in visiting:
            raise ValueError("skill_dependency_cycle")
        if versions.setdefault(skill.name, skill.version) != skill.version:
            raise ValueError("skill_dependency_version_conflict")
        if depth > MAX_DEPTH:
            raise ValueError("skill_dependency_depth_limit")
        if key in complete:
            return
        visiting.add(key)
        if len(visiting) + len(complete) > MAX_SKILLS:
            raise ValueError("skill_dependency_count_limit")
        for reference in skill.dependencies:
            dependency = definitions.get(reference)
            if dependency is None:
                raise ValueError("skill_dependency_definition_missing")
            if not set(dependency.allowed_tools) <= set(skill.allowed_tools):
                raise PermissionError("skill_dependency_tools_exceed_parent")
            if not set(dependency.required_data_domains) <= set(skill.required_data_domains):
                raise PermissionError("skill_dependency_domains_exceed_parent")
            if any(
                getattr(dependency.budget, field) > getattr(skill.budget, field)
                for field in ("max_tool_calls", "max_tokens", "max_seconds")
            ):
                raise PermissionError("skill_dependency_budget_exceeds_parent")
            visit(dependency, depth + 1)
        visiting.remove(key)
        complete.add(key)
        ordered.append(skill)

    visit(root, 1)
    return tuple(ordered)
