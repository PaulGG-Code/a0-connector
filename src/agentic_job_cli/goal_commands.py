from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from agentic_job_cli.app import AgentZeroCLI


_AUTO_PROMPT = (
    "Please create and manage a goal for this chat. Use the goal tools to inspect "
    "any current goal, create a concise goal objective, and update it when the work "
    "is complete or genuinely blocked."
)


async def cmd_goal(app: AgentZeroCLI, *, query: str = "") -> None:
    if not app.current_context:
        app._show_notice("Open or create a chat context first.", error=True)
        return

    raw = query.strip()
    action, remainder = _split_action(raw)

    if action in {"", "status", "show"}:
        await app._refresh_goal_bar(silent=False)
        app._show_notice(_goal_summary(app.goal))
        return

    if action in {"pause", "paused"}:
        await _update_goal(app, "pause", "Goal paused.")
        return
    if action in {"resume", "start", "active"}:
        await _update_goal(app, "resume", "Goal resumed.")
        return
    if action in {"delete", "clear", "remove"}:
        await _update_goal(app, "delete", "Goal deleted.")
        return
    if action in {"complete", "done"}:
        await _update_goal(app, "update", "Goal marked complete.", status="complete")
        return
    if action == "blocked":
        await _update_goal(app, "update", "Goal marked blocked.", status="blocked", note=remainder)
        return
    if action in {"update", "edit"}:
        if not remainder:
            app._show_notice("Usage: /goal update <goal>", error=True)
            return
        response = await _update_goal(
            app,
            "update",
            "Goal updated.",
            objective=remainder,
            status="active",
        )
        if response and response.get("reactivated") is True:
            await app._send_chat_text(remainder, raw_text=remainder, attachments=[])
        return
    if action in {"auto", "ask", "model"}:
        prompt = _AUTO_PROMPT
        if remainder:
            prompt = f"{prompt}\n\nUser hint: {remainder}"
        await app._send_chat_text(prompt, raw_text=prompt, attachments=[])
        return

    if await _update_goal(app, "create", "Goal set.", objective=raw, created_by="user"):
        await app._send_chat_text(raw, raw_text=raw, attachments=[])


async def _update_goal(
    app: AgentZeroCLI,
    action: str,
    success_message: str,
    **payload: Any,
) -> dict[str, Any] | None:
    try:
        response = await app.client.goal_action(action, app.current_context or "", **payload)
    except Exception as exc:
        app._show_notice(f"Goal command failed: {exc}", error=True)
        return None

    if not response.get("ok"):
        message = str(response.get("message") or response.get("error") or "Goal command failed.")
        app._show_notice(message, error=True)
        return None

    goal = response.get("goal") if isinstance(response, Mapping) else None
    app._set_goal(goal if isinstance(goal, Mapping) else None)
    app._show_notice(success_message)
    return response


def _split_action(raw: str) -> tuple[str, str]:
    if not raw:
        return "", ""
    try:
        tokens = shlex.split(raw)
    except ValueError:
        token, _, remainder = raw.partition(" ")
        return token.lower(), remainder.strip()
    if not tokens:
        return "", ""
    token = tokens[0].lower()
    _, _, remainder = raw.partition(" ")
    return token, remainder.strip()


def _goal_summary(goal: Mapping[str, Any] | None) -> str:
    if not goal:
        return "No goal is set for this chat."
    objective = str(goal.get("objective") or "").strip()
    status = str(goal.get("status") or "active").strip() or "active"
    if not objective:
        return "No goal is set for this chat."
    return f"{status}: {objective}"
