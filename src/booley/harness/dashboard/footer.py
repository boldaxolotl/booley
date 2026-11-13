"""The Dashboard footer: only the keys that apply where the cursor is (#2244)."""

from __future__ import annotations

from typing import Literal

# What Down returns to from the top bar.
BackTarget = Literal["list", "detail", "Help"]

MOVE = "↑↓ move"
OPEN = "⏎ open"
WINDOWS = "←→ Sessions / Jobs / Targets"
FILTERS = "↑ filters"
WORKTREE = "↑ worktree"
BACK = "esc back"
TOP_BAR_KEYS = "←→ choose · ⏎ apply"
RUN_KEYS = "←→ choose · ⏎ run a Flow"
SCROLL = "↑↓ PgUp/PgDn scroll"
FLOWS_BELOW = "↓ Flows to run"
RUN = "⏎ run"
SEPARATOR = " · "


def footer_hints(
    *,
    top_bar: bool,
    enter_opens: bool,
    jobs_list: bool,
    targets_list: bool,
    nested: bool,
    scrolling: bool,
    back_to: BackTarget,
    on_run: bool,
    flows_below: bool = False,
    run_list: bool = False,
) -> str:
    """The one footer line for the current cursor position.

    ``top_bar``: the top bar holds the keyboard. ``enter_opens``: Enter on the
    selected row opens something. ``jobs_list``: the Jobs window's list is
    shown. ``targets_list``: the Targets window's list is shown. ``nested``: the window is
    inside a detail, so Esc goes back.
    ``scrolling``: the side panel holds the keyboard, so arrows scroll it and
    Enter does nothing. ``back_to``: what Down returns to from the top bar
    ("list", "detail" or "Help"). ``on_run``: the top bar's cursor is on Run.
    ``flows_below``: the detail ends with a list of Flows, which Down reaches after the last
    line. ``run_list``: that list holds the keyboard, so Enter runs the Flow.
    """
    if top_bar:
        return f"{RUN_KEYS if on_run else TOP_BAR_KEYS}{SEPARATOR}↓ back to {back_to}"
    if run_list:
        hints = [MOVE, RUN, WINDOWS]
        if nested:
            hints.append(BACK)
        return SEPARATOR.join(hints)
    if scrolling:
        hints = [SCROLL, FLOWS_BELOW, WINDOWS] if flows_below else [SCROLL, WINDOWS]
        if nested:
            hints.append(BACK)
        return SEPARATOR.join(hints)
    hints = [MOVE]
    if enter_opens:
        hints.append(OPEN)
    hints.append(WINDOWS)
    if jobs_list:
        hints.append(FILTERS)
    if targets_list:
        hints.append(WORKTREE)
    if nested:
        hints.append(BACK)
    return SEPARATOR.join(hints)
