from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_zero_cli.config import CLIConfig
from agent_zero_cli.headless.commands import dispatch_headless_command
from agent_zero_cli.headless.renderer import JsonlRenderer, TextRenderer
from agent_zero_cli.headless.runner import HeadlessOptions, HeadlessRunner
from agent_zero_cli.session import SessionError


pytestmark = pytest.mark.anyio


class FakeSession:
    instances: list["FakeSession"] = []
    connect_error: SessionError | None = None

    def __init__(
        self,
        config: CLIConfig,
        observer: Any,
        *,
        workspace: Path,
        remote_file_write_enabled: bool,
        remote_exec_enabled: bool,
    ) -> None:
        self.config = config
        self.observer = observer
        self.workspace = workspace
        self.remote_file_write_enabled = remote_file_write_enabled
        self.remote_exec_enabled = remote_exec_enabled
        self.remote_files = SimpleNamespace(scan_root=str(workspace))
        self.connector_features = {"message_queue", "chat_create"}
        self.host = ""
        self.context_id = "ctx-1"
        self.agent_active = False
        self.sent: list[str] = []
        self.closed = False
        FakeSession.instances.append(self)

    async def connect(
        self,
        host: str,
        *,
        username: str = "",
        password: str = "",
        context_id: str = "",
        chat_last: bool = False,
        new_chat: bool = False,
        restore_session: bool = True,
    ) -> str:
        del username, password, chat_last, restore_session
        if FakeSession.connect_error is not None:
            raise FakeSession.connect_error
        self.host = host
        self.context_id = context_id or ("ctx-new" if new_chat else "ctx-1")
        self.observer.on_stage("ready", "Ready when you are.", host)
        return self.context_id

    async def send_message(self, text: str, attachments: list[str] | None = None) -> dict[str, Any]:
        del attachments
        self.sent.append(text)
        self.agent_active = True
        self.observer.on_event(
            {
                "context_id": self.context_id,
                "event": "assistant_message",
                "sequence": 2,
                "data": {"text": "4"},
            }
        )
        self.agent_active = False
        self.observer.on_complete(self.context_id)
        return {}

    async def pause(self) -> dict[str, Any]:
        return {"ok": True}

    async def resume(self) -> dict[str, Any]:
        return {"ok": True}

    async def nudge(self) -> dict[str, Any]:
        return {"ok": True}

    async def reset(self) -> dict[str, Any]:
        return {"ok": True}

    async def list_chats(self) -> list[dict[str, Any]]:
        return [
            {"id": self.context_id, "name": "Active", "updated_at": "2026-06-11T08:00:00+00:00"},
            {"id": "ctx-2", "name": "Archive", "updated_at": "2026-06-10T08:00:00+00:00"},
        ]

    async def switch_context(self, context_id: str, *, has_messages_hint: bool | None = None) -> None:
        del has_messages_hint
        self.context_id = context_id

    async def new_context(self) -> str:
        self.context_id = "ctx-new"
        return self.context_id

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def reset_fake_session(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSession.instances = []
    FakeSession.connect_error = None

    import agent_zero_cli.headless.runner as runner_mod

    monkeypatch.setattr(runner_mod, "ConnectorSession", FakeSession)


def test_text_renderer_deduplicates_status_lines() -> None:
    renderer = TextRenderer(color=False)
    event = {
        "event": "tool_start",
        "sequence": 1,
        "data": {"heading": "web_search", "text": ""},
    }

    assert renderer.render_event(event) == ["- Using tool [web_search]"]
    assert renderer.render_event(event) == []
    assert renderer.render_event(
        {
            "event": "assistant_message",
            "sequence": 2,
            "data": {"text": "Done."},
        }
    ) == ["Done."]


def test_jsonl_renderer_emits_valid_records() -> None:
    renderer = JsonlRenderer()
    lines = renderer.render_event(
        {
            "context_id": "ctx-1",
            "event": "assistant_message",
            "sequence": 2,
            "data": {"text": "Done."},
        }
    )

    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["type"] == "event"
    assert payload["event"] == "assistant_message"


async def test_headless_commands_status_and_tui_only(tmp_path: Path) -> None:
    session = FakeSession(
        CLIConfig(),
        SimpleNamespace(),
        workspace=tmp_path,
        remote_file_write_enabled=True,
        remote_exec_enabled=True,
    )
    session.host = "http://agent.test"

    status = await dispatch_headless_command(session, "/status")
    unavailable = await dispatch_headless_command(session, "/browser host on")

    assert any(line == "host: http://agent.test" for line in status.lines)
    assert unavailable.error is True
    assert unavailable.lines == ["/browser is not available in headless mode."]


async def test_print_mode_jsonl_stdout_is_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    del monkeypatch
    stdout = io.StringIO()
    stderr = io.StringIO()
    options = HeadlessOptions(
        host="http://agent.test",
        output="jsonl",
        print_prompt="",
        workspace=tmp_path,
        config=CLIConfig(),
        stdin=io.StringIO("what is 2+2\n"),
        stdout=stdout,
        stderr=stderr,
    )

    exit_code = await HeadlessRunner(options).run()

    assert exit_code == 0
    assert stderr.getvalue() == ""
    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [record["type"] for record in records] == ["ready", "event", "complete"]
    assert records[1]["data"]["text"] == "4"
    assert FakeSession.instances[-1].sent == ["what is 2+2"]
    assert FakeSession.instances[-1].closed is True


async def test_completion_wait_stops_on_disconnect_without_timeout(tmp_path: Path) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    runner = HeadlessRunner(
        HeadlessOptions(
            host="http://agent.test",
            workspace=tmp_path,
            config=CLIConfig(),
            stdout=stdout,
            stderr=stderr,
        )
    )
    runner.session = FakeSession(
        CLIConfig(),
        runner,
        workspace=tmp_path,
        remote_file_write_enabled=True,
        remote_exec_enabled=True,
    )
    runner.session.agent_active = True

    wait_task = asyncio.create_task(runner._wait_for_completion())
    await asyncio.sleep(0)
    runner.on_disconnect()

    exit_code = await asyncio.wait_for(wait_task, timeout=1.0)

    assert exit_code == 1
    assert "DISCONNECTED" in stderr.getvalue()


async def test_non_tty_auth_failure_exits_two(tmp_path: Path) -> None:
    FakeSession.connect_error = SessionError(
        "AUTH_REQUIRED",
        "auth required: set A0_USERNAME/A0_PASSWORD or run the TUI once with remember host.",
        exit_code=2,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    options = HeadlessOptions(
        host="http://agent.test",
        output="text",
        print_prompt="hello",
        workspace=tmp_path,
        config=CLIConfig(),
        stdin=io.StringIO(""),
        stdout=stdout,
        stderr=stderr,
    )

    exit_code = await HeadlessRunner(options).run()

    assert exit_code == 2
    assert "AUTH_REQUIRED" in stderr.getvalue()
    assert "A0_USERNAME/A0_PASSWORD" in stderr.getvalue()
