"""The import-light Bootstrap owner verifies compatibility and migration.

Every test drives ``acquire`` or ``main`` against the shared fake Docker CLI with an
image store (``tests/fake_docker_executable.py``): nothing of Booley is patched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.fake_docker_executable import FakeDocker, install_fake_docker
from tests.subprocess_budget import run_bounded_child

from booley.runtime import host_base_acquisition as owner
from booley.runtime.docker_capacity import (
    BuildEstimateClass,
    DockerBuildPlan,
    DockerBuildRequest,
    DockerCapacityError,
)
from booley.runtime.image_build_contracts import source_image_build_contracts
from booley.runtime.image_provenance import (
    LABEL_RUNTIME_BASE_CONTRACT,
    LABEL_SCHEMA,
)

ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "src/booley/data/docker/Dockerfile.base"
ROLE = "io.booley.artifact.role"
SKIP_PREFLIGHT = "BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT"
GIB = 2**30


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Install the store-backed fake Docker CLI; call it again to restart with new state."""
    if os.name == "nt":
        # host_base_acquisition starts the bare name ``docker``; Windows process creation
        # finds only ``docker.exe`` for it, and the fake is a ``.cmd`` launcher.
        pytest.skip("the fake Docker CLI is a .cmd launcher that the bare name docker misses")
    monkeypatch.setenv(SKIP_PREFLIGHT, "1")

    def install(replies=None, images=None, **options) -> FakeDocker:
        return install_fake_docker(
            tmp_path / "docker-bin", monkeypatch, replies or {}, images=images or {}, **options
        )

    return install


@pytest.fixture
def current(fake) -> dict:
    """The store record of a base that one real ``acquire`` built: exactly the build's labels."""
    docker = fake()
    owner.acquire(ROOT)
    return docker.images[owner.REFERENCE]


def _stale(current: dict) -> dict:
    """The installed base with the current contract and one other label changed."""
    return {owner.REFERENCE: {**current, "Labels": {**current["Labels"], ROLE: "stale"}}}


def _flags(command: list[str], flag: str) -> dict[str, str]:
    """``KEY=value`` pairs given to *flag* in a ``docker build`` command."""
    return dict(
        command[index + 1].split("=", 1) for index, item in enumerate(command) if item == flag
    )


def _builds(docker: FakeDocker) -> list[list[str]]:
    return [call for call in docker.calls if call[0] == "build"]


def _removals(docker: FakeDocker) -> list[str]:
    """The references ``image rm`` was asked to remove."""
    return [call[-1] for call in docker.calls if call[:2] == ["image", "rm"]]


@pytest.mark.parametrize("case", ["missing", "stale", "refresh"])
def test_acquire_builds_missing_stale_or_refreshed_base(fake, current, case) -> None:
    images = {
        "missing": {},
        "stale": {
            owner.REFERENCE: {
                **current,
                "Labels": {**current["Labels"], LABEL_RUNTIME_BASE_CONTRACT: "old"},
            }
        },
        "refresh": {owner.REFERENCE: current},
    }[case]
    docker = fake(images=images)

    image_id = owner.acquire(ROOT, rebuild=case == "refresh")

    (build,) = _builds(docker)
    assert build[-5:] == ["-t", owner.REFERENCE, "-f", str(RECIPE), str(ROOT)]
    labels = _flags(build, "--label")
    contract = source_image_build_contracts(ROOT).runtime_base
    assert labels[LABEL_RUNTIME_BASE_CONTRACT] == contract
    assert labels[ROLE] == "runtime-base"
    assert labels[LABEL_SCHEMA] == "3"  # the persisted label format
    build_args = _flags(build, "--build-arg")
    assert set(build_args) == {
        "BOOLEY_BASE_SOURCE_REVISION",
        "BOOLEY_BASE_CONTRACT",
        "BOOLEY_BASE_BUILT_AT",
    }
    assert build_args["BOOLEY_BASE_CONTRACT"] == contract
    assert image_id == docker.images[owner.REFERENCE]["Id"]
    assert (image_id != current["Id"]) is (case != "missing")  # the empty store restarts its IDs


def test_acquire_reuses_current_base_without_docker_changes(fake, current) -> None:
    docker = fake(images={owner.REFERENCE: current})

    assert owner.acquire(ROOT) == current["Id"]
    assert docker.calls == [["image", "inspect", owner.REFERENCE, "--format", "{{json .}}"]]


