from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SRC = PROJECT_ROOT / "packages" / "a0-computer-use-x11" / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from a0_computer_use_x11 import X11_BACKEND_SPEC, get_backend_spec
from a0_computer_use_x11 import detection as x11_detection
from a0_computer_use_x11 import paths as x11_paths
from a0_computer_use_x11.computer_use_helper import (
    X11ComputerUseError,
    X11ComputerUseHelper,
)


class _FakeDriver:
    display_name = ":99"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.closed = False

    def screen_size(self) -> tuple[int, int]:
        self.calls.append(("screen_size", tuple(), {}))
        return 1024, 768

    def capture_png(self, output_path: str | None = None) -> dict[str, object]:
        self.calls.append(("capture_png", (output_path,), {}))
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/5wAAAABJRU5ErkJggg=="
        )
        result: dict[str, object] = {
            "width": 1,
            "height": 1,
            "captured_at": 1.0,
        }
        if output_path:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png_bytes)
            result["capture_path"] = output_path
        else:
            result["png_base64"] = base64.b64encode(png_bytes).decode("ascii")
        return result

    def move(self, x: float, y: float) -> None:
        self.calls.append(("move", (x, y), {}))

    def click(self, *, button: str, count: int) -> None:
        self.calls.append(("click", tuple(), {"button": button, "count": count}))

    def scroll(self, dx: int, dy: int) -> None:
        self.calls.append(("scroll", (dx, dy), {}))

    def key(self, keys: list[str]) -> None:
        self.calls.append(("key", (tuple(keys),), {}))

    def type_text(self, text: str, *, submit: bool) -> None:
        self.calls.append(("type_text", (text,), {"submit": submit}))

    def close(self) -> None:
        self.closed = True


def test_x11_backend_spec_exposes_expected_metadata() -> None:
    spec = get_backend_spec()

    assert spec is X11_BACKEND_SPEC
    assert spec.backend_id == "x11"
    assert spec.backend_family == "linux"
    assert spec.priority == 90
    assert spec.interpreter_strategy == "current_python"
    assert spec.helper_target == str(x11_paths.HELPER_SCRIPT)
    assert spec.supports_trust_mode("interactive") is True
    assert spec.supports_trust_mode("persistent") is True
    assert spec.supports_trust_mode("allow") is True
    assert "x11-xtest" in spec.features
    assert "inline-png-capture" in spec.features
    assert "global-pixel-actions" in spec.features
    assert "real-cursor-may-move" in spec.features
    assert "focus-risk" in spec.features


def test_x11_detection_and_support_reason_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(x11_detection.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(x11_detection, "_runtime_support_issue", lambda: "")

    assert x11_detection.detect_x11_support() is True
    assert x11_detection.x11_support_reason() == "X11 XTEST backend is available."

    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert x11_detection.detect_x11_support() is False
    assert "Wayland portal backend" in x11_detection.x11_support_reason()

    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("DISPLAY")
    assert x11_detection.detect_x11_support() is False
    assert "DISPLAY is not set" in x11_detection.x11_support_reason()


def test_x11_helper_rejects_allow_without_restore_token() -> None:
    helper = X11ComputerUseHelper(driver=_FakeDriver())

    with pytest.raises(X11ComputerUseError) as exc_info:
        helper.start_session({"context_id": "ctx-1", "trust_mode": "allow"})

    assert exc_info.value.code == "COMPUTER_USE_REARM_REQUIRED"


def test_x11_helper_session_and_capture_contract(tmp_path: Path) -> None:
    driver = _FakeDriver()
    helper = X11ComputerUseHelper(driver=driver)

    session = helper.start_session({"context_id": "ctx-1", "trust_mode": "persistent"})
    capture_path = tmp_path / "captures" / "screen.png"
    capture = helper.capture({"session_id": session["session_id"], "capture_path": str(capture_path)})
    inline_capture = helper.capture({"session_id": session["session_id"]})

    assert session["active"] is True
    assert session["display"] == ":99"
    assert session["width"] == 1024
    assert session["height"] == 768
    assert session["restore_token"]
    assert capture["capture_path"] == str(capture_path)
    assert capture_path.exists()
    assert "png_base64" not in capture
    assert base64.b64decode(str(inline_capture["png_base64"]))


def test_x11_helper_routes_input_actions() -> None:
    driver = _FakeDriver()
    helper = X11ComputerUseHelper(driver=driver)
    session = helper.start_session({"context_id": "ctx-1", "trust_mode": "persistent"})

    helper.move({"session_id": session["session_id"], "x": 0.25, "y": 0.75})
    helper.click({"session_id": session["session_id"], "x": 0.5, "y": 0.5, "button": "right", "count": 2})
    helper.scroll({"session_id": session["session_id"], "dx": 1, "dy": -2})
    helper.key({"session_id": session["session_id"], "keys": ["ctrl", "shift", "t"]})
    helper.type_text({"session_id": session["session_id"], "text": "hello", "submit": True})

    assert [call[0] for call in driver.calls] == [
        "screen_size",
        "move",
        "move",
        "click",
        "scroll",
        "key",
        "type_text",
    ]
    assert driver.calls[2] == ("move", (0.5, 0.5), {})
    assert driver.calls[3] == ("click", tuple(), {"button": "right", "count": 2})
    assert driver.calls[5] == ("key", (("ctrl", "shift", "t"),), {})
    assert driver.calls[6] == ("type_text", ("hello",), {"submit": True})
