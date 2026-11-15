"""Reusable commit-message validation."""

from __future__ import annotations

from pathlib import Path

try:
    from booley.commit_policy.policy import (
        find_banned as _find_banned,
    )
    from booley.commit_policy.policy import (
        max_body_lines,
        stealth_enabled,
    )
except ImportError:
    from booley_commit_policy import (  # pyright: ignore[reportMissingImports]
        find_banned as _find_banned,
    )
    from booley_commit_policy import (  # pyright: ignore[reportMissingImports]
        max_body_lines,
        stealth_enabled,
    )


def _body_content_lines(msg: str) -> list[str]:
    """Return nonblank, non-comment body lines from *msg*."""
    normalized = msg.replace("\r\n", "\n").replace("\r", "\n")
    parts = normalized.split("\n", 1)
    if len(parts) < 2:
        return []
    return [
        line for line in parts[1].split("\n") if line.strip() and not line.lstrip().startswith("#")
    ]


def body_length_errors(msg: str, *, project_root: Path | None = None) -> list[str]:
    """Return the ``[stealth] max_body_lines`` error for *msg*, if any.

    Booley enforces no subject format. Banned terms are not reported here;
    :func:`validate_message` adds them.
    """
    errors: list[str] = []
    cap = max_body_lines(project_root)
    if cap is not None:
        body = _body_content_lines(msg)
        if len(body) > cap:
            detail = "commit messages must be a single subject line" if cap == 0 else f"max {cap}"
            errors.append(
                f"Body is {len(body)} line(s) ([stealth] max_body_lines = {cap}): {detail}"
            )
    return errors


def validate_message(msg: str, *, project_root: Path | None = None) -> list[str]:
    """Validate a commit message and return every policy error."""
    errors = body_length_errors(msg, project_root=project_root)
    if stealth_enabled(project_root):
        errors.extend(
            f"Banned phrase in commit message: '{phrase}'"
            for phrase in _find_banned(msg, project_root)
        )
    return errors
