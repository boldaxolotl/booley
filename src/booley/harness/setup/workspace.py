"""Workspace preparation: create worktree and feature branch (plus git hooks)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from booley.dev_support.validate_commit_msg import validate_message
from booley.runtime.filesystem_utils import copy_booley_tree, safe_rmtree
from booley.runtime.git import add_git_excludes, git_run
from booley.runtime.paths import dev_support_dir
from booley.runtime.platform_paths import bash_bin
from booley.runtime.project_dir import resolve_project_dir
from booley.runtime.ticket_repositories import paired_project_repository, project_repository_scope

from ..models import StepResult, TicketContext
from ..worktree_health import check_worktree_health
from .worktree_lock_gc import _prune_stale_worktree_locks

logger = logging.getLogger(__name__)


def _set_worktree_hooks_path(worktree_path: Path, hooks_posix: str) -> None:
    """Point a worktree at its own hooks dir via core.hooksPath.

    Enables extensions.worktreeConfig in the shared repo config (required for
    per-worktree config files), then sets core.hooksPath in the worktree's own
    config.worktree file.
    """
    # Enable per-worktree config support (idempotent, shared setting)
    subprocess.run(
        ["git", "config", "extensions.worktreeConfig", "true"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        check=False,
    )
    result = subprocess.run(
        ["git", "config", "--worktree", "core.hooksPath", hooks_posix],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        logger.debug("Set core.hooksPath=%s for worktree", hooks_posix)
    else:
        logger.warning(
            "Failed to set core.hooksPath for worktree (rc=%d): %s",
            result.returncode,
            result.stderr.strip(),
        )


def _install_scope_hook(
    worktree_path: Path,
    scope: list[str],
    *,
    project_root: Path | None = None,
    contract_surface_root: Path | None = None,
) -> None:
    """Write .scope.json and install the scope pre-commit hook (always) plus the
    stealth commit-msg hook (unless ``[stealth] enabled = false``)."""
    scope_file = worktree_path / ".scope.json"
    controls = _hook_contract_controls(worktree_path, contract_surface_root)
    scope_file.write_text(
        json.dumps({"scope": scope, "contract_control": controls}, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.debug("Wrote scope file: %s (%d entries)", scope_file, len(scope))

    # .scope.json is harness bookkeeping consumed by the scope pre-commit hook
    # for the run's lifetime; it must never be committed (_NEVER_COMMIT) nor
    # travel with the branch. Exclude it via the honored info/exclude so a
    # review-stage `git status` reads clean instead of forcing triage to reason
    # about a stray untracked file every time.
    add_git_excludes(worktree_path, [".scope.json"])

    hook_script = dev_support_dir() / "scope_precommit_hook.py"
    if not hook_script.exists():
        logger.warning("scope_precommit_hook.py not found at %s", hook_script)
        return

    git_dir = _resolve_worktree_git_dir(worktree_path)
    if git_dir is None:
        return

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    _write_hook(hooks_dir / "pre-commit", hook_script)
    # The commit-msg hook is stealth mode's message sanitizer — opt-out via
    # [stealth] enabled = false (scope enforcement above stays unconditional).
    from booley.dev_support.commit_msg_utils import stealth_enabled

    if stealth_enabled(project_root):
        commit_msg_hook = dev_support_dir() / "commit_msg_hook.py"
        if commit_msg_hook.exists():
            _write_hook(hooks_dir / "commit-msg", commit_msg_hook)

    # Point worktree at its own hooks dir
    git_pointer = worktree_path / ".git"
    if git_pointer.is_file():
        _set_worktree_hooks_path(worktree_path, hooks_dir.as_posix())


def _hook_contract_controls(
    worktree_path: Path, surface_root: Path | None
) -> list[str]:
    """Translate sealed surface paths for the repository receiving the hook."""
    root = surface_root or worktree_path
    try:
        from booley.ticket_board.target_contract import contract_control_paths

        controls = contract_control_paths(root)
    except (OSError, ValueError):
        logger.warning("Could not enumerate Target contract controls for %s", root)
        return []
    if worktree_path == root:
        return [path for path in controls if not path.startswith(".booley_project/")]
    prefix = ".booley_project/"
    return [path.removeprefix(prefix) for path in controls if path.startswith(prefix)]


def refresh_scope_guards(
    worktree_path: Path,
    scope: list[str],
    *,
    project_root: Path,
) -> None:
    """Refresh persisted scope and hooks before a resumed developer run."""
    if not worktree_path.is_dir():
        raise FileNotFoundError(f"ticket worktree is missing: {worktree_path}")
    _install_scope_hook(worktree_path, scope, project_root=project_root)


def _resolve_worktree_git_dir(worktree_path: Path) -> Path | None:
    """Resolve the .git directory for a worktree (file or dir)."""
    git_pointer = worktree_path / ".git"
    if git_pointer.is_file():
        content = git_pointer.read_text(encoding="utf-8").strip()
        git_dir = Path(content.split(":", 1)[1].strip())
        if not git_dir.is_absolute():
            git_dir = (worktree_path / git_dir).resolve()
        return git_dir
    if git_pointer.is_dir():
        return git_pointer
    logger.warning("Cannot locate .git for worktree %s", worktree_path)
    return None


def _write_hook(hook_path: Path, script: Path) -> None:
    """Write a git hook that delegates to a Python script."""
    hook_path.write_text(
        f'#!/bin/sh\npython "{script.as_posix()}" "$@"\n',
        encoding="utf-8",
    )
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC)
    logger.debug("Installed hook at %s -> %s", hook_path, script)


def _find_project_dir(project_root: Path) -> Path:
    """Resolve the project data directory (.booley_project/ or legacy project/)."""
    env = os.environ.get("BOOLEY_PROJECT_DIR")
    if env:
        return Path(env).resolve()
    bp = project_root / ".booley_project"
    if bp.is_dir():
        return bp
    return project_root / ".booley" / "project"


def _resync_project_hooks(project_root: Path, worktree_path: Path) -> None:
    """Copy project hooks from main repo into worktree to avoid stale copies."""
    if paired_project_repository(worktree_path) is not None:
        return
    src = _find_project_dir(project_root) / "hooks"
    dst = worktree_path / ".booley_project" / "hooks"
    if not src.is_dir():
        return
    if dst.exists():
        safe_rmtree(dst)
    shutil.copytree(src, dst)
    logger.info("Re-synced project hooks into worktree")


def _resync_booley_dir(project_root: Path, worktree_path: Path) -> None:
    """Re-copy .booley/ from main repo into worktree so scripts stay current."""
    src = project_root / ".booley"
    dst = worktree_path / ".booley"
    if not src.is_dir():
        return
    copy_booley_tree(src, dst)
    logger.info("Re-synced .booley/ into worktree")


@dataclass(frozen=True)
class _HookRun:
    """Bundles the parameters threaded through post-setup hook execution.

    ``ctx`` carries the worktree path (``ctx.worktree_path``), so it is not
    duplicated here; the remaining fields are values derived once by
    ``_run_project_hook``.
    """

    ctx: TicketContext
    project_dir: Path
    hook: Path
    ticket_file: str
    sim_flow_enabled: bool


def _run_project_hook(
    ctx: TicketContext,
    sim_flow_enabled: bool,
) -> StepResult | None:
    """Run .booley_project/hooks/post-setup if it exists.

    The hook runs as a plain subprocess — Booley is container-only
    (ADR 0028), so the process already lives inside the Session Runtime.

    Returns StepResult on failure, None on success or if hook is absent.
    """
    project_dir = _find_project_dir(ctx.project_root)
    hook_dir = project_dir / "hooks"

    # Discover hook script (try .sh, .py, then extensionless)
    hook: Path | None = None
    for suffix in (".sh", ".py", ""):
        candidate = hook_dir / f"post-setup{suffix}"
        if candidate.exists():
            hook = candidate
            break

    if hook is None:
        logger.debug("No post-setup hook in %s", hook_dir)
        return None

    # ctx.ticket_path may point to queue/ but the ticket has been moved to
    # active/ by ticket intake.  Resolve current location by slug.
    ticket_file = ""
    if ctx.ticket_path.exists():
        ticket_file = str(ctx.ticket_path)
    else:
        board_dir = ctx.ticket_path.parent.parent
        for status in ("active", "queue", "waiting", "blocked", "review", "done", "archived"):
            candidate = board_dir / status / f"{ctx.slug}.md"
            if candidate.exists():
                ticket_file = str(candidate)
                break

    run = _HookRun(ctx, project_dir, hook, ticket_file, sim_flow_enabled)
    return _run_project_hook_local(run)


def _local_hook_cmd(hook: Path) -> list[str]:
    """Resolve the command list for running a hook script locally."""
    if hook.suffix == ".py":
        return [sys.executable, str(hook)]
    if hook.suffix in {".sh", ""}:
        return [bash_bin(), str(hook)]
    return [str(hook)]


def _log_hook_output(result: subprocess.CompletedProcess) -> None:
    """Log truncated stdout/stderr from a hook subprocess."""
    if result.stdout.strip():
        logger.debug("Hook stdout: %s", result.stdout.strip()[:500])
    if result.stderr.strip():
        logger.debug("Hook stderr: %s", result.stderr.strip()[:500])


def _run_project_hook_local(run: _HookRun) -> StepResult | None:
    """Run the project hook on the host."""
    ctx = run.ctx
    worktree_path = ctx.worktree_path
    paired = paired_project_repository(worktree_path)
    project_content_dir = paired.worktree if paired is not None else run.project_dir
    env = {
        **os.environ,
        "BOOLEY_WORKTREE": str(worktree_path),
        "BOOLEY_PROJECT_DIR": str(project_content_dir),
        "BOOLEY_PROJECT_ROOT": str(ctx.project_root),
        "BOOLEY_TICKET_SLUG": ctx.slug,
        "BOOLEY_TICKET_FILE": run.ticket_file,
        "BOOLEY_SIM_FLOW_ENABLED": "1" if run.sim_flow_enabled else "0",
        "BOOLEY_IN_DOCKER": "",
    }

    logger.debug("Running post-setup hook: %s", run.hook)
    try:
        result = subprocess.run(
            _local_hook_cmd(run.hook),
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return StepResult(block_reason="post-setup hook timed out (15 min)")
    except OSError as exc:
        return StepResult(block_reason=f"post-setup hook failed (OS error): {exc}")

    _log_hook_output(result)
    if result.returncode != 0:
        return StepResult(
            block_reason=f"post-setup hook failed (rc={result.returncode}): "
            f"{result.stderr.strip()[:500]}"
        )

    logger.info("post-setup hook OK")
    return None


def _remove_stale_worktree(project_root: Path, worktree_path: Path) -> None:
    """Force-remove a worktree and prune dangling refs."""
    from .project_worktree import remove_project_worktree

    remove_project_worktree(project_root, worktree_path)
    subprocess.run(
        ["git", "worktree", "remove", str(worktree_path), "--force"],
        cwd=project_root,
        capture_output=True,
        check=False,
    )
    try:
        safe_rmtree(worktree_path)
    except (OSError, ValueError):
        logger.warning("Could not fully remove stale worktree %s", worktree_path)
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=project_root,
        capture_output=True,
        check=False,
    )


def _try_reuse_worktree(
    ctx: TicketContext,
    project_root: Path,
    expected_wt: Path,
) -> bool:
    """Reuse existing worktree if it looks complete; return True on success."""
    if not expected_wt.exists():
        return False

    health = check_worktree_health(project_root, expected_wt)
    if not health.ok:
        logger.warning(
            "Stale/corrupt worktree at %s (%s) -- removing",
            expected_wt,
            health.reason,
        )
        _remove_stale_worktree(project_root, expected_wt)
        return False

    # Validate the worktree is functional — a stale/corrupt .git file
    # from an interrupted run would pass .exists() but fail git ops.
    probe = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=expected_wt,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if probe.returncode != 0:
        logger.warning("Stale/corrupt worktree at %s — removing", expected_wt)
        _remove_stale_worktree(project_root, expected_wt)
        return False

    logger.info("Worktree reused")
    ctx.worktree_path = expected_wt
    try:
        _resync_booley_dir(project_root, expected_wt)
        _resync_project_hooks(project_root, expected_wt)
    except OSError as exc:
        # Resync failed — tear down and fall through to fresh creation.
        logger.warning("Resync into reused worktree failed (%s) — recreating", exc)
        import contextlib

        _remove_stale_worktree(project_root, expected_wt)
        with contextlib.suppress(OSError, ValueError):
            safe_rmtree(expected_wt)
        return False

    return True


def _crlf_safe_script(script: Path) -> Path:
    """Return a path to *script* whose line endings bash can parse.

    A package built from a CRLF checkout ships scripts that fail under bash
    with ``$'\\r': command not found`` (F-12), and the installed tree may be
    root-owned — so instead of repairing in place, normalize into a temp copy
    and run that. LF scripts (the normal case) are returned untouched.
    """
    try:
        data = script.read_bytes()
    except OSError:
        return script
    if b"\r" not in data:
        return script
    import tempfile

    fd, tmp_name = tempfile.mkstemp(prefix=f"booley_{script.stem}_", suffix=".sh")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data.replace(b"\r\n", b"\n"))
    logger.warning(
        "%s has CRLF line endings (F-12) — running an LF-normalized temp copy: %s",
        script.name,
        tmp_name,
    )
    return Path(tmp_name)


def _create_fresh_worktree(
    ctx: TicketContext,
    expected_wt: Path,
) -> StepResult | None:
    """Create worktree via shell script; return StepResult on failure."""
    wt_script = dev_support_dir() / "worktree_create.sh"
    if not wt_script.exists():
        return StepResult(block_reason=f"Worktree script not found: {wt_script}")
    wt_script = _crlf_safe_script(wt_script)

    hook_input = json.dumps({"name": ctx.slug, "cwd": str(ctx.project_root)})
    env = {**os.environ}
    # Pin the script's Python to our own interpreter — a Windows host may have
    # no runnable python3/python on the shell's PATH, only the Microsoft Store
    # aliases (F-7).
    env.setdefault("BOOLEY_PYTHON", sys.executable)

    logger.debug("Creating worktree for %s...", ctx.slug)
    try:
        result = subprocess.run(
            [bash_bin(), str(wt_script)],
            input=hook_input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return StepResult(block_reason="Worktree creation timed out (5 min)")
    except OSError as exc:
        return StepResult(block_reason=f"Worktree creation failed (OS error): {exc}")

    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip() or "(no output)"
        logger.debug(
            "worktree script failed (rc=%d) stdout=%r stderr=%r",
            result.returncode,
            result.stdout,
            result.stderr,
        )
        return StepResult(block_reason=f"Worktree creation failed (rc={result.returncode}): {err}")

    # Use expected_wt (native Python path) instead of parsing stdout —
    # bash outputs POSIX paths (/c/...) which don't resolve on Windows.
    if not expected_wt.exists():
        return StepResult(
            block_reason=f"Worktree path doesn't exist after creation: {expected_wt}"
        )

    ctx.worktree_path = expected_wt
    return None


def _ensure_base_branch(
    worktree_path: Path,
    base_ref: str,
) -> StepResult | None:
    """Ensure worktree HEAD matches the target base branch."""
    head_result = git_run(worktree_path, ["rev-parse", "HEAD"], timeout=10)
    if head_result.returncode != 0 or not head_result.stdout.strip():
        return StepResult(
            block_reason=f"git rev-parse HEAD failed in worktree "
            f"(rc={head_result.returncode}): {head_result.stderr.strip()}"
        )
    current_head = head_result.stdout.strip()

    base_result = git_run(worktree_path, ["rev-parse", base_ref], timeout=10)
    if base_result.returncode != 0 or not base_result.stdout.strip():
        return StepResult(
            block_reason=f"git rev-parse {base_ref!r} failed in worktree "
            f"(rc={base_result.returncode}): {base_result.stderr.strip()}"
        )
    base_target = base_result.stdout.strip()

    if current_head != base_target:
        logger.debug(
            "Resetting worktree from %s to %s (%s)",
            current_head[:8],
            base_target[:8],
            base_ref,
        )
        git_run(worktree_path, ["checkout", "--detach", base_target], timeout=30)

    return None


def _create_feature_branch(
    ctx: TicketContext,
    worktree_path: Path,
    base_ref: str,
) -> StepResult | None:
    """Create feature branch (or checkout integration branch)."""
    if ctx.is_integration:
        branch_name = ctx.branch
        logger.info("Integration branch: %s", branch_name)
        git_run(worktree_path, ["checkout", branch_name], timeout=30)
    else:
        branch_name = ctx.slug
        logger.info("Feature branch from %s", base_ref)
        existing = git_run(
            worktree_path,
            ["rev-parse", "--verify", f"refs/heads/{branch_name}"],
            timeout=10,
        )
        if existing.returncode == 0 and existing.stdout.strip():
            base = git_run(worktree_path, ["rev-parse", base_ref], timeout=10)
            if base.returncode != 0 or existing.stdout.strip() != base.stdout.strip():
                return StepResult(
                    block_reason=(
                        f"Refusing to reset surviving feature branch {branch_name!r} "
                        f"to {base_ref!r}: it points at {existing.stdout.strip()[:12]} "
                        "and may contain preserved unmerged work. Move, merge, or "
                        "delete that branch explicitly before rerunning the ticket."
                    )
                )
        # -B is safe here: an existing branch was proven byte-for-byte equal
        # to the requested base.  A divergent branch is preserved above.
        result = git_run(worktree_path, ["checkout", "-B", branch_name], timeout=30)
        if result.returncode != 0:
            return StepResult(
                block_reason=f"Failed to create/checkout branch {branch_name}: {result.stderr}"
            )

    ctx.feature_branch = branch_name
    return None


def _resume_branch_name(ctx: TicketContext) -> str:
    """Return the branch that owns a resumed ticket's preserved work."""
    if ctx.is_integration:
        return ctx.branch
    return ctx.feature_branch or ctx.slug


