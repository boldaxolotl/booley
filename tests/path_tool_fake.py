"""A fake of the command boundary for tools that code finds by name on the search path.

``PathToolFake.install`` writes a launcher named like the tool into a directory that comes
first on ``PATH``, so the code under test (in process or in a child it starts) runs the
fake instead of the real tool. Tests state answers with ``answer`` and read the calls the
fake received with ``calls``.

A rule matches a call when every word equals some argument; the first matching rule wins.
Answers given with the same words form a sequence: the n-th matching call gets the n-th
answer and the last one repeats. A call no rule matches exits 127 on stderr, so a stray
call fails loudly. The fake logs a call only after it wrote its whole answer (a call that
hangs logs first), so a logged call is a handshake the test can poll.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_CHILD = """\
import base64, json, os, sys, time
from pathlib import Path

here = Path(__file__).resolve().parent
args = sys.argv[1:]
rules = json.loads((here / "NAME_rules.json").read_text(encoding="utf-8"))
log = here / "NAME_calls.jsonl"
done = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
first = next((rule for rule in rules if all(word in args for word in rule["words"])), None)
if first is None:
    sys.stderr.write("path tool fake: no answer for " + " ".join(args) + "\\n")
    raise SystemExit(127)
sequence = [rule for rule in rules if rule["words"] == first["words"]]
seen = sum(1 for entry in done if entry["words"] == first["words"])
rule = sequence[min(seen, len(sequence) - 1)]
text = sys.stdin.read() if rule["reads_input"] else None
entry = {"args": args, "words": first["words"], "input": text, "env": sorted(os.environ)}
if rule["hang_s"] is not None:
    with log.open("a", encoding="utf-8", newline="\\n") as handle:
        handle.write(json.dumps(entry) + "\\n")
    time.sleep(rule["hang_s"])
    raise SystemExit(rule["returncode"])
if rule["output"] is not None:
    Path(args[args.index("-o") + 1]).write_bytes(base64.b64decode(rule["output"]))
sys.stdout.write(rule["stdout"])
sys.stdout.flush()
sys.stderr.write(rule["stderr"])
sys.stderr.flush()
with log.open("a", encoding="utf-8", newline="\\n") as handle:
    handle.write(json.dumps(entry) + "\\n")
raise SystemExit(rule["returncode"])
"""


@dataclass(frozen=True)
class Call:
    args: list[str]
    input: str | None  # stdin, for answers that declare reads_input
    env: list[str]  # names of the environment variables the tool saw


class PathToolFake:
    """A command-line tool on ``PATH`` that answers from rules the test states."""

    def __init__(self, directory: Path, name: str) -> None:
        self.executable = directory / (f"{name}.cmd" if os.name == "nt" else name)
        self.rules_file = directory / f"{name}_rules.json"
        self.log_file = directory / f"{name}_calls.jsonl"
        self.rules: list[dict[str, object]] = []
        self.save()
        source = _CHILD.replace("NAME", name)
        if os.name == "nt":
            script = directory / f"{name}_fake.py"
            script.write_bytes(source.encode("utf-8"))
            launcher = f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n'
            self.executable.write_bytes(launcher.encode("utf-8"))
        else:
            self.executable.write_bytes(f"#!{sys.executable}\n{source}".encode())
            self.executable.chmod(0o755)

    @classmethod
    def install(cls, directory: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> PathToolFake:
        directory.mkdir(parents=True, exist_ok=True)
        fake = cls(directory, name)
        monkeypatch.setenv("PATH", os.pathsep.join((str(directory), os.environ.get("PATH", ""))))
        return fake

    def save(self) -> None:
        self.rules_file.write_bytes(json.dumps(self.rules).encode("utf-8"))

    def answer(
        self,
        *words: str,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        reads_input: bool = False,
        hang_s: float | None = None,
        output: bytes | None = None,  # written to the file after ``-o``
    ) -> None:
        self.rules.append(
            {
                "words": list(words),
                "stdout": stdout,
                "stderr": stderr,
                "returncode": returncode,
                "reads_input": reads_input,
                "hang_s": hang_s,
                "output": None if output is None else base64.b64encode(output).decode("ascii"),
            }
        )
        self.save()

    def calls(self) -> list[Call]:
        if not self.log_file.exists():
            return []
        # A line still being appended has no newline yet, so it is not a call yet.
        lines = self.log_file.read_text(encoding="utf-8").split("\n")[:-1]
        entries: list[dict[str, Any]] = [json.loads(line) for line in lines]
        return [Call(entry["args"], entry["input"], entry["env"]) for entry in entries]
