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
                "label": "RTL security review",
                "outcome": "met",
                "freshness": "stale",
                "changed_categories": ["tb", "rtl<script>"],
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
    assert "RTL security review" in rendered
    assert "review_security_done" not in rendered
    assert "<td>met</td><td>STALE (tb, rtl&lt;script&gt;)</td>" in rendered


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


def test_review_audit_is_inert_and_links_only_evidence() -> None:
    package = {
        "slug": "audit",
        "review_audit": [
            {
                "criterion": "review_rtl_bugs_clean",
                "reason": "source_scope",
                "file": "[click](https://invalid)/<img>.sv",
                "summary": "<script>unsafe</script>",
                "phase": "discovery",
                "attempt_id": "one",
                "ordinal": 1,
                "evidence": "/tmp/evidence [one].json",
            },
            {
                "criterion": "review_rtl_bugs_clean",
                "errors": ["<img src=x>invalid"],
                "raw": "<script>raw-only</script>",
                "phase": "verification",
                "attempt_id": "two",
                "ordinal": 1,
                "evidence": "/tmp/evidence [two].json",
            },
        ],
    }
    rendered = render_explanation_html(StructuredExplanation.parse(_value()), package)
    assert "Filtered proposals and parsing rejections — do not affect Criteria" in rendered
    assert "&lt;script&gt;unsafe&lt;/script&gt;" in rendered
    assert '<a href="/tmp/evidence%20%5Bone%5D.json">Immutable reviewer evidence</a>' in rendered
    assert 'href="https://invalid' not in rendered
    assert "raw-only" not in rendered


@pytest.mark.parametrize(
    "original", ["current", "advisory", "deferred", "out_of_scope", "superseded", "", "<script>&"]
)
def test_html_displays_original_reviewer_disposition_inertly(original):
    from html import escape

    package = {
        "slug": "review",
        "review_dispositions": [
            {
                "criterion": "review_rtl_bugs_done",
                "severity": "MAJOR",
                "file": "rtl/dut.sv",
                "line": 0,
                "disposition": "reported",
                "reviewer_disposition": original,
                "summary": "finding",
            }
        ],
    }
    rendered = render_explanation_html(StructuredExplanation.parse(_value()), package)
    assert "<th>Reviewer disposition</th>" in rendered
    assert f"<td>reported</td><td>{escape(original)}</td>" in rendered
    assert rendered.count("<script>") == 1
