"""The import-light Bootstrap owner verifies compatibility and migration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.runtime import host_base_acquisition as owner
from booley.runtime import interactive_docker
from booley.runtime.docker_build import DockerBuildResult
from booley.runtime.image_identity import ImageMetadata
from booley.runtime.image_provenance import (
    LABEL_RUNTIME_BASE_CONTRACT,
    LABEL_SCHEMA,
    PROVENANCE_SCHEMA,
)


def _image(
    image_id: str, labels: dict[str, str] | None = None, layers: tuple[str, ...] = ()
) -> ImageMetadata:
    """Docker metadata for one image, as the shared strict reader returns it."""
    return ImageMetadata(f"ref-{image_id}", image_id, labels or {}, {}, layers)


_BUILD_OK = DockerBuildResult(returncode=0)


@pytest.fixture
def base_owner(monkeypatch):
    labels = {LABEL_RUNTIME_BASE_CONTRACT: "current", "role": "runtime-base"}
    monkeypatch.setattr(owner, "_labels", lambda _root: labels)
    monkeypatch.setattr(
        owner,
        "source_image_build_contracts",
        lambda _root: SimpleNamespace(runtime_base="current"),
    )
    return labels


@pytest.mark.parametrize("installed,rebuild", [(None, False), ("old", False), ("current", True)])
def test_owner_builds_only_missing_stale_or_explicitly_refreshed_base(
    tmp_path, monkeypatch, base_owner, installed, rebuild
):
    records = iter(
        [
            None
            if installed is None
            else _image("old-id", {LABEL_RUNTIME_BASE_CONTRACT: installed}),
            _image("new-id", base_owner),
        ]
    )
    monkeypatch.setattr(owner, "inspect_image_metadata", lambda _ref: next(records))
    builds = []
    monkeypatch.setattr(
        owner, "_build", lambda *_args, **_kwargs: builds.append("base") or _BUILD_OK
    )
    assert owner.acquire(tmp_path, rebuild=rebuild) == "new-id"
    assert builds == ["base"]


def test_owner_reuses_current_labels_without_build_or_mutation(tmp_path, monkeypatch, base_owner):
    monkeypatch.setattr(
        owner, "inspect_image_metadata", lambda _ref: _image("same-id", base_owner)
    )
    monkeypatch.setattr(owner, "_build", lambda *_a, **_k: pytest.fail("current base built"))
    assert owner.acquire(tmp_path) == "same-id"


@pytest.mark.parametrize("layers", [["base"], ["different"]])
def test_metadata_upgrade_preserves_layers_and_never_cold_builds(
    tmp_path, monkeypatch, base_owner, layers
):
    installed = _image("old-id", {LABEL_RUNTIME_BASE_CONTRACT: "current"}, ("base",))
    candidate = _image("upgraded-id", base_owner, tuple(layers))
    monkeypatch.setattr(
        owner,
        "inspect_image_metadata",
        lambda ref: candidate if ref.endswith(":candidate") else installed,
    )
    monkeypatch.setattr(
        owner,
        "_build",
        lambda _root, recipe, output, plan: DockerBuildResult(
            returncode=0 if recipe.read_text().endswith(":parent\n") else 1
        ),
    )
    mutations = []
    monkeypatch.setattr(
        owner.subprocess,
        "run",
        lambda args, **_kw: mutations.append(args) or SimpleNamespace(returncode=0, stderr=""),
    )
    if layers == ["base"]:
        assert owner.acquire(tmp_path) == "upgraded-id"
        assert ["docker", "tag", "upgraded-id", owner.REFERENCE] in mutations
    else:
        with pytest.raises(RuntimeError, match="changed filesystem layers"):
            owner.acquire(tmp_path)
        assert not any(
            command[1] == "tag" and command[-1] == owner.REFERENCE for command in mutations
        )
    assert mutations[-1][:3] == ["docker", "image", "rm"]


def test_owner_refuses_missing_identity_after_build(tmp_path, monkeypatch, base_owner):
    monkeypatch.setattr(owner, "inspect_image_metadata", lambda _ref: None)
    monkeypatch.setattr(owner, "_build", lambda *_a, **_k: _BUILD_OK)
    with pytest.raises(RuntimeError, match="incomplete provenance"):
        owner.acquire(tmp_path)


@pytest.mark.parametrize(
    "outcome",
    [DockerBuildResult(returncode=1), DockerBuildResult(returncode=None, timed_out=True)],
)
def test_owner_raises_build_error_carrying_failed_result(
    tmp_path, monkeypatch, base_owner, outcome
):
    monkeypatch.setattr(owner, "inspect_image_metadata", lambda _ref: None)
    monkeypatch.setattr(owner, "_build", lambda *_a, **_k: outcome)
    with pytest.raises(owner.RuntimeBaseBuildError) as caught:
        owner.acquire(tmp_path)
    assert caught.value.result is outcome


def test_owner_wraps_docker_cli_failure_and_keeps_capacity_errors(
    tmp_path, monkeypatch, base_owner
):
    monkeypatch.setattr(owner, "inspect_image_metadata", lambda _ref: None)
    cli_missing = FileNotFoundError("docker")

    def run(*_args, **_kwargs):
        raise cli_missing

    monkeypatch.setattr(owner, "run_docker_build", run)
    with pytest.raises(owner.RuntimeBaseBuildError, match="docker build failed") as caught:
        owner.acquire(tmp_path)
    assert caught.value.result is None
    assert caught.value.__cause__ is cli_missing

    capacity = owner.DockerCapacityError("no space")

    def exhausted(*_args, **_kwargs):
        raise capacity

    monkeypatch.setattr(owner, "run_docker_build", exhausted)
    with pytest.raises(owner.DockerCapacityError):
        owner.acquire(tmp_path)


def test_owner_cli_imports_with_only_standard_library():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-S", "-m", "booley.runtime.host_base_acquisition", "--help"],
        env=dict(os.environ, PYTHONPATH=str(root / "src")),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--refresh" in result.stdout


def _inspect_response(monkeypatch, *, returncode=0, stdout="", stderr=""):
    """Answer every ``docker image inspect`` with one canned CLI result."""

    def answer(*_args, **_kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(interactive_docker, "_run_docker", answer)
    monkeypatch.setattr(owner.subprocess, "run", answer)


def test_acquire_preserves_inspect_failure_before_build(tmp_path, monkeypatch):
    for detail in ("Cannot connect to Docker daemon", "permission denied", ""):
        _inspect_response(monkeypatch, returncode=1, stderr=detail)
        with pytest.raises(RuntimeError, match="cannot inspect Docker image") as caught:
            owner.acquire(tmp_path)
        assert owner.REFERENCE in str(caught.value)
        assert (detail or "Docker exited 1") in str(caught.value)


def test_acquire_controls_malformed_inspect_metadata_before_build(tmp_path, monkeypatch):
    bodies = [
        "not JSON",
        "[]",
        "{}",
        "[null]",
        '[{"Id": "base"}, {"Id": "other"}]',
        '[{"Config": {}, "RootFS": {"Layers": []}}]',
        '[{"Id": "base", "Config": {"Labels": []}, "RootFS": {"Layers": []}}]',
        '[{"Id": "base", "Config": {"Labels": {"role": 42}}, "RootFS": {"Layers": []}}]',
        '[{"Id": "base", "Config": {}, "RootFS": null}]',
        '[{"Id": "base", "Config": {}, "RootFS": {"Layers": [42]}}]',
    ]
    for body in bodies:
        _inspect_response(monkeypatch, stdout=body)
        with pytest.raises(RuntimeError, match="cannot inspect Docker image"):
            owner.acquire(tmp_path)


@pytest.mark.parametrize(
    "failure",
    [subprocess.CalledProcessError(1, "docker tag"), subprocess.TimeoutExpired("docker tag", 30)],
)
def test_upgrade_controls_tag_failure_and_attempts_both_cleanups(
    tmp_path, monkeypatch, base_owner, failure
):
    installed = _image("old-id", {LABEL_RUNTIME_BASE_CONTRACT: "current"}, ("base",))
    monkeypatch.setattr(owner, "inspect_image_metadata", lambda _ref: installed)
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[1] == "tag":
            raise failure
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(owner.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="cannot tag Bootstrap image") as caught:
        owner.acquire(tmp_path)
    assert caught.value.__cause__ is failure
    assert len(calls) == 3
    assert calls[-2][-1].endswith(":candidate")
    assert calls[-1][-1].endswith(":parent")
    assert "cleanup failed" in caught.value.__notes__[0]


@pytest.mark.parametrize("changed_base", [False, True])
@pytest.mark.parametrize("base_in_plan", [False, True])
def test_upgrade_pins_parent_uses_thin_plan_and_rechecks_base(
    tmp_path, monkeypatch, base_owner, changed_base, base_in_plan
):
    installed = _image("sha256:old", {}, ("base",))
    candidate = _image("sha256:new", base_owner, ("base",))
    calls = []
    records = {owner.REFERENCE: installed}

    def run(command, **_kwargs):
        calls.append(command)
        if command[1] == "tag":
            records[command[-1]] = installed if command[2] == installed.image_id else candidate
        return SimpleNamespace(returncode=0, stderr="")

    def build(_root, recipe, output, plan):
        parent = recipe.read_text().strip().removeprefix("FROM ")
        assert parent.endswith(":parent")
        assert records[parent].image_id == "sha256:old"
        assert plan.current.estimate_class is owner.BuildEstimateClass.THIN_OVERLAY
        assert plan.current.output_tag == output
        assert plan.requests[1:] == tuple(
            request for request in heavyweight.requests if request.output_tag != owner.REFERENCE
        )
        records[output] = candidate
        if changed_base:
            records[owner.REFERENCE] = replace(installed, image_id="sha256:foreign")
        return _BUILD_OK

    monkeypatch.setattr(owner.subprocess, "run", run)
    monkeypatch.setattr(owner, "inspect_image_metadata", records.get)
    monkeypatch.setattr(owner, "_build", build)
    heavyweight = owner.DockerBuildPlan(
        (
            owner.DockerBuildRequest(owner.REFERENCE, owner.REFERENCE),
            owner.DockerBuildRequest("wheel", "wheel", owner.BuildEstimateClass.THIN_OVERLAY),
        )
    )
    if not base_in_plan:
        heavyweight = owner.DockerBuildPlan(heavyweight.requests[1:])
    if changed_base:
        with pytest.raises(RuntimeError, match="identity changed before"):
            owner._upgrade(tmp_path, installed, heavyweight)
        assert not any(call[1] == "tag" and call[-1] == owner.REFERENCE for call in calls)
    else:
        assert owner._upgrade(tmp_path, installed, heavyweight) == "sha256:new"
        assert ["docker", "tag", "sha256:new", owner.REFERENCE] in calls
    assert calls[-2][-1].endswith(":candidate")
    assert calls[-1][-1].endswith(":parent")


def test_cleanup_failure_without_primary_is_controlled(monkeypatch):
    def run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("docker image rm", 30)

    monkeypatch.setattr(owner.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Bootstrap migration cleanup failed"):
        owner._cleanup_migration(("owned:parent",), None)


def test_owner_cli_reports_error_without_traceback(tmp_path, monkeypatch, capsys):
    from contextlib import nullcontext

    monkeypatch.setattr(sys, "argv", ["owner", "--repo", str(tmp_path)])
    monkeypatch.setattr(owner, "host_lifecycle_lock", lambda _reason: nullcontext())

    def fail(*_args, **_kwargs):
        raise RuntimeError("Docker unavailable")

    monkeypatch.setattr(owner, "acquire", fail)
    assert owner.main() == 2
    error = capsys.readouterr().err
    assert "Bootstrap runtime-base acquisition failed: Docker unavailable" in error
    assert "Traceback" not in error


@pytest.mark.parametrize("planned", [False, True])
def test_base_build_emits_verified_provenance_and_honors_build_settings(
    monkeypatch: pytest.MonkeyPatch, planned: bool
) -> None:
    root = Path(__file__).resolve().parents[2]
    recipe = root / "src/booley/data/docker/Dockerfile.base"
    plan = (
        owner.DockerBuildPlan((owner.DockerBuildRequest(owner.REFERENCE, owner.REFERENCE),))
        if planned
        else None
    )
    calls = []
    outcome = DockerBuildResult(returncode=1, timed_out=True)

    def build(command: list[str], **kwargs: object) -> DockerBuildResult:
        calls.append((command, kwargs))
        return outcome

    monkeypatch.setattr(owner, "run_docker_build", build)
    monkeypatch.setenv("BOOLEY_IMAGE_BUILD_TIMEOUT", "321")
    extra = ("--build-arg", "EXTRA=1")
    assert (
        owner._build(root, recipe, owner.REFERENCE, plan, verbose=False, extra_args=extra)
        is outcome
    )
    command, kwargs = calls[0]
    labels = dict(
        command[index + 1].split("=", 1) for index, item in enumerate(command) if item == "--label"
    )
    assert (
        labels[LABEL_RUNTIME_BASE_CONTRACT]
        == owner.source_image_build_contracts(root).runtime_base
    )
    assert labels["io.booley.artifact.role"] == "runtime-base"
    assert f"BOOLEY_BASE_CONTRACT={labels[LABEL_RUNTIME_BASE_CONTRACT]}" in command
    assert "EXTRA=1" in command
    assert command[-4:] == [owner.REFERENCE, "-f", str(recipe), str(root)]
    assert kwargs["timeout"] == 321
    assert kwargs["verbose"] is False
    assert kwargs["current_request"] == (plan.current if plan else None)
    assert kwargs["remaining_plan"] is plan


def test_build_timeout_defaults_to_two_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOOLEY_IMAGE_BUILD_TIMEOUT", raising=False)
    assert owner.build_timeout() == 7200


@pytest.mark.parametrize("failure", ["alias", "build", "provenance"])
def test_acquire_migration_failure_preserves_base_and_cleans_owned_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_owner: dict[str, str], failure: str
) -> None:
    installed = _image("old", {LABEL_RUNTIME_BASE_CONTRACT: "current"}, ("layer",))
    candidate = _image("new", {}, ("layer",))
    calls = []

    def inspect(reference: str) -> ImageMetadata | None:
        if reference == owner.REFERENCE:
            return installed
        if reference.endswith(":parent"):
            return None if failure == "alias" else installed
        return candidate

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(
            returncode=1 if command[1] == "image" else 0, stderr="image is in use"
        )

    monkeypatch.setattr(owner, "inspect_image_metadata", inspect)
    monkeypatch.setattr(owner.subprocess, "run", run)
    monkeypatch.setattr(
        owner,
        "_build",
        lambda *_args: DockerBuildResult(returncode=1 if failure == "build" else 0),
    )
    message = {
        "alias": "parent alias changed",
        "build": "migration failed",
        "provenance": "expected provenance",
    }[failure]
    with pytest.raises(RuntimeError, match=message) as caught:
        owner.acquire(tmp_path)
    assert not any(command[1] == "tag" and command[-1] == owner.REFERENCE for command in calls)
    assert calls[-2][-1].endswith(":candidate") and calls[-1][-1].endswith(":parent")
    assert "image is in use" in caught.value.__notes__[0]


@pytest.mark.parametrize("build_fails", [False, True])
def test_cli_exit_status_reflects_acquisition_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_fails: bool
) -> None:
    from contextlib import nullcontext

    monkeypatch.setattr(sys, "argv", ["owner", "--repo", str(tmp_path)])
    monkeypatch.setattr(owner, "host_lifecycle_lock", lambda _reason: nullcontext())

    def acquire(*_args: object, **_kwargs: object) -> str:
        if build_fails:
            raise owner.RuntimeBaseBuildError("build failed", None)
        return "sha256:ready"

    monkeypatch.setattr(owner, "acquire", acquire)
    assert owner.main() == (2 if build_fails else 0)


def test_cli_includes_cleanup_recovery_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from contextlib import nullcontext

    monkeypatch.setattr(sys, "argv", ["owner", "--repo", str(tmp_path)])
    monkeypatch.setattr(owner, "host_lifecycle_lock", lambda _reason: nullcontext())

    def fail(*_args: object, **_kwargs: object) -> None:
        error = RuntimeError("migration failed")
        error.add_note("owned candidate cleanup failed: image is in use")
        raise error

    monkeypatch.setattr(owner, "acquire", fail)
    assert owner.main() == 2
    assert "owned candidate cleanup failed: image is in use" in capsys.readouterr().err


class FakeDocker:
    """In-memory Docker: answers ``image inspect`` and records ``docker build``."""

    def __init__(self) -> None:
        self.images: dict[str, dict[str, str]] = {}
        self.builds: list[list[str]] = []
        self.build_kwargs: list[dict[str, object]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch, *build_owners: object) -> None:
        """Route inspection and every named module's ``run_docker_build`` here."""
        monkeypatch.setattr(interactive_docker, "_run_docker", self._run_docker)
        for build_owner in build_owners:
            monkeypatch.setattr(build_owner, "run_docker_build", self._run_docker_build)

    def _run_docker(self, args: list[str], **_kwargs: object) -> SimpleNamespace:
        assert args[:2] == ["image", "inspect"]
        labels = self.images.get(args[2])
        if labels is None:
            return SimpleNamespace(
                returncode=1, stdout="", stderr=f"Error: No such image: {args[2]}"
            )
        record = {"Id": f"sha256:{args[2]}", "Config": {"Labels": labels}}
        return SimpleNamespace(returncode=0, stdout=json.dumps([record]), stderr="")

    def _run_docker_build(self, command: list[str], **kwargs: object) -> DockerBuildResult:
        self.builds.append(command)
        self.build_kwargs.append(kwargs)
        self.images[command[command.index("-t") + 1]] = _flags(command, "--label")
        return DockerBuildResult(returncode=0)


