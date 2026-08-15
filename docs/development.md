# Development

## Repo layout

```
aj-connector/
├── src/agentic_job_cli/     # CLI (Textual, httpx, python-socketio)
├── packages/               # Embedded computer-use backend source and metadata
├── tests/                  # pytest
└── docs/                   # You are here
```

The builtin `_aj_connector` plugin is not vendored in this repository. Backend
changes happen directly in Agentic Job Core under `plugins/_aj_connector` (or
`/aj/plugins/_aj_connector` in Docker).

## Runtime setup options

- Local Agentic Job checkout: builtin plugin path `<agent-zero>/plugins/_aj_connector`
- Dockerized Agentic Job: builtin plugin path `/aj/plugins/_aj_connector`

## Setup

### Plugin runtime

```bash
cd /path/to/agent-zero
python run_ui.py --host=127.0.0.1 --port=50001
```

Edit the builtin `_aj_connector` plugin in that Agentic Job checkout directly, then restart Agentic Job. End users should get `_aj_connector` from Agentic Job Core as a builtin plugin.

To test a protected instance, start Agentic Job with `AUTH_LOGIN` and `AUTH_PASSWORD` configured in its runtime `.env`.

### CLI

The root editable install includes the embedded computer-use backends from
`packages/`, matching the release wheel model where the CLI and local
computer-use support update as one unit.

Windows PowerShell:

```powershell
py -3.10 -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -e .
$env:AGENT_ZERO_HOST = "http://localhost:50001"
a0
```

Linux / Wayland:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
export AGENT_ZERO_HOST=http://localhost:50001
a0
```

One-off host overrides can also be passed directly:

```bash
a0 --host http://localhost:50001
```

macOS:

```bash
uv venv --python 3.11 .venv && source .venv/bin/activate
pip install -e .
export AGENT_ZERO_HOST=http://localhost:50001
a0
```

When you are developing against a Docker-detected local Agentic Job instance, prefer `localhost` over `127.0.0.1` so the saved host matches the discovered host exactly.

For connection-flow testing, `a0 --no-auto-connect` keeps the picker open when a single Docker instance is detected, and `a0 --no-docker-discovery` opens the manual URL path without inspecting Docker.

The published `aj` wheel embeds the Wayland, macOS, and Windows remote computer-use backend modules. Environment markers install only the third-party runtime libraries relevant to the current platform. Linux remote host control uses the Wayland portal backend; X11/Xpra automation is maintained in Agentic Job Core's internal Docker Desktop tooling instead of the AJ CLI host connector.

The sibling `packages/aj-computer-use-*` manifests remain useful for isolated backend package development, but end-user installs should use the root `aj` package.

The standalone installers and `aj update` default to a managed CPython 3.12
runtime via `uv`, so end users do not need a preinstalled Python 3.10+ on the
host to get a consistent tool environment. The updater resolves the latest
published GitHub release at runtime instead of baking the current tag into the
installed CLI, then installs with the runtime and build constraints committed to
that same release.

Runtime dependencies are locked as release artifacts:

```bash
./.venv/bin/python devtools/lock_dependencies.py
./.venv/bin/python devtools/lock_dependencies.py --check
```

Edit `requirements/a0-runtime.in` or `requirements/a0-build.in`, regenerate the
constraints, and commit the updated `constraints/` files plus the synced
`pyproject.toml` pins together. The package metadata is intentionally exact
pinned because `aj` is a CLI app installed into an isolated `uv tool`
environment, and it protects users who update from older unpinned CLIs.

### Backend source of truth

There is no repo-local mirror to sync. The source of truth for backend work is
your Agentic Job Core/runtime copy of `plugins/_aj_connector`. The tests in this
repo resolve that plugin from `AJ_CONNECTOR_PLUGIN_ROOT` when set, otherwise
from a sibling `../agent-zero/plugins/_aj_connector` checkout if present.

## Tests

```bash
pip install pytest
pytest tests/ -v
```

Uses `anyio` with the **asyncio** backend. If you see trio-related errors:

```bash
pytest -p anyio --anyio-backends=asyncio
```

## Dev patterns

### Plugin import paths

Agentic Job loads plugins by file path. All imports use the full path:

```python
import plugins._aj_connector.api.v1.base as connector_base
```

`test_plugin_backend.py` stubs the `plugins` namespace to validate these imports work.

### Lazy imports

Never import `initialize`, `agent`, or `helpers.projects` at module level in plugin code.

```python
# BAD
from agent import AgentContext

# GOOD
async def process(self, ...):
    from agent import AgentContext
```

### aiohttp compatibility shim

`client.py` patches `aiohttp.ClientWSTimeout` if missing. This keeps the Engine.IO WebSocket transport working across supported aiohttp versions.
