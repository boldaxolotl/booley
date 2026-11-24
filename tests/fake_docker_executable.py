"""A fake ``docker`` program on ``PATH``: the shared fake of the Docker CLI boundary.

Tests that need Booley's Docker calls answered install it with
``install_fake_docker``; nothing in ``subprocess``, command lookup or a Booley runner
is patched.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

_SCRIPT = """\
import json, sys
from pathlib import Path

here = Path(__file__).parent
replies = json.loads((here / "fake_docker_replies.json").read_text(encoding="utf-8"))
command = sys.argv[1:]
with (here / "fake_docker_calls.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps(command) + "\\n")
if command and command[0] == "build":
    (here / "build-started").touch()
first, last = (command[0], command[-1]) if command else ("", "")
code, text = replies.get(f"{first} {last}", replies.get(first, [0, ""]))
if text:
    print(text, file=sys.stdout if code == 0 else sys.stderr)
raise SystemExit(code)
"""


@dataclass(frozen=True)
class FakeDocker:
    """The installed fake: the program name and path, the marker a ``docker build`` leaves."""

    name: str
    build_marker: Path
    path: Path
    replies_path: Path  # rewrite it to change the replies: the program rereads it per call

    @property
    def calls(self) -> list[list[str]]:
        """Every argument list the program was called with, in order."""
        log = (self.path.parent / "fake_docker_calls.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in log.splitlines()]

    @property
    def built(self) -> bool:
        return self.build_marker.exists()


def install_fake_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replies: Mapping[str, tuple[int, str]]
) -> FakeDocker:
    """Put a ``docker`` on ``PATH`` answering with ``(exit code, text)``.

    A reply keyed ``"<first> <last>"`` argument (``"image sha256:aa"``) wins over one keyed
    by the first argument. The text goes to stdout on exit code 0 and to stderr otherwise;
    a call without a reply exits 0 with no output. A ``build`` also touches ``build_marker``.
    """
    replies_path = tmp_path / "fake_docker_replies.json"
    replies_path.write_bytes(json.dumps(replies).encode("utf-8"))
    script = tmp_path / "fake_docker.py"
    script.write_bytes(_SCRIPT.encode("utf-8"))
    if os.name == "nt":
        docker = tmp_path / "docker.cmd"
        docker.write_bytes(f'@"{sys.executable}" "{script}" %*\r\n'.encode())
    else:
        docker = tmp_path / "docker"
        docker.write_bytes(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n'.encode()
        )
        docker.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(tmp_path), os.environ["PATH"])))
    return FakeDocker(docker.name, tmp_path / "build-started", docker, replies_path)
