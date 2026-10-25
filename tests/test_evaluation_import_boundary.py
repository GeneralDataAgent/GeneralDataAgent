"""The offline evaluation package must not sit in a production import path.

Pulling an offline protocol module into a runtime entry point makes every
startup pay for it and invites the runtime to depend on evaluation-only types.
These tests pin the direction, and also pin the package's export surface, which
regressed once when two ``__all__`` assignments silently replaced each other.
"""

import ast
import subprocess
import sys
from pathlib import Path

import generaldataagent.evaluation as evaluation

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src/generaldataagent"


def _evaluation_modules_after_import(statement: str) -> list[str]:
    code = (
        "import sys;"
        f"{statement};"
        "print('\\n'.join(sorted("
        "m for m in sys.modules if m.startswith('generaldataagent.evaluation.'))))"
    )
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()


def test_runtime_entry_points_import_no_evaluation_protocols():
    # The production catalog and security admission are loaded by the API; the
    # review store is the API's own runtime port.
    for statement in (
        "import generaldataagent.catalog.semantic_context",
        "import generaldataagent.security.business_admission",
        "import generaldataagent.review",
    ):
        assert _evaluation_modules_after_import(statement) == [], statement


def test_the_package_exports_everything_it_defines():
    # A second ``__all__`` assignment dropped the stage-one/two/review exports
    # while leaving the names importable, so nothing failed loudly.
    assert len(evaluation.__all__) == len(set(evaluation.__all__))
    for name in (
        "DuplicateReviewError",
        "LocalHumanReviewStore",
        "EvaluationReport",
        "ExternalGateEvidence",
        "HardenedSandboxReview",
        "write_external_gate_manifest",
    ):
        assert name in evaluation.__all__, name
    assert all(hasattr(evaluation, name) for name in evaluation.__all__)


def test_stage_five_external_remains_importable_where_it_is_pinned():
    """The contract moved to ``security``; its evaluation path is a re-export."""
    from generaldataagent.evaluation.stage_five_external import (
        QualifiedActionBundle as LegacyBundle,
    )
    from generaldataagent.security.action_qualification import (
        QualifiedActionBundle as SecurityBundle,
    )

    assert LegacyBundle is SecurityBundle


def _reaches_evaluation(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").startswith("generaldataagent.evaluation")
    if isinstance(node, ast.Import):
        return any(alias.name.startswith("generaldataagent.evaluation") for alias in node.names)
    return False


def _module_level_imports(node: ast.AST):
    """Every import that executes when the module is imported.

    Function bodies are skipped: an import inside one runs only when that
    function is called, which is the sanctioned form. ``if``/``try``/class
    bodies are descended into, because they do run at import time.
    """
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return
    if _reaches_evaluation(node):
        yield node
    for child in ast.iter_child_nodes(node):
        yield from _module_level_imports(child)


def test_runtime_modules_do_not_import_evaluation_at_module_level():
    """Evaluation may be reached from a function body, never from an import block.

    ``business.experience`` re-runs a template extractor that lives in the
    offline package, and importing it at module level would put the whole
    evaluation package on the import path of the API's experience store. The
    function-local import keeps that crossing explicit and one call wide.
    """
    offenders: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if relative.startswith("evaluation/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders.extend(f"{relative}:{node.lineno}" for node in _module_level_imports(tree))
    assert offenders == []
