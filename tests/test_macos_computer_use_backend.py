from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MACOS_PACKAGE_SRC = PROJECT_ROOT / "packages" / "a0-computer-use-macos" / "src"
if str(MACOS_PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(MACOS_PACKAGE_SRC))

from a0_computer_use_macos.backend import MACOS_BACKEND_SPEC, MacOSComputerUseBackend
import a0_computer_use_macos.runtime as macos_runtime_mod
from a0_computer_use_macos.runtime import (
    MacOSComputerUseError,
    MacOSComputerUseRuntime,
    MacOSSessionStore,
)
from a0_computer_use_macos.shared import normalize_action_payload


class _FakeDriver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self._width = 1280
        self._height = 720

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


def _runtime(tmp_path: Path) -> MacOSComputerUseRuntime:
    runtime = MacOSComputerUseRuntime(driver=_FakeDriver(), state_dir=tmp_path / "state")
    runtime._ensure_accessibility_permission = lambda **kwargs: None  # type: ignore[method-assign]
    runtime._probe_capture_dimensions = lambda **kwargs: (1280, 720)  # type: ignore[method-assign]
    return runtime


def test_macos_backend_spec_exports_expected_metadata() -> None:
    spec = MACOS_BACKEND_SPEC

    assert spec.backend_id == "macos"
    assert spec.backend_family == "macos"
    assert spec.interpreter_strategy == "current_python"
    assert Path(spec.helper_target).name == "runtime.py"
    assert spec.supports_trust_mode("interactive") is True
    assert spec.supports_trust_mode("persistent") is True
    assert spec.supports_trust_mode("allow") is True
    assert "inline-png-capture" in spec.features
    assert "coregraphics-screen-capture" in spec.features
    assert "background-screen-capture" in spec.features
    assert "no-cursor-steal-capture" in spec.features
    assert "accessibility-trust" in spec.features
    assert "global-pixel-actions" in spec.features
    assert "keyboard-targets-frontmost-app" in spec.features
    assert "accessibility-element-click" in spec.features
    assert "semantic-click-before-quartz-fallback" in spec.features
    assert "no-cursor-steal-accessibility-click" in spec.features
    assert "real-cursor-may-move" in spec.features
    assert "cursor-position-restore-after-click" in spec.features
    assert "frontmost-app-restore-after-click" in spec.features


def test_macos_backend_wrapper_uses_current_python() -> None:
    backend = MacOSComputerUseBackend()

    assert backend.spec is MACOS_BACKEND_SPEC
    assert backend.helper_command()[0] == sys.executable
    assert backend.helper_command()[-1] == "--stdio"


def test_macos_action_normalization_matches_shared_surface() -> None:
    move = normalize_action_payload("move", {"x": 0.25, "y": 0.75}, context_id="ctx-1")
    click = normalize_action_payload(
        "click",
        {"x": 0.4, "y": 0.6, "button": "right", "count": 2},
        context_id="ctx-1",
    )
    scroll = normalize_action_payload("scroll", {"dx": 1, "dy": -2}, context_id="ctx-1")
    keys = normalize_action_payload("key", {"key": "cmd+shift+t"}, context_id="ctx-1")
    typed = normalize_action_payload("type", {"text": "hello", "submit": True}, context_id="ctx-1")

    assert move["x"] == 0.25 and move["y"] == 0.75
    assert click["button"] == "right" and click["count"] == 2
    assert scroll["dx"] == 1 and scroll["dy"] == -2
    assert keys["keys"] == ["cmd", "shift", "t"]
    assert typed["text"] == "hello" and typed["submit"] is True


