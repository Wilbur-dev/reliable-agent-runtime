import asyncio
import subprocess
from pathlib import Path

import pytest

from sandbox.docker import DockerCommandRunner
from tools.base import ToolContext
from tools.development import CommandArgs, RunTestsTool
from tools.errors import CommandTimeoutError

pytestmark = pytest.mark.docker


def run_in_sandbox(tmp_path: Path, code: str, *, timeout: float = 10):
    tmp_path.chmod(0o777)
    runner = DockerCommandRunner()
    return asyncio.run(
        runner.run(
            ("python", "-c", code),
            context=ToolContext(tmp_path),
            timeout_seconds=timeout,
        )
    )


def test_container_is_non_root_and_resource_limited(tmp_path) -> None:
    result = run_in_sandbox(tmp_path, "import os; print(os.getuid())")
    assert result.success
    assert result.output.strip() == "65532"
    assert result.metadata["network"] == "none"
    assert result.metadata["cpus"] == 1.0
    assert result.metadata["memory"] == "512m"
    assert result.metadata["pids_limit"] == 128


def test_container_cannot_see_host_path_outside_workspace(tmp_path) -> None:
    secret = tmp_path.parent / "host-only-secret.txt"
    secret.write_text("must-not-be-mounted")
    result = run_in_sandbox(
        tmp_path,
        f"from pathlib import Path; print(Path({str(secret)!r}).exists())",
    )
    assert result.success
    assert result.output.strip() == "False"


def test_container_network_is_disabled(tmp_path) -> None:
    code = (
        "import socket; "
        "s=socket.socket(); s.settimeout(1); "
        "\ntry: s.connect(('1.1.1.1', 53)); print('reachable')"
        "\nexcept OSError: print('blocked')"
    )
    result = run_in_sandbox(tmp_path, code)
    assert result.success
    assert result.output.strip() == "blocked"


def test_timeout_kills_and_removes_container(tmp_path) -> None:
    runner = DockerCommandRunner()
    with pytest.raises(CommandTimeoutError):
        asyncio.run(
            runner.run(
                ("python", "-c", "import time; time.sleep(30)"),
                context=ToolContext(tmp_path),
                timeout_seconds=0.5,
            )
        )
    leftovers = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=reliable-agent-runtime.phase=5"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert leftovers.stdout.strip() == ""


def test_normal_configured_python_check_passes(tmp_path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_solution.py").write_text(
        "import unittest\n\n"
        "class SolutionTest(unittest.TestCase):\n"
        "    def test_sum(self):\n"
        "        self.assertEqual(2 + 2, 4)\n"
    )
    tmp_path.chmod(0o777)
    tool = RunTestsTool(
        {
            "unittest": (
                "python",
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
            )
        }
    )
    result = asyncio.run(
        tool.execute(
            ToolContext(tmp_path, command_runner=DockerCommandRunner()),
            CommandArgs(command_name="unittest"),
        )
    )
    assert result.success
    assert result.metadata["sandbox"] == "docker"
