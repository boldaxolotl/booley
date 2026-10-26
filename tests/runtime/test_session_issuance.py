"""Universal host-issued Sandbox specification tests."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests import eda_grant_support

from booley.eda.provisioning import authority
from booley.eda.provisioning import session_requirements as eda_requirements
from booley.eda.provisioning.licensing import flexnet_docker
from booley.eda.provisioning.policies.vivado import CONTAINER_TARGET, POLICY_REVISION, wrapper_path
from booley.harness import booley, eda_grants
from booley.runtime import devcontainer as dc
from booley.runtime import (
    issuance_invalidation,
    session_admission,
    session_refresh,
    session_runtime,
    session_spec,
)
from booley.runtime import session_issuance as runtime_spec
from booley.runtime.booley_dir import reset_cache
from booley.runtime.platform_paths import docker_mount_path

# Issuance fixtures replace PATH with a synthetic launcher directory.
_HOST_PATH = os.environ.get("PATH", os.defpath)
_HOST_GIT = shutil.which("git", path=_HOST_PATH)


def _install_trusted_validator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a host-owned validator without relying on the runner's install layout."""
    prefix = tmp_path / "trusted-prefix"
    executable = prefix / "bin" / ("booley.exe" if os.name == "nt" else "booley")
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        runtime_spec,
        "_validator_prefix_anchors",
        lambda: {executable.parent.resolve(): prefix.resolve()},
    )
    return executable


@pytest.fixture
def trusted_validator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    executable = _install_trusted_validator(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", str(executable.parent))
    return executable


def test_pin_image_preserves_reconciled_immutable_id(monkeypatch: pytest.MonkeyPatch) -> None:
    image_id = "sha256:" + "a" * 64
    spec = {"image": "booley-sandbox"}
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda image: image)

    assert runtime_spec.pin_image(spec, expected_image_id=image_id) == image_id
    assert spec["image"] == image_id


def test_pin_image_rejects_reconciled_id_that_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = "sha256:" + "a" * 64
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:other")

    with pytest.raises(runtime_spec.RuntimeSpecError, match="no longer resolves"):
        runtime_spec.pin_image({"image": "booley-sandbox"}, expected_image_id=expected)


def _stub_preview_dependencies(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    requirements: eda_requirements.SessionEdaRequirements | None = None,
) -> list[runtime_spec.SessionSpecInputs]:
    captured: list[runtime_spec.SessionSpecInputs] = []
    previewed = requirements or eda_requirements._requirements(project, None, None)
    monkeypatch.setattr(runtime_spec, "flow_enabled", lambda *_args: True)
    monkeypatch.setattr(
        runtime_spec.eda_requirements,
        "inspect_build_requirements",
        lambda *_args, **_kwargs: previewed,
    )
    monkeypatch.setattr(runtime_spec, "authorized_project_data_source", lambda _root: project)
    monkeypatch.setattr(
        runtime_spec,
        "pin_image",
        lambda spec, **_kwargs: spec.update(image="sha256:pinned") or spec["image"],
    )
    monkeypatch.setattr(runtime_spec, "_pin_initialize_command", lambda *_args: None)
    monkeypatch.setattr(runtime_spec, "_pin_project_data_mount", lambda *_args: None)
    monkeypatch.setattr(runtime_spec, "_pin_devcontainer_mount", lambda *_args: None)
    monkeypatch.setattr(runtime_spec, "_seal_with_requirements", lambda *_args: "digest")
    monkeypatch.setattr(
        runtime_spec,
        "_prospective_issuance",
        lambda *_args: SimpleNamespace(project_data_layout="legacy"),
    )
    return captured


def test_preview_exposes_only_detached_inputs_and_does_not_persist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    installation = authority.Installation("vivado", "vivado", "/host/tool", "2025.2", "x86_64", 1)
    profile = authority.LicenseProfile("floating", "flexnet", "192.0.2.1", "0", 2100, 2101)
    captured = _stub_preview_dependencies(
        project,
        monkeypatch,
        eda_requirements._requirements(project, installation, profile),
    )

    def build(inputs: runtime_spec.SessionSpecInputs) -> dict:
        captured.append(inputs)
        return {"image": "booley-sandbox"}

    prepared = runtime_spec.preview(project, build)

    assert prepared.digest == "digest"
    assert prepared.spec["image"] == "sha256:pinned"
    assert captured == [prepared.inputs]
    assert prepared.inputs.project_data_source == project
    assert prepared.inputs.trusted_eda_mounts == (("/host/tool", CONTAINER_TARGET),)
    assert dict(prepared.inputs.fixed_container_environment)["XILINXD_LICENSE_FILE"] == (
        "2100@booley-license-xilinx"
    )
    assert (prepared.inputs.installation_name, prepared.inputs.license_profile_name) == (
        "vivado",
        "floating",
    )
    assert prepared.prospective_issuance is not None
    assert not (project / ".devcontainer" / "devcontainer.json").exists()


def test_licensed_preview_does_not_create_authority_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _concurrent_licensed_project(tmp_path, monkeypatch)
    lock = authority.state_dir() / "authority.lock"
    lock.unlink()
    before = authority.state_path().read_bytes()

    prepared = runtime_spec.preview(project, lambda _inputs: {"image": "booley-sandbox"})

    assert prepared.inputs.license_profile_name == "site"
    assert authority.state_path().read_bytes() == before
    assert not lock.exists()


def test_issue_prepared_revalidates_authority_and_persists_exact_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    inputs = runtime_spec.SessionSpecInputs(project, (), (), None, None)
    spec = {"image": "sha256:pinned", "runArgs": []}
    prospective = SimpleNamespace(project_data_layout="legacy")
    prepared = runtime_spec.PreparedSessionSpec(spec, "digest", inputs, prospective)
    leased = SimpleNamespace()
    requirements = SimpleNamespace()
    issuance = SimpleNamespace()
    persist = Mock(return_value=issuance)
    events: list[str] = []
    monkeypatch.setattr(runtime_spec, "_spec_digest", lambda value: "digest")
    monkeypatch.setattr(runtime_spec, "flow_enabled", lambda *_args: False)
    monkeypatch.setattr(
        runtime_spec.eda_requirements,
        "lease_requirements",
        lambda *_args, **_kwargs: nullcontext(leased),
    )
    monkeypatch.setattr(
        runtime_spec,
        "_session_spec_inputs",
        lambda *_args: events.append("validate authority") or inputs,
    )
    monkeypatch.setattr(
        runtime_spec,
        "_prospective_issuance",
        lambda *_args: events.append("validate policy") or prospective,
    )
    monkeypatch.setattr(
        runtime_spec.eda_requirements,
        "prepare_runtime_dependencies",
        lambda *_args, **_kwargs: events.append("prepare dependencies") or requirements,
    )
    monkeypatch.setattr(runtime_spec, "_persist_prepared", persist)

    result = runtime_spec.issue_prepared(project, prepared)

    assert result is issuance
    assert events == ["validate authority", "validate policy", "prepare dependencies"]
    persist.assert_called_once_with(
        project, prepared, requirements=requirements, reconcile_dashboard=True
    )


def test_issue_prepared_rejects_authority_drift_before_persisting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    prospective = SimpleNamespace(project_data_layout="legacy")
    prepared = runtime_spec.PreparedSessionSpec(
        {"image": "sha256:pinned", "runArgs": []},
        "digest",
        runtime_spec.SessionSpecInputs(project, (), (), None, None),
        prospective,
    )
    changed = runtime_spec.SessionSpecInputs(project, (), (), "vivado-new", "license-new")
    leased = SimpleNamespace()
    persist = Mock()
    prepare_dependencies = Mock(side_effect=pytest.fail)
    monkeypatch.setattr(runtime_spec, "_spec_digest", lambda value: "digest")
    monkeypatch.setattr(runtime_spec, "flow_enabled", lambda *_args: False)
    monkeypatch.setattr(
        runtime_spec.eda_requirements,
        "lease_requirements",
        lambda *_args, **_kwargs: nullcontext(leased),
    )
    monkeypatch.setattr(runtime_spec, "_session_spec_inputs", lambda *_args: changed)
    monkeypatch.setattr(runtime_spec, "_prospective_issuance", lambda *_args: prospective)
    monkeypatch.setattr(
        runtime_spec.eda_requirements,
        "prepare_runtime_dependencies",
        prepare_dependencies,
    )
    monkeypatch.setattr(runtime_spec, "_persist_prepared", persist)

    with pytest.raises(runtime_spec.RuntimeSpecError, match="authority changed") as caught:
        runtime_spec.issue_prepared(project, prepared)

    assert "installation_name <none> -> vivado-new" in str(caught.value)
    assert "license_profile_name <none> -> license-new" in str(caught.value)

    prepare_dependencies.assert_not_called()
    persist.assert_not_called()


