"""Helpers for Agentic Job's lightweight icon marker text."""

from __future__ import annotations

import re


_ICON_MARKER_RE = re.compile(r"icon://[A-Za-z0-9_]+(?:\[(?:\\.|[^\]])*\])?\s*")


def strip_icon_markers(value: object) -> str:
    """Remove WebUI icon markers from text rendered in the terminal UI."""
    return _ICON_MARKER_RE.sub("", str(value or ""))


def normalize_icon_text(value: object) -> str:
    """Remove icon markers and collapse whitespace for compact status text."""
    return " ".join(strip_icon_markers(value).split())
