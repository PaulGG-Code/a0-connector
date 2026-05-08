from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_zero_cli.config import CLIConfig
from agent_zero_cli.host_browser import (
    BrowserCandidate,
    BrowserProfile,
    CONTENT_HELPER_PATH,
    HostBrowserManager,
    HostBrowserSession,
    ProfileLockState,
    RELAUNCH_CONTEXT_ID,
    a0_managed_user_data_dir,
    chromium_launch_args,
    content_helper_sha256,
    remote_debugging_endpoint_from_active_port_file,
    discover_remote_debugging_profiles,
    discover_profiles,
    is_profile_locked,
    normalize_remote_debugging_endpoint,
    parse_content_helper_payload,
    profile_lock_state,
    remote_debugging_restriction_reason,
)


pytestmark = pytest.mark.anyio

MINIMAL_CONTENT_HELPER_SOURCE = """
(() => {
  globalThis.__spaceBrowserPageContent__ = {
    annotate() {},
    boundingBoxFor() {},
    capture() {},
    detail() {},
    fileInputElementFor() {},
    fileInputFor() {},
    pointFor() {},
    select() {},
    setChecked() {},
  };
})();
"""


class FakeKeyboard:
    async def down(self, key: str) -> None:
        del key

    async def up(self, key: str) -> None:
        del key

    async def press(self, key: str) -> None:
        del key

    async def type(self, text: str) -> None:
        del text

    async def insert_text(self, text: str) -> None:
        del text


class FakeMouse:
    async def click(self, x: float, y: float, button: str = "left") -> None:
        del x, y, button

    async def dblclick(self, x: float, y: float, button: str = "left") -> None:
        del x, y, button

    async def move(self, x: float, y: float, steps: int | None = None) -> None:
        del x, y, steps

    async def down(self) -> None:
        return None

    async def up(self) -> None:
        return None

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        del delta_x, delta_y


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.keyboard = FakeKeyboard()
        self.mouse = FakeMouse()
        self.viewport_size = {"width": 1280, "height": 800}
        self.handlers = {}

    def on(self, event: str, callback) -> None:
        self.handlers[event] = callback

    async def goto(self, url: str, **_: object) -> None:
        self.url = url

    async def wait_for_load_state(self, *_: object, **__: object) -> None:
        return None

    async def title(self) -> str:
        return "Example"

    async def evaluate(self, script: str, arg: object = None) -> object:
        del arg
        if "history" in script:
            return 1
        if "__spaceBrowserPageContent__" in script and "Boolean" in script:
            return True
        return {"ok": True}

    async def screenshot(self, **kwargs: object) -> bytes:
        payload = b"fake-jpeg"
        path = kwargs.get("path")
        if path:
            Path(str(path)).write_bytes(payload)
        return payload

    async def close(self) -> None:
        return None

    async def set_viewport_size(self, viewport: dict[str, int]) -> None:
        self.viewport_size = dict(viewport)


class FakeContext:
    def __init__(self) -> None:
        self.pages = []
        self.handlers = {}
        self.closed = False
        self.init_scripts: list[str] = []

    def set_default_timeout(self, timeout: int) -> None:
        del timeout

    def set_default_navigation_timeout(self, timeout: int) -> None:
        del timeout

    def on(self, event: str, callback) -> None:
        self.handlers[event] = callback

    async def add_init_script(self, script: object = None, *, path: object = None) -> None:
        if script is not None:
            self.init_scripts.append(str(script))
        elif path is not None:
            self.init_scripts.append(Path(str(path)).read_text(encoding="utf-8"))
        return None

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True
        return None


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.contexts = [context]

    async def new_context(self, **_: object) -> FakeContext:
        context = FakeContext()
        self.contexts.append(context)
        return context


