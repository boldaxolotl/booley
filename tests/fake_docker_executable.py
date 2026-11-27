"""The shared fake of the Docker CLI boundary: a ``docker`` program on ``PATH``.

Its per-test state holds images, tags,
containers, networks and volumes. This module is also that program, run as
``python -I <module> <state folder> <docker arguments>``.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the program imports the standard library only
    import pytest

_State = dict[str, Any]
_Result = tuple[int, str, str]
_which = shutil.which  # tests patch ``shutil.which``; the Windows launcher lookup must not
_LABEL_FIELD = re.compile(r'\{\{\.Label "([^"]+)"\}\}')
_LABEL_OF_IMAGE = re.compile(r'\{\{ ?index \.Config\.Labels "([^"]+)" ?\}\}')


def _record(value: Any) -> _State:
    return {"Image": value} if isinstance(value, str) else value


def _network(value: Any) -> _State:
    """A network is its labels, or a record with ``Labels``, ``Internal`` and ``Options``."""
    return value if {"Labels", "Internal", "Options"} & set(value) else {"Labels": value}


def image_row(repository: str, tag: str, image_id: str, eol: str = "\n") -> str:
    """One ``docker image ls --format '{{json .}}'`` row, escaped as Docker escapes it."""
    row = json.dumps({"ID": image_id, "Repository": repository, "Tag": tag}, separators=(",", ":"))
    return row.replace("<", "\\u003c").replace(">", "\\u003e") + eol


def _id(value: Any) -> str:
    return value if isinstance(value, str) else value["Id"]


def _find(images: _State, reference: str) -> str | None:
    if reference in images:
        return reference
    return next((key for key, value in images.items() if _id(value) == reference), None)


def _layers(value: Any) -> list[str]:
    return [value] if isinstance(value, str) else value.get("Layers", [value["Id"]])


def _container(containers: _State, reference: str) -> str | None:
    """A container's name, given its name or ``Id``."""
    if reference in containers:
        return reference
    return next((n for n, v in containers.items() if _record(v).get("Id") == reference), None)


def _container_document(name: str, record: _State) -> _State:
    """``container inspect``; an ``Inspect`` document overrides what the record derives."""
    inspect = record.get("Inspect", {})
    running = {"Running": record.get("Running", True)}
    return {
        "Id": record.get("Id", name),
        "Name": f"/{name}",
        "Image": record["Image"],
        "Config": {"Labels": record.get("Labels", {})},
        "Mounts": record.get("Mounts", []),
        **inspect,
        "State": {**inspect.get("State", {}), **running},
        "NetworkSettings": {"Networks": {net: {} for net in record.get("Networks", [])}},
    }


def _container_answers(name: str, value: Any, _template: str | None) -> dict[str | None, str]:
    record = _record(value)
    document = json.dumps(_container_document(name, record))
    return {"{{.Image}}": record["Image"], "{{json .}}": document}


def _image_answers(_key: str, value: Any, template: str | None) -> dict[str | None, str]:
    record = {} if isinstance(value, str) else value
    layers = _layers(value)
    config = {"Labels": record.get("Labels", {}), "Env": record.get("Env", [])}
    document = {"Id": _id(value), "Config": config, "RootFS": {"Layers": layers}}
    answers: dict[str | None, str] = {
        None: json.dumps([document]),
        "{{json .}}": json.dumps(document),
        "{{.Id}}": _id(value),
        "{{len .RootFS.Layers}}": str(len(layers)),
        "{{json .RepoDigests}}": "[]",
    }
    label = _LABEL_OF_IMAGE.fullmatch(template or "")
    if label:
        answers[template] = config["Labels"].get(label.group(1), "<no value>")
    return answers


