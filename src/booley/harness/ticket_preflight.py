"""Ticket Preflight checks -- fast-fail before any ticket work begins.

Runs at the very top of run_ticket(), before ticket intake.
These are environment/repo sanity checks that don't need a ticket context.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from booley.runtime import runtime_context

logger = logging.getLogger(__name__)


class TicketPreflightError(Exception):
    """Raised when Ticket Preflight checks fail -- execution should not start."""

    def __init__(self, failures: list[str]) -> None:
        self.failures = failures
        msg = "Ticket Preflight failed:\n  " + "\n  ".join(failures)
        super().__init__(msg)


def run_ticket_preflight(project_root: Path) -> None:
    """Run all Ticket Preflight checks. Raises TicketPreflightError on failure.

    Checks (in order):
      0. Running inside the Sandbox (Ticket Mode is container-only)
      1. .tickets/ directory exists
      2. Git is available and we're in a repo
      3. Dirty working tree warning (non-blocking)
      4. No in-progress git operations (merge/rebase/cherry-pick)
      5. ticket_board package is importable
      6. FuseSoC core-tree setup hazards
      7. Custom MCP endpoints & criteria validation
      8. Agent backend health (warning only)
    """
    failures: list[str] = []

    # 0. Ticket Mode is container-only (ADR 0028): every ticket runs inside
    # the Sandbox alongside the interactive session. Fail loud here
    # with the fix rather than later with a confusing path/Flow error.
    _check_inside_container()

    # 1. Tickets directory
    from booley.ticket_board.helpers import tickets_dir_from_project_root

    tickets_dir = tickets_dir_from_project_root(project_root)
    if not tickets_dir.is_dir():
        failures.append(f"tickets directory not found at {tickets_dir}")

    # 2-4. Git checks
    failures.extend(_check_git(project_root))

    # 5. ticket_board reachable
    failures.extend(_check_ticket_board(project_root))

    # 6. Conditions that otherwise hang or force network access during setup.
    failures.extend(_check_core_setup_hazards(project_root))

    if failures:
        raise TicketPreflightError(failures)

    # 7. Custom MCP endpoints & criteria validation
    from booley.mcp.endpoint_config import EndpointConfigError

    try:
        _validate_custom_endpoints_and_criteria(project_root)
    except EndpointConfigError as exc:
        raise TicketPreflightError([str(exc)]) from exc

    # 8. Active agent backend health (warning only)
    _check_agent_backend()

    logger.info("Ticket Preflight OK")


def _check_inside_container() -> None:
    """Refuse to start a ticket run anywhere but the Sandbox.

    Booley is container-only (ADR 0028): tickets execute inside the same
    devcontainer as the interactive session — one runtime, one filesystem, one
    slot store. A host-side `booley run` would race the container over the
    same `.booley_project/` state through different path roots.
    """
    error = runtime_context.container_only_error("booley run")
    if error is not None:
        raise TicketPreflightError([error])


def _check_git(project_root: Path) -> list[str]:
    """Check git availability, dirty tree, and in-progress operations."""
    git_cwd = str(project_root)

    # Git available?
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=git_cwd,
            check=False,
        )
        if result.returncode != 0:
            return ["Not inside a git work tree"]
    except FileNotFoundError:
        return ["git not found on PATH"]
    except subprocess.TimeoutExpired:
        return ["git timed out -- possible filesystem issue"]

    _warn_dirty_tree(git_cwd)
    return _check_in_progress_ops(git_cwd, project_root)


def _warn_dirty_tree(git_cwd: str) -> None:
    """Warn (non-blocking) if the working tree has uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--ignore-submodules"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=git_cwd,
            check=False,
        )
        if result.returncode == 0:
            dirty = [ln for ln in result.stdout.strip().split("\n") if ln.strip()]
            if dirty:
                preview = dirty[:5]
                suffix = f" (and {len(dirty) - 5} more)" if len(dirty) > 5 else ""
                file_list = ", ".join(ln.strip() for ln in preview) + suffix
                logger.warning(
                    "Dirty working tree (%d modified files): %s  "
                    "-- proceeding anyway (worktree will use committed state)",
                    len(dirty),
                    file_list,
                )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _check_in_progress_ops(git_cwd: str, project_root: Path) -> list[str]:
    """Return errors if a merge/rebase/cherry-pick is in progress."""
    errors: list[str] = []
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=git_cwd,
            check=False,
        )
        if result.returncode == 0:
            git_dir = Path(result.stdout.strip())
            if not git_dir.is_absolute():
                git_dir = project_root / git_dir
            conflict_markers = {
                "merge": git_dir / "MERGE_HEAD",
                "rebase": git_dir / "rebase-merge",
                "rebase (apply)": git_dir / "rebase-apply",
                "cherry-pick": git_dir / "CHERRY_PICK_HEAD",
            }
            for state, marker in conflict_markers.items():
                if marker.exists():
                    errors.append(f"Git {state} in progress -- resolve before running Booley")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return errors


