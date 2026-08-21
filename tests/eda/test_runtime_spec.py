"""Universal host-issued Session Runtime specification tests."""

from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.eda import authority, runtime_spec
from booley.eda.vivado import wrapper_path
from booley.harness import devcontainer as dc
from booley.runtime.platform_paths import docker_mount_path
from booley.runtime.project_dir import reset_cache


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


@pytest.fixture
def issued(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
):
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
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
    stamp = runtime_spec.issue(project, spec, path)
    return project, spec, path, stamp


def test_every_project_requires_exact_host_stamp(issued) -> None:
    project, spec, path, stamp = issued
    assert runtime_spec.validate(project, spec, path) == stamp
    runtime_spec.stamp_path(project).unlink()
    with pytest.raises(runtime_spec.RuntimeSpecError, match="missing or corrupt"):
        runtime_spec.validate(project, spec, path)


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
    monkeypatch.setattr("booley.harness.interactive_docker.tag_image", tag)
    runtime_spec._retain_issued_image(replace(stamp, image=new_id, image_id=new_id))
    assert tags == [(new_id, stamp.keeper_image)]
    assert retained[stamp.keeper_image] == new_id


def test_legacy_no_eda_spec_cannot_bypass_issuance(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
    spec = dc.build_devcontainer_spec(dc.APP_NONE)
    path = dc.write_devcontainer(project, spec)
    with pytest.raises(runtime_spec.RuntimeSpecError):
        runtime_spec.validate(project, spec, path)


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
            lambda spec: spec.__setitem__("postStartCommand", "sh /work/owned.sh"),
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
    (project / ".booley_project").mkdir()
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
    (project / ".booley_project").mkdir()
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
    project_dir = project / ".booley_project"
    project_dir.mkdir(parents=True)
    (project_dir / "booley.toml").write_text(
        """\
[flows.fpga]
enabled = false

[eda.vivado]
provisioning = "host"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("booley.eda.config.sys.platform", "linux")
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

    config, installation = runtime_spec.requested_host_installation(project)
    assert config is not None
    assert installation is None
    assert runtime_spec.requested_license(project) is None
    assert requested == [False]


def test_licensed_seal_gives_vscode_and_headless_the_same_networks_and_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
) -> None:
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
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

    from booley.eda.flexnet_docker import resources_for_session
    from booley.harness.session_runtime import docker_run_argv

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
                None,
                profile,
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
    (project / ".booley_project").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
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
    (project / ".booley_project").mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", source)
    with pytest.raises(
        runtime_spec.RuntimeSpecError,
        match=r"too broad|mount grammar|unavailable|must be an absolute host path",
    ):
        runtime_spec.authorized_project_data_source(project)


def test_project_authored_external_mount_is_not_authority(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
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
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(linked))
    with pytest.raises(runtime_spec.RuntimeSpecError, match="symlink"):
        runtime_spec.authorized_project_data_source(project)


def test_project_data_mount_rejects_nested_workspace_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    nested = project / "state" / "data"
    nested.mkdir(parents=True)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(nested))
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
    (project / ".booley_project").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:image")
    spec = dc.build_devcontainer_spec(
        dc.APP_NONE,
        project_dir_source="/home/victim",
        mcp_start_command=dc.mcp_post_start_command(),
        protected_devcontainer_source=str(project / ".devcontainer"),
    )
    runtime_spec.pin_image(spec)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="not authorized"):
        runtime_spec.seal(project, spec)


def test_seal_rejects_workspace_path_poisoned_booley(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
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


def test_seal_skips_tmp_path_poisoned_booley(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
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
    expected_source = str((project / ".booley_project").resolve())
    assert stamp.project_data_source == expected_source
    expected_mount_source = docker_mount_path(Path(expected_source))
    assert f"source={expected_mount_source},target=/booley-project,type=bind" in spec["mounts"]
    assert spec["mounts"][1] == (
        f"source={expected_mount_source},target=/work/.booley_project,type=bind"
    )
    assert spec["mounts"][-1] == runtime_spec.expected_devcontainer_mount(project)


def test_issue_rejects_missing_project_data_workspace_bind(issued) -> None:
    project, spec, _path, _stamp = issued
    spec["mounts"] = [
        mount for mount in spec["mounts"] if "target=/work/.booley_project," not in mount
    ]
    path = dc.write_devcontainer(project, spec)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="workspace view must be pinned"):
        runtime_spec.issue(project, spec, path)


def test_issue_rejects_readonly_project_data_workspace_bind(issued) -> None:
    project, spec, _path, _stamp = issued
    shadow = next(
        index
        for index, mount in enumerate(spec["mounts"])
        if "target=/work/.booley_project," in mount
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
    (project / ".booley_project").mkdir()
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
    (project / ".booley_project").mkdir()
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
    monkeypatch.setattr(runtime_spec, "_relay_image_id", lambda _profile: first)
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
    monkeypatch.setattr(runtime_spec, "_relay_image_id", lambda _profile: "sha256:" + "b" * 64)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="relay image has drifted"):
        runtime_spec.validate(project, spec, path)


def test_validate_rejects_post_issuance_project_data_symlink_swap(issued) -> None:
    project, spec, path, _stamp = issued
    source = project / ".booley_project"
    replacement = project / "replacement-data"
    source.rename(replacement)
    source.symlink_to(replacement, target_is_directory=True)
    with pytest.raises(runtime_spec.RuntimeSpecError, match="symlink"):
        runtime_spec.validate(project, spec, path)


def test_seal_requires_canonical_hash_scoped_state_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_validator: Path,
) -> None:
    del trusted_validator
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley_project").mkdir()
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
    from booley.harness import interactive_docker

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


def test_extracted_image_contract_rejects_fake_library(tmp_path: Path) -> None:
    (tmp_path / "vivado-wrapper").write_bytes(wrapper_path().read_bytes())
    (tmp_path / "libudev.so.1").write_bytes(b"not-elf")
    (tmp_path / "libpixman-1.so.0").write_bytes(b"\x7fELFfixture")
    (tmp_path / "locale-archive").write_bytes(b"archive:en_US.utf8")
    with pytest.raises(runtime_spec.RuntimeSpecError, match="invalid libudev"):
        runtime_spec._validate_extracted_image_contract(tmp_path)
