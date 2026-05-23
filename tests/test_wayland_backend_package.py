from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


PACKAGE_SRC = Path(__file__).resolve().parents[1] / "packages/a0-computer-use-wayland/src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

from a0_computer_use_wayland import WAYLAND_BACKEND_SPEC, get_backend_spec
from a0_computer_use_wayland import detection as wayland_detection
from a0_computer_use_wayland import paths as wayland_paths


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WAYLAND_HELPER_FILES = [
    PROJECT_ROOT / "src" / "agent_zero_cli" / "computer_use_helper.py",
    PROJECT_ROOT
    / "packages"
    / "a0-computer-use-wayland"
    / "src"
    / "a0_computer_use_wayland"
    / "computer_use_helper.py",
]


class _FakeInt(int):
    def __new__(cls, value: object = 0, *args: object, **kwargs: object) -> "_FakeInt":
        del args, kwargs
        return int.__new__(cls, int(value))


class _FakeFloat(float):
    def __new__(cls, value: object = 0.0, *args: object, **kwargs: object) -> "_FakeFloat":
        del args, kwargs
        return float.__new__(cls, float(value))


class _FakeStr(str):
    def __new__(cls, value: object = "", *args: object, **kwargs: object) -> "_FakeStr":
        del args, kwargs
        return str.__new__(cls, str(value))


class _FakeArray(list):
    def __init__(self, value: object = (), *args: object, **kwargs: object) -> None:
        del args, kwargs
        super().__init__(value)


class _FakeDictionary(dict):
    def __init__(self, value: object = (), *args: object, **kwargs: object) -> None:
        del args, kwargs
        super().__init__(value)


class _FakeStruct(tuple):
    def __new__(cls, value: object = (), *args: object, **kwargs: object) -> "_FakeStruct":
        del args, kwargs
        return tuple.__new__(cls, value)


class _FakeGdk:
    _KEYVALS = {
        "Alt_L": 0xFFE9,
        "BackSpace": 0xFF08,
        "Control_L": 0xFFE3,
        "Delete": 0xFFFF,
        "Down": 0xFF54,
        "Escape": 0xFF1B,
        "Left": 0xFF51,
        "Page_Down": 0xFF56,
        "Page_Up": 0xFF55,
        "Return": 0xFF0D,
        "Right": 0xFF53,
        "Shift_L": 0xFFE1,
        "Super_L": 0xFFEB,
        "Tab": 0xFF09,
        "Up": 0xFF52,
        "XF86AudioMute": 0x1008FF12,
        "space": 0x20,
    }

    @staticmethod
    def unicode_to_keyval(value: int) -> int:
        return value

    @classmethod
    def keyval_from_name(cls, name: str) -> int:
        return cls._KEYVALS.get(name, 0)


class _FakeRemoteDesktop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, int]] = []

    def NotifyKeyboardKeycode(
        self,
        session_handle: object,
        options: object,
        keycode: object,
        state: object,
    ) -> None:
        del options
        self.calls.append(("keycode", str(session_handle), int(keycode), int(state)))

    def NotifyKeyboardKeysym(
        self,
        session_handle: object,
        options: object,
        keysym: object,
        state: object,
    ) -> None:
        del options
        self.calls.append(("keysym", str(session_handle), int(keysym), int(state)))


class _FakeAtspiExtents:
    def __init__(self, x: int, y: int, width: int, height: int) -> None:
        self.x = x
        self.y = y
        self.width = width
        self.height = height


class _FakeAtspiStateSet:
    def __init__(self, states: set[str]) -> None:
        self._states = states

    def contains(self, state: str) -> bool:
        return state in self._states


