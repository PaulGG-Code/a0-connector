from __future__ import annotations

from typing import TYPE_CHECKING

from agent_zero_cli.commands import CommandAvailability
from agent_zero_cli.host_browser import ProfileLockedError, format_profile_rows

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


_CONTENT_POLICY_NOTICE = (
    "For Browser model-use settings, visit Agent Zero WebUI > Browser settings to choose "
    "Local models only, Warn when using cloud, or Allow."
)


def browser_availability(app: "AgentZeroCLI") -> CommandAvailability:
    del app
    return CommandAvailability(True)


async def cmd_browser(app: "AgentZeroCLI", query: str = "") -> None:
    tokens = [token.strip() for token in str(query or "").split() if token.strip()]
    if not tokens or tokens[0].lower() in {"status", "state"}:
        await _cmd_browser_status(app)
        return

    command = tokens[0].lower()
    if command == "host":
        if len(tokens) == 1:
            await _cmd_browser_runtime(app, "host_required")
            return
        await _cmd_browser_host(app, tokens[1:])
        return
    if command in {"container", "docker", "docker_container"}:
        await _cmd_browser_runtime(app, "container")
        return
    if command == "profile":
        await _cmd_browser_profile(app, tokens[1:])
        return
    if command == "relaunch":
        await _cmd_browser_relaunch(app)
        return
    if command == "repair":
        if await _ensure_playwright_dependency(app):
            await _refresh_browser_metadata_notice(app, "Host browser repair completed.")
        return
    if command == "privacy":
        app._show_notice(_CONTENT_POLICY_NOTICE)
        return

    app._show_notice(
        "Usage: /browser host | container | status | host on|off | "
        "profile [family] [profile] | relaunch | repair | privacy",
        error=True,
    )


async def _cmd_browser_status(app: "AgentZeroCLI") -> None:
    lines = [app._host_browser.status_text()]
    if _browser_runtime_config_available(app) and app.current_context:
        try:
            payload = await app.client.get_browser_runtime(app.current_context)
        except Exception as exc:
            lines.append(f"Browser mode unavailable: {exc}")
        else:
            if payload.get("ok"):
                label = _browser_mode_label(payload.get("runtime_backend"))
                lines.insert(0, f"Browser mode: {label}.")
    lines.append("Use /browser host or /browser container to choose where Browser runs.")
    app._show_notice("\n".join(lines))


async def _cmd_browser_runtime(app: "AgentZeroCLI", runtime_backend: str) -> None:
    if not _browser_runtime_config_available(app):
        app._show_notice(
            "This Agent Zero server does not support CLI Browser mode changes.",
            error=True,
        )
        return
    if not app.current_context:
        app._show_notice("Open or create a chat context before changing Browser mode.", error=True)
        return
    if app.agent_active:
        app._show_notice("Wait for the current run to finish before changing Browser mode.", error=True)
        return

    if runtime_backend == "host_required":
        app._host_browser.set_enabled(True)
        if _selected_profile_needs_playwright(app) and not await _ensure_playwright_dependency(app):
            await _refresh_browser_metadata_notice(app, "Browser host mode is selected but unsupported.")
            return
        if not await app._refresh_remote_tool_metadata():
            app._show_notice(
                "Host browser changed locally, but Agent Zero did not acknowledge "
                f"the update: {app._remote_tool_metadata_error}",
                error=True,
            )
            return

    try:
        payload = await app.client.set_browser_runtime(app.current_context, runtime_backend)
    except Exception as exc:
        app._show_notice(f"Could not update Browser mode: {exc}", error=True)
        return

    if not payload.get("ok"):
        app._show_notice(str(payload.get("error") or "Could not update Browser mode."), error=True)
        return

    label = _browser_mode_label(payload.get("runtime_backend"))
    scope = _browser_scope_label(payload)
    app._show_notice(f"Browser set to {label}{scope}. {_CONTENT_POLICY_NOTICE}")


async def _cmd_browser_host(app: "AgentZeroCLI", args: list[str]) -> None:
    if not args or args[0].lower() not in {"on", "off", "enable", "disable"}:
        app._show_notice("Usage: /browser host on|off", error=True)
        return

    enabled = args[0].lower() in {"on", "enable"}
    app._host_browser.set_enabled(enabled)
    if enabled and _selected_profile_needs_playwright(app) and not await _ensure_playwright_dependency(app):
        await _refresh_browser_metadata_notice(app, "Host browser is enabled but still unsupported.")
        return
    synced = await app._refresh_remote_tool_metadata()
    state = "enabled" if enabled else "disabled"
    if not synced:
        app._show_notice(
            "Host browser changed locally, but Agent Zero did not acknowledge "
            f"the update: {app._remote_tool_metadata_error}",
            error=True,
        )
        return

    runtime_payload, runtime_message, runtime_error = await _sync_browser_runtime_for_host_toggle(
        app,
        enabled=enabled,
    )
    if runtime_error:
        app._show_notice(
            f"Host browser {state}, but {runtime_message}",
            error=True,
        )
        return

    message = f"Host browser {state}. {app._host_browser.status_text()}"
    if runtime_payload:
        label = _browser_mode_label(runtime_payload.get("runtime_backend"))
        scope = _browser_scope_label(runtime_payload)
        message += f" Browser set to {label}{scope}."
        if enabled:
            message += f" {_CONTENT_POLICY_NOTICE}"
    elif runtime_message:
        message += f" {runtime_message}"
    app._show_notice(message)


