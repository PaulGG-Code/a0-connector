from __future__ import annotations

import sys
from pathlib import Path

import pytest


PACKAGE_SRC = Path(__file__).resolve().parents[1] / "packages/a0-computer-use-wayland/src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from a0_computer_use_wayland import WAYLAND_BACKEND_SPEC, get_backend_spec
from a0_computer_use_wayland import detection as wayland_detection
from a0_computer_use_wayland import paths as wayland_paths


def test_wayland_backend_spec_exposes_expected_metadata() -> None:
    spec = get_backend_spec()

    assert spec is WAYLAND_BACKEND_SPEC
    assert spec.backend_id == "wayland"
    assert spec.backend_family == "linux"
    assert spec.priority == 100
    assert spec.interpreter_strategy == "system_python"
    assert spec.helper_target == str(wayland_paths.HELPER_SCRIPT)
    assert spec.supports_trust_mode("interactive") is True
    assert spec.supports_trust_mode("persistent") is True
    assert spec.supports_trust_mode("allow") is True
    assert "portal-remote-desktop" in spec.features
    assert "inline-png-capture" in spec.features
    assert "fresh-frame-capture" in spec.features
    assert "global-pixel-actions" in spec.features
    assert "real-cursor-may-move" in spec.features


def test_wayland_detection_and_support_reason_are_additive_and_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wayland_detection, "SYSTEM_PYTHON", sys.executable)

    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert wayland_detection.detect_wayland_support() is True
    assert wayland_detection.wayland_support_reason() == "Wayland portal backend is available."

    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert wayland_detection.detect_wayland_support() is False
    assert "not supported by the Wayland portal backend" in wayland_detection.wayland_support_reason()

    monkeypatch.setattr(wayland_detection, "SYSTEM_PYTHON", str(Path(sys.executable).with_name("definitely-missing-python")))
    assert wayland_detection.detect_wayland_support() is False
    assert "Required system Python interpreter not found" in wayland_detection.wayland_support_reason()
