"""Conventional-commit message auto-formatting.

Extracted from ``specialist`` (principle 8): rewriting a raw agent commit
message into ``<type>(<scope>): <summary>`` form is one reason to change
(the conventional-commit convention), independent of agent invocation or
git plumbing. ``specialist._prepare_commit_message`` is the sole caller.
"""

from __future__ import annotations

import re

from .validate_commit_msg import ALLOWED_TYPES

# Matches valid conventional-commit subject lines. The type alternation is
# built from validate_commit_msg.ALLOWED_TYPES so the two can never drift
# (a hardcoded copy here once missed `chore` — valid messages got rewritten).
# Scope stays lowercase-only on purpose: this is the *formatter* — an
# uppercase scope from an agent should be normalized, not passed through.
_CONVENTIONAL_RE = re.compile(
    r"^(?:" + "|".join(ALLOWED_TYPES) + r")"
    r"(?:\([a-z0-9_-]+\))?"
    r": "
    r".+$"
)

# Heuristic: detect type from the message content
_TYPE_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(fix|correct|repair|patch|resolve)\b", re.IGNORECASE), "fix"),
    (
        re.compile(r"\b(refactor|reorganize|restructure|simplify|clean)\b", re.IGNORECASE),
        "refactor",
    ),
    (re.compile(r"\b(test|testbench|tb|coverage|stimulus)\b", re.IGNORECASE), "test"),
    (re.compile(r"\b(review|address findings|per review)\b", re.IGNORECASE), "review"),
    (re.compile(r"\b(doc|comment|readme|describe)\b", re.IGNORECASE), "docs"),
]


def _auto_format_commit_message(msg: str, *, category: str = "") -> str:
    """Rewrite a commit message into conventional-commit format if needed.

    Already-valid messages are returned as-is (with summary truncation if
    over 72 chars).  Non-conforming messages are wrapped into
    ``<type>(<scope>): <lowercased summary>``.

    Args:
        msg: Raw commit message (first line used, body discarded).
        category: Optional category hint ("rtl", "tb") used as fallback scope.
    """
    subject = msg.split("\n", 1)[0].strip()
    if not subject:
        return msg

    # Already valid — just enforce length limit on summary portion
    if _CONVENTIONAL_RE.match(subject):
        return _trim_summary(subject)

    # --- Not conventional — auto-format ---

    # Infer type from content heuristics; default to "feat"
    commit_type = "feat"
    for pattern, ctype in _TYPE_HINTS:
        if pattern.search(subject):
            commit_type = ctype
            break

    # Infer scope: use category if available, else extract from subject
    scope = _infer_scope(subject, category)

    # Clean up summary: strip leading verbs that duplicate the type,
    # lowercase first char, strip trailing period
    summary = _clean_summary(subject)

    # Truncate summary to 72 chars (accounting for prefix length)
    prefix = f"{commit_type}({scope}): " if scope else f"{commit_type}: "
    max_summary = 72 - len(prefix)
    if len(summary) > max_summary:
        summary = summary[: max_summary - 3].rstrip() + "..."

    return f"{prefix}{summary}"


def _trim_summary(subject: str) -> str:
    """Trim the summary portion of an already-valid conventional subject to 72 chars."""
    # Split at the first ": " to get prefix and summary
    idx = subject.index(": ") + 2
    prefix = subject[:idx]
    summary = subject[idx:]
    max_len = 72 - len(prefix)
    if len(summary) > max_len:
        summary = summary[: max_len - 3].rstrip() + "..."
    return prefix + summary


def _infer_scope(subject: str, category: str) -> str:
    """Best-effort scope extraction from subject or category fallback."""
    # If category provided, use it as scope
    if category and category.lower() in ("rtl", "tb"):
        return category.lower()

    # Try to extract a module-like token from the subject
    # Look for patterns like "xxx_yyy" (underscore-separated identifiers)
    m = re.search(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b", subject, re.IGNORECASE)
    if m:
        return m.group(1).lower()

    # Fallback: use first significant word (skip common verbs)
    _SKIP = {
        "implement",
        "add",
        "create",
        "update",
        "fix",
        "correct",
        "remove",
        "delete",
        "change",
        "modify",
        "refactor",
        "the",
        "a",
        "an",
        "for",
        "in",
        "of",
        "to",
        "with",
        "from",
        "and",
        "or",
        "top",
        "level",
        "top-level",
        "initial",
        "new",
        "basic",
        "simple",
        "full",
        "complete",
    }
    words = re.findall(r"[a-z0-9][-a-z0-9]*", subject.lower())
    for w in words:
        if w not in _SKIP and len(w) > 2:
            return w.replace("-", "_")

    return "rtl"  # absolute fallback


def _clean_summary(subject: str) -> str:
    """Clean a raw subject into a summary suitable for the conventional format."""
    s = subject.strip()
    # Strip leading verbs that are redundant with the commit type
    s = re.sub(
        r"^(implement|add|create|introduce|set up|setup|apply|write|"
        r"fix|correct|repair|patch|resolve|"
        r"refactor|reorganize|restructure|simplify|clean up|"
        r"test|review|document)\s+",
        "",
        s,
        flags=re.IGNORECASE,
    )
    # Lowercase first char — but only if the second char is also lowercase
    # (preserve acronyms like "IIR", "AES", "DES")
    if s and s[0].isupper() and (len(s) < 2 or s[1].islower()):
        s = s[0].lower() + s[1:]
    # Strip trailing period
    s = s.rstrip(".")
    return s
