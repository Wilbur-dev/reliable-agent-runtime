import json
from pathlib import Path

from evaluator.analysis import summarize, write_summary
from evaluator.dataset import EXPECTED_COUNTS, load_tasks
from evaluator.models import EvaluationProfile, EvaluationRecord
from evaluator.runner import profile_capabilities

PROJECT_ROOT = Path(__file__).parents[2]


def record(profile: EvaluationProfile, **overrides) -> EvaluationRecord:
    payload = {
        "run_id": "run-1",
        "task_id": "task-1",
        "category": "failure_recovery",
        "profile": profile,
        "model": "test-model",
        "status": "COMPLETED",
        "task_success": True,
        "verified_success": True,
        "false_completion": False,
        "step_count": 4,
        "tool_call_count": 3,
        "retry_count": 1,
        "elapsed_seconds": 2.0,
        "input_tokens": 100,
        "output_tokens": 20,
        "estimated_cost_usd": 0.01,
    }
    payload.update(overrides)
    return EvaluationRecord.model_validate(payload)


def test_task_manifest_has_planned_30_task_distribution() -> None:
    tasks = load_tasks(PROJECT_ROOT / "evaluation" / "tasks" / "tasks.json")
    counts = {category: 0 for category in EXPECTED_COUNTS}
    for task in tasks:
        counts[task.category] += 1
    assert len(tasks) == 30
    assert counts == EXPECTED_COUNTS


def test_four_profiles_add_controls_incrementally() -> None:
    single = profile_capabilities(EvaluationProfile.SINGLE_CALL)
    basic = profile_capabilities(EvaluationProfile.BASIC_LOOP)
    guarded = profile_capabilities(EvaluationProfile.GUARDED_RUNTIME)
    reliable = profile_capabilities(EvaluationProfile.RELIABLE_RUNTIME)
    assert not single["tools"]
    assert basic["tools"] and not basic["policy"]
    assert guarded["policy"] and not guarded["persistence"]
    assert reliable["persistence"] and reliable["context"]


def test_summary_is_derived_from_immutable_records(tmp_path) -> None:
    records = [
        record(EvaluationProfile.BASIC_LOOP, false_completion=True, verified_success=False),
        record(EvaluationProfile.RELIABLE_RUNTIME),
    ]
    summaries = summarize(records)
    assert summaries[0].false_completion_rate == 1.0
    assert summaries[1].verified_success_rate == 1.0

    write_summary(
        summaries,
        json_path=tmp_path / "summary.json",
        csv_path=tmp_path / "summary.csv",
    )
    assert len(json.loads((tmp_path / "summary.json").read_text())) == 2
    assert "false_completion_rate" in (tmp_path / "summary.csv").read_text()
