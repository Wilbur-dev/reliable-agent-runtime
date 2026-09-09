from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from pydantic import TypeAdapter

from evaluator.models import EvaluationTask

EXPECTED_COUNTS = {
    "code_understanding": 5,
    "config_repair": 5,
    "single_file_bug": 8,
    "multi_file_bug": 6,
    "failure_recovery": 3,
    "security_boundary": 3,
}


def load_tasks(path: str | Path) -> list[EvaluationTask]:
    source = Path(path)
    tasks = TypeAdapter(list[EvaluationTask]).validate_json(source.read_text(encoding="utf-8"))
    ids = [task.id for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("Evaluation task ids must be unique")
    counts = Counter(task.category for task in tasks)
    if dict(counts) != EXPECTED_COUNTS:
        raise ValueError(f"Expected category distribution {EXPECTED_COUNTS}, got {dict(counts)}")
    for task in tasks:
        if task.category == "code_understanding" and not task.expected_answer_terms:
            raise ValueError(f"Readonly task {task.id} must define expected_answer_terms")
        workspace = source.parents[2] / task.workspace
        if not workspace.is_dir():
            raise ValueError(f"Task {task.id} workspace does not exist: {workspace}")
        if task.fake_actions and not (source.parents[2] / task.fake_actions).is_file():
            raise ValueError(f"Task {task.id} fake actions do not exist: {task.fake_actions}")
    return tasks


def dump_tasks(tasks: list[EvaluationTask], path: str | Path) -> None:
    payload = [task.model_dump(mode="json") for task in tasks]
    Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
