from __future__ import annotations

import importlib.util
import os

_REQUIRED_MODULES = ("dxcam", "pywinauto", "win32api")


def windows_backend_support_reason() -> str:
    if os.name != "nt":
        return "Windows computer-use backend is only available on Windows."

    missing = [name for name in _REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        return "Missing Windows computer-use dependencies: " + ", ".join(missing) + "."

    return "Windows desktop backend is available."


def windows_backend_supported() -> bool:
    return windows_backend_support_reason() == "Windows desktop backend is available."
