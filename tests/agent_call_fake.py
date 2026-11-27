"""The shared fake of the agent call: one scripted agent behind
``booley.runtime.agent.call_agent``."""

from __future__ import annotations

import inspect
import json
import threading
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from booley.core.models import AgentCallParams, AgentResult

Answer = AgentResult | Exception | dict[str, Any] | str
Script = Answer | Sequence[Answer] | Callable[[AgentCallParams], Any]


def play(script: Script, params: AgentCallParams, index: int) -> Any:
    """What *script* answers to the call with *params*, the *index*-th call it answers."""
    if callable(script):
        return script(params)
    return script[min(index, len(script) - 1)] if isinstance(script, list) else script


class ScriptedAgent:
    """Records every ``AgentCallParams`` and answers from a script.

    ``answer`` takes one answer for every call, a list consumed one per call (the last
    repeats) or a callable (sync or async) of the call's params that returns an answer or
    raises. An answer is a payload dict (the JSON the agent prints), raw output text, an
    ``AgentResult`` or an exception.
    """

    def __init__(self, script: Script = "") -> None:
        self.calls: list[AgentCallParams] = []
        self.answer(script)
        self._lock = threading.Lock()
        self.on_call: Callable[[AgentCallParams], None] | None = None
        self.on_events: list[Any] = []  # the ``on_event`` callback each call received

    def answer(self, script: Script) -> None:
        self._script = script

    def reported(self, *calls: dict[str, Any], output: str = "") -> AgentResult:
        """An agent result whose ``ReportFindings`` capability was called once per dict."""
        return AgentResult(
            output=output, captured_agent_capability_calls={"ReportFindings": list(calls)}
        )

    async def __call__(self, params: AgentCallParams, on_event: Any = None) -> AgentResult:
        with self._lock:
            index = len(self.calls)
            self.calls.append(params)
            self.on_events.append(on_event)
        if self.on_call is not None:
            self.on_call(params)
        with self._lock:
            answer = play(self._script, params, index)
        if inspect.isawaitable(answer):
            answer = await answer
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, AgentResult):
            return answer
        return AgentResult(output=answer if isinstance(answer, str) else json.dumps(answer))


def install_agent(
    monkeypatch: pytest.MonkeyPatch,
    script: Script = "",
    agent_type: type[ScriptedAgent] = ScriptedAgent,
) -> ScriptedAgent:
    """Answer every agent call with an *agent_type* playing *script*."""
    agent = agent_type(script)
    monkeypatch.setattr("booley.runtime.agent.call_agent", agent)  # boundary: provider CLI
    return agent