class FakeChromium:
    def __init__(self) -> None:
        self.launch_kwargs: dict[str, object] = {}
        self.cdp_endpoint = ""
        self.context = FakeContext()

    async def launch_persistent_context(self, **kwargs: object) -> FakeContext:
        self.launch_kwargs = dict(kwargs)
        return self.context

    async def connect_over_cdp(self, endpoint: str) -> FakeBrowser:
        self.cdp_endpoint = endpoint
        return FakeBrowser(self.context)


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeChromium()
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    async def start(self) -> FakePlaywright:
        return self.playwright


def test_discover_profiles_reads_local_state_names(tmp_path: Path) -> None:
    root = tmp_path / "Chrome"
    (root / "Default").mkdir(parents=True)
    (root / "Profile 1").mkdir()
    (root / "Local State").write_text(
        '{"profile":{"info_cache":{"Default":{"name":"Personal"},"Profile 1":{"name":"Work"}}}}',
        encoding="utf-8",
    )

    profiles = discover_profiles(BrowserCandidate("chrome", "Google Chrome", "/bin/echo", root))

    assert [(item.profile_label, item.display_name) for item in profiles] == [
        ("Default", "Personal"),
        ("Profile 1", "Work"),
    ]


def test_discover_profiles_exposes_a0_managed_profile_without_existing_root(tmp_path: Path) -> None:
    root = tmp_path / "a0-chrome"

    profiles = discover_profiles(
        BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", "/bin/echo", root)
    )

    assert len(profiles) == 1
    assert profiles[0].family == "chrome-a0"
    assert profiles[0].profile_label == "Default"
    assert profiles[0].profile_path == root


def test_a0_managed_user_data_dir_is_separate_from_default_chrome_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    path = a0_managed_user_data_dir("chrome")

    assert path == tmp_path / "data" / "a0/browser-profiles/chrome"
    assert path != tmp_path / "config" / "google-chrome"


def test_content_helper_source_is_owned_by_agent_zero_browser_plugin() -> None:
    assert not CONTENT_HELPER_PATH.exists()

    agent_zero_asset = (
        Path(__file__).resolve().parents[2]
        / "agent-zero"
        / "plugins"
        / "_browser"
        / "assets"
        / "browser-page-content.js"
    )
    if not agent_zero_asset.exists():
        pytest.skip("Agent Zero sibling repo is not available for content-helper contract")

    agent_zero_source = agent_zero_asset.read_text(encoding="utf-8")
    agent_zero_hash = content_helper_sha256(agent_zero_source)

    assert parse_content_helper_payload(
        {"content_helper": {"source": agent_zero_source, "sha256": agent_zero_hash}}
    ) == (agent_zero_source, agent_zero_hash)
    for api_name in (
        "annotate",
        "boundingBoxFor",
        "capture",
        "detail",
        "fileInputElementFor",
        "fileInputFor",
        "pointFor",
        "select",
        "setChecked",
    ):
        assert api_name in agent_zero_source


def test_remote_debugging_profile_is_discovered_when_chrome_allows_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "google-chrome"
    root.mkdir()
    (root / "DevToolsActivePort").write_text("9222\n/devtools/browser/test\n", encoding="utf-8")

    profiles = discover_remote_debugging_profiles(
        [BrowserCandidate("chrome", "Google Chrome", "/bin/chrome", root)]
    )

    assert (
        remote_debugging_endpoint_from_active_port_file(root / "DevToolsActivePort")
        == "ws://localhost:9222/devtools/browser/test"
    )
    assert (
        normalize_remote_debugging_endpoint("ws://127.0.0.1:9222/devtools/browser/test")
        == "ws://127.0.0.1:9222/devtools/browser/test"
    )
    assert len(profiles) == 1
    assert profiles[0].family == "chrome-cdp"
    assert profiles[0].profile_label == "localhost:9222"
    assert profiles[0].cdp_endpoint == "ws://localhost:9222/devtools/browser/test"
    assert profiles[0].as_dict()["locked"] is False


