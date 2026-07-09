# Agent Zero CLI DOX

## Purpose

- Own the `agent_zero_cli` Textual application, headless frontend, connector transport/session client, slash commands, local state, host browser bridge, remote file/exec tools, model/profile/project commands, and computer-use orchestration.

## Ownership

- Root package files such as `app.py`, `client.py`, `config.py`, `connection.py`, `session.py`, `protocol.py`, `event_handlers.py`, `chat_commands.py`, `goal_commands.py`, `browser_commands.py`, `computer_use.py`, `computer_use_backend.py`, `host_browser*.py`, `remote_files.py`, `remote_exec.py`, `model_*.py`, `project_*.py`, `profile_commands.py`, `self_update.py`, and `textual_compat.py` are owned here.
- `headless/` is owned here and must remain importable without Textual.
- UI widgets, screens, and TCSS are owned by child docs in `widgets/`, `screens/`, and `styles/`.
- `assets/` is currently empty; keep it root-package owned until it becomes a durable asset boundary.

## Local Contracts

- `A0Client` owns HTTP, login/session cookies, Socket.IO setup, connector event registration, and the `a0-connector.v1` protocol constants.
- Keep `aiohttp.ClientWSTimeout` compatibility in `client.py` unless all supported aiohttp versions have been verified.
- Remote file, exec, computer-use, and browser operation handlers must emit their `connector_*_op_result` event before follow-up metadata refresh work starts.
- Use the client after-result callbacks for browser and computer-use status refreshes so server-side pending operations resolve before any nested `connector_hello` round trip.
- Refresh the active chat tab metadata after context completion so server-side automatic chat renames become visible in the TUI.
- `/computer-use on` is a human approval command. It must force `ComputerUseManager.rearm()` immediately instead of silently validating a saved restore token first.
- Host-browser `open` must reuse an already-open tab with the same normalized URL before creating a new tab. Keep `list` and `set_active` workflows available for title-based or URL-based selection.
- Host-browser metadata must advertise stable browser choices, and incoming `browser_selection` / `host_browser_selection` values must select that browser instead of falling back to the automatic profile picker.
- Host-browser discovery covers major Chromium-family browsers with CDP-compatible profiles, including Chrome, Chromium, Edge, Brave, Opera, and Vivaldi.
- `/browser list`, `/browser auto`, and direct `/browser <number|id|ws://...>` own CLI-side host-browser target selection for the current Agent Zero project.
- `/goal <objective>` creates the active chat goal through the builtin `_goal` plugin and sends the objective to the agent; `/goal update <text>` and `/goal delete` mutate goal state without sending a message.
- The CLI may remember host/context and computer-use settings, and protected web sessions may persist browser-style session cookies through the remembered-host/session flow. It may consume ephemeral `A0_USERNAME` and `A0_PASSWORD` environment variables for non-interactive login, but it must not persist usernames, passwords, connector tokens, API keys, or other secrets.
- Local Docker instance discovery should prefer launcher-owned friendly names
  from the `a0.launcher.instanceName` container label over generated Docker
  container or clone image names in visible picker/login text.
- Local Docker instance discovery should try reachable Unix-socket Docker API
  endpoints from `DOCKER_HOST`, Docker contexts, and known local runtimes such as
  Colima profiles before declaring the runtime unavailable.
- On Windows, local Docker instance discovery must not require `docker.exe` on
  the host PATH. Try reachable local Docker API endpoints such as the
  launcher/WSL Engine bridge before falling back to WSL-hosted Docker commands
  through `wsl.exe`.
- Remote workspace tools must respect their write/exec enablement flags and must not widen filesystem access accidentally.
- Textual compatibility guards live in `textual_compat.py`. Install them only on the interactive TUI startup path so `a0 headless` remains Textual-free.

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
