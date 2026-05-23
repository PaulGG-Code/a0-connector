from __future__ import annotations

import base64
import os
import sys
import types
from pathlib import Path

import numpy as np
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_PACKAGE_SRC = PROJECT_ROOT / "packages" / "a0-computer-use-windows" / "src"
if str(WINDOWS_PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(WINDOWS_PACKAGE_SRC))

from a0_computer_use_windows.backend import WINDOWS_BACKEND_SPEC, WindowsComputerUseBackend
import a0_computer_use_windows.runtime as windows_runtime_mod
from a0_computer_use_windows.runtime import (
    ScreenGeometry,
    WindowsComputerUseError,
    WindowsComputerUseRuntime,
    WindowsSessionStore,
)
from a0_computer_use_windows.shared import normalize_action_payload


class _FakeRect:
    def __init__(self, x: float, y: float, width: float, height: float) -> None:
        self.left = x
        self.top = y
        self.right = x + width
        self.bottom = y + height


class _FakeUIAElement:
    def __init__(
        self,
        *,
        role: str,
        title: str = "",
        automation_id: str = "",
        class_name: str = "",
        rect: _FakeRect | None = None,
        children: list["_FakeUIAElement"] | None = None,
        enabled: bool = True,
        visible: bool = True,
        editable: bool = False,
        handle: int | None = None,
        invokable: bool | None = None,
        clickable: bool = True,
        focusable: bool = True,
    ) -> None:
        self.element_info = self
        self.control_type = role
        self.name = title
        self.automation_id = automation_id
        self.class_name = class_name
        self.rectangle = rect
        self.handle = handle
        self.enabled = enabled
        self.visible = visible
        self._children = children or []
        self._parent: _FakeUIAElement | None = None
        for child in self._children:
            child._parent = self
        self.invoked = False
        self.clicked = False
        self.focused = False
        self.window_actions: list[str] = []
        self.value = ""
        if invokable is None:
            invokable = role.lower() in {"button", "menuitem", "menu item", "checkbox", "radio button", "hyperlink"}
        if not invokable:
            self.invoke = None  # type: ignore[method-assign]
        if not clickable:
            self.click_input = None  # type: ignore[method-assign]
        if not focusable:
            self.set_focus = None  # type: ignore[method-assign]
        if editable:
            self.set_edit_text = self._set_edit_text  # type: ignore[method-assign]

    def children(self) -> list["_FakeUIAElement"]:
        return list(self._children)

    def parent(self) -> "_FakeUIAElement | None":
        return self._parent

    def top_level_parent(self) -> "_FakeUIAElement":
        element = self
        while element._parent is not None:
            element = element._parent
        return element

    def invoke(self) -> None:
        self.invoked = True

    def click_input(self) -> None:
        self.clicked = True

    def set_focus(self) -> None:
        self.focused = True

    def has_keyboard_focus(self) -> bool:
        return self.focused

    def _set_edit_text(self, value: str) -> None:
        self.value = value

    def restore(self) -> None:
        self.window_actions.append("restore")

    def show(self) -> None:
        self.window_actions.append("show")

    def minimize(self) -> None:
        self.window_actions.append("minimize")

    def maximize(self) -> None:
        self.window_actions.append("maximize")

    def close(self) -> None:
        self.window_actions.append("close")


class _FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self._origin_x = 0
        self._origin_y = 0
        self._width = 1280
        self._height = 720
        self.uia_root_elements: list[_FakeUIAElement] = []

    def screen_geometry(self) -> ScreenGeometry:
        self.calls.append(("screen_geometry", tuple(), {}))
        return ScreenGeometry(
            origin_x=self._origin_x,
            origin_y=self._origin_y,
            width=self._width,
            height=self._height,
        )

    def screen_size(self) -> tuple[int, int]:
        self.calls.append(("screen_size", tuple(), {}))
        return self._width, self._height

    def capture_png(self) -> tuple[bytes, int, int]:
        self.calls.append(("capture_png", tuple(), {}))
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/5wAAAABJRU5ErkJggg=="
        )
        return png_bytes, 1, 1

    def move(self, x: float, y: float) -> None:
        self.calls.append(("move", (x, y), {}))

    def click(self, x: float, y: float, *, button: str, count: int) -> None:
        self.calls.append(("click", (x, y), {"button": button, "count": count}))

    def scroll(self, dx: int, dy: int) -> None:
        self.calls.append(("scroll", (dx, dy), {}))

    def key(self, keys: list[str]) -> None:
        self.calls.append(("key", (tuple(keys),), {}))

    def type_text(self, text: str, *, submit: bool) -> None:
        self.calls.append(("type_text", (text,), {"submit": submit}))

    def uia_roots(self) -> list[_FakeUIAElement]:
        self.calls.append(("uia_roots", tuple(), {}))
        return list(self.uia_root_elements)


