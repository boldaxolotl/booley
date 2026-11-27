"""Shared fakes for Reviewer tests: the shared scripted agent, answering per review guide.

``install_agent`` also fakes the agent workspace snapshot (``specialist.isolated_agent_workspace``).
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import pytest

from booley.core.models import AgentCallParams
from booley.evidence.review_vocabulary import ORIGIN_PRE_EXISTING
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.specialists.reviewer import ReviewerSpecialist
from tests.agent_call_fake import Script, ScriptedAgent, play
from tests.agent_call_fake import install_agent as install_scripted

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


class GuideScriptedAgent(ScriptedAgent):
    """Also plays a ``{guide: script}`` mapping (``"*"`` for the rest), read at each call."""

    def answer(self, script: Script | Mapping[str, Script]) -> None:
        guides, seen = script, {}

        def by_guide(params: AgentCallParams) -> Any:
            guide = guide_of(params)
            seen[guide] = seen.get(guide, -1) + 1
            return play(guides.get(guide, guides.get("*", {"issues": []})), params, seen[guide])

        super().answer(by_guide if isinstance(guides, dict) and "issues" not in guides else script)


def install_agent(monkeypatch: pytest.MonkeyPatch, *, snapshot: bool = False) -> ScriptedAgent:
    """Replace the model with a ScriptedAgent; skip the workspace snapshot unless asked."""
    agent = install_scripted(monkeypatch, {"issues": []}, GuideScriptedAgent)
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
