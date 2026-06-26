from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import shlex
from typing import Any, Mapping

from agent_zero_cli.session import ConnectorSession

_TUI_ONLY_COMMANDS = {
    "/attach",
    "/browser",
    "/computer",
    "/computer-use",
    "/cu",
    "/image",
    "/img",
    "/keys",
    "/model",
    "/models",
    "/plugin",
    "/plugins",
    "/profile",
    "/project",
    "/projects",
}


@dataclass(frozen=True)
class HeadlessCommandResult:
    lines: list[str] = field(default_factory=list)
    exit_requested: bool = False
    error: bool = False
    await_completion: bool = False


def is_headless_command(text: str) -> bool:
    return str(text or "").lstrip().startswith("/")


def command_may_start_agent(text: str) -> bool:
    stripped = str(text or "").strip()
    token, _, remainder = stripped.partition(" ")
    command = token.lower()
    if command == "/send":
        return True
    if command != "/queue":
        return False
    try:
        tokens = shlex.split(remainder.strip())
    except ValueError:
        return False
    return bool(tokens and tokens[0].lower() in {"send", "all", "flush"})


async def dispatch_headless_command(session: ConnectorSession, text: str) -> HeadlessCommandResult:
    stripped = str(text or "").strip()
    token, _, remainder = stripped.partition(" ")
    command = token.lower()
    argument = remainder.strip()

    if command in {"/quit", "/exit"}:
        return HeadlessCommandResult(["bye"], exit_requested=True)
    if command == "/pause":
        response = await session.pause()
        return _response_result(response, "pause requested")
    if command == "/resume":
        response = await session.resume()
        return _response_result(response, "resume requested")
    if command == "/nudge":
        response = await session.nudge()
        return _response_result(response, "nudge sent")
    if command == "/clear":
        response = await session.reset()
        return _response_result(response, "chat reset")
    if command == "/chats":
        return await _cmd_chats(session)
    if command == "/chat":
        if not argument:
            return HeadlessCommandResult(["usage: /chat <context_id>"], error=True)
        await session.switch_context(argument)
        return HeadlessCommandResult([f"switched to {session.context_id}"])
    if command == "/new":
        context_id = await session.new_context()
        return HeadlessCommandResult([f"created {context_id}"])
    if command == "/send":
        return await _cmd_queue_send(session)
    if command == "/queue":
        return await _cmd_queue(session, argument)
    if command == "/status":
        return HeadlessCommandResult(_status_lines(session))
    if command in {"/help", "/?"}:
        return HeadlessCommandResult(_help_lines())
    if command in _TUI_ONLY_COMMANDS:
        return HeadlessCommandResult(
            [f"{command} is not available in headless mode."],
            error=True,
        )
    return HeadlessCommandResult(
        [f"unknown command: {command}. Type /help for available commands."],
        error=True,
    )


async def _cmd_chats(session: ConnectorSession) -> HeadlessCommandResult:
    contexts = await session.list_chats()
    if not contexts:
        return HeadlessCommandResult(["no chats found"])

    rows = sorted(
        contexts,
        key=lambda context: _context_timestamp(context) or 0.0,
        reverse=True,
    )
    id_width = min(36, max(2, *(len(_context_id(row)) for row in rows)))
    lines = [f"{'id'.ljust(id_width)}  updated              name"]
    for context in rows:
        context_id = _context_id(context)
        marker = "*" if context_id == session.context_id else " "
        updated = _context_updated_label(context)
        name = _context_name(context)
        lines.append(f"{marker}{context_id[:id_width].ljust(id_width)}  {updated.ljust(19)}  {name}")
    return HeadlessCommandResult(lines)


def _response_result(response: dict[str, Any], success_message: str) -> HeadlessCommandResult:
    if response.get("ok", True):
        message = str(response.get("message") or success_message)
        return HeadlessCommandResult([message])
    message = str(response.get("message") or response.get("error") or "command failed")
    return HeadlessCommandResult([message], error=True)


async def _cmd_queue(session: ConnectorSession, argument: str) -> HeadlessCommandResult:
    if "message_queue" not in session.connector_features:
        return HeadlessCommandResult(
            ["Message queue is unavailable on this Agent Zero instance."],
            error=True,
        )
    if not argument:
        return HeadlessCommandResult(_queue_summary_lines(session))

    try:
        tokens = shlex.split(argument)
    except ValueError as exc:
        return HeadlessCommandResult([f"Could not parse queue command: {exc}"], error=True)
    if not tokens:
        return HeadlessCommandResult(_queue_summary_lines(session))

    action = tokens[0].lower()
    if action in {"send", "all", "flush"}:
        return await _cmd_queue_send(session)
    if action in {"clear", "delete"} and len(tokens) == 1:
        return await _cmd_queue_clear(session)
    if action in {"remove", "rm", "delete"}:
        if len(tokens) < 2:
            return HeadlessCommandResult(["usage: /queue remove <number|id>"], error=True)
        return await _cmd_queue_remove(session, tokens[1])
    return HeadlessCommandResult(["usage: /queue [send|clear|remove <number|id>]"], error=True)


