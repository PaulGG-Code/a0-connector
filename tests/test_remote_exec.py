from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path

import pytest

from agent_zero_cli.remote_exec import LocalShellSession, RemoteExecManager


pytestmark = pytest.mark.anyio


class FakeShellSession:
    def __init__(self, *, cwd: str | None = None) -> None:
        self.cwd = cwd
        self.commands: list[str] = []
        self.inputs: list[str] = []
        self.is_alive = True
        self.command_completed = False
        self._full_output = ""
        self._partial_output = ""

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self.is_alive = False

    async def send_command(self, command: str) -> None:
        self.commands.append(command)
        if command == "ansi":
            self._full_output = "\x1b[31mhello\x1b[0m\r\n"
            self._partial_output = self._full_output
            self.command_completed = True
            return

        if "A0_PY_CODE" in command:
            self._full_output = "42\r\n"
            self._partial_output = self._full_output
            self.command_completed = True
            return

        if "A0_NODE_CODE" in command:
            self._full_output = "node ok\r\n"
            self._partial_output = self._full_output
            self.command_completed = True
            return

        if command == "ask":
            self._full_output = "Continue? "
            self._partial_output = self._full_output
            self.command_completed = False
            return

        self._full_output = f"ran:{command}\r\n"
        self._partial_output = self._full_output
        self.command_completed = True

    async def send_input(self, text: str) -> None:
        self.inputs.append(text)
        self._full_output = f"input:{text}\r\n"
        self._partial_output = self._full_output
        self.command_completed = True

    async def reset_output(self) -> None:
        self._full_output = ""
        self._partial_output = ""

    async def read_output(
        self,
        *,
        timeout: float = 0,
        reset_full_output: bool = False,
    ) -> tuple[str, str | None]:
        del timeout, reset_full_output
        partial = self._partial_output or None
        self._partial_output = ""
        return self._full_output, partial


@pytest.fixture
def created_shells(monkeypatch: pytest.MonkeyPatch) -> list[FakeShellSession]:
    shells: list[FakeShellSession] = []

    def _create_shell_session(self: RemoteExecManager) -> FakeShellSession:
        shell = FakeShellSession(cwd=self.cwd)
        shells.append(shell)
        return shell

    monkeypatch.setattr(RemoteExecManager, "_create_shell_session", _create_shell_session)
    return shells


def _manager(tmp_path: Path, *, enabled: bool = True) -> RemoteExecManager:
    return RemoteExecManager(cwd=str(tmp_path), enabled=enabled, poll_interval=0.01)


async def test_remote_exec_uses_connector_local_runtime_without_core_checkout(
    tmp_path: Path,
    created_shells: list[FakeShellSession],
) -> None:
    manager = _manager(tmp_path)

    result = await manager.handle_exec_op(
        {
            "op_id": "exec-terminal",
            "runtime": "terminal",
            "session": 0,
            "code": "ansi",
        }
    )

    assert result["ok"] is True
    assert result["result"]["output"] == "hello"
    assert result["result"]["running"] is False
    assert created_shells[0].commands == ["ansi"]

    await manager.close()


async def test_terminal_python_and_nodejs_runtimes_are_supported(
    tmp_path: Path,
    created_shells: list[FakeShellSession],
) -> None:
    manager = _manager(tmp_path)

    terminal_result = await manager.handle_exec_op(
        {
            "op_id": "exec-terminal",
            "runtime": "terminal",
            "session": 0,
            "code": "ansi",
        }
    )
    python_result = await manager.handle_exec_op(
        {
            "op_id": "exec-python",
            "runtime": "python",
            "session": 1,
            "code": "print(42)",
        }
    )
    node_result = await manager.handle_exec_op(
        {
            "op_id": "exec-node",
            "runtime": "nodejs",
            "session": 2,
            "code": "console.log('ok')",
        }
    )

    assert terminal_result["ok"] is True
    assert terminal_result["result"]["output"] == "hello"

    assert python_result["ok"] is True
    assert python_result["result"]["output"] == "42"
    assert "A0_PY_CODE" in created_shells[1].commands[0]
    assert sys.executable in created_shells[1].commands[0]

    assert node_result["ok"] is True
    assert node_result["result"]["output"] == "node ok"
    assert "A0_NODE_CODE" in created_shells[2].commands[0]

    await manager.close()