def test_remote_debugging_discovery_reads_active_port_without_network_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_cdp as host_browser_cdp_module

    root = tmp_path / "google-chrome"
    root.mkdir()
    (root / "DevToolsActivePort").write_text("9222\n/devtools/browser/test\n", encoding="utf-8")

    def fail_client_session(*_: object, **__: object) -> object:
        raise AssertionError("remote debugging discovery must not open network connections")

    monkeypatch.setattr(host_browser_cdp_module.aiohttp, "ClientSession", fail_client_session)

    profiles = discover_remote_debugging_profiles(
        [BrowserCandidate("chrome", "Google Chrome", "/bin/chrome", root)]
    )

    assert [profile.cdp_endpoint for profile in profiles] == [
        "ws://localhost:9222/devtools/browser/test"
    ]


def test_selected_profile_prefers_user_allowed_remote_debugging_over_a0_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_manager as host_browser_manager_module

    a0_root = tmp_path / "a0-chrome"
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    remote_profile = BrowserProfile(
        "chrome-cdp",
        "Chrome (remote debugging)",
        "",
        Path(),
        "127.0.0.1:9222",
        "Remote debugging allowed",
        cdp_endpoint="ws://127.0.0.1:9222/devtools/browser/test",
    )
    monkeypatch.setattr(host_browser_manager_module, "discover_remote_debugging_profiles", lambda *_: [remote_profile])
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_family="chrome-a0",
            host_browser_profile_path=str(a0_root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), a0_root)
        ],
        playwright_available=True,
    )

    selected = manager.selected_profile()

    assert selected is not None
    assert selected.family == "chrome-cdp"


def test_remote_debugging_profile_does_not_require_playwright(tmp_path: Path) -> None:
    root = tmp_path / "google-chrome"
    root.mkdir()
    (root / "DevToolsActivePort").write_text("9222\n/devtools/browser/test\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", "/bin/chrome", root)],
        playwright_available=False,
    )

    metadata = manager.hello_metadata()

    assert metadata["supported"] is True
    assert metadata["browser_family"] == "chrome-cdp"
    assert metadata["cdp_endpoint"] == "ws://localhost:9222/devtools/browser/test"


