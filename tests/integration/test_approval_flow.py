from pathlib import Path

from fastapi.testclient import TestClient

from app.api.main import create_app

DEPENDENCY_PATCH = """--- a/pyproject.toml
+++ b/pyproject.toml
@@ -1 +1 @@
-old
+new
"""


def create_waiting_task(client: TestClient, workspace: Path) -> str:
    response = client.post(
        "/tasks",
        json={
            "goal": "update dependency configuration",
            "workspace": str(workspace),
            "mode": "write",
            "sandbox": False,
            "fake_actions": [
                {
                    "type": "tool_call",
                    "tool_name": "apply_patch",
                    "arguments": {"patch": DEPENDENCY_PATCH},
                },
                {"type": "finish", "summary": "done"},
            ],
        },
    )
    task_id = response.json()["id"]
    run = client.post(f"/tasks/{task_id}/run")
    assert run.json()["status"] == "WAITING_APPROVAL"
    return task_id


def test_approval_is_step_bound_and_consumed_before_execution(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("old\n")
    with TestClient(create_app(f"sqlite:///{tmp_path / 'runtime.db'}")) as client:
        task_id = create_waiting_task(client, workspace)
        approval = client.get(f"/tasks/{task_id}/approvals").json()[0]
        assert (workspace / "pyproject.toml").read_text() == "old\n"
        repeated_run = client.post(f"/tasks/{task_id}/run")

        wrong = client.post(f"/tasks/{task_id}/approve", json={"step_id": 9999})
        approved = client.post(f"/tasks/{task_id}/approve", json={"step_id": approval["step_id"]})
        client.post(f"/tasks/{task_id}/run")
        final_approval = client.get(f"/tasks/{task_id}/approvals").json()[0]

    assert wrong.status_code == 404
    assert repeated_run.status_code == 409
    assert approved.json()["status"] == "APPROVED"
    assert final_approval["status"] == "CONSUMED"
    assert (workspace / "pyproject.toml").read_text() == "new\n"


def test_rejection_reason_is_persisted_and_returned_to_agent_context(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("old\n")
    url = f"sqlite:///{tmp_path / 'runtime.db'}"
    with TestClient(create_app(url)) as client:
        task_id = create_waiting_task(client, workspace)
        step_id = client.get(f"/tasks/{task_id}/approvals").json()[0]["step_id"]
        rejected = client.post(
            f"/tasks/{task_id}/reject",
            json={"step_id": step_id, "reason": "Keep dependencies frozen"},
        )
        steps = client.get(f"/tasks/{task_id}/steps").json()
        approval = client.get(f"/tasks/{task_id}/approvals").json()[0]

    assert rejected.json()["status"] == "REJECTED"
    assert approval["rejection_reason"] == "Keep dependencies frozen"
    assert steps[0]["result"]["error_type"] == "policy_rejected"
    assert steps[0]["result"]["error_message"] == "Keep dependencies frozen"
    assert (workspace / "pyproject.toml").read_text() == "old\n"
