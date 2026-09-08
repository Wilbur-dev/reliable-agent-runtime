import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from llm.fake import FakeLLMClient
from runtime.actions import FinishAction, ToolCallAction
from runtime.loop import InMemoryAgentLoop
from runtime.models import Task, TaskStatus, TerminationReason
from tools.base import ToolContext
from tools.development import ApplyPatchTool, RunLinterTool, RunTestsTool
from tools.readonly import ListFilesTool, ReadFileTool, SearchTextTool
from tools.registry import ToolRegistry
from verifiers.development import CommandExitCodeVerifier, ProtectedPathsUnchangedVerifier

PROJECT_ROOT = Path(__file__).parents[2]


def initialize_fixture(tmp_path: Path) -> Path:
    workspace = tmp_path / "order_service"
    shutil.copytree(PROJECT_ROOT / "examples" / "order_service", workspace)
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=workspace, check=True)
    return workspace


def test_finish_requires_verification_and_failed_evidence_returns_to_agent(tmp_path) -> None:
    workspace = initialize_fixture(tmp_path)
    first_patch = """--- a/src/discount.py
+++ b/src/discount.py
@@ -2,6 +2,6 @@
     rates = {
         "standard": 0.0,
         "silver": 0.05,
-        "gold": 0.01,
+        "gold": 0.08,
     }
     return rates.get(customer_tier, 0.0)
"""
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
        ToolCallAction(tool_name="list_files", arguments={"path": "."}),
        ToolCallAction(tool_name="search_text", arguments={"query": "gold", "path": "."}),
        ToolCallAction(tool_name="read_file", arguments={"path": "src/discount.py"}),
        ToolCallAction(tool_name="apply_patch", arguments={"patch": first_patch}),
        ToolCallAction(tool_name="run_tests", arguments={"command_name": "pytest"}),
        FinishAction(summary="Fixed the discount."),
        ToolCallAction(tool_name="apply_patch", arguments={"patch": second_patch}),
        ToolCallAction(tool_name="run_tests", arguments={"command_name": "pytest"}),
        ToolCallAction(tool_name="run_linter", arguments={"command_name": "compile"}),
        FinishAction(summary="Fixed the gold discount and verified the project."),
    ]
    registry = ToolRegistry(
        [
            ListFilesTool(),
            SearchTextTool(),
            ReadFileTool(),
            ApplyPatchTool(),
            RunTestsTool({"pytest": (sys.executable, "-m", "pytest", "-q")}),
            RunLinterTool({"compile": (sys.executable, "-m", "compileall", "-q", "src")}),
        ]
    )
    context = ToolContext(workspace, protected_paths=("tests",))
    loop = InMemoryAgentLoop(
        FakeLLMClient(actions),
        registry,
        verifiers=[
            CommandExitCodeVerifier("run_tests"),
            CommandExitCodeVerifier("run_linter"),
            ProtectedPathsUnchangedVerifier(),
        ],
        tool_context=context,
    )

    result = asyncio.run(
        loop.run(
            Task(
                goal="Fix the gold discount so tests pass.",
                workspace=str(workspace),
                constraints=["Do not modify tests/"],
            )
        )
    )

    assert result.task.status is TaskStatus.COMPLETED
    assert result.task.termination_reason is TerminationReason.VERIFIED_COMPLETE
    assert len(result.verifications) == 6
    assert any(not verification.success for verification in result.verifications[:3])
    assert all(verification.success for verification in result.verifications[-3:])
    assert any(message.get("name") == "verification" for message in result.messages)
    assert '"gold": 0.10' in (workspace / "src" / "discount.py").read_text()
    assert not subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", "tests"], cwd=workspace, check=False
    ).returncode
