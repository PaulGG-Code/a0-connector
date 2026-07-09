from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Protocol

from agent_zero_cli.client import A0Client, A0ConnectorPluginMissingError, DEFAULT_HOST
from agent_zero_cli.config import CLIConfig, save_last_context
from agent_zero_cli.protocol import connector_version_warning, validate_capabilities
from agent_zero_cli.remote_exec import RemoteExecManager
from agent_zero_cli.remote_files import RemoteFileUtility

_REMOTE_TREE_KEEPALIVE_SECONDS = 60.0
_RECOVERY_DELAYS_SECONDS = (1.0, 2.0, 5.0, 10.0, 20.0)


class SessionObserver(Protocol):
    def on_stage(self, stage: str, message: str, detail: str = "") -> None: ...
    def on_event(self, event: dict[str, Any]) -> None: ...
    def on_snapshot(self, events: list[dict[str, Any]], queue: list[dict[str, Any]]) -> None: ...
    def on_complete(self, context_id: str) -> None: ...
    def on_error(self, code: str, message: str) -> None: ...
    def on_disconnect(self) -> None: ...


class NullSessionObserver:
    def on_stage(self, stage: str, message: str, detail: str = "") -> None:
        return None

    def on_event(self, event: dict[str, Any]) -> None:
        return None

    def on_snapshot(self, events: list[dict[str, Any]], queue: list[dict[str, Any]]) -> None:
        return None

    def on_complete(self, context_id: str) -> None:
        return None

    def on_error(self, code: str, message: str) -> None:
        return None

    def on_disconnect(self) -> None:
        return None


class SessionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str = "",
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.exit_code = exit_code


ClientFactory = Callable[[str], A0Client]


def normalize_host(host: str) -> str:
    return str(host or "").strip() or DEFAULT_HOST


def _chat_identifier(chat: dict[str, Any]) -> str:
    return str(chat.get("id") or chat.get("context_id") or chat.get("ctxid") or "").strip()


def _chat_has_messages(chat: dict[str, Any]) -> bool:
    return bool(chat.get("last_message") or chat.get("log_entries"))


def _unsupported_result(data: dict[str, Any], *, tool: str, code: str) -> dict[str, Any]:
    return {
        "op_id": data.get("op_id", ""),
        "ok": False,
        "error": f"{tool} is not available in headless mode.",
        "code": code,
    }


