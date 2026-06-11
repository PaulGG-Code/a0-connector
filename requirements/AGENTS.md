# Requirements DOX

## Purpose

- Own human-edited dependency input files for release locking.

## Ownership

- `a0-runtime.in` owns runtime dependency intent.
- `a0-build.in` owns build dependency intent.
- Generated lock outputs are owned by `constraints/AGENTS.md`.
- Top-level `requirements.txt` is root-owned as a compatibility pointer to the runtime lock.

## Local Contracts

- Edit `.in` files for dependency intent; regenerate locks through `devtools/lock_dependencies.py`.
- Keep platform-specific dependencies guarded with environment markers.
- New dependencies require user approval before installation and must be justified by project needs.
- Dependency changes must keep `pyproject.toml`, `requirements/`, and `constraints/` coherent.

## Work Guidance

- Prefer the smallest dependency surface that solves the problem.
- Keep minimum-version constraints broad enough for locking but narrow enough to express real compatibility.

## Verification

- With `uv` available: `./.venv/bin/python devtools/lock_dependencies.py --check`

## Child DOX Index
