"""Tests for the ad-hoc feedback path — the `/booley-feedback` skill's engine.

Three things separate it from a setup run, and each one is load-bearing:

- **Friction is filable.** "Nothing broke, this was just confusing" is a report
  Booley wants, so it clears a different evidence bar than a crash does.
- **Nothing is filed twice.** The log outlives the run. Without the already-filed
  stamp, a bug reported in July re-publishes March's setup findings.
- **The reports know which flow they came from.** Setup entries and everything
  else land in separate local reports, because a maintainer triages the two
  differently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.feedback import render
from booley.feedback.findings import Finding, append, read_log
from booley.feedback.render import render_booley_report, report_origin

PUBLIC_FEEDBACK_DOCS = (
    "README.md",
    "docs/user/CONFIG.md",
    "docs/internals/CONTRIBUTING.md",
    "docs/user/USAGE.md",
    "src/booley/data/cheatsheet.md",
)


@pytest.mark.parametrize(
    "relative_path",
    ("src/booley/data/skills/booley-feedback/SKILL.md",),
)
def test_feedback_docs_explain_shared_storage_and_checkout_context(relative_path):
    root = Path(__file__).parents[2]
    text = " ".join((root / relative_path).read_text(encoding="utf-8").split())

    assert "each Project has one Findings Log shared by all of its worktrees" in text
    assert "Main Booley Directory" in text
    assert "`-C` / `--project`" in text
    assert "does not move that shared log" in text
    assert "BOOLEY_DIR" in text
    assert "may use a separate log" in text
    if relative_path.endswith("/SKILL.md"):
        assert (
            "Configured names and `[feedback]` / `[stealth]` redaction settings come from "
            "`booley.toml` in the feedback storage directory"
        ) in text
        assert "Run `report` without `--user-report-path`" in text
        assert "parent directory is the feedback storage directory" in text
        assert "regardless of `BOOLEY_DIR`" in text


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
    (root / ".booley").mkdir(parents=True)
    (root / ".booley" / "booley.toml").write_text(
        '[project]\nname = "rocketwidget"\n', encoding="utf-8"
    )
    return root


@pytest.fixture
def booley_dir(project):
    return project / ".booley"


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


def _impression(title, sentiment="praise"):
    return Finding(
        title=title, kind="impression", bucket="booley", sentiment=sentiment, origin="impression"
    )


def test_friction_is_not_counted_as_a_bug(booley_dir):
    """Otherwise every friction report inflates the "Booley is broken" number."""
    append(_bug(severity="blocker"), booley_dir)
    append(_friction(), booley_dir)
    log = read_log(booley_dir)
    assert len(log.bugs) == 1
    assert len(log.friction) == 1
    assert sum(log.counts().values()) == 1


# Opinions (friction, impressions) are framed apart from defects and carry no
# "unverified diagnosis" caveat: the entry is the reporter's experience.
_OPINIONS = [
    pytest.param(_friction(), ["*(friction)*"], [], id="friction"),
    pytest.param(
        _impression("saved me a week on the AXI port"),
        ["booley-feedback", "## What they said", "nothing is broken"],
        ["booley feedback say"],
        id="impression",
    ),
]


@pytest.mark.parametrize(("opinion", "framing", "absent"), _OPINIONS)
def test_an_all_opinion_batch_is_framed_as_feedback_without_the_caveat(
    project, booley_dir, opinion, framing, absent
):
    append(opinion, booley_dir)
    report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
    for text in framing:
        assert text in report.body
    for text in [*absent, "not a diagnosis", "Not verified against Booley's source"]:
        assert text not in report.body


@pytest.mark.parametrize("opinion", [_friction(), _impression("love the waveform viewer")])
def test_one_bug_makes_the_batch_a_bug_report_and_keeps_the_opinion(project, booley_dir, opinion):
    """A defect must not be softened into feedback by the praise next to it."""
    append(opinion, booley_dir)
    append(_bug(), booley_dir)
    report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
    assert "## Findings" in report.body
    assert opinion.title in report.body


class TestAlreadyFiled:
    def test_a_filed_finding_is_never_filable_again_and_is_stamped(self):
        finding = _bug()
        assert finding.is_filable()
        finding.mark_filed("https://github.com/boldaxolotl/Booley/issues/42")
        assert not finding.is_filable()
        assert finding.filed.endswith("/issues/42")
        assert finding.filed_at

    def test_filed_findings_are_absent_from_a_later_report(self, project, booley_dir):
        """The dedup that makes a second bug report safe on a long-lived project."""
        append(_bug(title="a setup-run finding", origin="setup"), booley_dir)
        from booley.feedback.findings import rewrite

        rewrite(
            lambda entries: entries[0].mark_filed(
                "https://github.com/boldaxolotl/Booley/issues/1"
            ),
            booley_dir,
        )
        append(_bug(title="a bug hit months later"), booley_dir)

        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        titles = [f.title for f in report.filable]
        assert titles == ["a bug hit months later"]
        assert "a setup-run finding" not in report.body

    def test_a_filed_finding_is_not_reported_as_lacking_evidence(self, project, booley_dir):
        """It is complete; it has simply been sent. Listing it as withheld would
        read as "you forgot something" forever."""
        append(_bug(), booley_dir)
        from booley.feedback.findings import rewrite

        rewrite(lambda entries: entries[0].mark_filed("manual"), booley_dir)
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert report.withheld == []
        assert not report.has_content

    def test_the_user_report_still_shows_it(self, project, booley_dir):
        """Local memory of what was reported, and where it went."""
        append(_bug(), booley_dir)
        from booley.feedback.findings import rewrite

        rewrite(
            lambda entries: entries[0].mark_filed(
                "https://github.com/boldaxolotl/Booley/issues/7"
            ),
            booley_dir,
        )
        render.write_user_reports(project, booley_dir)
        assert "issues/7" in (booley_dir / render.FEEDBACK_REPORT_NAME).read_text(encoding="utf-8")


class TestOrigin:
    def test_a_bug_only_log_writes_only_the_feedback_report(self, project, booley_dir):
        append(_bug(), booley_dir)
        written, _ = render.write_user_reports(project, booley_dir)
        assert [w.path.name for w in written] == [render.FEEDBACK_REPORT_NAME]
        assert not (booley_dir / render.SETUP_REPORT_NAME).exists()

    def test_an_empty_log_reads_as_setup(self, booley_dir):
        assert report_origin(read_log(booley_dir)) == "setup"

    def test_wins_alone_do_not_decide_the_origin(self, booley_dir):
        append(Finding(title="doctor was clean", kind="win", origin="setup"), booley_dir)
        append(_bug(), booley_dir)
        assert report_origin(read_log(booley_dir)) == "bug"

    @pytest.mark.parametrize(
        ("origin", "present", "absent"),
        [("bug", "booley-feedback", "booley-setup` run"), ("setup", "`booley-setup` run", None)],
    )
    def test_the_report_is_framed_by_its_origin(
        self, project, booley_dir, origin, present, absent
    ):
        append(_bug(origin=origin), booley_dir)
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert present in report.body
        if absent:
            assert absent not in report.body


class TestAttachments:
    def test_only_the_tail_of_a_huge_file_is_inlined(self, project, booley_dir, tmp_path):
        """A 10k-line log must not become the issue body."""
        log_file = tmp_path / "run.log"
        log_file.write_text("\n".join(f"line {i}" for i in range(10_000)), encoding="utf-8")
        append(_bug(attachments=[str(log_file)]), booley_dir)
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert "line 9999" in report.body
        assert "line 0\n" not in report.body
        assert "of 10000 lines" in report.body

    def test_an_attachment_is_redacted_with_the_body(self, project, booley_dir, tmp_path):
        """The reason attachments go through the report at all instead of being
        pasted into notes by hand."""
        log_file = tmp_path / "run.log"
        log_file.write_text(f"error opening {project}/rtl/rocketwidget.sv\n", encoding="utf-8")
        append(_bug(attachments=[str(log_file)]), booley_dir)
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert str(project) not in report.body
        assert "rocketwidget.sv" not in report.body

    def test_an_identifier_buried_in_a_longer_one_survives(self, project, booley_dir, tmp_path):
        """Pins the known limit rather than pretending it isn't there: identifiers
        are replaced on word boundaries, so a name that appears only as part of a
        longer one an attached log invented is not caught. The export says so —
        see the attachment risk line — and that warning is the actual mitigation."""
        log_file = tmp_path / "run.log"
        log_file.write_text("error in myrocketwidgetalu3\n", encoding="utf-8")
        append(_bug(attachments=[str(log_file)]), booley_dir)
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert "myrocketwidgetalu3" in report.body
        assert any("inlines an attached file" in risk for risk in report.risks)

    def test_a_vanished_attachment_says_so_instead_of_failing(self, project, booley_dir, tmp_path):
        append(_bug(attachments=[str(tmp_path / "gone.log")]), booley_dir)
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert "unreadable at report time" in report.body

    def test_backticks_in_an_attachment_cannot_break_out_of_the_fence(
        self, project, booley_dir, tmp_path
    ):
        """Otherwise a log containing a fence turns the rest of the issue into prose."""
        log_file = tmp_path / "run.log"
        log_file.write_text("```\nnot the end of the block\n```\n", encoding="utf-8")
        append(_bug(attachments=[str(log_file)]), booley_dir)
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert "````" in report.body


@pytest.mark.parametrize(
    "relative_path",
    (
        "docs/user/CONFIG.md",
        "src/booley/data/skills/booley-feedback/SKILL.md",
        "src/booley/data/skills/booley-setup/steps/6-findings.md",
    ),
)
def test_feedback_report_location_has_no_setup_exception(relative_path):
    root = Path(__file__).parents[2]
    text = " ".join((root / relative_path).read_text(encoding="utf-8").split())
    if relative_path == "docs/user/CONFIG.md":
        section = text.split("## Feedback", 1)[1].split(" ## ", 1)[0]
        assert "SETUP-REPORT.md" not in section
        text = section

    if relative_path.endswith("booley-feedback/SKILL.md"):
        assert "`FEEDBACK-REPORT.md`" in text
    else:
        assert "`.booley/FEEDBACK-REPORT.md`" in text
        assert "/booley-feedback" in text
    assert "never ran setup" not in text
    assert "setup-origin log" not in text
