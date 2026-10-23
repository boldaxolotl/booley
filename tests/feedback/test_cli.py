"""Tests for the ``booley feedback`` subcommands themselves.

The unit tests cover what the reports contain; these cover the local logging,
reporting, redaction, and explicit-export wiring an agent actually touches.
"""

from __future__ import annotations

import argparse
import json
import subprocess

import pytest

from booley.feedback import cli
from booley.feedback.findings import read_log
from booley.feedback.storage import feedback_storage_dir
from booley.runtime import booley_dir as booley_dir_mod


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "rocketwidget"
    (root / ".booley").mkdir(parents=True)
    (root / ".booley" / "booley.toml").write_text(
        '[project]\nname = "rocketwidget"\n', encoding="utf-8"
    )
    return root


@pytest.fixture
def run(project):
    """Invoke a subcommand through the real parser, as the CLI would.

    The cache reset is not optional: ``resolve_booley_dir`` memoizes at module
    level, so without it every test after the first writes into the first test's
    tmp project and the assertions read someone else's log.
    """
    booley_dir_mod.reset_cache()

    def _run(*argv):
        parser = argparse.ArgumentParser()
        cli.add_subparser(parser.add_subparsers(dest="command"))
        return cli.run(parser.parse_args(["feedback", *argv]), project)

    yield _run
    # And again on the way out: a tmp path left in the cache would follow the
    # next test module into a directory that no longer exists.
    booley_dir_mod.reset_cache()


def _log(project):
    return read_log(project / ".booley")


def _commit_project_data(state, monkeypatch):
    from tests.project_runtime_git_support import initialize_repository

    initialize_repository(state, state, monkeypatch)


def _project_data_status(state):
    from tests.project_runtime_git_support import git

    return git(state, "status", "--short", "--untracked-files=all")


def test_source_feedback_uses_shared_git_metadata(tmp_path):
    source = tmp_path / "booley-source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        "[tool.booley]\nsource_checkout = true\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(source)], check=True, timeout=10)
    parser = argparse.ArgumentParser()
    cli.add_subparser(parser.add_subparsers(dest="command"))

    assert cli.run(parser.parse_args(["feedback", "add", "--title", "boom"]), source) == 0

    state = source / ".git" / "booley-feedback"
    assert read_log(state).entries[0].title == "boom"
    assert not (source / ".booley").exists()


def test_source_feedback_is_shared_by_linked_worktree(tmp_path):
    source = tmp_path / "booley-source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        "[tool.booley]\nsource_checkout = true\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True, timeout=10)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "add",
            "pyproject.toml",
        ],
        check=True,
        timeout=10,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
        timeout=10,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(source), "worktree", "add", "-q", "-b", "linked", str(linked)],
        check=True,
        timeout=10,
    )

    assert feedback_storage_dir(source) == feedback_storage_dir(linked)
    assert feedback_storage_dir(linked) == source / ".git" / "booley-feedback"


