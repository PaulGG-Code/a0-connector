from __future__ import annotations

import sys
from typing import Any

from agent_zero_cli.computer_use_backend import (
    ComputerUseBackendSpec,
    register_backend_spec,
)

from a0_computer_use_x11.detection import detect_x11_support, x11_support_reason
from a0_computer_use_x11.paths import HELPER_SCRIPT


X11_BACKEND_SPEC = ComputerUseBackendSpec(
    backend_id="x11",
    backend_family="linux",
    priority=90,
    detect=detect_x11_support,
    features=(
        "x11-xtest",
        "x11-screenshot",
        "inline-png-capture",
        "normalized-screen-coordinates",
        "global-pixel-actions",
        "pointer-injection",
        "keyboard-injection",
        "real-cursor-may-move",
        "focus-risk",
    ),
    interpreter_strategy="current_python",
    helper_target=str(HELPER_SCRIPT),
    trust_mode_support=("interactive", "persistent", "allow"),
    support_reason=x11_support_reason,
)


class X11ComputerUseBackend:
    spec = X11_BACKEND_SPEC

    def hello_metadata(self) -> dict[str, Any]:
        return {
            "supported": self.spec.detect(),
            "backend_id": self.spec.backend_id,
            "backend_family": self.spec.backend_family,
            "features": list(self.spec.features),
            "support_reason": x11_support_reason(),
        }

    def helper_command(self) -> list[str]:
        return [sys.executable, self.spec.helper_target, "--stdio"]


def get_backend_spec() -> ComputerUseBackendSpec:
    return X11_BACKEND_SPEC


def install_backend_spec() -> ComputerUseBackendSpec:
    return register_backend_spec(X11_BACKEND_SPEC)
