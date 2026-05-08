from __future__ import annotations

import asyncio
import base64
import contextlib
from functools import lru_cache
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from agent_zero_cli.config import (
    CLIConfig,
    normalize_host_browser_relaunch_preference,
    save_host_browser_enabled,
    save_host_browser_profile,
    save_host_browser_relaunch_preference,
)

CONTENT_HELPER_PATH = Path(__file__).resolve().parent / "assets" / "browser-page-content.js"
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
CHROME_SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
HOST_BROWSER_ARTIFACT_ROOT_ENV = "A0_HOST_BROWSER_ARTIFACT_ROOT"
DEFAULT_HOST_BROWSER_ARTIFACT_ROOT = Path(tempfile.gettempdir()) / "_a0_connector" / "host_browser"
PLAYWRIGHT_PYTHON_PACKAGE = "playwright"
HOST_BROWSER_OZONE_PLATFORM_ENV = "A0_HOST_BROWSER_OZONE_PLATFORM"
HOST_BROWSER_REMOTE_DEBUGGING_ENDPOINTS_ENV = "A0_HOST_BROWSER_REMOTE_DEBUGGING_ENDPOINTS"
REMOTE_DEBUGGING_CONNECT_TIMEOUT_SECONDS = 60.0
REMOTE_DEBUGGING_RESTRICTED_MAJOR = 136
RELAUNCH_CONTEXT_ID = "_a0_cli_browser_check"
MAX_INSTALL_OUTPUT_CHARS = 4000
_URL_SCHEME_RE = re.compile(r"^[a-z][a-z\d+\-.]*://", re.I)
_SPECIAL_SCHEME_RE = re.compile(r"^(?:about|blob|data|file|mailto|tel):", re.I)
_LOCAL_HOST_RE = re.compile(
    r"^(?:localhost|\[[0-9a-f:.]+\]|(?:\d{1,3}\.){3}\d{1,3})(?::\d+)?$",
    re.I,
)
_TYPED_HOST_RE = re.compile(
    r"^(?:localhost|\[[0-9a-f:.]+\]|(?:\d{1,3}\.){3}\d{1,3}|"
    r"(?:[a-z\d](?:[a-z\d-]{0,61}[a-z\d])?\.)+[a-z\d-]{2,63})(?::\d+)?$",
    re.I,
)
_SAFE_CONTEXT_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
_SUPPORTED_ACTIONS = {
    "open",
    "list",
    "state",
    "set_active",
    "navigate",
    "back",
    "forward",
    "reload",
    "content",
    "detail",
    "evaluate",
    "click",
    "type",
    "submit",
    "type_submit",
    "scroll",
    "hover",
    "double_click",
    "right_click",
    "drag",
    "wheel",
    "keyboard",
    "key_chord",
    "clipboard",
    "set_viewport",
    "select_option",
    "set_checked",
    "upload_file",
    "mouse",
    "screenshot",
    "screenshot_file",
    "close",
    "close_all",
    "ensure",
    "multi",
    "status",
}
_SENSITIVE_ACTIONS = {"content", "detail", "evaluate", "screenshot", "screenshot_file"}
_VALID_MODIFIERS = {"Control", "Shift", "Alt", "Meta"}


@dataclass(frozen=True)
class BrowserCandidate:
    family: str
    label: str
    executable_path: str
    user_data_dir: Path


@dataclass(frozen=True)
class BrowserProfile:
    family: str
    family_label: str
    executable_path: str
    user_data_dir: Path
    profile_directory: str
    display_name: str
    cdp_endpoint: str = ""

    @property
    def profile_path(self) -> Path:
        return self.user_data_dir

    @property
    def profile_path_display(self) -> str:
        return self.cdp_endpoint or str(self.profile_path)

    @property
    def profile_label(self) -> str:
        return self.profile_directory

    @property
    def is_remote_debugging(self) -> bool:
        return bool(self.cdp_endpoint)

    def as_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "family_label": self.family_label,
            "executable_path": self.executable_path,
            "profile_path": self.profile_path_display,
            "profile_label": self.profile_label,
            "display_name": self.display_name,
            "cdp_endpoint": self.cdp_endpoint,
            "locked": False if self.is_remote_debugging else is_profile_locked(self.profile_path),
        }


@dataclass(frozen=True)
class ProfileLockState:
    locked: bool
    lock_files: tuple[str, ...] = ()
    owner_pid: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "locked": self.locked,
            "lock_files": list(self.lock_files),
            "owner_pid": self.owner_pid,
        }


@dataclass
class HostBrowserPage:
    id: int
    page: Any


class CDPError(RuntimeError):
    pass


class CDPConnection:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()

    async def connect(self) -> None:
        timeout = aiohttp.ClientTimeout(total=REMOTE_DEBUGGING_CONNECT_TIMEOUT_SECONDS)
        self._session = aiohttp.ClientSession(timeout=timeout)
        try:
            self._ws = await self._session.ws_connect(
                self.endpoint,
                timeout=REMOTE_DEBUGGING_CONNECT_TIMEOUT_SECONDS,
                autoclose=True,
                autoping=True,
            )
        except Exception:
            await self.close()
            raise
        self._reader_task = asyncio.create_task(self._read_loop())

    async def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if self._ws is None:
            raise CDPError("Chrome DevTools connection is not open.")
        async with self._send_lock:
            msg_id = self._next_id
            self._next_id += 1
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._pending[msg_id] = future
            payload: dict[str, Any] = {"id": msg_id, "method": method}
            if params:
                payload["params"] = params
            if session_id:
                payload["sessionId"] = session_id
            await self._ws.send_json(payload)
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(msg_id, None)
        if "error" in response:
            error = response.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise CDPError(str(message or f"CDP command failed: {method}"))
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result({"error": {"message": "Chrome DevTools connection closed."}})
        self._pending.clear()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        async for message in self._ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except Exception:
                    continue
                msg_id = payload.get("id")
                if isinstance(msg_id, int):
                    future = self._pending.get(msg_id)
                    if future is not None and not future.done():
                        future.set_result(payload)
            elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                break
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result({"error": {"message": "Chrome DevTools connection closed."}})


class CDPMouse:
    def __init__(self, page: "CDPPage") -> None:
        self.page = page
        self._x = 0.0
        self._y = 0.0

    async def move(self, x: float, y: float, steps: int | None = None) -> None:
        del steps
        self._x = float(x)
        self._y = float(y)
        await self.page.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": self._x, "y": self._y})

    async def click(self, x: float, y: float, button: str = "left") -> None:
        self._x = float(x)
        self._y = float(y)
        params = {"x": float(x), "y": float(y), "button": _cdp_mouse_button(button), "clickCount": 1}
        await self.page.send("Input.dispatchMouseEvent", {"type": "mousePressed", **params})
        await self.page.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **params})

    async def dblclick(self, x: float, y: float, button: str = "left") -> None:
        self._x = float(x)
        self._y = float(y)
        params = {"x": float(x), "y": float(y), "button": _cdp_mouse_button(button), "clickCount": 2}
        await self.page.send("Input.dispatchMouseEvent", {"type": "mousePressed", **params})
        await self.page.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **params})

    async def down(self) -> None:
        await self.page.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": self._x, "y": self._y, "button": "left", "clickCount": 1},
        )

    async def up(self) -> None:
        await self.page.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": self._x, "y": self._y, "button": "left", "clickCount": 1},
        )

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await self.page.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": self._x, "y": self._y, "deltaX": float(delta_x), "deltaY": float(delta_y)},
        )


class CDPKeyboard:
    def __init__(self, page: "CDPPage") -> None:
        self.page = page

    async def type(self, text: str) -> None:
        await self.insert_text(text)

    async def insert_text(self, text: str) -> None:
        await self.page.send("Input.insertText", {"text": str(text or "")})

    async def press(self, key: str) -> None:
        key_text = str(key or "")
        if len(key_text) == 1:
            await self.page.send("Input.dispatchKeyEvent", {"type": "keyDown", "text": key_text, "key": key_text})
            await self.page.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key_text})
            return
        await self.page.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": key_text})
        await self.page.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key_text})

    async def down(self, key: str) -> None:
        await self.page.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": str(key or "")})

    async def up(self, key: str) -> None:
        await self.page.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": str(key or "")})


class CDPPage:
    def __init__(self, context: "CDPContext", target_id: str, session_id: str, url: str = "") -> None:
        self.context = context
        self.connection = context.connection
        self.target_id = target_id
        self.session_id = session_id
        self.url = url or "about:blank"
        self.mouse = CDPMouse(self)
        self.keyboard = CDPKeyboard(self)
        self.viewport_size = dict(DEFAULT_VIEWPORT)
        self._handlers: dict[str, Any] = {}

    def on(self, event: str, callback: Any) -> None:
        self._handlers[event] = callback

    async def send(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        return await self.connection.command(method, params, session_id=self.session_id, timeout=timeout)

    async def goto(self, url: str, **_: object) -> None:
        await self.send("Page.navigate", {"url": url})
        self.url = url

    async def go_back(self, **_: object) -> None:
        await self.evaluate("() => history.back()")

    async def go_forward(self, **_: object) -> None:
        await self.evaluate("() => history.forward()")

    async def reload(self, **_: object) -> None:
        await self.send("Page.reload", {})

    async def wait_for_load_state(self, *_: object, **__: object) -> None:
        await asyncio.sleep(0.15)

    async def bring_to_front(self) -> None:
        await self.connection.command("Target.activateTarget", {"targetId": self.target_id})

    async def title(self) -> str:
        result = await self.evaluate("() => document.title")
        return str(result or "")

    async def evaluate(self, script: str, arg: object = None) -> object:
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": _cdp_evaluate_expression(script, arg),
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        if result.get("exceptionDetails"):
            raise CDPError(str(result["exceptionDetails"]))
        remote = result.get("result") if isinstance(result.get("result"), dict) else {}
        if "value" in remote:
            return remote.get("value")
        if remote.get("type") == "undefined":
            return None
        return remote.get("description")

    async def screenshot(self, **kwargs: object) -> bytes:
        image_type = str(kwargs.get("type") or "jpeg")
        params: dict[str, Any] = {"format": image_type}
        if image_type == "jpeg":
            params["quality"] = int(kwargs.get("quality") or 80)
        if kwargs.get("full_page"):
            params["captureBeyondViewport"] = True
        result = await self.send("Page.captureScreenshot", params, timeout=60.0)
        data = base64.b64decode(str(result.get("data") or ""))
        path = kwargs.get("path")
        if path:
            Path(str(path)).write_bytes(data)
        return data

    async def close(self) -> None:
        await self.connection.command("Target.closeTarget", {"targetId": self.target_id})

    async def set_viewport_size(self, viewport: dict[str, int]) -> None:
        self.viewport_size = dict(viewport)
        await self.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": int(viewport["width"]),
                "height": int(viewport["height"]),
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )

    async def evaluate_handle(self, *_: object, **__: object) -> None:
        return None

    async def set_input_files(self, *_: object, **__: object) -> None:
        raise NotImplementedError("File uploads are not supported for user-authorized Chrome remote debugging yet.")


