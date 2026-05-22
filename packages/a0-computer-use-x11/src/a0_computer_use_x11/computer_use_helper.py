from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class X11ComputerUseError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class X11Session:
    context_id: str
    trust_mode: str
    session_id: str
    width: int
    height: int
    display: str
    restore_token: str


class X11Driver(Protocol):
    def screen_size(self) -> tuple[int, int]:
        ...

    def capture_png(self, output_path: str | None = None) -> dict[str, Any]:
        ...

    def move(self, x: float, y: float) -> None:
        ...

    def click(self, *, button: str, count: int) -> None:
        ...

    def scroll(self, dx: int, dy: int) -> None:
        ...

    def key(self, keys: list[str]) -> None:
        ...

    def type_text(self, text: str, *, submit: bool) -> None:
        ...

    def close(self) -> None:
        ...


_CHAR_KEYSYM_NAMES = {
    " ": "space",
    "!": "exclam",
    '"': "quotedbl",
    "#": "numbersign",
    "$": "dollar",
    "%": "percent",
    "&": "ampersand",
    "'": "apostrophe",
    "(": "parenleft",
    ")": "parenright",
    "*": "asterisk",
    "+": "plus",
    ",": "comma",
    "-": "minus",
    ".": "period",
    "/": "slash",
    ":": "colon",
    ";": "semicolon",
    "<": "less",
    "=": "equal",
    ">": "greater",
    "?": "question",
    "@": "at",
    "[": "bracketleft",
    "\\": "backslash",
    "]": "bracketright",
    "^": "asciicircum",
    "_": "underscore",
    "`": "grave",
    "{": "braceleft",
    "|": "bar",
    "}": "braceright",
    "~": "asciitilde",
    "\n": "Return",
    "\r": "Return",
    "\t": "Tab",
}

_KEY_ALIASES = {
    "alt": "Alt_L",
    "backspace": "BackSpace",
    "cmd": "Super_L",
    "command": "Super_L",
    "control": "Control_L",
    "ctrl": "Control_L",
    "del": "Delete",
    "delete": "Delete",
    "down": "Down",
    "end": "End",
    "enter": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "home": "Home",
    "left": "Left",
    "meta": "Meta_L",
    "option": "Alt_L",
    "pagedown": "Page_Down",
    "page_down": "Page_Down",
    "pageup": "Page_Up",
    "page_up": "Page_Up",
    "return": "Return",
    "right": "Right",
    "shift": "Shift_L",
    "space": "space",
    "super": "Super_L",
    "tab": "Tab",
    "up": "Up",
    "win": "Super_L",
    "windows": "Super_L",
}

_MODIFIER_KEYSYM_NAMES = {
    "Alt_L",
    "Alt_R",
    "Control_L",
    "Control_R",
    "ISO_Level3_Shift",
    "Meta_L",
    "Meta_R",
    "Shift_L",
    "Shift_R",
    "Super_L",
    "Super_R",
}

_BUTTONS = {
    "left": 1,
    "middle": 2,
    "right": 3,
}


