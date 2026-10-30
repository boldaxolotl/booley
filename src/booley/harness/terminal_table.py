"""Shared terminal table renderer for harness output.

One layout for every CLI table (``booley cheat``, ``booley targets``, ...):
a bold-accent header row, a chrome rule, then one line per row. Columns are
sized by *visible* width, so ANSI-styled cells align; padding goes outside the
style codes. All styling goes through :mod:`booley.harness.colors`, which
returns plain text when color is off (pipes, ``NO_COLOR``, ``TERM=dumb``), so
the plain layout equals the ANSI-stripped colored layout.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .colors import bold_accent, chrome, len_visible, strip_ansi

_INDENT = "  "
_GAP = "   "
_ELLIPSIS = "…"


def _cap(cell: str, cap: int | None) -> str:
    """Cut *cell* to *cap* visible characters, ending in an ellipsis.

    A cut cell loses its styling: it is cut on its ANSI-stripped text.
    """
    if cap is None or len_visible(cell) <= cap:
        return cell
    return strip_ansi(cell)[: cap - 1].rstrip() + _ELLIPSIS


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
) -> list[str]:
    """Render a column-aligned table as lines: header, rule, then one line per row.

    ``lines[2 + i]`` is always ``rows[i]``, so callers can interleave their own
    lines (group headings) by index. A row shorter than ``headers`` is padded
    with empty cells; extra cells are ignored. ``max_widths`` maps a column
    index to a cap on its visible width: a longer cell is cut and ends in
    ``…`` (cells never wrap). Raises ``ValueError`` for a cap below 1.
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
    lines = [
        _join([bold_accent(h) for h in headers], widths),
        _INDENT + chrome(_GAP.join("─" * w for w in widths)),
    ]
    lines.extend(_join(row, widths) for row in body)
    return lines