async def test_input_runtime_sends_keystrokes_into_running_session(
    tmp_path: Path,
    created_shells: list[FakeShellSession],
) -> None:
    manager = _manager(tmp_path)
    manager.set_exec_config(
        {
            "version": 1,
            "code_exec_timeouts": {
                "first_output_timeout": 0,
                "between_output_timeout": 1,
                "max_exec_timeout": 5,
                "dialog_timeout": 0,
            },
            "output_timeouts": {
                "first_output_timeout": 0,
                "between_output_timeout": 1,
                "max_exec_timeout": 5,
                "dialog_timeout": 0,
            },
            "dialog_patterns": [r"\?\s*$"],
        }
    )

    running_result = await manager.handle_exec_op(
        {
            "op_id": "exec-ask",
            "runtime": "terminal",
            "session": 0,
            "code": "ask",
        }
    )
    input_result = await manager.handle_exec_op(
        {
            "op_id": "exec-input",
            "runtime": "input",
            "session": 0,
            "keyboard": "y",
        }
    )

    assert running_result["ok"] is True
    assert running_result["result"]["running"] is True
    assert "Potential dialog detected" in running_result["result"]["message"]

    assert input_result["ok"] is True
    assert input_result["result"]["output"] == "input:y"
    assert input_result["result"]["running"] is False
    assert created_shells[0].inputs == ["y"]

    await manager.close()


async def test_terminal_runtime_reset_true_closes_running_session_and_runs_replacement(
    tmp_path: Path,
    created_shells: list[FakeShellSession],
) -> None:
    manager = _manager(tmp_path)
    manager.set_exec_config(
        {
            "version": 1,
            "code_exec_timeouts": {
                "first_output_timeout": 0,
                "between_output_timeout": 1,
                "max_exec_timeout": 5,
                "dialog_timeout": 0,
            },
            "output_timeouts": {
                "first_output_timeout": 0,
                "between_output_timeout": 1,
                "max_exec_timeout": 5,
                "dialog_timeout": 0,
            },
            "dialog_patterns": [r"\?\s*$"],
        }
    )

    running_result = await manager.handle_exec_op(
        {
            "op_id": "exec-ask",
            "runtime": "terminal",
            "session": 0,
            "code": "ask",
        }
    )
    replacement_result = await manager.handle_exec_op(
        {
            "op_id": "exec-reset-run",
            "runtime": "terminal",
            "session": 0,
            "code": "ansi",
            "reset": True,
        }
    )

    assert running_result["ok"] is True
    assert running_result["result"]["running"] is True
    assert len(created_shells) == 2
    assert created_shells[0].is_alive is False
    assert created_shells[0].commands == ["ask"]
    assert created_shells[1].commands == ["ansi"]
    assert replacement_result["ok"] is True
    assert replacement_result["result"]["output"] == "hello"
    assert replacement_result["result"]["running"] is False

    await manager.close()


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX shell quoting")
async def test_shell_close_terminates_child_process_that_keeps_stdout_open(
    tmp_path: Path,
) -> None:
    shell = LocalShellSession(cwd=str(tmp_path))
    sleeper = "import time; print('ready', flush=True); time.sleep(30)"

    await shell.connect()
    await shell.send_command(f"{shlex.quote(sys.executable)} -c {shlex.quote(sleeper)}")
    output, _ = await shell.read_output(timeout=2)

    assert "ready" in output
    await asyncio.wait_for(shell.close(), timeout=5)


async def test_mutating_exec_runtimes_are_blocked_when_local_access_is_read_only(
    tmp_path: Path,
    created_shells: list[FakeShellSession],
) -> None:
    manager = RemoteExecManager(
        cwd=str(tmp_path),
        enabled=True,
        allow_writes=False,
        poll_interval=0.01,
    )

    result = await manager.handle_exec_op(
        {
            "op_id": "exec-read-only",
            "runtime": "terminal",
            "session": 0,
            "code": "ansi",
        }
    )

    assert result["ok"] is False
    assert "Press F3 to switch to Read&Write" in result["error"]
    assert created_shells == []

    await manager.close()