def _preview_for_rollback(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> runtime_spec.PreparedSessionSpec:
    """Preview an empty-EDA spec that `issue_prepared` accepts after revalidation."""
    _stub_preview_dependencies(project, monkeypatch)
    monkeypatch.setattr(runtime_spec, "_spec_digest", lambda _spec: "digest")
    return runtime_spec.preview(project, lambda _inputs: {"image": "booley-sandbox"})


def test_prepared_issue_restores_previous_spec_when_issuance_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = project / ".devcontainer" / "devcontainer.json"
    path.parent.mkdir()
    path.write_text("previous\n", encoding="utf-8")
    previous_issuance = runtime_spec.Issuance(
        version=runtime_spec.STAMP_VERSION,
        project_root=str(project),
        spec_sha256="a" * 64,
        image="sha256:old",
        image_id="sha256:old",
        keeper_image=runtime_spec.keeper_image(project),
        policy_revision=0,
        installation=None,
        license_profile=None,
        wrapper_sha256=None,
        relay_image_id=None,
        validator_sha256="b" * 64,
        file_sha256="c" * 64,
        project_data_source=str(project),
    )
    stamp = runtime_spec.stamp_path(project)
    runtime_spec._write_stamp(stamp, previous_issuance)
    keeper_restores = []
    monkeypatch.setattr(
        "booley.runtime.interactive_docker.tag_image",
        lambda source, target: keeper_restores.append((source, target)),
    )
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:old")

    def write_devcontainer(_project: Path, _spec: dict) -> Path:
        path.write_text("prospective\n", encoding="utf-8")
        return path

    def fail_after_stamp_write(*_args) -> None:
        stamp.write_text("replacement", encoding="utf-8")
        raise runtime_spec.RuntimeSpecError("failed")

    monkeypatch.setattr(dc, "write_devcontainer", write_devcontainer)
    monkeypatch.setattr(runtime_spec, "_issue_document_with_requirements", fail_after_stamp_write)
    prepared = _preview_for_rollback(project, monkeypatch)

    with pytest.raises(runtime_spec.RuntimeSpecError, match="failed"):
        runtime_spec.issue_prepared(project, prepared)

    assert path.read_text(encoding="utf-8") == "previous\n"
    assert runtime_spec._load_stamp(stamp) == previous_issuance
    assert keeper_restores == [(previous_issuance.image_id, previous_issuance.keeper_image)]


def test_prepared_issue_restores_spec_when_writer_partially_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = project / ".devcontainer" / "devcontainer.json"
    path.parent.mkdir()
    path.write_text("previous\n", encoding="utf-8")
    prepared = _preview_for_rollback(project, monkeypatch)

    def partial_write(_project: Path, _spec: dict) -> Path:
        path.write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(dc, "write_devcontainer", partial_write)
    monkeypatch.setattr("booley.runtime.interactive_docker.image_id_strict", lambda _image: None)
    monkeypatch.setattr("booley.runtime.interactive_docker.remove_image_tag", lambda _image: None)

    with pytest.raises(OSError, match="disk full"):
        runtime_spec.issue_prepared(project, prepared)

    assert path.read_text(encoding="utf-8") == "previous\n"


def test_failed_first_issue_removes_new_keeper_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    prepared = _preview_for_rollback(project, monkeypatch)
    removed = []

    def remove_keeper(image: str) -> None:
        removed.append(image)

    monkeypatch.setattr("booley.runtime.interactive_docker.image_id_strict", lambda _image: None)
    monkeypatch.setattr(
        "booley.runtime.interactive_docker.remove_image_tag",
        remove_keeper,
    )
    monkeypatch.setattr(
        dc,
        "write_devcontainer",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        runtime_spec.issue_prepared(project, prepared)

    assert removed == [runtime_spec.keeper_image(project)]


def test_issue_fails_before_writing_when_keeper_state_cannot_be_inspected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    prepared = _preview_for_rollback(project, monkeypatch)
    writer = Mock()
    monkeypatch.setattr(dc, "write_devcontainer", writer)
    monkeypatch.setattr(
        "booley.runtime.interactive_docker.image_id_strict",
        lambda _image: (_ for _ in ()).throw(RuntimeError("daemon unavailable")),
    )

    with pytest.raises(runtime_spec.RuntimeSpecError, match="cannot snapshot"):
        runtime_spec.issue_prepared(project, prepared)

    writer.assert_not_called()


def _concurrent_licensed_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    reset_cache()
    project = tmp_path / "project"
    booley_dir = project / ".booley"
    booley_dir.mkdir(parents=True)
    (booley_dir / "booley.toml").write_text(
        '[flows.fpga]\nenabled = true\n[eda.vivado]\nprovisioning = "image"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    profile = eda_grant_support.register_license("site")
    eda_grant_support.add_grant(project, "vivado", license_profile=profile.name)
    monkeypatch.setattr(eda_requirements, "ensure_relay_image", lambda **_kwargs: False)
    monkeypatch.setattr(
        eda_requirements,
        "_relay_image_id",
        lambda: "sha256:" + "d" * 64,
    )
    monkeypatch.setattr(runtime_spec, "pin_image", lambda *_args, **_kwargs: "sha256:image")
    monkeypatch.setattr(runtime_spec, "_pin_initialize_command", lambda *_args: None)
    monkeypatch.setattr(runtime_spec, "_pin_project_data_mount", lambda *_args: None)
    monkeypatch.setattr(runtime_spec, "_pin_devcontainer_mount", lambda *_args: None)
    monkeypatch.setattr(runtime_spec, "_seal_with_requirements", lambda *_args: "digest")
    monkeypatch.setattr(runtime_spec, "_spec_digest", lambda _spec: "digest")
    monkeypatch.setattr(
        runtime_spec,
        "_prospective_issuance",
        lambda *_args: SimpleNamespace(project_data_layout="legacy"),
    )
    monkeypatch.setattr(flexnet_docker, "cleanup_project_resources_for_identity", lambda _root: ())
    return project


def test_builder_issuance_serializes_with_concurrent_grant_revoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _concurrent_licensed_project(tmp_path, monkeypatch)

    persistence_entered = threading.Event()
    allow_persistence = threading.Event()
    issue_done = threading.Event()
    revoke_done = threading.Event()
    errors: list[BaseException] = []

    def persist(*_args, **_kwargs):
        persistence_entered.set()
        if not allow_persistence.wait(2):
            raise runtime_spec.RuntimeSpecError("test persistence gate timed out")
        return object()

    monkeypatch.setattr(runtime_spec, "_persist_prepared", persist)

    def run_issue() -> None:
        try:
            prepared = runtime_spec.preview(project, lambda _inputs: {"image": "booley-sandbox"})
            runtime_spec.issue_prepared(project, prepared)
        except RuntimeError as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            issue_done.set()

    def run_revoke() -> None:
        try:
            eda_grants.GrantCoordinator().revoke(project, "vivado")
        except RuntimeError as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            revoke_done.set()

    issuer = threading.Thread(target=run_issue)
    issuer.start()
    assert persistence_entered.wait(2)
    revoker = threading.Thread(target=run_revoke)
    revoker.start()
    assert not revoke_done.wait(0.2)
    allow_persistence.set()
    issuer.join(2)
    revoker.join(2)

    assert issue_done.is_set() and revoke_done.is_set()
    assert errors == []
    reset_cache()


def test_resolve_image_id_rejects_missing_runtime_image(monkeypatch) -> None:
    monkeypatch.setattr("booley.runtime.interactive_docker.image_id", lambda _image: None)

    with pytest.raises(runtime_spec.RuntimeSpecError, match="cannot resolve Sandbox Image"):
        runtime_spec._resolve_image_id("missing:latest")


@pytest.fixture
def issued(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
):
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    host_skill = tmp_path / "host-skills" / "example-skill"
    host_skill.mkdir(parents=True)
    (host_skill / "SKILL.md").write_text("# Example skill\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        host_skills=(("example-skill", docker_mount_path(host_skill)),),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    path = dc.write_devcontainer(project, spec)
    stamp = runtime_spec.issue(project, spec, path)
    return project, spec, path, stamp


def test_issued_spec_seals_pinned_image_identity(issued) -> None:
    _project, spec, _path, stamp = issued
    assert spec["containerEnv"].get("BOOLEY_SANDBOX_IMAGE_ID") == stamp.image_id
    assert runtime_spec._spec_digest(spec) == stamp.spec_sha256
    spec["containerEnv"]["BOOLEY_SANDBOX_IMAGE_ID"] = "sha256:" + "f" * 64
    assert runtime_spec._spec_digest(spec) != stamp.spec_sha256


def test_every_project_requires_exact_host_stamp(issued) -> None:
    project, spec, path, stamp = issued
    assert runtime_spec.validate(project, spec, path) == stamp
    runtime_spec.stamp_path(project).unlink()
    with pytest.raises(runtime_spec.RuntimeSpecError) as raised:
        runtime_spec.validate(project, spec, path)
    assert str(raised.value) == _WITHDRAWN_ISSUANCE
    assert isinstance(raised.value.__cause__, FileNotFoundError)


_WITHDRAWN_ISSUANCE = (
    "Sandbox issuance was withdrawn (EDA grant change, invalidation or Project forget)"
)


@pytest.mark.parametrize("reader", ["validate", "authenticate", "issued", "recovery"])
@pytest.mark.parametrize("remove_parent", [False, True])
def test_missing_issuance_is_withdrawn(issued, reader, remove_parent) -> None:
    project, spec, path, _stamp = issued
    current = runtime_spec.stamp_path(project)
    current.unlink()
    assert not runtime_spec._legacy_stamp_path(str(project.resolve())).exists()
    if remove_parent:
        current.parent.rmdir()
    readers = {
        "validate": lambda: runtime_spec.validate(project, spec, path),
        "authenticate": lambda: runtime_spec.authenticate(project, spec, path),
        "issued": lambda: runtime_spec.load_issued_snapshot(project),
        "recovery": lambda: runtime_spec.load_recovery_snapshot(project, spec, path),
    }
    with pytest.raises(runtime_spec.RuntimeSpecError) as raised:
        readers[reader]()
    assert str(raised.value) == _WITHDRAWN_ISSUANCE
    assert isinstance(raised.value.__cause__, FileNotFoundError)


@pytest.mark.parametrize("damage", ["json", "utf8", "schema", "version", "type", "permission"])
def test_corrupt_issuance_is_not_withdrawn(issued, monkeypatch, damage) -> None:
    project, spec, path, _stamp = issued
    current = runtime_spec.stamp_path(project)
    if damage == "permission":
        original_open = os.open

        def denied(file, flags, *args, **kwargs):
            if file == current:
                raise PermissionError("stamp access denied")
            return original_open(file, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", denied)
    elif damage in {"json", "utf8"}:
        current.write_bytes(b"{" if damage == "json" else b"\xff")
    else:
        document = json.loads(current.read_text(encoding="utf-8"))
        if damage == "schema":
            del document["image"]
        else:
            document["version"] = 999 if damage == "version" else "invalid"
        current.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(runtime_spec.RuntimeSpecError) as raised:
        runtime_spec.validate(project, spec, path)
    assert "corrupt" in str(raised.value)
    assert "withdrawn" not in str(raised.value)
    causes = {
        "json": json.JSONDecodeError,
        "utf8": UnicodeDecodeError,
        "permission": PermissionError,
        "schema": runtime_spec.RuntimeSpecError,
        "version": runtime_spec.RuntimeSpecError,
        "type": runtime_spec.RuntimeSpecError,
    }
    assert isinstance(raised.value.__cause__, causes[damage])


def test_valid_legacy_issuance_still_authenticates(issued) -> None:
    project, spec, path, stamp = issued
    current = runtime_spec.stamp_path(project)
    legacy = runtime_spec._legacy_stamp_path(str(project.resolve()))
    authority._store().ensure_directory()
    legacy.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    current.replace(legacy)
    assert runtime_spec.validate(project, spec, path) == stamp


def test_real_invalidation_removes_both_issuance_stamps(issued, monkeypatch) -> None:
    project, spec, path, _stamp = issued
    current = runtime_spec.stamp_path(project)
    legacy = runtime_spec._legacy_stamp_path(str(project.resolve()))
    authority._store().ensure_directory()
    legacy.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    legacy.write_bytes(current.read_bytes())
    monkeypatch.setattr(session_refresh, "shared_recovery_blocks_command", lambda **_kw: False)
    monkeypatch.setattr(flexnet_docker, "cleanup_project_resources_for_identity", lambda _id: ())
    issuance_invalidation.prepare(str(project.resolve()))
    assert issuance_invalidation.recover_project_locked(str(project.resolve()))
    assert not current.exists()
    assert not legacy.exists()
    assert issuance_invalidation.pending_invalidations() == ()
    with pytest.raises(runtime_spec.RuntimeSpecError) as raised:
        runtime_spec.validate(project, spec, path)
    assert str(raised.value) == _WITHDRAWN_ISSUANCE


def test_real_grant_add_writes_no_journal_and_removes_stamp(issued, monkeypatch) -> None:
    project, _spec, _path, _stamp = issued
    current = runtime_spec.stamp_path(project)
    legacy = runtime_spec._legacy_stamp_path(str(project.resolve()))
    authority._store().ensure_directory()
    legacy.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    legacy.write_bytes(current.read_bytes())
    monkeypatch.setattr(session_refresh, "shared_recovery_blocks_command", lambda **_kw: False)
    monkeypatch.setattr(
        issuance_invalidation,
        "prepare",
        lambda *_args, **_kw: pytest.fail("grant add must not write an invalidation journal"),
    )
    profile = authority.register_license(
        "site",
        server_ipv4="10.20.30.40",
        server_hostid="licenses.example.internal",
        lmgrd_port=2100,
        vendor_port=2101,
    )

    grant = eda_grants.GrantCoordinator().add(
        project,
        "vivado",
        installation=None,
        license_profile=profile.name,
    )

    assert authority.load_state().grants == (grant,)
    assert not current.exists()
    assert not legacy.exists()
    assert issuance_invalidation.pending_invalidations() == ()


def _revoke_issued_project(project: Path, monkeypatch: pytest.MonkeyPatch) -> Mock:
    profile = eda_grant_support.register_license("site")
    grant = eda_grant_support.add_grant(project, "vivado", license_profile=profile.name)
    monkeypatch.setattr(session_refresh, "shared_recovery_blocks_command", lambda **_kw: False)
    monkeypatch.setattr(
        session_refresh,
        "recover_project_locked",
        lambda _root: SimpleNamespace(outcome=session_refresh.RecoveryOutcome.NONE),
    )
    cleanup = Mock(return_value=())
    monkeypatch.setattr(flexnet_docker, "cleanup_project_resources_for_identity", cleanup)
    docker = Mock(side_effect=AssertionError("Docker must not run after withdrawn issuance"))
    monkeypatch.setattr("booley.runtime.interactive_docker._run_docker", docker)
    monkeypatch.setattr(session_runtime, "_run", docker)
    monkeypatch.setattr(session_runtime, "_create_session_container", docker)
    monkeypatch.setattr("booley.runtime.runtime_attachment.run_command", docker)
    assert eda_grants.GrantCoordinator().revoke(project, "vivado") == grant
    assert authority.load_state().grants == ()
    assert issuance_invalidation.pending_invalidations() == ()
    assert not runtime_spec.stamp_path(project).exists()
    cleanup.assert_called_once_with(str(project.resolve()))
    return docker


@pytest.mark.parametrize("boundary", ["up", "ensure", "enter", "cli", "refresh"])
def test_real_revoke_reports_withdrawal_at_enter_and_cli(
    issued, monkeypatch, capsys, boundary
) -> None:
    project, _spec, _path, _stamp = issued
    docker = _revoke_issued_project(project, monkeypatch)
    if boundary in {"up", "ensure", "enter"}:
        with pytest.raises(session_runtime.SessionError) as raised:
            if boundary == "enter":
                session_runtime.enter(project, ["true"], tty=False)
            else:
                getattr(session_runtime, boundary)(project)
        assert _WITHDRAWN_ISSUANCE in str(raised.value)
        assert "corrupt" not in str(raised.value)
    elif boundary == "cli":
        args = SimpleNamespace(session_command="enter", exec_cmd=["--", "true"])
        assert booley._cmd_session(args, project) == 2
        error = capsys.readouterr().err
        assert _WITHDRAWN_ISSUANCE in error
        assert "corrupt" not in error
    else:
        with pytest.raises(session_runtime.SessionError) as refresh_error:
            session_refresh._load_recovery_issuance(project)
        assert str(refresh_error.value).startswith(
            "cannot preserve the prior Sandbox: " + _WITHDRAWN_ISSUANCE
        )
    docker.assert_not_called()


def test_inspect_prepared_accepts_exact_current_issuance(issued) -> None:
    project, spec, _path, stamp = issued
    prepared = runtime_spec.PreparedSessionSpec(
        spec=spec,
        digest=stamp.spec_sha256,
        inputs=runtime_spec.SessionSpecInputs(project / ".booley", (), (), None, None),
    )

    assert runtime_spec.inspect_prepared(project, prepared) == stamp


def test_inspect_prepared_rejects_different_desired_spec(issued) -> None:
    project, spec, _path, _stamp = issued
    desired = json.loads(json.dumps(spec))
    desired["name"] = "changed desired spec"
    prepared = runtime_spec.PreparedSessionSpec(
        spec=desired,
        digest=runtime_spec._spec_digest(desired),
        inputs=runtime_spec.SessionSpecInputs(project / ".booley", (), (), None, None),
    )

    with pytest.raises(runtime_spec.RuntimeSpecError, match="prepared specification"):
        runtime_spec.inspect_prepared(project, prepared)


def test_inspect_prepared_rejects_missing_document_without_creating_it(issued) -> None:
    project, spec, path, stamp = issued
    path.unlink()
    prepared = runtime_spec.PreparedSessionSpec(
        spec=spec,
        digest=stamp.spec_sha256,
        inputs=runtime_spec.SessionSpecInputs(project / ".booley", (), (), None, None),
    )

    with pytest.raises(runtime_spec.RuntimeSpecError, match="missing"):
        runtime_spec.inspect_prepared(project, prepared)

    assert not path.exists()


def test_legacy_registrar_remains_valid_for_issued_specs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
) -> None:
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    legacy = "python -m booley.runtime.incontainer_register"
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=legacy,
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    path = dc.write_devcontainer(project, spec)
    stamp = runtime_spec.issue(project, spec, path)

    assert runtime_spec.validate(project, spec, path) == stamp


def test_recovery_snapshot_uses_sealed_issuance_without_current_authority(
    issued, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _spec, _path, stamp = issued
    monkeypatch.setattr(
        authority,
        "resolve_for_issuance",
        lambda *_args, **_kwargs: pytest.fail("recovery consulted current authority"),
    )

    assert runtime_spec.load_issued_snapshot(project) == stamp


def test_legacy_eda_stamp_recovers_and_next_issue_migrates_to_runtime(issued) -> None:
    project, spec, path, stamp = issued
    current = runtime_spec.stamp_path(project)
    legacy = runtime_spec._legacy_stamp_path(str(project.resolve()))
    legacy.parent.mkdir(parents=True, mode=0o700)
    legacy.parent.chmod(0o700)
    current.replace(legacy)

    assert runtime_spec.load_issued_snapshot(project) == stamp
    snapshot = session_spec.capture_session_spec(project)
    assert snapshot.stamp_path == current
    assert snapshot.stamp_content == legacy.read_bytes()
    assert runtime_spec.issue(project, spec, path) == stamp
    assert current.is_file()
    assert not legacy.exists()


def test_recovery_snapshot_rejects_different_project_identity(issued, monkeypatch) -> None:
    project, _spec, _path, stamp = issued
    monkeypatch.setattr(
        runtime_spec,
        "_load_stamp",
        lambda _path: replace(stamp, project_root=str(project.parent / "other")),
    )

    with pytest.raises(runtime_spec.RuntimeSpecError, match="different Project"):
        runtime_spec.load_issued_snapshot(project)


def test_recovery_snapshot_rejects_different_keeper(issued, monkeypatch) -> None:
    project, _spec, _path, stamp = issued
    monkeypatch.setattr(
        runtime_spec,
        "_load_stamp",
        lambda _path: replace(stamp, keeper_image="foreign:session"),
    )

    with pytest.raises(runtime_spec.RuntimeSpecError, match="image keeper differs"):
        runtime_spec.load_issued_snapshot(project)


def test_recovery_snapshot_rejects_missing_keeper_image(issued, monkeypatch) -> None:
    project, spec, path, _stamp = issued

    def missing(_image: str) -> str:
        raise runtime_spec.RuntimeSpecError("missing")

    monkeypatch.setattr(runtime_spec, "_resolve_image_id", missing)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="Sandbox Image keeper is missing"):
        runtime_spec.load_recovery_snapshot(project, spec, path)


def test_no_eda_issuance_and_validation_never_open_authority_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
) -> None:
    del trusted_validator
    reset_cache()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")

    authority.state_dir().mkdir(parents=True, mode=0o700)
    authority.state_dir().parent.chmod(0o700)
    authority.state_dir().parent.parent.chmod(0o700)
    authority.state_path().write_text("not valid authority JSON", encoding="utf-8")
    authority.state_path().chmod(0o400)
    monkeypatch.setattr(
        authority,
        "resolve_for_issuance",
        lambda *_args, **_kwargs: pytest.fail("no-EDA runtime opened the authority store"),
    )

    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    path = dc.write_devcontainer(project, spec)
    stamp = runtime_spec.issue(project, spec, path)

    assert runtime_spec.validate(project, spec, path) == stamp
    assert authority.state_path().read_text(encoding="utf-8") == "not valid authority JSON"
    assert not (authority.state_dir() / "authority.lock").exists()

    authority.state_dir().chmod(0o500)
    try:
        monkeypatch.setattr(session_runtime, "_warn_on_image_drift", lambda *_args: None)
        monkeypatch.setattr(session_runtime, "_warn_on_stale_booley_bake", lambda *_args: None)
        monkeypatch.setattr(
            session_runtime, "_warn_on_stale_session_containers", lambda *_args: None
        )
        monkeypatch.setattr(session_runtime, "_preflight", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(session_admission, "admit_start", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            session_admission, "running_project_sandboxes", lambda *_args, **_kwargs: ()
        )
        monkeypatch.setattr(session_runtime.idk, "container_exists", lambda _name: False)
        monkeypatch.setattr(
            session_runtime,
            "_strict_all_interactive_states",
            lambda _project_id: [],
        )
        monkeypatch.setattr(session_runtime, "_create_session_container", lambda *_args: None)
        monkeypatch.setattr(session_runtime, "_run_hook", lambda *_args: None)

        assert session_runtime.up(project).startswith("booley-session-")
    finally:
        authority.state_dir().chmod(0o700)


def test_runtime_spec_rejects_non_mapping_container_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
) -> None:
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    spec["containerEnv"] = []

    with pytest.raises(runtime_spec.RuntimeSpecError, match="containerEnv must be a mapping"):
        runtime_spec.seal(project, spec)


def test_host_eda_issuance_still_requires_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
) -> None:
    del trusted_validator
    project = tmp_path / "project"
    booley_dir = project / ".booley"
    booley_dir.mkdir(parents=True)
    (booley_dir / "booley.toml").write_text(
        '[eda.vivado]\nprovisioning = "host"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        eda_requirements,
        "load_eda_config",
        lambda _project: {"vivado": SimpleNamespace(provisioning="host")},
    )
    monkeypatch.setattr(runtime_spec, "flow_enabled", lambda *_args: True)
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")

    @contextmanager
    def required(_project: Path, host_provisioning: bool):
        assert host_provisioning is True
        raise authority.AuthorityError("host EDA authority was checked")
        yield None, None

    monkeypatch.setattr(authority, "resolve_for_issuance", required)
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)

    with pytest.raises(runtime_spec.RuntimeSpecError, match="host EDA authority was checked"):
        runtime_spec.seal(project, spec)


def test_requested_license_requires_issued_profile_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(name="site")
    monkeypatch.setattr(eda_requirements, "_optional_license", lambda _project: profile)

    assert runtime_spec.requested_license(tmp_path, expected_name="site") is profile
    with pytest.raises(runtime_spec.RuntimeSpecError, match="differs from the issued Sandbox"):
        runtime_spec.requested_license(tmp_path, expected_name="other")


def test_active_image_vivado_cannot_omit_license_marker_to_bypass_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
) -> None:
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setattr(runtime_spec, "flow_enabled", lambda *_args: True)
    monkeypatch.setattr(
        eda_requirements,
        "load_eda_config",
        lambda _project: {"vivado": SimpleNamespace(provisioning="image")},
    )
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")

    @contextmanager
    def required(*_args):
        raise authority.AuthorityError("active Vivado authority was checked")
        yield None, None

    monkeypatch.setattr(authority, "resolve_for_issuance", required)
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)

    with pytest.raises(runtime_spec.RuntimeSpecError, match="active Vivado authority was checked"):
        runtime_spec.seal(project, spec)


