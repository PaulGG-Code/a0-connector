from __future__ import annotations

import importlib.util
import sys

_REQUIRED_MODULES = ("AppKit", "ApplicationServices", "Quartz")


def macos_backend_support_reason() -> str:
    if sys.platform != "darwin":
        return "macOS computer-use backend is only available on macOS."

    missing = [name for name in _REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        return "Missing macOS computer-use dependencies: " + ", ".join(missing) + "."

    return "macOS desktop backend is available."


def macos_backend_supported() -> bool:
    return macos_backend_support_reason() == "macOS desktop backend is available."