def _flags(command: list[str], flag: str) -> dict[str, str]:
    """``KEY=value`` pairs given to *flag* in a ``docker build`` command."""
    return dict(
        command[index + 1].split("=", 1) for index, item in enumerate(command) if item == flag
    )


def test_bootstrap_and_maintenance_paths_share_one_runtime_base_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import nullcontext

    from booley.harness.setup import docker_image
    from booley.harness.setup.common import InitContext

    root = Path(__file__).resolve().parents[2]
    bootstrap, maintenance = FakeDocker(), FakeDocker()

    # Host Bootstrap acquisition (through Project Initialization's wrapper).
    bootstrap.install(monkeypatch, owner)
    monkeypatch.setattr(
        docker_image,
        "run_docker_build",
        lambda *_a, **_k: pytest.fail("second runtime-base builder"),
    )
    image = docker_image._acquire_runtime_base(
        InitContext(project_root=tmp_path), root, rebuild=True
    )
    assert image == f"sha256:{owner.REFERENCE}"

    # Host maintenance entry point.
    maintenance.install(monkeypatch, owner)
    monkeypatch.setattr(sys, "argv", ["owner", "--repo", str(root), "--refresh"])
    monkeypatch.setattr(owner, "host_lifecycle_lock", lambda _reason: nullcontext())
    assert owner.main() == 0

    (bootstrap_build,), (maintenance_build,) = bootstrap.builds, maintenance.builds
    assert _flags(bootstrap_build, "--label") == _flags(maintenance_build, "--label")
    bootstrap_args = _flags(bootstrap_build, "--build-arg")
    assert set(bootstrap_args) == set(_flags(maintenance_build, "--build-arg"))
    assert set(bootstrap_args) == {
        "BOOLEY_BASE_SOURCE_REVISION",
        "BOOLEY_BASE_CONTRACT",
        "BOOLEY_BASE_BUILT_AT",
    }


