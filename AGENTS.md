# Agentic Job Connector - DOX Rail

## Purpose

- Define the project-wide DOX contract for `aj-connector`.
- Keep every source file, durable document, workflow, and artifact understandable from this root `AGENTS.md` plus the nearest child `AGENTS.md`.
- Preserve the connector's two-part product shape: the `aj` Textual CLI in this repo, and the builtin `_aj_connector` Agentic Job Core plugin outside this repo.

## Ownership

- This root doc owns repo-wide behavior, safety, verification, top-level files, packaging metadata, installers, and the Child DOX Index.
- Top-level files owned here include `README.md`, `pyproject.toml`, `requirements.txt`, `install.sh`, `install.ps1`, `test_context_patch.txt`, `.gitignore`, `LICENSE`, and any future root-level release or packaging files.
- Child docs own the scoped rules for `src/`, `packages/`, `tests/`, `docs/`, `devtools/`, `requirements/`, and `constraints/`.
- Generated or local-only artifacts such as `.venv/`, `.pytest_cache/`, `tmp/`, `.tmp-tests/`, `textual.log`, `__pycache__/`, and generated snapshots are not durable DOX scopes.

## Local Contracts

### DOX Framework

- `AGENTS.md` files are binding work contracts for their subtrees.
- Before editing, read this root doc, identify every path you expect to touch, then read every `AGENTS.md` from the repo root to each target path.
- Do not rely on memory. Re-read the applicable DOX chain in the current session before editing.
- If a parent `AGENTS.md` lists a child whose scope contains the path, read that child and continue from there.
- The nearest `AGENTS.md` controls local details. Child docs may specialize parent rules but may not weaken DOX itself.
- After every meaningful change, run a DOX pass: re-check changed paths against the DOX chain, update the nearest owning docs and affected parent or child indexes, remove stale or contradictory instructions, and run relevant verification.
- Update docs when a change affects purpose, ownership, structure, workflow, contracts, inputs, outputs, permissions, constraints, side effects, artifacts, quality standards, communication preferences, or any `AGENTS.md` scope/index.
- Small edits that do not change behavior or contracts may leave docs unchanged, but the DOX pass still happens.

### Product Contracts

- Tech stack: Python 3.10+, Textual 8+, `httpx`, `aiohttp`, `python-socketio` / Engine.IO.
- Run the TUI with `aj` or `./.venv/bin/python -m agentic_job_cli`.
- Launcher direct-connect path is `a0 --host <local-url> --no-docker-discovery --connect`; `--host` selects the target URL, `--no-docker-discovery` skips Docker discovery, and `--connect` connects immediately instead of opening the host picker.
- Run the plain stdin/stdout connector with `aj headless`; use
  `aj headless --print` for one-shot pipe-friendly runs.
- Run the Launcher-owned tools-only connector with `aj gateway`. It is a
  Textual-free, newline-delimited JSON stdin/stdout contract and must not create,
  select, or subscribe to a chat.
- Interactive transcript images use `AJ_CLI_IMAGE_MODE=auto|tgp|sixel|halfcell|off`.
  Automatic selection combines reliable terminal capability advertisements,
  live protocol probes, and compatibility exclusions for terminals that report
  only a protocol subset. A false-positive native probe must fall back cleanly
  instead of emitting protocol placeholder glyphs.
  Images open in their expanded complete-aspect view and may be collapsed with
  click, Enter, or Space.
  Explicit `halfcell`, browser preview, and SVG snapshot paths use a real
  half-cell widget without native protocol probes; pytest's ordinary TUI path
  remains library-free. Preview output is layout evidence and does not establish
  native TGP/Sixel acceptance. Keep automated CLI, Core deployment, and
  capable-terminal visual evidence as separate surfaces.
- Gateway release 2.6 adds `computer_use_setup_v1`: correlated setup commands,
  staged macOS Accessibility then Screen Recording approval, and fresh-helper
  polling bounded to 120 seconds so the initiating agent tool call can resume.
- Release installs include the Python Playwright client needed to launch a host
  Chromium-family profile. They do not download a separate Chromium binary;
  Browser setup and `/browser repair` remain recovery paths for older or damaged
  CLI environments.
