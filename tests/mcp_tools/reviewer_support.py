"""Shared fakes for Reviewer tests: one scripted agent behind the public ``call_agent`` seam."""

from __future__ import annotations

import contextlib
import json
import re
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

import pytest

from booley.core.models import AgentCallParams, AgentResult
from booley.evidence.review_vocabulary import ORIGIN_PRE_EXISTING
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.specialists.reviewer import ReviewerSpecialist

Answer = AgentResult | Exception | dict[str, Any] | str
_GUIDE_HEADING = re.compile(r"^## Review guide — \S+ \(([^)]+)\)", re.MULTILINE)


def issue(**overrides: Any) -> dict[str, Any]:
    """One schema-valid canonical finding (rtl bugs, MAJOR, current)."""
    row: dict[str, Any] = {
        "severity": "MAJOR",
        "confidence": "HIGH",
        "category": "bugs",
        "kind": "code_defect",
        "disposition": "current",
        "ticket_clause": "Reviewed source must satisfy the current Ticket requirements.",
        "file": "rtl/dut.sv",
        "line": 3,
        "summary": "Wrong reset value",
        "fix_suggestion": "Fix it",
    }
    row.update(overrides)
    return row


def guide_of(params: AgentCallParams) -> str:
    """The guide names one call's system prompt carries, joined with ``+``."""
    return "+".join(_GUIDE_HEADING.findall(params.system_prompt or ""))


def open_findings(detail: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The corrective rows of a review detail."""
    return [
        row
        for row in detail["issue_list"]
        if row["disposition"] == "current" and row.get("origin") != ORIGIN_PRE_EXISTING
    ]


def _result(answer: Answer) -> AgentResult:
    if isinstance(answer, AgentResult):
        return answer
    if isinstance(answer, str):
        return AgentResult(output=answer)
    return AgentResult(output=json.dumps(answer))


class ScriptedAgent:
    """Records every ``AgentCallParams`` and answers from a script.

    ``answer`` takes one answer for every call, a ``{guide: answer}`` mapping, or a
    list consumed one per call (the last repeats). An answer is a payload dict (the
    JSON the agent prints), raw output text, an ``AgentResult`` or an exception.
    """

    def __init__(self) -> None:
        self.calls: list[AgentCallParams] = []
        self._script: Answer | Mapping[str, Answer] | Sequence[Answer] = {"issues": []}
        self._lock = threading.Lock()
        self._seen: dict[str, int] = {}
        self.on_call: Callable[[AgentCallParams], None] | None = None
        self.on_events: list[Any] = []  # the ``on_event`` callback each call received

    def answer(self, script: Answer | Mapping[str, Answer] | Sequence[Answer]) -> None:
        self._script = script

    def reported(self, *calls: dict[str, Any], output: str = "") -> AgentResult:
        """An agent result whose ``ReportFindings`` capability was called once per dict."""
        return AgentResult(
            output=output, captured_agent_capability_calls={"ReportFindings": list(calls)}
        )

    def _pick(self, params: AgentCallParams, index: int) -> Answer:
        script = self._script
        if isinstance(script, dict) and "issues" not in script:
            guide = guide_of(params)
            per_guide = script.get(guide, script.get("*", {"issues": []}))
            index = self._seen[guide] = self._seen.get(guide, -1) + 1
            script = per_guide
        if isinstance(script, list):
            return script[min(index, len(script) - 1)]
        return script  # type: ignore[return-value]

    async def __call__(self, params: AgentCallParams, on_event: Any = None) -> AgentResult:
        with self._lock:
            index = len(self.calls)
            self.calls.append(params)
            self.on_events.append(on_event)
        if self.on_call is not None:
            self.on_call(params)
        with self._lock:
            answer = self._pick(params, index)
        if isinstance(answer, Exception):
            raise answer
        return _result(answer)


def install_agent(monkeypatch: pytest.MonkeyPatch, *, snapshot: bool = False) -> ScriptedAgent:
    """Replace the model with a ScriptedAgent; skip the workspace snapshot unless asked."""
    agent = ScriptedAgent()
    monkeypatch.setattr("booley.runtime.agent.call_agent", agent)
    if not snapshot:

        @contextlib.contextmanager
        def passthrough(params: Any, _access: Any, _category: Any = None, **_kw: Any) -> Iterator:
            yield params, None

        monkeypatch.setattr("booley.specialists.specialist.isolated_agent_workspace", passthrough)
    return agent


def run_review(
    argv: Sequence[str], endpoint: ReviewerSpecialist | None = None
) -> tuple[EndpointOutcome, ReviewerSpecialist]:
    """``parse_args`` then the public run of a Reviewer (a new one unless given)."""
    endpoint = endpoint or ReviewerSpecialist()
    endpoint.parse_args(list(argv))
    return endpoint.prepare_endpoint_invocation().run(None), endpoint
