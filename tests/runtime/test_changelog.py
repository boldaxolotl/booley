"""Contracts for stable packaged changelog parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.runtime import changelog
from booley.runtime.paths import changelog_path


def _history() -> str:
    return """# Changelog

## Unreleased

Future work.

## 1.2.0 - 31 AUG 2026

### New features

- New behavior.

## 1.1.0 - 30 AUG 2026

### Bug fixes

- Old fix.
"""


@pytest.mark.parametrize("value", ["1.2", "v1.2.3", "1.2.3rc1", "01.2.3", "latest"])
def test_stable_version_parser_rejects_ambiguous_versions(value: str) -> None:
    with pytest.raises(changelog.ChangelogError, match=r"stable MAJOR\.MINOR\.PATCH"):
        changelog.parse_stable_version(value)


def test_release_entries_are_exact_and_newest_first() -> None:
    entries = changelog.parse_releases(_history())

    assert [str(entry.version) for entry in entries] == ["1.2.0", "1.1.0"]
    assert entries[0].heading == "## 1.2.0 - 31 AUG 2026"
    assert entries[0].body.startswith("\n### New features")
    assert "Old fix" not in entries[0].body


def test_crlf_headings_are_accepted_without_normalizing_body() -> None:
    text = _history().replace("\n", "\r\n")

    entry = changelog.release_entry(text, "1.2.0")

    assert entry.heading == "## 1.2.0 - 31 AUG 2026"
    assert entry.body.startswith("\r\n### New features")


def test_duplicate_and_misordered_entries_are_rejected() -> None:
    duplicate = _history() + "\n## 1.2.0 - 29 AUG 2026\n"
    with pytest.raises(changelog.ChangelogError, match="duplicate"):
        changelog.parse_releases(duplicate)

    misordered = _history().replace("1.2.0", "1.0.0")
    with pytest.raises(changelog.ChangelogError, match="newest first"):
        changelog.parse_releases(misordered)


@pytest.mark.parametrize(
    "heading",
    [
        "## v1.2.0 - 31 AUG 2026",
        "## 1.2 - 31 AUG 2026",
        "## 01.2.0 - 31 AUG 2026",
        "## 1.2.0",
        "## 1.2.0 - 31 August 2026",
        "## 1.2.0 - 31 FEB 2026",
    ],
)
def test_malformed_release_like_headings_are_rejected(heading: str) -> None:
    with pytest.raises(changelog.ChangelogError, match=r"release (heading|date)"):
        changelog.parse_releases(f"# Changelog\n\n{heading}\n")


def test_release_range_is_oldest_first_and_reports_history_boundary() -> None:
    covered = changelog.releases_between(_history(), "1.1.0", "1.2.0")
    crossing = changelog.releases_between(_history(), "1.0.0", "1.2.0")

    assert [str(entry.version) for entry in covered.entries] == ["1.2.0"]
    assert covered.older_history_gap is False
    assert [str(entry.version) for entry in crossing.entries] == ["1.1.0", "1.2.0"]
    assert crossing.older_history_gap is True


def test_packaged_changelog_is_byte_identical_to_public_document() -> None:
    public = Path(__file__).resolve().parents[2] / "CHANGELOG.md"

    assert changelog_path().read_bytes() == public.read_bytes()


def test_packaged_changelog_retains_pre_review_release_history() -> None:
    release_range = changelog.releases_between(
        changelog_path().read_text(encoding="utf-8"),
        "0.2.6",
        "0.2.10",
    )

    assert [str(entry.version) for entry in release_range.entries] == [
        "0.2.7",
        "0.2.8",
        "0.2.9",
        "0.2.10",
    ]
