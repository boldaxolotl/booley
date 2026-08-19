"""Tests for the submission gate.

Every assertion here is a guard against publishing something the user did not
agree to. None of these tests may touch the network: the one that reached GitHub
during development filed a real issue on the public repo, which is precisely the
failure mode the token and ``--dry-run`` now exist to prevent.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from booley.feedback import submit as submit_mod
from booley.feedback.submit import (
    DEFAULT_MODE,
    INTAKE_EMAIL,
    MAX_MAILTO_BODY,
    MAX_URL_BODY,
    GhStatus,
    confirmation_token,
    issue_url,
    mailto_url,
    preflight,
    preview,
    read_mode,
    submit,
)
from booley.harness import colors


@pytest.fixture
def project_dir(tmp_path):
    d = tmp_path / ".booley_project"
    d.mkdir()
    return d


@pytest.fixture
def body_file(project_dir):
    path = project_dir / "BOOLEY-FEEDBACK.md"
    path.write_text("## What happened\n\nSomething broke.\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def never_touch_the_network(monkeypatch):
    """Hard stop: no test in this file may shell out to `gh`."""

    def explode(*args, **kwargs):  # pragma: no cover - only fires on a bug
        raise AssertionError(f"a test tried to run a subprocess: {args!r}")

    monkeypatch.setattr(submit_mod.subprocess, "run", explode)


@pytest.fixture
def on_host(monkeypatch):
    monkeypatch.setattr(submit_mod, "in_container", lambda: False)


class TestMode:
    def test_absent_config_defaults_to_ask(self, project_dir):
        assert read_mode(project_dir) == DEFAULT_MODE == "ask"

    @pytest.mark.parametrize("mode", ["ask", "email", "file-only", "off"])
    def test_each_mode_is_read(self, project_dir, mode):
        (project_dir / "booley.toml").write_text(
            f'[feedback]\nmode = "{mode}"\n', encoding="utf-8"
        )
        assert read_mode(project_dir) == mode

    def test_a_nonsense_mode_falls_back_to_the_default(self, project_dir):
        (project_dir / "booley.toml").write_text('[feedback]\nmode = "yolo"\n', encoding="utf-8")
        assert read_mode(project_dir) == DEFAULT_MODE

    def test_malformed_toml_falls_back_to_the_default(self, project_dir):
        (project_dir / "booley.toml").write_text("[[[nope", encoding="utf-8")
        assert read_mode(project_dir) == DEFAULT_MODE


class TestPreflight:
    def test_off_refuses(self, project_dir, on_host):
        (project_dir / "booley.toml").write_text('[feedback]\nmode = "off"\n', encoding="utf-8")
        result = preflight(project_dir)
        assert not result.ok
        assert "disabled" in result.reason

    def test_file_only_refuses_and_explains(self, project_dir, on_host):
        (project_dir / "booley.toml").write_text(
            '[feedback]\nmode = "file-only"\n', encoding="utf-8"
        )
        result = preflight(project_dir)
        assert not result.ok
        assert "by hand" in result.reason

    def test_in_container_refuses_and_names_the_fix(self, project_dir, monkeypatch):
        monkeypatch.setattr(submit_mod, "in_container", lambda: True)
        result = preflight(project_dir)
        assert not result.ok
        assert "host" in result.reason

    def test_ask_on_the_host_proceeds(self, project_dir, on_host, monkeypatch):
        monkeypatch.setattr(submit_mod, "check_gh", lambda: GhStatus(True, True))
        result = preflight(project_dir)
        assert result.ok
        assert result.route == "github"

    def test_email_on_the_host_proceeds_without_probing_gh(
        self, project_dir, on_host, monkeypatch
    ):
        """A route that never touches GitHub must not report on GitHub's health."""
        monkeypatch.setattr(
            submit_mod,
            "check_gh",
            lambda: pytest.fail("the email route must not probe for gh"),
        )
        (project_dir / "booley.toml").write_text('[feedback]\nmode = "email"\n', encoding="utf-8")
        result = preflight(project_dir)
        assert result.ok
        assert result.route == "email"
        assert result.gh is None

    def test_email_in_container_refuses_too(self, project_dir, monkeypatch):
        """No mail client in the Session Runtime either — same host-only rule."""
        monkeypatch.setattr(submit_mod, "in_container", lambda: True)
        (project_dir / "booley.toml").write_text('[feedback]\nmode = "email"\n', encoding="utf-8")
        result = preflight(project_dir)
        assert not result.ok
        assert "host" in result.reason


