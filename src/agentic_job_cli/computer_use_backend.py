from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import os
import sys
from typing import Any, Callable, Protocol

_ENTRY_POINT_GROUP = "aj.computer_use_backends"
_DISABLED_REMOTE_BACKEND_IDS = {"x11"}
COMPUTER_USE_CONTRACT_VERSION = 1
COMPUTER_USE_CONTRACT_ACTIONS = (
    "start_session",
    "status",
    "capture",
    "list_windows",
    "get_window_state",
    "element_action",
    "move",
    "click",
    "scroll",
    "key",
    "type",
    "stop_session",
)
_TREE_FEATURES = {
    "accessibility-tree-snapshot",
    "atspi-tree-snapshot",
    "uia-tree-snapshot",
}
_STRUCTURAL_TARGETING_FEATURES = {
    "accessibility-structural-targeting",
    "atspi-structural-targeting",
    "uia-structural-targeting",
}
_ELEMENT_ACTION_FEATURES = {
    "accessibility-element-click",
    "atspi-element-action",
    "uia-element-action",
}
_SET_VALUE_FEATURES = {
    "atspi-set-value",
    "uia-element-action",
}
_POINTER_INPUT_FEATURES = {
    "global-pixel-actions",
    "mouse-injection",
    "pointer-injection",
}
_KEYBOARD_INPUT_FEATURES = {
    "keyboard-injection",
    "quartz-input-events",
}
_SCREENSHOT_FEATURES = {
    "inline-png-capture",
    "dxcam-screen-capture",
    "coregraphics-screen-capture",
    "portal-screencast",
    "screencapture-screen-capture",
}


@dataclass(frozen=True)
class ComputerUseBackendSpec:
    backend_id: str
    backend_family: str
    priority: int
    detect: Callable[[], bool]
    features: tuple[str, ...]
    interpreter_strategy: str
    helper_target: str
    trust_mode_support: tuple[str, ...]
    support_reason: Callable[[], str] = lambda: ""

    def supports_trust_mode(self, mode: str) -> bool:
        return str(mode or "").strip().lower() in self.trust_mode_support

    def capabilities(self) -> dict[str, Any]:
        return computer_use_capabilities_from_features(
            backend_id=self.backend_id,
            backend_family=self.backend_family,
            features=self.features,
        )


@dataclass(frozen=True)
class ComputerUseBackendSelection:
    spec: ComputerUseBackendSpec | None
    supported: bool
    support_reason: str


class ComputerUseBackend(Protocol):
    spec: ComputerUseBackendSpec

    def hello_metadata(self) -> dict[str, Any]:
        ...

    def helper_command(self) -> list[str]:
        ...


_BUILTIN_SPECS: dict[str, ComputerUseBackendSpec] = {}
_EXTRA_SPECS: dict[str, ComputerUseBackendSpec] = {}


def _feature_set(features: tuple[str, ...] | list[str] | set[str]) -> set[str]:
    return {str(feature or "").strip().lower() for feature in features if str(feature or "").strip()}


def computer_use_capabilities_from_features(
    *,
    backend_id: str,
    backend_family: str,
    features: tuple[str, ...] | list[str] | set[str],
) -> dict[str, Any]:
    feature_names = _feature_set(features)
    tree_backends: list[str] = []
    if "uia-tree-snapshot" in feature_names:
        tree_backends.append("uia")
    if "accessibility-tree-snapshot" in feature_names:
        tree_backends.append("ax")
    if "atspi-tree-snapshot" in feature_names:
        tree_backends.append("at-spi")

    has_background_dispatch = "background-dispatch" in feature_names
    has_element_index = "element-index-targeting" in feature_names
    has_window_state = "window-state" in feature_names
    has_native_windows = "native-window-list" in feature_names
    has_tree = bool(feature_names & _TREE_FEATURES)
    has_structural_targeting = bool(feature_names & _STRUCTURAL_TARGETING_FEATURES)
    has_element_action = bool(feature_names & _ELEMENT_ACTION_FEATURES)

    dispatch_modes = ["foreground"]
    if has_background_dispatch:
        dispatch_modes.insert(0, "background")
        dispatch_modes.insert(1, "auto")

    return {
        "contract_version": COMPUTER_USE_CONTRACT_VERSION,
        "backend": {
            "id": str(backend_id or "").strip(),
            "family": str(backend_family or "").strip(),
        },
        "actions": list(COMPUTER_USE_CONTRACT_ACTIONS),
        "identity": {
            "pid": has_native_windows or has_window_state,
            "window_id": has_native_windows or has_window_state,
            "element_index": has_element_index,
        },
        "capture": {
            "screenshot": bool(feature_names & _SCREENSHOT_FEATURES),
            "inline_png": "inline-png-capture" in feature_names,
            "fresh_frame": "fresh-frame-capture" in feature_names,
            "background": bool(
                feature_names
                & {
                    "background-screen-capture",
                    "no-cursor-steal-capture",
                    "portal-screencast",
                    "dxcam-screen-capture",
                }
            ),
            "coordinate_space": "normalized_global_screen",
        },
        "windows": {
            "list": has_native_windows,
            "state": has_window_state,
        },
        "elements": {
            "tree": has_tree,
            "tree_backends": tree_backends,
            "structural_targeting": has_structural_targeting,
            "element_index": has_element_index,
            "action": has_element_action,
            "set_value": bool(feature_names & _SET_VALUE_FEATURES),
        },
        "dispatch": {
            "default": "background" if has_background_dispatch else "foreground",
            "background": has_background_dispatch,
            "foreground": True,
            "foreground_fallback": "foreground-dispatch-fallback" in feature_names,
            "modes": dispatch_modes,
        },
        "input": {
            "global_coordinates": "global-pixel-actions" in feature_names,
            "normalized_coordinates": "normalized-screen-coordinates" in feature_names,
            "pointer": bool(feature_names & _POINTER_INPUT_FEATURES),
            "keyboard": bool(feature_names & _KEYBOARD_INPUT_FEATURES),
            "real_cursor_may_move": "real-cursor-may-move" in feature_names,
        },
    }


