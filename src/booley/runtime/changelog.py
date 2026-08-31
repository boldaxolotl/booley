"""Parse the packaged public changelog for upgrade review and release tooling."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_RELEASE_HEADING = re.compile(
    r"^## (?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"
    r" - (?P<day>\d{2}) (?P<month>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
    r" (?P<year>\d{4})\r?$",
    re.MULTILINE,
)
_VERSIONISH_HEADING = re.compile(r"^## (?=[v\d])(?P<heading>.+)$", re.MULTILINE)
_MONTH_NUMBER = {
    month: index
    for index, month in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}


class ChangelogError(ValueError):
    """The changelog does not satisfy Booley's release-entry contract."""


@dataclass(frozen=True, order=True)
class StableVersion:
    """A stable ``MAJOR.MINOR.PATCH`` version with numeric ordering."""

    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class ReleaseEntry:
    """One release heading and its exact GitHub-Release-ready body."""

    version: StableVersion
    heading: str
    body: str


@dataclass(frozen=True)
class ReleaseRange:
    """Available entries in an upgrade range and whether older history is absent."""

    entries: tuple[ReleaseEntry, ...]
    older_history_gap: bool


def parse_stable_version(value: str) -> StableVersion:
    """Parse an exact stable version or raise :class:`ChangelogError`."""
    match = re.fullmatch(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)",
        value,
    )
    if match is None:
        raise ChangelogError(f"unsupported version {value!r}; expected stable MAJOR.MINOR.PATCH")
    return StableVersion(*(int(part) for part in match.groups()))


def parse_releases(text: str) -> tuple[ReleaseEntry, ...]:
    """Return unique release entries in required newest-first order."""
    _validate_versionish_headings(text)
    matches = list(_RELEASE_HEADING.finditer(text))
    entries: list[ReleaseEntry] = []
    seen: set[StableVersion] = set()
    for index, match in enumerate(matches):
        version = parse_stable_version(match.group("version"))
        if version in seen:
            raise ChangelogError(f"duplicate changelog release heading for {version}")
        seen.add(version)
        body_start = match.end() + (1 if text[match.end() :].startswith("\n") else 0)
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[body_start:body_end]
        entries.append(ReleaseEntry(version, match.group(0).removesuffix("\r"), body))

    versions = [entry.version for entry in entries]
    if versions != sorted(versions, reverse=True):
        raise ChangelogError("stable changelog release headings must be ordered newest first")
    return tuple(entries)


def _validate_versionish_headings(text: str) -> None:
    """Reject release-like headings instead of silently omitting malformed ones."""
    for candidate in _VERSIONISH_HEADING.finditer(text):
        heading = candidate.group(0)
        match = _RELEASE_HEADING.fullmatch(heading)
        if match is None:
            raise ChangelogError(f"invalid stable release heading {heading!r}")
        try:
            date(
                int(match.group("year")),
                _MONTH_NUMBER[match.group("month")],
                int(match.group("day")),
            )
        except ValueError as exc:
            raise ChangelogError(f"invalid release date in heading {heading!r}") from exc


def release_entry(text: str, version: str) -> ReleaseEntry:
    """Return the one entry for *version*, or raise a precise error."""
    target = parse_stable_version(version)
    for entry in parse_releases(text):
        if entry.version == target:
            return entry
    raise ChangelogError(f"changelog has no release entry for {target}")


def releases_between(text: str, reviewed_through: str, pending_target: str) -> ReleaseRange:
    """Return available entries in ``(reviewed_through, pending_target]`` oldest first."""
    reviewed = parse_stable_version(reviewed_through)
    target = parse_stable_version(pending_target)
    if target <= reviewed:
        raise ChangelogError("pending target must be newer than reviewed-through version")
    releases = parse_releases(text)
    selected = tuple(
        sorted(
            (entry for entry in releases if reviewed < entry.version <= target),
            key=lambda entry: entry.version,
        )
    )
    oldest = min((entry.version for entry in releases), default=None)
    return ReleaseRange(selected, oldest is None or reviewed < oldest)