def test_saved_default_profile_uses_authorized_remote_debugging(tmp_path: Path) -> None:
    root = tmp_path / "google-chrome"
    (root / "Default").mkdir(parents=True)
    (root / "DevToolsActivePort").write_text("9222\n/devtools/browser/test\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_family="chrome",
            host_browser_profile_path=str(root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", "/bin/chrome", root)],
        playwright_available=False,
    )

    selected = manager.selected_profile()

    assert selected is not None
    assert selected.family == "chrome-cdp"
    assert selected.cdp_endpoint == "ws://localhost:9222/devtools/browser/test"


def test_profile_lock_detection_reports_singleton_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    lock = tmp_path / "SingletonLock"
    try:
        os.symlink("host-12345", lock)
    except (PermissionError, OSError, NotImplementedError):
        pytest.skip("symlink not supported in this environment")
    monkeypatch.setattr(host_browser_common_module, "_pid_is_alive", lambda pid: pid == 12345)

    state = profile_lock_state(tmp_path)

    assert is_profile_locked(tmp_path) is True
    assert state.locked is True
    assert state.owner_pid == 12345
    assert str(lock) in state.lock_files


def test_profile_lock_detection_ignores_stale_singleton_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    lock = tmp_path / "SingletonLock"
    try:
        os.symlink("host-12345", lock)
    except (PermissionError, OSError, NotImplementedError):
        pytest.skip("symlink not supported in this environment")
    (tmp_path / "SingletonCookie").symlink_to("cookie")
    monkeypatch.setattr(host_browser_common_module, "_pid_is_alive", lambda pid: False)

    state = profile_lock_state(tmp_path)

    assert is_profile_locked(tmp_path) is False
    assert state.locked is False
    assert state.owner_pid == 12345
    assert state.lock_files == ()


def test_chromium_launch_args_do_not_request_a_devtools_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("A0_HOST_BROWSER_OZONE_PLATFORM", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")

    args = chromium_launch_args("Default")

    assert args == ["--profile-directory=Default"]
    assert not any(arg.startswith("--remote-debugging-port=") for arg in args)


def test_chromium_launch_args_use_wayland_only_without_x_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("A0_HOST_BROWSER_OZONE_PLATFORM", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")

    args = chromium_launch_args("Default")

    assert "--ozone-platform=wayland" in args


def test_remote_debugging_restriction_blocks_default_chrome_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    config_root = tmp_path / "config"
    default_root = config_root / "google-chrome"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setattr(host_browser_common_module, "browser_major_version", lambda _: 147)
    profile = BrowserProfile("chrome", "Chrome", "/bin/chrome", default_root, "Default", "Default")

    reason = remote_debugging_restriction_reason(profile)

    assert "blocks Playwright remote debugging" in reason
    assert "/browser profile chrome-a0 Default" in reason


def test_remote_debugging_restriction_allows_a0_managed_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    monkeypatch.setattr(host_browser_common_module, "browser_major_version", lambda _: 147)
    profile = BrowserProfile("chrome-a0", "Chrome", "/bin/chrome", tmp_path, "Default", "Default")

    assert remote_debugging_restriction_reason(profile) == ""


def test_selected_profile_prefers_supported_a0_profile_when_default_is_restricted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    config_root = tmp_path / "config"
    default_root = config_root / "google-chrome"
    (default_root / "Default").mkdir(parents=True)
    a0_root = tmp_path / "a0-chrome"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setattr(host_browser_common_module, "browser_major_version", lambda _: 147)
    manager = HostBrowserManager(
        CLIConfig(),
        candidate_provider=lambda: [
            BrowserCandidate("chrome", "Google Chrome", str(executable), default_root),
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), a0_root),
        ],
        playwright_available=True,
    )

    selected = manager.selected_profile()

    assert selected is not None
    assert selected.family == "chrome-a0"


def test_hello_metadata_marks_missing_playwright_as_preparable(tmp_path: Path) -> None:
    root = tmp_path / "Chrome"
    (root / "Default").mkdir(parents=True)
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=False,
            host_browser_family="chrome-a0",
            host_browser_profile_path=str(root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), root)
        ],
        playwright_available=False,
    )

    metadata = manager.hello_metadata()

    assert metadata["supported"] is False
    assert metadata["can_prepare"] is True
    assert metadata["status"] == "unsupported"
    assert "Python Playwright" in metadata["support_reason"]


def test_hello_metadata_marks_restricted_saved_profile_as_preparable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    config_root = tmp_path / "config"
    default_root = config_root / "google-chrome"
    (default_root / "Default").mkdir(parents=True)
    managed_root = tmp_path / "a0-chrome"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setattr(host_browser_common_module, "browser_major_version", lambda _: 147)
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=False,
            host_browser_family="chrome",
            host_browser_profile_path=str(default_root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome", "Google Chrome", str(executable), default_root),
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), managed_root),
        ],
        playwright_available=True,
    )

    metadata = manager.hello_metadata()

    assert metadata["supported"] is False
    assert metadata["can_prepare"] is True
    assert metadata["browser_family"] == "chrome"
    assert "Select the A0-controlled local profile" in metadata["support_reason"]


async def test_host_browser_manager_dispatches_open_and_screenshot_artifact(tmp_path: Path) -> None:
    root = tmp_path / "Chrome"
    (root / "Default").mkdir(parents=True)
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    playwright = FakePlaywright()
    config = CLIConfig(
        host_browser_enabled=True,
        host_browser_family="chrome",
        host_browser_profile_path=str(root),
        host_browser_profile_label="Default",
    )
    manager = HostBrowserManager(
        config,
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", str(executable), root)],
        playwright_available=True,
        playwright_starter=lambda: FakeStarter(playwright),
    )

    opened = await manager.handle_op(
        {"op_id": "op-open", "context_id": "ctx-1", "action": "open", "url": "example.com"}
    )
    screenshot = await manager.handle_op(
        {"op_id": "op-shot", "context_id": "ctx-1", "action": "screenshot", "browser_id": 1}
    )

    assert opened["ok"] is True
    assert opened["result"]["state"]["currentUrl"] == "https://example.com/"
    assert screenshot["ok"] is True
    artifact = screenshot["result"]["artifact"]
    assert artifact["encoding"] == "base64"
    assert artifact["mime"] == "image/jpeg"
    assert artifact["data"]
    assert playwright.chromium.launch_kwargs["user_data_dir"] == str(root)