@pytest.mark.parametrize("operation", ["issue", "validate"])
def test_issuance_paths_reject_any_missing_generated_bind_source(
    issued, tmp_path: Path, operation: str
) -> None:
    project, spec, path, _stamp = issued
    host_skill = tmp_path / "host-skills" / "example-skill"
    (host_skill / "SKILL.md").unlink()
    host_skill.rmdir()

    with pytest.raises(runtime_spec.RuntimeSpecError) as caught:
        getattr(runtime_spec, operation)(project, spec, path)

    message = str(caught.value)
    assert "generated bind source" in message
    assert docker_mount_path(host_skill) in message
    assert f"{dc.HOST_SKILLS_SIDECAR}/example-skill" in message
    assert "missing" in message


def test_generated_bind_without_source_is_rejected() -> None:
    with pytest.raises(
        runtime_spec.RuntimeSpecError,
        match=r"generated bind source for /missing-source is missing",
    ):
        runtime_spec._validate_bind_sources(["target=/missing-source,type=bind,readonly"])


def test_generated_bind_without_target_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(
        runtime_spec.RuntimeSpecError,
        match=re.escape(f"generated bind target for {tmp_path} is missing"),
    ):
        runtime_spec._validate_bind_sources([f"source={tmp_path},type=bind,readonly"])


@pytest.mark.parametrize("operation", ["issue", "validate"])
def test_missing_project_data_bind_names_source_and_target(issued, operation: str) -> None:
    project, spec, path, _stamp = issued
    source = project / ".booley"
    source.rmdir()

    with pytest.raises(runtime_spec.RuntimeSpecError) as caught:
        getattr(runtime_spec, operation)(project, spec, path)

    message = str(caught.value)
    assert docker_mount_path(source) in message
    assert "/booley-project" in message
    assert "unavailable" in message


@pytest.mark.parametrize("operation", ["issue", "validate"])
def test_missing_vivado_bind_names_source_and_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
    operation: str,
) -> None:
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    vivado_root = tmp_path / "Vivado-2025.2"
    vivado_root.mkdir()
    vivado_source = docker_mount_path(vivado_root)
    installation = authority.Installation(
        "site-vivado",
        "vivado",
        vivado_source,
        "2025.2",
        "linux-x86_64",
        POLICY_REVISION,
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "flow_enabled", lambda *_args: True)
    monkeypatch.setattr(
        eda_requirements,
        "load_eda_config",
        lambda _project: {"vivado": SimpleNamespace(provisioning="host")},
    )
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    monkeypatch.setattr(runtime_spec, "_validate_image_contract", lambda _image: None)

    @contextmanager
    def resolved(*_args):
        yield installation, None

    monkeypatch.setattr(authority, "resolve_for_issuance", resolved)
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        trusted_eda_mounts=((vivado_source, CONTAINER_TARGET),),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    path = dc.write_devcontainer(project, spec)
    runtime_spec.issue(project, spec, path)
    vivado_root.rmdir()

    @contextmanager
    def unavailable(*_args):
        raise authority.InstallationValidationError(
            f"registered Vivado installation failed revalidation: "
            f"Vivado source is unavailable: {vivado_root}"
        )
        yield  # pragma: no cover - makes this an intentionally failing context manager

    monkeypatch.setattr(authority, "resolve_for_issuance", unavailable)

    with pytest.raises(runtime_spec.RuntimeSpecError) as caught:
        getattr(runtime_spec, operation)(project, spec, path)

    message = str(caught.value)
    assert vivado_source in message
    assert CONTAINER_TARGET in message
    assert "unavailable" in message


def test_issuance_records_project_scoped_image_keeper(issued) -> None:
    project, _spec, _path, stamp = issued
    assert stamp.keeper_image == runtime_spec.keeper_image(project)
    assert stamp.keeper_image.startswith("booley-issued-")
    assert stamp.keeper_image.endswith(":session")


def test_validate_rejects_missing_issued_image_keeper(issued, monkeypatch) -> None:
    project, spec, path, stamp = issued

    def resolve(image: str) -> str:
        if image == stamp.keeper_image:
            raise runtime_spec.RuntimeSpecError("missing")
        return stamp.image_id

    monkeypatch.setattr(runtime_spec, "_resolve_image_id", resolve)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="missing"):
        runtime_spec.validate(project, spec, path)


def test_validate_rejects_runtime_image_digest_drift(issued, monkeypatch) -> None:
    project, spec, path, stamp = issued
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:other")

    with pytest.raises(runtime_spec.RuntimeSpecError, match="tag/digest has drifted") as caught:
        runtime_spec.validate(project, spec, path)

    assert f"image_id {stamp.image_id} -> sha256:other" in str(caught.value)
    assert "; image " not in str(caught.value)


def test_validate_rejects_keeper_for_another_project(issued, monkeypatch) -> None:
    project, spec, path, stamp = issued
    monkeypatch.setattr(
        runtime_spec,
        "authenticate",
        lambda *_args: replace(stamp, keeper_image="booley-issued-foreign:session"),
    )

    with pytest.raises(runtime_spec.RuntimeSpecError, match="keeper differs from this Project"):
        runtime_spec.validate(project, spec, path)


def test_validate_rejects_keeper_pointing_at_other_bytes(issued, monkeypatch) -> None:
    project, spec, path, stamp = issued

    def resolve(image: str) -> str:
        return stamp.image_id if image == stamp.image else "sha256:other"

    monkeypatch.setattr(runtime_spec, "_resolve_image_id", resolve)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="keeper points at different bytes"):
        runtime_spec.validate(project, spec, path)


def test_reissuance_moves_keeper_to_new_immutable_image(issued, monkeypatch) -> None:
    _project, _spec, _path, stamp = issued
    old_id = "sha256:" + "a" * 64
    new_id = "sha256:" + "b" * 64
    retained = {stamp.keeper_image: old_id, old_id: old_id, new_id: new_id}
    tags: list[tuple[str, str]] = []

    def resolve(image: str) -> str:
        try:
            return retained[image]
        except KeyError as exc:
            raise runtime_spec.RuntimeSpecError("missing") from exc

    def tag(source: str, target: str) -> None:
        tags.append((source, target))
        retained[target] = retained[source]

    monkeypatch.setattr(runtime_spec, "_resolve_image_id", resolve)
    monkeypatch.setattr("booley.runtime.interactive_docker.tag_image", tag)
    runtime_spec._retain_issued_image(replace(stamp, image=new_id, image_id=new_id))
    assert tags == [(new_id, stamp.keeper_image)]
    assert retained[stamp.keeper_image] == new_id


def test_reissuance_rejects_unverifiable_keeper(issued, monkeypatch) -> None:
    _project, _spec, _path, stamp = issued
    resolutions = iter((runtime_spec.RuntimeSpecError("missing"), "sha256:other"))

    def resolve(_image: str) -> str:
        value = next(resolutions)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(runtime_spec, "_resolve_image_id", resolve)
    monkeypatch.setattr("booley.runtime.interactive_docker.tag_image", lambda *_args: None)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="keeper could not be verified"):
        runtime_spec._retain_issued_image(stamp)


