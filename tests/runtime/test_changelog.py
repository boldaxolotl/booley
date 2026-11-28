"""Contracts for stable packaged changelog parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.runtime import changelog

_PUBLIC_CHANGELOG = Path(__file__).resolve().parents[2] / "CHANGELOG.md"


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


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_release_entries_are_exact_newest_first_and_keep_line_endings(newline: str) -> None:
    text = _history().replace("\n", newline)

    entries = changelog.parse_releases(text)

    assert [str(entry.version) for entry in entries] == ["1.2.0", "1.1.0"]
    assert changelog.release_entry(text, "1.2.0") == entries[0]
    assert entries[0].heading == "## 1.2.0 - 31 AUG 2026"
    assert entries[0].body.startswith(f"{newline}### New features")
    assert "Old fix" not in entries[0].body


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (_history() + "\n## 1.2.0 - 29 AUG 2026\n", "duplicate"),
        (_history().replace("1.2.0", "1.0.0"), "newest first"),
        *(
            (f"# Changelog\n\n{heading}\n", r"release (heading|date)")
            for heading in (
                "## v1.2.0 - 31 AUG 2026",
                "## 1.2 - 31 AUG 2026",
                "## 01.2.0 - 31 AUG 2026",
                "## 1.2.0",
                "## 1.2.0 - 31 August 2026",
                "## 1.2.0 - 31 FEB 2026",
            )
        ),
    ],
)
def test_invalid_release_headings_are_rejected(text: str, match: str) -> None:
    with pytest.raises(changelog.ChangelogError, match=match):
        changelog.parse_releases(text)


def test_release_range_is_oldest_first_and_reports_history_boundary() -> None:
    covered = changelog.releases_between(_history(), "1.1.0", "1.2.0")
    crossing = changelog.releases_between(_history(), "1.0.0", "1.2.0")

    assert [str(entry.version) for entry in covered.entries] == ["1.2.0"]
    assert covered.older_history_gap is False
    assert [str(entry.version) for entry in crossing.entries] == ["1.1.0", "1.2.0"]
    assert crossing.older_history_gap is True


def test_changelog_retains_pre_review_release_history() -> None:
    release_range = changelog.releases_between(
        _PUBLIC_CHANGELOG.read_text(encoding="utf-8"), "0.2.6", "0.2.10"
    )

    assert [str(entry.version) for entry in release_range.entries] == [
        "0.2.7",
        "0.2.8",
        "0.2.9",
        "0.2.10",
    ]


def test_next_release_review_lists_entries_drafted_from_unreleased() -> None:
    """Cutting the next release must keep the pre-release entries reviewable.

    Release prep drafts the dated heading directly under ``## Unreleased``, so
    everything that was unreleased becomes the next release's body. The probe is
    the section's last line, which comes from the oldest unreleased entry. It is
    taken from the changelog itself because the release drafter rewords entries.
    """
    text = _PUBLIC_CHANGELOG.read_text(encoding="utf-8")
    repo_version = (
        (Path(__file__).resolve().parents[2] / "VERSION").read_text(encoding="utf-8").strip()
    )
    current = changelog.parse_stable_version(repo_version)
    next_release = f"{current.major}.{current.minor}.{current.patch + 1}"
    marker = "## Unreleased\n"
    assert marker in text
    unreleased = text.split(marker, 1)[1].split("\n## ", 1)[0]
    probe = unreleased.strip().splitlines()[-1] if unreleased.strip() else ""
    # Simulate release prep: the drafted heading goes directly under Unreleased.
    drafted = text.replace(marker, f"{marker}\n## {next_release} - 28 OCT 2026\n", 1)

    review = changelog.releases_between(drafted, "0.3.0", next_release)

    assert probe in "".join(entry.body for entry in review.entries)