class TestLogging:
    def test_add_records_origin_and_attachments(self, run, project):
        assert run("add", "--title", "boom", "--origin", "bug", "--attach", "run.log") == 0
        entry = _log(project).entries[0]
        assert entry.origin == "bug"
        assert entry.attachments == ["run.log"]

    def test_attach_is_repeatable(self, run, project):
        run("add", "--title", "boom", "--attach", "a.log", "--attach", "b.log")
        assert _log(project).entries[0].attachments == ["a.log", "b.log"]

    def test_origin_defaults_to_setup(self, run, project):
        """The setup skill passes no --origin and must keep working unchanged."""
        run("add", "--title", "boom")
        assert _log(project).entries[0].origin == "setup"

    def test_friction_logs_a_friction_entry(self, run, project):
        assert (
            run(
                "friction",
                "--title",
                "'0 targets matched' reads like a crash",
                "--component",
                "targets",
                "--expected",
                "a line saying the filter matched nothing",
                "--origin",
                "bug",
            )
            == 0
        )
        entry = _log(project).entries[0]
        assert entry.kind == "friction"
        assert entry.bucket == "booley"  # friction is about Booley by definition
        assert entry.is_filable()

    def test_say_logs_an_impression_with_nothing_but_a_message(self, run, project):
        """The whole point of `say`: one argument, no interrogation."""
        assert run("say", "the waveform flow is the best part of this thing") == 0
        entry = _log(project).entries[0]
        assert entry.kind == "impression"
        assert entry.bucket == "booley"
        assert entry.origin == "impression"
        assert entry.sentiment == "mixed"  # unstated is a general take, not praise
        assert entry.is_filable()  # no evidence bar — it goes upstream as-is

    def test_say_carries_the_sentiment_and_the_long_version(self, run, project):
        run(
            "say",
            "I want per-Target coverage in the run report",
            "--sentiment",
            "wish",
            "--component",
            "sim",
            "--notes",
            "the numbers exist, they just are not summarized anywhere",
        )
        entry = _log(project).entries[0]
        assert entry.sentiment == "wish"
        assert entry.component == "sim"
        assert "not summarized" in entry.notes

    def test_say_rejects_an_invented_sentiment(self, run, project):
        """argparse choices, not a free-text field — the taxonomy stays small."""
        with pytest.raises(SystemExit):
            run("say", "meh", "--sentiment", "grumpy")

    def test_say_never_nags_for_evidence(self, run, project, capsys):
        """An opinion has no reproduction, and asking for one drives people off."""
        run("say", "the setup grill is exhausting", "--sentiment", "gripe")
        assert "needs these" not in capsys.readouterr().err

    def test_triage_adds_attachments_without_dropping_the_old_ones(self, run, project):
        run("add", "--title", "boom", "--attach", "first.log")
        run("triage", "F-1", "--attach", "second.log")
        assert _log(project).entries[0].attachments == ["first.log", "second.log"]

    def test_triage_does_not_duplicate_the_same_attachment(self, run, project):
        run("add", "--title", "boom", "--attach", "first.log")
        run("triage", "F-1", "--attach", "first.log")
        assert _log(project).entries[0].attachments == ["first.log"]


class TestFiled:
    def test_it_stamps_the_named_findings(self, run, project):
        run("add", "--title", "boom", "--origin", "bug")
        run("add", "--title", "bang", "--origin", "bug")
        assert run("filed", "F-1", "--url", "https://example.invalid/1") == 0
        entries = _log(project).entries
        assert entries[0].filed == "https://example.invalid/1"
        assert entries[1].filed == ""

    def test_it_defaults_to_manual(self, run, project):
        run("add", "--title", "boom")
        run("filed", "F-1")
        assert _log(project).entries[0].filed == "manual"

    def test_an_unknown_id_fails_without_touching_the_log(self, run, project):
        run("add", "--title", "boom")
        assert run("filed", "F-1", "F-99") == 1
        assert _log(project).entries[0].filed == ""


class TestReporting:
    @pytest.fixture
    def filable(self, run):
        run(
            "add",
            "--title",
            "simulate exits 2",
            "--bucket",
            "booley",
            "--repro",
            "booley flow sim",
            "--observed",
            "exit 2",
            "--expected",
            "a result",
        )

    def test_report_writes_only_the_user_report(self, run, project, filable, capsys):
        assert run("report") == 0
        state = project / ".booley"
        assert (state / "SETUP-REPORT.md").is_file()
        assert not (state / "BOOLEY-FEEDBACK.md").exists()
        assert "no redacted file was written" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "finding_args",
        [
            ("add", "--title", "project note", "--bucket", "project"),
            ("add", "--title", "bug note", "--bucket", "project", "--origin", "bug"),
        ],
        ids=["setup-report", "bug-report"],
    )
    def test_user_report_keeps_project_data_repository_clean(
        self, run, project, finding_args, monkeypatch
    ):
        state = project / ".booley"
        _commit_project_data(state, monkeypatch)
        run(*finding_args)
        assert _project_data_status(state) == ""

        assert run("report") == 0
        assert _project_data_status(state) == ""

    def test_report_warns_that_an_existing_export_was_not_refreshed(
        self, run, project, filable, capsys
    ):
        export = project / ".booley" / "BOOLEY-FEEDBACK.md"
        export.write_text("old export", encoding="utf-8")
        assert run("report") == 0
        assert export.read_text(encoding="utf-8") == "old export"
        assert "may be stale" in capsys.readouterr().out

    def test_export_is_the_explicit_redacted_file_path(self, run, project, filable):
        assert run("export", "--all") == 0
        assert (project / ".booley" / "BOOLEY-FEEDBACK.md").is_file()

    def test_export_accepts_an_output_override(self, run, project, filable, tmp_path):
        target = tmp_path / "sanitized.md"
        assert run("export", "--all", "--output", str(target)) == 0
        assert target.is_file()
        assert not (project / ".booley" / "BOOLEY-FEEDBACK.md").exists()

    def test_export_honours_the_selected_finding_ids(self, run, project, filable, tmp_path):
        run(
            "add",
            "--title",
            "only this finding",
            "--origin",
            "bug",
            "--bucket",
            "booley",
            "--repro",
            "repro",
            "--observed",
            "observed",
            "--expected",
            "expected",
        )
        target = tmp_path / "selected.md"
        assert run("export", "F-2", "--output", str(target)) == 0
        body = target.read_text(encoding="utf-8")
        assert "only this finding" in body
        assert "simulate exits 2" not in body

    @pytest.mark.parametrize("command", ["preview", "submit"])
    def test_removed_transmission_commands_are_not_registered(self, run, command):
        with pytest.raises(SystemExit):
            run(command)


