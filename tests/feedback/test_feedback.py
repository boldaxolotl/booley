"""Tests for the ad-hoc feedback path — the `/booley-feedback` skill's engine.

Three things separate it from a setup run, and each one is load-bearing:

- **Friction is filable.** "Nothing broke, this was just confusing" is a report
  Booley wants, so it clears a different evidence bar than a crash does.
- **Nothing is filed twice.** The log outlives the run. Without the already-filed
  stamp, a bug reported in July re-publishes March's setup findings.
- **The report knows which flow it came from.** File name, issue title, label and
  framing all follow, because a maintainer triages the two differently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.feedback.findings import Finding, append, read_log
from booley.feedback.render import (
    BUG_USER_REPORT_NAME,
    USER_REPORT_NAME,
    render_booley_report,
    report_origin,
    write_user_report,
)

PUBLIC_FEEDBACK_DOCS = (
    "README.md",
    "docs/user/CONFIG.md",
    "docs/CONTEXT.md",
    "docs/internals/CONTRIBUTING.md",
    "docs/user/USAGE.md",
    "src/booley/data/cheatsheet.md",
)


@pytest.mark.parametrize("relative_path", PUBLIC_FEEDBACK_DOCS)
def test_public_docs_route_feedback_through_the_skill(relative_path):
    root = Path(__file__).parents[2]
    text = (root / relative_path).read_text(encoding="utf-8")
    assert "/booley-feedback" in text
    assert "booley-bug-report" not in text
    assert "booley feedback" not in text


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "rocketwidget"
    (root / ".booley_project").mkdir(parents=True)
    (root / ".booley_project" / "booley.toml").write_text(
        '[project]\nname = "rocketwidget"\n', encoding="utf-8"
    )
    return root


@pytest.fixture
def project_dir(project):
    return project / ".booley_project"


def _bug(**kw):
    """A filable bug-origin finding."""
    return Finding(
        title=kw.pop("title", "simulate exits 2 with no error text"),
        bucket=kw.pop("bucket", "booley"),
        origin=kw.pop("origin", "bug"),
        component=kw.pop("component", "sim"),
        repro=kw.pop("repro", "booley flow sim"),
        observed=kw.pop("observed", "exit 2, empty stderr"),
        expected=kw.pop("expected", "a result or a reason"),
        **kw,
    )


def _friction(**kw):
    return Finding(
        title=kw.pop("title", "'0 targets matched' reads like a crash"),
        kind="friction",
        bucket=kw.pop("bucket", "booley"),
        origin=kw.pop("origin", "bug"),
        component=kw.pop("component", "targets"),
        expected=kw.pop("expected", "a line saying the filter matched nothing"),
        **kw,
    )


class TestFriction:
    def test_friction_is_filable_without_a_reproduction(self):
        """The whole point: demanding a repro would filter out the entire class."""
        assert _friction().is_filable()

    def test_friction_still_needs_somewhere_to_aim_the_fix(self):
        stranded = _friction(component="", exposed_by="")
        assert not stranded.is_filable()
        assert "component" in stranded.missing_evidence()

    def test_friction_needs_what_was_expected_instead(self):
        vague = _friction(expected="")
        assert not vague.is_filable()
        assert "expected" in vague.missing_evidence()

    def test_notes_can_stand_in_for_expected(self):
        """A paragraph explaining the confusion is as actionable as a one-liner."""
        assert _friction(
            expected="", notes="I looked for the target list for ten minutes"
        ).is_filable()

    def test_exposed_by_can_stand_in_for_component(self):
        assert _friction(component="", exposed_by="booley targets --for sim").is_filable()

    def test_friction_is_not_counted_as_a_bug(self, project_dir):
        """Otherwise every friction report inflates the "Booley is broken" number."""
        append(_bug(severity="blocker"), project_dir)
        append(_friction(), project_dir)
        log = read_log(project_dir)
        assert len(log.bugs) == 1
        assert len(log.friction) == 1
        assert sum(log.counts().values()) == 1

    def test_friction_carries_no_unverified_caveat(self, project, project_dir):
        """It is definitionally the reporter's experience — the caveat says nothing."""
        append(_friction(), project_dir)
        report = render_booley_report(read_log(project_dir), project, project_dir=project_dir)
        assert "Not verified against Booley's source" not in report.body

    def test_an_all_friction_batch_is_tagged_ux(self, project, project_dir):
        append(_friction(), project_dir)
        report = render_booley_report(read_log(project_dir), project, project_dir=project_dir)
        assert report.tag == "ux"
        assert report.issue_title().startswith("[ux]")

    def test_one_bug_in_the_batch_makes_it_a_bug_report(self, project, project_dir):
        append(_friction(), project_dir)
        append(_bug(), project_dir)
        report = render_booley_report(read_log(project_dir), project, project_dir=project_dir)
        assert report.tag == "bug"


