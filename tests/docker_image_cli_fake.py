"""In-process fake of the Docker CLI's image, container and tag verbs.

Unlike ``FakeDocker``, a ``DockerPort`` stand-in, it answers the commands the real
adapter runs; its one patch, ``subprocess.run``, replaces an external boundary.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

import pytest

from tests.docker_cli_guard import route_docker_helper_through_subprocess_run

_REAL_RUN = subprocess.run
_Reply = subprocess.CompletedProcess[str] | BaseException


def image_row(repository: str, tag: str, image_id: str, eol: str = "\n") -> str:
    """One ``docker image ls --format '{{json .}}'`` row, escaped as Docker escapes it."""
    row = json.dumps({"ID": image_id, "Repository": repository, "Tag": tag}, separators=(",", ":"))
    return row.replace("<", "\\u003c").replace(">", "\\u003e") + eol


class DockerImageCli:
    """Daemon state served as ``docker`` commands, with per-command replies."""

    def __init__(self) -> None:
        #: Reference (or image ID, as Docker accepts) -> image ID.
        self.images: dict[str, str] = {}
        self.containers: dict[str, str] = {}
        #: Containers that are not running: ``container ls`` lists them only with ``--all``.
        self.stopped: set[str] = set()
        #: Extra ``(Repository, Tag, ID)`` rows: digest-only, dangling.
        self.untagged_rows: list[tuple[str, str, str]] = []
        #: Words every matching argv holds -> reply (an exception is raised).
        self.replies: dict[tuple[str, ...], _Reply] = {}
        self.calls: list[list[str]] = []
        self.eol = "\n"
        #: The size every listed image reports.
        self.size = "1GB"
        #: Called with the argv after each served command, to change daemon state mid-operation.
        self.after_call: Callable[[list[str]], None] | None = None

    def answer(
        self, *words: str, stdout="", stderr="", code=0, error: BaseException | None = None
    ):
        """Answer (or, with *error*, fail) every command whose argv holds all *words*."""
        self.replies[words] = error or subprocess.CompletedProcess([], code, stdout, stderr)

    def removals(self) -> list[list[str]]:
        return [call for call in self.calls if call[1:3] == ["image", "rm"]]

    def run(self, argv: list[str], *args: object, **kwargs: object):
        if argv[0] != "docker":
            return _REAL_RUN(argv, *args, **kwargs)  # type: ignore[call-overload]
        self.calls.append(list(argv))
        words = argv[1:]
        matching = [key for key in self.replies if set(key) <= set(words)]
        if matching:
            reply = self.replies[max(matching, key=len)]
            if isinstance(reply, BaseException):
                raise reply
            return subprocess.CompletedProcess(argv, reply.returncode, reply.stdout, reply.stderr)
        code, out, err = self._serve(words)
        if self.after_call is not None:
            self.after_call(list(argv))
        return subprocess.CompletedProcess(argv, code, out, err)

    def _all_ids(self) -> set[str]:
        return set(self.images.values()) | {row[2] for row in self.untagged_rows}

    def _id_of(self, reference: str) -> str | None:
        return self.images.get(reference) or (reference if reference in self._all_ids() else None)

    def _serve(self, words: list[str]) -> tuple[int, str, str]:  # noqa: PLR0911 -- one branch per verb
        verb = (
            " ".join(words[:2])
            if words[0] in ("image", "container", "volume", "network")
            else words[0]
        )
        if verb == "image inspect":
            return self._inspect(words)
        if verb == "image ls":
            return self._list(words)
        if verb == "container ls":
            if "--filter" in words:  # Project-labelled or volume-using containers: none
                return 0, "", ""
            listed = [c for c in self.containers if "--all" in words or c not in self.stopped]
            return 0, "".join(f"{container}{self.eol}" for container in listed), ""
        if verb == "container inspect":
            target = next(word for word in words[2:] if not word.startswith(("-", "{{")))
            if target not in self.containers:
                return 1, "", f"Error: No such container: {target}"
            return 0, self.containers[target] + "\n", ""
        if verb in ("tag", "image tag"):
            self.images[words[-1]] = self._id_of(words[-2]) or ""
            return 0, "", ""
        if verb in ("volume ls", "network ls"):  # no state volumes or Project networks
            return 0, "", ""
        if verb == "image rm":
            return self._remove(words[-1])
        raise AssertionError(f"unexpected docker call: docker {' '.join(words)}")

    def _inspect(self, words: list[str]) -> tuple[int, str, str]:
        reference = next(word for word in words[2:] if not word.startswith(("-", "{{")))
        if "<none>" in reference:
            return 1, "", f"Error response from daemon: invalid reference format: {reference}"
        image_id = self._id_of(reference)
        if image_id is None:
            return 1, "", f"Error: No such image: {reference}"
        if "{{len .RootFS.Layers}}" in words:  # purge orders tags by layer count
            return 0, "1\n", ""
        if "--format" in words or "-f" in words:  # ``-f`` is RepoDigests: tests answer it
            return 0, (image_id if "--format" in words else "[]") + "\n", ""
        return 0, json.dumps([{"Id": image_id}]), ""

    def _list(self, words: list[str]) -> tuple[int, str, str]:
        quiet, full = "--quiet" in words, "--no-trunc" in words
        template = words[words.index("--format") + 1] if "--format" in words else "{{json .}}"

        def shown(image_id: str) -> str:  # Docker shortens IDs unless asked not to
            return image_id if full else image_id.removeprefix("sha256:")[:12]

        if quiet:
            return 0, "".join(f"{shown(i)}{self.eol}" for i in sorted(self._all_ids())), ""
        rows = [
            (*reference.rpartition(":")[::2], shown(image_id))
            for reference, image_id in sorted(self.images.items())
            if not reference.startswith("sha256:")
        ] + [(*row[:2], shown(row[2])) for row in self.untagged_rows]
        if template == "{{json .}}":
            return 0, "".join(image_row(*row, self.eol) for row in rows), ""
        lines = [
            template.replace("{{.ID}}", image_id)
            .replace("{{.Repository}}", repository)
            .replace("{{.Tag}}", tag)
            .replace("{{.Size}}", self.size)
            for repository, tag, image_id in rows
        ]
        return 0, "".join(line + self.eol for line in lines), ""

    def _remove(self, reference: str) -> tuple[int, str, str]:
        image_id = self._id_of(reference)
        if image_id is None:
            return 1, "", f"Error: No such image: {reference}"
        if reference == image_id and image_id in self.containers.values():
            return 1, "", f"conflict: unable to delete {image_id} (used by a container)"
        if reference != image_id:
            self.images.pop(reference)
            return 0, "", ""
        if sum(v == image_id for n, v in self.images.items() if n != image_id) > 1:
            return 1, "", "must be forced: image is referenced in multiple repositories"
        self.images = {name: value for name, value in self.images.items() if value != image_id}
        self.untagged_rows = [row for row in self.untagged_rows if row[2] != image_id]
        return 0, "", ""


def serve_docker_image_cli(monkeypatch: pytest.MonkeyPatch) -> DockerImageCli:
    """Answer every ``docker`` command from a new :class:`DockerImageCli`."""
    cli = DockerImageCli()
    route_docker_helper_through_subprocess_run(monkeypatch)
    monkeypatch.setattr(subprocess, "run", cli.run)
    return cli
