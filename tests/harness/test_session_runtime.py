"""Tests for session_runtime: headless Sandbox lifecycle (F-4).

The spec -> `docker run` translation is the part that must not drift from
`devcontainer.build_devcontainer_spec`, so it is tested against a real spec
rather than a hand-written dict.
"""

from __future__ import annotations

import json
import logging
import ntpath
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import Mock

import pytest

from booley.runtime import devcontainer as dc
from booley.runtime import health, issuance_invalidation, platform_paths
from booley.runtime import session_issuance as runtime_spec
from booley.runtime import session_runtime as sr
from booley.runtime.sandbox_apply import Attachment
from booley.runtime.session_admission import (
    admit_start as real_admit_start,
)
from booley.runtime.session_admission import (
    claim_vscode_start as real_claim_vscode_start,
)
from booley.runtime.session_admission import (
    clear_vscode_claim as real_clear_vscode_claim,
)
from booley.runtime.session_admission import (
    has_pending_claim as real_has_pending_claim,
)
from booley.runtime.session_admission import (
    running_project_sandboxes as real_running_project_sandboxes,
)
from booley.runtime.session_admission import (
    vscode_sandboxes as real_vscode_sandboxes,
)
from booley.runtime.session_refresh import RefreshImage
from tests.devcontainer_spec_support import build_spec
from tests.docker_cli_guard import assert_docker_cli_unused, forbid_docker_cli
from tests.harness.booley_dir_support import select_booley_dir
from tests.harness.session_docker_fake import (
    SandboxDocker,
    attach_commands,
    completed,
    headless_session_entry,
    isolate_session_cli,
    run_cli,
    run_registrar_on_post_start,
    sandbox_exec_on_path,
    serve_sandbox_docker,
)
from tests.lifecycle_lock_support import held_lifecycle_lock, observe_lifecycle_contention
from tests.pipe_holding_tree import (
    heartbeat_tree,
    parent_argv,
    start_deadline_after_tree_start,
    wait_until_heartbeat_stops,
)
from tests.subprocess_budget import run_bounded_child
from tests.t3code_fake import TOKEN, echo_listener, serve_t3_code
from tests.time_budget import hang_guard_s, scaled


@pytest.fixture(autouse=True)
def _no_docker_cli(monkeypatch):
    """No test in this module may start a real ``docker`` process."""
    forbid_docker_cli(monkeypatch)
    yield
    assert_docker_cli_unused()


