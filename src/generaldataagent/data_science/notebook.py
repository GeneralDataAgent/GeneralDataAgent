"""Export and actually replay exact fixed-template notebook cells in the sandbox."""

import hashlib
import json

from generaldataagent.data_science._worker import notebook_sources
from generaldataagent.data_science.checks import split_indices, validate_experiment_data
from generaldataagent.data_science.sandbox import FixedTemplateSandbox
from generaldataagent.data_science.snapshot import load_snapshot_rows


def export_replayed_notebook(*, snapshot, spec, artifact_store, sandbox=None):
    if spec.task_id != snapshot.task_id or spec.snapshot_id != snapshot.snapshot_id:
        raise ValueError("notebook must bind exact task and snapshot")
    from generaldataagent.data_science.workbench import DataScienceWorkbench

    DataScienceWorkbench._validate_schema_binding(snapshot, spec)
    rows = load_snapshot_rows(snapshot, artifact_store)
    train, test = split_indices(rows, spec)
    if any(
        not c.passed and c.severity == "error"
        for c in validate_experiment_data(rows, spec, train, test)
    ):
        raise ValueError("notebook input failed science checks")
    payload = {
        "rows": rows,
        "spec": spec.model_dump(mode="json", exclude={"quota"}),
        "train_indices": train,
        "test_indices": test,
    }
    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
            "generaldataagent": {"payload": payload, "reproducible": False},
        },
        "cells": [
            {
                "cell_type": "code",
                "id": f"fixed-{i}",
                "metadata": {},
                "source": source,
                "execution_count": None,
                "outputs": [],
            }
            for i, source in enumerate(notebook_sources(payload))
        ],
    }
    runner = sandbox or FixedTemplateSandbox()
    arguments = dict(rows=rows, spec=spec, train_indices=train, test_indices=test)
    original = runner.run(**arguments)
    replay = runner.run(**arguments, notebook=notebook)
    if original != replay:
        raise ValueError("notebook replay diverged from execution record")
    for i, cell in enumerate(notebook["cells"], 1):
        cell["execution_count"] = i
    text = json.dumps(replay, sort_keys=True, allow_nan=False)
    notebook["cells"][-1]["outputs"] = [
        {"output_type": "stream", "name": "stdout", "text": text + "\n"}
    ]
    notebook["metadata"]["generaldataagent"].update(
        reproducible=True,
        runtime_fingerprint=runner.runtime_fingerprint,
        result_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        replay_scope=(
            "actual exact-cell execution in fixed-template sandbox; no arbitrary notebook code"
        ),
    )
    return artifact_store.put_bytes(
        task_id=spec.task_id,
        artifact_type="replayed_notebook",
        created_by="data_scientist",
        content=json.dumps(notebook, ensure_ascii=False, allow_nan=False).encode(),
        lineage=(snapshot.data_artifact.artifact_id,),
        extension="ipynb",
    )