def _inspect(state: _State, args: list[str]) -> _Result:
    template = None
    for flag in ("--format", "-f"):
        if flag in args:
            at = args.index(flag)
            template = args[at + 1]
            del args[at : at + 2]
    reference = next(word for word in args[2:] if not word.startswith("-"))
    if "<none>" in reference:
        return 1, "", f"Error response from daemon: invalid reference format: {reference}\n"
    containers = args[0] == "container"
    values = state["containers" if containers else "images"]
    key = (_container if containers else _find)(values, reference)
    if key is None:
        return 1, "", f"Error: No such {'container' if containers else 'image'}: {reference}\n"
    answers = (_container_answers if containers else _image_answers)(key, values[key], template)
    if template in answers:
        return 0, answers[template] + "\n", ""
    return 2, "", f"fake docker: unsupported {args[0]} inspect template: {template}\n"


def _list_images(state: _State, args: list[str]) -> _Result:
    images, eol = state["images"], state["eol"]
    ids = [_id(value) for value in images.values()]

    def shown(image_id: str) -> str:  # Docker shortens IDs unless asked not to
        return image_id if "--no-trunc" in args else image_id.removeprefix("sha256:")[:12]

    if "--quiet" in args:
        return 0, "".join(f"{shown(image_id)}{eol}" for image_id in sorted(set(ids))), ""
    template = args[args.index("--format") + 1] if "--format" in args else "{{json .}}"
    out = ""
    for reference, value in sorted(images.items()):
        if reference == _id(value):  # an image no tag names
            if ids.count(reference) > 1:
                continue
            repository, tag = "<none>", "<none>"
        else:
            repository, tag = (reference if ":" in reference else f"{reference}:latest").rsplit(
                ":", 1
            )
        if template == "{{json .}}":
            out += image_row(repository, tag, shown(_id(value)), eol)
            continue
        size = "1GB" if isinstance(value, str) else value.get("Size", "1GB")
        fields = {"ID": shown(_id(value)), "Repository": repository, "Tag": tag, "Size": size}
        line = template
        for name, text in fields.items():
            line = line.replace("{{." + name + "}}", text)
        out += line + eol
    return 0, out, ""


def _matching(state: _State, kind: str, args: list[str]) -> list[tuple[str, _State]] | _Result:
    """The ``kind`` records the ``--filter`` words select, or the failure answer."""
    filters = [args[at + 1] for at, word in enumerate(args) if word == "--filter"]
    found = []
    for name, value in state[kind + "s"].items():
        if kind == "container":
            record = _record(value)
            if not {"-a", "--all"} & set(args) and not record.get("Running", True):
                continue
        else:
            record = _network(value) if kind == "network" else {"Labels": value}
        matches = True
        for text in filters:
            key, _, wanted = text.partition("=")
            if key == "label":
                label, equals, expected = wanted.partition("=")
                labels = record.get("Labels", {})
                matches &= label in labels and (not equals or labels[label] == expected)
            elif key == "volume":
                matches &= wanted in record.get("Volumes", ())
            else:
                return 2, "", f"fake docker: unsupported filter: {text}\n"
        if matches:
            found.append((name, record))
    return found


def _list_named(state: _State, args: list[str]) -> _Result:
    """``container ls``, ``volume ls`` and ``network ls`` with label and volume filters."""
    found = _matching(state, args[0], args)
    if isinstance(found, tuple):
        return found
    return 0, "".join(name + state["eol"] for name, _ in found), ""


def _list_processes(state: _State, args: list[str]) -> _Result:
    """``ps``: the ``{{.ID}}``, ``{{.Names}}``, ``{{.State}}`` and ``{{.Label}}`` fields."""
    found = _matching(state, "container", args)
    if isinstance(found, tuple):
        return found
    out = ""
    for name, record in sorted(found):
        status = record.get("State") or ("running" if record.get("Running", True) else "exited")
        line = args[args.index("--format") + 1]
        for field, text in (("ID", record.get("Id", name)), ("Names", name), ("State", status)):
            line = line.replace("{{." + field + "}}", text)
        for label in _LABEL_FIELD.findall(line):
            line = line.replace(
                f'{{{{.Label "{label}"}}}}', record.get("Labels", {}).get(label, "")
            )
        out += line + state["eol"]
    return 0, out, ""


