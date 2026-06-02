from __future__ import annotations

from typing import Any, Mapping, Sequence

import pytest
from textual.app import App, ComposeResult
from textual.css.query import NoMatches
from textual.widgets import Static

from agent_zero_cli.screens.installed_plugins import (
    InstalledPluginEntry,
    InstalledPluginsScreen,
    coerce_installed_plugins,
)


pytestmark = pytest.mark.anyio


class InstalledPluginsHarness(App[None]):
    def __init__(self, plugins: Sequence[Mapping[str, Any]]) -> None:
        super().__init__()
        self.plugins = [dict(plugin) for plugin in plugins]
        self.toggles: list[tuple[str, bool]] = []
        self.screen_ref: InstalledPluginsScreen | None = None

    def compose(self) -> ComposeResult:
        yield Static("base")

    async def on_mount(self) -> None:
        self.screen_ref = InstalledPluginsScreen(
            self.plugins,
            toggle_callback=self._toggle,
        )
        self.push_screen(self.screen_ref)

    async def _toggle(
        self,
        entry: InstalledPluginEntry,
        enabled: bool,
    ) -> Sequence[Mapping[str, Any]]:
        self.toggles.append((entry.name, enabled))
        updated: list[dict[str, Any]] = []
        for plugin in self.plugins:
            copy = dict(plugin)
            if copy.get("name") == entry.name:
                copy["enabled"] = enabled
                copy["toggle_state"] = "enabled" if enabled else "disabled"
            updated.append(copy)
        self.plugins = updated
        return updated


def test_installed_plugins_coercion_sorts_enabled_first() -> None:
    entries = coerce_installed_plugins(
        [
            {"name": "_linear", "display_name": "Linear", "enabled": False},
            {"name": "_documents", "display_name": "Documents", "enabled": True},
        ]
    )

    assert [entry.name for entry in entries] == ["_documents", "_linear"]
    assert entries[0].source == "Builtin"


async def test_installed_plugins_screen_space_toggles_selected_plugin() -> None:
    app = InstalledPluginsHarness(
        [
            {
                "name": "_documents",
                "display_name": "Documents",
                "enabled": True,
                "toggleable": True,
            }
        ]
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        await pilot.press("space")
        await pilot.pause(0.3)

    assert app.toggles == [("_documents", False)]


async def test_installed_plugins_screen_typing_from_list_starts_search() -> None:
    app = InstalledPluginsHarness(
        [
            {
                "name": "_documents",
                "display_name": "Documents",
                "enabled": True,
                "toggleable": True,
            },
            {
                "name": "_linear",
                "display_name": "Linear",
                "enabled": True,
                "toggleable": True,
            },
        ]
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        await pilot.press("l")
        await pilot.pause(0.2)
        assert app.screen_ref is not None
        search = app.screen_ref.query_one("#installed-plugins-search")
        assert search.value == "l"


async def test_installed_plugins_screen_strips_control_chars_from_search() -> None:
    app = InstalledPluginsHarness(
        [
            {
                "name": "_linear",
                "display_name": "Linear",
                "enabled": True,
                "toggleable": True,
            }
        ]
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        assert app.screen_ref is not None
        search = app.screen_ref.query_one("#installed-plugins-search")
        search.value = "\x06linear"
        await pilot.pause(0.2)
        assert search.value == "linear"


async def test_installed_plugins_screen_omits_unavailable_marketplace_copy() -> None:
    app = InstalledPluginsHarness(
        [
            {
                "name": "_linear",
                "display_name": "Linear",
                "enabled": True,
                "toggleable": True,
            }
        ]
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        assert app.screen_ref is not None
        with pytest.raises(NoMatches):
            app.screen_ref.query_one("#installed-plugins-tabs")
        help_text = app.screen_ref.query_one("#installed-plugins-help", Static)
        assert "ctrl+f" not in str(help_text.content).casefold()


async def test_installed_plugins_screen_refuses_protected_toggle() -> None:
    app = InstalledPluginsHarness(
        [
            {
                "name": "_a0_connector",
                "display_name": "A0 Connector",
                "enabled": True,
                "toggleable": False,
                "protected_reason": "The A0 Connector plugin keeps this CLI session connected.",
            }
        ]
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause(0.2)
        await pilot.press("space")
        await pilot.pause(0.2)
        assert app.screen_ref is not None
        status = app.screen_ref.query_one("#installed-plugins-status", Static)
        assert "cannot be toggled" in str(status.content)

    assert app.toggles == []
