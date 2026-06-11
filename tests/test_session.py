from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_zero_cli.config import CLIConfig
from agent_zero_cli.session import ConnectorSession, SessionError


pytestmark = pytest.mark.anyio


def _capabilities(**overrides: Any) -> dict[str, Any]:
    payload = {
        "protocol": "a0-connector.v1",
        "websocket_namespace": "/ws",
        "websocket_handlers": ["plugins/_a0_connector/ws_connector"],
        "auth": ["session"],
        "auth_required": False,
        "features": ["chat_create", "chat_get", "message_queue"],
    }
    payload.update(overrides)
    return payload


class Observer:
    def __init__(self) -> None:
        self.stages: list[tuple[str, str, str]] = []
        self.events: list[dict[str, Any]] = []
        self.snapshots: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []
        self.completed: list[str] = []
        self.errors: list[tuple[str, str]] = []
        self.disconnected = 0

    def on_stage(self, stage: str, message: str, detail: str = "") -> None:
        self.stages.append((stage, message, detail))

    def on_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def on_snapshot(self, events: list[dict[str, Any]], queue: list[dict[str, Any]]) -> None:
        self.snapshots.append((events, queue))

    def on_complete(self, context_id: str) -> None:
        self.completed.append(context_id)

    def on_error(self, code: str, message: str) -> None:
        self.errors.append((code, message))

    def on_disconnect(self) -> None:
        self.disconnected += 1


