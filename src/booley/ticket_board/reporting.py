"""Step detail formatting, timing/usage reports, and board display."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .analytics import compute_step_cost
from .constants import (
    PRIORITY_ORDER,
    STEP_ORDER,
)
from .helpers import fmt_datetime_user, fmt_duration, fmt_tokens, parse_iso

# Import colors from harness; fall back to no-op if unavailable
try:
    from booley.harness.colors import (
        COLORS_ENABLED,
        bold,
        chrome,
        dim,
        green,
        hyperlink,
        in_vscode,
        len_visible,
        lilac,
        red,
        yellow,
    )
except ImportError:
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        from booley.harness.colors import (
            COLORS_ENABLED,
            bold,
            chrome,
            dim,
            green,
            hyperlink,
            in_vscode,
            len_visible,
            lilac,
            red,
            yellow,
        )
    except ImportError:

        def _identity(t: str) -> str:
            return t

        bold = dim = green = yellow = red = chrome = lilac = _identity  # type: ignore[assignment]
        COLORS_ENABLED = False

        def len_visible(t: str) -> int:  # type: ignore[misc]
            return len(t)

        def hyperlink(t: str, uri: str) -> str:  # type: ignore[misc]
            return t

        def in_vscode() -> bool:  # type: ignore[misc]
            return False


def _order_steps(step_dict):
    """Order stage keys by STEP_ORDER, then remaining stages."""
    ordered = [s for s in STEP_ORDER if s in step_dict]
    ordered.extend(s for s in step_dict if s not in ordered)
    return ordered


# ---------------------------------------------------------------------------
# Step detail formatting
# ---------------------------------------------------------------------------


def _detail_review(meta):
    """Format detail for rtl-review-1 / rtl-review-final steps."""
    found = meta.get("issues_found", 0)
    fixed = meta.get("issues_fixed", 0)
    if found == 0 and fixed == 0:
        return "clean"
    parts = [f"{found} found, {fixed} fixed"]
    severity_counts = {}
    for iss in meta.get("issues", []):
        sev = iss.get("severity", "?")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    if severity_counts:
        sev_parts = [f"{n} {s}" for s, n in severity_counts.items()]
        parts.append(f"({', '.join(sev_parts)})")
    return " ".join(parts)


def _detail_sim_debug(meta):
    """Format detail for sim-debug-loop step."""
    parts = []
    used, mx = meta.get("debug_rounds_used"), meta.get("debug_rounds_max")
    if used is not None and mx is not None:
        parts.append(f"{used}/{mx} debug rounds")
    total_cfg = meta.get("targets_passed", 0) + meta.get("configs_failed", 0)
    if total_cfg > 0:
        parts.append(f"{meta.get('targets_passed', 0)}/{total_cfg} configs pass")
    return ", ".join(parts)


def _detail_synthesis(meta):
    """Format detail for synthesis step."""
    targets = meta.get("targets", [])
    if not targets:
        return ""
    summaries = []
    for c in targets[:3]:
        name, delta, cells = c.get("name", "?"), c.get("delta_pct", ""), c.get("cells")
        if cells is not None and delta:
            summaries.append(f"{name}: {cells} ({delta})")
        elif delta:
            summaries.append(f"{name}: {delta}")
    return "; ".join(summaries)


def format_step_detail(step: str, meta: dict[str, Any]) -> str:
    """Format a concise detail string for a step from its metadata."""
    if not meta:
        return ""

    detail = ""
    if step == "planning":
        q = meta.get("clarifying_questions")
        if q is not None:
            detail = f"{q} clarifying question{'s' if q != 1 else ''}"
    elif step in ("rtl-review-1", "rtl-review-final"):
        detail = _detail_review(meta)
    elif step == "sim-debug-loop":
        detail = _detail_sim_debug(meta)
    elif step == "rtl-mutation-testing":
        injected = meta.get("mutations_injected", 0)
        detected = meta.get("mutations_detected", 0)
        rate = meta.get("detection_rate")
        if injected > 0:
            pct = f" ({rate:.0%})" if rate is not None else ""
            detail = f"{detected}/{injected} detected{pct}"
    elif step == "synthesis":
        detail = _detail_synthesis(meta)

    incidents = meta.get("incidents", 0)
    if incidents:
        incident_str = f"{incidents} incident{'s' if incidents != 1 else ''}"
        detail = f"{detail}, {incident_str}" if detail else incident_str

    return detail


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def _build_timing_header(has_tokens, has_meta):
    """Build markdown table header/separator for timing report."""
    hdr, sep = "| Step | Duration", "|-------|--------"
    if has_tokens:
        hdr += " | Tokens"
        sep += "-|-------:"
    if has_meta:
        hdr += " | Details"
        sep += "-|---------"
    return hdr + " |", sep + "-|"


def format_timing_report(
    durations: dict[str, float],
    title: str = "Step Timing Report",
    step_meta: dict[str, Any] | None = None,
) -> str:
    """Format per-step durations as a markdown report."""
    if not durations:
        return f"# {title}\n\nNo timing data available."

    has_meta = bool(step_meta)
    has_tokens = has_meta and any(step_meta.get(s, {}).get("tokens") for s in durations)
    ordered = _order_steps(durations)
    hdr, sep = _build_timing_header(has_tokens, has_meta)
    lines = [f"# {title}", "", hdr, sep]

    total_secs, total_tokens = 0, 0
    for step_name in ordered:
        secs = durations[step_name]
        total_secs += secs
        meta = step_meta.get(step_name, {}) if has_meta else {}
        row = f"| {step_name} | {fmt_duration(secs)}"
        if has_tokens:
            tok = meta.get("tokens", 0)
            total_tokens += tok
            row += f" | {fmt_tokens(tok)}" if tok else " | "
        if has_meta:
            row += f" | {format_step_detail(step_name, meta)}"
        lines.append(row + " |")

    total_row = f"| **Total** | **{fmt_duration(total_secs)}**"
    if has_tokens:
        total_row += f" | **{fmt_tokens(total_tokens)}**"
    if has_meta:
        total_row += " | "
    lines.extend([total_row + " |", ""])
    return "\n".join(lines)


def _usage_row(step_name, d, has_timing, step_durations):
    """Build a single usage report table row. Returns (row_str, dur_secs)."""
    cost = compute_step_cost(d)
    # Provider input totals already include cache read/create tokens. Cache
    # columns explain pricing; adding them again inflates the headline total.
    step_total = d["input_tokens"] + d["output_tokens"]
    dur_secs = 0
    row = f"| {step_name} | "
    if has_timing:
        if step_name in step_durations:
            dur_secs = step_durations[step_name]
            row += f"{fmt_duration(dur_secs)} | "
        else:
            row += " | "
    row += (
        f"{step_total:,} | {d['input_tokens']:,} | {d['output_tokens']:,} | "
        f"{d['cache_read_tokens']:,} | {d['cache_create_tokens']:,} | "
        f"{d['message_count']} | {cost:.2f} |"
    )
    return row, dur_secs


def format_usage_report(
    step_data: dict[str, Any],
    total_cost: float,
    title: str = "Token Usage Report",
    step_durations: dict[str, float] | None = None,
) -> str:
    """Format step token data as a markdown report."""
    lines = [f"# {title}", ""]
    has_timing = bool(step_durations)

    # Table header
    cols = "| Step | Duration | " if has_timing else "| Step | "
    cols += "Step Total | Input | Output | Cache Read | Cache Create | Msgs | ~USD |"
    sep_prefix = "|-------|----------|" if has_timing else "|-------|"
    sep = sep_prefix + "----------:|------:|-------:|-----------:|-------------:|-----:|-----:|"
    lines.extend([cols, sep])

    grand = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "message_count": 0,
    }
    total_secs = 0
    for step_name in _order_steps(step_data):
        row, dur_secs = _usage_row(
            step_name, step_data[step_name], has_timing, step_durations or {}
        )
        total_secs += dur_secs
        lines.append(row)
        for k in grand:
            grand[k] += step_data[step_name][k]

    grand_total = grand["input_tokens"] + grand["output_tokens"]
    total_row = "| **Total** | "
    if has_timing:
        total_row += f"**{fmt_duration(total_secs)}** | "
    total_row += (
        f"**{grand_total:,}** | **{grand['input_tokens']:,}** | **{grand['output_tokens']:,}** | "
        f"**{grand['cache_read_tokens']:,}** | **{grand['cache_create_tokens']:,}** | "
        f"**{grand['message_count']}** | **{total_cost:.2f}** |"
    )
    lines.extend(
        [
            total_row,
            "",
            f"*{grand_total:,} total tokens across {grand['message_count']} messages "
            f"(~${total_cost:.2f} at API rates)*",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ticket Board display
# ---------------------------------------------------------------------------


def _pad(text: str, width: int) -> str:
    """Pad *text* to *width* visible characters, respecting ANSI escapes."""
    visible = len_visible(text)
    if visible >= width:
        return text
    return text + " " * (width - visible)


def _colorize_status(status: str) -> str:
    """Return a colored status label (no emoji — pure text)."""
    _STATUS_COLOR = {
        "review": lilac,
        "running": green,
        "blocked": red,
        "waiting": yellow,
        "queued": dim,
        "done": green,
        "draft": chrome,
    }
    color_fn = _STATUS_COLOR.get(status, dim)
    return color_fn(status)


def _colorize_priority(prio: str) -> str:
    _PRIO_COLOR = {"high": red, "medium": yellow, "low": green}
    return _PRIO_COLOR.get(prio, dim)(prio)


def _colorize_criteria(criteria_str: str) -> str:
    """Color criteria progress: green if all met, yellow if partial, dim if none."""
    if criteria_str == "---":
        return dim(criteria_str)
    parts = criteria_str.split("/")
    if len(parts) == 2 and parts[0] == parts[1]:
        return green(criteria_str)
    return yellow(criteria_str)


def _build_board_rows(
    tickets: list[dict[str, Any]],
    tickets_dir: Path | None = None,
) -> tuple[list[tuple[str, ...]], list[str], dict[str, int]]:
    """Extract row tuples, per-row file URIs, and status counts from sorted tickets.

    Rows hold plain (uncolored, unlinked) text so column-width math stays
    simple; the link URI travels separately and is applied at print time.
    """
    rows: list[tuple[str, ...]] = []
    links: list[str] = []
    counts: dict[str, int] = {}
    # VS Code's terminal ignores OSC 8 file:// links (microsoft/vscode#176812)
    # but auto-links plain file paths. So there we render the basename as
    # plain text (VS Code resolves the unique slug by workspace search) and
    # skip the OSC 8 wrapper; every other modern terminal keeps the compact
    # OSC 8 name-link. Gated on COLORS_ENABLED so piped output stays plain.
    vscode_paths = COLORS_ENABLED and in_vscode()
    for t in tickets:
        ticket_file = t.get("file", "")
        ticket_name = Path(ticket_file).stem if ticket_file else "---"

        # file:// URI for the ticket .md — OSC 8-capable terminals make the
        # name Ctrl+Click-able to open the file.
        link = ""
        if tickets_dir is not None and ticket_file:
            ticket_path = (Path(tickets_dir) / ticket_file).resolve()
            if ticket_path.is_file():
                if vscode_paths:
                    # Plain basename, no OSC 8 — VS Code's own link detector
                    # opens it on Ctrl+Click.
                    ticket_name = ticket_path.name
                else:
                    link = ticket_path.as_uri()
        links.append(link)

        status = t.get("status", "queued")
        counts[status] = counts.get(status, 0) + 1

        step = t.get("step") or "---"
        endpoints_run = len(t.get("steps_completed", []))
        endpoints_str = str(endpoints_run) if endpoints_run else "---"

        updated = fmt_datetime_user(t.get("last_update", ""))
        error = t.get("error") or ""
        if len(error) > 40:
            error = error[:37] + "..."

        # Criteria progress from development state
        cr_total = t.get("criteria_total")
        if cr_total is not None and cr_total > 0:
            cr_passed = t.get("criteria_passed", 0)
            criteria_str = f"{cr_passed}/{cr_total}"
        else:
            criteria_str = "---"

        rows.append(
            (
                ticket_name,
                t.get("priority", "medium"),
                status,
                step,
                endpoints_str,
                updated,
                error,
                criteria_str,
            )
        )
    return rows, links, counts


_STATUS_ORDER = {
    "review": 0,
    "running": 1,
    "blocked": 2,
    "waiting": 3,
    "queued": 4,
    "done": 5,
    "archived": 6,
    "draft": 7,
}


def _sort_tickets(tickets):
    """Sort tickets by status priority, then priority, then last update."""
    return sorted(
        tickets,
        key=lambda t: (
            _STATUS_ORDER.get(t.get("status", "queued"), 9),
            PRIORITY_ORDER.get(t.get("priority", "medium"), 1),
            -(
                parse_iso(t.get("last_update", "1970-01-01T00:00:00")).timestamp()
                if t.get("last_update")
                else 0
            ),
        ),
    )


def _compute_col_widths(headers, rows):
    """Compute column widths from headers and row data. Returns (col_widths, show_error)."""
    n_width = max(2, len(str(len(rows))))
    col_widths = [n_width]
    for col_idx in range(len(headers) - 1):
        col_widths.append(max(len(headers[col_idx + 1]), *(len(r[col_idx]) for r in rows)))
    # Error column (index 7) is hidden if all errors are empty
    show_error = col_widths[7] > 0
    if not show_error:
        return headers[:7] + headers[8:], col_widths[:7] + col_widths[8:], show_error
    return headers, col_widths, show_error


def _print_board_row(i, row, col_widths, show_error, n_width, link=""):
    """Print a single colored board row; *link* makes the name clickable."""
    name, prio, status, stage, endpoints, updated, error, criteria = row
    cells = [
        _pad(dim(str(i)), n_width),
        _pad(hyperlink(bold(name), link), col_widths[1]),
        _pad(_colorize_priority(prio), col_widths[2]),
        _pad(_colorize_status(status), col_widths[3]),
        _pad(stage, col_widths[4]),
        _pad(endpoints, col_widths[5]),
        _pad(dim(updated) if updated else "", col_widths[6]),
    ]
    col_idx = 7
    if show_error:
        cells.append(_pad(red(error) if error else "", col_widths[col_idx]))
        col_idx += 1
    cells.append(_pad(_colorize_criteria(criteria), col_widths[col_idx]))
    print(f" {' │ '.join(cells)}")


def display_board(
    tickets: list[dict[str, Any]],
    tickets_dir: Path | None = None,
) -> None:
    """Display tickets as a fixed-width, ANSI-colored terminal table.

    When *tickets_dir* is given, ticket names become OSC 8 hyperlinks to
    their .md files (clickable in VS Code and other modern terminals).
    """
    if not tickets:
        print("Ticket board is empty -- no tickets tracked.")
        return

    tickets = _sort_tickets(tickets)
    rows, links, counts = _build_board_rows(tickets, tickets_dir)

    headers = (
        "#",
        "Ticket",
        "Priority",
        "Status",
        "Last Endpoint",
        "Endpoints",
        "Updated",
        "Error",
        "Criteria",
    )
    headers, col_widths, show_error = _compute_col_widths(headers, rows)

    hdr = " │ ".join(_pad(bold(h), w) for h, w in zip(headers, col_widths, strict=True))
    sep = "─┼─".join("─" * w for w in col_widths)
    print(f" {hdr}")
    print(f"─{sep}─")

    for i, (row, link) in enumerate(zip(rows, links, strict=True), 1):
        _print_board_row(i, row, col_widths, show_error, col_widths[0], link)

    print()
    parts = [
        f"{c} {s}"
        for s in ["draft", "queued", "waiting", "running", "blocked", "review", "done", "archived"]
        if (c := counts.get(s, 0)) > 0
    ]
    print(f" {bold(str(len(tickets)))} tickets: {', '.join(parts)}")
