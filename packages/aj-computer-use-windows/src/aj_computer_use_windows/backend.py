from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from agentic_job_cli.computer_use_backend import (
    ComputerUseBackendSpec,
    register_backend_spec,
)

from aj_computer_use_windows.detection import (
    windows_backend_support_reason,
    windows_backend_supported,
)
from aj_computer_use_windows.shared import (
    WINDOWS_BACKEND_FEATURES,
    WINDOWS_BACKEND_FAMILY,
    WINDOWS_BACKEND_ID,
    WINDOWS_BACKEND_PRIORITY,
    WINDOWS_TRUST_MODES,
)

_HELPER_TARGET = str(Path(__file__).with_name("runtime.py"))


def _detect() -> bool:
    return windows_backend_supported()


WINDOWS_BACKEND_SPEC = ComputerUseBackendSpec(
    backend_id=WINDOWS_BACKEND_ID,
    backend_family=WINDOWS_BACKEND_FAMILY,
    priority=WINDOWS_BACKEND_PRIORITY,
    detect=_detect,
    features=WINDOWS_BACKEND_FEATURES,
    interpreter_strategy="current_python",
    helper_target=_HELPER_TARGET,
    trust_mode_support=WINDOWS_TRUST_MODES,
    support_reason=windows_backend_support_reason,
)


class WindowsComputerUseBackend:
    spec = WINDOWS_BACKEND_SPEC

    def hello_metadata(self) -> dict[str, Any]:
        return {
            "supported": self.spec.detect(),
            "backend_id": self.spec.backend_id,
            "backend_family": self.spec.backend_family,
            "features": list(self.spec.features),
            "contract_version": self.spec.capabilities()["contract_version"],
            "capabilities": self.spec.capabilities(),
            "support_reason": windows_backend_support_reason(),
        }

    def helper_command(self) -> list[str]:
        return [sys.executable, self.spec.helper_target, "--stdio"]


def install_backend_spec() -> ComputerUseBackendSpec:
    return register_backend_spec(WINDOWS_BACKEND_SPEC)