def _branch_exists(worktree_path: Path, branch_name: str) -> bool:
    """Return whether a local branch exists in the worktree's repository."""
    result = git_run(
        worktree_path,
        ["rev-parse", "--verify", f"refs/heads/{branch_name}"],
        timeout=10,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _checkout_resume_branch(
    ctx: TicketContext,
    worktree_path: Path,
    branch_name: str,
) -> StepResult | None:
    """Attach a resumed ticket worktree to its preserved branch."""
    logger.info("Resuming preserved branch %s", branch_name)
    result = git_run(worktree_path, ["checkout", branch_name], timeout=30)
    if result.returncode != 0:
        return StepResult(
            block_reason=(
                f"Failed to attach preserved branch {branch_name!r}: {result.stderr.strip()}"
            )
        )
    ctx.feature_branch = branch_name
    return None


def _prepare_branch(
    ctx: TicketContext,
    worktree_path: Path,
    base_ref: str,
) -> StepResult | None:
    """Attach preserved work on resume, otherwise create a fresh branch."""
    resume_branch = _resume_branch_name(ctx)
    if ctx.workspace_intent == "resume" and _branch_exists(worktree_path, resume_branch):
        return _checkout_resume_branch(ctx, worktree_path, resume_branch)
    return _ensure_base_branch(worktree_path, base_ref) or _create_feature_branch(
        ctx, worktree_path, base_ref
    )


def _load_flow_enablement(project_root: Path | None = None) -> tuple[bool, bool]:
    """Load Simulation and ASIC Synthesis Flow enablement from project config.

    Pass *project_root* so the config is read from that project rather than
    the CWD cache; otherwise harness runs that span multiple projects in one
    process can mix configs.
    """
    from booley.flows.execution import resolve_execution

    sim_flow_enabled = resolve_execution("sim", project_root).enabled
    synth_flow_enabled = resolve_execution("synth", project_root).enabled
    return sim_flow_enabled, synth_flow_enabled


def _commit_hook_files(ctx: TicketContext) -> None:
    """Commit any files the post-setup hook created/modified (host-side)."""
    worktree_path = ctx.worktree_path
    files_to_stage = _discover_hook_files(worktree_path)
    if not files_to_stage:
        return

    add_result = git_run(worktree_path, ["add", "--", *files_to_stage], timeout=30)
    if add_result.returncode != 0:
        logger.warning(
            "git add failed (rc=%d): %s", add_result.returncode, add_result.stderr.strip()
        )

    diff_result = git_run(worktree_path, ["diff", "--cached", "--quiet"], timeout=10)
    if diff_result.returncode == 0:
        return  # nothing staged

    # Guard: refuse to commit in main worktree (Principle 3)
    if (worktree_path / ".git").is_dir():
        logger.error("_commit_hook_files called on main worktree (%s) — refusing", worktree_path)
        return

    commit_msg = f"feat({ctx.slug}): post-setup hook files"
    errors = validate_message(commit_msg)
    if errors:
        logger.warning("Commit message validation failed: %s", "; ".join(errors))
        return

    commit_result = git_run(worktree_path, ["commit", "--no-verify", "-m", commit_msg], timeout=30)
    if commit_result.returncode != 0:
        logger.warning(
            "Post-setup commit failed (rc=%d): %s",
            commit_result.returncode,
            (commit_result.stdout + commit_result.stderr).strip()[:300],
        )
    else:
        logger.debug("Committed post-setup hook files on feature branch")


_NEVER_COMMIT = {".scope.json"}


def _discover_hook_files(worktree_path: Path) -> list[str]:
    """Discover modified/untracked files from the post-setup hook."""
    status_result = git_run(
        worktree_path, ["status", "--porcelain", "--ignore-submodules"], timeout=30
    )
    if status_result.returncode != 0:
        logger.warning(
            "git status failed (rc=%d): %s", status_result.returncode, status_result.stderr.strip()
        )
        return []
    return [
        line[3:].strip()
        for line in status_result.stdout.splitlines()
        if len(line) > 3 and line[3:].strip() and line[3:].strip() not in _NEVER_COMMIT
    ]


def _verify_project_paths(
    worktree_path: Path,
    synth_flow_enabled: bool,
    needs_synth: bool,
) -> StepResult | None:
    """Run path checks required by the Booley Flows this Ticket uses."""
    if needs_synth and synth_flow_enabled:
        try:
            from booley.runtime.shared_infra import get_syn_output_dir

            syn_dir = get_syn_output_dir(worktree_path)
        except Exception:  # noqa: BLE001 — fall back to the conventional syn output dir when lookup fails
            syn_dir = worktree_path / "util" / "syn"
        syn_dir.mkdir(parents=True, exist_ok=True)

    check_scripts: list[str] = []
    if needs_synth and synth_flow_enabled:
        check_scripts.append("yosys/run_yosys_syn.py")

    for script_rel in check_scripts:
        script = worktree_path / ".booley" / "src" / script_rel
        if not script.exists():
            continue
        check = subprocess.run(
            [sys.executable, str(script), "check-paths"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(worktree_path),
            timeout=30,
            check=False,
        )
        if check.returncode != 0:
            output = (check.stdout + check.stderr).strip()
            return StepResult(block_reason=f"{script_rel} check-paths failed: {output[:300]}")

    return None


def _freeze_synth_baseline(ctx: TicketContext) -> StepResult | None:
    """Freeze synthesis baseline SHA and include it in step metadata."""
    if not ctx.has_synth:
        return None

    if ctx.base_sha:
        ctx._synth_baseline_sha = ctx.base_sha
        logger.debug("Using ticket synthesis baseline SHA: %s", ctx.base_sha[:12])
        return None

    base_sha_result = subprocess.run(
        ["git", "rev-parse", ctx.branch],
        cwd=str(ctx.project_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    if base_sha_result.returncode != 0:
        return StepResult(
            block_reason=f"Failed to resolve base branch {ctx.branch!r} to SHA: "
            f"{base_sha_result.stderr.strip()}"
        )
    base_sha = base_sha_result.stdout.strip()
    if not base_sha:
        return StepResult(block_reason=f"git rev-parse {ctx.branch} returned empty output")

    logger.debug("Froze synthesis baseline SHA: %s (%s)", base_sha[:12], ctx.branch)
    # SHA stored in StepResult.metadata — caller writes to step-meta.json
    ctx._synth_baseline_sha = base_sha
    return None


def _build_setup_result(ctx: TicketContext) -> StepResult:
    """Build the final StepResult with worktree metadata."""
    meta = {"worktree": str(ctx.worktree_path), "branch": ctx.feature_branch}
    if getattr(ctx, "_synth_baseline_sha", None):
        meta["synthesis_baseline_sha"] = ctx._synth_baseline_sha
    return StepResult(metadata=meta)


async def run(ctx: TicketContext) -> StepResult:
    """Create isolated worktree and set up feature branch."""
    project_root = ctx.project_root

    # Clean up locks from dead processes before attempting worktree creation.
    _prune_stale_worktree_locks(project_root)

    # Worktree: reuse or create fresh
    expected_wt = (
        resolve_project_dir(project_root) / "worktrees" / ctx.slug
        if ctx.target_contract is not None
        else project_root / ".booley_project" / "worktrees" / ctx.slug
    )
    if not _try_reuse_worktree(ctx, project_root, expected_wt):
        fail = _create_fresh_worktree(ctx, expected_wt)
        if fail:
            return fail

    worktree_path = ctx.worktree_path
    base_ref = (
        ctx.target_contract.outer_sha if ctx.target_contract is not None else ctx.branch
    )
    logger.info("Worktree ready")

    # Branch setup: ensure base, create feature branch
    fail = _prepare_branch(ctx, worktree_path, base_ref)
    if fail:
        return fail

    from .project_worktree import ProjectWorktreeError, prepare_project_worktree

    try:
        project_worktree = prepare_project_worktree(ctx)
    except ProjectWorktreeError as exc:
        return StepResult(block_reason=f"Project worktree setup failed: {exc}")

    _install_scope_hook(worktree_path, ctx.scope, project_root=project_root)
    if project_worktree is not None:
        _install_scope_hook(
            project_worktree,
            project_repository_scope(ctx.scope_raw),
            project_root=project_root,
            contract_surface_root=worktree_path,
        )
    sim_flow_enabled, synth_flow_enabled = _load_flow_enablement(project_root)

    # Project hook + commit any files it staged
    hook_fail = _run_project_hook(ctx, sim_flow_enabled)
    if hook_fail is not None:
        return hook_fail
    _commit_hook_files(ctx)

    # Final verification and baseline freeze
    fail = _verify_project_paths(
        worktree_path, synth_flow_enabled, ctx.has_synth
    ) or _freeze_synth_baseline(ctx)
    if fail:
        return fail

    return _build_setup_result(ctx)
