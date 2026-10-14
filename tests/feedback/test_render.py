"""Tests for the persistent user report and transient outbound view.

The audience split remains load-bearing, but only the user's copy is written by
default. The Booley-bound view is filtered and redacted in memory, then written
only by an explicit export. A leak in either direction is a bug.
"""

from __future__ import annotations

import pytest

from booley.feedback.findings import Finding, append, read_log
from booley.feedback.render import (
    BOOLEY_REPORT_NAME,
    USER_REPORT_NAME,
    Environment,
    export_booley_report,
    render_booley_report,
    render_user_report,
    write_user_report,
)


@pytest.mark.parametrize(
    ("value", "rendered"),
    [(True, "yes"), (False, "no"), (None, "not recorded")],
)
def test_environment_renders_resolved_doctor_observation(value, rendered):
    environment = Environment(doctor_deep_clean=value)

    assert ("doctor --deep clean", rendered) in environment.as_rows()


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


def _populate(booley_dir):
    append(
        Finding(
            title="doctor fails on a valid multi-clock SDC",
            severity="blocker",
            bucket="booley",
            component="doctor",
            repro="booley doctor",
            observed="FAIL: no create_clock",
            expected="PASS",
            verified_against_source=True,
        ),
        booley_dir,
    )
    append(
        Finding(
            title="CONFIG.md documents a removed table",
            bucket="docs",
            component="docs/user/CONFIG.md",
            observed="describes [sources.rtl]",
        ),
        booley_dir,
    )
    append(
        Finding(title="verilator missing from PATH", severity="workaround", bucket="project"),
        booley_dir,
    )
    append(Finding(title="the messages felt confusing", bucket="booley"), booley_dir)
    append(Finding(title="targets resolved first try", kind="win"), booley_dir)


