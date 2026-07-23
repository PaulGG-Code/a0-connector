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


class FakeComputerManager(FakeManager):
    def __init__(self, config: CLIConfig, *, persist_enabled: bool) -> None:
        super().__init__(config, persist_enabled=persist_enabled)
        self.prompted_setup_calls = 0

    async def setup_permissions(self, context_id: str, *, prompt: bool) -> dict[str, Any]:
        assert context_id == "launcher"
        if prompt:
            self.prompted_setup_calls += 1
            if self.prompted_setup_calls > 1:
                return {
                    "ok": False,
                    "code": "COMPUTER_USE_RESTART_REQUIRED",
                    "error": "Restart Agent Zero Launcher.",
                    "result": {"state": "restart_required"},
                }
            return {
                "ok": True,
                "result": {
                    "state": "ready",
                    "accessibility": "granted",
                    "screen_recording": "granted",
                },
            }
        return {
            "ok": True,
            "result": {
                "state": "screen_recording_required",
                "accessibility": "granted",
                "screen_recording": "required",
            },
        }

    async def rearm(self, _context_id: str) -> dict[str, Any]:
        raise AssertionError("gateway rearm must delegate to setup_permissions")


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
        scopes=normalize_scopes("file_read,file_write,code_execution,browser,computer_use"),
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
                            "file_write": True,
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
    config = CLIConfig()
    runner = GatewayRunner(
        _options(tmp_path),
        config,
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
    assert (
        session.kwargs["computer_use_manager"].config.computer_use_trust_mode
        == "persistent"
    )
    assert config.computer_use_trust_mode == "allow"
    assert session.gateway["scopes"]["files"] is False
    assert session.gateway["scopes"]["file_write"] is False
    assert session.gateway["scopes"]["code_execution"] is False
    assert session.gateway["features"] == ["computer_use_setup_v1"]
    assert session.closed is True

    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert records[-1] == {"type": "stopped"}
    assert any(record.get("request_id") == "scope-1" and record.get("ok") for record in records)
    assert "launcher-secret" not in output.getvalue()
    assert "launcher-user" not in output.getvalue()


async def test_gateway_computer_use_setup_is_correlated_and_unwraps_manager_results(
    tmp_path: Path,
) -> None:
    output = io.StringIO()
    commands = io.StringIO(
        "\n".join(
            [
                json.dumps(
                    {
                        "request_id": "setup-1",
                        "action": "setup_computer_use",
                        "prompt": True,
                    }
                ),
                json.dumps({"request_id": "rearm-1", "action": "rearm_computer_use"}),
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
        computer_use_factory=FakeComputerManager,
    )

    assert await runner.run() == 0
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    setup = next(record for record in records if record.get("request_id") == "setup-1")
    assert setup == {
        "type": "result",
        "request_id": "setup-1",
        "ok": True,
        "result": {
            "state": "ready",
            "accessibility": "granted",
            "screen_recording": "granted",
        },
    }
    rearm = next(record for record in records if record.get("request_id") == "rearm-1")
    assert rearm["ok"] is False
    assert rearm["code"] == "COMPUTER_USE_RESTART_REQUIRED"
    assert rearm["result"] == {"state": "restart_required"}


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
                            "file_write": False,
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


async def test_gateway_does_not_emit_a_second_result_when_metadata_refresh_fails(
    tmp_path: Path,
) -> None:
    class FailingRefreshSession(FakeSession):
        async def refresh_remote_tool_metadata(self) -> bool:
            raise RuntimeError("metadata refresh failed")

    output = io.StringIO()
    commands = io.StringIO(
        "\n".join(
            [
                json.dumps(
                    {
                        "request_id": "scope-refresh-1",
                        "action": "replace_scopes",
                        "scopes": {"browser": True},
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
        session_factory=FailingRefreshSession,
        browser_factory=FakeManager,
        computer_use_factory=FakeComputerManager,
    )

    assert await runner.run() == 0
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    correlated = [item for item in records if item.get("request_id") == "scope-refresh-1"]
    assert len(correlated) == 1
    assert correlated[0]["ok"] is True
    assert any(
        item.get("type") == "error"
        and "metadata refresh failed" in item.get("message", "")
        for item in records
    )


async def test_failed_browser_repair_still_refreshes_gateway_metadata(tmp_path: Path) -> None:
    class RefreshingSession(FakeSession):
        refresh_count = 0

        async def refresh_remote_tool_metadata(self) -> bool:
            type(self).refresh_count += 1
            self.gateway["status"] = {
                "browser": {
                    "status": "unsupported",
                    "can_repair": False,
                    "support_reason": "No installed Chromium-family browser profile was detected.",
                }
            }
            return True

    class RepairingBrowser(FakeManager):
        async def ensure_available(self, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("No Chromium-family browser profile was found.")

    RefreshingSession.refresh_count = 0
    output = io.StringIO()
    commands = io.StringIO(
        "\n".join(
            [
                json.dumps({"request_id": "browser-1", "action": "prepare_browser"}),
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
        session_factory=RefreshingSession,
        browser_factory=RepairingBrowser,
        computer_use_factory=FakeComputerManager,
    )

    assert await runner.run() == 0
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    result = next(item for item in records if item.get("request_id") == "browser-1")
    assert result["ok"] is False
    assert RefreshingSession.refresh_count == 1
    refreshed = [item for item in records if item.get("type") == "status"][-1]
    assert refreshed["gateway"]["status"]["browser"]["can_repair"] is False


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
        scopes="file_read,code_execution,browser",
        browser_selection="chrome:default",
    )

    assert sanitize_gateway_id(" launcher id / unsafe ") == "launcher-id-unsafe"
    assert options.gateway_id == "launcher-id-unsafe"
    assert options.host == "http://agent.test"
    assert options.host_label == "My computer"
    assert options.scopes["files"] is True
    assert options.scopes["file_write"] is False
    assert options.scopes["code_execution"] is False


def test_gateway_scopes_keep_legacy_files_read_write() -> None:
    assert normalize_scopes({"files": True})["file_write"] is True
    assert normalize_scopes("files")["file_write"] is True
    assert normalize_scopes("files,code_execution")["code_execution"] is True
    assert normalize_scopes("file_read")["file_write"] is False


def test_gateway_host_preserves_base_path_and_rejects_embedded_credentials() -> None:
    assert normalize_gateway_host("https://agent.test/a0/?view=chat#active") == "https://agent.test/a0"
    with pytest.raises(ValueError, match="without embedded credentials"):
        normalize_gateway_host("https://user:secret@agent.test/a0")
