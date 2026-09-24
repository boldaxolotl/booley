"""Tests for the ``booley feedback`` subcommands themselves.

The unit tests cover what the reports contain; these cover the local logging,
reporting, redaction, and explicit-export wiring an agent actually touches.
"""

from __future__ import annotations

import argparse
import subprocess

import pytest

from booley.feedback import cli
from booley.feedback.findings import read_log
from booley.feedback.storage import feedback_storage_dir
from booley.harness.init_cmd import PROJECT_GITIGNORE
from booley.runtime import project_dir as project_dir_mod


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "rocketwidget"
    (root / ".booley_project").mkdir(parents=True)
    (root / ".booley_project" / "booley.toml").write_text(
        '[project]\nname = "rocketwidget"\n', encoding="utf-8"
    )
    return root


@pytest.fixture
def run(project):
    """Invoke a subcommand through the real parser, as the CLI would.

    The cache reset is not optional: ``resolve_project_dir`` memoizes at module
    level, so without it every test after the first writes into the first test's
    tmp project and the assertions read someone else's log.
    """
    project_dir_mod.reset_cache()

    def _run(*argv):
        parser = argparse.ArgumentParser()
        cli.add_subparser(parser.add_subparsers(dest="command"))
        return cli.run(parser.parse_args(["feedback", *argv]), project)

    yield _run
    # And again on the way out: a tmp path left in the cache would follow the
    # next test module into a directory that no longer exists.
    project_dir_mod.reset_cache()


def _log(project):
    return read_log(project / ".booley_project")


def _commit_project_data(state):
    (state / ".gitignore").write_text(PROJECT_GITIGNORE, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(state)], check=True)
    subprocess.run(["git", "-C", str(state), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(state),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )


def _project_data_status(state):
    result = subprocess.run(
        ["git", "-C", str(state), "status", "--short"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


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
    assert not (source / ".booley_project").exists()


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
        state = project / ".booley_project"
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
    def test_user_report_keeps_project_data_repository_clean(self, run, project, finding_args):
        state = project / ".booley_project"
        run(*finding_args)
        _commit_project_data(state)

        assert run("report") == 0
        assert _project_data_status(state) == ""

    def test_report_warns_that_an_existing_export_was_not_refreshed(
        self, run, project, filable, capsys
    ):
        export = project / ".booley_project" / "BOOLEY-FEEDBACK.md"
        export.write_text("old export", encoding="utf-8")
        assert run("report") == 0
        assert export.read_text(encoding="utf-8") == "old export"
        assert "may be stale" in capsys.readouterr().out

    def test_export_is_the_explicit_redacted_file_path(self, run, project, filable):
        assert run("export", "--all") == 0
        assert (project / ".booley_project" / "BOOLEY-FEEDBACK.md").is_file()

    def test_export_accepts_an_output_override(self, run, project, filable, tmp_path):
        target = tmp_path / "sanitized.md"
        assert run("export", "--all", "--output", str(target)) == 0
        assert target.is_file()
        assert not (project / ".booley_project" / "BOOLEY-FEEDBACK.md").exists()

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
