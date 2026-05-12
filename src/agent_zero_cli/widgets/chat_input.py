"""Multi-line chat input widget that grows up to 4 lines."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from textual import events
from textual.message import Message
from textual.widgets import TextArea
from textual.widgets.text_area import TextAreaTheme

from agent_zero_cli.attachments import AttachmentRef, attachment_label


_PLACEHOLDER = "Type a message... (/help for commands)"
_PROGRESS_CLASS = "progress-active"
# Same prefix as Agent Zero WebUI composer (see webui/components/chat/input/input-store.js).
_PROGRESS_PREFIX = "|>  "
_DEFAULT_HISTORY_SCOPE = "__default__"
_MAX_HISTORY_ITEMS = 50

# Minimal theme so the input blends with the app style.
_INPUT_THEME = TextAreaTheme(
    name="chat_input",
    syntax_styles={},
)

_MAX_CONTENT_LINES = 4


class ChatInput(TextArea):
    """A multi-line text input that auto-grows up to 4 lines.

    * **Enter** submits the message.
    * **Ctrl+J** inserts a newline (`Shift+Enter` also works in terminals
      that report it distinctly).
    * Scrolls internally when content exceeds 4 lines.
    * While the agent is busy, progress appears as placeholder text inside the
      input (when it is empty), matching the core WebUI behavior.
    """

    @dataclass
    class Submitted(Message):
        """Posted when the user presses Enter to submit."""

        value: str
        input: ChatInput
        attachments: list[AttachmentRef] = field(default_factory=list)

    @dataclass
    class ValueChanged(Message):
        """Posted when the text content changes."""

        value: str
        input: ChatInput

    DEFAULT_CSS = """
    ChatInput {
        height: auto;
        min-height: 3;
        max-height: 6;
    }
    """

    def __init__(
        self,
        *,
        placeholder: str = _PLACEHOLDER,
        id: str | None = None,
    ) -> None:
        super().__init__(
            "",
            language=None,
            theme="css",
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
            id=id,
            placeholder=placeholder,
        )
        self._base_placeholder = placeholder
        self._activity_active = False
        self._activity_label = ""
        self._activity_detail = ""
        self.attachments: list[AttachmentRef] = []
        self._history_scope = _DEFAULT_HISTORY_SCOPE
        self._history_by_scope: dict[str, list[str]] = {_DEFAULT_HISTORY_SCOPE: []}
        self._history_index: int | None = None
        self._history_draft = ""

    def on_mount(self) -> None:
        self.register_theme(_INPUT_THEME)
        self.theme = "chat_input"
        self._update_height()

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, new: str) -> None:
        self.clear()
        if new:
            self.insert(new)
        self._update_height()

    # ---- key handling ------------------------------------------------

    async def _on_key(self, event: events.Key) -> None:
        if event.key in {"ctrl+v", "cmd+v"}:
            attach_clipboard_image = getattr(self.app, "attach_clipboard_image", None)
            if attach_clipboard_image is not None and await attach_clipboard_image():
                event.prevent_default()
                event.stop()
                return

        if self._is_newline_key(event):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            self._update_height()
            return

        if event.key == "up" and self._history_previous():
            event.prevent_default()
            event.stop()
            return

        if event.key == "down" and self._history_next():
            event.prevent_default()
            event.stop()
            return

        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = self.text
            attachments = list(self.attachments)
            self._push_history(text)
            self.clear()
            self.clear_attachments()
            self._update_height()
            self.post_message(self.Submitted(value=text, input=self, attachments=attachments))
            return

        if event.key == "backspace" and not self.text and self.attachments:
            event.prevent_default()
            event.stop()
            self.remove_attachment(-1)
            return

    def _on_text_area_changed(self, _event: TextArea.Changed) -> None:
        self._update_height()
        self._sync_progress_placeholder()
        self.post_message(self.ValueChanged(value=self.text, input=self))

    # ---- in-input progress (WebUI-style) ----------------------------

    def _compose_activity_placeholder(self) -> str:
        detail = f" [{self._activity_detail}]" if self._activity_detail else ""
        return f"{_PROGRESS_PREFIX}{self._activity_label}{detail}"

    def _compose_placeholder(self) -> str:
        prefix = f"{attachment_label(len(self.attachments))} " if self.attachments else ""
        if self._activity_active:
            return prefix + self._compose_activity_placeholder()
        return prefix + self._base_placeholder

    def _sync_progress_placeholder(self) -> None:
        if self.text:
            return
        self.placeholder = self._compose_placeholder()

    def set_activity(self, label: str, detail: str = "") -> None:
        """Show progress as the placeholder when the field is empty."""
        self._activity_label = label
        self._activity_detail = detail
        self._activity_active = True
        self.add_class(_PROGRESS_CLASS)
        self._sync_progress_placeholder()

    def set_idle(self) -> None:
        """Clear progress state and restore the normal placeholder."""
        self._activity_active = False
        self._activity_label = ""
        self._activity_detail = ""
        self.remove_class(_PROGRESS_CLASS)
        self.placeholder = self._compose_placeholder()

    # ---- attachments -------------------------------------------------

    def add_attachment(self, attachment: AttachmentRef) -> None:
        if any(existing.path == attachment.path for existing in self.attachments):
            return
        self.attachments.append(attachment)
        self._sync_progress_placeholder()

    def remove_attachment(self, index: int) -> None:
        if not self.attachments:
            return
        self.attachments.pop(index)
        self._sync_progress_placeholder()

    def clear_attachments(self) -> None:
        self.attachments = []
        self._sync_progress_placeholder()

    def set_attachments(self, attachments: list[AttachmentRef]) -> None:
        self.attachments = list(attachments)
        self._sync_progress_placeholder()

    # ---- input history ----------------------------------------------

    def set_history_context(self, context_id: str | None) -> None:
        """Switch the active history scope to match the current chat."""
        scope = context_id.strip() if context_id else _DEFAULT_HISTORY_SCOPE
        if scope == self._history_scope:
            return
        self._history_scope = scope
        self._history_by_scope.setdefault(scope, [])
        self._history_index = None
        self._history_draft = ""

    def _history(self) -> list[str]:
        return self._history_by_scope.setdefault(self._history_scope, [])

    def seed_history(self, values: Iterable[str]) -> None:
        """Append known user messages to the current history scope."""
        for value in values:
            self._push_history(value)

    def _push_history(self, text: str) -> None:
        trimmed = text.strip()
        if not trimmed:
            return

        history = self._history()
        if history and history[-1] == trimmed:
            self._history_index = None
            self._history_draft = ""
            return

        history.append(trimmed)
        if len(history) > _MAX_HISTORY_ITEMS:
            del history[:-_MAX_HISTORY_ITEMS]
        self._history_index = None
        self._history_draft = ""

    def _is_newline_key(self, event: events.Key) -> bool:
        aliases = {event.key, *event.aliases}
        return bool({"shift+enter", "ctrl+j", "newline", "alt+enter"} & aliases)

    def _selection_is_collapsed_at(self, location: tuple[int, int]) -> bool:
        start, end = self.selection
        return start == end == location

    def _document_end(self) -> tuple[int, int]:
        last_row = max(0, self.document.line_count - 1)
        return last_row, len(self.document[last_row])

    def _set_history_value(self, value: str, *, cursor: str) -> None:
        self.value = value
        if cursor == "start":
            self.move_cursor((0, 0))
        else:
            self.move_cursor(self._document_end())
        self._update_height()

    def _history_previous(self) -> bool:
        if not self._selection_is_collapsed_at((0, 0)):
            return False

        history = self._history()
        if not history:
            return True

        if self._history_index is None:
            self._history_draft = self.text
            self._history_index = len(history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return True

        self._set_history_value(history[self._history_index], cursor="start")
        return True

    def _history_next(self) -> bool:
        if not self._selection_is_collapsed_at(self._document_end()):
            return False

        history = self._history()
        if self._history_index is None:
            return True

        if self._history_index < len(history) - 1:
            self._history_index += 1
            self._set_history_value(history[self._history_index], cursor="end")
        else:
            self._history_index = None
            self._set_history_value(self._history_draft, cursor="end")
            self._history_draft = ""
        return True

    # ---- dynamic height ---------------------------------------------

    def _update_height(self) -> None:
        line_count = (
            self.wrapped_document.height
            if self.soft_wrap and self.wrap_width
            else self.document.line_count
        )
        visible = max(1, min(line_count, _MAX_CONTENT_LINES))
        new_h = visible + 2  # +2 for border
        self.styles.height = new_h

    def _on_resize(self) -> None:
        super()._on_resize()
        self._update_height()

    # ---- disabled state ----------------------------------------------

    def watch_disabled(self, disabled: bool) -> None:
        self.read_only = disabled
