"""In-process fake of the Docker CLI's image, container and tag verbs.

Unlike ``FakeDocker``, a ``DockerPort`` stand-in, it answers the commands the real
adapter runs; its one patch, ``subprocess.run``, replaces an external boundary.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.docker_cli_guard import route_docker_helper_through_subprocess_run

_REAL_RUN = subprocess.run
_Reply = subprocess.CompletedProcess[str] | BaseException


def image_row(repository: str, tag: str, image_id: str, eol: str = "\n") -> str:
    """One ``docker image ls --format '{{json .}}'`` row, escaped as Docker escapes it."""

    def escaped(text: str) -> str:
        return text.replace("<", "\\u003c").replace(">", "\\u003e")

    return (
        f'{{"ID":"{image_id}","Repository":"{escaped(repository)}","Tag":"{escaped(tag)}"}}{eol}'
    )


class DockerImageCli:
    """Daemon state served as ``docker`` commands, with per-command replies."""

    def __init__(self) -> None:
        #: Reference (or image ID, as Docker accepts) -> image ID.
        self.images: dict[str, str] = {}
        #: Container ID -> the ID of the image it runs.
        self.containers: dict[str, str] = {}
        #: Containers that are not running: ``container ls`` lists them only with ``--all``.
        self.stopped: set[str] = set()
        #: Extra ``(Repository, Tag, ID)`` rows: digest-only, dangling.
        self.untagged_rows: list[tuple[str, str, str]] = []
        #: Words every matching argv holds -> reply (an exception is raised).
        self.replies: dict[tuple[str, ...], _Reply] = {}
        self.calls: list[list[str]] = []
        self.eol = "\n"

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
        return subprocess.CompletedProcess(argv, code, out, err)

    def _all_ids(self) -> set[str]:
        return set(self.images.values()) | {row[2] for row in self.untagged_rows}

    def _id_of(self, reference: str) -> str | None:
        if reference in self.images:
            return self.images[reference]
        return reference if reference in self._all_ids() else None

    def _serve(self, words: list[str]) -> tuple[int, str, str]:
        verb = " ".join(words[:2]) if words[0] in ("image", "container") else words[0]
        if verb == "image inspect":
            return self._inspect(words)
        if verb == "image ls":
            return self._list(quiet="--quiet" in words)
        if verb == "container ls":
            listed = [c for c in self.containers if "--all" in words or c not in self.stopped]
            return 0, "".join(f"{container}{self.eol}" for container in listed), ""
        if verb == "container inspect":
            return 0, self.containers[words[-1]] + "\n", ""
        if verb == "tag":
            self.images[words[2]] = self._id_of(words[1]) or ""
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
        if "--format" in words:
            return 0, image_id + "\n", ""
        if "-f" in words:  # RepoDigests: the tests answer it explicitly
            return 0, "[]\n", ""
        return 0, json.dumps([{"Id": image_id}]), ""

    def _list(self, *, quiet: bool) -> tuple[int, str, str]:
        if quiet:
            return 0, "".join(f"{image_id}{self.eol}" for image_id in sorted(self._all_ids())), ""
        rows = [
            (*reference.rpartition(":")[::2], image_id)
            for reference, image_id in sorted(self.images.items())
            if not reference.startswith("sha256:")
        ] + self.untagged_rows
        return 0, "".join(image_row(*row, self.eol) for row in rows), ""

    def _remove(self, reference: str) -> tuple[int, str, str]:
        image_id = self._id_of(reference)
        if image_id is None:
            return 1, "", f"Error: No such image: {reference}"
        if reference == image_id and image_id in self.containers.values():
            return 1, "", f"conflict: unable to delete {image_id} (used by a container)"
        if reference != image_id:
            self.images.pop(reference)
            return 0, "", ""
        self.images = {name: value for name, value in self.images.items() if value != image_id}
        self.untagged_rows = [row for row in self.untagged_rows if row[2] != image_id]
        return 0, "", ""


def serve_docker_image_cli(monkeypatch: pytest.MonkeyPatch) -> DockerImageCli:
    """Answer every ``docker`` command from a new :class:`DockerImageCli`."""
    cli = DockerImageCli()
    route_docker_helper_through_subprocess_run(monkeypatch)
    monkeypatch.setattr(subprocess, "run", cli.run)
    return cli
