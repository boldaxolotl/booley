"""Tests for the ``booley feedback`` subcommands themselves.

The unit tests cover what the reports contain; these cover the wiring an agent
actually touches — and one path that cannot be allowed to regress: a successful
submit must stamp what it sent, or the next report re-publishes it.
"""

from __future__ import annotations

import argparse

import pytest

from booley.feedback import cli
from booley.feedback import submit as submit_mod
from booley.feedback.findings import read_log
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
        assert "no second report was written" in capsys.readouterr().out

    def test_report_warns_that_an_existing_export_was_not_refreshed(
        self, run, project, filable, capsys
    ):
        export = project / ".booley_project" / "BOOLEY-FEEDBACK.md"
        export.write_text("old export", encoding="utf-8")
        assert run("report") == 0
        assert export.read_text(encoding="utf-8") == "old export"
        assert "may be stale" in capsys.readouterr().out

    def test_preview_does_not_persist_the_redacted_view(self, run, project, filable, capsys):
        assert run("preview", "--all") == 0
        out = capsys.readouterr().out
        assert "exact text that would be posted" in out
        assert "feedback submit --all --yes --confirm" in out
        assert not (project / ".booley_project" / "BOOLEY-FEEDBACK.md").exists()

    def test_preview_requires_an_explicit_batch(self, run, filable, capsys):
        assert run("preview") == 1
        assert "choose finding IDs or --all" in capsys.readouterr().err

    def test_preview_rejects_ids_together_with_all(self, run, filable, capsys):
        assert run("preview", "F-1", "--all") == 1
        assert "not both" in capsys.readouterr().err

    def test_preview_can_limit_the_batch_to_the_current_discussion(self, run, filable, capsys):
        run(
            "add",
            "--title",
            "reviewer cannot reverify a fix",
            "--origin",
            "bug",
            "--bucket",
            "booley",
            "--repro",
            "ask the reviewer Specialist to recheck the change",
            "--observed",
            "the rerun is refused",
            "--expected",
            "a bounded recheck",
        )
        capsys.readouterr()

        assert run("preview", "F-2") == 0
        out = capsys.readouterr().out
        assert "reviewer cannot reverify a fix" in out
        assert "simulate exits 2" not in out
        assert "during normal use" in out
        assert "`booley-setup` run" not in out
        assert "feedback submit F-2 --yes --confirm" in out

    def test_unknown_preview_selection_fails_clearly(self, run, filable, capsys):
        assert run("preview", "F-99") == 1
        assert "No finding(s) F-99" in capsys.readouterr().err

    def test_export_is_the_explicit_redacted_file_path(self, run, project, filable):
        assert run("export", "--all") == 0
        assert (project / ".booley_project" / "BOOLEY-FEEDBACK.md").is_file()

    def test_export_accepts_an_output_override(self, run, project, filable, tmp_path):
        target = tmp_path / "sanitized.md"
        assert run("export", "--all", "--output", str(target)) == 0
        assert target.is_file()
        assert not (project / ".booley_project" / "BOOLEY-FEEDBACK.md").exists()

    def test_export_honours_the_same_selection_as_preview(self, run, project, filable, tmp_path):
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

    def test_submit_uses_and_removes_a_transient_body(self, run, project, filable, monkeypatch):
        seen = {}

        def _fake(_title, body_path, _project_dir, **_kwargs):
            seen["path"] = body_path
            seen["body"] = body_path.read_text(encoding="utf-8")
            return submit_mod.SubmitOutcome(False, "[dry-run] Nothing was posted")

        monkeypatch.setattr(submit_mod, "submit", _fake)
        assert run("submit", "--all", "--dry-run") == 0
        assert "simulate exits 2" in seen["body"]
        assert not seen["path"].exists()
        assert not (project / ".booley_project" / "BOOLEY-FEEDBACK.md").exists()