def test_macos_runtime_rejects_allow_without_restore_token(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    with pytest.raises(MacOSComputerUseError) as exc_info:
        runtime.start_session({"context_id": "ctx-1", "trust_mode": "allow"})

    assert exc_info.value.code == "COMPUTER_USE_REARM_REQUIRED"


def test_macos_runtime_session_policies_are_persisted_when_valid(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
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
    stored = MacOSSessionStore(state_dir=tmp_path / "state").get("ctx-1")
    assert stored is not None
    assert stored.restore_token == restore_token


def test_macos_runtime_capture_returns_inline_png_payload(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
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
    assert capture["png_base64"]
    assert base64.b64decode(capture["png_base64"])


def test_macos_runtime_capture_debug_dir_does_not_persist_screenshot(tmp_path: Path) -> None:
    runtime = MacOSComputerUseRuntime(
        driver=_FakeDriver(),
        state_dir=tmp_path / "state",
        capture_debug_dir=tmp_path / "debug-captures",
    )
    runtime._ensure_accessibility_permission = lambda **kwargs: None  # type: ignore[method-assign]
    runtime._probe_capture_dimensions = lambda **kwargs: (1280, 720)  # type: ignore[method-assign]
    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    capture = runtime.capture({"context_id": "ctx-1"})

    assert capture["png_base64"]
    assert not (tmp_path / "debug-captures").exists()


def test_macos_runtime_capture_writes_requested_path_without_inline_payload(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
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


def test_macos_driver_capture_prefers_coregraphics_without_screencapture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePNGData(bytes):
        pass

    class FakeImageRep:
        def initWithCGImage_(self, image: object) -> "FakeImageRep":
            return self

        def representationUsingType_properties_(self, png_type: object, properties: dict[str, object]) -> bytes:
            del png_type, properties
            return base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAIAAAADCAQAAABWKLW/AAAADElEQVR42mP8z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
            )

    class FakeNSBitmapImageRep:
        @classmethod
        def alloc(cls) -> FakeImageRep:
            return FakeImageRep()

    class FakeQuartz:
        kCGEventMouseMoved = 5
        kCGMouseButtonLeft = 0

        @staticmethod
        def CGMainDisplayID() -> int:
            return 1

        @staticmethod
        def CGDisplayCreateImage(display_id: int) -> object:
            assert display_id == 1
            return object()

    fake_appkit = type(
        "FakeAppKit",
        (),
        {
            "NSBitmapImageRep": FakeNSBitmapImageRep,
            "NSBitmapImageFileTypePNG": 4,
        },
    )
    monkeypatch.setattr(macos_runtime_mod, "_load_quartz_module", lambda: FakeQuartz)
    monkeypatch.setattr(macos_runtime_mod, "_load_appkit_module", lambda: fake_appkit)
    monkeypatch.setattr(macos_runtime_mod.shutil, "which", lambda name: None)

    driver = macos_runtime_mod._MacOSDesktopAutomation()

    _png_bytes, width, height = driver.capture_png()

    assert (width, height) == (2, 3)
    assert driver.last_capture_strategy == "coregraphics"


def test_macos_driver_click_restores_cursor_and_frontmost_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class FakePoint:
        x = 11
        y = 22

    class FakeApplication:
        def activateWithOptions_(self, options: int) -> None:
            events.append(("activate", options))

    class FakeWorkspace:
        def frontmostApplication(self) -> FakeApplication:
            return FakeApplication()

    class FakeNSWorkspace:
        @staticmethod
        def sharedWorkspace() -> FakeWorkspace:
            return FakeWorkspace()

    class FakeQuartz:
        kCGEventMouseMoved = 1
        kCGEventLeftMouseDown = 2
        kCGEventLeftMouseUp = 3
        kCGEventRightMouseDown = 4
        kCGEventRightMouseUp = 5
        kCGEventOtherMouseDown = 6
        kCGEventOtherMouseUp = 7
        kCGMouseButtonLeft = 0
        kCGMouseButtonRight = 1
        kCGMouseButtonCenter = 2
        kCGMouseEventClickState = 1
        kCGHIDEventTap = 0

        @staticmethod
        def CGEventCreate(source: object) -> object:
            del source
            return object()

        @staticmethod
        def CGEventGetLocation(event: object) -> FakePoint:
            del event
            return FakePoint()

        @staticmethod
        def CGEventCreateMouseEvent(source: object, event_type: int, point: tuple[float, float], button: int) -> dict[str, object]:
            del source
            return {"type": event_type, "point": point, "button": button}

        @staticmethod
        def CGEventSetIntegerValueField(event: dict[str, object], field: int, value: int) -> None:
            del field
            event["click_state"] = value

        @staticmethod
        def CGEventPost(tap: int, event: dict[str, object]) -> None:
            del tap
            events.append(("post", event["point"]))

    fake_appkit = type(
        "FakeAppKit",
        (),
        {
            "NSWorkspace": FakeNSWorkspace,
            "NSApplicationActivateIgnoringOtherApps": 2,
        },
    )
    monkeypatch.setattr(macos_runtime_mod, "_load_quartz_module", lambda: FakeQuartz)
    monkeypatch.setattr(macos_runtime_mod, "_load_appkit_module", lambda: fake_appkit)
    monkeypatch.setattr(
        macos_runtime_mod,
        "_load_accessibility_module",
        lambda: (_ for _ in ()).throw(MacOSComputerUseError("UNAVAILABLE", "no ax")),
    )

    driver = macos_runtime_mod._MacOSDesktopAutomation()

    driver.click(100, 200, button="left", count=1)

    posted_points = [event[1] for event in events if event[0] == "post"]
    assert posted_points[0] == (100.0, 200.0)
    assert posted_points[-1] == (11.0, 22.0)
    assert ("activate", 2) in events
    assert driver.last_click_strategy == "quartz-cursor-restore"


def test_macos_driver_click_prefers_accessibility_press_without_cursor_motion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class FakeQuartz:
        pass

    class FakeAccessibility:
        kAXPressAction = "AXPress"

        @staticmethod
        def AXUIElementCreateSystemWide() -> str:
            return "system"

        @staticmethod
        def AXUIElementCopyElementAtPosition(system: str, x: float, y: float, stop: object) -> tuple[int, str]:
            del stop
            events.append(("element_at_position", (system, x, y)))
            return 0, "button"

        @staticmethod
        def AXUIElementPerformAction(element: str, action: str) -> int:
            events.append(("perform_action", (element, action)))
            return 0

    monkeypatch.setattr(macos_runtime_mod, "_load_quartz_module", lambda: FakeQuartz)
    monkeypatch.setattr(macos_runtime_mod, "_load_accessibility_module", lambda: FakeAccessibility)

    driver = macos_runtime_mod._MacOSDesktopAutomation()

    driver.click(100, 200, button="left", count=1)

    assert events == [
        ("element_at_position", ("system", 100.0, 200.0)),
        ("perform_action", ("button", "AXPress")),
    ]
    assert driver.last_click_strategy == "accessibility-press"


def test_macos_runtime_debug_logs_start_session_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("A0_COMPUTER_USE_DEBUG", "1")
    runtime = _runtime(tmp_path)

    runtime.start_session(
        {
            "context_id": "ctx-1",
            "trust_mode": "persistent",
            "restore_token": "123e4567-e89b-12d3-a456-426614174000",
        }
    )

    stderr = capsys.readouterr().err
    assert "start_session.begin" in stderr
    assert "start_session.accessibility.begin" in stderr
    assert "start_session.capture_probe.begin" in stderr
    assert "start_session.created_session" in stderr


def test_macos_runtime_normalizes_actions_and_routes_input(tmp_path: Path) -> None:
    driver = _FakeDriver()
    runtime = MacOSComputerUseRuntime(driver=driver, state_dir=tmp_path / "state")
    runtime._ensure_accessibility_permission = lambda **kwargs: None  # type: ignore[method-assign]
    runtime._probe_capture_dimensions = lambda **kwargs: (1280, 720)  # type: ignore[method-assign]
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
    runtime.key({"context_id": "ctx-1", "keys": ["cmd", "shift", "t"]})
    runtime.type_text({"context_id": "ctx-1", "text": "hello", "submit": True})

    assert [call[0] for call in driver.calls if call[0] != "capture_png"] == [
        "move",
        "click",
        "scroll",
        "key",
        "type_text",
    ]
