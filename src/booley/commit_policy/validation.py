"""Reusable commit-message validation."""

from __future__ import annotations

import re
from pathlib import Path

try:
    from booley.commit_policy.policy import (
        enforce_convention,
        max_body_lines,
        stealth_enabled,
    )
    from booley.commit_policy.policy import (
        find_banned as _find_banned,
    )
except ImportError:
    from booley_commit_policy import (
        enforce_convention,
        max_body_lines,
        stealth_enabled,
    )
    from booley_commit_policy import (
        find_banned as _find_banned,
    )

MAX_SUMMARY_LEN = 72
ALLOWED_TYPES = ("feat", "fix", "refactor", "test", "review", "wip", "docs", "chore")
SUBJECT_RE = re.compile(
    r"^(?P<prefix>(?:" + "|".join(ALLOWED_TYPES) + r")"
    r"(?:\([a-zA-Z0-9_-]+\))?"
    r": )"
    r"(?P<summary>.+)$"
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


def validate_message(msg: str, *, project_root: Path | None = None) -> list[str]:
    """Validate a commit message and return every policy error."""
    errors: list[str] = []
    subject = msg.split("\n", 1)[0]
    is_merge = subject.startswith("Merge ") or subject.startswith("merge(")

    if enforce_convention(project_root) and not is_merge:
        match = SUBJECT_RE.match(subject)
        if not match:
            errors.append(
                f"Subject doesn't match '<type>(<scope>): <summary>' "
                f"(allowed types: {', '.join(ALLOWED_TYPES)}): '{subject}'"
            )
        else:
            summary = match.group("summary")
            if len(summary) > MAX_SUMMARY_LEN:
                errors.append(
                    f"Summary is {len(summary)} chars (max {MAX_SUMMARY_LEN}): '{summary}'"
                )

    cap = max_body_lines(project_root)
    if cap is not None:
        body = _body_content_lines(msg)
        if len(body) > cap:
            detail = "commit messages must be a single subject line" if cap == 0 else f"max {cap}"
            errors.append(
                f"Body is {len(body)} line(s) ([stealth] max_body_lines = {cap}): {detail}"
            )

    if stealth_enabled(project_root):
        errors.extend(
            f"Banned phrase in commit message: '{phrase}'"
            for phrase in _find_banned(msg, project_root)
        )
    return errors