def _remote_backend_enabled(spec: ComputerUseBackendSpec) -> bool:
    backend_id = str(spec.backend_id or "").strip().lower()
    return backend_id not in _DISABLED_REMOTE_BACKEND_IDS


def register_builtin_backend_spec(spec: ComputerUseBackendSpec) -> ComputerUseBackendSpec:
    _BUILTIN_SPECS[spec.backend_id] = spec
    return spec


def register_backend_spec(spec: ComputerUseBackendSpec) -> ComputerUseBackendSpec:
    _EXTRA_SPECS[spec.backend_id] = spec
    return spec


def clear_backend_specs() -> None:
    _EXTRA_SPECS.clear()


def _coerce_spec(candidate: object) -> ComputerUseBackendSpec | None:
    if isinstance(candidate, ComputerUseBackendSpec):
        return candidate

    spec = getattr(candidate, "spec", None)
    if isinstance(spec, ComputerUseBackendSpec):
        return spec

    if callable(candidate):
        try:
            loaded = candidate()
        except Exception:
            return None
        if isinstance(loaded, ComputerUseBackendSpec):
            return loaded
        spec = getattr(loaded, "spec", None)
        if isinstance(spec, ComputerUseBackendSpec):
            return spec

    return None


def _entry_point_specs() -> list[ComputerUseBackendSpec]:
    try:
        entry_points = metadata.entry_points()
    except Exception:
        return []

    if hasattr(entry_points, "select"):
        group = entry_points.select(group=_ENTRY_POINT_GROUP)
    else:  # pragma: no cover - legacy importlib.metadata shape
        group = entry_points.get(_ENTRY_POINT_GROUP, [])

    specs: list[ComputerUseBackendSpec] = []
    for entry_point in group:
        try:
            candidate = entry_point.load()
        except Exception:
            continue
        spec = _coerce_spec(candidate)
        if spec is not None:
            specs.append(spec)
    return specs


def available_backend_specs() -> list[ComputerUseBackendSpec]:
    merged: dict[str, ComputerUseBackendSpec] = {}
    for source in (_BUILTIN_SPECS.values(), _entry_point_specs(), _EXTRA_SPECS.values()):
        for spec in source:
            if not _remote_backend_enabled(spec):
                continue
            merged[spec.backend_id] = spec
    return sorted(merged.values(), key=lambda item: (-item.priority, item.backend_id))


def _host_backend_families() -> tuple[str, ...]:
    if sys.platform == "darwin":
        return ("macos",)
    if sys.platform == "win32":
        return ("windows",)
    if sys.platform.startswith("linux"):
        return ("linux",)
    return ()


def _support_reason(spec: ComputerUseBackendSpec) -> str:
    try:
        return str(spec.support_reason() or "").strip()
    except Exception as exc:
        return f"{spec.backend_id} support_reason() failed: {exc}"


def _unsupported_preference(spec: ComputerUseBackendSpec) -> int:
    if not sys.platform.startswith("linux"):
        return 0

    backend_id = str(spec.backend_id or "").strip().lower()
    session_type = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    display_name = (os.environ.get("DISPLAY") or "").strip()

    if session_type == "wayland":
        return 0 if backend_id == "wayland" else 1
    if session_type in {"x11", "xorg"} or display_name:
        return 0 if backend_id == "wayland" else 1
    return 0


def resolve_backend_selection() -> ComputerUseBackendSelection:
    specs = sorted(
        (spec for spec in available_backend_specs() if _remote_backend_enabled(spec)),
        key=lambda item: (-item.priority, item.backend_id),
    )
    if not specs:
        return ComputerUseBackendSelection(
            spec=None,
            supported=False,
            support_reason="No computer-use backend specs are registered.",
        )

    detect_error: str | None = None
    for spec in specs:
        try:
            if spec.detect():
                reason = _support_reason(spec) or "Detected computer-use backend."
                return ComputerUseBackendSelection(spec=spec, supported=True, support_reason=reason)
        except Exception as exc:
            if detect_error is None:
                detect_error = f"{spec.backend_id} detect() failed: {exc}"
            continue

    host_families = _host_backend_families()
    host_specs = [
        spec
        for spec in specs
        if str(spec.backend_family or "").strip().lower() in host_families
    ]
    host_specs = sorted(
        host_specs,
        key=lambda item: (_unsupported_preference(item), -item.priority, item.backend_id),
    )
    spec = host_specs[0] if host_specs else specs[0]
    if not host_specs and host_families:
        reason = f"No computer-use backend spec is registered for host platform {sys.platform!r}."
    else:
        reason = _support_reason(spec)
    if not reason:
        reason = detect_error or f"No detected backend matched {spec.backend_id!r}."
    return ComputerUseBackendSelection(spec=spec, supported=False, support_reason=reason)
