import asyncio
from pathlib import Path

from app.cli import load_actions
from evaluator.dataset import load_tasks
from evaluator.models import EvaluationProfile
from evaluator.runner import EvaluationRunner
from llm.fake import FakeLLMClient

PROJECT_ROOT = Path(__file__).parents[2]


def test_reliable_profile_writes_raw_record_for_deterministic_task(tmp_path) -> None:
    task = load_tasks(PROJECT_ROOT / "evaluation" / "tasks" / "tasks.json")[0]
    client = FakeLLMClient(load_actions(PROJECT_ROOT / "examples" / "phase1_actions.json"))
    runner = EvaluationRunner(project_root=PROJECT_ROOT, output_dir=tmp_path)

    result = asyncio.run(
        runner.run_task(
            task,
            EvaluationProfile.RELIABLE_RUNTIME,
            client,
            model="fake",
            run_id="smoke-run",
        )
    )

    raw = tmp_path / "raw" / "smoke-run" / "reliable_runtime" / f"{task.id}.json"
    assert result.task_success
    assert result.verified_success
    assert raw.is_file()
    assert '"profile": "reliable_runtime"' in raw.read_text()
