from pathlib import Path

from runtime.context import ContextBuilder, ContextConfig


def test_compaction_preserves_fixed_context_and_recent_messages(tmp_path) -> None:
    messages = [
        {"role": "system", "content": "system rule: never change tests"},
        {
            "role": "user",
            "content": {
                "goal": "fix bug",
                "constraints": ["do not change tests"],
                "acceptance_criteria": ["tests pass"],
            },
        },
    ]
    for index in range(8):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": {
                        "type": "tool_call",
                        "tool_name": "read_file",
                        "arguments": {"path": f"src/{index}.py"},
                    },
                },
                {
                    "role": "tool",
                    "name": "read_file",
                    "content": {"success": True, "output": "x" * 250, "metadata": {}},
                },
            ]
        )

    result = ContextBuilder(
        ContextConfig(max_prompt_chars=1_000, recent_messages=4, max_tool_output_chars=200)
    ).build(messages, workspace=tmp_path)

    assert result.compacted
    assert result.estimated_tokens_after < result.estimated_tokens_before
    assert result.messages[:2] == messages[:2]
    assert result.messages[-4:][0]["content"]["arguments"]["path"] == "src/6.py"
    summary = result.messages[2]
    assert summary["name"] == "context_summary"
    assert summary["content"]["source_message_count"] == 12


def test_large_output_uses_retrievable_hash_reference(tmp_path) -> None:
    output = "begin\n" + ("detail\n" * 100) + "end"
    messages = [
        {"role": "system", "content": "rule"},
        {"role": "user", "content": {"goal": "inspect"}},
        {
            "role": "tool",
            "name": "run_tests",
            "content": {"success": False, "output": output, "metadata": {}},
        },
    ]
    result = ContextBuilder(ContextConfig(max_prompt_chars=4_000, max_tool_output_chars=200)).build(
        messages, workspace=tmp_path
    )

    bounded = result.messages[-1]["content"]
    assert output not in bounded["output"]
    assert bounded["metadata"]["result_id"] in bounded["output"]
    assert result.result_ids == [bounded["metadata"]["result_id"]]


def test_changed_file_invalidates_old_read_observation(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("old\n")
    import hashlib

    old_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    source.write_text("new\n")
    messages = [
        {
            "role": "tool",
            "name": "read_file",
            "content": {
                "success": True,
                "output": "1: old",
                "metadata": {"path": "source.py", "content_hash": old_hash},
            },
        }
    ]

    result = ContextBuilder().build(messages, workspace=tmp_path)

    assert result.stale_file_references == ["source.py"]
    assert result.messages[0]["content"]["stale"] is True
    assert "read the file again" in result.messages[0]["content"]["output"]
