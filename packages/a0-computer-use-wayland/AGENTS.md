# Wayland Computer-Use DOX

## Purpose

- Own the Linux Wayland computer-use backend package.

## Ownership

- `pyproject.toml`, `README.md`, and `src/a0_computer_use_wayland/` are owned here.
- The package provides Wayland portal remote desktop/screencast control, AT-SPI accessibility tree/action support, and native window metadata.

## Local Contracts

- `WAYLAND_BACKEND_SPEC` uses backend ID `wayland`, family `linux`, priority `100`, `interpreter_strategy="system_python"`, and helper target `computer_use_helper.py`.
- Trust modes are `interactive`, `persistent`, and `allow`.
- Preserve the feature contract for portal capture/control, inline PNG capture, fresh-frame capture, normalized coordinates, pointer/keyboard injection, AT-SPI tree/action/set-value, native window listing/state, element index targeting, background dispatch, and foreground fallback.
- Keep DBus, GI, GStreamer, Pillow, and AT-SPI imports inside this package/helper path, not in shared CLI import paths.

## Work Guidance

- Prefer defensive handling around portal permission/session state; user approval is part of the runtime contract.
- Keep helper output machine-readable and avoid noisy stdout that would corrupt stdio JSON.
- Treat coordinate-space changes as protocol changes and update tests/docs accordingly.

## Verification

- `./.venv/bin/python -m pytest tests/test_wayland_backend_package.py tests/test_computer_use_contract.py -v`

## Child DOX Index
