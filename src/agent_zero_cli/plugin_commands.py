from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Sequence

from agent_zero_cli.screens.installed_plugins import InstalledPluginEntry, InstalledPluginsScreen

if TYPE_CHECKING:
    from agent_zero_cli.app import AgentZeroCLI


async def cmd_plugins(app: AgentZeroCLI) -> None:
    try:
        plugins = await app.client.list_installed_plugins()
    except Exception as exc:
        app._show_notice(f"Error listing installed plugins: {exc}", error=True)
        return

    async def _toggle(
        entry: InstalledPluginEntry,
        enabled: bool,
    ) -> Sequence[Mapping[str, Any]]:
        result = await app.client.set_installed_plugin_enabled(entry.name, enabled)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("message") or "Plugin toggle failed."))

        state = "enabled" if enabled else "disabled"
        app._show_notice(f"{entry.title} {state}. Agent Zero may reload plugin state.")
        refreshed = await app.client.list_installed_plugins()
        return refreshed

    await app.push_screen_wait(
        InstalledPluginsScreen(
            plugins,
            toggle_callback=_toggle,
        )
    )
