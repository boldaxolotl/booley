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
    FEEDBACK_REPORT_NAME,
    SETUP_REPORT_NAME,
    Environment,
    collect_environment,
    export_booley_report,
    render_booley_report,
    render_user_report,
    write_user_reports,
)
from booley.runtime.runtime_context import RuntimeLocation
from tests.sandbox_venue_fake import place, point_mounts


@pytest.mark.parametrize(
    ("value", "rendered"), [(True, "yes"), (False, "no"), (None, "not recorded")]
)
def test_environment_renders_resolved_doctor_observation(value, rendered):
    environment = Environment(doctor_deep_clean=value)

    assert ("doctor --deep clean", rendered) in environment.as_rows()


def _container_host(monkeypatch):
    place(monkeypatch, RuntimeLocation.HOST, container_host=True)


def _unmarked_sandbox(monkeypatch):
    """A Sandbox its spec declares, in an image that left no marker file."""
    place(monkeypatch, RuntimeLocation.HOST)
    point_mounts(monkeypatch, RuntimeLocation.HAND_WRITTEN_SANDBOX)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")


def _podman_sandbox(monkeypatch):
    """Only the container runtime's marker file tells; ``BOOLEY_CONTAINER`` is unset."""
    place(monkeypatch, RuntimeLocation.HAND_WRITTEN_SANDBOX)
    monkeypatch.delenv("BOOLEY_CONTAINER")


@pytest.mark.parametrize(
    ("venue", "rendered"),
    [
        pytest.param(_container_host, "host CLI", id="container-host"),
        pytest.param(_unmarked_sandbox, "Sandbox", id="no-marker"),
        pytest.param(_podman_sandbox, "Sandbox", id="podman"),
    ],
)
def test_environment_location_follows_the_sandbox_classification(monkeypatch, venue, rendered):
    venue(monkeypatch)

    assert ("Location", rendered) in collect_environment().as_rows()


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
    def test_every_finding_win_and_bucket_appears_regardless_of_evidence(self, booley_dir):
        _populate(booley_dir)
        out = render_user_report(read_log(booley_dir))
        for title in (
            "doctor fails on a valid multi-clock SDC",
            "CONFIG.md documents a removed table",
            "verilator missing from PATH",
            "the messages felt confusing",
            "targets resolved first try",
        ):
            assert title in out
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


def _log_impression(booley_dir, title, sentiment="mixed"):
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


class TestImpressions:
    """`booley feedback say` — opinions ride the same path as bugs, framed apart."""

    def test_the_user_report_keeps_them_out_of_the_defect_sections(self, booley_dir):
        """An opinion listed under 'Booley behaviour' reads as one more bug."""
        _log_impression(booley_dir, "the waveform flow is great", "praise")
        out = render_user_report(read_log(booley_dir))
        assert "## What you told Booley" in out
        assert "the waveform flow is great" in out
        assert "## Booley behaviour" not in out

    def test_they_are_counted_apart_from_bugs(self, booley_dir):
        _log_impression(booley_dir, "I wish it had a coverage dashboard", "wish")
        out = render_user_report(read_log(booley_dir))
        assert "Impressions (what you think of Booley): **1**" in out
        assert read_log(booley_dir).counts() == {"blocker": 0, "workaround": 0, "note": 0}

    def test_the_sentiment_is_shown_to_the_maintainer(self, project, booley_dir):
        _log_impression(booley_dir, "elaborate is too slow to be useful", "gripe")
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


class TestWriteUserReports:
    def test_only_the_user_reports_land_in_the_booley_dir(self, project, booley_dir):
        _populate(booley_dir)
        written, report = write_user_reports(project, booley_dir)
        assert [w.path for w in written] == [booley_dir / SETUP_REPORT_NAME]
        assert report.has_content
        # The footprint guardrail, enforced in code rather than in prose.
        assert not (booley_dir / BOOLEY_REPORT_NAME).exists()
        assert not (project / SETUP_REPORT_NAME).exists()
        assert not (project / BOOLEY_REPORT_NAME).exists()

    def test_the_user_report_is_written_when_nothing_is_filable(self, project, booley_dir):
        append(Finding(title="my own problem", bucket="project"), booley_dir)
        written, report = write_user_reports(project, booley_dir)
        assert written[0].path.is_file()
        assert not report.has_content
        assert not (booley_dir / BOOLEY_REPORT_NAME).exists()

    def test_a_log_with_no_entries_writes_no_report(self, project, booley_dir):
        written, _ = write_user_reports(project, booley_dir)
        assert written == []
        assert not (booley_dir / SETUP_REPORT_NAME).exists()
        assert not (booley_dir / FEEDBACK_REPORT_NAME).exists()

    def test_the_dogfood_override_writes_only_the_setup_report(self, project, booley_dir):
        """`--user-report-path`, the one sanctioned footprint exception."""
        append(Finding(title="setup entry"), booley_dir)
        append(Finding(title="bug entry", origin="bug"), booley_dir)
        target = project / SETUP_REPORT_NAME
        written, _ = write_user_reports(project, booley_dir, setup_report_path=target)
        assert [w.path for w in written] == [target]
        text = target.read_text(encoding="utf-8")
        assert "setup entry" in text
        assert "bug entry" not in text
        assert not (booley_dir / SETUP_REPORT_NAME).exists()
        assert not (booley_dir / FEEDBACK_REPORT_NAME).exists()

    def test_rerendering_is_stable_and_a_changed_entry_rewrites(self, project, booley_dir):
        """A second render finds the same content and leaves the file alone."""
        _populate(booley_dir)
        first, _ = write_user_reports(project, booley_dir)
        before = first[0].path.read_bytes()
        second, _ = write_user_reports(project, booley_dir)
        assert [w.changed for w in first] == [True]
        assert [w.changed for w in second] == [False]
        assert second[0].path.read_bytes() == before
        append(Finding(title="second setup entry"), booley_dir)
        third, _ = write_user_reports(project, booley_dir)
        assert third[0].changed
        assert "second setup entry" in third[0].path.read_text(encoding="utf-8")

    def test_a_new_project_name_or_environment_leaves_the_report_alone(self, project, booley_dir):
        """A later render from another host or without --project-name keeps the file."""
        append(Finding(title="setup entry"), booley_dir)
        first_env = Environment(booley_version="1.0", platform="Linux x86_64")
        write_user_reports(project, booley_dir, project_name="demo", env=first_env)
        path = booley_dir / SETUP_REPORT_NAME
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n## Delivered Flow results\n\nsim passed\n")
        before = path.read_bytes()
        later_env = Environment(booley_version="2.0", in_sandbox=True)
        written, _ = write_user_reports(project, booley_dir, env=later_env)
        assert [w.changed for w in written] == [False]
        assert path.read_bytes() == before


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
