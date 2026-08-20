"""Structured agent explanation data and Booley-owned HTML rendering."""

from __future__ import annotations

import hashlib
import re
from base64 import b64encode
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from typing import Any

from booley.core.boundary import BoundaryError, require_bool, require_dict, require_str


class ExplanationError(ValueError):
    """Structured explanation data violates the agent-output contract."""


def _plain(value: str, field: str) -> str:
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        raise ExplanationError(f"{field} contains forbidden control content")
    if re.search(r"<\s*/?\s*[a-z][^>]*>", value, flags=re.IGNORECASE):
        raise ExplanationError(f"{field} must be plain text, not HTML")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ExplanationError(f"{field} must be a list")
    return value


@dataclass(frozen=True)
class ExplanationSection:
    title: str
    body: str

    @classmethod
    def parse(cls, value: Any) -> ExplanationSection:
        row = require_dict(value, field="explanation section")
        return cls(
            _plain(require_str(row, "title"), "section title"),
            _plain(require_str(row, "body"), "section body"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "body": self.body}


@dataclass(frozen=True)
class CodeReference:
    repository: str
    path: str
    revision: str
    summary: str

    @classmethod
    def parse(cls, value: Any) -> CodeReference:
        row = require_dict(value, field="code reference")
        return cls(
            _plain(require_str(row, "repository"), "code repository"),
            _plain(require_str(row, "path"), "code path"),
            _plain(require_str(row, "revision"), "code revision"),
            _plain(require_str(row, "summary"), "code summary"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "repository": self.repository,
            "path": self.path,
            "revision": self.revision,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class ExplanationFinding:
    title: str
    detail: str

    @classmethod
    def parse(cls, value: Any) -> ExplanationFinding:
        row = require_dict(value, field="explanation finding")
        return cls(
            _plain(require_str(row, "title"), "finding title"),
            _plain(require_str(row, "detail"), "finding detail"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "detail": self.detail}


@dataclass(frozen=True)
class QuizChoice:
    text: str
    correct: bool
    feedback: str

    @classmethod
    def parse(cls, value: Any) -> QuizChoice:
        row = require_dict(value, field="quiz choice")
        return cls(
            _plain(require_str(row, "text"), "quiz choice"),
            require_bool(row, "correct", field="quiz choice correct"),
            _plain(require_str(row, "feedback"), "quiz feedback"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "correct": self.correct, "feedback": self.feedback}


@dataclass(frozen=True)
class QuizQuestion:
    question: str
    choices: tuple[QuizChoice, ...]

    @classmethod
    def parse(cls, value: Any) -> QuizQuestion:
        row = require_dict(value, field="quiz question")
        choices = tuple(QuizChoice.parse(item) for item in _list(row.get("choices"), "choices"))
        if len(choices) < 2:
            raise ExplanationError("quiz question needs at least two choices")
        if sum(choice.correct for choice in choices) != 1:
            raise ExplanationError("quiz question must have exactly one correct choice")
        return cls(_plain(require_str(row, "question"), "quiz question"), choices)

    def to_dict(self) -> dict[str, Any]:
        return {"question": self.question, "choices": [item.to_dict() for item in self.choices]}


@dataclass(frozen=True)
class StructuredExplanation:
    """Validated plain-text explanation returned by the report agent."""

    background: tuple[ExplanationSection, ...]
    intuition: tuple[ExplanationSection, ...]
    code_references: tuple[CodeReference, ...]
    findings: tuple[ExplanationFinding, ...]
    quiz: tuple[QuizQuestion, ...]

    @classmethod
    def parse(cls, value: Any) -> StructuredExplanation:
        try:
            row = require_dict(value, field="structured explanation")
            background = tuple(
                ExplanationSection.parse(item)
                for item in _list(row.get("background"), "background")
            )
            intuition = tuple(
                ExplanationSection.parse(item) for item in _list(row.get("intuition"), "intuition")
            )
            references = tuple(
                CodeReference.parse(item)
                for item in _list(row.get("code_references"), "code_references")
            )
            findings = tuple(
                ExplanationFinding.parse(item)
                for item in _list(row.get("findings"), "explanation findings")
            )
            quiz = tuple(QuizQuestion.parse(item) for item in _list(row.get("quiz"), "quiz"))
            if not background or not intuition or not references:
                raise ExplanationError(
                    "explanation needs background, intuition, and code references"
                )
            if len(quiz) != 5:
                raise ExplanationError("explanation must contain exactly five quiz questions")
            return cls(background, intuition, references, findings, quiz)
        except BoundaryError as exc:
            raise ExplanationError(str(exc)) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "background": [item.to_dict() for item in self.background],
            "intuition": [item.to_dict() for item in self.intuition],
            "code_references": [item.to_dict() for item in self.code_references],
            "findings": [item.to_dict() for item in self.findings],
            "quiz": [item.to_dict() for item in self.quiz],
        }


_QUIZ_SCRIPT = """document.addEventListener('click',function(e){const b=e.target.closest('.quiz-option');if(!b)return;const q=b.closest('.quiz-question');const o=q.querySelector('.quiz-feedback');const c=b.dataset.correct==='true';o.textContent=(c?'Correct. ':'Not quite. ')+b.dataset.feedback;o.className='quiz-feedback '+(c?'correct':'incorrect');});"""
_SCRIPT_HASH = b64encode(hashlib.sha256(_QUIZ_SCRIPT.encode()).digest()).decode()
_CSS = """
body{font:16px/1.55 system-ui,sans-serif;max-width:960px;margin:auto;padding:2rem;color:#17202a;background:#f7f9fb}h1,h2,h3{line-height:1.2}.card,.quiz-question{background:white;border:1px solid #d9e2ec;border-radius:10px;padding:1rem;margin:1rem 0}.plain{white-space:pre-wrap}.quiz-option{display:block;width:100%;text-align:left;margin:.5rem 0;padding:.7rem;border:1px solid #9fb3c8;border-radius:6px;background:#f0f4f8}.correct{color:#087f23}.incorrect{color:#b42318}table{border-collapse:collapse;width:100%}th,td{border:1px solid #bcccdc;padding:.5rem;text-align:left}code{overflow-wrap:anywhere}
""".strip()


def _section_cards(title: str, sections: tuple[ExplanationSection, ...]) -> str:
    cards = "".join(
        f'<article class="card"><h3>{escape(item.title)}</h3>'
        f'<div class="plain">{escape(item.body)}</div></article>'
        for item in sections
    )
    return f"<section><h2>{title}</h2>{cards}</section>"


def _criteria_table(package: Mapping[str, Any]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{escape(str(row['criterion']))}</td>"
        f"<td>{escape(str(row['outcome']))}</td>"
        f"<td>{escape(str(row['freshness']))}</td>"
        "</tr>"
        for row in package.get("criteria", [])
    )
    return (
        "<section><h2>Criteria</h2><table><tr><th>Criterion</th><th>Outcome</th><th>Freshness</th></tr>"
        + rows
        + "</table></section>"
    )


def _changed_files(package: Mapping[str, Any]) -> str:
    rows = "".join(
        f"<li><code>{escape(str(row['path']))}</code> — "
        f"{escape(str(row['action']))}; {escape(str(row['content_kind']))}; "
        f"{escape(str(row['presentation']))}</li>"
        for row in package.get("changed_files", [])
    )
    return f"<section><h2>Changed files</h2><ul>{rows}</ul></section>"


def _package_findings(package: Mapping[str, Any]) -> str:
    assessment = package.get("assessment", {})
    items = list(assessment.get("findings", [])) if isinstance(assessment, Mapping) else []
    health = package.get("health", {})
    if isinstance(health, Mapping):
        for label, key in (
            ("Dirty worktree", "dirty_worktree"),
            ("Flow or specialist exit 2", "exit_2_tools"),
            ("Developer crashes", "developer_crashes"),
            ("Missing evidence", "missing_evidence"),
            ("Harness-path contamination", "harness_paths"),
        ):
            values = health.get(key, [])
            if values:
                items.append(f"{label}: {', '.join(map(str, values))}")
    return "".join(f"<li>{escape(str(item))}</li>" for item in items)


def render_explanation_html(
    explanation: StructuredExplanation,
    package: Mapping[str, Any],
) -> str:
    """Render escaped deterministic HTML from one typed package."""
    references = "".join(
        f'<article class="card"><h3><code>{escape(item.path)}</code></h3>'
        f"<p>{escape(item.repository)} @ <code>{escape(item.revision)}</code></p>"
        f'<div class="plain">{escape(item.summary)}</div></article>'
        for item in explanation.code_references
    )
    explanation_findings = (
        "".join(
            f'<article class="card"><h3>{escape(item.title)}</h3>'
            f'<div class="plain">{escape(item.detail)}</div></article>'
            for item in explanation.findings
        )
        or "<p>None.</p>"
    )
    package_findings = _package_findings(package)
    quiz = "".join(
        _render_quiz(index, question) for index, question in enumerate(explanation.quiz, 1)
    )
    csp = (
        "default-src 'none'; style-src 'unsafe-inline'; "
        f"script-src 'sha256-{_SCRIPT_HASH}'; connect-src 'none'; object-src 'none'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
        f"<style>{_CSS}</style><title>{escape(str(package['slug']))} review</title></head><body>"
        f"<h1>{escape(str(package['slug']))}</h1>"
        + _section_cards("Background", explanation.background)
        + _section_cards("Intuition", explanation.intuition)
        + f"<section><h2>Code and change references</h2>{references}</section>"
        + _changed_files(package)
        + _criteria_table(package)
        + "<section><h2>Findings</h2>"
        + explanation_findings
        + f"<ul>{package_findings}</ul></section>"
        + f"<section><h2>Quiz</h2>{quiz}</section>"
        + f"<script>{_QUIZ_SCRIPT}</script></body></html>\n"
    )


def _render_quiz(index: int, question: QuizQuestion) -> str:
    choices = "".join(
        f'<button class="quiz-option" data-correct="{str(choice.correct).lower()}" '
        f'data-feedback="{escape(choice.feedback, quote=True)}">{escape(choice.text)}</button>'
        for choice in question.choices
    )
    return (
        f'<article class="quiz-question"><h3>{index}. {escape(question.question)}</h3>'
        f'{choices}<p class="quiz-feedback" aria-live="polite"></p></article>'
    )
