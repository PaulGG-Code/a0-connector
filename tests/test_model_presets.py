from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from agent_zero_cli.screens.model_presets import ModelPresetsResult, ModelPresetsScreen


pytestmark = pytest.mark.anyio


class ModelPresetsHarness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.result: ModelPresetsResult | None | str = "pending"

    def compose(self) -> ComposeResult:
        yield Static("base")

    async def on_mount(self) -> None:
        self.push_screen(
            ModelPresetsScreen(
                [
                    {"name": "fast", "label": "Fast"},
                    {"name": "deep", "label": "Deep"},
                ],
                current_preset="fast",
            ),
            self._capture,
        )

    def _capture(self, result: ModelPresetsResult | None) -> None:
        self.result = result


async def test_model_presets_keyboard_enter_applies_highlighted_dropdown_choice() -> None:
    app = ModelPresetsHarness()

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        await pilot.press("space")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause(0.2)

    assert isinstance(app.result, ModelPresetsResult)
    assert app.result.preset_name == "deep"
