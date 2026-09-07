import asyncio
from pathlib import Path

from tools.base import ToolContext
from tools.readonly import (
    ListFilesTool,
    ReadFileTool,
    ReadOnlyLimits,
    SearchTextTool,
)
from tools.registry import ToolRegistry


def execute(tool, arguments: dict[str, object], workspace: Path):
    return asyncio.run(ToolRegistry([tool]).execute(tool.name, arguments, ToolContext(workspace)))


def test_list_files_ignores_hidden_files(tmp_path) -> None:
    (tmp_path / "visible.txt").write_text("visible", encoding="utf-8")
    (tmp_path / ".secret").write_text("hidden", encoding="utf-8")

    result = execute(ListFilesTool(), {"path": "."}, tmp_path)

    assert result.success is True
    assert "visible.txt" in (result.output or "")
    assert ".secret" not in (result.output or "")


def test_read_file_returns_numbered_line_range(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = execute(
        ReadFileTool(),
        {"path": "notes.txt", "start_line": 2, "max_lines": 2},
        tmp_path,
    )

    assert result.output == "2: two\n3: three"
    assert result.metadata["total_lines"] == 3


def test_parent_traversal_is_rejected(tmp_path) -> None:
    result = execute(ReadFileTool(), {"path": "../secret.txt"}, tmp_path)

    assert result.success is False
    assert result.error_type == "path_boundary_error"


def test_absolute_path_is_rejected(tmp_path) -> None:
    result = execute(ReadFileTool(), {"path": "/etc/passwd"}, tmp_path)

    assert result.success is False
    assert result.error_type == "path_boundary_error"


def test_symlink_escape_is_rejected(tmp_path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)

    result = execute(ReadFileTool(), {"path": "link.txt"}, tmp_path)

    assert result.success is False
    assert result.error_type == "path_boundary_error"


def test_large_file_is_rejected(tmp_path) -> None:
    (tmp_path / "large.txt").write_text("x" * 20, encoding="utf-8")
    tool = ReadFileTool(ReadOnlyLimits(max_file_bytes=10))

    result = execute(tool, {"path": "large.txt"}, tmp_path)

    assert result.success is False
    assert result.error_type == "file_too_large"


def test_search_result_limit_marks_output_truncated(tmp_path) -> None:
    (tmp_path / "matches.txt").write_text("needle\nneedle\nneedle\n", encoding="utf-8")

    result = execute(
        SearchTextTool(),
        {"query": "needle", "max_results": 2},
        tmp_path,
    )

    assert result.success is True
    assert result.metadata["match_count"] == 2
    assert result.truncated is True


def test_output_character_limit_marks_output_truncated(tmp_path) -> None:
    (tmp_path / "long.txt").write_text("abcdefghijk", encoding="utf-8")
    tool = ReadFileTool(ReadOnlyLimits(max_output_chars=10))

    result = execute(tool, {"path": "long.txt"}, tmp_path)

    assert result.success is True
    assert result.truncated is True
    assert len(result.output or "") <= 10