- Use Linux commands and paths by default. Prefer `./.venv/bin/python`, not Windows-only virtualenv paths.
- UI preview is the primary loop for TUI work: `./.venv/bin/python devtools/serve.py` at `http://localhost:8566`.
- The CLI talks to Agentic Job through the connector protocol `aj-connector.v1`, HTTP routes under `/api/plugins/_aj_connector/v1/`, and Socket.IO events on namespace `/ws` with `connector_*` event names.

### Plugin Backend

- The builtin `_aj_connector` plugin is not vendored here. It lives in Agentic Job Core under `plugins/_aj_connector`.
- For this workstation, the real Agentic Job Core plugin repo is `/home/eclypso/aj/agent-zero/plugins`.
- When testing Dockerized Agentic Job backend behavior, verify the exact live runtime named for the task instead of assuming a fixed localhost port.
- When explicitly asked or approved to change plugin/backend code outside this repo, keep the live runtime copy and `/home/eclypso/aj/agent-zero/plugins` in sync.
- Plugin code must not import `agent`, `initialize`, or `helpers.projects` at module level. Import Agentic Job internals inside handler methods.
- In plugin `api/ws_connector.py`, `from_sequence` is a log-output cursor (`LogOutput.end`), not a connector event sequence. Do not mix cursor and event sequence domains.
- Large chat history must replay through bounded `connector_context_snapshot` pages before live streaming. Do not send full transcripts in a single WebSocket frame or turn old history into live `connector_context_event` messages.

### Safety And Permissions

- Allowed without asking: read files, edit repo source/docs/tests/devtools/requirements/constraints/AGENTS docs, run devtools scripts, and run pytest.
- Ask before installing new dependencies, editing external Agentic Job plugin/backend files, deleting files outside normal generated outputs, or making git commits/pushes.
- Never hardcode API keys, tokens, passwords, cookies, or connector secrets.
- Do not persist usernames, passwords, connector tokens, or API keys. Protected
  Agentic Job web sessions may persist browser-style session cookies only through
  the existing remembered-host/session flow.
- Never use destructive git commands such as `git reset --hard` or `git checkout --` unless the user explicitly asks.
- Preserve user work. If the worktree contains unrelated changes, leave them alone.

## Work Guidance

- Prefer `rg` and `rg --files` for search.
- Use `apply_patch` for manual file edits.
- Keep code enterprise/research quality: minimal, adapted to local style, robust, and testable.
- Prefer existing project patterns and helper APIs over new abstractions.
- For structured data, use structured parsers/APIs where available.
- Keep UI changes visually verified. Text must fit, avoid incoherent overlap, and remain usable in the Textual browser preview.
- Record durable user behavior preferences in this root doc or the closest relevant child doc.

## User Preferences

- The operating shell is `bash` on Ubuntu Linux.
- Prefer Linux paths and command examples unless a Windows or macOS-specific file requires platform-specific wording.
- Treat plugin/backend discussion as connected to the explicitly named Dockerized Agentic Job runtime when one is in scope.
- Always mirror live Agentic Job Core plugin runtime changes into `/home/eclypso/aj/agent-zero/plugins` when backend/plugin changes are in scope.
- Aim for solutions that unite rigor and elegance: concise, technically strong, and beautiful in the small details.

## Verification

- Full test suite: `./.venv/bin/python -m pytest tests/ -v`.
- If anyio backend issues appear: `./.venv/bin/python -m pytest tests/ -v -p anyio --anyio-backends=asyncio`.
- UI preview: `./.venv/bin/python devtools/serve.py`.
- Static TUI snapshot: `./.venv/bin/python devtools/snapshot.py`.
- Dependency lock check, when dependency files change and `uv` is available: `./.venv/bin/python devtools/lock_dependencies.py --check`.

## Child DOX Index

- `src/AGENTS.md` - Python source tree and package routing.
- `packages/AGENTS.md` - Platform computer-use backend packages.
- `tests/AGENTS.md` - Test suite, fixtures, fakes, and async test conventions.
- `docs/AGENTS.md` - Durable documentation in `docs/`.
- `devtools/AGENTS.md` - Browser preview, snapshots, and dependency lock tooling.
- `requirements/AGENTS.md` - Human-edited dependency input files.
- `constraints/AGENTS.md` - Generated release dependency lock files.
