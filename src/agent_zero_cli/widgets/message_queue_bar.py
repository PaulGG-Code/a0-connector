"""Compact preview for Agent Zero queued messages."""

from __future__ import annotations

from typing import Any, Iterable

from rich.text import Text
from textual.widgets import Static


_EMPTY_ATTACHMENT_LABEL = "(attachment only)"
_MAX_VISIBLE_ITEMS = 4
_MAX_ITEM_TEXT = 96


class MessageQueueBar(Static):
    """Small queue preview shown above the composer."""

    DEFAULT_CSS = """
    MessageQueueBar {
        height: auto;
    }
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__("", *args, **kwargs)
        self.items: list[dict[str, Any]] = []
        self.display = False

    def clear(self) -> None:
        self.set_items([])

    def set_items(self, items: Iterable[dict[str, Any]] | None) -> None:
        self.items = [self._normalize_item(item) for item in (items or []) if isinstance(item, dict)]
        self.display = bool(self.items)
        self.update(self._render_queue())

    def _normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        attachments = item.get("attachments", [])
        if not isinstance(attachments, list):
            attachments = []
        attachment_count = item.get("attachment_count", len(attachments))
        try:
            attachment_count = int(attachment_count)
        except (TypeError, ValueError):
            attachment_count = len(attachments)

        return {
            "id": str(item.get("id", "") or ""),
            "seq": int(item.get("seq", 0) or 0),
            "text": str(item.get("text", "") or ""),
            "attachments": [str(attachment) for attachment in attachments],
            "attachment_count": max(0, attachment_count),
        }

    def _render_queue(self) -> Text:
        text = Text()
        if not self.items:
            return text

        count = len(self.items)
        label = "message" if count == 1 else "messages"
        text.append(f"Queued {count} {label}", style="bold #d9e2ec")
        text.append("  ")
        text.append("/send", style="bold #00b4ff")
        text.append(" send all  ")
        text.append("/queue clear", style="bold #00b4ff")
        text.append(" clear")

        for index, item in enumerate(self.items[:_MAX_VISIBLE_ITEMS], start=1):
            text.append("\n")
            text.append(f"{index}. ", style="#7f8c98")
            body = item["text"].strip() or _EMPTY_ATTACHMENT_LABEL
            if len(body) > _MAX_ITEM_TEXT:
                body = body[: _MAX_ITEM_TEXT - 3].rstrip() + "..."
            text.append(body, style="#c9d3dd")
            attachment_count = int(item.get("attachment_count", 0) or 0)
            if attachment_count:
                noun = "file" if attachment_count == 1 else "files"
                text.append(f" [{attachment_count} {noun}]", style="#7f8c98")

        remaining = count - _MAX_VISIBLE_ITEMS
        if remaining > 0:
            text.append("\n")
            text.append(f"+ {remaining} more", style="#7f8c98")

        return text