async def test_host_browser_manager_uses_agent_zero_supplied_content_helper(tmp_path: Path) -> None:
    root = tmp_path / "Chrome"
    (root / "Default").mkdir(parents=True)
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    playwright = FakePlaywright()
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=True,
            host_browser_family="chrome",
            host_browser_profile_path=str(root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [BrowserCandidate("chrome", "Google Chrome", str(executable), root)],
        playwright_available=True,
        playwright_starter=lambda: FakeStarter(playwright),
    )
    helper_hash = content_helper_sha256(MINIMAL_CONTENT_HELPER_SOURCE)

    opened = await manager.handle_op(
        {
            "op_id": "op-open",
            "context_id": "ctx-helper",
            "action": "open",
            "url": "example.com",
            "content_helper": {
                "source": MINIMAL_CONTENT_HELPER_SOURCE,
                "sha256": helper_hash,
            },
        }
    )

    assert opened["ok"] is True
    assert manager.metadata()["content_helper_sha256"] == helper_hash
    assert MINIMAL_CONTENT_HELPER_SOURCE in playwright.chromium.context.init_scripts


async def test_relaunch_session_is_adopted_by_first_browser_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    root = tmp_path / "Chrome"
    (root / "Default").mkdir(parents=True)
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    playwright = FakePlaywright()
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=True,
            host_browser_family="chrome-a0",
            host_browser_profile_path=str(root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), root)
        ],
        playwright_available=True,
        playwright_starter=lambda: FakeStarter(playwright),
    )
    await manager.relaunch()
    monkeypatch.setattr(
        host_browser_common_module,
        "profile_lock_state",
        lambda _: ProfileLockState(True, (str(root / "SingletonLock"),), 12345),
    )

    opened = await manager.handle_op(
        {"op_id": "op-open", "context_id": "chat-1", "action": "open", "url": "example.com"}
    )

    assert opened["ok"] is True
    assert RELAUNCH_CONTEXT_ID not in manager._sessions
    assert "chat-1" in manager._sessions
    assert manager._sessions["chat-1"].context_id == "chat-1"


async def test_remote_debugging_session_attaches_without_closing_user_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_zero_cli.host_browser_session as host_browser_session_module

    instances = []

    class FakeCDPConnection:
        def __init__(self, endpoint: str) -> None:
            self.endpoint = endpoint
            self.closed = False
            instances.append(self)

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

        async def command(
            self,
            method: str,
            params: dict[str, object] | None = None,
            *,
            session_id: str | None = None,
            timeout: float = 30.0,
        ) -> dict[str, object]:
            del session_id, timeout
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"targetId": "target-1", "type": "page", "url": "https://example.com/"}
                    ]
                }
            if method == "Target.attachToTarget":
                return {"sessionId": "session-1"}
            if method in {"Page.enable", "Runtime.enable", "Page.addScriptToEvaluateOnNewDocument"}:
                return {}
            if method == "Runtime.evaluate":
                expression = str((params or {}).get("expression") or "")
                if "location.href" in expression:
                    return {"result": {"type": "string", "value": "https://example.com/"}}
                if "document.title" in expression:
                    return {"result": {"type": "string", "value": "Example"}}
                if "history" in expression:
                    return {"result": {"type": "number", "value": 1}}
                return {"result": {"type": "undefined"}}
            return {}

    monkeypatch.setattr(host_browser_session_module, "CDPConnection", FakeCDPConnection)
    profile = BrowserProfile(
        "chrome-cdp",
        "Chrome (remote debugging)",
        "",
        Path(),
        "127.0.0.1:9222",
        "Remote debugging allowed",
        cdp_endpoint="ws://127.0.0.1:9222/devtools/browser/test",
    )
    session = HostBrowserSession(
        context_id="ctx-cdp",
        profile=profile,
    )

    await session.ensure_started()
    listed = await session.list()
    await session.close()

    assert instances[0].endpoint == "ws://127.0.0.1:9222/devtools/browser/test"
    assert listed["browsers"][0]["currentUrl"] == "https://example.com/"
    assert instances[0].closed is True


