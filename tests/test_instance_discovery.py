from __future__ import annotations

import json

import pytest

import agent_zero_cli.instance_discovery as discovery


pytestmark = pytest.mark.anyio


async def test_discover_local_instances_reports_unavailable_without_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discovery, "_find_docker_cli", lambda: None)

    result = await discovery.discover_local_instances()

    assert result.status == "unavailable"
    assert result.instances == ()
    assert result.detail == "Docker CLI was not found. Enter a URL manually."


async def test_discover_local_instances_returns_multiple_agent_zero_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "Id": "container-a",
            "Name": "/agent-zero",
            "Config": {"Image": "agent0ai/agent-zero:latest"},
            "State": {"Running": True},
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5080"}]}},
        },
        {
            "Id": "container-b",
            "Name": "/agent-zero-2",
            "Config": {"Image": "agent0ai/agent-zero:latest"},
            "State": {"Running": True},
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5081"}]}},
        },
    ]

    async def fake_run_command(*args: str) -> discovery._CommandResult:
        if args[-1] == "{{.ID}}":
            return discovery._CommandResult(returncode=0, stdout="container-a\ncontainer-b\n", stderr="")
        return discovery._CommandResult(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(discovery, "_find_docker_cli", lambda: "docker")
    monkeypatch.setattr(discovery, "_run_command", fake_run_command)

    result = await discovery.discover_local_instances()

    assert result.status == "ready"
    assert [instance.url for instance in result.instances] == [
        "http://localhost:5080",
        "http://127.0.0.1:5081",
    ]
    assert [instance.host_port for instance in result.instances] == ["5080", "5081"]


async def test_discover_local_instances_prefers_launcher_friendly_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "Id": "container-a",
            "Name": "/a0-inst-agent-zero-latest-mqo7xckc",
            "Config": {
                "Image": "a0-launcher-clone:clone-mqo7vz54-0e399e4dd406",
                "Labels": {"a0.launcher.instanceName": "agent-zero-latest"},
            },
            "State": {"Running": True},
            "Mounts": [{"Destination": "/a0", "Type": "bind"}],
            "NetworkSettings": {"Ports": {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32769"}]}},
        },
    ]

    async def fake_run_command(*args: str) -> discovery._CommandResult:
        if args[-1] == "{{.ID}}":
            return discovery._CommandResult(returncode=0, stdout="container-a\n", stderr="")
        return discovery._CommandResult(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(discovery, "_find_docker_cli", lambda: "docker")
    monkeypatch.setattr(discovery, "_run_command", fake_run_command)

    result = await discovery.discover_local_instances()

    assert result.status == "ready"
    assert len(result.instances) == 1
    instance = result.instances[0]
    assert instance.name == "agent-zero-latest"
    assert instance.status_text == "agent-zero-latest"
    assert instance.url == "http://127.0.0.1:32769"