class TestAlreadyFiled:
    def test_a_filed_finding_is_never_filable_again(self):
        finding = _bug()
        assert finding.is_filable()
        finding.mark_filed("https://github.com/boldaxolotl/Booley/issues/42")
        assert not finding.is_filable()

    def test_mark_filed_stamps_when(self):
        finding = _bug()
        finding.mark_filed("manual")
        assert finding.filed == "manual"
        assert finding.filed_at

    def test_filed_findings_are_absent_from_a_later_report(self, project, project_dir):
        """The dedup that makes a second bug report safe on a long-lived project."""
        old = append(_bug(title="a setup-run finding", origin="setup"), project_dir)
        old.mark_filed("https://github.com/boldaxolotl/Booley/issues/1")
        log = read_log(project_dir)
        log.entries[0].mark_filed("https://github.com/boldaxolotl/Booley/issues/1")
        from booley.feedback.findings import rewrite

        rewrite(log.entries, project_dir)
        append(_bug(title="a bug hit months later"), project_dir)

        report = render_booley_report(read_log(project_dir), project, project_dir=project_dir)
        titles = [f.title for f in report.filable]
        assert titles == ["a bug hit months later"]
        assert "a setup-run finding" not in report.body

    def test_a_filed_finding_is_not_reported_as_lacking_evidence(self, project, project_dir):
        """It is complete; it has simply been sent. Listing it as withheld would
        read as "you forgot something" forever."""
        entry = append(_bug(), project_dir)
        entry.mark_filed("manual")
        from booley.feedback.findings import rewrite

        rewrite([entry], project_dir)
        report = render_booley_report(read_log(project_dir), project, project_dir=project_dir)
        assert report.withheld == []
        assert not report.has_content

    def test_the_user_report_still_shows_it(self, project, project_dir):
        """Local memory of what was reported, and where it went."""
        entry = append(_bug(), project_dir)
        entry.mark_filed("https://github.com/boldaxolotl/Booley/issues/7")
        from booley.feedback.findings import rewrite

        rewrite([entry], project_dir)
        user_path, _ = write_user_report(project, project_dir)
        assert "issues/7" in user_path.read_text(encoding="utf-8")


class TestOrigin:
    def test_a_bug_only_log_gets_the_neutral_report_name(self, project, project_dir):
        append(_bug(), project_dir)
        user_path, _ = write_user_report(project, project_dir)
        assert user_path.name == BUG_USER_REPORT_NAME

    def test_one_setup_entry_keeps_the_setup_report_name(self, project, project_dir):
        """The file the user already knows about must not be renamed under them."""
        append(_bug(origin="setup"), project_dir)
        append(_bug(origin="bug"), project_dir)
        user_path, _ = write_user_report(project, project_dir)
        assert user_path.name == USER_REPORT_NAME

    def test_an_empty_log_reads_as_setup(self, project_dir):
        assert report_origin(read_log(project_dir)) == "setup"

    def test_wins_alone_do_not_decide_the_origin(self, project_dir):
        append(Finding(title="doctor was clean", kind="win", origin="setup"), project_dir)
        append(_bug(), project_dir)
        assert report_origin(read_log(project_dir)) == "bug"

    def test_normal_feedback_says_normal_use_not_setup(self, project, project_dir):
        append(_bug(), project_dir)
        report = render_booley_report(read_log(project_dir), project, project_dir=project_dir)
        assert "booley-feedback" in report.body
        assert "booley-setup` run" not in report.body
        assert report.label == "user-feedback"

    def test_a_setup_report_is_unchanged(self, project, project_dir):
        append(_bug(origin="setup"), project_dir)
        report = render_booley_report(read_log(project_dir), project, project_dir=project_dir)
        assert "`booley-setup` run" in report.body
        assert report.label == "setup-feedback"
        assert report.issue_title().startswith("[bug]") is False


