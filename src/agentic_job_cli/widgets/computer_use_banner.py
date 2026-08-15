from __future__ import annotations

from textual.widgets import Static


def _normalize_status(value: str) -> str:
    return str(value or "").strip().lower()


def _is_windows_backend(*, backend_id: str = "", backend_family: str = "") -> bool:
    return _normalize_status(backend_id) == "windows" or _normalize_status(backend_family) == "windows"


def _message_for_status(
    status: str,
    *,
    enabled: bool,
    backend_id: str = "",
    backend_family: str = "",
) -> str:
    normalized = _normalize_status(status)
    if not enabled or normalized == "disabled":
        return ""
    if _is_windows_backend(backend_id=backend_id, backend_family=backend_family):
        if normalized in {"approval required", "rearm required"}:
            return "Computer Use is checking Windows desktop access."
    if normalized == "active":
        return "Computer Use is active for this CLI session."
    if normalized == "arming":
        return "Computer Use is checking host permissions."
    if normalized == "approval required":
        return (
            "Computer Use is enabled. Ask Agentic Job to perform the desktop task; "
            "the system permission portal will appear."
        )
    if normalized == "rearm required":
        return "Computer use needs re-arming before Agentic Job can control your computer again."
    return "Agentic Job CLI can control your computer in this session."


class ComputerUseBanner(Static):
    """High-visibility warning banner above the composer controls."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("", id=id)
        self.display = False

    def set_state(
        self,
        *,
        enabled: bool,
        status: str = "",
        backend_id: str = "",
        backend_family: str = "",
    ) -> None:
        message = _message_for_status(
            status,
            enabled=enabled,
            backend_id=backend_id,
            backend_family=backend_family,
        )
        self.display = bool(message)
        self.update(message)


__all__ = ["ComputerUseBanner"]
