"""Deterministic Project preparation before Goal admission."""

from __future__ import annotations

import codecs
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.booley_dir import resolve_checkout_booley_dir
from booley.runtime.platform_paths import bash_bin
from booley.runtime.project_repositories import paired_project_repository

logger = logging.getLogger(__name__)

#: Seconds between checks of a logged hook's exit and its newly written output.
_LOG_POLL_S = 0.2


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
    return resolve_checkout_booley_dir(worktree)


def _find_hook(booley_dir: Path) -> Path | None:
    for suffix in (".sh", ".py", ""):
        candidate = booley_dir / "hooks" / f"post-setup{suffix}"
        if candidate.is_file():
            return candidate
    return None


def find_post_setup_hook(checkout: Path) -> Path | None:
    """Return the post-setup hook that :func:`prepare_project` would run in *checkout*."""
    return _find_hook(_project_content_dir(checkout))


def _hook_command(hook: Path) -> list[str]:
    if hook.suffix == ".py":
        return [sys.executable, str(hook)]
    if hook.suffix in {".sh", ""}:
        return [bash_bin(), str(hook)]
    return [str(hook)]


def _hook_environment(
    checkout: Path,
    booley_dir: Path,
    root: Path,
    slug: str,
    ticket_path: Path | str | None,
    sim_flow_enabled: bool,
) -> dict[str, str]:
    return {
        **os.environ,
        "BOOLEY_WORKTREE": str(checkout),
        "BOOLEY_DIR": str(booley_dir),
        "BOOLEY_PROJECT_ROOT": str(root),
        "BOOLEY_TICKET_SLUG": slug,
        "BOOLEY_TICKET_FILE": str(ticket_path or ""),
        "BOOLEY_SIM_FLOW_ENABLED": "1" if sim_flow_enabled else "0",
        "BOOLEY_IN_DOCKER": "",
    }


def _wait_tick() -> None:
    time.sleep(_LOG_POLL_S)


def _echo_new_log_bytes(log: Path, offset: int, decoder: codecs.IncrementalDecoder) -> int:
    """Copy log bytes written after *offset* to stderr; return the new offset."""
    with log.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
    if chunk:
        sys.stderr.write(decoder.decode(chunk))
        sys.stderr.flush()
    return offset + len(chunk)


def _run_logged(
    command: list[str], cwd: Path, env: dict[str, str], timeout_s: int, log: Path
) -> int | None:
    """Run *command* with its output in *log*, echoed live to stderr; ``None`` on timeout.

    The hook writes straight into the log file (no pipe), in the caller's
    process group so a terminal Ctrl-C reaches it. The parent waits for the
    process itself, never for end of output, so a background child that keeps
    the log open cannot stall a finished hook. A timeout or interrupt kills the
    direct child, as ``subprocess.run(timeout=)`` does.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    offset = 0
    deadline = time.monotonic() + timeout_s
    with log.open("wb") as sink:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait()
                    offset = _echo_new_log_bytes(log, offset, decoder)
                    return None
                offset = _echo_new_log_bytes(log, offset, decoder)
                _wait_tick()
        except BaseException:
            process.kill()
            process.wait()
            raise
    _echo_new_log_bytes(log, offset, decoder)
    return process.returncode


def _prepare_logged(
    hook: Path, checkout: Path, env: dict[str, str], timeout_s: int, log: Path
) -> PreparationResult:
    """Run *hook* through :func:`_run_logged` and map its outcome like the captured path."""
    try:
        returncode = _run_logged(_hook_command(hook), checkout, env, timeout_s, log)
    except OSError as exc:
        return PreparationResult(False, hook, f"post-setup hook failed (OS error): {exc}")
    if returncode is None:
        return PreparationResult(False, hook, f"post-setup hook timed out ({timeout_s}s)")
    if returncode != 0:
        return PreparationResult(False, hook, f"post-setup hook failed (rc={returncode})")
    logger.info("post-setup hook OK")
    return PreparationResult(True, hook)


def prepare_project(
    project_root: Path | str,
    worktree: Path | str,
    *,
    slug: str,
    ticket_path: Path | str | None = None,
    sim_flow_enabled: bool,
    timeout_s: int = 900,
    log: Path | None = None,
) -> PreparationResult:
    """Run the authored post-setup hook without staging or committing output.

    Hooks are expected to be idempotent. Generated artifacts may remain ignored
    in the prepared checkout, but this function never changes board state,
    stages files, or creates commits.

    With *log*, the hook's combined output is written there and echoed live to
    stderr instead of being captured for the debug log.
    """
    root = Path(project_root).resolve()
    checkout = Path(worktree).resolve()
    booley_dir = _project_content_dir(checkout)
    hook = _find_hook(booley_dir)
    if hook is None:
        logger.debug("No post-setup hook in %s", booley_dir / "hooks")
        return PreparationResult(ok=True)

    env = _hook_environment(checkout, booley_dir, root, slug, ticket_path, sim_flow_enabled)
    if log is not None:
        return _prepare_logged(hook, checkout, env, timeout_s, log)
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
