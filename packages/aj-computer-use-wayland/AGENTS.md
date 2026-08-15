# Wayland Computer-Use DOX

## Purpose

- Own the Linux Wayland computer-use backend package.

## Ownership

- `pyproject.toml`, `README.md`, and `src/aj_computer_use_wayland/` are owned here.
- The package provides Wayland portal remote desktop/screencast control, AT-SPI accessibility tree/action support, and native window metadata.

## Local Contracts

- `WAYLAND_BACKEND_SPEC` uses backend ID `wayland`, family `linux`, priority `100`, `interpreter_strategy="system_python"`, and helper target `computer_use_helper.py`.
- Trust modes are `interactive`, `persistent`, and `allow`.
- Preserve the feature contract for portal capture/control, inline PNG capture, fresh-frame capture, normalized coordinates, pointer/keyboard injection, AT-SPI tree/action/set-value, native window listing/state, element index targeting, background dispatch, and foreground fallback.
- Native window IDs identify AT-SPI frame/window/dialog nodes, not application wrappers. Window-scoped snapshots must stay within that node; foreground focus may target that window ID directly and must be observed after activation; keyboard text injection requires that same window to be active or focused.
- If a top-level XWayland window rejects AT-SPI `grab_focus`, use its unambiguous PID/title match through `wmctrl` and still require the same AT-SPI node to report active or focused before claiming success.
- Never choose an arbitrary AT-SPI action by index. Press only a recognized press/click action, and require the focus operation for application or window activation.
- Keep DBus, GI, GStreamer, Pillow, and AT-SPI imports inside this package/helper path, not in shared CLI import paths.

## Work Guidance

- Prefer defensive handling around portal permission/session state; user approval is part of the runtime contract.
- Keep helper output machine-readable and avoid noisy stdout that would corrupt stdio JSON.
- Treat coordinate-space changes as protocol changes and update tests/docs accordingly.

## Verification

- `./.venv/bin/python -m pytest tests/test_wayland_backend_package.py tests/test_computer_use_contract.py -v`

## Child DOX Index