class TestConsentGate:
    def test_no_approval_means_no_submission(self, project_dir, body_file, on_host):
        outcome = submit("t", body_file, project_dir, approved=False)
        assert not outcome.posted
        assert "--yes" in outcome.message

    def test_approval_without_a_token_is_refused(self, project_dir, body_file, on_host):
        outcome = submit("t", body_file, project_dir, approved=True)
        assert not outcome.posted
        assert "no --confirm token" in outcome.message

    def test_a_wrong_token_is_refused(self, project_dir, body_file, on_host):
        outcome = submit("t", body_file, project_dir, approved=True, confirm="deadbeef")
        assert not outcome.posted
        assert "does not match" in outcome.message

    def test_the_refusal_never_leaks_the_correct_token(self, project_dir, body_file, on_host):
        """Otherwise a caller scrapes it from the error and re-fires without ever
        showing the user anything — which defeats the entire mechanism."""
        correct = confirmation_token(body_file.read_text(encoding="utf-8"))
        for confirm in ("", "deadbeef"):
            outcome = submit("t", body_file, project_dir, approved=True, confirm=confirm)
            assert correct not in outcome.message

    def test_a_stale_token_stops_working_when_the_report_changes(
        self, project_dir, body_file, on_host
    ):
        """Approval covers the words the user read, not their replacement."""
        token = confirmation_token(body_file.read_text(encoding="utf-8"))
        body_file.write_text("## What happened\n\nSomething else entirely.\n", encoding="utf-8")
        outcome = submit("t", body_file, project_dir, approved=True, confirm=token)
        assert not outcome.posted
        assert "does not match" in outcome.message

    def test_the_token_is_case_insensitive_for_the_human_typing_it(
        self, project_dir, body_file, on_host, monkeypatch
    ):
        monkeypatch.setattr(submit_mod, "check_gh", lambda: GhStatus(True, True))
        token = confirmation_token(body_file.read_text(encoding="utf-8"))
        outcome = submit(
            "t",
            body_file,
            project_dir,
            approved=True,
            confirm=f"  {token.upper()}  ",
            dry_run=True,
        )
        assert not outcome.posted
        assert "[dry-run]" in outcome.message

    def test_a_valid_token_plus_dry_run_posts_nothing(
        self, project_dir, body_file, on_host, monkeypatch
    ):
        monkeypatch.setattr(submit_mod, "check_gh", lambda: GhStatus(True, True))
        token = confirmation_token(body_file.read_text(encoding="utf-8"))
        outcome = submit("t", body_file, project_dir, approved=True, confirm=token, dry_run=True)
        assert not outcome.posted
        assert "Nothing was posted" in outcome.message

    def test_mode_off_beats_a_valid_token(self, project_dir, body_file, on_host):
        """Config outranks consent: a disabled project cannot be talked into it."""
        (project_dir / "booley.toml").write_text('[feedback]\nmode = "off"\n', encoding="utf-8")
        token = confirmation_token(body_file.read_text(encoding="utf-8"))
        outcome = submit("t", body_file, project_dir, approved=True, confirm=token)
        assert not outcome.posted
        assert "disabled" in outcome.message

    def test_a_missing_report_is_refused_before_anything_else(self, project_dir, on_host):
        outcome = submit(
            "t", project_dir / "absent.md", project_dir, approved=True, confirm="whatever"
        )
        assert not outcome.posted
        assert "does not exist" in outcome.message

    def test_no_gh_falls_back_to_a_prefilled_url(
        self, project_dir, body_file, on_host, monkeypatch
    ):
        monkeypatch.setattr(
            submit_mod,
            "check_gh",
            lambda: GhStatus(False, False, "the GitHub CLI (`gh`) is not installed"),
        )
        token = confirmation_token(body_file.read_text(encoding="utf-8"))
        outcome = submit("t", body_file, project_dir, approved=True, confirm=token)
        assert not outcome.posted
        assert "https://github.com/" in outcome.message
        assert "booley feedback export" in outcome.message


