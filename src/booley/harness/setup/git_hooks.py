"""Git commit-msg hook generation and installation for ``booley init``.

Extracted from ``init_cmd.py`` (Single Responsibility): the ``git_hooks`` step
installs the leak guard into the project-agnostic ``.booley/`` repo, and the
``project_git_hooks`` step vendors the commit-message sanitizer scripts into
``.booley_project/hooks/`` and installs a repo-relative ``commit-msg``
delegator into the project's own ``.git/hooks/``. Steps are named by their
record key, never by a display number — the banner numbers are allocated at
print time from the steps that actually run (see :meth:`InitContext.step_banner`),
so a hardcoded display number in a comment drifts the moment a step is
added or skipped (fpu F-12).

Depends only on ``init_common`` for console output and :class:`InitContext`;
it never imports back from ``init_cmd``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from booley.harness.setup.common import (
    InitContext,
    WriteOutcome,
    guarded_write,
    info,
    note,
    ok,
    skip,
    warn,
)
from booley.harness.setup.line_endings import (
    GITATTRIBUTES_RULE,
    LineEndingActionKind,
    LineEndingActionResult,
    LineEndingActionState,
    LineEndingMode,
    LineEndingObservationCode,
    LineEndingStatus,
    RepositoryLineEndingReport,
    line_ending_repository_display,
    reconcile_project_line_endings,
)
from booley.runtime.paths import dev_support_dir
from booley.runtime.project_dir import resolve_project_dir


def _step_git_hooks(ctx: InitContext) -> None:
    ctx.step_banner("git hooks")

    project_dir = resolve_project_dir(ctx.project_root)
    guard_sh = project_dir / "hooks" / "booley_commit_guard.sh"
    if not guard_sh.is_file():
        # Only Booley's own dev repo ships this repo-level guard; a normal
        # project repo has none, so there is nothing to install — expected, not
        # a failure (SETUP-4). The project commit-msg hook is a separate step.
        skip(
            "no repo-level commit guard (booley_commit_guard.sh) to install — "
            "expected for a project repo; the project commit-msg hook installs "
            "separately below"
        )
        ctx.record("git_hooks", "skip", "no repo-level guard (expected)")
        return

    # Find the .booley/.git/hooks/ target
    # For pip-installed booley, the git repo is wherever the package is installed from
    # Try common locations
    booley_git_hooks: Path | None = None
    for candidate in [
        ctx.project_root / ".booley" / ".git" / "hooks",
    ]:
        if candidate.parent.is_dir():
            booley_git_hooks = candidate
            break

    if booley_git_hooks is None:
        skip("no .booley/.git/ found — git hooks skipped")
        ctx.record("git_hooks", "skip", "no .booley git dir")
        return

    hook_target = booley_git_hooks / "commit-msg"

    guard_body = guard_sh.read_text(encoding="utf-8")
    # The content sniff that marks an installed hook as ours. A guard script
    # that never names itself degrades to create-only: installed once, never
    # refreshed, and an existing hook is left alone.
    marker = "booley_commit_guard" if "booley_commit_guard" in guard_body else None

    outcome = guarded_write(
        hook_target,
        guard_body,
        owner_marker=marker,
        dry_run=ctx.check_only,
        newline="\n",
        executable=True,
    )
    if outcome in (WriteOutcome.UNCHANGED, WriteOutcome.SKIPPED):
        skip("commit-msg hook already installed")
        ctx.record("git_hooks", "skip", "already installed")
        return
    if outcome is WriteOutcome.REFUSED:
        warn("existing .booley/.git/hooks/commit-msg is not booley's — leaving it untouched")
        ctx.record("git_hooks", "warn", "foreign hook preserved")
        return
    if ctx.check_only:
        warn("would install commit-msg hook")
        ctx.record("git_hooks", "warn", "would install")
        return

    ok("commit-msg hook installed in .booley/.git/hooks/")
    ctx.record("git_hooks", "ok", "installed")


# Utility scripts that make up the self-contained project git hooks.
# commit_msg_hook.py and pre_push_hook.py import the utils by bare name,
# resolving them via their own directory — so all must sit side-by-side in
# the project repo.
# (run_command.py is vendored alongside too — see _step_project_git_hooks — but it
# lives in core/, not dev_support/, so it isn't listed here.)
_PROJECT_HOOK_SCRIPTS = (
    "commit_msg_hook.py",
    "commit_msg_utils.py",
    "validate_commit_msg.py",
    "pre_push_hook.py",
)

_PROJECT_HOOK_HELPERS = {
    "boundary.py": Path("core") / "boundary.py",
    "checkout_role.py": Path("runtime") / "checkout_role.py",
    "run_command.py": Path("core") / "run_command.py",
}


def _build_hook_delegator_body(
    project_root: Path,
    hooks_dst: Path,
    script_name: str,
    purpose: str,
    *,
    fail_open: bool,
) -> str:
    """Build a delegator shell script for a vendored Python hook.

    References the vendored script repo-relatively so the hook works on any
    clone regardless of where the project lives. Falls back to an absolute
    path if the project data dir is somehow outside the repo.

    The script may legitimately be absent from ``$ROOT``: a user-made secondary
    worktree (``git worktree add``) has its own toplevel, and
    ``.booley_project/`` is untracked/git-ignored, so it exists only in the main
    worktree. ``exec``ing the missing path made EVERY commit in such a worktree
    fail with ENOENT (fpu F-42). The body therefore always resolves through the
    shared git dir first, so the hook genuinely RUNS in a secondary worktree.

    Only the last-resort fallback differs per hook, and *fail_open* picks it:

    - ``True`` (commit-msg): skip with one explanatory line. The sanitizer is a
      convenience; a missing script must never wedge local committing (F-42).
    - ``False`` (pre-push): refuse the push. That hook exists for one reason —
      to block — so treating "I could not check" as "nothing to report" is the
      one answer it must never give. ``.booley_project/`` is git-ignored, so a
      ``git clean -xdf`` deletes the vendored script while ``.git/hooks/``
      survives; fail-open there would wave every subsequent push past the
      ``[stealth]`` banned-term scan and the ``allowed_authors`` allowlist.
    """
    try:
        script_rel = f"{(hooks_dst.relative_to(project_root)).as_posix()}/{script_name}"
        resolve = (
            f'SCRIPT="$ROOT/{script_rel}"\n'
            'if [ ! -f "$SCRIPT" ]; then\n'
            "    # Secondary worktree (F-42): .booley_project/ is untracked, so it\n"
            "    # exists only in the MAIN worktree — reach it via the shared git dir.\n"
            '    COMMON=$(cd "$ROOT" && git rev-parse --path-format=absolute '
            "--git-common-dir 2>/dev/null) || COMMON=\n"
            '    case "$COMMON" in\n'
            "        '') ;;\n"
            f'        /*) SCRIPT="$(dirname "$COMMON")/{script_rel}" ;;\n'
            f'        [A-Za-z]:/*) SCRIPT="$(dirname "$COMMON")/{script_rel}" ;;\n'
            f'        *) SCRIPT="$(dirname "$ROOT/$COMMON")/{script_rel}" ;;\n'
            "    esac\n"
            "fi\n"
        )
    except ValueError:
        resolve = f'SCRIPT="{(hooks_dst / script_name).as_posix()}"\n'
    if fail_open:
        # A hook whose script is genuinely unavailable must SKIP, not ENOENT-fail
        # every commit in the worktree (F-42).
        missing = (
            f"    echo 'booley {script_name}: vendored hook script not found"
            " — skipping (run `booley init` to reinstall)' >&2\n"
            "    exit 0\n"
        )
    else:
        # ENOENT would also have blocked, but only by accident and with a
        # baffling message. Block on purpose, and say how to get unblocked.
        missing = (
            f"    echo 'booley {script_name}: vendored hook script not found at'"
            ' "$SCRIPT" >&2\n'
            "    echo 'This is the leak guard — it cannot pass what it could not"
            " check, so the push is REFUSED.' >&2\n"
            "    echo 'Restore it with `booley init` (it is git-ignored, so"
            " `git clean -xdf` removes it).' >&2\n"
            "    exit 1\n"
        )
    resolve += 'if [ ! -f "$SCRIPT" ]; then\n' + missing + "fi\n"
    script_ref = '"$SCRIPT"'

    return (
        "#!/bin/sh\n"
        f"# Booley {purpose}. Self-contained: delegates to\n"
        "# the copy vendored under the project's hooks dir, located repo-\n"
        "# relatively so it needs no Booley source checkout.\n"
        # Same fail-open/fail-closed split as the missing-script branch below:
        # a blocking guard that cannot even locate the repo must not report "ok".
        f"ROOT=$(git rev-parse --show-toplevel) || exit {0 if fail_open else 1}\n"
        + resolve
        + "# Interpreter ladder (F-7): stock Windows has no python3.exe — only the\n"
        "# Microsoft Store alias, which prints a Store nag and exits non-zero,\n"
        "# failing EVERY commit. `command -v` alone can't tell the alias from a\n"
        "# real interpreter, so each candidate must actually run `-c ''`.\n"
        "PY=\n"
        "for cand in python3 python; do\n"
        "    if \"$cand\" -c '' >/dev/null 2>&1; then PY=$cand; break; fi\n"
        "done\n"
        "if [ -z \"$PY\" ] && py -3 -c '' >/dev/null 2>&1; then PY='py -3'; fi\n"
        'if [ -z "$PY" ]; then\n'
        f"    echo 'booley {script_name}: no usable Python found (tried python3, python, py -3)' >&2\n"
        "    exit 1\n"
        "fi\n"
        f'exec $PY {script_ref} "$@"\n'
    )


def _build_commit_msg_hook_body(project_root: Path, hooks_dst: Path) -> str:
    """Build the commit-msg delegator shell script for the project repo."""
    return _build_hook_delegator_body(
        project_root,
        hooks_dst,
        "commit_msg_hook.py",
        "commit-msg hook (sanitize + validate) — strips AI/tooling\n"
        "# attribution (Co-Authored-By, claude, generated, ...) and project-\n"
        "# internal terms from commit messages",
        # A sanitizer that cannot run is an inconvenience; a commit that cannot
        # be made is a wedge. Skip (F-42).
        fail_open=True,
    )


def _build_pre_push_hook_body(project_root: Path, hooks_dst: Path) -> str:
    """Build the pre-push delegator shell script for the project repo.

    The pre-push leak guard closes the F-17 hole: ``git revert`` and
    ``git commit --no-verify`` bypass the commit-msg sanitizer entirely, so
    banned terms are re-checked on every outgoing commit at push time. It also
    enforces the ``[stealth] allowed_authors`` identity allowlist, which
    commit-msg structurally cannot: git hands that hook only the message file,
    so ``git commit --author=...`` never reaches it.
    """
    return _build_hook_delegator_body(
        project_root,
        hooks_dst,
        "pre_push_hook.py",
        "pre-push hook (leak guard, F-17) — blocks pushes whose\n"
        "# outgoing commits carry banned terms, or an author/committer\n"
        "# identity outside [stealth] allowed_authors (git revert,\n"
        "# --author and --no-verify all bypass the commit-msg sanitizer)",
        # The guard's whole job is blocking, so an unrunnable guard must block.
        # Fail-open here silently re-opens F-17 the first time `git clean -xdf`
        # takes the git-ignored .booley_project/ with it.
        fail_open=False,
    )


def _step_project_git_hooks(ctx: InitContext) -> None:
    """Install the commit-msg sanitizer + pre-push leak guard.

    The ``git_hooks`` step installs the leak guard into the project-agnostic
    .booley/ repo.
    This step covers the repo the user actually commits from: it vendors the
    sanitizer scripts into .booley_project/hooks/ and installs repo-relative
    commit-msg and pre-push delegators into the project's own .git/hooks/.
    The pre-push guard re-checks outgoing commits because ``git revert`` and
    ``--no-verify`` bypass commit-msg entirely (F-17).

    Both the scripts and the hook are local to this machine (git hooks never
    travel with a clone, and .booley_project/ is commonly git-ignored) — they
    are reproduced on every machine by `booley init`. Source-independence
    comes from copying out of the installed booley package (dev_support_dir()), not
    from any Booley source checkout, so end users need only the pip package.
    """
    ctx.step_banner("project commit-msg hook")

    project_dir = resolve_project_dir(ctx.project_root)
    hooks_dst = project_dir / "hooks"
    src_dir = dev_support_dir()
    missing = [s for s in _PROJECT_HOOK_SCRIPTS if not (src_dir / s).is_file()]
    helper_sources = {
        name: src_dir.parent / relative for name, relative in _PROJECT_HOOK_HELPERS.items()
    }
    missing.extend(name for name, source in helper_sources.items() if not source.is_file())
    if missing:
        skip(f"sanitizer scripts not found in developer-support dir: {', '.join(missing)}")
        ctx.record("project_git_hooks", "skip", "sanitizer scripts missing")
        return

    # Locate the project repo's hooks dir (handles worktrees, where .git is a
    # file pointing elsewhere) via git itself.
    proc = subprocess.run(
        ["git", "-C", str(ctx.project_root), "rev-parse", "--git-path", "hooks"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        skip("project root is not a git repo — project commit-msg hook skipped")
        ctx.record("project_git_hooks", "skip", "not a git repo")
        return
    hooks_dir = (ctx.project_root / proc.stdout.strip()).resolve()

    # (hook filename, delegated vendored script, delegator body)
    hook_installs = [
        (
            "commit-msg",
            "commit_msg_hook.py",
            _build_commit_msg_hook_body(ctx.project_root, hooks_dst),
        ),
        ("pre-push", "pre_push_hook.py", _build_pre_push_hook_body(ctx.project_root, hooks_dst)),
    ]

    if ctx.check_only:
        warn("would vendor sanitizer scripts and install project commit-msg and pre-push hooks")
        ctx.record("project_git_hooks", "warn", "would install")
        return

    hooks_dst.mkdir(parents=True, exist_ok=True)
    for name in _PROJECT_HOOK_SCRIPTS:
        shutil.copy2(str(src_dir / name), str(hooks_dst / name))
    for name, source in helper_sources.items():
        shutil.copy2(str(source), str(hooks_dst / name))

    hooks_dir.mkdir(parents=True, exist_ok=True)
    for hook_name, script_name, hook_body in hook_installs:
        hook_target = hooks_dir / hook_name

        # A hook referencing the vendored script is ours (refreshed in place);
        # a pre-existing non-Booley hook is backed up, never silently clobbered.
        # newline="\n" is load-bearing: the default (newline=None) applies OS line
        # translation, so on a Windows host `#!/bin/sh\n` lands on disk as
        # `#!/bin/sh\r\n`. The Session Runtime container then tries to exec an
        # interpreter literally named "/bin/sh\r", which does not exist — an ENOENT
        # that fails EVERY in-container commit and misleadingly names the hook, not
        # the interpreter (QA_REPORT D0a). A shell script must always be LF.
        outcome = guarded_write(
            hook_target,
            hook_body,
            owner_marker=script_name,
            backup_suffix=".pre-booley",
            newline="\n",
            executable=True,
        )
        if outcome is WriteOutcome.BACKED_UP:
            warn(f"backed up existing {hook_name} hook to {hook_name}.pre-booley")

    ok("project commit-msg + pre-push hooks installed (vendored into .booley_project/hooks/)")
    ctx.record("project_git_hooks", "ok", "installed")


_OBSERVATION_MESSAGES = {
    LineEndingObservationCode.CRLF_MISMATCH: (
        "{count} tracked file(s) are checked out with CRLF — the Session Runtime container "
        "will see every one as modified (phantom diffs break the dirty-tree check, scope "
        "enforcement, and ticket worktrees)"
    ),
    LineEndingObservationCode.AUTOCRLF_EFFECTIVE_TRUE: (
        "core.autocrlf=true (Git for Windows' installer default) re-creates CRLF checkouts "
        "on every clone/checkout"
    ),
    LineEndingObservationCode.AUTOCRLF_NOT_PINNED: (
        "core.autocrlf is not pinned false in this repository"
    ),
}

_ACTION_LINES = {
    (LineEndingActionState.COMPLETED, LineEndingActionKind.PIN_AUTOCRLF): (
        (ok, "core.autocrlf=false (repo-local; CRLF will not come back on checkout)"),
    ),
    (LineEndingActionState.COMPLETED, LineEndingActionKind.NORMALIZE_FILES): (
        (note, "detected {count} tracked file(s) are checked out with CRLF"),
        (ok, "normalized {count} file(s) to LF atomically — tree is container-safe"),
    ),
    (LineEndingActionState.COMPLETED, LineEndingActionKind.REFRESH_INDEX): (
        (ok, "refreshed stale Git index metadata for {count} tracked file(s)"),
    ),
    (LineEndingActionState.COMPLETED, LineEndingActionKind.PUBLISH_ATTRIBUTES): (
        (ok, "added '{rule}' as the first line of .gitattributes"),
        (info, "  commit it — the rule only travels to your team through git"),
    ),
    (LineEndingActionState.PLANNED, LineEndingActionKind.PIN_AUTOCRLF): (
        (info, "  would set core.autocrlf=false"),
    ),
    (LineEndingActionState.PLANNED, LineEndingActionKind.NORMALIZE_FILES): (
        (info, "  would normalize {count} tracked file(s) to LF atomically"),
    ),
    (LineEndingActionState.PLANNED, LineEndingActionKind.REFRESH_INDEX): (
        (warn, "would refresh stale Git index metadata for {count} tracked file(s)"),
    ),
    (LineEndingActionState.PLANNED, LineEndingActionKind.PUBLISH_ATTRIBUTES): (
        (info, "  would add '{rule}' to .gitattributes"),
    ),
}

_FAILED_ACTION_DETAILS = {
    LineEndingActionKind.PIN_AUTOCRLF: "autocrlf update failed",
    LineEndingActionKind.NORMALIZE_FILES: "normalization failed",
    LineEndingActionKind.REFRESH_INDEX: "normalization failed",
    LineEndingActionKind.PUBLISH_ATTRIBUTES: "normalization failed",
}

_OBSERVATION_RESULT_DETAILS = (
    (LineEndingObservationCode.CRLF_MISMATCH, "CRLF working tree"),
    (LineEndingObservationCode.AUTOCRLF_EFFECTIVE_TRUE, "autocrlf policy unsafe"),
    (LineEndingObservationCode.AUTOCRLF_NOT_PINNED, "autocrlf policy unsafe"),
    (LineEndingObservationCode.STALE_INDEX, "stale index metadata"),
)

_COMPLETED_ACTION_DETAILS = (
    (LineEndingActionKind.NORMALIZE_FILES, "normalized"),
    (LineEndingActionKind.REFRESH_INDEX, "index refreshed"),
)


def _render_line_ending_observations(report: RepositoryLineEndingReport) -> None:
    for observation in report.observations:
        template = _OBSERVATION_MESSAGES.get(observation.code)
        message = template.format(count=observation.count) if template else observation.detail
        if message:
            warn(message)


def _render_line_ending_action(action: LineEndingActionResult) -> None:
    lines = _ACTION_LINES.get((action.state, action.kind))
    if lines is not None:
        if (
            action.kind is LineEndingActionKind.PIN_AUTOCRLF
            and action.state is LineEndingActionState.COMPLETED
            and action.detail == "effective true"
        ):
            note("detected core.autocrlf=true (Git for Windows' installer default)")
        for emit, template in lines:
            emit(template.format(count=action.count, rule=GITATTRIBUTES_RULE))
    elif action.detail:
        prefix = (
            "would leave tracked files untouched: "
            if action.state is LineEndingActionState.REFUSED
            else ""
        )
        warn(f"{prefix}{action.detail}")


def _unreadable_line_ending_detail(
    report: RepositoryLineEndingReport,
    codes: set[LineEndingObservationCode],
) -> str | None:
    fixed_details = (
        (LineEndingObservationCode.AUTOCRLF_UNREADABLE, "autocrlf unreadable"),
        (LineEndingObservationCode.LOCAL_AUTOCRLF_UNREADABLE, "local autocrlf unreadable"),
        (LineEndingObservationCode.STATUS_UNREADABLE, "status comparison unreadable"),
    )
    for code, detail in fixed_details:
        if code in codes:
            return detail
    if LineEndingObservationCode.EOL_SCAN_UNREADABLE in codes:
        normalized = any(
            action.kind is LineEndingActionKind.NORMALIZE_FILES
            and action.state is LineEndingActionState.COMPLETED
            for action in report.actions
        )
        return "EOL verification unreadable" if normalized else "EOL scan unreadable"
    return None


def _line_ending_result_detail(report: RepositoryLineEndingReport) -> str:
    codes = {observation.code for observation in report.observations}
    unreadable = _unreadable_line_ending_detail(report, codes)
    failed = next(
        (action for action in report.actions if action.state is LineEndingActionState.FAILED),
        None,
    )
    refused = next(
        (action for action in report.actions if action.state is LineEndingActionState.REFUSED),
        None,
    )
    if unreadable is not None:
        detail = unreadable
    elif failed:
        detail = _FAILED_ACTION_DETAILS[failed.kind]
    elif refused and refused.kind is LineEndingActionKind.NORMALIZE_FILES:
        detail = refused.detail or "candidate unsafe"
        detail = "dirty tree" if detail.startswith("working tree has") else "candidate unsafe"
    elif report.actions:
        observation_detail = next(
            (detail for code, detail in _OBSERVATION_RESULT_DETAILS if code in codes),
            None,
        )
        completed = {
            action.kind
            for action in report.actions
            if action.state is LineEndingActionState.COMPLETED
        }
        completed_detail = next(
            (detail for kind, detail in _COMPLETED_ACTION_DETAILS if kind in completed),
            None,
        )
        detail = (
            observation_detail
            or completed_detail
            or "+".join(action.kind.value for action in report.actions)
        )
    else:
        detail = next(
            (detail for code, detail in _OBSERVATION_RESULT_DETAILS if code in codes),
            "no CRLF",
        )
    return detail


def _step_line_endings(ctx: InitContext, project_dir: Path | None = None) -> None:
    """Render the shared line-ending report for Project Initialization."""
    ctx.step_banner("line endings")
    mode = LineEndingMode.INSPECT if ctx.check_only else LineEndingMode.REPAIR
    report = reconcile_project_line_endings(ctx.project_root, project_dir, mode=mode)
    if report.status is LineEndingStatus.NOT_APPLICABLE:
        skip("project root is not a git repo — line-endings check skipped")
        ctx.record("line_endings", "skip", "not a git repo")
        return
    details: list[str] = []
    for repository_report in report.repositories:
        repository = repository_report.repository
        info(line_ending_repository_display(repository.role, repository.root))
        _render_line_ending_observations(repository_report)
        for action in repository_report.actions:
            _render_line_ending_action(action)
        if repository_report.status is LineEndingStatus.SAFE and not repository_report.actions:
            ok("working tree is container-safe (no CRLF checkouts, autocrlf off)")
        if repository_report.status is LineEndingStatus.UNSAFE:
            details.append(f"{repository.role}: {_line_ending_result_detail(repository_report)}")
    for failure in report.discovery_failures:
        display = line_ending_repository_display(failure.role, failure.candidate)
        warn(f"could not inspect {display}: {failure.detail}")
        details.append(f"{failure.role}: {failure.detail}")
    status = "ok" if report.status is LineEndingStatus.SAFE else "warn"
    if len(report.repositories) == 1 and not report.discovery_failures:
        detail = _line_ending_result_detail(report.repositories[0])
    elif status == "ok":
        detail = f"{len(report.repositories)} Git repositories container-safe"
    else:
        detail = "; ".join(details)
    ctx.record("line_endings", status, detail)


WORKTREE_PRUNE_KEY = "gc.worktreePruneExpire"
WORKTREE_PRUNE_VALUE = "never"


def read_worktree_prune_expire(project_root: Path) -> str | None:
    """Return the repo's ``gc.worktreePruneExpire`` value, or None if unset.

    Also returns None when *project_root* is not a git repo or git itself is
    unavailable — callers distinguish those cases themselves if they care.
    """
    proc = subprocess.run(
        ["git", "-C", str(project_root), "config", "--get", WORKTREE_PRUNE_KEY],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def _step_worktree_prune_guard(ctx: InitContext) -> None:
    """Set ``gc.worktreePruneExpire=never`` on the project repo.

    Ticket Mode worktrees are created in-container (ADR 0028 Decision 10), so
    their git worktree metadata records container paths. A host-side
    ``git gc`` cannot see those paths and would prune the registrations —
    "never" makes worktree pruning explicit-only (``git worktree prune``).
    """
    ctx.step_banner("worktree prune guard")

    # Confirm the project root is a git repo before touching its config.
    probe = subprocess.run(
        ["git", "-C", str(ctx.project_root), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if probe.returncode != 0:
        skip("project root is not a git repo — worktree prune guard skipped")
        ctx.record("worktree_prune_guard", "skip", "not a git repo")
        return

    if read_worktree_prune_expire(ctx.project_root) == WORKTREE_PRUNE_VALUE:
        skip(f"{WORKTREE_PRUNE_KEY} already '{WORKTREE_PRUNE_VALUE}'")
        ctx.record("worktree_prune_guard", "skip", "already set")
        return

    if ctx.check_only:
        warn(f"would set {WORKTREE_PRUNE_KEY}={WORKTREE_PRUNE_VALUE}")
        ctx.record("worktree_prune_guard", "warn", "would set")
        return

    proc = subprocess.run(
        ["git", "-C", str(ctx.project_root), "config", WORKTREE_PRUNE_KEY, WORKTREE_PRUNE_VALUE],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if proc.returncode != 0:
        warn(f"could not set {WORKTREE_PRUNE_KEY}: {proc.stderr.strip()}")
        ctx.record("worktree_prune_guard", "warn", "git config failed")
        return

    ok(
        f"{WORKTREE_PRUNE_KEY}={WORKTREE_PRUNE_VALUE} (host `git gc` can no "
        "longer prune in-container worktrees)"
    )
    ctx.record("worktree_prune_guard", "ok", "set")