def _pairs(args: list[str], flag: str) -> dict[str, str]:
    """The ``key=value`` words that follow each *flag*."""
    return dict(a.split("=", 1) for p, a in itertools.pairwise(args) if p == flag)


def _run(state: _State, args: list[str]) -> _Result:
    key = _find(state["images"], args[-1])
    if key is None:
        return 1, "", f"Unable to find image {args[-1]!r} locally\n"
    networks = [args[at + 1] for at, word in enumerate(args) if word == "--network"]
    record = {"Image": _id(state["images"][key]), "Labels": _pairs(args, "--label")}
    state["containers"][args[args.index("--name") + 1]] = record | {"Networks": networks}
    return 0, "", ""


def _change_container(state: _State, args: list[str]) -> _Result:
    """``rm [-f]``, ``start`` and ``stop`` of one container."""
    name = _container(state["containers"], args[-1])
    if name is None:
        return 1, "", f"Error: No such container: {args[-1]}\n"
    if args[0] == "rm":
        del state["containers"][name]
    else:
        record = _record(state["containers"][name]) | {"Running": args[0] == "start"}
        state["containers"][name] = record
    return 0, args[-1] + "\n", ""


def _network_command(state: _State, args: list[str]) -> _Result:
    """``network create``, ``rm``, ``connect`` and ``inspect``."""
    verb, networks = args[1], state["networks"]
    name = args[-1]
    if verb == "create":
        record = {"Internal": "--internal" in args, "Options": _pairs(args, "--opt")}
        networks[name] = record | {"Labels": _pairs(args, "--label")}
        return 0, name + "\n", ""
    if verb == "connect":
        name = args[-2]
    elif verb == "inspect":
        name = next(word for word in args[2:] if not word.startswith("-"))
    if name not in networks:
        return 1, "", f"Error response from daemon: network {name} not found\n"
    if verb == "rm":
        del networks[name]
    elif verb == "connect":
        container = _record(state["containers"][args[-1]])
        container["Networks"] = [*container.get("Networks", []), name]
        state["containers"][args[-1]] = container
    else:
        record = _network(networks[name])
        attached = {
            f"id-{n}": {"Name": n}
            for n, value in state["containers"].items()
            if _record(value).get("Running", True) and name in _record(value).get("Networks", [])
        }
        document = {"Internal": record.get("Internal", False), "Containers": attached}
        document["Options"] = record.get("Options", {})
        return 0, json.dumps(document) + "\n", ""
    return 0, "", ""


def _pull(state: _State, args: list[str]) -> _Result:
    if args[-1] not in state["images"]:
        state["counter"] += 1
        state["images"][args[-1]] = f"sha256:image-{state['counter']}"
    return 0, "", ""


def _tag(state: _State, args: list[str]) -> _Result:
    source, target = args[-2:]
    key = _find(state["images"], source)
    if key is None:
        return 1, "", f"Error: No such image: {source}\n"
    state["images"][target] = state["images"][key]
    return 0, "", ""


def _remove(state: _State, args: list[str]) -> _Result:
    images, reference = state["images"], args[-1]
    key = _find(images, reference)
    if key is None:
        return 1, "", f"Error: No such image: {reference}\n"
    if key == reference and reference != _id(images[key]):  # a tag: untag only
        del images[reference]
        return 0, "", ""
    used = [c if isinstance(c, str) else c["Image"] for c in state["containers"].values()]
    if reference in used:
        return 1, "", f"conflict: unable to delete {reference} (used by a container)\n"
    if sum(_id(value) == reference for name, value in images.items() if name != reference) > 1:
        return 1, "", "must be forced: image is referenced in multiple repositories\n"
    state["images"] = {name: v for name, v in images.items() if _id(v) != reference}
    return 0, "", ""


