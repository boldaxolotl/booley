"""A fake of the command boundary for code that runs Git through ``subprocess.run``.

Real Git runs for every command no rule answers; fixtures that build repositories call
``REAL_RUN``, which never passes through the rules.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import pytest

REAL_RUN = subprocess.run


@dataclass
class _Rule:
    words: tuple[str, ...]
    call: int | None
    before: Callable[[], object] | None
    error: BaseException | None
    answer: tuple[int, bytes, bytes] | None
    seen: int = 0

    def fires_for(self, command: Sequence[str]) -> bool:
        if not all(word in command for word in self.words):
            return False
        self.seen += 1
        return self.call is None or self.seen == self.call


class GitCommandFake:
    """Stand-in for ``subprocess.run``: real Git, except for the commands a rule answers."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []  # every command run while installed
        self._rules: list[_Rule] = []

    @classmethod
    def install(cls, monkeypatch: pytest.MonkeyPatch) -> GitCommandFake:
        fake = cls()
        monkeypatch.setattr(subprocess, "run", fake.run)
        return fake

    def answer(
        self,
        *words: str,
        call: int | None = None,
        returncode: int = 128,
        stdout: bytes = b"",
        stderr: bytes = b"simulated failure",
        before: Callable[[], object] | None = None,
    ) -> None:
        self._rules.append(_Rule(words, call, before, None, (returncode, stdout, stderr)))

    def crash(
        self,
        *words: str,
        error: BaseException,
        call: int | None = None,
        before: Callable[[], object] | None = None,
    ) -> None:
        self._rules.append(_Rule(words, call, before, error, None))

    def hook(self, *words: str, action: Callable[[], object], call: int = 1) -> None:
        self._rules.append(_Rule(words, call, action, None, None))

    def run(self, command: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        self.commands.append(list(command))
        for rule in self._rules:
            if not rule.fires_for(command):
                continue
            if rule.before is not None:
                rule.before()
            if rule.error is not None:
                raise rule.error
            if rule.answer is not None:
                return _completed(command, kwargs, *rule.answer)
        return REAL_RUN(command, *args, **kwargs)


def _completed(
    command: Sequence[str], kwargs: dict[str, Any], returncode: int, stdout: bytes, stderr: bytes
) -> subprocess.CompletedProcess[Any]:
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)
