from __future__ import annotations

import os
import sys
from typing import Any


def _environment_support_issue() -> str:
    if not sys.platform.startswith("linux"):
        return f"platform={sys.platform!r} is not supported by the X11 backend."

    session_type = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if session_type == "wayland":
        return "XDG_SESSION_TYPE=wayland is handled by the Wayland portal backend."

    display_name = (os.environ.get("DISPLAY") or "").strip()
    if not display_name:
        return "DISPLAY is not set; an X11 display is required."

    return ""


def _runtime_support_issue() -> str:
    try:
        from Xlib import display
        from Xlib import error as x_error
    except Exception as exc:
        return f"python-xlib is not importable: {exc}"

    try:
        import mss  # noqa: F401
    except Exception as exc:
        return f"mss is not importable: {exc}"

    x_display: Any | None = None
    try:
        x_display = display.Display()
        if not x_display.query_extension("XTEST"):
            return "X11 display does not advertise the XTEST extension."
        return ""
    except getattr(x_error, "DisplayError", Exception) as exc:
        return f"Unable to connect to X11 display: {exc}"
    except Exception as exc:
        return f"Unable to probe X11 display: {exc}"
    finally:
        if x_display is not None:
            try:
                x_display.close()
            except Exception:
                pass


def _support_issue() -> str:
    return _environment_support_issue() or _runtime_support_issue()


def detect_x11_support() -> bool:
    return _support_issue() == ""


def x11_support_reason() -> str:
    issue = _support_issue()
    if issue:
        return issue
    return "X11 XTEST backend is available."
