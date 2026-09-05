"""Utility functions for the ticket board system."""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import IO

from booley.runtime import pid as runtime_pid
from booley.runtime.file_lock import acquire_file_lock, release_file_lock
from booley.runtime.timefmt import format_human_datetime, parse_timestamp, utc_now_rfc3339

# ---------------------------------------------------------------------------
# Platform-specific file locking
# ---------------------------------------------------------------------------

is_pid_alive = runtime_pid.is_pid_alive

_SAFE_TICKET_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class TicketSlugError(ValueError):
    """A ticket slug from an external boundary is unsafe."""


def validate_ticket_slug(slug: str) -> str:
    """Return *slug* when it is safe for refs and project-relative paths."""
    if not _SAFE_TICKET_SLUG_RE.fullmatch(slug):
        raise TicketSlugError(f"unsafe ticket slug: {slug!r}")
    return slug


def resolve_runtime_ticket_slug(ticket_path: Path) -> str:
    """Resolve and validate the Ticket Mode slug from its environment boundary."""
    slug = (
        os.environ.get("BOOLEY_SLUG", "").strip()
        or os.environ.get("BOOLEY_TICKET_SLUG", "").strip()
        or ticket_path.stem
    )
    return validate_ticket_slug(slug)


def lock_fd(f: IO) -> None:
    """Compatibility wrapper for the runtime-owned lock primitive."""
    acquire_file_lock(f)


def unlock_fd(f: IO) -> None:
    """Compatibility wrapper for the runtime-owned lock primitive."""
    release_file_lock(f)


def read_lock_pid(lock_path: str | Path) -> int | None:
    """Read PID from a lock file. Returns int or None."""
    try:
        text = Path(lock_path).read_text(encoding="utf-8").strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# UTF-8 output on Windows
# ---------------------------------------------------------------------------


def ensure_utf8_output() -> None:
    """Reconfigure stdout/stderr to UTF-8 if needed."""
    if sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr.encoding != "utf-8":
        sys.stderr.reconfigure(encoding="utf-8")


# ---------------------------------------------------------------------------
# Timestamp & formatting helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return utc_now_rfc3339()


def slug_from_file(file_path: str) -> str:
    """Extract slug from a file path like 'active/my-ticket.md' -> 'my-ticket'."""
    return Path(file_path).stem if file_path else ""


def generate_slug(summary: str, max_len: int = 40) -> str:
    """Convert summary to URL-safe slug: lowercase, special chars -> '-', truncated."""
    slug = re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")
    return slug[:max_len].rstrip("-")


def compute_done_slugs(tickets: list[dict]) -> set[str]:
    """Return set of slugs for tickets with status 'done'."""
    done = set()
    for t in tickets:
        if t.get("status") == "done":
            fb = t.get("feature_branch") or slug_from_file(t.get("file", ""))
            if fb:
                done.add(fb)
    return done


def detect_tickets_dir() -> Path:
    """Auto-detect the tickets directory.

    Honors TICKETS_DIR env var for test isolation.
    """
    if "TICKETS_DIR" in os.environ:
        return Path(os.environ["TICKETS_DIR"])
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from booley.runtime.project_dir import resolve_project_dir

    return resolve_project_dir() / "tickets"


def tickets_dir_from_project_root(project_root: str | Path) -> Path:
    """Resolve tickets directory from a project root path.

    Honors TICKETS_DIR env var for test isolation, then BOOLEY_PROJECT_DIR,
    then convention (.booley_project/ sibling), then legacy fallback.
    """
    if "TICKETS_DIR" in os.environ:
        return Path(os.environ["TICKETS_DIR"])
    if "BOOLEY_PROJECT_DIR" in os.environ:
        return Path(os.environ["BOOLEY_PROJECT_DIR"]) / "tickets"
    project_root = Path(project_root)
    sibling = project_root / ".booley_project"
    if sibling.is_dir():
        return sibling / "tickets"
    return project_root / ".booley" / "project" / "tickets"


def detect_project_root() -> Path:
    """Auto-detect project root (parent of the project data directory).

    Honors the Ticket Mode control-plane root, then its durable runtime Ticket
    path, then PROJECT_ROOT for test isolation.
    """
    if "BOOLEY_CONTROL_PROJECT_ROOT" in os.environ:
        return Path(os.environ["BOOLEY_CONTROL_PROJECT_ROOT"])
    runtime_ticket = os.environ.get("BOOLEY_TICKET_FILE")
    if runtime_ticket:
        ticket_path = Path(runtime_ticket).resolve()
        for parent in ticket_path.parents:
            if parent.name != "tickets":
                continue
            control_root = parent.parent.parent
            if control_root != control_root.parent:
                return control_root
    if "PROJECT_ROOT" in os.environ:
        return Path(os.environ["PROJECT_ROOT"])
    try:
        from booley.runtime.project_dir import resolve_project_dir

        project_dir = resolve_project_dir()
    except FileNotFoundError:
        # Fallback for source-tree-only invocations that do not have an RTL
        # project nearby. This preserves the historical behavior.
        return Path(__file__).resolve().parents[3]
    root = project_dir.parent
    # Convention: the data dir lives inside the repo as <repo>/.booley_project,
    # so its parent is the repo root. The Session Runtime breaks that — it can
    # bind-mount the data dir as a top-level sibling named /booley-project (no
    # leading dot), whose parent is the filesystem root. `/` is never a project
    # root, and returning it makes every scope/TB/branch check resolve against
    # the wrong tree (QA_REPORT D1). Recover the real root from the cwd (the
    # repo the caller stands in — /work in the in-container session).
    if root == root.parent:
        cwd = Path.cwd().resolve()
        for cand in [cwd, *cwd.parents]:
            if (cand / ".booley_project").is_dir():
                return cand
    return root


def parse_arrow(transition: str) -> tuple[str, str] | None:
    """Split a 'from -> to' or 'from → to' transition string. Returns (from, to) or None."""
    for sep in (" -> ", " \u2192 "):
        if sep in transition:
            parts = transition.split(sep)
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()
    return None


def parse_iso(ts: str) -> datetime:
    """Parse ISO-8601 timestamp string (with optional Z suffix) to datetime."""
    return parse_timestamp(ts)


def fmt_duration(secs: float) -> str:
    """Format seconds as HH:MM:SS or MM:SS duration."""
    if secs < 0:
        return "---"
    secs = int(secs)
    hours, remainder = divmod(secs, 3600)
    mins, s = divmod(remainder, 60)
    if hours > 0:
        return f"{hours:02d}:{mins:02d}:{s:02d}"
    return f"{mins:02d}:{s:02d}"


def fmt_datetime_user(iso_str: str | None) -> str:
    """Format an ISO timestamp using Booley's human-visible date convention."""
    try:
        return format_human_datetime(iso_str)
    except (ValueError, AttributeError, TypeError):
        return iso_str or "---"


def fmt_tokens(tokens: int | None) -> str:
    """Format token count as human-readable (e.g. '74K', '1.2M')."""
    if not tokens:
        return ""
    if tokens < 1000:
        return str(tokens)
    if tokens < 1_000_000:
        return f"{tokens / 1000:.0f}K"
    return f"{tokens / 1_000_000:.1f}M"
