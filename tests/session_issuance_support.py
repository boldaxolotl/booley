"""Test-only helpers that issue Runtime specs through ``preview``/``issue_prepared``."""

from __future__ import annotations

import copy
import os
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from booley.runtime import auth_token, host_docker, project_image, session_runtime
from booley.runtime import devcontainer as dc
from booley.runtime import session_issuance as runtime_spec
from tests.devcontainer_spec_support import build_spec


def _docker_without_images(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Docker CLI stand-in on which no image (so no keeper) exists."""
    return subprocess.CompletedProcess(args, 1, "", "Error: No such image")


def install_trusted_validator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a host-owned validator without relying on the runner's install layout."""
    prefix = tmp_path / "trusted-prefix"
    executable = prefix / "bin" / ("booley.exe" if os.name == "nt" else "booley")
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    # private: the trusted install prefixes are host locations no test may write.
    monkeypatch.setattr(
        runtime_spec,
        "_validator_prefix_anchors",
        lambda: {executable.parent.resolve(): prefix.resolve()},
    )
    return executable


def stub_keeper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the Docker keeper-tag calls that `issue_prepared` makes around persistence.

    `_persist_prepared` snapshots the keeper before writing and restores it when a
    guard refuses, so without these stubs a refusal is replaced by a Docker error.
    Patching the test's own ``monkeypatch`` lets a test override a stub later.
    """
    # private: the Docker CLI seam of `host_docker`, the shared fake of that boundary.
    monkeypatch.setattr("booley.runtime.host_docker._run_docker", _docker_without_images)
    monkeypatch.setattr("booley.runtime.host_docker.tag_image", lambda *_args: None)
    monkeypatch.setattr("booley.runtime.host_docker.remove_image_tag", lambda _image: None)


def plain_builder(
    project: Path, app: str = dc.APP_NONE, **spec_options: Any
) -> runtime_spec.SpecBuilder:
    """Return a builder for the unlicensed Sandbox spec most tests issue."""

    def build(_inputs: runtime_spec.SessionSpecInputs) -> dict[str, Any]:
        return build_spec(
            app,
            mcp_start_command=dc.mcp_post_start_command(),
            protected_devcontainer_source=str(project / ".devcontainer"),
            **spec_options,
        )

    return build


def fixed(spec: dict[str, Any]) -> runtime_spec.SpecBuilder:
    """Return a builder that hands out a deep copy of an already built spec."""
    return lambda _inputs: copy.deepcopy(spec)


def issue_built(
    project: Path,
    build: runtime_spec.SpecBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[runtime_spec.PreparedSessionSpec, Path, runtime_spec.Issuance]:
    """Preview *build* and issue it, as production does; return prepared, path and stamp."""
    stub_keeper(monkeypatch)
    prepared = runtime_spec.preview(project, build)
    stamp = runtime_spec.issue_prepared(project, prepared)
    return prepared, dc.devcontainer_path(project), stamp


def damaged(
    prepared: runtime_spec.PreparedSessionSpec,
    mutate: Callable[[dict[str, Any]], None],
    **prospective_overrides: Any,
) -> runtime_spec.PreparedSessionSpec:
    """Return *prepared* with its spec tampered with after pinning.

    The digest, the prospective issuance and its run-argument labels follow the damaged
    spec, so revalidation accepts it and the guard under test is the one that fires at
    issue time. Digest and label damage cannot be injected this way, and a mutation that
    changes the project-data layout must pass the matching ``project_data_layout``
    override. The label rebuild mirrors `_seal_with_requirements`.
    """
    spec = copy.deepcopy(prepared.spec)
    mutate(spec)
    # private: the seal a tampered spec must carry; `preview` would refuse the tampering.
    digest = runtime_spec._spec_digest(spec)
    prospective = replace(
        prepared.prospective_issuance, spec_sha256=digest, **prospective_overrides
    )
    assert runtime_spec.spec_project_data_layout(spec) == prospective.project_data_layout, (
        "pass the new project_data_layout: revalidation would refuse it before the guard under test"
    )
    spec["runArgs"] = runtime_spec.without_issuance_labels(spec)["runArgs"]
    for label in runtime_spec.labels(prospective):
        spec["runArgs"] += ["--label", label]
    return replace(prepared, spec=spec, digest=digest, prospective_issuance=prospective)


SANDBOX_IMAGE_ID = "sha256:" + "a" * 64


class HostDocker:
    """The Docker CLI of a host that holds the Sandbox Image and no container or network.

    ``image`` is the ID every ``image inspect`` reports (``None``: no such image);
    ``containers`` maps a container-name prefix to its ``inspect`` document and
    ``networks`` holds network-name prefixes that exist; ``calls`` lists every argument
    list, without the leading ``docker``. A container listing (``ps``) is empty.
    """

    def __init__(self) -> None:
        self.image: str | None = SANDBOX_IMAGE_ID
        self.containers: dict[str, str] = {}
        self.networks: set[str] = set()
        self.calls: list[list[str]] = []

    def answer(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        verb = " ".join(args[:2])
        if verb == "image inspect":
            found, out = self.image is not None, self.image or ""
        elif args[0] == "run":  # the Sandbox Image's Booley Directory layout probe
            found, out = True, "directory"
        elif verb == "container inspect":
            docs = [doc for name, doc in self.containers.items() if args[2].startswith(name)]
            found, out = bool(docs), docs[0] if docs else ""
        elif verb == "network inspect":
            found, out = any(args[2].startswith(name) for name in self.networks), "[]"
        elif args[0] == "ps" or verb in ("image tag", "image rm"):
            found, out = True, ""
        else:
            return _answer(args, 1, "", "unexpected Docker call")
        if found:
            return _answer(args, 0, out)
        return _answer(args, 1, "", f"Error: No such {args[0]}: {args[2]}")

    @property
    def mutations(self) -> list[list[str]]:
        """Calls that would change Docker state: tags, removals, stops and new containers."""
        changing = {"tag", "rm", "stop", "kill", "create", "start", "connect"}
        return [
            args
            for args in self.calls
            if changing & set(args[:2]) or (args[0] == "run" and "--rm" not in args)
        ]


def _answer(
    args: list[str], code: int, out: str, err: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker", *args], code, out, err)


def serve_host_docker(home: Path, monkeypatch: pytest.MonkeyPatch) -> HostDocker:
    """Run Sandbox Issuance on this host: Docker answered in process, ``booley`` installed.

    The shared fake of the Docker CLI boundary for Sandbox Issuance and ``booley init``'s
    Sandbox step: it replaces the three runners those Docker calls go through. A
    ``docker`` program on ``PATH`` cannot stand in on Windows, where the runners cannot
    start a ``.cmd``. The trusted ``booley`` launcher goes in *home*'s ``.local/bin``,
    first on ``PATH``.
    """
    docker = HostDocker()
    # Issuance stamps live in a private store: the host's config folder is private.
    auth_token.config_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    auth_token.config_dir().chmod(0o700)
    auth_token.config_dir().parent.chmod(0o700)
    launcher = home / ".local" / "bin" / ("booley.exe" if os.name == "nt" else "booley")
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(launcher.parent), os.environ["PATH"])))
    # private: `host_docker`'s Docker CLI seam.
    monkeypatch.setattr(host_docker, "_run_docker", lambda args, **_kw: docker.answer(args))
    # private: the Docker runner of `session_runtime`, which `_run_docker` does not serve.
    monkeypatch.setattr(session_runtime, "_run", lambda argv, **_kw: docker.answer(argv[1:]))
    monkeypatch.setattr(
        project_image, "run_captured_bounded", lambda argv, **_kw: docker.answer(argv[1:])
    )
    return docker
