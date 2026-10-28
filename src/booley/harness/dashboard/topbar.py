"""The Dashboard top bar: window tabs, Jobs filters and Help on the first screen line."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget

from booley.harness.colors import PALETTE_DODGER_BLUE

# (label, window name) for the window tabs. Help is an action, not a window.
ITEMS: tuple[tuple[str, str], ...] = (
    ("Sessions", "sessions"),
    ("Jobs", "jobs"),
)
# (label, filter name): the Jobs window's status filters, shown only while it is active.
JOB_FILTERS: tuple[tuple[str, str], ...] = (
    ("Running", "running"),
    ("Finished", "finished"),
    ("All", "all"),
)
# Item kinds posted in ``Selected``.
WINDOW, FILTER, OWNER, HELP_ITEM = "window", "filter", "owner", "help"


class TopBar(Widget, can_focus=True):
    """Window tabs, the Jobs filters and Help.

    The active window's tab and the active status filter are bold, the cursor
    item is reversed. The Jobs window adds the status filters and the owner
    chip between its tab and Help; the app tells the bar their state.
    """

    DEFAULT_CSS = "TopBar { height: 1; }"
    BINDINGS: ClassVar = [
        Binding("left", "move(-1)", show=False),
        Binding("right", "move(1)", show=False),
        Binding("enter", "choose", show=False),
        Binding("down", "leave", show=False),
        Binding("escape", "leave", show=False),
    ]

    #: Name of the window whose tab is highlighted.
    active = reactive("sessions")
    #: Index into the visible items under the cursor; only drawn while the bar has focus.
    cursor = reactive(0)
    #: Status filter of the Jobs window (a ``JOB_FILTERS`` name).
    job_filter = reactive("running")
    #: Label of the Jobs window's owner filter.
    owner = reactive("All sessions")
    #: Number of unfinished Jobs, shown on the Jobs tab; nothing when zero.
    unfinished = reactive(0)

    class Selected(Message):
        """Enter was pressed on an item: its kind and value (window name, filter name or None)."""

        def __init__(self, kind: str, value: str | None) -> None:
            super().__init__()
            self.kind = kind
            self.value = value

    class Left(Message):
        """The user asked to return to the window content."""

    def _items(self) -> list[tuple[str, str, str | None]]:
        """The visible items as (label, kind, value); the Jobs filters only in the Jobs window."""
        items: list[tuple[str, str, str | None]] = []
        for label, window in ITEMS:
            count = f" {self.unfinished}▶" if window == "jobs" and self.unfinished else ""
            items.append((label + count, WINDOW, window))
        if self.active == "jobs":
            items.extend((label, FILTER, name) for label, name in JOB_FILTERS)
            items.append((f"Owner: {self.owner}", OWNER, None))
        items.append(("Help", HELP_ITEM, None))
        return items

    def watch_active(self) -> None:
        """Leaving the Jobs window drops its items; keep the cursor on a visible one."""
        self.cursor = min(self.cursor, len(self._items()) - 1)

    def on_focus(self) -> None:
        """Start on the active window's tab."""
        self.cursor = next(
            (
                index
                for index, (_, kind, value) in enumerate(self._items())
                if kind == WINDOW and value == self.active
            ),
            0,
        )

    def action_move(self, step: int) -> None:
        self.cursor = max(0, min(len(self._items()) - 1, self.cursor + step))

    def action_choose(self) -> None:
        _, kind, value = self._items()[self.cursor]
        self.post_message(self.Selected(kind, value))

    def action_leave(self) -> None:
        self.post_message(self.Left())

    def render(self) -> Text:
        text = Text()
        previous_kind = ""
        for index, (label, kind, value) in enumerate(self._items()):
            # A divider opens the filter group and sets Help apart.
            if (kind == FILTER and previous_kind != FILTER) or kind == HELP_ITEM:
                text.append("│")
            previous_kind = kind
            highlighted = (kind == WINDOW and value == self.active) or (
                kind == FILTER and value == self.job_filter
            )
            style = f"bold color({PALETTE_DODGER_BLUE})" if highlighted else ""
            if self.has_focus and index == self.cursor:
                style = f"{style} reverse".strip()
            text.append(f" {label} ", style=style)
        return text