@pytest.fixture(autouse=True)
def _isolate_session_admission(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from booley.runtime import session_admission

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(session_admission, "admit_start", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(session_admission, "claim_vscode_start", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(session_admission, "clear_vscode_claim", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(session_admission, "has_pending_claim", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(session_admission, "vscode_sandboxes", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        session_admission, "running_project_sandboxes", lambda *_args, **_kwargs: ()
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "i2c"
    ws.mkdir()
    return ws


def test_session_up_waits_for_lifecycle_lock(
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lock_path = tmp_path / "host-config" / "locks" / "docker-lifecycle.lock"
    waiting = observe_lifecycle_contention(monkeypatch)

    with (
        held_lifecycle_lock(lock_path) as holder,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        result = executor.submit(sr.up, workspace)
        assert waiting.wait(hang_guard_s())
        # Without a spec `up` refuses at once, so only a held lock keeps it pending.
        assert not result.done()
        holder.release()
        with pytest.raises(sr.SessionError, match="run `booley init`"):
            result.result(timeout=hang_guard_s())

    assert "host Docker lifecycle is busy" in caplog.text


def test_doctor_start_checks_recovery_after_taking_lifecycle_lock(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "host-config" / "locks" / "docker-lifecycle.lock"
    waiting = observe_lifecycle_contention(monkeypatch)

    with (
        held_lifecycle_lock(lock_path) as holder,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        result = executor.submit(sr.up_without_recovery, workspace)
        assert waiting.wait(hang_guard_s())
        issuance_invalidation.prepare(str(tmp_path / "another-project"))
        holder.release()
        with pytest.raises(sr.RecoveryPendingError):
            result.result(timeout=hang_guard_s())

    assert issuance_invalidation.pending_invalidations() == (str(tmp_path / "another-project"),)


def _spec(**kwargs) -> dict:
    defaults = {
        "booley_dir_source": "/c/ws/i2c/.booley",
        "mcp_start_command": dc.mcp_post_start_command(),
    }
    return build_spec("claude", **{**defaults, **kwargs})


def _argv(spec: dict, workspace: Path) -> list[str]:
    return sr.docker_run_argv(spec, workspace, "booley-session-i2c")


def _flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


# ---------------------------------------------------------------------------
# substitute()
# ---------------------------------------------------------------------------


class TestSubstitute:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("${localEnv:CLAUDE_CODE_OAUTH_TOKEN}", ""),
            # Fail loudly at docker, rather than silently mounting the wrong path.
            ("${containerEnv:HOME}", "${containerEnv:HOME}"),
        ],
        ids=["unset-local-env", "unknown-variable"],
    )
    def test_unresolved_variables(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, value: str, expected: str
    ) -> None:
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        assert sr.substitute(value, workspace) == expected


# ---------------------------------------------------------------------------
# docker_run_argv()
# ---------------------------------------------------------------------------


class TestDockerRunArgv:
    def test_names_a_detached_container(self, workspace: Path):
        argv = _argv(_spec(), workspace)
        assert argv[:5] == ["docker", "run", "-d", "--name", "booley-session-i2c"]

    def test_ends_with_image_then_keepalive(self, workspace: Path):
        argv = _argv(_spec(image="booley-sandbox"), workspace)
        assert argv[-3:] == ["booley-sandbox", "sleep", "infinity"]

    def test_workspace_and_booley_dir_are_mounted(self, workspace: Path):
        from booley.runtime.platform_paths import docker_mount_path

        mounts = _flag_values(_argv(_spec(), workspace), "--mount")
        assert f"source={docker_mount_path(workspace)},target=/work,type=bind" in mounts
        assert "source=/c/ws/i2c/.booley,target=/booley,type=bind" in mounts

    def test_readonly_credential_mounts_survive_translation(self, workspace: Path):
        spec = _spec(login_source="/c/home/.credentials.json")
        mounts = _flag_values(_argv(spec, workspace), "--mount")
        creds = [m for m in mounts if "claude-creds-seed" in m]
        assert creds and creds[0].endswith(",readonly")

    def test_state_volume_basename_is_substituted(self, workspace: Path):
        mounts = _flag_values(_argv(_spec(), workspace), "--mount")
        assert "source=booley-claude-state-i2c,target=/home/agent/.claude,type=volume" in mounts

    def test_remote_env_becomes_e_flags(self, workspace: Path):
        env = _flag_values(_argv(_spec(), workspace), "-e")
        assert "BOOLEY_MCP_DEVELOPER_AGENT=1" in env
        assert "BOOLEY_DIR=/booley" in env
        assert "BOOLEY_AGENT_APP=claude" in env
        assert "HTTP_PROXY=http://booley-proxy:8080" in env

    def test_user_and_workdir(self, workspace: Path):
        argv = _argv(_spec(), workspace)
        assert _flag_values(argv, "--user") == ["agent"]
        assert _flag_values(argv, "--workdir") == ["/work"]

    def test_run_args_carry_network_label_and_hardening(self, workspace: Path):
        argv = _argv(_spec(memory="8g"), workspace)
        assert "--init" in argv
        assert _flag_values(argv, "--network") == [dc.EGRESS_NETWORK]
        assert _flag_values(argv, "--label") == [dc.INTERACTIVE_ROLE_LABEL]
        assert _flag_values(argv, "--cap-drop") == ["ALL"]
        assert _flag_values(argv, "--security-opt") == ["no-new-privileges"]
        assert _flag_values(argv, "--pids-limit") == [str(dc.SESSION_PIDS_LIMIT)]
        assert _flag_values(argv, "--memory") == ["8g"]

    def test_no_image_is_a_session_error(self, workspace: Path):
        with pytest.raises(sr.SessionError, match="no 'image'"):
            sr.docker_run_argv({}, workspace, "x")

    def test_oauth_token_is_resolved_not_passed_literally(self, workspace: Path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-tok")
        spec = _spec(credentials={"CLAUDE_CODE_OAUTH_TOKEN": "PLAN_VALUE_UNUSED"})
        argv = _argv(spec, workspace)
        env = _flag_values(argv, "-e")
        assert "CLAUDE_CODE_OAUTH_TOKEN" in env
        assert "sk-tok" not in str(argv)
        assert argv.environment["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-tok"


class TestExecArgv:
    @pytest.mark.parametrize(
        ("tty", "env", "expected"),
        [
            # F-43a: only the non-tty branch lacked TERM, and only it gets a value.
            pytest.param(False, None, ["-e", "TERM=dumb", "-i"], id="non-tty-dumb-term"),
            # `docker exec -t` sets TERM itself; a forwarded host TERM has no terminfo in the image.
            pytest.param(True, None, ["-t", "-i"], id="tty-leaves-term"),
            pytest.param(
                True, {"BOOLEY_TEST": "bad"}, ["-t", "-e", "BOOLEY_TEST=bad", "-i"], id="env"
            ),
        ],
    )
    def test_terminal_and_environment_flags(
        self, tty: bool, env: dict[str, str] | None, expected: list[str]
    ) -> None:
        argv = sr.exec_argv("c1", ["echo", "hi"], tty=tty, env=env)

        assert argv == ["docker", "exec", *expected, "c1", "echo", "hi"]

    def test_tty_never_forwards_an_exotic_host_term(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TERM", "xterm-kitty")

        assert sr.exec_argv("c1", ["ls"], tty=True) == ["docker", "exec", "-t", "-i", "c1", "ls"]


class TestContainerName:
    def test_same_basename_projects_do_not_collide(self, tmp_path: Path):
        first = tmp_path / "one" / "i2c"
        second = tmp_path / "two" / "i2c"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        assert sr.session_container_name(first) != sr.session_container_name(second)


def _test_issuance(workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(
        project_root=str(workspace),
        spec_sha256="current-spec",
        policy_revision=1,
        installation=None,
        license_profile=None,
        relay_image_id="sha256:" + "a" * 64,
        image_id="sha256:" + "b" * 64,
        project_data_source=str(workspace / ".booley"),
        project_data_layout="legacy",
    )


def _vscode_labels(
    workspace: Path,
    issuance: SimpleNamespace,
    *,
    spec_digest: str = "current-spec",
) -> dict[str, str]:
    labels = dict(label.split("=", 1) for label in runtime_spec.labels(issuance))
    labels.update(
        {
            "booley.role": "interactive",
            "booley.spec-digest": spec_digest,
            "devcontainer.local_folder": str(workspace),
            "devcontainer.config_file": str(workspace / ".devcontainer" / "devcontainer.json"),
        }
    )
    return labels


_NEW_IMAGE = "sha256:" + "c" * 64
_OLD_IMAGE = "sha256:" + "d" * 64
_DIRECTORY_IMAGE = "sha256:" + "e" * 64
_OUTDATED_LAYOUT_REFUSAL = r"mount an outdated Booley Directory layout"
_REBUILD = "on the host run `booley init`, then use Dev Containers: Rebuild Container"
_VSCODE_FIX = f"for VS Code Sandbox(s) 'editor', {_REBUILD}"
_OUTDATED_FIX = (
    "close VS Code, then on the host run `docker stop editor` and `docker rm editor` "
    "(mounted folders and named volumes survive; anything written only inside the "
    "container is lost), run `booley init --seed`, and reopen the folder in the "
    "container (Dev Containers: Rebuild Container)"
)
_AMBIGUOUS = (
    "inspect ambiguous Sandbox resource(s) 'editor'; Booley will not remove them automatically"
)


def _vscode_container(
    workspace: Path,
    *,
    running: bool = True,
    spec_digest: str = "current-spec",
    booley_dir_targets: tuple[str, ...] = ("/booley", "/work/.booley"),
    image: str | None = _NEW_IMAGE,
) -> dict:
    """Inspect state of this Project's VS Code container, its Booley Directory bound at the targets."""
    source = str(workspace / ".booley")
    return {
        "Image": image,
        "State": {"Running": running},
        "Config": {
            "Labels": _vscode_labels(workspace, _test_issuance(workspace), spec_digest=spec_digest)
        },
        "Mounts": [
            {"Destination": target, "Source": source, "Type": "bind", "RW": True}
            for target in booley_dir_targets
        ],
    }


@pytest.fixture
def sandbox_docker(monkeypatch: pytest.MonkeyPatch) -> SandboxDocker:
    docker = serve_sandbox_docker(monkeypatch)
    monkeypatch.setattr(sr.idk, "network_exists", lambda _name: True)
    monkeypatch.setattr(sr.idk, "image_exists", lambda _image: True)
    monkeypatch.setattr(sr.idk, "image_id", lambda image: image)
    return docker


@pytest.fixture
def issued(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, sandbox_docker: SandboxDocker
) -> Mock:
    """A Project whose host-issued spec validates as `_test_issuance`; returns the `validate` mock."""
    (workspace / ".booley").mkdir()
    _write_spec(workspace, _spec())
    validate = Mock(return_value=_test_issuance(workspace))
    monkeypatch.setattr(runtime_spec, "validate", validate)
    monkeypatch.setattr(
        runtime_spec, "authorized_project_data_source", lambda _path: workspace / ".booley"
    )
    monkeypatch.setattr(runtime_spec, "requested_license", lambda _path, **_kwargs: None)
    return validate


def _stub_layout_probe(monkeypatch: pytest.MonkeyPatch, layouts: dict[str, str]) -> None:
    """Answer the real Booley Directory layout probe per image from *layouts*."""
    from booley.runtime import project_image

    project_image.project_data_alias_capable.cache_clear()

    def answer(argv, **_kwargs):
        image = next(arg for arg in argv if arg.startswith("sha256:"))
        return subprocess.CompletedProcess(argv, 0, layouts[image] + "\n", "")

    monkeypatch.setattr(project_image, "run_captured_bounded", answer)


class TestPrepareLegacyContainer:
    @pytest.mark.parametrize(
        ("running", "labels", "bind", "removed"),
        [
            pytest.param(
                False, {"booley.spec-digest": "old"}, ".booley", ["editor"], id="old-issuance"
            ),
            pytest.param(
                False, {"devcontainer.config_file": None}, ".booley", ["editor"], id="no-config"
            ),
            pytest.param(
                False,
                {"devcontainer.config_file": "x.json"},
                ".booley",
                ["editor"],
                id="other-config",
            ),
            pytest.param(False, {}, "missing-socket", ["editor"], id="missing-bind"),
            pytest.param(False, {}, ".booley", [], id="current"),
            pytest.param(False, {}, "/run/desktop/mnt/host/wsl/x", [], id="unmappable-bind"),
            pytest.param(
                False,
                {"booley.spec-digest": "old", "devcontainer.local_folder": None},
                ".booley",
                [],
                id="headless",
            ),
            pytest.param(True, {}, ".booley", [], id="running-current"),
        ],
    )
    def test_prepare_removes_only_outdated_stopped_vscode_containers(
        self,
        workspace: Path,
        issued: Mock,
        sandbox_docker: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
        running: bool,
        labels: dict,
        bind: str,
        removed: list[str],
    ) -> None:
        monkeypatch.setattr(
            sr,
            "host_path_from_docker_mount",
            lambda source: None if source.startswith("/run/desktop/") else Path(source),
        )
        state = _vscode_container(workspace, running=running)
        for key, value in labels.items():
            state["Config"]["Labels"][key] = value
            if value is None:
                del state["Config"]["Labels"][key]
        source = bind if bind.startswith("/") else str(workspace / bind)
        state["Mounts"] += [
            {"Destination": "/extra", "Source": source, "Type": "bind", "RW": True},
            {
                "Destination": "/home/agent/.claude",
                "Name": "state",
                "Source": "/var/lib/x",
                "Type": "volume",
            },
        ]
        sandbox_docker.containers["editor"] = state

        assert sr.prepare(workspace) == "current-spec"
        assert sandbox_docker.removed == removed

    def test_other_projects_containers_are_never_inspected(
        self, workspace: Path, issued: Mock, sandbox_docker: SandboxDocker
    ) -> None:
        state = _vscode_container(workspace, running=False, spec_digest="old")
        state["Config"]["Labels"]["booley.project-id"] = "other-project"
        sandbox_docker.containers["vanished"] = state
        sandbox_docker.failing.add("vanished")

        assert sr.prepare(workspace) == "current-spec"

    @pytest.mark.parametrize(
        ("running", "failing", "refusal"),
        [
            pytest.param(
                True, set(), r"running Sandbox 'editor' uses an older host issuance", id="running"
            ),
            pytest.param(False, {"rm"}, r"not force-remove.*may have become active", id="started"),
        ],
    )
    def test_prepare_never_removes_an_active_container(
        self,
        workspace: Path,
        issued: Mock,
        sandbox_docker: SandboxDocker,
        running: bool,
        failing: set[str],
        refusal: str,
    ) -> None:
        sandbox_docker.containers["editor"] = _vscode_container(
            workspace, running=running, spec_digest="old"
        )
        sandbox_docker.failing |= failing

        with pytest.raises(sr.SessionError, match=refusal):
            sr.prepare(workspace)

        assert "editor" in sandbox_docker.containers
        assert all(argv == ["docker", "rm", "editor"] for argv in sandbox_docker.rm_attempts)
        assert len(sandbox_docker.rm_attempts) <= 1

    def test_unreadable_mounts_of_a_stopped_container_fail_loudly(
        self, workspace: Path, issued: Mock, sandbox_docker: SandboxDocker
    ) -> None:
        sandbox_docker.containers["editor"] = {
            **_vscode_container(workspace, running=False),
            "Mounts": None,
        }

        with pytest.raises(
            sr.SessionError, match=r"cannot inspect bind mounts for Sandbox 'editor'"
        ):
            sr.prepare(workspace)

    def test_inventory_failure_blocks_before_stamp_validation(
        self, workspace: Path, issued: Mock, sandbox_docker: SandboxDocker
    ) -> None:
        sandbox_docker.failing.add("ps")

        with pytest.raises(sr.SessionError, match="cannot inventory running Sandbox containers"):
            sr.prepare(workspace)

        issued.assert_not_called()

    def test_missing_generated_bind_names_source_and_reseed_action(
        self, workspace: Path, issued: Mock
    ) -> None:
        source = "/host/skills/renamed-skill"
        target = f"{dc.HOST_SKILLS_SIDECAR}/example-skill"
        issued.side_effect = runtime_spec.RuntimeSpecError(
            f"generated bind source for {target} is missing: {source}"
        )

        with pytest.raises(sr.SessionError) as caught:
            sr.prepare(workspace)

        message = str(caught.value)
        assert f"generated bind source for {target} is missing: {source}; {_REBUILD}" in message
        assert "session refresh" not in message

    @pytest.mark.parametrize(
        "damage",
        [{"image": 7}, {"mounts": "not-a-list"}, {"mounts": [7]}],
        ids=["image", "mounts-type", "mount-entry"],
    )
    def test_unreadable_spec_is_left_to_validation(
        self, workspace: Path, issued: Mock, sandbox_docker: SandboxDocker, damage: dict
    ) -> None:
        _write_spec(workspace, {**_spec(), **damage})
        sandbox_docker.containers["editor"] = _vscode_container(workspace, booley_dir_targets=())
        issued.side_effect = runtime_spec.RuntimeSpecError("spec is malformed")

        with pytest.raises(
            sr.SessionError, match="refusing Sandbox preparation: spec is malformed"
        ):
            sr.prepare(workspace)

    @pytest.mark.parametrize(
        ("targets", "external", "identified"),
        [
            pytest.param((), False, True, id="workspace-only"),
            pytest.param((), False, False, id="unidentified"),
            pytest.param(("/booley", "/work/.booley"), True, True, id="external-source"),
            pytest.param(("/former-mount", "/work/.booley"), False, True, id="former-target"),
        ],
    )
    def test_running_container_with_outdated_layout_is_refused_first(
        self,
        workspace: Path,
        issued: Mock,
        sandbox_docker: SandboxDocker,
        targets: tuple[str, ...],
        external: bool,
        identified: bool,
    ) -> None:
        issued.side_effect = runtime_spec.RuntimeSpecError("spec stamp has an unsupported version")
        state = _vscode_container(workspace, booley_dir_targets=targets)
        for mount in state["Mounts"] if external else ():
            mount["Source"] = str(workspace.parent / "external-project-data")
        if not identified:
            del state["Config"]["Labels"]["booley.project-id"]
        sandbox_docker.containers["editor"] = state

        with pytest.raises(sr.SessionError, match=_OUTDATED_LAYOUT_REFUSAL) as caught:
            sr.prepare(workspace)

        message = str(caught.value)
        repair = (
            "close VS Code, then on the host run `docker stop editor` and `docker rm editor`"
            if identified
            else _AMBIGUOUS
        )
        assert repair in message
        assert ("`booley init --seed`" in message) is identified
        issued.assert_not_called()

    @pytest.mark.parametrize(
        ("image", "mask", "accepted"),
        [
            pytest.param(_NEW_IMAGE, False, True, id="alias-image"),
            pytest.param(_OLD_IMAGE, False, False, id="pre-rename-image"),
            pytest.param(_DIRECTORY_IMAGE, False, False, id="directory-image"),
            pytest.param(None, False, False, id="no-image-identity"),
            pytest.param(_NEW_IMAGE, True, False, id="masked-alias"),
        ],
    )
    def test_alias_layout_is_proven_by_the_container_image(
        self,
        workspace: Path,
        issued: Mock,
        sandbox_docker: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
        image: str | None,
        mask: bool,
        accepted: bool,
    ) -> None:
        spec = _spec(image=_NEW_IMAGE)
        spec["mounts"] = [m for m in spec["mounts"] if f"target={dc.BOOLEY_DIR_TARGET}," not in m]
        spec["mounts"].append(f"source={workspace / '.booley'},target=/work/.booley,type=bind")
        _write_spec(workspace, spec)
        _stub_layout_probe(
            monkeypatch,
            {_NEW_IMAGE: "canonical-alias", _OLD_IMAGE: "absent", _DIRECTORY_IMAGE: "directory"},
        )
        targets = ("/work/.booley", "/booley/secrets") if mask else ("/work/.booley",)
        sandbox_docker.containers["editor"] = _vscode_container(
            workspace, booley_dir_targets=targets, image=image
        )

        if accepted:
            assert sr.prepare(workspace) == "current-spec"
            return
        with pytest.raises(sr.SessionError, match=_OUTDATED_LAYOUT_REFUSAL) as caught:
            sr.prepare(workspace)
        assert "`docker rm editor`" in str(caught.value)

    def test_external_booley_dir_must_be_bound_at_the_current_target(
        self,
        workspace: Path,
        issued: Mock,
        sandbox_docker: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        external = workspace.parent / "external-project-data"
        monkeypatch.setattr(runtime_spec, "authorized_project_data_source", lambda _path: external)
        state = _vscode_container(workspace, booley_dir_targets=("/booley",))
        state["Mounts"][0]["Source"] = str(external)
        sandbox_docker.containers["editor"] = state

        assert sr.prepare(workspace) == "current-spec"
        state["Mounts"][0]["Destination"] = "/former-mount"
        with pytest.raises(sr.SessionError, match=_OUTDATED_LAYOUT_REFUSAL):
            sr.prepare(workspace)


class TestIssuedRuntimeDriftFix:
    @pytest.mark.parametrize(
        ("edit", "layout", "fix"),
        [
            pytest.param(
                lambda state: state.update(State={"Running": False}),
                "legacy",
                _VSCODE_FIX,
                id="stopped",
            ),
            pytest.param(lambda state: None, "legacy", _VSCODE_FIX, id="running-pinned"),
            pytest.param(
                lambda state: state.update(Mounts=[]),
                "legacy",
                _OUTDATED_FIX,
                id="running-unpinned",
            ),
            pytest.param(
                lambda state: state.update(Mounts=None),
                "legacy",
                _OUTDATED_FIX,
                id="unreadable-mounts",
            ),
            pytest.param(
                lambda state: state["Config"]["Labels"].pop("devcontainer.local_folder"),
                "legacy",
                _AMBIGUOUS,
                id="unknown-origin",
            ),
            pytest.param(
                lambda state: state.update(State="unknown"),
                "legacy",
                _AMBIGUOUS,
                id="malformed-state",
            ),
            pytest.param(lambda state: "not-json", "legacy", _AMBIGUOUS, id="unreadable-inspect"),
            pytest.param(
                lambda state: None, "canonical-alias", _AMBIGUOUS, id="failed-layout-probe"
            ),
        ],
    )
    def test_names_the_repair_of_each_drifted_container(
        self,
        workspace: Path,
        sandbox_docker: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
        edit,
        layout: str,
        fix: str,
    ) -> None:
        _stub_layout_probe(monkeypatch, {_NEW_IMAGE: "unprovable"})
        state = _vscode_container(workspace)
        sandbox_docker.containers["editor"] = edit(state) or state
        issuance = _test_issuance(workspace)
        issuance.project_data_layout = layout
        canonical = sr.session_container_name(workspace)

        assert sr.issued_runtime_drift_fix(workspace, issuance, ["editor", canonical]) == (
            f"{fix}; run `booley session refresh` on the host"
        )

    def test_reports_running_and_ambiguous_resources_together(
        self, workspace: Path, sandbox_docker: SandboxDocker
    ) -> None:
        sandbox_docker.containers["stranger"] = {
            **_vscode_container(workspace),
            "State": "unknown",
        }
        sandbox_docker.containers["editor"] = _vscode_container(workspace)

        fix = sr.issued_runtime_drift_fix(
            workspace, _test_issuance(workspace), ["stranger", "editor"]
        )

        assert fix == (
            "inspect ambiguous Sandbox resource(s) 'stranger'; Booley will not remove them "
            f"automatically; {_VSCODE_FIX}"
        )

    def test_drift_without_a_named_resource_lists_both_workflows(self, workspace: Path) -> None:
        fix = sr.issued_runtime_drift_fix(workspace, _test_issuance(workspace), [])

        assert fix == f"in VS Code, {_REBUILD}; headless, run `booley session refresh` on the host"

    def test_windows_host_paths_identify_running_vscode(
        self, sandbox_docker: SandboxDocker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = Path(r"C:\workplace\picorv32")
        state = _vscode_container(workspace)
        state["Config"]["Labels"]["devcontainer.local_folder"] = "c:/workplace/picorv32"
        state["Config"]["Labels"]["devcontainer.config_file"] = (
            "c:/workplace/picorv32/.devcontainer/devcontainer.json"
        )
        sandbox_docker.containers["editor"] = state
        monkeypatch.setattr(os.path, "normcase", ntpath.normcase)

        fix = sr.issued_runtime_drift_fix(workspace, _test_issuance(workspace), ["editor"])

        assert fix == _VSCODE_FIX


# ---------------------------------------------------------------------------
# up() / down() / status()
# ---------------------------------------------------------------------------


def _write_spec(workspace: Path, spec: dict) -> None:
    dc.write_devcontainer(workspace, spec)


@pytest.fixture
def wired(workspace: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """A workspace with a spec on disk and mocked external boundaries."""
    from booley.runtime import image_lifecycle

    _write_spec(workspace, _spec())
    if getattr(request, "param", None) != "real-image-lifecycle":
        monkeypatch.setattr(
            image_lifecycle,
            "reconcile",
            Mock(
                return_value=image_lifecycle.LifecycleResult(
                    "booley-sandbox", "sha256:fixture", image_lifecycle.Status.CURRENT
                )
            ),
        )
    issuance = SimpleNamespace(
        project_root=str(workspace),
        spec_sha256="abc",
        policy_revision=1,
        installation=None,
        license_profile=None,
        relay_image_id="sha256:" + "a" * 64,
        project_data_source=str(workspace / ".booley"),
        project_data_layout="legacy",
    )
    # monkeypatch, not patch.object: tests re-patch these names with the same fixture, and one
    # undo stack restores them in order instead of leaking a mock into other modules.
    monkeypatch.setattr(sr.idk, "network_exists", Mock(return_value=True))
    monkeypatch.setattr(sr.idk, "image_exists", Mock(return_value=True))
    # No image metadata, as when the inspect finds nothing (no Docker CLI call).
    monkeypatch.setattr(sr.idk, "inspect_image_metadata", Mock(return_value=None))
    # The stale-container probe resolves the image ID through the Docker helper.
    monkeypatch.setattr(sr.idk, "image_id", Mock(return_value=None))
    monkeypatch.setattr(runtime_spec, "validate", Mock(return_value=issuance))
    monkeypatch.setattr(runtime_spec, "authenticate", Mock(return_value=issuance))
    monkeypatch.setattr(
        runtime_spec, "authorized_project_data_source", Mock(return_value=workspace / ".booley")
    )
    # Every Docker command of session_runtime goes through `_run`, which no public entry
    # takes as an argument; the tests that use `wired` script its replies, and they set
    # failing replies the strict Sandbox inventories must not read.
    monkeypatch.setattr(sr, "_strict_running_sandbox_states", Mock(return_value=[]))
    monkeypatch.setattr(sr, "_strict_all_sandbox_states", Mock(return_value=[]))
    monkeypatch.setattr(sr, "container_matches_issuance", Mock(return_value=True))
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(sr, "_run", run)
    return workspace, run


def _argv_of(call) -> list[str]:
    return call.args[0]


def _refresh_state(
    *,
    running: bool = False,
    networks: dict | None = None,
    labels: dict | None = None,
) -> dict:
    return {
        "Config": {
            "Labels": labels
            or {
                "booley.role": "interactive",
                "booley.project-id": "project-id",
                "booley.license-profile": "none",
            }
        },
        "State": {"Running": running},
        "NetworkSettings": {"Networks": networks or {}},
    }


_LICENSE_PROFILE = SimpleNamespace(
    server_ipv4="10.20.30.40",
    server_hostid="license-server-01",
    lmgrd_port=2100,
    vendor_port=2101,
)


def _relay(workspace: Path):
    """The Project's deterministic license relay objects."""
    from booley.eda.provisioning.licensing import flexnet_docker

    return flexnet_docker.resources_for_session(str(workspace.resolve()))


def _issued_state(workspace: Path, image: str, labels: dict[str, str | None]) -> dict:
    """Strict inspect data of a container carrying `_test_issuance`'s labels and *labels*."""
    merged = dict(label.split("=", 1) for label in runtime_spec.labels(_test_issuance(workspace)))
    merged.update(labels)
    state = _refresh_state(labels={k: v for k, v in merged.items() if v is not None})
    state.update(Id="c" * 64, Image=image)
    return state


def _headless_state(
    workspace: Path, *, running: bool = True, image: str | None = None, **labels: str | None
) -> dict:
    """This Project's headless Sandbox, current for `_test_issuance` unless overridden."""
    image = image or _test_issuance(workspace).image_id
    state = _issued_state(workspace, image, {"booley.role": "interactive", **labels})
    state["State"]["Running"] = running
    return state


def _relay_state(workspace: Path, *, image: str | None = None, **labels: str) -> dict:
    """This Project's license relay, current for `_test_issuance` unless overridden."""
    owner = {"booley.role": "license-relay", "booley.session-id": _relay(workspace).session_id}
    image = image or _test_issuance(workspace).relay_image_id
    return _issued_state(workspace, image, {**owner, **labels})


@pytest.fixture
def host_docker(sandbox_docker: SandboxDocker, monkeypatch: pytest.MonkeyPatch) -> SandboxDocker:
    """`sandbox_docker` that also answers existence probes and removes the license relay."""
    from booley.eda.provisioning.licensing import flexnet_docker

    docker = sandbox_docker
    monkeypatch.setattr(sr.idk, "container_exists", lambda name: docker.find(name) is not None)
    monkeypatch.setattr(
        sr.idk, "container_running", lambda name: docker.containers[name]["State"]["Running"]
    )
    monkeypatch.setattr(sr.idk, "network_exists", docker.networks.__contains__)
    monkeypatch.setattr(
        flexnet_docker,
        "remove_relay",
        lambda relay, **_kwargs: docker.removed.append(relay.relay_container),
    )
    return docker


class TestInitReconciliation:
    @pytest.mark.parametrize(
        "drift",
        [{"booley.spec-digest": "older-spec"}, {"image": _OLD_IMAGE}],
        ids=["labels", "image"],
    )
    def test_plans_then_removes_stopped_runtime_from_an_old_issuance(
        self, workspace: Path, host_docker: SandboxDocker, drift: dict
    ) -> None:
        name = sr.session_container_name(workspace)
        host_docker.containers[name] = _headless_state(workspace, running=False, **drift)
        issuance = _test_issuance(workspace)

        assert sr.plan_stopped_headless_runtime_reconciliation(workspace, issuance).pending
        assert host_docker.removed == []
        assert sr.reconcile_stopped_headless_runtime(workspace, issuance)
        assert host_docker.removed == [name]

    @pytest.mark.parametrize("running", [True, False])
    def test_preserves_current_runtime(
        self, workspace: Path, host_docker: SandboxDocker, running: bool
    ) -> None:
        name = sr.session_container_name(workspace)
        host_docker.containers[name] = _headless_state(workspace, running=running)

        assert not sr.reconcile_stopped_headless_runtime(workspace, _test_issuance(workspace))
        assert host_docker.removed == []

    @pytest.mark.parametrize("withdrawn", [True, False], ids=["grant-change", "issuance-stands"])
    def test_stale_running_runtime_names_session_refresh(
        self,
        workspace: Path,
        host_docker: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
        withdrawn: bool,
    ) -> None:
        """The refusal names the workflow Sandbox validation prints, also after a Grant change."""
        from booley.runtime import runtime_context

        def snapshot(_root: Path) -> SimpleNamespace:
            if withdrawn:
                raise runtime_spec.IssuanceUnavailableError("withdrawn")
            return SimpleNamespace(license_profile=None)

        monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
        monkeypatch.setattr(runtime_spec, "load_issued_snapshot", snapshot)
        monkeypatch.setattr(sr.project_image, "project_sandbox_image", lambda _root: "selected")
        monkeypatch.setattr(sr.project_image, "is_managed_sandbox_image", lambda *_args: True)
        name = sr.session_container_name(workspace)
        host_docker.containers[name] = _headless_state(workspace, image=_OLD_IMAGE)

        with pytest.raises(sr.SessionError) as refusal:
            sr.reconcile_stopped_headless_runtime(workspace, _test_issuance(workspace))

        assert str(refusal.value).endswith(
            "uses an older host issuance; run `booley session refresh` on the host"
        )
        assert host_docker.removed == []

    def test_other_project_session_blocks_relay_mutation(
        self, workspace: Path, host_docker: SandboxDocker
    ) -> None:
        relay = _relay(workspace)
        host_docker.containers[sr.session_container_name(workspace)] = _headless_state(
            workspace, running=False, image=_OLD_IMAGE
        )
        host_docker.containers["vscode-session"] = _headless_state(workspace)
        host_docker.containers[relay.relay_container] = _relay_state(workspace, image=_OLD_IMAGE)

        with pytest.raises(
            sr.SessionError, match="other Project Sessions exist: 'vscode-session'"
        ):
            sr.reconcile_stopped_headless_runtime(workspace, _test_issuance(workspace))

        assert host_docker.removed == []

    @pytest.mark.parametrize(
        ("relay_state", "stale"),
        [
            (None, True),
            ({}, False),
            ({"booley.spec-digest": "older-spec"}, True),
            ({"image": _OLD_IMAGE}, True),
        ],
        ids=["only-networks-left", "current", "older-labels", "other-image"],
    )
    def test_retry_finishes_stale_relay_cleanup_after_container_is_gone(
        self, workspace: Path, host_docker: SandboxDocker, relay_state: dict | None, stale: bool
    ) -> None:
        relay = _relay(workspace)
        host_docker.networks.add(relay.private_network)
        if relay_state is not None:
            host_docker.containers[relay.relay_container] = _relay_state(workspace, **relay_state)

        assert sr.reconcile_stopped_headless_runtime(workspace, _test_issuance(workspace)) is stale
        assert host_docker.removed == ([relay.relay_container] if stale else [])

    @pytest.mark.parametrize("owner", ["sandbox", "relay"])
    def test_preserves_foreign_container(
        self, workspace: Path, host_docker: SandboxDocker, owner: str
    ) -> None:
        if owner == "sandbox":
            name = sr.session_container_name(workspace)
            state = _headless_state(workspace, running=False, **{"booley.project-id": None})
        else:
            name = _relay(workspace).relay_container
            state = _relay_state(workspace, **{"booley.role": "interactive"})
        host_docker.containers[name] = state

        with pytest.raises(sr.SessionError, match="it was not modified"):
            sr.reconcile_stopped_headless_runtime(workspace, _test_issuance(workspace))

        assert host_docker.removed == []


class TestRefreshContainerTransactions:
    @pytest.mark.parametrize(
        ("state", "message"),
        [
            (None, "cannot inspect Sandbox"),
            ("not-json", "invalid inspection"),
            ("[]", "invalid inspection"),
            (
                json.dumps(
                    {
                        "Config": {},
                        "State": {"Running": False},
                        "NetworkSettings": {"Networks": {}},
                    }
                ),
                "incomplete inspection",
            ),
            (_refresh_state(labels={"booley.role": 7}), "invalid labels"),
        ],
        ids=["docker-error", "not-json", "not-an-object", "incomplete", "non-string-label"],
    )
    def test_strict_inspect_rejects_untrusted_docker_output(
        self, workspace: Path, host_docker: SandboxDocker, state: dict | str | None, message: str
    ) -> None:
        if state is None:
            host_docker.failing.add("container")
        else:
            host_docker.containers[sr.session_container_name(workspace)] = state

        with pytest.raises(sr.SessionError, match=message):
            sr.remove_refresh_predecessor(workspace)

        assert host_docker.removed == []

    @pytest.mark.parametrize(
        ("backup_exists", "message"),
        [(True, "recovery container"), (False, r"rename failed.*restart.*start failed")],
        ids=["recovery-container-exists", "park-fails"],
    )
    def test_rebuild_keeps_the_running_sandbox_when_it_cannot_park_it(
        self, wired, monkeypatch: pytest.MonkeyPatch, backup_exists: bool, message: str
    ) -> None:
        workspace, run = wired
        name = sr.session_container_name(workspace)
        monkeypatch.setattr(sr.idk, "container_exists", lambda n: n == name or backup_exists)
        monkeypatch.setattr(sr.idk, "container_running", lambda _name: True)
        run.side_effect = lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, int(argv[1] in {"rename", "start"}), "", f"{argv[1]} failed"
        )

        with pytest.raises(sr.SessionError, match=message):
            sr.up(workspace, rebuild=True)

        assert not any(_argv_of(call)[1] in {"run", "rm"} for call in run.call_args_list)


def _sandbox_state(
    workspace: Path, *, vscode: bool = False, running: bool = True, name: str | None = None
) -> dict:
    """Inspect state of a Sandbox of `_test_issuance`: what the real matcher and selection accept."""
    spec = json.loads(dc.devcontainer_path(workspace).read_text(encoding="utf-8"))
    issuance = _test_issuance(workspace)
    state = _issued_container(spec, workspace, vscode=vscode)
    state["Image"] = issuance.image_id
    state["Config"]["Labels"].update(
        {**dict(label.split("=", 1) for label in runtime_spec.labels(issuance))}
    )
    entry = headless_session_entry(name or sr.session_container_name(workspace), workspace)
    state["Config"]["Labels"].update(entry["Config"]["Labels"])
    state.update(Id=entry["Id"], Name=entry["Name"], Created=entry["Created"])
    state["State"] = {**entry["State"], "Running": running}
    return state


def _issue_project(docker: SandboxDocker, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give *root* a spec on disk, and its image and the egress network on the Docker fake."""
    (root / ".booley").mkdir()
    image = sr.project_image.project_image_name(root)
    _write_spec(root, _spec(image=image))
    docker.networks.add(dc.EGRESS_NETWORK)
    docker.images[image] = {"Id": _test_issuance(root).image_id}
    # The host-issued authority store and its licence record lie outside these entry points.
    monkeypatch.setattr(
        runtime_spec, "validate", lambda project, *_a, **_k: _test_issuance(project)
    )
    monkeypatch.setattr(
        runtime_spec, "authorized_project_data_source", lambda path: path / ".booley"
    )
    monkeypatch.setattr(runtime_spec, "requested_license", lambda _path, **_kwargs: None)


@pytest.fixture
def up_docker(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> SandboxDocker:
    """A Project with a spec on disk, and the egress network and spec image on a Docker fake."""
    docker = serve_sandbox_docker(monkeypatch)
    _issue_project(docker, workspace, monkeypatch)
    return docker


@pytest.fixture
def attached(up_docker: SandboxDocker, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """`up_docker` with the real Sandbox selection and a stand-in attachment."""
    from booley.runtime import session_admission

    # The autouse fixture stubs the selection; these tests read the fake through the real one.
    monkeypatch.setattr(
        session_admission, "running_project_sandboxes", real_running_project_sandboxes
    )
    return SimpleNamespace(docker=up_docker, commands=attach_commands(monkeypatch))


def _configure_image(monkeypatch: pytest.MonkeyPatch, workspace: Path, image: str) -> None:
    """Point `[sandbox].image` of the Project's real `booley.toml` at *image*."""
    from booley.runtime.booley_dir import reset_cache

    (workspace / ".booley" / "booley.toml").write_text(
        f'[sandbox]\nimage = "{image}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("BOOLEY_DIR", str(workspace / ".booley"))
    reset_cache()


def _mutations(docker: SandboxDocker) -> list[list[str]]:
    """The recorded Docker calls that change a container or network."""
    return [
        call
        for call in docker.calls
        if call[1] in {"run", "start", "stop", "rename", "rm", "exec"}
        or call[1:3] in (["network", "create"], ["network", "connect"])
    ]


class TestUp:
    def test_new_sandbox_refusal_happens_before_relay_or_docker_mutation(
        self, workspace: Path, up_docker: SandboxDocker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.eda.provisioning.licensing.flexnet_docker import RELAY_IMAGE
        from booley.runtime import session_admission

        up_docker.images[RELAY_IMAGE] = {"Id": _test_issuance(workspace).relay_image_id}
        monkeypatch.setattr(runtime_spec, "requested_license", lambda *_a, **_k: _LICENSE_PROFILE)
        # The refusal text under test is the admission's; the fake Docker cannot hold a
        # host-wide session count, so the public boundary cannot make the real admission refuse.
        monkeypatch.setattr(
            session_admission,
            "admit_start",
            Mock(side_effect=session_admission.AdmissionError("host is at max_sessions=1")),
        )

        with pytest.raises(sr.SessionError, match="max_sessions=1"):
            sr.up(workspace)

        assert _mutations(up_docker) == []

    @pytest.mark.parametrize("own", [True, False], ids=["own", "other"])
    def test_doctor_start_refuses_pending_recovery_that_up_completes(
        self, workspace: Path, up_docker: SandboxDocker, own: bool
    ) -> None:
        identity = str(workspace.resolve() if own else workspace / "other")
        up_docker.containers[sr.session_container_name(workspace)] = _sandbox_state(workspace)
        issuance_invalidation.prepare(identity)
        stamps = runtime_spec.stamp_store()
        stamps.write(identity, "{}")

        with pytest.raises(sr.RecoveryPendingError):
            sr.up_without_recovery(workspace)

        assert issuance_invalidation.pending_invalidations() == (identity,)
        assert stamps.path(identity).read_bytes() == b"{}"
        assert _mutations(up_docker) == []
        with pytest.raises(sr.SessionError, match="recovered interrupted Sandbox host state"):
            sr.up(workspace)
        assert issuance_invalidation.pending_invalidations() == ()

    @pytest.mark.parametrize("entry", ["up", "up_without_recovery"])
    @pytest.mark.parametrize(
        ("state", "verbs", "hooks"),
        [
            pytest.param(None, ["run"], ["postCreateCommand", "postStartCommand"], id="absent"),
            pytest.param(False, ["start"], ["postStartCommand"], id="stopped"),
            # A resumed container must not re-copy the credential seed.
            pytest.param(True, [], ["postStartCommand"], id="running"),
        ],
    )
    def test_lifecycle_verbs_and_hooks(
        self,
        workspace: Path,
        up_docker: SandboxDocker,
        caplog: pytest.LogCaptureFixture,
        state: bool | None,
        verbs: list[str],
        hooks: list[str],
        entry: str,
    ) -> None:
        name = sr.session_container_name(workspace)
        if state is not None:
            up_docker.containers[name] = _sandbox_state(workspace, running=state)
        spec = json.loads(dc.devcontainer_path(workspace).read_text(encoding="utf-8"))

        assert getattr(sr, entry)(workspace) == name

        assert [call[1] for call in up_docker.calls if call[1] in {"run", "start"}] == verbs
        assert [call for call in up_docker.calls if call[1] == "exec"] == [
            ["docker", "exec", name, "bash", "-lc", spec[hook]] for hook in hooks
        ]
        assert "superseded" not in caplog.text

    @pytest.mark.skipif(sys.platform == "win32", reason="the registrar runs in the Linux Sandbox")
    def test_post_start_starts_t3_code_once_and_registers_the_project(
        self,
        workspace: Path,
        up_docker: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        t3 = serve_t3_code(monkeypatch)
        runs = run_registrar_on_post_start(up_docker, workspace, monkeypatch)
        up_docker.containers[sr.session_container_name(workspace)] = _sandbox_state(
            workspace, running=False
        )

        sr.up(workspace)
        sr.up(workspace)

        base = str(workspace / ".booley" / ".runtime" / "t3")
        serve = ["t3", "serve", "--host", "127.0.0.1", "--port", "3773", "--base-dir", base]
        assert t3.served == [[*serve, str(workspace)]]
        assert t3.added == [["t3", "project", "add", "--base-dir", base, str(workspace)]]
        assert [("t3:started" in run, "t3:running" in run) for run in runs] == [
            (True, False),
            (False, True),
        ]
        logs = [
            caplog.text,
            *runs,
            *(f.read_text(errors="replace") for f in tmp_path.rglob("*") if f.is_file()),
        ]
        logs += [Path(sink).read_text() for sink in t3.sinks]
        assert t3.sinks
        assert t3.serve_stdouts == [subprocess.DEVNULL]
        assert TOKEN not in "".join(logs)

    def test_existing_container_with_runtime_drift_is_never_started(
        self, workspace: Path, up_docker: SandboxDocker
    ) -> None:
        name = sr.session_container_name(workspace)
        up_docker.containers[name] = {**_sandbox_state(workspace, running=False), "Mounts": []}

        with pytest.raises(sr.SessionError, match="does not match the current host issuance"):
            sr.up(workspace)

        assert _mutations(up_docker) == []

    @pytest.mark.parametrize(
        ("running", "spec_digest", "bind", "failing", "removed"),
        [
            pytest.param(False, "old", None, set(), True, id="stopped-old-issuance"),
            pytest.param(False, "old", None, {"run"}, False, id="removal-waits-for-create"),
            pytest.param(
                False,
                "current-spec",
                "missing-wayland-socket",
                set(),
                False,
                id="unavailable-bind",
            ),
            pytest.param(True, "old", None, set(), False, id="running-old-issuance"),
        ],
    )
    def test_vscode_containers_reconciled_around_create(
        self,
        workspace: Path,
        up_docker: SandboxDocker,
        running: bool,
        spec_digest: str,
        bind: str | None,
        failing: set[str],
        removed: bool,
    ) -> None:
        state = _vscode_container(workspace, running=running, spec_digest=spec_digest)
        if bind is not None:
            state["Mounts"] = [
                {
                    "Type": "bind",
                    "Source": str(workspace / bind),
                    "Destination": "/tmp/vscode-wayland.sock",
                }
            ]
        up_docker.containers["editor"] = state
        up_docker.failing |= failing

        if running or failing:
            with pytest.raises(sr.SessionError, match=r"older host issuance|docker run failed"):
                sr.up(workspace)
        else:
            sr.up(workspace)

        calls = up_docker.calls
        assert (["docker", "rm", "editor"] in calls) is removed
        if removed:  # only after the replacement exists
            assert calls.index(["docker", "rm", "editor"]) > next(
                i for i, call in enumerate(calls) if call[1] == "run"
            )
        if running:
            assert not any(call[1] == "run" for call in calls)

    def test_rebuild_parks_old_container_until_replacement_succeeds(
        self, workspace: Path, up_docker: SandboxDocker
    ) -> None:
        name = sr.session_container_name(workspace)
        backup = f"{name}-pre-refresh"
        up_docker.containers[name] = _sandbox_state(workspace, running=False)

        sr.up(workspace, rebuild=True)

        verbs = [call for call in _mutations(up_docker) if call[1] != "exec"]
        assert verbs[0] == ["docker", "rename", name, backup]
        assert verbs[1][:2] == ["docker", "run"]
        assert verbs[2:] == [["docker", "rm", "-f", backup]]

    @pytest.mark.parametrize(
        ("parked", "created", "message"),
        [
            pytest.param(True, "issued", "refreshed Sandbox uses", id="prior-runtime-restored"),
            pytest.param(False, "issued", "refreshed Sandbox uses", id="no-prior-image-mismatch"),
            pytest.param(False, "foreign", "labels or network topology", id="no-prior-labels"),
        ],
    )
    def test_failed_refresh_rolls_back(
        self,
        workspace: Path,
        up_docker: SandboxDocker,
        parked: bool,
        created: str,
        message: str,
    ) -> None:
        name = sr.session_container_name(workspace)
        backup = f"{name}-pre-refresh"
        if created == "issued":
            up_docker.created = _sandbox_state(workspace)
        if parked:
            up_docker.containers[name] = _sandbox_state(workspace, running=False)
            # Cleanup of stale VS Code containers must wait until the replacement verified.
            up_docker.containers["stale-vscode"] = _vscode_container(
                workspace, running=False, spec_digest="old"
            )

        with pytest.raises(sr.SessionError, match=message):
            sr.up(workspace, rebuild=True, expected_image_id="sha256:fresh")

        calls = up_docker.calls
        assert ["docker", "rm", "-f", name] in calls
        assert ["docker", "rm", "stale-vscode"] not in calls
        assert ["docker", "rm", "-f", backup] not in calls
        assert (["docker", "rename", backup, name] in calls) is parked
        assert not any("-I" in call for call in calls)  # no wheel probe of an unverified container
        assert (name in up_docker.containers) is parked

    def test_old_container_cleanup_failure_keeps_verified_replacement(
        self, workspace: Path, up_docker: SandboxDocker, caplog: pytest.LogCaptureFixture
    ) -> None:
        name = sr.session_container_name(workspace)
        up_docker.containers[name] = _sandbox_state(workspace, running=False)
        up_docker.created = _sandbox_state(workspace)
        up_docker.failing.add("rm")  # only the backup removal runs

        result = sr.up(
            workspace, rebuild=True, expected_image_id=_test_issuance(workspace).image_id
        )

        assert result == name
        assert "replacement succeeded" in caplog.text

    @pytest.mark.parametrize(
        ("image", "steps"),
        [
            pytest.param(None, "run `booley session refresh` on the host", id="booley-image"),
            pytest.param(
                "custom/session",
                "run `booley session down`, `booley init --seed`, "
                "and `booley session up` on the host",
                id="own-image",
            ),
        ],
    )
    def test_licensed_refresh_fails_before_parking_existing_runtime(
        self,
        workspace: Path,
        up_docker: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
        image: str | None,
        steps: str,
    ) -> None:
        from booley.eda.provisioning.licensing.flexnet_docker import RELAY_IMAGE

        up_docker.images[RELAY_IMAGE] = {"Id": _test_issuance(workspace).relay_image_id}
        up_docker.containers[sr.session_container_name(workspace)] = _sandbox_state(
            workspace, running=False
        )
        monkeypatch.setattr(runtime_spec, "requested_license", lambda *_a, **_k: _LICENSE_PROFILE)
        if image is not None:
            _configure_image(monkeypatch, workspace, image)

        with pytest.raises(
            sr.SessionError,
            match=re.escape(
                "`booley session up --rebuild` cannot preserve a licensed relay topology; "
                f"instead, {steps}"
            ),
        ):
            sr.up(workspace, rebuild=True)

        assert _mutations(up_docker) == []

    def test_docker_run_failure_raises(self, workspace: Path, up_docker: SandboxDocker) -> None:
        up_docker.failing.add("run")

        with pytest.raises(
            sr.SessionError, match=r"docker run failed.*run failed: container is running"
        ):
            sr.up(workspace)

    def test_failing_hook_warns_but_leaves_container_up(
        self, workspace: Path, up_docker: SandboxDocker, caplog: pytest.LogCaptureFixture
    ) -> None:
        up_docker.exec_reply = lambda argv: subprocess.CompletedProcess(
            argv, 1, "", "registrar boom"
        )

        assert sr.up(workspace) == sr.session_container_name(workspace)

        assert "postCreateCommand failed" in caplog.text
        assert sr.session_container_name(workspace) in up_docker.containers


def _docker_mounts(spec: dict, workspace: Path, booley_dir_target: str) -> list[dict]:
    """Docker's view of the spec's mounts, with the Booley Directory bound at *booley_dir_target*."""
    mounts: list[dict] = []
    for raw in [spec["workspaceMount"], *spec["mounts"]]:
        fields = dict(
            f.split("=", 1) for f in sr.substitute(raw, workspace).split(",") if "=" in f
        )
        target = fields["target"]
        mount = {
            "Destination": booley_dir_target if target == dc.BOOLEY_DIR_TARGET else target,
            "Source": fields["source"],
            "Type": fields["type"],
            "RW": "readonly" not in raw,
        }
        if fields["type"] == "volume":
            mount.update(
                Name=fields["source"], Source=f"/var/lib/docker/volumes/{fields['source']}"
            )
        mounts.append(mount)
    return mounts


def _issued_container(spec: dict, workspace: Path, *, vscode: bool = False) -> dict:
    """Inspect state of a container started exactly as *spec* issues it."""
    sections = (spec["containerEnv"],) if vscode else (spec["remoteEnv"], spec["containerEnv"])
    environment = {key: value for section in sections for key, value in section.items()}
    return {
        "Image": spec["image"],
        "Config": {
            "Image": spec["image"],
            "User": "agent",
            "WorkingDir": "/work",
            "Env": [f"{key}={value}" for key, value in environment.items()],
            "Labels": {"devcontainer.local_folder": str(workspace)} if vscode else {},
        },
        "HostConfig": {
            "CapDrop": ["ALL"],
            "Privileged": False,
            "PublishAllPorts": False,
            "SecurityOpt": ["no-new-privileges"],
            "PidsLimit": dc.SESSION_PIDS_LIMIT,
            "Memory": 0,
        },
        "NetworkSettings": {
            "Networks": {n: {} for n in _flag_values(spec["runArgs"], "--network")}
        },
        "Mounts": _docker_mounts(spec, workspace, dc.BOOLEY_DIR_TARGET),
    }


def _resumes(docker: SandboxDocker, spec: dict, workspace: Path, state: dict) -> bool:
    """Whether `session up` would resume *state* as the Sandbox *spec* issues."""
    issuance = _test_issuance(workspace)
    issuance.image_id = spec["image"]
    state["Config"]["Labels"].update(
        label.split("=", 1) for label in runtime_spec.labels(issuance)
    )
    docker.containers["sandbox"] = state
    return sr.container_matches_issuance("sandbox", issuance, spec=spec, workspace=workspace)


def _issued_spec(workspace: Path, **options: object) -> dict:
    spec = build_spec(
        "claude", image="sha256:test", booley_dir_source=str(workspace / ".booley"), **options
    )
    runtime_spec.pin_image(spec)
    return spec


_VSCODE_VOLUME = {"Destination": "/vscode", "Name": "vscode", "Type": "volume", "RW": True}
_WAYLAND_SOCKET = {
    "Destination": "/tmp/vscode-wayland-1234-abcd.sock",
    "Source": "/run/user/1000/wayland-0",
    "Type": "bind",
    "RW": True,
}
_HOST_SECRET = {"Destination": "/host-secret", "Source": "/etc", "Type": "bind", "RW": False}


class TestContainerResumeChecks:
    @pytest.mark.parametrize(
        ("vscode", "edit", "matches"),
        [
            pytest.param(False, lambda mounts: None, True, id="issued"),
            pytest.param(
                True,
                lambda m: m.extend([_VSCODE_VOLUME, _WAYLAND_SOCKET]),
                True,
                id="vscode-managed",
            ),
            pytest.param(
                False, lambda m: m.append(_VSCODE_VOLUME), False, id="headless-vscode-volume"
            ),
            pytest.param(False, lambda m: m.append(_HOST_SECRET), False, id="headless-extra"),
            pytest.param(True, lambda m: m.append(_HOST_SECRET), False, id="vscode-extra"),
            pytest.param(False, lambda m: m[0].update(Source=None), False, id="unreadable-source"),
            pytest.param(
                False,
                lambda m: m[1].update(Destination="/former-mount"),
                False,
                id="former-booley-dir",
            ),
            pytest.param(False, lambda m: m[2].update(Name="other"), False, id="other-volume"),
            pytest.param(False, lambda m: m.pop(1), False, id="missing-booley-dir"),
        ],
    )
    def test_resume_accepts_only_issued_and_vscode_managed_mounts(
        self, workspace: Path, sandbox_docker: SandboxDocker, vscode: bool, edit, matches: bool
    ) -> None:
        spec = _issued_spec(workspace)
        state = _issued_container(spec, workspace, vscode=vscode)
        edit(state["Mounts"])

        assert _resumes(sandbox_docker, spec, workspace, state) is matches

    @pytest.mark.parametrize("spec_source", ["/c/Users/dev/project", "/C/Users/dev/project"])
    def test_wsl2_mount_path_matches_spec(
        self, sandbox_docker: SandboxDocker, monkeypatch: pytest.MonkeyPatch, spec_source: str
    ) -> None:
        workspace = Path("/c/Users/dev/project")
        spec = _issued_spec(workspace)
        state = _issued_container(spec, workspace)
        monkeypatch.setattr(platform_paths, "IS_WINDOWS", True)
        spec["workspaceMount"] = f"source={spec_source},target=/work,type=bind"
        state["Mounts"][0]["Source"] = "/run/desktop/mnt/host/c/Users/dev/project"

        assert _resumes(sandbox_docker, spec, workspace, state)

    @pytest.mark.parametrize("vscode", [False, True])
    @pytest.mark.parametrize(
        "drift",
        [
            None,
            ("GIT_CONFIG_VALUE_0", None),
            ("GIT_CONFIG_VALUE_0", "Human"),
            ("BOOLEY_SANDBOX_IMAGE_ID", None),
        ],
        ids=["issued", "missing", "changed", "pre-image-identity"],
    )
    def test_resume_requires_issued_git_environment(
        self, workspace: Path, sandbox_docker: SandboxDocker, vscode: bool, drift
    ) -> None:
        spec = _issued_spec(workspace)
        state = _issued_container(spec, workspace, vscode=vscode)
        if drift is not None:
            key, value = drift
            env = [item for item in state["Config"]["Env"] if not item.startswith(f"{key}=")]
            state["Config"]["Env"] = env + ([f"{key}={value}"] if value else [])

        assert _resumes(sandbox_docker, spec, workspace, state) is (drift is None)

    @pytest.mark.parametrize(
        ("network", "matches"),
        [(dc.UNPROXIED_NETWORK, True), (dc.EGRESS_NETWORK, False)],
        ids=["routed-network", "egress-network"],
    )
    def test_resume_matches_issued_egress_topology(
        self, workspace: Path, sandbox_docker: SandboxDocker, network: str, matches: bool
    ) -> None:
        spec = _issued_spec(workspace, egress="unproxied")
        state = _issued_container(spec, workspace)
        state["NetworkSettings"]["Networks"] = {network: {}}

        assert _resumes(sandbox_docker, spec, workspace, state) is matches


@pytest.mark.parametrize(
    ("project_source", "view_source", "view_writable", "pinned"),
    [
        pytest.param("/run/desktop/mnt/host/c/Users/dev/project", None, True, True, id="wsl2"),
        pytest.param("/c/Users/dev/project", None, True, True, id="native"),
        pytest.param(
            "/run/desktop/mnt/host/c/Users/dev/project",
            "/c/Users/dev/other",
            True,
            False,
            id="mismatch",
        ),
        pytest.param("/c/Users/dev/project", None, False, False, id="read-only-view"),
        pytest.param(None, None, True, False, id="unreadable-inspect"),
    ],
)
def test_booley_dir_pin_reads_docker_desktop_sources(
    monkeypatch: pytest.MonkeyPatch,
    project_source: str | None,
    view_source: str | None,
    view_writable: bool,
    pinned: bool,
) -> None:
    # Docker Desktop reports these sources only on a Windows host, and `prepare` cannot run its
    # other host-path handling with IS_WINDOWS forced on a POSIX runner: drive the pin check.
    # Its callers decode the inspect output first, so only this call reaches the undecodable case.
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", True)
    workspace = SimpleNamespace(resolve=lambda: Path("C:/Users/dev/project"))
    mounts = [
        {
            "Destination": "/booley",
            "Source": f"{project_source}/.booley",
            "Type": "bind",
            "RW": True,
        },
        {
            "Destination": "/work/.booley",
            "Source": f"{view_source or project_source}/.booley",
            "Type": "bind",
            "RW": view_writable,
        },
    ]
    raw = json.dumps([{"Mounts": mounts}]) if project_source else "not-json"

    assert (
        sr._project_data_mount_root_is_pinned(
            raw, workspace, Path("C:/Users/dev/project/.booley"), layout="legacy"
        )
        is pinned
    )


class TestLicensedRelayLifecycle:
    @pytest.fixture
    def relay(self, wired, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
        """License the `wired` Project and fake the relay module's Docker operations."""
        from booley.eda.provisioning.licensing import flexnet_docker

        monkeypatch.setattr(runtime_spec, "requested_license", lambda *_a, **_k: _LICENSE_PROFILE)
        monkeypatch.setattr(sr.idk, "container_exists", lambda _name: False)
        fake = SimpleNamespace(relay=SimpleNamespace(relay_container="relay"))
        fake.recreate, fake.validate, fake.remove = Mock(return_value=fake.relay), Mock(), Mock()
        monkeypatch.setattr(flexnet_docker, "recreate_relay", fake.recreate)
        monkeypatch.setattr(flexnet_docker, "validate_relay", fake.validate)
        monkeypatch.setattr(flexnet_docker, "remove_relay", fake.remove)
        return fake

    def test_relay_start_failure_prevents_session_create(self, wired, relay):
        from booley.eda.provisioning.licensing import flexnet_docker

        workspace, run = wired
        relay.recreate.side_effect = flexnet_docker.RelayDockerError("unhealthy")
        with pytest.raises(sr.SessionError, match="could not start licensed Sandbox: unhealthy"):
            sr.up(workspace)
        assert not any(_argv_of(call)[:2] == ["docker", "run"] for call in run.call_args_list)

    @pytest.mark.parametrize("failure", ["create", "connect"])
    def test_failed_start_removes_session_and_relay(self, wired, relay, failure):
        from booley.eda.provisioning.licensing import flexnet_docker

        workspace, run = wired
        if failure == "create":
            run.return_value = subprocess.CompletedProcess([], 1, "", "create failed")
        else:
            relay.validate.side_effect = flexnet_docker.RelayDockerError("connect failed")
        with pytest.raises(sr.SessionError, match=f"{failure} failed"):
            sr.up(workspace)
        relay.remove.assert_called_once()
        assert relay.remove.call_args.args == (relay.relay,)
        removed = ["docker", "rm", "-f", sr.session_container_name(workspace)]
        assert (removed in [_argv_of(call) for call in run.call_args_list]) is (
            failure == "connect"
        )

    def test_resume_validates_exact_relay_before_start(self, wired, relay, monkeypatch):
        workspace, run = wired
        ours = sr.session_container_name(workspace)
        monkeypatch.setattr(sr.idk, "container_exists", lambda name: name == ours)
        monkeypatch.setattr(sr.idk, "container_running", lambda _name: False)

        sr.up(workspace)

        assert relay.validate.call_args.args[:2] == (_relay(workspace), ours)
        relay.recreate.assert_not_called()
        assert ["docker", "start", ours] in [_argv_of(call) for call in run.call_args_list]

    @pytest.mark.parametrize("relay_exists", [False, True], ids=["provisions", "validates"])
    def test_vscode_prepare_readies_relay_before_create(
        self, wired, relay, monkeypatch, relay_exists: bool
    ):
        workspace, _run = wired
        monkeypatch.setattr(sr.idk, "container_exists", lambda _name: relay_exists)
        monkeypatch.setattr(
            sr.idk, "network_exists", lambda name: relay_exists or name == dc.EGRESS_NETWORK
        )

        assert sr.prepare(workspace) == "abc"

        assert relay.recreate.called is not relay_exists
        assert relay.validate.called is relay_exists
        if relay_exists:  # validated on its own: no Session container exists yet
            assert relay.validate.call_args.args[:2] == (_relay(workspace), None)


class TestPreflight:
    @pytest.mark.parametrize(
        ("missing", "message"),
        [
            pytest.param("network", "booley init", id="egress-network"),
            pytest.param("image", "not built", id="sandbox-image"),
            pytest.param("relay-image", r"license relay image.*booley init", id="relay-image"),
        ],
    )
    def test_start_refused_before_create(
        self,
        workspace: Path,
        up_docker: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
        missing: str,
        message: str,
    ) -> None:
        if missing == "network":
            up_docker.networks.clear()
        elif missing == "image":
            up_docker.images.clear()
        else:
            monkeypatch.setattr(
                runtime_spec, "requested_license", lambda *_a, **_k: _LICENSE_PROFILE
            )

        with pytest.raises(sr.SessionError, match=message):
            sr.up(workspace)

        assert _mutations(up_docker) == []

    def test_missing_spec_names_booley_init(self, workspace: Path):
        with pytest.raises(sr.SessionError, match="run `booley init`"):
            sr.up(workspace)

    def test_unproxied_spec_creates_routed_network_without_egress_network(
        self, workspace: Path, up_docker: SandboxDocker
    ) -> None:
        image = sr.project_image.project_image_name(workspace)
        _write_spec(workspace, _spec(egress="unproxied", image=image))
        up_docker.networks.clear()

        assert sr.up(workspace) == sr.session_container_name(workspace)

        creates = [call for call in up_docker.calls if call[1:3] == ["network", "create"]]
        assert [call[-1] for call in creates] == [dc.UNPROXIED_NETWORK]


_DIGEST = "sha256:" + "a" * 64
_OTHER_DIGEST = "sha256:" + "b" * 64


def _provenance(fingerprint: str) -> dict:
    """Image record of a Booley-built Sandbox Image selected by *fingerprint*."""
    labels = {
        "io.booley.provenance.schema": "3",
        "io.booley.artifact.role": "wheel-overlay",
        "io.booley.build.recipe-fingerprint": "wheel-recipe",
        "io.booley.sandbox.selection-fingerprint": fingerprint,
    }
    return {"Config": {"Labels": labels}}


class TestImageDriftWarning:
    """F-6: [sandbox].image changed after init leaves the untracked spec frozen
    on the old image, and `session up` would silently start it. doctor already
    WARNs on the drift; `up` must surface it inline (warn only, never rewrite
    the spec)."""

    @pytest.mark.parametrize(
        ("configured", "issued", "images", "warns", "moved"),
        [
            # The spec on disk says the default image; the toml was pointed at a project image.
            pytest.param("i2c-booley-sandbox", "booley-sandbox", {}, True, 0, id="spec-vs-toml"),
            pytest.param("booley-sandbox", None, {}, False, 0, id="matching"),
            pytest.param(None, None, {}, False, 0, id="no-project-config"),
            pytest.param(
                None,
                _DIGEST,
                {_DIGEST: {"Id": _DIGEST}, "PROJECT": {"Id": _DIGEST}},
                False,
                0,
                id="pinned-digest-same-id",
            ),
            # #786: the tag moved but the selection is the same: a DEBUG note, never a WARNING.
            pytest.param(
                None,
                _DIGEST,
                {
                    _DIGEST: {"Id": _DIGEST, **_provenance("f" * 64)},
                    "PROJECT": {"Id": _OTHER_DIGEST, **_provenance("f" * 64)},
                },
                False,
                1,
                id="same-selection-tag-move",
            ),
            pytest.param(
                None,
                _DIGEST,
                {
                    _DIGEST: {"Id": _DIGEST, **_provenance("f" * 64)},
                    "PROJECT": {"Id": _OTHER_DIGEST, **_provenance("e" * 64)},
                },
                True,
                0,
                id="logically-different",
            ),
            # 7b576678a: an external image rebuilt on the same labels is another image.
            pytest.param(
                "custom-sandbox",
                _DIGEST,
                {_DIGEST: {"Id": _DIGEST}, "custom-sandbox": {"Id": _OTHER_DIGEST}},
                True,
                0,
                id="rebuilt-external",
            ),
        ],
    )
    def test_drift_warning(
        self,
        workspace: Path,
        up_docker: SandboxDocker,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        configured: str | None,
        issued: str | None,
        images: dict[str, dict],
        warns: bool,
        moved: int,
    ) -> None:
        project = sr.project_image.project_image_name(workspace)
        if configured is not None:
            _configure_image(monkeypatch, workspace, configured)
        up_docker.images.clear()
        up_docker.images.update({project if k == "PROJECT" else k: v for k, v in images.items()})
        spec_image = issued or project
        up_docker.images.setdefault(spec_image, {"Id": _DIGEST})
        _write_spec(workspace, _spec(image=spec_image))

        with caplog.at_level("DEBUG", logger=sr.logger.name):
            sr.check_refresh_start(workspace)

        expected = configured or project
        message = f"!= [sandbox].image '{expected}'"
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert [message in r.getMessage() for r in warnings] == [True] * warns
        if warns:
            assert "`booley init" in caplog.text  # literal remedy text, not sandbox_remedy()
            assert "`booley session " in caplog.text
        notes = [r for r in caplog.records if "equivalent Sandbox Image" in r.getMessage()]
        assert [r.levelname for r in notes] == ["DEBUG"] * moved


class TestStaleBooleyBakeWarning:
    """The stale-wheel guard: `up` compares the image's build-fingerprint label
    against the current checkout and warns when the session would run stale
    Booley code. Advisory only — no verdict (pip install, unlabeled image) must
    stay silent."""

    @pytest.mark.parametrize(
        ("labels", "warns"),
        [
            pytest.param(
                {
                    "org.opencontainers.image.version": "1.0",
                    "org.opencontainers.image.revision": "def456",
                    "io.booley.wheel.source-fingerprint": "stale",
                },
                True,
                id="mismatch",
            ),
            pytest.param(
                {
                    "org.opencontainers.image.version": "1.0",
                    "org.opencontainers.image.revision": "abc123",
                    "io.booley.wheel.source-fingerprint": "current",
                },
                False,
                id="match",
            ),
            pytest.param({}, False, id="unknown-identity"),
        ],
    )
    def test_warns_only_when_the_issued_image_proves_drift(
        self,
        workspace: Path,
        up_docker: SandboxDocker,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        labels: dict[str, str],
        warns: bool,
    ) -> None:
        # The host installation's build identity is host environment, not Booley's own code.
        monkeypatch.setattr(
            sr.image_identity,
            "current_host_build_identity",
            lambda: sr.image_identity.BooleyBuildIdentity("1.0", "abc123", None, "current"),
        )
        image = sr.project_image.project_image_name(workspace)
        up_docker.images[image] = {"Id": _DIGEST, "Config": {"Labels": labels}}

        sr.check_refresh_start(workspace)

        assert ("stale Booley code" in caplog.text) is warns
        if warns:
            assert "revision abc123 -> def456" in caplog.text
            assert "wheel_source_fingerprint current -> stale" in caplog.text
            assert sr.sandbox_remedy(workspace) in caplog.text


_RELAY_SLOT_WIDTH = 16
_RELAY_SLOTS = 300


class TestT3CodeRelay:
    @pytest.fixture
    def relay_ports(self) -> Iterator[range]:
        """Claim a port range of this test's own: concurrent test processes share the host.

        The product range is 100 ports from 3780 that every test process would prefer first, so
        one test's relay (or its "nothing listens here" probe) would answer another's. A slot's
        lock port is held for the test, which makes its range exclusive among test processes.
        """
        start = os.getpid() % _RELAY_SLOTS
        for offset in range(_RELAY_SLOTS):
            slot = (start + offset) % _RELAY_SLOTS
            lock = socket.socket()
            try:
                lock.bind(("127.0.0.1", 25000 + slot))
            except OSError:
                lock.close()
                continue
            break
        else:  # pragma: no cover - every slot is held by another test process
            pytest.fail("no free relay port slot")
        with lock:
            yield range(20000 + slot * _RELAY_SLOT_WIDTH, 20000 + (slot + 1) * _RELAY_SLOT_WIDTH)

    @staticmethod
    def relay_module(monkeypatch: pytest.MonkeyPatch, ports: range):
        """The relay module, pointed at *ports* instead of the product range."""
        from booley.runtime import t3_relay

        monkeypatch.setattr(t3_relay, "PORTS", ports)
        return t3_relay

    @pytest.fixture
    def relay_cli(
        self,
        up_docker: SandboxDocker,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        relay_ports: range,
    ):
        """Run `booley session up` and `down` on the Docker fake; stop every relay at teardown."""
        from booley.harness import booley as cli
        from booley.harness import doctor

        isolate_session_cli(monkeypatch, tmp_path)
        # Doctor, its due check and upgrade advice need a real image and host state that the
        # Docker fake lacks, and they are not what these tests observe.
        monkeypatch.setattr(health, "due_reason", lambda _root: None)
        monkeypatch.setattr(cli, "_report_upgrade_before_session", lambda _root: None)
        monkeypatch.setattr(
            doctor, "observe_deep_status", lambda *_a, **_k: SimpleNamespace(render=lambda: "")
        )
        roots: dict[Path, None] = {}
        # Any exec answers a pairing request with a link on the relay port it names.
        up_docker.exec_reply = lambda argv: completed(
            stdout=json.dumps({"pairUrl": f"http://127.0.0.1:{argv[-1].split()[-1]}/pair#token=t"})
        )

        def run(command: list[str], root: Path) -> tuple[str, str]:
            self.relay_module(monkeypatch, relay_ports)
            roots[root] = None
            assert run_cli(monkeypatch, ["-C", str(root), "session", *command]) == 0
            captured = capsys.readouterr()
            return captured.out, captured.err

        def up(root: Path) -> tuple[int, str]:
            out, err = run(["up"], root)
            assert "localhost" not in out
            match = re.search(r"T3 Code relay: 127\.0\.0\.1:(\d+)", out)
            assert match, f"no T3 Code relay address in: {out}"
            return int(match[1]), err

        yield SimpleNamespace(up=up, run=run, down=lambda root: run(["down"], root)[0])
        for root in roots:
            run(["down"], root)

    linux_only = pytest.mark.skipif(
        sys.platform != "linux", reason="a script runs as docker; 127.0.0.2 is Linux loopback"
    )

    @linux_only
    def test_session_up_relays_host_loopback_into_the_sandbox_until_down(
        self,
        workspace: Path,
        tmp_path: Path,
        relay_cli: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def connect(host: str, port: int) -> socket.socket:
            return socket.create_connection((host, port), timeout=hang_guard_s())

        with echo_listener() as echo:
            sandbox_exec_on_path(
                monkeypatch, tmp_path / "bin", echo.port, sr.session_container_name(workspace)
            )
            port, _ = relay_cli.up(workspace)
            with connect("127.0.0.1", port) as client:
                client.sendall(b"ping")
                client.shutdown(socket.SHUT_WR)
                assert b"".join(iter(lambda: client.recv(64), b"")) == b"ping"
            with pytest.raises(
                OSError
            ):  # a wildcard bind would accept this other loopback address
                connect("127.0.0.2", port)
            down = relay_cli.down(workspace)

        assert "stopped the T3 Code relay" in down
        with pytest.raises(OSError):
            connect("127.0.0.1", port)

    @linux_only
    def test_a_client_reset_ends_the_connection_into_the_sandbox(
        self,
        workspace: Path,
        tmp_path: Path,
        relay_cli: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with echo_listener() as echo:
            sandbox_exec_on_path(
                monkeypatch, tmp_path / "bin", echo.port, sr.session_container_name(workspace)
            )
            port, _ = relay_cli.up(workspace)
            with socket.create_connection(("127.0.0.1", port), timeout=hang_guard_s()) as client:
                client.sendall(b"ping")
                assert client.recv(64) == b"ping"
                # Closing with a zero linger time sends a reset, not a graceful close.
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

            assert echo.closed.wait(hang_guard_s())

    def test_the_relay_never_imports_from_its_working_directory(
        self,
        workspace: Path,
        tmp_path: Path,
        relay_ports: range,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        t3_relay = self.relay_module(monkeypatch, relay_ports)
        shadow, marker = tmp_path / "shadow", tmp_path / "ran"
        shadow.mkdir()
        (shadow / "booley.py").write_bytes(f"open({str(marker)!r}, 'w').close()\n".encode())
        monkeypatch.chdir(shadow)
        try:
            t3_relay.start(workspace, "one")
        finally:
            t3_relay.stop(workspace)

        assert not marker.exists()

    def test_start_reuses_a_running_relay_and_replaces_one_for_another_container(
        self, workspace: Path, relay_ports: range, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        t3_relay = self.relay_module(monkeypatch, relay_ports)

        def record() -> dict[str, object]:
            document = t3_relay.relay_store().read(str(workspace.resolve()))
            assert document is not None
            return document

        first = t3_relay.start(workspace, "one")
        try:
            one = record()
            assert t3_relay.start(workspace, "one") == first
            assert record() == one
            # The old relay holds the port until it ends, so the same port is a new relay.
            assert t3_relay.start(workspace, "two") == first
            assert record()["container"] == "two"
            assert record()["process"] != one["process"]
        finally:
            assert t3_relay.stop(workspace)

    @pytest.mark.parametrize(
        ("changes", "problem"),
        [
            ({"port": 80}, "port 80 is outside"),
            ({"process": {"pid": 1}}, "not a process identity"),
            ({"schema_version": 2}, "schema_version must be 1"),
        ],
    )
    def test_session_up_refuses_an_invalid_relay_record(
        self,
        workspace: Path,
        relay_cli: SimpleNamespace,
        relay_ports: range,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        changes: dict[str, object],
        problem: str,
    ) -> None:
        t3_relay = self.relay_module(monkeypatch, relay_ports)
        root = str(workspace.resolve())
        record = {
            "schema_version": 1,
            "project_root": root,
            "port": t3_relay.PORTS[0],
            "container": "c",
            "process": None,
        }
        t3_relay.relay_store().write(root, json.dumps({**record, **changes}))

        assert run_cli(monkeypatch, ["-C", root, "session", "up"]) == 2

        assert problem in capsys.readouterr().err
        t3_relay.relay_store().path(root).unlink()

    def test_relay_port_is_per_project_stable_and_moves_off_a_held_port(
        self,
        workspace: Path,
        tmp_path: Path,
        up_docker: SandboxDocker,
        relay_cli: SimpleNamespace,
        relay_ports: range,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        _issue_project(up_docker, other, monkeypatch)

        first, _ = relay_cli.up(workspace)
        second, _ = relay_cli.up(other)
        relay_cli.down(workspace)
        again, notice = relay_cli.up(workspace)
        relay_cli.down(workspace)
        with socket.socket() as holder:
            with suppress(OSError):  # already held by another process is as good
                holder.bind(("127.0.0.1", first))
                holder.listen()
            moved, warning = relay_cli.up(workspace)

        assert {first, second, moved} <= set(relay_ports)
        assert first != second
        assert (again, notice) == (first, "")
        assert moved not in {first, second}
        assert f"127.0.0.1:{first} is held" in warning
        assert "must pair again" in warning

    def record_port(self, root: Path, port: int) -> None:
        """Record *port* for the Project at *root*, as an earlier `session up` and `down` did."""
        from booley.runtime import t3_relay

        document = {
            "schema_version": 1,
            "project_root": str(root.resolve()),
            "port": port,
            "container": "c",
            "process": None,
        }
        t3_relay.relay_store().write(str(root.resolve()), json.dumps(document))

    def test_a_held_recorded_port_moves_to_the_next_port_up_not_the_first_free_one(
        self, workspace: Path, relay_cli: SimpleNamespace, relay_ports: range
    ) -> None:
        recorded = relay_ports[10]
        self.record_port(workspace, recorded)
        assert relay_cli.up(workspace) == (recorded, "")
        relay_cli.down(workspace)

        with socket.socket() as holder:
            with suppress(OSError):  # already held by another process is as good
                holder.bind(("127.0.0.1", recorded))
                holder.listen()
            moved, warning = relay_cli.up(workspace)

        assert moved > recorded
        assert f"127.0.0.1:{recorded} is held" in warning

    def test_a_free_recorded_port_stays_when_another_project_recorded_it_too(
        self,
        workspace: Path,
        tmp_path: Path,
        up_docker: SandboxDocker,
        relay_cli: SimpleNamespace,
        relay_ports: range,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        _issue_project(up_docker, other, monkeypatch)
        recorded = relay_ports[10]
        self.record_port(other, recorded)
        self.record_port(workspace, recorded)

        assert relay_cli.up(workspace) == (recorded, "")

    def test_session_up_fails_when_the_range_has_no_free_port(
        self,
        workspace: Path,
        relay_cli: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from booley.runtime import t3_relay

        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen()
            port = holder.getsockname()[1]
            # The real range is 100 ports on a shared host: a one-port range stands in for it.
            monkeypatch.setattr(t3_relay, "PORTS", range(port, port + 1))

            assert run_cli(monkeypatch, ["-C", str(workspace), "session", "up"]) == 2

        assert f"no free port in 127.0.0.1:{port}-{port}" in capsys.readouterr().err

    @linux_only
    def test_pairing_links_ui_state_and_no_ui(
        self,
        workspace: Path,
        tmp_path: Path,
        up_docker: SandboxDocker,
        relay_cli: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from booley.runtime import session_admission

        caplog.set_level(logging.DEBUG)
        monkeypatch.setattr(
            session_admission, "running_project_sandboxes", real_running_project_sandboxes
        )
        up_docker.created = _sandbox_state(workspace)
        t3 = serve_t3_code(monkeypatch)
        runs = run_registrar_on_post_start(up_docker, workspace, monkeypatch)

        no_ui, _ = relay_cli.run(["up", "--no-ui"], workspace)
        off, _ = relay_cli.run(["status"], workspace)
        up, _ = relay_cli.run(["up"], workspace)
        pair, _ = relay_cli.run(["pair"], workspace)
        status, _ = relay_cli.run(["status"], workspace)

        assert "pair#token" not in no_ui
        assert all("t3:off" in run for run in runs[:2])  # the create and the start hook
        assert off == "running\nUI: off\n"
        assert len(t3.served) == 1
        (port,) = re.findall(r"T3 Code relay: 127\.0\.0\.1:(\d+)", up)
        base = f"http://127.0.0.1:{port}"
        assert f"{base}/pair#token={TOKEN}-1" in up
        assert f"{base}/pair#token={TOKEN}-2" in pair
        assert [
            (cmd[cmd.index("--ttl") + 1], cmd[cmd.index("--base-url") + 1]) for cmd in t3.paired
        ] == [("5m", base)] * 2
        assert status.endswith(f"T3 Code: running\nT3 Code relay: 127.0.0.1:{port}\n")
        files = (f.read_text(errors="replace") for f in tmp_path.rglob("*") if f.is_file())
        assert TOKEN not in "".join([caplog.text, *runs, *files])

    def test_session_up_warns_and_succeeds_when_the_pairing_link_fails(
        self, workspace: Path, up_docker: SandboxDocker, relay_cli: SimpleNamespace
    ) -> None:
        up_docker.exec_reply = lambda _argv: completed(1, TOKEN)

        _, err = relay_cli.up(workspace)

        assert "no T3 Code pairing link: t3 auth pairing create failed (exit 1)" in err
        assert TOKEN not in err

    def test_status_and_pair_report_an_unobservable_relay(
        self, workspace: Path, attached: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.core.process_identity import ProcessIdentity
        from booley.runtime import t3_relay
        from booley.runtime.pid import ProcessState, capture_process_identity

        name = sr.session_container_name(workspace)
        _running_headless(attached.docker, workspace)
        attached.docker.exec_reply = lambda argv: completed(stdout="running\n")
        identity = capture_process_identity(os.getpid())
        assert isinstance(identity, ProcessIdentity)
        root = str(workspace.resolve())
        record = {"schema_version": 1, "project_root": root, "port": 3780, "container": name}
        t3_relay.relay_store().write(
            root, json.dumps({**record, "process": identity.to_payload()})
        )
        monkeypatch.setattr(
            t3_relay,
            "observe_process",
            lambda _identity: SimpleNamespace(state=ProcessState.UNKNOWN),
        )

        assert sr.ui_status(workspace) == "T3 Code: running\nT3 Code relay: unknown"
        with pytest.raises(t3_relay.T3RelayError, match="cannot tell whether"):
            sr.pairing_link(workspace)


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        (subprocess.CompletedProcess([], 1, TOKEN, TOKEN), r"failed \(exit 1\)"),
        (subprocess.CompletedProcess([], 0, f"not json {TOKEN}", ""), "is not a link"),
        (
            subprocess.CompletedProcess(
                [], 0, json.dumps({"pairUrl": f"http://localhost:3780/pair#token={TOKEN}"}), ""
            ),
            "is not a link",
        ),
        (subprocess.TimeoutExpired("docker", 1), r"no pairing link in \d+ seconds"),
    ],
    ids=["exit", "not-json", "localhost", "timeout"],
)
def test_a_bad_pairing_reply_is_refused_without_its_token(
    reply: subprocess.CompletedProcess[str] | Exception,
    problem: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def answer(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if isinstance(reply, Exception):
            raise reply
        return reply

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(sr, "run_captured_bounded", answer)

    with pytest.raises(sr.SessionError, match=problem) as refused:
        sr.create_pairing_link("c", 3780)

    assert TOKEN not in f"{refused.value}{caplog.text}"


@pytest.mark.parametrize("argv", [[], ["unknown"], ["pair"], ["pair", "not-a-port"]])
def test_the_in_sandbox_t3_entry_refuses_a_bad_verb_or_port(argv: list[str]) -> None:
    from booley.harness import incontainer_t3

    with pytest.raises(SystemExit) as refused:
        incontainer_t3.main(argv)

    assert refused.value.code == 2


@pytest.mark.parametrize(
    ("sandbox", "problem"),
    [(None, "no running Sandbox"), ("editor", "VS Code owns"), ("headless", "the UI is off")],
)
def test_pair_is_refused_without_a_headless_sandbox_and_relay(
    workspace: Path, attached: SimpleNamespace, sandbox: str | None, problem: str
) -> None:
    if sandbox is not None:
        name = sandbox if sandbox == "editor" else sr.session_container_name(workspace)
        attached.docker.containers[name] = _sandbox_state(
            workspace, vscode=sandbox == "editor", name=name
        )

    with pytest.raises(sr.SessionError, match=problem):
        sr.pairing_link(workspace)


def _record_relay(workspace: Path, monkeypatch: pytest.MonkeyPatch, process: str) -> None:
    """Record a relay into the canonical Sandbox whose process is observed as *process*."""
    from booley.core.process_identity import ProcessIdentity
    from booley.runtime import t3_relay
    from booley.runtime.pid import ProcessState, capture_process_identity

    identity = capture_process_identity(os.getpid())
    assert isinstance(identity, ProcessIdentity)
    root = str(workspace.resolve())
    record = {"schema_version": 1, "project_root": root, "port": 3780}
    record.update(container=sr.session_container_name(workspace), process=identity.to_payload())
    t3_relay.relay_store().write(root, json.dumps(record))
    state = SimpleNamespace(state=ProcessState[process])
    monkeypatch.setattr(t3_relay, "observe_process", lambda _identity: state)


def _ui_findings(workspace: Path) -> list[tuple[str, str, str]]:
    from booley.runtime import t3_inspection

    report = t3_inspection.inspect_ui(workspace)
    return [(str(f.severity), f.message, f.fix) for f in report.findings]


def _t3_not_applicable(reason: str) -> list[tuple[str, str, str]]:
    return [
        ("skip", f"T3 Code {check} check not applicable - {reason}", "")
        for check in ("server", "relay")
    ]


_SERVER_OK = ("pass", "T3 Code answers on 127.0.0.1:3773 in the Sandbox", "")
_SERVER_OFF = (
    "fail",
    "T3 Code does not answer on 127.0.0.1:3773 in the Sandbox",
    "run `booley session up`, which starts T3 Code",
)
_SERVER_UNKNOWN = (
    "fail",
    "Doctor cannot tell whether T3 Code answers in the Sandbox",
    "run `booley session up`, then rerun `booley doctor`",
)
_RELAY_OK = ("pass", "T3 Code relay runs on 127.0.0.1:3780", "")
_RELAY_DOWN = (
    "fail",
    "the T3 Code relay is not running for this Sandbox",
    "run `booley session up`, which starts the relay",
)
_RELAY_UNOBSERVABLE = (
    "fail",
    f"cannot tell whether the T3 Code relay (process {os.getpid()}) still runs; "
    "run `booley session up` again",
    "rerun `booley doctor` after the repair the message names",
)
_RELAY_UNJUDGED = (
    "skip",
    "T3 Code relay check skipped - no relay is recorded and Doctor cannot tell whether "
    "T3 Code runs",
    "",
)
_UI_OFF = _t3_not_applicable("the UI is off (booley session up --no-ui)")


@pytest.mark.parametrize(
    ("sandbox", "t3", "relay", "expected"),
    [
        pytest.param("headless", "running", "RUNNING", [_SERVER_OK, _RELAY_OK], id="healthy"),
        pytest.param("headless", "off", "RUNNING", [_SERVER_OFF, _RELAY_OK], id="server-off"),
        pytest.param("headless", "running", "DEAD", [_SERVER_OK, _RELAY_DOWN], id="relay-died"),
        pytest.param("headless", "off", "DEAD", [_SERVER_OFF, _RELAY_DOWN], id="both-died"),
        pytest.param(
            "headless",
            "running",
            "UNKNOWN",
            [_SERVER_OK, _RELAY_UNOBSERVABLE],
            id="relay-unobservable",
        ),
        pytest.param(
            "headless", "running", None, [_SERVER_OK, _RELAY_DOWN], id="relay-unrecorded"
        ),
        pytest.param(
            "headless", "", None, [_SERVER_UNKNOWN, _RELAY_UNJUDGED], id="server-unknown"
        ),
        pytest.param(
            "stopped",
            "off",
            "RUNNING",
            _t3_not_applicable("the Sandbox is not running"),
            id="stopped",
        ),
        pytest.param("headless", "off", None, _UI_OFF, id="no-ui"),
        pytest.param(
            "editor",
            "off",
            None,
            _t3_not_applicable("VS Code owns the Sandbox, which has no T3 Code relay"),
            id="vscode",
        ),
    ],
)
def test_t3_code_ui_inspection(
    workspace: Path,
    attached: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    sandbox: str,
    t3: str,
    relay: str | None,
    expected: list[tuple[str, str, str]],
) -> None:
    name = "editor" if sandbox == "editor" else sr.session_container_name(workspace)
    attached.docker.containers[name] = _sandbox_state(
        workspace, vscode=sandbox == "editor", running=sandbox != "stopped", name=name
    )
    attached.docker.exec_reply = lambda _argv: completed(0 if t3 else 1, f"{t3}\n")
    if relay is not None:
        _record_relay(workspace, monkeypatch, relay)

    assert _ui_findings(workspace) == expected


def test_t3_code_ui_inspection_skips_when_the_sandbox_cannot_be_listed(
    workspace: Path, attached: SimpleNamespace
) -> None:
    attached.docker.failing.add("ps")

    ((severity, message, _fix),) = _ui_findings(workspace)

    assert severity == "skip"
    assert message.startswith("T3 Code server and relay checks could not run: ")


def test_no_ui_start_of_a_stopped_sandbox_leaves_no_ui_to_check(
    workspace: Path, attached: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = sr.session_container_name(workspace)
    attached.docker.containers[name] = _sandbox_state(workspace, running=False)
    attached.docker.exec_reply = lambda _argv: completed(stdout="off\n")
    _record_relay(workspace, monkeypatch, "DEAD")

    assert sr.ensure(workspace, ui=False).started

    assert _ui_findings(workspace) == _UI_OFF


class TestDownAndStatus:
    @pytest.mark.parametrize("relay_exists", [False, True], ids=["nothing", "relay-only"])
    def test_down_without_a_sandbox_removes_only_the_relay(
        self, workspace: Path, host_docker: SandboxDocker, relay_exists: bool
    ) -> None:
        relay = _relay(workspace)
        if relay_exists:
            host_docker.networks.add(relay.private_network)
        hook = Mock()

        assert sr.down(workspace, before_stop=hook) == sr.DownResult(relay=relay_exists)

        assert host_docker.removed == ([relay.relay_container] if relay_exists else [])
        hook.assert_not_called()

    @pytest.mark.parametrize("owner", ["unproven", "vscode"])
    def test_down_refuses_before_any_mutation(
        self,
        workspace: Path,
        host_docker: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
        owner: str,
    ) -> None:
        from booley.runtime import session_admission

        def found(*_args: object, **_kwargs: object) -> tuple[SimpleNamespace, ...]:
            if owner == "unproven":
                raise session_admission.AdmissionError("identity disagrees")
            return (SimpleNamespace(container_id="id", name="ed", running=True, vscode=True),)

        monkeypatch.setattr(session_admission, "running_project_sandboxes", found)
        host_docker.containers[sr.session_container_name(workspace)] = _headless_state(workspace)
        hook = Mock()

        message = "identity disagrees" if owner == "unproven" else "VS Code owns"
        with pytest.raises(sr.SessionError, match=message):
            sr.down(workspace, before_stop=hook)

        assert host_docker.calls == []
        hook.assert_not_called()

    @pytest.mark.parametrize("running", [True, False], ids=["running", "stopped-container"])
    def test_down_runs_before_stop_then_stops(
        self,
        workspace: Path,
        host_docker: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
        running: bool,
    ) -> None:
        from booley.runtime import session_admission

        name = sr.session_container_name(workspace)
        host_docker.containers[name] = _headless_state(workspace, running=running)
        item = SimpleNamespace(container_id="c" * 64, name=name, running=True, vscode=False)
        found = (item,) if running else ()
        monkeypatch.setattr(
            session_admission, "running_project_sandboxes", lambda *_args, **_kwargs: found
        )

        result = sr.down(workspace, before_stop=host_docker.calls.append)

        assert result.headless is True
        stopped = item.container_id if running else name
        assert host_docker.calls == [
            item if running else None,
            ["docker", "stop", stopped],
            ["docker", "rm", "-f", stopped],
        ]

    @pytest.mark.parametrize("failure", ["stop", "rm"])
    def test_down_reports_headless_docker_failure(
        self, workspace: Path, host_docker: SandboxDocker, failure: str
    ) -> None:
        host_docker.containers[sr.session_container_name(workspace)] = _headless_state(workspace)
        host_docker.failing.add(failure)

        message = "cannot remove" if failure == "rm" else "cannot stop"
        with pytest.raises(sr.SessionError, match=message):
            sr.down(workspace)

    @pytest.mark.parametrize(
        ("running", "pending", "expected"),
        [
            (None, True, "start-pending"),
            (None, False, "absent"),
            (False, False, "stopped"),
            (True, False, "running"),
        ],
    )
    def test_status_reports_each_state(
        self,
        workspace: Path,
        host_docker: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
        running: bool | None,
        pending: bool,
        expected: str,
    ) -> None:
        from booley.runtime import session_admission

        monkeypatch.setattr(session_admission, "has_pending_claim", lambda _root: pending)
        if running is not None:
            host_docker.containers[sr.session_container_name(workspace)] = _headless_state(
                workspace, running=running
            )

        assert sr.status(workspace) == expected

    def test_status_reports_claim_inspection_failure(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime import session_admission

        def fail(_root: Path) -> bool:
            raise session_admission.AdmissionError("claim unavailable")

        monkeypatch.setattr(session_admission, "has_pending_claim", fail)

        with pytest.raises(sr.SessionError, match="claim unavailable"):
            sr.status(workspace)


# ---------------------------------------------------------------------------
# conflicting_vscode_session()
# ---------------------------------------------------------------------------


class TestSessionsOnStaleImage:
    """F-9: `booley init` rebuilds the tag, but a container born from the old
    image keeps serving it — and the tag being unchanged is what hides that. The
    probe therefore compares resolved image IDs. It only gates an advisory
    warning, so it never raises: a missing image is silent, and a Docker failure
    resolving the image logs a warning and reports no stale sessions."""

    NEW_ID = "sha256:aaa"
    OLD_ID = "sha256:bbb"
    IMAGE = "i2c-booley-sandbox"

    @pytest.fixture
    def docker(self, monkeypatch: pytest.MonkeyPatch) -> SandboxDocker:
        docker = serve_sandbox_docker(monkeypatch)
        docker.images[self.IMAGE] = {"Id": self.NEW_ID}
        return docker

    @staticmethod
    def _running(image: str, folder: Path | None = None) -> dict:
        labels = {"booley.role": "interactive"}
        if folder is not None:
            labels["devcontainer.local_folder"] = str(folder)
        return {"Image": image, "State": {"Running": True}, "Config": {"Labels": labels}}

    @pytest.mark.parametrize(
        ("container", "image", "folder", "stale"),
        [
            pytest.param("ours", OLD_ID, None, True, id="ours-on-old-image"),
            pytest.param("editor", OLD_ID, "workspace", True, id="vscode-for-this-folder"),
            # The rebuild was a no-op (all layers cached): nothing to restart for.
            pytest.param("ours", NEW_ID, None, False, id="same-image-id"),
            pytest.param("editor", OLD_ID, "elsewhere", False, id="another-workspace"),
            pytest.param(
                "editor",
                OLD_ID,
                "case-variant",
                False,
                id="case-variant-folder",
                marks=pytest.mark.skipif(
                    sys.platform != "linux", reason="needs a case-sensitive filesystem"
                ),
            ),
            pytest.param(
                "editor",
                OLD_ID,
                "symlink",
                True,
                id="symlinked-folder",
                marks=pytest.mark.skipif(
                    sys.platform == "win32", reason="symlinks need privileges on Windows"
                ),
            ),
        ],
    )
    def test_reports_only_containers_on_a_superseded_image(
        self,
        workspace: Path,
        docker: SandboxDocker,
        container: str,
        image: str,
        folder: str | None,
        stale: bool,
    ) -> None:
        if folder == "symlink":
            (workspace.parent / "alias").symlink_to(workspace, target_is_directory=True)
        folders = {
            "workspace": workspace,
            "elsewhere": workspace.parent / "elsewhere",
            # `<tmp>/I2C` differs from `<tmp>/i2c` only in case: another directory on Linux.
            "case-variant": workspace.parent / "I2C",
            "symlink": workspace.parent / "alias",
            None: None,
        }
        name = sr.session_container_name(workspace) if container == "ours" else "goofy_ellis"
        docker.containers[name] = self._running(image, folders[folder])

        assert sr.sessions_on_stale_image(workspace, self.IMAGE) == ([name] if stale else [])

    @pytest.mark.parametrize("case", ["none-running", "unbuilt-image", "vanished-container"])
    def test_silent_without_evidence(
        self, workspace: Path, docker: SandboxDocker, case: str
    ) -> None:
        ours = sr.session_container_name(workspace)
        if case != "none-running":
            docker.containers[ours] = self._running(self.OLD_ID)
        if case == "unbuilt-image":
            docker.images.clear()
        if case == "vanished-container":  # gone between `docker ps` and the inspect
            docker.failing.add(ours)

        assert sr.sessions_on_stale_image(workspace, self.IMAGE) == []

    def test_docker_failure_warns_and_reports_no_stale_sessions(
        self, workspace: Path, docker: SandboxDocker, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The probe stays advisory, but says why it is blind.
        docker.missing = True
        with caplog.at_level("WARNING", logger=sr.logger.name):
            assert sr.sessions_on_stale_image(workspace, self.IMAGE) == []
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert self.IMAGE in warnings[0]
        assert "cannot check running Sandboxes" in warnings[0]
        assert "docker" in warnings[0]

    def test_up_names_a_stale_vscode_container_and_its_remedy(
        self, workspace: Path, up_docker: SandboxDocker, caplog: pytest.LogCaptureFixture
    ) -> None:
        up_docker.containers["editor"] = _vscode_container(workspace)  # on `_NEW_IMAGE`

        sr.up(workspace)

        assert "'editor' is running an image superseded by a rebuild" in caplog.text
        assert "use Dev Containers: Rebuild Container." in caplog.text
        assert "session down" not in caplog.text

    def test_stale_headless_session_is_told_to_refresh(self, workspace: Path) -> None:
        advice = sr.stale_image_advice(workspace, sr.session_container_name(workspace))

        assert "booley session refresh" in advice
        assert "session down" not in advice


class _ParallelEnterProbe:
    """Hold the first `enter` inside its Docker calls, under the lifecycle lock."""

    def __init__(self) -> None:
        self.first_selecting = threading.Event()
        self.release_first = threading.Event()
        self.second_waiting = threading.Event()
        self.execution_barrier = threading.Barrier(2)
        self.guard = threading.Lock()
        self.blocked = False

    def hold_first_call(self, _argv: list[str]) -> None:
        with self.guard:
            first = not self.blocked
            self.blocked = True
        if first:
            self.first_selecting.set()
            assert self.release_first.wait(timeout=hang_guard_s())


def _running_headless(docker: SandboxDocker, workspace: Path) -> None:
    name = sr.session_container_name(workspace)
    docker.containers[name] = _sandbox_state(workspace)


class TestRunProjectCommand:
    COMMAND: ClassVar[list[str]] = ["claude", "setup-token"]

    @staticmethod
    def _attachments(attached: SimpleNamespace) -> list[tuple[str, list[str], dict]]:
        return [
            (container, command, kwargs) for _, container, command, kwargs in attached.commands
        ]

    def test_reuses_valid_running_vscode_runtime(
        self, workspace: Path, attached: SimpleNamespace
    ) -> None:
        attached.docker.containers["editor"] = _sandbox_state(
            workspace, vscode=True, name="editor"
        )

        assert sr.run_project_command(workspace, self.COMMAND, tty=True) == 0

        ((container, command, kwargs),) = self._attachments(attached)
        assert (container, command) == ("id-editor", self.COMMAND)
        assert (kwargs["tty"], kwargs["cwd"]) == (True, None)
        # The container already holds the credential; the spec only carries a placeholder.
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in kwargs["env"]
        assert kwargs["env"]["HTTP_PROXY"] == dc.PROXY_URL
        assert _mutations(attached.docker) == []

    def test_starts_headless_runtime_when_none_is_running(
        self, workspace: Path, attached: SimpleNamespace
    ) -> None:
        name = sr.session_container_name(workspace)
        attached.docker.created = _sandbox_state(workspace)

        assert sr.run_project_command(workspace, self.COMMAND, tty=False) == 0

        assert [call[1] for call in attached.docker.calls if call[1] == "run"] == ["run"]
        ((container, command, kwargs),) = self._attachments(attached)
        assert (container, command, kwargs["tty"]) == (f"id-{name}", self.COMMAND, False)

    def test_recovers_interrupted_invalidation_before_selecting_runtime(
        self, workspace: Path, attached: SimpleNamespace
    ) -> None:
        issuance_invalidation.prepare(str(workspace.resolve()))

        with pytest.raises(sr.SessionError, match="recovered interrupted Sandbox host state"):
            sr.run_project_command(workspace, self.COMMAND)

        assert attached.commands == []
        assert not any(call[1] == "run" for call in attached.docker.calls)

    def test_parallel_enters_wait_then_execute_concurrently(
        self,
        workspace: Path,
        up_docker: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from booley.runtime import lifecycle_lock, session_admission

        probe = _ParallelEnterProbe()
        real_wait = lifecycle_lock.wait_for_file_lock

        def observed_wait(handle, *, timeout_s, on_wait=None) -> None:
            if probe.first_selecting.is_set():
                probe.second_waiting.set()
            real_wait(handle, timeout_s=timeout_s, on_wait=on_wait)

        monkeypatch.setattr(lifecycle_lock, "wait_for_file_lock", observed_wait)
        monkeypatch.setattr(
            session_admission, "running_project_sandboxes", real_running_project_sandboxes
        )
        commands = attach_commands(
            monkeypatch, before=lambda: probe.execution_barrier.wait(timeout=hang_guard_s())
        )
        _running_headless(up_docker, workspace)
        up_docker.on_call = probe.hold_first_call

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(sr.enter, workspace, ["bwave", "first"], tty=False)
            assert probe.first_selecting.wait(timeout=hang_guard_s())
            second = executor.submit(sr.enter, workspace, ["bwave", "second"], tty=False)
            assert probe.second_waiting.wait(timeout=hang_guard_s())
            assert not second.done()
            probe.release_first.set()
            assert first.result(timeout=hang_guard_s()) == 0
            assert second.result(timeout=hang_guard_s()) == 0

        assert sorted(command[-1] for _, _, command, _ in commands) == ["first", "second"]

    def test_stale_running_runtime_blocks_headless_creation(
        self, workspace: Path, attached: SimpleNamespace
    ) -> None:
        stale = _sandbox_state(workspace)
        stale["Image"] = _OLD_IMAGE
        attached.docker.containers[sr.session_container_name(workspace)] = stale

        with pytest.raises(sr.SessionError, match="does not match the current host issuance"):
            sr.run_project_command(workspace, self.COMMAND)

        assert not any(call[1] == "run" for call in attached.docker.calls)

    def test_multiple_running_runtimes_are_refused(
        self, workspace: Path, attached: SimpleNamespace
    ) -> None:
        _running_headless(attached.docker, workspace)
        attached.docker.containers["editor"] = _sandbox_state(
            workspace, vscode=True, name="editor"
        )

        with pytest.raises(sr.SessionError, match="multiple running Sandboxes"):
            sr.run_project_command(workspace, self.COMMAND)

        assert not any(call[1] == "run" for call in attached.docker.calls)

    def test_attachment_failure_is_reported_as_session_error(
        self, workspace: Path, attached: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _running_headless(attached.docker, workspace)
        attach_commands(monkeypatch, error=OSError("docker disappeared"))

        with pytest.raises(sr.SessionError, match=r"could not attach.*docker disappeared"):
            sr.run_project_command(workspace, self.COMMAND)

    @pytest.mark.parametrize(
        ("command", "warns"),
        [
            pytest.param(["python3", "-m", "x", "--report-dir", "C:/Temp/rep"], True, id="path"),
            pytest.param(["--report-dir=C:\\Temp\\rep"], True, id="equals-form"),
            pytest.param(["python3", "--report-dir", "/tmp/rep-lint"], False, id="container-path"),
        ],
    )
    def test_mangled_arguments_warn_and_pass_verbatim(
        self,
        workspace: Path,
        attached: SimpleNamespace,
        caplog: pytest.LogCaptureFixture,
        command: list[str],
        warns: bool,
    ) -> None:
        _running_headless(attached.docker, workspace)

        assert sr.enter(workspace, command, tty=False) == 0

        # The warning is advisory: only the caller knows which arguments are paths.
        assert [attached_command for _, attached_command, _ in self._attachments(attached)] == [
            command
        ]
        assert ("Windows host paths" in caplog.text) is warns
        assert ("MSYS_NO_PATHCONV" in caplog.text) is warns


class TestSessionRefresh:
    def test_session_command_coordinator_fits_on_a_screen(self):
        import inspect

        from booley.harness import booley

        # Pins source structure, which #4359 owns; the public CLI cannot reach a line count.
        assert len(inspect.getsourcelines(booley._cmd_session)[0]) <= 50

    @pytest.fixture
    def cli_project(
        self, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> SandboxDocker:
        """A Project under isolated host state, and the Docker fake its commands reach."""
        (workspace / ".booley").mkdir()
        isolate_session_cli(monkeypatch, tmp_path)
        # The Doctor-due check reads host health state that no session command can set up.
        monkeypatch.setattr(health, "due_reason", lambda _root: None)
        return serve_sandbox_docker(monkeypatch)

    @pytest.mark.skipif(os.name != "posix", reason="POSIX mode validation")
    @pytest.mark.parametrize(
        "command",
        ["up", "enter", "down", "status", "validate", "prepare", "refresh"],
    )
    def test_public_session_commands_report_unsafe_private_store(
        self,
        command: str,
        workspace: Path,
        tmp_path: Path,
        cli_project: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        issuance_invalidation.prepare(str(workspace.resolve()))
        issuance_invalidation.cancel(str(workspace.resolve()))
        booley_config = tmp_path / "xdg-config" / "booley"
        booley_config.chmod(0o755)

        assert run_cli(monkeypatch, ["-C", str(workspace), "session", command]) == 2

        captured = capsys.readouterr()
        assert str(booley_config) in captured.err
        assert "0755" in captured.err
        assert f"chmod 700 {booley_config}" in captured.err
        assert "Traceback" not in captured.out
        assert "Traceback" not in captured.err
        assert cli_project.calls == []  # unsafe recovery must precede Docker mutation

    def test_refresh_configures_progress_before_reconciling_image(
        self,
        workspace: Path,
        cli_project: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from booley.harness import session_refresh
        from booley.harness.setup import common as setup_common
        from booley.runtime.session_refresh import RefreshResult

        result = RefreshResult(
            RefreshImage("sha256:fresh", None, None, False), "replaced", "old", "new"
        )
        events: list[str] = []
        # Only call order is pinned: the real refresh needs a Docker image rebuild, and the
        # progress configuration has no public effect to observe before it runs.
        monkeypatch.setattr(
            setup_common, "configure_progress_output", lambda: events.append("configure")
        )
        monkeypatch.setattr(
            session_refresh,
            "refresh",
            lambda *_args, **_kwargs: events.append("reconcile") or result,
        )

        assert run_cli(monkeypatch, ["-C", str(workspace), "session", "refresh"]) == 0

        assert events == ["configure", "reconcile"]

    def test_enter_strips_the_separator_and_allocates_a_tty_for_two_terminals(
        self, workspace: Path, attached: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        isolate_session_cli(monkeypatch, workspace.parent)
        # The Doctor-due check reads host health state that no session command can set up.
        monkeypatch.setattr(health, "due_reason", lambda _root: None)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        _running_headless(attached.docker, workspace)

        argv = ["-C", str(workspace), "session", "enter", "--", "echo", "ready"]
        assert run_cli(monkeypatch, argv) == 0

        ((_, command, kwargs),) = TestRunProjectCommand._attachments(attached)
        assert command == ["echo", "ready"]
        assert kwargs["tty"] is True

    @pytest.mark.parametrize(
        ("expected", "output", "failure"),
        [
            pytest.param(
                {"source": "wheel-source", "sha256": "b" * 64},
                {"source": "wheel-source", "sha256": "b" * 64},
                None,
                id="exact-identity",
            ),
            pytest.param({}, None, None, id="no-expectations"),
            pytest.param({"source": "wheel-source"}, "not-json", "wheel identity", id="malformed"),
            pytest.param(
                {"source": "wheel-source", "sha256": "b" * 64},
                {"source": "other-source", "sha256": "b" * 64},
                "source_fingerprint wheel-source -> other-source",
                id="other-source",
            ),
        ],
    )
    def test_verify_refreshed_session(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        expected: dict[str, str],
        output: dict[str, str] | str | None,
        failure: str | None,
    ) -> None:
        docker = serve_sandbox_docker(monkeypatch)
        image_id = "sha256:" + "a" * 64
        name = sr.session_container_name(workspace)
        docker.containers[name] = {"Image": image_id}
        stdout = (
            output
            if isinstance(output, str)
            else json.dumps(
                {**(output or {}), "package_version": "0.2.6", "module_version": "0.2.6"}
            )
        )
        docker.exec_reply = lambda argv: subprocess.CompletedProcess(
            argv, 0, stdout, "probe failed"
        )

        def verify() -> None:
            sr.verify_refreshed_session(
                workspace,
                image_id,
                expected_wheel_source_fingerprint=expected.get("source"),
                expected_wheel_sha256=expected.get("sha256"),
            )

        if failure is None:
            verify()
        else:
            with pytest.raises(sr.SessionError, match="wheel identity") as caught:
                verify()
            assert failure in str(caught.value)
            assert "wheel_sha256" not in str(caught.value)

        probes = [call for call in docker.calls if call[1] == "exec"]
        if expected:
            assert [call[:3] for call in probes] == [["docker", "exec", name]]
            assert probes[0][3:6] == ["python3", "-I", "-c"]
        else:
            assert probes == []

    def test_down_up_never_announces_persisted_stale_doctor_findings(
        self,
        workspace: Path,
        cli_project: SandboxDocker,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from booley.harness import doctor as doctor_module

        down = ["-C", str(workspace), "session", "down"]
        monkeypatch.setattr(sr, "down", lambda _root, **_kwargs: sr.DownResult(headless=True))
        assert run_cli(monkeypatch, down) == 0
        capsys.readouterr()

        monkeypatch.setattr(
            sr, "ensure", lambda *_args, **_kwargs: sr.EnsureResult("session", False, True)
        )
        monkeypatch.setattr(health, "due_reason", lambda _root: "Doctor inputs changed")
        monkeypatch.setattr(
            health,
            "consume_changed_snapshot",
            lambda *_args, **_kwargs: pytest.fail("stale Doctor summary was consumed"),
        )
        # The deep status would probe the Sandbox through Docker.
        monkeypatch.setattr(
            doctor_module,
            "observe_deep_status",
            lambda *_args, **_kwargs: SimpleNamespace(render=lambda: "deep status"),
        )

        assert run_cli(monkeypatch, ["-C", str(workspace), "session", "up"]) == 0

        output = capsys.readouterr()
        assert "Automatic Doctor is running" in output.err
        assert "Doctor inputs changed" in output.err
        assert "Sandbox ready: session" in output.out


def _container_matches_swap(tmp_path, monkeypatch, memory_swap, swap_limit="{}"):
    spec = build_spec(dc.APP_NONE, image="sha256:test", memory="8g", memory_swap="10g")
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda image: image)
    runtime_spec.pin_image(spec)
    environment = {**spec["remoteEnv"], **spec["containerEnv"]}
    state = {
        "Image": "sha256:test",
        "Config": {
            "Image": "sha256:test",
            "User": "agent",
            "WorkingDir": "/work",
            "Env": [f"{key}={value}" for key, value in environment.items()],
        },
        "HostConfig": {"Memory": 8 * 1024**3, "MemorySwap": memory_swap},
        "NetworkSettings": {"Networks": {dc.EGRESS_NETWORK: {}}},
    }
    monkeypatch.setattr(runtime_spec, "labels", lambda _stamp: ())
    monkeypatch.setattr(sr, "_session_hardening_matches", lambda *_args: True)
    monkeypatch.setattr(sr, "_mounts_match_spec", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        sr,
        "_docker_stdout",
        lambda argv: (
            swap_limit
            if argv[1] == "info"
            else json.dumps({})
            if "--format" in argv
            else json.dumps([state])
        ),
    )
    return sr.container_matches_issuance(
        "test", SimpleNamespace(image_id="sha256:test"), spec=spec, workspace=tmp_path
    )


def test_container_swap_limit_must_match_spec(tmp_path, monkeypatch):
    """Docker's default of twice the memory and ``-1`` do not match the issued limit."""
    assert _container_matches_swap(tmp_path, monkeypatch, 10 * 1024**3) is True
    assert _container_matches_swap(tmp_path, monkeypatch, 16 * 1024**3) is False
    assert _container_matches_swap(tmp_path, monkeypatch, -1) is False


def test_container_swap_limit_unsupported_host_reports_unlimited(tmp_path, monkeypatch):
    """A Docker host without swap-limit support drops the issued limit to ``-1``."""
    assert _container_matches_swap(tmp_path, monkeypatch, -1, swap_limit="false") is True
    assert _container_matches_swap(tmp_path, monkeypatch, -1, swap_limit="true") is False
    assert _container_matches_swap(tmp_path, monkeypatch, 16 * 1024**3, "false") is False


def test_docker_run_carries_literal_identity_and_cleanup_hooks(tmp_path):
    spec = build_spec(dc.APP_NONE)
    argv = sr.docker_run_argv(spec, tmp_path, "test")
    for key, value in spec["containerEnv"].items():
        if key.startswith("GIT_CONFIG_"):
            assert argv.count(f"{key}={value}") == 2
    assert "incontainer_git_identity_cleanup" in spec["postCreateCommand"]
    assert "incontainer_git_identity_cleanup" in spec["postStartCommand"]


def test_startup_preserves_identity_drift_diagnostic(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_load_spec", lambda _root: {})

    def refuse(*_args):
        raise runtime_spec.RuntimeSpecError(
            "[agent.git] Sandbox identity has drifted; refresh and recreate"
        )

    monkeypatch.setattr(runtime_spec, "validate", refuse)
    with pytest.raises(sr.SessionError, match=r"\[agent.git\].*refresh and recreate"):
        sr._validate_up_request(tmp_path)


def test_startup_egress_drift_names_session_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_load_spec", lambda _root: {})
    monkeypatch.setattr(sr, "sandbox_attachment", lambda _root: Attachment.HEADLESS)

    def refuse(*_args):
        raise runtime_spec.EgressDriftError(
            "Sandbox spec egress mode 'proxy' differs from host [sandbox] egress = 'unproxied'"
        )

    monkeypatch.setattr(runtime_spec, "validate", refuse)
    with pytest.raises(sr.SessionError) as raised:
        sr._validate_up_request(tmp_path)
    message = str(raised.value)
    assert "egress = 'unproxied'" in message
    assert message.endswith("run `booley session refresh` on the host")
    assert "--seed" not in message


def test_vscode_prepare_names_init_for_egress_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_refuse_outdated_vscode_containers", lambda *_args: None)

    def refuse(*_args):
        raise runtime_spec.EgressDriftError("Sandbox spec egress mode differs from host policy")

    monkeypatch.setattr(runtime_spec, "validate", refuse)
    with pytest.raises(sr.SessionError) as raised:
        sr._refuse_then_validate(tmp_path, {})
    message = str(raised.value)
    assert message.endswith(
        "on the host run `booley init`, then use Dev Containers: Rebuild Container"
    )
    assert "--seed" not in message
    assert "stop" not in message
    assert "session refresh" not in message


def test_mismatched_editor_stop_remove_down_releases_capacity(tmp_path, monkeypatch):
    from booley.runtime import session_admission as admission

    monkeypatch.setattr(admission, "admit_start", real_admit_start)
    monkeypatch.setattr(admission, "claim_vscode_start", real_claim_vscode_start)
    monkeypatch.setattr(admission, "clear_vscode_claim", real_clear_vscode_claim)
    monkeypatch.setattr(admission, "vscode_sandboxes", real_vscode_sandboxes)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        admission, "load_host_policy", lambda **kw: admission.SandboxHostPolicy(max_sessions=1)
    )
    documents = {}

    def docker(argv, **kwargs):
        if argv[1] == "ps":
            rows = []
            for key, document in documents.items():
                if "-a" not in argv and not document["State"]["Running"]:
                    continue
                row = f"{key}\teditor"
                if "-a" in argv:
                    row += f"\t{project}"
                rows.append(row)
            return subprocess.CompletedProcess(argv, 0, "\n".join(rows), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(documents[argv[3]]), "")

    assert admission.claim_vscode_start(project, run=docker)
    documents["editor-id"] = {
        "Name": "/editor",
        "Created": "2026-09-27T08:00:00Z",
        "Config": {
            "Labels": {
                "booley.role": "interactive",
                "booley.project-id": "wrong",
                "devcontainer.local_folder": str(project),
            }
        },
        "State": {"Running": True, "StartedAt": "2026-09-27T08:00:00Z"},
    }
    # Every Docker command of session_runtime goes through `_run`; no public entry takes a runner.
    monkeypatch.setattr(sr, "_run", docker)
    monkeypatch.setattr(sr.idk, "container_exists", lambda name: False)
    monkeypatch.setattr(sr.idk, "network_exists", lambda name: False)
    with pytest.raises(sr.SessionError, match="identity disagrees"):
        sr.down(project)
    documents["editor-id"]["State"]["Running"] = False
    with pytest.raises(sr.SessionError, match="identity disagrees"):
        sr.down(project)
    del documents["editor-id"]
    assert sr.down(project).claim_cleared
    admission.admit_start(project, target_name="headless", run=docker)
    assert admission.claim_vscode_start(project, run=docker)


def test_spawn_passes_credential_in_child_env_without_secret_argv(workspace, monkeypatch):
    captured = {}
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "SYNTHETIC_SECRET")
    command = sr.docker_run_argv(
        _spec(credentials={"CLAUDE_CODE_OAUTH_TOKEN": "PLAN_VALUE_UNUSED"}), workspace, "test"
    )
    monkeypatch.setattr(
        sr.subprocess, "run", lambda argv, **kwargs: captured.update(argv=argv, **kwargs)
    )
    sr._run(command)
    assert "SYNTHETIC_SECRET" not in str(captured["argv"])
    assert captured["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "SYNTHETIC_SECRET"


def test_headless_api_policy_refuses_missing_key(workspace, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    spec = _spec()
    spec["remoteEnv"]["BOOLEY_PRIMARY_AUTH"] = "api_key"
    spec["remoteEnv"]["BOOLEY_AGENT_CLIS"] = "claude=api_key"
    spec["remoteEnv"]["ANTHROPIC_API_KEY"] = "${localEnv:ANTHROPIC_API_KEY}"
    with pytest.raises(sr.SessionError, match=r"requires.*credential"):
        sr.docker_run_argv(spec, workspace, "test")


@pytest.mark.parametrize("app,key", [("claude", "ANTHROPIC_API_KEY"), ("codex", "OPENAI_API_KEY")])
def test_headless_api_policy_refuses_late_export_without_transport(
    workspace, monkeypatch, app, key
):
    monkeypatch.setenv(key, "SYNTHETIC_LATE_KEY")
    spec = _spec()
    spec["remoteEnv"]["BOOLEY_AGENT_APP"] = app
    spec["remoteEnv"]["BOOLEY_PRIMARY_AUTH"] = "api_key"
    for credential in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        spec["remoteEnv"].pop(credential, None)
    with pytest.raises(sr.SessionError, match="refresh"):
        sr.docker_run_argv(spec, workspace, "test")


def test_project_sandbox_resolution_editor_status(workspace, monkeypatch):
    from booley.core.project_identity import project_label_id as project_id

    container_id = "a" * 64
    state = {
        "Id": container_id,
        "Name": "/editor",
        "Created": "2026-01-01T00:00:00Z",
        "State": {"Running": True, "StartedAt": "2026-01-01T00:00:00Z"},
        "Config": {
            "Labels": {
                "booley.role": "interactive",
                "booley.project-id": project_id(str(workspace)),
                "devcontainer.local_folder": str(workspace),
            }
        },
        "Mounts": [{"Type": "bind", "Source": str(workspace), "Destination": "/work"}],
    }

    def run(argv, **kwargs):
        if argv[1] == "ps":
            output = (
                f"{container_id}\teditor\t{workspace}\t{project_id(str(workspace))}\tinteractive"
            )
        elif argv[1:3] == ["container", "inspect"]:
            output = json.dumps(state)
        else:
            pytest.fail(f"unexpected mutation: {argv}")
        return subprocess.CompletedProcess(argv, 0, output, "")

    from booley.runtime import session_admission

    monkeypatch.setattr(
        session_admission, "running_project_sandboxes", real_running_project_sandboxes
    )
    monkeypatch.setattr(sr, "_run", run)
    monkeypatch.setattr(sr.idk, "container_exists", lambda _: False)
    assert sr.status(workspace) == "running (owner: VS Code, name: editor)"


def _project_resolution_data(workspace, issuance):
    spec = {
        "image": issuance.image_id,
        "remoteUser": "agent",
        "workspaceFolder": "/work",
        "workspaceMount": "source=${localWorkspaceFolder},target=/work,type=bind",
        "mounts": [],
        "containerEnv": {"CONTAINER": "yes"},
        "remoteEnv": {"BOOLEY_DIR": "/booley", "CLAUDE_CODE_OAUTH_TOKEN": "secret"},
        "runArgs": ["--network", "sandbox-network"],
    }
    labels = _vscode_labels(workspace, issuance)
    state = {
        "Id": "a" * 64,
        "Name": "/editor",
        "Image": issuance.image_id,
        "Created": "2026-01-01T00:00:00Z",
        "State": {"Running": True, "StartedAt": "2026-01-01T00:00:00Z"},
        "Config": {
            "Labels": labels,
            "Image": spec["image"],
            "User": "agent",
            "WorkingDir": "/work",
            "Env": ["CONTAINER=yes"],
        },
        "HostConfig": {
            "CapDrop": ["ALL"],
            "Privileged": False,
            "PublishAllPorts": False,
            "SecurityOpt": ["no-new-privileges"],
            "PidsLimit": 4096,
            "Memory": 0,
        },
        "NetworkSettings": {"Networks": {"sandbox-network": {}}},
        "Mounts": [{"Type": "bind", "Source": str(workspace), "Destination": "/work", "RW": True}],
    }
    return spec, state


def _project_resolution_transport(state):
    documents = {state["Id"]: state}
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "ps":
            rows = []
            for key, doc in documents.items():
                if doc["State"].get("Running") is False:
                    continue
                lab = doc["Config"]["Labels"]
                if (
                    "label=booley.role=interactive" in argv
                    and lab.get("booley.role") != "interactive"
                ):
                    continue
                fields = [key, doc.get("Name", "").removeprefix("/")]
                if "--no-trunc" in argv or "-a" in argv:
                    fields.append(lab.get("devcontainer.local_folder", ""))
                if "--no-trunc" in argv:
                    fields.extend([lab.get("booley.project-id", ""), lab.get("booley.role", "")])
                rows.append("\t".join(fields))
            return subprocess.CompletedProcess(argv, 0, "\n".join(rows), "")
        if argv[1:3] == ["container", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(documents[argv[3]]), "")
        pytest.fail(f"unexpected mutation: {argv}")

    def inspect(argv):
        calls.append(argv)
        if argv[1] == "ps":
            return "\n".join(doc["Name"].removeprefix("/") for doc in documents.values())
        reference = argv[2]
        doc = documents.get(reference) or next(
            (doc for doc in documents.values() if doc.get("Name") == "/" + reference), None
        )
        if doc is None:
            return None
        if "--format" in argv and "index .Config.Labels" in argv[-1]:
            return doc["Config"]["Labels"].get("devcontainer.local_folder")
        return json.dumps(doc["Config"]["Labels"]) if "--format" in argv else json.dumps([doc])

    return documents, calls, run, inspect


@pytest.fixture
def project_sandbox_resolution(workspace, monkeypatch):
    """Actual selection and issuance matching over deterministic Docker responses."""
    from booley.runtime import session_admission

    issuance = _test_issuance(workspace)
    issuance.image_id = "sha256:" + "f" * 64
    spec, state = _project_resolution_data(workspace, issuance)
    documents, calls, run, inspect = _project_resolution_transport(state)
    monkeypatch.setattr(
        session_admission, "running_project_sandboxes", real_running_project_sandboxes
    )
    monkeypatch.setattr(sr, "_run", run)
    monkeypatch.setattr(sr, "_docker_stdout", inspect)
    monkeypatch.setattr(sr, "_recover_before_lifecycle", lambda *_: None)
    monkeypatch.setattr(sr, "_load_spec", lambda _: spec)
    monkeypatch.setattr(runtime_spec, "validate", lambda *_: issuance)
    monkeypatch.setattr(
        sr,
        "_validate_up_request",
        lambda *_: sr.UpRequest(
            spec,
            issuance,
            None,
            sr.session_container_name(workspace),
            runtime_spec.labels(issuance),
            None,
        ),
    )
    return SimpleNamespace(
        state=state, documents=documents, calls=calls, spec=spec, issuance=issuance, run=run
    )


def test_project_sandbox_resolution_reuses_editor_for_up_and_both_enters(
    workspace, project_sandbox_resolution, monkeypatch
):
    fixture = project_sandbox_resolution
    attached = Mock(return_value=SimpleNamespace(exit_code=0))
    monkeypatch.setattr("booley.runtime.runtime_attachment.run_command", attached)
    assert sr.up(workspace) == "editor"
    assert sr.enter(workspace, ["hostname"], tty=False) == 0
    assert sr.enter(workspace, tty=True) == 0
    assert [call.args[1] for call in attached.call_args_list] == ["a" * 64] * 2
    assert attached.call_args.args[2] == ["/bin/bash", "-l"]
    assert attached.call_args.kwargs == {
        "tty": True,
        "env": {"BOOLEY_DIR": "/booley"},
        "cwd": None,
    }
    assert sr.validate(workspace) == fixture.issuance.spec_sha256
    assert all(call[1] in ("ps", "container", "inspect") for call in fixture.calls)


@pytest.mark.parametrize("action", ["down", "rebuild"])
@pytest.mark.parametrize("stale", [False, True])
def test_project_sandbox_resolution_editor_refuses_cli_mutation(
    workspace, project_sandbox_resolution, action, stale
):
    fixture = project_sandbox_resolution
    if stale:
        fixture.state["Config"]["Labels"]["booley.spec-digest"] = "old"
    with pytest.raises(sr.SessionError, match="VS Code owns"):
        sr.down(workspace) if action == "down" else sr.up(workspace, rebuild=True)
    assert all(call[1] in ("ps", "container") for call in fixture.calls)


@pytest.mark.parametrize("change", ["stale", "user", "workdir", "id", "stopped"])
@pytest.mark.parametrize("consumer", ["enter", "validate"])
def test_project_sandbox_resolution_refuses_drift_before_attachment(
    workspace, project_sandbox_resolution, monkeypatch, change, consumer
):
    fixture = project_sandbox_resolution
    if change == "stale":
        fixture.state["Config"]["Labels"]["booley.spec-digest"] = "old"
    elif change == "user":
        fixture.state["Config"]["User"] = "root"
    elif change == "workdir":
        fixture.state["Config"]["WorkingDir"] = "/"
    else:
        original = sr._docker_stdout

        def replaced(argv):
            result = original(argv)
            if "--format" not in argv:
                state = json.loads(result)[0]
                state["Id"] = "b" * 64 if change == "id" else state["Id"]
                state["State"]["Running"] = change != "stopped"
                return json.dumps([state])
            return result

        monkeypatch.setattr(sr, "_docker_stdout", replaced)
    attached = Mock()
    monkeypatch.setattr("booley.runtime.runtime_attachment.run_command", attached)
    with pytest.raises(sr.SessionError, match="current host issuance"):
        if consumer == "enter":
            sr.enter(workspace, tty=False)
        else:
            sr.validate(workspace)
    attached.assert_not_called()


@pytest.mark.parametrize("consumer", ["status", "up", "enter", "down", "prepare", "validate"])
@pytest.mark.parametrize(
    "problem",
    [
        "duplicate",
        "project-label",
        "root",
        "owner",
        "owner-missing",
        "canonical-owner-missing",
        "canonical-relay",
        "relay-owner",
        "running",
        "id",
        "mount",
    ],
)
def test_project_sandbox_resolution_fail_closed(
    workspace, project_sandbox_resolution, consumer, problem
):
    import copy

    fixture = project_sandbox_resolution
    state = fixture.state
    if problem == "duplicate":
        other = copy.deepcopy(state)
        other["Id"], other["Name"] = "b" * 64, "/editor-two"
        fixture.documents[other["Id"]] = other
    elif problem == "project-label":
        state["Config"]["Labels"].pop("booley.project-id")
    elif problem == "root":
        state["Config"]["Labels"]["devcontainer.local_folder"] = str(workspace.parent)
    elif problem == "owner":
        state["Config"]["Labels"]["booley.role"] = "other"
    elif problem == "owner-missing":
        state["Config"]["Labels"].pop("booley.role")
    elif problem == "canonical-owner-missing":
        state["Name"] = "/" + sr.session_container_name(workspace)
        state["Config"]["Labels"].clear()
    elif problem == "canonical-relay":
        state["Name"] = "/" + sr.session_container_name(workspace)
        state["Config"]["Labels"].pop("devcontainer.local_folder")
        state["Config"]["Labels"]["booley.role"] = "license-relay"
        state["Mounts"] = []
    elif problem == "relay-owner":
        state["Config"]["Labels"].pop("devcontainer.local_folder")
        state["Config"]["Labels"]["booley.role"] = "license-relay"
    elif problem == "running":
        state["State"].pop("Running")
    elif problem == "id":
        state.pop("Id")
    else:
        state["Mounts"][0]["Source"] = str(workspace.parent)
    with pytest.raises(sr.SessionError):
        getattr(sr, consumer)(workspace)
    assert all(call[1] in ("ps", "container") for call in fixture.calls)


def test_project_sandbox_resolution_prepare_allows_stale_editor_without_claim(
    workspace, project_sandbox_resolution, monkeypatch
):
    fixture = project_sandbox_resolution
    fixture.state["Config"]["Labels"]["booley.spec-digest"] = "old"
    monkeypatch.setattr(sr, "_refuse_then_validate", lambda *_: fixture.issuance)
    monkeypatch.setattr(sr, "_requested_issued_license", lambda *_: None)
    monkeypatch.setattr(sr, "_preflight", lambda *_args, **_kw: None)
    from booley.runtime import session_admission

    claim = Mock()
    monkeypatch.setattr(session_admission, "claim_vscode_start", claim)
    assert sr.prepare(workspace) == fixture.issuance.spec_sha256
    claim.assert_not_called()


def test_project_sandbox_resolution_headless_prepare_refuses_before_validation(
    workspace, project_sandbox_resolution, monkeypatch
):
    fixture = project_sandbox_resolution
    fixture.state["Name"] = "/" + sr.session_container_name(workspace)
    fixture.state["Config"]["Labels"].pop("devcontainer.local_folder")
    validate = Mock()
    monkeypatch.setattr(sr, "_refuse_then_validate", validate)
    with pytest.raises(sr.SessionError, match="headless Sandbox is running"):
        sr.prepare(workspace)
    validate.assert_not_called()


@pytest.mark.parametrize("problem", ["listing", "inspect", "malformed", "changed-id"])
def test_project_sandbox_resolution_inventory_errors(
    workspace, project_sandbox_resolution, monkeypatch, problem
):
    fixture = project_sandbox_resolution

    def run(argv, **kwargs):
        if (problem == "listing" and argv[1] == "ps") or (
            problem == "inspect" and argv[1] == "container"
        ):
            return subprocess.CompletedProcess(argv, 1, "", "Docker unavailable")
        if problem == "malformed" and argv[1] == "ps":
            return subprocess.CompletedProcess(argv, 0, "missing-fields", "")
        result = fixture.run(argv, **kwargs)
        if problem == "changed-id" and argv[1] == "container":
            document = json.loads(result.stdout)
            document["Id"] = "c" * 64
            return subprocess.CompletedProcess(argv, 0, json.dumps(document), "")
        return result

    monkeypatch.setattr(sr, "_run", run)
    with pytest.raises(sr.SessionError):
        sr.up(workspace)


def test_project_sandbox_resolution_excludes_proven_foreign_uninspectable(
    workspace, project_sandbox_resolution, monkeypatch
):
    from booley.core.project_identity import project_label_id

    fixture = project_sandbox_resolution
    foreign = workspace.parent / "foreign"

    def run(argv, **kwargs):
        result = fixture.run(argv, **kwargs)
        if argv[1] == "ps":
            result.stdout += "\n" + "\t".join(
                [
                    "c" * 64,
                    "foreign-editor",
                    str(foreign),
                    project_label_id(str(foreign)),
                    "interactive",
                ]
            )
        return result

    monkeypatch.setattr(sr, "_run", run)
    assert sr.resolve_running_sandbox(workspace).container_id == fixture.state["Id"]


def test_project_sandbox_resolution_status_does_not_hide_editor_behind_claim(
    workspace, project_sandbox_resolution, monkeypatch
):
    from booley.runtime import session_admission

    monkeypatch.setattr(session_admission, "has_pending_claim", lambda _: True)
    assert sr.status(workspace) == "running (owner: VS Code, name: editor)"


def test_project_sandbox_resolution_cli_editor_reuse_reports_due(
    workspace, project_sandbox_resolution, monkeypatch, capsys
):
    import argparse

    from booley.harness import booley as cli
    from booley.harness import doctor
    from booley.runtime import health

    monkeypatch.setattr(cli, "_report_upgrade_before_session", lambda _: None)
    monkeypatch.setattr(health, "due_reason", lambda _: "expired")
    monkeypatch.setattr(
        doctor,
        "observe_deep_status",
        lambda *_a, **_kw: SimpleNamespace(render=lambda: "deep status"),
    )
    assert cli._session_up(argparse.Namespace(rebuild=False), workspace) == 0
    captured = capsys.readouterr()
    assert "Reusing Sandbox owned by VS Code: editor" in captured.out
    assert "T3 Code relay" not in captured.out
    assert "Automatic Doctor is due" in captured.err
    assert "Automatic Doctor is running" not in captured.err


def test_project_sandbox_resolution_stale_headless_rebuild_keeps_transaction(
    workspace, project_sandbox_resolution, monkeypatch
):
    fixture = project_sandbox_resolution
    fixture.state["Config"]["Labels"].pop("devcontainer.local_folder")
    fixture.state["Config"]["Labels"]["booley.spec-digest"] = "stale"
    fixture.state["Name"] = "/" + sr.session_container_name(workspace)
    monkeypatch.setattr(sr, "_stopped_vscode_reconcile_candidates", lambda *_a, **_kw: [])
    transaction = Mock()
    monkeypatch.setattr(sr, "_run_up_transaction", transaction)
    monkeypatch.setattr(sr, "_warn_on_stale_session_containers", lambda *_: None)
    assert sr.up(workspace, rebuild=True) == sr.session_container_name(workspace)
    assert transaction.call_args.kwargs["rebuild"]


def test_project_sandbox_resolution_consumed_claim_status(
    workspace, project_sandbox_resolution, monkeypatch
):
    from booley.runtime import session_admission

    fixture = project_sandbox_resolution
    state = fixture.documents.pop(fixture.state["Id"])
    assert real_claim_vscode_start(workspace, run=fixture.run)
    claim = session_admission.claim_store().path(str(workspace))
    assert claim.exists()
    fixture.documents[state["Id"]] = state
    monkeypatch.setattr(
        session_admission,
        "has_pending_claim",
        lambda root: real_has_pending_claim(root, run=fixture.run),
    )
    assert sr.status(workspace) == "running (owner: VS Code, name: editor)"
    assert not claim.exists()


def test_project_sandbox_resolution_never_touches_claim_store(
    workspace, project_sandbox_resolution, monkeypatch
):
    from booley.runtime import session_admission

    store = Mock(side_effect=AssertionError("structural selection must be read-only"))
    monkeypatch.setattr(session_admission, "claim_store", store)
    assert sr.resolve_running_sandbox(workspace).name == "editor"
    store.assert_not_called()


def test_project_sandbox_resolution_current_headless_reuses_immutable_id(
    workspace, project_sandbox_resolution, monkeypatch
):
    fixture = project_sandbox_resolution
    fixture.state["Name"] = "/" + sr.session_container_name(workspace)
    fixture.state["Config"]["Labels"].pop("devcontainer.local_folder")
    fixture.state["Config"]["Env"] += [
        f"{key}={value}" for key, value in fixture.spec["remoteEnv"].items()
    ]
    attached = Mock(return_value=SimpleNamespace(exit_code=0))
    monkeypatch.setattr("booley.runtime.runtime_attachment.run_command", attached)
    assert sr.status(workspace) == "running"
    assert sr.up(workspace) == sr.session_container_name(workspace)
    assert sr.enter(workspace, ["hostname"], tty=False) == 0
    assert attached.call_args.args[1] == fixture.state["Id"]


@pytest.mark.parametrize("stopped", [False, True])
def test_project_sandbox_resolution_no_live_runtime(
    workspace, project_sandbox_resolution, monkeypatch, stopped
):
    fixture = project_sandbox_resolution
    fixture.state["State"]["Running"] = False
    monkeypatch.setattr(sr.idk, "container_exists", lambda _: stopped)
    monkeypatch.setattr(sr.idk, "container_running", lambda _: False)
    assert sr.resolve_running_sandbox(workspace) is None
    assert sr.status(workspace) == ("stopped" if stopped else "absent")


def test_project_sandbox_resolution_symlink_alias(workspace, project_sandbox_resolution):
    alias = workspace.parent / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    assert sr.resolve_running_sandbox(alias).container_id == project_sandbox_resolution.state["Id"]


@pytest.mark.parametrize("pending", [False, True])
def test_project_sandbox_resolution_status_ignores_uninspectable_foreign(
    workspace, project_sandbox_resolution, monkeypatch, pending
):
    import copy

    from booley.core.project_identity import project_label_id
    from booley.runtime import session_admission

    fixture = project_sandbox_resolution
    if pending:
        fixture.documents.clear()
        assert real_claim_vscode_start(workspace, run=fixture.run)
        fixture.documents[fixture.state["Id"]] = fixture.state
    foreign = copy.deepcopy(fixture.state)
    root = workspace.parent / "foreign"
    foreign["Id"], foreign["Name"] = "c" * 64, "/foreign-editor"
    foreign["Config"]["Labels"].update(
        {"devcontainer.local_folder": str(root), "booley.project-id": project_label_id(str(root))}
    )
    fixture.documents[foreign["Id"]] = foreign

    def run(argv, **kwargs):
        if argv[1:3] == ["container", "inspect"] and argv[3] == foreign["Id"]:
            return subprocess.CompletedProcess(argv, 1, "", "foreign inspection denied")
        return fixture.run(argv, **kwargs)

    monkeypatch.setattr(sr, "_run", run)
    monkeypatch.setattr(
        session_admission, "has_pending_claim", lambda root: real_has_pending_claim(root, run=run)
    )
    assert sr.status(workspace) == "running (owner: VS Code, name: editor)"


@pytest.mark.parametrize("kind", ["ordinary-service", "license-relay"])
def test_project_sandbox_resolution_ignores_noninteractive_resources(
    workspace, project_sandbox_resolution, kind
):
    import copy

    fixture = project_sandbox_resolution
    resource = copy.deepcopy(fixture.state)
    resource["Id"], resource["Name"] = "d" * 64, "/" + kind
    labels = resource["Config"]["Labels"]
    labels.pop("devcontainer.local_folder")
    if kind == "ordinary-service":
        labels.clear()
    else:
        labels["booley.role"] = "license-relay"
    resource["Mounts"] = []
    fixture.documents[resource["Id"]] = resource
    assert sr.resolve_running_sandbox(workspace).container_id == fixture.state["Id"]
    if kind == "ordinary-service":
        assert not any(resource["Id"] in call for call in fixture.calls if call[1] == "container")


@pytest.mark.parametrize(
    "state,problem",
    [
        (None, "immutable container identity"),
        ({"Id": "", "State": {"Running": True}}, "immutable container identity"),
        ({"Id": "immutable", "State": {"Running": False}}, "not running"),
        ({"Id": "immutable-container", "State": {"Running": True}}, None),
    ],
)
def test_refresh_identity_requires_running_immutable_container(
    tmp_path, monkeypatch, state, problem
):
    monkeypatch.setattr(sr, "_strict_refresh_container", lambda _name: state)
    if problem is None:
        assert sr.refresh_container_id(tmp_path) == "immutable-container"
    else:
        with pytest.raises(sr.SessionError, match=problem):
            sr.refresh_container_id(tmp_path)


@pytest.mark.parametrize("problem", ["changed-id", "malformed", "denied"])
def test_project_sandbox_resolution_revalidation_fails_closed(
    workspace, project_sandbox_resolution, monkeypatch, problem
):
    fixture = project_sandbox_resolution
    inspections = 0

    def run(argv, **kwargs):
        nonlocal inspections
        result = fixture.run(argv, **kwargs)
        if argv[1:3] == ["container", "inspect"]:
            inspections += 1
            if inspections == 2:
                if problem == "denied":
                    return subprocess.CompletedProcess(argv, 1, "", "inspection denied")
                state = json.loads(result.stdout)
                if problem == "changed-id":
                    state["Id"] = "b" * 64
                else:
                    state.pop("Mounts")
                result.stdout = json.dumps(state)
        return result

    monkeypatch.setattr(sr, "_run", run)
    with pytest.raises(sr.SessionError, match=r"identity changed|cannot revalidate"):
        sr.down(workspace)
    assert inspections == 2
    assert all(call[1] in ("ps", "container") for call in fixture.calls)


@pytest.mark.parametrize("owner", ["editor", "headless"])
@pytest.mark.parametrize("command", [None, ["hostname"]])
def test_enter_preserves_container_credentials_without_literal_placeholder_overrides(
    workspace, tmp_path, request, project_sandbox_resolution, monkeypatch, owner, command
):
    from booley.runtime import auth_token, runtime_attachment

    select_booley_dir(request, monkeypatch, tmp_path)

    fixture = project_sandbox_resolution
    for key in auth_token.CREDENTIAL_ENV_KEYS:
        fixture.spec["remoteEnv"][key] = "${localEnv:" + key + "}"
    if owner == "headless":
        fixture.state["Config"]["Labels"].pop("devcontainer.local_folder")
        fixture.state["Name"] = "/" + sr.session_container_name(workspace)
        fixture.state["Config"]["Env"] += [
            f"{key}={sr.substitute(value, workspace)}"
            for key, value in fixture.spec["remoteEnv"].items()
        ]
    captured = []

    def run(_driver, _execution_id, argv):
        captured.extend(argv)
        return runtime_attachment.ExecutionResult(0, "terminal", True, "complete")

    monkeypatch.setattr(runtime_attachment.SupervisedExecutionDriver, "run", run)
    assert sr.enter(workspace, command, tty=False) == 0
    overrides = [captured[index + 1] for index, value in enumerate(captured) if value == "-e"]
    assert not any(item.split("=", 1)[0] in auth_token.CREDENTIAL_ENV_KEYS for item in overrides)
    assert "BOOLEY_DIR=/booley" in overrides
    assert fixture.state["Id"] in captured
    assert captured[-len(command or ["/bin/bash", "-l"]) :] == (command or ["/bin/bash", "-l"])


def _replace_file(path, text):
    """Rewrite a file Git may have marked hidden; Windows refuses to truncate hidden files."""
    path.unlink()
    path.write_text(text)


def _linked_project(tmp_path, monkeypatch, *, external=False, sandbox_pointer=False):
    from booley.harness import booley as cli
    from booley.runtime.booley_dir import reset_cache

    root = tmp_path / "owner"
    root.mkdir()
    data = root / ".booley"
    data.mkdir()
    for argv in (
        ["git", "init", str(root)],
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "initial",
        ],
    ):
        run_bounded_child(argv, check=True)
    linked = tmp_path / "linked" if external else data / "worktrees" / "goal"
    run_bounded_child(
        ["git", "-C", str(root), "worktree", "add", "--detach", str(linked)], check=True
    )
    if sandbox_pointer:
        # A Sandbox-created worktree records both Git pointers as container paths.
        admin = Path((linked / ".git").read_text().removeprefix("gitdir:").strip())
        _replace_file(linked / ".git", "gitdir: /work/.git/worktrees/goal\n")
        _replace_file(admin / "gitdir", "/work/.booley/worktrees/goal/.git\n")
    for key in ("BOOLEY_DIR", "RTL_PROJECT_ROOT", "BOOLEY_CONTAINER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda *_: None)
    monkeypatch.setattr(
        "booley.runtime.layout_consumers.directory_consumer", lambda *_: nullcontext()
    )
    monkeypatch.setattr(sr, "_recover_before_lifecycle", lambda *_: None)
    monkeypatch.setattr(
        "booley.runtime.lifecycle_lock.host_lifecycle_lock", lambda *_: nullcontext()
    )
    reset_cache()
    monkeypatch.chdir(linked)
    return root, linked


def _linked_cli(monkeypatch, argv):
    import sys

    from booley.harness import booley as cli

    monkeypatch.setattr(sys, "argv", ["booley", *argv])
    return cli.main()


def test_ctrl_c_while_session_enter_prepares_the_sandbox_prints_one_line(
    tmp_path, monkeypatch, capsys
):
    _linked_project(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)

    def interrupted(_root):
        raise KeyboardInterrupt

    # This is where the host Sandbox preparation (the Docker image checks) runs; no Docker fake
    # can deliver a Ctrl-C into it (the same seam `_linked_attachment` replaces).
    monkeypatch.setattr(sr, "_select_or_start_project_runtime", interrupted)

    with pytest.raises(KeyboardInterrupt) as raised:
        _linked_cli(monkeypatch, ["session", "enter", "--", "true"])

    assert type(raised.value) is KeyboardInterrupt
    assert capsys.readouterr().err.splitlines()[-1:] == ["interrupted; session enter cancelled"]
    sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
    assert capsys.readouterr().err == ""


def _linked_attachment(monkeypatch):
    calls = []
    monkeypatch.setattr(sr, "_select_or_start_project_runtime", lambda root: ("immutable-id", {}))

    def attach(root, name, command, **kwargs):
        calls.append((root, name, command, kwargs))
        return SimpleNamespace(exit_code=7)

    monkeypatch.setattr("booley.runtime.runtime_attachment.run_command", attach)
    return calls


@pytest.mark.parametrize("mode", ["command", "shell"])
def test_session_enter_from_booley_worktree_uses_owner_and_worktree_cwd(
    tmp_path, monkeypatch, mode
):
    root, linked = _linked_project(tmp_path, monkeypatch, sandbox_pointer=True)
    (linked / ".booley").mkdir()
    child = linked / "rtl"
    child.mkdir()
    monkeypatch.chdir(child)
    calls = _linked_attachment(monkeypatch)
    command = ["printf", "%s", "a b;$HOME"] if mode == "command" else ["/bin/bash", "-l"]
    result = _linked_cli(
        monkeypatch, ["session", "enter", *(["--", *command] if mode == "command" else [])]
    )
    observed = [(r, n, c, kw.get("cwd")) for r, n, c, kw in calls]
    assert observed == [(root, "immutable-id", command, "/work/.booley/worktrees/goal/rtl")], (
        "session enter must use the owning Project and mapped worktree cwd"
    )
    assert result == 7
    assert calls[0][3]["tty"] is False


@pytest.mark.parametrize(
    "action", ["default", "up", "up-rebuild", "down", "status", "validate", "refresh"]
)
def test_session_lifecycle_from_user_worktree_uses_owner(tmp_path, monkeypatch, action):
    from booley.harness import booley as cli

    root, _linked = _linked_project(tmp_path, monkeypatch, external=True)
    observations = []
    leaf = "up" if action in ("default", "up-rebuild") else action
    monkeypatch.setattr(
        cli, "_session_" + leaf, lambda args, selected: observations.append(selected) or 0
    )
    argv = ["session"] + (
        [] if action == "default" else ["up", "--rebuild"] if action == "up-rebuild" else [action]
    )
    _linked_cli(monkeypatch, argv)
    assert observations == [root], "session lifecycle must use the owning Project"


def test_session_enter_selected_worktree_ignores_unrelated_cwd(tmp_path, monkeypatch):
    root, linked = _linked_project(tmp_path, monkeypatch)
    (linked / ".booley").mkdir()
    monkeypatch.chdir(tmp_path)
    calls = _linked_attachment(monkeypatch)
    _linked_cli(monkeypatch, ["-C", str(linked), "session", "enter", "--", "pwd"])
    assert [(r, kw.get("cwd")) for r, _, _, kw in calls] == [
        (root, "/work/.booley/worktrees/goal")
    ], "explicit worktree selection must control session cwd"


def _linked_transport(tmp_path, monkeypatch):
    from booley.runtime import runtime_attachment

    root, linked = _linked_project(tmp_path, monkeypatch)
    (linked / ".booley").mkdir()
    monkeypatch.setattr(sr, "_select_or_start_project_runtime", lambda root: ("immutable-id", {}))
    monkeypatch.setattr(
        sr, "_run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "")
    )
    captured = []

    def run(driver, execution_id, argv):
        captured.append((driver._project_data, argv))
        return runtime_attachment.ExecutionResult(7, "terminal", True, "complete")

    monkeypatch.setattr(runtime_attachment.SupervisedExecutionDriver, "run", run)
    _linked_cli(monkeypatch, ["session", "enter", "--", "printf", "a b;$HOME"])
    return root, captured


def test_enter_passes_worktree_cwd_to_docker_exec(tmp_path, monkeypatch):
    root, captured = _linked_transport(tmp_path, monkeypatch)
    argv = captured[0][1] if captured else []
    assert any(
        argv[i : i + 2] == ["--workdir", "/work/.booley/worktrees/goal"] for i in range(len(argv))
    ), "linked worktree cwd must reach docker exec"
    assert captured[0][0] == root / ".booley"
    assert argv[-2:] == ["printf", "a b;$HOME"]
    assert argv.index("--workdir") < argv.index("immutable-id")


def test_worktree_enter_supervisor_ignores_cwd_imports(tmp_path, monkeypatch):
    _, captured = _linked_transport(tmp_path, monkeypatch)
    argv = captured[0][1] if captured else []
    assert any(argv[i : i + 3] == ["python3", "-P", "-m"] for i in range(len(argv))), (
        "supervisor bootstrap must ignore worktree import shadowing"
    )


@pytest.mark.parametrize("mode", ["command", "shell"])
def test_external_worktree_enter_refuses_before_runtime(tmp_path, monkeypatch, capsys, mode):
    _linked_project(tmp_path, monkeypatch, external=True)
    calls = _linked_attachment(monkeypatch)
    selected = []
    monkeypatch.setattr(
        sr, "_select_or_start_project_runtime", lambda root: selected.append(root) or ("id", {})
    )
    result = _linked_cli(
        monkeypatch, ["session", "enter", *(["--", "pwd"] if mode == "command" else [])]
    )
    text = capsys.readouterr().err
    assert (
        result == 2
        and not calls
        and not selected
        and "run from the Project Root" in text
        and "mount" in text
    ), "unmounted linked enter must refuse before runtime selection"


@pytest.mark.parametrize("selection", ["implicit", "explicit"])
def test_linked_worktree_prepare_refuses_before_validation(
    tmp_path, monkeypatch, capsys, selection
):
    _, linked = _linked_project(tmp_path, monkeypatch)
    (linked / ".booley").mkdir()
    effects = []
    monkeypatch.setattr(sr, "prepare", lambda root: effects.append(root) or "prepared")
    result = _linked_cli(
        monkeypatch,
        ["session", "prepare", *(["--project", str(linked)] if selection == "explicit" else [])],
    )
    text = capsys.readouterr().err
    assert result == 2 and not effects and "run from the Project Root" in text, (
        "linked prepare must refuse before exact-folder admission"
    )


def test_main_project_subdirectory_retains_work_start(tmp_path, monkeypatch):
    root, _ = _linked_project(tmp_path, monkeypatch)
    child = root / "rtl"
    child.mkdir()
    monkeypatch.chdir(child)
    calls = _linked_attachment(monkeypatch)
    assert _linked_cli(monkeypatch, ["session", "enter", "--", "pwd"]) == 7
    assert calls[0][0] == root
    assert calls[0][3].get("cwd") is None


def test_project_root_prepare_validates_exact_folder(tmp_path, monkeypatch):
    root, _ = _linked_project(tmp_path, monkeypatch)
    monkeypatch.chdir(root)
    calls = []
    monkeypatch.setattr(sr, "prepare", lambda selected: calls.append(selected) or "prepared")
    assert _linked_cli(monkeypatch, ["session", "prepare", "--project", str(root)]) == 0
    assert calls == [root]


@pytest.mark.parametrize("problem", ["missing", "inaccessible"])
def test_enter_rejects_unavailable_mapped_cwd(tmp_path, monkeypatch, problem):
    root, linked = _linked_project(tmp_path, monkeypatch)
    (linked / ".booley").mkdir()
    selected = []
    monkeypatch.setattr(sr, "_select_or_start_project_runtime", lambda root: ("immutable-id", {}))
    monkeypatch.setattr(
        sr, "_run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", problem)
    )
    monkeypatch.setattr(
        "booley.runtime.runtime_attachment.SupervisedExecutionDriver.run",
        lambda *_: selected.append(True),
    )
    with pytest.raises(sr.SessionError, match="directory"):
        sr.enter(root, ["pwd"], tty=False, cwd="/work/.booley/worktrees/goal")
    assert not selected


def test_mapped_cwd_probe_is_bounded_and_isolated(monkeypatch):
    calls = []

    def run(argv, *, timeout):
        calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sr, "_run", run)
    sr._probe_command_cwd("immutable-id", "/work/.booley/worktrees/goal")
    argv, timeout = calls[0]
    assert timeout == 10
    assert argv[:8] == [
        "docker",
        "exec",
        "--user",
        "agent",
        "--workdir",
        "/",
        "immutable-id",
        "python3",
    ]
    assert argv[8:10] == ["-I", "-c"]
    assert argv[-1] == "/work/.booley/worktrees/goal"


@pytest.mark.parametrize("route", ["global-long", "leaf-short", "leaf-long", "environment"])
def test_session_owner_selection_routes(tmp_path, monkeypatch, route):
    from booley.harness import booley as cli

    root, linked = _linked_project(tmp_path, monkeypatch, external=True)
    monkeypatch.chdir(tmp_path)
    selected = []
    monkeypatch.setattr(cli, "_session_status", lambda args, owner: selected.append(owner) or 0)
    monkeypatch.setattr(cli, "_session_down", lambda args, owner: selected.append(owner) or 0)
    argv = ["session", "status"]
    if route == "environment":
        monkeypatch.setenv("RTL_PROJECT_ROOT", str(linked))
    elif route == "global-long":
        argv = ["--project", str(linked), *argv]
    else:
        argv = [
            "session",
            "down",
            "-p" if route == "leaf-short" else "--project-root",
            str(linked),
        ]
    assert _linked_cli(monkeypatch, argv) == 0
    assert selected == [root]


def _write_spec_file(workspace: Path) -> Path:
    path = dc.devcontainer_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"image": "x"}', encoding="utf-8")
    return path


def test_spec_read_survives_transient_sharing_error(tmp_path, monkeypatch):
    from tests.sharing_error_support import hold_opens

    path = _write_spec_file(tmp_path)
    attempts = hold_opens(monkeypatch, {path: 1})
    assert sr._load_spec(tmp_path) == {"image": "x"}
    assert attempts[path] == 2


def test_lasting_spec_sharing_error_is_a_session_error(tmp_path, monkeypatch):
    from tests.sharing_error_support import hold_opens

    path = _write_spec_file(tmp_path)
    attempts = hold_opens(monkeypatch, {path: 99})
    with pytest.raises(sr.SessionError, match="could not read") as caught:
        sr._load_spec(tmp_path)
    assert caught.value.__cause__.winerror == 32
    assert attempts[path] > 1


def _two_cli_spec(*, app: str = "claude", record: str, **remote_env: str) -> dict:
    """A Claude-default spec recording *record* as its Agent CLI set."""
    spec = _spec()
    spec["remoteEnv"]["BOOLEY_AGENT_APP"] = app
    spec["remoteEnv"]["BOOLEY_AGENT_CLIS"] = record
    spec["remoteEnv"].update(remote_env)
    return spec


def test_docker_run_argv_forwards_every_listed_cli_credential(workspace, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_OPENAI")
    placeholder = {"OPENAI_API_KEY": "${localEnv:OPENAI_API_KEY}"}

    command = sr.docker_run_argv(
        _two_cli_spec(record="claude=subscription,codex=api_key", **placeholder), workspace, "test"
    )

    assert command.environment["OPENAI_API_KEY"] == "SYNTHETIC_OPENAI"
    assert "OPENAI_API_KEY" in _flag_values(command, "-e")
    with pytest.raises(sr.SessionError, match="credential environment"):
        sr.docker_run_argv(
            _two_cli_spec(record="claude=subscription", **placeholder), workspace, "test"
        )


def test_docker_run_argv_requires_api_key_only_for_default_cli(workspace, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_ANTHROPIC")
    placeholders = {
        "OPENAI_API_KEY": "${localEnv:OPENAI_API_KEY}",
        "ANTHROPIC_API_KEY": "${localEnv:ANTHROPIC_API_KEY}",
    }
    # A missing key for the non-default Codex must not block the whole Sandbox.
    sr.docker_run_argv(
        _two_cli_spec(
            record="claude=api_key,codex=api_key", **placeholders, BOOLEY_PRIMARY_AUTH="api_key"
        ),
        workspace,
        "test",
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(sr.SessionError, match=r"requires.*credential"):
        sr.docker_run_argv(
            _two_cli_spec(
                record="claude=api_key,codex=api_key",
                **placeholders,
                BOOLEY_PRIMARY_AUTH="api_key",
            ),
            workspace,
            "test",
        )


def test_docker_run_argv_refuses_malformed_cli_record(workspace):
    with pytest.raises(sr.SessionError, match="Agent CLI record"):
        sr.docker_run_argv(
            _two_cli_spec(record="claude=subscription,bogus=subscription"), workspace, "test"
        )


def test_docker_run_argv_refuses_a_retired_auto_record(workspace):
    with pytest.raises(sr.SessionError, match=r"retired auth 'auto'.*refresh the Sandbox"):
        sr.docker_run_argv(_two_cli_spec(record="claude=auto"), workspace, "test")


_GATEWAY_REFS = {
    "BOOLEY_GATEWAY_URL": "${localEnv:BOOLEY_GATEWAY_URL}",
    "BOOLEY_GATEWAY_TOKEN": "${localEnv:BOOLEY_GATEWAY_TOKEN}",
}
_GATEWAY_RECORD = "claude=subscription,codex=gateway"


def _gateway_spec(**remote_env: str) -> dict:
    """A Claude-default issued-shape spec with Codex on the gateway (one token ref)."""
    return _two_cli_spec(record=_GATEWAY_RECORD, **{**_GATEWAY_REFS, **remote_env})


def _export_gateway(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    """Export a valid gateway configuration; ``None`` in *overrides* unsets a variable."""
    values: dict[str, str | None] = {
        "BOOLEY_GATEWAY_URL": "https://gateway.example.test/v1",
        "BOOLEY_GATEWAY_TOKEN": "SYNTHETIC_GATEWAY_TOKEN",
        "BOOLEY_GATEWAY_TOKEN_COMMAND": None,
        **overrides,
    }
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_docker_run_argv_refuses_gateway_spec_without_host_gateway_settings(
    workspace, monkeypatch
):
    _export_gateway(monkeypatch, BOOLEY_GATEWAY_URL=None)
    with pytest.raises(sr.SessionError, match="BOOLEY_GATEWAY_URL"):
        sr.docker_run_argv(_gateway_spec(), workspace, "test")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        # Another token source exported than the one the spec issued.
        (
            {"BOOLEY_GATEWAY_TOKEN": None, "BOOLEY_GATEWAY_TOKEN_COMMAND": "print-token"},
            "BOOLEY_GATEWAY_TOKEN is not set",
        ),
        # Both token sources exported: the host's gateway validation refuses.
        ({"BOOLEY_GATEWAY_TOKEN_COMMAND": "print-token"}, "exactly one"),
        # An invalid URL is refused before a Sandbox starts without a working gateway.
        ({"BOOLEY_GATEWAY_URL": "http://gateway.example.test"}, "BOOLEY_GATEWAY_URL"),
    ],
)
def test_docker_run_argv_refuses_unusable_gateway_settings(
    workspace, monkeypatch, overrides, message
):
    _export_gateway(monkeypatch, **overrides)
    with pytest.raises(sr.SessionError, match=message):
        sr.docker_run_argv(_gateway_spec(), workspace, "test")


def test_docker_run_argv_forwards_gateway_token_without_secret_argv(workspace, monkeypatch):
    _export_gateway(monkeypatch)

    command = sr.docker_run_argv(_gateway_spec(), workspace, "test")

    assert command.environment["BOOLEY_GATEWAY_TOKEN"] == "SYNTHETIC_GATEWAY_TOKEN"
    assert command.environment["BOOLEY_GATEWAY_URL"] == "https://gateway.example.test/v1"
    flags = _flag_values(command, "-e")
    assert "BOOLEY_GATEWAY_TOKEN" in flags
    assert "BOOLEY_GATEWAY_URL" in flags
    assert "SYNTHETIC_GATEWAY_TOKEN" not in " ".join(command)
    assert not any("localEnv" in flag for flag in flags)


def test_docker_run_argv_refuses_literal_gateway_value_in_gateway_spec(workspace, monkeypatch):
    _export_gateway(monkeypatch)
    spec = _gateway_spec(BOOLEY_GATEWAY_TOKEN="SYNTHETIC_LITERAL")
    with pytest.raises(sr.SessionError, match="credential environment"):
        sr.docker_run_argv(spec, workspace, "test")


def test_docker_run_argv_keeps_literal_gateway_values_without_gateway_record(
    workspace, monkeypatch
):
    """A hand-written spec (no record) keeps its literal environment and no refusal."""
    for key in ("BOOLEY_GATEWAY_URL", "BOOLEY_GATEWAY_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    spec = _spec()
    spec["remoteEnv"].update(
        BOOLEY_GATEWAY_URL="https://gateway.example.test/v1", BOOLEY_GATEWAY_TOKEN="LITERAL"
    )
    command = sr.docker_run_argv(spec, workspace, "test")
    assert "BOOLEY_GATEWAY_TOKEN=LITERAL" in _flag_values(command, "-e")


def test_prepare_refuses_gateway_spec_without_vscode_gateway_settings(
    workspace, project_sandbox_resolution, monkeypatch
):
    """VS Code's ``initializeCommand`` refuses without the exports and leaves no claim."""
    fixture = project_sandbox_resolution
    fixture.state["Config"]["Labels"]["booley.spec-digest"] = "old"
    fixture.spec["remoteEnv"]["BOOLEY_AGENT_CLIS"] = _GATEWAY_RECORD
    fixture.spec["remoteEnv"].update(_GATEWAY_REFS)
    _export_gateway(monkeypatch, BOOLEY_GATEWAY_TOKEN=None)
    monkeypatch.setattr(sr, "_refuse_then_validate", lambda *_: fixture.issuance)
    from booley.runtime import session_admission

    claim = Mock()
    monkeypatch.setattr(session_admission, "claim_vscode_start", claim)
    with pytest.raises(sr.SessionError, match="BOOLEY_GATEWAY_TOKEN is not set"):
        sr.prepare(workspace)
    claim.assert_not_called()


def test_enter_running_headless_gateway_sandbox_without_exports_names_variable(
    workspace, project_sandbox_resolution, monkeypatch
):
    fixture = project_sandbox_resolution
    fixture.spec["remoteEnv"]["BOOLEY_AGENT_CLIS"] = _GATEWAY_RECORD
    fixture.spec["remoteEnv"].update(_GATEWAY_REFS)
    fixture.state["Config"]["Labels"].pop("devcontainer.local_folder")
    fixture.state["Name"] = "/" + sr.session_container_name(workspace)
    _export_gateway(monkeypatch, BOOLEY_GATEWAY_URL=None)

    with pytest.raises(sr.SessionError, match="BOOLEY_GATEWAY_URL") as caught:
        sr.enter(workspace, ["hostname"], tty=False)
    assert "does not match" not in str(caught.value)


def test_enter_gateway_sandbox_never_overrides_container_gateway_values(
    workspace, project_sandbox_resolution, monkeypatch
):
    """``command_env`` drops the forwarded gateway keys: no placeholder ``-e`` override."""
    from booley.config.gateway import GATEWAY_FORWARDED_ENV  # absent before #2897

    fixture = project_sandbox_resolution
    fixture.spec["remoteEnv"]["BOOLEY_AGENT_CLIS"] = _GATEWAY_RECORD
    fixture.spec["remoteEnv"].update(_GATEWAY_REFS)
    fixture.state["Config"]["Labels"].pop("devcontainer.local_folder")
    fixture.state["Name"] = "/" + sr.session_container_name(workspace)
    _export_gateway(monkeypatch)
    monkeypatch.setattr(sr, "container_matches_issuance", lambda *_a, **_k: True)

    container_id, command_env = sr._select_or_start_project_runtime(workspace)

    assert container_id == fixture.state["Id"]
    assert command_env["BOOLEY_DIR"] == "/booley"
    assert not set(command_env) & set(GATEWAY_FORWARDED_ENV)


# ---------------------------------------------------------------------------
# Refresh roll-forward primitives (issue #3644)
# ---------------------------------------------------------------------------

_REFRESH_IMAGE_ID = "sha256:" + "f" * 64
_CREDENTIAL_KEY = "CLAUDE_CODE_OAUTH_TOKEN"


def _refresh_image() -> RefreshImage:
    """The Sandbox Image facts a refresh verifies, matching the resolution fixture."""
    return RefreshImage(_REFRESH_IMAGE_ID, None, None, False)


def _as_headless_sandbox(workspace: Path, fixture: SimpleNamespace, monkeypatch) -> None:
    """Turn the resolution fixture's VS Code container into the canonical headless Sandbox.

    The container's environment holds its own credential value for a spec key that
    the spec fills from the caller's shell (``${localEnv:KEY}``).
    """
    labels = fixture.state["Config"]["Labels"]
    labels.pop("devcontainer.local_folder")
    labels.pop("devcontainer.config_file")
    fixture.state["Name"] = "/" + sr.session_container_name(workspace)
    fixture.spec["remoteEnv"][_CREDENTIAL_KEY] = "${localEnv:" + _CREDENTIAL_KEY + "}"
    fixture.state["Config"]["Env"] = [
        "CONTAINER=yes",
        "BOOLEY_DIR=/booley",
        f"{_CREDENTIAL_KEY}=container-held-secret",
    ]
    # The shared resolution fixture serves its inspections through `_docker_stdout`, the
    # module's Docker probe seam that no public entry takes; wrap it for the image-ID format.
    inspect = sr._docker_stdout

    def stdout(argv: list[str]) -> str | None:
        if argv[-1] == "{{.Image}}":
            return fixture.state["Image"]
        return inspect(argv)

    monkeypatch.setattr(sr, "_docker_stdout", stdout)


def test_verified_headless_container_ignores_calling_shell_env(
    workspace, project_sandbox_resolution, monkeypatch
):
    fixture = project_sandbox_resolution
    _as_headless_sandbox(workspace, fixture, monkeypatch)
    container_id = fixture.state["Id"]

    # A key the calling shell does not export is skipped: the container holds its own.
    monkeypatch.delenv(_CREDENTIAL_KEY, raising=False)
    assert sr.verified_headless_container(workspace, _refresh_image()) == container_id
    # An exported value must match, as `session up` requires; a changed one is not current.
    monkeypatch.setenv(_CREDENTIAL_KEY, "container-held-secret")
    assert sr.verified_headless_container(workspace, _refresh_image()) == container_id
    monkeypatch.setenv(_CREDENTIAL_KEY, "a-different-shell-value")
    assert sr.verified_headless_container(workspace, _refresh_image()) is None
    monkeypatch.delenv(_CREDENTIAL_KEY)

    # A drifted non-`localEnv` key or another image is not current.
    fixture.state["Config"]["Env"][1] = "BOOLEY_DIR=/elsewhere"
    assert sr.verified_headless_container(workspace, _refresh_image()) is None
    fixture.state["Config"]["Env"][1] = "BOOLEY_DIR=/booley"
    assert sr.verified_headless_container(workspace, _refresh_image()) == container_id
    other_image = RefreshImage("sha256:" + "e" * 64, None, None, False)
    assert sr.verified_headless_container(workspace, other_image) is None


def test_verified_headless_container_requires_running_relay(
    workspace, project_sandbox_resolution, monkeypatch
):
    from booley.eda.provisioning.licensing import flexnet_docker

    fixture = project_sandbox_resolution
    _as_headless_sandbox(workspace, fixture, monkeypatch)
    fixture.issuance.license_profile = "site"
    # The running Sandbox carries the licensed issuance's labels.
    fixture.state["Config"]["Labels"].update(
        label.split("=", 1) for label in runtime_spec.labels(fixture.issuance)
    )
    monkeypatch.setattr(runtime_spec, "requested_license", lambda *_a, **_k: _LICENSE_PROFILE)
    validate = Mock()
    monkeypatch.setattr(flexnet_docker, "validate_relay", validate)

    assert sr.verified_headless_container(workspace, _refresh_image()) == fixture.state["Id"]
    assert validate.call_args.args[:2] == (_relay(workspace), sr.session_container_name(workspace))
    assert validate.call_args.kwargs["image"] == fixture.issuance.relay_image_id

    # A stopped (or otherwise drifted) relay makes the matching Sandbox not current.
    validate.side_effect = flexnet_docker.RelayDockerError("license relay is not running")
    assert sr.verified_headless_container(workspace, _refresh_image()) is None


def test_remove_refresh_relay_removes_existing_relay_objects(workspace, host_docker):
    relay = _relay(workspace)
    sr.remove_refresh_relay(workspace)
    assert host_docker.removed == []

    host_docker.networks.add(relay.outbound_network)
    sr.remove_refresh_relay(workspace)
    assert host_docker.removed == [relay.relay_container]


def test_require_only_canonical_sandbox(workspace, monkeypatch):
    canonical = sr.session_container_name(workspace)
    cases = {
        "none": (None, False),
        "canonical": (SimpleNamespace(name=canonical, vscode=False), False),
        "pre-refresh": (SimpleNamespace(name=f"{canonical}-pre-refresh", vscode=False), True),
        "vscode": (SimpleNamespace(name="editor", vscode=True), True),
        "vscode-with-canonical-name": (SimpleNamespace(name=canonical, vscode=True), True),
    }
    for label, (selected, refuses) in cases.items():
        monkeypatch.setattr(sr, "resolve_running_sandbox", lambda _workspace, s=selected: s)
        if refuses:
            with pytest.raises(sr.SessionError, match="not this Project's canonical"):
                sr.require_only_canonical_sandbox(workspace)
        else:
            assert sr.require_only_canonical_sandbox(workspace) is None, label


def _prepare_refresh_start(wired, monkeypatch, *, licensed: bool):
    """A reissued spec, fake Docker and Sandbox identity probes for ``start_refreshed_sandbox``."""
    from booley.eda.provisioning.licensing import flexnet_docker

    workspace, run = wired
    name = sr.session_container_name(workspace)
    issuance = _test_issuance(workspace)
    issuance.image_id = _REFRESH_IMAGE_ID
    issuance.license_profile = "site" if licensed else None
    started = json.dumps({**_headless_state(workspace), "Id": "new-id"})
    events: list[str] = []

    def execute(argv, **_kwargs):
        if argv[:2] == ["docker", "run"]:
            events.append("docker run")
        if argv[1:3] == ["container", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, started, "")
        stdout = _REFRESH_IMAGE_ID if argv[-1] == "{{.Image}}" else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    run.side_effect = execute
    request = sr.UpRequest(
        _spec(),
        issuance,
        _LICENSE_PROFILE if licensed else None,
        name,
        runtime_spec.labels(issuance),
        _relay(workspace),
    )
    monkeypatch.setattr(sr.idk, "container_exists", lambda _name: False)
    monkeypatch.setattr(sr.idk, "container_running", lambda _name: False)
    relay = SimpleNamespace(relay_container="relay")
    recreate = Mock(side_effect=lambda *_a, **_k: events.append("recreate relay") or relay)
    validate = Mock(side_effect=lambda *_a, **_k: events.append("validate relay"))
    monkeypatch.setattr(flexnet_docker, "recreate_relay", recreate)
    monkeypatch.setattr(flexnet_docker, "validate_relay", validate)
    return SimpleNamespace(
        workspace=workspace,
        run=run,
        request=request,
        issuance=issuance,
        events=events,
        recreate=recreate,
        validate=validate,
        relay=relay,
    )


def test_start_refreshed_sandbox_provisions_relay_for_new_issuance(wired, monkeypatch):
    fixture = _prepare_refresh_start(wired, monkeypatch, licensed=True)

    container_id = sr.start_refreshed_sandbox(fixture.workspace, fixture.request, _refresh_image())

    assert container_id == "new-id"
    fixture.recreate.assert_called_once()
    assert fixture.recreate.call_args.kwargs["issuance_labels"] == fixture.request.labels
    assert fixture.recreate.call_args.kwargs["image"] == fixture.issuance.relay_image_id
    # Relay first, then the Sandbox, then the exact topology check.
    assert fixture.events == ["recreate relay", "docker run", "validate relay"]
    assert fixture.validate.call_args.args[0] is fixture.relay
    assert fixture.validate.call_args.args[1] == fixture.request.name
    assert fixture.validate.call_args.kwargs["image"] == fixture.issuance.relay_image_id


def test_start_refreshed_sandbox_skips_stopped_vscode_cleanup(wired, monkeypatch):
    fixture = _prepare_refresh_start(wired, monkeypatch, licensed=False)
    stale = "vscode-stale"
    # `wired` empties the strict Sandbox inventory that `session up` reads for this
    # cleanup; no public entry restores it, so serve the stale container through it.
    state = _vscode_container(fixture.workspace, running=False, spec_digest="older-spec")
    monkeypatch.setattr(
        sr, "_strict_all_sandbox_states", lambda _project: [(stale, json.dumps([state]))]
    )

    assert (
        sr.start_refreshed_sandbox(fixture.workspace, fixture.request, _refresh_image())
        == "new-id"
    )

    assert fixture.events == ["docker run"]
    assert not any(stale in _argv_of(call) for call in fixture.run.call_args_list)


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        ({"booley.project-id": "someone-else"}, "not this Project's headless Sandbox"),
        ({"booley.role": None}, "not this Project's headless Sandbox"),
        ({"devcontainer.local_folder": "/work"}, "not this Project's headless Sandbox"),
        (None, "no immutable container identity"),
    ],
    ids=["other-project", "missing-role", "vscode-folder", "no-identity"],
)
def test_remove_refresh_predecessor_refuses_unproven_container(
    workspace, host_docker, labels, message
):
    state = _headless_state(workspace, **(labels or {}))
    if labels is None:
        del state["Id"]
    host_docker.containers[sr.session_container_name(workspace)] = state

    with pytest.raises(sr.SessionError, match=message):
        sr.remove_refresh_predecessor(workspace)

    assert [argv[1] for argv in host_docker.calls] == ["container"]


def test_remove_refresh_predecessor_stops_then_removes_by_immutable_id(workspace, host_docker):
    assert sr.remove_refresh_predecessor(workspace) is None
    assert [argv[1] for argv in host_docker.calls] == ["container"]
    host_docker.containers[sr.session_container_name(workspace)] = _headless_state(workspace)
    host_docker.calls.clear()

    assert sr.remove_refresh_predecessor(workspace) == "c" * 64
    assert host_docker.calls[1:] == [
        ["docker", "stop", "c" * 64],
        ["docker", "rm", "-f", "c" * 64],
    ]


# ---------------------------------------------------------------------------
# _run timeout contract (real processes, no Docker needed)
# ---------------------------------------------------------------------------
# `_run` is the module's only process runner, and every public entry passes it a Docker
# argv; these tests run real non-Docker processes through it, so they call it directly.


def test_run_timeout_kills_the_tree_and_returns_promptly(tmp_path, monkeypatch):
    """A timed call ends with ``TimeoutExpired`` soon and leaves no descendant running."""
    with heartbeat_tree(tmp_path) as paths:
        argv = parent_argv(tmp_path, sys.executable, paths, "live")
        started = start_deadline_after_tree_start(monkeypatch, paths)
        with pytest.raises(subprocess.TimeoutExpired):
            sr._run(argv, timeout=scaled(2))
        elapsed = time.monotonic() - started[0]
        assert elapsed < scaled(25), f"_run hung {elapsed:.1f}s on a pipe held by a grandchild"
        assert wait_until_heartbeat_stops(paths["heartbeat"]), (
            "grandchild still running after the timeout"
        )


def test_run_keeps_its_result_contract():
    """Timed and untimed calls return text, return non-zero exits, and raise on a missing program."""
    for timeout in (None, hang_guard_s()):
        ok = sr._run(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            timeout=timeout,
        )
        assert (ok.returncode, ok.stdout.strip(), ok.stderr.strip()) == (0, "out", "err"), timeout
        failed = sr._run([sys.executable, "-c", "raise SystemExit(3)"], timeout=timeout)
        assert failed.returncode == 3, timeout
        with pytest.raises(FileNotFoundError):
            sr._run(["booley-no-such-program-3858"], timeout=timeout)
        env = {**os.environ, "BOOLEY_TEST_3858": "seen"}
        argv = sr.DockerRunArgv(
            [sys.executable, "-c", "import os; print(os.environ['BOOLEY_TEST_3858'])"], env
        )
        assert sr._run(argv, timeout=timeout).stdout.strip() == "seen", timeout
