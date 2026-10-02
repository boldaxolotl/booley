"""The import-light Bootstrap owner verifies compatibility and migration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.runtime import host_base_acquisition as owner
from booley.runtime.image_lifecycle import HostImageScope
from booley.runtime.image_provenance import LABEL_RUNTIME_BASE_CONTRACT


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
            else {"Id": "old-id", "Config": {"Labels": {LABEL_RUNTIME_BASE_CONTRACT: installed}}},
            {"Id": "new-id", "Config": {"Labels": base_owner}},
        ]
    )
    monkeypatch.setattr(owner, "_inspect", lambda _ref: next(records))
    builds = []
    assert (
        owner.acquire(
            tmp_path,
            scope=HostImageScope(),
            rebuild=rebuild,
            build=lambda: builds.append("base") or True,
        )
        == "new-id"
    )
    assert builds == ["base"]


def test_owner_reuses_current_labels_without_build_or_mutation(tmp_path, monkeypatch, base_owner):
    monkeypatch.setattr(
        owner, "_inspect", lambda _ref: {"Id": "same-id", "Config": {"Labels": base_owner}}
    )
    assert (
        owner.acquire(
            tmp_path, scope=HostImageScope(), build=lambda: pytest.fail("current base built")
        )
        == "same-id"
    )


@pytest.mark.parametrize("layers", [["base"], ["different"]])
def test_metadata_upgrade_preserves_layers_and_never_cold_builds(
    tmp_path, monkeypatch, base_owner, layers
):
    installed = {
        "Id": "old-id",
        "Config": {"Labels": {LABEL_RUNTIME_BASE_CONTRACT: "current"}},
        "RootFS": {"Layers": ["base"]},
    }
    candidate = {
        "Id": "upgraded-id",
        "Config": {"Labels": base_owner},
        "RootFS": {"Layers": layers},
    }
    monkeypatch.setattr(
        owner, "_inspect", lambda ref: candidate if ref.endswith(":candidate") else installed
    )
    monkeypatch.setattr(
        owner,
        "_build",
        lambda _root, recipe, output, plan: recipe.read_text().endswith(":parent\n"),
    )
    mutations = []
    monkeypatch.setattr(
        owner.subprocess,
        "run",
        lambda args, **_kw: mutations.append(args) or SimpleNamespace(returncode=0, stderr=""),
    )
    if layers == ["base"]:
        assert (
            owner.acquire(
                tmp_path, scope=HostImageScope(), build=lambda: pytest.fail("cold base built")
            )
            == "upgraded-id"
        )
        assert ["docker", "tag", "upgraded-id", owner.REFERENCE] in mutations
    else:
        with pytest.raises(RuntimeError, match="changed filesystem layers"):
            owner.acquire(
                tmp_path, scope=HostImageScope(), build=lambda: pytest.fail("cold base built")
            )
        assert not any(
            command[1] == "tag" and command[-1] == owner.REFERENCE for command in mutations
        )
    assert mutations[-1][:3] == ["docker", "image", "rm"]


@pytest.mark.parametrize("result", [False, True])
def test_owner_refuses_failed_build_or_missing_identity(tmp_path, monkeypatch, base_owner, result):
    monkeypatch.setattr(owner, "_inspect", lambda _ref: None)
    if result:
        with pytest.raises(RuntimeError, match="incomplete provenance"):
            owner.acquire(tmp_path, scope=HostImageScope(), build=lambda: result)
    else:
        assert owner.acquire(tmp_path, scope=HostImageScope(), build=lambda: result) is None


def test_owner_requires_explicit_host_scope(tmp_path):
    with pytest.raises(TypeError, match="HostImageScope"):
        owner.acquire(tmp_path, scope=tmp_path)


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
    monkeypatch.setattr(
        owner.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr
        ),
    )


def test_inspect_missing_image_is_absent(monkeypatch):
    _inspect_response(monkeypatch, returncode=1, stderr="Error: No such image: missing")
    assert owner._inspect("missing") is None


@pytest.mark.parametrize("detail", ["Cannot connect to Docker daemon", "permission denied", ""])
def test_acquire_preserves_inspect_failure_before_build(tmp_path, monkeypatch, detail):
    _inspect_response(monkeypatch, returncode=1, stderr=detail)
    with pytest.raises(RuntimeError, match="cannot inspect Bootstrap image") as caught:
        owner.acquire(
            tmp_path, scope=HostImageScope(), build=lambda: pytest.fail("inspection failure built")
        )
    assert owner.REFERENCE in str(caught.value)
    assert (detail or "Docker exited 1") in str(caught.value)


@pytest.mark.parametrize(
    "failure", [OSError("Docker missing"), subprocess.TimeoutExpired("docker", 30)]
)
def test_inspect_controls_command_start_and_timeout_errors(monkeypatch, failure):
    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(owner.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="cannot inspect Bootstrap image 'base'") as caught:
        owner._inspect("base")
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize(
    "body",
    [
        "not JSON",
        "[]",
        "{}",
        "[null]",
        '[{"Id": "base"}, {"Id": "other"}]',
        '[{"Config": {}, "RootFS": {"Layers": []}}]',
        '[{"Id": "base", "Config": null, "RootFS": {"Layers": []}}]',
        '[{"Id": "base", "Config": {"Labels": []}, "RootFS": {"Layers": []}}]',
        '[{"Id": "base", "Config": {"Labels": {"role": 42}}, "RootFS": {"Layers": []}}]',
        '[{"Id": "base", "Config": {}, "RootFS": null}]',
        '[{"Id": "base", "Config": {}, "RootFS": {"Layers": [42]}}]',
    ],
)
def test_acquire_controls_malformed_inspect_metadata_before_build(tmp_path, monkeypatch, body):
    _inspect_response(monkeypatch, stdout=body)
    with pytest.raises(RuntimeError, match="invalid Bootstrap image metadata"):
        owner.acquire(
            tmp_path, scope=HostImageScope(), build=lambda: pytest.fail("malformed metadata built")
        )


@pytest.mark.parametrize("labels", [None, {}, {"role": "runtime-base", "parent": ""}])
def test_inspect_accepts_valid_metadata_and_normalizes_null_labels(monkeypatch, labels):
    record = {"Id": "sha256:base", "Config": {"Labels": labels}, "RootFS": {"Layers": ["base"]}}
    _inspect_response(monkeypatch, stdout=json.dumps([record]))
    observed = owner._inspect("base")
    assert observed["Id"] == "sha256:base"
    assert observed["Config"]["Labels"] == (labels or {})
    assert observed["RootFS"]["Layers"] == ["base"]


@pytest.mark.parametrize(
    "failure",
    [subprocess.CalledProcessError(1, "docker tag"), subprocess.TimeoutExpired("docker tag", 30)],
)
def test_upgrade_controls_tag_failure_and_attempts_both_cleanups(
    tmp_path, monkeypatch, base_owner, failure
):
    installed = {
        "Id": "old-id",
        "Config": {"Labels": {LABEL_RUNTIME_BASE_CONTRACT: "current"}},
        "RootFS": {"Layers": ["base"]},
    }
    monkeypatch.setattr(owner, "_inspect", lambda _ref: installed)
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[1] == "tag":
            raise failure
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(owner.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="cannot tag Bootstrap image") as caught:
        owner.acquire(tmp_path, scope=HostImageScope())
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
    installed = {"Id": "sha256:old", "Config": {"Labels": {}}, "RootFS": {"Layers": ["base"]}}
    candidate = {
        "Id": "sha256:new",
        "Config": {"Labels": base_owner},
        "RootFS": {"Layers": ["base"]},
    }
    calls = []
    records = {owner.REFERENCE: installed}

    def run(command, **_kwargs):
        calls.append(command)
        if command[1] == "tag":
            records[command[-1]] = installed if command[2] == installed["Id"] else candidate
        return SimpleNamespace(returncode=0, stderr="")

    def build(_root, recipe, output, plan):
        parent = recipe.read_text().strip().removeprefix("FROM ")
        assert parent.endswith(":parent")
        assert records[parent]["Id"] == "sha256:old"
        assert plan.current.estimate_class is owner.BuildEstimateClass.THIN_OVERLAY
        assert plan.current.output_tag == output
        assert plan.requests[1:] == tuple(
            request for request in heavyweight.requests if request.output_tag != owner.REFERENCE
        )
        records[output] = candidate
        if changed_base:
            records[owner.REFERENCE] = {**installed, "Id": "sha256:foreign"}
        return True

    monkeypatch.setattr(owner.subprocess, "run", run)
    monkeypatch.setattr(owner, "_inspect", records.get)
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


@pytest.mark.parametrize("returncode,timed_out", [(0, False), (1, False), (0, True)])
@pytest.mark.parametrize("planned", [False, True])
def test_base_build_emits_verified_provenance_and_honors_build_outcome(
    monkeypatch: pytest.MonkeyPatch, returncode: int, timed_out: bool, planned: bool
) -> None:
    root = Path(__file__).resolve().parents[2]
    recipe = root / "src/booley/data/docker/Dockerfile.base"
    plan = (
        owner.DockerBuildPlan((owner.DockerBuildRequest(owner.REFERENCE, owner.REFERENCE),))
        if planned
        else None
    )
    calls = []

    def build(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=returncode, timed_out=timed_out)

    monkeypatch.setattr(owner, "run_docker_build", build)
    assert owner._build(root, recipe, owner.REFERENCE, plan) is (returncode == 0 and not timed_out)
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
    assert command[-4:] == [owner.REFERENCE, "-f", str(recipe), str(root)]
    assert kwargs["timeout"] == 7200
    assert kwargs["current_request"] == (plan.current if plan else None)
    assert kwargs["remaining_plan"] is plan


@pytest.mark.parametrize("failure", ["alias", "build", "provenance"])
def test_acquire_migration_failure_preserves_base_and_cleans_owned_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_owner: dict[str, str], failure: str
) -> None:
    installed = {
        "Id": "old",
        "Config": {"Labels": {LABEL_RUNTIME_BASE_CONTRACT: "current"}},
        "RootFS": {"Layers": ["layer"]},
    }
    candidate = {"Id": "new", "Config": {"Labels": {}}, "RootFS": {"Layers": ["layer"]}}
    calls = []

    def inspect(reference: str) -> dict[str, object] | None:
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

    monkeypatch.setattr(owner, "_inspect", inspect)
    monkeypatch.setattr(owner.subprocess, "run", run)
    monkeypatch.setattr(owner, "_build", lambda *_args: failure != "build")
    message = {
        "alias": "parent alias changed",
        "build": "migration failed",
        "provenance": "expected provenance",
    }[failure]
    with pytest.raises(RuntimeError, match=message) as caught:
        owner.acquire(tmp_path, scope=HostImageScope())
    assert not any(command[1] == "tag" and command[-1] == owner.REFERENCE for command in calls)
    assert calls[-2][-1].endswith(":candidate") and calls[-1][-1].endswith(":parent")
    assert "image is in use" in caught.value.__notes__[0]


@pytest.mark.parametrize("image", [None, "sha256:ready"])
def test_cli_exit_status_reflects_acquisition_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image: str | None
) -> None:
    from contextlib import nullcontext

    monkeypatch.setattr(sys, "argv", ["owner", "--repo", str(tmp_path)])
    monkeypatch.setattr(owner, "host_lifecycle_lock", lambda _reason: nullcontext())
    monkeypatch.setattr(owner, "acquire", lambda *_args, **_kwargs: image)
    assert owner.main() == (0 if image else 2)


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
