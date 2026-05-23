from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_zero_cli.widgets import SplashState, SplashView
from agent_zero_cli.widgets.splash_view import (
    SplashHostPanel,
    _normalize_connection_target,
    _validate_connection_target,
)


@pytest.mark.parametrize(
    "host",
    [
        "https://webmasters-ink-tribe-zope.trycloudflare.com",
        "https://webmasters-ink-tribe-zope.trycloudflare.com:443",
        "http://localhost",
    ],
)
def test_validate_connection_target_accepts_standard_urls_without_explicit_port(host: str) -> None:
    valid, message = _validate_connection_target(host)

    assert valid is True
    assert message.startswith("URL format looks valid.")


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost:32080", "http://localhost:32080"),
        ("127.0.0.1:5080", "http://127.0.0.1:5080"),
        ("[::1]:5080", "http://[::1]:5080"),
        ("agent-zero.example.com", "http://agent-zero.example.com"),
        ("//localhost:32080", "http://localhost:32080"),
        ("https://agent-zero.example.com/", "https://agent-zero.example.com"),
    ],
)
def test_normalize_connection_target_adds_http_for_manual_hostnames(
    host: str,
    expected: str,
) -> None:
    assert _normalize_connection_target(host) == expected


def test_validate_connection_target_accepts_manual_host_without_scheme() -> None:
    valid, message = _validate_connection_target("localhost:32080")

    assert valid is True
    assert message == "URL format looks valid. Using http://localhost:32080."


def test_validate_connection_target_rejects_unsupported_scheme() -> None:
    valid, message = _validate_connection_target("ws://localhost:32080")

    assert valid is False
    assert message == "Invalid URL format. Include http:// or https://."


def test_manual_host_panel_connect_host_normalizes_url() -> None:
    panel = SplashHostPanel()
    panel._state = SplashState(stage="host", manual_entry_expanded=True)
    panel._host = SimpleNamespace(value="localhost:32080")  # type: ignore[assignment]

    assert panel.connect_host == "http://localhost:32080"


def test_error_back_button_requests_navigation_to_host() -> None:
    view = SplashView()
    messages: list[object] = []
    view.post_message = lambda message: messages.append(message)  # type: ignore[method-assign]

    view.on_button_pressed(
        SimpleNamespace(button=SimpleNamespace(id="splash-status-back"))  # type: ignore[arg-type]
    )

    assert len(messages) == 1
    assert isinstance(messages[0], SplashView.ActionRequested)
    assert messages[0].action == "back"