class TestList:
    def test_it_flags_filed_entries_instead_of_nagging_for_evidence(self, run, capsys):
        run("add", "--title", "boom", "--bucket", "booley")
        run("filed", "F-1", "--url", "https://example.invalid/3")
        capsys.readouterr()
        run("list")
        out = capsys.readouterr().out
        assert "[filed: https://example.invalid/3]" in out
        assert "needs evidence" not in out

    def test_the_counts_add_up(self, run, capsys):
        """Bugs, friction, impressions and wins are separate tallies — one bug
        plus one friction is not 'two findings, one of which is a blocker'."""
        run("add", "--title", "boom", "--severity", "blocker", "--bucket", "booley")
        run("friction", "--title", "confusing", "--component", "doctor", "--expected", "x")
        run("say", "genuinely useful on a real SoC", "--sentiment", "praise")
        run("win", "--title", "doctor was clean")
        capsys.readouterr()
        run("list")
        out = capsys.readouterr().out
        assert "1 finding(s)" in out
        assert "💬 1 friction" in out
        assert "📣 1 impression(s)" in out
        assert "1 win(s)" in out


@pytest.mark.parametrize("origin", ["setup", "bug"])
def test_actual_feedback_writers_keep_versioned_project_clean(run, project, monkeypatch, origin):
    from booley.feedback.materialize import materialize_attachments
    from booley.feedback.render import Environment
    from tests.project_runtime_git_support import assert_clean, initialize_repository

    state = project / ".booley"
    initialize_repository(state, state, monkeypatch)
    environment = Environment(doctor_deep_clean=True)
    monkeypatch.setattr(
        "booley.feedback.render.collect_environment", lambda *_a, **_kw: environment
    )
    source = project / "attached.log"
    source.write_text("local attachment\n", encoding="utf-8")
    assert (
        run(
            "add",
            "--title",
            "local note",
            "--origin",
            origin,
            "--attach",
            str(source),
            "--bucket",
            "booley",
            "--repro",
            "run",
            "--observed",
            "error",
            "--expected",
            "pass",
        )
        == 0
    )
    assert (state / "findings.jsonl").is_file()
    assert_clean(state)
    assert run("triage", "F-1", "--bucket", "booley") == 0
    assert _log(project).entries[0].bucket == "booley"
    assert_clean(state)
    assert materialize_attachments(state, [source]) == (source,)
    assert list((state / "setup-evidence/attachments").rglob("*.txt"))
    assert list((state / "setup-evidence/attachments").rglob("*.txt.json"))
    assert_clean(state)
    assert run("report") == 0
    name = "SETUP-REPORT.md" if origin == "setup" else "FEEDBACK-REPORT.md"
    assert (state / name).is_file()
    assert_clean(state)
    assert run("export", "--all") == 0
    assert (state / "BOOLEY-FEEDBACK.md").is_file()
    assert_clean(state)


@pytest.fixture
def legacy_duplicate_log(project):
    from booley.feedback.findings import Finding, log_path

    entries = [
        Finding(
            title=title,
            id=finding_id,
            created="2026-01-01T00:00:00Z",
            kind="impression",
            bucket="booley",
            notes="private body",
            attachments=["never-include-attachment-path-in-error.log"],
        )
        for title, finding_id in [
            ("unique", "F-1"),
            ("first match", "F-2"),
            ("second match", "F-2"),
        ]
    ]
    path = log_path(project / ".booley")
    path.write_text("".join(entry.to_json() + "\n" for entry in entries), encoding="utf-8")
    return path