def test_windows_backend_spec_exports_expected_metadata() -> None:
    spec = WINDOWS_BACKEND_SPEC

    assert spec.backend_id == "windows"
    assert spec.backend_family == "windows"
    assert spec.interpreter_strategy == "current_python"
    assert Path(spec.helper_target).name == "runtime.py"
    assert spec.supports_trust_mode("interactive") is True
    assert spec.supports_trust_mode("persistent") is True
    assert spec.supports_trust_mode("allow") is True
    assert "inline-png-capture" in spec.features
    assert "uia-automation" in spec.features
    assert "uia-tree-snapshot" in spec.features
    assert "uia-structural-targeting" in spec.features
    assert "uia-element-action" in spec.features
    assert "uia-window-management" in spec.features
    assert "global-pixel-actions" in spec.features
    assert "virtual-screen-coordinates" in spec.features
    assert "multi-monitor-virtual-screen" in spec.features
    assert "real-cursor-may-move" in spec.features


def test_windows_backend_wrapper_uses_current_python() -> None:
    backend = WindowsComputerUseBackend()

    assert backend.spec is WINDOWS_BACKEND_SPEC
    assert backend.helper_command()[0] == sys.executable
    assert backend.helper_command()[-1] == "--stdio"


def test_windows_action_normalization_matches_shared_surface() -> None:
    move = normalize_action_payload("move", {"x": 0.25, "y": 0.75}, context_id="ctx-1")
    click = normalize_action_payload(
        "click",
        {"x": 0.4, "y": 0.6, "button": "right", "count": 2},
        context_id="ctx-1",
    )
    scroll = normalize_action_payload("scroll", {"dx": 1, "dy": -2}, context_id="ctx-1")
    keys = normalize_action_payload("key", {"key": "ctrl+alt+t"}, context_id="ctx-1")
    typed = normalize_action_payload("type", {"text": "hello", "submit": True}, context_id="ctx-1")
    uia_snapshot = normalize_action_payload(
        "uia_snapshot",
        {"max_depth": 3, "max_nodes": 50},
        context_id="ctx-1",
    )
    uia_action = normalize_action_payload(
        "uia_action",
        {
            "target": {"role": "Button", "title": "Save"},
            "selector": "role:Button && name:Save",
            "operation": "invoke",
        },
        context_id="ctx-1",
    )

    assert move["x"] == 0.25 and move["y"] == 0.75
    assert click["button"] == "right" and click["count"] == 2
    assert scroll["dx"] == 1 and scroll["dy"] == -2
    assert keys["keys"] == ["ctrl", "alt", "t"]
    assert typed["text"] == "hello" and typed["submit"] is True
    assert uia_snapshot["max_depth"] == 3 and uia_snapshot["max_nodes"] == 50
    assert uia_action["target"]["title"] == "Save"
    assert uia_action["target"]["selector"] == "role:Button && name:Save"
    assert uia_action["operation"] == "invoke"


def test_windows_runtime_rejects_allow_without_restore_token(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")

    with pytest.raises(WindowsComputerUseError) as exc_info:
        runtime.start_session({"context_id": "ctx-1", "trust_mode": "allow"})

    assert exc_info.value.code == "COMPUTER_USE_REARM_REQUIRED"


def test_windows_runtime_session_policies_are_persisted_when_valid(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")
    restore_token = "123e4567-e89b-12d3-a456-426614174000"

    first = runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": restore_token,
        }
    )
    runtime.stop_session({"context_id": "ctx-1"})
    second = runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": restore_token,
        }
    )

    assert first["session_id"] == second["session_id"]
    assert second["reused"] is True
    stored = WindowsSessionStore(state_dir=tmp_path / "state").get("ctx-1")
    assert stored is not None
    assert stored.restore_token == restore_token


