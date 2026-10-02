#!/usr/bin/env python3
"""Project-owned commit and leak policy.

Single source of truth for banned phrases and content checking.
Consumed by packaged callers and standalone Git-hook adapters.

Banned phrases are read from booley.toml [stealth] banned_words if available,
falling back to a hardcoded default list; banned_substrings adds literal terms. The same [stealth] table also carries
the commit-body cap (max_body_lines) and the identity allowlist
(allowed_authors), both read here so the validator and the pre-push guard share
one parser.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path

try:
    from booley.core.boundary import (
        BoundaryError,
        as_dict,
        is_str_list,
        require_bool,
        require_int,
    )
except ImportError:
    from boundary import (  # pyright: ignore[reportMissingImports]
        BoundaryError,
        as_dict,
        is_str_list,
        require_bool,
        require_int,
    )

logger = logging.getLogger(__name__)

# Default banned phrases (used when booley.toml has no [stealth] section).
_DEFAULT_BANNED_PHRASES = [
    # Multi-word phrases
    "co-authored-by",
    # Single words
    "claude",
    "anthropic",
    "copilot",
    "cursor",
    "codex",
    "openai",
    "chatgpt",
    "gemini",
    "ticket",
    "gpt",
    "llm",
    "booley",
]


_SUBSTRING_IDENTITIES = frozenset(
    {"booley", "claude", "anthropic", "codex", "openai", "chatgpt", "copilot", "gemini"}
)


def _unique_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    unique = []
    for term in terms:
        if term and term.lower() not in seen:
            unique.append(term)
            seen.add(term.lower())
    return tuple(unique)


def _reserved_substring(term: str) -> bool:
    lowered = term.lower()
    fixed = (
        "redacted",
        "<redacted>",
        "<author>",
        "<author-email>",
        "<repo>",
        "<home>",
        "<remote>",
        "<org>",
        "<org>/<repo>",
        "<email>",
    )
    pattern = re.compile(re.escape(term), re.IGNORECASE)
    if any(pattern.search(placeholder) for placeholder in fixed):
        return True
    # A substring can contain at most one contiguous decimal run. Try each
    # placement of that run in a positive integer, preserving literal edges.
    numbers = {"1", "10"}
    for digits in re.findall(r"[0-9]+", lowered):
        numbers.update((digits, "1" + digits, digits + "1", "1" + digits + "1"))
    return any(pattern.search(f"<module-{number}>") for number in numbers if number[0] != "0")


def parse_stealth_vocabulary(
    section: dict, *, defaults: bool = True
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Parse the union and substring tier; feedback explicitly suppresses defaults."""
    raw_words = section.get("banned_words")
    if raw_words is None:
        words = tuple(_DEFAULT_BANNED_PHRASES) if defaults else ()
    elif is_str_list(raw_words):
        words = tuple(word for word in raw_words if word)
    else:
        fallback = "using defaults" if defaults else "ignoring invalid override"
        logger.warning("[stealth] banned_words must be a list of strings — %s", fallback)
        words = tuple(_DEFAULT_BANNED_PHRASES) if defaults else ()
    raw_substrings = section.get("banned_substrings", [])
    if not is_str_list(raw_substrings):
        raise BoundaryError("[stealth] banned_substrings must be a list of strings")
    substrings = tuple(word for word in raw_substrings if word)
    if any(_reserved_substring(word) for word in substrings):
        raise BoundaryError(
            "[stealth] banned_substrings must not match reserved redaction placeholders"
        )
    identities = tuple(word for word in words if word.lower() in _SUBSTRING_IDENTITIES)
    return _unique_terms(words + substrings), _unique_terms(identities + substrings)


_TOML_SUBDIRS = [Path(".booley_project"), Path(".booley") / "project"]
_TOML_NAMES = ("booley.toml", "pipeline.toml")


