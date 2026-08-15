# TUI frontend file map

This note lists files that define the **Textual** terminal UI (layout, widgets, styles, and modal screens) for the Agentic Job CLI under `src/agentic_job_cli/`.

## IDE embedded terminal

When you run the CLI inside Cursor or VS Code, it appears in the **integrated terminal** at the bottom of the window. You get the same full-screen TUI as in an external terminal: chat log, multiline input (`Enter` sends, `Ctrl+J` inserts a newline, and `Ctrl+A` selects the full draft; placeholder shows normal help when idle; while the agent works it shows in-input progress text like the core WebUI), image attachment support through `Ctrl+V` for clipboard images or `/attach <image-path>`, top chat tabs, and a compact footer with shortcuts (for example `F3` local file access toggle, `F4` remote-exec toggle, `F6` Chats, `F7` Nudge, `F8` Pause or Resume, and `^P` Commands). In the local instance picker, use Up/Down to change the selected endpoint and Enter or Space to connect.

The interactive TUI opens a long chat at its newest 100 log entries. Scroll to the top (or press `Home`) to load the next older page; the full transcript remains available without mounting it all at once.

Clipboard images use Pillow's native reader on macOS and Windows. Linux uses
`wl-paste` from `wl-clipboard` on Wayland or `xclip` on X11; the Unix installer
prints the matching package commands when neither helper is available.

## Inline transcript images

The interactive transcript renders eligible images in place: Browser screenshots
appear directly beneath their existing Browser tool metadata, user attachments
under the user message, and assistant image metadata or Markdown under the
assistant message. A browser screenshot does not create an extra log entry.
Images open in the 96-by-32-cell expanded view. Click an image, or focus it and
press `Enter` or `Space`, to collapse it to the 36-by-12-cell thumbnail or
expand it again. Both preserve complete aspect ratio and retain their state
through transcript updates and resize.

The supported raster formats are PNG, JPEG, GIF (first frame), WebP, and BMP.
SVG, unavailable, unsupported, invalid, unauthenticated, and over-limit sources
display stable placeholders. Image loading is same-origin through the
authenticated `/api/image_get` route; the UI never fetches arbitrary remote
URLs. Its in-memory cache is bounded to 64 MiB. Up to four fetch/load tasks may
run concurrently, but only one full-resolution decoder runs at a time and it
downsamples before applying orientation and color conversion. Source limits are
25 MiB encoded data and 32 megapixels decoded.

Rendering is selected with `AJ_CLI_IMAGE_MODE=auto|tgp|sixel|halfcell|off`.
Automatic selection prefers supported TGP or Sixel and otherwise uses
half-cell; `off` retains text placeholders. The browser xterm.js preview and
SVG snapshots force half-cell, so they are useful for deterministic layout but
not native-protocol cleanup acceptance. Apple Terminal must not be assumed to
support TGP or Sixel: use auto/half-cell unless a compatible terminal has been
verified. Headless and gateway modes stay text/JSONL-only.

## Chat Tab Shortcuts

Focus the top chat tab strip with `Tab` first, then use:

| Shortcut | Action |
|----------|--------|
| Click a tab's `[×]` | Close/hide that visible tab without deleting the chat, when another tab remains. |
| `Tab`, then `n` | Create a new chat in a new tab. |
| `Tab`, then `x` | Close/hide the current tab without deleting the chat, when another tab remains. |
| `Tab`, then `Left` / `Right` | Move between visible chat tabs. |

## Files that are mainly “frontend”

| Path | Role |
|------|------|
| `src/agentic_job_cli/styles/app.tcss` | Global TUI styling (colors, borders, splash surface, `#chat-log`, `#message-input`, footer). |
| `src/agentic_job_cli/widgets/chat_input.py` | Multiline input (`Enter` to send, `Ctrl+J` for a new line, `Ctrl+A` to select the draft, grows up to a few lines; agent progress as placeholder inside the field when empty). |
| `src/agentic_job_cli/widgets/chat_log.py` | Selectable chat rows, expandable status/code details, cached Rich conversion, and paged older-history loading. |
| `src/agentic_job_cli/widgets/image_entry.py` | Inline image placeholder/loading/error state, focusable thumbnail/expanded rendering, and native-widget cleanup. |
| `src/agentic_job_cli/widgets/__init__.py` | Re-exports widgets (small; part of the UI package). |
| `src/agentic_job_cli/widgets/splash_view.py` | Staged connection surface for arrow-key local instance picking, single-instance auto-connect, manual URL fallback, login with detected-instance context, refreshed `Change URL` back-navigation, connecting/error states, and empty ready-state actions. |
| `src/agentic_job_cli/widgets/profile_menu_popover.py` | Current-profile menu for selecting, creating, or editing profiles. |
| `src/agentic_job_cli/screens/chat_list.py` | Chat list picker (TUI overlay). |
| `src/agentic_job_cli/screens/profile_editor.py` | Compact two-step profile editor for identity/instructions and tool choices. |
| `src/agentic_job_cli/screens/permissions.py` | Current-profile Tools, MCPs, and Skills permission editor. |

## Where UI meets logic

These are not “layout only,” but they drive or support what you see:

| Path | Role |
|------|------|
| `src/agentic_job_cli/app.py` | Main `App`: composes the main screen (`ChatLog`, `ChatInput`, `Footer`) and owns WebSocket handling, commands, and most state. |
| `src/agentic_job_cli/__main__.py` | Entry point that starts the app. |
| `src/agentic_job_cli/client.py` | HTTP/WebSocket client (no widgets). |
| `src/agentic_job_cli/config.py` | Configuration and env (no widgets). |
| `src/agentic_job_cli/media_refs.py` / `image_store.py` / `image_render.py` | Pure reference extraction, authenticated bounded image loading, and interactive renderer selection. |

## Tests

`tests/test_app.py` exercises application behavior (including UI-adjacent flows), not the `.tcss` file directly.
