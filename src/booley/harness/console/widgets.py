"""Console widgets — MainPane, TicketHeader, TopStrip, BottomStrip, StatusBar."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

# Hardware-domain metric formatters moved to criteria_format (principle 8 —
# Single Responsibility). Imported here so MainPane can render metric strings,
# and re-exported for backward compatibility with existing import sites.
from .criteria_format import (  # noqa: F401 — re-exported for backward compatibility
    _COVERAGE_KEYS,
    _format_coverage_metric,
    _format_fpga_impl_metric,
    _format_metric,
    _format_synthesis_metric,
)

if TYPE_CHECKING:
    from booley.config.editor import ResolvedEditor

    from .links import LinkContext

logger = logging.getLogger(__name__)

_ENTRY_STYLES = {0: ("✓", "green"), 1: ("✗", "yellow"), 2: ("!", "bold red")}

_ENDPOINT_STYLES: dict[str, str] = {
    "tb_coder": "color(114)",
    "reviewer": "color(183)",
    "coverage_analyst": "color(183)",
    "mutation_tester": "color(183)",
    "sim": "color(75)",
    "lint": "color(75)",
    "synth": "color(75)",
    "submit_run_report": "color(75)",
}
_DEFAULT_ENDPOINT_STYLE = "color(249)"

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

_GROUP_PREFIXES: list[tuple[str, str]] = [
    ("coverage_", "coverage"),
    ("review_", "reviews"),
    ("mutation_", "mutation"),
]


@dataclass
class McpToolCompletionMark:
    # Logical line indices (0-based newline counts) into the MainPane's content.
    # These are resolved to *visual* rows at strip-render time via the current
    # pane width, since rendered wrapping is not knowable at append time.
    start_line: int  # logical line of the opening "┌─" banner
    end_line: int  # logical line of the closing "└─" banner
    name: str
    target: str | None
    exit_code: int
    duration_s: float
    cost_usd: float
    summary: str


def _render_entry_line(mark: McpToolCompletionMark) -> Text:
    icon_char, icon_style = _ENTRY_STYLES.get(mark.exit_code, ("!", "bold red"))
    dur = f"{mark.duration_s:.0f}s" if mark.duration_s < 60 else f"{mark.duration_s / 60:.1f}m"
    cost = f" ${mark.cost_usd:.2f}" if mark.cost_usd else ""
    target_str = f" [{mark.target}]" if mark.target else ""
    summary_str = f" — {mark.summary}" if mark.summary else ""
    name_style = _ENDPOINT_STYLES.get(mark.name, _DEFAULT_ENDPOINT_STYLE)
    line = Text()
    line.append(icon_char, style=icon_style)
    line.append(f" {mark.name}{target_str}", style=name_style)
    line.append(f" {dur}{cost}{summary_str}")
    return line


def _group_of(key: str) -> str | None:
    for prefix, name in _GROUP_PREFIXES:
        if key.startswith(prefix):
            return name
    return None


_CriterionStatus = Literal["met", "failing", "needs_recheck", "not_run"]


def _criterion_status(entry: dict) -> _CriterionStatus:
    """Classify a criterion by its current verdict and evidence freshness."""
    if entry.get("stale"):
        return "needs_recheck"
    if entry.get("met"):
        return "met"
    if entry.get("detail") or entry.get("ever_failed") or entry.get("ever_met"):
        return "failing"
    return "not_run"


def _is_never_evaluated(entry: dict) -> bool:
    return _criterion_status(entry) == "not_run"


def _truncate_name(name: str, max_len: int = 28) -> str:
    if len(name) <= max_len:
        return name
    return name[: max_len - 1] + "…"


def _format_token_count(tokens: int) -> str:
    """Compact token count: 812, 46k, 1.1m."""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}m"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.0f}k"
    return str(tokens)


# ---------------------------------------------------------------------------
# MainPane — accumulating scrollable log
# ---------------------------------------------------------------------------


class ScrollPositionChanged(Message):
    """Posted when MainPane scroll position changes."""

    pass


class MainPane(VerticalScroll):
    """Scrollable container with accumulating log content."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._content: Text = Text()
        self._endpoint_style: str = _DEFAULT_ENDPOINT_STYLE
        self._endpoint_completions: list[McpToolCompletionMark] = []
        self._visual_span_cache: tuple[Content, int, list[tuple[int, int]]] | None = None
        self._auto_scroll: bool = True
        self._endpoint_open_line: int | None = None
        # Track the setup -> developer boundary so we draw a divider exactly
        # once, the first time non-setup content lands.
        self._had_setup: bool = False
        self._setup_divider_drawn: bool = False
        # Click-link wiring — attached by the app at startup; None in tests
        # and log mode so the legacy plain-text path stays untouched.
        self._link_ctx: LinkContext | None = None
        self._editor: ResolvedEditor | None = None

    def set_link_context(
        self,
        link_ctx: LinkContext | None,
        editor: ResolvedEditor | None,
    ) -> None:
        """Attach the click resolver context after composition.

        Called by ConsoleApp.on_mount once both the link context and
        editor config have been built. Subsequent ``append_*`` calls
        emit clickable spans for resolvable backticked tokens.
        """
        self._link_ctx = link_ctx
        self._editor = editor

    def compose(self) -> ComposeResult:
        yield Static("", id="main-content")

    def _maybe_scroll(self) -> None:
        if self._auto_scroll:
            self.scroll_end(animate=False)

    def append_setup_line(self, text: str) -> None:
        if self._content.plain:
            self._content.append("\n")
        self._content.append(text, style="dim italic")
        self._had_setup = True
        self.query_one("#main-content", Static).update(self._content)
        self._maybe_scroll()

    def _emit_setup_divider(self) -> None:
        """Draw a one-shot horizontal rule between setup output and the first
        developer/endpoint/thinking line so the two phases don't visually merge."""
        if not self._had_setup or self._setup_divider_drawn:
            return
        width = self._box_width()
        self._content.append("\n\n")
        self._content.append("─" * width, style="dim")
        self._setup_divider_drawn = True

    def _make_clickable(self, line: str, *, base_style: str = "") -> Text:
        """Wrap raw paths in backticks, then render with click spans.

        Returns a Rich ``Text`` carrying ``meta`` on resolvable spans.
        When no link context is attached (tests, log-mode fallback), the
        result is a plain ``Text`` with ``base_style`` applied.
        """
        if self._link_ctx is None:
            return Text(line, style=base_style) if base_style else Text(line)
        from ..render_md import inline_rich
        from .path_backtick import wrap_paths_in_backticks

        wrapped = wrap_paths_in_backticks(line, self._link_ctx)
        out = inline_rich(wrapped, self._link_ctx)
        if base_style:
            # Apply base style to the spans that don't already carry a meta.
            # The simplest approach: stylize the whole Text with the base
            # style; existing per-span styles win.
            out.stylize(base_style)
        return out

    def append_thinking(self, text: str) -> None:
        self._emit_setup_divider()
        for line in text.splitlines():
            if line.strip():
                self._content.append("\n")
                self._content.append("  . ", style="dim")
                self._content.append_text(self._make_clickable(line, base_style="dim"))
        self.query_one("#main-content", Static).update(self._content)
        self._maybe_scroll()

    def append_file_edits(
        self,
        files: list[str],
        line_counts: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        """Append file edits with current fork-base counts when available."""
        self._emit_setup_divider()
        counts = line_counts or {}
        for path in files:
            self._content.append("\n")
            self._content.append("  edited ", style="bold cyan")
            link_text = f"`{path}`" if self._link_ctx is not None else path
            self._content.append_text(self._make_clickable(link_text))
            if path in counts:
                added, removed = counts[path]
                self._content.append("  total ", style="dim")
                self._content.append(f"+{added}", style="green")
                self._content.append(" ")
                self._content.append(f"-{removed}", style="red")
        self.query_one("#main-content", Static).update(self._content)
        self._maybe_scroll()

    def _box_width(self) -> int:
        # Use the actual scrollable width. ``content_size`` includes Textual's
        # reserved scrollbar gutter and makes full-width dividers wrap.
        avail = self.scrollable_content_region.width
        if avail <= 4:
            return 60
        return avail

    def _endpoint_divider(self, edge: str, name: str, target: str | None) -> str:
        """Build a one-row, terminal-cell-aware endpoint divider."""
        width = self._box_width()
        label = f"{name} [{target}]" if target else name
        fitted = Text(label)
        fitted.expand_tabs(8)
        fitted.truncate(max(1, width - 5), overflow="ellipsis")
        prefix = f"{edge}─ {fitted.plain} "
        return prefix + "─" * max(0, width - cell_len(prefix))

    def open_endpoint_box(self, name: str, target: str | None) -> None:
        self._emit_setup_divider()
        self._endpoint_style = _ENDPOINT_STYLES.get(name, _DEFAULT_ENDPOINT_STYLE)
        banner = self._endpoint_divider("┌", name, target)
        if self._content.plain:
            self._content.append("\n")
        self._content.append("\n")
        self._content.append(banner, style=f"bold {self._endpoint_style}")
        # Logical line of the opening banner = newline count after the append.
        # Resolved to a visual row at strip-render time.
        self._endpoint_open_line = self._content.plain.count("\n")
        self.query_one("#main-content", Static).update(self._content)
        self._maybe_scroll()

    def close_endpoint_box(
        self,
        name: str,
        target: str | None,
        exit_code: int,
        duration_s: float,
        cost_usd: float,
        display_lines: list[str] | None,
        summary: str = "",
    ) -> None:
        if self._endpoint_open_line is None:
            # Reconciled background jobs can emit an orphan endpoint_end. Give it
            # a real opening mark instead of reusing the previous endpoint's row.
            self.open_endpoint_box(name, target)
        assert self._endpoint_open_line is not None
        start_line = self._endpoint_open_line
        if display_lines:
            for dl in display_lines:
                self._content.append("\n")
                self._content.append("│ ", style=self._endpoint_style)
                self._content.append(dl)

        icon_char, icon_style = _ENTRY_STYLES.get(exit_code, ("!", "bold red"))
        dur = f"{duration_s:.0f}s" if duration_s < 60 else f"{duration_s / 60:.1f}m"
        cost = f" ${cost_usd:.2f}" if cost_usd else ""
        self._content.append("\n")
        self._content.append("│ ", style=self._endpoint_style)
        self._content.append(f"{icon_char} {dur}{cost}", style=icon_style)

        closing = self._endpoint_divider("└", name, target)
        self._content.append("\n")
        self._content.append(closing, style=f"bold {self._endpoint_style}")

        end_line = self._content.plain.count("\n")
        self._endpoint_completions.append(
            McpToolCompletionMark(
                start_line=start_line,
                end_line=end_line,
                name=name,
                target=target,
                exit_code=exit_code,
                duration_s=duration_s,
                cost_usd=cost_usd,
                summary=summary,
            )
        )
        self._endpoint_open_line = None
        self._endpoint_style = _DEFAULT_ENDPOINT_STYLE
        self.query_one("#main-content", Static).update(self._content)
        self._maybe_scroll()

    def append_endpoint_line(self, line: str) -> None:
        self._content.append("\n")
        self._content.append("│ ", style=self._endpoint_style)
        self._content.append_text(self._make_clickable(line))
        self.query_one("#main-content", Static).update(self._content)
        self._maybe_scroll()

    def append_specialist_text(self, text: str) -> None:
        for line in text.splitlines():
            if line.strip():
                self._content.append("\n")
                self._content.append("│ ", style=self._endpoint_style)
                self._content.append_text(
                    self._make_clickable(line, base_style="dim"),
                )
        self.query_one("#main-content", Static).update(self._content)
        self._maybe_scroll()

    # ------------------------------------------------------------------
    # Click handling for resolvable backticked tokens
    # ------------------------------------------------------------------

    def on_click(self, event: events.Click) -> None:
        """Resolve and launch the editor for the clicked link span.

        Bubbles to the app's StatusBar on failure so the user sees why
        nothing happened. Never raises out of the handler.
        """
        if self._link_ctx is None or self._editor is None:
            return
        style = event.style
        meta = getattr(style, "meta", None) if style is not None else None
        if not meta:
            return
        target = meta.get("booley_target")
        if target is None:
            return
        from . import links

        action = links.resolve(target, self._link_ctx)
        result = links.invoke(action, self._editor)
        if result.hint:
            try:
                self.app.query_one(StatusBar).show_hint(result.hint)
            except Exception:  # noqa: BLE001 — cosmetic hint failure cannot break links
                logger.debug("StatusBar hint dispatch failed", exc_info=True)
        event.stop()

    def watch_scroll_y(self, old_val: float, new_val: float) -> None:
        super().watch_scroll_y(old_val, new_val)
        # Direction matters: a small wheel-up from the very bottom still lands
        # within the tail band, so a position-only check would keep autoscroll
        # on and snap us back on the next content append. A layout reflow can
        # also lower scroll_y, but a follower clamped to the new end must stay
        # in follow mode.
        if new_val < old_val:
            if not self._auto_scroll or new_val < self.max_scroll_y:
                self._auto_scroll = False
        elif new_val >= self.max_scroll_y - 5:
            self._auto_scroll = True
        self.post_message(ScrollPositionChanged())

    def action_scroll_end(self) -> None:
        """Jump to the tail and explicitly resume follow mode."""
        self._auto_scroll = True
        self.scroll_end(animate=False, x_axis=False)

    def get_completion_marks(self) -> list[McpToolCompletionMark]:
        return self._endpoint_completions

    def _visual_line_spans(self) -> list[tuple[int, int]]:
        """Map logical lines to the rows Textual actually renders."""
        content_widget = self.query_one("#main-content", Static)
        visual = content_widget.visual
        if not isinstance(visual, Content):
            raise TypeError("MainPane content must render as textual.content.Content")
        width = content_widget.size.width
        if (
            self._visual_span_cache is not None
            and self._visual_span_cache[0] is visual
            and self._visual_span_cache[1] == width
        ):
            return self._visual_span_cache[2]
        spans: list[tuple[int, int]] = []
        row = 0
        for line in visual.split(allow_blank=True):
            height = line.get_height(content_widget.styles, width) if width > 0 else 1
            line_end = row + max(1, height)
            spans.append((row, line_end))
            row = line_end
        resolved = spans or [(0, 1)]
        self._visual_span_cache = (visual, width, resolved)
        return resolved

    def resolve_mark_divider_visual_rows(
        self,
    ) -> list[tuple[McpToolCompletionMark, int, int, int, int]]:
        """Return each mark with visual row spans for its opening/closing dividers."""
        if not self._endpoint_completions:
            return []
        spans = self._visual_line_spans()
        last = len(spans) - 1
        out: list[tuple[McpToolCompletionMark, int, int, int, int]] = []
        for mark in self._endpoint_completions:
            open_start, open_end = spans[min(mark.start_line, last)]
            close_start, close_end = spans[min(mark.end_line, last)]
            out.append((mark, open_start, open_end, close_start, close_end))
        return out

    def on_resize(self) -> None:
        self._maybe_scroll()
        # Strip membership depends on viewport height even when a paused view's
        # scroll_y does not move.
        self.post_message(ScrollPositionChanged())


# ---------------------------------------------------------------------------
# TicketHeader — compact/expanded ticket info + criteria
# ---------------------------------------------------------------------------


class TicketHeader(VerticalScroll):
    """Ticket metadata + compressed criteria status."""

    _AGENT_DISPLAY: ClassVar[dict[str, str]] = {"claude": "Claude", "codex": "ChatGPT"}

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._slug: str = ""
        self._ticket_type: str = ""
        self._branch: str = ""
        self._criteria: dict = {}
        self._expanded: bool = False

    def compose(self) -> ComposeResult:
        yield Static("", id="header-content")

    def set_ticket_info(
        self,
        slug: str,
        ticket_type: str,
        branch: str,
    ) -> None:
        self._slug = slug
        self._ticket_type = ticket_type
        self._branch = branch
        self._render_header()

    def update_criteria(self, criteria: dict) -> None:
        self._criteria = criteria
        self._render_header()

    def toggle_expanded(self) -> None:
        self._expanded = not self._expanded
        if self._expanded:
            self.add_class("expanded")
        else:
            self.remove_class("expanded")
        self._render_header()

    def _render_header(self) -> None:
        if self._expanded:
            self._render_expanded()
        else:
            self._render_compact()

    def _render_compact(self) -> None:
        content = Text()
        if not self._slug:
            self.query_one("#header-content", Static).update(content)
            return

        meta = f" · {self._ticket_type} · {self._branch}"
        content.append(self._slug, style="bold yellow")
        content.append(meta)
        if self._criteria:
            real = {k: v for k, v in self._criteria.items() if not k.startswith("_")}
            counts: dict[_CriterionStatus, int] = {
                "met": 0,
                "failing": 0,
                "needs_recheck": 0,
                "not_run": 0,
            }
            for v in real.values():
                counts[_criterion_status(v)] += 1

            # Just the counts — press 'c' for the full breakdown.
            content.append("\n")
            first = True
            for status, icon, label, style in (
                ("met", "✓", "met", "green"),
                ("failing", "✗", "failing", "red"),
                ("needs_recheck", "↻", "recheck", "color(208)"),
                ("not_run", "○", "not run", "dim"),
            ):
                count = counts[status]
                if count == 0:
                    continue
                if not first:
                    content.append("   ")
                content.append(f"{icon} {count} {label}", style=style)
                first = False
            content.append("   (press c for details)", style="dim italic")

        self.query_one("#header-content", Static).update(content)

    def _append_expanded_ticket_header(self, content: Text) -> None:
        """Rule-bordered slug and metadata at the top of the expanded panel."""
        meta = f"{self._ticket_type} · {self._branch}"
        avail = self.content_size.width
        w = max(avail - 2, max(len(l) for l in [self._slug, meta]) + 4)
        if avail > 4:
            w = min(w, avail - 2)

        rule = "─" * w
        content.append(rule + "\n", style="dim")
        slug_trunc = self._slug[: w - 2]
        content.append(f"  {slug_trunc}\n", style="bold yellow")
        meta_trunc = meta[: w - 2]
        content.append(f"  {meta_trunc}\n")
        content.append(rule + "\n", style="dim")
        content.append("\n")

    def _bucket_expanded_criteria(
        self,
    ) -> tuple[
        list[tuple[str, dict]],
        list[tuple[str, dict]],
        list[tuple[str, dict]],
        list[tuple[str, dict | None]],
    ]:
        """Sort criteria into failing, recheck, not-run, and met buckets.

        Groups whose criteria have never run collapse into one placeholder row.
        """
        real = {k: v for k, v in self._criteria.items() if not k.startswith("_")}

        groups: dict[str, list[tuple[str, dict]]] = {}
        for k, v in real.items():
            gname = _group_of(k)
            if gname is not None:
                groups.setdefault(gname, []).append((k, v))

        # Collapse groups whose criteria have never run into one placeholder row.
        collapsed = {
            gname
            for gname, members in groups.items()
            if all(_is_never_evaluated(e) for _, e in members)
        }

        failing_items: list[tuple[str, dict]] = []
        recheck_items: list[tuple[str, dict]] = []
        not_run_items: list[tuple[str, dict | None]] = []
        met_items: list[tuple[str, dict]] = []
        emitted: set[str] = set()

        for key, entry in real.items():
            gname = _group_of(key)
            if gname in collapsed:
                if gname not in emitted:
                    emitted.add(gname)
                    not_run_items.append((gname, None))
                continue
            status = _criterion_status(entry)
            if status == "met":
                met_items.append((key, entry))
            elif status == "failing":
                failing_items.append((key, entry))
            elif status == "needs_recheck":
                recheck_items.append((key, entry))
            else:
                not_run_items.append((key, entry))

        return failing_items, recheck_items, not_run_items, met_items

    @staticmethod
    def _append_expanded_section(
        content: Text,
        items: list[tuple[str, dict | None]],
        *,
        heading: str,
        icon: str,
        style: str,
        name_max: int,
    ) -> None:
        """Append one labeled criterion-status section."""
        content.append(f"{icon} {heading}\n", style=f"bold {style}")
        for key, entry in items:
            content.append("  ")
            if entry is None:
                content.append(f"{key} (not yet run)\n", style="dim italic")
                continue
            content.append(
                _truncate_name(key, name_max), style="dim" if icon in {"✓", "○"} else ""
            )
            metric = _format_metric(key, entry)
            if metric and metric not in key:
                content.append(f"  {metric}", style="dim")
            content.append("\n")

    def _render_expanded_criteria(self, content: Text, name_max: int) -> None:
        """Render actionable criteria first, followed by not-run and met."""
        buckets = self._bucket_expanded_criteria()
        sections = (
            (buckets[0], "Failing", "✗", "red"),
            (buckets[1], "Needs recheck", "↻", "color(208)"),
            (buckets[2], "Not run", "○", "dim"),
            (buckets[3], "Met", "✓", "green"),
        )
        rendered_section = False
        for items, heading, icon, style in sections:
            if not items:
                continue
            if rendered_section:
                content.append("\n")
            self._append_expanded_section(
                content,
                items,
                heading=heading,
                icon=icon,
                style=style,
                name_max=name_max,
            )
            rendered_section = True

    def _render_expanded(self) -> None:
        content = Text()
        # 2 chars consumed by the status icon + space prefix on each row.
        name_max = max(self.content_size.width - 2, 32)
        if self._slug:
            self._append_expanded_ticket_header(content)

        if not self._criteria:
            content.append("awaiting criteria…", style="dim italic")
        else:
            self._render_expanded_criteria(content, name_max)

        self.query_one("#header-content", Static).update(content)

    def on_resize(self) -> None:
        if self._slug:
            self._render_header()


# ---------------------------------------------------------------------------
# TopStrip / BottomStrip — scrolled-off completion summaries
# ---------------------------------------------------------------------------


class _CompletionStrip(Vertical):
    """Base for top/bottom completion summary strips."""

    def __init__(self, position: Literal["top", "bottom"], **kwargs) -> None:
        super().__init__(**kwargs)
        self._position = position
        self._entries: list[Text] = []
        self._overflow_count: int = 0
        # Start hidden so the strip + border-bottom don't eat ~2 chrome
        # rows before the first endpoint completion. update_entries() flips
        # display back on when there's actually something to show.
        self.display = False

    def compose(self) -> ComposeResult:
        yield Static("", id=f"{self._position}-strip-content")

    def update_entries(self, entries: list[Text], overflow_count: int) -> None:
        self._entries = entries
        self._overflow_count = overflow_count
        if not entries and overflow_count == 0:
            self.display = False
            return
        self.display = True
        content = Text()
        if self._position == "top" and overflow_count > 0:
            content.append(f"─── ▲ {overflow_count} more above ───", style="dim")
            if entries:
                content.append("\n")
        for i, entry in enumerate(entries):
            if i > 0:
                content.append("\n")
            content.append_text(entry)
        if self._position == "bottom" and overflow_count > 0:
            if entries:
                content.append("\n")
            content.append(f"─── ▼ {overflow_count} more below ───", style="dim")
        self.query_one(f"#{self._position}-strip-content", Static).update(content)


class TopStrip(_CompletionStrip):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(position="top", **kwargs)


class BottomStrip(_CompletionStrip):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(position="bottom", **kwargs)


# ---------------------------------------------------------------------------
# StatusBar — single line with counters
# ---------------------------------------------------------------------------


class StatusBar(Widget):
    """Single line: runtime budgets | context | output | cost | lines.

    ``context`` is how full the developer agent's window is *now* (absolute,
    with the model's limit when known) — the one token number a user can act
    on, since it says whether compaction is near. ``output`` is cumulative and
    tracks work done. A single flat input+output total was shown here before,
    but with prompt caching it is dominated by re-reads of the same context and
    grows with turn count, so it moved opposite to cost; the per-token-class
    breakdown lives in the per-step usage report instead.

    Also displays transient hints (4s) for failed link clicks via
    :meth:`show_hint` — the hint replaces the counter line while
    active, then the counter line re-renders.
    """

    # How long a transient click-failure hint replaces the counter line.
    _HINT_DURATION_S = 4.0

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._start_time: float = 0.0
        self._context_tokens: int = 0
        self._context_limit: int | None = None
        self._output_tokens: int = 0
        self._cost_usd: float = 0.0
        self._lines_added: int = 0
        self._lines_removed: int = 0
        self._activity: str = ""
        self._developer_budget: tuple[float, float, int, int, bool, str] | None = None
        # Hint state: text + expiry time. None = no active hint.
        self._hint_text: str = ""
        self._hint_expiry: float = 0.0
        self._hint_clear_timer: Timer | None = None

    def on_mount(self) -> None:
        self._start_time = time.monotonic()
        self.set_interval(1.0, self._refresh_elapsed)

    def compose(self) -> ComposeResult:
        yield Static(
            "elapsed: 0s | context 0 | 0 out | $0.00 | +0 -0 lines",
            id="status-text",
            markup=False,
        )

    def update_counters(
        self,
        *,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        lines_added: int = 0,
        lines_removed: int = 0,
        context_tokens: int | None = None,
        context_limit: int | None = None,
    ) -> None:
        """Accumulate the delta counters; replace the context snapshot.

        ``context_tokens``/``context_limit`` are absolute readings, so they are
        assigned rather than summed. ``None`` leaves the current value alone —
        an endpoint-completion update carries no context reading of its own.
        """
        self._output_tokens += output_tokens
        self._cost_usd += cost_usd
        self._lines_added += lines_added
        self._lines_removed += lines_removed
        if context_tokens is not None:
            self._context_tokens = context_tokens
        if context_limit is not None:
            self._context_limit = context_limit
        self._refresh_display()

    def _refresh_elapsed(self) -> None:
        self._refresh_display()

    def set_line_counts(self, lines_added: int, lines_removed: int) -> None:
        """Replace the absolute worktree line counts."""
        self._lines_added = lines_added
        self._lines_removed = lines_removed
        self._refresh_display()

    def set_activity(self, activity: str) -> None:
        """Set the current high-level harness activity, or clear it."""
        self._activity = activity
        self._refresh_display()

    def set_developer_budget(
        self,
        *,
        wall_elapsed_seconds: float,
        active_elapsed_seconds: float,
        wall_limit_seconds: int,
        active_limit_seconds: int,
        paused: bool,
        pause_reason: str,
    ) -> None:
        """Replace the live Developer Agent budget snapshot."""
        self._developer_budget = (
            wall_elapsed_seconds,
            active_elapsed_seconds,
            wall_limit_seconds,
            active_limit_seconds,
            paused,
            pause_reason,
        )
        self._refresh_display()

    def show_hint(self, hint: str) -> None:
        """Display *hint* in place of counters for ``_HINT_DURATION_S`` seconds.

        Used by MainPane.on_click when an editor invocation fails or
        a target can't be resolved.
        """
        if not hint:
            return
        self._hint_text = hint
        self._hint_expiry = time.monotonic() + self._HINT_DURATION_S
        if self._hint_clear_timer is not None:
            self._hint_clear_timer.stop()
        self._hint_clear_timer = self.set_timer(
            self._HINT_DURATION_S,
            self._clear_hint,
        )
        self._refresh_display()

    def _clear_hint(self) -> None:
        self._hint_text = ""
        self._hint_expiry = 0.0
        self._hint_clear_timer = None
        self._refresh_display()

    def _context_str(self) -> str:
        """``142k/1m`` when the model's window is known, else just ``142k``."""
        used = _format_token_count(self._context_tokens)
        if not self._context_limit:
            return used
        return f"{used}/{_format_token_count(self._context_limit)}"

    def _refresh_display(self) -> None:
        if self._hint_text and time.monotonic() < self._hint_expiry:
            t = Text()
            t.append("⚠ ", style="yellow")
            t.append(self._hint_text, style="yellow")
            self.query_one("#status-text", Static).update(t)
            return
        t = Text()
        if self._activity:
            t.append(f"{self._activity} ", style="bold yellow")
            t.append("│ ", style="dim")
        self._append_runtime(t)
        t.append(" │ ", style="dim")
        t.append("context ", style="dim")
        t.append(self._context_str())
        t.append(" │ ", style="dim")
        t.append(f"{_format_token_count(self._output_tokens)} out")
        t.append(" │ ", style="dim")
        t.append(f"${self._cost_usd:.2f}")
        t.append(" │ ", style="dim")
        t.append(f"+{self._lines_added}", style="green")
        t.append(" ")
        t.append(f"-{self._lines_removed}", style="red")
        t.append(" lines")
        self.query_one("#status-text", Static).update(t)

    def _append_runtime(self, text: Text) -> None:
        if self._developer_budget is None:
            elapsed = time.monotonic() - self._start_time
            text.append("elapsed: ", style="dim")
            text.append(_format_runtime(elapsed, include_hour_seconds=True))
            return
        wall, active, wall_limit, active_limit, paused, reason = self._developer_budget
        text.append("wall ", style="dim")
        text.append(f"{_format_runtime(wall)}/{_format_runtime_limit(wall_limit)}")
        text.append(" │ ", style="dim")
        text.append("active ", style="dim")
        text.append(f"{_format_runtime(active)}/{_format_runtime_limit(active_limit)}")
        if paused:
            label = reason or "Booley tool wait"
            text.append(f" ({label})", style="cyan")


def _format_runtime(seconds: float, *, include_hour_seconds: bool = False) -> str:
    """Format runtime without allowing minutes to grow past 59."""
    seconds = max(0.0, seconds)
    rounded = round(seconds)
    if seconds < 60:
        return f"{rounded}s"
    if rounded < 3600:
        minutes, remainder = divmod(rounded, 60)
        return f"{minutes}m{remainder:02d}s"
    hours, remainder = divmod(rounded, 3600)
    minutes, trailing_seconds = divmod(remainder, 60)
    suffix = f"{trailing_seconds:02d}s" if include_hour_seconds else ""
    return f"{hours}h{minutes:02d}m{suffix}"


def _format_runtime_limit(seconds: int) -> str:
    """Format configured limits without redundant zero-valued components."""
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0 and seconds < 3600:
        return f"{seconds // 60}m"
    return _format_runtime(seconds, include_hour_seconds=seconds % 60 != 0)