async def test_remote_debugging_session_opens_lists_and_reads_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_zero_cli.host_browser_session as host_browser_session_module

    instances = []

    class FakeCDPConnection:
        def __init__(self, endpoint: str) -> None:
            self.endpoint = endpoint
            self.closed = False
            self.targets: dict[str, dict[str, str]] = {}
            self.sessions: dict[str, str] = {}
            self.closed_targets: list[str] = []
            instances.append(self)

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

        async def command(
            self,
            method: str,
            params: dict[str, object] | None = None,
            *,
            session_id: str | None = None,
            timeout: float = 30.0,
        ) -> dict[str, object]:
            del timeout
            params = params or {}
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {
                            "targetId": target_id,
                            "type": "page",
                            "url": target["url"],
                        }
                        for target_id, target in self.targets.items()
                    ]
                }
            if method == "Target.createTarget":
                target_id = f"target-{len(self.targets) + 1}"
                self.targets[target_id] = {"url": str(params.get("url") or "about:blank")}
                return {"targetId": target_id}
            if method == "Target.attachToTarget":
                target_id = str(params.get("targetId") or "")
                session = f"session-{target_id}"
                self.sessions[session] = target_id
                return {"sessionId": session}
            if method == "Target.closeTarget":
                target_id = str(params.get("targetId") or "")
                self.closed_targets.append(target_id)
                self.targets.pop(target_id, None)
                return {}
            if method in {"Page.enable", "Runtime.enable", "Page.addScriptToEvaluateOnNewDocument"}:
                return {}
            if method == "Page.navigate":
                target_id = self.sessions[str(session_id)]
                self.targets[target_id]["url"] = str(params.get("url") or "")
                return {}
            if method == "Runtime.evaluate":
                expression = str(params.get("expression") or "")
                target_id = self.sessions[str(session_id)]
                url = self.targets[target_id]["url"]
                if "location.href" in expression:
                    return {"result": {"type": "string", "value": url}}
                if "document.title" in expression:
                    return {"result": {"type": "string", "value": "Example"}}
                if "history" in expression:
                    return {"result": {"type": "number", "value": 1}}
                if "__spaceBrowserPageContent__?.capture" in expression:
                    return {"result": {"type": "boolean", "value": True}}
                if "__spaceBrowserPageContent__.capture" in expression:
                    return {
                        "result": {
                            "type": "object",
                            "value": {"document": "[button 1] Continue"},
                        }
                    }
                return {"result": {"type": "undefined"}}
            return {}

    monkeypatch.setattr(host_browser_session_module, "CDPConnection", FakeCDPConnection)
    profile = BrowserProfile(
        "chrome-cdp",
        "Chrome (remote debugging)",
        "",
        Path(),
        "127.0.0.1:9222",
        "Remote debugging allowed",
        cdp_endpoint="ws://127.0.0.1:9222/devtools/browser/test",
    )
    session = HostBrowserSession(context_id="ctx-cdp-actions", profile=profile)

    opened = await session.open("example.com")
    content = await session.content(opened["id"])
    listed = await session.list(include_content=True)
    closed = await session.close_browser(opened["id"])
    await session.close()

    assert opened["state"]["currentUrl"] == "https://example.com/"
    assert content == {"document": "[button 1] Continue"}
    assert listed["browsers"][0]["content"] == {"document": "[button 1] Continue"}
    assert closed == {"browsers": [], "last_interacted_browser_id": None}
    assert instances[0].closed_targets == ["target-1"]
    assert instances[0].closed is True


