"""Click resolver layer for Console clickable file links.

One module owns the "what does this clickable token actually do" question.
Widget click handlers build a :class:`LinkTarget`, call :func:`resolve`
with a :class:`LinkContext`, then pass the :class:`ResolvedAction` to
:func:`invoke` along with the user's :class:`ResolvedEditor`.

All four pieces (target, context, action, invocation) live here so that
worktree-vs-project resolution, ticket-slug → current-status lookup, and
subprocess plumbing share one home. Click handlers must not invoke
subprocesses directly — that responsibility is concentrated here.

Resolution rules (Phase 2.2 of the plan):

* ``kind == "ticket"``: ``find_ticket_file`` at click time; the ticket may
  have moved board status mid-run, so we never cache the path.
* ``kind == "file"``:
  - Source under both worktree and project → diff worktree-copy vs
    fork-base-copy (the latter fetched lazily via ``git show <base>:<path>``).
  - Newly created in worktree only → diff worktree-copy vs an empty file.
  - Project-only → open (or open_at_line if a line number is captured).
  - Post-cleanup (worktree gone): diff degrades to open on the project copy.
  - File missing entirely → ``none`` with a hint string.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from booley.config.editor import ResolvedEditor

logger = logging.getLogger(__name__)


def build_link_context(
    *,
    project_root: Path,
    worktree_root: Path | None,
    fork_base_sha: str | None,
    sandbox_mount_prefix: str = "/work",
) -> LinkContext:
    """Construct a :class:`LinkContext` from the harness run inputs.

    Reads RTL/TB source dirs from booley.toml (via shared_infra) and
    wires :func:`ticket_board.find_ticket_file` so the resolver can
    look up ticket-slug targets without importing the board layer.
    """
    rtl_dirs: tuple[str, ...] = ()
    tb_dirs: tuple[str, ...] = ()
    try:
        from booley.runtime.shared_infra import get_rtl_source_dirs, get_tb_source_dirs

        rtl_dirs = tuple(get_rtl_source_dirs(project_root))
        tb_dirs = tuple(get_tb_source_dirs(project_root))
    except (ImportError, OSError) as e:
        logger.debug("RTL/TB source dirs not loaded: %s", e)

    tickets_dir: Path | None = None
    find_ticket_file: FindTicketFile | None = None
    try:
        from booley.ticket_board.helpers import tickets_dir_from_project_root
        from booley.ticket_board.scanner import find_ticket_file as _find

        tickets_dir = tickets_dir_from_project_root(project_root)
        find_ticket_file = _find
    except ImportError as e:
        logger.debug("ticket board not importable: %s", e)

    return LinkContext(
        project_root=project_root,
        worktree_root=worktree_root,
        fork_base_sha=fork_base_sha,
        rtl_dirs=rtl_dirs,
        tb_dirs=tb_dirs,
        sandbox_mount_prefix=sandbox_mount_prefix,
        find_ticket_file=find_ticket_file,
        tickets_dir=tickets_dir,
    )


def resolve_fork_base_sha(
    worktree_root: Path | None,
    base_ref: str,
) -> str | None:
    """Resolve the fork-base commit SHA for diff materialization.

    Uses ``git merge-base feature-branch base-ref`` so the answer is
    honest even after the base branch advances during a long run. Falls
    back to ``rev-parse base-ref`` if the worktree's HEAD isn't yet
    on a feature branch (early-setup case).
    """
    if worktree_root is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "merge-base", "HEAD", base_ref],
            cwd=str(worktree_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        proc = subprocess.run(
            ["git", "rev-parse", base_ref],
            cwd=str(worktree_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug("resolve_fork_base_sha failed: %s", e)
    return None


# Type-only forward; avoids a hard cyclic import with ticket_board.
FindTicketFile = Callable[[Path, str], tuple[Path | None, str | None]]


@dataclass(frozen=True)
class LinkTarget:
    """A clickable token's identity at render time.

    Resolution happens at click time, not render time — see ADR 0010 for
    why ticket-slug paths must not be frozen at render.
    """

    kind: Literal["file", "ticket"]
    raw: str  # backtick contents as displayed
    line: int | None = None


@dataclass(frozen=True)
class ResolvedAction:
    """An editor action — what to run, with which paths.

    ``args`` are positional substitutions for the editor template's
    placeholders. For ``open`` and ``open_at_line``, ``args`` is
    ``(file,)`` (line rides on the template itself); for ``diff``,
    ``args`` is ``(left, right)``. For ``none``, ``args`` is empty and
    ``hint`` carries the status-bar message.
    """

    kind: Literal["open", "open_at_line", "diff", "none"]
    args: tuple[str, ...] = ()
    line: int | None = None
    hint: str = ""


@dataclass
class LinkContext:
    """Per-Console-run resolution state.

    Built once at Console startup and reused across every click. Most
    fields are immutable for the run; ``worktree_root`` flips to
    ``None`` after the worktree manager cleans up so diffs degrade to
    plain-open of the project copy.
    """

    project_root: Path
    worktree_root: Path | None
    # Commit SHA the worktree was branched from. Used as the "before"
    # side of a live diff: not git HEAD, because main may have advanced
    # mid-run; not the agent's last write, because the agent may have
    # rewritten the file ten times.
    fork_base_sha: str | None
    # RTL/TB source dirs from booley.toml (already normalized to project-
    # relative posix prefixes by the loader).
    rtl_dirs: tuple[str, ...] = ()
    tb_dirs: tuple[str, ...] = ()
    # Sandbox container mount prefix, e.g. ``/work``. Stripped from
    # EDA-tool-output paths before resolution.
    sandbox_mount_prefix: str = ""
    # Ticket-board lookup. Injected so this module doesn't import
    # ticket_board (which would create a layering knot).
    find_ticket_file: FindTicketFile | None = None
    tickets_dir: Path | None = None
    # Lazy temp-file cache keyed by (project-relative-path, fork_base_sha).
    # Populated by the resolver on the first diff click; cleaned up on
    # Console exit by the caller.
    _fork_base_cache: dict[tuple[str, str], Path] = field(default_factory=dict)

    def attach_worktree(
        self,
        worktree_root: Path | None,
        fork_base_sha: str | None,
    ) -> None:
        """Set the worktree info once setup has materialized it.

        Called by the developer after workspace setup completes; before
        the call, file-target clicks resolve against project only.
        """
        self.worktree_root = worktree_root
        self.fork_base_sha = fork_base_sha

    def strip_sandbox_prefix(self, raw: str) -> str:
        """Strip a leading container mount prefix (e.g. ``/work``)."""
        if not self.sandbox_mount_prefix:
            return raw
        norm_prefix = self.sandbox_mount_prefix.rstrip("/")
        if not norm_prefix:
            return raw
        if raw == norm_prefix:
            return ""
        if raw.startswith(norm_prefix + "/"):
            return raw[len(norm_prefix) + 1 :]
        return raw

    def resolves_under_roots(self, rel_or_abs: str) -> bool:
        """Return True iff *rel_or_abs* points at a file under project/worktree.

        Absolute paths are tested directly; relative paths are joined under
        each root in turn.
        """
        p = Path(rel_or_abs)
        if p.is_absolute():
            try:
                return p.is_file()
            except OSError:
                return False
        roots: list[Path] = []
        if self.worktree_root is not None:
            roots.append(self.worktree_root)
        if self.project_root is not None:
            roots.append(self.project_root)
        for root in roots:
            try:
                if (root / p).is_file():
                    return True
            except OSError:
                continue
        return False


@dataclass(frozen=True)
class InvokeResult:
    """Outcome of an editor invocation. Never raises out of :func:`invoke`.

    ``ok=False`` carries a status-bar hint; ``ok=True`` may still carry
    a hint for advisory cases (e.g. degraded action).
    """

    ok: bool
    hint: str = ""


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve(target: LinkTarget, ctx: LinkContext) -> ResolvedAction:
    """Translate a target into a concrete editor action.

    The resolver is pure (besides the lazy fork-base tempfile cache) —
    it never invokes subprocesses. Click handlers pair it with
    :func:`invoke`.
    """
    if target.kind == "ticket":
        return _resolve_ticket(target, ctx)
    if target.kind == "file":
        return _resolve_file(target, ctx)
    return ResolvedAction(kind="none", hint=f"Unknown link kind {target.kind!r}")


def _resolve_ticket(target: LinkTarget, ctx: LinkContext) -> ResolvedAction:
    if ctx.find_ticket_file is None or ctx.tickets_dir is None:
        return ResolvedAction(
            kind="none",
            hint="ticket board not wired into Console",
        )
    try:
        path, _status = ctx.find_ticket_file(ctx.tickets_dir, target.raw)
    except OSError as e:
        logger.debug("find_ticket_file failed for %s: %s", target.raw, e)
        return ResolvedAction(
            kind="none",
            hint=f"ticket lookup failed: {target.raw}",
        )
    if path is None:
        return ResolvedAction(
            kind="none",
            hint=f"ticket not found on board: {target.raw}",
        )
    return ResolvedAction(kind="open", args=(str(path),))


def _resolve_file(target: LinkTarget, ctx: LinkContext) -> ResolvedAction:
    # Strip sandbox container mount prefix if present.
    raw = ctx.strip_sandbox_prefix(target.raw)
    rel = _to_project_relative(raw, ctx)
    if rel is None:
        return ResolvedAction(
            kind="none",
            hint=f"path outside project/worktree: {target.raw}",
        )

    project_path = (ctx.project_root / rel).resolve()
    worktree_path = (ctx.worktree_root / rel).resolve() if ctx.worktree_root else None

    in_project = project_path.is_file()
    in_worktree = worktree_path is not None and worktree_path.is_file()

    is_source = _is_rtl_or_tb(rel, ctx)

    # Source under both: live diff against fork-base.
    if is_source and in_worktree and ctx.worktree_root is not None:
        if in_project and ctx.fork_base_sha:
            base_copy = _materialize_fork_base(rel, ctx)
            if base_copy is not None:
                return ResolvedAction(
                    kind="diff",
                    args=(str(worktree_path), str(base_copy)),
                )
        # Newly created in worktree only — diff against an empty file
        # so the editor shows "all added".
        if not in_project:
            empty = _empty_tempfile()
            return ResolvedAction(
                kind="diff",
                args=(str(worktree_path), str(empty)),
            )

    # Worktree has the file but it's not RTL/TB — plain open of the worktree copy.
    if in_worktree and not is_source:
        if target.line is not None:
            return ResolvedAction(
                kind="open_at_line",
                args=(str(worktree_path),),
                line=target.line,
            )
        return ResolvedAction(kind="open", args=(str(worktree_path),))

    # Fall through: project-only (worktree cleaned up, or non-source file
    # that lives in main repo like plans/specs/ADRs).
    if in_project:
        if target.line is not None:
            return ResolvedAction(
                kind="open_at_line",
                args=(str(project_path),),
                line=target.line,
            )
        return ResolvedAction(kind="open", args=(str(project_path),))

    return ResolvedAction(
        kind="none",
        hint=f"file not found: {raw}",
    )


def _to_project_relative(raw: str, ctx: LinkContext) -> str | None:
    """Coerce a raw path into a project-relative posix string.

    Accepts absolute paths under either project or worktree root, plus
    already-relative paths. Returns ``None`` if the path falls outside
    every known root (false-positive guard against random tokens like
    ``foo.bar``).
    """
    p = Path(raw)
    if p.is_absolute():
        for root in (ctx.worktree_root, ctx.project_root):
            if root is None:
                continue
            try:
                return p.resolve().relative_to(root.resolve()).as_posix()
            except (ValueError, OSError):
                continue
        return None
    # Relative path — accept only if it resolves under one of the roots.
    for root in (ctx.worktree_root, ctx.project_root):
        if root is None:
            continue
        candidate = (root / p).resolve()
        try:
            candidate.relative_to(root.resolve())
        except (ValueError, OSError):
            continue
        if candidate.exists() or _looks_like_source_path(raw):
            return p.as_posix()
    return None


def _looks_like_source_path(raw: str) -> bool:
    """Heuristic: token has a path-like shape (slash or extension).

    Used to admit paths from EDA-tool stderr that don't yet exist (e.g. a
    user-reported missing file). We only need a yes/no — the actual
    resolution result will still surface 'not found' if it really is bogus.
    """
    return "/" in raw or "\\" in raw or "." in raw


def _is_rtl_or_tb(rel: str, ctx: LinkContext) -> bool:
    """Classify a project-relative path against the [sources.*] dirs."""
    rel_posix = rel.replace("\\", "/")
    for prefix in (*ctx.rtl_dirs, *ctx.tb_dirs):
        norm = prefix.replace("\\", "/").rstrip("/")
        if not norm:
            continue
        if rel_posix == norm or rel_posix.startswith(norm + "/"):
            return True
    return False


def _materialize_fork_base(rel: str, ctx: LinkContext) -> Path | None:
    """Materialize the file's content at ``fork_base_sha`` into a tempfile.

    Lazy: the first click for a given (path, sha) shells out to
    ``git show``; subsequent clicks reuse the tempfile. The cache is
    keyed by (project-relative-path, sha) so changing fork-base would
    invalidate.

    Returns ``None`` (with a logged warning) if ``git show`` fails —
    the caller falls back to plain-open of the project copy.
    """
    if not ctx.fork_base_sha or ctx.project_root is None:
        return None
    key = (rel, ctx.fork_base_sha)
    cached = ctx._fork_base_cache.get(key)
    if cached is not None and cached.exists():
        return cached

    spec = f"{ctx.fork_base_sha}:{rel}"
    try:
        # cwd = project_root: the fork-base sha lives in the main repo's
        # object database, not necessarily in the worktree. text=False
        # because the file may be UTF-16/Latin-1 etc.
        proc = subprocess.run(
            ["git", "show", spec],
            cwd=str(ctx.project_root),
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug("git show %s failed: %s", spec, e)
        return None
    if proc.returncode != 0:
        logger.debug(
            "git show %s exited %d: %s",
            spec,
            proc.returncode,
            proc.stderr.decode("utf-8", "replace")[:200],
        )
        return None

    # Suffix preserves the language so the editor's diff view enables
    # syntax highlighting.
    suffix = Path(rel).suffix or ""
    fd, tmp_str = tempfile.mkstemp(prefix="booley-base-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(proc.stdout)
    except OSError as e:
        logger.debug("write fork-base tempfile failed: %s", e)
        return None
    tmp = Path(tmp_str)
    ctx._fork_base_cache[key] = tmp
    return tmp


def _empty_tempfile() -> Path:
    """Return a path to an empty tempfile, created on first call."""
    fd, name = tempfile.mkstemp(prefix="booley-empty-", suffix=".diff")
    os.close(fd)
    return Path(name)


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------


def invoke(action: ResolvedAction, editor: ResolvedEditor) -> InvokeResult:
    """Execute the editor for *action*. Never raises.

    Subprocess is detached (Popen + no wait); the editor lives past
    Console exit. Errors collapse into a status-bar hint.
    """
    if action.kind == "none":
        return InvokeResult(ok=False, hint=action.hint)

    template = _template_for(action, editor)
    if template is None:
        return InvokeResult(
            ok=False,
            hint=f"editor has no template for {action.kind!r}",
        )

    argv = _substitute(template, action)
    if argv is None:
        return InvokeResult(
            ok=False,
            hint=f"editor template missing placeholder for {action.kind!r}",
        )

    try:
        kwargs: dict = {}
        if sys.platform == "win32":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — child outlives us
            # without inheriting our console (which Textual owns).
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except FileNotFoundError:
        return InvokeResult(
            ok=False,
            hint=f"editor not found: {argv[0]}",
        )
    except OSError as e:
        return InvokeResult(
            ok=False,
            hint=f"editor launch failed: {e}",
        )
    return InvokeResult(ok=True)


def _template_for(
    action: ResolvedAction,
    editor: ResolvedEditor,
) -> tuple[str, ...] | None:
    """Pick the right template from *editor* for *action*.

    Degrades ``diff`` → ``open`` (on the worktree side, i.e. ``args[0]``)
    when the editor has no diff support.
    """
    if action.kind == "open":
        return editor.open
    if action.kind == "open_at_line":
        return editor.open_at_line
    if action.kind == "diff":
        if editor.diff is not None:
            return editor.diff
        # Degrade: open the worktree copy plain.
        return editor.open
    return None


def _substitute(
    template: tuple[str, ...],
    action: ResolvedAction,
) -> list[str] | None:
    """Fill placeholders in *template* using *action*.

    Placeholder-driven (not action.kind-driven) so a degraded
    diff→open template substitutes ``{file}`` from the worktree-side
    path, while a real ``diff`` template substitutes ``{left}``/
    ``{right}``. Returns ``None`` if any placeholder remains unfilled.
    """
    if not action.args:
        return None

    # Build the substitution map from whichever args we have.
    # First arg is always the primary file (worktree side for diff,
    # the target for open/open_at_line).
    subs: dict[str, str] = {
        "{file}": action.args[0],
        "{left}": action.args[0],
        "{line}": str(action.line) if action.line is not None else "",
    }
    if len(action.args) >= 2:
        subs["{right}"] = action.args[1]
    else:
        # Degraded diff — no second path available. Leave {right} unset
        # so the placeholder check catches a mismatch (a real diff
        # template fed only one arg is a bug).
        pass

    out: list[str] = []
    for tok in template:
        tok2 = tok
        for key, val in subs.items():
            tok2 = tok2.replace(key, val)
        if "{" in tok2 and "}" in tok2:
            # Unfilled placeholder — template/action mismatch.
            return None
        out.append(tok2)
    return out
