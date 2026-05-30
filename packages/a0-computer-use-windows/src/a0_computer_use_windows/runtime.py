from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent_zero_cli.computer_use_backend import (
    COMPUTER_USE_CONTRACT_VERSION,
    computer_use_capabilities_from_features,
)

if __package__ in {None, ""}:
    package_dir = Path(__file__).resolve().parent
    parent_dir = package_dir.parent
    if str(parent_dir) not in sys.path:
        sys.path.insert(0, str(parent_dir))

from a0_computer_use_windows.detection import (
    windows_backend_support_reason,
    windows_backend_supported,
)
from a0_computer_use_windows.shared import (
    CAPTURE_DEBUG_DIR_ENV,
    STATE_DIR_ENV,
    WINDOWS_BACKEND_FEATURES,
    WINDOWS_BACKEND_FAMILY,
    WINDOWS_BACKEND_ID,
    WINDOWS_TRUST_MODES,
    TrustModePolicy,
    coerce_bool,
    coerce_int,
    normalize_action_payload,
    normalize_context_id,
    normalize_restore_token,
    resolve_trust_mode_policy,
    safe_context_segment,
)

_UIA_DEFAULT_MAX_DEPTH = 4
_UIA_DEFAULT_MAX_NODES = 200
_UIA_HARD_MAX_DEPTH = 8
_UIA_HARD_MAX_NODES = 500
_UIA_TEXT_LIMIT = 240
_WINDOW_OPERATION_ALIASES = {
    "activate_window": "focus_window",
    "bring_to_front": "focus_window",
    "foreground": "focus_window",
    "foreground_window": "focus_window",
    "window_focus": "focus_window",
    "minimize_window": "minimize",
    "hide": "minimize",
    "restore_window": "restore",
    "normal": "restore",
    "show": "restore",
    "maximize_window": "maximize",
    "close_window": "close",
}
_WINDOW_OPERATIONS = {"focus_window", "minimize", "restore", "maximize", "close"}
_WINDOW_VISUAL_STATES = {
    "normal": 0,
    "restore": 0,
    "maximized": 1,
    "maximize": 1,
    "minimized": 2,
    "minimize": 2,
}
_SW_HIDE = 0
_SW_NORMAL = 1
_SW_SHOW = 5
_SW_MINIMIZE = 6
_SW_RESTORE = 9
_SW_MAXIMIZE = 3


def _backend_contract_metadata() -> dict[str, Any]:
    return {
        "contract_version": COMPUTER_USE_CONTRACT_VERSION,
        "capabilities": computer_use_capabilities_from_features(
            backend_id=WINDOWS_BACKEND_ID,
            backend_family=WINDOWS_BACKEND_FAMILY,
            features=WINDOWS_BACKEND_FEATURES,
        ),
    }


class WindowsComputerUseError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.result = result


class WindowsDesktopDriver(Protocol):
    def screen_size(self) -> tuple[int, int]:
        ...

    def capture_png(self) -> tuple[bytes, int, int] | tuple[bytes, int, int, int, int]:
        ...

    def move(self, x: float, y: float) -> None:
        ...

    def click(self, x: float, y: float, *, button: str, count: int) -> None:
        ...

    def scroll(self, dx: int, dy: int) -> None:
        ...

    def key(self, keys: list[str]) -> None:
        ...

    def type_text(self, text: str, *, submit: bool) -> None:
        ...

    def uia_roots(self) -> list[Any]:
        ...


@dataclass(frozen=True)
class ScreenGeometry:
    origin_x: int
    origin_y: int
    width: int
    height: int


