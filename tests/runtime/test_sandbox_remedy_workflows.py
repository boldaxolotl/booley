"""Sandbox remedies name the documented apply workflow for the Sandbox's owner (#4182)."""

from __future__ import annotations

import json
import ntpath
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.sandbox_venue_fake import place
from tests.time_budget import scaled

from booley.core.booley_dir_pin import BooleyDirPinError
from booley.core.project_identity import project_label_id
from booley.runtime import (
    image_lifecycle,
    sandbox_apply,
    session_admission,
    session_issuance,
    session_runtime,
)
from booley.runtime.project_image import project_image_name
from booley.runtime.runtime_context import RuntimeLocation

VSCODE_INIT = "on the host run `booley init`, then use Dev Containers: Rebuild Container"
VSCODE_SEED = "on the host run `booley init --seed`, then use Dev Containers: Rebuild Container"
HEADLESS_REFRESH = "run `booley session refresh` on the host"
HEADLESS_RESEED = (
    "run `booley session down`, `booley init --seed`, and `booley session up` on the host"
)
EXTERNAL = "rebuild/pull the selected external image, then "
DOC = "https://booley.dev/docs/sandbox/#applying-sandbox-changes"
UNKNOWN_MANAGED = (
    f"in VS Code, {VSCODE_INIT}; headless, {HEADLESS_REFRESH}; "
    f"with your own image, follow the steps in {DOC}"
)
UNKNOWN_EXTERNAL = f"in VS Code, {VSCODE_SEED}; headless, {HEADLESS_RESEED}"

_SPELLED = ("session refresh", "session down", "session up", "--force", "stop")


def _write_image(root: Path, image: str | None) -> None:
    booley = root / ".booley"
    booley.mkdir(exist_ok=True)
    text = f'[sandbox]\nimage = "{image}"\n' if image else ""
    (booley / "booley.toml").write_bytes(text.encode())