class TestUserReport:
    def test_every_finding_appears_regardless_of_evidence(self, booley_dir):
        _populate(booley_dir)
        out = render_user_report(read_log(booley_dir))
        for title in (
            "doctor fails on a valid multi-clock SDC",
            "CONFIG.md documents a removed table",
            "verilator missing from PATH",
            "the messages felt confusing",
        ):
            assert title in out

    def test_wins_are_shown(self, booley_dir):
        _populate(booley_dir)
        assert "targets resolved first try" in render_user_report(read_log(booley_dir))

    def test_buckets_get_their_own_sections(self, booley_dir):
        _populate(booley_dir)
        out = render_user_report(read_log(booley_dir))
        assert "## Your project or environment" in out
        assert "## Booley behaviour" in out

    def test_untriaged_findings_are_called_out_as_unfinished(self, booley_dir):
        append(Finding(title="never triaged"), booley_dir)
        out = render_user_report(read_log(booley_dir))
        assert "Not yet triaged" in out
        assert "clean bill of health" in out

    def test_blockers_sort_above_notes(self, booley_dir):
        append(Finding(title="just a note", bucket="booley"), booley_dir)
        append(Finding(title="a blocker", severity="blocker", bucket="booley"), booley_dir)
        out = render_user_report(read_log(booley_dir))
        assert out.index("a blocker") < out.index("just a note")

    def test_corrupt_lines_are_disclosed_not_hidden(self, booley_dir, project):
        append(Finding(title="ok"), booley_dir)
        with (booley_dir / "findings.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("garbage\n")
        out = render_user_report(read_log(booley_dir))
        assert "could not be parsed" in out

    def test_an_empty_log_still_renders(self, booley_dir):
        out = render_user_report(read_log(booley_dir))
        assert "# Booley setup report" in out

    def test_it_says_it_is_local(self, booley_dir):
        """The user should not have to wonder whether this file phones home."""
        out = render_user_report(read_log(booley_dir))
        assert "never" in out and "published" in out


class TestImpressions:
    """`booley feedback say` — opinions ride the same path as bugs, framed apart."""

    @staticmethod
    def _say(booley_dir, title, sentiment="mixed"):
        return append(
            Finding(
                title=title,
                kind="impression",
                bucket="booley",
                sentiment=sentiment,
                origin="impression",
            ),
            booley_dir,
        )

    def test_the_user_report_keeps_them_out_of_the_defect_sections(self, booley_dir):
        """An opinion listed under 'Booley behaviour' reads as one more bug."""
        self._say(booley_dir, "the waveform flow is great", "praise")
        out = render_user_report(read_log(booley_dir))
        assert "## What you told Booley" in out
        assert "the waveform flow is great" in out
        assert "## Booley behaviour" not in out

    def test_they_are_counted_apart_from_bugs(self, booley_dir):
        self._say(booley_dir, "I wish it had a coverage dashboard", "wish")
        out = render_user_report(read_log(booley_dir))
        assert "Impressions (what you think of Booley): **1**" in out
        assert read_log(booley_dir).counts() == {"blocker": 0, "workaround": 0, "note": 0}

    def test_an_all_impression_batch_is_framed_as_feedback_not_bug(self, project, booley_dir):
        self._say(booley_dir, "saved me a week on the AXI port", "praise")
        self._say(booley_dir, "the setup grill is too long", "gripe")
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert "booley-feedback" in report.body
        assert "booley feedback say" not in report.body
        assert "## What they said" in report.body
        assert "nothing is broken" in report.body

    def test_one_bug_makes_the_batch_a_bug_report_again(self, project, booley_dir):
        """A defect must not be softened into feedback by the praise next to it."""
        self._say(booley_dir, "love the waveform viewer", "praise")
        append(
            Finding(
                title="simulate exits 2 with an empty stderr",
                bucket="booley",
                origin="bug",
                repro="booley flow sim",
                observed="rc=2, no message",
                expected="a diagnosis",
            ),
            booley_dir,
        )
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert "## Findings" in report.body
        # The impression still rides along — it just does not set the framing.
        assert "love the waveform viewer" in report.body

    def test_they_never_carry_the_unverified_caveat(self, project, booley_dir):
        """ "Not verified against Booley's source" is nonsense about an opinion."""
        self._say(booley_dir, "the docs are better than most", "praise")
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert "not a diagnosis" not in report.body

    def test_the_sentiment_is_shown_to_the_maintainer(self, project, booley_dir):
        self._say(booley_dir, "elaborate is too slow to be useful", "gripe")
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert "*(impression: gripe)*" in report.body


class TestBooleyReport:
    def test_only_booley_and_docs_findings_are_included(self, project, booley_dir):
        _populate(booley_dir)
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        titles = [f.title for f in report.filable]
        assert "doctor fails on a valid multi-clock SDC" in titles
        assert "CONFIG.md documents a removed table" in titles
        assert "verilator missing from PATH" not in titles

    def test_the_users_own_problems_never_reach_the_body(self, project, booley_dir):
        _populate(booley_dir)
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert "verilator missing from PATH" not in report.body

    def test_evidence_free_findings_are_withheld_and_named(self, project, booley_dir):
        _populate(booley_dir)
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert [f.title for f in report.withheld] == ["the messages felt confusing"]
        assert "the messages felt confusing" not in report.body

    def test_unverified_findings_are_labelled_as_observations(self, project, booley_dir):
        _populate(booley_dir)
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert "not a diagnosis" in report.body

    def test_project_identifiers_are_redacted_from_the_body(self, project, booley_dir):
        append(
            Finding(
                title="simulate cannot find the toplevel",
                bucket="booley",
                repro=f"cd {project} && booley flow sim",
                observed="rocketwidget not found",
                expected="it resolves",
            ),
            booley_dir,
        )
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert str(project) not in report.body
        assert "rocketwidget" not in report.body

    def test_nothing_filable_means_no_report(self, project, booley_dir):
        append(Finding(title="my own problem", bucket="project"), booley_dir)
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        assert not report.has_content

    def test_the_environment_fingerprint_is_present(self, project, booley_dir):
        _populate(booley_dir)
        report = render_booley_report(
            read_log(booley_dir),
            project,
            booley_dir=booley_dir,
            env=Environment(
                booley_version="9.9.9", python_version="3.14.0", platform="Linux x86_64"
            ),
        )
        assert "9.9.9" in report.body

    def test_the_fingerprint_carries_no_paths_or_names(self, project, booley_dir):
        """Clustering duplicates must not require identifying the reporter."""
        rows = dict(Environment(booley_version="1.0", platform="Linux x86_64").as_rows())
        assert "/" not in "".join(v for k, v in rows.items() if k != "Platform")


class TestWriteUserReport:
    def test_only_the_user_report_lands_in_the_booley_dir(self, project, booley_dir):
        _populate(booley_dir)
        user_path, report = write_user_report(project, booley_dir)
        assert user_path == booley_dir / USER_REPORT_NAME
        assert report.has_content
        assert not (booley_dir / BOOLEY_REPORT_NAME).exists()

    def test_nothing_is_written_into_the_tracked_tree(self, project, booley_dir):
        """The footprint guardrail, enforced in code rather than in prose."""
        _populate(booley_dir)
        write_user_report(project, booley_dir)
        assert not (project / USER_REPORT_NAME).exists()
        assert not (project / BOOLEY_REPORT_NAME).exists()

    def test_the_user_report_is_written_when_nothing_is_filable(self, project, booley_dir):
        append(Finding(title="my own problem", bucket="project"), booley_dir)
        user_path, report = write_user_report(project, booley_dir)
        assert user_path.is_file()
        assert not report.has_content
        assert not (booley_dir / BOOLEY_REPORT_NAME).exists()

    def test_the_dogfood_override_can_place_the_user_report_at_a_root(self, project, booley_dir):
        """`--user-report-path`, the one sanctioned footprint exception."""
        _populate(booley_dir)
        target = project / "SETUP-REPORT.md"
        user_path, _ = write_user_report(project, booley_dir, user_report_path=target)
        assert user_path == target
        assert target.is_file()
        assert not (booley_dir / USER_REPORT_NAME).exists()

    def test_rerendering_is_stable(self, project, booley_dir):
        """A second setup run rewrites the same canonical report, not a companion."""
        _populate(booley_dir)
        first_path, _ = write_user_report(project, booley_dir)
        first = first_path.read_text(encoding="utf-8")
        second_path, _ = write_user_report(project, booley_dir)
        assert second_path.read_text(encoding="utf-8") == first


class TestExplicitExport:
    def test_export_writes_the_redacted_view_only_on_request(self, project, booley_dir):
        _populate(booley_dir)
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        target = booley_dir / BOOLEY_REPORT_NAME
        assert export_booley_report(report, target) == target
        assert target.read_text(encoding="utf-8") == report.body

    def test_export_rejects_an_empty_view(self, project, booley_dir):
        append(Finding(title="project problem", bucket="project"), booley_dir)
        report = render_booley_report(read_log(booley_dir), project, booley_dir=booley_dir)
        with pytest.raises(ValueError, match="empty"):
            export_booley_report(report, booley_dir / BOOLEY_REPORT_NAME)