@pytest.mark.parametrize("plan", ["no plan", "plan with base", "plan without base"])
def test_acquire_upgrades_metadata_on_pinned_parent(fake, current, plan) -> None:
    base = DockerBuildRequest(owner.REFERENCE, owner.REFERENCE)
    wheel = DockerBuildRequest("wheel", "wheel", BuildEstimateClass.THIN_OVERLAY)
    capacity_plan = {
        "no plan": None,
        "plan with base": DockerBuildPlan((base, wheel)),
        "plan without base": DockerBuildPlan((wheel,)),
    }[plan]
    docker = fake(images=_stale(current))

    image_id = owner.acquire(ROOT, capacity_plan=capacity_plan)

    record = docker.images[owner.REFERENCE]
    assert image_id == record["Id"] != current["Id"]
    assert record["Labels"] == current["Labels"]
    assert record["Layers"] == current["Layers"]
    assert record["Parent"].endswith(":parent")  # built on the pinned alias, not the moving tag
    (build,) = _builds(docker)
    assert build[build.index("-f") + 1] != str(RECIPE)  # a thin overlay, never a cold build
    assert list(docker.images) == [owner.REFERENCE]  # both transaction tags are gone


_UPGRADE_FAILURES = {
    "alias": ({"tag": (0, "")}, {}, "parent alias changed"),
    "build": ({"build": (1, "boom")}, {}, "migration failed"),
    "layers": ({}, {"<output>": {"Layers": ["sha256:other"]}}, "changed filesystem layers"),
    "provenance": ({}, {"<output>": {"Labels": {}}}, "expected provenance"),
    "base-changed": ({}, {owner.REFERENCE: {"Id": "sha256:foreign"}}, "identity changed before"),
}


@pytest.mark.parametrize("failure", list(_UPGRADE_FAILURES))
def test_upgrade_failure_keeps_base_and_cleans_up(fake, current, failure) -> None:
    replies, after_build, message = _UPGRADE_FAILURES[failure]
    docker = fake(
        replies | {"image rm": (1, "image is in use")},
        images=_stale(current),
        after_build=after_build,
    )

    with pytest.raises(RuntimeError, match=message) as caught:
        owner.acquire(ROOT)

    assert not any(call[0] == "tag" and call[-1] == owner.REFERENCE for call in docker.calls)
    assert [ref.rpartition(":")[2] for ref in _removals(docker)] == ["candidate", "parent"]
    assert docker.calls[-1][:3] == ["image", "rm", _removals(docker)[1]]
    assert "image is in use" in caught.value.__notes__[0]


@pytest.mark.parametrize(
    ("reply", "detail"),
    [((1, "image is in use"), "image is in use"), ((1, "", ""), "exit 1")],
    ids=["reason", "no-output"],
)
def test_upgrade_cleanup_failure_after_success_is_reported(fake, current, reply, detail) -> None:
    docker = fake({"image rm": reply}, images=_stale(current))

    with pytest.raises(RuntimeError, match="Bootstrap migration cleanup failed") as caught:
        owner.acquire(ROOT)

    assert f":candidate: {detail}" in str(caught.value)
    assert docker.images[owner.REFERENCE]["Id"] != current["Id"]  # REFERENCE already moved


def test_upgrade_tag_failure_still_attempts_both_cleanups(fake, current) -> None:
    docker = fake({"tag": (1, "denied")}, images=_stale(current), remove_after="tag")

    with pytest.raises(RuntimeError, match="cannot tag Bootstrap image") as caught:
        owner.acquire(ROOT)

    assert isinstance(caught.value.__cause__, subprocess.CalledProcessError)
    note = caught.value.__notes__[0]
    assert ":candidate" in note and ":parent" in note  # the CLI is gone: both removals raised
    assert not _removals(docker)


@pytest.mark.parametrize("case", ["build fails", "no image", "CLI missing", "capacity"])
def test_acquire_build_failures(fake, monkeypatch, case) -> None:
    replies = {
        "build fails": {"build": (1, "boom")},
        "no image": {"build": (0, "")},
        "CLI missing": {},
        "capacity": {"info": (1, "daemon down")},
    }[case]
    if case == "capacity":
        monkeypatch.delenv(SKIP_PREFLIGHT)
    fake(replies, remove_after="image" if case == "CLI missing" else "")

    if case == "no image":
        with pytest.raises(RuntimeError, match="incomplete provenance"):
            owner.acquire(ROOT)
    elif case == "capacity":
        with pytest.raises(DockerCapacityError, match="could not query Docker storage root"):
            owner.acquire(ROOT)
    else:
        with pytest.raises(owner.RuntimeBaseBuildError) as caught:
            owner.acquire(ROOT)
        if case == "build fails":
            assert caught.value.result.returncode == 1
        else:
            assert caught.value.result is None
            assert isinstance(caught.value.__cause__, FileNotFoundError)


@pytest.mark.parametrize("detail", ["Cannot connect to Docker daemon", "permission denied", ""])
def test_acquire_preserves_inspect_failure_before_build(fake, detail) -> None:
    docker = fake({"image": (1, detail)})

    with pytest.raises(RuntimeError, match="cannot inspect Docker image") as caught:
        owner.acquire(ROOT)

    assert owner.REFERENCE in str(caught.value)
    assert (detail or "Docker exited 1") in str(caught.value)
    assert not _builds(docker)


