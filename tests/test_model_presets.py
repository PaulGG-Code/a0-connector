from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from agent_zero_cli.model_config import apply_model_switcher_state
from agent_zero_cli.screens.model_presets import (
    ModelPresetsResult,
    ModelPresetsScreen,
    _coerce_model_preset,
    _render_preset_details,
)
from agent_zero_cli.widgets.model_switcher_bar import _preset_options


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


def test_unified_preset_payload_uses_effective_name_and_clear_copy() -> None:
    _, state = apply_model_switcher_state(
        {
            "allowed": True,
            "override": {"preset_name": "Power"},
            "configured_preset": "Default",
            "effective_preset": "Power",
            "presets": [{"name": "Default"}, {"name": "Power"}],
            "main_model": {"provider": "openrouter", "name": "openai/gpt-5.6-sol"},
        }
    )

    assert state["selected_preset"] == "Power"
    assert state["configured_preset"] == "Default"
    assert _preset_options(
        state["presets"],
        configured_preset="Default",
        include_settings=True,
    )[0] == ("Use preset from settings (Default)", "")


def test_model_preset_details_include_embedding_model() -> None:
    preset = _coerce_model_preset(
        {
            "name": "Power",
            "chat": {"provider": "openrouter", "name": "openai/gpt-5.6-sol"},
            "utility": {"provider": "openrouter", "name": "openai/gpt-5.6-luna"},
            "embedding": {"provider": "openai", "name": "text-embedding-3-large"},
        }
    )

    assert "Embedding model: openai/text-embedding-3-large" in _render_preset_details(preset).plain
