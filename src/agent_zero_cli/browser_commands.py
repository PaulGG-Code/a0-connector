from __future__ import annotations

from typing import TYPE_CHECKING

from agent_zero_cli.commands import CommandAvailability
from agent_zero_cli.host_browser import ProfileLockedError, format_profile_rows

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


def browser_availability(app: "AgentZeroCLI") -> CommandAvailability:
    del app
    return CommandAvailability(True)


async def cmd_browser(app: "AgentZeroCLI", query: str = "") -> None:
    tokens = [token.strip() for token in str(query or "").split() if token.strip()]
    if not tokens or tokens[0].lower() in {"status", "state"}:
        app._show_notice(app._host_browser.status_text())
        return

    command = tokens[0].lower()
    if command == "host":
        await _cmd_browser_host(app, tokens[1:])
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
        app._show_notice(
            "Host-browser privacy is controlled per project in Agent Zero Browser settings."
        )
        return

    app._show_notice(
        "Usage: /browser status | host on|off | profile [family] [profile] | relaunch | repair | privacy",
        error=True,
    )


async def _cmd_browser_host(app: "AgentZeroCLI", args: list[str]) -> None:
    if not args or args[0].lower() not in {"on", "off", "enable", "disable"}:
        app._show_notice("Usage: /browser host on|off", error=True)
        return

    enabled = args[0].lower() in {"on", "enable"}
    app._host_browser.set_enabled(enabled)
    if enabled and not await _ensure_playwright_dependency(app):
        await _refresh_browser_metadata_notice(app, "Host browser is enabled but still unsupported.")
        return
    synced = await app._refresh_remote_tool_metadata()
    state = "enabled" if enabled else "disabled"
    if synced:
        app._show_notice(f"Host browser {state}. {app._host_browser.status_text()}")
    else:
        app._show_notice(
            "Host browser changed locally, but Agent Zero did not acknowledge "
            f"the update: {app._remote_tool_metadata_error}",
            error=True,
        )


async def _cmd_browser_profile(app: "AgentZeroCLI", args: list[str]) -> None:
    profiles = app._host_browser.available_profiles()
    if not args:
        rows = format_profile_rows(profiles)
        if not rows:
            app._show_notice("No installed Chrome-family profiles were detected.", error=True)
            return
        selected = app._host_browser.selected_profile()
        selected_text = ""
        if selected is not None:
            selected_text = f"\nSelected: {selected.family} {selected.profile_label} ({selected.profile_path})"
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
    message = f"Selected {profile.family} profile {profile.profile_label} ({profile.profile_path})."
    if synced:
        app._show_notice(message)
    else:
        app._show_notice(
            f"{message} Agent Zero did not acknowledge the update: {app._remote_tool_metadata_error}",
            error=True,
        )


async def _cmd_browser_relaunch(app: "AgentZeroCLI") -> None:
    if not await _ensure_playwright_dependency(app):
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
            "A0 will use your installed Chrome-family browser; no bundled browser was installed."
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
