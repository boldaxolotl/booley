"""Render a subset of Markdown to ANSI terminal output.

Handles: headers (##/###/####), tables, bold, backticks, bullet lists.
No external dependencies for ANSI mode.

Rich-mode rendering (``inline_rich``) emits Rich ``Text`` objects with
click metadata on resolved backticked path/ticket tokens. The Console
uses this; log mode keeps the ANSI string path unchanged.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .colors import accent, bold, bold_accent, dim, len_visible

if TYPE_CHECKING:
    from rich.text import Text

    from .console.links import LinkContext

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"\*([^*]+)\*")
_BACKTICK_RE = re.compile(r"`([^`]+)`")

# Ticket-link sentinel: a backticked token of the form ``ticket:<slug>``
# renders as the slug (no ``ticket:`` prefix) but resolves to the ticket
# board at click time. See ADR 0010 — resolution must be deferred because
# the ticket may change board status mid-run.
_TICKET_PREFIX = "ticket:"

# Inline ``path:line[:col]`` capture inside an already-backticked token.
# Used by the Rich renderer to peel a line number off the link target.
_BACKTICK_PATH_LINE_RE = re.compile(
    r"^(?P<path>[^:`]+?):(?P<line>\d+)(?::\d+)?$",
)


def _inline(text: str) -> str:
    """Apply inline formatting: **bold**, *italic*, `code`."""
    text = _BOLD_RE.sub(lambda m: bold(m.group(1)), text)
    text = _ITALIC_RE.sub(lambda m: dim(m.group(1)), text)
    text = _BACKTICK_RE.sub(lambda m: accent(m.group(1)), text)
    return text


# ---------------------------------------------------------------------------
# Rich-mode rendering (Console clickable links)
# ---------------------------------------------------------------------------


def inline_rich(text: str, link_ctx: LinkContext | None = None) -> Text:
    """Inline-format *text* as a Rich ``Text`` object.

    When ``link_ctx`` is provided, backticked tokens that resolve to a
    file (or to ``ticket:<slug>``) become styled spans with click meta:
    ``{"@click": "open_booley_link", "booley_target": LinkTarget(...)}``.
    Spans that don't resolve fall back to plain ``accent`` styling (same
    as the ANSI renderer) so non-path backticks like ``clk`` still look
    like inline code.

    Bold and italic ranges are styled but never clickable.
    """
    from rich.text import Text

    out = Text()
    spans = _split_inline(text)
    for kind, payload in spans:
        if kind == "plain":
            out.append(payload)
        elif kind == "bold":
            out.append(payload, style="bold")
        elif kind == "italic":
            out.append(payload, style="dim")
        elif kind == "backtick":
            _append_backtick_span(out, payload, link_ctx)
    return out


def _split_inline(text: str) -> list[tuple[str, str]]:
    """Split *text* into a list of (kind, payload) spans.

    Backticks bind tightest; then bold; then italic. Unmatched markup
    characters fall through as plain text so user-typed text with stray
    ``*`` characters isn't mangled.
    """
    # Walk the string left-to-right, taking the earliest of the three
    # markers. This keeps the precedence simple without a real parser.
    out: list[tuple[str, str]] = []
    pos = 0
    n = len(text)
    while pos < n:
        m_back = _BACKTICK_RE.search(text, pos)
        m_bold = _BOLD_RE.search(text, pos)
        m_ital = _ITALIC_RE.search(text, pos)
        candidates = [
            (m.start(), kind, m)
            for m, kind in (
                (m_back, "backtick"),
                (m_bold, "bold"),
                (m_ital, "italic"),
            )
            if m is not None
        ]
        if not candidates:
            out.append(("plain", text[pos:]))
            break
        candidates.sort(key=lambda c: c[0])
        start, kind, m = candidates[0]
        if start > pos:
            out.append(("plain", text[pos:start]))
        out.append((kind, m.group(1)))
        pos = m.end()
    return out


def _append_backtick_span(
    out: Text,
    payload: str,
    link_ctx: LinkContext | None,
) -> None:
    """Append a backticked token to *out*, with click meta when resolvable."""
    from rich.style import Style

    if link_ctx is None:
        out.append(payload, style="cyan")
        return

    # Ticket sentinel: ``ticket:<slug>`` renders as the slug and resolves
    # to the board at click time. See ADR 0010 for why resolution must
    # be deferred (status moves during a run).
    if payload.startswith(_TICKET_PREFIX):
        slug = payload[len(_TICKET_PREFIX) :]
        target = _make_link_target(kind="ticket", raw=slug, line=None)
        out.append(
            slug,
            style=Style(
                color="cyan",
                underline=True,
                meta={"@click": "open_booley_link", "booley_target": target},
            ),
        )
        return

    # File link: peel off a ``:42`` suffix if present, then ask the
    # resolver whether this looks like a real path under one of the roots.
    m = _BACKTICK_PATH_LINE_RE.match(payload)
    if m:
        path = m.group("path")
        line_num: int | None = int(m.group("line"))
    else:
        path = payload
        line_num = None

    if link_ctx.resolves_under_roots(path):
        target = _make_link_target(kind="file", raw=path, line=line_num)
        out.append(
            payload,
            style=Style(
                color="cyan",
                underline=True,
                meta={"@click": "open_booley_link", "booley_target": target},
            ),
        )
        return

    # Not resolvable — fall back to plain inline-code styling. The user
    # still sees the backticks were applied; clicks do nothing.
    out.append(payload, style="cyan")


def _make_link_target(*, kind: str, raw: str, line: int | None):
    """Build a LinkTarget. Late-imported to keep render_md import-light."""
    from .console.links import LinkTarget

    return LinkTarget(kind=kind, raw=raw, line=line)  # type: ignore[arg-type]


def _is_separator(row: str) -> bool:
    """Check if a table row is a separator (|---|---|)."""
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    return all(re.fullmatch(r":?-+:?", c) for c in cells)


def _parse_table_row(row: str) -> list[str]:
    """Split a pipe-delimited table row into cells."""
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _render_table(rows: list[str]) -> list[str]:
    """Render markdown table rows as column-aligned text."""
    header = _parse_table_row(rows[0])
    data_rows = [_parse_table_row(r) for r in rows[2:]]  # skip separator

    # Compute column widths from raw text (no ANSI)
    ncols = len(header)
    widths = [len(h) for h in header]
    for row in data_rows:
        for i, cell in enumerate(row[:ncols]):
            clean = _BOLD_RE.sub(r"\1", cell)
            clean = _ITALIC_RE.sub(r"\1", clean)
            clean = _BACKTICK_RE.sub(r"\1", clean)
            widths[i] = max(widths[i], len(clean))

    out: list[str] = []

    # Header
    styled_header = [bold_accent(h.ljust(widths[i])) for i, h in enumerate(header)]
    out.append("  " + "   ".join(styled_header))

    # Separator
    out.append("  " + "   ".join("─" * w for w in widths))

    # Data rows
    for row in data_rows:
        cells: list[str] = []
        for i in range(ncols):
            raw = row[i] if i < len(row) else ""
            styled = _inline(raw)
            pad = widths[i] - len_visible(styled)
            cells.append(styled + " " * max(0, pad))
        out.append("  " + "   ".join(cells))

    return out


def render(markdown: str) -> str:
    """Render markdown string to ANSI-formatted terminal text."""
    lines = markdown.splitlines()
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Table: collect consecutive pipe-rows
        if line.strip().startswith("|"):
            table_rows: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_rows.append(lines[i])
                i += 1
            if len(table_rows) >= 2 and _is_separator(table_rows[1]):
                out.extend(_render_table(table_rows))
            else:
                out.extend(_inline(r) for r in table_rows)
            continue

        # Headers
        if line.startswith("#### "):
            out.append(bold(line[5:]))
        elif line.startswith("### "):
            out.append("")
            out.append(bold_accent(line[4:]))
            out.append("")
        elif line.startswith("## "):
            out.append(bold_accent(line[3:]))
            out.append("─" * len(line[3:]))
        # Bullet points
        elif line.strip().startswith("- "):
            indent = len(line) - len(line.lstrip())
            out.append(" " * indent + "  • " + _inline(line.strip()[2:]))
        # Regular text
        else:
            out.append(_inline(line))

        i += 1

    return "\n".join(out) + "\n"
