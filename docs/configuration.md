# Configuration

## Environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `AGENT_ZERO_HOST` | Agent Zero base URL | `http://localhost:5080` |
| `AGENT_ZERO_DEFAULT_CONTEXT_ID` / `A0_DEFAULT_CHAT` | Chat context to open after connecting | Last remembered chat for the host, then a new chat |
| `AGENT_ZERO_REMOTE_EXEC_ENABLED` / `A0_REMOTE_EXEC` | Start with host-side remote execution enabled | disabled |
| `A0_UPDATE_CHECK` | Startup check for a newer CLI release. Set to `0`, `false`, `no`, or `off` to disable. | enabled |

## Resolution order

For `AGENT_ZERO_HOST`:

1. `a0 --host URL`
2. Process environment
3. `~/.agent-zero/.env`
4. Builtin default `http://localhost:5080`

`AGENT_ZERO_API_KEY` is ignored. The CLI no longer reads, writes, or uses it.

For the initial chat:

1. `a0 --chat CONTEXT_ID`
2. `AGENT_ZERO_DEFAULT_CONTEXT_ID` or `A0_DEFAULT_CHAT`
3. The last remembered chat for the connected host
4. A new chat

`a0 --chat-last` skips any configured default chat and uses the last remembered
chat for the host.

For frontend remote execution, the CLI no longer runtime-imports a local Agent Zero Core checkout. The backend sends execution settings in the WebSocket `connector_hello` payload, and the CLI keeps the platform-specific shell and TTY logic locally.

## First-run behavior

1. Every launch starts at the picker and begins Docker-only local discovery in the background.
2. If there is exactly one detected local Agent Zero endpoint and no conflicting saved manual host, the CLI auto-enters it.
3. Open instances connect immediately.
4. Protected instances advance to login unless an in-memory session is already valid.
5. Manual entry follows the same host rules.
6. With `Remember this host` enabled, a successful connection writes only `AGENT_ZERO_HOST` to `~/.agent-zero/.env` and removes any stale `AGENT_ZERO_API_KEY`.
7. Successful chat selection remembers the active chat for that host.
8. Explicit disconnect clears the in-memory session cookie jar, attempts `/logout`, and returns to login for protected hosts or host selection for open hosts.

## Local discovery

- The startup picker only inspects Docker. It does not probe arbitrary localhost ports.
- A container is considered an Agent Zero candidate only when it is running, publishes `80/tcp`, and exposes at least one Agent Zero signal such as:
  - an image name containing `agent-zero`
  - a command or entrypoint containing `/exe/initialize.sh` or `run_ui.py`
  - a bind mount targeting `/a0`
- Wildcard Docker bindings such as `0.0.0.0`, `::`, or empty host bindings are shown as `http://localhost:<port>`.
- If Docker discovery shows `localhost`, prefer keeping `AGENT_ZERO_HOST` on `localhost` too. Mixing `localhost` and `127.0.0.1` can trigger host and origin mismatches for the session login or WebSocket flow.
- `a0 --no-auto-connect` keeps the picker open even when Docker finds exactly one local instance.
- `a0 --no-docker-discovery` skips Docker inspection and opens manual URL entry immediately.

## Persisted file

Path: `~/.agent-zero/.env`

- Created only when `Remember this host` is enabled
- Read on next launch to seed the picker, manual URL, and single-instance auto-enter decisions
- Stores `AGENT_ZERO_HOST` when the host is remembered, plus the last active chat host/context after chat selection
- Never stores usernames, passwords, session cookies, or tokens
