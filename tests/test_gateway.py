from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import threading
from typing import Any

import pytest

from agent_zero_cli.config import CLIConfig
from agent_zero_cli.gateway import (
    GatewayOptions,
    GatewayRunner,
    JsonlWriter,
    gateway_options,
    normalize_gateway_host,
    normalize_scopes,
    sanitize_gateway_id,
)


pytestmark = pytest.mark.anyio


class FakeManager:
    def __init__(self, config: CLIConfig, *, persist_enabled: bool) -> None:
        self.config = config
        self.persist_enabled = persist_enabled


class FakeSession:
    instances: list["FakeSession"] = []

    def __init__(self, config: CLIConfig, observer: Any, **kwargs: Any) -> None:
        self.config = config
        self.observer = observer
        self.kwargs = kwargs
        self.closed = False
        self.connect_args: dict[str, Any] = {}
        self.gateway = dict(kwargs["gateway"])
        self.gateway.setdefault("state", "connected")
        self._state_callback = kwargs["on_gateway_state_change"]
        FakeSession.instances.append(self)

    async def connect(self, host: str, **kwargs: Any) -> str:
        self.connect_args = {"host": host, **kwargs}
        self._state_callback(self._gateway_metadata())
        return ""

    async def close(self) -> None:
        self.closed = True

    def _gateway_metadata(self) -> dict[str, Any]:
        return dict(self.gateway)

    async def set_gateway_master(self, enabled: bool) -> None:
        self.gateway["master_enabled"] = enabled

    async def replace_gateway_scopes(self, scopes: dict[str, Any]) -> None:
        self.gateway["scopes"] = normalize_scopes(scopes)

    async def refresh_remote_tool_metadata(self) -> bool:
        return True


def _options(tmp_path: Path) -> GatewayOptions:
    return GatewayOptions(
        host="http://agent.test",
        workspace=tmp_path,
        gateway_id="launcher-test",
        host_label="Test host",
        master_enabled=True,
        scopes=normalize_scopes("files,code_execution,browser,computer_use"),
        browser_selection="chromium:default",
    )


async def test_gateway_jsonl_contract_and_environment_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeSession.instances = []
    monkeypatch.setenv("A0_USERNAME", "launcher-user")
    monkeypatch.setenv("A0_PASSWORD", "launcher-secret")
    output = io.StringIO()
    commands = io.StringIO(
        '\n'.join(
            [
                json.dumps(
                    {
                        "request_id": "scope-1",
                        "action": "replace_scopes",
                        "scopes": {
                            "files": False,
                            "code_execution": True,
                            "browser": True,
                            "computer_use": False,
                        },
                    }
                ),
                json.dumps({"request_id": "stop-1", "action": "shutdown"}),
                "",
            ]
        )
    )
    runner = GatewayRunner(
        _options(tmp_path),
        CLIConfig(),
        writer=JsonlWriter(output),
        input_stream=commands,
        session_factory=FakeSession,
        browser_factory=FakeManager,
        computer_use_factory=FakeManager,
    )

    assert await runner.run() == 0

    session = FakeSession.instances[-1]
    assert session.connect_args["username"] == "launcher-user"
    assert session.connect_args["password"] == "launcher-secret"
    assert session.kwargs["tools_only"] is True
    assert session.kwargs["host_browser_manager"].persist_enabled is False
    assert session.kwargs["computer_use_manager"].persist_enabled is False
    assert session.gateway["scopes"]["files"] is False
    assert session.gateway["scopes"]["code_execution"] is False
    assert session.closed is True

    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert records[-1] == {"type": "stopped"}
    assert any(record.get("request_id") == "scope-1" and record.get("ok") for record in records)
    assert "launcher-secret" not in output.getvalue()
    assert "launcher-user" not in output.getvalue()


async def test_gateway_rejects_invalid_workspace_without_starting_session(tmp_path: Path) -> None:
    FakeSession.instances = []
    output = io.StringIO()
    options = _options(tmp_path / "missing")
    runner = GatewayRunner(
        options,
        CLIConfig(),
        writer=JsonlWriter(output),
        input_stream=io.StringIO(),
        session_factory=FakeSession,
        browser_factory=FakeManager,
        computer_use_factory=FakeManager,
    )

    assert await runner.run() == 2
    assert FakeSession.instances == []
    assert json.loads(output.getvalue())["code"] == "INVALID_WORKSPACE"


async def test_gateway_writes_command_result_before_metadata_refresh(tmp_path: Path) -> None:
    order: list[str] = []

    class OrderingSession(FakeSession):
        async def refresh_remote_tool_metadata(self) -> bool:
            order.append("refresh")
            return True

    class OrderingWriter(JsonlWriter):
        def write(self, payload: dict[str, Any]) -> None:
            if payload.get("type") == "result" and payload.get("request_id") == "scope-1":
                order.append("result")
            super().write(payload)

    commands = io.StringIO(
        "\n".join(
            [
                json.dumps(
                    {
                        "request_id": "scope-1",
                        "action": "replace_scopes",
                        "scopes": {
                            "files": False,
                            "code_execution": False,
                            "browser": True,
                            "computer_use": False,
                        },
                    }
                ),
                json.dumps({"request_id": "stop-1", "action": "shutdown"}),
                "",
            ]
        )
    )
    runner = GatewayRunner(
        _options(tmp_path),
        CLIConfig(),
        writer=OrderingWriter(io.StringIO()),
        input_stream=commands,
        session_factory=OrderingSession,
        browser_factory=FakeManager,
        computer_use_factory=FakeManager,
    )

    assert await runner.run() == 0
    assert order == ["result", "refresh"]


async def test_gateway_can_stop_while_jsonl_input_is_blocked(tmp_path: Path) -> None:
    class BlockingInput:
        def readline(self) -> str:
            threading.Event().wait()
            return ""

    FakeSession.instances = []
    output = io.StringIO()
    runner = GatewayRunner(
        _options(tmp_path),
        CLIConfig(),
        writer=JsonlWriter(output),
        input_stream=BlockingInput(),
        session_factory=FakeSession,
        browser_factory=FakeManager,
        computer_use_factory=FakeManager,
    )

    task = asyncio.create_task(runner.run())
    for _ in range(20):
        if runner.session is not None:
            break
        await asyncio.sleep(0)
    runner.stop_event.set()

    assert await asyncio.wait_for(task, timeout=0.2) == 0
    assert FakeSession.instances[-1].closed is True


def test_gateway_options_sanitize_identity_and_enforce_scope_dependency() -> None:
    options = gateway_options(
        host="http://agent.test/",
        workspace=".",
        gateway_id=" launcher id / unsafe ",
        host_label="  My   computer  ",
        master_enabled=True,
        scopes="code_execution,browser",
        browser_selection="chrome:default",
    )

    assert sanitize_gateway_id(" launcher id / unsafe ") == "launcher-id-unsafe"
    assert options.gateway_id == "launcher-id-unsafe"
    assert options.host == "http://agent.test"
    assert options.host_label == "My computer"
    assert options.scopes["files"] is False
    assert options.scopes["code_execution"] is False


def test_gateway_host_preserves_base_path_and_rejects_embedded_credentials() -> None:
    assert normalize_gateway_host("https://agent.test/a0/?view=chat#active") == "https://agent.test/a0"
    with pytest.raises(ValueError, match="without embedded credentials"):
        normalize_gateway_host("https://user:secret@agent.test/a0")
