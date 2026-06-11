from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
    "/queue",
    "/send",
}


@dataclass(frozen=True)
class HeadlessCommandResult:
    lines: list[str] = field(default_factory=list)
    exit_requested: bool = False
    error: bool = False


def is_headless_command(text: str) -> bool:
    return str(text or "").lstrip().startswith("/")


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


def _status_lines(session: ConnectorSession) -> list[str]:
    features = ", ".join(sorted(session.connector_features)) or "none"
    return [
        f"host: {session.host or '(not connected)'}",
        f"context: {session.context_id or '(none)'}",
        f"workspace: {session.remote_files.scan_root}",
        f"remote files: {'read/write' if session.remote_file_write_enabled else 'read only'}",
        f"remote exec: {'enabled' if session.remote_exec_enabled else 'disabled'}",
        "computer use: unavailable in headless mode",
        "host browser: unavailable in headless mode",
        f"features: {features}",
    ]


def _help_lines() -> list[str]:
    return [
        "commands: /status, /chats, /chat <id>, /new, /pause, /resume, /nudge, /clear, /quit",
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