def test_host_maintenance_entry_point_acquires_base_with_fake_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext

    root = Path(__file__).resolve().parents[2]
    docker = FakeDocker()
    docker.install(monkeypatch, owner)
    monkeypatch.setattr(sys, "argv", ["owner", "--repo", str(root), "--refresh"])
    monkeypatch.setattr(owner, "host_lifecycle_lock", lambda _reason: nullcontext())

    assert owner.main() == 0

    (command,) = docker.builds
    assert command[command.index("-t") + 1] == "booley-runtime-base:local"
    assert command[command.index("-f") + 1] == str(root / "src/booley/data/docker/Dockerfile.base")
    labels = docker.images[owner.REFERENCE]
    assert labels["io.booley.artifact.role"] == "runtime-base"
    assert (
        labels[LABEL_RUNTIME_BASE_CONTRACT]
        == owner.source_image_build_contracts(root).runtime_base
    )
    assert labels[LABEL_SCHEMA] == PROVENANCE_SCHEMA == "3"
    build_args = _flags(command, "--build-arg")
    assert build_args["BOOLEY_BASE_CONTRACT"] == labels[LABEL_RUNTIME_BASE_CONTRACT]
    assert build_args["BOOLEY_BASE_SOURCE_REVISION"]
    assert build_args["BOOLEY_BASE_BUILT_AT"]


def test_full_runtime_base_build_honors_capacity_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[2]
    docker = FakeDocker()
    docker.install(monkeypatch, owner)
    plan = owner.DockerBuildPlan((owner.DockerBuildRequest(owner.REFERENCE, owner.REFERENCE),))

    owner.acquire(root, rebuild=True, capacity_plan=plan, verbose=False)

    (kwargs,) = docker.build_kwargs
    assert kwargs["current_request"] == plan.current
    assert kwargs["remaining_plan"] is plan
    assert kwargs["verbose"] is False