class _FakeAtspiAccessible:
    def __init__(
        self,
        *,
        role: str,
        name: str = "",
        description: str = "",
        children: list["_FakeAtspiAccessible"] | None = None,
        actions: list[str] | None = None,
        states: set[str] | None = None,
        frame: tuple[int, int, int, int] | None = None,
        text: str = "",
        value: float | None = None,
        pid: int = 123,
    ) -> None:
        self.role = role
        self.name = name
        self.description = description
        self.children = children or []
        self.actions = actions or []
        self.states = states or {"VISIBLE", "SHOWING", "ENABLED"}
        self.frame = frame
        self.text = text
        self.value = value
        self.pid = pid
        self.performed_actions: list[int] = []
        self.focused = False
        self.set_text_values: list[str] = []
        self.set_numeric_values: list[float] = []

    def get_name(self) -> str:
        return self.name

    def get_role_name(self) -> str:
        return self.role

    def get_description(self) -> str:
        return self.description

    def get_process_id(self) -> int:
        return self.pid

    def get_child_count(self) -> int:
        return len(self.children)

    def get_child_at_index(self, index: int) -> "_FakeAtspiAccessible":
        return self.children[index]

    def get_extents(self, coord_type: object) -> _FakeAtspiExtents | None:
        del coord_type
        if self.frame is None:
            return None
        return _FakeAtspiExtents(*self.frame)

    def get_state_set(self) -> _FakeAtspiStateSet:
        return _FakeAtspiStateSet(self.states)

    def get_n_actions(self) -> int:
        return len(self.actions)

    def get_action_name(self, index: int) -> str:
        return self.actions[index]

    def get_action_description(self, index: int) -> str:
        return f"{self.actions[index]} action"

    def get_key_binding(self, index: int) -> str:
        del index
        return ""

    def do_action(self, index: int) -> bool:
        self.performed_actions.append(index)
        return True

    def grab_focus(self) -> bool:
        self.focused = True
        return True

    def get_character_count(self) -> int:
        return len(self.text)

    def get_text(self, start: int, end: int) -> str:
        return self.text[start:end]

    def set_text_contents(self, value: str) -> bool:
        self.set_text_values.append(value)
        self.text = value
        return True

    def get_current_value(self) -> float | None:
        return self.value

    def set_current_value(self, value: float) -> bool:
        self.set_numeric_values.append(value)
        self.value = value
        return True


class _FakeAtspi:
    CoordType = types.SimpleNamespace(SCREEN="screen")
    StateType = types.SimpleNamespace(
        ACTIVE="ACTIVE",
        CHECKED="CHECKED",
        EDITABLE="EDITABLE",
        ENABLED="ENABLED",
        EXPANDED="EXPANDED",
        FOCUSED="FOCUSED",
        FOCUSABLE="FOCUSABLE",
        PRESSED="PRESSED",
        SELECTED="SELECTED",
        SHOWING="SHOWING",
        VISIBLE="VISIBLE",
    )
    desktop: _FakeAtspiAccessible | None = None

    @classmethod
    def get_desktop(cls, index: int) -> _FakeAtspiAccessible:
        assert index == 0
        assert cls.desktop is not None
        return cls.desktop


def _install_wayland_helper_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAtspi.desktop = None
    dbus_mod = types.ModuleType("dbus")
    for name in (
        "Boolean",
        "Byte",
        "Int16",
        "Int32",
        "Int64",
        "UInt16",
        "UInt32",
        "UInt64",
    ):
        setattr(dbus_mod, name, _FakeInt)
    dbus_mod.Double = _FakeFloat
    dbus_mod.String = _FakeStr
    dbus_mod.ObjectPath = _FakeStr
    dbus_mod.Signature = _FakeStr
    dbus_mod.Array = _FakeArray
    dbus_mod.Dictionary = _FakeDictionary
    dbus_mod.Struct = _FakeStruct
    dbus_mod.Interface = lambda obj, _iface=None: obj
    dbus_mod.SessionBus = object

    dbus_mainloop_mod = types.ModuleType("dbus.mainloop")
    dbus_glib_mod = types.ModuleType("dbus.mainloop.glib")
    dbus_glib_mod.DBusGMainLoop = lambda *args, **kwargs: None

    gi_mod = types.ModuleType("gi")
    gi_mod.require_version = lambda *args, **kwargs: None
    gi_repository_mod = types.ModuleType("gi.repository")
    gi_repository_mod.Gdk = _FakeGdk
    gi_repository_mod.GLib = types.SimpleNamespace(MainLoop=object)
    gi_repository_mod.Gst = types.SimpleNamespace(init=lambda *args, **kwargs: None)
    gi_repository_mod.Atspi = _FakeAtspi

    monkeypatch.setitem(sys.modules, "dbus", dbus_mod)
    monkeypatch.setitem(sys.modules, "dbus.mainloop", dbus_mainloop_mod)
    monkeypatch.setitem(sys.modules, "dbus.mainloop.glib", dbus_glib_mod)
    monkeypatch.setitem(sys.modules, "gi", gi_mod)
    monkeypatch.setitem(sys.modules, "gi.repository", gi_repository_mod)


