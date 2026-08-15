# Computer-Use Packages DOX

## Purpose

- Own platform-specific computer-use backend packages under `packages/`.
- Keep backend entry points, helper runtimes, feature metadata, and trust-mode behavior aligned with the main CLI contract.

## Ownership

- Each `aj-computer-use-*` package owns its `pyproject.toml`, platform detection, backend spec, helper/runtime code, and package README where present.
- The main CLI contract for backend discovery and capability mapping lives in `src/agentic_job_cli/computer_use_backend.py`.

## Local Contracts

- Backends expose `ComputerUseBackendSpec` through the `aj.computer_use_backends` entry point group or an explicit install helper.
- Capability metadata must be derived from feature flags through `computer_use_capabilities_from_features(...)` where the package uses the shared contract.
- Helper runtimes communicate through stdio JSON and must report backend ID, family, features, support reason, contract version, and capabilities when supported by the backend.
- Mutating desktop actions must respect trust modes and platform permission/rearm semantics.
- Keep platform-only dependencies behind package-specific dependencies or environment markers.
- The root wheel currently embeds Wayland, macOS, and Windows backend packages. The X11 package exists separately and remote X11 is disabled in the CLI unless an explicit product decision changes that.

## Work Guidance

- Avoid importing desktop-control libraries from cross-platform CLI modules.
- Keep detection cheap and safe to import on unsupported systems.
- Keep feature lists conservative. Advertising a feature is a contract with Agentic Job Core.
- Add or adjust tests whenever backend metadata, trust modes, helper protocol, or feature flags change.

## Verification

- Contract checks: `./.venv/bin/python -m pytest tests/test_computer_use_contract.py -v`.
- Package checks: `./.venv/bin/python -m pytest tests/test_wayland_backend_package.py tests/test_x11_backend_package.py tests/test_macos_backend_package.py tests/test_windows_computer_use_backend.py tests/test_macos_computer_use_backend.py -v`.

## Child DOX Index

- `aj-computer-use-wayland/AGENTS.md` - Wayland portal, screencast, and AT-SPI backend package.
- `aj-computer-use-x11/AGENTS.md` - X11 helper package and focus-risk backend.
- `aj-computer-use-macos/AGENTS.md` - macOS Accessibility/CoreGraphics backend package.
- `aj-computer-use-windows/AGENTS.md` - Windows UI Automation and screen capture backend package.