def test_legacy_no_eda_spec_cannot_bypass_issuance(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    spec = dc.build_devcontainer_spec(dc.APP_NONE)
    path = dc.write_devcontainer(project, spec)
    with pytest.raises(runtime_spec.RuntimeSpecError):
        runtime_spec.validate(project, spec, path)


def test_valid_agent_app_is_omitted_from_name_only_drift(issued) -> None:
    _project, spec, _path, _stamp = issued
    spec["remoteEnv"]["BOOLEY_AGENT_APP"] = "codex"
    spec["name"] = "wrong"

    with pytest.raises(runtime_spec.RuntimeSpecError, match="Sandbox identity") as caught:
        runtime_spec._validate_spec_identity(spec)

    assert "name Booley Interactive (codex) -> wrong" in str(caught.value)
    assert "app " not in str(caught.value)


def test_invalid_agent_app_does_not_invent_expected_name(issued) -> None:
    _project, spec, _path, _stamp = issued
    spec["remoteEnv"]["BOOLEY_AGENT_APP"] = "other"

    with pytest.raises(runtime_spec.RuntimeSpecError, match="Sandbox identity") as caught:
        runtime_spec._validate_spec_identity(spec)

    assert "app [claude, codex, none] -> other" in str(caught.value)
    assert "name " not in str(caught.value)


def test_missing_fixed_environment_uses_missing_marker(issued) -> None:
    _project, spec, _path, _stamp = issued
    del spec["remoteEnv"]["HTTP_PROXY"]

    with pytest.raises(runtime_spec.RuntimeSpecError, match="missing fixed") as caught:
        runtime_spec._validate_environment(spec, None)

    assert "HTTP_PROXY present -> <missing>" in str(caught.value)


def test_fixed_environment_drift_names_field_and_values(issued) -> None:
    _project, spec, _path, _stamp = issued
    spec["remoteEnv"]["NO_PROXY"] = "example.test"

    with pytest.raises(runtime_spec.RuntimeSpecError, match="fixed Sandbox") as caught:
        runtime_spec._validate_environment(spec, None)

    assert "NO_PROXY localhost,127.0.0.1 -> example.test" in str(caught.value)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda spec: spec["runArgs"].append("--privileged"), "forbidden host authority"),
        (
            lambda spec: spec["mounts"].insert(
                -1, "source=/var/run/docker.sock,target=/var/run/docker.sock,type=bind"
            ),
            "forbidden host authority",
        ),
        (
            lambda spec: spec["mounts"].insert(-1, "source=/,target=/host,type=bind"),
            "host bind must be read-only",
        ),
        (lambda spec: spec.__setitem__("dockerComposeFile", "compose.yml"), "escape surface"),
        (lambda spec: spec.__setitem__("remoteUser", "root"), "workspace/user"),
        (
            lambda spec: spec.__setitem__("updateRemoteUserUID", True),
            "immutable user policy",
        ),
        (
            lambda spec: spec.__setitem__("initializeCommand", "sh project-script"),
            "validation command",
        ),
        (
            lambda spec: spec.setdefault("remoteEnv", {}).__setitem__(
                "BOOLEY_HOST_MCP_URL", "http://host:19750"
            ),
            "Host MCP environment",
        ),
        (
            lambda spec: spec.setdefault("containerEnv", {}).__setitem__("LD_PRELOAD", "/evil"),
            "unsupported container environment",
        ),
        (
            lambda spec: spec["containerEnv"].pop("BOOLEY_SANDBOX_IMAGE_ID", None),
            "Sandbox Image identity",
        ),
        (
            lambda spec: spec.__setitem__("postStartCommand", "sh /work/owned.sh"),
            "postStartCommand",
        ),
        (
            lambda spec: spec.__setitem__("postStartCommand", dc.mcp_post_start_command()),
            "postStartCommand",
        ),
        (
            lambda spec: spec.__setitem__(
                "postStartCommand",
                f"{dc.git_identity_command()}; {dc.mcp_post_start_command()}",
            ),
            "postStartCommand",
        ),
        (
            lambda spec: spec.__setitem__(
                "postStartCommand",
                f"{dc.mcp_post_start_command()} && {dc.git_identity_command()}",
            ),
            "postStartCommand",
        ),
        (
            lambda spec: spec.__setitem__(
                "postStartCommand",
                f"{dc.git_identity_command()} && sh /work/owned.sh "
                f"&& {dc.mcp_post_start_command()}",
            ),
            "postStartCommand",
        ),
        (
            lambda spec: spec.__setitem__("workspaceMount", "source=/,target=/work,type=bind"),
            "workspace mount",
        ),
    ],
)
def test_dangerous_full_spec_drift_is_rejected_before_issuance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
    mutate,
    match: str,
) -> None:
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    mutate(spec)
    path = dc.write_devcontainer(project, spec)
    with pytest.raises(runtime_spec.RuntimeSpecError, match=match):
        runtime_spec.issue(project, spec, path)


def test_readonly_false_does_not_satisfy_protected_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
) -> None:
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    spec["mounts"][-1] = spec["mounts"][-1].replace("readonly", "readonly=false")
    path = dc.write_devcontainer(project, spec)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="exact read-only"):
        runtime_spec.issue(project, spec, path)


def test_nested_definition_mount_is_final() -> None:
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        trusted_eda_mounts=(("/opt/Xilinx/2025.2", "/opt/booley-eda/vivado"),),
        protected_devcontainer_source="/repo/.devcontainer",
    )
    assert spec["mounts"][-1] == (
        "source=/repo/.devcontainer,target=/work/.devcontainer,type=bind,readonly"
    )


