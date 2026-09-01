"""Structured explanation validation and deterministic rendering tests."""

from __future__ import annotations

import pytest

from booley.review.explanation import (
    ExplanationError,
    StructuredExplanation,
    render_explanation_html,
)


def _value() -> dict:
    return {
        "background": [{"title": "A & B", "body": 'Plain "background" [one].'}],
        "intuition": [{"title": "Toy data", "body": "old -> new & stable"}],
        "code_references": [
            {
                "repository": "rtl",
                "path": "rtl/core [v2].sv",
                "revision": "b" * 40,
                "summary": "Preserve λ and Markdown-like `tokens` as text.",
            }
        ],
        "findings": [{"title": "Finding", "detail": "Quotes: 'single' & \"double\"."}],
        "quiz": [
            {
                "question": f"Question {index}?",
                "choices": [
                    {"text": "A", "correct": True, "feedback": 'Correct & "safe".'},
                    {"text": "B", "correct": False, "feedback": "Try [again]."},
                ],
            }
            for index in range(5)
        ],
    }


def test_render_escapes_agent_text_and_owns_active_content() -> None:
    explanation = StructuredExplanation.parse(_value())
    package = {
        "slug": "review & verify",
        "criteria": [
            {
                "criterion": "review_security_done",
                "outcome": "met",
                "freshness": "stale",
            }
        ],
    }

    rendered = render_explanation_html(explanation, package)

    assert "review &amp; verify" in rendered
    assert "A &amp; B" in rendered
    assert "rtl/core [v2].sv" in rendered
    assert 'data-feedback="Correct &amp; &quot;safe&quot;."' in rendered
    assert rendered.count("<script>") == 1
    assert "Content-Security-Policy" in rendered
    assert "review_security_done" in rendered
    assert "<td>met</td><td>stale</td>" in rendered


@pytest.mark.parametrize("quiz_size", [0, 4, 6])
def test_quiz_requires_exactly_five_questions(quiz_size: int) -> None:
    value = _value()
    value["quiz"] = value["quiz"][:quiz_size]
    if quiz_size == 6:
        value["quiz"].append(value["quiz"][0])

    with pytest.raises(ExplanationError, match="exactly five"):
        StructuredExplanation.parse(value)


def test_each_question_requires_exactly_one_correct_choice() -> None:
    value = _value()
    value["quiz"][0]["choices"][1]["correct"] = True

    with pytest.raises(ExplanationError, match="exactly one correct"):
        StructuredExplanation.parse(value)


@pytest.mark.parametrize("text", ["<script>alert(1)</script>", "bad\x00control"])
def test_markup_and_forbidden_control_content_are_rejected(text: str) -> None:
    value = _value()
    value["background"][0]["body"] = text

    with pytest.raises(ExplanationError):
        StructuredExplanation.parse(value)