async def test_ensure_action_enables_and_starts_host_browser(tmp_path: Path) -> None:
    root = tmp_path / "Chrome"
    (root / "Default").mkdir(parents=True)
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    playwright = FakePlaywright()
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=False,
            host_browser_family="chrome-a0",
            host_browser_profile_path=str(root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), root)
        ],
        playwright_available=True,
        playwright_starter=lambda: FakeStarter(playwright),
    )

    result = await manager.handle_op(
        {"op_id": "op-ensure", "context_id": "chat-1", "action": "ensure"}
    )

    assert result["ok"] is True
    assert manager.enabled is True
    assert result["result"]["status"] == "active"
    assert RELAUNCH_CONTEXT_ID in manager._sessions


async def test_ensure_auto_selects_supported_managed_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    config_root = tmp_path / "config"
    default_root = config_root / "google-chrome"
    (default_root / "Default").mkdir(parents=True)
    managed_root = tmp_path / "a0-chrome"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setattr(host_browser_common_module, "browser_major_version", lambda _: 147)
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=False,
            host_browser_family="chrome",
            host_browser_profile_path=str(default_root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome", "Google Chrome", str(executable), default_root),
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), managed_root),
        ],
        playwright_available=True,
        playwright_starter=lambda: FakeStarter(FakePlaywright()),
    )

    result = await manager.handle_op(
        {"op_id": "op-ensure", "context_id": "chat-1", "action": "ensure"}
    )

    assert result["ok"] is True
    assert result["result"]["browser_family"] == "chrome-a0"
    assert manager.config.host_browser_family == "chrome-a0"
    assert manager.config.host_browser_profile_path == str(managed_root)


async def test_locked_profile_owned_by_active_context_reports_context_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent_zero_cli.host_browser_common as host_browser_common_module

    root = tmp_path / "Chrome"
    (root / "Default").mkdir(parents=True)
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    playwright = FakePlaywright()
    manager = HostBrowserManager(
        CLIConfig(
            host_browser_enabled=True,
            host_browser_family="chrome-a0",
            host_browser_profile_path=str(root),
            host_browser_profile_label="Default",
        ),
        candidate_provider=lambda: [
            BrowserCandidate("chrome-a0", "Google Chrome (A0 controlled profile)", str(executable), root)
        ],
        playwright_available=True,
        playwright_starter=lambda: FakeStarter(playwright),
    )
    await manager.handle_op(
        {"op_id": "op-open-1", "context_id": "chat-1", "action": "open", "url": "example.com"}
    )
    monkeypatch.setattr(
        host_browser_common_module,
        "profile_lock_state",
        lambda _: ProfileLockState(True, (str(root / "SingletonLock"),), 12345),
    )

    result = await manager.handle_op(
        {"op_id": "op-open-2", "context_id": "chat-2", "action": "open", "url": "example.org"}
    )

    assert result["ok"] is False
    assert result["code"] == "HOST_BROWSER_CONTEXT_ACTIVE"
    assert result["result"]["active_context"] == "chat-1"


async def test_host_browser_session_stops_playwright_after_launch_failure(tmp_path: Path) -> None:
    class FailingChromium(FakeChromium):
        async def launch_persistent_context(self, **kwargs: object) -> FakeContext:
            self.launch_kwargs = dict(kwargs)
            raise RuntimeError("launch boom")

    playwright = FakePlaywright()
    playwright.chromium = FailingChromium()
    profile = BrowserProfile("chrome", "Chrome", "/bin/chrome", tmp_path, "Default", "Default")
    session = HostBrowserSession(
        context_id="ctx-launch-failure",
        profile=profile,
        playwright_starter=lambda: FakeStarter(playwright),
    )

    with pytest.raises(RuntimeError, match="launch boom"):
        await session.ensure_started()

    assert playwright.stopped is True
    assert session.playwright is None
    assert session.context is None