@pytest.fixture(params=WAYLAND_HELPER_FILES, ids=("cli-helper", "package-helper"))
def wayland_helper_module(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    _install_wayland_helper_stubs(monkeypatch)
    helper_path = Path(request.param)
    module_name = f"_a0_test_wayland_helper_{abs(hash(helper_path))}"
    spec = importlib.util.spec_from_file_location(module_name, helper_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _portal_helper(module):
    helper = module.PortalComputerUseHelper.__new__(module.PortalComputerUseHelper)
    remote_desktop = _FakeRemoteDesktop()
    helper._remote_desktop = remote_desktop
    helper._session = module.PortalSession(
        context_id="ctx-1",
        trust_mode="persistent",
        session_id="sess-1",
        session_handle="/org/freedesktop/portal/desktop/session/a0/test",
        stream_id=1,
        width=1920,
        height=1080,
        devices=3,
        restore_token="",
        capture_stream=None,
    )
    return helper, remote_desktop


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
    assert "atspi-tree-snapshot" in spec.features
    assert "atspi-structural-targeting" in spec.features
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


def test_wayland_shortcut_dispatch_uses_evdev_keycodes_for_ctrl_t(
    wayland_helper_module,
) -> None:
    helper, remote_desktop = _portal_helper(wayland_helper_module)

    result = helper.key({"session_id": "sess-1", "keys": ["ctrl", "T"]})

    assert result["keys"] == ["ctrl", "T"]
    assert remote_desktop.calls == [
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", 29, 1),
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", 20, 1),
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", 20, 0),
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", 29, 0),
    ]


@pytest.mark.parametrize(
    ("keys", "codes"),
    [
        (["Super", "H"], [125, 35]),
        (["alt", "F9"], [56, 67]),
    ],
)
def test_wayland_shortcut_dispatch_covers_super_alt_and_function_keys(
    wayland_helper_module,
    keys: list[str],
    codes: list[int],
) -> None:
    helper, remote_desktop = _portal_helper(wayland_helper_module)

    helper.key({"session_id": "sess-1", "keys": keys})

    assert remote_desktop.calls == [
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", codes[0], 1),
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", codes[1], 1),
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", codes[1], 0),
        ("keycode", "/org/freedesktop/portal/desktop/session/a0/test", codes[0], 0),
    ]


def test_wayland_shortcut_dispatch_falls_back_to_keysyms_for_unknown_keys(
    wayland_helper_module,
) -> None:
    helper, remote_desktop = _portal_helper(wayland_helper_module)

    helper.key({"session_id": "sess-1", "keys": ["XF86AudioMute"]})

    assert remote_desktop.calls == [
        ("keysym", "/org/freedesktop/portal/desktop/session/a0/test", 0x1008FF12, 1),
        ("keysym", "/org/freedesktop/portal/desktop/session/a0/test", 0x1008FF12, 0),
    ]


def test_wayland_text_dispatch_still_uses_keysyms(
    wayland_helper_module,
) -> None:
    helper, remote_desktop = _portal_helper(wayland_helper_module)

    helper.type_text({"session_id": "sess-1", "text": "T", "submit": True})

    assert remote_desktop.calls == [
        ("keysym", "/org/freedesktop/portal/desktop/session/a0/test", ord("T"), 1),
        ("keysym", "/org/freedesktop/portal/desktop/session/a0/test", ord("T"), 0),
        ("keysym", "/org/freedesktop/portal/desktop/session/a0/test", 0xFF0D, 1),
        ("keysym", "/org/freedesktop/portal/desktop/session/a0/test", 0xFF0D, 0),
    ]


def test_wayland_ax_snapshot_returns_linux_atspi_tree(
    wayland_helper_module,
) -> None:
    button = _FakeAtspiAccessible(
        role="push button",
        name="Open",
        actions=["press"],
        frame=(100, 200, 80, 30),
    )
    text_field = _FakeAtspiAccessible(
        role="text",
        name="Search",
        states={"VISIBLE", "SHOWING", "ENABLED", "FOCUSABLE", "EDITABLE"},
        frame=(20, 40, 300, 36),
        text="",
    )
    app = _FakeAtspiAccessible(
        role="application",
        name="Fake App",
        children=[button, text_field],
        frame=(0, 0, 800, 600),
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(role="desktop", children=[app])
    helper, _remote_desktop = _portal_helper(wayland_helper_module)

    result = helper.ax_snapshot({"session_id": "sess-1", "max_depth": 3, "max_nodes": 20})

    assert result["app"] == {"name": "Linux desktop", "backend": "at-spi"}
    assert result["node_count"] == 4
    assert result["truncated"] is False
    root = result["tree"]
    assert root["role"] == "Desktop"
    assert root["children"][0]["path"] == [0]
    assert root["children"][0]["title"] == "Fake App"
    assert root["children"][0]["children"][0]["path"] == [0, 0]
    assert root["children"][0]["children"][0]["actions"][0]["name"] == "press"
    assert root["children"][0]["children"][1]["states"] == [
        "editable",
        "enabled",
        "focusable",
        "showing",
        "visible",
    ]


def test_wayland_ax_action_presses_element_by_path(
    wayland_helper_module,
) -> None:
    button = _FakeAtspiAccessible(
        role="push button",
        name="Open",
        actions=["press"],
        frame=(100, 200, 80, 30),
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[_FakeAtspiAccessible(role="application", name="Fake App", children=[button])],
    )
    helper, _remote_desktop = _portal_helper(wayland_helper_module)

    result = helper.ax_action({"session_id": "sess-1", "path": [0, 0], "operation": "press"})

    assert button.performed_actions == [0]
    assert result["operation"] == "press"
    assert result["target"]["path"] == [0, 0]
    assert result["target"]["role"] == "push button"
    assert result["target"]["title"] == "Open"


def test_wayland_ax_action_sets_text_by_semantic_target(
    wayland_helper_module,
) -> None:
    text_field = _FakeAtspiAccessible(
        role="text",
        name="Search",
        states={"VISIBLE", "SHOWING", "ENABLED", "FOCUSABLE", "EDITABLE"},
        frame=(20, 40, 300, 36),
    )
    _FakeAtspi.desktop = _FakeAtspiAccessible(
        role="desktop",
        children=[_FakeAtspiAccessible(role="application", name="Fake App", children=[text_field])],
    )
    helper, _remote_desktop = _portal_helper(wayland_helper_module)

    result = helper.ax_action(
        {
            "session_id": "sess-1",
            "target": {"role": "text", "title": "Search"},
            "operation": "set_value",
            "value": "hello",
        }
    )

    assert text_field.set_text_values == ["hello"]
    assert result["operation"] == "set_value"
    assert result["target"]["path"] == [0, 0]
    assert result["target"]["text"] == "hello"