@dataclass
class WindowsSession:
    context_id: str
    session_id: str
    trust_mode: str
    restore_token: str = ""
    active: bool = False
    width: int = 0
    height: int = 0
    origin_x: int = 0
    origin_y: int = 0
    updated_at: float = field(default_factory=time.time)

    def to_payload(self, *, reused: bool = False) -> dict[str, Any]:
        payload = {
            "context_id": self.context_id,
            "session_id": self.session_id,
            "trust_mode": self.trust_mode,
            "active": self.active,
            "status": "active" if self.active else "stopped",
            "width": self.width,
            "height": self.height,
            "origin_x": self.origin_x,
            "origin_y": self.origin_y,
            "backend_id": WINDOWS_BACKEND_ID,
            "backend_family": WINDOWS_BACKEND_FAMILY,
            "features": list(WINDOWS_BACKEND_FEATURES),
            "supported": windows_backend_supported(),
            "support_reason": windows_backend_support_reason(),
        }
        payload.update(_backend_contract_metadata())
        if self.restore_token:
            payload["restore_token"] = self.restore_token
        if reused:
            payload["reused"] = True
        return payload

    def to_record(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "session_id": self.session_id,
            "trust_mode": self.trust_mode,
            "restore_token": self.restore_token,
            "active": self.active,
            "width": self.width,
            "height": self.height,
            "origin_x": self.origin_x,
            "origin_y": self.origin_y,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(cls, payload: dict[str, Any]) -> "WindowsSession":
        return cls(
            context_id=str(payload.get("context_id", "") or "default"),
            session_id=str(payload.get("session_id", "") or ""),
            trust_mode=str(payload.get("trust_mode", "") or "persistent").strip().lower() or "persistent",
            restore_token=normalize_restore_token(payload.get("restore_token", "")),
            active=bool(payload.get("active")),
            width=coerce_int(payload.get("width"), name="width", default=0),
            height=coerce_int(payload.get("height"), name="height", default=0),
            origin_x=coerce_int(payload.get("origin_x"), name="origin_x", default=0),
            origin_y=coerce_int(payload.get("origin_y"), name="origin_y", default=0),
            updated_at=float(payload.get("updated_at") or time.time()),
        )


class WindowsSessionStore:
    def __init__(self, state_dir: str | os.PathLike[str] | None = None) -> None:
        configured = str(state_dir or os.environ.get(STATE_DIR_ENV, "")).strip()
        if configured:
            self.state_dir = Path(configured)
        else:
            self.state_dir = Path.home() / ".a0" / "computer-use-windows"
        self.state_file = self.state_dir / "sessions.json"

    def _read_records(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        records: dict[str, dict[str, Any]] = {}
        for context_id, record in payload.items():
            if isinstance(context_id, str) and isinstance(record, dict):
                records[context_id] = record
        return records

    def _write_records(self, records: dict[str, dict[str, Any]]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(records, indent=2, sort_keys=True), encoding="utf-8")

    def get(self, context_id: str) -> WindowsSession | None:
        record = self._read_records().get(context_id)
        if record is None:
            return None
        return WindowsSession.from_record(record)

    def put(self, session: WindowsSession) -> None:
        records = self._read_records()
        records[session.context_id] = session.to_record()
        self._write_records(records)

    def clear(self, context_id: str) -> None:
        records = self._read_records()
        if context_id not in records:
            return
        records.pop(context_id, None)
        if records:
            self._write_records(records)
        else:
            try:
                self.state_file.unlink()
            except OSError:
                pass


def _default_capture_debug_dir() -> Path | None:
    configured = str(os.environ.get(CAPTURE_DEBUG_DIR_ENV, "")).strip()
    if configured:
        return Path(configured)
    return None


def _load_dxcam_module() -> Any:
    try:
        import dxcam  # type: ignore
    except Exception as exc:  # pragma: no cover - only exercised on Windows
        raise WindowsComputerUseError(
            "COMPUTER_USE_UNSUPPORTED",
            "dxcam is required for Windows computer-use capture. Reinstall the A0 CLI Windows dependencies in the active virtual environment.",
        ) from exc
    return dxcam


def _load_pywinauto_modules() -> tuple[Any, Any]:  # pragma: no cover - only exercised on Windows
    try:
        from pywinauto import keyboard, mouse
    except Exception as exc:
        raise WindowsComputerUseError(
            "COMPUTER_USE_UNSUPPORTED",
            "pywinauto is required for Windows computer-use input injection.",
        ) from exc
    return keyboard, mouse


def _load_desktop_module() -> Any:  # pragma: no cover - only exercised on Windows
    try:
        from pywinauto import Desktop
    except Exception as exc:
        raise WindowsComputerUseError(
            "COMPUTER_USE_UNSUPPORTED",
            "pywinauto is required for Windows desktop automation.",
        ) from exc
    return Desktop


class _WindowsDesktopAutomation:
    def __init__(self) -> None:
        self._camera: Any = None
        try:
            desktop_cls = _load_desktop_module()
            self._desktop = desktop_cls(backend="uia")
        except Exception as exc:  # pragma: no cover - only exercised on Windows
            raise WindowsComputerUseError(
                "COMPUTER_USE_UNSUPPORTED",
                (
                    "pywinauto UIA desktop automation could not initialize. Run the A0 CLI "
                    "inside the same interactive Windows desktop session as the target apps; "
                    "services, disconnected Remote Desktop sessions, and UAC/elevated-app "
                    "isolation can block desktop automation."
                ),
            ) from exc

    def screen_geometry(self) -> ScreenGeometry:
        try:  # pragma: no cover - only exercised on Windows
            import ctypes

            user32 = ctypes.windll.user32
            origin_x = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
            origin_y = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
            width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
            height = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
            if width <= 0 or height <= 0:
                width = int(user32.GetSystemMetrics(0))
                height = int(user32.GetSystemMetrics(1))
                origin_x = 0
                origin_y = 0
            return ScreenGeometry(origin_x=origin_x, origin_y=origin_y, width=width, height=height)
        except Exception as exc:
            raise WindowsComputerUseError(
                "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                (
                    "Unable to read the Windows virtual desktop dimensions. Make sure the "
                    "desktop session is unlocked and the A0 CLI is not running as a service "
                    "or in a disconnected Remote Desktop session."
                ),
            ) from exc

    def screen_size(self) -> tuple[int, int]:
        geometry = self.screen_geometry()
        return geometry.width, geometry.height

    def _capture_all_screens_png(self, geometry: ScreenGeometry) -> tuple[bytes, int, int, int, int] | None:
        if sys.platform != "win32":
            return None
        try:  # pragma: no cover - only exercised on Windows
            from PIL import ImageGrab

            image = ImageGrab.grab(all_screens=True)
        except Exception:
            return None

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue(), int(image.width), int(image.height), geometry.origin_x, geometry.origin_y

    def capture_png(self) -> tuple[bytes, int, int, int, int]:
        geometry = self.screen_geometry()
        all_screens = self._capture_all_screens_png(geometry)
        if all_screens is not None:
            return all_screens

        dxcam = _load_dxcam_module()
        camera = self._camera
        if camera is None:
            # Prefer dxcam's NumPy processor so capture works without a cv2 dependency.
            camera = dxcam.create(output_idx=0, processor_backend="numpy")
            self._camera = camera
        if camera is None:
            raise WindowsComputerUseError(
                "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                (
                    "dxcam could not initialize a Windows capture session. Make sure the "
                    "desktop is unlocked and visible to the same user session running A0 CLI."
                ),
            )

        frame = camera.grab()
        if frame is None:
            raise WindowsComputerUseError(
                "COMPUTER_USE_CAPTURE_UNAVAILABLE",
                (
                    "dxcam did not return a Windows screen frame. This can happen when the "
                    "desktop is locked, a Remote Desktop session is disconnected/minimized, "
                    "or the CLI is isolated from the interactive desktop."
                ),
            )

        from PIL import Image

        image = Image.fromarray(frame)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue(), int(image.width), int(image.height), 0, 0

    def move(self, x: float, y: float) -> None:  # pragma: no cover - only exercised on Windows
        _, mouse = _load_pywinauto_modules()
        mouse.move(coords=(int(round(x)), int(round(y))))

    def click(self, x: float, y: float, *, button: str, count: int) -> None:  # pragma: no cover
        _, mouse = _load_pywinauto_modules()
        coords = (int(round(x)), int(round(y)))
        if count > 1 and button == "left":
            for _ in range(count):
                mouse.double_click(button="left", coords=coords)
            return
        for _ in range(count):
            mouse.click(button=button, coords=coords)

    def scroll(self, dx: int, dy: int) -> None:  # pragma: no cover - only exercised on Windows
        _, mouse = _load_pywinauto_modules()
        if dy:
            mouse.scroll(coords=None, wheel_dist=dy)
        if dx:
            mouse.scroll(coords=None, wheel_dist=dx, horiz=True)

    def key(self, keys: list[str]) -> None:  # pragma: no cover - only exercised on Windows
        keyboard, _ = _load_pywinauto_modules()
        keyboard.send_keys(_format_key_sequence(keys), pause=0.01, with_spaces=True)

    def type_text(self, text: str, *, submit: bool) -> None:  # pragma: no cover - Windows only
        keyboard, _ = _load_pywinauto_modules()
        keyboard.send_keys(text, pause=0.01, with_spaces=True)
        if submit:
            keyboard.send_keys("{ENTER}", pause=0.01, with_spaces=True)

    def uia_roots(self) -> list[Any]:  # pragma: no cover - only exercised on Windows
        try:
            return list(self._desktop.windows())
        except Exception as exc:
            raise WindowsComputerUseError(
                "COMPUTER_USE_UIA_UNAVAILABLE",
                (
                    "Windows UI Automation roots are unavailable. Make sure the A0 CLI "
                    "is running in the interactive desktop session and is not isolated "
                    "from the target UI by UAC elevation, services, or a disconnected "
                    "Remote Desktop session."
                ),
            ) from exc


def _normalize_key_token(key: str) -> str:
    aliases = {
        "alt": "ALT",
        "ctrl": "CTRL",
        "control": "CTRL",
        "delete": "DELETE",
        "down": "DOWN",
        "enter": "ENTER",
        "esc": "ESC",
        "escape": "ESC",
        "left": "LEFT",
        "pagedown": "PGDN",
        "pageup": "PGUP",
        "pgdn": "PGDN",
        "pgup": "PGUP",
        "right": "RIGHT",
        "shift": "SHIFT",
        "space": "SPACE",
        "super": "WIN",
        "tab": "TAB",
        "up": "UP",
        "backspace": "BACKSPACE",
    }
    cleaned = str(key or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) == 1 and cleaned.isprintable():
        return cleaned
    return aliases.get(cleaned.lower(), cleaned.upper())


def _format_key_sequence(keys: list[str]) -> str:
    normalized = [_normalize_key_token(key) for key in keys if _normalize_key_token(key)]
    if not normalized:
        raise WindowsComputerUseError("COMPUTER_USE_KEYS_REQUIRED", "key requires a non-empty keys list.")
    if len(normalized) == 1:
        token = normalized[0]
        return token if len(token) == 1 else f"{{{token}}}"

    modifiers = normalized[:-1]
    body = normalized[-1]
    prefix = "".join(f"{{{modifier} down}}" for modifier in modifiers)
    suffix = "".join(f"{{{modifier} up}}" for modifier in reversed(modifiers))
    if len(body) == 1:
        return prefix + body + suffix
    return prefix + f"{{{body}}}" + suffix


def _bounded_text(value: Any, *, limit: int = _UIA_TEXT_LIMIT) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _uia_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return _bounded_text(value.decode("utf-8", errors="replace"))
    return _bounded_text(value)