@pytest.mark.parametrize("command", ["filed", "triage", "export"])
def test_explicit_duplicate_id_refuses_every_match_without_output(
    run, project, legacy_duplicate_log, capsys, command, tmp_path
):
    path = legacy_duplicate_log
    before = path.read_bytes()
    output = tmp_path / "export.md"
    args = [command, "F-2"]
    if command == "filed":
        args = [command, "F-1", "F-2"]  # The entire batch must remain unchanged.
    elif command == "triage":
        args += ["--bucket", "project"]
    else:
        args += ["--output", str(output)]
    assert run(*args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    for detail in (
        "F-2",
        str(path),
        "row 2",
        "row 3",
        "first match",
        "second match",
        "2026-01-01T00:00:00Z",
    ):
        assert detail in captured.err
    assert "private body" not in captured.err
    assert "never-include-attachment-path" not in captured.err
    assert path.read_bytes() == before
    assert not output.exists()
    assert not (project / ".booley" / "BOOLEY-FEEDBACK.md").exists()
    assert not path.with_suffix(".jsonl.tmp").exists()


def test_export_all_keeps_every_legacy_duplicate_and_redacts(
    run, project, legacy_duplicate_log, tmp_path
):
    path = legacy_duplicate_log
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        row["notes"] = f"project path {project}"
        row["attachments"] = []
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    before = path.read_bytes()
    output = tmp_path / "all.md"
    assert run("export", "--all", "--output", str(output)) == 0
    body = output.read_text()
    assert "first match" in body
    assert "second match" in body
    assert "unique" in body
    assert str(project) not in body
    assert "rocketwidget" not in body
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "argv",
    [
        ("add", "--title", "blocked"),
        ("friction", "--title", "blocked"),
        ("say", "blocked"),
        ("win", "--title", "blocked"),
        ("filed", "F-1"),
        ("triage", "F-1", "--bucket", "booley"),
    ],
)
def test_mutating_dispatch_reports_lock_timeout_without_success(
    run, project, monkeypatch, capsys, argv
):
    from booley.feedback import findings
    from tests.feedback.test_findings import holding_findings_lock

    directory = project / ".booley"
    findings.append(findings.Finding(title="original"), directory)
    path = findings.log_path(directory)
    before = path.read_bytes()
    monkeypatch.setattr(findings, "_LOCK_TIMEOUT_S", 0.01)
    with holding_findings_lock(directory):
        assert run(*argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "findings.lock" in captured.err
    assert "busy" in captured.err
    assert str(path) in captured.err
    assert path.read_bytes() == before
    assert not path.with_suffix(".jsonl.tmp").exists()


def test_redact_reports_invalid_redact_extra(run, project, tmp_path, capsys):
    toml = project / ".booley" / "booley.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8") + '\n[feedback]\nredact_extra = "Skunkworks"\n',
        encoding="utf-8",
    )
    sample = tmp_path / "sample.txt"
    sample.write_text("part of the Skunkworks program\n", encoding="utf-8")

    assert run("redact", "--file", str(sample)) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Error: booley.toml [feedback].redact_extra must be a list of strings" in captured.err


@pytest.mark.parametrize("command", ["filed", "triage", "export"])
def test_missing_explicit_id_does_not_write_log_or_export(
    run, project, legacy_duplicate_log, tmp_path, capsys, command
):
    before = legacy_duplicate_log.read_bytes()
    output = tmp_path / "missing.md"
    args = [command, "F-99"]
    if command == "export":
        args += ["--output", str(output)]
    assert run(*args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "F-99" in captured.err
    assert legacy_duplicate_log.read_bytes() == before
    assert not output.exists()
    assert not (project / ".booley" / "BOOLEY-FEEDBACK.md").exists()


@pytest.mark.parametrize("command", ["filed", "triage"])
def test_corrupt_log_mutation_preserves_raw_bytes(run, project, capsys, command):
    path = project / ".booley" / "findings.jsonl"
    path.write_text('{"title":"good","id":"F-1"}\n{unparseable evidence\n')
    before = path.read_bytes()
    assert run(command, "F-1") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unparseable" in captured.err
    assert "would be lost" in captured.err
    assert path.read_bytes() == before
    assert not path.with_suffix(".jsonl.tmp").exists()


def test_ambiguous_export_preserves_existing_output(run, legacy_duplicate_log, tmp_path, capsys):
    target = tmp_path / "existing.md"
    target.write_text("prior export")
    before = legacy_duplicate_log.read_bytes()
    assert run("export", "F-2", "--output", str(target)) == 1
    assert capsys.readouterr().out == ""
    assert target.read_text() == "prior export"
    assert legacy_duplicate_log.read_bytes() == before