def test_disabled_fpga_does_not_request_host_vivado(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    trusted_validator: Path,
) -> None:
    del trusted_validator
    reset_cache()
    request.addfinalizer(reset_cache)
    project = tmp_path / "project"
    booley_dir = project / ".booley"
    booley_dir.mkdir(parents=True)
    (booley_dir / "booley.toml").write_text(
        """\
[flows.fpga]
enabled = false

[eda.vivado]
provisioning = "host"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("booley.eda.provisioning.configuration.sys.platform", "linux")
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    requested: list[bool] = []

    @contextmanager
    def resolved(_project: Path, host_provisioning: bool):
        requested.append(host_provisioning)
        yield None, None

    monkeypatch.setattr(runtime_spec.authority, "resolve_for_issuance", resolved)
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)

    assert runtime_spec.requested_license(project) is None
    assert requested == []


def test_licensed_seal_gives_vscode_and_headless_the_same_networks_and_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
) -> None:
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    profile = SimpleNamespace(name="site", lmgrd_port=2100)

    @contextmanager
    def resolved(_project: Path, _host_provisioning: bool):
        yield None, profile

    monkeypatch.setattr(runtime_spec.authority, "resolve_for_issuance", resolved)
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
        fixed_container_env={"XILINXD_LICENSE_FILE": "2100@booley-license-xilinx"},
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)

    from booley.eda.provisioning.licensing.flexnet_docker import resources_for_session
    from booley.runtime.session_runtime import docker_run_argv

    expected_networks = {
        dc.EGRESS_NETWORK,
        resources_for_session(str(project.resolve())).private_network,
    }
    run_args = spec["runArgs"]
    spec_networks = {
        run_args[index + 1] for index, item in enumerate(run_args) if item == "--network"
    }
    argv = docker_run_argv(spec, project, "session")
    headless_networks = {argv[index + 1] for index, item in enumerate(argv) if item == "--network"}
    assert spec_networks == headless_networks == expected_networks
    expected_labels = set(
        runtime_spec.labels(
            runtime_spec._issuance(
                project.resolve(),
                spec,
                runtime_spec._spec_digest(spec),
                eda_requirements._requirements(project.resolve(), None, profile),
                file_sha256=None,
            )
        )
    )
    actual_labels = {
        run_args[index + 1] for index, item in enumerate(run_args) if item == "--label"
    }
    assert expected_labels.issubset(actual_labels)


def test_issue_rejects_mutable_image_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
) -> None:
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    spec["containerEnv"]["BOOLEY_SANDBOX_IMAGE_ID"] = spec["image"]
    runtime_spec.seal(project, spec)
    path = dc.write_devcontainer(project, spec)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="image is mutable"):
        runtime_spec.issue(project, spec, path)


@pytest.mark.parametrize("source", ["/", "/home", "/tmp/evil,data"])
def test_project_data_mount_rejects_broad_and_mount_grammar_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setenv("BOOLEY_DIR", source)
    with pytest.raises(
        runtime_spec.RuntimeSpecError,
        match=r"too broad|mount grammar|unavailable|must be an absolute host path",
    ):
        runtime_spec.authorized_project_data_source(project)


def test_project_authored_external_mount_is_not_authority(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    (project / "booley.toml").write_text('[project]\ndir = "/home"\n', encoding="utf-8")
    with pytest.raises(runtime_spec.RuntimeSpecError, match="cannot authorize"):
        runtime_spec.authorized_project_data_source(project)


def test_project_data_mount_rejects_symlink_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    actual = tmp_path / "project-data"
    actual.mkdir()
    linked = tmp_path / "linked-data"
    linked.symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv("BOOLEY_DIR", str(linked))
    with pytest.raises(runtime_spec.RuntimeSpecError, match="symlink"):
        runtime_spec.authorized_project_data_source(project)


def test_project_data_mount_rejects_nested_workspace_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    nested = project / "state" / "data"
    nested.mkdir(parents=True)
    monkeypatch.setenv("BOOLEY_DIR", str(nested))
    with pytest.raises(runtime_spec.RuntimeSpecError, match="overlaps the Project"):
        runtime_spec.authorized_project_data_source(project)


def test_seal_rejects_project_controlled_mount_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
) -> None:
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        booley_dir_source="/home/victim",
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="not authorized"):
        runtime_spec.seal(project, spec)


def test_seal_rejects_workspace_path_poisoned_booley(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = _install_trusted_validator(tmp_path, monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    poisoned = project / ".venv" / "bin" / "booley"
    poisoned.parent.mkdir(parents=True)
    poisoned.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    poisoned.chmod(0o755)
    monkeypatch.setenv("PATH", str(poisoned.parent))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="trusted host Booley"):
        runtime_spec.seal(project, spec)
    assert trusted.is_file()


def test_seal_skips_tmp_path_poisoned_booley(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    poisoned = tmp_path / "untrusted-bin" / "booley"
    poisoned.parent.mkdir()
    poisoned.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    poisoned.chmod(0o755)
    trusted = _install_trusted_validator(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", os.pathsep.join((str(poisoned.parent), str(trusted.parent))))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    assert Path(spec["initializeCommand"][0]) == trusted


def test_finder_uses_current_absolute_entry_point_before_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    invoked = _install_trusted_validator(tmp_path, monkeypatch)
    fallback = tmp_path / "other-prefix" / "bin" / invoked.name
    fallback.parent.mkdir(parents=True)
    fallback.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fallback.chmod(0o755)
    anchors = {
        invoked.parent.resolve(): invoked.parent.parent.resolve(),
        fallback.parent.resolve(): fallback.parent.parent.resolve(),
    }
    monkeypatch.setattr(runtime_spec, "_validator_prefix_anchors", lambda: anchors)
    monkeypatch.setattr(runtime_spec.sys, "argv", [str(invoked)])
    monkeypatch.setenv("PATH", str(fallback.parent))

    assert runtime_spec._find_trusted_validator(project) == invoked


def test_finder_restores_windows_console_launcher_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    prefix = tmp_path / "Python313"
    scripts = prefix / "Scripts"
    executable = scripts / "booley.exe"
    scripts.mkdir(parents=True)
    executable.write_text("launcher", encoding="utf-8")
    executable.chmod(0o755)
    invoked = executable.with_suffix("")
    monkeypatch.setattr(runtime_spec, "_IS_WINDOWS", True)
    monkeypatch.setattr(runtime_spec.sys, "argv", [str(invoked)])
    monkeypatch.setattr(
        runtime_spec,
        "_validator_prefix_anchors",
        lambda: {scripts.resolve(): prefix.resolve()},
    )

    assert not invoked.exists()
    assert runtime_spec._resolve_trusted_validator(executable, project) == executable
    assert runtime_spec._find_trusted_validator(project) == executable


def test_finder_preserves_existing_windows_extensionless_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    prefix = tmp_path / "Python313"
    scripts = prefix / "Scripts"
    invoked = scripts / "booley"
    scripts.mkdir(parents=True)
    invoked.write_text("launcher", encoding="utf-8")
    invoked.chmod(0o755)
    monkeypatch.setattr(runtime_spec, "_IS_WINDOWS", True)
    monkeypatch.setattr(runtime_spec.sys, "argv", [str(invoked)])
    monkeypatch.setattr(
        runtime_spec,
        "_validator_prefix_anchors",
        lambda: {scripts.resolve(): prefix.resolve()},
    )

    assert runtime_spec._resolve_trusted_validator(invoked, project) == invoked
    assert runtime_spec._find_trusted_validator(project) == invoked


def test_normalized_project_launcher_does_not_fall_back_to_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    trusted = _install_trusted_validator(tmp_path, monkeypatch)
    poisoned = project / ".venv" / "Scripts" / "booley.exe"
    poisoned.parent.mkdir(parents=True)
    poisoned.write_text("launcher", encoding="utf-8")
    poisoned.chmod(0o755)
    invoked = poisoned.with_suffix("")
    monkeypatch.setattr(runtime_spec, "_IS_WINDOWS", True)
    monkeypatch.setenv("PATH", str(trusted.parent))

    monkeypatch.setattr(runtime_spec.sys, "argv", ["python"])
    assert runtime_spec._find_trusted_validator(project) == trusted

    monkeypatch.setattr(runtime_spec.sys, "argv", [str(invoked)])
    assert not invoked.exists()
    assert runtime_spec._find_trusted_validator(project) is None


def test_finder_uses_trusted_interpreter_scripts_directory_off_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scripts = tmp_path / "Python313" / ("Scripts" if os.name == "nt" else "bin")
    executable = scripts / ("booley.exe" if os.name == "nt" else "booley")
    scripts.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(runtime_spec.sys, "argv", ["python"])
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(
        runtime_spec,
        "_validator_script_directories",
        lambda: (scripts,),
        raising=False,
    )
    monkeypatch.setattr(
        runtime_spec,
        "_validator_prefix_anchors",
        lambda: {scripts.resolve(): scripts.parent.resolve()},
    )

    assert runtime_spec._find_trusted_validator(project) == executable


def test_finder_preserves_path_precedence_over_interpreter_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    name = "booley.exe" if os.name == "nt" else "booley"
    selected = tmp_path / "selected" / "bin" / name
    fallback = tmp_path / "fallback" / "bin" / name
    for executable in (selected, fallback):
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setattr(runtime_spec.sys, "argv", ["python"])
    monkeypatch.setenv("PATH", str(selected.parent))
    monkeypatch.setattr(
        runtime_spec,
        "_validator_script_directories",
        lambda: (fallback.parent,),
        raising=False,
    )
    monkeypatch.setattr(
        runtime_spec,
        "_validator_prefix_anchors",
        lambda: {
            selected.parent.resolve(): selected.parent.parent.resolve(),
            fallback.parent.resolve(): fallback.parent.parent.resolve(),
        },
    )

    assert runtime_spec._find_trusted_validator(project) == selected


@pytest.mark.skipif(os.name == "nt", reason="uv uses a launcher executable on Windows")
def test_find_trusted_validator_resolves_managed_cli_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    trusted = _install_trusted_validator(tmp_path, monkeypatch)
    launcher = tmp_path / ".local" / "bin" / "booley"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(trusted)
    monkeypatch.setenv("PATH", str(launcher.parent))

    assert runtime_spec._find_trusted_validator(project) == trusted.resolve()
    assert not runtime_spec._trusted_validator(launcher, project)


def test_validator_script_directories_ignore_unavailable_user_scheme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = tmp_path / "scripts"

    def unavailable_user_scheme(_name: str) -> str:
        raise KeyError("user")

    monkeypatch.setattr(runtime_spec.sysconfig, "get_path", lambda *_args, **_kwargs: scripts)
    monkeypatch.setattr(
        runtime_spec.sysconfig,
        "get_preferred_scheme",
        unavailable_user_scheme,
    )

    assert runtime_spec._validator_script_directories() == (scripts.resolve(),)


def test_is_executable_file_accepts_an_executable(tmp_path: Path) -> None:
    executable = tmp_path / "booley"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    assert runtime_spec._is_executable_file(executable)


def test_find_trusted_validator_returns_none_without_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(runtime_spec.sys, "argv", ["python"])
    monkeypatch.setattr(runtime_spec.shutil, "which", lambda _name: None)
    monkeypatch.setattr(runtime_spec, "_validator_script_directories", tuple)
    monkeypatch.setenv("PATH", "")

    assert runtime_spec._find_trusted_validator(project) is None


def test_trusted_validator_rejects_group_writable_executable_for_shared_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    trusted = home / ".local" / "bin" / "booley"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    trusted.chmod(0o775)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_spec, "_private_primary_group", lambda *_args: False)
    assert not runtime_spec._trusted_validator(trusted, project)


def test_trusted_validator_rejects_group_writable_parent_for_shared_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    trusted = home / ".local" / "bin" / "booley"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    trusted.chmod(0o755)
    trusted.parent.chmod(0o775)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_spec, "_private_primary_group", lambda *_args: False)
    assert not runtime_spec._trusted_validator(trusted, project)


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX primary-group mode policy")
def test_trusted_validator_allows_exclusive_primary_group_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    trusted = home / ".local" / "bin" / "booley"
    trusted.parent.mkdir(parents=True)
    trusted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    trusted.chmod(0o775)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_spec, "_private_primary_group", lambda *_args: True)
    assert runtime_spec._trusted_validator(trusted, project)


def test_seal_pins_absolute_host_executable_and_canonical_mount(issued) -> None:
    project, spec, _path, stamp = issued
    command = spec["initializeCommand"]
    assert Path(command[0]).is_absolute()
    assert project not in Path(command[0]).parents
    expected_source = str((project / ".booley").resolve())
    assert stamp.project_data_source == expected_source
    expected_mount_source = docker_mount_path(Path(expected_source))
    assert f"source={expected_mount_source},target=/booley-project,type=bind" in spec["mounts"]
    assert spec["mounts"][1] == (f"source={expected_mount_source},target=/work/.booley,type=bind")
    assert spec["mounts"][-1] == runtime_spec.expected_devcontainer_mount(project)


def test_issue_rejects_missing_project_data_workspace_bind(issued) -> None:
    project, spec, _path, _stamp = issued
    spec["mounts"] = [mount for mount in spec["mounts"] if "target=/work/.booley," not in mount]
    path = dc.write_devcontainer(project, spec)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="workspace view must be pinned"):
        runtime_spec.issue(project, spec, path)


def test_issue_rejects_readonly_project_data_workspace_bind(issued) -> None:
    project, spec, _path, _stamp = issued
    shadow = next(
        index for index, mount in enumerate(spec["mounts"]) if "target=/work/.booley," in mount
    )
    spec["mounts"][shadow] += ",readonly"
    path = dc.write_devcontainer(project, spec)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="exact host-authorized bind"):
        runtime_spec.issue(project, spec, path)


def test_validator_bytes_are_bound_into_issuance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    path = dc.write_devcontainer(project, spec)
    runtime_spec.issue(project, spec, path)
    trusted_validator.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    with pytest.raises(runtime_spec.RuntimeSpecError, match="validator has changed"):
        runtime_spec.validate(project, spec, path)


def test_relay_image_bytes_are_bound_into_licensed_issuance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
) -> None:
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    profile = authority.LicenseProfile(
        "site",
        "flexnet",
        "10.20.30.40",
        "license-server-01",
        2100,
        2101,
    )

    @contextmanager
    def resolved(*_args):
        yield None, profile

    monkeypatch.setattr(authority, "resolve_for_issuance", resolved)
    first = "sha256:" + "a" * 64
    monkeypatch.setattr(eda_requirements, "_relay_image_id", lambda: first)
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
        fixed_container_env={"XILINXD_LICENSE_FILE": "2100@booley-license-xilinx"},
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    path = dc.write_devcontainer(project, spec)
    stamp = runtime_spec.issue(project, spec, path)
    assert stamp.relay_image_id == first
    monkeypatch.setattr(eda_requirements, "_relay_image_id", lambda: "sha256:" + "b" * 64)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="relay image has drifted"):
        runtime_spec.validate(project, spec, path)


def test_validate_rejects_post_issuance_project_data_symlink_swap(issued) -> None:
    project, spec, path, _stamp = issued
    source = project / ".booley"
    replacement = project / "replacement-data"
    source.rename(replacement)
    source.symlink_to(replacement, target_is_directory=True)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="symlink"):
        runtime_spec.validate(project, spec, path)


def _sealed_fresh_spec(project: Path) -> dict:
    """Build, pin and seal a default Sandbox spec for *project*."""
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    return spec


def _link_booley_dir_entry(project: Path, target: Path) -> None:
    """Replace or create ``<project>/.booley`` as a directory symlink to *target*."""
    entry = project / ".booley"
    if entry.is_dir() and not entry.is_symlink():
        entry.rmdir()
    entry.symlink_to(target, target_is_directory=True)


@pytest.mark.skipif(os.name == "nt", reason="directory symlink requires host privilege")
@pytest.mark.parametrize("alias_capable", [False, True], ids=["legacy", "canonical-alias"])
def test_symlinked_booley_dir_entry_issues_the_real_directory_mounts(
    issued, monkeypatch, tmp_path: Path, alias_capable: bool
) -> None:
    """A symlinked ``.booley`` entry issues the same mounts as a real directory."""
    from booley.runtime import project_image

    project, _spec, _path, _stamp = issued
    monkeypatch.setattr(project_image, "project_data_alias_capable", lambda _image: alias_capable)
    entry = project / ".booley"
    (entry / "booley.toml").write_text("[project]\n", encoding="utf-8")
    real_directory_mounts = list(_sealed_fresh_spec(project)["mounts"])

    external = tmp_path / "external-data"
    entry.rename(external)
    _link_booley_dir_entry(project, external)
    spec = _sealed_fresh_spec(project)

    assert spec["mounts"] == real_directory_mounts
    expected_source = docker_mount_path(entry)
    assert f"source={expected_source},target=/work/.booley,type=bind" in spec["mounts"]
    path = dc.write_devcontainer(project, spec)
    stamp = runtime_spec.issue(project, spec, path)
    assert runtime_spec.validate(project, spec, path) == stamp
    assert stamp.project_data_source == str(entry)
    assert stamp.project_data_layout == ("canonical-alias" if alias_capable else "legacy")


@pytest.mark.skipif(os.name == "nt", reason="directory symlink requires host privilege")
@pytest.mark.parametrize(
    ("case", "message"),
    [
        pytest.param("inside-project", "overlaps the Project", id="inside-project"),
        pytest.param("home", "too broad", id="home"),
        pytest.param("link-not-entry", "must not be a symlink", id="link-not-entry"),
        pytest.param("mount-grammar", "mount grammar", id="mount-grammar"),
        pytest.param(
            "target-without-booley-toml", "not a Booley Directory", id="target-without-booley-toml"
        ),
    ],
)
def test_symlinked_booley_dir_entry_keeps_mount_safety_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, message: str
) -> None:
    """Only the exact entry may be a link, and its target keeps every mount check."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    project = tmp_path / ("proj,x" if case == "mount-grammar" else "project")
    project.mkdir()
    booley_files = tmp_path / "booley-files"
    booley_files.mkdir()
    (booley_files / "booley.toml").write_text("[project]\n", encoding="utf-8")
    if case == "inside-project":
        inner = project / "inner"
        inner.mkdir()
        (inner / "booley.toml").write_text("[project]\n", encoding="utf-8")
        _link_booley_dir_entry(project, inner)
    elif case == "home":
        home = tmp_path / "home"
        home.mkdir()
        (home / "booley.toml").write_text("[project]\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        _link_booley_dir_entry(project, home)
    elif case == "target-without-booley-toml":
        # Stand-in for a user-owned directory that is not a Booley Directory.
        unrelated = tmp_path / "unrelated-config"
        unrelated.mkdir()
        _link_booley_dir_entry(project, unrelated)
    else:
        _link_booley_dir_entry(project, booley_files)
    if case == "link-not-entry":
        alias = tmp_path / "alias"
        alias.symlink_to(project, target_is_directory=True)
        monkeypatch.setenv("BOOLEY_DIR", str(alias / ".booley"))
    with pytest.raises(runtime_spec.RuntimeSpecError, match=message):
        runtime_spec.authorized_project_data_source(project)


def test_seal_requires_canonical_hash_scoped_state_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
) -> None:
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    spec = dc.build_devcontainer_spec(
        dc.APP_CLAUDE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="canonical Project"):
        runtime_spec.seal(project, spec)

    scoped = dc.build_devcontainer_spec(
        dc.APP_CLAUDE,
        project_id=dc.canonical_project_id(project),
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(scoped)
    runtime_spec.seal(project, scoped)
    expected = dc.state_volume_mount(dc.APP_CLAUDE, dc.canonical_project_id(project))
    assert expected in scoped["mounts"]


def test_image_contract_is_inspected_without_starting_candidate_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.runtime import interactive_docker

    calls: list[list[str]] = []

    def run(args: list[str], *, timeout: int):
        del timeout
        calls.append(args)
        if args[:2] == ["container", "create"]:
            return subprocess.CompletedProcess(args, 0, "inert-container\n", "")
        if args[:3] == ["container", "cp", "-L"]:
            destination = Path(args[-1])
            source = args[-2].split(":", 1)[1]
            if source.endswith("/vivado"):
                destination.write_bytes(wrapper_path().read_bytes())
            elif source.endswith("locale-archive"):
                destination.write_bytes(b"archive:en_US.utf8")
            else:
                destination.write_bytes(b"\x7fELFfixture")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(interactive_docker, "_run_docker", run)
    runtime_spec._validate_image_contract("sha256:trusted")
    assert not any(call[:2] in (["run", "--rm"], ["container", "start"]) for call in calls)
    create = calls[0]
    assert create[:2] == ["container", "create"]
    assert {"--network", "none", "--read-only", "--cap-drop", "ALL"}.issubset(create)
    assert calls[-1][:3] == ["container", "rm", "-f"]


def test_image_contract_rejects_failed_inert_container_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.runtime import interactive_docker

    monkeypatch.setattr(
        interactive_docker,
        "_run_docker",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "failed"),
    )
    with pytest.raises(runtime_spec.RuntimeSpecError, match="cannot create inert container"):
        runtime_spec._validate_image_contract("sha256:trusted")


def test_extracted_image_contract_rejects_fake_library(tmp_path: Path) -> None:
    (tmp_path / "vivado-wrapper").write_bytes(wrapper_path().read_bytes())
    (tmp_path / "libudev.so.1").write_bytes(b"not-elf")
    (tmp_path / "libpixman-1.so.0").write_bytes(b"\x7fELFfixture")
    (tmp_path / "locale-archive").write_bytes(b"archive:en_US.utf8")
    with pytest.raises(eda_requirements.SessionRequirementsError, match="invalid libudev"):
        eda_requirements.validate_image_observations(tmp_path)


def test_extracted_image_contract_rejects_wrong_wrapper(tmp_path: Path) -> None:
    (tmp_path / "vivado-wrapper").write_bytes(b"wrong")
    with pytest.raises(
        eda_requirements.SessionRequirementsError,
        match="wrong Vivado wrapper digest",
    ):
        eda_requirements.validate_image_observations(tmp_path)


def test_extracted_image_contract_rejects_unreadable_library(tmp_path: Path) -> None:
    (tmp_path / "vivado-wrapper").write_bytes(wrapper_path().read_bytes())
    with pytest.raises(
        eda_requirements.SessionRequirementsError,
        match="cannot inspect Sandbox Image library",
    ):
        eda_requirements.validate_image_observations(tmp_path)


def test_extracted_image_contract_requires_en_us_locale(tmp_path: Path) -> None:
    (tmp_path / "vivado-wrapper").write_bytes(wrapper_path().read_bytes())
    (tmp_path / "libudev.so.1").write_bytes(b"\x7fELFfixture")
    (tmp_path / "libpixman-1.so.0").write_bytes(b"\x7fELFfixture")
    (tmp_path / "locale-archive").write_bytes(b"archive:C.UTF-8")
    with pytest.raises(
        eda_requirements.SessionRequirementsError,
        match="lacks the required en_US",
    ):
        eda_requirements.validate_image_observations(tmp_path)


@pytest.mark.parametrize("section", ["remoteEnv", "containerEnv"])
def test_compiler_cache_identity_is_fixed_and_old_specs_need_refresh(issued, section):
    project, spec, path, _stamp = issued
    assert (
        spec[section]["BOOLEY_COMPILER_CACHE_ROOT"]
        == "/booley-project/.runtime/compiler-cache/ccache"
    )
    spec[section].pop("BOOLEY_COMPILER_CACHE_ROOT")
    with pytest.raises(runtime_spec.RuntimeSpecError):
        runtime_spec.validate(project, spec, path)
    # An old Sandbox authenticates against its own stamp; the environment
    # check must then name the actionable fix, not a generic key diff.
    with pytest.raises(runtime_spec.RuntimeSpecError, match="Compiler-cache root differs"):
        runtime_spec._validate_environment(spec, None)


@pytest.mark.parametrize(
    "target",
    [
        "/booley-project/hidden",
        "//booley-project/hidden",
        "/booley-project/../booley-project/hidden",
    ],
)
def test_canonical_alias_seal_keeps_shadow_and_rejects_alias_subtree_drift(
    issued, monkeypatch, target
):
    from booley.runtime import project_image

    project, _legacy, _path, _stamp = issued
    monkeypatch.setattr(project_image, "project_data_alias_capable", lambda _image: True)
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    assert not any("target=/booley-project," in mount for mount in spec["mounts"])
    expected_source = docker_mount_path(project / ".booley")
    assert f"source={expected_source},target=/work/.booley,type=bind" in spec["mounts"]
    path = dc.write_devcontainer(project, spec)
    stamp = runtime_spec.issue(project, spec, path)
    assert stamp.project_data_layout == "canonical-alias"
    assert runtime_spec.validate(project, spec, path) == stamp
    spec["mounts"].insert(1, f"source={expected_source},target={target},type=bind,readonly")
    path = dc.write_devcontainer(project, spec)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="alias-subtree"):
        runtime_spec.issue(project, spec, path)


