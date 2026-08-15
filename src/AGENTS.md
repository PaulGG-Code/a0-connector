# Source Tree DOX

## Purpose

- Own Python source code under `src/`.
- Route CLI package work to the narrower `agentic_job_cli` contract.

## Ownership

- `src/agentic_job_cli/` is the only durable package in this tree and has its own child DOX.
- This doc owns source-tree level packaging assumptions and any future sibling packages under `src/`.

## Local Contracts

- The root `pyproject.toml` builds `src/agentic_job_cli` into the `aj` distribution.
- Keep package imports compatible with Python 3.10+.
- Do not add platform-specific desktop libraries to source-tree imports unless guarded so other platforms can still import the CLI.

## Work Guidance

- Prefer package-relative imports matching the current `agentic_job_cli` style.
- Keep implementation details in the package subtree rather than adding source-tree globals.

## Verification

- `./.venv/bin/python -m pytest tests/ -v`

## Child DOX Index

- `agentic_job_cli/AGENTS.md` - Main Textual CLI, connector client, commands, remote tools, and local UI contracts.
