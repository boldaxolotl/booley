"""Path resolution for per-ticket log artifacts.

The ticket log root is a human-facing surface: ``logs/<slug>/``.  Runtime
state, locks, JSONL streams, and raw Flow/MCP-tool reports live under ``.runtime/``.
Large but human-readable logs live under ``human-logs/``.
"""

from __future__ import annotations

from pathlib import Path

RUNTIME_DIR = ".runtime"
HUMAN_LOGS_DIR = "human-logs"

# ---------------------------------------------------------------------------
# Legacy step directory mapping (kept for analytics backward compat)
# ---------------------------------------------------------------------------

STEP_DIR_MAP: dict[str, str] = {
    "setup": "01-setup",
    "planning": "02-planning",
    "run-config": "02c-run-config",
    "implementation": "03-implementation",
    "implementation-tb": "03-implementation-tb",
    "lint-check": "03a-lint-check",
    "rtl-review-1": "05-rtl-review-1",
    "tb-review": "06-tb-review",
    "sim-debug-loop": "07-sim-debug-loop",
    "rtl-mutation-testing": "08-rtl-mutation-testing",
    "rtl-review-final": "09-rtl-review-final",
    "post-review-sim": "09b-post-review-sim",
    "synthesis": "10-synthesis",
    "acceptance-check": "11-acceptance-check",
    "summary": "12-summary",
    "review": "13-review",
}


def ticket_log_dir(logs_dir: str | Path, slug: str) -> Path:
    """Return the human-facing ticket log root."""
    return Path(logs_dir) / slug


def ticket_runtime_dir(log_dir: str | Path) -> Path:
    """Return the runtime directory for one ticket log root."""
    return Path(log_dir) / RUNTIME_DIR


def runtime_dir(logs_dir: str | Path, slug: str) -> Path:
    """Return ``logs/<slug>/.runtime``."""
    return ticket_runtime_dir(ticket_log_dir(logs_dir, slug))


def human_logs_dir(log_dir: str | Path) -> Path:
    """Return the directory for large human-readable logs."""
    return Path(log_dir) / HUMAN_LOGS_DIR


def runtime_file(logs_dir: str | Path, slug: str, filename: str) -> Path:
    """Return a runtime file path under ``logs/<slug>/.runtime``."""
    return runtime_dir(logs_dir, slug) / filename


def ticket_runtime_file(log_dir: str | Path, filename: str) -> Path:
    """Return a runtime file path from one ticket log root."""
    return ticket_runtime_dir(log_dir) / filename


def human_log_file(logs_dir: str | Path, slug: str, filename: str) -> Path:
    """Return a large human-readable log path under ``human-logs``."""
    return human_logs_dir(ticket_log_dir(logs_dir, slug)) / filename


def ticket_human_log_file(log_dir: str | Path, filename: str) -> Path:
    """Return a human log path from one ticket log root."""
    return human_logs_dir(log_dir) / filename


def legacy_file(logs_dir: str | Path, slug: str, filename: str) -> Path:
    """Return the pre-split root-level path for a file."""
    return ticket_log_dir(logs_dir, slug) / filename


def ticket_legacy_file(log_dir: str | Path, filename: str) -> Path:
    """Return the pre-split root-level path from one ticket log root."""
    return Path(log_dir) / filename


def existing_runtime_file(logs_dir: str | Path, slug: str, filename: str) -> Path:
    """Return the new runtime path, falling back to legacy if it exists."""
    new_path = runtime_file(logs_dir, slug, filename)
    old_path = legacy_file(logs_dir, slug, filename)
    return new_path if new_path.exists() or not old_path.exists() else old_path


def existing_ticket_runtime_file(log_dir: str | Path, filename: str) -> Path:
    """Return the new runtime path, falling back to legacy if it exists."""
    new_path = ticket_runtime_file(log_dir, filename)
    old_path = ticket_legacy_file(log_dir, filename)
    return new_path if new_path.exists() or not old_path.exists() else old_path


def existing_human_log_file(logs_dir: str | Path, slug: str, filename: str) -> Path:
    """Return the new human-log path, falling back to legacy if it exists."""
    new_path = human_log_file(logs_dir, slug, filename)
    old_path = legacy_file(logs_dir, slug, filename)
    return new_path if new_path.exists() or not old_path.exists() else old_path


def migrate_file_to(path: Path, target: Path) -> Path:
    """Move *path* to *target* when needed and return the authoritative path."""
    if path == target:
        return target
    if target.exists() or not path.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.replace(target)
    except PermissionError:
        # A legacy lock file may already be held by another process. Use the
        # legacy inode so the caller can contend on the existing lock.
        return path
    return target


def migrate_runtime_file(log_dir: str | Path, filename: str) -> Path:
    """Move a legacy root runtime file into ``.runtime`` if present."""
    return migrate_file_to(
        ticket_legacy_file(log_dir, filename),
        ticket_runtime_file(log_dir, filename),
    )
