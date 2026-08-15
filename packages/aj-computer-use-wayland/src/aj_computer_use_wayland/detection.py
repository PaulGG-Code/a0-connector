from __future__ import annotations

import os
from pathlib import Path

from .paths import SYSTEM_PYTHON


def detect_wayland_support() -> bool:
    if not Path(SYSTEM_PYTHON).exists():
        return False
    return (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower() == "wayland"


def wayland_support_reason() -> str:
    if not Path(SYSTEM_PYTHON).exists():
        return f"Required system Python interpreter not found at {SYSTEM_PYTHON}."

    session_type = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if session_type != "wayland":
        return f"XDG_SESSION_TYPE={session_type or 'unset'} is not supported by the Wayland portal backend."

    return "Wayland portal backend is available."
