# a0-connector

Terminal connector for [Agent Zero](https://github.com/frdel/agent-zero). It pairs a Textual CLI with a small Agent Zero plugin so you can chat from the terminal, follow streaming events, and use the connector-specific remote editing/runtime features.

## Components

| Component | Location | Purpose |
|-----------|----------|---------|
| CLI (`a0`) | `src/agent_zero_cli/` | Terminal UI and session-aware transport client |
| Plugin (`_a0_connector`) | Agent Zero Core `plugins/_a0_connector` | Builtin plugin that exposes the connector HTTP + Socket.IO surface |

The CLI requires an Agent Zero build that includes the builtin `_a0_connector` plugin.

## Install

### 1. Install on macOS / Linux

```bash
curl -LsSf https://raw.githubusercontent.com/agent0ai/a0-connector/main/install.sh | sh
```

### 2. Install on Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/agent0ai/a0-connector/main/install.ps1 | iex
```

### 3. Run

```bash
a0
```

Computer-use backends are embedded in the `a0` wheel, so the CLI and local computer-use support install and update together. Linux backend selection is automatic: Wayland sessions use the portal backend, while Xorg/X11 sessions use the X11 backend.

## Manual install

If you already use `uv`, the installer and update flow resolve the latest
published GitHub release at runtime. They default to a managed CPython 3.11
tool environment across macOS, Linux, and Windows, and install with the
dependency locks committed to the same A0 release. `uv` can download the
managed Python automatically without requiring `git` to be installed:

```bash
curl -LsSf https://raw.githubusercontent.com/agent0ai/a0-connector/main/install.sh | sh
```

Set `A0_PYTHON_SPEC` if you need to override that interpreter request. If you
set `A0_PACKAGE_SPEC` for a custom package source, also set
`A0_RUNTIME_CONSTRAINTS` and `A0_BUILD_CONSTRAINTS`; use
`A0_ALLOW_UNPINNED_UPDATE=1` only for intentional development installs.
Advanced one-off runs with `uvx` also work, but they are intentionally not the
primary install path for this project.

## Update

If you installed `a0` with the standard `uv tool` flow, update it in place with:

```bash
a0 update
```

By default `a0 update` resolves the latest published GitHub release at runtime,
downloads that release's runtime and build constraints, and installs it into
the managed CPython 3.11 tool runtime used by the installer. The updater
upgrades A0 itself while keeping dependencies pinned to the tested release set.
For advanced cases you can override the interpreter request with
`A0_PYTHON_SPEC`, or provide `A0_PACKAGE_SPEC` together with
`A0_RUNTIME_CONSTRAINTS` and `A0_BUILD_CONSTRAINTS`.

`a0 update` requires `uv` to be available on your `PATH`.

## Agent Zero Core

No separate plugin install is required for users once Agent Zero Core ships `_a0_connector` as a builtin plugin.

This repo does not contain a vendored plugin copy. For Core development, edit the builtin plugin directly in your Agent Zero checkout/runtime copy and restart Agent Zero after changes:

- Local Agent Zero checkout: `<agent-zero>/plugins/_a0_connector`
- Docker-based Agent Zero runtime: `/a0/plugins/_a0_connector`

## Connect

```bash
a0
```

On every launch the CLI opens the host picker first. It checks Docker for local Agent Zero containers, lists any detected WebUI endpoints as friendly URLs such as `http://localhost:50001`, and lets you connect explicitly with Enter or the `Connect` button.

If Docker finds exactly one local Agent Zero endpoint and there is no conflicting saved manual host, the CLI auto-enters that instance:
- open instance: it connects immediately
- protected instance: it advances directly to the login stage

Manual URL entry is available from the same panel for remote hosts or anything Docker cannot see. `AGENT_ZERO_HOST` still seeds the picker/manual URL instead of forcing an immediate connection.

Protected instances use the same web login as Agent Zero itself. The CLI posts to `/login`, keeps the resulting session cookie in memory for the current process, and forwards that session to `/ws`. Open instances skip the login stage entirely.

If you want to prefill a host, export it before starting the CLI:

```bash
export AGENT_ZERO_HOST=http://localhost:50001
a0
```

Or pass it directly for a one-off launch:

```bash
a0 --host http://localhost:5080
```

Use `a0 --no-auto-connect` to keep the picker open even when Docker finds exactly one local instance. Use `a0 --no-docker-discovery` to skip Docker inspection and open manual URL entry immediately, which is useful for remote hosts, HTTPS tunnels such as Cloudflare, or machines without Docker.

You can optionally remember only the chosen host in `~/.agent-zero/.env` from inside the app. The CLI never stores usernames, passwords, session cookies, or connector tokens.

## Usage

### Footer shortcuts

| Shortcut | Action |
|----------|--------|
| `F6` | List chats |
| `F7` | Nudge agent |
| `F8` | Pause / resume |
| `Ctrl+P` | Command palette |

### Slash commands

| Command | Action |
|---------|--------|
| `/help` | Show available commands |
| `/chats` | Switch chats |
| `/clear` | Clear the visible chat log |
| `/new` | Start a new chat |
| `/compact` | Compact the current chat when supported |
| `/nudge` | Nudge the current agent run |
| `/pause` / `/resume` | Pause or resume the active agent run |
| `/presets` | Pick a model preset |
| `/models` | Override runtime models for the current chat |
| `/computer-use on` / `/computer-use off` | Advertise or disable local Computer Use from this CLI; enabling arms the platform permission flow when needed |
| `/browser status` | Show host-browser connector status |
| `/browser host on` / `/browser host off` | Advertise or disable host-browser control from this CLI and sync Agent Zero Browser mode when supported |
| `/browser profile` | List detected Chrome-family profiles; pass `<family> <profile>` to select, for example `chrome-a0 Default` |
| `/browser relaunch` | Prepare the host browser now, either by attaching to allowed Chrome remote debugging or by starting the selected local profile |
| `/browser repair` | Install missing Python Playwright for the local-profile launch path |
| `/browser privacy` | Show where host-browser content policy is configured |
| `/disconnect` | Disconnect and return to the current host connection flow |
| `/keys` | Toggle key help |
| `/quit` | Exit |

### Skill shortcuts

Type `$` in the composer to browse skills available to the current chat. Selecting a skill, or submitting `$skill-name`, activates it for that chat; `$skill-name your prompt` activates the skill and sends `your prompt` immediately.

### Host browser mode

Agent Zero can route its existing `browser` tool through A0 CLI so the CLI controls a real Chrome-family browser on the host machine while the Agent Zero server still runs in Docker or another remote runtime.

Happy path:

1. Keep A0 CLI connected to the Agent Zero chat.
2. If you want Agent Zero to use your already-open personal Chrome, visit `chrome://inspect/#remote-debugging` and click **Allow**.
3. In Agent Zero Browser settings, choose `host_when_available` or `host_required`.
4. Ask the agent to use the browser.

When a subscribed CLI supports host-browser control, the first browser action can enable and prepare the local browser automatically. The CLI slash commands remain available for diagnostics and manual override.

The CLI does not bundle Chromium and it does not copy credentials, cookies, or profile data out of the browser profile. If Chrome's Remote debugging page has been allowed, A0 reads Chrome's local `DevToolsActivePort` file and keeps one DevTools Protocol connection open for browser actions. Status checks and profile listing do not connect to Chrome, so they should not create repeated **Allow** prompts.

If the selected profile must be launched by A0 and is already locked by normal Chrome, A0 reports `relaunch_required`; close that browser and retry the agent request or run `/browser relaunch` manually. This launch path uses Python Playwright in the A0 CLI host environment against an installed Chrome, Chromium, or Edge executable.

Chrome 136+ blocks Playwright remote debugging against the default personal Chrome data directory. If Chrome's own Remote debugging consent path is not available, A0 exposes and auto-selects a separate local profile such as `chrome-a0 Default` under the user's data directory. Site data stays in that local browser profile, and the user may need to sign in once there. You can still select it manually with `/browser profile chrome-a0 Default`.

The Playwright runtime and Chromium binary under the Agent Zero Docker container, such as `/a0/tmp/playwright`, belong to the container browser backend. They are useful when Browser settings use `container`, but they cannot control a host Chrome-family profile from inside Docker.

When automatic host-browser preparation, `/browser host on`, or `/browser relaunch` finds that Python Playwright is missing, the TUI shows the install command and runs the same repair automatically. You can also trigger it directly:

```bash
/browser repair
```

Platform caveats:
- macOS: detects Chrome, Chromium, and Edge apps in `/Applications` and profile data in `~/Library/Application Support`.
- Windows: detects Chrome, Chromium, and Edge under `%LOCALAPPDATA%`/Program Files profile conventions.
- Linux: detects `google-chrome`, `chromium`, `chromium-browser`, and Edge variants on `PATH`; X11 and Wayland are both supported by the underlying system browser.

## Troubleshooting

- `404` on `/api/plugins/_a0_connector/v1/capabilities`: the running Agent Zero build does not include the builtin `_a0_connector` plugin, or the local Core checkout/runtime copy is out of sync.
- Browser UI works but `a0` does not: the core web UI can run without the connector plugin; the CLI cannot.
- `Connector contract mismatch`: the server is advertising an older connector auth contract. Update Agent Zero Core so its builtin `_a0_connector` plugin matches the CLI.
- WebSocket connection rejected: ensure proxies forward both `/socket.io` and `/api/plugins/` unchanged, and that `AGENT_ZERO_HOST` exactly matches the real host seen by Agent Zero. If Docker discovery shows `localhost`, prefer `localhost` over `127.0.0.1`.
- `a0 update` says `uv` is required: Install `uv` or rerun the existing installer.
- `A0_PACKAGE_SPEC requires A0_RUNTIME_CONSTRAINTS and A0_BUILD_CONSTRAINTS`: custom package updates must provide matching lock files, or explicitly set `A0_ALLOW_UNPINNED_UPDATE=1` for a development-only unlocked install.

## Docs

- [Configuration](https://github.com/agent0ai/a0-connector/blob/main/docs/configuration.md)
- [Architecture](https://github.com/agent0ai/a0-connector/blob/main/docs/architecture.md)
- [Development](https://github.com/agent0ai/a0-connector/blob/main/docs/development.md)
- [TUI frontend](https://github.com/agent0ai/a0-connector/blob/main/docs/tui-frontend.md)
