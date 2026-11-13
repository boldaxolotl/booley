"""Every timed, captured Docker CLI call in the host-side runtime is bounded.

``subprocess.run(..., timeout=...)`` kills only the direct child and then waits for the
output pipes with no limit (on Windows a surviving descendant holds them), so a hung
Docker CLI could hang Doctor, image upkeep and similar commands past their timeout.
``platform_paths.run_captured_bounded`` kills the whole tree and abandons held pipes.
These tests pin that each audited call site uses it, keeps its caller-visible results,
and that a hung probe really returns promptly with no descendant left running.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from tests.pipe_holding_tree import heartbeat_tree, parent_argv, wait_until_heartbeat_stops

from booley.runtime import (
    booley_dir_migration,
    docker_capacity,
    host_base_acquisition,
    image_lifecycle,
    inspection,
    project_image,
    session_issuance,
    vaporview,
)

_IMAGE_ID = "sha256:" + "a" * 64
_OUTCOMES = ("ok", "nonzero", "missing", "timeout")


@dataclass(frozen=True)
class _Site:
    """One audited call site and what its caller shows for each command outcome."""

    name: str
    module: ModuleType
    #: Leading argv words of the command under test.
    target: list[str]
    #: The timeout the site has always passed.
    timeout: float
    #: Calls the command under test (given the tmp dir) and returns what the caller shows.
    drive: Callable[[Path], Any]
    #: Stdout the command prints on success.
    ok_stdout: str
    #: Observation per outcome: ``rc=N``, ``returned:<value>`` or the raised type name.
    expected: dict[str, str]
    #: Stdout of earlier commands in the same call (the command under test is the last).
    prior_stdout: str = ""


def _same(shown: str) -> dict[str, str]:
    """Expected observations when failures all surface as one exception type."""
    return {"nonzero": shown, "missing": shown, "timeout": shown}


def _sites() -> list[_Site]:
    cli = image_lifecycle._DockerCli()
    return [
        _Site(
            "booley_dir_migration docker ps",
            booley_dir_migration,
            ["docker", "ps"],
            30,
            lambda _root: booley_dir_migration._running_sandboxes(),
            "",
            {"ok": "returned:[]"}
            | _same("BooleyLayoutError")
            | {"missing": "FileNotFoundError"}
            | {"timeout": "TimeoutExpired"},
        ),
        _Site(
            "booley_dir_migration docker inspect",
            booley_dir_migration,
            ["docker", "inspect"],
            30,
            lambda _root: booley_dir_migration._running_sandboxes(),
            "not json",
            {"ok": "BooleyLayoutError", "nonzero": "BooleyLayoutError"}
            | {"missing": "FileNotFoundError", "timeout": "TimeoutExpired"},
            prior_stdout="abc\n",
        ),
        _Site(
            "docker_capacity probe",
            docker_capacity,
            ["docker", "info"],
            15,
            lambda _root: docker_capacity._run_docker_probe(["docker", "info"]).returncode,
            "",
            {"ok": "returned:0", "nonzero": "returned:1"}
            | {"missing": "DockerCapacityError", "timeout": "DockerCapacityError"},
        ),
        _Site(
            "host_base_acquisition tag",
            host_base_acquisition,
            ["docker", "tag"],
            30,
            lambda _root: host_base_acquisition._tag("src", "dst"),
            "",
            {"ok": "returned:None"} | _same("RuntimeError"),
        ),
        _Site(
            "host_base_acquisition image rm",
            host_base_acquisition,
            ["docker", "image", "rm"],
            30,
            lambda _root: host_base_acquisition._cleanup_migration(("img",), None),
            "",
            {"ok": "returned:None"} | _same("RuntimeError"),
        ),
        _Site(
            "image_lifecycle repo_digests",
            image_lifecycle,
            ["docker", "image", "inspect"],
            15,
            lambda _root: cli.repo_digests("img"),
            "[]",
            {"ok": "returned:()"} | _same("ImageLifecycleError"),
        ),
        _Site(
            "image_lifecycle image_ids",
            image_lifecycle,
            ["docker", "image", "ls", "--all"],
            30,
            lambda _root: cli.image_ids(),
            "",
            {"ok": "returned:frozenset()"} | _same("ImageLifecycleError"),
        ),
        _Site(
            "image_lifecycle image_references",
            image_lifecycle,
            ["docker", "image", "ls", "--no-trunc"],
            30,
            lambda _root: cli.image_references(),
            "",
            {"ok": "returned:()"} | _same("ImageLifecycleError"),
        ),
        _Site(
            "image_lifecycle container ls",
            image_lifecycle,
            ["docker", "container", "ls"],
            30,
            lambda _root: cli.container_image_ids(),
            "",
            {"ok": "returned:frozenset()"} | _same("ImageLifecycleError"),
        ),
        _Site(
            "image_lifecycle container inspect",
            image_lifecycle,
            ["docker", "container", "inspect"],
            15,
            lambda _root: image_lifecycle._inspect_container_image_ids(["c1"]),
            _IMAGE_ID,
            {"ok": f"returned:frozenset({{'{_IMAGE_ID}'}})"} | _same("ImageLifecycleError"),
        ),
        _Site(
            "inspection list issued containers",
            inspection,
            ["docker", "ps", "-aq"],
            15,
            lambda _root: inspection._list_issued_containers("docker", None).returncode,  # type: ignore[arg-type]
            "",
            {"ok": "returned:0", "nonzero": "returned:1"}
            | {"missing": "FileNotFoundError", "timeout": "TimeoutExpired"},
        ),
        _Site(
            "project_image base_image_packages",
            project_image,
            ["docker", "run", "--rm", "--entrypoint", "python3"],
            7,
            lambda _root: project_image.base_image_packages("img", timeout_s=7),
            "Foo==1.0\n",
            {"ok": "returned:{'foo': '1.0'}"} | _same("returned:{}"),
        ),
        _Site(
            "project_image project_data_alias_capable",
            project_image,
            ["docker", "run", "--rm", "--pull=never", "--network=none", "--user=0:0"],
            30,
            lambda _root: project_image.project_data_alias_capable(_IMAGE_ID),
            "canonical-alias",
            {"ok": "returned:True"} | _same("DockerImageError"),
        ),
        _Site(
            "project_image inspect_layout_image",
            project_image,
            ["docker", "image", "inspect"],
            30,
            lambda _root: project_image.inspect_layout_image("img"),
            '[{"Id": "x"}]',
            {"ok": "returned:{'Id': 'x'}"} | _same("RuntimeError"),
        ),
        _Site(
            "project_image probe layout directory",
            project_image,
            ["docker", "run", "--rm", "--pull=never", "--network=none", "--entrypoint=sh"],
            30,
            lambda _root: project_image._probe_layout_directory(_IMAGE_ID),
            "",
            {"ok": "returned:None"} | _same("RuntimeError"),
        ),
        _Site(
            "project_image image history",
            project_image,
            ["docker", "history"],
            30,
            lambda _root: project_image._image_history(_IMAGE_ID, "docker"),
            "one\ntwo\n",
            {"ok": "returned:['one', 'two']"} | _same("RuntimeError"),
        ),
        _Site(
            "vaporview probe",
            vaporview,
            ["docker", "exec", "c", "python3", "-c"],
            20,
            lambda _root: _probe_returncode(),
            "",
            {"ok": "returned:0", "nonzero": "returned:1"}
            | {"missing": "returned:None", "timeout": "returned:None"},
        ),
    ]


def _probe_returncode() -> int | None:
    result = vaporview._run_probe(["docker", "exec", "c"], "pass")
    return None if result is None else result.returncode


def _observe(drive: Callable[[], Any]) -> str:
    """What the caller shows: ``returned:<value>`` or the raised exception's type name."""
    try:
        value = drive()
    except Exception as exc:  # noqa: BLE001 - the type name is the observation
        return type(exc).__name__
    return f"returned:{value!r}"


