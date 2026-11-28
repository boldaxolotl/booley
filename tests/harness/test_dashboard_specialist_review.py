"""The Run form's reviewer note and review lines: model, token use, timeout and sign-in (#3817).

The real argument check child resolves the worktree's settings and runs the Agent CLI's
real health check in an empty home.
"""

from __future__ import annotations

import json
from pathlib import Path

from booley.harness.dashboard import launch
from tests.harness import test_dashboard_run as run_tests
from tests.harness.test_dashboard_run import write_config
from tests.harness.test_dashboard_run_specialist import empty_home as empty_home_fixture

# Fixtures of the Run tests, shared by assignment so a test finds them by name.
checkout = run_tests.checkout
empty_home = empty_home_fixture

SIGN_IN = "run claude and use /login, or on the host: booley auth --cli claude"
BASE_ARGUMENTS = ("--category", "rtl", "--focus", "bugs", "--scope", "fifo.v")


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


def test_review_shows_the_first_usable_fallback_entry(checkout, empty_home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    write_config(
        checkout,
        '[agent]\nclis = ["claude", "codex"]\nauth = "api_key"\n\n'
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


def test_visitor_without_a_credential_warns_with_the_fix():
    facts = launch.SpecialistFacts(
        model="claude-sonnet-5-5",
        cli="claude",
        policy="auto",
        timeout_ms=5,
        asked_ms=5,
        warning=SIGN_IN,
    )

    warnings = [line for line in lines_of(facts) if line.startswith("Warning")]

    assert warnings == [
        "Warning: no claude credential found in this Sandbox; a proxy token or cloud provider"
        f" may still work. To sign in: {SIGN_IN}"
    ]


def test_a_host_without_a_credential_refuses_instead_of_warning(checkout, empty_home):
    write_config(checkout, "")  # a missing auth bills the subscription, which has no login here

    facts = check_facts(checkout)

    assert facts.policy == "subscription"
    assert facts.warning == ""
    assert "no subscription credential found" in facts.refusal


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


def test_malformed_specialist_facts_are_no_facts():
    good = {
        "model": "m",
        "cli": "claude",
        "policy": "subscription",
        "timeout_ms": 5,
        "asked_ms": 5,
        "refusal": "",
        "warning": "",
    }
    base = {"job_class": "memory", "dry_run": False}

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