class TestAttachments:
    def test_an_attached_file_is_inlined(self, project, project_dir, tmp_path):
        log_file = tmp_path / "run.log"
        log_file.write_text("elaborate ok\nsimulate: SIGSEGV\n", encoding="utf-8")
        append(_bug(attachments=[str(log_file)]), project_dir)
        report = render_booley_report(read_log(project_dir), project, project_dir=project_dir)
        assert "simulate: SIGSEGV" in report.body

    def test_only_the_tail_of_a_huge_file_is_inlined(self, project, project_dir, tmp_path):
        """A 10k-line log must not become the issue body."""
        log_file = tmp_path / "run.log"
        log_file.write_text("\n".join(f"line {i}" for i in range(10_000)), encoding="utf-8")
        append(_bug(attachments=[str(log_file)]), project_dir)
        report = render_booley_report(read_log(project_dir), project, project_dir=project_dir)
        assert "line 9999" in report.body
        assert "line 0\n" not in report.body
        assert "of 10000 lines" in report.body

    def test_an_attachment_is_redacted_with_the_body(self, project, project_dir, tmp_path):
        """The reason attachments go through the report at all instead of being
        pasted into notes by hand."""
        log_file = tmp_path / "run.log"
        log_file.write_text(f"error opening {project}/rtl/rocketwidget.sv\n", encoding="utf-8")
        append(_bug(attachments=[str(log_file)]), project_dir)
        report = render_booley_report(read_log(project_dir), project, project_dir=project_dir)
        assert str(project) not in report.body
        assert "rocketwidget.sv" not in report.body

    def test_an_identifier_buried_in_a_longer_one_survives(self, project, project_dir, tmp_path):
        """Pins the known limit rather than pretending it isn't there: identifiers
        are replaced on word boundaries, so a name that appears only as part of a
        longer one an attached log invented is not caught. The preview says so —
        see the attachment risk line — and that warning is the actual mitigation."""
        log_file = tmp_path / "run.log"
        log_file.write_text("error in rocketwidget_alu_stage3\n", encoding="utf-8")
        append(_bug(attachments=[str(log_file)]), project_dir)
        report = render_booley_report(read_log(project_dir), project, project_dir=project_dir)
        assert "rocketwidget_alu_stage3" in report.body
        assert any("inlines an attached file" in risk for risk in report.risks)

    def test_a_vanished_attachment_says_so_instead_of_failing(
        self, project, project_dir, tmp_path
    ):
        append(_bug(attachments=[str(tmp_path / "gone.log")]), project_dir)
        report = render_booley_report(read_log(project_dir), project, project_dir=project_dir)
        assert "unreadable at report time" in report.body

    def test_backticks_in_an_attachment_cannot_break_out_of_the_fence(
        self, project, project_dir, tmp_path
    ):
        """Otherwise a log containing a fence turns the rest of the issue into prose."""
        log_file = tmp_path / "run.log"
        log_file.write_text("```\nnot the end of the block\n```\n", encoding="utf-8")
        append(_bug(attachments=[str(log_file)]), project_dir)
        report = render_booley_report(read_log(project_dir), project, project_dir=project_dir)
        assert "````" in report.body

    def test_attachments_survive_a_log_round_trip(self, project_dir, tmp_path):
        append(_bug(attachments=["a.log", "b.log"]), project_dir)
        assert read_log(project_dir).entries[0].attachments == ["a.log", "b.log"]

    def test_a_malformed_attachments_field_costs_one_line_not_the_log(self, project_dir):
        """Same contract as every other corrupt entry: skip it, keep the rest."""
        path = project_dir / "findings.jsonl"
        append(_bug(), project_dir)
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"title": "junk", "attachments": "not-a-list"}\n')
        log = read_log(project_dir)
        assert len(log.entries) == 1
        assert log.corrupt_lines == 1


class TestDocsEvidence:
    def test_a_docs_finding_is_not_nagged_for_a_reproduction(self):
        """ "CONFIG.md says X, the code does Y" is complete without a command."""
        finding = Finding(
            title="CONFIG.md documents a removed table",
            bucket="docs",
            component="docs/user/CONFIG.md",
            observed="describes [sources.rtl]",
        )
        assert finding.is_filable()
        assert finding.missing_evidence() == []

    def test_a_docs_finding_without_a_file_is_not_actionable(self):
        finding = Finding(title="a doc is wrong", bucket="docs", observed="it is wrong")
        assert not finding.is_filable()
        assert finding.missing_evidence() == ["component"]
