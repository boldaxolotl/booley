"""The Run wizard's reviewer form note and review lines: model, token use, timeout and sign-in (#3817).

Pilots drive the real wizard and the real argument check child, which resolves the
worktree's settings and runs the Agent CLI's real health check in an empty home.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from textual.widgets import OptionList

from booley.harness.dashboard import launch
from tests.harness import test_dashboard_run as run_tests
from tests.harness.test_dashboard_run import (
    make_app,
    real_launcher,
    review_command,
    set_choice,
    set_text,
    shown,
    write_config,
)
from tests.harness.test_dashboard_run_specialist import empty_home as empty_home_fixture
from tests.harness.test_dashboard_run_specialist import to_reviewer

# Fixtures of the Run tests, shared by assignment so a test finds them by name.
checkout = run_tests.checkout
spawned = run_tests.spawned
empty_home = empty_home_fixture

SIGN_IN = "run claude and use /login, or on the host: booley auth --cli claude"
BASE_ARGUMENTS = ("--category", "rtl", "--focus", "bugs", "--scope", "fifo.v")


async def review_reviewer(app, pilot):
    """Fill the reviewer's form with a valid command and review it."""
    wizard = await to_reviewer(app, pilot)
    await set_choice(pilot, wizard, "Focus", "bugs")
    await set_text(pilot, wizard, "Scope", "fifo.v")
    await review_command(app, pilot, wizard)
    return wizard


def lines_of(facts: launch.SpecialistFacts) -> list[str]:
    # Imported here so a base without the review text fails the test body, not collection.
    from booley.harness.dashboard.review import specialist_lines

    return specialist_lines(facts)


def check_facts(checkout: Path, *extra: str) -> launch.SpecialistFacts:
    """The real check child's Specialist facts for the reviewer in *checkout*."""
    result = launch.check(checkout, "specialist", "reviewer", (*BASE_ARGUMENTS, *extra))
    assert result.error == ""
    specialist = getattr(result.facts, "specialist", None)
    assert specialist is not None, "the check reports no Specialist facts"
    return specialist