def _inspect(root: Path, *, vscode: bool, running: bool) -> str:
    labels = {"booley.project-id": project_label_id(str(root.resolve()))}
    if vscode:
        labels["devcontainer.local_folder"] = str(root)
    state = {"Id": "abc", "State": {"Running": running}, "Config": {"Labels": labels}}
    return json.dumps([state])


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Host-side remedy with Docker faked at the two inventory boundaries."""
    root = tmp_path / "proj"
    root.mkdir()
    fake = SimpleNamespace(root=root, running=None, stopped=[])
    place(monkeypatch, RuntimeLocation.HOST)
    monkeypatch.setattr(session_issuance, "load_issued_snapshot", lambda _root: object())
    monkeypatch.setattr(session_runtime, "resolve_running_sandbox", lambda _root: fake.running)
    monkeypatch.setattr(
        session_runtime, "_strict_all_sandbox_states", lambda _id: list(fake.stopped)
    )
    return fake


def _place(host: SimpleNamespace, owner: str | None, state: str) -> None:
    if owner is None:
        return
    vscode = owner == "vscode"
    if state == "running":
        host.running = SimpleNamespace(name="box", vscode=vscode)
        return
    name = "vscode-box" if vscode else session_runtime.session_container_name(host.root)
    host.stopped = [(name, _inspect(host.root, vscode=vscode, running=False))]


_ROWS = [
    ("vscode", "running", False, VSCODE_INIT),
    ("vscode", "stopped", False, VSCODE_INIT),
    ("vscode", "running", True, EXTERNAL + VSCODE_SEED),
    ("vscode", "stopped", True, EXTERNAL + VSCODE_SEED),
    ("headless", "running", False, HEADLESS_REFRESH),
    ("headless", "stopped", False, HEADLESS_REFRESH),
    ("headless", "running", True, EXTERNAL + HEADLESS_RESEED),
    ("headless", "stopped", True, EXTERNAL + HEADLESS_RESEED),
    (None, "absent", False, UNKNOWN_MANAGED.rsplit("; with", 1)[0]),
    (None, "absent", True, EXTERNAL + UNKNOWN_EXTERNAL),
]


@pytest.mark.parametrize(
    ("owner", "state", "external", "expected"),
    [
        pytest.param(
            *row, id=f"{row[0] or 'unknown'}-{row[1]}-{'own-image' if row[2] else 'managed'}"
        )
        for row in _ROWS
    ],
)
def test_sandbox_remedy_names_owner_workflow(
    host: SimpleNamespace, owner: str | None, state: str, external: bool, expected: str
) -> None:
    _write_image(host.root, "registry.example/own:1" if external else None)
    _place(host, owner, state)

    remedy = session_runtime.sandbox_remedy(host.root)

    assert remedy == expected
    if owner == "vscode":
        assert not any(word in remedy for word in _SPELLED)


def test_sandbox_remedy_unknown_owner_when_both_kinds_exist(host: SimpleNamespace) -> None:
    host.stopped = [
        ("vscode-box", _inspect(host.root, vscode=True, running=False)),
        (
            session_runtime.session_container_name(host.root),
            _inspect(host.root, vscode=False, running=False),
        ),
    ]

    assert session_runtime.sandbox_remedy(host.root) == (
        f"in VS Code, {VSCODE_INIT}; headless, {HEADLESS_REFRESH}"
    )


@pytest.mark.parametrize(
    ("owner", "expected"),
    [("vscode", VSCODE_INIT), ("headless", HEADLESS_RESEED)],
)
def test_corrupt_stamp_keeps_headless_reseed_and_vscode_init(
    host: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, owner: str, expected: str
) -> None:
    def corrupt(_root: Path) -> object:
        raise session_issuance.RuntimeSpecError("unreadable stamp")

    monkeypatch.setattr(session_issuance, "load_issued_snapshot", corrupt)
    monkeypatch.setattr(session_issuance, "stamp_is_absent", lambda _root: False)
    _place(host, owner, "running")

    assert session_runtime.sandbox_remedy(host.root) == expected


@pytest.mark.parametrize(
    ("owner", "expected"),
    [("vscode", VSCODE_INIT), ("headless", HEADLESS_REFRESH)],
)
def test_withdrawn_stamp_is_what_refresh_repairs(
    host: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, owner: str, expected: str
) -> None:
    def withdrawn(_root: Path) -> object:
        raise session_issuance.RuntimeSpecError("no stamp")

    monkeypatch.setattr(session_issuance, "load_issued_snapshot", withdrawn)
    monkeypatch.setattr(session_issuance, "stamp_is_absent", lambda _root: True)
    _place(host, owner, "running")

    assert session_runtime.sandbox_remedy(host.root) == expected


@pytest.mark.parametrize(
    ("owner", "steps"),
    [("vscode", VSCODE_INIT), ("headless", HEADLESS_RESEED)],
)
def test_misnamed_stamp_starts_with_removing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str, steps: str
) -> None:
    root = tmp_path / "MixedCase"
    root.mkdir()
    place(monkeypatch, RuntimeLocation.HOST)
    monkeypatch.setattr(os.path, "normcase", ntpath.normcase)
    store = session_issuance.stamp_store()
    old = store.paths(str(root.resolve()))[1]
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"{}")
    fake = SimpleNamespace(root=root, running=None, stopped=[])
    monkeypatch.setattr(session_runtime, "resolve_running_sandbox", lambda _root: fake.running)
    _place(fake, owner, "running")

    assert session_runtime.sandbox_remedy(root) == f"remove {old}, then {steps}"


def test_own_image_named_like_the_generated_one_is_external(host: SimpleNamespace) -> None:
    _write_image(host.root, project_image_name(host.root) + "-custom")
    _place(host, "headless", "running")

    assert session_runtime.sandbox_remedy(host.root) == EXTERNAL + HEADLESS_RESEED


@pytest.mark.parametrize("location", ["issued", "hand-written"])
def test_inside_the_sandbox_lists_both_workflows(
    host: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    location: str,
) -> None:
    hand_written = location == "hand-written"
    place(
        monkeypatch,
        RuntimeLocation.HAND_WRITTEN_SANDBOX if hand_written else RuntimeLocation.ISSUED_SANDBOX,
    )

    expected = (
        UNKNOWN_MANAGED if location == "issued" else sandbox_apply.HAND_WRITTEN_SANDBOX_STEPS
    )
    assert session_runtime.sandbox_remedy(host.root) == expected


def test_unreachable_docker_asks_for_connectivity(
    host: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(_root: Path) -> None:
        raise session_runtime.SessionError("docker is down")

    monkeypatch.setattr(session_runtime, "resolve_running_sandbox", unreachable)

    assert session_runtime.sandbox_remedy(host.root).startswith("restore Docker connectivity")


@pytest.mark.skipif(os.name == "nt", reason="the hanging docker stub is a POSIX shell script")
@pytest.mark.docker_cli
def test_hung_docker_inventory_times_out_to_the_connectivity_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    calls = tmp_path / "docker-calls"
    stub = tmp_path / "bin" / "docker"
    stub.parent.mkdir()
    # No Sandbox is running; the inventory of all Sandbox containers that follows hangs.
    # The stub outlasts twice the elapsed bound, so a missed timeout still fails it.
    bound_s = scaled(15)
    stub.write_text(
        f'#!/bin/sh\necho "$*" >> "{calls}"\ncase "$1 $2" in "ps -a") exec sleep {2 * bound_s:g};; esac\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(session_admission, "_DOCKER_TIMEOUT_S", scaled(2))
    place(monkeypatch, RuntimeLocation.HOST)
    monkeypatch.setattr(session_issuance, "load_issued_snapshot", lambda _root: object())

    start = time.monotonic()
    remedy = session_runtime.sandbox_remedy(root)

    assert time.monotonic() - start < bound_s
    assert calls.read_text(encoding="utf-8").splitlines()[-1].startswith("ps -a ")
    assert remedy == (
        "restore Docker connectivity and inspect Sandbox ownership on the host before repair"
    )


def test_vscode_spec_refusal_keeps_the_external_image_prefix(host: SimpleNamespace) -> None:
    _write_image(host.root, "registry.example/own:1")
    error = session_issuance.RuntimeSpecError("stale stamp")

    remedy = session_runtime._spec_refusal_remedy(host.root, error, vscode=True)

    assert remedy == EXTERNAL + VSCODE_SEED


def test_vscode_spec_refusal_starts_with_removing_a_misnamed_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "MixedCase"
    root.mkdir()
    monkeypatch.setattr(os.path, "normcase", ntpath.normcase)
    old = session_issuance.stamp_store().paths(str(root.resolve()))[1]
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"{}")
    error = session_issuance.RuntimeSpecError("misnamed stamp")

    remedy = session_runtime._spec_refusal_remedy(root, error, vscode=True)

    assert remedy == f"remove {old}, then {VSCODE_INIT}"


def test_unpinned_booley_link_does_not_break_the_remedy(
    host: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unpinned(_root: Path) -> bool:
        raise BooleyDirPinError("link moved", "repin it")

    monkeypatch.setattr(image_lifecycle, "is_externally_managed", unpinned)
    _place(host, "vscode", "running")

    assert session_runtime.sandbox_remedy(host.root) == VSCODE_INIT
