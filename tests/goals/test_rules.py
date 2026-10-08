"""The Goal Mode rules text carries the red-first bug-fix guidance (issue #1097)."""

from __future__ import annotations

from booley.goals.rules import goal_mode_rules


def test_rules_tell_the_agent_to_reproduce_a_bug_red_first() -> None:
    rules = goal_mode_rules()
    assert "confirm the test fails" in rules
    assert "before changing code" in rules
    assert "for the reported bug, not for an unrelated build or setup error" in rules
    assert "If it already passes" in rules
    assert "tell the human in chat" in rules
    assert "`goal_propose_change`" in rules


def test_rules_do_not_claim_red_first_is_enforced() -> None:
    # Goal acceptance does not check for a red run yet, so the text must not say it does.
    rules = goal_mode_rules()
    assert "needs a recorded failing run" not in rules
    assert "fail -> pass" not in rules
