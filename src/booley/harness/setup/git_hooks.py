"""Git commit-msg hook generation and installation for ``booley init``.

Extracted from ``init_cmd.py`` (Single Responsibility): the ``git_hooks`` step
installs the leak guard into the project-agnostic ``.booley/`` repo. The
``project_git_hooks`` step lives in ``project_git_hook_reconcile``.
``.booley/hooks/`` remains reserved for Project-authored lifecycle hooks.
Steps are named by their
record key, never by a display number — the banner numbers are allocated at
print time from the steps that actually run (see :meth:`InitContext.step_banner`),
so a hardcoded display number in a comment drifts the moment a step is
added or skipped (fpu F-12).

It never imports back from ``init_cmd``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from booley.harness.bootstrap import (
    RELATIVE_WORKTREE_MIN_GIT_VERSION,
    parse_git_version,
)
from booley.harness.setup.common import (
    InitContext,
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
    LineEndingReport,
    LineEndingStatus,
    RepositoryLineEndingReport,
    line_ending_repository_display,
    reconcile_project_line_endings,
    unknown_attributes_policy_details,
)
from booley.runtime.booley_dir import resolve_checkout_booley_dir


def _step_git_hooks(ctx: InitContext) -> None:
    """Embedded Booley source checkouts are retired; Project hooks reconcile separately."""
    ctx.record("git_hooks", "skip", "Project policy bundle is reconciled separately")


_OBSERVATION_MESSAGES = {
    LineEndingObservationCode.CRLF_MISMATCH: (
        "{count} tracked file(s) are checked out with CRLF — the Sandbox container "
        "will see every one as modified (phantom diffs break the dirty-tree check, scope "
        "enforcement, and worktrees)"
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
    LineEndingActionKind.PUBLISH_ATTRIBUTES: "attributes publication failed",
    LineEndingActionKind.MIGRATE_ATTRIBUTES: "attributes migration failed",
}

_OBSERVATION_RESULT_DETAILS = (
    (LineEndingObservationCode.LEAKED_ROOT_POLICY, "untracked legacy .gitattributes"),
    (
        LineEndingObservationCode.LOCAL_POLICY_CONFLICT,
        "repository-local line-ending policy conflict",
    ),
    (
        LineEndingObservationCode.LOCAL_POLICY_MISSING,
        "repository-local line-ending policy missing",
    ),
    (LineEndingObservationCode.CANDIDATE_UNSAFE, "candidate unsafe"),
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
    if action.kind is LineEndingActionKind.MIGRATE_ATTRIBUTES:
        if action.state is LineEndingActionState.PLANNED:
            info(f"  would migrate line-ending default: {action.detail}")
        elif action.state is LineEndingActionState.COMPLETED:
            ok(f"migrated line-ending default: {action.detail}")
        else:
            warn(f"attributes migration {action.state}: {action.detail}")
        return
    if (
        action.kind is LineEndingActionKind.PUBLISH_ATTRIBUTES
        and action.target
        and action.target.local
    ):
        destination = f"{action.target.path} (repository-local; shared by linked worktrees)"
        if action.state is LineEndingActionState.COMPLETED:
            ok(f"added '{GITATTRIBUTES_RULE}' to {destination}")
        elif action.state is LineEndingActionState.PLANNED:
            info(f"  would add '{GITATTRIBUTES_RULE}' to {destination}")
        elif action.detail:
            warn(f"attributes publication {action.state}: {action.detail}")
        return
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


def _unsafe_line_ending_observation_detail(report: RepositoryLineEndingReport) -> str | None:
    observations = {
        observation.code: observation
        for observation in report.observations
        if observation.code
        not in (
            LineEndingObservationCode.UPSTREAM_POLICY,
            LineEndingObservationCode.ATTRIBUTES_POLICY_UNKNOWN,
        )
    }
    for code, detail in _OBSERVATION_RESULT_DETAILS:
        if code in observations:
            if code is LineEndingObservationCode.CANDIDATE_UNSAFE:
                return observations[code].detail or detail
            return detail
    if report.status is LineEndingStatus.UNSAFE and observations:
        observation = next(iter(observations.values()))
        return observation.detail or observation.code.value
    return None


def _line_ending_result_detail(report: RepositoryLineEndingReport) -> str:
    codes = {observation.code for observation in report.observations}
    unreadable = _unreadable_line_ending_detail(report, codes)
    if unreadable is not None:
        return unreadable
    failed = next(
        (action for action in report.actions if action.state is LineEndingActionState.FAILED),
        None,
    )
    if failed:
        return _FAILED_ACTION_DETAILS[failed.kind]
    refused = next(
        (action for action in report.actions if action.state is LineEndingActionState.REFUSED),
        None,
    )
    if refused:
        detail = refused.detail or (
            "candidate unsafe"
            if refused.kind is LineEndingActionKind.NORMALIZE_FILES
            else f"{refused.kind.value} refused"
        )
        if refused.kind is LineEndingActionKind.NORMALIZE_FILES and detail.startswith(
            "working tree has"
        ):
            detail = "dirty tree"
        return detail
    observation_detail = _unsafe_line_ending_observation_detail(report)
    if observation_detail:
        return observation_detail
    if report.status is LineEndingStatus.UNSAFE:
        return "unsafe line-ending state"
    return _safe_line_ending_result_detail(report)


def _safe_line_ending_result_detail(report: RepositoryLineEndingReport) -> str:
    unknown = unknown_attributes_policy_details(report)
    if unknown:
        return f"attributes policy inspection incomplete: {'; '.join(unknown)}"
    completed = {
        action.kind for action in report.actions if action.state is LineEndingActionState.COMPLETED
    }
    completed_detail = next(
        (detail for kind, detail in _COMPLETED_ACTION_DETAILS if kind in completed),
        None,
    )
    return (
        completed_detail or "+".join(action.kind.value for action in report.actions) or "no CRLF"
    )


def _step_line_endings(
    ctx: InitContext,
    booley_dir: Path | None = None,
    *,
    clean_baseline: Mapping[Path, bool | None] | None = None,
) -> None:
    """Render the shared line-ending report for Project Initialization.

    ``clean_baseline`` is the per-repository cleanliness sampled before init
    wrote any files in the Booley Directory, so Booley's own earlier edits do not block repair.
    """
    ctx.step_banner("line endings")
    mode = LineEndingMode.INSPECT if ctx.check_only else LineEndingMode.REPAIR
    report = reconcile_project_line_endings(
        ctx.project_root, booley_dir, mode=mode, clean_baseline=clean_baseline
    )
    if report.status is LineEndingStatus.NOT_APPLICABLE:
        skip("Project Root is not a git repo — line-endings check skipped")
        ctx.record("line_endings", "skip", "not a git repo")
        return
    details: list[str] = []
    for repository_report in report.repositories:
        detail = _render_repository_line_endings(repository_report)
        if detail:
            details.append(detail)
    for failure in report.discovery_failures:
        display = line_ending_repository_display(failure.role, failure.candidate)
        warn(f"could not inspect {display}: {failure.detail}")
        details.append(f"{failure.role}: {failure.detail}")
    _record_line_ending_summary(ctx, report, details)


def _render_repository_line_endings(report: RepositoryLineEndingReport) -> str | None:
    repository = report.repository
    info(line_ending_repository_display(repository.role, repository.root))
    _render_line_ending_observations(report)
    for action in report.actions:
        _render_line_ending_action(action)
    unknown = bool(unknown_attributes_policy_details(report))
    if report.status is LineEndingStatus.SAFE and not report.actions and not unknown:
        ok("working tree is container-safe (no CRLF checkouts, autocrlf off)")
    if report.status is LineEndingStatus.UNSAFE or unknown:
        return f"{repository.role}: {_line_ending_summary_detail(report)}"
    return None


def _line_ending_summary_detail(report: RepositoryLineEndingReport) -> str:
    detail = _line_ending_result_detail(report)
    unknown = unknown_attributes_policy_details(report)
    if report.status is LineEndingStatus.UNSAFE and unknown:
        return f"{detail}; attributes policy inspection incomplete: {'; '.join(unknown)}"
    return detail


def _record_line_ending_summary(
    ctx: InitContext, report: LineEndingReport, details: list[str]
) -> None:
    if report.status is LineEndingStatus.SAFE:
        status = "warn" if details else "ok"
    else:
        status = "warn" if ctx.check_only else "err"
    if len(report.repositories) == 1 and not report.discovery_failures:
        detail = _line_ending_summary_detail(report.repositories[0])
    elif status == "ok":
        detail = f"{len(report.repositories)} Git repositories container-safe"
    else:
        detail = "; ".join(details)
    ctx.record("line_endings", status, detail)


WORKTREE_PRUNE_KEY = "gc.worktreePruneExpire"
WORKTREE_PRUNE_VALUE = "never"
WORKTREE_RELATIVE_KEY = "worktree.useRelativePaths"


def worktree_policy_repositories(
    project_root: Path, *, booley_dir: Path | None = None
) -> tuple[Path, ...]:
    """Return durable repositories that create worktrees."""
    repositories = [project_root]
    if booley_dir is None:
        try:
            booley_dir = resolve_checkout_booley_dir(project_root)
        except FileNotFoundError:
            return tuple(repositories)
    if booley_dir != project_root and _policy_standalone_repository(booley_dir):
        repositories.append(booley_dir)
    return tuple(repositories)


def _policy_git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        environment.pop(key, None)
    return environment


def _policy_standalone_repository(repository: Path) -> bool:
    if not (repository / ".git").is_dir():
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=_policy_git_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0 or not result.stdout.strip():
        return False
    try:
        return Path(result.stdout.strip()).resolve() == repository.resolve()
    except OSError:
        return False


def read_local_config(repository: Path, key: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "config", "--local", "--get", key],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=_policy_git_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _repository_uses_relative_extension(repository: Path) -> bool:
    return read_local_config(repository, "extensions.relativeWorktrees") == "true"


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


def _set_local_config(repository: Path, value: str | None) -> str | None:
    args = ["git", "-C", str(repository), "config", "--local"]
    args += (
        [WORKTREE_RELATIVE_KEY, value]
        if value is not None
        else ["--unset-all", WORKTREE_RELATIVE_KEY]
    )
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=_policy_git_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if result.returncode not in ({0, 5} if value is None else {0}):
        return result.stderr.strip() or "git config failed"
    return None


def _reconcile_relative_policy(repositories: tuple[Path, ...], desired: str) -> list[str]:
    previous = {
        repository: read_local_config(repository, WORKTREE_RELATIVE_KEY)
        for repository in repositories
    }
    changed: list[Path] = []
    for repository in repositories:
        if error := _set_local_config(repository, desired):
            failures = [f"{repository}: {error}"]
            for completed in reversed(changed):
                if rollback_error := _set_local_config(completed, previous[completed]):
                    failures.append(f"rollback {completed}: {rollback_error}")
            return failures
        changed.append(repository)
    return []


def _worktree_policy_capable(
    host_version: tuple[int, int, int] | None,
    sandbox_version: tuple[int, int, int] | None,
) -> bool:
    minimum = RELATIVE_WORKTREE_MIN_GIT_VERSION
    return (
        host_version is not None
        and sandbox_version is not None
        and host_version >= minimum
        and sandbox_version >= minimum
    )


def _step_worktree_link_policy(
    ctx: InitContext,
    *,
    sandbox_git_version: tuple[int, int, int] | None,
    host_git_version: tuple[int, int, int] | None = None,
    booley_dir: Path | None = None,
) -> None:
    """Reconcile portable worktree creation after two-sided capability proof."""
    ctx.step_banner("worktree link policy")
    host_version = host_git_version if host_git_version is not None else _host_git_version()
    capable = _worktree_policy_capable(host_version, sandbox_git_version)
    repositories = worktree_policy_repositories(ctx.project_root, booley_dir=booley_dir)
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
        repo for repo in repositories if read_local_config(repo, WORKTREE_RELATIVE_KEY) != desired
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

    failures = _reconcile_relative_policy(tuple(pending), desired)
    if failures:
        warn("could not reconcile worktree link policy: " + "; ".join(failures))
        ctx.record("worktree_link_policy", "warn", "git config failed")
        return
    _report_worktree_link_policy(ctx, capable=capable)


def _report_worktree_link_policy(ctx: InitContext, *, capable: bool) -> None:
    if capable:
        ok("new worktrees use relative links on host and in the Sandbox")
        detail = "relative links enabled"
    else:
        warn(
            "new worktrees use the container-only absolute-link fallback; "
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


def _step_worktree_prune_guard(ctx: InitContext, *, booley_dir: Path | None = None) -> None:
    """Keep automatic pruning disabled in every worktree repository."""
    ctx.step_banner("worktree prune guard")

    # Confirm the Project Root is a git repo before touching its config.
    probe = subprocess.run(
        ["git", "-C", str(ctx.project_root), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if probe.returncode != 0:
        skip("Project Root is not a git repo — worktree prune guard skipped")
        ctx.record("worktree_prune_guard", "skip", "not a git repo")
        return

    repositories = worktree_policy_repositories(ctx.project_root, booley_dir=booley_dir)
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