class TestSubmitStamping:
    """The dedup guarantee, end to end through the CLI."""

    @pytest.fixture
    def filable(self, run):
        run(
            "add",
            "--title",
            "simulate exits 2",
            "--origin",
            "bug",
            "--bucket",
            "booley",
            "--repro",
            "booley flow sim",
            "--observed",
            "exit 2",
            "--expected",
            "a result",
        )
        run("report")

    def test_a_posted_report_marks_its_findings_filed(
        self, run, project, filable, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            submit_mod,
            "submit",
            lambda *a, **kw: submit_mod.SubmitOutcome(
                True, "Filed: https://example.invalid/7", url="https://example.invalid/7"
            ),
        )
        assert run("submit", "--all", "--yes", "--confirm", "whatever") == 0
        assert _log(project).entries[0].filed == "https://example.invalid/7"
        assert "will not be sent again" in capsys.readouterr().out

    def test_a_selected_submit_sends_and_stamps_only_that_batch(
        self, run, project, filable, monkeypatch
    ):
        run(
            "add",
            "--title",
            "the current discussion",
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
        seen = {}

        def _fake(_title, body_path, _project_dir, **_kwargs):
            seen["body"] = body_path.read_text(encoding="utf-8")
            return submit_mod.SubmitOutcome(
                True, "Filed: https://example.invalid/8", url="https://example.invalid/8"
            )

        monkeypatch.setattr(submit_mod, "submit", _fake)
        assert run("submit", "F-2", "--yes", "--confirm", "whatever") == 0

        entries = _log(project).entries
        assert entries[0].filed == ""
        assert entries[1].filed == "https://example.invalid/8"
        assert "simulate exits 2" not in seen["body"]
        assert "the current discussion" in seen["body"]

    def test_changing_the_selected_ids_invalidates_the_preview_token(
        self, run, project, filable, capsys
    ):
        run(
            "add",
            "--title",
            "a different finding",
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
        capsys.readouterr()
        assert run("preview", "F-1") == 0
        preview_out = capsys.readouterr().out
        token = preview_out.rsplit("--confirm ", maxsplit=1)[1].splitlines()[0]

        assert run("submit", "F-2", "--yes", "--confirm", token) == 1
        assert "does not match" in capsys.readouterr().out
        assert all(not entry.filed for entry in _log(project).entries)

    def test_the_label_follows_the_origin(self, run, filable, monkeypatch):
        seen = {}

        def _fake(*args, **kwargs):
            seen.update(kwargs)
            return submit_mod.SubmitOutcome(False, "nope")

        monkeypatch.setattr(submit_mod, "submit", _fake)
        run("submit", "--all", "--yes", "--confirm", "whatever")
        assert seen["label"] == "user-feedback"

    def test_a_refused_submit_stamps_nothing(self, run, project, filable, monkeypatch):
        """A rejected token must not look like a successful filing."""
        monkeypatch.setattr(
            submit_mod,
            "submit",
            lambda *a, **kw: submit_mod.SubmitOutcome(False, "Not submitted: bad token"),
        )
        assert run("submit", "--all", "--yes", "--confirm", "wrong") == 1
        assert _log(project).entries[0].filed == ""

    def test_an_email_hand_off_stamps_nothing_but_exits_clean(
        self, run, project, filable, monkeypatch, capsys
    ):
        """Booley handed over a mailto: link; whether it was sent is unknowable, so
        the batch stays unfiled — and this is not a failure to exit non-zero on."""
        monkeypatch.setattr(
            submit_mod,
            "submit",
            lambda *a, **kw: submit_mod.SubmitOutcome(False, "mailto:…", handed_off=True),
        )
        assert run("submit", "--all", "--yes", "--confirm", "whatever") == 0
        assert _log(project).entries[0].filed == ""
        out = capsys.readouterr().out
        assert "booley feedback filed F-1 --url email" in out

    def test_a_second_report_after_filing_has_nothing_to_send(
        self, run, project, filable, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            submit_mod,
            "submit",
            lambda *a, **kw: submit_mod.SubmitOutcome(
                True, "Filed", url="https://example.invalid/7"
            ),
        )
        run("submit", "--all", "--yes", "--confirm", "whatever")
        capsys.readouterr()
        run("report")
        assert "nothing to offer upstream" in capsys.readouterr().out


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
