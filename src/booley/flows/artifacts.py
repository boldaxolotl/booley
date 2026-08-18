"""The ``artifacts`` block every Flow report carries — one place, one shape.

The structured result a Flow hands the agent answers two questions: *what
happened* (verdict + headline numbers, at a glance) and *where do I look for
more*. The second half used to live only in the Flow's stdout, which the MCP
layer tail-truncates — so on exactly the long, noisy runs where an agent needs
the log, the pointer to it was the first thing cut.

**Directories, not file inventories.** An earlier version of this enumerated
every artifact a run produced: 16 keys per report, 80-90% of the payload, three
overlapping copies of the same paths, and a hardcoded filename per key that
goes silently missing the day a Flow renames its output. A run's artifacts live
in two or three directories, and the agent has a shell. So the block names the
directories and lets ``ls`` do the rest — that answer cannot go stale, costs
~400 bytes instead of ~4 KB, and needs no code change when a Flow starts
emitting a new report.

What stays enumerated is only what a directory listing genuinely cannot give
you: the two *entry points*.

  log     the full raw Flow output for this run (``run.log``) — usually not in
          the same directory as the build artifacts, and the first thing a
          reader wants
  report  this run's own JSON, so a consumer holding only the report can find
          everything else
  dirs    ``{name: path}`` of the directories holding everything else. Names
          are roles (``build``, ``timing``, ``impl``, ``synth``,
          ``mutant_logs``), so the *meaning* survives even though the file
          names are not listed.

Two rules keep a pointer trustworthy:

* **Never point at something that is not there.** A pointer the reader cannot
  open costs them a round trip and teaches them to distrust the rest of the
  block, so :func:`artifacts_block` drops missing entries rather than
  advertising them. (Same reasoning as ``simulate``'s freshness-guarded ``log``
  pointer: a stale pointer is worse than none.)
* **Work-dir-relative, always.** An absolute path is either a sandbox
  ``/work/...`` path a host-side reader cannot open, or a host path that means
  nothing inside the Session Runtime. Relative to the work dir it resolves in
  both views, which is the same reason the Boundary Command Contract passes
  ``make -C`` a relative path. Note this is relative to the *work dir*, not to
  the project root: in Ticket Mode the work dir is a worktree while the report
  and lock dirs live under ``tickets/logs/<slug>/``, so those keys legitimately
  come out as ``../../...``. They still resolve, because the whole project root
  is what gets mounted — not just the worktree.

A multi-target Flow nests one block per target: ``artifacts[target][key]``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from booley.runtime.platform_paths import posix_relpath

logger = logging.getLogger(__name__)


def relative(path: Path | str | None, work_dir: Path | str) -> str | None:
    """*path* as a work-dir-relative POSIX string, or None when unusable.

    None — not a best-guess string — when the path is absent or does not exist
    on disk: the caller drops the key entirely rather than advertise something
    the reader cannot open.

    A *relative* input is taken as already work-dir-relative and is anchored on
    *work_dir* for the existence check, never on the process cwd — a Flow
    process does not necessarily run from the work dir, and resolving against
    cwd would silently drop every live pointer a caller passes in the very form
    this function returns.
    """
    if not path:
        return None
    try:
        root = Path(work_dir)
        p = Path(path)
        absolute = p if p.is_absolute() else root / p
        if not absolute.exists():
            return None
        return posix_relpath(absolute, root)
    except (OSError, ValueError):  # unreadable, or no relative form (other drive)
        logger.debug("artifacts: could not relativize %s against %s", path, work_dir)
        return None


def artifacts_block(
    work_dir: Path | str,
    *,
    dirs: dict[str, Path | str | None] | None = None,
    **files: Path | str | None,
) -> dict[str, object]:
    """Build the ``artifacts`` block: entry-point *files* plus a ``dirs`` map.

    Every value is run through :func:`relative`, so the result holds only
    entries that exist and could be expressed work-dir-relatively. ``dirs`` is
    omitted entirely when none of its candidates resolved, and the whole block
    can legitimately come back empty (a run that produced nothing durable) —
    callers omit the key in that case rather than emitting ``{}``, which would
    read as "checked, found nothing" instead of "not applicable".
    """
    block: dict[str, object] = {}
    for key, value in files.items():
        rel = relative(value, work_dir)
        if rel is not None:
            block[key] = rel
    resolved_dirs: dict[str, str] = {}
    for key, value in (dirs or {}).items():
        rel = relative(value, work_dir)
        if rel is not None:
            resolved_dirs[key] = rel
    if resolved_dirs:
        block["dirs"] = resolved_dirs
    return block


def merge_artifacts(detail: dict, block: dict[str, object]) -> None:
    """Merge *block* into ``detail["artifacts"]`` in place (no-op when empty).

    ``dirs`` is merged key-by-key rather than replaced, so a caller that adds
    directories in two passes does not lose the first pass's entries.
    """
    if not block:
        return
    existing = detail.setdefault("artifacts", {})
    if not isinstance(existing, dict):
        return
    for key, value in block.items():
        if key == "dirs" and isinstance(value, dict):
            merged = existing.setdefault("dirs", {})
            if isinstance(merged, dict):
                merged.update(value)
        else:
            existing[key] = value