@pytest.mark.asyncio
async def test_review_shows_the_model_and_token_use_lines(
    checkout, spawned, empty_home, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    write_config(
        checkout, '[agent]\nauth = "api_key"\n\n[models]\nreviewer = "claude-sonnet-5-5"\n'
    )
    app = make_app(checkout, real_launcher(checkout / ".booley"))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await review_reviewer(app, pilot)
        review = shown(wizard, "#run-review").splitlines()
        assert "Model: claude-sonnet-5-5 on the claude Agent CLI" in review
        assert "Uses model tokens on the claude Agent CLI's credential (auth = api_key)" in review
        assert shown(wizard, "#run-error") == ""


def test_review_shows_the_first_usable_fallback_entry(checkout, empty_home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    write_config(
        checkout,
        '[agent]\nclis = ["claude", "codex"]\n\n'
        '[models]\nreviewer = ["gpt-6-luna", "claude-sonnet-5-5"]\n',
    )
    facts = check_facts(checkout)  # Codex has no login here, so its entry is skipped
    assert lines_of(facts)[0] == "Model: claude-sonnet-5-5 on the claude Agent CLI"
    assert facts.refusal == ""
    # With no usable entry the launch would refuse, and so does the check.
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    refusal = check_facts(checkout).refusal
    assert refusal.startswith(
        "cannot check the agent settings: ValueError: no usable [models] reviewer"
    )
    assert "gpt-6-luna: " in refusal


@pytest.mark.asyncio
async def test_a_failed_auth_check_refuses_the_launch_and_shows_the_fix(
    checkout, spawned, empty_home
):
    write_config(checkout, '[agent]\nauth = "api_key"\n')
    app = make_app(checkout, real_launcher(checkout / ".booley"))
    copied: list[str] = []
    app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await review_reviewer(app, pilot)
        expected = (
            "Refused: [agent] auth = 'api_key' but ANTHROPIC_API_KEY is not set — export it,"
            " or switch auth to 'subscription' or 'auto'. Copy command still works."
        )
        assert shown(wizard, "#run-error") == expected
        await pilot.press("enter")  # Submit
        await pilot.pause()
        assert spawned == []
        assert shown(wizard, "#run-error") == expected
        await pilot.press("down", "enter")  # Copy command still works
        assert copied == [shown(wizard, "#run-command")]


@pytest.mark.asyncio
async def test_auto_without_a_credential_warns_with_the_fix_and_still_submits(
    checkout, spawned, empty_home
):
    app = make_app(checkout, real_launcher(checkout / ".booley"))
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await review_reviewer(app, pilot)
        warnings = [
            line
            for line in shown(wizard, "#run-review").splitlines()
            if line.startswith("Warning")
        ]
        assert len(warnings) == 1, (
            f"expected one sign-in warning, got {warnings}; review:"
            f" {shown(wizard, '#run-review')!r}; error: {shown(wizard, '#run-error')!r}"
        )
        (warning,) = warnings
        assert warning.endswith(f"To sign in: {SIGN_IN}")
        assert "no claude credential found under auth = 'auto'" in warning
        assert shown(wizard, "#run-error") == ""
        await pilot.press("enter")  # Submit
        await pilot.pause()
        assert len(spawned) == 1


def test_timeout_line_shows_the_timeout_the_run_uses_including_a_raised_minimum(
    checkout, empty_home
):
    write_config(checkout, "")
    lines = {
        "default": lines_of(check_facts(checkout)),
        "short": lines_of(check_facts(checkout, "--timeout", "5m")),
    }
    assert "Timeout: 30m 00s per model call" in lines["default"]
    assert (
        "Timeout: 10m 00s per model call (raised to the Specialist's minimum;"
        " the form asked for 5m 00s)"
    ) in lines["short"]


def test_an_error_the_check_cannot_evaluate_refuses_with_its_message(
    checkout, empty_home, monkeypatch
):
    write_config(checkout, '[models]\nbogus = "x"\n')
    unknown_key = check_facts(checkout)
    assert unknown_key.refusal.startswith(
        "cannot check the agent settings: BackendConfigError:"
        " booley.toml [models] has an unknown key 'bogus'"
    )
    from booley.harness.dashboard.review import agent_refusal

    assert agent_refusal(unknown_key).startswith("Refused: cannot check")
    # Settings that could not be read name no model; the timeout line still shows.
    assert lines_of(unknown_key) == ["Timeout: 30m 00s per model call"]
    write_config(checkout, "")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "a b")
    spaced = check_facts(checkout)
    assert "credential contains whitespace" in spaced.refusal
    assert agent_refusal(spaced).startswith("Refused: ")


@pytest.mark.asyncio
async def test_form_note_shows_the_model_and_agent_cli(checkout, empty_home):
    write_config(
        checkout, '[agent]\nclis = ["claude", "codex"]\n\n[models]\nreviewer = "gpt-6-luna"\n'
    )
    launcher = real_launcher(checkout / ".booley")
    app = make_app(checkout, launcher)
    async with app.run_test(size=(120, 40)) as pilot:
        wizard = await to_reviewer(app, pilot)
        assert shown(wizard, "#run-note") == "Model: gpt-6-luna on the codex Agent CLI"
        assert wizard.query_one("#run-form", OptionList).display
    write_config(checkout, '[models]\nbogus = "x"\n')
    note = launcher.form("specialist", "reviewer", checkout).note
    assert note.startswith("Cannot read the agent settings: BackendConfigError:")


def test_malformed_specialist_facts_are_no_facts():
    good = {
        "model": "m",
        "cli": "claude",
        "policy": "auto",
        "timeout_ms": 5,
        "asked_ms": 5,
        "refusal": "",
        "warning": "",
    }
    base = {"job_class": "light", "dry_run": False}

    def parsed(specialist: object) -> object:
        return launch._parse_facts(json.dumps({**base, "specialist": specialist}))

    assert parsed(good) is not None
    for bad in (
        "text",
        {**good, "model": 1},
        {**good, "asked_ms": "5"},
        {**good, "asked_ms": True},
    ):
        assert parsed(bad) is None
