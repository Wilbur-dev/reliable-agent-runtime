import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.main import create_app


def test_long_task_compacts_prompt_but_preserves_full_trajectory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "large.txt"
    source.write_text("".join(f"line {index}: {'x' * 80}\n" for index in range(300)))
    actions = [
        {
            "type": "tool_call",
            "tool_name": "read_file",
            "arguments": {"path": "large.txt", "start_line": index * 20 + 1, "max_lines": 20},
        }
        for index in range(8)
    ]
    actions.append({"type": "finish", "summary": "Long inspection completed."})
    url = f"sqlite:///{tmp_path / 'runtime.db'}"

    with TestClient(create_app(url)) as client:
        created = client.post(
            "/tasks",
            json={
                "goal": "inspect a large file without losing constraints",
                "workspace": str(workspace),
                "constraints": ["read only"],
                "acceptance_criteria": ["finish after inspection"],
                "budget": {
                    "max_no_progress_steps": 20,
                    "max_repeated_actions": 10,
                },
                "context": {
                    "max_prompt_chars": 1_200,
                    "recent_messages": 4,
                    "max_tool_output_chars": 300,
                },
                "fake_actions": actions,
            },
        )
        task_id = created.json()["id"]
        run = client.post(f"/tasks/{task_id}/run")
        detail = client.get(f"/tasks/{task_id}").json()
        steps = client.get(f"/tasks/{task_id}/steps").json()
        compactions = client.get(f"/tasks/{task_id}/context-compactions").json()
        events = client.get(f"/tasks/{task_id}/events").json()
        metrics = client.get("/metrics").text

        result_ids = [
            result_id
            for event in events
            if event["event_type"] == "context_compacted"
            for result_id in event["payload"]["result_ids"]
        ]
        full_result = client.get(f"/tasks/{task_id}/results/{result_ids[0]}").json()

    assert run.json()["status"] == "COMPLETED"
    assert len(steps) == 9
    assert len(compactions) >= 1
    assert all(row["tokens_after"] < row["tokens_before"] for row in compactions)
    assert compactions[0]["summary"]["kind"] == "compacted_history"
    assert detail["metrics"]["context_compaction_count"] == len(compactions)
    assert (
        detail["metrics"]["prompt_tokens_after_compaction"]
        < detail["metrics"]["prompt_tokens_before_compaction"]
    )
    assert len(steps[0]["result"]["output"]) > 300
    assert hashlib.sha256(full_result["output"].encode()).hexdigest() == full_result["result_id"]
    assert {event["event_type"] for event in events} >= {
        "context_compacted",
        "tool_finished",
        "task_finished",
    }
    assert "agent_tasks_total" in metrics
    assert "agent_context_compactions_total" in metrics
    assert "agent_tool_calls_total" in metrics