def _build(state: _State, args: list[str]) -> _Result:
    images = state["images"]
    labels = _pairs(args, "--label")
    output = args[args.index("-t") + 1]
    recipe = Path(args[args.index("-f") + 1]).read_text(encoding="utf-8")
    parent = next(line.split()[1] for line in recipe.splitlines() if line.startswith("FROM "))
    state["counter"] += 1
    number = state["counter"]
    layers = _layers(images[parent]) if parent in images else [f"sha256:layer-{number}"]
    built = {"Id": f"sha256:image-{number}", "Labels": labels, "Layers": layers, "Parent": parent}
    images[output] = built
    edits, state["after_build"] = state["after_build"], {}
    for reference, fields in edits.items():
        images[output if reference == "<output>" else reference].update(fields)
    return 0, "", ""


def _remove_volume(state: _State, args: list[str]) -> _Result:
    name = args[-1]
    if name not in state["volumes"]:
        return 1, "", f"Error response from daemon: get {name}: no such volume\n"
    records = [c for c in state["containers"].values() if not isinstance(c, str)]
    if any(name in record.get("Volumes", ()) for record in records):
        return 1, "", f"Error response from daemon: remove {name}: volume is in use\n"
    del state["volumes"][name]
    return 0, name + state["eol"], ""


def _disk_usage(state: _State, args: list[str]) -> _Result:
    """``system df --verbose --format '{{json .}}'``, as far as the volume sizes."""
    volumes = [{"Name": name, "Size": "1GB"} for name in state["volumes"]]
    return 0, json.dumps({"Volumes": volumes}) + "\n", ""


_VERBS = {
    ("image", "inspect"): _inspect,
    ("container", "inspect"): _inspect,
    ("image", "ls"): _list_images,
    ("container", "ls"): _list_named,
    ("volume", "ls"): _list_named,
    ("network", "ls"): _list_named,
    ("image", "tag"): _tag,
    ("image", "rm"): _remove,
    ("volume", "rm"): _remove_volume,
    ("system", "df"): _disk_usage,
    ("network", "create"): _network_command,
    ("network", "rm"): _network_command,
    ("network", "connect"): _network_command,
    ("network", "inspect"): _network_command,
    ("tag",): _tag,
    ("build",): _build,
    ("pull",): _pull,
    ("ps",): _list_processes,
    ("run",): _run,
    ("rm",): _change_container,
    ("start",): _change_container,
    ("stop",): _change_container,
}


def _answer(here: Path, state: _State, command: list[str]) -> _Result:
    replies = json.loads((here / "fake_docker_replies.json").read_text(encoding="utf-8"))
    matching = [key for key in replies if set(key.split()) <= set(command)]
    if matching:  # the most words win, then the later rule
        code, *texts = replies[max(reversed(matching), key=lambda key: len(key.split()))]
        if len(texts) == 2:
            return code, texts[0], texts[1]
        text = texts[0] + "\n" if texts[0] else ""
        return (code, text, "") if code == 0 else (code, "", text)
    if not state["serve"]:
        return 0, "", ""
    serve = _VERBS.get(tuple(command[:2])) or _VERBS.get(tuple(command[:1]))
    if serve:
        return serve(state, list(command))
    return 2, "", f"fake docker: unsupported command: {' '.join(command)}\n"


def _wait_for(marker: Path, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not marker.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"no {marker.name} marker within {seconds} s")
        time.sleep(0.01)