def _normalize_uia_path(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                loaded = []
            return _normalize_uia_path(loaded)
        parts = [part for part in text.replace(".", "/").split("/") if part.strip()]
        return [int(part) for part in parts]
    if isinstance(value, (list, tuple)):
        return [int(part) for part in value]
    raise ValueError("UIA path must be a list of integers or a slash-delimited string.")


def _call_noarg(target: Any, name: str) -> Any:
    member = getattr(target, name, None)
    if not callable(member):
        return None
    try:
        return member()
    except Exception:
        return None


def _uia_info(element: Any) -> Any:
    return getattr(element, "element_info", element)


def _read_attr(target: Any, name: str) -> Any:
    value = getattr(target, name, None)
    if callable(value):
        try:
            return value()
        except Exception:
            return None
    return value


def _read_first_attr(*sources: Any, names: tuple[str, ...]) -> Any:
    for source in sources:
        if source is None:
            continue
        for name in names:
            value = _read_attr(source, name)
            if value is not None and value != "":
                return value
    return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rect_value(rect: Any, name: str) -> float | None:
    return _float_or_none(_read_attr(rect, name))


def _uia_frame_from_rect(rect: Any, geometry: ScreenGeometry) -> dict[str, Any] | None:
    if rect is None:
        return None

    x = y = width = height = None
    if isinstance(rect, dict):
        x = _float_or_none(rect.get("x", rect.get("left")))
        y = _float_or_none(rect.get("y", rect.get("top")))
        width = _float_or_none(rect.get("width"))
        height = _float_or_none(rect.get("height"))
        right = _float_or_none(rect.get("right"))
        bottom = _float_or_none(rect.get("bottom"))
        if width is None and x is not None and right is not None:
            width = right - x
        if height is None and y is not None and bottom is not None:
            height = bottom - y
    elif isinstance(rect, (list, tuple)) and len(rect) >= 4:
        x = _float_or_none(rect[0])
        y = _float_or_none(rect[1])
        third = _float_or_none(rect[2])
        fourth = _float_or_none(rect[3])
        if third is not None and fourth is not None:
            width = third
            height = fourth
    else:
        x = _rect_value(rect, "left")
        y = _rect_value(rect, "top")
        right = _rect_value(rect, "right")
        bottom = _rect_value(rect, "bottom")
        width = _rect_value(rect, "width")
        height = _rect_value(rect, "height")
        if width is None and x is not None and right is not None:
            width = right - x
        if height is None and y is not None and bottom is not None:
            height = bottom - y

    if x is None or y is None or width is None or height is None:
        return None
    if width <= 0 or height <= 0:
        return None

    frame: dict[str, Any] = {
        "x": round(x, 2),
        "y": round(y, 2),
        "width": round(width, 2),
        "height": round(height, 2),
    }
    if geometry.width > 0 and geometry.height > 0:
        frame["normalized"] = {
            "x": round((x - geometry.origin_x) / geometry.width, 6),
            "y": round((y - geometry.origin_y) / geometry.height, 6),
            "width": round(width / geometry.width, 6),
            "height": round(height / geometry.height, 6),
        }
    return frame


def _uia_children(element: Any) -> list[Any]:
    for method_name in ("children", "iter_children"):
        method = getattr(element, method_name, None)
        if not callable(method):
            continue
        try:
            children = method()
        except Exception:
            continue
        try:
            return list(children)
        except TypeError:
            return []

    info = _uia_info(element)
    for method_name in ("children", "iter_children"):
        method = getattr(info, method_name, None)
        if not callable(method):
            continue
        try:
            children = method()
        except Exception:
            continue
        try:
            return list(children)
        except TypeError:
            return []
    return []


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None


def _uia_native_handle(element: Any) -> int | None:
    info = _uia_info(element)
    value = _read_first_attr(
        info,
        element,
        names=("handle", "hwnd", "native_window_handle", "native_handle"),
    )
    handle = _int_or_none(value)
    if handle is None or handle <= 0:
        return None
    return handle


def _uia_role_text(element: Any, summary: dict[str, Any] | None = None) -> str:
    if summary is not None:
        role = str(summary.get("role") or "").strip()
        if role:
            return role
    info = _uia_info(element)
    role = _read_first_attr(info, element, names=("control_type", "friendly_class_name"))
    return str(role or "").strip()


def _uia_is_window_like(element: Any, summary: dict[str, Any] | None = None) -> bool:
    role = _uia_role_text(element, summary).lower()
    if role in {"window", "dialog"}:
        return True
    path = summary.get("path") if isinstance(summary, dict) else None
    if isinstance(path, list) and len(path) == 1 and _uia_native_handle(element) is not None:
        return True
    return False


def _uia_parent(element: Any) -> Any | None:
    for target in (element, _uia_info(element)):
        if target is None:
            continue
        for name in ("parent", "parent_element"):
            value = _read_attr(target, name)
            if value is not None and value is not element:
                return value
    return None


def _uia_window_element(element: Any) -> Any:
    top_level_parent = getattr(element, "top_level_parent", None)
    if callable(top_level_parent):
        with contextlib.suppress(Exception):
            parent = top_level_parent()
            if parent is not None:
                return parent

    candidate = element
    seen: set[int] = set()
    for _ in range(32):
        marker = id(candidate)
        if marker in seen:
            break
        seen.add(marker)
        parent = _uia_parent(candidate)
        if parent is None:
            break
        candidate = parent
    return candidate


def _uia_selector_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.search(r"\s|['\":&>]", text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _uia_selector_from_summary(summary: dict[str, Any]) -> str:
    parts: list[str] = []
    role = _uia_selector_value(summary.get("role"))
    if role:
        parts.append(f"role:{role}")
    automation_id = _uia_selector_value(summary.get("automation_id"))
    if automation_id:
        parts.append(f"id:{automation_id}")
    title = _uia_selector_value(summary.get("title"))
    if title:
        parts.append(f"name:{title}")
    class_name = _uia_selector_value(summary.get("class_name"))
    if class_name:
        parts.append(f"classname:{class_name}")
    return " && ".join(parts)


def _parse_uia_selector(selector: Any) -> dict[str, str]:
    text = str(selector or "").strip()
    if not text:
        return {}
    segment = text.split(">>")[-1].strip()
    if not segment:
        return {}

    mapping = {
        "automationid": "automation_id",
        "automation_id": "automation_id",
        "class": "class_name",
        "classname": "class_name",
        "class_name": "class_name",
        "controltype": "role",
        "framework": "framework_id",
        "frameworkid": "framework_id",
        "framework_id": "framework_id",
        "handle": "handle",
        "hwnd": "handle",
        "id": "automation_id",
        "name": "title",
        "nativeid": "handle",
        "native_id": "handle",
        "process": "process_id",
        "processid": "process_id",
        "process_id": "process_id",
        "role": "role",
        "text": "title",
        "title": "title",
    }
    fields: dict[str, str] = {}
    for part in re.split(r"\s*&&\s*", segment):
        key, separator, value = part.partition(":")
        if not separator:
            continue
        normalized_key = re.sub(r"[\s_-]+", "", key.strip().lower())
        target_key = mapping.get(normalized_key)
        if not target_key:
            continue
        cleaned = value.strip().strip("'\"")
        if cleaned:
            fields[target_key] = cleaned.replace('\\"', '"').replace("\\\\", "\\")
    return fields


def _expand_uia_target(target: dict[str, Any]) -> dict[str, Any]:
    expanded = dict(target)
    for key, value in _parse_uia_selector(expanded.get("selector")).items():
        expanded.setdefault(key, value)
    return expanded


def _windows_show_window(handle: Any, command: int) -> bool:
    hwnd = _int_or_none(handle)
    if hwnd is None or hwnd <= 0 or sys.platform != "win32":
        return False
    try:  # pragma: no cover - only exercised on Windows
        import ctypes

        ctypes.windll.user32.ShowWindow(ctypes.c_void_p(hwnd), command)
        return True
    except Exception:
        return False


def _windows_set_foreground(handle: Any) -> bool:
    hwnd = _int_or_none(handle)
    if hwnd is None or hwnd <= 0 or sys.platform != "win32":
        return False
    try:  # pragma: no cover - only exercised on Windows
        import ctypes

        user32 = ctypes.windll.user32
        user32.ShowWindow(ctypes.c_void_p(hwnd), _SW_RESTORE)
        user32.BringWindowToTop(ctypes.c_void_p(hwnd))
        return bool(user32.SetForegroundWindow(ctypes.c_void_p(hwnd)))
    except Exception:
        return False


def _uia_operation_error(operation: str) -> WindowsComputerUseError:
    return WindowsComputerUseError(
        "COMPUTER_USE_UIA_ACTION_UNSUPPORTED",
        f"Windows UIA element does not support operation {operation!r}.",
    )


def _load_default_driver() -> _WindowsDesktopAutomation:
    if not windows_backend_supported():  # pragma: no cover - only exercised on Windows
        raise WindowsComputerUseError("COMPUTER_USE_UNSUPPORTED", windows_backend_support_reason())
    return _WindowsDesktopAutomation()


@dataclass
class _RuntimeSession:
    session: WindowsSession
    policy: TrustModePolicy


class WindowsComputerUseRuntime:
    def __init__(
        self,
        *,
        driver: Any | None = None,
        store: WindowsSessionStore | None = None,
        state_dir: str | os.PathLike[str] | None = None,
        capture_debug_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self._driver = driver or _load_default_driver()
        self._store = store or WindowsSessionStore(state_dir=state_dir)
        self._capture_debug_dir = (
            Path(capture_debug_dir)
            if capture_debug_dir is not None
            else _default_capture_debug_dir()
        )
        self._session: _RuntimeSession | None = None
        self._element_index_cache: dict[int, dict[str, Any]] = {}

    @property
    def supported(self) -> bool:
        return windows_backend_supported()

    def hello_metadata(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "backend_id": WINDOWS_BACKEND_ID,
            "backend_family": WINDOWS_BACKEND_FAMILY,
            "features": list(WINDOWS_BACKEND_FEATURES),
            **_backend_contract_metadata(),
            "support_reason": windows_backend_support_reason(),
        }

    def _screen_geometry(self) -> ScreenGeometry:
        screen_geometry = getattr(self._driver, "screen_geometry", None)
        if callable(screen_geometry):
            geometry = screen_geometry()
            if isinstance(geometry, ScreenGeometry):
                return geometry
            if isinstance(geometry, dict):
                return ScreenGeometry(
                    origin_x=coerce_int(geometry.get("origin_x"), name="origin_x", default=0),
                    origin_y=coerce_int(geometry.get("origin_y"), name="origin_y", default=0),
                    width=coerce_int(geometry.get("width"), name="width", default=0),
                    height=coerce_int(geometry.get("height"), name="height", default=0),
                )
            if isinstance(geometry, (list, tuple)) and len(geometry) >= 4:
                return ScreenGeometry(
                    origin_x=coerce_int(geometry[0], name="origin_x"),
                    origin_y=coerce_int(geometry[1], name="origin_y"),
                    width=coerce_int(geometry[2], name="width"),
                    height=coerce_int(geometry[3], name="height"),
                )

        width, height = self._driver.screen_size()
        return ScreenGeometry(origin_x=0, origin_y=0, width=int(width), height=int(height))

    def _capture_png(
        self,
        *,
        fallback_origin_x: int,
        fallback_origin_y: int,
    ) -> tuple[bytes, ScreenGeometry]:
        captured = self._driver.capture_png()
        if isinstance(captured, (list, tuple)) and len(captured) >= 5:
            png_bytes, width, height, origin_x, origin_y = captured[:5]
            return bytes(png_bytes), ScreenGeometry(
                origin_x=coerce_int(origin_x, name="origin_x"),
                origin_y=coerce_int(origin_y, name="origin_y"),
                width=coerce_int(width, name="width"),
                height=coerce_int(height, name="height"),
            )
        if isinstance(captured, (list, tuple)) and len(captured) >= 3:
            png_bytes, width, height = captured[:3]
            return bytes(png_bytes), ScreenGeometry(
                origin_x=fallback_origin_x,
                origin_y=fallback_origin_y,
                width=coerce_int(width, name="width"),
                height=coerce_int(height, name="height"),
            )
        raise WindowsComputerUseError(
            "COMPUTER_USE_CAPTURE_UNAVAILABLE",
            "Windows capture backend returned an invalid screen frame.",
        )

    @staticmethod
    def _to_screen_pixels(session: WindowsSession, x: float, y: float) -> tuple[float, float]:
        return session.origin_x + (session.width * x), session.origin_y + (session.height * y)

    def status(self, params: dict[str, Any]) -> dict[str, Any]:
        context_id = normalize_context_id(params.get("context_id"))
        if self._session is not None and self._session.session.context_id == context_id:
            payload = self._session.session.to_payload(reused=False)
            payload["active"] = True
            payload["status"] = "active"
            return payload

        stored = self._store.get(context_id)
        if stored is not None:
            payload = stored.to_payload(reused=False)
            payload["active"] = bool(stored.active)
            payload["status"] = "active" if stored.active else "stopped"
            return payload

        return {
            "active": False,
            "context_id": context_id,
            "backend_id": WINDOWS_BACKEND_ID,
            "backend_family": WINDOWS_BACKEND_FAMILY,
            "features": list(WINDOWS_BACKEND_FEATURES),
            **_backend_contract_metadata(),
            "support_reason": windows_backend_support_reason(),
        }

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "start_session": self.start_session,
            "status": self.status,
            "capture": self.capture,
            "list_windows": self.list_windows,
            "get_window_state": self.get_window_state,
            "element_action": self.element_action,
            "uia_snapshot": self.uia_snapshot,
            "uia_action": self.uia_action,
            "move": self.move,
            "click": self.click,
            "scroll": self.scroll,
            "key": self.key,
            "type": self.type_text,
            "stop_session": self.stop_session,
        }
        handler = handlers.get(str(method or "").strip().lower())
        if handler is None:
            raise WindowsComputerUseError(
                "UNKNOWN_METHOD",
                f"Unknown computer-use helper method: {method}",
            )
        normalized_method = str(method or "").strip().lower()
        normalized_params = dict(params)
        if normalized_method in {"capture", "uia_snapshot", "uia_action", "move", "click", "scroll", "key", "type"}:
            normalized_params = normalize_action_payload(
                normalized_method,
                normalized_params,
                context_id=normalize_context_id(normalized_params.get("context_id")),
            )
        return handler(normalized_params)

    def start_session(self, params: dict[str, Any]) -> dict[str, Any]:
        trust_mode = str(params.get("trust_mode") or "persistent").strip().lower()
        context_id = normalize_context_id(params.get("context_id"))
        restore_token = normalize_restore_token(params.get("restore_token"))
        policy = resolve_trust_mode_policy(trust_mode, restore_token)

        if policy.trust_mode not in WINDOWS_TRUST_MODES:
            raise WindowsComputerUseError(
                "COMPUTER_USE_UNSUPPORTED",
                f"Unsupported trust mode: {trust_mode!r}",
            )
        if policy.trust_mode == "allow" and not policy.reuse_allowed:
            raise WindowsComputerUseError(
                "COMPUTER_USE_REARM_REQUIRED",
                "Allow requires a stored restore token.",
            )

        if self._session is not None and self._session.session.context_id == context_id:
            self._session.session.active = True
            return self._session.session.to_payload(reused=False)

        reusable = self._store.get(context_id)
        if reusable is not None and policy.reuse_allowed and reusable.restore_token == restore_token:
            reusable = WindowsSession(
                context_id=context_id,
                session_id=reusable.session_id,
                trust_mode=policy.trust_mode,
                restore_token=reusable.restore_token,
                active=True,
                width=reusable.width,
                height=reusable.height,
                origin_x=reusable.origin_x,
                origin_y=reusable.origin_y,
            )
            self._session = _RuntimeSession(session=reusable, policy=policy)
            self._store.put(reusable)
            return reusable.to_payload(reused=True)

        geometry = self._screen_geometry()
        session = WindowsSession(
            context_id=context_id,
            session_id=uuid.uuid4().hex,
            trust_mode=policy.trust_mode,
            restore_token=restore_token if policy.persist_metadata else "",
            active=True,
            width=geometry.width,
            height=geometry.height,
            origin_x=geometry.origin_x,
            origin_y=geometry.origin_y,
        )
        if policy.persist_metadata and not session.restore_token:
            session = WindowsSession(
                context_id=session.context_id,
                session_id=session.session_id,
                trust_mode=session.trust_mode,
                restore_token=str(uuid.uuid4()),
                active=session.active,
                width=session.width,
                height=session.height,
                origin_x=session.origin_x,
                origin_y=session.origin_y,
            )
        self._session = _RuntimeSession(session=session, policy=policy)
        if policy.persist_metadata:
            self._store.put(session)
        return session.to_payload(reused=False)

    def capture(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        png_bytes, geometry = self._capture_png(
            fallback_origin_x=session.session.origin_x,
            fallback_origin_y=session.session.origin_y,
        )
        session.session.width = geometry.width
        session.session.height = geometry.height
        session.session.origin_x = geometry.origin_x
        session.session.origin_y = geometry.origin_y
        session.session.updated_at = time.time()
        if session.policy.persist_metadata:
            self._store.put(session.session)

        result = {
            "session_id": session.session.session_id,
            "context_id": session.session.context_id,
            "width": geometry.width,
            "height": geometry.height,
            "origin_x": geometry.origin_x,
            "origin_y": geometry.origin_y,
            "captured_at": time.time(),
        }
        capture_path_value = str(params.get("capture_path") or "").strip()
        if capture_path_value:
            capture_path = Path(capture_path_value)
            capture_path.parent.mkdir(parents=True, exist_ok=True)
            capture_path.write_bytes(png_bytes)
            result["capture_path"] = str(capture_path)
        elif self._capture_debug_dir is not None:
            debug_path = self._capture_debug_dir / safe_context_segment(session.session.context_id)
            debug_path.mkdir(parents=True, exist_ok=True)
            filename = f"{uuid.uuid4().hex}.png"
            capture_path = debug_path / filename
            capture_path.write_bytes(png_bytes)
            result["capture_path"] = str(capture_path)
            result["png_base64"] = base64.b64encode(png_bytes).decode("ascii")
        else:
            result["png_base64"] = base64.b64encode(png_bytes).decode("ascii")
        return result

    def list_windows(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        max_windows = max(1, coerce_int(params.get("max_windows"), name="max_windows", default=80))
        include_hidden = coerce_bool(params.get("include_hidden"), default=False)
        include_offscreen = coerce_bool(params.get("include_offscreen"), default=False)
        roots = self._uia_roots()
        geometry = ScreenGeometry(
            origin_x=session.session.origin_x,
            origin_y=session.session.origin_y,
            width=session.session.width,
            height=session.session.height,
        )
        windows: list[dict[str, Any]] = []
        for index, root in enumerate(roots):
            summary = self._uia_target_summary(root, path=[index], geometry=geometry)
            if not include_hidden and summary.get("visible") is False:
                continue
            if not include_offscreen and self._uia_summary_is_offscreen(summary):
                continue
            window_id = self._window_id_for_summary(summary, path=[index])
            windows.append(
                {
                    "window_id": window_id,
                    "pid": summary.get("process_id"),
                    "app_name": summary.get("class_name") or summary.get("framework_id"),
                    "title": summary.get("title"),
                    "role": summary.get("role", "Window"),
                    "frame": summary.get("frame"),
                    "visible": summary.get("visible"),
                    "focused": summary.get("focused"),
                    "path": [index],
                }
            )
            if len(windows) >= max_windows:
                break
        return {
            "session_id": session.session.session_id,
            "context_id": session.session.context_id,
            "backend": "uia",
            "count": len(windows),
            "windows": windows,
        }

    def get_window_state(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        max_depth = min(
            _UIA_HARD_MAX_DEPTH,
            max(0, coerce_int(params.get("max_depth"), name="max_depth", default=_UIA_DEFAULT_MAX_DEPTH)),
        )
        max_nodes = min(
            _UIA_HARD_MAX_NODES,
            max(1, coerce_int(params.get("max_nodes"), name="max_nodes", default=_UIA_DEFAULT_MAX_NODES)),
        )
        roots = self._uia_roots()
        geometry = ScreenGeometry(
            origin_x=session.session.origin_x,
            origin_y=session.session.origin_y,
            width=session.session.width,
            height=session.session.height,
        )
        element, path, window_summary = self._resolve_window_root(params, roots=roots, geometry=geometry)
        budget = {"count": 0, "truncated": False}
        tree = self._serialize_uia_element(
            element,
            path=path,
            depth=0,
            max_depth=max_depth,
            max_nodes=max_nodes,
            budget=budget,
            geometry=geometry,
        ) or {}
        window_id = self._window_id_for_summary(window_summary, path=path)
        self._cache_element_indices(tree, window_id=window_id)
        window_summary["window_id"] = window_id
        return {
            "session_id": session.session.session_id,
            "context_id": session.session.context_id,
            "backend": "uia",
            "mode": str(params.get("mode") or "uia").strip() or "uia",
            "window_id": window_id,
            "window": window_summary,
            "app": {"name": "Windows desktop", "backend": "uia"},
            "tree": tree,
            "node_count": budget["count"],
            "truncated": bool(budget["truncated"]),
            "max_depth": max_depth,
            "max_nodes": max_nodes,
        }

    def element_action(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        dispatch = str(params.get("dispatch") or "background").strip().lower()
        if dispatch not in {"background", "foreground", "auto"}:
            raise WindowsComputerUseError(
                "COMPUTER_USE_BAD_DISPATCH",
                "element_action dispatch must be background, foreground, or auto.",
            )
        operation = str(params.get("operation") or params.get("name") or "invoke").strip().lower()
        if operation in {"press", "activate"}:
            operation = "invoke"
        if operation in {"type", "type_text"}:
            operation = "set_value"
        operation = _WINDOW_OPERATION_ALIASES.get(operation, operation)
        element, target = self._resolve_element_action_target(params)
        target.setdefault("element_index", params.get("element_index"))

        if dispatch in {"background", "auto"}:
            background_result = self._try_background_uia_action(
                element,
                target=target,
                operation=operation,
                params=params,
                session_id=session.session.session_id,
                context_id=session.session.context_id,
                requested_dispatch=dispatch,
            )
            if not background_result.get("background_unavailable"):
                return background_result
            if dispatch == "background":
                return background_result

        foreground_params = {
            **params,
            "operation": operation,
            "path": target.get("path"),
        }
        foreground_result = self.uia_action(foreground_params)
        foreground_result["requested_dispatch"] = dispatch
        foreground_result["actual_dispatch"] = "foreground"
        foreground_result["foreground_fallback_used"] = dispatch == "auto"
        return foreground_result

    def uia_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        max_depth = min(
            _UIA_HARD_MAX_DEPTH,
            max(0, coerce_int(params.get("max_depth"), name="max_depth", default=_UIA_DEFAULT_MAX_DEPTH)),
        )
        max_nodes = min(
            _UIA_HARD_MAX_NODES,
            max(1, coerce_int(params.get("max_nodes"), name="max_nodes", default=_UIA_DEFAULT_MAX_NODES)),
        )
        roots = self._uia_roots()
        geometry = ScreenGeometry(
            origin_x=session.session.origin_x,
            origin_y=session.session.origin_y,
            width=session.session.width,
            height=session.session.height,
        )
        budget = {"count": 1, "truncated": False}
        tree: dict[str, Any] = {
            "path": [],
            "role": "Desktop",
            "title": "Windows desktop",
            "frame": {
                "x": geometry.origin_x,
                "y": geometry.origin_y,
                "width": geometry.width,
                "height": geometry.height,
                "normalized": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            },
        }
        children: list[dict[str, Any]] = []
        for index, root in enumerate(roots):
            child_node = self._serialize_uia_element(
                root,
                path=[index],
                depth=1,
                max_depth=max_depth,
                max_nodes=max_nodes,
                budget=budget,
                geometry=geometry,
            )
            if child_node is not None:
                children.append(child_node)
            if bool(budget.get("truncated")):
                break
        if children:
            tree["children"] = children
        return {
            "session_id": session.session.session_id,
            "context_id": session.session.context_id,
            "app": {"name": "Windows desktop", "backend": "uia"},
            "tree": tree,
            "node_count": budget["count"],
            "truncated": bool(budget["truncated"]),
            "max_depth": max_depth,
            "max_nodes": max_nodes,
        }

    def uia_action(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        operation = str(
            params.get("operation")
            or params.get("uia_action")
            or params.get("name")
            or "invoke"
        ).strip().lower()
        if operation in {"press", "activate"}:
            operation = "invoke"
        if operation in {"type", "type_text"}:
            operation = "set_value"
        operation = _WINDOW_OPERATION_ALIASES.get(operation, operation)
        allowed_operations = {"invoke", "click", "focus", "set_value", *_WINDOW_OPERATIONS}
        if operation not in allowed_operations:
            raise WindowsComputerUseError(
                "COMPUTER_USE_BAD_UIA_ACTION",
                "uia_action operation must be one of: invoke, click, focus, set_value, "
                "focus_window, minimize, restore, maximize, close.",
            )

        element, target = self._resolve_uia_target(params)
        if operation == "invoke":
            self._invoke_uia_element(element)
        elif operation == "click":
            self._click_uia_element(element)
        elif operation == "focus":
            self._focus_uia_element(element)
        elif operation in _WINDOW_OPERATIONS:
            self._window_uia_element_action(element, operation)
        else:
            value = params.get("value", params.get("text"))
            if value is None:
                raise WindowsComputerUseError(
                    "COMPUTER_USE_UIA_VALUE_REQUIRED",
                    "uia_action set_value requires value or text.",
                )
            self._set_uia_element_value(element, str(value), submit=coerce_bool(params.get("submit")))

        return {
            "session_id": session.session.session_id,
            "context_id": session.session.context_id,
            "operation": operation,
            "target": target,
        }

    def move(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        x = float(params.get("x"))
        y = float(params.get("y"))
        pixel_x, pixel_y = self._to_screen_pixels(session.session, x, y)
        self._driver.move(pixel_x, pixel_y)
        return {
            "session_id": session.session.session_id,
            "x": x,
            "y": y,
            "pixel_x": pixel_x,
            "pixel_y": pixel_y,
        }

    def click(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        x = float(params.get("x", 0.5))
        y = float(params.get("y", 0.5))
        button_name = str(params.get("button") or "left").strip().lower()
        count = max(1, int(params.get("count") or 1))
        pixel_x, pixel_y = self._to_screen_pixels(session.session, x, y)
        self._driver.click(pixel_x, pixel_y, button=button_name, count=count)
        return {
            "session_id": session.session.session_id,
            "button": button_name,
            "count": count,
            "x": x,
            "y": y,
            "pixel_x": pixel_x,
            "pixel_y": pixel_y,
        }

    def scroll(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        dx = int(params.get("dx") or 0)
        dy = int(params.get("dy") or 0)
        self._driver.scroll(dx, dy)
        return {
            "session_id": session.session.session_id,
            "dx": dx,
            "dy": dy,
        }

    def key(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        keys = params.get("keys")
        if not isinstance(keys, list) or not keys:
            raise WindowsComputerUseError(
                "COMPUTER_USE_KEYS_REQUIRED",
                "key requires a non-empty keys list.",
            )
        normalized = [str(item).strip() for item in keys if str(item).strip()]
        if not normalized:
            raise WindowsComputerUseError(
                "COMPUTER_USE_KEYS_REQUIRED",
                "key requires a non-empty keys list.",
            )
        self._driver.key(normalized)
        return {
            "session_id": session.session.session_id,
            "keys": normalized,
        }

    def type_text(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._require_session(params)
        text = str(params.get("text") or "")
        submit = coerce_bool(params.get("submit"))
        if not text:
            raise WindowsComputerUseError(
                "COMPUTER_USE_TEXT_REQUIRED",
                "type requires text.",
            )
        self._driver.type_text(text, submit=submit)
        return {
            "session_id": session.session.session_id,
            "text": text,
            "submitted": submit,
        }

    def stop_session(self, params: dict[str, Any]) -> dict[str, Any]:
        context_id = normalize_context_id(params.get("context_id"))
        session = self._session
        if session is not None and session.session.context_id == context_id:
            session.session.active = False
            session.session.updated_at = time.time()
            if session.policy.persist_metadata:
                self._store.put(session.session)
            self._session = None
        return {"active": False, "status": "stopped", "session_id": ""}

    def _uia_roots(self) -> list[Any]:
        roots_method = getattr(self._driver, "uia_roots", None)
        if callable(roots_method):
            try:
                return list(roots_method())
            except WindowsComputerUseError:
                raise
            except Exception as exc:
                raise WindowsComputerUseError(
                    "COMPUTER_USE_UIA_UNAVAILABLE",
                    "Windows UI Automation roots are unavailable.",
                ) from exc
        raise WindowsComputerUseError(
            "COMPUTER_USE_UIA_UNAVAILABLE",
            "The Windows backend does not expose UI Automation roots.",
        )

    def _serialize_uia_element(
        self,
        element: Any,
        *,
        path: list[int],
        depth: int,
        max_depth: int,
        max_nodes: int,
        budget: dict[str, Any],
        geometry: ScreenGeometry,
    ) -> dict[str, Any] | None:
        if int(budget.get("count") or 0) >= max_nodes:
            budget["truncated"] = True
            return None
        budget["count"] = int(budget.get("count") or 0) + 1

        node = self._uia_target_summary(element, path=path, geometry=geometry)
        if depth >= max_depth:
            return node

        children: list[dict[str, Any]] = []
        for index, child in enumerate(_uia_children(element)):
            child_node = self._serialize_uia_element(
                child,
                path=[*path, index],
                depth=depth + 1,
                max_depth=max_depth,
                max_nodes=max_nodes,
                budget=budget,
                geometry=geometry,
            )
            if child_node is not None:
                children.append(child_node)
            if bool(budget.get("truncated")):
                break
        if children:
            node["children"] = children
        return node

    def _uia_target_summary(
        self,
        element: Any,
        *,
        path: list[int],
        geometry: ScreenGeometry,
    ) -> dict[str, Any]:
        info = _uia_info(element)
        summary: dict[str, Any] = {"path": list(path)}
        field_specs = (
            ("role", ("control_type", "friendly_class_name")),
            ("title", ("name", "window_text")),
            ("value", ("value",)),
            ("automation_id", ("automation_id",)),
            ("class_name", ("class_name",)),
            ("framework_id", ("framework_id",)),
            ("process_id", ("process_id",)),
            ("handle", ("handle",)),
            ("enabled", ("enabled",)),
            ("visible", ("visible",)),
        )
        for output_key, names in field_specs:
            value = _read_first_attr(info, element, names=names)
            if value is None or value == "":
                continue
            summary[output_key] = _uia_scalar(value)

        focused = _call_noarg(element, "has_keyboard_focus")
        if focused is not None:
            summary["focused"] = bool(focused)

        frame = _uia_frame_from_rect(_read_first_attr(info, element, names=("rectangle",)), geometry)
        if frame:
            summary["frame"] = frame

        actions = self._uia_available_actions(element, summary)
        if actions:
            summary["actions"] = actions
        selector = _uia_selector_from_summary(summary)
        if selector:
            summary["selector"] = selector
        return summary

    def _window_id_for_summary(self, summary: dict[str, Any], *, path: list[int]) -> str:
        handle = summary.get("handle")
        if handle not in (None, ""):
            return f"uia-hwnd:{handle}"
        return "uia-path:" + ".".join(str(item) for item in path)

    def _uia_summary_is_offscreen(self, summary: dict[str, Any]) -> bool:
        frame = summary.get("frame")
        if not isinstance(frame, dict):
            return False
        normalized = frame.get("normalized")
        if not isinstance(normalized, dict):
            return False
        x = _float_or_none(normalized.get("x"))
        y = _float_or_none(normalized.get("y"))
        width = _float_or_none(normalized.get("width"))
        height = _float_or_none(normalized.get("height"))
        if x is None or y is None or width is None or height is None:
            return False
        return x + width <= 0 or y + height <= 0 or x >= 1 or y >= 1

    def _parse_window_path(self, window_id: str) -> list[int] | None:
        value = str(window_id or "").strip()
        if not value:
            return None
        if value.startswith("uia-path:"):
            value = value.removeprefix("uia-path:")
        elif value.startswith("path:"):
            value = value.removeprefix("path:")
        else:
            return None
        if not value:
            return []
        path: list[int] = []
        for part in value.replace("/", ".").split("."):
            if not part:
                continue
            try:
                path.append(int(part))
            except ValueError:
                return None
        return path

    def _resolve_window_root(
        self,
        params: dict[str, Any],
        *,
        roots: list[Any],
        geometry: ScreenGeometry,
    ) -> tuple[Any, list[int], dict[str, Any]]:
        window_id = str(params.get("window_id") or "").strip()
        pid = params.get("pid")
        parsed_path = self._parse_window_path(window_id)
        if parsed_path:
            element = self._uia_element_for_path(roots, parsed_path)
            if element is not None:
                summary = self._uia_target_summary(element, path=parsed_path, geometry=geometry)
                return element, parsed_path, summary
        handle_text = window_id.removeprefix("uia-hwnd:") if window_id.startswith("uia-hwnd:") else window_id
        for index, root in enumerate(roots):
            path = [index]
            summary = self._uia_target_summary(root, path=path, geometry=geometry)
            if handle_text and str(summary.get("handle") or "") == handle_text:
                return root, path, summary
            if pid is not None and str(summary.get("process_id") or "") == str(pid):
                return root, path, summary
        if not window_id and pid is None and roots:
            path = [0]
            summary = self._uia_target_summary(roots[0], path=path, geometry=geometry)
            return roots[0], path, summary
        raise WindowsComputerUseError(
            "COMPUTER_USE_WINDOW_NOT_FOUND",
            "No matching Windows UIA top-level window was found.",
        )

    def _cache_element_indices(self, tree: dict[str, Any], *, window_id: str) -> None:
        self._element_index_cache.clear()
        next_index = 0

        def visit(node: dict[str, Any]) -> None:
            nonlocal next_index
            index = next_index
            next_index += 1
            node["element_index"] = index
            self._element_index_cache[index] = {
                "window_id": window_id,
                "path": list(node.get("path") or []),
                "target": dict(node),
            }
            children = node.get("children")
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        visit(child)

        if tree:
            visit(tree)

    def _resolve_element_action_target(self, params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        element_index = params.get("element_index")
        if element_index is not None:
            try:
                index = int(element_index)
            except (TypeError, ValueError) as exc:
                raise WindowsComputerUseError(
                    "COMPUTER_USE_BAD_ELEMENT_INDEX",
                    "element_index must be an integer from the latest get_window_state.",
                ) from exc
            cached = self._element_index_cache.get(index)
            if cached is None:
                raise WindowsComputerUseError(
                    "COMPUTER_USE_ELEMENT_INDEX_STALE",
                    "element_index was not found. Call get_window_state and retry with a fresh index.",
                )
            requested_window_id = str(params.get("window_id") or "").strip()
            cached_window_id = str(cached.get("window_id") or "").strip()
            if requested_window_id and requested_window_id != cached_window_id:
                raise WindowsComputerUseError(
                    "COMPUTER_USE_ELEMENT_WINDOW_MISMATCH",
                    "element_index belongs to a different cached window_id.",
                )
            params = {**params, "path": cached.get("path"), "target": cached.get("target")}
        return self._resolve_uia_target(params)

    def _background_unavailable(
        self,
        *,
        session_id: str,
        context_id: str,
        target: dict[str, Any],
        operation: str,
        reason: str,
        requested_dispatch: str = "background",
    ) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "context_id": context_id,
            "operation": operation,
            "target": target,
            "requested_dispatch": requested_dispatch,
            "actual_dispatch": "none",
            "background_unavailable": True,
            "reason": reason,
        }

    def _try_background_uia_action(
        self,
        element: Any,
        *,
        target: dict[str, Any],
        operation: str,
        params: dict[str, Any],
        session_id: str,
        context_id: str,
        requested_dispatch: str,
    ) -> dict[str, Any]:
        if operation in {"click", "focus", *_WINDOW_OPERATIONS}:
            return self._background_unavailable(
                session_id=session_id,
                context_id=context_id,
                target=target,
                operation=operation,
                reason=f"operation {operation!r} requires foreground window activation on this backend.",
                requested_dispatch=requested_dispatch,
            )
        try:
            if operation == "invoke":
                invoke = getattr(element, "invoke", None)
                if not callable(invoke):
                    raise _uia_operation_error("invoke")
                invoke()
            elif operation == "set_value":
                value = params.get("value", params.get("text"))
                if value is None:
                    raise WindowsComputerUseError(
                        "COMPUTER_USE_UIA_VALUE_REQUIRED",
                        "element_action set_value requires value or text.",
                    )
                if coerce_bool(params.get("submit")):
                    return self._background_unavailable(
                        session_id=session_id,
                        context_id=context_id,
                        target=target,
                        operation=operation,
                        reason="submit requires keyboard input and cannot be guaranteed in background.",
                        requested_dispatch=requested_dispatch,
                    )
                if not self._set_uia_element_value_background(element, str(value)):
                    raise _uia_operation_error("set_value")
            else:
                return self._background_unavailable(
                    session_id=session_id,
                    context_id=context_id,
                    target=target,
                    operation=operation,
                    reason=f"operation {operation!r} is not supported for background dispatch.",
                    requested_dispatch=requested_dispatch,
                )
        except WindowsComputerUseError as exc:
            return self._background_unavailable(
                session_id=session_id,
                context_id=context_id,
                target=target,
                operation=operation,
                reason=str(exc),
                requested_dispatch=requested_dispatch,
            )
        except Exception as exc:
            return self._background_unavailable(
                session_id=session_id,
                context_id=context_id,
                target=target,
                operation=operation,
                reason=str(exc),
                requested_dispatch=requested_dispatch,
            )
        return {
            "session_id": session_id,
            "context_id": context_id,
            "operation": operation,
            "target": target,
            "requested_dispatch": requested_dispatch,
            "actual_dispatch": "background",
            "background_unavailable": False,
        }

    def _set_uia_element_value_background(self, element: Any, value: str) -> bool:
        set_edit_text = getattr(element, "set_edit_text", None)
        if callable(set_edit_text):
            try:
                set_edit_text(value)
                return True
            except Exception:
                pass
        value_pattern = None
        with contextlib.suppress(Exception):
            value_pattern = getattr(element, "iface_value", None)
        set_value = getattr(value_pattern, "SetValue", None) or getattr(value_pattern, "set_value", None)
        if callable(set_value):
            try:
                set_value(value)
                return True
            except Exception:
                pass
        return False

    def _uia_available_actions(self, element: Any, summary: dict[str, Any]) -> list[str]:
        actions: list[str] = []
        is_window_like = _uia_is_window_like(element, summary)
        if is_window_like:
            actions.extend(["focus_window", "minimize", "restore", "maximize"])

        has_invoke = callable(getattr(element, "invoke", None))
        if not is_window_like and has_invoke:
            actions.append("invoke")
        if not is_window_like and not has_invoke and callable(getattr(element, "click_input", None)):
            actions.append("click")
        if not is_window_like and (
            callable(getattr(element, "set_focus", None))
            or callable(getattr(element, "set_keyboard_focus", None))
        ):
            actions.append("focus")

        role = str(summary.get("role") or "").strip().lower()
        if (
            callable(getattr(element, "set_edit_text", None))
            or callable(getattr(element, "type_keys", None))
            or role in {"edit", "document", "combobox", "textbox"}
        ):
            actions.append("set_value")
        return actions

    def _resolve_uia_target(self, params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        target_value = params.get("target")
        target = dict(target_value) if isinstance(target_value, dict) else {}
        if params.get("selector") is not None:
            target["selector"] = params.get("selector")
        target = _expand_uia_target(target)
        path = _normalize_uia_path(params.get("path", target.get("path")))
        roots = self._uia_roots()
        geometry = self._current_geometry()

        if path:
            element = self._uia_element_for_path(roots, path)
            if element is not None:
                summary = self._uia_target_summary(element, path=path, geometry=geometry)
                if self._uia_summary_matches(summary, target, allow_empty=True):
                    return element, summary

        matches = self._find_uia_matches(roots, target=target, geometry=geometry)
        if not matches:
            raise WindowsComputerUseError(
                "COMPUTER_USE_UIA_TARGET_NOT_FOUND",
                "No matching Windows UI Automation element was found.",
            )
        best_score = matches[0][0]
        best = [item for item in matches if item[0] == best_score]
        if len(best) > 1:
            previews = [item[2] for item in best[:5]]
            raise WindowsComputerUseError(
                "COMPUTER_USE_UIA_TARGET_AMBIGUOUS",
                f"UIA target matched {len(best)} elements. Narrow the target. Matches: {previews}",
            )
        _score, element, summary = best[0]
        return element, summary

    def _current_geometry(self) -> ScreenGeometry:
        if self._session is not None:
            session = self._session.session
            return ScreenGeometry(
                origin_x=session.origin_x,
                origin_y=session.origin_y,
                width=session.width,
                height=session.height,
            )
        return self._screen_geometry()

    def _uia_element_for_path(self, roots: list[Any], path: list[int]) -> Any | None:
        if not path:
            return None
        root_index = path[0]
        if root_index < 0 or root_index >= len(roots):
            return None
        element = roots[root_index]
        for index in path[1:]:
            children = _uia_children(element)
            if index < 0 or index >= len(children):
                return None
            element = children[index]
        return element

    def _find_uia_matches(
        self,
        roots: list[Any],
        *,
        target: dict[str, Any],
        geometry: ScreenGeometry,
    ) -> list[tuple[int, Any, dict[str, Any]]]:
        if not any(
            str(target.get(key) or "").strip()
            for key in (
                "role",
                "title",
                "name",
                "text",
                "value",
                "automation_id",
                "identifier",
                "native_id",
                "class_name",
                "framework_id",
                "process_id",
                "handle",
                "selector",
            )
        ):
            raise WindowsComputerUseError(
                "COMPUTER_USE_UIA_TARGET_REQUIRED",
                "uia_action requires path or a semantic target.",
            )

        matches: list[tuple[int, Any, dict[str, Any]]] = []
        queue: list[tuple[Any, list[int], int]] = [
            (element, [index], 0) for index, element in enumerate(roots)
        ]
        visited = 0
        while queue and visited < _UIA_HARD_MAX_NODES:
            element, path, depth = queue.pop(0)
            visited += 1
            summary = self._uia_target_summary(element, path=path, geometry=geometry)
            score = self._uia_match_score(summary, target)
            if score > 0:
                matches.append((score, element, summary))
            if depth >= _UIA_HARD_MAX_DEPTH:
                continue
            for index, child in enumerate(_uia_children(element)):
                queue.append((child, [*path, index], depth + 1))
        return sorted(matches, key=lambda item: item[0], reverse=True)

    def _uia_summary_matches(
        self,
        summary: dict[str, Any],
        target: dict[str, Any],
        *,
        allow_empty: bool = False,
    ) -> bool:
        if allow_empty and not target:
            return True
        return self._uia_match_score(summary, target) > 0

    def _uia_match_score(self, summary: dict[str, Any], target: dict[str, Any]) -> int:
        score = 0
        used = 0
        key_specs = (
            ("automation_id", ("automation_id", "identifier", "native_id"), 90, False),
            ("handle", ("handle",), 100, False),
            ("role", ("role",), 25, False),
            ("class_name", ("class_name",), 20, False),
            ("framework_id", ("framework_id",), 10, False),
            ("process_id", ("process_id",), 10, False),
            ("title", ("title", "name", "text"), 55, True),
            ("value", ("value",), 25, True),
        )
        for actual_key, target_keys, weight, allow_contains in key_specs:
            expected = ""
            for target_key in target_keys:
                expected = str(target.get(target_key) or "").strip().lower()
                if expected:
                    break
            if not expected:
                continue
            used += 1
            actual = str(summary.get(actual_key) or "").strip().lower()
            if not actual:
                return 0
            if actual == expected:
                score += weight
            elif allow_contains and expected in actual:
                score += max(1, weight // 2)
            else:
                return 0
        return score if used else 0

    def _invoke_uia_element(self, element: Any) -> None:
        self._activate_owning_window(element)
        invoke = getattr(element, "invoke", None)
        if callable(invoke):
            try:
                invoke()
                return
            except Exception:
                pass
        raise _uia_operation_error("invoke")

    def _click_uia_element(self, element: Any) -> None:
        self._activate_owning_window(element)
        click_input = getattr(element, "click_input", None)
        if callable(click_input):
            click_input()
            return
        raise _uia_operation_error("click")

    def _focus_uia_element(self, element: Any) -> None:
        self._activate_owning_window(element)
        for method_name in ("set_focus", "set_keyboard_focus"):
            method = getattr(element, method_name, None)
            if not callable(method):
                continue
            try:
                method()
                return
            except Exception:
                continue
        if _uia_is_window_like(element) and self._activate_uia_window(element):
            return
        raise _uia_operation_error("focus")

    def _set_uia_element_value(self, element: Any, value: str, *, submit: bool) -> None:
        self._activate_owning_window(element)
        set_edit_text = getattr(element, "set_edit_text", None)
        if callable(set_edit_text):
            set_edit_text(value)
            if submit:
                self._focus_uia_element(element)
                self._driver.key(["enter"])
            return

        value_pattern = None
        with contextlib.suppress(Exception):
            value_pattern = getattr(element, "iface_value", None)
        set_value = getattr(value_pattern, "SetValue", None) or getattr(value_pattern, "set_value", None)
        if callable(set_value):
            set_value(value)
            if submit:
                self._focus_uia_element(element)
                self._driver.key(["enter"])
            return

        self._focus_uia_element(element)
        self._driver.type_text(value, submit=submit)

    def _activate_owning_window(self, element: Any) -> bool:
        window = _uia_window_element(element)
        activated = self._activate_uia_window(window)
        if window is not element:
            return activated
        return activated

    def _activate_uia_window(self, window: Any) -> bool:
        ok = False
        for method_name in ("restore", "show"):
            method = getattr(window, method_name, None)
            if not callable(method):
                continue
            try:
                method()
                ok = True
                break
            except Exception:
                continue

        ok = self._set_uia_window_visual_state(window, "restore") or ok

        handle = _uia_native_handle(window)
        if handle is not None:
            ok = _windows_show_window(handle, _SW_RESTORE) or ok
            ok = _windows_set_foreground(handle) or ok

        for method_name in ("set_focus", "set_keyboard_focus"):
            method = getattr(window, method_name, None)
            if not callable(method):
                continue
            try:
                method()
                ok = True
                break
            except Exception:
                continue
        return ok

    def _set_uia_window_visual_state(self, window: Any, state_name: str) -> bool:
        state = _WINDOW_VISUAL_STATES.get(state_name)
        if state is None:
            return False
        pattern = None
        with contextlib.suppress(Exception):
            pattern = getattr(window, "iface_window", None)
        if pattern is None:
            return False
        for method_name in ("SetWindowVisualState", "set_window_visual_state"):
            method = getattr(pattern, method_name, None)
            if not callable(method):
                continue
            try:
                method(state)
                return True
            except Exception:
                continue
        return False

    def _window_uia_element_action(self, element: Any, operation: str) -> None:
        window = _uia_window_element(element)
        if operation == "focus_window":
            if self._activate_uia_window(window):
                return
            raise _uia_operation_error(operation)

        if operation == "minimize":
            method = getattr(window, "minimize", None)
            if callable(method):
                try:
                    method()
                    return
                except Exception:
                    pass
            if self._set_uia_window_visual_state(window, "minimize"):
                return
            if _windows_show_window(_uia_native_handle(window), _SW_MINIMIZE):
                return
            raise _uia_operation_error(operation)

        if operation == "restore":
            if self._activate_uia_window(window):
                return
            raise _uia_operation_error(operation)

        if operation == "maximize":
            method = getattr(window, "maximize", None)
            if callable(method):
                try:
                    method()
                    return
                except Exception:
                    pass
            if self._set_uia_window_visual_state(window, "maximize"):
                return
            if _windows_show_window(_uia_native_handle(window), _SW_MAXIMIZE):
                return
            raise _uia_operation_error(operation)

        if operation == "close":
            method = getattr(window, "close", None)
            if callable(method):
                try:
                    method()
                    return
                except Exception:
                    pass
            pattern = None
            with contextlib.suppress(Exception):
                pattern = getattr(window, "iface_window", None)
            close_pattern = getattr(pattern, "Close", None) if pattern is not None else None
            if callable(close_pattern):
                try:
                    close_pattern()
                    return
                except Exception:
                    pass
            self._activate_uia_window(window)
            self._driver.key(["alt", "f4"])
            return

        raise _uia_operation_error(operation)

    def _require_session(self, params: dict[str, Any]) -> _RuntimeSession:
        context_id = normalize_context_id(params.get("context_id"))
        session = self._session
        if session is None or not session.session.active or session.session.context_id != context_id:
            raise WindowsComputerUseError(
                "COMPUTER_USE_SESSION_REQUIRED",
                "No computer-use session is active.",
            )

        requested_session_id = str(params.get("session_id", "")).strip()
        if requested_session_id and requested_session_id != session.session.session_id:
            raise WindowsComputerUseError(
                "COMPUTER_USE_SESSION_MISMATCH",
                "Requested session_id does not match the active computer-use session.",
            )
        return session

    def close(self) -> None:
        if self._session is not None and self._session.session.active:
            self.stop_session({"context_id": self._session.session.context_id})


def _build_error_response(
    request_id: str,
    error: WindowsComputerUseError,
) -> dict[str, Any]:
    payload = {
        "request_id": request_id,
        "ok": False,
        "error": str(error),
        "code": error.code,
    }
    if error.result is not None:
        payload["result"] = error.result
    return payload


def serve_stdio(runtime: WindowsComputerUseRuntime | None = None) -> int:
    runtime = runtime or WindowsComputerUseRuntime()
    try:
        while True:
            raw_line = sys.stdin.readline()
            if not raw_line:
                break
            try:
                request = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                response = {
                    "request_id": "",
                    "ok": False,
                    "error": f"Invalid JSON: {exc}",
                    "code": "INVALID_JSON",
                }
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
                continue

            if not isinstance(request, dict):
                response = {
                    "request_id": "",
                    "ok": False,
                    "error": "Request must be a JSON object.",
                    "code": "INVALID_REQUEST",
                }
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
                continue

            request_id = str(request.get("request_id", "") or "")
            action = str(request.get("action", "") or "").strip().lower()
            if action == "shutdown":
                break

            try:
                with contextlib.redirect_stdout(sys.stderr):
                    if action in {
                        "start_session",
                        "status",
                        "capture",
                        "list_windows",
                        "get_window_state",
                        "element_action",
                        "uia_snapshot",
                        "uia_action",
                        "move",
                        "click",
                        "scroll",
                        "key",
                        "type",
                        "stop_session",
                    }:
                        if action not in {"start_session", "status", "stop_session"}:
                            request = normalize_action_payload(action, request, context_id=normalize_context_id(request.get("context_id")))
                        result = runtime.dispatch(action, request)
                        response = {
                            "request_id": request_id,
                            "ok": True,
                            "result": result,
                        }
                    else:
                        raise WindowsComputerUseError(
                            "UNKNOWN_METHOD",
                            f"Unknown computer-use helper method: {action}",
                        )
            except WindowsComputerUseError as exc:
                response = _build_error_response(request_id, exc)
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
        runtime.close()
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