def test_windows_runtime_interactive_sessions_are_fresh_each_time(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")

    first = runtime.start_session({"context_id": "ctx-1", "trust_mode": "interactive"})
    runtime.stop_session({"context_id": "ctx-1"})
    second = runtime.start_session({"context_id": "ctx-1", "trust_mode": "interactive"})

    assert first["session_id"] != second["session_id"]
    assert "restore_token" not in first
    assert "restore_token" not in second


def test_windows_runtime_capture_returns_inline_png_payload(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    capture = runtime.capture({"context_id": "ctx-1"})

    assert capture["width"] == 1
    assert capture["height"] == 1
    assert capture["origin_x"] == 0
    assert capture["origin_y"] == 0
    assert capture["png_base64"]
    assert base64.b64decode(capture["png_base64"])


def test_windows_runtime_capture_writes_requested_path_without_inline_payload(tmp_path: Path) -> None:
    runtime = WindowsComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )
    capture_path = tmp_path / "captures" / "capture.png"

    capture = runtime.capture({"context_id": "ctx-1", "capture_path": str(capture_path)})

    assert capture["width"] == 1
    assert capture["height"] == 1
    assert capture["capture_path"] == str(capture_path)
    assert "png_base64" not in capture
    assert capture_path.exists()


def test_windows_desktop_automation_prefers_dxcam_numpy_processor(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeCamera:
        def grab(self):
            return np.zeros((1, 1, 4), dtype=np.uint8)

    def create(**kwargs):
        calls.append(dict(kwargs))
        return FakeCamera()

    automation = windows_runtime_mod._WindowsDesktopAutomation.__new__(windows_runtime_mod._WindowsDesktopAutomation)
    automation._camera = None
    automation.screen_geometry = lambda: ScreenGeometry(origin_x=0, origin_y=0, width=1, height=1)
    automation._capture_all_screens_png = lambda _geometry: None
    monkeypatch.setattr(windows_runtime_mod, "_load_dxcam_module", lambda: types.SimpleNamespace(create=create))

    png_bytes, width, height, origin_x, origin_y = automation.capture_png()

    assert png_bytes
    assert (width, height) == (1, 1)
    assert (origin_x, origin_y) == (0, 0)
    assert calls == [{"output_idx": 0, "processor_backend": "numpy"}]


def test_windows_runtime_uia_snapshot_returns_bounded_structural_tree(tmp_path: Path) -> None:
    driver = _FakeDriver()
    driver._origin_x = -100
    driver._origin_y = -50
    driver._width = 1400
    driver._height = 900
    save_button = _FakeUIAElement(
        role="Button",
        title="Save",
        automation_id="save-button",
        rect=_FakeRect(100, 200, 80, 30),
    )
    text_field = _FakeUIAElement(
        role="Edit",
        title="File name",
        automation_id="file-name",
        rect=_FakeRect(200, 300, 240, 24),
        editable=True,
    )
    window = _FakeUIAElement(
        role="Window",
        title="Document",
        class_name="FakeWindow",
        rect=_FakeRect(0, 100, 800, 600),
        children=[save_button, text_field],
    )
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    snapshot = runtime.uia_snapshot({"context_id": "ctx-1", "max_depth": 3, "max_nodes": 10})

    assert snapshot["app"]["name"] == "Windows desktop"
    assert snapshot["node_count"] == 4
    assert snapshot["truncated"] is False
    tree = snapshot["tree"]
    assert tree["role"] == "Desktop"
    window_node = tree["children"][0]
    assert window_node["path"] == [0]
    assert window_node["role"] == "Window"
    assert window_node["title"] == "Document"
    assert window_node["actions"] == ["focus_window", "minimize", "restore", "maximize"]
    button_node = window_node["children"][0]
    assert button_node["path"] == [0, 0]
    assert button_node["role"] == "Button"
    assert button_node["automation_id"] == "save-button"
    assert button_node["actions"] == ["invoke", "focus"]
    assert button_node["selector"] == "role:Button && id:save-button && name:Save"
    assert button_node["frame"]["normalized"]["x"] == round((100 - (-100)) / 1400, 6)


def test_windows_runtime_uia_action_invokes_semantic_target(tmp_path: Path) -> None:
    driver = _FakeDriver()
    button = _FakeUIAElement(role="Button", title="Save", automation_id="save-button")
    window = _FakeUIAElement(role="Window", title="Document", children=[button])
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    result = runtime.uia_action(
        {
            "context_id": "ctx-1",
            "target": {"role": "Button", "title": "Save"},
            "operation": "invoke",
        }
    )

    assert result["operation"] == "invoke"
    assert result["target"]["path"] == [0, 0]
    assert result["target"]["automation_id"] == "save-button"
    assert button.invoked is True
    assert button.clicked is False
    assert window.window_actions == ["restore"]


def test_windows_runtime_uia_action_matches_terminator_style_selector(tmp_path: Path) -> None:
    driver = _FakeDriver()
    button = _FakeUIAElement(role="Button", title="Save As", automation_id="save-as-button")
    window = _FakeUIAElement(role="Window", title="Document", children=[button])
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    result = runtime.uia_action(
        {
            "context_id": "ctx-1",
            "target": {"selector": 'role:Button && name:"Save As"'},
            "operation": "invoke",
        }
    )

    assert result["target"]["path"] == [0, 0]
    assert button.invoked is True


def test_windows_runtime_uia_invoke_does_not_fallback_to_click(tmp_path: Path) -> None:
    driver = _FakeDriver()
    button = _FakeUIAElement(role="Button", title="Save", invokable=False, clickable=True)
    window = _FakeUIAElement(role="Window", title="Document", children=[button])
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    with pytest.raises(WindowsComputerUseError) as exc_info:
        runtime.uia_action(
            {
                "context_id": "ctx-1",
                "target": {"role": "Button", "title": "Save"},
                "operation": "invoke",
            }
        )

    assert exc_info.value.code == "COMPUTER_USE_UIA_ACTION_UNSUPPORTED"
    assert button.clicked is False


def test_windows_runtime_uia_action_minimizes_owning_window_structurally(tmp_path: Path) -> None:
    driver = _FakeDriver()
    button = _FakeUIAElement(role="Button", title="Save")
    window = _FakeUIAElement(role="Window", title="Document", children=[button])
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    result = runtime.uia_action(
        {
            "context_id": "ctx-1",
            "path": [0, 0],
            "operation": "hide",
        }
    )

    assert result["operation"] == "minimize"
    assert window.window_actions == ["minimize"]


def test_windows_runtime_uia_set_value_focuses_owner_before_typing_fallback(tmp_path: Path) -> None:
    driver = _FakeDriver()
    text_field = _FakeUIAElement(role="Edit", title="File name", automation_id="file-name")
    window = _FakeUIAElement(role="Window", title="Document", children=[text_field])
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    result = runtime.uia_action(
        {
            "context_id": "ctx-1",
            "path": [0, 0],
            "operation": "set_value",
            "value": "final.txt",
        }
    )

    assert result["operation"] == "set_value"
    assert window.window_actions == ["restore", "restore"]
    assert window.focused is True
    assert text_field.focused is True
    assert ("type_text", ("final.txt",), {"submit": False}) in driver.calls


def test_windows_runtime_uia_action_sets_value_by_path(tmp_path: Path) -> None:
    driver = _FakeDriver()
    text_field = _FakeUIAElement(role="Edit", title="File name", automation_id="file-name", editable=True)
    window = _FakeUIAElement(role="Window", title="Document", children=[text_field])
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    result = runtime.uia_action(
        {
            "context_id": "ctx-1",
            "path": [0, 0],
            "operation": "set_value",
            "value": "final.txt",
        }
    )

    assert result["operation"] == "set_value"
    assert result["target"]["role"] == "Edit"
    assert text_field.value == "final.txt"


def test_windows_runtime_uia_action_rejects_ambiguous_targets(tmp_path: Path) -> None:
    driver = _FakeDriver()
    window = _FakeUIAElement(
        role="Window",
        title="Document",
        children=[
            _FakeUIAElement(role="Button", title="Save"),
            _FakeUIAElement(role="Button", title="Cancel"),
        ],
    )
    driver.uia_root_elements = [window]
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    with pytest.raises(WindowsComputerUseError) as exc_info:
        runtime.uia_action({"context_id": "ctx-1", "target": {"role": "Button"}})

    assert exc_info.value.code == "COMPUTER_USE_UIA_TARGET_AMBIGUOUS"


def test_windows_runtime_uses_virtual_screen_origin_for_normalized_actions(tmp_path: Path) -> None:
    driver = _FakeDriver()
    driver._origin_x = -1920
    driver._origin_y = -120
    driver._width = 3200
    driver._height = 1200
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    session = runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    moved = runtime.move({"context_id": "ctx-1", "x": 0.25, "y": 0.5})
    clicked = runtime.click({"context_id": "ctx-1", "x": 1.0, "y": 0.0, "button": "left", "count": 1})

    assert session["origin_x"] == -1920
    assert session["origin_y"] == -120
    assert moved["pixel_x"] == -1120
    assert moved["pixel_y"] == 480
    assert clicked["pixel_x"] == 1280
    assert clicked["pixel_y"] == -120
    assert ("move", (-1120.0, 480.0), {}) in driver.calls
    assert ("click", (1280.0, -120.0), {"button": "left", "count": 1}) in driver.calls


def test_windows_runtime_normalizes_actions_and_routes_input(tmp_path: Path) -> None:
    driver = _FakeDriver()
    runtime = WindowsComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    runtime.move({"context_id": "ctx-1", "x": 0.25, "y": 0.75})
    runtime.click({"context_id": "ctx-1", "x": 0.5, "y": 0.5, "button": "left", "count": 2})
    runtime.scroll({"context_id": "ctx-1", "dx": 1, "dy": -2})
    runtime.key({"context_id": "ctx-1", "keys": ["ctrl", "alt", "t"]})
    runtime.type_text({"context_id": "ctx-1", "text": "hello", "submit": True})

    setup_calls = {"screen_geometry", "screen_size"}
    assert [call[0] for call in driver.calls if call[0] not in setup_calls] == [
        "move",
        "click",
        "scroll",
        "key",
        "type_text",
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows desktop support probe is Windows-only")
def test_windows_support_probe_is_true_when_dependencies_exist() -> None:
    # This is a smoke check for the real Windows path; it stays skipped on Linux.
    assert WINDOWS_BACKEND_SPEC.detect() is True
