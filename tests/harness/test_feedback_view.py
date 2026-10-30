"""``booley feedback list`` renders an aligned table in the harness view."""

from __future__ import annotations

import json
from argparse import ArgumentParser

import pytest

from booley.feedback import cli as feedback_cli
from booley.feedback.findings import Finding, append, read_log
from booley.harness import booley, colors
from booley.runtime import booley_dir as booley_dir_mod

_CREATED = "2026-01-01T00:00:00Z"
_EMOJI = "🔴🟡🔵💬📣✅"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A Project with a Findings Log, and no ambient colour or Booley dir."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    root = tmp_path / "project"
    (root / ".booley").mkdir(parents=True)
    (root / ".booley" / "booley.toml").write_text(
        '[project]\nname = "project"\n', encoding="utf-8"
    )
    booley_dir_mod.reset_cache()
    yield root
    booley_dir_mod.reset_cache()


def _log(project, *findings: Finding) -> None:
    for finding in findings:
        finding.created = _CREATED
        append(finding, project / ".booley")


def _run_list(project, capsys, *flags: str) -> tuple[str, str]:
    """Run ``booley feedback list`` and return what it wrote to stdout and stderr."""
    parser = ArgumentParser()
    feedback_cli.add_subparser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["feedback", "list", *flags])
    capsys.readouterr()
    assert booley._cmd_feedback(args, project) == 0
    captured = capsys.readouterr()
    return captured.out, captured.err


def _list(project, capsys, *flags: str) -> str:
    return _run_list(project, capsys, *flags)[0]


def _mixed_log(project) -> None:
    _log(
        project,
        Finding(title="boom", severity="blocker", bucket="booley"),
        Finding(title="confusing", kind="friction", bucket="project"),
        Finding(title="genuinely useful on a real SoC", kind="impression"),
        Finding(title="doctor was clean", kind="win"),
        Finding(
            title="x" * 60,
            severity="note",
            bucket="booley",
            filed="https://example.invalid/3",
        ),
    )


def test_list_renders_an_aligned_table_with_words_not_emoji(project, capsys):
    _mixed_log(project)

    out = _list(project, capsys)

    lines = out.splitlines()
    assert lines[:7] == [
        "  ID    Kind            Bucket    Title" + " " * 46 + "Status",
        "  ───   ─────────────   ───────   " + "─" * 48 + "   " + "─" * 32,
        "  F-1   bug (blocker)   booley    boom" + " " * 44 + "   needs evidence",
        "  F-2   friction        project   confusing",
        "  F-3   impression      unknown   genuinely useful on a real SoC",
        "  F-4   win             —         doctor was clean",
        "  F-5   bug (note)      booley    " + "x" * 47 + "…   filed: https://example.invalid/3",
    ]
    assert lines[7:] == [
        "",
        "  bugs 2 (blocker 1 · workaround 0 · note 1)   friction 1   impressions 1   wins 1",
    ]
    title_column = lines[0].index("Title")
    assert all(line[title_column] != " " for line in lines[2:7])
    assert not any(char in out for char in _EMOJI)
    assert "|" not in out


def test_list_spells_bug_severity_in_plain_text(project, capsys):
    _log(
        project,
        Finding(title="a", severity="blocker", bucket="project"),
        Finding(title="b", severity="workaround", bucket="project"),
        Finding(title="c", severity="note", bucket="project"),
        Finding(title="d", kind="friction", bucket="project"),
    )

    out = _list(project, capsys)

    # Kind is the second column: indent 2 + ID 3 + gap 3 = offset 8, width 16.
    rows = out.splitlines()[2:6]
    assert [row[8:24].strip() for row in rows] == [
        "bug (blocker)",
        "bug (workaround)",
        "bug (note)",
        "friction",
    ]
    assert "\x1b[" not in out


def test_list_cuts_long_titles_with_an_ellipsis(project, capsys):
    _log(project, Finding(title="x" * 60, bucket="project"))

    out = _list(project, capsys)

    assert "x" * 47 + "…" in out
    assert "x" * 48 not in out