async def test_set_checked_dispatch_parses_false_string(tmp_path: Path) -> None:
    profile = BrowserProfile("chrome", "Chrome", "/bin/chrome", tmp_path, "Default", "Default")
    session = HostBrowserSession(context_id="ctx-checked", profile=profile)
    seen: dict[str, object] = {}

    async def fake_set_checked(browser_id: object, ref: object, checked: bool = True) -> dict[str, object]:
        seen.update({"browser_id": browser_id, "ref": ref, "checked": checked})
        return {"ok": True}

    session.set_checked = fake_set_checked  # type: ignore[method-assign]

    await session.dispatch(
        {"action": "set_checked", "browser_id": 1, "ref": "input-1", "checked": "false"}
    )

    assert seen == {"browser_id": 1, "ref": "input-1", "checked": False}


async def test_manager_preserves_error_payload_shape_for_unknown_action(tmp_path: Path) -> None:
    manager = HostBrowserManager(CLIConfig(host_browser_enabled=True), playwright_available=True)

    result = await manager.handle_op(
        {"op_id": "op-unknown", "context_id": "ctx-error-shape", "action": "dance"}
    )

    assert result == {
        "op_id": "op-unknown",
        "ok": False,
        "code": "UNKNOWN_ACTION",
        "error": "Unknown host browser action: 'dance'",
    }


async def test_goto_surfaces_navigation_failures(tmp_path: Path) -> None:
    profile = BrowserProfile("chrome", "Chrome", "/bin/chrome", tmp_path, "Default", "Default")
    session = HostBrowserSession(context_id="ctx-goto", profile=profile)

    class FailingPage(FakePage):
        def __init__(self) -> None:
            super().__init__()
            self.settled = False

        async def goto(self, url: str, **_: object) -> None:
            del url
            raise ValueError("navigation boom")

        async def wait_for_load_state(self, *_: object, **__: object) -> None:
            self.settled = True

    page = FailingPage()

    with pytest.raises(RuntimeError, match="Browser navigation failed"):
        await session._goto(page, "https://example.invalid")

    assert page.settled is False


async def test_manager_recreates_session_when_profile_changes(tmp_path: Path) -> None:
    profile_one = BrowserProfile("chrome", "Chrome", "/bin/chrome", tmp_path / "one", "Default", "One")
    profile_two = BrowserProfile("chrome", "Chrome", "/bin/chrome", tmp_path / "two", "Default", "Two")
    manager = HostBrowserManager(CLIConfig(host_browser_enabled=True), playwright_available=True)
    session_one = await manager._session("ctx-profile", profile=profile_one)
    closed = False

    async def fake_close() -> None:
        nonlocal closed
        closed = True

    session_one.close = fake_close  # type: ignore[method-assign]

    session_two = await manager._session("ctx-profile", profile=profile_two)

    assert closed is True
    assert session_two is not session_one
    assert session_two.profile == profile_two


async def test_host_browser_manager_can_repair_missing_playwright(tmp_path: Path) -> None:
    del tmp_path
    calls: list[list[str]] = []

    async def fake_installer(command: list[str]) -> tuple[int, str]:
        calls.append(command)
        manager._playwright_available = True
        return 0, "installed"

    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        playwright_available=False,
        playwright_installer=fake_installer,
    )

    result = await manager.ensure_playwright_dependency()

    assert result["installed"] is True
    assert calls == [manager.playwright_install_command()]
    assert manager.has_playwright_dependency() is True


async def test_host_browser_manager_reports_repair_failure() -> None:
    async def fake_installer(command: list[str]) -> tuple[int, str]:
        del command
        return 2, "boom"

    manager = HostBrowserManager(
        CLIConfig(host_browser_enabled=True),
        playwright_available=False,
        playwright_installer=fake_installer,
    )

    with pytest.raises(RuntimeError, match="install failed"):
        await manager.ensure_playwright_dependency()