def _check_site(
    site: _Site, outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Drive *site* with the scripted *outcome*; return what differs from the contract."""
    problems: list[str] = []
    calls: list[tuple[list[str], float]] = []

    def bounded(
        argv: list[str], *, timeout: float, cwd: object = None, env: object = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), timeout))
        if list(argv[: len(site.target)]) != site.target:
            return subprocess.CompletedProcess(argv, 0, site.prior_stdout, "")
        if outcome == "missing":
            raise FileNotFoundError(argv[0])
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if outcome == "nonzero":
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        return subprocess.CompletedProcess(argv, 0, site.ok_stdout, "")

    def unbounded(*args: object, **kwargs: object) -> object:
        problems.append(f"{site.name}: unbounded subprocess.run used")
        raise AssertionError("unbounded subprocess.run used")

    with monkeypatch.context() as patch:
        patch.setattr(site.module, "run_captured_bounded", bounded, raising=False)
        patch.setattr(subprocess, "run", unbounded)
        patch.setattr(shutil, "which", lambda name: name)
        patch.setattr(session_issuance, "labels", lambda _issuance: ("booley.project-id=x",))
        project_image.project_data_alias_capable.cache_clear()
        shown = _observe(lambda: site.drive(tmp_path))
    if shown != site.expected[outcome]:
        problems.append(
            f"{site.name} [{outcome}]: shows {shown!r}, expected {site.expected[outcome]!r}"
        )
    made = [call for call in calls if call[0][: len(site.target)] == site.target]
    if len(made) != 1 or made[0][1] != site.timeout:
        problems.append(
            f"{site.name} [{outcome}]: bounded calls {calls!r}, expected one "
            f"{site.target!r} with timeout {site.timeout}"
        )
    return problems


def test_every_timed_docker_call_uses_the_bounded_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each audited call goes through the bounded helper and keeps its caller-visible results."""
    failures = [
        problem
        for site in _sites()
        for outcome in _OUTCOMES
        for problem in _check_site(site, outcome, tmp_path, monkeypatch)
    ]
    assert not failures, "\n".join(failures)


def test_hung_vaporview_probe_kills_the_tree_and_returns_promptly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe whose command hangs ends soon as ``None`` and leaves no descendant running."""
    monkeypatch.setattr(vaporview, "_PROBE_TIMEOUT_S", 2, raising=False)
    with heartbeat_tree(tmp_path) as paths:
        # The parent script ignores the trailing ``python3 -c pass`` the probe appends.
        prefix = parent_argv(tmp_path, sys.executable, paths, "live")
        start = time.monotonic()
        result = vaporview._run_probe(prefix, "pass")
        elapsed = time.monotonic() - start
        assert result is None
        assert elapsed < 25, f"probe hung {elapsed:.1f}s on a pipe held by a grandchild"
        assert paths["heartbeat"].exists(), "grandchild never started"
        assert wait_until_heartbeat_stops(paths["heartbeat"]), (
            "grandchild still running after the timeout"
        )