def test_list_colors_bug_kind_by_severity_and_strips_to_plain(project, capsys, monkeypatch):
    _log(
        project,
        Finding(title="a", severity="blocker", bucket="project"),
        Finding(title="b", severity="workaround", bucket="project"),
        Finding(title="c", severity="note", bucket="project"),
        Finding(title="d", kind="friction", bucket="booley"),
    )
    plain = _list(project, capsys)

    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    colored = _list(project, capsys)

    assert "\x1b[31mbug (blocker)\x1b[0m" in colored
    assert "\x1b[38;5;208mbug (workaround)\x1b[0m" in colored
    assert "\x1b[2mbug (note)\x1b[0m" in colored
    assert "\x1b[38;5;208mneeds evidence\x1b[0m" in colored
    assert colors.strip_ansi(colored) == plain


_JSON_GOLDEN = """[
  {
    "title": "boom",
    "kind": "finding",
    "severity": "blocker",
    "bucket": "booley",
    "exposed_by": "",
    "step": "",
    "component": "",
    "repro": "",
    "observed": "",
    "expected": "",
    "notes": "",
    "sentiment": "",
    "verified_against_source": false,
    "origin": "setup",
    "attachments": [],
    "filed": "",
    "filed_at": "",
    "id": "F-1",
    "created": "2026-01-01T00:00:00Z"
  },
  {
    "title": "doctor was clean",
    "kind": "win",
    "severity": "note",
    "bucket": "unknown",
    "exposed_by": "",
    "step": "",
    "component": "",
    "repro": "",
    "observed": "",
    "expected": "",
    "notes": "",
    "sentiment": "",
    "verified_against_source": false,
    "origin": "setup",
    "attachments": [],
    "filed": "",
    "filed_at": "",
    "id": "F-2",
    "created": "2026-01-01T00:00:00Z"
  }
]
"""


def test_list_json_is_byte_identical_while_human_view_is_a_table(project, capsys):
    _log(
        project,
        Finding(title="boom", severity="blocker", bucket="booley"),
        Finding(title="doctor was clean", kind="win"),
    )

    as_json = _list(project, capsys, "--json")
    human = _list(project, capsys)

    assert as_json == _JSON_GOLDEN
    assert json.loads(as_json)[0]["id"] == "F-1"
    assert human.splitlines()[0].split() == ["ID", "Kind", "Bucket", "Title", "Status"]


def test_list_keeps_one_row_per_entry_for_control_characters_in_user_text(project, capsys):
    _log(
        project,
        Finding(title="two\nlines\tand \x1b[31mred", bucket="project"),
        Finding(title="after", bucket="project", filed="a\nb"),
    )

    out = _list(project, capsys)

    rows = out.splitlines()[2:4]
    assert rows[0].endswith("two lines and red")
    assert rows[1].split() == ["F-2", "bug", "(note)", "project", "after", "filed:", "a", "b"]
    assert "\x1b[" not in out


def test_list_filters_by_kind_and_bucket_but_totals_count_the_whole_log(project, capsys):
    _mixed_log(project)

    by_kind = _list(project, capsys, "--kind", "win")
    by_bucket = _list(project, capsys, "--bucket", "project")

    assert [line.split()[0] for line in by_kind.splitlines()[2:3]] == ["F-4"]
    assert "F-1" not in by_kind
    assert [line.split()[0] for line in by_bucket.splitlines()[2:3]] == ["F-2"]
    assert "F-4" not in by_bucket
    for out in (by_kind, by_bucket):
        assert out.splitlines()[-1].strip() == (
            "bugs 2 (blocker 1 · workaround 0 · note 1)   friction 1   impressions 1   wins 1"
        )


def test_list_says_nothing_is_logged_for_an_empty_log(project, capsys):
    out = _list(project, capsys)

    assert out.startswith("Nothing logged yet (")
    assert "ID" not in out


def test_list_warns_on_stderr_about_unparseable_lines(project, capsys):
    _log(project, Finding(title="boom", bucket="booley"))
    log_path = read_log(project / ".booley").path
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")

    out, err = _run_list(project, capsys)

    assert "boom" in out
    assert "1 unparseable line(s) skipped" in err