class X11DesktopDriver:
    def __init__(self) -> None:
        try:
            from Xlib import X
            from Xlib import XK
            from Xlib import display
        except Exception as exc:
            raise X11ComputerUseError(
                "COMPUTER_USE_X11_IMPORT_FAILED",
                f"python-xlib is required for X11 computer use: {exc}",
            ) from exc

        try:
            from mss import mss
            from mss import tools as mss_tools
        except Exception as exc:
            raise X11ComputerUseError(
                "COMPUTER_USE_X11_CAPTURE_IMPORT_FAILED",
                f"mss is required for X11 screen capture: {exc}",
            ) from exc

        self._X = X
        self._XK = XK
        self._display = display.Display()
        if not self._display.query_extension("XTEST"):
            self._display.close()
            raise X11ComputerUseError(
                "COMPUTER_USE_X11_XTEST_UNAVAILABLE",
                "The X11 display does not advertise the XTEST extension.",
            )

        display_name = (os.environ.get("DISPLAY") or "").strip() or None
        try:
            self._mss = mss(display=display_name)
        except TypeError:
            self._mss = mss()
        self._mss_tools = mss_tools
        self._monitor_cache: dict[str, int] | None = None

    @property
    def display_name(self) -> str:
        return str(self._display.get_display_name() or os.environ.get("DISPLAY") or "")

    def close(self) -> None:
        try:
            self._mss.close()
        except Exception:
            pass
        try:
            self._display.close()
        except Exception:
            pass

    def screen_size(self) -> tuple[int, int]:
        monitor = self._monitor()
        return monitor["width"], monitor["height"]

    def capture_png(self, output_path: str | None = None) -> dict[str, Any]:
        monitor = self._monitor()
        screenshot = self._mss.grab(monitor)
        width, height = int(screenshot.size[0]), int(screenshot.size[1])
        result = {
            "width": width,
            "height": height,
            "captured_at": time.time(),
        }

        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            self._mss_tools.to_png(screenshot.rgb, screenshot.size, output=output_path)
            result["capture_path"] = output_path
            return result

        png_bytes = self._mss_tools.to_png(screenshot.rgb, screenshot.size)
        if not isinstance(png_bytes, bytes):
            raise X11ComputerUseError(
                "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                "X11 screen capture did not return PNG bytes.",
            )
        result["png_base64"] = base64.b64encode(png_bytes).decode("ascii")
        return result

    def move(self, x: float, y: float) -> None:
        pixel_x, pixel_y = self._absolute_position(x, y)
        self._fake_motion(pixel_x, pixel_y)

    def click(self, *, button: str, count: int) -> None:
        button_code = _BUTTONS.get(button)
        if button_code is None:
            raise X11ComputerUseError("COMPUTER_USE_BAD_BUTTON", "button must be left, right, or middle")
        for _ in range(max(1, count)):
            self._fake_button(button_code)

    def scroll(self, dx: int, dy: int) -> None:
        if dy > 0:
            for _ in range(dy):
                self._fake_button(4)
        elif dy < 0:
            for _ in range(abs(dy)):
                self._fake_button(5)

        if dx < 0:
            for _ in range(abs(dx)):
                self._fake_button(6)
        elif dx > 0:
            for _ in range(dx):
                self._fake_button(7)

    def key(self, keys: list[str]) -> None:
        if not keys:
            raise X11ComputerUseError("COMPUTER_USE_KEYS_REQUIRED", "key requires a non-empty keys list")
        self._send_key_sequence(keys)

    def type_text(self, text: str, *, submit: bool) -> None:
        if not text:
            raise X11ComputerUseError("COMPUTER_USE_TEXT_REQUIRED", "type requires text")
        for character in text:
            self._send_key_sequence([character])
        if submit:
            self._send_key_sequence(["Return"])

    def _monitor(self) -> dict[str, int]:
        if self._monitor_cache is None:
            monitors = list(self._mss.monitors)
            if not monitors:
                raise X11ComputerUseError(
                    "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                    "No X11 monitors were discovered.",
                )
            monitor = monitors[0]
            self._monitor_cache = {
                "left": int(monitor.get("left", 0)),
                "top": int(monitor.get("top", 0)),
                "width": int(monitor.get("width", 0)),
                "height": int(monitor.get("height", 0)),
            }
            if self._monitor_cache["width"] <= 0 or self._monitor_cache["height"] <= 0:
                raise X11ComputerUseError(
                    "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                    "The X11 monitor geometry is invalid.",
                )
        return dict(self._monitor_cache)

    def _absolute_position(self, x: float, y: float) -> tuple[int, int]:
        monitor = self._monitor()
        max_x = max(0, monitor["width"] - 1)
        max_y = max(0, monitor["height"] - 1)
        pixel_x = monitor["left"] + min(max_x, max(0, round(max_x * x)))
        pixel_y = monitor["top"] + min(max_y, max(0, round(max_y * y)))
        return pixel_x, pixel_y

    def _fake_motion(self, x: int, y: int) -> None:
        root = self._display.screen().root
        self._display.xtest_fake_input(self._X.MotionNotify, root=root, x=int(x), y=int(y))
        self._display.sync()

    def _fake_button(self, button: int) -> None:
        self._display.xtest_fake_input(self._X.ButtonPress, detail=int(button))
        self._display.xtest_fake_input(self._X.ButtonRelease, detail=int(button))
        self._display.sync()

    def _send_key_sequence(self, keys: list[str]) -> None:
        press_order: list[int] = []
        pressed: set[int] = set()
        for key in keys:
            modifiers, keycode = self._keycode_with_modifiers(key)
            for modifier in modifiers:
                if modifier not in pressed:
                    press_order.append(modifier)
                    pressed.add(modifier)
            if keycode not in pressed:
                press_order.append(keycode)
                pressed.add(keycode)

        for keycode in press_order:
            self._display.xtest_fake_input(self._X.KeyPress, detail=keycode)
        for keycode in reversed(press_order):
            self._display.xtest_fake_input(self._X.KeyRelease, detail=keycode)
        self._display.sync()

    def _keycode_with_modifiers(self, value: str) -> tuple[list[int], int]:
        keysym_name = self._keysym_name(value)
        keycode, index = self._keycode_for_keysym_name(keysym_name)
        if keysym_name in _MODIFIER_KEYSYM_NAMES:
            return [], keycode

        modifiers: list[int] = []
        if index in {1, 3}:
            modifiers.append(self._modifier_keycode("Shift_L"))
        if index in {2, 3}:
            modifiers.append(self._modifier_keycode("ISO_Level3_Shift"))
        return modifiers, keycode

    def _modifier_keycode(self, keysym_name: str) -> int:
        keycode, _index = self._keycode_for_keysym_name(keysym_name)
        return keycode

    def _keycode_for_keysym_name(self, keysym_name: str) -> tuple[int, int]:
        keysym = int(self._XK.string_to_keysym(keysym_name))
        if keysym == int(self._X.NoSymbol):
            raise X11ComputerUseError("COMPUTER_USE_BAD_KEY", f"Unsupported key: {keysym_name}")

        keycodes = list(self._display.keysym_to_keycodes(keysym))
        if keycodes:
            keycode, index = keycodes[0]
            return int(keycode), int(index)

        keycode = int(self._display.keysym_to_keycode(keysym))
        if keycode <= 0:
            raise X11ComputerUseError("COMPUTER_USE_BAD_KEY", f"Key is not mapped on this X11 keyboard: {keysym_name}")
        return keycode, 0

    def _keysym_name(self, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            raise X11ComputerUseError("COMPUTER_USE_BAD_KEY", "Empty key value.")
        if raw in _CHAR_KEYSYM_NAMES:
            return _CHAR_KEYSYM_NAMES[raw]

        lowered = raw.lower()
        if lowered in _KEY_ALIASES:
            return _KEY_ALIASES[lowered]
        if lowered.startswith("f") and lowered[1:].isdigit():
            return "F" + lowered[1:]
        if len(raw) == 1:
            return _CHAR_KEYSYM_NAMES.get(raw, raw)
        return raw


class X11ComputerUseHelper:
    def __init__(self, driver: X11Driver | None = None) -> None:
        self._driver = driver or X11DesktopDriver()
        self._session: X11Session | None = None

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "start_session": self.start_session,
            "status": self.status,
            "capture": self.capture,
            "move": self.move,
            "click": self.click,
            "scroll": self.scroll,
            "key": self.key,
            "type": self.type_text,
            "stop_session": self.stop_session,
        }
        handler = handlers.get(method)
        if handler is None:
            raise X11ComputerUseError("UNKNOWN_METHOD", f"Unknown computer-use helper method: {method}")
        return handler(params)

    def start_session(self, params: dict[str, Any]) -> dict[str, Any]:
        trust_mode = str(params.get("trust_mode") or "persistent").strip().lower()
        context_id = str(params.get("context_id") or "default").strip() or "default"
        restore_token = str(params.get("restore_token") or "").strip()
        if trust_mode == "allow" and not restore_token:
            raise X11ComputerUseError(
                "COMPUTER_USE_REARM_REQUIRED",
                "Allow requires a stored restore token.",
            )

        width, height = self._driver.screen_size()
        self._session = X11Session(
            context_id=context_id,
            trust_mode=trust_mode,
            session_id=uuid.uuid4().hex,
            width=width,
            height=height,
            display=getattr(self._driver, "display_name", os.environ.get("DISPLAY", "")),
            restore_token=restore_token or str(uuid.uuid4()),
        )
        return self._session_payload(self._session)

    def status(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        if self._session is None:
            return {"active": False}
        return self._session_payload(self._session)

    def capture(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        capture_path = str(params.get("capture_path") or "").strip()
        result = self._driver.capture_png(capture_path or None)
        result["session_id"] = session.session_id
        return result

    def move(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        x = float(params.get("x"))
        y = float(params.get("y"))
        self._driver.move(x, y)
        return {
            "x": x,
            "y": y,
            "pixel_x": int(round(max(0, session.width - 1) * x)),
            "pixel_y": int(round(max(0, session.height - 1) * y)),
            "session_id": session.session_id,
        }

    def click(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        button = str(params.get("button") or "left").strip().lower()
        count = max(1, int(params.get("count") or 1))
        if button not in _BUTTONS:
            raise X11ComputerUseError("COMPUTER_USE_BAD_BUTTON", "button must be left, right, or middle")
        if "x" in params and "y" in params:
            self.move(params)
        self._driver.click(button=button, count=count)
        return {"button": button, "count": count, "session_id": session.session_id}

    def scroll(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        dx = int(params.get("dx") or 0)
        dy = int(params.get("dy") or 0)
        self._driver.scroll(dx, dy)
        return {"dx": dx, "dy": dy, "session_id": session.session_id}

    def key(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        keys = params.get("keys")
        if not isinstance(keys, list) or not keys:
            raise X11ComputerUseError("COMPUTER_USE_KEYS_REQUIRED", "key requires a non-empty keys list")
        normalized = [str(item).strip() for item in keys if str(item).strip()]
        if not normalized:
            raise X11ComputerUseError("COMPUTER_USE_KEYS_REQUIRED", "key requires a non-empty keys list")
        self._driver.key(normalized)
        return {"keys": normalized, "session_id": session.session_id}

    def type_text(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        text = str(params.get("text") or "")
        submit = bool(params.get("submit"))
        if not text:
            raise X11ComputerUseError("COMPUTER_USE_TEXT_REQUIRED", "type requires text")
        self._driver.type_text(text, submit=submit)
        return {"text": text, "submitted": submit, "session_id": session.session_id}

    def stop_session(self, params: dict[str, Any]) -> dict[str, Any]:
        del params
        self._session = None
        return {"active": False, "status": "stopped", "session_id": ""}

    def close(self) -> None:
        self._session = None
        self._driver.close()

    def _require_session(self, params: dict[str, Any]) -> X11Session:
        session = self._session
        if session is None:
            raise X11ComputerUseError("COMPUTER_USE_SESSION_REQUIRED", "No computer-use session is active.")
        requested_id = str(params.get("session_id") or "").strip()
        if requested_id and requested_id != session.session_id:
            raise X11ComputerUseError(
                "COMPUTER_USE_SESSION_MISMATCH",
                "The requested computer-use session is no longer active.",
            )
        return session

    def _session_payload(self, session: X11Session) -> dict[str, Any]:
        return {
            "active": True,
            "context_id": session.context_id,
            "trust_mode": session.trust_mode,
            "session_id": session.session_id,
            "display": session.display,
            "width": session.width,
            "height": session.height,
            "restore_token": session.restore_token,
        }


def serve_stdio() -> int:
    helper = X11ComputerUseHelper()
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            request_id = ""
            try:
                request = json.loads(line)
                action = str(request.get("action") or "").strip()
                request_id = str(request.get("request_id") or "")
                if action == "shutdown":
                    response = {"request_id": request_id, "ok": True, "result": {"shutdown": True}}
                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()
                    break
                if not isinstance(request, dict):
                    raise X11ComputerUseError("COMPUTER_USE_BAD_REQUEST", "Invalid helper request.")
                result = helper.dispatch(action, request)
                response = {"request_id": request_id, "ok": True, "result": result}
            except X11ComputerUseError as exc:
                response = {
                    "request_id": request_id,
                    "ok": False,
                    "error": str(exc),
                    "code": exc.code,
                }
            except Exception as exc:
                response = {
                    "request_id": request_id,
                    "ok": False,
                    "error": str(exc),
                    "code": "COMPUTER_USE_ERROR",
                }
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    finally:
        helper.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdio", action="store_true")
    args = parser.parse_args(argv)
    if not args.stdio:
        parser.error("Use --stdio to run the computer-use helper protocol.")
    return serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
