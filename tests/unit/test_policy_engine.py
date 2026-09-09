from pathlib import Path

from policies.engine import PolicyDecision, PolicyEngine
from runtime.actions import ToolCallAction
from tools.base import ToolContext
from tools.development import ApplyPatchTool, RunTestsTool


def evaluate(tmp_path: Path, patch: str):
    return PolicyEngine().evaluate(
        ToolCallAction(tool_name="apply_patch", arguments={"patch": patch}),
        tool=ApplyPatchTool(),
        context=ToolContext(tmp_path, protected_paths=("tests",)),
    )


def test_allows_small_source_patch(tmp_path) -> None:
    result = evaluate(tmp_path, "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n")
    assert result.decision is PolicyDecision.ALLOW


def test_denies_protected_path_patch(tmp_path) -> None:
    result = evaluate(
        tmp_path, "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    assert result.decision is PolicyDecision.DENY
    assert result.rule == "protected_path"


def test_dependency_change_requires_approval(tmp_path) -> None:
    result = evaluate(tmp_path, "--- a/pyproject.toml\n+++ b/pyproject.toml\n@@ -1 +1 @@\n-a\n+b\n")
    assert result.decision is PolicyDecision.REQUIRE_APPROVAL
    assert result.rule == "dependency_change"


def test_deletion_and_large_change_require_approval(tmp_path) -> None:
    deleted = evaluate(
        tmp_path,
        "deleted file mode 100644\n--- a/src/a.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n",
    )
    large_patch = "--- a/src/a.py\n+++ b/src/a.py\n" + "".join(f"+line{i}\n" for i in range(200))
    assert deleted.rule == "file_deletion"
    assert evaluate(tmp_path, large_patch).rule == "large_change"


def test_configured_test_command_is_allowed(tmp_path) -> None:
    result = PolicyEngine().evaluate(
        ToolCallAction(tool_name="run_tests", arguments={"command_name": "pytest"}),
        tool=RunTestsTool({"pytest": ("python", "-m", "pytest")}),
        context=ToolContext(tmp_path),
    )
    assert result.decision is PolicyDecision.ALLOW