@pytest.mark.parametrize("alias_capable", [False, True], ids=["legacy", "canonical-alias"])
def test_previewed_spec_issues_for_every_project_data_layout(
    issued, monkeypatch, alias_capable
) -> None:
    """Init's preview -> issue_prepared round trip must accept its own layout.

    An in-repository Booley Directory on an alias-capable image yields a
    canonical-alias spec; that prepared spec must issue without being mistaken
    for authority drift (the 0.3.0 release-candidate init Step 15 failure).
    """
    from booley.runtime import project_image

    project, _spec, _path, _stamp = issued
    monkeypatch.setattr(project_image, "project_data_alias_capable", lambda _image: alias_capable)
    expected_layout = "canonical-alias" if alias_capable else "legacy"

    def build(inputs: runtime_spec.SessionSpecInputs) -> dict:
        return dc.build_devcontainer_spec(
            dc.APP_NONE,
            mcp_start_command=dc.mcp_post_start_command(),
            booley_dir_source=docker_mount_path(inputs.project_data_source),
            protected_devcontainer_source=str(project / ".devcontainer"),
        )

    prepared = runtime_spec.preview(project, build)
    assert prepared.prospective_issuance is not None
    assert prepared.prospective_issuance.project_data_layout == expected_layout

    stamp = runtime_spec.issue_prepared(project, prepared)

    assert stamp.project_data_layout == expected_layout
    assert runtime_spec.inspect_prepared(project, prepared) == stamp


@pytest.mark.parametrize("key", ["dst", "destination"])
def test_canonical_alias_rejects_nonbind_mount_destination_synonyms(issued, monkeypatch, key):
    from booley.runtime import project_image

    project, _legacy, _path, _stamp = issued
    monkeypatch.setattr(project_image, "project_data_alias_capable", lambda _image: True)
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    spec["mounts"].append(f"type=tmpfs,{key}=//booley-project/hidden")
    path = dc.write_devcontainer(project, spec)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="alias-subtree"):
        runtime_spec.issue(project, spec, path)


def test_sealing_alias_probe_failure_is_a_runtime_spec_error(issued, monkeypatch):
    from booley.runtime import project_image

    project, _legacy, _path, _stamp = issued
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)

    def unavailable(_image):
        raise project_image.DockerImageError("Docker alias probe unavailable")

    monkeypatch.setattr(project_image, "project_data_alias_capable", unavailable)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="alias probe unavailable"):
        runtime_spec.seal(project, spec)


def test_validation_alias_probe_failure_is_a_runtime_spec_error(issued, monkeypatch):
    from booley.runtime import project_image

    project, _legacy, _path, _stamp = issued
    monkeypatch.setattr(project_image, "project_data_alias_capable", lambda _image: True)
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    path = dc.write_devcontainer(project, spec)
    runtime_spec.issue(project, spec, path)

    def unavailable(_image):
        raise project_image.DockerImageError("Docker alias probe unavailable")

    monkeypatch.setattr(project_image, "project_data_alias_capable", unavailable)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="alias probe unavailable"):
        runtime_spec.validate(project, spec, path)


@pytest.mark.parametrize(
    "mount",
    [
        "type=tmpfs,Target=/booley-project/hidden",
        'type=tmpfs,"dst=/booley-project/hidden"',
        "type=tmpfs,target=/safe,dst=/booley-project/hidden",
    ],
)
def test_mount_parser_rejects_noncanonical_or_conflicting_destination_authority(mount):
    with pytest.raises(runtime_spec.RuntimeSpecError):
        runtime_spec._mount_target(mount)


def test_mount_parser_normalizes_source_alias_and_rejects_conflict():
    fields = runtime_spec._mount_fields("src=/owned,target=/safe,type=bind,readonly")
    assert fields["source"] == "/owned"
    with pytest.raises(runtime_spec.RuntimeSpecError, match="conflicting or repeated fields"):
        runtime_spec._mount_fields("source=/owned,src=/other,target=/safe,type=bind,readonly")


def test_mount_surface_rejects_normalized_forbidden_target():
    with pytest.raises(runtime_spec.RuntimeSpecError, match="forbidden host authority"):
        runtime_spec._validate_mount_surfaces(
            ["source=/owned,target=/var/run//docker.sock,type=bind,readonly"], None
        )


def test_issuance_document_accepts_only_legacy_layout_omission(
    issued: tuple[Path, dict, Path, runtime_spec.Issuance],
) -> None:
    from dataclasses import asdict

    _project, _spec, _path, stamp = issued
    raw = asdict(stamp)
    raw.pop("project_data_layout")
    assert runtime_spec.issuance_from_document(raw).project_data_layout == "legacy"
    with pytest.raises(runtime_spec.RuntimeSpecError, match="unexpected or missing fields"):
        runtime_spec.issuance_from_document({**raw, "unknown": "legacy"})
    raw.pop("image_id")
    with pytest.raises(runtime_spec.RuntimeSpecError, match="unexpected or missing fields"):
        runtime_spec.issuance_from_document(raw)


@pytest.mark.parametrize("layout", [[], {}, None, 42, "unknown-layout"])
def test_issuance_document_rejects_malformed_layout_as_spec_error(
    issued: tuple[Path, dict, Path, runtime_spec.Issuance], layout: object
) -> None:
    from dataclasses import asdict

    _project, _spec, _path, stamp = issued
    with pytest.raises(runtime_spec.RuntimeSpecError):
        runtime_spec.issuance_from_document({**asdict(stamp), "project_data_layout": layout})


@pytest.mark.parametrize("section", ["containerEnv", "remoteEnv"])
@pytest.mark.parametrize(
    "key",
    [
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_KEY_1",
        "GIT_CONFIG_VALUE_1",
    ],
)
@pytest.mark.parametrize("drift", [None, "different", "${localEnv:GITHUB_TOKEN}"])
def test_issued_git_environment_rejects_missing_and_drift(issued, section, key, drift):
    _project, spec, _path, stamp = issued
    assert spec["containerEnv"][key] == spec["remoteEnv"][key]
    if drift is None:
        spec[section].pop(key)
    else:
        spec[section][key] = drift
    with pytest.raises(runtime_spec.RuntimeSpecError, match=r"\[agent.git\].*drifted"):
        runtime_spec._validate_git_identity_environment(spec, Path(stamp.project_data_source))


def test_issuance_pins_authoritative_external_identity_and_detects_stale_config(issued, tmp_path):
    _project, spec, _path, _stamp = issued
    (tmp_path / "booley.toml").write_text(
        '[agent.git]\nname="External Agent"\nemail="external@example.invalid"\n'
    )
    runtime_spec._pin_git_identity(spec, tmp_path)
    for section in ("containerEnv", "remoteEnv"):
        assert spec[section]["GIT_CONFIG_VALUE_0"] == "External Agent"
        assert spec[section]["GIT_CONFIG_VALUE_1"] == "external@example.invalid"
    runtime_spec._validate_git_identity_environment(spec, tmp_path)
    (tmp_path / "booley.toml").write_text('[agent.git]\nname="Changed Agent"\n')
    with pytest.raises(runtime_spec.RuntimeSpecError, match="identity has drifted"):
        runtime_spec._validate_git_identity_environment(spec, tmp_path)


def test_old_identity_lifecycle_is_rejected(issued):
    _project, spec, _path, _stamp = issued
    spec["postStartCommand"] = "python -m booley.runtime.incontainer_git_identity"
    with pytest.raises(runtime_spec.RuntimeSpecError, match="postStartCommand"):
        runtime_spec._validate_lifecycle(spec)


def test_seal_pins_external_project_identity_over_builder_defaults(issued, tmp_path, monkeypatch):
    project, _spec, _path, _stamp = issued
    external = tmp_path / "external-data"
    external.mkdir()
    (external / "booley.toml").write_text(
        '[agent.git]\nname="Mounted Agent"\nemail="mounted@example.invalid"\n'
    )
    monkeypatch.setenv("BOOLEY_DIR", str(external))
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE, protected_devcontainer_source=str(project / ".devcontainer")
    )
    assert spec["remoteEnv"]["GIT_CONFIG_VALUE_0"] == "Dev"
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    for section in ("containerEnv", "remoteEnv"):
        assert spec[section]["GIT_CONFIG_VALUE_0"] == "Mounted Agent"
        assert spec[section]["GIT_CONFIG_VALUE_1"] == "mounted@example.invalid"


def test_pipx_symlink_resolves_to_active_persistent_venv(tmp_path, monkeypatch):
    home = tmp_path / "home"
    prefix = home / ".local/share/pipx/venvs/booley-rtl"
    scripts = "Scripts" if os.name == "nt" else "bin"
    name = "booley.exe" if os.name == "nt" else "booley"
    launcher = prefix / scripts / name
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    linked = home / ".local/bin" / name
    linked.parent.mkdir(parents=True)
    linked.symlink_to(launcher)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(runtime_spec.sys, "prefix", str(prefix))
    monkeypatch.setattr(runtime_spec.sys, "base_prefix", "/usr")
    monkeypatch.setattr(runtime_spec.sys, "argv", [str(linked)])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert runtime_spec._find_trusted_validator(project) == launcher.resolve()


def test_missing_launcher_repair_names_canonical_installed_launcher(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_spec, "_find_trusted_validator", lambda _project: None)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="canonical installed Booley launcher"):
        runtime_spec._pin_initialize_command(
            tmp_path, {"initializeCommand": runtime_spec.initialize_command()}
        )


@pytest.mark.parametrize("flag", ["--project", "--project-root"])
def test_initialize_validator_accepts_only_approved_complete_forms(
    tmp_path, monkeypatch, trusted_validator, flag
):
    project = tmp_path / "project"
    project.mkdir()
    command = runtime_spec.initialize_command(str(trusted_validator))
    command[3] = flag
    runtime_spec._validate_initialize_command(project, command)
    for altered in (
        [*command, "extra"],
        [*command[:4], "/untrusted/workspace"],
        ["relative-booley", *command[1:]],
        [command[0], "session", "up", *command[3:]],
    ):
        with pytest.raises(runtime_spec.RuntimeSpecError):
            runtime_spec._validate_initialize_command(project, altered)


@pytest.mark.parametrize("flag", ["--project", "--project-root"])
def test_pinning_migrates_approved_initialize_forms(tmp_path, monkeypatch, flag):
    project = tmp_path / "project"
    command = runtime_spec.initialize_command()
    command[3] = flag
    executable = tmp_path / "trusted/bin/booley"
    monkeypatch.setattr(runtime_spec, "_find_trusted_validator", lambda _project: executable)
    spec = {"initializeCommand": command}
    runtime_spec._pin_initialize_command(project, spec)
    assert spec["initializeCommand"] == runtime_spec.initialize_command(str(executable))
    assert spec["initializeCommand"][3] == "--project"