class CDPContext:
    def __init__(self, connection: CDPConnection) -> None:
        self.connection = connection
        self.pages: list[CDPPage] = []
        self._pages_by_target: dict[str, CDPPage] = {}
        self._handlers: dict[str, Any] = {}
        self._init_scripts: list[str] = []

    def set_default_timeout(self, timeout: int) -> None:
        del timeout

    def set_default_navigation_timeout(self, timeout: int) -> None:
        del timeout

    def on(self, event: str, callback: Any) -> None:
        self._handlers[event] = callback

    async def add_init_script(self, script: str | None = None, *, path: str | None = None) -> None:
        source = script if script is not None else Path(str(path)).read_text(encoding="utf-8")
        self._init_scripts.append(str(source))
        for page in list(self.pages):
            with contextlib.suppress(Exception):
                await page.send("Page.addScriptToEvaluateOnNewDocument", {"source": str(source)})

    async def new_page(self) -> CDPPage:
        result = await self.connection.command("Target.createTarget", {"url": "about:blank"})
        target_id = str(result.get("targetId") or "")
        return await self._attach_page(target_id, "about:blank")

    async def close(self) -> None:
        return None

    async def discover_pages(self) -> list[CDPPage]:
        result = await self.connection.command("Target.getTargets", {})
        infos = result.get("targetInfos") if isinstance(result.get("targetInfos"), list) else []
        visible_targets: set[str] = set()
        for info in infos:
            if not isinstance(info, dict) or info.get("type") != "page":
                continue
            target_id = str(info.get("targetId") or "")
            url = str(info.get("url") or "about:blank")
            if not target_id or not _cdp_target_visible(url):
                continue
            visible_targets.add(target_id)
            page = self._pages_by_target.get(target_id)
            if page is None:
                page = await self._attach_page(target_id, url)
            else:
                page.url = url
        for target_id in list(self._pages_by_target):
            if target_id not in visible_targets:
                self._pages_by_target.pop(target_id, None)
        self.pages = [page for page in self.pages if page.target_id in self._pages_by_target]
        return list(self.pages)

    async def _attach_page(self, target_id: str, url: str) -> CDPPage:
        attach = await self.connection.command("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session_id = str(attach.get("sessionId") or "")
        if not target_id or not session_id:
            raise CDPError(f"Could not attach to Chrome target {target_id}.")
        page = CDPPage(self, target_id, session_id, url)
        self._pages_by_target[target_id] = page
        self.pages.append(page)
        with contextlib.suppress(Exception):
            await page.send("Page.enable", {})
        with contextlib.suppress(Exception):
            await page.send("Runtime.enable", {})
        for source in self._init_scripts:
            with contextlib.suppress(Exception):
                await page.send("Page.addScriptToEvaluateOnNewDocument", {"source": source})
        return page


def _cdp_mouse_button(button: str) -> str:
    normalized = str(button or "left").strip().lower()
    if normalized == "right":
        return "right"
    if normalized in {"middle", "auxiliary"}:
        return "middle"
    return "left"


def _cdp_evaluate_expression(script: str, arg: object = None) -> str:
    source = str(script or "undefined")
    if arg is not None:
        return f"({source})({json.dumps(arg)})"
    stripped = source.strip()
    if _cdp_expression_is_iife(stripped):
        return source
    if _cdp_expression_is_function(stripped):
        return f"({source})()"
    return source


def _cdp_expression_is_function(source: str) -> bool:
    return bool(
        re.match(r"^(?:async\s+)?function\b", source)
        or re.match(r"^(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>", source)
    )


def _cdp_expression_is_iife(source: str) -> bool:
    compact = source.rstrip()
    return compact.endswith(")();") or compact.endswith("})();")


def _cdp_target_visible(url: str) -> bool:
    normalized = str(url or "")
    if normalized in {"", "about:blank", "chrome://newtab/"}:
        return True
    if normalized.startswith("chrome://inspect"):
        return True
    return not normalized.startswith(
        ("chrome://", "chrome-untrusted://", "chrome-extension://", "devtools://")
    )


@dataclass
class HostBrowserSession:
    context_id: str
    profile: BrowserProfile
    playwright_starter: Callable[[], Any] | None = None
    playwright: Any = None
    browser: Any = None
    context: Any = None
    pages: dict[int, HostBrowserPage] = field(default_factory=dict)
    next_browser_id: int = 1
    last_interacted_browser_id: int | None = None
    _content_helper_source: str | None = None
    _start_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _registry_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _closing: bool = False

    async def dispatch(self, payload: dict[str, Any]) -> Any:
        action = normalize_action(payload.get("action"))
        if action == "open":
            return await self.open(str(payload.get("url") or ""))
        if action == "list":
            return await self.list(include_content=coerce_bool(payload.get("include_content")))
        if action == "state":
            return await self.state(payload.get("browser_id"))
        if action == "set_active":
            return await self.set_active(payload.get("browser_id"))
        if action == "navigate":
            return await self.navigate(payload.get("browser_id"), str(payload.get("url") or ""))
        if action == "back":
            return await self.back(payload.get("browser_id"))
        if action == "forward":
            return await self.forward(payload.get("browser_id"))
        if action == "reload":
            return await self.reload(payload.get("browser_id"))
        if action == "content":
            selector_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else None
            return await self.content(payload.get("browser_id"), selector_payload)
        if action == "detail":
            return await self.detail(payload.get("browser_id"), require_ref(payload.get("ref"), "detail"))
        if action == "evaluate":
            return await self.evaluate(payload.get("browser_id"), str(payload.get("script") or ""))
        if action == "click":
            return await self.click(
                payload.get("browser_id"),
                require_ref(payload.get("ref"), "click"),
                modifiers=payload.get("modifiers"),
                focus_popup=payload.get("focus_popup"),
            )
        if action == "type":
            return await self.type(
                payload.get("browser_id"),
                require_ref(payload.get("ref"), "type"),
                str(payload.get("text") or ""),
            )
        if action == "submit":
            return await self.submit(payload.get("browser_id"), require_ref(payload.get("ref"), "submit"))
        if action == "type_submit":
            return await self.type_submit(
                payload.get("browser_id"),
                require_ref(payload.get("ref"), "type_submit"),
                str(payload.get("text") or ""),
            )
        if action == "scroll":
            return await self.scroll(payload.get("browser_id"), require_ref(payload.get("ref"), "scroll"))
        if action == "hover":
            return await self.hover(
                payload.get("browser_id"),
                ref=payload.get("ref"),
                x=coerce_float(payload.get("x")),
                y=coerce_float(payload.get("y")),
                offset_x=coerce_float(payload.get("offset_x")),
                offset_y=coerce_float(payload.get("offset_y")),
            )
        if action == "double_click":
            return await self.double_click(
                payload.get("browser_id"),
                ref=payload.get("ref"),
                x=coerce_float(payload.get("x")),
                y=coerce_float(payload.get("y")),
                button=str(payload.get("button") or "left"),
                modifiers=payload.get("modifiers"),
                offset_x=coerce_float(payload.get("offset_x")),
                offset_y=coerce_float(payload.get("offset_y")),
            )
        if action == "right_click":
            return await self.right_click(
                payload.get("browser_id"),
                ref=payload.get("ref"),
                x=coerce_float(payload.get("x")),
                y=coerce_float(payload.get("y")),
                modifiers=payload.get("modifiers"),
                offset_x=coerce_float(payload.get("offset_x")),
                offset_y=coerce_float(payload.get("offset_y")),
            )
        if action == "drag":
            return await self.drag(
                payload.get("browser_id"),
                ref=payload.get("ref"),
                target_ref=payload.get("target_ref"),
                x=coerce_float(payload.get("x")),
                y=coerce_float(payload.get("y")),
                to_x=coerce_float(payload.get("to_x")),
                to_y=coerce_float(payload.get("to_y")),
                offset_x=coerce_float(payload.get("offset_x")),
                offset_y=coerce_float(payload.get("offset_y")),
                target_offset_x=coerce_float(payload.get("target_offset_x")),
                target_offset_y=coerce_float(payload.get("target_offset_y")),
            )
        if action == "wheel":
            return await self.wheel(
                payload.get("browser_id"),
                coerce_float(payload.get("x")),
                coerce_float(payload.get("y")),
                coerce_float(payload.get("delta_x")),
                coerce_float(payload.get("delta_y")),
            )
        if action == "mouse":
            return await self.mouse(
                payload.get("browser_id"),
                str(payload.get("event_type") or "click"),
                coerce_float(payload.get("x")),
                coerce_float(payload.get("y")),
                button=str(payload.get("button") or "left"),
                modifiers=payload.get("modifiers"),
            )
        if action == "keyboard":
            return await self.keyboard(
                payload.get("browser_id"),
                key=str(payload.get("key") or ""),
                text=str(payload.get("text") or ""),
            )
        if action == "key_chord":
            keys = payload.get("keys")
            if not isinstance(keys, list) or not keys:
                raise ValueError("key_chord requires non-empty keys")
            return await self.key_chord(payload.get("browser_id"), [str(key) for key in keys])
        if action == "clipboard":
            return await self.clipboard(
                payload.get("browser_id"),
                action=str(payload.get("clipboard_action") or payload.get("operation") or ""),
                text=str(payload.get("text") or ""),
            )
        if action == "set_viewport":
            return await self.set_viewport(
                payload.get("browser_id"),
                coerce_int(payload.get("width"), default=0),
                coerce_int(payload.get("height"), default=0),
            )
        if action == "select_option":
            return await self.select_option(
                payload.get("browser_id"),
                require_ref(payload.get("ref"), "select_option"),
                value=str(payload.get("value") or ""),
                values=payload.get("values"),
            )
        if action == "set_checked":
            return await self.set_checked(
                payload.get("browser_id"),
                require_ref(payload.get("ref"), "set_checked"),
                checked=True
                if payload.get("checked") is None
                else coerce_bool(payload.get("checked")),
            )
        if action == "upload_file":
            return await self.upload_file(
                payload.get("browser_id"),
                require_ref(payload.get("ref"), "upload_file"),
                path=str(payload.get("path") or ""),
                paths=payload.get("paths"),
            )
        if action in {"screenshot", "screenshot_file"}:
            return await self.screenshot_file(
                payload.get("browser_id"),
                quality=coerce_int(payload.get("quality"), default=80),
                full_page=coerce_bool(payload.get("full_page")),
                path=str(payload.get("path") or ""),
            )
        if action == "close":
            return await self.close_browser(payload.get("browser_id"))
        if action == "close_all":
            return await self.close_all_browsers()
        if action == "multi":
            calls = payload.get("calls")
            if not isinstance(calls, list) or not calls:
                raise ValueError("multi requires a non-empty calls list")
            return await self.multi(calls)
        raise ValueError(f"Unsupported host browser action: {action}")

    async def ensure_started(self) -> None:
        if self.context is not None:
            return
        async with self._start_lock:
            if self.context is not None:
                return
            await self._start()

    async def _start(self) -> None:
        lock = profile_lock_state_for_profile(self.profile)
        if lock.locked:
            raise ProfileLockedError(
                "Chrome profile is already in use. Run /browser relaunch after closing that browser, "
                "or select a profile that is not currently open.",
                lock_state=lock,
            )

        try:
            if self.profile.is_remote_debugging:
                connection = CDPConnection(self.profile.cdp_endpoint)
                await connection.connect()
                self.browser = connection
                self.context = CDPContext(connection)
                await self.context.discover_pages()
            else:
                if self.playwright_starter is not None:
                    starter = self.playwright_starter
                else:
                    from playwright.async_api import async_playwright

                    starter = async_playwright
                self.playwright = await starter().start()
                launch_args = chromium_launch_args(self.profile.profile_directory)
                self.context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile.user_data_dir),
                    executable_path=self.profile.executable_path,
                    headless=False,
                    accept_downloads=True,
                    viewport=DEFAULT_VIEWPORT,
                    screen=DEFAULT_VIEWPORT,
                    no_viewport=False,
                    args=launch_args,
                )
        except BaseException:
            if isinstance(self.browser, CDPConnection):
                with contextlib.suppress(Exception):
                    await self.browser.close()
            self.browser = None
            if self.playwright is not None:
                with contextlib.suppress(Exception):
                    await self.playwright.stop()
            self.playwright = None
            self.context = None
            raise
        self.context.set_default_timeout(30000)
        self.context.set_default_navigation_timeout(30000)
        self.context.on("close", self._on_context_closed)
        self.context.on("page", self._on_new_page_sync)
        with contextlib.suppress(Exception):
            await self.context.add_init_script(self._shadow_dom_script())
        with contextlib.suppress(Exception):
            await self.context.add_init_script(path=str(CONTENT_HELPER_PATH))

        for page in list(getattr(self.context, "pages", []) or []):
            if getattr(page, "url", "") == "about:blank":
                continue
            await self._register_page(page)

    async def open(self, url: str = "") -> dict[str, Any]:
        await self.ensure_started()
        page = await self.context.new_page()
        browser_page = await self._register_page(page)
        self.last_interacted_browser_id = browser_page.id
        target_url = normalize_url(url) if str(url or "").strip() else "about:blank"
        if target_url != "about:blank":
            await self._goto(page, target_url)
        else:
            await self._settle(page)
        return {"id": browser_page.id, "state": await self._state(browser_page.id)}

    async def list(self, include_content: bool = False) -> dict[str, Any]:
        await self.ensure_started()
        if isinstance(self.context, CDPContext):
            discovered_pages = await self.context.discover_pages()
            visible_pages = set(discovered_pages)
            for browser_id, browser_page in list(self.pages.items()):
                if browser_page.page not in visible_pages:
                    self.pages.pop(browser_id, None)
                    if self.last_interacted_browser_id == browser_id:
                        self.last_interacted_browser_id = None
            for page in discovered_pages:
                await self._register_page(page)
            if self.last_interacted_browser_id not in self.pages:
                self.last_interacted_browser_id = next(iter(sorted(self.pages)), None)
        ids = sorted(self.pages)
        if not ids:
            return {"browsers": [], "last_interacted_browser_id": self.last_interacted_browser_id}
        states = await asyncio.gather(*(self._state(browser_id) for browser_id in ids))
        if include_content:
            contents = await asyncio.gather(
                *(self.content(browser_id) for browser_id in ids),
                return_exceptions=True,
            )
            for idx, content in enumerate(contents):
                if isinstance(content, Exception):
                    states[idx]["content_error"] = str(content)
                else:
                    states[idx]["content"] = content
        return {"browsers": states, "last_interacted_browser_id": self.last_interacted_browser_id}

    async def multi(self, calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[Any, list[tuple[int, dict[str, Any]]]] = {}
        for idx, call in enumerate(calls):
            if not isinstance(call, dict):
                raise ValueError(f"calls[{idx}] is not an object")
            groups.setdefault(multi_group_key(call), []).append((idx, call))

        results: list[dict[str, Any] | None] = [None] * len(calls)

        async def run_group(group: list[tuple[int, dict[str, Any]]]) -> None:
            for idx, call in group:
                try:
                    normalized = dict(call)
                    normalized["action"] = normalize_action(normalized.get("action"))
                    out = await self.dispatch(normalized)
                    results[idx] = {"ok": True, "result": out}
                except Exception as exc:
                    results[idx] = {"ok": False, "error": str(exc)}

        await asyncio.gather(*(run_group(group) for group in groups.values()))
        return [item if item is not None else {"ok": False, "error": "missing"} for item in results]

    async def set_active(self, browser_id: int | str | None) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        self.last_interacted_browser_id = resolved_id
        with contextlib.suppress(Exception):
            await self._page(resolved_id).bring_to_front()
        return await self._state(resolved_id)

    async def state(self, browser_id: int | str | None = None) -> dict[str, Any]:
        await self.ensure_started()
        return await self._state(self._resolve_browser_id(browser_id))

    async def navigate(self, browser_id: int | str | None, url: str) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        await self._goto(self._page(resolved_id), normalize_url(url))
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def back(self, browser_id: int | str | None = None) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        await self._page(resolved_id).go_back(wait_until="domcontentloaded", timeout=10000)
        await self._settle(self._page(resolved_id))
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def forward(self, browser_id: int | str | None = None) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        await self._page(resolved_id).go_forward(wait_until="domcontentloaded", timeout=10000)
        await self._settle(self._page(resolved_id))
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def reload(self, browser_id: int | str | None = None) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        await self._page(resolved_id).reload(wait_until="domcontentloaded", timeout=15000)
        await self._settle(self._page(resolved_id))
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def content(
        self,
        browser_id: int | str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await self._ensure_content_helper(page)
        result = await page.evaluate(
            "(payload) => globalThis.__spaceBrowserPageContent__.capture(payload || null)",
            payload or None,
        )
        self._maybe_promote(resolved_id)
        return result or {}

    async def detail(self, browser_id: int | str | None, reference_id: int | str) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await self._ensure_content_helper(page)
        result = await page.evaluate(
            "(ref) => globalThis.__spaceBrowserPageContent__.detail(ref)",
            reference_id,
        )
        self._maybe_promote(resolved_id)
        return result or {}

    async def evaluate(self, browser_id: int | str | None, script: str) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        result = await page.evaluate(str(script or "undefined"))
        self._maybe_promote(resolved_id)
        return {"result": result, "state": await self._state(resolved_id)}

    async def click(
        self,
        browser_id: int | str | None,
        reference_id: int | str,
        modifiers: list[str] | str | None = None,
        focus_popup: bool | None = None,
    ) -> dict[str, Any]:
        del focus_popup
        normalized_modifiers = normalize_modifiers(modifiers)
        if normalized_modifiers:
            return await self._modifier_click(browser_id, reference_id, normalized_modifiers)
        return await self._reference_action("click", browser_id, reference_id)

    async def _modifier_click(
        self,
        browser_id: int | str | None,
        reference_id: int | str,
        modifiers: list[str],
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        point = await self._point_for(page, reference_id)
        pressed: list[str] = []
        try:
            for modifier in modifiers:
                await page.keyboard.down(modifier)
                pressed.append(modifier)
            await page.mouse.click(float(point["x"]), float(point["y"]))
        finally:
            for modifier in reversed(pressed):
                with contextlib.suppress(Exception):
                    await page.keyboard.up(modifier)
        await self._settle(page)
        self._maybe_promote(resolved_id)
        return {
            "action": {"ref": reference_id, "modifiers": modifiers, "point": point},
            "state": await self._state(resolved_id),
        }

    async def type(
        self,
        browser_id: int | str | None,
        reference_id: int | str,
        text: str,
    ) -> dict[str, Any]:
        return await self._reference_action("type", browser_id, reference_id, text)

    async def submit(self, browser_id: int | str | None, reference_id: int | str) -> dict[str, Any]:
        return await self._reference_action("submit", browser_id, reference_id)

    async def type_submit(
        self,
        browser_id: int | str | None,
        reference_id: int | str,
        text: str,
    ) -> dict[str, Any]:
        return await self._reference_action("typeSubmit", browser_id, reference_id, text)

    async def scroll(self, browser_id: int | str | None, reference_id: int | str) -> dict[str, Any]:
        return await self._reference_action("scroll", browser_id, reference_id)

    async def key_chord(self, browser_id: int | str | None, keys: list[str]) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        pressed: list[str] = []
        try:
            for key in keys:
                await page.keyboard.down(str(key))
                pressed.append(str(key))
        finally:
            for key in reversed(pressed):
                with contextlib.suppress(Exception):
                    await page.keyboard.up(key)
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def hover(
        self,
        browser_id: int | str | None,
        ref: int | str | None = None,
        x: float = 0,
        y: float = 0,
        offset_x: float = 0,
        offset_y: float = 0,
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        point = await self._input_point(page, ref, x=x, y=y, offset_x=offset_x, offset_y=offset_y)
        await page.mouse.move(float(point["x"]), float(point["y"]))
        self._maybe_promote(resolved_id)
        return {"action": {"point": point, "ref": ref if has_ref(ref) else None}, "state": await self._state(resolved_id)}

    async def double_click(
        self,
        browser_id: int | str | None,
        ref: int | str | None = None,
        x: float = 0,
        y: float = 0,
        button: str = "left",
        modifiers: list[str] | str | None = None,
        offset_x: float = 0,
        offset_y: float = 0,
    ) -> dict[str, Any]:
        normalized_modifiers = normalize_modifiers(modifiers)
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        point = await self._input_point(page, ref, x=x, y=y, offset_x=offset_x, offset_y=offset_y)
        pressed: list[str] = []
        try:
            for modifier in normalized_modifiers or []:
                await page.keyboard.down(modifier)
                pressed.append(modifier)
            await page.mouse.dblclick(float(point["x"]), float(point["y"]), button=button or "left")
        finally:
            for modifier in reversed(pressed):
                with contextlib.suppress(Exception):
                    await page.keyboard.up(modifier)
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return {"action": {"button": button or "left", "modifiers": normalized_modifiers or [], "point": point, "ref": ref if has_ref(ref) else None}, "state": await self._state(resolved_id)}

    async def right_click(
        self,
        browser_id: int | str | None,
        ref: int | str | None = None,
        x: float = 0,
        y: float = 0,
        modifiers: list[str] | str | None = None,
        offset_x: float = 0,
        offset_y: float = 0,
    ) -> dict[str, Any]:
        normalized_modifiers = normalize_modifiers(modifiers)
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        point = await self._input_point(page, ref, x=x, y=y, offset_x=offset_x, offset_y=offset_y)
        pressed: list[str] = []
        try:
            for modifier in normalized_modifiers or []:
                await page.keyboard.down(modifier)
                pressed.append(modifier)
            await page.mouse.click(float(point["x"]), float(point["y"]), button="right")
        finally:
            for modifier in reversed(pressed):
                with contextlib.suppress(Exception):
                    await page.keyboard.up(modifier)
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return {
            "action": {
                "button": "right",
                "modifiers": normalized_modifiers or [],
                "point": point,
                "ref": ref if has_ref(ref) else None,
            },
            "state": await self._state(resolved_id),
        }

    async def drag(
        self,
        browser_id: int | str | None,
        ref: int | str | None = None,
        target_ref: int | str | None = None,
        x: float = 0,
        y: float = 0,
        to_x: float = 0,
        to_y: float = 0,
        offset_x: float = 0,
        offset_y: float = 0,
        target_offset_x: float = 0,
        target_offset_y: float = 0,
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        start_point = await self._input_point(page, ref, x=x, y=y, offset_x=offset_x, offset_y=offset_y)
        end_point = await self._input_point(
            page,
            target_ref,
            x=to_x,
            y=to_y,
            offset_x=target_offset_x,
            offset_y=target_offset_y,
        )
        await page.mouse.move(float(start_point["x"]), float(start_point["y"]))
        await page.mouse.down()
        await page.mouse.move(float(end_point["x"]), float(end_point["y"]), steps=12)
        await page.mouse.up()
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return {"action": {"from": start_point, "to": end_point, "ref": ref if has_ref(ref) else None, "target_ref": target_ref if has_ref(target_ref) else None}, "state": await self._state(resolved_id)}

    async def wheel(
        self,
        browser_id: int | str | None,
        x: float,
        y: float,
        delta_x: float = 0,
        delta_y: float = 0,
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await page.mouse.move(float(x), float(y))
        await page.mouse.wheel(float(delta_x), float(delta_y))
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def mouse(
        self,
        browser_id: int | str | None,
        event_type: str,
        x: float,
        y: float,
        button: str = "left",
        modifiers: list[str] | str | None = None,
    ) -> dict[str, Any]:
        normalized_modifiers = normalize_modifiers(modifiers)
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        event_type_lower = str(event_type or "click").strip().lower()
        if event_type_lower == "move":
            await page.mouse.move(float(x), float(y))
        elif event_type_lower == "down":
            await page.mouse.down()
        elif event_type_lower == "up":
            await page.mouse.up()
        else:
            pressed: list[str] = []
            try:
                for modifier in normalized_modifiers or []:
                    await page.keyboard.down(modifier)
                    pressed.append(modifier)
                await page.mouse.click(float(x), float(y), button=button or "left")
            finally:
                for modifier in reversed(pressed):
                    with contextlib.suppress(Exception):
                        await page.keyboard.up(modifier)
            await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def keyboard(
        self,
        browser_id: int | str | None,
        *,
        key: str = "",
        text: str = "",
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        if text:
            await page.keyboard.type(str(text))
        elif key:
            await page.keyboard.press(str(key))
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return await self._state(resolved_id)

    async def clipboard(
        self,
        browser_id: int | str | None,
        *,
        action: str,
        text: str = "",
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"copy", "cut", "paste"}:
            raise ValueError(f"Unsupported clipboard action: {normalized_action}")

        shortcut_key = "Meta" if platform.system() == "Darwin" else "Control"
        result: dict[str, Any] = {"action": normalized_action, "changed": False, "handled": True}
        if normalized_action == "paste":
            insert_text = getattr(page.keyboard, "insert_text", None)
            if callable(insert_text):
                await insert_text(str(text or ""))
            else:
                await page.keyboard.type(str(text or ""))
            result["changed"] = bool(text)
        else:
            await page.keyboard.press(f"{shortcut_key}+{'C' if normalized_action == 'copy' else 'X'}")
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return {"state": await self._state(resolved_id), "clipboard": result}

    async def set_viewport(self, browser_id: int | str | None, width: int, height: int) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        viewport = {
            "width": max(320, min(4096, int(width or DEFAULT_VIEWPORT["width"]))),
            "height": max(200, min(4096, int(height or DEFAULT_VIEWPORT["height"]))),
        }
        await self._page(resolved_id).set_viewport_size(viewport)
        await self._settle(self._page(resolved_id), short=True)
        self._maybe_promote(resolved_id)
        return {"state": await self._state(resolved_id), "viewport": viewport}

    async def select_option(
        self,
        browser_id: int | str | None,
        ref: int | str,
        value: str = "",
        values: list[str] | None = None,
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await self._ensure_content_helper(page)
        action = await page.evaluate(
            "(args) => globalThis.__spaceBrowserPageContent__.select(args.ref, args.values)",
            {"ref": ref, "values": values if values is not None else value},
        )
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return {"action": action or {}, "state": await self._state(resolved_id)}

    async def set_checked(self, browser_id: int | str | None, ref: int | str, checked: bool = True) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await self._ensure_content_helper(page)
        action = await page.evaluate(
            "(args) => globalThis.__spaceBrowserPageContent__.setChecked(args.ref, args.checked)",
            {"ref": ref, "checked": bool(checked)},
        )
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return {"action": action or {}, "state": await self._state(resolved_id)}

    async def upload_file(
        self,
        browser_id: int | str | None,
        ref: int | str,
        path: str = "",
        paths: list[str] | None = None,
    ) -> dict[str, Any]:
        upload_paths = normalize_upload_paths(path=path, paths=paths)
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await self._ensure_content_helper(page)
        metadata = await page.evaluate(
            "(ref) => globalThis.__spaceBrowserPageContent__.fileInputFor(ref)",
            ref,
        )
        handle = None
        try:
            handle = await page.evaluate_handle(
                "(ref) => globalThis.__spaceBrowserPageContent__.fileInputElementFor(ref)",
                ref,
            )
            element = handle.as_element() if handle else None
            if element:
                await element.set_input_files(upload_paths)
            elif metadata and metadata.get("selector"):
                await page.set_input_files(metadata["selector"], upload_paths)
            else:
                raise ValueError(f"Browser ref {ref!r} does not resolve to a file input")
        finally:
            if handle:
                with contextlib.suppress(Exception):
                    await handle.dispose()
        await self._settle(page, short=True)
        self._maybe_promote(resolved_id)
        return {"action": {"files": upload_paths, "input": metadata or {}, "ref": ref}, "state": await self._state(resolved_id)}

    async def screenshot_file(
        self,
        browser_id: int | str | None = None,
        *,
        quality: int = 80,
        full_page: bool = False,
        path: str = "",
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        output_path, image_type, mime = screenshot_output_path(self.context_id, resolved_id, path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "path": str(output_path),
            "type": image_type,
            "full_page": bool(full_page),
        }
        if image_type == "jpeg":
            kwargs["quality"] = max(20, min(95, int(quality)))
        await page.screenshot(**kwargs)
        image = output_path.read_bytes()
        return {
            "browser_id": resolved_id,
            "host_path": str(output_path),
            "mime": mime,
            "artifact": {
                "filename": output_path.name,
                "mime": mime,
                "encoding": "base64",
                "data": base64.b64encode(image).decode("ascii"),
            },
            "state": await self._state(resolved_id),
        }

    async def close_browser(self, browser_id: int | str | None = None) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await page.close()
        self.pages.pop(resolved_id, None)
        if self.last_interacted_browser_id == resolved_id:
            self.last_interacted_browser_id = next(iter(sorted(self.pages)), None)
        return await self.list()

    async def close_all_browsers(self) -> dict[str, Any]:
        await self.ensure_started()
        for browser_id in list(self.pages):
            with contextlib.suppress(Exception):
                await self.pages[browser_id].page.close()
        self.pages.clear()
        self.last_interacted_browser_id = None
        return {"browsers": [], "last_interacted_browser_id": None}

    async def close(self) -> None:
        self._closing = True
        if not self.profile.is_remote_debugging:
            for browser_id in list(self.pages):
                with contextlib.suppress(Exception):
                    await self.pages[browser_id].page.close()
        self.pages.clear()
        if self.context is not None and not self.profile.is_remote_debugging:
            with contextlib.suppress(Exception):
                await self.context.close()
            self.context = None
        if isinstance(self.browser, CDPConnection):
            with contextlib.suppress(Exception):
                await self.browser.close()
        self.browser = None
        if self.playwright is not None:
            with contextlib.suppress(Exception):
                await self.playwright.stop()
            self.playwright = None
        self.context = None
        self.last_interacted_browser_id = None

    def _maybe_promote(self, resolved_id: int) -> None:
        current = self.last_interacted_browser_id
        if current is None or current == resolved_id:
            self.last_interacted_browser_id = int(resolved_id)

    async def _reference_action(
        self,
        helper_method: str,
        browser_id: int | str | None,
        reference_id: int | str,
        text: str | None = None,
    ) -> dict[str, Any]:
        await self.ensure_started()
        resolved_id = self._resolve_browser_id(browser_id)
        page = self._page(resolved_id)
        await self._ensure_content_helper(page)
        if text is None:
            action = await page.evaluate(
                "(args) => globalThis.__spaceBrowserPageContent__[args.method](args.ref)",
                {"method": helper_method, "ref": reference_id},
            )
        else:
            action = await page.evaluate(
                "(args) => globalThis.__spaceBrowserPageContent__[args.method](args.ref, args.text)",
                {"method": helper_method, "ref": reference_id, "text": text},
            )
        await self._settle(page)
        self._maybe_promote(resolved_id)
        return {"action": action or {}, "state": await self._state(resolved_id)}

    async def _point_for(
        self,
        page: Any,
        reference_id: int | str,
        *,
        offset_x: float = 0,
        offset_y: float = 0,
    ) -> dict[str, Any]:
        await self._ensure_content_helper(page)
        point = await page.evaluate(
            "(args) => globalThis.__spaceBrowserPageContent__.pointFor(args.ref, args.offsets)",
            {
                "ref": reference_id,
                "offsets": {
                    "offset_x": float(offset_x),
                    "offset_y": float(offset_y),
                    "useOffsets": bool(offset_x or offset_y),
                },
            },
        )
        if not point or not isinstance(point, dict):
            raise ValueError(f"Could not resolve Browser ref {reference_id!r} to a viewport point")
        return point

    async def _input_point(
        self,
        page: Any,
        reference_id: int | str | None,
        *,
        x: float = 0,
        y: float = 0,
        offset_x: float = 0,
        offset_y: float = 0,
    ) -> dict[str, Any]:
        if has_ref(reference_id):
            return await self._point_for(
                page,
                reference_id,
                offset_x=offset_x,
                offset_y=offset_y,
            )
        return {"x": float(x), "y": float(y), "rect": None, "selector": None}

    async def _goto(self, page: Any, url: str) -> None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            raise RuntimeError(f"Browser navigation failed for {url!r}: {exc}") from exc
        await self._settle(page)

    async def _settle(self, page: Any, short: bool = False) -> None:
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=1000 if short else 5000)
        await asyncio.sleep(0.1 if short else 0.35)

    async def _state(self, browser_id: int) -> dict[str, Any]:
        browser_page = self.pages.get(int(browser_id))
        if not browser_page:
            raise KeyError(f"Browser {browser_id} is not open.")
        page = browser_page.page
        if isinstance(page, CDPPage):
            with contextlib.suppress(Exception):
                current_url = await page.evaluate("() => location.href")
                if current_url:
                    page.url = str(current_url)
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            history_length = await page.evaluate("() => globalThis.history?.length || 0")
        except Exception:
            history_length = 0
        return {
            "id": browser_page.id,
            "context_id": self.context_id,
            "currentUrl": getattr(page, "url", ""),
            "title": title,
            "canGoBack": bool(history_length and int(history_length) > 1),
            "canGoForward": False,
            "loading": False,
            "runtime": "host",
        }

    async def _register_page(self, page: Any) -> HostBrowserPage:
        async with self._registry_lock:
            existing = self._browser_id_for_page(page)
            if existing is not None:
                return self.pages[existing]
            browser_id = self.next_browser_id
            self.next_browser_id += 1
            browser_page = HostBrowserPage(id=browser_id, page=page)
            self.pages[browser_id] = browser_page

            def on_close() -> None:
                try:
                    asyncio.create_task(self._unregister_page_async(browser_id))
                except RuntimeError:
                    self.pages.pop(browser_id, None)

            with contextlib.suppress(Exception):
                page.on("close", on_close)
            return browser_page

    async def _unregister_page_async(self, browser_id: int) -> None:
        async with self._registry_lock:
            self.pages.pop(browser_id, None)
            if self.last_interacted_browser_id == browser_id:
                self.last_interacted_browser_id = next(iter(sorted(self.pages)), None)

    def _on_new_page_sync(self, page: Any) -> None:
        if self._closing:
            return
        with contextlib.suppress(RuntimeError):
            asyncio.create_task(self._on_new_page_async(page))

    async def _on_new_page_async(self, page: Any) -> None:
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=2000)
        if self._closing:
            return
        browser_page = await self._register_page(page)
        if self.last_interacted_browser_id is None:
            self.last_interacted_browser_id = browser_page.id

    def _on_context_closed(self) -> None:
        if self._closing:
            return
        self.context = None
        self.pages.clear()
        self.last_interacted_browser_id = None

    def _browser_id_for_page(self, page: Any) -> int | None:
        for browser_id, browser_page in self.pages.items():
            if browser_page.page == page:
                return browser_id
        return None

    def _resolve_browser_id(self, browser_id: int | str | None = None) -> int:
        if browser_id is None or str(browser_id).strip() == "":
            if self.last_interacted_browser_id in self.pages:
                return int(self.last_interacted_browser_id)
            if self.pages:
                return sorted(self.pages)[0]
            raise KeyError("No browser is open. Use action=open first.")
        value = str(browser_id).strip()
        if value.startswith("browser-"):
            value = value.split("-", 1)[1]
        resolved = int(value)
        if resolved not in self.pages:
            raise KeyError(f"Browser {resolved} is not open.")
        return resolved

    def _page(self, browser_id: int) -> Any:
        return self.pages[int(browser_id)].page

    async def _ensure_content_helper(self, page: Any) -> None:
        has_helper = await page.evaluate(
            "() => Boolean(globalThis.__spaceBrowserPageContent__?.capture && globalThis.__spaceBrowserPageContent__?.detail && globalThis.__spaceBrowserPageContent__?.pointFor)"
        )
        if has_helper:
            return
        if self._content_helper_source is None:
            self._content_helper_source = CONTENT_HELPER_PATH.read_text(encoding="utf-8")
        await page.evaluate(self._content_helper_source)

    @staticmethod
    def _shadow_dom_script() -> str:
        return """
(() => {
  const original = Element.prototype.attachShadow;
  if (original && !original.__a0BrowserOpenShadowPatch) {
    const patched = function attachShadow(options) {
      return original.call(this, { ...(options || {}), mode: "open" });
    };
    patched.__a0BrowserOpenShadowPatch = true;
    Element.prototype.attachShadow = patched;
  }
})();
"""


class ProfileLockedError(RuntimeError):
    def __init__(self, message: str, *, lock_state: ProfileLockState):
        super().__init__(message)
        self.lock_state = lock_state


class HostBrowserManager:
    def __init__(
        self,
        config: CLIConfig,
        *,
        candidate_provider: Callable[[], list[BrowserCandidate]] | None = None,
        playwright_available: bool | None = None,
        playwright_starter: Callable[[], Any] | None = None,
        playwright_installer: Callable[[list[str]], Awaitable[tuple[int, str]]] | None = None,
    ) -> None:
        self.config = config
        self.enabled = bool(config.host_browser_enabled)
        self._candidate_provider = candidate_provider or detect_browser_candidates
        self._playwright_available = playwright_available
        self._playwright_starter = playwright_starter
        self._playwright_installer = playwright_installer or _run_install_command
        self._sessions: dict[str, HostBrowserSession] = {}
        self.last_error = ""

    @property
    def supported(self) -> bool:
        return self._support_reason() == ""

    def hello_metadata(self) -> dict[str, Any]:
        profile = self.selected_profile()
        status = self.status_snapshot(profile=profile)
        return {
            "supported": bool(status["supported"]),
            "can_prepare": bool(status["can_prepare"]),
            "enabled": bool(status["enabled"]),
            "status": status["status"],
            "browser_family": profile.family if profile else "",
            "profile_label": profile.profile_label if profile else "",
            "profile_path": profile.profile_path_display if profile else "",
            "cdp_endpoint": profile.cdp_endpoint if profile else "",
            "features": [
                "existing_profile",
                "dedicated_profile",
                "user_authorized_remote_debugging",
                "playwright",
                "artifacts",
                "background_tabs",
                "local_upload_paths",
                *sorted(_SUPPORTED_ACTIONS),
            ],
            "support_reason": status["support_reason"],
        }

    def metadata(self) -> dict[str, Any]:
        return self.hello_metadata()

    def status_snapshot(self, profile: BrowserProfile | None = None) -> dict[str, Any]:
        profile = profile if profile is not None else self.selected_profile()
        support_reason = self._support_reason(profile)
        supported = not support_reason
        can_prepare = self._can_prepare(profile)
        lock = profile_lock_state_for_profile(profile) if profile else ProfileLockState(False)
        if not supported:
            status = "unsupported"
        elif not self.enabled:
            status = "disabled"
        elif profile is not None and self._active_context_for_profile(profile):
            status = "active"
        elif lock.locked:
            status = "relaunch_required"
        else:
            status = "ready"
        return {
            "supported": supported,
            "can_prepare": can_prepare,
            "enabled": self.enabled and supported,
            "status": status,
            "browser_family": profile.family if profile else "",
            "profile_label": profile.profile_label if profile else "",
            "profile_path": profile.profile_path_display if profile else "",
            "cdp_endpoint": profile.cdp_endpoint if profile else "",
            "profile_locked": lock.locked,
            "lock": lock.as_dict(),
            "support_reason": support_reason,
            "last_error": self.last_error,
            "active_contexts": sorted(self._sessions),
        }

    def status_text(self) -> str:
        status = self.status_snapshot()
        if not status["supported"]:
            if status["can_prepare"]:
                return (
                    "Host browser can be prepared automatically when Agent Zero first uses "
                    "the Browser tool."
                )
            return f"Host browser unsupported: {status['support_reason']}"
        if status["status"] == "disabled":
            return "Host browser is disabled. Use /browser host on to advertise it to Agent Zero."
        profile_text = (
            f"remote debugging browser at {status['cdp_endpoint']}"
            if status.get("cdp_endpoint")
            else f"{status['browser_family']} profile {status['profile_label']} ({status['profile_path']})"
        )
        if status["status"] == "relaunch_required":
            return (
                f"Host browser needs relaunch consent for {profile_text}. "
                "Close that Chrome-family browser, then run /browser relaunch."
            )
        return f"Host browser {status['status']}: {profile_text}."

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.config.host_browser_enabled = self.enabled
        save_host_browser_enabled(self.enabled)

    def set_relaunch_preference(self, preference: str) -> str:
        normalized = normalize_host_browser_relaunch_preference(preference)
        self.config.host_browser_relaunch_preference = normalized
        save_host_browser_relaunch_preference(normalized)
        return normalized

    def has_playwright_dependency(self) -> bool:
        return self._has_playwright()

    def playwright_install_command(self) -> list[str]:
        return [sys.executable, "-m", "pip", "install", PLAYWRIGHT_PYTHON_PACKAGE]

    async def ensure_playwright_dependency(self) -> dict[str, object]:
        command = self.playwright_install_command()
        if self._has_playwright():
            return {"installed": False, "command": command, "output": ""}

        returncode, output = await self._playwright_installer(command)
        importlib.invalidate_caches()
        if self._playwright_available is not True:
            self._playwright_available = None
        if returncode != 0:
            raise RuntimeError(
                "Python Playwright install failed with exit code "
                f"{returncode}: {_trim_install_output(output)}"
            )
        if not self._has_playwright():
            raise RuntimeError(
                "Python Playwright install completed, but the package is still not importable "
                f"from {sys.executable}."
            )
        return {"installed": True, "command": command, "output": _trim_install_output(output)}

    def selected_profile(self) -> BrowserProfile | None:
        profiles = self.available_profiles()
        family = str(self.config.host_browser_family or "").strip().lower()
        profile_path = str(self.config.host_browser_profile_path or "").strip()
        profile_label = str(self.config.host_browser_profile_label or "").strip()
        remote_profiles = [profile for profile in profiles if profile.is_remote_debugging]
        if remote_profiles:
            matching_remote = self._matching_remote_profile(
                remote_profiles,
                family=family,
                profile_label=profile_label,
                profile_path=profile_path,
            )
            if matching_remote is not None:
                return matching_remote
        if family or profile_path or profile_label:
            for profile in profiles:
                if family and profile.family != family:
                    continue
                if profile_path and profile.profile_path_display != profile_path:
                    continue
                if profile_label and profile.profile_label != profile_label:
                    continue
                return profile
        for profile in profiles:
            if self._profile_support_reason(profile) == "":
                return profile
        return profiles[0] if profiles else None

    def available_profiles(self) -> list[BrowserProfile]:
        candidates = self._candidate_provider()
        profiles: list[BrowserProfile] = discover_remote_debugging_profiles(candidates)
        for candidate in candidates:
            profiles.extend(discover_profiles(candidate))
        return profiles

    def select_profile(self, family: str, profile_label: str = "", profile_path: str = "") -> BrowserProfile:
        family = str(family or "").strip().lower()
        profile_label = str(profile_label or "").strip()
        profile_path = str(profile_path or "").strip()
        for profile in self.available_profiles():
            if family and profile.family != family:
                continue
            if profile_label and profile.profile_label.lower() != profile_label.lower():
                continue
            if profile_path and profile.profile_path_display != profile_path:
                continue
            self._persist_selected_profile(profile)
            return profile
        raise ValueError("No matching Chrome-family profile was found.")

    async def relaunch(self) -> dict[str, Any]:
        return await self.ensure_available()

    async def ensure_available(self) -> dict[str, Any]:
        profile = self._auto_start_profile()
        if profile is None:
            raise RuntimeError("No Chrome-family browser profile was found.")
        if not profile.is_remote_debugging and not self._has_playwright():
            await self.ensure_playwright_dependency()
        support_reason = self._support_reason(profile)
        if support_reason:
            raise RuntimeError(support_reason)
        lock = profile_lock_state_for_profile(profile)
        active_context = self._active_context_for_profile(profile)
        if active_context:
            self.set_enabled(True)
            return self.status_snapshot(profile=profile)
        if lock.locked:
            raise ProfileLockedError(
                "The selected profile is still locked. Close the normal browser window first, "
                "then run /browser relaunch again.",
                lock_state=lock,
            )
        self.set_enabled(True)
        session = await self._session(RELAUNCH_CONTEXT_ID, profile=profile)
        await session.ensure_started()
        return self.status_snapshot(profile=profile)

    async def close(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.close()

    async def disconnect(self) -> None:
        await self.close()

    async def handle_op(self, payload: dict[str, Any]) -> dict[str, Any]:
        op_id = str(payload.get("op_id", "") or "").strip()
        action = normalize_action(payload.get("action"))
        context_id = str(payload.get("context_id", "") or "").strip() or "default"

        if not op_id:
            return {"op_id": "", "ok": False, "error": "op_id is required", "code": "MISSING_OP_ID"}
        if action not in _SUPPORTED_ACTIONS:
            return self._error(op_id, "UNKNOWN_ACTION", f"Unknown host browser action: {action!r}")
        if action == "ensure":
            try:
                return self._success(op_id, await self.ensure_available())
            except ProfileLockedError as exc:
                self.last_error = str(exc)
                profile = self.selected_profile()
                return self._error(
                    op_id,
                    "HOST_BROWSER_RELAUNCH_REQUIRED",
                    str(exc),
                    result={
                        "lock": exc.lock_state.as_dict(),
                        "profile": profile.as_dict() if profile else None,
                    },
                )
            except Exception as exc:
                self.last_error = str(exc)
                return self._error(op_id, "HOST_BROWSER_ERROR", str(exc))
        if action == "status":
            snapshot = self.status_snapshot()
            snapshot["context_id"] = context_id
            return self._success(op_id, snapshot)
        if not self.enabled:
            return self._error(op_id, "HOST_BROWSER_DISABLED", "Host browser is disabled in the A0 CLI.")

        profile = self.selected_profile()
        support_reason = self._support_reason(profile)
        if support_reason:
            return self._error(op_id, "HOST_BROWSER_UNSUPPORTED", support_reason)
        if profile is None:
            return self._error(op_id, "HOST_BROWSER_NO_PROFILE", "No Chrome-family browser profile was found.")

        lock = profile_lock_state_for_profile(profile)
        active_context = self._active_context_for_profile(profile)
        if lock.locked and context_id not in self._sessions and active_context != RELAUNCH_CONTEXT_ID:
            if active_context:
                return self._error(
                    op_id,
                    "HOST_BROWSER_CONTEXT_ACTIVE",
                    (
                        "Host browser is already controlled by another Agent Zero browser context. "
                        "Close that browser context before starting a new host-browser context."
                    ),
                    result={"active_context": active_context, "profile": profile.as_dict()},
                )
            return self._error(
                op_id,
                "HOST_BROWSER_RELAUNCH_REQUIRED",
                (
                    "The selected Chrome-family profile is already open. "
                    "Run /browser relaunch after closing that browser to give A0 explicit control."
                ),
                result={"lock": lock.as_dict(), "profile": profile.as_dict()},
            )

        try:
            session = await self._session(context_id, profile=profile)
            result = await session.dispatch(payload)
        except ProfileLockedError as exc:
            self.last_error = str(exc)
            return self._error(
                op_id,
                "HOST_BROWSER_RELAUNCH_REQUIRED",
                str(exc),
                result={"lock": exc.lock_state.as_dict(), "profile": profile.as_dict()},
            )
        except Exception as exc:
            self.last_error = str(exc)
            return self._error(op_id, "HOST_BROWSER_ERROR", str(exc))
        return self._success(op_id, result)

    async def _session(self, context_id: str, *, profile: BrowserProfile) -> HostBrowserSession:
        session = self._sessions.get(context_id)
        if session is not None and session.profile != profile:
            await session.close()
            self._sessions.pop(context_id, None)
            session = None
        if session is None and context_id != RELAUNCH_CONTEXT_ID:
            relaunch_session = self._sessions.get(RELAUNCH_CONTEXT_ID)
            if relaunch_session is not None and relaunch_session.profile == profile:
                self._sessions.pop(RELAUNCH_CONTEXT_ID, None)
                relaunch_session.context_id = context_id
                session = relaunch_session
                self._sessions[context_id] = session
        if session is None:
            session = HostBrowserSession(
                context_id=context_id,
                profile=profile,
                playwright_starter=self._playwright_starter,
            )
            self._sessions[context_id] = session
        return session

    def _active_context_for_profile(self, profile: BrowserProfile) -> str:
        for context_id, session in self._sessions.items():
            if session.profile == profile and session.context is not None:
                return context_id
        return ""

    def _active_profile(self) -> BrowserProfile | None:
        for session in self._sessions.values():
            if session.context is not None:
                return session.profile
        return None

    def _auto_start_profile(self) -> BrowserProfile | None:
        active_profile = self._active_profile()
        if active_profile is not None:
            return active_profile
        remote_profile = self._first_remote_debugging_profile()
        if remote_profile is not None:
            self._persist_selected_profile(remote_profile)
            return remote_profile
        profile = self.selected_profile()
        if profile is not None and self._profile_support_reason(profile) == "":
            return profile
        fallback = self._first_supported_profile()
        if fallback is not None:
            self._persist_selected_profile(fallback)
            return fallback
        return profile

    def _first_supported_profile(self) -> BrowserProfile | None:
        for profile in self.available_profiles():
            if self._profile_support_reason(profile) == "":
                return profile
        return None

    def _first_remote_debugging_profile(self) -> BrowserProfile | None:
        for profile in self.available_profiles():
            if profile.is_remote_debugging and self._profile_support_reason(profile) == "":
                return profile
        return None

    def _matching_remote_profile(
        self,
        remote_profiles: list[BrowserProfile],
        *,
        family: str,
        profile_label: str,
        profile_path: str,
    ) -> BrowserProfile | None:
        if not family or is_a0_managed_family(family) or is_remote_debugging_family(family):
            if profile_label:
                for profile in remote_profiles:
                    if profile.profile_label.lower() == profile_label.lower():
                        return profile
            return remote_profiles[0]
        base_family = base_browser_family(family)
        for profile in remote_profiles:
            if base_browser_family(profile.family) != base_family:
                continue
            profile_path_matches = profile_path in {profile.profile_path_display, str(profile.user_data_dir)}
            if profile_path and not profile_path_matches:
                continue
            if profile_label and profile_label.lower() != profile.profile_label.lower() and not profile_path_matches:
                continue
            return profile
        return None

    def _can_prepare(self, profile: BrowserProfile | None = None) -> bool:
        if not CONTENT_HELPER_PATH.exists():
            return False
        if profile is not None and self._profile_support_reason(profile) == "":
            return True
        return self._first_supported_profile() is not None

    def _persist_selected_profile(self, profile: BrowserProfile) -> None:
        self.config.host_browser_family = profile.family
        self.config.host_browser_profile_path = profile.profile_path_display
        self.config.host_browser_profile_label = profile.profile_label
        save_host_browser_profile(
            family=profile.family,
            profile_path=profile.profile_path_display,
            profile_label=profile.profile_label,
        )

    def _support_reason(self, profile: BrowserProfile | None = None) -> str:
        if not CONTENT_HELPER_PATH.exists():
            return "Host browser content helper is missing from the A0 CLI package."
        profile = profile if profile is not None else self.selected_profile()
        if profile is not None and profile.is_remote_debugging:
            return self._profile_support_reason(profile)
        if not self._has_playwright():
            return (
                f"Python Playwright is not installed in the A0 CLI host environment ({sys.executable}). "
                "Run /browser repair in the A0 CLI, or install it with: "
                f"{sys.executable} -m pip install {PLAYWRIGHT_PYTHON_PACKAGE}. "
                "The Playwright runtime inside the Agent Zero Docker container is used by the "
                "container browser backend and cannot control host Chrome-family profiles."
            )
        return self._profile_support_reason(profile)

    def _profile_support_reason(self, profile: BrowserProfile | None) -> str:
        if profile is None:
            return "No installed Chrome-family browser profile was detected."
        if profile.is_remote_debugging:
            return ""
        if not profile.executable_path or not Path(profile.executable_path).exists():
            return "Selected Chrome-family browser executable was not found."
        restriction_reason = remote_debugging_restriction_reason(profile)
        if restriction_reason:
            return restriction_reason
        return ""

    def _has_playwright(self) -> bool:
        if self._playwright_available is not None:
            return bool(self._playwright_available)
        try:
            return importlib.util.find_spec("playwright.async_api") is not None
        except (ModuleNotFoundError, ValueError):
            return False

    def _success(self, op_id: str, result: Any) -> dict[str, Any]:
        return {"op_id": op_id, "ok": True, "result": result}

    def _error(
        self,
        op_id: str,
        code: str,
        message: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "op_id": op_id,
            "ok": False,
            "code": code,
            "error": message,
        }
        if result is not None:
            payload["result"] = result
        return payload


def normalize_action(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "setactive": "set_active",
        "activate": "set_active",
        "focus": "set_active",
        "typesubmit": "type_submit",
        "keychord": "key_chord",
        "close_browser": "close",
        "close_all_browsers": "close_all",
    }
    return aliases.get(normalized, normalized)


def action_is_sensitive(payload: dict[str, Any]) -> bool:
    action = normalize_action(payload.get("action"))
    if action in _SENSITIVE_ACTIONS:
        return True
    if action == "list" and coerce_bool(payload.get("include_content")):
        return True
    if action == "multi":
        calls = payload.get("calls")
        if isinstance(calls, list):
            return any(action_is_sensitive(call) for call in calls if isinstance(call, dict))
    return False


def normalize_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Browser navigation requires a non-empty URL.")

    def with_trailing_path(url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme in {"http", "https"} and not parts.path:
            return urlunsplit((parts.scheme, parts.netloc, "/", parts.query, parts.fragment))
        return urlunsplit(parts)

    try:
        host = re.split(r"[/?#]", raw, maxsplit=1)[0] or ""
        if (
            not _URL_SCHEME_RE.match(raw)
            and not _SPECIAL_SCHEME_RE.match(raw)
            and not raw.startswith(("/", "?", "#", "."))
            and not re.search(r"\s", raw)
            and _TYPED_HOST_RE.match(host)
        ):
            protocol = "http://" if _LOCAL_HOST_RE.match(host) else "https://"
            return with_trailing_path(protocol + raw)
        parts = urlsplit(raw)
        if parts.scheme:
            return with_trailing_path(raw)
    except Exception:
        pass
    return with_trailing_path("https://" + raw)


def detect_browser_candidates() -> list[BrowserCandidate]:
    system = platform.system()
    if system == "Darwin":
        return _detect_macos_candidates()
    if system == "Windows":
        return _detect_windows_candidates()
    return _detect_linux_candidates()


def _detect_macos_candidates() -> list[BrowserCandidate]:
    home = Path.home()
    specs = [
        ("chrome", "Google Chrome", Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), home / "Library/Application Support/Google/Chrome"),
        ("chromium", "Chromium", Path("/Applications/Chromium.app/Contents/MacOS/Chromium"), home / "Library/Application Support/Chromium"),
        ("edge", "Microsoft Edge", Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"), home / "Library/Application Support/Microsoft Edge"),
    ]
    candidates = [
        BrowserCandidate(f, label, str(exe), profile)
        for f, label, exe, profile in specs
        if exe.exists()
    ]
    return _with_a0_managed_candidates(candidates)


def _detect_windows_candidates() -> list[BrowserCandidate]:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    program_files = [Path(os.environ.get("PROGRAMFILES", "")), Path(os.environ.get("PROGRAMFILES(X86)", ""))]
    specs: list[tuple[str, str, list[Path], Path]] = [
        (
            "chrome",
            "Google Chrome",
            [base / "Google/Chrome/Application/chrome.exe" for base in program_files if str(base)],
            local_app_data / "Google/Chrome/User Data",
        ),
        (
            "chromium",
            "Chromium",
            [local_app_data / "Chromium/Application/chrome.exe"],
            local_app_data / "Chromium/User Data",
        ),
        (
            "edge",
            "Microsoft Edge",
            [base / "Microsoft/Edge/Application/msedge.exe" for base in program_files if str(base)],
            local_app_data / "Microsoft/Edge/User Data",
        ),
    ]
    candidates: list[BrowserCandidate] = []
    for family, label, executables, profile in specs:
        executable = next((path for path in executables if path.exists()), None)
        if executable is not None:
            candidates.append(BrowserCandidate(family, label, str(executable), profile))
    return _with_a0_managed_candidates(candidates)


def _detect_linux_candidates() -> list[BrowserCandidate]:
    home_config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    specs = [
        ("chrome", "Google Chrome", ("google-chrome", "google-chrome-stable"), home_config / "google-chrome"),
        ("chromium", "Chromium", ("chromium", "chromium-browser"), home_config / "chromium"),
        ("edge", "Microsoft Edge", ("microsoft-edge", "microsoft-edge-stable"), home_config / "microsoft-edge"),
        ("edge-dev", "Microsoft Edge Dev", ("microsoft-edge-dev",), home_config / "microsoft-edge-dev"),
    ]
    candidates: list[BrowserCandidate] = []
    for family, label, names, profile in specs:
        executable = next((shutil.which(name) for name in names if shutil.which(name)), None)
        if executable:
            candidates.append(BrowserCandidate(family, label, executable, profile))
    return _with_a0_managed_candidates(candidates)


def _with_a0_managed_candidates(candidates: list[BrowserCandidate]) -> list[BrowserCandidate]:
    expanded: list[BrowserCandidate] = []
    for candidate in candidates:
        expanded.append(candidate)
        expanded.append(
            BrowserCandidate(
                family=f"{candidate.family}-a0",
                label=f"{candidate.label} (A0 controlled profile)",
                executable_path=candidate.executable_path,
                user_data_dir=a0_managed_user_data_dir(candidate.family),
            )
        )
    return expanded


def a0_managed_user_data_dir(family: str) -> Path:
    family_slug = _SAFE_CONTEXT_RE.sub("-", str(family or "chrome").strip().lower()).strip("-")
    system = platform.system()
    if system == "Darwin":
        root = Path.home() / "Library/Application Support/A0/Browser Profiles"
    elif system == "Windows":
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        root = local_app_data / "A0/Browser Profiles"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "a0/browser-profiles"
    return root / family_slug


def is_a0_managed_family(family: str) -> bool:
    return str(family or "").strip().lower().endswith("-a0")


def is_remote_debugging_family(family: str) -> bool:
    return str(family or "").strip().lower().endswith("-cdp")


def base_browser_family(family: str) -> str:
    normalized = str(family or "").strip().lower()
    for suffix in ("-a0", "-cdp"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def discover_profiles(candidate: BrowserCandidate) -> list[BrowserProfile]:
    root = candidate.user_data_dir.expanduser()
    if is_a0_managed_family(candidate.family):
        return [
            BrowserProfile(
                family=candidate.family,
                family_label=candidate.label,
                executable_path=candidate.executable_path,
                user_data_dir=root,
                profile_directory="Default",
                display_name="A0 controlled",
            )
        ]
    if not root.exists():
        return []
    display_names = _profile_display_names(root)
    profile_dirs = _profile_directories(root)
    profiles: list[BrowserProfile] = []
    for profile_dir in profile_dirs:
        display = display_names.get(profile_dir.name) or profile_dir.name
        profiles.append(
            BrowserProfile(
                family=candidate.family,
                family_label=candidate.label,
                executable_path=candidate.executable_path,
                user_data_dir=root,
                profile_directory=profile_dir.name,
                display_name=display,
            )
        )
    return profiles


def discover_remote_debugging_profiles(candidates: Iterable[BrowserCandidate] | None = None) -> list[BrowserProfile]:
    profiles: list[BrowserProfile] = []
    seen: set[str] = set()
    candidate_list = list(candidates) if candidates is not None else detect_browser_candidates()
    for candidate in candidate_list:
        if is_a0_managed_family(candidate.family):
            continue
        endpoint = remote_debugging_endpoint_from_user_data_dir(candidate.user_data_dir)
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        profiles.append(remote_debugging_profile_from_candidate(candidate, endpoint))
    for endpoint in remote_debugging_endpoint_candidates():
        if endpoint in seen:
            continue
        seen.add(endpoint)
        profiles.append(
            BrowserProfile(
                family="chrome-cdp",
                family_label="Chrome-family browser (remote debugging)",
                executable_path="",
                user_data_dir=Path(),
                profile_directory=remote_debugging_endpoint_label(endpoint),
                display_name="Remote debugging allowed",
                cdp_endpoint=endpoint,
            )
        )
    return profiles


def remote_debugging_endpoint_candidates() -> list[str]:
    raw = os.environ.get(HOST_BROWSER_REMOTE_DEBUGGING_ENDPOINTS_ENV, "")
    values = re.split(r"[\s,]+", raw.strip()) if raw.strip() else []
    endpoints: list[str] = []
    seen: set[str] = set()
    for value in values:
        endpoint = normalize_remote_debugging_endpoint(value)
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        endpoints.append(endpoint)
    return endpoints


def normalize_remote_debugging_endpoint(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"ws://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme not in {"ws", "wss"} or not parsed.netloc or not parsed.path:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def remote_debugging_endpoint_from_user_data_dir(user_data_dir: Path | str) -> str:
    return remote_debugging_endpoint_from_active_port_file(Path(user_data_dir).expanduser() / "DevToolsActivePort")


def remote_debugging_endpoint_from_active_port_file(active_port_file: Path | str) -> str:
    path = Path(active_port_file).expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    if len(lines) < 2:
        return ""
    port = lines[0].strip()
    devtools_path = lines[1].strip()
    if not port.isdigit() or not devtools_path.startswith("/"):
        return ""
    return normalize_remote_debugging_endpoint(f"ws://localhost:{int(port)}{devtools_path}")


def remote_debugging_profile_from_candidate(candidate: BrowserCandidate, endpoint: str) -> BrowserProfile:
    family = f"{base_browser_family(candidate.family)}-cdp"
    return BrowserProfile(
        family=family,
        family_label=f"{candidate.label} (remote debugging)",
        executable_path="",
        user_data_dir=candidate.user_data_dir.expanduser(),
        profile_directory=remote_debugging_endpoint_label(endpoint),
        display_name="Remote debugging allowed",
        cdp_endpoint=endpoint,
    )


def remote_debugging_endpoint_label(endpoint: str) -> str:
    normalized = normalize_remote_debugging_endpoint(endpoint) or str(endpoint or "")
    parsed = urlsplit(normalized if "://" in normalized else f"ws://{normalized}")
    return parsed.netloc or "remote debugging"


def _profile_directories(user_data_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    for name in ("Default", "Guest Profile"):
        path = user_data_dir / name
        if path.is_dir():
            candidates.append(path)
    candidates.extend(sorted(path for path in user_data_dir.glob("Profile *") if path.is_dir()))
    if not candidates and user_data_dir.exists():
        candidates.append(user_data_dir / "Default")
    return candidates


def _profile_display_names(user_data_dir: Path) -> dict[str, str]:
    local_state = user_data_dir / "Local State"
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
    except Exception:
        return {}
    info_cache = data.get("profile", {}).get("info_cache", {})
    if not isinstance(info_cache, dict):
        return {}
    names: dict[str, str] = {}
    for profile_dir, info in info_cache.items():
        if isinstance(info, dict):
            name = str(info.get("name") or info.get("user_name") or "").strip()
            if name:
                names[str(profile_dir)] = name
    return names


def profile_lock_state(profile_path: Path | str) -> ProfileLockState:
    root = Path(profile_path).expanduser()
    lock_files: list[str] = []
    owner_pid: int | None = None
    for name in CHROME_SINGLETON_FILES:
        path = root / name
        if path.exists() or path.is_symlink():
            lock_files.append(str(path))
            if name == "SingletonLock":
                owner_pid = owner_pid or _singleton_lock_owner_pid(path)
    if owner_pid is not None and not _pid_is_alive(owner_pid):
        return ProfileLockState(locked=False, lock_files=(), owner_pid=owner_pid)
    return ProfileLockState(locked=bool(lock_files), lock_files=tuple(lock_files), owner_pid=owner_pid)


def profile_lock_state_for_profile(profile: BrowserProfile) -> ProfileLockState:
    if profile.is_remote_debugging:
        return ProfileLockState(False)
    return profile_lock_state(profile.profile_path)


def is_profile_locked(profile_path: Path | str) -> bool:
    return profile_lock_state(profile_path).locked


def _singleton_lock_owner_pid(path: Path) -> int | None:
    try:
        target = os.readlink(path)
    except OSError:
        return None
    raw_pid = target.rsplit("-", 1)[-1]
    if raw_pid.isdigit():
        return int(raw_pid)
    return None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def chromium_launch_args(profile_directory: str) -> list[str]:
    args = [f"--profile-directory={profile_directory}"]
    explicit_ozone = os.environ.get(HOST_BROWSER_OZONE_PLATFORM_ENV, "").strip()
    if explicit_ozone:
        args.append(f"--ozone-platform={explicit_ozone}")
    elif (
        platform.system() == "Linux"
        and os.environ.get("WAYLAND_DISPLAY")
        and not os.environ.get("DISPLAY")
    ):
        args.append("--ozone-platform=wayland")
    return args


def remote_debugging_restriction_reason(profile: BrowserProfile) -> str:
    if profile.is_remote_debugging:
        return ""
    if is_a0_managed_family(profile.family):
        return ""
    major = browser_major_version(profile.executable_path)
    if (
        major is not None
        and major >= REMOTE_DEBUGGING_RESTRICTED_MAJOR
        and is_default_user_data_dir(profile.family, profile.user_data_dir)
    ):
        managed_family = f"{profile.family}-a0"
        return (
            "This Chrome-family browser blocks Playwright remote debugging for its default "
            f"data directory in version {major}+. Select the A0-controlled local profile with "
            f"/browser profile {managed_family} Default, then run /browser relaunch. "
            "Cookies and site data stay inside that separate browser profile on this host."
        )
    return ""


@lru_cache(maxsize=32)
def browser_major_version(executable_path: str) -> int | None:
    try:
        result = subprocess.run(
            [executable_path, "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    match = re.search(r"\b(\d+)\.", result.stdout or "")
    if not match:
        return None
    return int(match.group(1))


def is_default_user_data_dir(family: str, user_data_dir: Path | str) -> bool:
    normalized_family = str(family or "").strip().lower().removesuffix("-a0")
    root = _resolve_path(user_data_dir)
    return any(root == _resolve_path(path) for path in default_user_data_dirs(normalized_family))


def default_user_data_dirs(family: str) -> list[Path]:
    normalized_family = str(family or "").strip().lower()
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        base = home / "Library/Application Support"
        mapping = {
            "chrome": base / "Google/Chrome",
            "chromium": base / "Chromium",
            "edge": base / "Microsoft Edge",
            "edge-dev": base / "Microsoft Edge Dev",
        }
    elif system == "Windows":
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local")
        mapping = {
            "chrome": local_app_data / "Google/Chrome/User Data",
            "chromium": local_app_data / "Chromium/User Data",
            "edge": local_app_data / "Microsoft/Edge/User Data",
            "edge-dev": local_app_data / "Microsoft/Edge Dev/User Data",
        }
    else:
        home_config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        mapping = {
            "chrome": home_config / "google-chrome",
            "chromium": home_config / "chromium",
            "edge": home_config / "microsoft-edge",
            "edge-dev": home_config / "microsoft-edge-dev",
        }
    return [mapping[normalized_family]] if normalized_family in mapping else []


def _resolve_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def coerce_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled", ""}:
        return False
    return default


def coerce_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def coerce_float(value: object, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def has_ref(value: object) -> bool:
    return value is not None and str(value).strip() != ""


def require_ref(value: object, action: str) -> int | str:
    if not has_ref(value):
        raise ValueError(f"{action} requires ref")
    return value  # type: ignore[return-value]


def normalize_modifiers(modifiers: list[str] | str | None) -> list[str] | None:
    if modifiers is None:
        return None
    raw = [modifiers] if isinstance(modifiers, str) else list(modifiers)
    normalized = [str(item).strip() for item in raw if str(item).strip()]
    if not normalized:
        return None
    invalid = set(normalized) - _VALID_MODIFIERS
    if invalid:
        raise ValueError(f"unsupported modifiers: {sorted(invalid)}; allowed: {sorted(_VALID_MODIFIERS)}")
    return normalized


def normalize_upload_paths(path: str = "", paths: list[str] | None = None) -> list[str]:
    raw_paths: list[str] = []
    if isinstance(paths, list):
        raw_paths.extend(str(item or "").strip() for item in paths)
    if str(path or "").strip():
        raw_paths.append(str(path or "").strip())
    normalized: list[str] = []
    for raw_path in raw_paths:
        if not raw_path:
            continue
        candidate = Path(raw_path).expanduser().resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"Upload file does not exist on the CLI host: {candidate}")
        normalized.append(str(candidate))
    if not normalized:
        raise ValueError("upload_file requires path or non-empty paths")
    return normalized


def artifact_root() -> Path:
    configured = os.environ.get(HOST_BROWSER_ARTIFACT_ROOT_ENV, "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_HOST_BROWSER_ARTIFACT_ROOT


def safe_context_id(context_id: str) -> str:
    return _SAFE_CONTEXT_RE.sub("_", str(context_id or "default")).strip("._") or "default"


def screenshot_output_path(context_id: str, browser_id: int, path: str = "") -> tuple[Path, str, str]:
    raw_path = str(path or "").strip()
    if raw_path:
        output_path = Path(raw_path).expanduser()
        if not output_path.is_absolute():
            output_path = artifact_root() / safe_context_id(context_id) / output_path
        suffix = output_path.suffix.lower()
        if suffix == ".png":
            return output_path, "png", "image/png"
        if suffix not in {".jpg", ".jpeg"}:
            output_path = output_path.with_suffix(".jpg") if not suffix else output_path.with_name(f"{output_path.name}.jpg")
        return output_path, "jpeg", "image/jpeg"

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    millis = int((time.time() % 1) * 1000)
    output_path = artifact_root() / safe_context_id(context_id) / f"host-browser-{int(browser_id)}-{timestamp}-{millis:03d}.jpg"
    return output_path, "jpeg", "image/jpeg"


def multi_group_key(call: dict[str, Any]) -> Any:
    value = call.get("browser_id")
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    if raw.startswith("browser-"):
        raw = raw.split("-", 1)[1]
    try:
        return int(raw)
    except ValueError:
        return raw


async def _run_install_command(command: list[str]) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    output = stdout.decode("utf-8", errors="replace") if stdout else ""
    return int(process.returncode or 0), output


def _trim_install_output(output: str) -> str:
    cleaned = str(output or "").strip()
    if not cleaned:
        return "no output"
    if len(cleaned) <= MAX_INSTALL_OUTPUT_CHARS:
        return cleaned
    return "..." + cleaned[-MAX_INSTALL_OUTPUT_CHARS:]


def format_profile_rows(profiles: Iterable[BrowserProfile]) -> list[str]:
    rows = []
    for profile in profiles:
        lock = "allowed" if profile.is_remote_debugging else "locked" if is_profile_locked(profile.profile_path) else "ready"
        rows.append(
            f"{profile.family} {profile.profile_label} - {profile.display_name} "
            f"({profile.profile_path_display}) [{lock}]"
        )
    return rows
