"""The Dashboard top bar: window tabs and Help on the first screen line."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget

from booley.harness.colors import PALETTE_DODGER_BLUE

# (label, window name). Help is an action, not a window, so it has no name.
# A tuple, so later tickets can append items (such as Jobs filters) in one place.
ITEMS: tuple[tuple[str, str | None], ...] = (
    ("Sessions", "sessions"),
    ("Jobs", "jobs"),
    ("Help", None),
)
_SEPARATOR_BEFORE = "Help"


class TopBar(Widget, can_focus=True):
    """Window tabs and Help; the active window's tab is bold, the cursor item reversed."""

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
    #: Index into ITEMS under the cursor; only drawn while the bar has focus.
    cursor = reactive(0)

    class Selected(Message):
        """Enter was pressed on an item."""

        def __init__(self, window: str | None) -> None:
            super().__init__()
            self.window = window

    class Left(Message):
        """The user asked to return to the window content."""

    def on_focus(self) -> None:
        """Start on the active window's tab."""
        self.cursor = next(
            (index for index, (_, window) in enumerate(ITEMS) if window == self.active), 0
        )

    def action_move(self, step: int) -> None:
        self.cursor = max(0, min(len(ITEMS) - 1, self.cursor + step))

    def action_choose(self) -> None:
        _, window = ITEMS[self.cursor]
        self.post_message(self.Selected(window))

    def action_leave(self) -> None:
        self.post_message(self.Left())

    def render(self) -> Text:
        text = Text()
        for index, (label, window) in enumerate(ITEMS):
            if label == _SEPARATOR_BEFORE:
                text.append("│")
            style = f"bold color({PALETTE_DODGER_BLUE})" if window == self.active else ""
            if self.has_focus and index == self.cursor:
                style = f"{style} reverse".strip()
            text.append(f" {label} ", style=style)
        return text