def test_previously_issued_legacy_prepare_survives_full_validation(
    tmp_path, monkeypatch, trusted_validator
):
    """Model an old issuer, then authenticate its unchanged sealed document."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    original_pin = runtime_spec._pin_initialize_command

    def old_issuer_pin(root, spec):
        original_pin(root, spec)
        spec["initializeCommand"][3] = "--project-root"

    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    with monkeypatch.context() as old_issuer:
        old_issuer.setattr(runtime_spec, "_pin_initialize_command", old_issuer_pin)
        runtime_spec.seal(project, spec)
    path = dc.write_devcontainer(project, spec)
    stamp = runtime_spec.issue(project, spec, path)
    assert spec["initializeCommand"] == [
        str(trusted_validator),
        "session",
        "prepare",
        "--project-root",
        "${localWorkspaceFolder}",
    ]
    assert runtime_spec.authenticate(project, spec, path) == stamp
    assert runtime_spec.validate(project, spec, path) == stamp
    assert runtime_spec.load_issued_snapshot(project) == stamp


def test_project_label_preserves_exact_full_digest_for_deleted_root():
    import hashlib

    stamp = runtime_spec.Issuance(
        version=1,
        project_root="/deleted/Project",
        spec_sha256="a" * 64,
        image="image",
        image_id="sha256:" + "b" * 64,
        keeper_image="keeper",
        policy_revision=1,
        installation=None,
        license_profile=None,
        wrapper_sha256=None,
        relay_image_id=None,
        validator_sha256="c" * 64,
    )
    labels = dict(label.split("=", 1) for label in runtime_spec.labels(stamp))
    assert labels["booley.project-id"] == hashlib.sha256(b"/deleted/Project").hexdigest()
    assert labels["booley.spec-digest"] == "a" * 64


def test_real_git_issued_environment_preserves_host_identity(issued, monkeypatch):
    import sys

    assert _HOST_GIT is not None, "real Git is required for the identity regression"
    project, spec, _path, stamp = issued
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        PATH=_HOST_PATH,
        HOME=str(project.parent),
        GIT_CONFIG_GLOBAL=str(project.parent / "global.gitconfig"),
        GIT_CONFIG_NOSYSTEM="1",
        PYTHONPATH=str(Path(dc.__file__).resolve().parents[2]),
    )

    def git(*args, env=environment):
        return subprocess.run(
            [_HOST_GIT, "-C", str(project), *args],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    git("init", "-q")
    git("config", "--global", "user.name", "Human Global")
    git("config", "--global", "user.email", "global@example.invalid")
    git("config", "user.name", "Human")
    git("config", "user.email", "human@example.invalid")
    sandbox = {
        **environment,
        **spec["containerEnv"],
        "BOOLEY_DIR": stamp.project_data_source,
        "BOOLEY_MCP_MODE": "interactive",
    }
    subprocess.run(
        [sys.executable, *dc.git_identity_command().split()[1:]],
        cwd=project,
        env=sandbox,
        check=True,
        timeout=10,
    )
    for env, expected in (
        (environment, "Human <human@example.invalid>"),
        (sandbox, "Dev <dev@localhost>"),
    ):
        git("-c", "commit.gpgSign=false", "commit", "--allow-empty", "-qm", "identity", env=env)
        assert (
            git("show", "-s", "--format=%an <%ae>|%cn <%ce>", env=env) == f"{expected}|{expected}"
        )
    assert git("config", "user.name") == "Human"
    assert git("config", "--global", "user.name") == "Human Global"


def test_validation_refuses_lost_canonical_alias_capability(issued, monkeypatch):
    from booley.runtime import project_image

    project, _legacy, _path, _stamp = issued
    monkeypatch.setattr(project_image, "project_data_alias_capable", lambda _image: True)
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, spec)
    path = dc.write_devcontainer(project, spec)
    runtime_spec.issue(project, spec, path)
    monkeypatch.setattr(project_image, "project_data_alias_capable", lambda _image: False)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="canonical Booley Directory alias"):
        runtime_spec.validate(project, spec, path)


def test_authorized_directory_source_refuses_an_ordinary_file(issued, monkeypatch, tmp_path):
    project, _spec, _path, _stamp = issued
    source = tmp_path / "not-a-directory"
    source.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("BOOLEY_DIR", str(source.resolve()))
    with pytest.raises(runtime_spec.RuntimeSpecError, match="mount source is not a directory"):
        runtime_spec.authorized_project_data_source(project)
    assert source.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("count", [0, 2])
def test_seal_refuses_missing_or_duplicate_directory_authority(issued, count):
    project, _issued_spec, _path, _stamp = issued
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    mounts = spec["mounts"]
    selected = [raw for raw in mounts if runtime_spec._mount_target(raw) == "/booley-project"]
    assert len(selected) == 1
    spec["mounts"] = [raw for raw in mounts if raw not in selected] + selected * count
    with pytest.raises(runtime_spec.RuntimeSpecError, match="one exact writable bind"):
        runtime_spec.seal(project, spec)


def test_seal_refuses_an_already_mounted_directory_workspace_view(issued):
    project, _issued_spec, _path, _stamp = issued
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    spec["mounts"].append("source=/untrusted,target=/work/.booley,type=bind")
    with pytest.raises(runtime_spec.RuntimeSpecError, match="workspace view is already mounted"):
        runtime_spec.seal(project, spec)


def test_runtime_directory_mount_requires_host_source_authority(issued):
    project, _spec, _path, _stamp = issued
    with pytest.raises(
        runtime_spec.RuntimeSpecError, match="lacks an authorized Booley Directory"
    ):
        runtime_spec._require_project_data_mount([], None, project)


def test_runtime_directory_mount_requires_canonical_source(issued):
    project, _spec, _path, _stamp = issued
    intermediate = project / "existing"
    intermediate.mkdir()
    source = intermediate / ".." / ".booley"
    with pytest.raises(runtime_spec.RuntimeSpecError, match="mount source is no longer canonical"):
        runtime_spec._require_project_data_mount([], str(source), project)


def test_canonical_alias_rejects_external_directory_authority(issued, tmp_path):
    project, _spec, _path, _stamp = issued
    external = tmp_path / "external"
    external.mkdir()
    with pytest.raises(runtime_spec.RuntimeSpecError, match="checkout-local Booley Directory"):
        runtime_spec._require_project_data_mount(
            [], str(external), project, layout="canonical-alias"
        )


@pytest.mark.parametrize("damage", ["missing", "duplicate", "readonly"])
def test_runtime_directory_mount_refuses_inexact_host_bind(issued, damage):
    project, _spec, _path, _stamp = issued
    source = project / ".booley"
    mount = f"source={docker_mount_path(source)},target=/booley-project,type=bind"
    mounts = {"missing": [], "duplicate": [mount, mount], "readonly": [mount + ",readonly"]}[
        damage
    ]
    with pytest.raises(runtime_spec.RuntimeSpecError, match="one exact host-authorized bind"):
        runtime_spec._require_project_data_mount(mounts, str(source), project)


@pytest.mark.parametrize("boundary", ["issuance", "runtime", "lifecycle"])
def test_directory_alias_proof_refuses_missing_image_identity_before_docker(monkeypatch, boundary):
    from booley.runtime import image_lifecycle, project_image

    probe = Mock(side_effect=AssertionError("malformed identity must not reach Docker"))
    monkeypatch.setattr(project_image, "project_data_alias_capable", probe)
    checks = {
        "issuance": (runtime_spec._project_data_alias_capable, runtime_spec.RuntimeSpecError),
        "runtime": (session_runtime._project_data_alias_capable, session_runtime.SessionError),
        "lifecycle": (image_lifecycle._layout_alias_capable, image_lifecycle.ImageLifecycleError),
    }
    check, error = checks[boundary]
    with pytest.raises(error, match="Sandbox image identity is unavailable"):
        check(None)
    probe.assert_not_called()


@pytest.mark.parametrize("section", ["remoteEnv", "containerEnv"])
@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY"])
def test_issued_spec_rejects_unplanned_credential_environment(issued, section, key):
    project, spec, path, _ = issued
    spec[section][key] = f"${{localEnv:{key}}}"
    with pytest.raises(runtime_spec.RuntimeSpecError):
        runtime_spec.validate(project, spec, path)


def test_policy_change_requires_refresh_even_when_shell_exports_change(issued, monkeypatch):
    project, spec, path, _ = issued
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(key, "SYNTHETIC")
    runtime_spec.validate(project, spec, path)
    (project / ".booley" / "booley.toml").write_text('[agent]\nauth="subscription"\n')
    with pytest.raises(runtime_spec.RuntimeSpecError, match=r"credential policy/schema.*refresh"):
        runtime_spec.validate(project, spec, path)


def test_old_credential_schema_requires_refresh(issued):
    project, spec, path, _ = issued
    del spec["remoteEnv"]["BOOLEY_CREDENTIAL_SCHEMA"]
    with pytest.raises(runtime_spec.RuntimeSpecError):
        runtime_spec.validate(project, spec, path)


@pytest.mark.parametrize(
    "damage", ["other-provider", "wrong-placeholder", "subscription-bind", "subscription-hook"]
)
@pytest.mark.parametrize("vivado_requested", [False, True])
def test_sealing_rejects_policy_violations_before_issuance(
    issued, monkeypatch, request, damage, vivado_requested
):
    from booley.runtime import auth_token

    project, _, _, _ = issued
    home = project.parent / "credential-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (project / ".booley" / "booley.toml").write_text(
        '[agent]\nprovider="claude"\nauth="api_key"\n'
        + ('[eda.vivado]\nprovisioning="image"\n' if vivado_requested else "")
    )
    reset_cache()
    request.addfinalizer(reset_cache)
    spec = dc.build_devcontainer_spec(
        "claude",
        credential_plan=auth_token.credential_plan("claude", "api_key"),
        project_id=dc.canonical_project_id(project),
        protected_devcontainer_source=str(project / ".devcontainer"),
        mcp_start_command=dc.mcp_post_start_command(),
    )
    spec["remoteEnv"]["BOOLEY_PRIMARY_AUTH"] = "api_key"
    runtime_spec.pin_image(spec)
    runtime_spec.seal(project, json.loads(json.dumps(spec)))
    if damage == "other-provider":
        spec["remoteEnv"]["OPENAI_API_KEY"] = "${localEnv:OPENAI_API_KEY}"
    elif damage == "wrong-placeholder":
        spec["remoteEnv"]["ANTHROPIC_API_KEY"] = "${localEnv:CLAUDE_CODE_OAUTH_TOKEN}"
    elif damage == "subscription-bind":
        spec["mounts"].insert(
            0,
            "source=/synthetic/login,target=/home/agent/.claude-creds-seed.json,type=bind,readonly",
        )
    else:
        fragment = next(
            fragment
            for fragment in runtime_spec._FIXED_SEED_FRAGMENTS
            if ".claude-creds-seed.json" in fragment
        )
        spec["postStartCommand"] = f"{fragment}; {spec['postStartCommand']}"
    with pytest.raises(runtime_spec.RuntimeSpecError, match=r"credential|seed"):
        runtime_spec.seal(project, spec)


def _mutate_issuance_identity(setting, project, tmp_path, config, monkeypatch):
    if setting == "project-data-source":
        source = tmp_path / "new-trusted-source"
        source.mkdir()
        (source / "booley.toml").write_text(config.read_text())
        monkeypatch.setenv("BOOLEY_DIR", str(source))
        reset_cache()
    if setting == "eda-policy":
        monkeypatch.setattr(
            eda_requirements, "POLICY_REVISION", eda_requirements.POLICY_REVISION + 1
        )
    if setting == "validator":
        validator = runtime_spec._find_trusted_validator(project)
        assert validator is not None
        validator.write_text("#!/bin/sh\nexit 1\n")


def _refresh_source_mutator(project, tmp_path, monkeypatch):
    from booley.harness import init_cmd

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    timezone = {"value": "UTC"}
    monkeypatch.setattr(init_cmd, "detect_host_timezone", lambda: timezone["value"])
    pdk = tmp_path / "pdk"
    pdk.mkdir()
    ready = {"value": False}
    monkeypatch.setattr(init_cmd.nangate_pdk, "is_ready", lambda: ready["value"])
    monkeypatch.setattr(init_cmd.nangate_pdk, "cache_root", lambda: pdk)
    config = project / ".booley" / "booley.toml"
    config.write_text('[agent]\nprovider="claude"\n')

    def mutate(setting):
        changes = {
            "git-name": '[agent.git]\nname="Changed Name"\n',
            "git-email": '[agent.git]\nemail="changed@example.test"\n',
            "provider": '[agent]\nprovider="codex"\n',
            "memory": '[sandbox]\nmemory="8g"\n',
            "masks": '[sandbox]\nmask_paths=["private"]\n',
            "dashboard": "[sandbox]\ndashboard=false\n",
            "skills": "[sandbox]\nmount_host_skills=true\n",
        }
        if setting in changes:
            prefix = "" if setting == "provider" else '[agent]\nprovider="claude"\n'
            config.write_text(prefix + changes[setting])
        if setting == "timezone":
            timezone["value"] = "Asia/Tbilisi"
        files = {
            "auth-bind": home / ".claude" / ".credentials.json",
            "config-seed": home / ".claude.json",
            "token-seed": init_cmd.auth_token.token_path("claude"),
            "skills": home / ".agents" / "skills" / "custom" / "SKILL.md",
        }
        if setting in files:
            target = files[setting]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture")
        if setting == "credential-forwarding":
            monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "SYNTHETIC_REFRESH_OAUTH")
        if setting == "pdk":
            ready["value"] = True
        _mutate_issuance_identity(setting, project, tmp_path, config, monkeypatch)

    return mutate


@pytest.mark.parametrize(
    "setting",
    [
        "git-name",
        "git-email",
        "provider",
        "memory",
        "masks",
        "dashboard",
        "timezone",
        "auth-bind",
        "config-seed",
        "token-seed",
        "credential-forwarding",
        "skills",
        "pdk",
        "project-data-source",
        "eda-policy",
        "validator",
    ],
)
def test_refresh_replaces_spec_drift_without_image_change(issued, monkeypatch, tmp_path, setting):
    from booley.harness.session_refresh import _RuntimeImages

    project, _spec, _path, stamp = issued
    mutate = _refresh_source_mutator(project, tmp_path, monkeypatch)
    image = session_refresh.RefreshImage(
        "booley-sandbox", stamp.image_id, image_graph_changed=False
    )
    initial = _RuntimeImages()
    initial.prepare_spec(project, image)
    initial.reissue(project, image.selected_id, verbose=False)
    current = runtime_spec.load_issued_snapshot(project)
    mutate(setting)
    events = []

    class Images(_RuntimeImages):
        def inspect(self, *_args, **_kwargs):
            pass

        def prepare(self, *_args, **_kwargs):
            return image

        def validate(self, *_args, **_kwargs):
            pass

        def commit(self):
            return image

        def abort(self):
            pass

        def finalize(self, *_args):
            pass

        def reissue(self, *args, **kwargs):
            events.append("reissue")
            super().reissue(*args, **kwargs)

    name = session_runtime.session_container_name(project)
    parked = session_runtime.ParkedSession(
        name,
        name + "-pre-refresh",
        True,
        project_id="project",
        reconnect_egress=False,
        container_id="old",
        image_id=current.image_id,
    )
    monkeypatch.setattr(session_runtime, "strict_conflicting_vscode_session", lambda *_: None)
    monkeypatch.setattr(session_runtime, "plan_session_refresh", lambda *_: parked)
    monkeypatch.setattr(session_runtime, "park_planned_session", lambda *_: events.append("park"))
    monkeypatch.setattr(session_runtime, "verify_refreshed_session", lambda *a, **k: None)
    monkeypatch.setattr(session_runtime, "_up_unlocked", lambda *a, **k: events.append("up"))
    monkeypatch.setattr(session_runtime, "discard_refresh_session", lambda *_: None)
    monkeypatch.setattr(session_runtime, "refresh_container_id", lambda *_: "new", raising=False)
    result = session_refresh.refresh(project, Images())
    assert events == ["park", "reissue", "up"]
    assert result.outcome == "replaced"
    assert result.previous_container_id == "old" and result.container_id == "new"
    reissued = json.loads(dc.devcontainer_path(project).read_text())
    assert runtime_spec.validate(project, reissued, dc.devcontainer_path(project))
    if setting == "git-name":
        assert "Changed Name" in reissued["containerEnv"].values()


def test_refresh_observes_sources_and_preview_once_before_parking(issued, monkeypatch):
    """Preparation builds sources and previews the spec once, then reaches parking."""
    from booley.harness import init_cmd
    from booley.harness.session_refresh import _RuntimeImages

    project, _spec, _path, stamp = issued
    prior = runtime_spec.load_issued_snapshot(project)
    image = session_refresh.RefreshImage(
        "booley-sandbox", stamp.image_id, image_graph_changed=False
    )
    observed = {"sources": 0, "preview": 0}
    build_sources = init_cmd._interactive_spec_sources
    preview = runtime_spec.preview

    def count_sources(*args, **kwargs):
        observed["sources"] += 1
        return build_sources(*args, **kwargs)

    def count_preview(*args, **kwargs):
        observed["preview"] += 1
        return preview(*args, **kwargs)

    class Images(_RuntimeImages):
        def prepare(self, *_args, **_kwargs):
            return image

        def validate(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(init_cmd, "inspect_refreshable_runtime_image", lambda *a, **k: None)
    monkeypatch.setattr(init_cmd, "_interactive_spec_sources", count_sources)
    monkeypatch.setattr(runtime_spec, "preview", count_preview)
    monkeypatch.setattr(session_runtime, "plan_session_refresh", lambda *_: None)
    images = Images()
    images.inspect(project, verbose=False)
    session_refresh._prepare_refresh(project, prior, images, verbose=False)
    assert observed == {"sources": 1, "preview": 1}


def test_refresh_rejects_config_races_without_publishing(issued, monkeypatch):
    from booley.harness.session_refresh import _RuntimeImages

    project, _spec, path, stamp = issued
    adapter = _RuntimeImages()
    image = session_refresh.RefreshImage(
        "booley-sandbox", stamp.image_id, image_graph_changed=False
    )
    adapter.prepare_spec(project, image)
    before = path.read_bytes(), runtime_spec.stamp_path(project).read_bytes()
    config = project / ".booley" / "booley.toml"
    lease = runtime_spec.eda_requirements.lease_requirements

    @contextmanager
    def mutate(*args, **kwargs):
        with lease(*args, **kwargs) as requirements:
            config.write_text('[sandbox]\nmemory="8g"\n')
            yield requirements

    monkeypatch.setattr(runtime_spec.eda_requirements, "lease_requirements", mutate)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="inputs changed"):
        adapter.reissue(project, image.selected_id, verbose=False)
    assert (path.read_bytes(), runtime_spec.stamp_path(project).read_bytes()) == before


@pytest.mark.parametrize("setting", ["provider", "memory", "masks", "skills"])
def test_refresh_builder_uses_trusted_external_booley_directory(
    issued, monkeypatch, tmp_path, setting
):
    from booley.harness import init_cmd
    from booley.harness.session_refresh import _RuntimeImages

    project, _spec, _path, stamp = issued
    external = tmp_path / "trusted-data"
    external.mkdir()
    home = tmp_path / "home"
    skill = home / ".agents" / "skills" / "custom"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("fixture")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("BOOLEY_DIR", str(external))
    reset_cache()
    values = {
        "provider": '[agent]\nprovider="codex"\n',
        "memory": '[sandbox]\nmemory="8g"\n',
        "masks": '[sandbox]\nmask_paths=["private"]\n',
        "skills": "[sandbox]\nmount_host_skills=true\n",
    }
    (external / "pipeline.toml").write_text(values[setting])
    ctx = init_cmd.InitContext(project_root=project, force=False, verbose=False)
    sources = init_cmd._interactive_spec_sources(
        ctx, init_cmd._NANGATE_PDK_NOT_REQUESTED, None, stamp.image_id
    )
    assert sources.project_data_source == external
    assert {
        "provider": sources.app == "codex",
        "memory": sources.memory == "8g",
        "masks": sources.mask_paths == ("private",),
        "skills": bool(sources.host_skills),
    }[setting]
    # The actual preview uses this same authorized source.
    adapter = _RuntimeImages()
    adapter.prepare_spec(project, session_refresh.RefreshImage("booley-sandbox", stamp.image_id))
    monkeypatch.delenv("BOOLEY_DIR")
    reset_cache()
    (project / "booley.toml").write_text('[project]\ndir="' + external.as_posix() + '"\n')
    with pytest.raises(runtime_spec.RuntimeSpecError, match="cannot authorize"):
        init_cmd._interactive_spec_sources(
            ctx, init_cmd._NANGATE_PDK_NOT_REQUESTED, None, stamp.image_id
        )


def test_refresh_dashboard_tasks_unchanged_until_verified_finalization(issued, monkeypatch):
    from booley.harness.session_refresh import _RuntimeImages
    from booley.runtime import dashboard_tasks

    project, _spec, _path, stamp = issued
    reconcile = Mock(side_effect=AssertionError("precommit dashboard mutation"))
    monkeypatch.setattr(dashboard_tasks, "reconcile", reconcile)
    adapter = _RuntimeImages()
    adapter.prepare_spec(project, session_refresh.RefreshImage("booley-sandbox", stamp.image_id))
    adapter.reissue(project, stamp.image_id, verbose=False)
    reconcile.assert_not_called()


@pytest.mark.parametrize("operation", ["add", "revoke"])
def test_real_grant_changes_refuse_refresh_then_allow_authorized_reseed(
    issued, monkeypatch, operation
):
    from booley.harness import init_cmd
    from booley.harness.session_refresh import _RuntimeImages

    project, _spec, _path, stamp = issued
    monkeypatch.setattr(session_refresh, "shared_recovery_blocks_command", lambda **_kw: False)
    monkeypatch.setattr(session_runtime, "strict_conflicting_vscode_session", lambda _root: None)
    monkeypatch.setattr(
        flexnet_docker, "cleanup_project_resources_for_identity", lambda _identity: ()
    )
    profile = eda_grant_support.register_license("site")
    if operation == "add":
        eda_grants.GrantCoordinator().add(
            project, "vivado", installation=None, license_profile=profile.name
        )
    else:
        eda_grant_support.add_grant(project, "vivado", license_profile=profile.name)
        eda_grants.GrantCoordinator().revoke(project, "vivado")
    assert not runtime_spec.stamp_path(project).exists()
    images = Mock(spec=session_refresh.RuntimeImageOperations)
    with pytest.raises(session_runtime.SessionError, match="cannot preserve the prior Sandbox"):
        session_refresh.refresh(project, images)
    images.inspect.assert_not_called()
    images.prepare.assert_not_called()
    # Host init's builder and issuance boundary can reseed using current authority.
    # This Project does not request EDA; a grant alone never mounts or licenses it.
    adapter = _RuntimeImages()
    adapter.prepare_spec(project, session_refresh.RefreshImage("booley-sandbox", stamp.image_id))
    monkeypatch.setattr(runtime_spec, "_current_keeper_id", lambda _project: None)
    adapter.reissue(project, stamp.image_id, verbose=False)
    renewed = runtime_spec.inspect_prepared(project, adapter._spec)
    assert renewed.license_profile is None
    assert renewed.installation is None
    assert init_cmd.authorized_project_data_source(project) == project / ".booley"


def test_licensed_prepared_matching_and_relay_drift_use_full_validation(issued, monkeypatch):
    """Non-withdrawing relay-byte drift; real grant withdrawal is tested separately."""
    from booley.harness.session_refresh import _RuntimeImages

    project, _spec, _path, stamp = issued
    (project / ".booley" / "booley.toml").write_text(
        '[flows.fpga]\nenabled=true\n[eda.vivado]\nprovisioning="image"\n'
    )
    reset_cache()
    profile = eda_grant_support.register_license("site")
    # Initial authenticated fixture, not a real-user add transition.
    eda_grant_support.add_grant(project, "vivado", license_profile=profile.name)
    relay = {"identity": "sha256:" + "d" * 64}
    monkeypatch.setattr(eda_requirements, "ensure_relay_image", lambda **_kwargs: False)
    monkeypatch.setattr(eda_requirements, "_relay_image_id", lambda: relay["identity"])
    adapter = _RuntimeImages()
    image = session_refresh.RefreshImage("booley-sandbox", stamp.image_id)
    adapter.prepare_spec(project, image)
    adapter.reissue(project, stamp.image_id, verbose=False)
    assert adapter.spec_is_current(project)
    before = dc.devcontainer_path(project).read_text(encoding="utf-8")
    relay["identity"] = "sha256:" + "e" * 64
    changed = _RuntimeImages()
    changed.prepare_spec(project, image)
    assert dc.render_devcontainer_json(changed._spec.spec) == before
    assert not changed.spec_is_current(project)


def test_fpga_disabled_prior_license_issuance_detects_withdrawn_grant(issued, monkeypatch):
    """A disabled Flow still opens the lease when the prior issuance named a profile."""
    from booley.harness.session_refresh import _RuntimeImages

    project, _spec, path, stamp = issued
    booley_toml = project / ".booley" / "booley.toml"
    booley_toml.write_text('[flows.fpga]\nenabled=true\n[eda.vivado]\nprovisioning="image"\n')
    reset_cache()
    profile = eda_grant_support.register_license("site")
    eda_grant_support.add_grant(project, "vivado", license_profile=profile.name)
    monkeypatch.setattr(eda_requirements, "ensure_relay_image", lambda **_kwargs: False)
    monkeypatch.setattr(eda_requirements, "_relay_image_id", lambda: "sha256:" + "d" * 64)
    adapter = _RuntimeImages()
    adapter.prepare_spec(project, session_refresh.RefreshImage("booley-sandbox", stamp.image_id))
    adapter.reissue(project, stamp.image_id, verbose=False)

    booley_toml.write_text("[flows.fpga]\nenabled=false\n")
    reset_cache()
    spec = json.loads(path.read_text(encoding="utf-8"))
    assert runtime_spec.validate(project, spec, path).license_profile == "site"

    eda_grant_support.revoke_grant(project)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="XILINXD_LICENSE_FILE"):
        runtime_spec.validate(project, spec, path)


def test_issuance_requirements_opens_lease_for_prior_stamp_without_marker(tmp_path, monkeypatch):
    seen: list[bool] = []

    @contextmanager
    def lease(_project, *, vivado_enabled, prior_eda):
        seen.append(prior_eda)
        yield None

    monkeypatch.setattr(runtime_spec, "flow_enabled", lambda *_args: False)
    monkeypatch.setattr(eda_requirements, "lease_requirements", lease)
    priors = (
        None,
        SimpleNamespace(installation=None, license_profile="site"),
        SimpleNamespace(installation="vivado", license_profile=None),
    )

    for prior in priors:
        with runtime_spec._issuance_requirements(
            tmp_path, {"containerEnv": {}}, prior, include_relay_identity=False
        ):
            pass

    assert seen == [False, True, True]