async def _cmd_queue_send(session: ConnectorSession) -> HeadlessCommandResult:
    if "message_queue" not in session.connector_features:
        return HeadlessCommandResult(
            ["Message queue is unavailable on this Agent Zero instance."],
            error=True,
        )
    if not session.context_id:
        return HeadlessCommandResult(["Open or create a chat context first."], error=True)
    if not session.message_queue:
        return HeadlessCommandResult(["No queued messages to send."])

    try:
        response = await session.send_message_queue(send_all=True)
    except Exception as exc:
        return HeadlessCommandResult([f"Error sending queued messages: {exc}"], error=True)

    try:
        sent_count = int(response.get("sent_count", 0) or 0) if isinstance(response, Mapping) else 0
    except (TypeError, ValueError):
        sent_count = 0
    if sent_count <= 0:
        return HeadlessCommandResult(["Queue is empty."])
    label = "message" if sent_count == 1 else "messages"
    return HeadlessCommandResult([f"sent {sent_count} queued {label}"], await_completion=True)


async def _cmd_queue_clear(session: ConnectorSession) -> HeadlessCommandResult:
    if "message_queue" not in session.connector_features:
        return HeadlessCommandResult(
            ["Message queue is unavailable on this Agent Zero instance."],
            error=True,
        )
    if not session.context_id:
        return HeadlessCommandResult(["Open or create a chat context first."], error=True)
    try:
        await session.clear_message_queue()
    except Exception as exc:
        return HeadlessCommandResult([f"Error clearing queue: {exc}"], error=True)
    return HeadlessCommandResult(["Queue cleared."])


async def _cmd_queue_remove(session: ConnectorSession, selector: str) -> HeadlessCommandResult:
    if "message_queue" not in session.connector_features:
        return HeadlessCommandResult(
            ["Message queue is unavailable on this Agent Zero instance."],
            error=True,
        )
    if not session.context_id:
        return HeadlessCommandResult(["Open or create a chat context first."], error=True)

    item_id = _queue_selector_to_item_id(session, selector)
    if not item_id:
        return HeadlessCommandResult([f"No queued message matches '{selector}'."], error=True)
    try:
        await session.remove_message_from_queue(item_id)
    except Exception as exc:
        return HeadlessCommandResult([f"Error removing queued message: {exc}"], error=True)
    return HeadlessCommandResult(["Queued message removed."])


def _queue_summary_lines(session: ConnectorSession) -> list[str]:
    if not session.message_queue:
        return ["No queued messages."]

    lines = [f"Queued messages ({len(session.message_queue)}):"]
    for index, item in enumerate(session.message_queue, start=1):
        text = str(item.get("text", "") or "").strip() or "(attachment only)"
        if len(text) > 100:
            text = text[:97].rstrip() + "..."
        try:
            attachment_count = int(item.get("attachment_count", 0) or 0)
        except (TypeError, ValueError):
            attachment_count = 0
        suffix = f" [{attachment_count} files]" if attachment_count else ""
        lines.append(f"{index}. {text}{suffix}")
    return lines


def _queue_selector_to_item_id(session: ConnectorSession, selector: str) -> str:
    value = selector.strip()
    if not value:
        return ""
    if value.isdigit():
        index = int(value) - 1
        if 0 <= index < len(session.message_queue):
            return str(session.message_queue[index].get("id", "") or "")
        return ""
    return value


def _status_lines(session: ConnectorSession) -> list[str]:
    features = ", ".join(sorted(session.connector_features)) or "none"
    queue_label = f"{len(session.message_queue)} queued" if session.message_queue else "empty"
    return [
        f"host: {session.host or '(not connected)'}",
        f"context: {session.context_id or '(none)'}",
        f"workspace: {session.remote_files.scan_root}",
        f"remote files: {'read/write' if session.remote_file_write_enabled else 'read only'}",
        f"remote exec: {'enabled' if session.remote_exec_enabled else 'disabled'}",
        f"message queue: {queue_label}",
        "computer use: unavailable in headless mode",
        "host browser: unavailable in headless mode",
        f"features: {features}",
    ]


def _help_lines() -> list[str]:
    return [
        "commands: /status, /chats, /chat <id>, /new, /pause, /resume, "
        "/nudge, /send, /queue [send|clear|remove <number|id>], /clear, /quit",
    ]


def _context_id(context: Mapping[str, Any]) -> str:
    return str(context.get("id") or context.get("context_id") or context.get("ctxid") or "").strip()


def _context_name(context: Mapping[str, Any]) -> str:
    name = str(context.get("name") or "").strip()
    if name:
        return name
    try:
        number = int(context.get("no", 0) or 0)
    except (TypeError, ValueError):
        number = 0
    if number > 0:
        return f"Chat #{number}"
    return _context_id(context)


def _context_timestamp(context: Mapping[str, Any]) -> float | None:
    for key in ("updated_at", "updated", "created_at"):
        value = context.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        raw = str(value or "").strip()
        if not raw:
            continue
        try:
            return float(raw)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed.timestamp()
    return None


def _context_updated_label(context: Mapping[str, Any]) -> str:
    timestamp = _context_timestamp(context)
    if timestamp is None:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
