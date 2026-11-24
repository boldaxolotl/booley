"""Shared fake of the two command boundaries the Sandbox Image init step crosses.

The step talks to the Docker CLI (``docker image inspect``, ``pull``, ``tag``,
``system df`` and ``build``) and to the wheel build (``python -P -m build``).
:func:`serve_sandbox_image_docker` answers those commands, so every Booley
function between ``_step_docker_image`` and them runs for real.  ``docker build``
starts a short-lived real child with the build's own keyword arguments, so the
pipe decoding, progress and diagnostics of ``run_docker_build`` run too.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
import subprocess
import sys
from collections import namedtuple
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.docker_cli_guard import route_docker_helper_through_subprocess_run

_PLENTY = namedtuple("_PLENTY", "total used free")(2**50, 0, 2**50)
_LABEL_TEMPLATE = re.compile(r'index \.Config\.Labels "([^"]+)"')


@dataclass
class Reply:
    """What one Docker verb or the wheel build answers."""

    code: int = 0
    stdout: str = ""
    stderr: str = ""
    raises: BaseException | None = None


@dataclass
class Build:
    """What one ``docker build`` writes and exits with (``raises``: the CLI cannot start)."""

    code: int = 0
    output: bytes = b""
    raises: BaseException | None = None


class SandboxImageDocker:
    """Images by reference, per-verb replies, and the commands received."""

    def __init__(self, real_popen: Callable[..., Any]) -> None:
        self._real_popen = real_popen
        self.images: dict[str, dict[str, Any]] = {}
        self.failing: set[str] = set()
        self.pull = Reply()
        self.tag = Reply()
        self.system_df = Reply(stdout="Build Cache\t0B\t0B\n")
        self.wheel = Reply()
        self.builds: dict[str, Build] = {}
        #: Callbacks run after the wheel build (key ``"wheel"``) or after the build of an image tag.
        self.after: dict[str, Callable[[], None]] = {}
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self._ids = 0

    def add(self, reference: str, labels: dict[str, str] | None = None) -> str:
        """Put an image under *reference* with a fresh Id and return the Id."""
        self._ids += 1
        image_id = f"sha256:{self._ids:064x}"
        self.images[reference] = {"Id": image_id, "Labels": dict(labels or {})}
        return image_id

    def retag(self, reference: str) -> str:
        return self.add(reference, self.images[reference]["Labels"])

    def count(self, *prefix: str) -> int:
        """Number of ``docker`` commands that start with *prefix* (after ``docker``)."""
        return sum(call[1 : 1 + len(prefix)] == list(prefix) for call in self.calls)

    def sent(self, *prefix: str) -> list[dict[str, Any]]:
        """Keyword arguments of each ``docker`` command that starts with *prefix*."""
        return [
            kwargs
            for call, kwargs in zip(self.calls, self.kwargs, strict=True)
            if call[1 : 1 + len(prefix)] == list(prefix)
        ]

    def _image(self, reference: str) -> dict[str, Any] | None:
        if reference in self.images:
            return self.images[reference]
        return next((i for i in self.images.values() if i["Id"] == reference), None)

    def _inspect(self, argv: list[str]) -> tuple[int, str, str]:
        reference = argv[-1] if argv[2] == "-f" else argv[2]
        if reference in self.failing:
            return 1, "", "daemon down"
        image = self._image(reference)
        if image is None:
            return 1, "", f"Error: No such image: {reference}"
        if argv[2] == "-f":
            label = _LABEL_TEMPLATE.search(argv[3])
            assert label is not None, argv
            return 0, f"{image['Labels'].get(label.group(1), '<no value>')}\n", ""
        if argv[3:] == ["--format", "{{.Id}}"]:
            return 0, f"{image['Id']}\n", ""
        record = {"Id": image["Id"], "Config": {"Labels": image["Labels"]}}
        return 0, json.dumps([record]), ""

    def _docker(self, argv: list[str]) -> Reply:
        verb = argv[1:3] if argv[1] == "image" else argv[1:2]
        if verb == ["image", "inspect"]:
            return Reply(*self._inspect(argv[1:]))
        if verb == ["pull"]:
            if self.pull.raises is None and self.pull.code == 0 and argv[2] not in self.images:
                self.add(argv[2])
            return self.pull
        if verb == ["tag"]:
            source = self._image(argv[2])
            if self.tag.raises is None and self.tag.code == 0 and source is not None:
                self.images[argv[3]] = source
            return self.tag
        if verb == ["info"]:
            return Reply(stdout=Path(__file__).resolve().anchor)  # any absolute path
        if verb == ["system"]:
            return self.system_df
        raise AssertionError(f"unexpected Docker command: {argv}")

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Stand in for ``subprocess.run``: a Docker verb or the wheel build."""
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        wheel = argv[1:6] == ["-P", "-m", "build", "--wheel", "--outdir"]
        if argv[0] == "docker":
            reply = self._docker(argv)
        elif wheel:
            reply = self.wheel
            if reply.code == 0:
                outdir = Path(argv[6])
                outdir.mkdir(parents=True, exist_ok=True)
                (outdir / "booley_rtl-0.0.0-py3-none-any.whl").write_bytes(b"")
        else:
            raise AssertionError(f"unexpected command: {argv}")
        if reply.raises is not None:
            raise reply.raises
        if wheel and "wheel" in self.after:
            self.after["wheel"]()
        if reply.code != 0 and kwargs.get("check"):
            raise subprocess.CalledProcessError(reply.code, argv, reply.stdout, reply.stderr)
        return subprocess.CompletedProcess(argv, reply.code, reply.stdout, reply.stderr)

    def popen(self, argv: list[str], *args: Any, **kwargs: Any) -> Any:
        """Stand in for ``subprocess.Popen``: ``docker build`` starts a real child."""
        if list(argv[:2]) != ["docker", "build"]:
            return self._real_popen(argv, *args, **kwargs)
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        tag = argv[argv.index("-t") + 1]
        build = self.builds.get(tag, Build())
        if build.raises is not None:
            raise build.raises
        code = (
            f"import sys; sys.stdout.buffer.write({build.output!r}); "
            f"sys.stdout.buffer.flush(); sys.exit({build.code})"
        )
        child = self._real_popen([sys.executable, "-c", code], **kwargs)
        if build.code == 0:
            labels = dict(
                item.split("=", 1) for flag, item in itertools.pairwise(argv) if flag == "--label"
            )
            self.add(tag, labels)
        if tag in self.after:
            self.after[tag]()
        return child


def serve_sandbox_image_docker(monkeypatch: pytest.MonkeyPatch) -> SandboxImageDocker:
    """Answer the Docker CLI and the wheel build for the rest of the test."""
    route_docker_helper_through_subprocess_run(monkeypatch)
    fake = SandboxImageDocker(subprocess.Popen)
    real_which = shutil.which
    # The capacity preflight runs for real; the host's free disk is not under test.
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: _PLENTY)
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(subprocess, "Popen", fake.popen)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *args, **kwargs: (
            "docker" if name == "docker" else real_which(name, *args, **kwargs)
        ),
    )
    return fake
