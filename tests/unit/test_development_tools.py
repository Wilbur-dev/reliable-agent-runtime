import asyncio
import shutil
import subprocess
from pathlib import Path

from tools.base import ToolContext
from tools.development import ApplyPatchTool, GitDiffTool, RunTestsTool
from tools.registry import ToolRegistry


def execute(tool, arguments: dict[str, object], context: ToolContext):
    return asyncio.run(ToolRegistry([tool]).execute(tool.name, arguments, context))


def git_workspace(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    return tmp_path


def test_apply_patch_and_git_diff(tmp_path) -> None:
    workspace = git_workspace(tmp_path)
    (workspace / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "value.py"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "fixture"], check=True)
    patch = """diff --git a/value.py b/value.py
--- a/value.py
+++ b/value.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
    context = ToolContext(workspace)

    applied = execute(ApplyPatchTool(), {"patch": patch}, context)
    diff = execute(GitDiffTool(), {}, context)

    assert applied.success is True
    assert applied.metadata["changed_paths"] == ["value.py"]
    assert "+VALUE = 2" in (diff.output or "")


def test_unapplicable_patch_returns_structured_failure(tmp_path) -> None:
    workspace = git_workspace(tmp_path)
    (workspace / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    patch = """--- a/value.py
+++ b/value.py
@@ -1 +1 @@
-VALUE = 9
+VALUE = 2
"""

    result = execute(ApplyPatchTool(), {"patch": patch}, ToolContext(workspace))

    assert result.success is False
    assert result.error_type == "patch_apply_error"


def test_apply_patch_rejects_protected_path(tmp_path) -> None:
    workspace = git_workspace(tmp_path)
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_value.py").write_text("VALUE = 1\n", encoding="utf-8")
    patch = """--- a/tests/test_value.py
+++ b/tests/test_value.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""

    result = execute(ApplyPatchTool(), {"patch": patch}, ToolContext(workspace))

    assert result.success is False
    assert result.error_type == "protected_path"


def test_run_tests_only_accepts_configured_command_name(tmp_path) -> None:
    tool = RunTestsTool({"suite": ("python", "-c", "print('ok')")})
    context = ToolContext(tmp_path)

    allowed = execute(tool, {"command_name": "suite"}, context)
    rejected = execute(tool, {"command_name": "python -c bad"}, context)

    assert allowed.success is True
    assert allowed.output == "ok\n"
    assert rejected.success is False
    assert rejected.error_type == "command_not_allowed"


def test_configured_command_schema_exposes_allowed_names() -> None:
    tool = RunTestsTool({"pytest": ("python", "-m", "pytest")})

    schema = tool.schema()

    assert schema["parameters"]["properties"]["command_name"]["enum"] == ["pytest"]
    assert "Allowed names: pytest" in schema["description"]


def test_run_tests_timeout(tmp_path) -> None:
    sleep = shutil.which("sleep")
    assert sleep is not None
    tool = RunTestsTool({"slow": (sleep, "2")}, timeout_seconds=0.01)

    result = execute(tool, {"command_name": "slow"}, ToolContext(tmp_path))

    assert result.success is False
    assert result.error_type == "command_timeout"
