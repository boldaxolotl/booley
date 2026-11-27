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
from typing import Any

import pytest

from tests.docker_cli_guard_plugin import allow_docker_cli

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
second = command[1] if len(command) > 1 else ""
keys = (f"{first} {last}", f"{first} {second}", first)
reply = next((replies[key] for key in keys if key in replies), None)
store_file = here / "fake_docker_images.json"
store = json.loads(store_file.read_text(encoding="utf-8")) if store_file.exists() else None


def find(images, ref):
    return ref if ref in images else next((k for k, v in images.items() if v["Id"] == ref), None)


def serve(store, command):
    images = store["images"]
    missing = (1, "Error: No such image: " + command[-1])
    if command[:2] == ["image", "inspect"]:
        key = find(images, command[2])
        if key is None:
            return missing
        found = images[key]
        document = {"Id": found["Id"], "Config": {"Labels": found["Labels"]}}
        document["RootFS"] = {"Layers": found["Layers"]}
        return 0, json.dumps([document])
    if command[0] == "tag":
        key = find(images, command[1])
        if key is None:
            return 1, "Error: No such image: " + command[1]
        images[command[2]] = dict(images[key])
    elif command[:2] == ["image", "rm"]:
        if command[2] not in images:
            return missing
        del images[command[2]]
    elif command[0] == "build":
        labels = dict(a.split("=", 1) for a, p in zip(command[1:], command) if p == "--label")
        output = command[command.index("-t") + 1]
        recipe = Path(command[command.index("-f") + 1]).read_text(encoding="utf-8")
        parent = next(x.split()[1] for x in recipe.splitlines() if x.startswith("FROM "))
        store["counter"] += 1
        number = store["counter"]
        layers = images[parent]["Layers"] if parent in images else [f"sha256:layer-{number}"]
        images[output] = {"Id": f"sha256:image-{number}", "Labels": labels, "Layers": layers, "Parent": parent}
        edits, store["after_build"] = store["after_build"], {}
        for ref, fields in edits.items():
            images[output if ref == "<output>" else ref].update(fields)
    return 0, ""


if reply is None:
    reply = serve(store, command) if store is not None and command else (0, "")
    if store is not None:
        store_file.write_text(json.dumps(store), encoding="utf-8")
code, text = reply
if text:
    print(text, file=sys.stdout if code == 0 else sys.stderr)
if store is not None and first == store["remove_after"]:
    for name in ("docker", "docker.cmd"):
        (here / name).unlink(missing_ok=True)
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
    def images(self) -> dict[str, dict[str, Any]]:
        """The image store (reference -> ``Id``, ``Labels``, ``Layers``) of a store-backed fake."""
        store = (self.path.parent / "fake_docker_images.json").read_text(encoding="utf-8")
        return json.loads(store)["images"]

    @property
    def built(self) -> bool:
        return self.build_marker.exists()


def install_fake_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replies: Mapping[str, tuple[int, str]],
    *,
    images: Mapping[str, Mapping[str, Any]] | None = None,
    after_build: Mapping[str, Mapping[str, Any]] | None = None,
    remove_after: str = "",
) -> FakeDocker:
    """Put a ``docker`` on ``PATH`` answering with ``(exit code, text)``.

    A reply keyed ``"<first> <last>"`` argument (``"image sha256:aa"``) wins over one keyed
    by the first two arguments (``"image rm"``), and that over one keyed by the first
    argument. The text goes to stdout on exit code 0 and to stderr otherwise;
    a call without a reply exits 0 with no output. A ``build`` also touches ``build_marker``.

    ``images`` (reference -> ``{"Id", "Labels", "Layers"}``) makes the fake an image store
    that answers the calls no reply covers like Docker: ``image inspect``, ``build`` (stores
    ``-t`` with the ``--label`` pairs and the layers of the recipe's ``FROM`` image), ``tag``
    and ``image rm``. ``after_build`` (reference -> fields, ``"<output>"`` for the built
    image) edits images once after the next build; ``remove_after`` names a first argument
    after whose answer the program deletes itself, so later calls find no Docker CLI. A
    store-backed fake is alone on ``PATH``: no host ``docker`` can answer in its place, and
    installing again in the same folder starts a new call log and store. Installing it lets
    the test start ``docker``: see ``tests/docker_cli_guard_plugin.py``.
    """
    allow_docker_cli()
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "fake_docker_calls.jsonl").unlink(missing_ok=True)
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
    if images is None:
        monkeypatch.setenv("PATH", os.pathsep.join((str(tmp_path), os.environ["PATH"])))
    else:
        store = {
            "images": images,
            "after_build": after_build or {},
            "remove_after": remove_after,
            "counter": len(images),
        }
        (tmp_path / "fake_docker_images.json").write_bytes(json.dumps(store).encode("utf-8"))
        monkeypatch.setenv("PATH", str(tmp_path))
    return FakeDocker(docker.name, tmp_path / "build-started", docker, replies_path)