def _load_booley_config(project_root: Path | None = None, *, strict: bool = False) -> dict:
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
            if _configuration_candidate(toml_path, strict=strict):
                try:
                    with toml_path.open("rb") as f:
                        return tomllib.load(f)
                except (OSError, tomllib.TOMLDecodeError) as e:
                    if strict:
                        raise ValueError("cannot read selected Project configuration") from e
                    logger.warning("Failed to parse %s: %s", toml_path, e)
    return {}


def _configuration_candidate(path: Path, *, strict: bool) -> bool:
    if not strict:
        return path.exists()
    try:
        path.stat()
    except FileNotFoundError:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ValueError("cannot read selected Project configuration") from exc
        raise ValueError("cannot read selected Project configuration") from None
    except OSError as exc:
        raise ValueError("cannot read selected Project configuration") from exc
    return True


@dataclass(frozen=True, slots=True)
class UpstreamRecord:
    """Owner-selected repository authority and immutable imported commit."""

    repository: str
    base: str


def upstream_record(project_root: Path | None = None) -> UpstreamRecord | None:
    """Read an explicit paired upstream authority, never infer one from refs."""
    if source_checkout_policy_owner(project_root):
        return None
    section = _load_booley_config(project_root, strict=True).get("stealth", {})
    if not isinstance(section, dict):
        raise ValueError("[stealth] must be a table")
    repository = section.get("upstream_repository")
    base = section.get("upstream_base")
    if repository is None and base is None:
        return None
    if (
        not isinstance(repository, str)
        or not repository.strip()
        or repository != repository.strip()
    ):
        raise ValueError("[stealth] upstream_repository and upstream_base require a complete pair")
    if (
        not isinstance(base, str)
        or re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", base) is None
    ):
        raise ValueError("[stealth] upstream_base must be a full immutable commit object ID")
    absolute = Path(repository).is_absolute()
    url = re.fullmatch(r"(?:https|ssh|file)://[^\s]+", repository)
    scp = (
        "://" not in repository
        and "::" not in repository
        and not repository.lower().startswith("file:")
        and re.fullmatch(r"(?:[^\s/@:]+@)?[^\s/:]+:[^\s]+", repository)
    )
    if not (absolute or url or scp):
        raise ValueError("[stealth] upstream_repository requires a literal URL or absolute path")
    return UpstreamRecord(repository, base.lower())


def _stealth_section(project_root: Path | None = None) -> dict:
    """Return the ``[stealth]`` table, or ``{}`` if absent/malformed."""
    section = _load_booley_config(project_root).get("stealth", {})
    return as_dict(section, default={}) or {}


def source_checkout_policy_owner(project_root: Path | None) -> bool:
    """Return whether an explicit policy owner lies inside Booley source.

    Project Git-hook modules are deliberately runnable as self-contained bundle members,
    where importing :mod:`booley` is impossible. Those installations vendor the
    same runtime classifier flat beside this module, keeping one implementation
    of the tracked-marker and legacy-layout contract.
    """
    if project_root is None:
        return False
    root = Path(project_root).resolve()
    try:
        from booley.core.checkout_role import source_checkout_root
    except ImportError:
        try:
            from checkout_role import source_checkout_root
        except ImportError:
            # A source-tree hook may be launched directly before the editable
            # package is installed. Add its src/ parent and retry the canonical
            # package import. If no classifier exists, preserve the old hook's
            # behavior rather than guessing that the checkout is Booley source.
            package_parent = Path(__file__).resolve().parents[2]
            classifier = package_parent / "booley" / "core" / "checkout_role.py"
            if not classifier.is_file():
                return False
            source_path = str(package_parent)
            if source_path not in sys.path:
                sys.path.insert(0, source_path)
            try:
                from booley.core.checkout_role import source_checkout_root
            except ImportError:
                return False
    return source_checkout_root(root) is not None


def _boolean_setting(section: dict, name: str, default: bool) -> bool:
    """Return one validated boolean setting, or its documented default."""
    try:
        return require_bool(section, name, default=default, field=f"[stealth] {name}")
    except BoundaryError as exc:
        logger.warning("%s — using %r", exc, default)
        return default


