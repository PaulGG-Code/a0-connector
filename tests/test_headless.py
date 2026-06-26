from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_zero_cli.config import CLIConfig
from agent_zero_cli.headless.commands import command_may_start_agent, dispatch_headless_command
from agent_zero_cli.headless.renderer import JsonlRenderer, TextRenderer
from agent_zero_cli.headless.runner import HeadlessOptions, HeadlessRunner
from agent_zero_cli.session import SessionError


pytestmark = pytest.mark.anyio


class FakeSession:
    instances: list["FakeSession"] = []
    connect_error: SessionError | None = None
    stream_text = "4"
    final_snapshot_text = "4"
    initial_queue: list[dict[str, Any]] = []

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
        self.message_queue = [dict(item) for item in FakeSession.initial_queue]
        self.sent: list[str] = []
        self.queue_send_calls: list[tuple[str | None, bool]] = []
        self.queue_remove_calls: list[str | None] = []
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
                "data": {"text": self.stream_text},
            }
        )
        self.agent_active = False
        self.observer.on_complete(self.context_id)
        return {}

    async def send_message_queue(
        self,
        *,
        item_id: str | None = None,
        send_all: bool = True,
    ) -> dict[str, Any]:
        self.queue_send_calls.append((item_id, send_all))
        self.message_queue = []
        self.agent_active = True
        self.observer.on_event(
            {
                "context_id": self.context_id,
                "event": "assistant_message",
                "sequence": 2,
                "data": {"text": self.stream_text},
            }
        )
        self.agent_active = False
        self.observer.on_complete(self.context_id)
        return {"sent_count": 1, "message_queue": []}

    async def clear_message_queue(self) -> dict[str, Any]:
        self.queue_remove_calls.append(None)
        self.message_queue = []
        return {"message_queue": []}

    async def remove_message_from_queue(self, item_id: str) -> dict[str, Any]:
        self.queue_remove_calls.append(item_id)
        self.message_queue = [item for item in self.message_queue if item.get("id") != item_id]
        return {"message_queue": self.message_queue}

    async def refresh_context_snapshot(self) -> None:
        if self.final_snapshot_text is None:
            return
        self.observer.on_snapshot(
            [
                {
                    "context_id": self.context_id,
                    "event": "assistant_message",
                    "sequence": 2,
                    "data": {"text": self.final_snapshot_text, "meta": {"finished": True}},
                }
            ],
            [],
        )

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
    FakeSession.stream_text = "4"
    FakeSession.final_snapshot_text = "4"
    FakeSession.initial_queue = []

    import agent_zero_cli.headless.runner as runner_mod

    monkeypatch.setattr(runner_mod, "ConnectorSession", FakeSession)
    monkeypatch.setattr(runner_mod, "_COMPLETION_SETTLE_SECONDS", 0.0)


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
        SimpleNamespace(on_event=lambda event: None, on_complete=lambda context_id: None),
        workspace=tmp_path,
        remote_file_write_enabled=True,
        remote_exec_enabled=True,
    )
    session.host = "http://agent.test"

    status = await dispatch_headless_command(session, "/status")
    unavailable = await dispatch_headless_command(session, "/browser host on")

    assert any(line == "host: http://agent.test" for line in status.lines)
    assert any(line == "message queue: empty" for line in status.lines)
    assert unavailable.error is True
    assert unavailable.lines == ["/browser is not available in headless mode."]


async def test_headless_queue_commands_send_clear_and_remove(tmp_path: Path) -> None:
    session = FakeSession(
        CLIConfig(),
        SimpleNamespace(on_event=lambda event: None, on_complete=lambda context_id: None),
        workspace=tmp_path,
        remote_file_write_enabled=True,
        remote_exec_enabled=True,
    )
    session.message_queue = [
        {"id": "item-1", "text": "first queued prompt", "attachment_count": 0},
        {"id": "item-2", "text": "second queued prompt", "attachment_count": 1},
    ]

    summary = await dispatch_headless_command(session, "/queue")
    remove = await dispatch_headless_command(session, "/queue remove 2")
    clear = await dispatch_headless_command(session, "/queue clear")
    session.message_queue = [{"id": "item-1", "text": "first queued prompt"}]
    send = await dispatch_headless_command(session, "/send")

    assert summary.lines == [
        "Queued messages (2):",
        "1. first queued prompt",
        "2. second queued prompt [1 files]",
    ]
    assert remove.lines == ["Queued message removed."]
    assert clear.lines == ["Queue cleared."]
    assert send.lines == ["sent 1 queued message"]
    assert send.await_completion is True
    assert session.queue_remove_calls == ["item-2", None]
    assert session.queue_send_calls == [(None, True)]


def test_headless_queue_send_command_is_agent_starting() -> None:
    assert command_may_start_agent("/send") is True
    assert command_may_start_agent("/queue send") is True
    assert command_may_start_agent("/queue") is False
    assert command_may_start_agent("/status") is False


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


async def test_print_mode_renders_final_snapshot_update_before_complete(tmp_path: Path) -> None:
    FakeSession.stream_text = "HEADLESS_REMOTE_EXEC_SHORT"
    FakeSession.final_snapshot_text = "HEADLESS_REMOTE_EXEC_SHORT_OK"
    stdout = io.StringIO()
    stderr = io.StringIO()
    options = HeadlessOptions(
        host="http://agent.test",
        output="jsonl",
        print_prompt="remote exec check",
        workspace=tmp_path,
        config=CLIConfig(),
        stdout=stdout,
        stderr=stderr,
    )

    exit_code = await HeadlessRunner(options).run()

    assert exit_code == 0
    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [record["type"] for record in records] == ["ready", "event", "event", "complete"]
    assert records[1]["data"]["text"] == "HEADLESS_REMOTE_EXEC_SHORT"
    assert records[2]["data"]["text"] == "HEADLESS_REMOTE_EXEC_SHORT_OK"
    assert records[2]["data"]["meta"]["finished"] is True


async def test_print_mode_send_command_flushes_queue_and_waits(tmp_path: Path) -> None:
    FakeSession.initial_queue = [{"id": "queued-1", "text": "queued prompt"}]
    stdout = io.StringIO()
    stderr = io.StringIO()
    options = HeadlessOptions(
        host="http://agent.test",
        output="jsonl",
        print_prompt="/send",
        workspace=tmp_path,
        config=CLIConfig(),
        stdout=stdout,
        stderr=stderr,
    )

    exit_code = await HeadlessRunner(options).run()

    assert exit_code == 0
    records = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [record["type"] for record in records] == ["ready", "event", "notice", "complete"]
    assert records[1]["data"]["text"] == "4"
    assert records[2]["message"] == "sent 1 queued message"
    assert FakeSession.instances[-1].queue_send_calls == [(None, True)]


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
