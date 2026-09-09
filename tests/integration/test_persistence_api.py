import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.main import create_app
from persistence.database import SQLiteStore
from runtime.actions import FinishAction, ToolCallAction
from runtime.models import Task, TaskStatus, ToolResult

PROJECT_ROOT = Path(__file__).parents[2]


def database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'runtime.db'}"


def test_store_recovers_running_task_and_infers_completed_mutation(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "value.txt"
    source.write_text("before\n")
    store = SQLiteStore(database_url(tmp_path))
    task = Task(goal="change value", workspace=str(workspace))
    action = ToolCallAction(tool_name="apply_patch", arguments={"patch": "example"})
    store.create_task(task)
    store.set_task_status(str(task.id), TaskStatus.RUNNING)
    store.begin_step(task, 1, action, mutates=True)

    # Simulate a process dying after the side effect but before recording its result.
    source.write_text("after\n")
    restarted_store = SQLiteStore(database_url(tmp_path))
    recovered = restarted_store.recover_interrupted()

    assert recovered == [str(task.id)]
    assert restarted_store.get_task(str(task.id)).status is TaskStatus.PENDING
    step = restarted_store.list_steps(str(task.id))[0]
    assert step["status"] == "SUCCEEDED"
    assert step["result"]["metadata"]["recovered_from_workspace_hash"] is True


def test_store_marks_unchanged_running_step_interrupted(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "value.txt").write_text("same\n")
    store = SQLiteStore(database_url(tmp_path))
    task = Task(goal="change value", workspace=str(workspace))
    store.create_task(task)
    store.set_task_status(str(task.id), TaskStatus.RUNNING)
    store.begin_step(
        task,
        1,
        ToolCallAction(tool_name="apply_patch", arguments={"patch": "example"}),
        mutates=True,
    )

    SQLiteStore(database_url(tmp_path)).recover_interrupted()
    step = store.list_steps(str(task.id))[0]

    assert step["status"] == "INTERRUPTED"
    assert step["idempotency_key"] is None


def test_failed_mutation_can_be_retried_with_same_idempotency_input(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "value.txt").write_text("same\n")
    store = SQLiteStore(database_url(tmp_path))
    task = Task(goal="retry patch", workspace=str(workspace))
    action = ToolCallAction(tool_name="apply_patch", arguments={"patch": "invalid"})
    store.create_task(task)
    first_step = store.begin_step(task, 1, action, mutates=True)
    store.finish_step(
        first_step,
        ToolResult(success=False, error_type="patch_apply_error"),
        task=task,
        mutates=True,
    )

    second_step = store.begin_step(task, 2, action, mutates=True)

    assert second_step != first_step


def test_api_creates_runs_queries_and_lists_persisted_steps(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("reliable\n")
    with TestClient(create_app(database_url(tmp_path))) as client:
        create_response = client.post(
            "/tasks",
            json={
                "goal": "inspect note",
                "workspace": str(workspace),
                "fake_actions": [
                    {
                        "type": "tool_call",
                        "tool_name": "read_file",
                        "arguments": {"path": "note.txt"},
                    },
                    {"type": "finish", "summary": "Inspected note."},
                ],
            },
        )
        assert create_response.status_code == 201
        task_id = create_response.json()["id"]

        run_response = client.post(f"/tasks/{task_id}/run")
        detail_response = client.get(f"/tasks/{task_id}")
        steps_response = client.get(f"/tasks/{task_id}/steps")

    assert run_response.status_code == 200
    assert run_response.json()["status"] == "COMPLETED"
    assert detail_response.json()["task"]["status"] == "COMPLETED"
    assert "elapsed_seconds" in detail_response.json()["metrics"]
    assert [step["status"] for step in steps_response.json()] == ["SUCCEEDED", "SUCCEEDED"]
    assert steps_response.json()[0]["result"]["output"] == "1: reliable"


def test_api_cancel_is_persisted_and_prevents_run(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with TestClient(create_app(database_url(tmp_path))) as client:
        task_id = client.post(
            "/tasks",
            json={
                "goal": "stop",
                "workspace": str(workspace),
                "fake_actions": [{"type": "finish", "summary": "done"}],
            },
        ).json()["id"]

        cancel_response = client.post(f"/tasks/{task_id}/cancel")
        run_response = client.post(f"/tasks/{task_id}/run")

    assert cancel_response.json()["status"] == "CANCELLED"
    assert run_response.status_code == 409


def test_restart_skips_mutation_already_observed_in_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    value = workspace / "value.txt"
    value.write_text("before\n")
    url = database_url(tmp_path)
    store = SQLiteStore(url)
    task = Task(goal="recover", workspace=str(workspace))
    first_action = ToolCallAction(tool_name="apply_patch", arguments={"patch": "original"})
    store.create_task(
        task,
        runtime_config={
            "mode": "readonly",
            "fake_actions": [
                first_action.model_dump(mode="json"),
                FinishAction(summary="Recovered without replay.").model_dump(mode="json"),
            ],
        },
    )
    store.set_task_status(str(task.id), TaskStatus.RUNNING)
    store.begin_step(task, 1, first_action, mutates=True)
    value.write_text("after\n")

    with TestClient(create_app(url)) as client:
        assert client.get("/health").json()["recovered_tasks"] == 1
        response = client.post(f"/tasks/{task.id}/run")

    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert value.read_text() == "after\n"
    assert len(SQLiteStore(url).list_steps(str(task.id))) == 2


def test_restart_continues_write_task_through_verification(tmp_path) -> None:
    workspace = tmp_path / "order_service"
    shutil.copytree(PROJECT_ROOT / "examples" / "order_service", workspace)
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=workspace,
        check=True,
    )
    first_patch = ToolCallAction(
        tool_name="apply_patch", arguments={"patch": "first mutation already executed"}
    )
    second_patch = """--- a/src/discount.py
+++ b/src/discount.py
@@ -2,6 +2,6 @@
     rates = {
         "standard": 0.0,
         "silver": 0.05,
-        "gold": 0.08,
+        "gold": 0.10,
     }
     return rates.get(customer_tier, 0.0)
"""
    actions = [
        first_patch,
        ToolCallAction(tool_name="apply_patch", arguments={"patch": second_patch}),
        ToolCallAction(tool_name="run_tests", arguments={"command_name": "pytest"}),
        ToolCallAction(tool_name="run_linter", arguments={"command_name": "ruff"}),
        FinishAction(summary="Recovered and verified."),
    ]
    url = database_url(tmp_path)
    store = SQLiteStore(url)
    task = Task(goal="fix gold discount", workspace=str(workspace))
    store.create_task(
        task,
        runtime_config={
            "mode": "write",
            "fake_actions": [action.model_dump(mode="json") for action in actions],
        },
    )
    store.set_task_status(str(task.id), TaskStatus.RUNNING)
    store.begin_step(task, 1, first_patch, mutates=True)
    source = workspace / "src" / "discount.py"
    source.write_text(source.read_text().replace('"gold": 0.01', '"gold": 0.08'))

    with TestClient(create_app(url)) as client:
        response = client.post(f"/tasks/{task.id}/run")
        steps = client.get(f"/tasks/{task.id}/steps").json()

    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["termination_reason"] == "VERIFIED_COMPLETE"
    assert '"gold": 0.10' in source.read_text()
    assert len(steps) == 5
    assert steps[0]["result"]["metadata"]["recovered_from_workspace_hash"] is True
