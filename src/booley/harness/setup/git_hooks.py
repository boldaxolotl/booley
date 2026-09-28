"""Git commit-msg hook generation and installation for ``booley init``.

Extracted from ``init_cmd.py`` (Single Responsibility): the ``git_hooks`` step
installs the leak guard into the project-agnostic ``.booley/`` repo, and the
``project_git_hooks`` step publishes one standalone policy bundle under
``.booley_project/.managed/`` and installs repo-relative adapters into the
project's own ``.git/hooks/``. ``.booley_project/hooks/`` remains reserved for
Project-authored lifecycle hooks. Steps are named by their
record key, never by a display number — the banner numbers are allocated at
print time from the steps that actually run (see :meth:`InitContext.step_banner`),
so a hardcoded display number in a comment drifts the moment a step is
added or skipped (fpu F-12).

Depends only on ``init_common`` for console output and :class:`InitContext`;
it never imports back from ``init_cmd``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from booley.harness.bootstrap import (
    RELATIVE_WORKTREE_MIN_GIT_VERSION,
    parse_git_version,
)
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
from booley.harness.setup.project_git_hook_reconcile import (
    _LEGACY_MANAGED_HOOKS,
)
from booley.harness.setup.project_git_hook_reconcile import (
    _build_commit_msg_hook_body as _managed_build_commit_msg_hook_body,
)
from booley.harness.setup.project_git_hook_reconcile import (
    _build_hook_delegator_body as _managed_build_hook_delegator_body,
)
from booley.harness.setup.project_git_hook_reconcile import (
    _build_pre_push_hook_body as _managed_build_pre_push_hook_body,
)
from booley.harness.setup.project_git_hook_reconcile import (
    step_project_git_hooks as _managed_step_project_git_hooks,
)
from booley.runtime.project_dir import resolve_checkout_project_dir, resolve_project_dir
from booley.runtime.project_repositories import is_standalone_git_repository


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


# Project Git-hook reconciliation lives in a focused module. Keep these names here
# because init_cmd and older integrations import this setup facade.
_PROJECT_HOOK_SCRIPTS = _LEGACY_MANAGED_HOOKS
_PROJECT_HOOK_HELPERS: dict[str, Path] = {}
_build_hook_delegator_body = _managed_build_hook_delegator_body
_build_commit_msg_hook_body = _managed_build_commit_msg_hook_body
_build_pre_push_hook_body = _managed_build_pre_push_hook_body
_step_project_git_hooks = _managed_step_project_git_hooks

_OBSERVATION_MESSAGES = {
    LineEndingObservationCode.CRLF_MISMATCH: (
        "{count} tracked file(s) are checked out with CRLF — the Sandbox container "
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
        detail = "dirty tree" if detail.startswith("working tree has") else detail
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
    if report.status is LineEndingStatus.SAFE:
        status = "ok"
    else:
        status = "warn" if ctx.check_only else "err"
    if len(report.repositories) == 1 and not report.discovery_failures:
        detail = _line_ending_result_detail(report.repositories[0])
    elif status == "ok":
        detail = f"{len(report.repositories)} Git repositories container-safe"
    else:
        detail = "; ".join(details)
    ctx.record("line_endings", status, detail)


WORKTREE_PRUNE_KEY = "gc.worktreePruneExpire"
WORKTREE_PRUNE_VALUE = "never"
WORKTREE_RELATIVE_KEY = "worktree.useRelativePaths"


def _worktree_policy_repositories(project_root: Path) -> tuple[Path, ...]:
    """Return durable repositories that create Ticket Workspaces."""
    repositories = [project_root]
    try:
        project_dir = resolve_checkout_project_dir(project_root)
    except FileNotFoundError:
        return tuple(repositories)
    if project_dir != project_root and is_standalone_git_repository(project_dir):
        repositories.append(project_dir)
    return tuple(repositories)


def _read_local_config(repository: Path, key: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "config", "--local", "--get", key],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _repository_uses_relative_extension(repository: Path) -> bool:
    return _read_local_config(repository, "extensions.relativeWorktrees") == "true"


def _host_git_version() -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_git_version(result.stdout) if result.returncode == 0 else None


def _step_worktree_link_policy(
    ctx: InitContext,
    *,
    sandbox_git_version: tuple[int, int, int] | None,
    host_git_version: tuple[int, int, int] | None = None,
) -> None:
    """Reconcile portable worktree creation after two-sided capability proof."""
    ctx.step_banner("worktree link policy")
    host_version = host_git_version if host_git_version is not None else _host_git_version()
    capable = (
        host_version is not None
        and sandbox_git_version is not None
        and host_version >= RELATIVE_WORKTREE_MIN_GIT_VERSION
        and sandbox_git_version >= RELATIVE_WORKTREE_MIN_GIT_VERSION
    )
    repositories = _worktree_policy_repositories(ctx.project_root)
    incompatible = [repo for repo in repositories if _repository_uses_relative_extension(repo)]
    if not capable and incompatible:
        warn(
            "relative worktrees are already enabled, but the host or Sandbox Git is "
            "older than 2.48 or could not be verified; restore Git 2.48 or newer "
            "before using or migrating this Project"
        )
        ctx.record("worktree_link_policy", "warn", "incompatible Git downgrade")
        return

    desired = "true" if capable else "false"
    pending = [
        repo for repo in repositories if _read_local_config(repo, WORKTREE_RELATIVE_KEY) != desired
    ]
    if not pending:
        skip(f"{WORKTREE_RELATIVE_KEY} already '{desired}'")
        ctx.record("worktree_link_policy", "skip", f"already {desired}")
        return
    if ctx.check_only:
        warn(
            f"would set {WORKTREE_RELATIVE_KEY}={desired} in "
            f"{len(pending)} Git repository/repositories"
        )
        ctx.record("worktree_link_policy", "warn", f"would set {desired}")
        return

    failures: list[str] = []
    for repository in pending:
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "config",
                    "--local",
                    WORKTREE_RELATIVE_KEY,
                    desired,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"{repository}: {exc}")
            continue
        if result.returncode != 0:
            failures.append(f"{repository}: {result.stderr.strip() or 'git config failed'}")
    if failures:
        warn("could not reconcile worktree link policy: " + "; ".join(failures))
        ctx.record("worktree_link_policy", "warn", "git config failed")
        return
    if capable:
        ok("new Ticket Workspaces use relative links on host and in the Sandbox")
        detail = "relative links enabled"
    else:
        warn(
            "new Ticket Workspaces use the container-only absolute-link fallback; "
            "do not run host `git worktree prune`"
        )
        detail = "absolute-link fallback"
    ctx.record("worktree_link_policy", "ok" if capable else "warn", detail)


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
    """Keep automatic pruning disabled in every Ticket Workspace repository."""
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

    repositories = _worktree_policy_repositories(ctx.project_root)
    pending = [
        repository
        for repository in repositories
        if read_worktree_prune_expire(repository) != WORKTREE_PRUNE_VALUE
    ]
    if not pending:
        skip(f"{WORKTREE_PRUNE_KEY} already '{WORKTREE_PRUNE_VALUE}'")
        ctx.record("worktree_prune_guard", "skip", "already set")
        return

    if ctx.check_only:
        warn(f"would set {WORKTREE_PRUNE_KEY}={WORKTREE_PRUNE_VALUE}")
        ctx.record("worktree_prune_guard", "warn", "would set")
        return

    failures: list[str] = []
    for repository in pending:
        proc = subprocess.run(
            ["git", "-C", str(repository), "config", WORKTREE_PRUNE_KEY, WORKTREE_PRUNE_VALUE],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if proc.returncode != 0:
            failures.append(f"{repository}: {proc.stderr.strip()}")
    if failures:
        warn(f"could not set {WORKTREE_PRUNE_KEY}: {'; '.join(failures)}")
        ctx.record("worktree_prune_guard", "warn", "git config failed")
        return

    ok(
        f"{WORKTREE_PRUNE_KEY}={WORKTREE_PRUNE_VALUE} in {len(repositories)} "
        "Git repository/repositories (automatic pruning disabled)"
    )
    ctx.record("worktree_prune_guard", "ok", "set")