class FakeClient:
    instances: list["FakeClient"] = []
    capabilities = _capabilities()
    verify_session_result = True
    login_result = True
    chats: list[dict[str, Any]] = []
    chat_metadata: dict[str, dict[str, Any]] = {}
    create_chat_id = "ctx-created"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.connected = False
        self.hello_calls: list[dict[str, Any]] = []
        self.subscribe_calls: list[tuple[str, int]] = []
        self.unsubscribe_calls: list[str] = []
        self.remote_tree_updates: list[dict[str, Any]] = []
        self.sent_messages: list[tuple[str, str, list[str] | None]] = []
        self.queued_messages: list[tuple[str, str, list[str] | None]] = []
        self.login_calls: list[tuple[str, str]] = []
        self.restore_calls: list[str] = []
        self.clear_session_calls = 0
        self.disconnect_calls: list[tuple[bool, bool]] = []
        self.on_connect = None
        self.on_disconnect = None
        self.on_context_snapshot = None
        self.on_context_event = None
        self.on_context_complete = None
        self.on_message_queue_updated = None
        self.on_settings_updated = None
        self.on_error = None
        self.on_file_op = None
        self.on_exec_op = None
        self.on_computer_use_op = None
        self.on_browser_op = None
        FakeClient.instances.append(self)

    async def fetch_capabilities(self) -> dict[str, Any]:
        return dict(self.capabilities)

    def restore_session(self, host: str) -> bool:
        self.restore_calls.append(host)
        return True

    async def verify_session(self) -> bool:
        return bool(self.verify_session_result)

    def clear_session(self) -> None:
        self.clear_session_calls += 1

    async def login(self, username: str, password: str) -> bool:
        self.login_calls.append((username, password))
        return bool(self.login_result)

    async def connect_websocket(self) -> None:
        self.connected = True
        if self.on_connect is not None:
            self.on_connect()

    async def send_hello(self, **payload: Any) -> dict[str, Any]:
        self.hello_calls.append(payload)
        return {"exec_config": {"version": 1}}

    async def create_chat(self, *, current_context_id: str | None = None) -> str:
        del current_context_id
        return self.create_chat_id

    async def list_chats(self) -> list[dict[str, Any]]:
        return list(self.chats)

    async def get_chat(self, context_id: str) -> dict[str, Any]:
        return dict(self.chat_metadata.get(context_id, {}))

    async def subscribe_context(self, context_id: str, from_seq: int = 0) -> dict[str, Any]:
        self.subscribe_calls.append((context_id, from_seq))
        if self.on_context_snapshot is not None:
            self.on_context_snapshot(
                {
                    "context_id": context_id,
                    "events": [{"event": "info", "sequence": 1, "data": {"text": "loaded"}}],
                    "message_queue": [],
                }
            )
        return {}

    async def unsubscribe_context(self, context_id: str) -> dict[str, Any]:
        self.unsubscribe_calls.append(context_id)
        return {}

    async def send_remote_tree_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.remote_tree_updates.append(payload)
        return {}

    async def send_message(
        self,
        text: str,
        context_id: str,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        self.sent_messages.append((text, context_id, attachments))
        return {}

    async def add_message_to_queue(
        self,
        text: str,
        context_id: str,
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        self.queued_messages.append((text, context_id, attachments))
        return {"message_queue": [{"id": "queued-1", "text": text}]}

    async def pause_agent(self, context_id: str | None, *, paused: bool = True) -> dict[str, Any]:
        return {"ok": True, "paused": paused, "context_id": context_id}

    async def reset_chat(self, context_id: str) -> dict[str, Any]:
        return {"ok": True, "context_id": context_id}

    async def disconnect(self, *, close_http: bool = True, notify: bool = True) -> None:
        self.disconnect_calls.append((close_http, notify))
        self.connected = False


@pytest.fixture(autouse=True)
def reset_fake_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.instances = []
    FakeClient.capabilities = _capabilities()
    FakeClient.verify_session_result = True
    FakeClient.login_result = True
    FakeClient.chats = []
    FakeClient.chat_metadata = {}
    FakeClient.create_chat_id = "ctx-created"

    import agent_zero_cli.config as config_mod

    env_dir = tmp_path / ".agent-zero"
    env_file = env_dir / ".env"
    monkeypatch.setattr(config_mod, "_ENV_DIR", env_dir)
    monkeypatch.setattr(config_mod, "_ENV_FILE", env_file)


async def test_session_connects_and_advertises_headless_metadata(tmp_path: Path) -> None:
    FakeClient.chat_metadata = {"ctx-default": {"last_message": "hello"}}
    observer = Observer()
    session = ConnectorSession(
        CLIConfig(default_context_id="ctx-default"),
        observer,
        workspace=tmp_path,
        client_factory=FakeClient,
    )

    context_id = await session.connect("http://agent.test")

    client = FakeClient.instances[-1]
    assert context_id == "ctx-default"
    assert client.subscribe_calls == [("ctx-default", 0)]
    assert observer.snapshots
    assert client.remote_tree_updates
    assert client.hello_calls[-1]["computer_use"]["enabled"] is False
    assert client.hello_calls[-1]["host_browser"]["supported"] is False
    assert client.hello_calls[-1]["remote_files"] == {
        "enabled": True,
        "write_enabled": True,
        "mode": "read_write",
    }
    assert client.hello_calls[-1]["remote_exec"] == {"enabled": True}

    await session.close()


async def test_session_raises_auth_required_without_credentials(tmp_path: Path) -> None:
    FakeClient.capabilities = _capabilities(auth_required=True)
    FakeClient.verify_session_result = False
    session = ConnectorSession(
        CLIConfig(),
        Observer(),
        workspace=tmp_path,
        client_factory=FakeClient,
    )

    with pytest.raises(SessionError) as exc_info:
        await session.connect("http://agent.test")

    assert exc_info.value.code == "AUTH_REQUIRED"
    assert exc_info.value.exit_code == 2
    assert FakeClient.instances[-1].login_calls == []


async def test_session_logs_in_with_credentials(tmp_path: Path) -> None:
    FakeClient.capabilities = _capabilities(auth_required=True)
    FakeClient.verify_session_result = False
    FakeClient.login_result = True
    session = ConnectorSession(
        CLIConfig(),
        Observer(),
        workspace=tmp_path,
        client_factory=FakeClient,
    )

    await session.connect("http://agent.test", username="neo", password="trinity")

    client = FakeClient.instances[-1]
    assert client.restore_calls == ["http://agent.test"]
    assert client.login_calls == [("neo", "trinity")]

    await session.close()


async def test_session_rejects_capability_mismatch(tmp_path: Path) -> None:
    FakeClient.capabilities = _capabilities(protocol="a0-connector.v0")
    session = ConnectorSession(
        CLIConfig(),
        Observer(),
        workspace=tmp_path,
        client_factory=FakeClient,
    )

    with pytest.raises(SessionError) as exc_info:
        await session.connect("http://agent.test")

    assert exc_info.value.code == "CONTRACT_MISMATCH"
    assert exc_info.value.exit_code == 2


async def test_session_restores_saved_context_when_no_default(tmp_path: Path) -> None:
    FakeClient.chats = [{"id": "ctx-saved", "last_message": "saved"}]
    config = CLIConfig(
        last_context_id="ctx-saved",
        last_context_host="http://agent.test",
    )
    session = ConnectorSession(
        config,
        Observer(),
        workspace=tmp_path,
        client_factory=FakeClient,
    )

    await session.connect("http://agent.test")

    assert session.context_id == "ctx-saved"
    assert FakeClient.instances[-1].subscribe_calls == [("ctx-saved", 0)]

    await session.close()


async def test_session_queues_messages_while_agent_active(tmp_path: Path) -> None:
    session = ConnectorSession(
        CLIConfig(default_context_id="ctx-default"),
        Observer(),
        workspace=tmp_path,
        client_factory=FakeClient,
    )
    await session.connect("http://agent.test")
    session.agent_active = True

    await session.send_message("next")

    client = FakeClient.instances[-1]
    assert client.sent_messages == []
    assert client.queued_messages == [("next", "ctx-default", [])]
    assert session.message_queue == [{"id": "queued-1", "text": "next"}]

    await session.close()
