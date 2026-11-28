"""The Run form's Specialists: the argument check, the reviewer form and the auth facts."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.harness.dashboard import launch
from booley.harness.dashboard.specialist_form import ReviewerForm
from booley.runtime import auth_token


@pytest.fixture
def empty_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No credential in the environment or on disk; the Claude Agent CLI is the default.

    The check child inherits this environment. ``BOOLEY_PRIMARY_PROVIDER`` keeps the
    settings loader from refusing an unresolved provider when the suite runs in a Sandbox.
    """
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in (
        *auth_token.CREDENTIAL_ENV_KEYS,
        "CODEX_HOME",
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_AGENT_APP",
        "BOOLEY_AGENT_CLIS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_PRIMARY_PROVIDER", "claude")
    return home


def test_specialist_check_parses_like_the_shell(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    arguments = ["--category", "rtl", "--focus", "bugs", "--scope", "a.v", "--timeout", "5m"]
    result = launch.check(tmp_path, "specialist", "reviewer", tuple(arguments))
    assert result.error == ""
    assert result.facts is not None
    assert (result.facts.job_class, result.facts.dry_run) == ("memory", False)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exit_info:
        launch.main(["specialist", "reviewer", "--", "--category", "x"])
    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_reviewer_form_spec_field_starts_empty_and_keeps_a_typed_path():
    form = ReviewerForm()
    assert form.initial() == {"category": "rtl", "spec": ""}
    typed = {**form.initial(), "focus": "spec", "spec": "docs/spec.md"}
    assert form.refusal({**typed, "scope": "a.v"}) == ""
    assert form.argv({**typed, "scope": "a.v"})[-4:-2] == ("--spec", "docs/spec.md")


def test_an_issued_auto_record_shows_as_auto_in_the_specialist_facts(
    empty_home, tmp_path, monkeypatch
):
    from booley.harness.dashboard import launch

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_AGENT_CLIS", "claude=auto")

    facts = launch._agent_facts("reviewer", 1000, 1000)

    assert facts["cli"] == "claude"
    assert facts["policy"] == "auto"
