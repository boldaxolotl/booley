"""Migrated runtime diagnostic contracts exercised through owning interfaces."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from tests.diagnostic_helpers import (
    _inspection_request,
    _isolate_runtime_files,
    _issued_runtime_state,
    _Rec,
    _record_report,
    _runtime_probe_subprocess,
)

from booley.harness import doctor
from booley.harness.setup import readiness
from booley.runtime import (
    auth_token,
    inspection,
    runtime_context,
    session_runtime,
)
from booley.runtime import devcontainer as dc
from booley.runtime import interactive_docker as idk


def test_host_doctor_rejects_unissued_session_spec(tmp_path, monkeypatch) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    (tmp_path / ".devcontainer").mkdir()
    (tmp_path / ".devcontainer" / "devcontainer.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    project = readiness.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")
    rec = _Rec()

    _record_report(
        inspection.inspect_runtime(_inspection_request(project, "docker")).issuance,
        passed=rec.p,
        skipped=rec.s,
        failed=rec.f,
    )

    assert any("host issuance is invalid" in message for message in rec.fails())


def test_host_doctor_rejects_issued_spec_with_missing_bind_source(tmp_path, monkeypatch) -> None:
    from booley.runtime import session_issuance as runtime_spec

    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    spec_path = tmp_path / ".devcontainer" / "devcontainer.json"
    spec_path.parent.mkdir()
    spec_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)

    def missing_bind(*_args):
        raise runtime_spec.RuntimeSpecError(
            "generated bind source for /home/agent/.booley-host-skills/example-skill "
            "is missing: /host/skills/renamed-skill"
        )

    monkeypatch.setattr(runtime_spec, "validate", missing_bind)
    project = readiness.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")
    rec = _Rec()

    _record_report(
        inspection.inspect_runtime(_inspection_request(project, "docker")).issuance,
        passed=rec.p,
        skipped=rec.s,
        failed=rec.f,
    )

    assert any("example-skill" in message and "missing" in message for message in rec.fails())


def test_host_doctor_accepts_issued_spec_and_no_live_resources(tmp_path, monkeypatch) -> None:
    from booley.runtime import session_issuance as runtime_spec

    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    spec_path = tmp_path / ".devcontainer" / "devcontainer.json"
    spec_path.parent.mkdir()
    spec_path.write_text("{}", encoding="utf-8")
    issuance = runtime_spec.Issuance(
        version=runtime_spec.STAMP_VERSION,
        project_root=str(tmp_path),
        spec_sha256="a" * 64,
        image="sha256:image",
        image_id="sha256:image",
        keeper_image=runtime_spec.keeper_image(tmp_path),
        policy_revision=1,
        installation=None,
        license_profile=None,
        wrapper_sha256=None,
        relay_image_id=None,
        validator_sha256="d" * 64,
        file_sha256="b" * 64,
    )
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(runtime_spec, "validate", lambda *_args: issuance)

    monkeypatch.setattr(subprocess, "run", _runtime_probe_subprocess(""))
    project = readiness.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")
    rec = _Rec()

    _record_report(
        inspection.inspect_runtime(_inspection_request(project, "docker")).issuance,
        passed=rec.p,
        skipped=rec.s,
        failed=rec.f,
    )

    assert not rec.fails()
    assert any(
        "valid host issuance" in message for level, message in rec.events if level == "pass"
    )


@pytest.mark.parametrize(
    "drift",
    [
        "image",
        "workspace-mount",
        "cap-add",
        "privileged",
        "host-pid",
        "host-ipc",
        "device",
        "published-port",
        "extra-security-option",
    ],
)
def test_host_doctor_rejects_full_live_runtime_state_drift(tmp_path, monkeypatch, drift) -> None:
    from booley.runtime import session_issuance as runtime_spec

    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    issuance, spec, labels, state = _issued_runtime_state(tmp_path)
    if drift == "image":
        state["Image"] = "sha256:" + "d" * 64
    elif drift == "workspace-mount":
        state["Mounts"][0]["Source"] = str(tmp_path / "wrong-workspace")
    elif drift == "cap-add":
        state["HostConfig"]["CapAdd"] = ["SYS_ADMIN"]
    elif drift == "privileged":
        state["HostConfig"]["Privileged"] = True
    elif drift == "host-pid":
        state["HostConfig"]["PidMode"] = "host"
    elif drift == "host-ipc":
        state["HostConfig"]["IpcMode"] = "host"
    elif drift == "device":
        state["HostConfig"]["Devices"] = [{"PathOnHost": "/dev/kvm"}]
    elif drift == "published-port":
        state["HostConfig"]["PortBindings"] = {"22/tcp": [{"HostPort": "2222"}]}
    else:
        state["HostConfig"]["SecurityOpt"].append("label=disable")
    spec_path = tmp_path / ".devcontainer" / "devcontainer.json"
    spec_path.parent.mkdir()
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(runtime_spec, "validate", lambda *_args: issuance)

    monkeypatch.setattr(subprocess, "run", _runtime_probe_subprocess("runtime-1\n"))

    def inspect(argv):
        if argv[-1] == "{{json .Config.Labels}}":
            return json.dumps(labels)
        return json.dumps([state])

    monkeypatch.setattr(session_runtime, "_docker_stdout", inspect)
    project = readiness.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")
    rec = _Rec()

    _record_report(
        inspection.inspect_runtime(_inspection_request(project, "docker")).issuance,
        passed=rec.p,
        skipped=rec.s,
        failed=rec.f,
    )

    assert any("state differs from current host issuance" in message for message in rec.fails())


def test_host_doctor_names_stop_first_repair_for_running_old_vscode(
    tmp_path,
    monkeypatch,
) -> None:
    from booley.runtime import session_issuance as runtime_spec

    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    issuance, spec, labels, state = _issued_runtime_state(tmp_path)
    state["Image"] = "sha256:" + "d" * 64
    labels.update(
        {
            "devcontainer.local_folder": str(tmp_path),
            "devcontainer.config_file": str(tmp_path / ".devcontainer" / "devcontainer.json"),
        }
    )
    state["Config"]["Labels"] = labels
    state["State"] = {"Running": True}
    spec_path = tmp_path / ".devcontainer" / "devcontainer.json"
    spec_path.parent.mkdir()
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(runtime_spec, "validate", lambda *_args: issuance)
    calls: list[list[str]] = []
    probe = _runtime_probe_subprocess("runtime-1\n")

    def record_probe(argv, **kwargs):
        calls.append(argv)
        return probe(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", record_probe)

    def inspect(argv):
        if argv[-1] == "{{json .Config.Labels}}":
            return json.dumps(labels)
        return json.dumps([state])

    monkeypatch.setattr(session_runtime, "_docker_stdout", inspect)
    project = readiness.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")
    rec = _Rec()

    _record_report(
        inspection.inspect_runtime(_inspection_request(project, "docker")).issuance,
        passed=rec.p,
        skipped=rec.s,
        failed=rec.f,
    )

    assert any("stop" in fix and "runtime-1" in fix for fix in rec.fix_hints)
    inventory = next(argv for argv in calls if argv[1:3] == ["ps", "-aq"])
    assert inventory[-2:] == ["--format", "{{.Names}}"]


def test_host_doctor_accepts_vscode_managed_runtime_state(tmp_path, monkeypatch) -> None:
    from booley.runtime import session_issuance as runtime_spec

    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    issuance, spec, labels, state = _issued_runtime_state(tmp_path)
    spec["remoteEnv"] = {"BOOLEY_PROJECT_DIR": "/booley-project"}
    labels["devcontainer.local_folder"] = str(tmp_path)
    state["Config"]["Labels"] = labels
    state["Mounts"].extend(
        [
            {"Destination": "/vscode", "Name": "vscode", "Type": "volume", "RW": True},
            {
                "Destination": "/tmp/vscode-wayland-1234-abcd.sock",
                "Source": "/run/user/1000/wayland-0",
                "Type": "bind",
                "RW": True,
            },
        ]
    )
    spec_path = tmp_path / ".devcontainer" / "devcontainer.json"
    spec_path.parent.mkdir()
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(runtime_spec, "validate", lambda *_args: issuance)

    monkeypatch.setattr(subprocess, "run", _runtime_probe_subprocess("runtime-1\n"))

    def inspect(argv):
        if argv[-1] == "{{json .Config.Labels}}":
            return json.dumps(labels)
        return json.dumps([state])

    monkeypatch.setattr(session_runtime, "_docker_stdout", inspect)
    project = readiness.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")
    rec = _Rec()

    _record_report(
        inspection.inspect_runtime(_inspection_request(project, "docker")).issuance,
        passed=rec.p,
        skipped=rec.s,
        failed=rec.f,
    )

    assert not rec.fails()
    assert any("state matches" in message for level, message in rec.events if level == "pass")


def test_in_runtime_doctor_executes_mounted_vivado_policy_branch(tmp_path, monkeypatch) -> None:
    from booley.eda.provisioning.policies import vivado

    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    wrapper_bytes = vivado.wrapper_path().read_bytes()
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text

    def read_bytes(path):
        if path == Path(vivado.WRAPPER_TARGET):
            return wrapper_bytes
        return original_read_bytes(path)

    def read_text(path, *args, **kwargs):
        if path == Path("/proc/self/mountinfo"):
            return "36 25 0:32 / /opt/booley-eda/vivado ro,relatime - ext4 /dev/root ro\n"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    _isolate_runtime_files(monkeypatch)
    original_is_dir = Path.is_dir
    original_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda path: True if path == Path(vivado.CONTAINER_TARGET) else original_is_dir(path),
    )
    compatibility = {
        Path("/usr/lib/x86_64-linux-gnu/libudev.so.1"),
        Path("/usr/lib/x86_64-linux-gnu/libpixman-1.so.0"),
        Path("/usr/lib/locale/locale-archive"),
    }
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda path: True if path in compatibility else original_is_file(path),
    )
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(
        os,
        "access",
        lambda path, mode: path == Path(vivado.CONTAINER_TARGET) / "Vivado" / "bin" / "vivado",
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, f"vivado v{vivado.SUPPORTED_VERSION}\n", ""
        ),
    )
    project = readiness.ProjectAudit(
        tmp_path,
        project_dir,
        {"eda": {"vivado": {"provisioning": "host"}}},
        {},
        "sim",
    )
    rec = _Rec()

    _record_report(
        inspection.inspect_runtime(_inspection_request(project, None)).issuance,
        passed=rec.p,
        skipped=rec.s,
        failed=rec.f,
    )

    assert not rec.fails()
    assert any(
        f"mounted Vivado {vivado.SUPPORTED_VERSION}" in message
        for level, message in rec.events
        if level == "pass"
    )


def test_in_runtime_doctor_does_not_require_vivado_for_disabled_fpga(
    tmp_path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    _isolate_runtime_files(monkeypatch)
    project = readiness.ProjectAudit(
        tmp_path,
        project_dir,
        {
            "eda": {"vivado": {"provisioning": "host"}},
            "flows": {"fpga": {"enabled": False}},
        },
        {},
        "sim",
    )
    rec = _Rec()

    _record_report(
        inspection.inspect_runtime(_inspection_request(project, None)).issuance,
        passed=rec.p,
        skipped=rec.s,
        failed=rec.f,
    )

    assert not rec.fails()
    assert any(
        "no active host-mounted commercial EDA request" in message
        for level, message in rec.events
        if level == "pass"
    )


class TestStateVolumeCheck:
    """_check_interactive_state_volumes surfaces orphans for pruning."""

    @staticmethod
    def _project(root: Path) -> readiness.ProjectAudit:
        pd = root / ".booley_project"
        pd.mkdir(exist_ok=True)
        return readiness.ProjectAudit(
            project_root=root,
            project_dir=pd,
            booley_toml={},
            configs_toml={"x": {}},
            first_target="x",
        )

    def _run(self, tmp_path, monkeypatch, vols, *, verbose=False) -> _Rec:
        from booley.runtime import interactive_docker as idk

        monkeypatch.setattr(idk, "state_volumes", lambda: vols)
        monkeypatch.setattr(idk, "issued_image_tags", lambda: [])
        rec = _Rec()
        _record_report(
            inspection.inspect_retained_resources(self._project(tmp_path).project_root, "docker"),
            passed=rec.p,
            noted=rec.w,
            skipped=rec.s,
        )
        return rec

    def test_skips_without_runtime(self, tmp_path):
        rec = _Rec()
        _record_report(
            inspection.inspect_retained_resources(self._project(tmp_path).project_root, None),
            passed=rec.p,
            noted=rec.w,
            skipped=rec.s,
        )
        assert rec.kinds() == {"skip"}

    def test_passes_when_no_volumes(self, tmp_path, monkeypatch):
        rec = self._run(tmp_path, monkeypatch, [])
        assert rec.kinds() == {"pass"}
        assert any("no persistent" in m for _, m in rec.events)

    def test_recognizes_this_projects_volume(self, tmp_path, monkeypatch):
        project_id = dc.canonical_project_id(tmp_path)
        mine = f"booley-claude-state-{project_id}"
        rec = self._run(tmp_path, monkeypatch, [mine])
        assert rec.fails() == []
        assert any(mine in m for lvl, m in rec.events if lvl == "pass")
        assert "warn" not in rec.kinds()

    def test_flags_other_projects_as_prunable(self, tmp_path, monkeypatch):
        project_id = dc.canonical_project_id(tmp_path)
        rec = self._run(
            tmp_path,
            monkeypatch,
            [
                f"booley-claude-state-{project_id}",  # mine
                "booley-codex-state-someoldproject",  # orphan
                "booley-claude-state-anotherproject",  # orphan
            ],
        )
        warns = [m for lvl, m in rec.events if lvl == "warn"]
        assert len(warns) == 1
        assert "2 interactive state volume(s) from other projects" in warns[0]
        assert "docker volume rm" in warns[0]

    def test_verbose_lists_orphans(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            inspection.idk, "state_volumes", lambda: ["booley-codex-state-someoldproject"]
        )
        monkeypatch.setattr(inspection.idk, "issued_image_tags", lambda: [])
        result = inspection.inspect_retained_resources(tmp_path, "docker")
        reporter = doctor._Reporter.create(verbose=True)
        reporter.diagnostics(result)
        assert "booley-codex-state-someoldproject" in capsys.readouterr().out


class TestDevcontainerSpecStaleness:
    """_check_devcontainer_spec must surface a spec that loses history on rebuild."""

    def _run(
        self,
        root: Path,
        monkeypatch,
        image: str | None = None,
        declared_provider: str | None = None,
    ) -> _Rec:
        from booley.runtime import devcontainer as dc

        # Isolate from git; tracking is exercised by other tests.
        # Real temporary-directory Git inspection reports no tracked spec.
        # Isolate from the developer machine's own `booley auth` store — the
        # token-seed drift branch reads it, and a real stored token would flip
        # the fresh-spec tests below from pass to warn.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg-isolated"))
        monkeypatch.setattr(idk, "image_id", lambda image: image)
        rec = _Rec()
        _record_report(
            inspection.inspect_runtime(
                inspection.RuntimeInspectionRequest(
                    root,
                    image or dc.SANDBOX_IMAGE,
                    None,
                    declared_provider=declared_provider,
                    expected_cache_mount=doctor.nangate_pdk.CONTAINER_ROOT
                    if doctor.nangate_pdk.is_ready()
                    else None,
                )
            ).configuration,
            passed=rec.p,
            warned=rec.w,
            failed=rec.f,
            noted=rec.n,
        )
        return rec

    def test_fresh_claude_spec_passes(self, tmp_path, monkeypatch):
        from booley.runtime import devcontainer as dc

        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CLAUDE))
        rec = self._run(tmp_path, monkeypatch)
        assert rec.kinds() == {"pass"}

    def test_verified_pdk_without_spec_mount_warns(self, tmp_path, monkeypatch):
        from booley.runtime import devcontainer as dc

        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CLAUDE))
        monkeypatch.setattr(doctor.nangate_pdk, "is_ready", lambda: True)

        rec = self._run(tmp_path, monkeypatch)

        assert rec.fails() == []
        assert any(
            level == "warn" and "/opt/pdk" in message and "synthesis" in message
            for level, message in rec.events
        )

    def test_verified_pdk_with_spec_mount_passes(self, tmp_path, monkeypatch):
        from booley.runtime import devcontainer as dc

        spec = dc.build_devcontainer_spec(
            dc.APP_CLAUDE,
            trusted_eda_mounts=(("/host/pdk", "/opt/pdk"),),
        )
        dc.write_devcontainer(tmp_path, spec)
        monkeypatch.setattr(doctor.nangate_pdk, "is_ready", lambda: True)

        rec = self._run(tmp_path, monkeypatch)

        assert rec.kinds() == {"pass"}

    def test_image_drift_warns_not_fails(self, tmp_path, monkeypatch):
        # Spec frozen on the base image while [sandbox].image now names a custom
        # project image (extra toolchain) — the openc910/Xuantie blocker shape.
        from booley.runtime import devcontainer as dc

        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CLAUDE))
        rec = self._run(tmp_path, monkeypatch, image="openc910-booley-sandbox:latest")
        assert rec.fails() == []
        assert any(
            lvl == "warn" and "openc910-booley-sandbox:latest" in m and "--seed" in m
            for lvl, m in rec.events
        )

    def test_matching_custom_image_passes(self, tmp_path, monkeypatch):
        # Spec built for the same custom image the project configures: no drift.
        from booley.runtime import devcontainer as dc

        spec = dc.build_devcontainer_spec(
            dc.APP_CLAUDE,
            image="openc910-booley-sandbox:latest",
        )
        dc.write_devcontainer(tmp_path, spec)
        rec = self._run(tmp_path, monkeypatch, image="openc910-booley-sandbox:latest")
        assert rec.kinds() == {"pass"}

    def test_immutable_image_pin_is_not_stale_when_resolution_unavailable(
        self, tmp_path, monkeypatch
    ):
        # Inside an issued Sandbox Docker is intentionally absent. The
        # spec is already pinned to an immutable ID, but the configured tag
        # cannot be resolved there; string-comparing the ID to the tag would be
        # a false stale-image warning.
        immutable_id = "sha256:" + "a" * 64
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE, image=immutable_id)
        dc.write_devcontainer(tmp_path, spec)
        # Real temporary-directory Git inspection reports no tracked spec.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-isolated"))
        monkeypatch.setattr(idk, "image_id", lambda image: None)
        rec = _Rec()

        _record_report(
            inspection.inspect_runtime(
                inspection.RuntimeInspectionRequest(
                    tmp_path,
                    dc.SANDBOX_IMAGE,
                    None,
                    declared_provider=None,
                    expected_cache_mount=doctor.nangate_pdk.CONTAINER_ROOT
                    if doctor.nangate_pdk.is_ready()
                    else None,
                )
            ).configuration,
            passed=rec.p,
            warned=rec.w,
            failed=rec.f,
            noted=rec.n,
        )

        assert rec.fails() == []
        assert not any(level == "warn" for level, _ in rec.events)
        assert any(
            level == "note" and "host `booley doctor` is authoritative" in message
            for level, message in rec.events
        )

    def test_immutable_image_pin_mismatch_warns_when_resolution_succeeds(
        self, tmp_path, monkeypatch
    ):
        old_id = "sha256:" + "a" * 64
        current_id = "sha256:" + "b" * 64
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE, image=old_id)
        dc.write_devcontainer(tmp_path, spec)
        # Real temporary-directory Git inspection reports no tracked spec.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-isolated"))
        monkeypatch.setattr(idk, "image_id", lambda image: current_id)
        rec = _Rec()

        _record_report(
            inspection.inspect_runtime(
                inspection.RuntimeInspectionRequest(
                    tmp_path,
                    dc.SANDBOX_IMAGE,
                    None,
                    declared_provider=None,
                    expected_cache_mount=doctor.nangate_pdk.CONTAINER_ROOT
                    if doctor.nangate_pdk.is_ready()
                    else None,
                )
            ).configuration,
            passed=rec.p,
            warned=rec.w,
            failed=rec.f,
            noted=rec.n,
        )

        assert rec.fails() == []
        assert any(
            level == "warn" and old_id in message and dc.SANDBOX_IMAGE in message
            for level, message in rec.events
        )

    def test_agent_app_drift_fails(self, tmp_path, monkeypatch):
        # The picorv32 shape, hit live 2026-07-27: the project switched to
        # `[agent] provider = "codex"` long after seeding, so the untracked spec
        # still said claude. incontainer_register then wrote the Booley MCP
        # entry into ~/.claude.json while the Codex session — the only agent
        # actually running — saw no Booley MCP tools at all.
        from booley.runtime import devcontainer as dc

        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CLAUDE))
        rec = self._run(tmp_path, monkeypatch, declared_provider=dc.APP_CODEX)
        assert any("BOOLEY_AGENT_APP" in m and "codex" in m for m in rec.fails())
        assert not any(lvl == "warn" for lvl, _ in rec.events)

    def test_agent_app_matching_declared_provider_passes(self, tmp_path, monkeypatch):
        from booley.runtime import devcontainer as dc

        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CODEX))
        rec = self._run(tmp_path, monkeypatch, declared_provider=dc.APP_CODEX)
        assert rec.kinds() == {"pass"}

    def test_undeclared_provider_mutes_the_app_drift_warn(self, tmp_path, monkeypatch):
        # No [agent] provider: the seeder falls back to host detection, so
        # there is nothing the on-disk app can be drift-checked against.
        from booley.runtime import devcontainer as dc

        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CLAUDE))
        rec = self._run(tmp_path, monkeypatch, declared_provider=None)
        assert rec.kinds() == {"pass"}

    def test_agent_app_drift_reported_over_missing_state_volume(self, tmp_path, monkeypatch):
        # A mismatched spec still mounts a volume for the app it *names*, so the
        # persistence check would pass and hide the real problem. Order matters:
        # the app drift must be what the user is told to fix.
        from booley.runtime import devcontainer as dc

        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        assert dc.spec_state_is_persisted(spec) is True  # the misleading "all good"
        dc.write_devcontainer(tmp_path, spec)
        rec = self._run(tmp_path, monkeypatch, declared_provider=dc.APP_CODEX)
        assert rec.fails() and all("BOOLEY_AGENT_APP" in m for m in rec.fails())
        assert not any(lvl == "warn" for lvl, _ in rec.events)

    def test_stale_claude_spec_warns_not_fails(self, tmp_path, monkeypatch):
        from booley.runtime import devcontainer as dc

        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        spec["mounts"] = [m for m in spec["mounts"] if "type=volume" not in m]
        dc.write_devcontainer(tmp_path, spec)
        rec = self._run(tmp_path, monkeypatch)
        assert rec.fails() == []
        assert any(lvl == "warn" and "stale" in m for lvl, m in rec.events)

    def test_missing_spec_warns_run_init(self, tmp_path, monkeypatch):
        rec = self._run(tmp_path, monkeypatch)
        assert any("no .devcontainer" in m for _, m in rec.events)

    def test_stored_token_without_seed_mount_warns(self, tmp_path, monkeypatch):
        # A credential stored AFTER the spec was seeded: VS Code sessions can't
        # see it (no sidecar mount), so they silently run on the refreshing
        # credential — surface the drift, don't fail.
        from booley.runtime import devcontainer as dc

        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CLAUDE))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        auth_token.store_token("sk-ant-oat01-stored")
        rec = _Rec()
        # Real temporary-directory Git inspection reports no tracked spec.
        monkeypatch.setattr(idk, "image_id", lambda image: image)
        _record_report(
            inspection.inspect_runtime(
                inspection.RuntimeInspectionRequest(
                    tmp_path,
                    dc.SANDBOX_IMAGE,
                    None,
                    declared_provider=None,
                    expected_cache_mount=doctor.nangate_pdk.CONTAINER_ROOT
                    if doctor.nangate_pdk.is_ready()
                    else None,
                )
            ).configuration,
            passed=rec.p,
            warned=rec.w,
            failed=rec.f,
            noted=rec.p,
        )
        assert rec.fails() == []
        assert any(lvl == "warn" and "booley auth" in m and "--seed" in m for lvl, m in rec.events)

    def test_stored_token_with_seed_mount_passes(self, tmp_path, monkeypatch):
        from booley.runtime import devcontainer as dc

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        path = auth_token.store_token("sk-ant-oat01-stored")
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE, token_seed_source=str(path))
        dc.write_devcontainer(tmp_path, spec)
        rec = _Rec()
        # Real temporary-directory Git inspection reports no tracked spec.
        monkeypatch.setattr(idk, "image_id", lambda image: image)
        _record_report(
            inspection.inspect_runtime(
                inspection.RuntimeInspectionRequest(
                    tmp_path,
                    dc.SANDBOX_IMAGE,
                    None,
                    declared_provider=None,
                    expected_cache_mount=doctor.nangate_pdk.CONTAINER_ROOT
                    if doctor.nangate_pdk.is_ready()
                    else None,
                )
            ).configuration,
            passed=rec.p,
            warned=rec.w,
            failed=rec.f,
            noted=rec.p,
        )
        assert rec.kinds() == {"pass"}

    def test_pre_adr_0035_spec_without_vaporview_warns(self, tmp_path, monkeypatch):
        # A spec seeded before the Waveform Viewer landed installs no VaporView
        # and pins no WCP settings; an image rebuild never fixes that, so the
        # agent's scoped `bwave gui` fails in every session — surface it.
        # Exact shape hit live on a real project 2026-07-14.
        from booley.runtime import devcontainer as dc

        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        spec["customizations"]["vscode"]["extensions"] = ["Anthropic.claude-code"]
        for key in ("vaporview.wcp.enabled", "vaporview.wcp.port"):
            del spec["customizations"]["vscode"]["settings"][key]
        dc.write_devcontainer(tmp_path, spec)
        rec = self._run(tmp_path, monkeypatch)
        assert rec.fails() == []
        assert any(lvl == "warn" and "VaporView" in m and "--seed" in m for lvl, m in rec.events)

    def test_spec_without_hdl_highlight_is_a_note(self, tmp_path, monkeypatch):
        # A spec seeded before the SystemVerilog highlighting extension landed
        # renders RTL as plain text in attached windows; extensions are
        # spec-delivered (never image-baked), so only a re-seed fixes it.
        from booley.runtime import devcontainer as dc

        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        spec["customizations"]["vscode"]["extensions"] = [
            "Anthropic.claude-code",
            "lramseyer.vaporview",
        ]
        dc.write_devcontainer(tmp_path, spec)
        rec = self._run(tmp_path, monkeypatch)
        assert rec.fails() == []
        assert any(lvl == "note" and "highlighting" in m for lvl, m in rec.events)

    def test_spec_without_live_preview_warns(self, tmp_path, monkeypatch):
        # Live Preview is spec-delivered, so a project seeded before it landed
        # cannot render review HTML in its attached container window.

        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        spec["customizations"]["vscode"]["extensions"].remove("ms-vscode.live-server")
        dc.write_devcontainer(tmp_path, spec)
        rec = self._run(tmp_path, monkeypatch)
        assert rec.fails() == []
        assert any(lvl == "warn" and "Live Preview" in m for lvl, m in rec.events)

    def test_spec_restoring_live_preview_ports_warns(self, tmp_path, monkeypatch):
        # F-14: an otherwise current spec can restore dead 3000/3001 tunnels
        # before Live Preview starts and leave the report preview blank.
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        spec["customizations"]["vscode"]["settings"]["remote.restoreForwardedPorts"] = True
        dc.write_devcontainer(tmp_path, spec)
        rec = self._run(tmp_path, monkeypatch)
        assert rec.fails() == []
        assert any(
            lvl == "warn" and "collision-safe Live Preview" in m and "--seed" in m
            for lvl, m in rec.events
        )

    def test_spec_without_live_preview_port_randomizer_warns(self, tmp_path, monkeypatch):
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        spec["postAttachCommand"] = dc.vaporview_patch_command()
        dc.write_devcontainer(tmp_path, spec)
        rec = self._run(tmp_path, monkeypatch)
        assert rec.fails() == []
        assert any(
            lvl == "warn" and "collision-safe Live Preview" in m and "--seed" in m
            for lvl, m in rec.events
        )

    def test_spec_with_python_terminal_autoactivation_is_a_note(self, tmp_path, monkeypatch):
        # Existing specs should direct users to re-seed so a Settings-Synced
        # Python extension stops injecting delayed activation commands.
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        for key in dc._PYTHON_TERMINAL_SETTINGS:
            del spec["customizations"]["vscode"]["settings"][key]
        dc.write_devcontainer(tmp_path, spec)
        rec = self._run(tmp_path, monkeypatch)
        assert rec.fails() == []
        assert any(
            lvl == "note" and "Python terminal activation" in m and "--seed" in m
            for lvl, m in rec.events
        )
