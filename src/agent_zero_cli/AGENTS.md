# Agent Zero CLI DOX

## Purpose

- Own the `agent_zero_cli` Textual application, connector transport client, slash commands, local state, host browser bridge, remote file/exec tools, model/profile/project commands, and computer-use orchestration.

## Ownership

- Root package files such as `app.py`, `client.py`, `config.py`, `connection.py`, `event_handlers.py`, `chat_commands.py`, `browser_commands.py`, `computer_use.py`, `computer_use_backend.py`, `host_browser*.py`, `remote_files.py`, `remote_exec.py`, `model_*.py`, `project_*.py`, `profile_commands.py`, and `self_update.py` are owned here.
- UI widgets, screens, and TCSS are owned by child docs in `widgets/`, `screens/`, and `styles/`.
- `assets/` is currently empty; keep it root-package owned until it becomes a durable asset boundary.

## Local Contracts

- `A0Client` owns HTTP, login/session cookies, Socket.IO setup, connector event registration, and the `a0-connector.v1` protocol constants.
- Keep `aiohttp.ClientWSTimeout` compatibility in `client.py` unless all supported aiohttp versions have been verified.
- Remote file, exec, computer-use, and browser operation handlers must emit their `connector_*_op_result` event before follow-up metadata refresh work starts.
- Use the client after-result callbacks for browser and computer-use status refreshes so server-side pending operations resolve before any nested `connector_hello` round trip.
- `/computer-use on` is a human approval command. It must force `ComputerUseManager.rearm()` immediately instead of silently validating a saved restore token first.
- Host-browser `open` must reuse an already-open tab with the same normalized URL before creating a new tab. Keep `list` and `set_active` workflows available for title-based or URL-based selection.
- The CLI may remember host/context and computer-use settings, but it must not persist usernames, passwords, session cookies, connector tokens, or other secrets.
- Remote workspace tools must respect their write/exec enablement flags and must not widen filesystem access accidentally.

## Work Guidance

- Query widgets with typed `query_one` calls, for example `self.query_one("#message-input", ChatInput)`.
- Route activity state through app-level helpers such as `_set_activity(...)` and `_set_idle()` rather than reaching into `ChatInput` from scattered event handlers.
- Keep `AgentZeroCLI` as the composition/orchestration surface; put command behavior in the focused command modules when that pattern already exists.
- Normalize server payloads defensively. The connector must tolerate older or partially-capable Agent Zero Core builds with user-facing errors.
- Keep command names, footer shortcuts, slash commands, and README/docs in sync when user-facing behavior changes.

## Verification

- Broad CLI checks: `./.venv/bin/python -m pytest tests/test_app.py tests/test_client.py -v`.
- Remote tools: `./.venv/bin/python -m pytest tests/test_remote_files.py tests/test_remote_exec.py -v`.
- Browser bridge: `./.venv/bin/python -m pytest tests/test_host_browser.py -v`.
- Computer use orchestration: `./.venv/bin/python -m pytest tests/test_computer_use.py tests/test_computer_use_contract.py -v`.
- Install/update/config paths: `./.venv/bin/python -m pytest tests/test_entrypoint.py tests/test_installers.py tests/test_self_update.py tests/test_instance_discovery.py -v`.

## Child DOX Index

- `widgets/AGENTS.md` - Reusable Textual widgets and chat rendering surfaces.
- `screens/AGENTS.md` - Modal and full-screen Textual screen contracts.
- `styles/AGENTS.md` - TCSS layout and visual styling rules.