def _check_agent_backend() -> None:
    """Validate local backend configuration without launching its CLI."""
    try:
        from booley.runtime.agent_config import get_backend_config

        cfg = get_backend_config()
        warning = cfg.active_backend.health_check()
        if warning:
            logger.warning("Agent backend (%s): %s", cfg.active_backend.name, warning)
        else:
            logger.debug(
                "Agent backend (%s): configuration OK (CLI startup not exercised)",
                cfg.active_backend.name,
            )
    except (ImportError, AttributeError, RuntimeError, OSError) as e:
        logger.warning("Agent backend health check failed: %s", e)


def _check_ticket_board(project_root: Path) -> list[str]:
    """Verify ticket_board package is importable."""
    errors: list[str] = []
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import booley.ticket_board"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(project_root),
            check=False,
        )
        if result.returncode != 0:
            errors.append(f"ticket_board package not importable: {result.stderr.strip()}")
    except (subprocess.TimeoutExpired, FileNotFoundError, NotADirectoryError):
        errors.append("Could not verify ticket_board package")
    return errors


def _doctor_core_files(project_root: Path) -> set[Path]:
    """Core files in Doctor-selected Target dependency closures."""
    from booley.targets.catalog import TargetCatalog

    catalog = TargetCatalog.build(project_root)
    seeds = [handle for handle in catalog.list() if handle.doctor_flows]
    closure = catalog.core_closure(seeds)
    return set(closure or ())


def _check_core_setup_hazards(project_root: Path) -> list[str]:
    """Reject recursive links and provider-backed cores selected by the project."""
    from booley.fusesoc import fusesoc_registry

    selected = _doctor_core_files(project_root)
    state_cores = fusesoc_registry.state_cores_dir(project_root)
    failures: list[str] = []
    for hazard in fusesoc_registry.core_setup_hazards(project_root):
        rel = hazard.path.relative_to(project_root)
        if hazard.kind == "recursive-symlink":
            failures.append(
                f"FuseSoC recursive symlink {rel}: {hazard.detail}; add a "
                "FUSESOC_IGNORE marker to the containing subtree or remove the link"
            )
            continue
        owned = hazard.path in selected or state_cores in hazard.path.parents
        if owned:
            failures.append(
                f"FuseSoC core {rel} has a provider block that requests network access; "
                "remove provider: from the in-tree core"
            )
        else:
            logger.warning(
                "FuseSoC core %s has a provider block, but no Doctor Target selects it",
                rel,
            )
    return failures


# ---------------------------------------------------------------------------
# Custom MCP endpoints & criteria validation
# ---------------------------------------------------------------------------


def _validate_custom_endpoints_and_criteria(project_root: Path) -> None:
    """Preserve the Ticket Preflight error contract for its execution callers."""
    from booley.mcp.endpoint_validation import (
        EndpointValidationError,
        validate_custom_endpoints_and_criteria,
    )

    try:
        validate_custom_endpoints_and_criteria(project_root)
    except EndpointValidationError as exc:
        raise TicketPreflightError(exc.failures) from exc
