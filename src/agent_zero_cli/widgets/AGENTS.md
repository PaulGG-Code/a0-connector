# Widgets DOX

## Purpose

- Own reusable Textual widgets used by the CLI shell: composer, chat log, splash view, command palette, footer, context tabs, status bars, banners, popovers, goal controls, and model/message queue controls.

## Ownership

- Files in this directory implement widget behavior and rendering only.
- App-level orchestration remains in `src/agent_zero_cli/app.py` and command modules.
- Visual constants that require layout styling belong in `src/agent_zero_cli/styles/app.tcss`.

## Local Contracts

- `ChatInput` is the single source for composer behavior: Enter submits, `Ctrl+J` inserts a newline, `Ctrl+A` selects the full draft, history is scoped by chat context, and content grows to four lines before internal scrolling.
- Discovered host rows use Up/Down to move the active selection and Enter or Space to connect.
- In-input activity must use the WebUI-style `|>  ` placeholder prefix, add the `progress-active` class, and escape detail text before putting it into Rich/Textual markup.
- `ChatInput.set_idle()` must clear activity state and restore the normal placeholder without losing attachment or queue placeholder state.
- Do not reintroduce `ActivityBar` or `#status-bar`. Activity belongs in `#message-input`.
- `ChatLog` status metadata must stay concise and must redact or summarize large/sensitive fields such as code, prompt text, stdout, stderr, markdown, HTML, and raw content.
- Transcript renderable caches must use A0-owned attribute names and must not
  shadow Textual's internal widget render cache.
- Footer/command palette behavior must not duplicate the command palette entry. The `ctrl+p` binding remains `show=False` in `app.py`.
- The compact model switcher bar shows the effective model pill without a `Main` role prefix. Its preset selector shows the effective preset and uses `Use preset from settings (<name>)` when a chat override can be cleared.

## Work Guidance

- Use Textual and Rich renderables instead of ad hoc ANSI strings when practical.
- Keep widget APIs small and mirrored in test fakes when `app.py` calls them.
- Avoid widget methods that perform network calls; pass state in from the app or command layer.
- Keep text and controls stable across narrow terminal/browser-preview widths.

## Verification

- `./.venv/bin/python -m pytest tests/test_chat_input.py tests/test_app.py tests/test_splash_view.py -v`

## Child DOX Index
