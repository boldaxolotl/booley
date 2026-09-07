"""Dependency-neutral Target selector policy shared by catalog adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from booley.targets.domain import AmbiguousTargetError, TargetRef, UnknownTargetError


def vlnv_key(vlnv: str) -> str:
    """Return the version-independent ``vendor:library:name`` identity."""
    parts = vlnv.split(":")
    return ":".join(parts[:3]) if len(parts) >= 3 else vlnv


def split_selector(token: str) -> tuple[str | None, str]:
    """Split a bare or ``vlnv#name`` selector into qualifier and name."""
    if "#" not in token:
        return None, token
    qualifier, _, name = token.rpartition("#")
    return qualifier or None, name


def vlnv_matches(query: str, vlnv: str) -> bool:
    """Return whether *query* is a segment suffix of *vlnv*'s identity."""
    key_segments = vlnv_key(vlnv).split(":")
    query_segments = vlnv_key(query).split(":")
    return len(query_segments) <= len(key_segments) and (
        key_segments[-len(query_segments) :] == query_segments
    )


def resolve(
    declarations: Mapping[str, Sequence[TargetRef]],
    token: str,
) -> TargetRef:
    """Resolve one selector against an already-filtered declaration snapshot."""
    qualifier, name = split_selector(token)
    bucket = declarations.get(name)
    if not bucket:
        known = ", ".join(sorted(declarations)) or "(none authored)"
        raise UnknownTargetError(f"Unknown target {token!r}; selectable Targets: {known}")
    if qualifier is None:
        if len(bucket) == 1:
            return bucket[0]
        candidates = sorted(ref.vlnv for ref in bucket)
        hint = f"{vlnv_key(candidates[0]).split(':')[-1]}#{name}"
        raise AmbiguousTargetError(
            f"Target {name!r} is declared by {len(bucket)} cores: "
            f"{', '.join(candidates)}; qualify it as 'vlnv#name' (e.g. {hint!r})."
        )
    matches = tuple(ref for ref in bucket if vlnv_matches(qualifier, ref.vlnv))
    if not matches:
        candidates = ", ".join(sorted(ref.vlnv for ref in bucket))
        raise UnknownTargetError(
            f"no Target {name!r} in a core matching {qualifier!r}; "
            f"cores declaring {name!r}: {candidates}"
        )
    if len(matches) > 1:
        candidates = ", ".join(sorted(ref.vlnv for ref in matches))
        raise AmbiguousTargetError(
            f"{token!r} is ambiguous — {qualifier!r} matches {len(matches)} "
            f"cores: {candidates}; use a longer VLNV qualifier."
        )
    return next(iter(matches))


def minimal_selector(ref: TargetRef, declaring: Sequence[TargetRef]) -> str:
    """Return the shortest selector that uniquely identifies *ref*."""
    if len(declaring) <= 1:
        return ref.name
    segments = vlnv_key(ref.vlnv).split(":")
    for length in range(1, len(segments) + 1):
        qualifier = ":".join(segments[-length:])
        if sum(vlnv_matches(qualifier, candidate.vlnv) for candidate in declaring) == 1:
            return f"{qualifier}#{ref.name}"
    return f"{ref.vlnv}#{ref.name}"


__all__ = ["minimal_selector", "resolve", "split_selector", "vlnv_key", "vlnv_matches"]
