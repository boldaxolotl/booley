"""Pure Scope entry matching shared by provenance checks and Runtime callers."""

from __future__ import annotations

import fnmatch

SCOPE_UNKNOWN = "*"
"""Exact Scope sentinel for unknown/any file allowed."""

NEW_SCOPE_SUFFIX = " [new]"
"""Suffix for Scope entries describing files expected to be created."""


def strip_scope_new_tag(entry: str) -> str:
    """Return a scope entry without the optional `` [new]`` marker."""
    return entry.removesuffix(NEW_SCOPE_SUFFIX)


def is_new_scope_entry(entry: str) -> bool:
    """True when a raw ticket scope entry is marked as expected-new."""
    return entry.endswith(NEW_SCOPE_SUFFIX)


def is_scope_unknown(scope: list[str]) -> bool:
    """True when scope is the wildcard sentinel (unknown/any file allowed)."""
    return scope == [SCOPE_UNKNOWN]


def has_glob_chars(pattern: str) -> bool:
    """Check if a scope entry contains glob metacharacters."""
    return any(c in pattern for c in ("*", "?", "["))


def matches_scope_pattern(filepath: str, pattern: str) -> bool:
    """Match one scope entry against a path: glob, exact file, or dir prefix.

    A non-glob entry is an exact path *or* a directory prefix: ``rtl/verilog``
    and ``rtl/verilog/`` both own every file beneath ``rtl/verilog/``. Without
    the prefix rule a bare directory entry — the most natural thing to write
    in ``scope:`` — silently matches nothing (F-14).
    """
    if has_glob_chars(pattern):
        return fnmatch.fnmatch(filepath, pattern)
    return filepath == pattern or filepath.startswith(pattern.rstrip("/") + "/")


def scope_matches_file(scope: list[str], filepath: str) -> bool:
    """Check if a filepath matches any scope entry (literal or glob).

    Unknown scope (["*"]) matches everything.
    The standalone pre-commit hook keeps its own matching implementation.
    """
    if is_scope_unknown(scope):
        return True
    return any(matches_scope_pattern(filepath, strip_scope_new_tag(e)) for e in scope)
