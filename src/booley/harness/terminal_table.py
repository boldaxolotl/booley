"""Shared terminal table renderer for harness output.

One layout for every CLI table (``booley cheat``, ``booley targets``, ...):
a bold-accent header row, a chrome rule, then one line per row. Columns are
sized by *visible* width, so ANSI-styled cells align; padding goes outside the
style codes. All styling goes through :mod:`booley.harness.colors`, which
returns plain text when color is off (pipes, ``NO_COLOR``, ``TERM=dumb``), so
the plain layout equals the ANSI-stripped colored layout.
"""

from __future__ import annotations

import itertools
import shutil
import sys
from collections.abc import Mapping, Sequence

from .colors import bold_accent, char_width, chrome, len_visible, strip_ansi

_INDENT = "  "
_GAP = "   "
_ELLIPSIS = "…"
#: Columns assumed when a terminal reports no size.
_FALLBACK_WIDTH = 100


def width_budget() -> int | None:
    """Columns a line may use: the terminal's on a TTY, else ``None``.

    Piped output has no budget, so readers of a pipe (agents, scripts) get every
    cell in full.
    """
    if not sys.stdout.isatty():
        return None
    return shutil.get_terminal_size((_FALLBACK_WIDTH, 24)).columns


def _cap(cell: str, cap: int | None) -> str:
    """Cut *cell* to *cap* terminal cells, ending in an ellipsis.

    A cut cell loses its styling: it is cut on its ANSI-stripped text. The cut
    never leaves a wide character straddling the cap.
    """
    if cap is None or len_visible(cell) <= cap:
        return cell
    plain = strip_ansi(cell)
    kept = sum(1 for cells in itertools.accumulate(map(char_width, plain)) if cells <= cap - 1)
    return plain[:kept].rstrip() + _ELLIPSIS


def _fit(widths: Sequence[int], width: int) -> list[int]:
    """Shrink the widest columns to a shared limit so the table fits *width*.

    The limit is the largest one for which every column ``min(w, limit)`` fits
    the space left after the indent and gaps; narrower columns keep their
    width. A table that cannot fit even at a limit of 1 stays at 1.
    """
    room = width - len(_INDENT) - len(_GAP) * (len(widths) - 1)
    limit = max(widths)
    while limit > 1 and sum(min(w, limit) for w in widths) > room:
        limit -= 1
    return [min(w, limit) for w in widths]


def _join(cells: Sequence[str], widths: Sequence[int]) -> str:
    """Join *cells* into one line with no trailing whitespace.

    Trailing empty cells are dropped, then every kept cell but the last is
    padded to its column width by visible length.
    """
    kept = list(cells)
    while kept and len_visible(kept[-1]) == 0:
        kept.pop()
    padded = [
        cell + " " * (widths[i] - len_visible(cell)) if i < len(kept) - 1 else cell
        for i, cell in enumerate(kept)
    ]
    return (_INDENT + _GAP.join(padded)) if padded else ""


def render_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    max_widths: Mapping[int, int] | None = None,
    width: int | None = None,
) -> list[str]:
    """Render a column-aligned table as lines: header, rule, then one line per row.

    ``lines[2 + i]`` is always ``rows[i]``, so callers can interleave their own
    lines (group headings) by index. A row shorter than ``headers`` is padded
    with empty cells; extra cells are ignored. ``max_widths`` maps a column
    index to a cap on its visible width: a longer cell is cut and ends in
    ``…`` (cells never wrap). Raises ``ValueError`` for a cap below 1.
    ``width`` is the budget for a whole line, indent included: a table wider
    than it shrinks its widest columns (cut cells end in ``…``) until it fits,
    or as far as one cell per column. ``None`` leaves the table unbounded.

    Widths count terminal cells, so double-width CJK/emoji align. Cell
    preconditions (callers own them; the renderer does not sanitize):
    each cell is a single line of text (no ``\n`` or ``\t``) whose styling is
    self-contained, i.e. every opened style code is closed inside the cell,
    because padding is appended after the cell.
    """
    caps = dict(max_widths or {})
    if any(cap < 1 for cap in caps.values()):
        raise ValueError("max_widths caps must be at least 1")
    ncols = len(headers)
    body = [
        [_cap(row[i] if i < len(row) else "", caps.get(i)) for i in range(ncols)] for row in rows
    ]
    widths = [
        max([len_visible(header), *(len_visible(row[i]) for row in body)])
        for i, header in enumerate(headers)
    ]
    if width is not None:
        fitted = _fit(widths, width)
        headers = [_cap(h, w) for h, w in zip(headers, fitted, strict=True)]
        body = [[_cap(cell, w) for cell, w in zip(row, fitted, strict=True)] for row in body]
        widths = fitted
    lines = [
        _join([bold_accent(h) for h in headers], widths),
        _INDENT + chrome(_GAP.join("─" * w for w in widths)),
    ]
    lines.extend(_join(row, widths) for row in body)
    return lines
