#!/usr/bin/env python3
"""Shared utilities for commit message sanitization and validation.

Single source of truth for banned phrases and content checking.
Used by both commit_msg_hook.py (sanitizer) and validate_commit_msg.py (validator).

Banned phrases are read from booley.toml [stealth] banned_words if available,
falling back to a hardcoded default list. The same [stealth] table also carries
the commit-body cap (max_body_lines) and the identity allowlist
(allowed_authors), both read here so the validator and the pre-push guard share
one parser.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Default banned phrases (used when booley.toml has no [stealth] section).
_DEFAULT_BANNED_PHRASES = [
    # Multi-word phrases
    "auto-review",
    "auto-approve",
    "per review findings",
    "as suggested by",
    "co-authored-by",
    "automated fix",
    "suggested fix",
    # Single words
    "claude",
    "anthropic",
    "copilot",
    "cursor",
    "codex",
    "openai",
    "chatgpt",
    "gemini",
    "generated",
    "agent",
    "ticket",
    "gpt",
    "llm",
    "docker",
    "booley",
]


_TOML_SUBDIRS = [Path(".booley_project"), Path(".booley") / "project"]
_TOML_NAMES = ("booley.toml", "pipeline.toml")


def _load_booley_config(project_root: Path | None = None) -> dict:
    """Return the parsed ``booley.toml`` as a dict, or ``{}`` if unavailable.

    With *project_root* given, read that project directly (setup-time, host
    side, where the root is known). Without it, walk up from this file to
    locate the project — the in-container commit-msg hook path, where this file
    lives in the mounted hooks dir and the root is not known ahead of time.
    """
    try:
        import tomllib
    except ModuleNotFoundError:
        return {}

    if project_root is not None:
        root: Path | None = Path(project_root)
    else:
        # Can't hardcode depth — this file may live in src/booley/dev_support/ on the
        # host or in the mounted hooks dir inside a Docker container.
        candidate = Path(__file__).resolve().parent
        root = None
        while True:
            if any((candidate / sub).is_dir() for sub in _TOML_SUBDIRS):
                root = candidate
                break
            parent = candidate.parent
            if parent == candidate:
                break
            candidate = parent

    if root is None:
        return {}

    for sub in _TOML_SUBDIRS:
        for name in _TOML_NAMES:
            toml_path = root / sub / name
            if toml_path.exists():
                try:
                    with toml_path.open("rb") as f:
                        return tomllib.load(f)
                except (OSError, tomllib.TOMLDecodeError) as e:
                    logger.warning("Failed to parse %s: %s", toml_path, e)
    return {}


def _stealth_section(project_root: Path | None = None) -> dict:
    """Return the ``[stealth]`` table, or ``{}`` if absent/malformed."""
    section = _load_booley_config(project_root).get("stealth", {})
    return section if isinstance(section, dict) else {}


def stealth_enabled(project_root: Path | None = None) -> bool:
    """Whether stealth mode is active. On by default; opt out with
    ``[stealth] enabled = false`` in booley.toml.

    Gates both the commit-msg hook install (setup) and the agent-facing
    banned-word prompt note (specialists).
    """
    return bool(_stealth_section(project_root).get("enabled", True))


def _load_stealth_config() -> list[str] | None:
    """Read ``[stealth] banned_words`` override from booley.toml, or None."""
    words = _stealth_section().get("banned_words")
    return list(words) if words is not None else None


def max_body_lines(project_root: Path | None = None) -> int | None:
    """``[stealth] max_body_lines``: cap on commit-body length, or None.

    ``None`` (the default, knob absent) means *unlimited* — bodies carry
    rationale worth keeping, which is why the old unconditional "single-line
    messages only" rule was removed (F-11, taxi port). A project that genuinely
    wants terse one-liners opts back in explicitly; ``0`` means no body at all.

    Read per call rather than cached at import: the knob is only consulted at
    commit time, where one extra TOML read is free, and a cached value would
    go stale for a long-lived process that edits the config.
    """
    raw = _stealth_section(project_root).get("max_body_lines")
    if raw is None:
        return None
    # bool is an int subclass — `max_body_lines = true` is a config typo, not a
    # cap of 1, so reject it explicitly rather than silently enforcing one line.
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        logger.warning(
            "[stealth] max_body_lines must be a non-negative integer, got %r — ignoring",
            raw,
        )
        return None
    return raw


def enforce_convention(project_root: Path | None = None) -> bool:
    """``[stealth] enforce_convention``: enforce the ``type(scope): summary``
    subject convention. **Opt-in** — off unless a project turns it on.

    Off by default because a design repo carries human- and upstream-style
    commits on code the team doesn't own, and forcing every one of those
    through Booley's subject format is noise, not hygiene (SETUP-10). A team
    that wants the convention across its own history opts in with
    ``[stealth] enforce_convention = true``; then the per-commit
    ``BOOLEY_SKIP_COMMIT_VALIDATION`` env var is the escape hatch for the odd
    upstream commit.

    Independent of sanitization: the IP-leak scrub always runs when stealth is
    enabled, regardless of this knob. Read per call for the same reason as
    :func:`max_body_lines` — the config may change under a long-lived process.
    """
    return bool(_stealth_section(project_root).get("enforce_convention", False))


def allowed_authors(project_root: Path | None = None) -> list[str]:
    """``[stealth] allowed_authors``: identity allowlist for outgoing commits.

    An empty list means *unrestricted* — both when the knob is absent and when
    it is written as ``[]``. Mirrors ``banned_words``, where an empty list also
    reads as "this check is off", and avoids the footgun where a half-written
    allowlist silently blocks every push instead of doing nothing.
    """
    raw = _stealth_section(project_root).get("allowed_authors")
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        logger.warning(
            "[stealth] allowed_authors must be a list of strings, got %r — ignoring",
            raw,
        )
        return []
    return [p.strip() for p in raw if p.strip()]


def identity_allowed(name: str, email: str, patterns: list[str]) -> bool:
    """Does the git identity ``name <email>`` match any allowlist *pattern*?

    Each pattern is an fnmatch glob tested against three renderings of the
    identity — the bare email, the bare name, and the full ``Name <email>``
    ident — so one entry can be an exact address (``dev@example.com``), a bare
    name (``Jane Doe``), a whole-domain glob (``*@example.com``), or a verbatim
    ident line copied out of ``git log``.

    Matching is case-insensitive: both sides are lowercased and compared with
    ``fnmatchcase``, because plain ``fnmatch`` defers to ``os.path.normcase``
    and would therefore fold case on Windows but not on Linux — the same
    allowlist has to mean the same thing on every machine that pushes.
    """
    if not patterns:
        return True
    name, email = name.strip(), email.strip()
    candidates = [email.lower(), name.lower(), f"{name} <{email}>".lower()]
    return any(
        fnmatch.fnmatchcase(candidate, pattern.lower())
        for pattern in patterns
        for candidate in candidates
    )


def _build_banned_res(phrases: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    """Compile word-boundary regexes for each banned phrase."""
    return [
        (phrase, re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE))
        for phrase in phrases
    ]


# Load at import time: project config overrides defaults.
_stealth_words = _load_stealth_config()
BANNED_PHRASES: list[str] = (
    _stealth_words if _stealth_words is not None else _DEFAULT_BANNED_PHRASES
)
_BANNED_RES = _build_banned_res(BANNED_PHRASES)


def find_banned(text: str) -> list[str]:
    """Return list of banned phrases found in text."""
    return [phrase for phrase, pattern in _BANNED_RES if pattern.search(text)]


def has_banned_content(text: str) -> bool:
    """Return True if text contains any banned phrase."""
    return any(pattern.search(text) for _, pattern in _BANNED_RES)


# Placeholder substituted for a banned phrase when the surrounding text must
# survive (e.g. a commit subject, which — unlike a merge-body line — cannot
# simply be dropped). Kept to [a-zA-Z0-9] so a redacted scope like
# ``fix(booley): ...`` -> ``fix(redacted): ...`` still satisfies SUBJECT_RE.
REDACTION_PLACEHOLDER = "redacted"


def redact_banned(text: str) -> str:
    """Replace every banned phrase in *text* with REDACTION_PLACEHOLDER.

    Used for text that must be preserved rather than dropped (commit
    subjects). Applying the same compiled, case-insensitive, word-boundary
    regexes as find_banned/has_banned_content guarantees the result is free
    of banned content, so a subsequent validation pass cannot re-flag it.
    """
    for _phrase, pattern in _BANNED_RES:
        text = pattern.sub(REDACTION_PLACEHOLDER, text)
    return text
