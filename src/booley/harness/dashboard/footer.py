"""The Dashboard footer: only the keys that apply where the cursor is (#2244)."""

from __future__ import annotations

MOVE = "↑↓ move"
OPEN = "⏎ open"
WINDOWS = "←→ Sessions / Jobs"
FILTERS = "↑ filters"
BACK = "esc back"
TOP_BAR = "←→ choose · ⏎ apply · ↓ back to list"
SEPARATOR = " · "


def footer_hints(*, top_bar: bool, enter_opens: bool, jobs_list: bool, nested: bool) -> str:
    """The one footer line for the current cursor position.

    ``top_bar``: the top bar holds the keyboard. ``enter_opens``: Enter on the
    selected row opens something. ``jobs_list``: the Jobs window's list is
    shown. ``nested``: the window is inside a detail, so Esc goes back.
    """
    if top_bar:
        return TOP_BAR
    hints = [MOVE]
    if enter_opens:
        hints.append(OPEN)
    hints.append(WINDOWS)
    if jobs_list:
        hints.append(FILTERS)
    if nested:
        hints.append(BACK)
    return SEPARATOR.join(hints)
