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
from dataclasses import dataclass
from functools import cache
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

    Callers pass the repository they are operating on.  Omitting it means
    shipped defaults, never ambient discovery from this module's own location.
    That keeps one imported Booley checkout from lending its policy to a
    different Project.
    """
    try:
        import tomllib
    except ModuleNotFoundError:
        return {}

    if project_root is None:
        return {}
    root = Path(project_root).resolve()

    state_repo = root.name == ".booley_project" or (
        root.name == "project" and root.parent.name == ".booley"
    )
    directories = (Path(), *_TOML_SUBDIRS) if state_repo else _TOML_SUBDIRS
    for subdir in directories:
        for name in _TOML_NAMES:
            toml_path = root / subdir / name
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


def _source_checkout(project_root: Path | None) -> bool:
    """Return whether an explicit policy owner lies inside Booley source."""
    if project_root is None:
        return False
    try:
        from booley.runtime.checkout_role import source_checkout_root
    except ImportError:
        return False  # vendored Project hooks intentionally carry no package
    return source_checkout_root(Path(project_root)) is not None


@dataclass(frozen=True, slots=True)
class StealthPolicy:
    """Immutable Project-owned leak and commit-history policy."""

    enabled: bool
    banned_phrases: tuple[str, ...]
    max_body_lines: int | None
    enforce_convention: bool
    allowed_authors: tuple[str, ...]

    def find_banned(self, text: str) -> list[str]:
        """Return this policy's banned phrases found in *text*."""
        return [
            phrase
            for phrase, pattern in _cached_banned_res(self.banned_phrases)
            if pattern.search(text)
        ]


def stealth_policy(project_root: Path | None = None) -> StealthPolicy:
    """Load one Project's Stealth policy; source checkouts are always disabled."""
    if _source_checkout(project_root):
        return StealthPolicy(False, (), None, False, ())

    section = _stealth_section(project_root)
    raw_words = section.get("banned_words")
    if raw_words is None:
        words = tuple(_DEFAULT_BANNED_PHRASES)
    elif isinstance(raw_words, list) and all(isinstance(word, str) for word in raw_words):
        words = tuple(word for word in raw_words if word)
    else:
        logger.warning(
            "[stealth] banned_words must be a list of strings, got %r — using defaults",
            raw_words,
        )
        words = tuple(_DEFAULT_BANNED_PHRASES)

    raw_cap = section.get("max_body_lines")
    cap = raw_cap if isinstance(raw_cap, int) and not isinstance(raw_cap, bool) else None
    if cap is not None and cap < 0:
        cap = None
    if raw_cap is not None and cap is None:
        logger.warning(
            "[stealth] max_body_lines must be a non-negative integer, got %r — ignoring",
            raw_cap,
        )

    raw_authors = section.get("allowed_authors")
    if raw_authors is None:
        authors: tuple[str, ...] = ()
    elif isinstance(raw_authors, list) and all(isinstance(item, str) for item in raw_authors):
        authors = tuple(item.strip() for item in raw_authors if item.strip())
    else:
        logger.warning(
            "[stealth] allowed_authors must be a list of strings, got %r — ignoring",
            raw_authors,
        )
        authors = ()
    return StealthPolicy(
        enabled=bool(section.get("enabled", True)),
        banned_phrases=words,
        max_body_lines=cap,
        enforce_convention=bool(section.get("enforce_convention", False)),
        allowed_authors=authors,
    )


def stealth_enabled(project_root: Path | None = None) -> bool:
    """Whether stealth mode is active. On by default; opt out with
    ``[stealth] enabled = false`` in booley.toml.

    Gates both the commit-msg hook install (setup) and the agent-facing
    banned-word prompt note (specialists).
    """
    return stealth_policy(project_root).enabled


def _load_stealth_config(project_root: Path | None = None) -> list[str] | None:
    """Read ``[stealth] banned_words`` override from booley.toml, or None."""
    words = _stealth_section(project_root).get("banned_words")
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
    return stealth_policy(project_root).max_body_lines


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
    return stealth_policy(project_root).enforce_convention


def allowed_authors(project_root: Path | None = None) -> list[str]:
    """``[stealth] allowed_authors``: identity allowlist for outgoing commits.

    An empty list means *unrestricted* — both when the knob is absent and when
    it is written as ``[]``. Mirrors ``banned_words``, where an empty list also
    reads as "this check is off", and avoids the footgun where a half-written
    allowlist silently blocks every push instead of doing nothing.
    """
    return list(stealth_policy(project_root).allowed_authors)


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


@cache
def _cached_banned_res(
    phrases: tuple[str, ...],
) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Compile one immutable policy once for repeated commit/tree scans."""
    return tuple(_build_banned_res(list(phrases)))


# Compatibility view of the shipped defaults. Project consumers must call
# :func:`banned_phrases` or :func:`find_banned` with their explicit root.
BANNED_PHRASES: list[str] = list(_DEFAULT_BANNED_PHRASES)


def banned_phrases(project_root: Path | None = None) -> list[str]:
    """Return the banned phrases belonging to one Project policy."""
    return list(stealth_policy(project_root).banned_phrases)


def find_banned(text: str, project_root: Path | None = None) -> list[str]:
    """Return list of banned phrases found in text."""
    return stealth_policy(project_root).find_banned(text)


def has_banned_content(text: str, project_root: Path | None = None) -> bool:
    """Return True if text contains any banned phrase."""
    return bool(find_banned(text, project_root))


# Placeholder substituted for a banned phrase when the surrounding text must
# survive (e.g. a commit subject, which — unlike a merge-body line — cannot
# simply be dropped). Kept to [a-zA-Z0-9] so a redacted scope like
# ``fix(booley): ...`` -> ``fix(redacted): ...`` still satisfies SUBJECT_RE.
REDACTION_PLACEHOLDER = "redacted"


def redact_banned(text: str, project_root: Path | None = None) -> str:
    """Replace every banned phrase in *text* with REDACTION_PLACEHOLDER.

    Used for text that must be preserved rather than dropped (commit
    subjects). Applying the same compiled, case-insensitive, word-boundary
    regexes as find_banned/has_banned_content guarantees the result is free
    of banned content, so a subsequent validation pass cannot re-flag it.
    """
    policy = stealth_policy(project_root)
    for _phrase, pattern in _cached_banned_res(policy.banned_phrases):
        text = pattern.sub(REDACTION_PLACEHOLDER, text)
    return text
