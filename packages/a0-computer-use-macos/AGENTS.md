# macOS Computer-Use DOX

## Purpose

- Own the macOS computer-use backend package.

## Ownership

- `pyproject.toml` and `src/a0_computer_use_macos/` are owned here.
- The package provides Accessibility tree/action support, CoreGraphics capture/input paths, runtime session state, and macOS-specific detection.

## Local Contracts

- `MACOS_BACKEND_SPEC` uses backend ID `macos`, family `macos`, `interpreter_strategy="current_python"`, and helper target `runtime.py`.
- Trust modes and shared feature constants live in `shared.py`; keep backend metadata, runtime metadata, and tests aligned.
- Runtime responses must include contract version and capabilities derived from the shared feature list.
- Debug logging must stay opt-in through environment flags and must not leak secrets.

## Work Guidance

- Keep macOS framework imports isolated to this package/runtime path.
- Preserve user permission semantics around Accessibility and screen capture.
- Keep session restore-token handling normalized through shared helpers.

## Verification

- `./.venv/bin/python -m pytest tests/test_macos_backend_package.py tests/test_macos_computer_use_backend.py tests/test_computer_use_contract.py -v`

## Child DOX Index