async def _sync_browser_runtime_for_host_toggle(
    app: "AgentZeroCLI",
    *,
    enabled: bool,
) -> tuple[dict | None, str, bool]:
    if not _browser_runtime_config_available(app):
        return None, "", False
    if not app.current_context:
        return (
            None,
            "Open or create a chat context before syncing Browser mode in Agent Zero.",
            False,
        )
    if app.agent_active:
        return (
            None,
            "Wait for the current run to finish before syncing Browser mode in Agent Zero.",
            False,
        )

    runtime_backend = "host_required" if enabled else "container"
    try:
        payload = await app.client.set_browser_runtime(app.current_context, runtime_backend)
    except Exception as exc:
        return None, f"could not update Browser mode: {exc}", True

    if not payload.get("ok"):
        return (
            None,
            str(payload.get("error") or "could not update Browser mode."),
            True,
        )
    return payload, "", False


async def _cmd_browser_profile(app: "AgentZeroCLI", args: list[str]) -> None:
    profiles = app._host_browser.available_profiles()
    if not args:
        rows = format_profile_rows(profiles)
        if not rows:
            app._show_notice("No installed Chromium-family profiles were detected.", error=True)
            return
        selected = app._host_browser.selected_profile()
        selected_text = ""
        if selected is not None:
            selected_text = f"\nSelected: {selected.family} {selected.profile_label} ({selected.profile_path_display})"
        app._show_notice(
            "Detected host browser profiles:\n"
            + "\n".join(rows[:12])
            + selected_text
            + "\nUse /browser profile <family> <profile> to select one."
        )
        return

    family = args[0]
    profile_label = " ".join(args[1:]) if len(args) > 1 else ""
    try:
        profile = app._host_browser.select_profile(family, profile_label=profile_label)
    except Exception as exc:
        app._show_notice(f"Could not select host browser profile: {exc}", error=True)
        return

    synced = await app._refresh_remote_tool_metadata()
    message = f"Selected {profile.family} profile {profile.profile_label} ({profile.profile_path_display})."
    if synced:
        app._show_notice(message)
    else:
        app._show_notice(
            f"{message} Agent Zero did not acknowledge the update: {app._remote_tool_metadata_error}",
            error=True,
        )


async def _cmd_browser_relaunch(app: "AgentZeroCLI") -> None:
    if _selected_profile_needs_playwright(app) and not await _ensure_playwright_dependency(app):
        await _refresh_browser_metadata_notice(app, "Host browser relaunch is blocked.")
        return
    try:
        status = await app._host_browser.relaunch()
    except ProfileLockedError as exc:
        app._show_notice(str(exc), error=True)
        return
    except Exception as exc:
        app._show_notice(f"Host browser relaunch failed: {exc}", error=True)
        return

    synced = await app._refresh_remote_tool_metadata()
    if synced:
        app._show_notice(
            "Host browser is ready: "
            f"{status.get('browser_family')} profile {status.get('profile_label')}."
        )
    else:
        app._show_notice(
            "Host browser relaunched locally, but Agent Zero did not acknowledge "
            f"the update: {app._remote_tool_metadata_error}",
            error=True,
    )


def _selected_profile_needs_playwright(app: "AgentZeroCLI") -> bool:
    profile = app._host_browser.selected_profile()
    return profile is None or not profile.is_remote_debugging


async def _ensure_playwright_dependency(app: "AgentZeroCLI") -> bool:
    if app._host_browser.has_playwright_dependency():
        return True

    command = " ".join(app._host_browser.playwright_install_command())
    app._show_notice(
        "Host browser needs Python Playwright in the A0 CLI environment. "
        f"Installing now: {command}"
    )
    try:
        result = await app._host_browser.ensure_playwright_dependency()
    except Exception as exc:
        app._show_notice(f"Host browser repair failed: {exc}", error=True)
        return False

    if result.get("installed"):
        app._show_notice(
            "Python Playwright installed for host browser control. "
            "A0 will use your installed Chromium-family browser; no bundled browser was installed."
        )
    else:
        app._show_notice("Python Playwright is already installed for host browser control.")
    return True


async def _refresh_browser_metadata_notice(app: "AgentZeroCLI", message: str) -> None:
    synced = await app._refresh_remote_tool_metadata()
    if synced:
        app._show_notice(f"{message} {app._host_browser.status_text()}")
    else:
        app._show_notice(
            f"{message} Agent Zero did not acknowledge the update: {app._remote_tool_metadata_error}",
            error=True,
        )


def _browser_runtime_config_available(app: "AgentZeroCLI") -> bool:
    return "browser_runtime_config" in getattr(app, "connector_features", set())


def _browser_mode_label(value: object) -> str:
    return (
        "Bring Your Own Browser"
        if str(value or "") == "host_required"
        else "Docker browser"
    )


def _browser_scope_label(payload: dict) -> str:
    project_name = str(payload.get("project_name") or "").strip()
    return f" for project {project_name}" if project_name else ""
