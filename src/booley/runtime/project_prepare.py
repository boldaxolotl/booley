"""Deterministic project preparation shared by every Ticket Mode entry point."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.platform_paths import bash_bin
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.runtime.ticket_repositories import paired_project_repository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparationResult:
    """Outcome of a project preparation hook without board or Git mutation."""

    ok: bool
    hook: Path | None = None
    error: str = ""


def _project_content_dir(worktree: Path) -> Path:
    paired = paired_project_repository(worktree)
    if paired is not None:
        return paired.worktree
    return resolve_checkout_project_dir(worktree)


def _find_hook(project_dir: Path) -> Path | None:
    for suffix in (".sh", ".py", ""):
        candidate = project_dir / "hooks" / f"post-setup{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _hook_command(hook: Path) -> list[str]:
    if hook.suffix == ".py":
        return [sys.executable, str(hook)]
    if hook.suffix in {".sh", ""}:
        return [bash_bin(), str(hook)]
    return [str(hook)]


def prepare_project(
    project_root: Path | str,
    worktree: Path | str,
    *,
    slug: str,
    ticket_path: Path | str | None = None,
    sim_flow_enabled: bool,
    timeout_s: int = 900,
) -> PreparationResult:
    """Run the authored post-setup hook without staging or committing output.

    Hooks are expected to be idempotent. Generated artifacts may remain ignored
    in the prepared checkout, but this function never changes board state,
    stages files, or creates commits.
    """
    root = Path(project_root).resolve()
    checkout = Path(worktree).resolve()
    project_dir = _project_content_dir(checkout)
    hook = _find_hook(project_dir)
    if hook is None:
        logger.debug("No post-setup hook in %s", project_dir / "hooks")
        return PreparationResult(ok=True)

    env = {
        **os.environ,
        "BOOLEY_WORKTREE": str(checkout),
        "BOOLEY_PROJECT_DIR": str(project_dir),
        "BOOLEY_PROJECT_ROOT": str(root),
        "BOOLEY_TICKET_SLUG": slug,
        "BOOLEY_TICKET_FILE": str(ticket_path or ""),
        "BOOLEY_SIM_FLOW_ENABLED": "1" if sim_flow_enabled else "0",
        "BOOLEY_IN_DOCKER": "",
    }
    try:
        result = subprocess.run(
            _hook_command(hook),
            cwd=checkout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return PreparationResult(False, hook, f"post-setup hook timed out ({timeout_s}s)")
    except OSError as exc:
        return PreparationResult(False, hook, f"post-setup hook failed (OS error): {exc}")

    if result.stdout.strip():
        logger.debug("Hook stdout: %s", result.stdout.strip()[:500])
    if result.stderr.strip():
        logger.debug("Hook stderr: %s", result.stderr.strip()[:500])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:500]
        return PreparationResult(
            False,
            hook,
            f"post-setup hook failed (rc={result.returncode}): {detail}",
        )

    logger.info("post-setup hook OK")
    return PreparationResult(True, hook)
