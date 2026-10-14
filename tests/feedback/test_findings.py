"""Tests for the setup/port findings log.

The log's job is to survive: concurrent writers, a corrupt line, a newer Booley
having added a field. Everything downstream (the report and outbound view)
reads through it, so a log that loses or mangles an entry loses a finding.
"""

from __future__ import annotations

import json

import pytest

from booley.feedback.findings import (
    CorruptFindingsLogError,
    Finding,
    append,
    log_path,
    read_log,
    rewrite,
)


@pytest.fixture
def booley_dir(tmp_path):
    d = tmp_path / ".booley"
    d.mkdir()
    return d


def test_append_assigns_sequential_ids(booley_dir):
    for expected in ("F-1", "F-2", "F-3"):
        assert append(Finding(title="x"), booley_dir).id == expected


def test_an_impression_is_filable_with_no_evidence_at_all(booley_dir):
    """The evidence bar that protects the bug queue must not gate opinions."""
    entry = append(
        Finding(title="Booley is not worth the setup cost", kind="impression", bucket="booley"),
        booley_dir,
    )
    assert entry.missing_evidence() == []
    assert entry.is_filable()


def test_an_impression_is_not_counted_as_a_bug(booley_dir):
    append(Finding(title="great framework", kind="impression", bucket="booley"), booley_dir)
    log = read_log(booley_dir)
    assert log.impressions and not log.bugs
    assert log.counts() == {"blocker": 0, "workaround": 0, "note": 0}


def test_sentiment_is_validated(booley_dir):
    with pytest.raises(ValueError, match="sentiment"):
        Finding(title="x", kind="impression", sentiment="grumpy")


def test_append_continues_numbering_across_processes(booley_dir):
    """A fresh process must not restart at F-1 and collide with the log."""
    append(Finding(title="a"), booley_dir)
    append(Finding(title="b"), booley_dir)
    # Simulate a separate invocation: nothing cached, ids come from the file.
    assert append(Finding(title="c"), booley_dir).id == "F-3"


def test_append_creates_the_booley_dir(tmp_path):
    """Findings must be loggable before setup has finished building the dir."""
    missing = tmp_path / "nope" / ".booley"
    append(Finding(title="early"), missing)
    assert log_path(missing).is_file()


def test_read_log_of_missing_file_is_empty_not_an_error(booley_dir):
    log = read_log(booley_dir)
    assert log.entries == []
    assert log.corrupt_lines == 0


def test_corrupt_line_costs_one_finding_not_the_log(booley_dir):
    append(Finding(title="good one"), booley_dir)
    with log_path(booley_dir).open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
    append(Finding(title="another good one"), booley_dir)

    log = read_log(booley_dir)
    assert [e.title for e in log.entries] == ["good one", "another good one"]
    assert log.corrupt_lines == 1


def test_blank_lines_are_ignored_silently(booley_dir):
    append(Finding(title="x"), booley_dir)
    with log_path(booley_dir).open("a", encoding="utf-8") as fh:
        fh.write("\n   \n")
    log = read_log(booley_dir)
    assert len(log.entries) == 1
    assert log.corrupt_lines == 0


def test_unknown_fields_from_a_newer_booley_are_dropped_not_fatal(booley_dir):
    """Forward compatibility: a later version's extra field must still render."""
    with log_path(booley_dir).open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"title": "from the future", "invented_field": 42}) + "\n")
    log = read_log(booley_dir)
    assert [e.title for e in log.entries] == ["from the future"]


def test_wins_and_findings_are_separated(booley_dir):
    append(Finding(title="broke"), booley_dir)
    append(Finding(title="worked", kind="win"), booley_dir)
    log = read_log(booley_dir)
    assert [f.title for f in log.findings] == ["broke"]
    assert [w.title for w in log.wins] == ["worked"]


def test_counts_are_per_severity(booley_dir):
    append(Finding(title="a", severity="blocker"), booley_dir)
    append(Finding(title="b", severity="note"), booley_dir)
    append(Finding(title="c", severity="note"), booley_dir)
    append(Finding(title="w", kind="win"), booley_dir)  # must not be counted
    assert read_log(booley_dir).counts() == {"blocker": 1, "workaround": 0, "note": 2}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": "x", "severity": "catastrophic"},
        {"title": "x", "bucket": "someone-elses"},
        {"title": "x", "kind": "rumour"},
        {"title": "   "},
    ],
)
def test_invalid_entries_are_rejected_at_construction(kwargs):
    with pytest.raises(ValueError):
        Finding(**kwargs)


def test_created_timestamp_is_stamped_once(booley_dir):
    finding = append(Finding(title="x"), booley_dir)
    assert finding.created
    # Round-tripping must not restamp it — the log records when it happened.
    assert read_log(booley_dir).entries[0].created == finding.created


def test_rewrite_replaces_the_log_atomically(booley_dir):
    append(Finding(title="a"), booley_dir)
    append(Finding(title="b"), booley_dir)
    entries = read_log(booley_dir).entries
    entries[0].bucket = "booley"

    rewrite(entries, booley_dir)

    reread = read_log(booley_dir)
    assert [e.bucket for e in reread.entries] == ["booley", "unknown"]
    # No temp file left behind for the next reader to trip over.
    assert not list(booley_dir.glob("*.tmp"))


def test_rewrite_refuses_to_drop_unparseable_raw_evidence(booley_dir):
    append(Finding(title="good"), booley_dir)
    path = log_path(booley_dir)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("{malformed but valuable evidence\n")
    before = path.read_bytes()

    with pytest.raises(CorruptFindingsLogError, match="would be lost"):
        rewrite(read_log(booley_dir).entries, booley_dir)

    assert path.read_bytes() == before


class TestFilability:
    """The evidence gate. This is what keeps the issue tracker usable."""

    def test_impression_without_evidence_is_not_filable(self):
        finding = Finding(title="the output was confusing", bucket="booley")
        assert not finding.is_filable()
        assert finding.missing_evidence() == ["repro", "observed", "expected"]

    def test_full_evidence_is_filable(self):
        finding = Finding(
            title="doctor fails on valid config",
            bucket="booley",
            repro="booley doctor",
            observed="FAIL",
            expected="PASS",
        )
        assert finding.is_filable()
        assert finding.missing_evidence() == []

    def test_partial_evidence_is_not_filable(self):
        finding = Finding(title="x", bucket="booley", repro="booley doctor", observed="FAIL")
        assert not finding.is_filable()
        assert finding.missing_evidence() == ["expected"]

    def test_docs_findings_need_no_reproduction(self):
        """ "The doc says X, the code does Y" is actionable without a command."""
        finding = Finding(
            title="CONFIG.md documents a removed table",
            bucket="docs",
            component="docs/user/CONFIG.md",
            observed="describes [sources.rtl], which no longer exists",
        )
        assert finding.is_filable()

    def test_docs_finding_still_needs_a_component_and_observation(self):
        assert not Finding(title="docs are wrong somewhere", bucket="docs").is_filable()

    def test_the_users_own_problems_are_never_filable(self):
        finding = Finding(
            title="verilator missing from PATH",
            bucket="project",
            repro="verilator --version",
            observed="not found",
            expected="a version",
        )
        assert not finding.is_filable()

    def test_untriaged_findings_are_never_filable(self):
        """Nothing goes upstream that nobody decided was Booley's fault."""
        finding = Finding(title="x", bucket="unknown", repro="c", observed="o", expected="e")
        assert not finding.is_filable()

    def test_a_win_is_never_filable(self):
        assert not Finding(title="worked", kind="win", bucket="booley").is_filable()
