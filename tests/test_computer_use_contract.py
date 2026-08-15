from __future__ import annotations

import sys
from pathlib import Path

from agentic_job_cli.computer_use_backend import COMPUTER_USE_CONTRACT_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for package in (
    "aj-computer-use-windows",
    "aj-computer-use-macos",
    "aj-computer-use-wayland",
):
    package_src = PROJECT_ROOT / "packages" / package / "src"
    if str(package_src) not in sys.path:
        sys.path.insert(0, str(package_src))

from aj_computer_use_macos.backend import MACOS_BACKEND_SPEC  # noqa: E402
from aj_computer_use_wayland import WAYLAND_BACKEND_SPEC  # noqa: E402
from aj_computer_use_windows.backend import WINDOWS_BACKEND_SPEC  # noqa: E402


def test_windows_uia_advertises_full_background_contract() -> None:
    capabilities = WINDOWS_BACKEND_SPEC.capabilities()

    assert capabilities["contract_version"] == COMPUTER_USE_CONTRACT_VERSION
    assert capabilities["backend"] == {"id": "windows", "family": "windows"}
    assert capabilities["identity"] == {
        "pid": True,
        "window_id": True,
        "element_index": True,
    }
    assert capabilities["windows"] == {"list": True, "state": True}
    assert capabilities["elements"]["tree_backends"] == ["uia"]
    assert capabilities["elements"]["tree"] is True
    assert capabilities["elements"]["structural_targeting"] is True
    assert capabilities["elements"]["action"] is True
    assert capabilities["dispatch"]["default"] == "background"
    assert capabilities["dispatch"]["background"] is True
    assert capabilities["dispatch"]["foreground_fallback"] is True


def test_macos_ax_advertises_same_identity_and_dispatch_contract() -> None:
    capabilities = MACOS_BACKEND_SPEC.capabilities()

    assert capabilities["contract_version"] == COMPUTER_USE_CONTRACT_VERSION
    assert capabilities["backend"] == {"id": "macos", "family": "macos"}
    assert capabilities["identity"] == {
        "pid": True,
        "window_id": True,
        "element_index": True,
    }
    assert capabilities["windows"] == {"list": True, "state": True}
    assert capabilities["elements"]["tree_backends"] == ["ax"]
    assert capabilities["elements"]["tree"] is True
    assert capabilities["dispatch"]["default"] == "background"
    assert capabilities["dispatch"]["background"] is True
    assert capabilities["input"]["keyboard"] is True


def test_wayland_atspi_advertises_same_portable_contract_with_linux_tree_backend() -> None:
    capabilities = WAYLAND_BACKEND_SPEC.capabilities()

    assert capabilities["contract_version"] == COMPUTER_USE_CONTRACT_VERSION
    assert capabilities["backend"] == {"id": "wayland", "family": "linux"}
    assert capabilities["identity"] == {
        "pid": True,
        "window_id": True,
        "element_index": True,
    }
    assert capabilities["windows"] == {"list": True, "state": True}
    assert capabilities["elements"]["tree_backends"] == ["ax", "at-spi"]
    assert capabilities["elements"]["tree"] is True
    assert capabilities["elements"]["set_value"] is True
    assert capabilities["dispatch"]["default"] == "background"
    assert capabilities["dispatch"]["background"] is True
    assert capabilities["input"]["pointer"] is True