def _serve_call(here: Path, command: list[str]) -> int:
    with (here / "fake_docker_calls.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(command) + "\n")
    if command[:1] == ["build"]:
        (here / "build-started").touch()
    state_path = here / "fake_docker_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    code, out, err = _answer(here, state, command)
    pause = bool(state["pause"]) and set(state["pause"]) <= set(command)
    if pause:
        state["pause"] = []
    state_path.write_bytes(json.dumps(state).encode("utf-8"))
    # bytes: Docker's line ends on Windows too, and a reply may hold a byte that is not UTF-8
    sys.stdout.buffer.write(out.encode("utf-8", "surrogateescape"))
    sys.stderr.buffer.write(err.encode("utf-8", "surrogateescape"))
    if state["remove_after"] and set(state["remove_after"].split()) <= set(command):
        for name in ("docker", "docker.cmd"):
            (here / name).unlink(missing_ok=True)
    if pause:
        (here / "paused").touch()
        _wait_for(here / "resumed", state["deadline"])
    return code


class _Section(MutableMapping[str, Any]):
    """A JSON file, or one section of it; set and delete persist, nested edits do not."""

    def __init__(self, path: Path, section: str | None = None) -> None:
        self.path, self.section = path, section

    def _read(self) -> _State:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data if self.section is None else data[self.section]

    def replace(self, value: Any) -> None:
        if self.section is not None:
            value = json.loads(self.path.read_text(encoding="utf-8")) | {self.section: value}
        self.path.write_bytes(json.dumps(value).encode("utf-8"))

    def __getitem__(self, key: str) -> Any:
        return self._read()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.replace(self._read() | {key: value})

    def __delitem__(self, key: str) -> None:
        data = self._read()
        del data[key]
        self.replace(data)

    def __iter__(self) -> Iterator[str]:
        return iter(self._read())

    def __len__(self) -> int:
        return len(self._read())


def _state_section(name: str) -> property:
    def read(self: FakeDocker) -> _Section:
        return _Section(self.state_path, name)

    def assign(self: FakeDocker, value: Mapping[str, Any]) -> None:
        read(self).replace(dict(value))

    return property(read, assign)


def _setting(name: str) -> property:
    def read(self: FakeDocker) -> Any:
        return _Section(self.state_path)[name]

    def assign(self: FakeDocker, value: Any) -> None:
        _Section(self.state_path)[name] = value

    return property(read, assign)


def _program(folder: Path) -> list[str]:
    return [sys.executable, "-I", __file__, str(folder)]


class FakeDocker:
    """The installed fake: its program name and path, state, reply rules and call log."""

    images = _state_section("images")  # reference or ID -> ID, or an inspect record
    # name -> image ID, or Image/Running/Labels/Volumes/Networks/State/Id/Mounts/Inspect
    containers = _state_section("containers")
    networks = _state_section("networks")  # name -> labels, or Labels/Internal/Options
    volumes = _state_section("volumes")  # name -> labels
    eol = _setting("eol")  # the line end of every listing
    after_build = _setting("after_build")  # edits applied once after the next build
    remove_after = _setting("remove_after")  # words of the last call before the CLI is gone

    def __init__(self, path: Path) -> None:
        self.path, self.name = path, path.name
        self.build_marker = path.parent / "build-started"
        self.replies_path = path.parent / "fake_docker_replies.json"  # reread on every call
        self.state_path = path.parent / "fake_docker_state.json"
        self.replies = _Section(self.replies_path)

    @property
    def calls(self) -> list[list[str]]:
        """Every argument list the program was called with, in order."""
        log = (self.path.parent / "fake_docker_calls.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in log.splitlines()]

    @property
    def built(self) -> bool:
        return self.build_marker.exists()

    def forget_calls(self) -> None:
        (self.path.parent / "fake_docker_calls.jsonl").write_bytes(b"")

    def removals(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["image", "rm"]]

    def answer(self, *words: str, code: int = 0, stdout: str = "", stderr: str = "") -> None:
        """Answer every call whose arguments hold all *words* instead of serving the state."""
        self.replies[" ".join(words)] = [code, stdout, stderr]

    @contextmanager
    def absent(self) -> Iterator[None]:
        """No Docker CLI is installed for the body."""
        hidden = self.path.rename(self.path.with_name("hidden-docker"))
        try:
            yield
        finally:
            hidden.rename(self.path)

    def pause_after(self, *words: str) -> None:
        """Hold the next call holding *words*, once answered, until :meth:`resume`."""
        from tests.time_budget import hang_guard_s

        for marker in ("paused", "resumed"):
            (self.path.parent / marker).unlink(missing_ok=True)
        self._deadline = hang_guard_s()
        _Section(self.state_path).update(pause=list(words), deadline=self._deadline)

    def wait_until_paused(self) -> None:
        # Not the state file: the paused operation's calls may be rewriting it.
        _wait_for(self.path.parent / "paused", self._deadline)

    def resume(self) -> None:
        (self.path.parent / "resumed").touch()

    def runner(self, args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        """The injected-runner front end, for production code that takes a Docker runner."""
        if not self.path.exists():
            raise FileNotFoundError(2, "No such file or directory", "docker")
        argv = [*_program(self.path.parent), *args]
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _find_launcher_as_posix_does(monkeypatch: pytest.MonkeyPatch, launcher: Path) -> None:
    """Let a bare ``docker`` reach *launcher*: Windows process creation finds only ``.exe``."""
    real = subprocess.Popen

    class Popen(real):
        def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
            if isinstance(args, list) and args[:1] == ["docker"] and not kwargs.get("executable"):
                env = kwargs.get("env")
                path = (os.environ if env is None else env).get("PATH", "")
                found = _which("docker", path=path)
                if found is None:
                    raise FileNotFoundError(2, "No such file or directory", "docker")
                if os.path.normcase(found) == os.path.normcase(launcher):
                    args = [*_program(launcher.parent), *args[1:]]
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", Popen)


def install_fake_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replies: Mapping[str, tuple[int, str]],
    *,
    images: Mapping[str, Any] | None = None,
    after_build: Mapping[str, Mapping[str, Any]] | None = None,
    remove_after: str = "",
) -> FakeDocker:
    """Put a ``docker`` on ``PATH`` answering with reply rules and, optionally, state.

    A reply keyed by words (``"image rm"``; ``""`` for every call) answers each call whose
    arguments hold them all with ``(exit code, text)``: stdout on code 0, else stderr. Most
    words win. Without ``images``, a call no reply covers exits 0 with no output.

    ``images`` (reference or ID -> image ID, or ``{"Id", "Labels", "Layers", "Env",
    "Size"}``) makes the fake store-backed: calls no reply covers are served from the
    state like Docker serves them, and a command the state does not model exits 2.
    The state serves image ``inspect``/``ls``/``tag``/``rm``/``pull``, ``build``, ``ps``,
    container ``inspect``/``ls``/``run``/``rm``/``start``/``stop``, network ``create``/``rm``/
    ``connect``/``inspect``/``ls``, volume ``ls``/``rm`` and ``system df``.
    ``after_build`` (reference -> fields, ``"<output>"``
    for the built image) edits images once after the next build; after a call holding the
    ``remove_after`` words the program deletes itself, so later calls find no Docker CLI.
    A store-backed fake is alone on ``PATH``; installing again in the same folder starts a
    new call log and state.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "fake_docker_calls.jsonl").write_bytes(b"")
    if os.name == "nt":
        docker = tmp_path / "docker.cmd"
        quoted = " ".join(f'"{word}"' for word in _program(tmp_path))
        docker.write_bytes(f"@{quoted} %*\r\n".encode())
        _find_launcher_as_posix_does(monkeypatch, docker)
    else:
        docker = tmp_path / "docker"
        docker.write_bytes(f'#!/bin/sh\nexec {shlex.join(_program(tmp_path))} "$@"\n'.encode())
        docker.chmod(0o755)
    fake = FakeDocker(docker)
    fake.replies.replace(dict(replies))
    _Section(fake.state_path).replace(
        {
            "serve": images is not None,
            "images": dict(images or {}),
            "containers": {},
            "networks": {},
            "volumes": {},
            "eol": "\n",
            "counter": len(images or {}),
            "after_build": after_build or {},
            "remove_after": remove_after,
            "pause": [],
        }
    )
    inherited = [] if images is not None else [os.environ["PATH"]]
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path), *inherited]))
    return fake


if __name__ == "__main__":
    raise SystemExit(_serve_call(Path(sys.argv[1]), sys.argv[2:]))
