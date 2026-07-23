from __future__ import annotations

from textual import on
from textual.app import ScreenStackError
from textual.command import CommandPalette, DiscoveryHit, Hit, Provider
from textual.widgets import Button, Input

from agent_zero_cli import project_commands
from agent_zero_cli.project_utils import display_project_title, normalize_project_list, project_name


class OrderedSystemCommandsProvider(Provider):
    """Expose app system commands without Textual's default discovery sorting."""

    async def discover(self):
        await self.app._load_server_commands(force=True)
        for title, help_text, callback, discover in self.app.get_system_commands(self.screen):
            if discover:
                yield DiscoveryHit(title, callback, help=help_text)

    async def search(self, query: str):
        normalized = str(query or "").strip()
        if normalized.startswith("/"):
            await self.app._load_server_commands(force=normalized == "/")

        async for hit in self._search_skill_targets(query):
            yield hit

        async for hit in self._search_project_targets(query):
            yield hit

        async for hit in self._search_browser_targets(query):
            yield hit

        if normalized == "/":
            score = 1_000_000
            for title, help_text, callback, *_ in self.app.get_system_commands(self.screen):
                if title.startswith("/"):
                    yield Hit(score, title, callback, help=help_text)
                    score -= 1
            return

        matcher = self.matcher(query)
        for title, help_text, callback, *_ in self.app.get_system_commands(self.screen):
            if (match := matcher.match(title)) > 0:
                yield Hit(match, matcher.highlight(title), callback, help=help_text)

    async def _search_browser_targets(self, query: str):
        normalized = str(query or "").strip().lower()
        if normalized != "/browser":
            return

        for title, help_text, callback, *_ in self.app.get_system_commands(self.screen):
            if title.startswith("Browser: "):
                yield Hit(1_000_000, title, callback, help=help_text)

    async def _search_project_targets(self, query: str):
        token, _, project_query = query.partition(" ")
        if token.lower() not in {"/project", "/projects"} or not project_query.strip():
            return

        availability = self.app._project_availability()
        if not availability.available:
            return

        matcher = self.matcher(query)
        projects = normalize_project_list(getattr(self.app, "project_list", []))
        current_name = project_name(getattr(self.app, "current_project", None))
        for project in projects:
            name = project_name(project)
            if not name or name == current_name:
                continue

            title = display_project_title(project, default=name)
            label = f"/project {title}"
            if name != title:
                label = f"/project {title} ({name})"

            if (match := matcher.match(label)) <= 0:
                continue

            worker_name = f"palette-project-{name.replace('/', '-').replace(' ', '-')}"
            yield Hit(
                match,
                matcher.highlight(label),
                lambda name=name, worker_name=worker_name: self.app.run_worker(
                    project_commands.cmd_project(self.app, query=name),
                    exclusive=True,
                    name=worker_name,
                ),
                help=f"Switch to {title}.",
            )

    async def _search_skill_targets(self, query: str):
        normalized = str(query or "").strip()
        if not normalized.startswith("$"):
            return
        if not getattr(self.app, "_skills_available", lambda: False)():
            return

        try:
            skills = await self.app._load_skill_palette_skills()
        except Exception:
            return
        if not skills:
            return

        skill_query = normalized[1:].strip().casefold()
        matcher = self.matcher(normalized)
        score = 1_000_000

        for skill in skills:
            name = self.app._skill_display_name(skill)
            if not name:
                continue

            title = f"${name}"
            help_text = self.app._skill_help_text(skill)
            if not skill_query:
                match = score
                display_title = title
                score -= 1
            else:
                match = matcher.match(title)
                display_title = matcher.highlight(title) if match > 0 else title
                if match <= 0 and skill_query not in self.app._skill_search_text(skill):
                    continue
                if match <= 0:
                    match = len(skill_query)

            worker_name = f"palette-skill-{self.app._command_worker_slug(name)}"
            yield Hit(
                match,
                display_title,
                lambda skill=skill, worker_name=worker_name: self.app.run_worker(
                    self.app._activate_skill(skill),
                    exclusive=True,
                    name=worker_name,
                ),
                help=help_text,
            )


def is_raw_slash_command(value: str) -> bool:
    raw = str(value or "").strip()
    return raw.startswith("/") and bool(raw.split(maxsplit=1)[0].strip()) and " " in raw


def is_raw_skill_command(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw.startswith("$") or raw == "$":
        return False
    token = raw[1:].split(maxsplit=1)[0].strip()
    return bool(token) and token[0].isalpha()


class AgentCommandPalette(CommandPalette):
    """Command palette with slash-first styling and optional seeded query."""

    def __init__(
        self,
        *args,
        initial_query: str = "",
        from_slash: bool = False,
        from_skill: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._initial_query = initial_query
        self._from_slash = from_slash
        self._from_skill = from_skill

    DEFAULT_CSS = CommandPalette.DEFAULT_CSS + """
    AgentCommandPalette > Vertical {
        margin-top: 0;
        background: transparent;
    }

    AgentCommandPalette SearchIcon {
        display: none;
        width: 0;
        margin: 0;
    }

    AgentCommandPalette #--input {
        min-height: 1;
        border: none;
        padding: 0;
        margin: 0;
    }

    AgentCommandPalette #--results {
        margin-top: 0;
    }

    AgentCommandPalette CommandList {
        border: none;
        background: transparent;
        max-height: 12;
    }

    AgentCommandPalette CommandList > .option-list--option {
        padding: 0 1;
    }
    """

    def on_mount(self) -> None:
        if self._initial_query:
            self.call_after_refresh(self._apply_initial_query)

    def _apply_initial_query(self) -> None:
        input_widget = self.query_one(Input)
        input_widget.value = self._initial_query
        input_widget.action_end()

    @on(Input.Submitted)
    @on(Button.Pressed)
    def _select_or_command(self, event: Input.Submitted | Button.Pressed | None = None) -> None:
        if event is not None:
            event.stop()

        input_widget = self.query_one(Input)
        raw_command = input_widget.value.strip()
        if self._from_slash and is_raw_slash_command(raw_command):
            self._cancel_gather_commands()

            token = raw_command.split(maxsplit=1)[0].strip().lower().lstrip("/") or "command"
            worker_name = f"slash-{token.replace('/', '-')}"
            self._close_and_call_later(
                lambda: self.app._run_dispatch_command(raw_command, worker_name=worker_name)
            )
            return

        if self._from_skill and is_raw_skill_command(raw_command):
            self._cancel_gather_commands()

            token = raw_command[1:].split(maxsplit=1)[0].strip().lower() or "skill"
            worker_name = f"skill-{self.app._command_worker_slug(token)}"
            self._close_and_call_later(
                lambda: self.app._run_skill_command(raw_command, worker_name=worker_name)
            )
            return

        if event is None and self._selected_command is not None:
            self._cancel_gather_commands()
            self._close_and_call_later(self._selected_command.command)
            return

        super()._select_or_command(event)

    def _close_and_call_later(self, callback) -> None:
        self.app.post_message(CommandPalette.Closed(option_selected=True))
        self.app.delay_update()
        try:
            self.dismiss()
        except ScreenStackError:
            pass
        self.app.call_later(callback)
