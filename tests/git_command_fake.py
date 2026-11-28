"""A fake of the command boundary for code that runs Git through ``subprocess.run``.

Real Git runs for every command no rule answers, unless ``others`` is set to answer it;
fixtures that build repositories call ``REAL_RUN``, which never passes through the rules.
``commands`` and ``options`` record each call's argument list and keyword arguments
(``stdin``, ``timeout``, ``env``, ``input``).
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
    edit: Callable[[Any, dict[str, Any]], Any] | None = None

    def fires_for(self, command: Sequence[str]) -> bool:
        if not all(word in command for word in self.words):
            return False
        self.seen += 1
        return self.call is None or self.seen == self.call


class GitCommandFake:
    """Stand-in for ``subprocess.run``: rules answer commands, ``others`` or real Git the rest."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []  # every command run while installed
        self.options: list[dict[str, Any]] = []  # keyword arguments of each command
        self._rules: list[_Rule] = []
        self.others: Callable[..., Any] | None = None  # answers non-Git and unanswered commands

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

    def hook(self, *words: str, action: Callable[[], object], call: int | None = 1) -> None:
        self._rules.append(_Rule(words, call, action, None, None))

    def rewrite(
        self, *words: str, edit: Callable[[Any, dict[str, Any]], Any], call: int | None = None
    ) -> None:
        """Run real Git, then return ``edit(result, options)`` (corrupt or fail its output)."""
        self._rules.append(_Rule(words, call, None, None, None, edit=edit))

    def calls(self, word: str) -> list[tuple[list[str], dict[str, Any]]]:
        """The recorded ``(command, options)`` pairs whose command holds *word*."""
        return [(c, o) for c, o in zip(self.commands, self.options, strict=True) if word in c]

    def run(self, command: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        argv = [str(part) for part in command]
        self.commands.append(argv)
        self.options.append(dict(kwargs))
        if self.others is not None and argv[:1] != ["git"]:
            return self.others(command, *args, **kwargs)
        for rule in self._rules:
            if not rule.fires_for(argv):
                continue
            if rule.before is not None:
                rule.before()
            if rule.error is not None:
                raise rule.error
            if rule.answer is not None:
                return _completed(command, kwargs, *rule.answer)
            if rule.edit is not None:
                return rule.edit(REAL_RUN(command, *args, **kwargs), kwargs)
        return (self.others or REAL_RUN)(command, *args, **kwargs)


def with_stdout(result: Any, stdout: bytes) -> subprocess.CompletedProcess[Any]:
    """A copy of a real *result* that carries other *stdout* (for ``rewrite`` edits)."""
    return subprocess.CompletedProcess(result.args, result.returncode, stdout, result.stderr)


def _completed(
    command: Sequence[str], kwargs: dict[str, Any], returncode: int, stdout: bytes, stderr: bytes
) -> subprocess.CompletedProcess[Any]:
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        return subprocess.CompletedProcess(
            command, returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
        )
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def answer_git_version(monkeypatch: pytest.MonkeyPatch, reply: str | OSError) -> None:
    """Answer ``git --version`` with *reply* (stdout, or the error starting Git raises).

    Booley probes the version through ``platform_paths.run_captured_bounded``, not
    ``subprocess.run``; every other command still runs real Git.
    """
    from booley.runtime import platform_paths

    real = platform_paths.run_captured_bounded

    def run(argv: list[str], **kwargs: Any) -> Any:
        if argv != ["git", "--version"]:
            return real(argv, **kwargs)
        if isinstance(reply, OSError):
            raise reply
        return subprocess.CompletedProcess(argv, 0, reply, "")

    monkeypatch.setattr(platform_paths, "run_captured_bounded", run)