@pytest.mark.parametrize(
    "body",
    [
        "not JSON",
        "[]",
        "{}",
        "null",
        '[{"Id": "base"}, {"Id": "other"}]',
        '{"Config": {}, "RootFS": {"Layers": []}}',
        '{"Id": "base", "Config": {"Labels": []}, "RootFS": {"Layers": []}}',
        '{"Id": "base", "Config": {"Labels": {"role": 42}}, "RootFS": {"Layers": []}}',
        '{"Id": "base", "Config": {}, "RootFS": null}',
        '{"Id": "base", "Config": {}, "RootFS": {"Layers": [42]}}',
    ],
)
def test_acquire_controls_malformed_inspect_metadata_before_build(fake, body) -> None:
    docker = fake({"image": (0, body)})

    with pytest.raises(RuntimeError, match="cannot inspect Docker image"):
        owner.acquire(ROOT)

    assert not _builds(docker)


@pytest.mark.parametrize("case", ["ok", "failure", "failure with note"])
def test_cli_reports_acquisition_result(fake, current, monkeypatch, capsys, case) -> None:
    replies, images, after_build = {
        "ok": ({}, {}, {}),
        "failure": ({"image": (1, "Cannot connect to the Docker daemon")}, {}, {}),
        "failure with note": (
            {"image rm": (1, "image is in use")},
            _stale(current),
            {"<output>": {"Layers": ["sha256:other"]}},
        ),
    }[case]
    fake(replies, images=images, after_build=after_build)
    monkeypatch.setattr(sys, "argv", ["owner", "--repo", str(ROOT)])

    status = owner.main()

    error = capsys.readouterr().err
    assert status == (0 if case == "ok" else 2)
    assert ("Bootstrap runtime-base acquisition failed: " in error) is (case != "ok")
    assert "Traceback" not in error
    assert ("image is in use" in error) is (case == "failure with note")


@pytest.mark.parametrize(
    ("installed", "free_gib", "refused", "with_base"),
    [
        (False, 37, True, True),
        (True, 20, False, True),
        (True, 11, True, True),
        (True, 11, True, False),
    ],
    ids=["full 37 GiB", "upgrade 20 GiB", "upgrade 11 GiB", "upgrade 11 GiB, plan without base"],
)
def test_acquire_passes_capacity_plan_to_preflight(
    fake, current, tmp_path, monkeypatch, installed, free_gib, refused, with_base
) -> None:
    monkeypatch.delenv(SKIP_PREFLIGHT)
    monkeypatch.setattr(
        "shutil.disk_usage", lambda _path: SimpleNamespace(total=0, used=0, free=free_gib * GIB)
    )
    replies = {"info": (0, str(tmp_path)), "system": (0, "Build Cache\t20GB\t18GB")}
    base = DockerBuildRequest(owner.REFERENCE, owner.REFERENCE)
    other = DockerBuildRequest("other", "other")  # a second cold build: the plan needs 40 GiB
    wheel = DockerBuildRequest("wheel", "wheel", BuildEstimateClass.THIN_OVERLAY)
    follow = wheel if installed else other
    plan = DockerBuildPlan((base, follow) if with_base else (follow,))
    fake(replies, images=_stale(current) if installed else {})

    if refused:
        with pytest.raises(DockerCapacityError, match="free"):
            owner.acquire(ROOT, capacity_plan=plan)
    else:
        owner.acquire(ROOT, capacity_plan=plan)


def test_bootstrap_and_maintenance_paths_build_the_same_base(fake, tmp_path, monkeypatch) -> None:
    from booley.harness.setup import docker_image
    from booley.harness.setup.common import InitContext

    docker = fake()
    # Public Project Initialization reaches this step only after its whole sandbox build
    # pipeline, which no test of the host base can run.
    docker_image._acquire_runtime_base(InitContext(project_root=tmp_path), ROOT, rebuild=True)
    monkeypatch.setattr(sys, "argv", ["owner", "--repo", str(ROOT), "--refresh"])
    assert owner.main() == 0

    bootstrap, maintenance = _builds(docker)
    assert _flags(bootstrap, "--label") == _flags(maintenance, "--label")
    assert set(_flags(bootstrap, "--build-arg")) == set(_flags(maintenance, "--build-arg"))


def test_build_timeout_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOLEY_IMAGE_BUILD_TIMEOUT", "321")
    assert owner.build_timeout() == 321


def test_owner_cli_imports_with_only_standard_library():
    result = run_bounded_child(
        [sys.executable, "-S", "-m", "booley.runtime.host_base_acquisition", "--help"],
        env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
    )
    assert result.returncode == 0, result.stderr
    assert "--refresh" in result.stdout