@dataclass(frozen=True, slots=True)
class StealthPolicy:
    """Immutable Project-owned leak and commit-history policy."""

    enabled: bool
    banned_phrases: tuple[str, ...]
    max_body_lines: int | None
    enforce_convention: bool
    allowed_authors: tuple[str, ...]

    banned_substrings: tuple[str, ...] = ()

    def find_banned(self, text: str) -> list[str]:
        """Return this policy's banned phrases found in *text*."""
        return [
            phrase
            for phrase, pattern in _cached_banned_res(self.banned_phrases, self.banned_substrings)
            if pattern.search(text)
        ]


def stealth_policy(project_root: Path | None = None) -> StealthPolicy:
    """Load one Project's Stealth policy; source checkouts are always disabled."""
    if source_checkout_policy_owner(project_root):
        return StealthPolicy(False, (), None, False, ())

    section = _stealth_section(project_root)
    words, substrings = parse_stealth_vocabulary(section)
    raw_cap = section.get("max_body_lines")
    try:
        cap = (
            require_int(raw_cap, field="[stealth] max_body_lines") if raw_cap is not None else None
        )
    except BoundaryError:
        cap = None
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
    elif is_str_list(raw_authors):
        authors = tuple(item.strip() for item in raw_authors if item.strip())
    else:
        logger.warning(
            "[stealth] allowed_authors must be a list of strings, got %r — ignoring",
            raw_authors,
        )
        authors = ()
    return StealthPolicy(
        enabled=_boolean_setting(section, "enabled", True),
        banned_phrases=words,
        max_body_lines=cap,
        enforce_convention=_boolean_setting(section, "enforce_convention", False),
        allowed_authors=authors,
        banned_substrings=substrings,
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


# Assertions inspect original casing independently of the case-insensitive literal.
_IDENTIFIER_EDGE = (
    r"(?-i:(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|"
    r"(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z]))"
)


def _build_banned_res(
    phrases: list[str], substrings: tuple[str, ...] = ()
) -> list[tuple[str, re.Pattern[str]]]:
    """Compile literal substring or original-text identifier-token patterns."""
    substring_terms = _SUBSTRING_IDENTITIES | {term.lower() for term in substrings}
    patterns = []
    for phrase in _unique_terms(tuple(phrases)):
        literal = re.escape(phrase)
        if phrase.lower() not in substring_terms:
            literal = (
                r"(?:(?-i:(?<![A-Za-z0-9]))|"
                + _IDENTIFIER_EDGE
                + ")"
                + literal
                + r"(?:(?-i:(?![A-Za-z0-9]))|"
                + _IDENTIFIER_EDGE
                + ")"
            )
        patterns.append((phrase, re.compile(literal, re.IGNORECASE)))
    return patterns


@cache
def _cached_banned_res(
    phrases: tuple[str, ...], substrings: tuple[str, ...] = ()
) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Cache both immutable matching tiers together."""
    return tuple(_build_banned_res(list(phrases), substrings))


def banned_spans(
    text: str, phrases: tuple[str, ...], substrings: tuple[str, ...] = ()
) -> list[tuple[int, int, str]]:
    """Return literal hits in original character coordinates, including overlaps."""
    return [
        (match.start(1), match.end(1), phrase)
        for phrase, pattern in _cached_banned_res(phrases, substrings)
        for match in re.finditer("(?=(" + pattern.pattern + "))", text, pattern.flags)
    ]


def _merged_spans(spans: list[tuple[int, int, str]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end, _phrase in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


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
    """Replace covered original spans once, preserving all surrounding text.

    A custom legacy banned_words entry may itself match the fixed placeholder.
    """
    policy = stealth_policy(project_root)
    spans = _merged_spans(banned_spans(text, policy.banned_phrases, policy.banned_substrings))
    for start, end in reversed(spans):
        text = text[:start] + REDACTION_PLACEHOLDER + text[end:]
    return text