class ConnectorSession:
    """UI-agnostic Agent Zero connector session.

    The session owns the connector transport plus host-side file and exec
    handlers. Presentation layers observe it through ``SessionObserver``.
    """

    def __init__(
        self,
        config: CLIConfig,
        observer: SessionObserver | None = None,
        *,
        workspace: Path | str | None = None,
        client_factory: ClientFactory = A0Client,
        remote_file_write_enabled: bool = True,
        remote_exec_enabled: bool = True,
    ) -> None:
        self.config = config
        self.observer = observer or NullSessionObserver()
        self.workspace = Path(workspace or Path.cwd()).expanduser().resolve()
        self._client_factory = client_factory
        self.remote_file_write_enabled = remote_file_write_enabled
        self.remote_exec_enabled = remote_exec_enabled
        self.remote_files = RemoteFileUtility(
            scan_root=str(self.workspace),
            allow_writes=self.remote_file_write_enabled,
        )
        self.remote_exec = RemoteExecManager(
            cwd=self.remote_files.scan_root,
            enabled=self.remote_exec_enabled,
            allow_writes=self.remote_file_write_enabled,
        )
        self.client: A0Client | None = None
        self.capabilities: dict[str, Any] = {}
        self.connector_features: set[str] = set()
        self.host = ""
        self.context_id = ""
        self.context_has_messages = False
        self.connected = False
        self.agent_active = False
        self.message_queue: list[dict[str, Any]] = []
        self.goal: dict[str, Any] | None = None
        self._context_run_complete = True
        self._last_remote_tree_hash = ""
        self._last_remote_tree_published_at = 0.0
        self._remote_tree_task: asyncio.Task[None] | None = None
        self._recovery_task: asyncio.Task[None] | None = None

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
        await self._reset_runtime()
        normalized_host = normalize_host(host).rstrip("/")
        self.host = normalized_host
        self.config.instance_url = normalized_host
        client = self._client_factory(normalized_host)
        self.client = client

        self._stage("connecting", "Probing connector capabilities...", normalized_host)
        capabilities = await self._fetch_and_validate_capabilities(client)
        self.capabilities = capabilities
        self.connector_features = set(capabilities.get("features") or [])

        if bool(capabilities.get("auth_required")):
            await self._ensure_authenticated(
                client,
                normalized_host,
                username=username,
                password=password,
                restore_session=restore_session,
            )

        self._wire_client_callbacks(client)

        self._stage("connecting", "Opening connector WebSocket...", normalized_host)
        try:
            await client.connect_websocket()
            hello = await client.send_hello(
                computer_use=self._computer_use_metadata(),
                host_browser=self._host_browser_metadata(),
                remote_files=self._remote_file_metadata(),
                remote_exec=self._remote_exec_metadata(),
            )
            self.remote_exec.set_exec_config(hello.get("exec_config") if isinstance(hello, dict) else None)
        except Exception as exc:
            await self._disconnect_client(close_http=False)
            raise SessionError(
                "WEBSOCKET_FAILED",
                str(exc),
                stage="websocket",
                exit_code=2,
            ) from exc

        self._stage("connecting", "Resolving chat context...", normalized_host)
        try:
            resolved_context_id, has_messages_hint = await self._resolve_initial_context(
                requested_context_id=context_id,
                chat_last=chat_last,
                new_chat=new_chat,
            )
            self.context_id = resolved_context_id
            self.context_has_messages = has_messages_hint
            self.message_queue = []
            self.goal = None
            self.agent_active = False
            self._context_run_complete = True
            await client.subscribe_context(resolved_context_id)
            await self.refresh_remote_tool_metadata()
            await self.publish_remote_tree_snapshot(force=True)
        except Exception as exc:
            await self._disconnect_client(close_http=False)
            raise SessionError(
                "CONTEXT_FAILED",
                str(exc),
                stage="context",
                exit_code=2,
            ) from exc

        self.connected = True
        self._remember_context(resolved_context_id)
        self._start_remote_tree_publisher()
        self._stage("ready", "Ready when you are.", normalized_host)
        if warning := connector_version_warning(capabilities):
            self._stage("warning", warning, "")
        return resolved_context_id

    async def send_message(self, text: str, attachments: list[str] | None = None) -> dict[str, Any]:
        client = self._require_client()
        context_id = self._require_context()
        message = str(text or "").strip()
        if not message and not attachments:
            raise SessionError("EMPTY_MESSAGE", "Message text is empty.", stage="send", exit_code=1)

        await self.refresh_remote_tool_metadata()
        await self.publish_remote_tree_snapshot(force=True)

        attachment_paths = list(attachments or [])
        if "message_queue" in self.connector_features and (self.agent_active or self.message_queue):
            response = await client.add_message_to_queue(message, context_id, attachments=attachment_paths)
            queue = response.get("message_queue") if isinstance(response, dict) else None
            if isinstance(queue, list):
                self.message_queue = [item for item in queue if isinstance(item, dict)]
            return response

        self.agent_active = True
        self._context_run_complete = False
        self.context_has_messages = True
        return await client.send_message(message, context_id, attachments=attachment_paths)

    async def send_message_queue(
        self,
        *,
        item_id: str | None = None,
        send_all: bool = True,
    ) -> dict[str, Any]:
        client = self._require_client()
        context_id = self._require_context()
        previous_agent_active = self.agent_active
        previous_run_complete = self._context_run_complete

        self.agent_active = True
        self._context_run_complete = False
        try:
            response = await client.send_message_queue(
                context_id,
                item_id=item_id,
                send_all=send_all,
            )
        except Exception:
            self.agent_active = previous_agent_active
            self._context_run_complete = previous_run_complete
            raise

        queue = response.get("message_queue") if isinstance(response, dict) else None
        if isinstance(queue, list):
            self.message_queue = [item for item in queue if isinstance(item, dict)]

        try:
            sent_count = int(response.get("sent_count", 0) or 0) if isinstance(response, dict) else 0
        except (TypeError, ValueError):
            sent_count = 0
        if sent_count <= 0:
            self.agent_active = previous_agent_active
            self._context_run_complete = previous_run_complete
        else:
            self.context_has_messages = True
        return response

    async def clear_message_queue(self) -> dict[str, Any]:
        response = await self._require_client().remove_message_from_queue(self._require_context())
        queue = response.get("message_queue") if isinstance(response, dict) else None
        self.message_queue = (
            [item for item in queue if isinstance(item, dict)]
            if isinstance(queue, list)
            else []
        )
        return response

    async def remove_message_from_queue(self, item_id: str) -> dict[str, Any]:
        normalized_item_id = str(item_id or "").strip()
        if not normalized_item_id:
            raise SessionError(
                "MISSING_QUEUE_ITEM",
                "Queued message id is required.",
                stage="queue",
                exit_code=1,
            )
        response = await self._require_client().remove_message_from_queue(
            self._require_context(),
            item_id=normalized_item_id,
        )
        queue = response.get("message_queue") if isinstance(response, dict) else None
        if isinstance(queue, list):
            self.message_queue = [item for item in queue if isinstance(item, dict)]
        return response

    async def goal_action(self, action: str, **payload: Any) -> dict[str, Any]:
        client = self._require_client()
        response = await client.goal_action(action, self._require_context(), **payload)
        if response.get("ok", True):
            goal = response.get("goal") if isinstance(response, dict) else None
            self.goal = dict(goal) if isinstance(goal, dict) else None
        return response

    async def refresh_goal(self) -> dict[str, Any]:
        return await self.goal_action("get")

    async def pause(self) -> dict[str, Any]:
        client = self._require_client()
        return await client.pause_agent(self._require_context(), paused=True)

    async def resume(self) -> dict[str, Any]:
        client = self._require_client()
        return await client.pause_agent(self._require_context(), paused=False)

    async def nudge(self) -> dict[str, Any]:
        client = self._require_client()
        self.agent_active = True
        self._context_run_complete = False
        return await client.nudge_agent(self._require_context())

    async def reset(self) -> dict[str, Any]:
        client = self._require_client()
        response = await client.reset_chat(self._require_context())
        self.agent_active = False
        self._context_run_complete = True
        self.context_has_messages = False
        self.message_queue = []
        self.goal = None
        return response

    async def list_chats(self) -> list[dict[str, Any]]:
        return await self._require_client().list_chats()

    async def new_context(self) -> str:
        client = self._require_client()
        context_id = await client.create_chat(current_context_id=self.context_id or None)
        await self.switch_context(context_id, has_messages_hint=False)
        return context_id

    async def switch_context(self, context_id: str, *, has_messages_hint: bool | None = None) -> None:
        client = self._require_client()
        normalized_context_id = str(context_id or "").strip()
        if not normalized_context_id:
            raise SessionError("MISSING_CONTEXT", "Context id is required.", stage="context", exit_code=1)

        if self.context_id:
            with contextlib.suppress(Exception):
                await client.unsubscribe_context(self.context_id)

        self.context_id = normalized_context_id
        self.context_has_messages = bool(has_messages_hint)
        self.agent_active = False
        self._context_run_complete = True
        self.message_queue = []
        self.goal = None
        await client.subscribe_context(normalized_context_id, from_seq=0)
        await self.refresh_remote_tool_metadata()
        await self.publish_remote_tree_snapshot(force=True)
        self._remember_context(normalized_context_id)
        self._stage("ready", "Switched chat.", normalized_context_id)

    async def refresh_context_snapshot(self) -> None:
        await self._require_client().subscribe_context(self._require_context(), from_seq=0)

    async def refresh_remote_tool_metadata(self) -> bool:
        client = self.client
        if client is None or not getattr(client, "connected", False):
            return True
        try:
            hello = await client.send_hello(
                context_id=self.context_id or None,
                computer_use=self._computer_use_metadata(),
                host_browser=self._host_browser_metadata(),
                remote_files=self._remote_file_metadata(),
                remote_exec=self._remote_exec_metadata(),
            )
        except Exception:
            return False
        self.remote_exec.set_exec_config(hello.get("exec_config") if isinstance(hello, dict) else None)
        return True

    async def publish_remote_tree_snapshot(self, *, force: bool = False) -> None:
        client = self.client
        if client is None or not getattr(client, "connected", False):
            return

        snapshot = self.remote_files.build_tree_snapshot()
        now = monotonic()
        if (
            not force
            and snapshot.tree_hash == self._last_remote_tree_hash
            and now - self._last_remote_tree_published_at < _REMOTE_TREE_KEEPALIVE_SECONDS
        ):
            return

        try:
            await client.send_remote_tree_update(snapshot.as_payload())
        except Exception:
            return

        self._last_remote_tree_hash = snapshot.tree_hash
        self._last_remote_tree_published_at = now

    async def close(self) -> None:
        self._stop_recovery()
        self._stop_remote_tree_publisher()
        with contextlib.suppress(Exception):
            await self.remote_exec.close()
        await self._disconnect_client(close_http=True)
        self.connected = False
        self.agent_active = False
        self._context_run_complete = True

    async def _reset_runtime(self) -> None:
        self._stop_recovery()
        self._stop_remote_tree_publisher()
        self.connected = False
        self.agent_active = False
        self._context_run_complete = True
        self.message_queue = []
        self.context_id = ""
        self.context_has_messages = False
        self.capabilities = {}
        self.connector_features = set()
        self._last_remote_tree_hash = ""
        self._last_remote_tree_published_at = 0.0
        self.goal = None
        with contextlib.suppress(Exception):
            await self.remote_exec.close()
        await self._disconnect_client(close_http=True)
        self.remote_exec = RemoteExecManager(
            cwd=self.remote_files.scan_root,
            enabled=self.remote_exec_enabled,
            allow_writes=self.remote_file_write_enabled,
        )

    async def _fetch_and_validate_capabilities(self, client: A0Client) -> dict[str, Any]:
        try:
            capabilities = await client.fetch_capabilities()
        except A0ConnectorPluginMissingError as exc:
            raise SessionError("PLUGIN_MISSING", str(exc), stage="capabilities", exit_code=2) from exc
        except Exception as exc:
            raise SessionError("CONNECTOR_UNAVAILABLE", str(exc), stage="capabilities", exit_code=2) from exc

        try:
            validate_capabilities(capabilities)
        except ValueError as exc:
            raise SessionError("CONTRACT_MISMATCH", str(exc), stage="capabilities", exit_code=2) from exc
        return capabilities

    async def _ensure_authenticated(
        self,
        client: A0Client,
        host: str,
        *,
        username: str,
        password: str,
        restore_session: bool,
    ) -> None:
        self._stage("login", "Verifying Agent Zero session...", host)
        if restore_session:
            with contextlib.suppress(Exception):
                client.restore_session(host)

        try:
            session_ok = await client.verify_session()
        except Exception as exc:
            raise SessionError("SESSION_VERIFY_FAILED", str(exc), stage="login", exit_code=2) from exc

        if not session_ok:
            with contextlib.suppress(Exception):
                client.clear_session()

        if not session_ok and username and password:
            self._stage("login", "Signing in...", host)
            try:
                session_ok = await client.login(username, password)
            except Exception as exc:
                raise SessionError("LOGIN_FAILED", str(exc), stage="login", exit_code=2) from exc

        if not session_ok:
            raise SessionError(
                "AUTH_REQUIRED",
                "auth required: set A0_USERNAME/A0_PASSWORD or run the TUI once with remember host.",
                stage="login",
                exit_code=2,
            )

    async def _resolve_initial_context(
        self,
        *,
        requested_context_id: str,
        chat_last: bool,
        new_chat: bool,
    ) -> tuple[str, bool]:
        client = self._require_client()
        if new_chat:
            return await client.create_chat(), False

        default_context_id = str(requested_context_id or "").strip()
        if not default_context_id and not chat_last:
            default_context_id = self.config.default_context_id.strip()
        if default_context_id:
            return default_context_id, await self._context_has_messages(default_context_id)

        saved_context_id = self._saved_context_for_host(self.host)
        if saved_context_id:
            try:
                contexts = await client.list_chats()
            except Exception:
                contexts = []
            selected = next(
                (context for context in contexts if _chat_identifier(context) == saved_context_id),
                None,
            )
            if selected is not None:
                has_messages_hint = _chat_has_messages(selected)
                if not has_messages_hint:
                    has_messages_hint = await self._context_has_messages(saved_context_id)
                return saved_context_id, has_messages_hint

        return await client.create_chat(), False

    async def _context_has_messages(self, context_id: str) -> bool:
        if "chat_get" not in self.connector_features:
            return False
        try:
            metadata = await self._require_client().get_chat(context_id)
        except Exception:
            return False
        return _chat_has_messages(metadata)

    def _wire_client_callbacks(self, client: A0Client) -> None:
        client.on_connect = self._handle_connect
        client.on_disconnect = self._handle_disconnect
        client.on_context_snapshot = self._handle_context_snapshot
        client.on_context_event = self._handle_context_event
        client.on_context_complete = self._handle_context_complete
        client.on_message_queue_updated = self._handle_message_queue_updated
        client.on_error = self._handle_connector_error
        client.on_file_op = self.remote_files.handle_file_op
        client.on_exec_op = self.remote_exec.handle_exec_op
        client.on_computer_use_op = self._handle_unsupported_computer_use_op
        client.on_browser_op = self._handle_unsupported_browser_op

    def _handle_connect(self) -> None:
        self.connected = True

    def _handle_disconnect(self) -> None:
        self.connected = False
        if self._recovery_task is not None and not self._recovery_task.done():
            return
        self._recovery_task = asyncio.create_task(self._recover_websocket())

    def _handle_context_snapshot(self, data: dict[str, Any]) -> None:
        if data.get("context_id") != self.context_id:
            return
        events = data.get("events", [])
        queue = data.get("message_queue", [])
        event_items = [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []
        queue_items = [item for item in queue if isinstance(item, dict)] if isinstance(queue, list) else []
        self.message_queue = queue_items
        for event in event_items:
            if event.get("event") in {"user_message", "assistant_message", "assistant_delta"}:
                self.context_has_messages = True
        self.observer.on_snapshot(event_items, queue_items)

    def _handle_context_event(self, data: dict[str, Any]) -> None:
        if data.get("context_id") != self.context_id:
            return
        event_type = str(data.get("event") or "")
        if event_type in {"user_message", "assistant_message", "assistant_delta"}:
            self.context_has_messages = True
        if event_type != "message_complete" and not self._context_run_complete:
            self.agent_active = True
            self._context_run_complete = False
        self.observer.on_event(data)

    def _handle_context_complete(self, data: dict[str, Any]) -> None:
        context_id = str(data.get("context_id") or "")
        if context_id != self.context_id:
            return
        self.agent_active = False
        self._context_run_complete = True
        self.observer.on_complete(context_id)

    def _handle_message_queue_updated(self, data: dict[str, Any]) -> None:
        if data.get("context_id") != self.context_id:
            return
        queue = data.get("message_queue", data.get("items", []))
        queue_items = [item for item in queue if isinstance(item, dict)] if isinstance(queue, list) else []
        self.message_queue = queue_items
        self.observer.on_snapshot([], queue_items)

    def _handle_connector_error(self, data: dict[str, Any]) -> None:
        code = str(data.get("code") or "ERROR")
        message = str(data.get("message") or data.get("error") or "Unknown connector error")
        self.observer.on_error(code, message)

    async def _handle_unsupported_computer_use_op(self, data: dict[str, Any]) -> dict[str, Any]:
        return _unsupported_result(data, tool="Computer use", code="COMPUTER_USE_UNSUPPORTED")

    async def _handle_unsupported_browser_op(self, data: dict[str, Any]) -> dict[str, Any]:
        return _unsupported_result(data, tool="Host browser", code="HOST_BROWSER_UNSUPPORTED")

    def _computer_use_metadata(self) -> dict[str, Any]:
        return {
            "supported": False,
            "enabled": False,
            "status": "unsupported",
            "last_error": "Computer use is not available in headless mode.",
            "restore_token_present": False,
        }

    def _host_browser_metadata(self) -> dict[str, Any]:
        return {
            "supported": False,
            "enabled": False,
            "status": "unsupported",
            "support_reason": "Host browser is not available in headless mode.",
            "capabilities": [],
        }

    def _remote_file_metadata(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "write_enabled": self.remote_file_write_enabled,
            "mode": "read_write" if self.remote_file_write_enabled else "read_only",
        }

    def _remote_exec_metadata(self) -> dict[str, Any]:
        return {
            "enabled": self.remote_exec_enabled,
        }

    def _start_remote_tree_publisher(self) -> None:
        self._stop_remote_tree_publisher()
        self._remote_tree_task = asyncio.create_task(self._remote_tree_publish_loop())

    def _stop_remote_tree_publisher(self) -> None:
        task = self._remote_tree_task
        self._remote_tree_task = None
        if task is not None and not task.done():
            task.cancel()

    def _stop_recovery(self) -> None:
        task = self._recovery_task
        self._recovery_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _recover_websocket(self) -> None:
        client = self.client
        context_id = self.context_id
        if client is None or not self.host or not context_id:
            self._recovery_task = None
            self.agent_active = False
            self.observer.on_disconnect()
            return

        try:
            self._stop_remote_tree_publisher()
            for attempt, delay in enumerate(_RECOVERY_DELAYS_SECONDS, start=1):
                self._stage(
                    "connecting",
                    "Connection lost; reconnecting...",
                    f"{self.host} (attempt {attempt})",
                )
                await asyncio.sleep(delay)
                if self.client is not client or not self.context_id:
                    return
                try:
                    await client.connect_websocket()
                    hello = await client.send_hello(
                        context_id=context_id,
                        computer_use=self._computer_use_metadata(),
                        host_browser=self._host_browser_metadata(),
                        remote_files=self._remote_file_metadata(),
                        remote_exec=self._remote_exec_metadata(),
                    )
                    exec_config = hello.get("exec_config") if isinstance(hello, dict) else None
                    self.remote_exec.set_exec_config(exec_config)
                    await client.subscribe_context(context_id)
                    await self.publish_remote_tree_snapshot(force=True)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = str(exc).strip() or exc.__class__.__name__
                    if attempt == len(_RECOVERY_DELAYS_SECONDS):
                        self._stage("error", "Connection lost", last_error)
                        self.agent_active = False
                        self.observer.on_disconnect()
                        return
                    continue

                self.connected = True
                self.agent_active = False
                self._context_run_complete = True
                self._start_remote_tree_publisher()
                self._stage("ready", "Reconnected.", self.host)
                return
        finally:
            self._recovery_task = None

    async def _remote_tree_publish_loop(self) -> None:
        try:
            while self.connected:
                await asyncio.sleep(30.0)
                await self.publish_remote_tree_snapshot()
        except asyncio.CancelledError:
            return

    def _saved_context_for_host(self, host: str) -> str:
        normalized_host = normalize_host(host).rstrip("/")
        saved_host = self.config.last_context_host.strip().rstrip("/")
        if not normalized_host or normalized_host != saved_host:
            return ""
        return self.config.last_context_id.strip()

    def _remember_context(self, context_id: str) -> None:
        normalized_context_id = str(context_id or "").strip()
        normalized_host = normalize_host(self.host).rstrip("/")
        if not normalized_context_id or not normalized_host:
            return
        self.config.last_context_id = normalized_context_id
        self.config.last_context_host = normalized_host
        save_last_context(normalized_host, normalized_context_id)

    async def _disconnect_client(self, *, close_http: bool) -> None:
        client = self.client
        self.client = None
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.disconnect(close_http=close_http, notify=False)

    def _require_client(self) -> A0Client:
        if self.client is None:
            raise SessionError("NOT_CONNECTED", "Not connected to Agent Zero.", stage="connection", exit_code=1)
        return self.client

    def _require_context(self) -> str:
        if not self.context_id:
            raise SessionError("NO_CONTEXT", "No active chat context.", stage="context", exit_code=1)
        return self.context_id

    def _stage(self, stage: str, message: str, detail: str = "") -> None:
        self.observer.on_stage(stage, message, detail)