class TestEmailRoute:
    """`mode = "email"` hands the user a mailto: link and sends nothing itself."""

    @pytest.fixture
    def email_project(self, project_dir):
        (project_dir / "booley.toml").write_text('[feedback]\nmode = "email"\n', encoding="utf-8")
        return project_dir

    def _submit(self, project_dir, body_file, **kw):
        token = confirmation_token(body_file.read_text(encoding="utf-8"))
        return submit("a subject", body_file, project_dir, approved=True, confirm=token, **kw)

    def test_it_hands_off_rather_than_posting(self, email_project, body_file, on_host):
        outcome = self._submit(email_project, body_file)
        assert not outcome.posted  # nothing was sent — Booley cannot know that it was
        assert outcome.handed_off
        assert f"mailto:{INTAKE_EMAIL}" in outcome.message

    def test_it_never_claims_a_url_to_stamp_findings_with(self, email_project, body_file, on_host):
        """`url` drives the already-filed stamp; an unsent mail must not earn one."""
        assert self._submit(email_project, body_file).url == ""

    def test_it_names_the_explicit_export_for_a_manual_compose(
        self, email_project, body_file, on_host
    ):
        assert "booley feedback export" in self._submit(email_project, body_file).message

    def test_the_consent_gate_still_applies(self, email_project, body_file, on_host):
        outcome = submit("s", body_file, email_project, approved=True)
        assert not outcome.handed_off
        assert "no --confirm token" in outcome.message

    def test_dry_run_opens_nothing(self, email_project, body_file, on_host):
        outcome = self._submit(email_project, body_file, dry_run=True)
        assert not outcome.handed_off
        assert "[dry-run]" in outcome.message
        assert "No mail client was opened" in outcome.message

    def test_mode_off_still_beats_it(self, project_dir, body_file, on_host):
        (project_dir / "booley.toml").write_text('[feedback]\nmode = "off"\n', encoding="utf-8")
        outcome = self._submit(project_dir, body_file)
        assert not outcome.handed_off
        assert "disabled" in outcome.message


class TestMailtoUrl:
    def test_it_targets_the_intake_address(self):
        assert mailto_url("s", "b").startswith(f"mailto:{INTAKE_EMAIL}?")

    def test_spaces_are_percent_encoded_not_plus_encoded(self):
        """RFC 6068 gives `+` no special meaning in a mailto: query — a form-encoded
        body arrives full of literal plus signs."""
        url = mailto_url("a subject", "a body")
        assert "subject=a%20subject" in url
        assert "+" not in url

    def test_an_oversized_body_is_visibly_truncated(self):
        url = mailto_url("s", "x" * (MAX_MAILTO_BODY + 5000))
        assert "truncated" in url
        assert len(url) < MAX_MAILTO_BODY * 3

    def test_a_short_body_is_left_whole(self):
        assert "truncated" not in mailto_url("s", "x" * 100)


class TestToken:
    def test_it_is_stable_for_the_same_text(self):
        assert confirmation_token("hello") == confirmation_token("hello")

    def test_it_changes_with_the_text(self):
        assert confirmation_token("hello") != confirmation_token("hello ")


class TestIssueUrl:
    def test_title_and_body_are_encoded(self):
        url = issue_url("a title", "a body")
        assert "title=a+title" in url
        assert "body=a+body" in url

    def test_an_oversized_body_is_visibly_truncated(self):
        url = issue_url("t", "x" * (MAX_URL_BODY + 5000))
        assert "truncated" in url
        assert len(url) < MAX_URL_BODY * 2


class TestPreview:
    def test_it_shows_the_exact_body(self, project_dir):
        out = preview("the body text", ["a risk"], "nothing")
        assert "the body text" in out

    def test_it_visually_separates_the_body_from_preview_chrome(self, project_dir):
        with patch.object(colors, "COLORS_ENABLED", True):
            out = preview("the body text", [], "nothing")

        assert "╭── exact text that would be posted ──" in out
        assert "\033[38;5;39mthe body text\033[0m" in out
        assert "╰── end of exact text ──" in out

    def test_it_states_the_issue_is_public_and_named(self, project_dir):
        out = preview("b", [], "nothing")
        assert "public" in out
        assert "GitHub account name" in out

    def test_the_email_route_names_the_recipient_and_claims_no_publication(self, project_dir):
        out = preview("b", [], "nothing", route="email")
        assert INTAKE_EMAIL in out
        assert "not to a public tracker" in out
        assert "GitHub account name" not in out

    def test_it_lists_the_residual_risks(self, project_dir):
        out = preview("b", ["metrics are kept"], "nothing")
        assert "metrics are kept" in out

    def test_it_says_no_is_free(self, project_dir):
        out = preview("b", [], "nothing")
        assert "optional" in out.lower()

    def test_it_ends_with_the_token_command(self, project_dir):
        out = preview("the body", [], "nothing")
        assert f"--confirm {confirmation_token('the body')}" in out

    def test_the_token_command_preserves_the_selected_batch(self, project_dir):
        out = preview("the body", [], "nothing", finding_ids=["F-8", "F-9"])
        assert "feedback submit F-8 F-9 --yes --confirm" in out

    def test_the_token_command_preserves_an_explicit_all_selection(self, project_dir):
        out = preview("the body", [], "nothing", all_findings=True)
        assert "feedback submit --all --yes --confirm" in out

    def test_it_says_the_redacted_view_was_not_persisted(self):
        out = preview("the body", [], "nothing")
        assert "no file was saved" in out
        assert "booley feedback export" in out
