"""Tests for session_runtime: headless Sandbox lifecycle (F-4).

The spec -> `docker run` translation is the part that must not drift from
`devcontainer.build_devcontainer_spec`, so it is tested against a real spec
rather than a hand-written dict.
"""

from __future__ import annotations

import json
import ntpath
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, NoReturn
from unittest.mock import Mock, patch

import pytest

from booley.runtime import devcontainer as dc
from booley.runtime import health, issuance_invalidation, platform_paths
from booley.runtime import session_issuance as runtime_spec
from booley.runtime import session_runtime as sr
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
from tests.lifecycle_lock_support import held_lifecycle_lock, observe_lifecycle_contention
from tests.subprocess_budget import run_bounded_child


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
    invoked: list[str] = []
    monkeypatch.setattr(sr, "_recover_before_lifecycle", lambda *_args: None)
    monkeypatch.setattr(
        sr,
        "_up_unlocked",
        lambda *_args, **_kwargs: invoked.append("up") or "sandbox",
    )

    with (
        held_lifecycle_lock(lock_path) as holder,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        result = executor.submit(sr.up, workspace)
        assert waiting.wait(2)
        assert not result.done()
        holder.release()
        assert result.result(timeout=5) == "sandbox"

    assert invoked == ["up"]
    assert "host Docker lifecycle is busy" in caplog.text


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
    def test_workspace_folder(self, workspace: Path):
        from booley.runtime.platform_paths import docker_mount_path

        out = sr.substitute("source=${localWorkspaceFolder},target=/work", workspace)
        assert out == f"source={docker_mount_path(workspace)},target=/work"

    def test_basename_is_not_eaten_by_the_shorter_key(self, workspace: Path):
        # "${localWorkspaceFolder}" is a prefix of "${localWorkspaceFolderBasename}".
        out = sr.substitute("booley-claude-state-${localWorkspaceFolderBasename}", workspace)
        assert out == "booley-claude-state-i2c"

    def test_local_env_resolves_from_host_env(self, workspace: Path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-tok")
        assert sr.substitute("${localEnv:CLAUDE_CODE_OAUTH_TOKEN}", workspace) == "sk-tok"

    def test_local_env_unset_is_empty(self, workspace: Path, monkeypatch, tmp_path):
        # Isolate the `booley auth` stored credential too: on a dev machine
        # with a real token at ~/.config/booley/ it would resolve and defeat
        # the unset case under test.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        assert sr.substitute("${localEnv:CLAUDE_CODE_OAUTH_TOKEN}", workspace) == ""

    def test_unknown_variable_is_left_alone(self, workspace: Path):
        # Fail loudly at docker, rather than silently mounting the wrong path.
        assert sr.substitute("${containerEnv:HOME}", workspace) == "${containerEnv:HOME}"


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
        assert "source=/c/ws/i2c/.booley,target=/booley-project,type=bind" in mounts

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
        assert "BOOLEY_MCP_MODE=interactive" in env
        assert "BOOLEY_DIR=/booley-project" in env
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


class TestHookArgv:
    def test_runs_the_hook_string_through_a_shell(self):
        argv = sr.hook_argv("c1", "a; b")
        assert argv == ["docker", "exec", "c1", "bash", "-lc", "a; b"]

    def test_post_create_hook_from_a_real_spec_is_a_shell_string(self):
        spec = _spec(login_source="/c/home/.credentials.json")
        # `;`-joined, so it cannot be exec'd as argv — the shell is required.
        assert ";" in spec["postCreateCommand"]
        assert sr.hook_argv("c1", spec["postCreateCommand"])[3:5] == ["bash", "-lc"]


class TestExecArgv:
    def test_non_tty_sets_dumb_terminal(self):
        assert sr.exec_argv("c1", ["python3", "-V"], tty=False) == [
            "docker",
            "exec",
            "-e",
            "TERM=dumb",
            "-i",
            "c1",
            "python3",
            "-V",
        ]

    def test_injects_explicit_command_environment(self):
        argv = sr.exec_argv("c1", ["booley", "doctor"], env={"BOOLEY_TEST": "bad"})
        assert argv == [
            "docker",
            "exec",
            "-t",
            "-e",
            "BOOLEY_TEST=bad",
            "-i",
            "c1",
            "booley",
            "doctor",
        ]


class TestContainerName:
    def test_derived_from_canonical_project(self, workspace: Path):
        assert sr.session_container_name(workspace) == (
            f"booley-session-{dc.canonical_project_id(workspace)}"
        )

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
    from booley.runtime import session_issuance as runtime_spec

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


def _container_probe(name: str, state: dict) -> object:
    encoded = json.dumps([state])

    def docker_stdout(argv: list[str]) -> str | None:
        if argv[:3] == ["docker", "ps", "-a"]:
            return f"{name}\n"
        if argv[:3] == ["docker", "ps", "--filter"]:
            return ""
        if argv[:3] == ["docker", "inspect", name]:
            return encoded
        return None

    return docker_stdout


def _stub_prepare(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    issuance: SimpleNamespace,
    docker_stdout: object,
) -> None:
    from booley.runtime import session_issuance as runtime_spec

    _write_spec(workspace, _spec())
    monkeypatch.setattr(sr, "_docker_stdout", docker_stdout)
    monkeypatch.setattr(
        runtime_spec,
        "authorized_project_data_source",
        lambda _path: workspace / ".booley",
    )
    monkeypatch.setattr(runtime_spec, "validate", lambda *_args: issuance)
    monkeypatch.setattr(runtime_spec, "requested_license", lambda _path, **_kwargs: None)
    monkeypatch.setattr(sr, "_preflight", lambda *_args, **_kwargs: None)


def _record_successful_removals(monkeypatch: pytest.MonkeyPatch, removed: list[list[str]]) -> None:
    monkeypatch.setattr(
        sr,
        "_run",
        lambda argv, **_kwargs: (
            removed.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
        ),
    )


_OUTDATED_LAYOUT_REFUSAL = r"mount an outdated Booley Directory layout"


class TestPrepareLegacyContainer:
    def test_malformed_mount_inventory_fails_loudly(self) -> None:
        with pytest.raises(sr.SessionError, match=r"cannot inspect bind mounts.*current-vscode"):
            sr._container_has_unavailable_bind("current-vscode", {"Mounts": None})

    def test_unmappable_windows_daemon_bind_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sr, "host_path_from_docker_mount", lambda _source: None)
        state = {"Mounts": [{"Type": "bind", "Source": "/run/desktop/mnt/host/wsl/x"}]}

        assert not sr._container_has_unavailable_bind("current-vscode", state)

    @pytest.mark.parametrize("config_label", ["current", "missing", "different"])
    def test_stopped_vscode_container_from_old_issuance_is_removed_before_create(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        config_label: str,
    ) -> None:
        issuance = _test_issuance(workspace)
        container_labels = _vscode_labels(workspace, issuance, spec_digest="old-spec")
        if config_label == "missing":
            container_labels.pop("devcontainer.config_file")
        elif config_label == "different":
            container_labels["devcontainer.config_file"] = str(
                workspace / ".devcontainer" / "legacy.json"
            )
        state = {
            "State": {"Running": False},
            "Config": {"Labels": container_labels},
            "Mounts": [],
        }
        removed: list[list[str]] = []
        _stub_prepare(monkeypatch, workspace, issuance, _container_probe("stale-vscode", state))
        _record_successful_removals(monkeypatch, removed)

        assert sr.prepare(workspace) == "current-spec"
        assert removed == [["docker", "rm", "stale-vscode"]]

    def test_stopped_current_container_with_missing_injected_bind_is_removed(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issuance = _test_issuance(workspace)
        state = {
            "State": {"Running": False},
            "Config": {"Labels": _vscode_labels(workspace, issuance)},
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(workspace / "missing-wayland-socket"),
                    "Destination": "/tmp/vscode-wayland.sock",
                }
            ],
        }
        removed: list[list[str]] = []
        _stub_prepare(monkeypatch, workspace, issuance, _container_probe("current-vscode", state))
        _record_successful_removals(monkeypatch, removed)

        assert sr.prepare(workspace) == "current-spec"
        assert removed == [["docker", "rm", "current-vscode"]]

    def test_running_vscode_container_from_old_issuance_is_never_removed(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime import session_issuance as runtime_spec

        _write_spec(workspace, _spec())
        issuance = SimpleNamespace(
            project_root=str(workspace),
            spec_sha256="current-spec",
            policy_revision=1,
            installation=None,
            license_profile=None,
        )
        labels = dict(label.split("=", 1) for label in runtime_spec.labels(issuance))
        labels["booley.spec-digest"] = "old-spec"
        labels.update(
            {
                "booley.role": "interactive",
                "devcontainer.local_folder": str(workspace),
                "devcontainer.config_file": str(workspace / ".devcontainer" / "devcontainer.json"),
            }
        )
        running = [{"State": {"Running": True}, "Config": {"Labels": labels}, "Mounts": []}]

        def docker_stdout(argv: list[str]) -> str | None:
            if argv[:3] == ["docker", "ps", "-a"]:
                return "active-vscode\n"
            if argv[:3] == ["docker", "ps", "--filter"]:
                return ""
            if argv[:3] == ["docker", "inspect", "active-vscode"]:
                return json.dumps(running)
            return None

        monkeypatch.setattr(sr, "_docker_stdout", docker_stdout)
        monkeypatch.setattr(
            runtime_spec,
            "authorized_project_data_source",
            lambda _path: workspace / ".booley",
        )
        monkeypatch.setattr(runtime_spec, "validate", lambda *_args: issuance)
        monkeypatch.setattr(sr, "_preflight", lambda *_args, **_kwargs: None)
        remove = Mock()
        monkeypatch.setattr(sr, "_run", remove)

        with pytest.raises(sr.SessionError, match=r"running Sandbox.*older host issuance"):
            sr.prepare(workspace)

        remove.assert_not_called()

    def test_headless_session_container_is_never_reconciled_for_vscode(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime import session_issuance as runtime_spec

        _write_spec(workspace, _spec())
        issuance = SimpleNamespace(
            project_root=str(workspace),
            spec_sha256="current-spec",
            policy_revision=1,
            installation=None,
            license_profile=None,
        )
        labels = dict(label.split("=", 1) for label in runtime_spec.labels(issuance))
        labels["booley.spec-digest"] = "old-spec"
        headless = [{"State": {"Running": False}, "Config": {"Labels": labels}, "Mounts": []}]
        name = sr.session_container_name(workspace)

        def docker_stdout(argv: list[str]) -> str | None:
            if argv[:3] == ["docker", "ps", "-a"]:
                return f"{name}\n"
            if argv[:3] == ["docker", "ps", "--filter"]:
                return ""
            if argv[:3] == ["docker", "inspect", name]:
                return json.dumps(headless)
            return None

        monkeypatch.setattr(sr, "_docker_stdout", docker_stdout)
        monkeypatch.setattr(
            runtime_spec,
            "authorized_project_data_source",
            lambda _path: workspace / ".booley",
        )
        monkeypatch.setattr(runtime_spec, "validate", lambda *_args: issuance)
        monkeypatch.setattr(runtime_spec, "requested_license", lambda _path, **_kwargs: None)
        monkeypatch.setattr(sr, "_preflight", lambda *_args, **_kwargs: None)
        remove = Mock()
        monkeypatch.setattr(sr, "_run", remove)

        assert sr.prepare(workspace) == "current-spec"
        remove.assert_not_called()

    def test_inventory_is_scoped_to_the_current_project(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime import session_issuance as runtime_spec

        _write_spec(workspace, _spec())
        issuance = SimpleNamespace(
            project_root=str(workspace),
            spec_sha256="current-spec",
            policy_revision=1,
            installation=None,
            license_profile=None,
        )
        expected_project_id = dict(label.split("=", 1) for label in runtime_spec.labels(issuance))[
            "booley.project-id"
        ]
        probes: list[list[str]] = []

        def docker_stdout(argv: list[str]) -> str | None:
            if argv[:3] == ["docker", "ps", "-a"]:
                probes.append(argv)
                return ""
            if argv[:3] == ["docker", "ps", "--filter"]:
                return ""
            return None

        monkeypatch.setattr(sr, "_docker_stdout", docker_stdout)
        monkeypatch.setattr(
            runtime_spec,
            "authorized_project_data_source",
            lambda _path: workspace / ".booley",
        )
        monkeypatch.setattr(runtime_spec, "validate", lambda *_args: issuance)
        monkeypatch.setattr(runtime_spec, "requested_license", lambda _path, **_kwargs: None)
        monkeypatch.setattr(sr, "_preflight", lambda *_args, **_kwargs: None)

        assert sr.prepare(workspace) == "current-spec"
        assert probes == [
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label={dc.INTERACTIVE_ROLE_LABEL}",
                "--filter",
                f"label=booley.project-id={expected_project_id}",
                "--format",
                "{{.Names}}",
            ]
        ]

    def test_current_container_keeps_valid_binds_and_named_volumes(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issuance = _test_issuance(workspace)
        state = {
            "State": {"Running": False},
            "Config": {"Labels": _vscode_labels(workspace, issuance)},
            "Mounts": [
                {"Type": "bind", "Source": str(workspace), "Destination": "/work"},
                {
                    "Type": "volume",
                    "Name": "booley-claude-state-i2c",
                    "Source": "/var/lib/docker/volumes/not-a-host-bind",
                    "Destination": "/home/agent/.claude",
                },
            ],
        }
        _stub_prepare(monkeypatch, workspace, issuance, _container_probe("current-vscode", state))
        remove = Mock()
        monkeypatch.setattr(sr, "_run", remove)

        assert sr.prepare(workspace) == "current-spec"
        remove.assert_not_called()

    def test_container_that_starts_during_cleanup_is_not_force_removed(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issuance = _test_issuance(workspace)
        state = {
            "State": {"Running": False},
            "Config": {"Labels": _vscode_labels(workspace, issuance, spec_digest="old-spec")},
            "Mounts": [],
        }
        _stub_prepare(monkeypatch, workspace, issuance, _container_probe("racing-vscode", state))
        remove = Mock(
            return_value=subprocess.CompletedProcess(
                ["docker", "rm", "racing-vscode"],
                1,
                "",
                "container is running",
            )
        )
        monkeypatch.setattr(sr, "_run", remove)

        with pytest.raises(sr.SessionError, match=r"not force-remove.*may have become active"):
            sr.prepare(workspace)

        assert remove.call_args.args[0] == ["docker", "rm", "racing-vscode"]

    def test_missing_generated_bind_names_source_and_reseed_action(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime import session_issuance as runtime_spec

        _write_spec(workspace, _spec())
        source = "/host/skills/renamed-skill"
        target = f"{dc.HOST_SKILLS_SIDECAR}/example-skill"
        monkeypatch.setattr(
            runtime_spec,
            "authorized_project_data_source",
            lambda _path: workspace / ".booley",
        )
        monkeypatch.setattr(
            runtime_spec,
            "validate",
            Mock(
                side_effect=runtime_spec.RuntimeSpecError(
                    f"generated bind source for {target} is missing: {source}"
                )
            ),
        )
        monkeypatch.setattr(sr, "_strict_running_interactive_states", lambda: [])

        with pytest.raises(sr.SessionError) as caught:
            sr.prepare(workspace)

        message = str(caught.value)
        assert source in message
        assert target in message
        assert "booley session refresh" in message

    def test_ambiguous_running_legacy_container_refuses_without_mutation(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_spec(workspace, _spec())
        legacy = [
            {
                "Config": {"Labels": {"devcontainer.local_folder": str(workspace)}},
                "Mounts": [{"Destination": "/work", "Type": "bind", "RW": True}],
            }
        ]
        validate = Mock()
        monkeypatch.setattr(
            sr, "_strict_running_interactive_states", lambda: [("legacy", json.dumps(legacy))]
        )
        monkeypatch.setattr(
            "booley.runtime.session_issuance.authorized_project_data_source",
            lambda _path: workspace / ".booley",
        )
        monkeypatch.setattr("booley.runtime.session_issuance.validate", validate)

        with pytest.raises(sr.SessionError, match=_OUTDATED_LAYOUT_REFUSAL) as caught:
            sr.prepare(workspace)

        message = str(caught.value)
        assert "'legacy'" in message
        assert "docker rm" not in message
        assert "Booley will not remove them automatically" in message
        validate.assert_not_called()

    def test_running_dual_bind_container_allows_validation(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_spec(workspace, _spec())
        source = str(workspace / ".booley")
        current = [
            {
                "Mounts": [
                    {
                        "Destination": "/booley-project",
                        "Source": source,
                        "Type": "bind",
                        "RW": True,
                    },
                    {
                        "Destination": "/work/.booley",
                        "Source": source,
                        "Type": "bind",
                        "RW": True,
                    },
                ],
                "Config": {"Labels": {"devcontainer.local_folder": str(workspace)}},
            }
        ]
        issuance = SimpleNamespace(
            project_root=str(workspace),
            spec_sha256="abc",
            policy_revision=1,
            installation=None,
            license_profile=None,
        )
        monkeypatch.setattr(
            sr, "_strict_running_interactive_states", lambda: [("current", json.dumps(current))]
        )
        monkeypatch.setattr(
            "booley.runtime.session_issuance.authorized_project_data_source",
            lambda _path: workspace / ".booley",
        )
        monkeypatch.setattr("booley.runtime.session_issuance.validate", lambda *_args: issuance)
        monkeypatch.setattr(
            "booley.runtime.session_issuance.requested_license", lambda _path, **_kwargs: None
        )
        monkeypatch.setattr(sr, "_preflight", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(sr, "_strict_all_interactive_states", lambda *_args: [])

        assert sr.prepare(workspace) == "abc"

    def test_inventory_failure_blocks_before_stamp_validation(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_spec(workspace, _spec())
        validate = Mock()
        monkeypatch.setattr(sr, "_docker_stdout", lambda _args: None)
        monkeypatch.setattr(
            "booley.runtime.session_issuance.authorized_project_data_source",
            lambda _path: workspace / ".booley",
        )
        monkeypatch.setattr("booley.runtime.session_issuance.validate", validate)

        with pytest.raises(sr.SessionError, match="cannot inventory"):
            sr.prepare(workspace)

        validate.assert_not_called()

    def test_source_transition_to_local_blocks_external_container(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_spec(workspace, _spec())
        external = workspace.parent / "external-project-data"
        legacy = [
            {
                "Config": {"Labels": {"devcontainer.local_folder": str(workspace)}},
                "Mounts": [
                    {
                        "Destination": "/booley-project",
                        "Source": str(external),
                        "Type": "bind",
                        "RW": True,
                    },
                    {
                        "Destination": "/work",
                        "Source": str(workspace),
                        "Type": "bind",
                        "RW": True,
                    },
                ],
            }
        ]
        monkeypatch.setattr(
            sr, "_strict_running_interactive_states", lambda: [("legacy", json.dumps(legacy))]
        )
        monkeypatch.setattr(
            "booley.runtime.session_issuance.authorized_project_data_source",
            lambda _path: workspace / ".booley",
        )

        with pytest.raises(sr.SessionError, match=_OUTDATED_LAYOUT_REFUSAL):
            sr.prepare(workspace)

    def _legacy_vscode_state(self, workspace: Path, container_id: str) -> str:
        """A running VS Code container that still mounts only the workspace (pre-0.3.0)."""
        return json.dumps(
            [
                {
                    "Id": container_id,
                    "State": {"Running": True},
                    "Config": {"Labels": _vscode_labels(workspace, _test_issuance(workspace))},
                    "Mounts": [{"Destination": "/work", "Type": "bind", "RW": True}],
                }
            ]
        )

    def test_running_legacy_vscode_container_is_refused_with_repair(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime import session_admission

        issuance = _test_issuance(workspace)
        _stub_prepare(monkeypatch, workspace, issuance, lambda _argv: None)
        monkeypatch.setattr(
            sr,
            "_strict_running_interactive_states",
            lambda: [("nifty_wright", self._legacy_vscode_state(workspace, "a" * 64))],
        )
        run = Mock()
        monkeypatch.setattr(sr, "_run", run)
        claim = Mock()
        monkeypatch.setattr(session_admission, "claim_vscode_start", claim)

        with pytest.raises(sr.SessionError) as caught:
            sr.prepare(workspace)

        message = str(caught.value)
        assert "'nifty_wright'" in message
        assert "close VS Code" in message
        assert "`docker stop nifty_wright` and `docker rm nifty_wright`" in message
        assert "`booley init --seed`" in message
        run.assert_not_called()
        claim.assert_not_called()

    def test_legacy_container_refusal_precedes_stale_stamp_rejection(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime import session_issuance as runtime_spec

        _write_spec(workspace, _spec())
        monkeypatch.setattr(
            runtime_spec, "authorized_project_data_source", lambda _path: workspace / ".booley"
        )
        validate = Mock(
            side_effect=runtime_spec.RuntimeSpecError(
                "host-issued spec stamp has an unsupported version"
            )
        )
        monkeypatch.setattr(runtime_spec, "validate", validate)
        monkeypatch.setattr(
            sr,
            "_strict_running_interactive_states",
            lambda: [("nifty_wright", self._legacy_vscode_state(workspace, "a" * 64))],
        )
        run = Mock()
        monkeypatch.setattr(sr, "_run", run)

        with pytest.raises(sr.SessionError) as caught:
            sr.prepare(workspace)

        assert "`docker rm nifty_wright`" in str(caught.value)
        assert "`booley init --seed`" in str(caught.value)
        validate.assert_not_called()
        run.assert_not_called()

    def test_source_transition_to_local_refuses_identified_container(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A current-release container on an external source is outdated once the source is local."""
        from booley.runtime import session_issuance as runtime_spec

        external = workspace.parent / "external-project-data"
        issuance = _test_issuance(workspace)
        state = json.loads(self._legacy_vscode_state(workspace, "b" * 64))
        state[0]["Mounts"] = [
            {
                "Destination": "/booley-project",
                "Source": str(external),
                "Type": "bind",
                "RW": True,
            },
            {"Destination": "/work", "Source": str(workspace), "Type": "bind", "RW": True},
        ]
        _stub_prepare(monkeypatch, workspace, issuance, lambda _argv: None)
        monkeypatch.setattr(runtime_spec, "validate", Mock())
        monkeypatch.setattr(
            sr, "_strict_running_interactive_states", lambda: [("editor", json.dumps(state))]
        )
        run = Mock()
        monkeypatch.setattr(sr, "_run", run)

        with pytest.raises(sr.SessionError, match=_OUTDATED_LAYOUT_REFUSAL) as caught:
            sr.prepare(workspace)

        assert "`docker rm editor`" in str(caught.value)
        run.assert_not_called()

    @pytest.mark.parametrize(
        "damage",
        [
            {"image": 7},
            {"mounts": "not-a-list"},
            {"mounts": [7]},
        ],
        ids=["image", "mounts-type", "mount-entry"],
    )
    def test_unreadable_spec_is_left_to_validation(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, damage: dict
    ) -> None:
        from booley.runtime import session_issuance as runtime_spec

        _write_spec(workspace, {**_spec(), **damage})
        monkeypatch.setattr(
            sr,
            "_strict_running_interactive_states",
            lambda: pytest.fail("an unreadable spec must not reach the container inventory"),
        )
        monkeypatch.setattr(
            runtime_spec,
            "validate",
            Mock(side_effect=runtime_spec.RuntimeSpecError("spec is malformed")),
        )

        with pytest.raises(
            sr.SessionError, match="refusing Sandbox preparation: spec is malformed"
        ):
            sr.prepare(workspace)

    def test_windows_host_paths_normalize_slashes_and_drive_case(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(os.path, "normcase", ntpath.normcase)
        assert platform_paths.same_host_path(
            "c:/workplace/picorv32/.devcontainer/devcontainer.json",
            Path(r"C:\workplace\picorv32\.devcontainer\devcontainer.json"),
        )


class TestIssuedRuntimeDriftFix:
    def _state(
        self,
        workspace: Path,
        *,
        running: bool,
        exact_origin: bool = True,
        pinned: bool = True,
    ) -> dict:
        issuance = _test_issuance(workspace)
        labels = _vscode_labels(workspace, issuance, spec_digest="old-spec")
        if not exact_origin:
            labels.pop("devcontainer.local_folder")
        # Current, pinned Booley Directory mounts: a running editor is not outdated.
        source = str(workspace.resolve() / ".booley")
        mounts = [
            {"Destination": target, "Source": source, "Type": "bind", "RW": True}
            for target in ("/booley-project", "/work/.booley")
        ]
        return {
            "State": {"Running": running},
            "Config": {"Labels": labels},
            "Mounts": mounts if pinned else [],
        }

    def test_stopped_exact_vscode_uses_automatic_rebuild_guidance(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = self._state(workspace, running=False)
        monkeypatch.setattr(sr, "_docker_stdout", lambda _argv: json.dumps([state]))

        fix = sr.issued_runtime_drift_fix(
            workspace,
            _test_issuance(workspace),
            ["stale-vscode"],
        )

        assert fix == "run `booley session down`, then `booley session up --rebuild`"

    def test_running_exact_vscode_requires_stop_first(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = self._state(workspace, running=True)
        monkeypatch.setattr(sr, "_docker_stdout", lambda _argv: json.dumps([state]))

        fix = sr.issued_runtime_drift_fix(
            workspace,
            _test_issuance(workspace),
            ["active-vscode"],
        )

        assert "stop VS Code Sandbox(s) 'active-vscode'" in fix

    def test_running_legacy_vscode_fix_names_removal_steps(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = self._state(workspace, running=True, pinned=False)
        monkeypatch.setattr(sr, "_docker_stdout", lambda _argv: json.dumps([state]))

        fix = sr.issued_runtime_drift_fix(
            workspace,
            _test_issuance(workspace),
            ["active-vscode"],
        )

        assert "`docker rm active-vscode`" in fix
        assert "`booley init --seed`" in fix
        assert "stop VS Code Sandbox(s)" not in fix

    def test_failed_layout_probe_requires_manual_inspection(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = self._state(workspace, running=True, pinned=False)
        monkeypatch.setattr(sr, "_docker_stdout", lambda _argv: json.dumps([state]))
        monkeypatch.setattr(
            sr,
            "_project_data_mount_root_is_pinned",
            Mock(side_effect=sr.SessionError("cannot prove Booley Directory layout")),
        )

        fix = sr.issued_runtime_drift_fix(
            workspace,
            _test_issuance(workspace),
            ["active-vscode"],
        )

        assert "inspect ambiguous Sandbox resource(s) 'active-vscode'" in fix
        assert "docker rm" not in fix

    def test_ambiguous_origin_requires_manual_inspection(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = self._state(workspace, running=False, exact_origin=False)
        monkeypatch.setattr(sr, "_docker_stdout", lambda _argv: json.dumps([state]))

        fix = sr.issued_runtime_drift_fix(
            workspace,
            _test_issuance(workspace),
            ["ambiguous-runtime"],
        )

        assert "inspect ambiguous Sandbox resource(s) 'ambiguous-runtime'" in fix
        assert "will not remove them automatically" in fix

    def test_malformed_runtime_state_requires_manual_inspection(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        state = self._state(workspace, running=False)
        state["State"] = "unknown"
        monkeypatch.setattr(sr, "_docker_stdout", lambda _argv: json.dumps([state]))

        fix = sr.issued_runtime_drift_fix(
            workspace,
            _test_issuance(workspace),
            ["malformed-runtime"],
        )

        assert "inspect ambiguous Sandbox resource(s) 'malformed-runtime'" in fix

    def test_reports_running_and_ambiguous_resources_together(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        states = {
            "active-vscode": self._state(workspace, running=True),
            "ambiguous-runtime": self._state(
                workspace,
                running=False,
                exact_origin=False,
            ),
        }
        monkeypatch.setattr(
            sr,
            "_docker_stdout",
            lambda argv: json.dumps([states[argv[-1]]]),
        )

        fix = sr.issued_runtime_drift_fix(
            workspace,
            _test_issuance(workspace),
            list(states),
        )

        assert "inspect ambiguous Sandbox resource(s) 'ambiguous-runtime'" in fix
        assert "stop VS Code Sandbox(s) 'active-vscode'" in fix

    def test_windows_host_paths_identify_running_vscode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = Path(r"C:\workplace\picorv32")
        state = self._state(workspace, running=True)
        labels = state["Config"]["Labels"]
        labels["devcontainer.local_folder"] = "c:/workplace/picorv32"
        labels["devcontainer.config_file"] = (
            "c:/workplace/picorv32/.devcontainer/devcontainer.json"
        )
        monkeypatch.setattr(os.path, "normcase", ntpath.normcase)
        monkeypatch.setattr(sr, "_docker_stdout", lambda _argv: json.dumps([state]))
        monkeypatch.setattr(sr, "session_container_name", lambda _workspace: "canonical")

        fix = sr.issued_runtime_drift_fix(
            workspace,
            _test_issuance(workspace),
            ["active-vscode"],
        )

        assert "stop VS Code Sandbox(s) 'active-vscode'" in fix


# ---------------------------------------------------------------------------
# up() / down() / status()
# ---------------------------------------------------------------------------


def _write_spec(workspace: Path, spec: dict) -> None:
    dc.write_devcontainer(workspace, spec)


@pytest.fixture
def wired(workspace: Path, request: pytest.FixtureRequest):
    """A workspace with a spec on disk and mocked external boundaries."""
    from booley.runtime import image_lifecycle

    _write_spec(workspace, _spec())
    lifecycle_reconcile = (
        nullcontext()
        if getattr(request, "param", None) == "real-image-lifecycle"
        else patch.object(
            image_lifecycle,
            "reconcile",
            return_value=image_lifecycle.LifecycleResult(
                "booley-sandbox",
                "sha256:fixture",
                image_lifecycle.Status.CURRENT,
            ),
        )
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
    with (
        patch.object(sr.idk, "network_exists", return_value=True),
        patch.object(sr.idk, "image_exists", return_value=True),
        # No image metadata, as when the inspect finds nothing (no Docker CLI call).
        patch.object(sr.idk, "inspect_image_metadata", return_value=None),
        # The stale-container probe resolves the image ID through the Docker helper.
        patch.object(sr.idk, "image_id", return_value=None),
        lifecycle_reconcile,
        patch(
            "booley.runtime.session_issuance.validate",
            return_value=issuance,
        ),
        patch("booley.runtime.session_issuance.authenticate", return_value=issuance),
        patch(
            "booley.runtime.session_issuance.authorized_project_data_source",
            return_value=workspace / ".booley",
        ),
        patch.object(sr, "_strict_running_interactive_states", return_value=[]),
        patch.object(sr, "_strict_all_interactive_states", return_value=[]),
        patch.object(sr, "container_matches_issuance", return_value=True),
        patch.object(sr, "_run") as run,
    ):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        yield workspace, run


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


class TestInitReconciliation:
    def test_plan_reports_stopped_old_runtime_without_removing_it(self, workspace: Path) -> None:
        issuance = SimpleNamespace(license_profile=None, relay_image_id=None)
        state = _refresh_state(running=False)
        with (
            patch.object(sr, "_strict_refresh_container", return_value=state),
            patch.object(sr, "_refresh_project_id", return_value="project-id"),
            patch.object(sr, "_refresh_candidate_matches", return_value=False),
            patch.object(sr, "_strict_all_interactive_states", return_value=[]),
            patch.object(sr, "_remove_stopped_session_container") as remove,
            patch.object(sr, "_relay_objects_exist", return_value=False),
        ):
            plan = sr.plan_stopped_headless_runtime_reconciliation(workspace, issuance)

        assert plan.pending
        remove.assert_not_called()

    def test_removes_only_a_stopped_owned_runtime_from_an_old_issuance(
        self, workspace: Path
    ) -> None:
        issuance = SimpleNamespace(license_profile=None, relay_image_id=None)
        state = _refresh_state(running=False)
        with (
            patch.object(sr, "_strict_refresh_container", return_value=state),
            patch.object(sr, "_refresh_project_id", return_value="project-id"),
            patch.object(sr, "_refresh_candidate_matches", return_value=False),
            patch.object(sr, "_strict_all_interactive_states", return_value=[]),
            patch.object(sr, "_remove_stopped_session_container") as remove,
            patch.object(sr, "_relay_objects_exist", return_value=False),
        ):
            changed = sr.reconcile_stopped_headless_runtime(workspace, issuance)

        assert changed
        remove.assert_called_once_with(sr.session_container_name(workspace))

    @pytest.mark.parametrize("running", [True, False])
    def test_preserves_current_runtime(self, workspace: Path, running: bool) -> None:
        issuance = SimpleNamespace(license_profile=None, relay_image_id=None)
        state = _refresh_state(running=running)
        with (
            patch.object(sr, "_strict_refresh_container", return_value=state),
            patch.object(sr, "_refresh_project_id", return_value="project-id"),
            patch.object(sr, "_refresh_candidate_matches", return_value=True),
            patch.object(sr, "_remove_stopped_session_container") as remove,
        ):
            changed = sr.reconcile_stopped_headless_runtime(workspace, issuance)

        assert not changed
        remove.assert_not_called()

    def test_stale_running_runtime_names_grant_change_remedy(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal names the workflow Sandbox validation prints after a Grant change."""
        from booley.runtime import runtime_context, session_issuance

        def withdrawn(_root: Path) -> NoReturn:
            raise session_issuance.IssuanceUnavailableError("withdrawn")

        monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
        monkeypatch.setattr(session_issuance, "load_issued_snapshot", withdrawn)
        state = _refresh_state(running=True)
        with (
            patch.object(sr, "_strict_refresh_container", return_value=state),
            patch.object(sr, "_refresh_project_id", return_value="project-id"),
            patch.object(sr, "_refresh_candidate_matches", return_value=False),
            patch.object(sr, "_remove_stopped_session_container") as remove,
            pytest.raises(sr.SessionError) as refusal,
        ):
            sr.reconcile_stopped_headless_runtime(workspace, SimpleNamespace())

        message = str(refusal.value)
        assert "uses an older host issuance; run `booley session refresh` on the host" in message
        assert "run `booley session refresh` on the host" in message
        assert "init --seed" not in message
        assert "retry `booley init`" not in message
        remove.assert_not_called()

    def test_stale_running_runtime_names_refresh_when_issuance_stands(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a withdrawn issuance the refusal names `session refresh`, as validation does."""
        from booley.runtime import runtime_context, session_issuance

        monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
        monkeypatch.setattr(
            session_issuance,
            "load_issued_snapshot",
            lambda _root: SimpleNamespace(license_profile=None),
        )
        monkeypatch.setattr(sr, "vscode_session_container", lambda _root: None)
        monkeypatch.setattr(sr.project_image, "project_sandbox_image", lambda _root: "selected")
        monkeypatch.setattr(sr.project_image, "is_managed_sandbox_image", lambda *_args: True)
        state = _refresh_state(running=True)
        with (
            patch.object(sr, "_strict_refresh_container", return_value=state),
            patch.object(sr, "_refresh_project_id", return_value="project-id"),
            patch.object(sr, "_refresh_candidate_matches", return_value=False),
            pytest.raises(sr.SessionError) as refusal,
        ):
            sr.reconcile_stopped_headless_runtime(workspace, SimpleNamespace())

        message = str(refusal.value)
        assert message.endswith("run `booley session refresh` on the host")
        assert "retry `booley init`" not in message

    def test_other_project_session_blocks_relay_mutation(self, workspace: Path) -> None:
        state = _refresh_state(running=False)
        with (
            patch.object(sr, "_strict_refresh_container", return_value=state),
            patch.object(sr, "_refresh_project_id", return_value="project-id"),
            patch.object(sr, "_refresh_candidate_matches", return_value=False),
            patch.object(
                sr,
                "_strict_all_interactive_states",
                return_value=[("vscode-session", "state")],
            ),
            patch.object(sr, "_remove_stopped_session_container") as remove,
            patch.object(sr, "_remove_license_relay") as remove_relay,
            pytest.raises(sr.SessionError, match="other Project Sessions"),
        ):
            sr.reconcile_stopped_headless_runtime(workspace, SimpleNamespace())

        remove.assert_not_called()
        remove_relay.assert_not_called()

    def test_retry_finishes_stale_relay_cleanup_after_container_is_gone(
        self, workspace: Path
    ) -> None:
        relay = SimpleNamespace(relay_container="relay", session_id="project-relay")
        with (
            patch.object(sr, "_strict_refresh_container", return_value=None),
            patch.object(sr, "_refresh_project_id", return_value="project-id"),
            patch.object(sr, "_relay_resources", return_value=relay),
            patch.object(sr, "_relay_objects_exist", return_value=True),
            patch.object(sr, "_relay_matches_issuance", return_value=False),
            patch.object(sr, "_strict_all_interactive_states", return_value=[]),
            patch.object(sr, "_remove_license_relay") as remove_relay,
        ):
            changed = sr.reconcile_stopped_headless_runtime(workspace, SimpleNamespace())

        assert changed
        remove_relay.assert_called_once_with(relay)

    def test_missing_relay_container_is_not_current(self, workspace: Path) -> None:
        relay = SimpleNamespace(relay_container="relay", session_id="project-relay")
        with patch.object(sr, "_strict_refresh_container", return_value=None):
            assert not sr._relay_matches_issuance(relay, _test_issuance(workspace))

    def test_foreign_relay_container_is_preserved(self, workspace: Path) -> None:
        relay = SimpleNamespace(relay_container="relay", session_id="project-relay")
        state = {
            "Config": {
                "Labels": {
                    "booley.role": "interactive",
                    "booley.session-id": relay.session_id,
                }
            }
        }
        with (
            patch.object(sr, "_strict_refresh_container", return_value=state),
            pytest.raises(sr.SessionError, match="not owned by this Project"),
        ):
            sr._relay_matches_issuance(relay, _test_issuance(workspace))

    def test_relay_issuance_labels_determine_currency(self, workspace: Path) -> None:
        relay = SimpleNamespace(relay_container="relay", session_id="project-relay")
        issuance = _test_issuance(workspace)
        labels = dict(label.split("=", 1) for label in runtime_spec.labels(issuance))
        labels.update(
            {
                "booley.role": "license-relay",
                "booley.session-id": relay.session_id,
            }
        )
        state = {"Image": issuance.relay_image_id, "Config": {"Labels": labels}}
        with patch.object(sr, "_strict_refresh_container", return_value=state):
            assert sr._relay_matches_issuance(relay, issuance)
            labels["booley.spec-digest"] = "older-spec"
            assert not sr._relay_matches_issuance(relay, issuance)

    def test_relay_image_identity_determines_currency(self, workspace: Path) -> None:
        relay = SimpleNamespace(relay_container="relay", session_id="project-relay")
        issuance = _test_issuance(workspace)
        labels = dict(label.split("=", 1) for label in runtime_spec.labels(issuance))
        labels.update(
            {
                "booley.role": "license-relay",
                "booley.session-id": relay.session_id,
            }
        )
        state = {"Image": "sha256:" + "b" * 64, "Config": {"Labels": labels}}

        with patch.object(sr, "_strict_refresh_container", return_value=state):
            assert not sr._relay_matches_issuance(relay, issuance)

    def test_preserves_foreign_container(self, workspace: Path) -> None:
        issuance = SimpleNamespace(license_profile=None, relay_image_id=None)
        state = _refresh_state(labels={"booley.role": "interactive"})
        with (
            patch.object(sr, "_strict_refresh_container", return_value=state),
            patch.object(sr, "_refresh_project_id", return_value="project-id"),
            patch.object(sr, "_remove_stopped_session_container") as remove,
            pytest.raises(sr.SessionError, match="was not modified"),
        ):
            sr.reconcile_stopped_headless_runtime(workspace, issuance)

        remove.assert_not_called()


class TestRefreshContainerTransactions:
    def test_strict_inspect_accepts_complete_state_and_absence(self, monkeypatch):
        state = _refresh_state()
        responses = iter(
            [
                subprocess.CompletedProcess([], 0, json.dumps(state), ""),
                subprocess.CompletedProcess([], 1, "", "No such container"),
            ]
        )
        monkeypatch.setattr(sr, "_run", lambda *_args, **_kwargs: next(responses))

        assert sr._strict_refresh_container("session") == state
        assert sr._strict_refresh_container("missing") is None

    @pytest.mark.parametrize(
        ("result", "message"),
        [
            (subprocess.CompletedProcess([], 1, "", "permission denied"), "permission denied"),
            (subprocess.CompletedProcess([], 0, "not-json", ""), "invalid inspection"),
            (subprocess.CompletedProcess([], 0, "[]", ""), "invalid inspection"),
            (
                subprocess.CompletedProcess(
                    [],
                    0,
                    json.dumps(
                        {
                            "Config": {},
                            "State": {"Running": False},
                            "NetworkSettings": {"Networks": {}},
                        }
                    ),
                    "",
                ),
                "incomplete inspection",
            ),
        ],
    )
    def test_strict_inspect_rejects_untrusted_docker_output(self, monkeypatch, result, message):
        monkeypatch.setattr(sr, "_run", lambda *_args, **_kwargs: result)

        with pytest.raises(sr.SessionError, match=message):
            sr._strict_refresh_container("session")

    def test_refresh_labels_reject_non_string_values(self):
        state = _refresh_state(labels={"booley.role": 7})

        with pytest.raises(sr.SessionError, match="invalid labels"):
            sr._refresh_container_labels(state)

    def test_candidate_match_checks_exact_image_and_all_issuance_labels(self):
        state = _refresh_state(labels={"expected": "yes", "extra": "allowed"})
        state["Image"] = "sha256:fresh"
        issuance = SimpleNamespace(image_id="sha256:fresh")
        with patch.object(runtime_spec, "labels", return_value=("expected=yes",)):
            assert sr._refresh_candidate_matches(state, issuance)
            state["Image"] = "sha256:different"
            assert not sr._refresh_candidate_matches(state, issuance)

    def test_shared_parking_reports_rename_and_restart_failures(self, monkeypatch):
        parked = sr.ParkedSession("session", "session-pre-refresh", True)

        def fail_rename_and_restart(argv, **_kwargs):
            if argv[:2] == ["docker", "stop"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 1, "", f"{argv[1]} failed")

        monkeypatch.setattr(sr, "_run", fail_rename_and_restart)

        with pytest.raises(sr.SessionError, match=r"rename failed.*restart.*start failed"):
            sr._park_session_container(parked)

    def test_rebuild_rejects_existing_recovery_container(self):
        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            pytest.raises(sr.SessionError, match="recovery container"),
        ):
            sr._park_session_for_rebuild("session")


class TestUp:
    def test_new_sandbox_refusal_happens_before_relay_or_docker_mutation(self, wired):
        workspace, run = wired
        from booley.runtime import session_admission

        run.reset_mock()
        with (
            patch.object(sr.idk, "container_exists", return_value=False),
            patch.object(
                session_admission,
                "admit_start",
                side_effect=session_admission.AdmissionError("host is at max_sessions=1"),
            ),
            patch.object(sr, "_prepare_license_relay") as relay,
            pytest.raises(sr.SessionError, match="max_sessions=1"),
        ):
            sr.up(workspace)

        relay.assert_not_called()
        run.assert_not_called()

    @pytest.mark.parametrize(
        "wired",
        ["real-image-lifecycle"],
        indirect=True,
        ids=["real-image-lifecycle"],
    )
    def test_checks_project_image_provenance_before_starting(
        self,
        wired,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from booley.runtime import image_lifecycle

        workspace, _run = wired
        booley_dir = workspace / ".booley"
        booley_dir.mkdir()
        (booley_dir / "booley.toml").write_text(
            '[sandbox]\nimage = "custom/session"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            image_lifecycle,
            "_docker_adapter",
            lambda: SimpleNamespace(image_id=lambda _image: None),
        )
        ours = sr.session_container_name(workspace)

        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr.idk, "container_running", return_value=True),
        ):
            assert sr.up(workspace) == ours

    def test_creates_container_and_runs_both_hooks(self, wired):
        workspace, run = wired
        ours = sr.session_container_name(workspace)
        with (
            patch.object(sr.idk, "container_exists", return_value=False),
            patch.object(sr.idk, "container_running", return_value=False),
        ):
            assert sr.up(workspace) == ours

        argvs = [_argv_of(c) for c in run.call_args_list]
        assert argvs[0][:2] == ["docker", "run"]
        hooks = [a for a in argvs if a[:2] == ["docker", "exec"]]
        assert len(hooks) == 2  # postCreate + postStart

    def test_rebuild_removes_stopped_vscode_from_old_issuance_after_create(
        self,
        wired,
    ) -> None:
        workspace, run = wired
        issuance = runtime_spec.validate(
            workspace,
            json.loads((workspace / ".devcontainer" / "devcontainer.json").read_text()),
            workspace / ".devcontainer" / "devcontainer.json",
        )
        state = {
            "State": {"Running": False},
            "Config": {"Labels": _vscode_labels(workspace, issuance, spec_digest="old-spec")},
            "Mounts": [],
        }
        stale = "stale-vscode"
        with (
            patch.object(
                sr, "_strict_all_interactive_states", return_value=[(stale, json.dumps([state]))]
            ),
            patch.object(sr.idk, "container_exists", return_value=False),
            patch.object(sr.idk, "container_running", return_value=False),
        ):
            sr.up(workspace, rebuild=True)

        argvs = [_argv_of(call) for call in run.call_args_list]
        assert argvs.index(["docker", "rm", stale]) > next(
            index for index, argv in enumerate(argvs) if argv[:2] == ["docker", "run"]
        )
        assert not any(argv[:2] == ["docker", "volume"] for argv in argvs)

    def test_session_up_preserves_current_vscode_with_unavailable_bind(
        self,
        wired,
    ) -> None:
        workspace, run = wired
        issuance = runtime_spec.validate(
            workspace,
            json.loads((workspace / ".devcontainer" / "devcontainer.json").read_text()),
            workspace / ".devcontainer" / "devcontainer.json",
        )
        state = {
            "State": {"Running": False},
            "Config": {
                "Labels": _vscode_labels(
                    workspace,
                    issuance,
                    spec_digest=issuance.spec_sha256,
                )
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(workspace / "missing-wayland-socket"),
                    "Destination": "/tmp/vscode-wayland.sock",
                }
            ],
        }
        with (
            patch.object(
                sr,
                "_strict_all_interactive_states",
                return_value=[("current-vscode", json.dumps([state]))],
            ),
            patch.object(sr.idk, "container_exists", return_value=False),
            patch.object(sr.idk, "container_running", return_value=False),
        ):
            sr.up(workspace)

        assert ["docker", "rm", "current-vscode"] not in [
            _argv_of(call) for call in run.call_args_list
        ]

    def test_session_up_rejects_running_stale_vscode_before_create(
        self,
        wired,
    ) -> None:
        workspace, run = wired
        issuance = runtime_spec.validate(
            workspace,
            json.loads((workspace / ".devcontainer" / "devcontainer.json").read_text()),
            workspace / ".devcontainer" / "devcontainer.json",
        )
        state = {
            "State": {"Running": True},
            "Config": {"Labels": _vscode_labels(workspace, issuance, spec_digest="old-spec")},
            "Mounts": [],
        }
        with (
            patch.object(
                sr,
                "_strict_all_interactive_states",
                return_value=[("active-vscode", json.dumps([state]))],
            ),
            pytest.raises(sr.SessionError, match=r"running Sandbox.*older host issuance"),
        ):
            sr.up(workspace)

        assert not any(_argv_of(call)[:2] == ["docker", "run"] for call in run.call_args_list)

    def test_existing_stopped_container_is_started_not_recreated(self, wired):
        workspace, run = wired
        ours = sr.session_container_name(workspace)
        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr.idk, "container_running", return_value=False),
        ):
            sr.up(workspace)

        argvs = [_argv_of(c) for c in run.call_args_list]
        assert ["docker", "start", ours] in argvs
        assert not any(a[:2] == ["docker", "run"] for a in argvs)

    def test_existing_container_with_runtime_drift_is_never_started(self, wired):
        workspace, run = wired
        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr, "container_matches_issuance", return_value=False),
            pytest.raises(sr.SessionError, match="does not match the current host issuance"),
        ):
            sr.up(workspace)
        assert not any(_argv_of(call)[:2] == ["docker", "start"] for call in run.call_args_list)

    def test_running_container_only_runs_post_start(self, wired):
        workspace, run = wired
        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr.idk, "container_running", return_value=True),
        ):
            sr.up(workspace)

        argvs = [_argv_of(c) for c in run.call_args_list]
        assert not any(a[:2] == ["docker", "run"] for a in argvs)
        assert not any(a[:2] == ["docker", "start"] for a in argvs)
        hooks = [a for a in argvs if a[:2] == ["docker", "exec"]]
        assert len(hooks) == 1  # postStart only; postCreate must not re-seed

    def test_post_create_does_not_rerun_on_resume(self, wired):
        """A resumed container must not re-copy the credential seed."""
        workspace, run = wired
        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr.idk, "container_running", return_value=False),
        ):
            sr.up(workspace)

        hooks = [
            _argv_of(c)[-1] for c in run.call_args_list if _argv_of(c)[:2] == ["docker", "exec"]
        ]
        assert hooks == [f"{dc.git_identity_command()} && {dc.mcp_post_start_command()}"]

    def test_rebuild_parks_old_container_until_replacement_succeeds(self, wired):
        workspace, run = wired
        exists = iter([True, False, False])
        with (
            patch.object(sr.idk, "container_exists", side_effect=lambda _n: next(exists)),
            patch.object(sr.idk, "container_running", return_value=False),
            patch.object(
                sr.idk,
                "network_exists",
                side_effect=lambda name: name == dc.EGRESS_NETWORK,
            ),
        ):
            sr.up(workspace, rebuild=True)

        argvs = [_argv_of(c) for c in run.call_args_list]
        backup = f"{sr.session_container_name(workspace)}-pre-refresh"
        assert ["docker", "rename", sr.session_container_name(workspace), backup] in argvs
        assert any(a[:2] == ["docker", "run"] for a in argvs)
        assert ["docker", "rm", "-f", backup] in argvs

    def test_failed_refresh_probe_restores_parked_container(self, wired):
        workspace, run = wired
        name = sr.session_container_name(workspace)
        backup = f"{name}-pre-refresh"
        issuance = runtime_spec.validate(
            workspace,
            json.loads((workspace / ".devcontainer" / "devcontainer.json").read_text()),
            workspace / ".devcontainer" / "devcontainer.json",
        )
        stale_vscode = {
            "State": {"Running": False},
            "Config": {"Labels": _vscode_labels(workspace, issuance, spec_digest="old-spec")},
            "Mounts": [],
        }
        exists = iter([True, False, False, True])
        with (
            patch.object(
                sr,
                "_strict_all_interactive_states",
                return_value=[("stale-vscode", json.dumps([stale_vscode]))],
            ),
            patch.object(sr.idk, "container_exists", side_effect=lambda _n: next(exists)),
            patch.object(sr.idk, "container_running", return_value=False),
            patch.object(
                sr,
                "verify_refreshed_session",
                side_effect=sr.SessionError("payload mismatch"),
            ),
            pytest.raises(sr.SessionError, match="payload mismatch"),
        ):
            sr.up(
                workspace,
                rebuild=True,
                expected_image_id="sha256:fresh",
            )

        argvs = [_argv_of(c) for c in run.call_args_list]
        assert ["docker", "rm", "stale-vscode"] not in argvs
        assert ["docker", "rename", name, backup] in argvs
        assert ["docker", "rm", "-f", name] in argvs
        assert ["docker", "rename", backup, name] in argvs
        assert ["docker", "rm", "-f", backup] not in argvs
        assert not any(argv[:2] == ["docker", "volume"] for argv in argvs)

    def test_failed_probe_without_prior_runtime_removes_candidate(self, wired):
        workspace, run = wired
        name = sr.session_container_name(workspace)
        exists = iter([False, False, True])
        with (
            patch.object(sr.idk, "container_exists", side_effect=lambda _n: next(exists)),
            patch.object(
                sr,
                "verify_refreshed_session",
                side_effect=sr.SessionError("payload mismatch"),
            ),
            pytest.raises(sr.SessionError, match="payload mismatch"),
        ):
            sr.up(
                workspace,
                rebuild=True,
                expected_image_id="sha256:fresh",
            )

        assert ["docker", "rm", "-f", name] in [_argv_of(call) for call in run.call_args_list]

    def test_fresh_refresh_rejects_unverified_labels_or_networks(self, wired):
        workspace, run = wired
        name = sr.session_container_name(workspace)
        exists = iter([False, False, True])
        with (
            patch.object(sr.idk, "container_exists", side_effect=lambda _n: next(exists)),
            patch.object(sr, "container_matches_issuance", return_value=False),
            patch.object(sr, "verify_refreshed_session") as verify,
            pytest.raises(sr.SessionError, match="labels or network topology"),
        ):
            sr.up(workspace, rebuild=True, expected_image_id="sha256:fresh")

        verify.assert_not_called()
        assert ["docker", "rm", "-f", name] in [_argv_of(call) for call in run.call_args_list]

    def test_old_container_cleanup_failure_keeps_verified_replacement(self, wired, caplog):
        workspace, run = wired
        name = sr.session_container_name(workspace)
        backup = f"{name}-pre-refresh"
        exists = iter([True, False, False])

        def response(argv, **_kwargs):
            if argv == ["docker", "rm", "-f", backup]:
                return subprocess.CompletedProcess(argv, 1, "", "busy")
            return subprocess.CompletedProcess(argv, 0, "", "")

        run.side_effect = response
        with (
            patch.object(sr.idk, "container_exists", side_effect=lambda _n: next(exists)),
            patch.object(sr.idk, "container_running", return_value=False),
            patch.object(sr, "verify_refreshed_session"),
        ):
            assert sr.up(workspace, rebuild=True, expected_image_id="sha256:fresh") == name

        assert "replacement succeeded" in caplog.text

    def test_refresh_to_unproxied_spec_needs_no_egress_network(self, wired):
        workspace, _run = wired
        _write_spec(workspace, _spec(egress="unproxied"))
        name = sr.session_container_name(workspace)
        exists = iter([False, False, False])
        with (
            patch.object(sr.idk, "network_exists", return_value=False),
            patch.object(sr.idk, "ensure_unproxied_network", return_value=False) as ensure,
            patch.object(sr.idk, "container_exists", side_effect=lambda _n: next(exists)),
            patch.object(sr.idk, "container_running", return_value=False),
            patch.object(sr, "verify_refreshed_session"),
        ):
            assert sr.up(workspace, rebuild=True, expected_image_id="sha256:fresh") == name
        ensure.assert_called_once_with()

    def test_licensed_refresh_fails_before_parking_existing_runtime(self, wired):
        workspace, run = wired
        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr, "_requested_issued_license", return_value=object()),
            pytest.raises(
                sr.SessionError,
                match=re.escape(
                    "`booley session up --rebuild` cannot preserve a licensed relay topology; "
                    "run `booley session refresh` instead"
                ),
            ),
        ):
            sr.up(workspace, rebuild=True)

        assert not any(_argv_of(call)[:2] == ["docker", "rename"] for call in run.call_args_list)

    def test_docker_run_failure_raises(self, wired):
        workspace, run = wired
        run.return_value = subprocess.CompletedProcess([], 1, "", "no such network")
        with (
            patch.object(sr.idk, "container_exists", return_value=False),
            patch.object(sr.idk, "container_running", return_value=False),
            pytest.raises(sr.SessionError, match="no such network"),
        ):
            sr.up(workspace)

    def test_failing_hook_warns_but_leaves_container_up(self, wired, caplog):
        workspace, run = wired
        ours = sr.session_container_name(workspace)

        def fake(argv, **kw):
            rc = 1 if argv[:2] == ["docker", "exec"] else 0
            return subprocess.CompletedProcess([], rc, "", "registrar boom")

        run.side_effect = fake
        with (
            patch.object(sr.idk, "container_exists", return_value=False),
            patch.object(sr.idk, "container_running", return_value=False),
        ):
            assert sr.up(workspace) == ours
        assert "postCreateCommand failed" in caplog.text


class TestMountIssuance:
    def _workspace_mount(self, workspace: Path) -> dict:
        return {
            "Destination": "/work",
            "Source": str(workspace),
            "Type": "bind",
            "RW": True,
        }

    def _spec(self, workspace: Path) -> dict:
        return {
            "workspaceMount": f"source={workspace},target=/work,type=bind",
            "mounts": [],
        }

    def test_matching_devcontainer_accepts_vscode_managed_mounts(self, workspace: Path):
        mounts = [
            self._workspace_mount(workspace),
            {"Destination": "/vscode", "Name": "vscode", "Type": "volume", "RW": True},
            {
                "Destination": "/tmp/vscode-wayland-1234-abcd.sock",
                "Source": "/run/user/1000/wayland-0",
                "Type": "bind",
                "RW": True,
            },
        ]

        assert sr._mounts_match_spec(
            mounts,
            self._spec(workspace),
            workspace,
            allow_vscode_mounts=True,
        )

    @pytest.mark.parametrize(
        "extra",
        [
            {"Destination": "/vscode", "Name": "vscode", "Type": "volume", "RW": True},
            {
                "Destination": "/host-secret",
                "Source": "/etc",
                "Type": "bind",
                "RW": False,
            },
        ],
    )
    def test_unissued_extra_mounts_remain_rejected(self, workspace: Path, extra: dict):
        mounts = [self._workspace_mount(workspace), extra]

        assert not sr._mounts_match_spec(mounts, self._spec(workspace), workspace)

    def test_arbitrary_extra_mount_is_rejected_for_devcontainer(self, workspace: Path):
        extra = {
            "Destination": "/host-secret",
            "Source": "/etc",
            "Type": "bind",
            "RW": False,
        }

        assert not sr._mounts_match_spec(
            [self._workspace_mount(workspace), extra],
            self._spec(workspace),
            workspace,
            allow_vscode_mounts=True,
        )

    @pytest.mark.parametrize("spec_source", ["/c/Users/dev/project", "/C/Users/dev/project"])
    def test_wsl2_mount_path_matches_spec(self, monkeypatch: pytest.MonkeyPatch, spec_source: str):
        from booley.runtime import platform_paths

        monkeypatch.setattr(platform_paths, "IS_WINDOWS", True)
        spec = {
            "workspaceMount": f"source={spec_source},target=/work,type=bind",
            "mounts": [],
        }
        mounts = [
            {
                "Destination": "/work",
                "Source": "/run/desktop/mnt/host/c/Users/dev/project",
                "Type": "bind",
                "RW": True,
            },
        ]
        workspace = Path("/c/Users/dev/project")

        assert sr._mounts_match_spec(mounts, spec, workspace)


class TestProjectDataMountPinned:
    @staticmethod
    def _mount_inspect_json(project_source: str, workspace_source: str) -> str:
        state = [
            {
                "Mounts": [
                    {
                        "Destination": "/booley-project",
                        "Source": project_source,
                        "Type": "bind",
                        "RW": True,
                    },
                    {
                        "Destination": "/work/.booley",
                        "Source": workspace_source,
                        "Type": "bind",
                        "RW": True,
                    },
                ],
            }
        ]
        return json.dumps(state)

    @staticmethod
    def _windows_workspace() -> tuple[SimpleNamespace, Path]:
        workspace = SimpleNamespace(resolve=lambda: Path("C:/Users/dev/project"))
        return workspace, workspace.resolve() / ".booley"

    def test_wsl2_prefixed_sources_are_recognized(self, monkeypatch: pytest.MonkeyPatch):
        from booley.runtime import platform_paths

        monkeypatch.setattr(platform_paths, "IS_WINDOWS", True)
        workspace, pending = self._windows_workspace()
        raw = self._mount_inspect_json(
            "/run/desktop/mnt/host/c/Users/dev/project/.booley",
            "/run/desktop/mnt/host/c/Users/dev/project/.booley",
        )

        assert sr._project_data_mount_root_is_pinned(
            raw, workspace, pending, layout="legacy", image_id=""
        )

    def test_native_sources_still_match(self, monkeypatch: pytest.MonkeyPatch):
        from booley.runtime import platform_paths

        monkeypatch.setattr(platform_paths, "IS_WINDOWS", True)
        workspace, pending = self._windows_workspace()
        raw = self._mount_inspect_json(
            "/c/Users/dev/project/.booley",
            "/c/Users/dev/project/.booley",
        )

        assert sr._project_data_mount_root_is_pinned(
            raw, workspace, pending, layout="legacy", image_id=""
        )

    def test_mismatched_sources_are_rejected(self, monkeypatch: pytest.MonkeyPatch):
        from booley.runtime import platform_paths

        monkeypatch.setattr(platform_paths, "IS_WINDOWS", True)
        workspace, pending = self._windows_workspace()
        raw = self._mount_inspect_json(
            "/run/desktop/mnt/host/c/Users/dev/project/.booley",
            "/run/desktop/mnt/host/c/Users/dev/other/.booley",
        )

        assert not sr._project_data_mount_root_is_pinned(
            raw, workspace, pending, layout="legacy", image_id=""
        )

    def test_nonwritable_workspace_view_is_rejected(self, monkeypatch: pytest.MonkeyPatch):
        from booley.runtime import platform_paths

        monkeypatch.setattr(platform_paths, "IS_WINDOWS", True)
        workspace, pending = self._windows_workspace()
        raw = json.loads(
            self._mount_inspect_json(
                "/c/Users/dev/project/.booley",
                "/c/Users/dev/project/.booley",
            )
        )
        raw[0]["Mounts"][1]["RW"] = False

        assert not sr._project_data_mount_root_is_pinned(
            json.dumps(raw), workspace, pending, layout="legacy", image_id=""
        )

    def test_malformed_inspect_json_is_rejected(self):
        workspace, pending = self._windows_workspace()

        assert not sr._project_data_mount_root_is_pinned(
            "not-json", workspace, pending, layout="legacy", image_id=""
        )
        assert sr._decode_container_inspect("not-json") is None


class TestCanonicalBindSource:
    def test_non_string_observed_source_is_rejected(self, workspace: Path):
        spec = {
            "workspaceMount": f"source={workspace},target=/work,type=bind",
            "mounts": [],
        }
        mounts = [{"Destination": "/work", "Source": None, "Type": "bind", "RW": True}]

        assert not sr._mounts_match_spec(mounts, spec, workspace)

    def test_volume_source_is_compared_by_name(self, workspace: Path):
        spec = {
            "workspaceMount": "source=vscode,target=/work,type=volume",
            "mounts": [],
        }
        mounts = [{"Destination": "/work", "Name": "vscode", "Type": "volume", "RW": True}]

        assert sr._mounts_match_spec(mounts, spec, workspace)


class TestLicensedRelayLifecycle:
    @staticmethod
    def _profile() -> SimpleNamespace:
        return SimpleNamespace(
            server_ipv4="10.20.30.40",
            server_hostid="license-server-01",
            lmgrd_port=2100,
            vendor_port=2101,
        )

    def test_provisions_healthy_relay_before_session_and_attaches_after_create(self, wired):
        workspace, run = wired
        events: list[str] = []
        relay = SimpleNamespace(relay_container="relay")

        def provision(*_args):
            events.append("provision")
            return relay

        def execute(argv, **_kwargs):
            events.append("session-create" if argv[:2] == ["docker", "run"] else "hook")
            return subprocess.CompletedProcess([], 0, "", "")

        with (
            patch(
                "booley.runtime.session_issuance.requested_license",
                return_value=self._profile(),
            ),
            patch.object(sr.idk, "container_exists", return_value=False),
            patch.object(sr.idk, "container_running", return_value=False),
            patch.object(sr, "_provision_license_relay", side_effect=provision),
            patch.object(
                sr,
                "_connect_and_validate_license_relay",
                side_effect=lambda *_args: events.append("connect-validate"),
            ),
        ):
            run.side_effect = execute
            sr.up(workspace)

        assert events[:3] == ["provision", "session-create", "connect-validate"]

    def test_relay_start_failure_prevents_session_create(self, wired):
        workspace, run = wired
        with (
            patch(
                "booley.runtime.session_issuance.requested_license",
                return_value=self._profile(),
            ),
            patch.object(sr.idk, "container_exists", return_value=False),
            patch.object(sr, "_provision_license_relay", side_effect=sr.SessionError("unhealthy")),
            pytest.raises(sr.SessionError, match="unhealthy"),
        ):
            sr.up(workspace)
        assert not any(_argv_of(call)[:2] == ["docker", "run"] for call in run.call_args_list)

    def test_fresh_create_replaces_deterministic_orphan_topology(self, workspace):
        profile = self._profile()
        expected = SimpleNamespace(relay_container="replacement")
        with patch(
            "booley.eda.provisioning.licensing.flexnet_docker.recreate_relay",
            return_value=expected,
        ) as recreate:
            assert (
                sr._provision_license_relay(
                    workspace,
                    profile,
                    ("label=value",),
                    "sha256:" + "a" * 64,
                )
                is expected
            )
        recreate.assert_called_once()
        assert recreate.call_args.kwargs["issuance_labels"] == ("label=value",)
        assert recreate.call_args.kwargs["image"] == "sha256:" + "a" * 64

    def test_session_create_failure_rolls_back_relay(self, wired):
        workspace, run = wired
        relay = SimpleNamespace(relay_container="relay")
        run.return_value = subprocess.CompletedProcess([], 1, "", "create failed")
        with (
            patch(
                "booley.runtime.session_issuance.requested_license",
                return_value=self._profile(),
            ),
            patch.object(sr.idk, "container_exists", return_value=False),
            patch.object(sr, "_provision_license_relay", return_value=relay),
            patch.object(sr, "_remove_license_relay") as remove,
            pytest.raises(sr.SessionError, match="create failed"),
        ):
            sr.up(workspace)
        remove.assert_called_once_with(relay)

    def test_private_connect_failure_removes_session_and_relay(self, wired):
        workspace, run = wired
        ours = sr.session_container_name(workspace)
        relay = SimpleNamespace(relay_container="relay")
        with (
            patch(
                "booley.runtime.session_issuance.requested_license",
                return_value=self._profile(),
            ),
            patch.object(sr.idk, "container_exists", return_value=False),
            patch.object(sr, "_provision_license_relay", return_value=relay),
            patch.object(
                sr,
                "_connect_and_validate_license_relay",
                side_effect=sr.SessionError("connect failed"),
            ),
            patch.object(sr, "_remove_license_relay") as remove,
            pytest.raises(sr.SessionError, match="connect failed"),
        ):
            sr.up(workspace)
        assert ["docker", "rm", "-f", ours] in [_argv_of(call) for call in run.call_args_list]
        remove.assert_called_once_with(relay)

    def test_resume_validates_exact_relay_before_start(self, wired):
        workspace, run = wired
        ours = sr.session_container_name(workspace)
        relay = SimpleNamespace(relay_container="relay")
        with (
            patch(
                "booley.runtime.session_issuance.requested_license",
                return_value=self._profile(),
            ),
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr.idk, "container_running", return_value=False),
            patch.object(sr, "_relay_resources", return_value=relay),
            patch.object(sr, "_validate_license_relay") as validate,
        ):
            sr.up(workspace)
        validate.assert_called_once()
        assert ["docker", "start", ours] in [_argv_of(call) for call in run.call_args_list]

    def test_vscode_prepare_provisions_relay_before_create(self, wired):
        workspace, _run = wired
        relay = SimpleNamespace(relay_container="relay")
        with (
            patch(
                "booley.runtime.session_issuance.requested_license",
                return_value=self._profile(),
            ),
            patch.object(sr, "_relay_objects_exist", return_value=False),
            patch.object(sr, "_provision_license_relay", return_value=relay) as provision,
        ):
            assert sr.prepare(workspace) == "abc"
        provision.assert_called_once()

    def test_vscode_prepare_validates_existing_relay_without_a_session(self, wired):
        workspace, _run = wired
        relay = SimpleNamespace(relay_container="relay")
        with (
            patch(
                "booley.runtime.session_issuance.requested_license",
                return_value=self._profile(),
            ),
            patch.object(sr, "_relay_resources", return_value=relay),
            patch.object(sr, "_relay_objects_exist", return_value=True),
            patch("booley.eda.provisioning.licensing.flexnet_docker.validate_relay") as validate,
            patch.object(sr, "_provision_license_relay") as provision,
        ):
            assert sr.prepare(workspace) == "abc"
        assert validate.call_args.args[1] is None
        provision.assert_not_called()

    def test_default_down_removes_relay_even_if_session_is_absent(self, workspace):
        relay = SimpleNamespace(relay_container="relay")
        with (
            patch.object(sr, "_relay_resources", return_value=relay),
            patch.object(sr.idk, "container_exists", side_effect=[False, True]),
            patch.object(sr, "_remove_license_relay") as remove,
        ):
            assert sr.down(workspace).relay is True
        remove.assert_called_once_with(relay)


class TestPreflight:
    def test_missing_network_names_booley_init(self, workspace: Path):
        _write_spec(workspace, _spec())
        with (
            patch(
                "booley.runtime.session_issuance.validate",
                return_value=SimpleNamespace(license_profile=None),
            ),
            patch.object(sr.idk, "network_exists", return_value=False),
            pytest.raises(sr.SessionError, match="booley init"),
        ):
            sr.up(workspace)

    def test_missing_image_names_booley_init(self, workspace: Path):
        _write_spec(workspace, _spec())
        with (
            patch(
                "booley.runtime.session_issuance.validate",
                return_value=SimpleNamespace(license_profile=None),
            ),
            patch.object(sr.idk, "network_exists", return_value=True),
            patch.object(sr.idk, "image_exists", return_value=False),
            pytest.raises(sr.SessionError, match="not built"),
        ):
            sr.up(workspace)

    def test_missing_license_relay_image_fails_before_session_create(self, wired):
        workspace, run = wired
        profile = SimpleNamespace(
            server_ipv4="10.20.30.40",
            server_hostid="license-server-01",
            lmgrd_port=2100,
            vendor_port=2101,
        )
        with (
            patch("booley.runtime.session_issuance.requested_license", return_value=profile),
            patch.object(
                sr.idk, "image_exists", side_effect=lambda image: image != "booley-flexnet-relay:1"
            ),
            pytest.raises(sr.SessionError, match=r"license relay image.*booley init"),
        ):
            sr.up(workspace)
        assert not any(_argv_of(call)[:2] == ["docker", "run"] for call in run.call_args_list)

    def test_missing_spec_names_booley_init(self, workspace: Path):
        with pytest.raises(sr.SessionError, match="run `booley init`"):
            sr.up(workspace)

    def test_unproxied_spec_ensures_routed_network_without_egress_network(self, wired):
        workspace, _run = wired
        _write_spec(workspace, _spec(egress="unproxied"))
        with (
            patch.object(sr.idk, "network_exists", return_value=False),
            patch.object(sr.idk, "ensure_unproxied_network", return_value=True) as ensure,
            patch.object(sr.idk, "container_exists", return_value=False),
            patch.object(sr.idk, "container_running", return_value=False),
        ):
            assert sr.up(workspace) == sr.session_container_name(workspace)
        ensure.assert_called_once_with()


class TestImageDriftWarning:
    """F-6: [sandbox].image changed after init leaves the untracked spec frozen
    on the old image, and `session up` would silently start it. doctor already
    WARNs on the drift; `up` must surface it inline (warn only, never rewrite
    the spec)."""

    def _set_toml_image(self, workspace: Path, image: str) -> None:
        pdir = workspace / ".booley"
        pdir.mkdir()
        (pdir / "booley.toml").write_text(f'[sandbox]\nimage = "{image}"\n', encoding="utf-8")

    def _up_running(self, workspace: Path) -> None:
        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr.idk, "container_running", return_value=True),
        ):
            sr.up(workspace)

    def test_image_comparison_uses_shared_dispatch(self, workspace, monkeypatch):
        assert callable(getattr(sr.idk, "compare_issued_image", None)), (
            "shared image dispatch is missing"
        )
        calls = []
        monkeypatch.setattr(sr.project_image, "project_sandbox_image", lambda root: "configured")

        def compare(*args, **kwargs):
            calls.append((args, kwargs))
            return sr.image_identity.Comparison(sr.image_identity.Status.MATCH)

        monkeypatch.setattr(sr.idk, "compare_issued_image", compare)
        sr._warn_on_image_drift({"image": "issued-tag"}, workspace)
        assert calls == [(("issued-tag", "configured"), {"project_root": workspace})]

    def test_equivalent_image_emits_one_debug_note(self, workspace, caplog, monkeypatch):
        self.test_same_source_tag_move_is_silent(workspace, caplog, monkeypatch)
        notes = [
            record
            for record in caplog.records
            if "moved to an equivalent Sandbox Image" in record.message
        ]
        assert len(notes) == 1
        assert notes[0].levelname == "DEBUG"
        assert not any(record.levelname == "WARNING" for record in caplog.records)

    def test_spec_vs_toml_drift_warns_and_names_the_fix(self, wired, caplog, monkeypatch):
        from booley.runtime.booley_dir import reset_cache

        workspace, _run = wired
        # The spec on disk says the default 'booley-sandbox'; the toml has
        # since been pointed at a project image.
        self._set_toml_image(workspace, "i2c-booley-sandbox")
        monkeypatch.setenv("BOOLEY_DIR", str(workspace / ".booley"))
        reset_cache()

        self._up_running(workspace)

        assert "!= [sandbox].image 'i2c-booley-sandbox'" in caplog.text
        assert "booley init --seed" in caplog.text
        assert "booley session down" in caplog.text

    def test_matching_image_is_silent(self, wired, caplog):
        workspace, _run = wired
        self._set_toml_image(workspace, "booley-sandbox")
        _write_spec(workspace, _spec(image=sr.project_image.project_image_name(workspace)))

        self._up_running(workspace)

        assert "[sandbox].image" not in caplog.text

    def test_pinned_digest_matching_configured_tag_is_silent(
        self, workspace: Path, caplog, monkeypatch
    ):

        digest = "sha256:" + "a" * 64
        monkeypatch.setattr(
            sr.project_image,
            "project_sandbox_image",
            lambda _root: sr.project_image.project_image_name(workspace),
        )
        monkeypatch.setattr(
            sr.idk,
            "image_id",
            lambda image: (
                digest
                if image in {digest, sr.project_image.project_image_name(workspace)}
                else None
            ),
        )
        monkeypatch.setattr(sr.idk, "inspect_image_metadata", lambda *_a, **_kw: None)

        sr._warn_on_image_drift({"image": digest}, workspace)

        assert "[sandbox].image" not in caplog.text

    def test_same_source_tag_move_is_silent(self, workspace: Path, caplog, monkeypatch):

        digest = "sha256:" + "a" * 64
        monkeypatch.setattr(
            sr.project_image,
            "project_sandbox_image",
            lambda _root: sr.project_image.project_image_name(workspace),
        )
        fingerprint = "f" * 64

        def inspect(reference: str, *, executable: str = "docker"):
            del executable
            return sr.image_identity.ImageMetadata(
                reference,
                digest if reference == digest else "sha256:" + "b" * 64,
                {
                    "io.booley.provenance.schema": "3",
                    "io.booley.artifact.role": "wheel-overlay",
                    "io.booley.build.recipe-fingerprint": "wheel-recipe",
                    "io.booley.sandbox.selection-fingerprint": fingerprint,
                },
                {},
                (),
            )

        monkeypatch.setattr(sr.idk, "inspect_image_metadata", inspect)

        with caplog.at_level("DEBUG"):
            sr._warn_on_image_drift({"image": digest}, workspace)

        assert not any(record.levelname == "WARNING" for record in caplog.records)
        assert "moved to an equivalent Sandbox Image" in caplog.text

    def test_logically_different_configured_image_warns(
        self, workspace: Path, caplog, monkeypatch
    ):
        digest = "sha256:" + "a" * 64
        monkeypatch.setattr(
            sr.project_image,
            "project_sandbox_image",
            lambda _root: sr.project_image.project_image_name(workspace),
        )
        monkeypatch.setattr(
            sr.idk,
            "compare_issued_selection",
            lambda *_args, **_kwargs: sr.image_identity.Comparison(
                sr.image_identity.Status.MISMATCH
            ),
        )

        sr._warn_on_image_drift({"image": digest}, workspace)

        assert (
            f"!= [sandbox].image '{sr.project_image.project_image_name(workspace)}'" in caplog.text
        )

    def test_rebuilt_external_image_warns_despite_inherited_labels(
        self, workspace: Path, caplog, monkeypatch
    ):
        digest = "sha256:" + "a" * 64
        monkeypatch.setattr(
            sr.project_image, "project_sandbox_image", lambda _root: "custom-sandbox"
        )
        monkeypatch.setattr(
            sr.idk,
            "compare_issued_reference",
            lambda *_args, **_kwargs: sr.image_identity.Comparison(
                sr.image_identity.Status.MISMATCH
            ),
        )

        sr._warn_on_image_drift({"image": digest}, workspace)

        assert "!= [sandbox].image 'custom-sandbox'" in caplog.text

    def test_no_project_config_is_silent(self, wired, caplog):
        # No .booley at all: the resolver falls back to the base image,
        # which is exactly what the generated spec carries — no drift.
        workspace, _run = wired
        _write_spec(workspace, _spec(image=sr.project_image.project_image_name(workspace)))

        self._up_running(workspace)

        assert "[sandbox].image" not in caplog.text

    def test_warning_does_not_block_the_session(self, wired, caplog):
        workspace, _run = wired
        ours = sr.session_container_name(workspace)
        self._set_toml_image(workspace, "i2c-booley-sandbox")

        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr.idk, "container_running", return_value=True),
        ):
            assert sr.up(workspace) == ours


class TestStaleBooleyBakeWarning:
    """The stale-wheel guard: `up` compares the image's build-fingerprint label
    against the current checkout and warns when the session would run stale
    Booley code. Advisory only — no verdict (pip install, unlabeled image) must
    stay silent."""

    def test_mismatch_warns_names_fields_and_fix(self, caplog, monkeypatch):
        expected = sr.image_identity.BooleyBuildIdentity("1.0", "abc123", None, "current")
        observed = sr.image_identity.ImageMetadata(
            "sha256:issued",
            "sha256:issued",
            {
                "org.opencontainers.image.version": "1.0",
                "org.opencontainers.image.revision": "def456",
                "io.booley.wheel.source-fingerprint": "stale",
            },
            {},
            (),
        )
        monkeypatch.setattr(sr.image_identity, "current_host_build_identity", lambda: expected)
        monkeypatch.setattr(sr.idk, "inspect_image_metadata", lambda *_args, **_kwargs: observed)

        sr._warn_on_stale_booley_bake({"image": "sha256:issued"})

        assert "stale Booley code" in caplog.text
        assert "revision abc123 -> def456" in caplog.text
        assert "wheel_source_fingerprint current -> stale" in caplog.text
        assert "booley session refresh" in caplog.text

    def test_unknown_identity_is_silent(self, caplog):
        result = sr.image_identity.Comparison(sr.image_identity.Status.UNKNOWN)
        with patch.object(sr.idk, "compare_issued_build", return_value=result):
            sr._warn_on_stale_booley_bake({"image": "sha256:issued"})
        assert "stale Booley code" not in caplog.text

    def test_match_is_silent(self, caplog):
        result = sr.image_identity.Comparison(sr.image_identity.Status.MATCH)
        with patch.object(sr.idk, "compare_issued_build", return_value=result):
            sr._warn_on_stale_booley_bake({"image": "sha256:issued"})
        assert "stale Booley code" not in caplog.text


class TestStaleSessionContainerWarning:
    """A rebuild moves the tag, not running containers: `up` must say when the
    live session was born from a superseded image (the previously dead
    `sessions_on_stale_image` probe, now wired in)."""

    def test_stale_container_warns_by_name(self, workspace, caplog):
        with patch.object(sr, "sessions_on_stale_image", return_value=["booley-session-i2c"]):
            sr._warn_on_stale_session_containers({"image": "booley-sandbox"}, workspace)
        assert "booley-session-i2c" in caplog.text
        assert "superseded by a rebuild" in caplog.text

    def test_current_containers_are_silent(self, workspace, caplog):
        with patch.object(sr, "sessions_on_stale_image", return_value=[]):
            sr._warn_on_stale_session_containers({"image": "booley-sandbox"}, workspace)
        assert "superseded" not in caplog.text

    def test_up_invokes_the_probe(self, wired, caplog):
        workspace, _run = wired
        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr.idk, "container_running", return_value=True),
            patch.object(
                sr, "sessions_on_stale_image", return_value=["booley-session-i2c"]
            ) as probe,
        ):
            sr.up(workspace)
        probe.assert_called_once()
        assert "superseded by a rebuild" in caplog.text


class TestDownAndStatus:
    def test_down_on_absent_container_is_false(self, workspace: Path):
        with (
            patch.object(sr.idk, "container_exists", return_value=False),
            patch.object(sr.idk, "network_exists", return_value=False),
        ):
            assert not sr.down(workspace)

    def test_down_never_removes_the_issuance_image_keeper(self, workspace: Path):
        relay = SimpleNamespace(relay_container="relay")
        with (
            patch.object(sr, "_relay_resources", return_value=relay),
            patch.object(sr, "_relay_objects_exist", return_value=False),
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr, "_run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0)
            assert sr.down(workspace).headless is True
        commands = [_argv_of(call) for call in run.call_args_list]
        assert ["docker", "stop", sr.session_container_name(workspace)] in commands
        assert ["docker", "rm", "-f", sr.session_container_name(workspace)] in commands
        assert not any(command[:3] == ["docker", "image", "rm"] for command in commands)

    def test_down_rejects_unproven_vscode_identity_before_mutation(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime import session_admission

        run = Mock()
        monkeypatch.setattr(sr, "_run", run)
        monkeypatch.setattr(sr.idk, "container_exists", lambda _name: True)
        monkeypatch.setattr(sr, "_relay_objects_exist", lambda _relay: False)
        monkeypatch.setattr(
            session_admission,
            "running_project_sandboxes",
            Mock(side_effect=session_admission.AdmissionError("identity disagrees")),
        )

        with pytest.raises(sr.SessionError, match="identity disagrees"):
            sr.down(workspace)

        run.assert_not_called()

    def test_down_stops_revalidated_vscode_and_clears_claim(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime import session_admission

        item = SimpleNamespace(container_id="editor-id", name="editor", running=True, vscode=True)
        discovered = Mock(side_effect=[(item,), (item,)])
        monkeypatch.setattr(session_admission, "running_project_sandboxes", discovered)
        monkeypatch.setattr(session_admission, "clear_vscode_claim", lambda _root: True)
        monkeypatch.setattr(sr.idk, "container_exists", lambda _name: False)
        monkeypatch.setattr(sr, "_relay_objects_exist", lambda _relay: False)
        run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        monkeypatch.setattr(sr, "_run", run)

        with pytest.raises(sr.SessionError, match="VS Code owns"):
            sr.down(workspace)
        run.assert_not_called()

    def test_down_rejects_vscode_that_disappears_before_stop(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime import session_admission

        item = SimpleNamespace(container_id="editor-id", name="editor", running=True, vscode=True)
        monkeypatch.setattr(
            session_admission, "running_project_sandboxes", Mock(side_effect=[(item,), ()])
        )
        monkeypatch.setattr(sr.idk, "container_exists", lambda _name: False)
        monkeypatch.setattr(sr, "_relay_objects_exist", lambda _relay: False)

        with pytest.raises(sr.SessionError, match="VS Code owns"):
            sr.down(workspace)

    @pytest.mark.parametrize("running", [True, False], ids=["running", "stopped-container"])
    def test_down_runs_before_stop_then_stops(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, running: bool
    ) -> None:
        from booley.runtime import session_admission

        events: list[str] = []
        item = SimpleNamespace(container_id="sandbox-id", name="sb", running=True, vscode=False)
        found = (item,) if running else ()
        monkeypatch.setattr(
            session_admission, "running_project_sandboxes", lambda *_args, **_kwargs: found
        )
        monkeypatch.setattr(sr.idk, "container_exists", lambda _name: True)
        monkeypatch.setattr(sr, "_relay_objects_exist", lambda _relay: False)
        monkeypatch.setattr(
            sr,
            "_run",
            lambda argv, **_kwargs: (
                events.append(" ".join(argv[:2])) or subprocess.CompletedProcess(argv, 0, "", "")
            ),
        )

        result = sr.down(workspace, before_stop=lambda sandbox: events.append(f"hook {sandbox}"))

        assert result.headless is True
        assert events == [f"hook {item if running else None}", "docker stop", "docker rm"]

    @pytest.mark.parametrize("case", ["no-container", "vscode-owned"])
    def test_down_skips_before_stop_when_it_will_not_stop(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, case: str
    ) -> None:
        from booley.runtime import session_admission

        found = (
            (SimpleNamespace(container_id="id", name="ed", running=True, vscode=True),)
            if case == "vscode-owned"
            else ()
        )
        monkeypatch.setattr(
            session_admission, "running_project_sandboxes", lambda *_args, **_kwargs: found
        )
        monkeypatch.setattr(sr.idk, "container_exists", lambda _name: False)
        monkeypatch.setattr(sr, "_relay_objects_exist", lambda _relay: False)
        hook = Mock()

        if case == "vscode-owned":
            with pytest.raises(sr.SessionError, match="VS Code owns"):
                sr.down(workspace, before_stop=hook)
        else:
            assert not sr.down(workspace, before_stop=hook)

        hook.assert_not_called()

    @pytest.mark.parametrize("failure", ["stop", "rm"])
    def test_down_reports_headless_docker_failure(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, failure: str
    ) -> None:
        monkeypatch.setattr(sr.idk, "container_exists", lambda _name: True)
        monkeypatch.setattr(sr, "_relay_objects_exist", lambda _relay: False)

        def run(argv: list[str], **_kwargs):
            failed = argv[1] == failure
            return subprocess.CompletedProcess(
                argv, 1 if failed else 0, "", "denied" if failed else ""
            )

        monkeypatch.setattr(sr, "_run", run)

        message = "cannot remove" if failure == "rm" else "cannot stop"
        with pytest.raises(sr.SessionError, match=message):
            sr.down(workspace)

    def test_down_reports_vscode_stop_failure(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime import session_admission

        item = SimpleNamespace(container_id="editor-id", name="editor", running=True, vscode=True)
        monkeypatch.setattr(
            session_admission, "running_project_sandboxes", lambda *_args, **_kwargs: (item,)
        )
        monkeypatch.setattr(sr.idk, "container_exists", lambda _name: False)
        monkeypatch.setattr(sr, "_relay_objects_exist", lambda _relay: False)
        monkeypatch.setattr(
            sr,
            "_run",
            lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, "", "denied"),
        )

        with pytest.raises(sr.SessionError, match="VS Code owns"):
            sr.down(workspace)

    def test_status_reports_pending_editor_start(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime import session_admission

        monkeypatch.setattr(session_admission, "has_pending_claim", lambda _root: True)

        assert sr.status(workspace) == "start-pending"

    def test_status_reports_claim_inspection_failure(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.runtime import session_admission

        def fail(_root: Path) -> bool:
            raise session_admission.AdmissionError("claim unavailable")

        monkeypatch.setattr(session_admission, "has_pending_claim", fail)

        with pytest.raises(sr.SessionError, match="claim unavailable"):
            sr.status(workspace)

    def test_status_reports_three_states(self, workspace: Path):
        with patch.object(sr.idk, "container_exists", return_value=False):
            assert sr.status(workspace) == "absent"
        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr.idk, "container_running", return_value=False),
        ):
            assert sr.status(workspace) == "stopped"
        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr.idk, "container_running", return_value=True),
        ):
            assert sr.status(workspace) == "running"


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

    def _patch_docker(
        self,
        monkeypatch,
        *,
        names: str,
        container_ids: dict[str, str],
        folders: dict[str, str] | None = None,
        image_id: str | None = NEW_ID,
    ):
        folders = folders or {}

        def fake_run(argv, **_kw):
            if argv[:3] == ["docker", "image", "inspect"]:
                if image_id is None:
                    return subprocess.CompletedProcess([], 1, "", "no such image")
                return subprocess.CompletedProcess([], 0, f"{image_id}\n", "")
            if argv[:2] == ["docker", "ps"]:
                return subprocess.CompletedProcess([], 0, names, "")
            if argv[:2] == ["docker", "inspect"]:
                # `docker inspect --format {{.Image}} <name>` (image ID) vs
                # `docker inspect <name> --format {{...local_folder}}` (label).
                if argv[2] == "--format":
                    return subprocess.CompletedProcess([], 0, container_ids.get(argv[4], ""), "")
                return subprocess.CompletedProcess([], 0, folders.get(argv[2], ""), "")
            return subprocess.CompletedProcess([], 1, "", "")

        monkeypatch.setattr(sr, "_run", fake_run)
        monkeypatch.setattr(sr.idk, "image_id", lambda _image: image_id)

    def test_our_session_on_the_old_image_is_reported(self, workspace: Path, monkeypatch):
        ours = sr.session_container_name(workspace)
        self._patch_docker(monkeypatch, names=f"{ours}\n", container_ids={ours: self.OLD_ID})
        assert sr.sessions_on_stale_image(workspace, self.IMAGE) == [ours]

    def test_vscode_container_for_this_folder_is_reported(self, workspace: Path, monkeypatch):
        self._patch_docker(
            monkeypatch,
            names="goofy_ellis\n",
            container_ids={"goofy_ellis": self.OLD_ID},
            folders={"goofy_ellis": str(workspace)},
        )
        assert sr.sessions_on_stale_image(workspace, self.IMAGE) == ["goofy_ellis"]

    @pytest.mark.skipif(sys.platform != "linux", reason="needs a case-sensitive filesystem")
    def test_vscode_container_for_case_variant_folder_is_ignored(
        self, workspace: Path, monkeypatch
    ):
        # `<tmp>/I2C` differs from the workspace `<tmp>/i2c` only in case: on Linux
        # that is another directory, so its editor container is not this workspace's.
        self._patch_docker(
            monkeypatch,
            names="goofy_ellis\n",
            container_ids={"goofy_ellis": self.OLD_ID},
            folders={"goofy_ellis": str(workspace.parent / "I2C")},
        )
        assert sr.sessions_on_stale_image(workspace, self.IMAGE) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_vscode_container_for_symlinked_folder_is_reported(self, workspace: Path, monkeypatch):
        # A label naming the workspace through a symlink is the same directory.
        alias = workspace.parent / "alias"
        alias.symlink_to(workspace, target_is_directory=True)
        self._patch_docker(
            monkeypatch,
            names="goofy_ellis\n",
            container_ids={"goofy_ellis": self.OLD_ID},
            folders={"goofy_ellis": str(alias)},
        )
        assert sr.sessions_on_stale_image(workspace, self.IMAGE) == ["goofy_ellis"]

    def test_same_image_id_is_silent(self, workspace: Path, monkeypatch):
        # The rebuild was a no-op (all layers cached): the tag still resolves to
        # the very image the container runs, so there is nothing to restart for.
        ours = sr.session_container_name(workspace)
        self._patch_docker(monkeypatch, names=f"{ours}\n", container_ids={ours: self.NEW_ID})
        assert sr.sessions_on_stale_image(workspace, self.IMAGE) == []

    def test_no_running_container_is_silent(self, workspace: Path, monkeypatch):
        self._patch_docker(monkeypatch, names="", container_ids={})
        assert sr.sessions_on_stale_image(workspace, self.IMAGE) == []

    def test_container_for_another_workspace_is_ignored(self, workspace: Path, monkeypatch):
        self._patch_docker(
            monkeypatch,
            names="other\n",
            container_ids={"other": self.OLD_ID},
            folders={"other": str(workspace.parent / "elsewhere")},
        )
        assert sr.sessions_on_stale_image(workspace, self.IMAGE) == []

    def test_unbuilt_image_is_silent(self, workspace: Path, monkeypatch):
        ours = sr.session_container_name(workspace)
        self._patch_docker(
            monkeypatch, names=f"{ours}\n", container_ids={ours: self.OLD_ID}, image_id=None
        )
        assert sr.sessions_on_stale_image(workspace, self.IMAGE) == []

    def test_docker_failure_warns_and_reports_no_stale_sessions(
        self, workspace: Path, monkeypatch, caplog
    ):
        # Every Docker seam (sr._run, the image-ID helper's _run_docker) sees a
        # missing Docker: the probe stays advisory, but says why it is blind.
        def no_docker(*_a, **_k):
            raise FileNotFoundError("docker")

        monkeypatch.setattr(sr, "_run", no_docker)
        monkeypatch.setattr(sr.idk, "_run_docker", no_docker)
        with caplog.at_level("WARNING", logger=sr.logger.name):
            assert sr.sessions_on_stale_image(workspace, self.IMAGE) == []
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert self.IMAGE in warnings[0]
        assert "cannot check running Sandboxes" in warnings[0]
        assert "docker" in warnings[0]

    def test_failed_container_inspect_is_silent(self, workspace: Path, monkeypatch):
        # Container vanished between `docker ps` and the inspect: no ID to
        # compare, so say nothing rather than guess.
        ours = sr.session_container_name(workspace)
        self._patch_docker(monkeypatch, names=f"{ours}\n", container_ids={})
        assert sr.sessions_on_stale_image(workspace, self.IMAGE) == []


class TestMangledArgWarning:
    """B1 (host half): `booley` is a native Windows exe, so Git Bash/MSYS
    rewrites POSIX argv on the way in — `-- ... --report-dir /tmp/rep` arrives
    already mangled to `C:/Users/.../Temp/rep`. We forward the command to
    `docker exec` verbatim (we cannot know which arguments are paths), but that
    string denotes nothing in the Linux container, so say so."""

    def test_windows_path_argument_warns(self, caplog):
        cmd = [
            "python3",
            "-m",
            "booley.flows.lint",
            "--report-dir",
            "C:/Users/andre/AppData/Local/Temp/rep-lint",
        ]
        sr._warn_on_mangled_args(cmd)
        assert "Windows host paths" in caplog.text
        assert "MSYS_NO_PATHCONV" in caplog.text

    def test_equals_form_is_caught_too(self, caplog):
        sr._warn_on_mangled_args([r"--report-dir=C:\Temp\rep"])
        assert "Windows host paths" in caplog.text

    def test_ordinary_container_command_is_silent(self, caplog):
        sr._warn_on_mangled_args(
            [
                "python3",
                "-m",
                "booley.flows.lint",
                "--target",
                "lint",
                "--report-dir",
                "/tmp/rep-lint",
            ],
        )
        assert caplog.text == ""

    def test_enter_still_forwards_the_command_verbatim(self, workspace: Path):
        # The warning is advisory: we must not rewrite the user's argv (only the
        # caller knows which arguments are paths). Exec gets exactly what it got.
        cmd = ["python3", "-c", "print(1)", "--out", "C:/Temp/x"]
        with patch.object(sr, "run_project_command", return_value=0) as run:
            assert sr.enter(workspace, cmd, tty=False) == 0
        run.assert_called_once_with(workspace, cmd, tty=False, cwd=None)


class _ParallelEnterProbe:
    def __init__(self, command_env: dict[str, str]) -> None:
        self.command_env = command_env
        self.first_selecting = threading.Event()
        self.release_first = threading.Event()
        self.second_waiting = threading.Event()
        self.execution_barrier = threading.Barrier(2)
        self.select_guard = threading.Lock()
        self.select_calls = 0

    def select_runtime(self, _workspace: Path) -> tuple[str, dict[str, str]]:
        with self.select_guard:
            self.select_calls += 1
            call = self.select_calls
        if call == 1:
            self.first_selecting.set()
            assert self.release_first.wait(timeout=2)
        return "runtime", self.command_env

    def run_command(self, *_args, **_kwargs) -> SimpleNamespace:
        self.execution_barrier.wait(timeout=2)
        return SimpleNamespace(exit_code=0)


class TestRunProjectCommand:
    COMMAND_ENV: ClassVar[dict[str, str]] = {
        "HTTP_PROXY": dc.PROXY_URL,
        "HTTPS_PROXY": dc.PROXY_URL,
        "http_proxy": dc.PROXY_URL,
        "https_proxy": dc.PROXY_URL,
        "NO_PROXY": "localhost,127.0.0.1",
        "BOOLEY_MCP_MODE": "interactive",
        "BOOLEY_DIR": "/booley-project",
        "BOOLEY_AGENT_APP": "claude",
    }

    @staticmethod
    def _request(workspace: Path):
        return SimpleNamespace(
            spec={
                "image": "booley-sandbox",
                "remoteEnv": {
                    **TestRunProjectCommand.COMMAND_ENV,
                    "CLAUDE_CODE_OAUTH_TOKEN": "${localEnv:CLAUDE_CODE_OAUTH_TOKEN}",
                },
            },
            issuance=object(),
            profile=None,
            name=sr.session_container_name(workspace),
            labels=(),
            relay=None,
        )

    @staticmethod
    def _vscode_state(workspace: Path) -> str:
        return json.dumps([{"Config": {"Labels": {"devcontainer.local_folder": str(workspace)}}}])

    def test_reuses_valid_running_vscode_runtime(self, workspace: Path):
        request = self._request(workspace)
        command = ["claude", "setup-token"]
        with (
            patch.object(sr, "_validate_up_request", return_value=request),
            patch.object(
                sr,
                "resolve_running_sandbox",
                return_value=SimpleNamespace(
                    name="vscode-runtime", container_id="vscode-runtime", vscode=True
                ),
            ),
            patch.object(sr, "container_matches_issuance", return_value=True),
            patch.object(sr, "_run_up_transaction") as start,
            patch("booley.runtime.lifecycle_lock.host_lifecycle_lock", return_value=nullcontext()),
            patch("booley.runtime.runtime_attachment.run_command") as run,
        ):
            run.return_value = SimpleNamespace(exit_code=0)
            assert sr.run_project_command(workspace, command, tty=True) == 0

        start.assert_not_called()
        run.assert_called_once_with(
            workspace,
            "vscode-runtime",
            command,
            tty=True,
            env=self.COMMAND_ENV,
            cwd=None,
        )

    def test_starts_headless_runtime_when_none_is_running(self, workspace: Path):
        request = self._request(workspace)
        command = ["claude", "setup-token"]
        with (
            patch.object(sr, "_validate_up_request", return_value=request),
            patch.object(
                sr,
                "resolve_running_sandbox",
                side_effect=[
                    None,
                    None,
                    SimpleNamespace(name=request.name, container_id=request.name, vscode=False),
                ],
            ),
            patch.object(sr, "container_matches_issuance", return_value=True),
            patch.object(sr, "_run_up_transaction") as start,
            patch.object(sr, "_warn_on_stale_session_containers"),
            patch("booley.runtime.lifecycle_lock.host_lifecycle_lock", return_value=nullcontext()),
            patch("booley.runtime.runtime_attachment.run_command") as run,
        ):
            run.return_value = SimpleNamespace(exit_code=0)
            assert sr.run_project_command(workspace, command, tty=False) == 0

        start.assert_called_once_with(
            workspace,
            request,
            rebuild=False,
            expected_image_id=None,
        )
        run.assert_called_once_with(
            workspace,
            request.name,
            command,
            tty=False,
            env=self.COMMAND_ENV,
            cwd=None,
        )

    def test_ignores_running_runtime_for_another_project(self, workspace: Path):
        request = self._request(workspace)
        with (
            patch.object(sr, "_validate_up_request", return_value=request),
            patch.object(
                sr,
                "resolve_running_sandbox",
                return_value=SimpleNamespace(
                    name="vscode-runtime", container_id="vscode-runtime", vscode=True
                ),
            ),
            patch.object(sr, "container_matches_issuance", return_value=True),
            patch.object(sr, "_run_up_transaction") as start,
            patch("booley.runtime.lifecycle_lock.host_lifecycle_lock", return_value=nullcontext()),
            patch("booley.runtime.runtime_attachment.run_command") as run,
        ):
            run.return_value = SimpleNamespace(exit_code=0)
            assert sr.run_project_command(workspace, ["claude", "setup-token"]) == 0

        start.assert_not_called()
        run.assert_called_once()
        assert run.call_args.args[1] == "vscode-runtime"

    def test_recovers_interrupted_invalidation_before_selecting_runtime(self, workspace: Path):
        select = Mock()
        with (
            patch.object(
                sr,
                "_recover_before_lifecycle",
                side_effect=sr.SessionError("recovered interrupted refresh"),
            ) as recover,
            patch.object(sr, "_select_or_start_project_runtime", select),
            patch("booley.runtime.lifecycle_lock.host_lifecycle_lock", return_value=nullcontext()),
            pytest.raises(sr.SessionError, match="recovered interrupted refresh"),
        ):
            sr.run_project_command(workspace, ["claude", "setup-token"])

        recover.assert_called_once_with(None)
        select.assert_not_called()

    def test_parallel_enters_wait_then_execute_concurrently(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from booley.runtime import lifecycle_lock

        probe = _ParallelEnterProbe(self.COMMAND_ENV)

        real_wait = lifecycle_lock.wait_for_file_lock

        def observed_wait(handle, *, timeout_s, on_wait=None) -> None:
            if probe.first_selecting.is_set():
                probe.second_waiting.set()
            real_wait(handle, timeout_s=timeout_s, on_wait=on_wait)

        monkeypatch.setattr(lifecycle_lock, "wait_for_file_lock", observed_wait)
        monkeypatch.setattr(sr, "_recover_before_lifecycle", lambda *_args: None)
        monkeypatch.setattr(sr, "_select_or_start_project_runtime", probe.select_runtime)
        monkeypatch.setattr(
            "booley.runtime.runtime_attachment.run_command",
            probe.run_command,
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(sr.enter, workspace, ["bwave", "first"], tty=False)
            assert probe.first_selecting.wait(timeout=2)
            second = executor.submit(sr.enter, workspace, ["bwave", "second"], tty=False)
            assert probe.second_waiting.wait(timeout=2)
            assert not second.done()
            probe.release_first.set()
            assert first.result(timeout=5) == 0
            assert second.result(timeout=5) == 0

        assert probe.select_calls == 2

    def test_stale_running_runtime_blocks_headless_creation(self, workspace: Path):
        request = self._request(workspace)
        with (
            patch.object(sr, "_validate_up_request", return_value=request),
            patch.object(
                sr,
                "resolve_running_sandbox",
                return_value=SimpleNamespace(
                    name="stale-runtime", container_id="stale-runtime", vscode=True
                ),
            ),
            patch.object(sr, "container_matches_issuance", return_value=False),
            patch.object(sr, "_run_up_transaction") as start,
            patch("booley.runtime.lifecycle_lock.host_lifecycle_lock", return_value=nullcontext()),
            pytest.raises(sr.SessionError, match="does not match the current host issuance"),
        ):
            sr.run_project_command(workspace, ["claude", "setup-token"])

        start.assert_not_called()

    def test_multiple_running_runtimes_are_refused(self, workspace: Path):
        request = self._request(workspace)
        with (
            patch.object(sr, "_validate_up_request", return_value=request),
            patch.object(
                sr,
                "resolve_running_sandbox",
                side_effect=sr.SessionError("multiple running Sandboxes"),
            ),
            patch.object(sr, "container_matches_issuance", return_value=True),
            patch.object(sr, "_run_up_transaction") as start,
            patch("booley.runtime.lifecycle_lock.host_lifecycle_lock", return_value=nullcontext()),
            pytest.raises(sr.SessionError, match="multiple running Sandboxes"),
        ):
            sr.run_project_command(workspace, ["claude", "setup-token"])

        start.assert_not_called()

    def test_attachment_failure_is_reported_as_session_error(self, workspace: Path):
        with (
            patch.object(
                sr,
                "_select_or_start_project_runtime",
                return_value=("runtime", self.COMMAND_ENV),
            ),
            patch.object(sr, "_recover_before_lifecycle"),
            patch("booley.runtime.lifecycle_lock.host_lifecycle_lock", return_value=nullcontext()),
            patch(
                "booley.runtime.runtime_attachment.run_command",
                side_effect=OSError("docker disappeared"),
            ),
            pytest.raises(sr.SessionError, match=r"could not attach.*docker disappeared"),
        ):
            sr.run_project_command(workspace, ["claude", "setup-token"])


class TestEnterAlwaysSetsTERM:
    """F-43a: every agent-driven exec answered "TERM environment variable not set."

    `docker exec -t` is what sets TERM, and `_enter` only allocates a tty when
    both streams are ttys — which no agent-driven caller is. So the shell in
    the container ran with no TERM at all and said so on every command.

    Only the non-tty branch had the bug, and only it gets a value: docker's own
    `-t` TERM=xterm resolves in the image, whereas a forwarded host TERM
    (xterm-kitty, alacritty, xterm-ghostty) has no terminfo entry there and
    would break clear/tput/less/vim for those users.
    """

    def _argv(self, workspace: Path, *, tty: bool, term_env: str | None) -> list[str]:
        env = {} if term_env is None else {"TERM": term_env}
        with (
            patch.object(
                sr,
                "_select_or_start_project_runtime",
                return_value=("booley-session-x", {}),
            ),
            patch.object(sr, "_recover_before_lifecycle"),
            patch("booley.runtime.lifecycle_lock.host_lifecycle_lock", return_value=nullcontext()),
            patch("booley.runtime.runtime_attachment.run_command") as run,
            patch.dict(sr.os.environ, env, clear=(term_env is None)),
        ):
            run.return_value = SimpleNamespace(exit_code=0)
            sr.enter(workspace, ["echo", "hi"], tty=tty)
        command = run.call_args.args[2]
        return sr.exec_argv("booley-session-x", command, tty=run.call_args.kwargs["tty"])

    def test_non_tty_run_gets_a_dumb_term(self, workspace: Path):
        argv = self._argv(workspace, tty=False, term_env="xterm-256color")
        assert "-t" not in argv
        assert argv[argv.index("-e") + 1] == "TERM=dumb"

    def test_tty_run_leaves_term_to_docker(self, workspace: Path):
        # `docker exec -t` sets TERM=xterm itself, and the image has terminfo
        # for it. We must not override that.
        argv = self._argv(workspace, tty=True, term_env="xterm-256color")
        assert "-t" in argv
        assert "-e" not in argv

    def test_tty_run_never_forwards_an_exotic_host_term(self, workspace: Path):
        # ubuntu:24.04 ships no ncurses-term: xterm-kitty has no terminfo entry,
        # so forwarding it would break every curses program in the container.
        argv = self._argv(workspace, tty=True, term_env="xterm-kitty")
        assert not any("kitty" in a for a in argv)

    def test_tty_run_without_a_host_term_still_adds_nothing(self, workspace: Path):
        argv = self._argv(workspace, tty=True, term_env=None)
        assert "-e" not in argv

    def test_the_command_still_comes_last(self, workspace: Path):
        argv = self._argv(workspace, tty=False, term_env="vt100")
        assert argv[-2:] == ["echo", "hi"]
        assert "booley-session-x" in argv


class TestSessionRefresh:
    def test_session_command_coordinator_fits_on_a_screen(self):
        import inspect

        from booley.harness import booley

        assert len(inspect.getsourcelines(booley._cmd_session)[0]) <= 50

    def test_parser_exposes_refresh_subcommand(self):
        from booley.harness.booley import _build_parser

        args = _build_parser().parse_args(["session", "refresh"])
        assert args.command == "session"
        assert args.session_command == "refresh"

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
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from booley.harness import booley
        from booley.harness.booley import _build_parser

        issuance_invalidation.prepare(str(workspace.resolve()))
        issuance_invalidation.cancel(str(workspace.resolve()))
        booley_config = tmp_path / "xdg" / "booley"
        booley_config.chmod(0o755)
        monkeypatch.setattr(booley, "_report_upgrade_before_session", lambda _root: None)
        monkeypatch.setattr(health, "due_reason", lambda _root: None)
        monkeypatch.setattr(
            sr,
            "_run",
            lambda *_a, **_kw: pytest.fail("unsafe recovery must precede Docker mutation"),
        )
        args = _build_parser().parse_args(["session", command])

        assert booley._cmd_session(args, workspace) == 2

        captured = capsys.readouterr()
        assert str(booley_config) in captured.err
        assert "0755" in captured.err
        assert f"chmod 700 {booley_config}" in captured.err
        assert "Traceback" not in captured.out
        assert "Traceback" not in captured.err

    def test_refresh_configures_progress_before_reconciling_image(self, tmp_path: Path):
        from booley.harness import booley, session_refresh
        from booley.harness.booley import _build_parser

        args = _build_parser().parse_args(["session", "refresh"])
        from booley.runtime.session_refresh import RefreshResult

        result = RefreshResult(
            RefreshImage("sha256:fresh", None, None, False), "replaced", "old", "new"
        )
        events: list[str] = []
        with (
            patch.object(
                booley,
                "configure_progress_output",
                side_effect=lambda: events.append("configure"),
                create=True,
            ),
            patch.object(
                session_refresh,
                "refresh",
                side_effect=lambda *_args, **_kwargs: events.append("reconcile") or result,
            ),
            patch.object(booley, "_report_session_health"),
            patch.object(health, "due_reason", return_value=None),
        ):
            assert booley._session_refresh(args, tmp_path) == 0

        assert events == ["configure", "reconcile"]

    def test_small_session_handlers_delegate_and_report(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from booley.harness import booley

        with (
            patch.object(sr, "enter", return_value=7) as enter,
            patch.object(booley.sys.stdin, "isatty", return_value=True),
            patch.object(booley.sys.stdout, "isatty", return_value=True),
        ):
            assert (
                booley._session_enter(SimpleNamespace(exec_cmd=["--", "echo", "ready"]), tmp_path)
                == 7
            )
        enter.assert_called_once_with(tmp_path, ["echo", "ready"], tty=True, cwd=None)

        with patch.object(sr, "down", return_value=sr.DownResult()):
            assert booley._session_down(SimpleNamespace(), tmp_path) == 0

        assert capsys.readouterr().out.splitlines() == ["no Sandbox container for this Project"]

    def test_refresh_refuses_active_vscode_before_reconciling_image(self, tmp_path: Path):
        from booley.harness import booley, session_refresh
        from booley.harness.booley import _build_parser

        args = _build_parser().parse_args(["session", "refresh"])
        with (
            patch.object(
                session_refresh,
                "refresh",
                side_effect=sr.SessionError("VS Code owns Sandbox 'vscode-owned'"),
            ) as refresh,
        ):
            assert booley._cmd_session(args, tmp_path) == 2

        refresh.assert_called_once_with(tmp_path, verbose=False)

    def test_runtime_probe_verifies_exact_wheel_identity(self, tmp_path: Path):
        image_id = "sha256:" + "a" * 64
        identity = json.dumps(
            {
                "source": "wheel-source",
                "sha256": "b" * 64,
                "package_version": "0.2.6",
                "module_version": "0.2.6",
            }
        )
        with (
            patch.object(sr, "_docker_stdout", return_value=image_id),
            patch.object(
                sr,
                "_run",
                return_value=subprocess.CompletedProcess([], 0, identity, ""),
            ) as run,
        ):
            sr.verify_refreshed_session(
                tmp_path,
                image_id,
                expected_wheel_source_fingerprint="wheel-source",
                expected_wheel_sha256="b" * 64,
            )

        assert run.call_count == 1
        argv = run.call_args.args[0]
        assert argv[:3] == ["docker", "exec", sr.session_container_name(tmp_path)]
        assert argv[3:6] == ["python3", "-I", "-c"]

    def test_runtime_probe_skips_optional_identity_checks_when_unrequested(
        self, tmp_path: Path
    ) -> None:
        image_id = "sha256:" + "a" * 64
        with (
            patch.object(sr, "_docker_stdout", return_value=image_id),
            patch.object(sr, "_run") as run,
        ):
            sr.verify_refreshed_session(tmp_path, image_id)
        run.assert_not_called()

    def test_runtime_probe_rejects_malformed_wheel_identity(self, tmp_path: Path):
        image_id = "sha256:" + "a" * 64
        with (
            patch.object(sr, "_docker_stdout", return_value=image_id),
            patch.object(
                sr,
                "_run",
                return_value=subprocess.CompletedProcess([], 0, "not-json", "probe failed"),
            ),
            pytest.raises(sr.SessionError, match="wheel identity"),
        ):
            sr.verify_refreshed_session(
                tmp_path,
                image_id,
                expected_wheel_source_fingerprint="wheel-source",
            )

    def test_runtime_probe_rejects_wheel_identity_mismatch(self, tmp_path: Path):
        image_id = "sha256:" + "a" * 64
        identity = json.dumps(
            {
                "source": "other-source",
                "sha256": "b" * 64,
                "package_version": "0.2.6",
                "module_version": "0.2.6",
            }
        )
        with (
            patch.object(sr, "_docker_stdout", return_value=image_id),
            patch.object(
                sr,
                "_run",
                return_value=subprocess.CompletedProcess([], 0, identity, ""),
            ),
            pytest.raises(sr.SessionError, match="wheel identity") as caught,
        ):
            sr.verify_refreshed_session(
                tmp_path,
                image_id,
                expected_wheel_source_fingerprint="wheel-source",
                expected_wheel_sha256="b" * 64,
            )

        assert "source_fingerprint wheel-source -> other-source" in str(caught.value)
        assert "wheel_sha256" not in str(caught.value)

    def test_down_up_never_announces_persisted_stale_doctor_findings(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from booley.harness import booley
        from booley.harness import doctor as doctor_module
        from booley.harness.booley import _build_parser

        monkeypatch.setattr(sr, "down", lambda _root, **_kwargs: sr.DownResult(headless=True))
        monkeypatch.setattr(sr, "session_container_name", lambda _root: "session")
        down_args = _build_parser().parse_args(["session", "down"])
        assert booley._cmd_session(down_args, tmp_path) == 0
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
        up_args = _build_parser().parse_args(["session", "up"])

        assert booley._cmd_session(up_args, tmp_path) == 0
        output = capsys.readouterr()
        assert "Automatic Doctor is running" in output.err
        assert "Doctor inputs changed" in output.err
        assert "Sandbox ready: session" in output.out


@pytest.mark.parametrize(
    "capable,duplicate,expected", [(True, False, True), (True, True, False), (False, False, False)]
)
def test_canonical_pin_checks_immutable_image_and_rejects_alias_mask(
    tmp_path, monkeypatch, capable, duplicate, expected
):
    import json

    from booley.runtime import project_image

    workspace = tmp_path / "project"
    workspace.mkdir()
    source = workspace / ".booley"
    mounts = [{"Destination": "/work/.booley", "Source": str(source), "Type": "bind", "RW": True}]
    if duplicate:
        mounts.append(
            {
                "Destination": "/booley-project/secrets",
                "Source": str(source),
                "Type": "bind",
                "RW": True,
            }
        )
    raw = json.dumps([{"Mounts": mounts}])
    image_id = "sha256:" + "a" * 64
    inspected = []
    monkeypatch.setattr(
        project_image,
        "project_data_alias_capable",
        lambda image: inspected.append(image) or capable,
    )
    assert (
        sr._project_data_mount_root_is_pinned(
            raw, workspace, source, layout="canonical-alias", image_id=image_id
        )
        is expected
    )
    assert inspected == [image_id]


@pytest.mark.parametrize("vscode", [False, True])
@pytest.mark.parametrize("drift", [None, "missing", "changed", "pre-image-identity"])
def test_container_resume_requires_issued_git_environment(tmp_path, monkeypatch, vscode, drift):
    spec = build_spec(dc.APP_NONE, image="sha256:test")
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda image: image)
    runtime_spec.pin_image(spec)
    labels = {"devcontainer.local_folder": str(tmp_path)} if vscode else {}
    sections = (spec["containerEnv"],) if vscode else (spec["containerEnv"], spec["remoteEnv"])
    environment = {key: value for section in sections for key, value in section.items()}
    if drift == "missing":
        environment.pop("GIT_CONFIG_VALUE_0")
    elif drift == "changed":
        environment["GIT_CONFIG_VALUE_0"] = "Human"
    elif drift == "pre-image-identity":
        environment.pop("BOOLEY_SANDBOX_IMAGE_ID", None)
    state = {
        "Image": "sha256:test",
        "Config": {
            "Image": "sha256:test",
            "User": "agent",
            "WorkingDir": "/work",
            "Env": [f"{key}={value}" for key, value in environment.items()],
        },
        "HostConfig": {"Memory": 0},
        "NetworkSettings": {
            "Networks": {network: {} for network in sr._flag_values(spec["runArgs"], "--network")}
        },
    }
    monkeypatch.setattr(runtime_spec, "labels", lambda _stamp: ())
    monkeypatch.setattr(sr, "_session_hardening_matches", lambda *_args: True)
    monkeypatch.setattr(sr, "_mounts_match_spec", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        sr,
        "_docker_stdout",
        lambda argv: json.dumps(labels) if "--format" in argv else json.dumps([state]),
    )
    assert sr.container_matches_issuance(
        "test", SimpleNamespace(image_id="sha256:test"), spec=spec, workspace=tmp_path
    ) is (drift is None)


@pytest.mark.parametrize(
    "network_constant, matches",
    [
        pytest.param("UNPROXIED_NETWORK", True, id="unproxied-on-routed-network"),
        pytest.param("EGRESS_NETWORK", False, id="unproxied-on-egress-network"),
    ],
)
def test_container_resume_matches_issued_egress_topology(
    tmp_path, monkeypatch, network_constant, matches
):
    spec = build_spec(dc.APP_NONE, image="sha256:test", egress="unproxied")
    network = getattr(dc, network_constant)
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
        "HostConfig": {"Memory": 0},
        "NetworkSettings": {"Networks": {network: {}}},
    }
    monkeypatch.setattr(runtime_spec, "labels", lambda _stamp: ())
    monkeypatch.setattr(sr, "_session_hardening_matches", lambda *_args: True)
    monkeypatch.setattr(sr, "_mounts_match_spec", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        sr,
        "_docker_stdout",
        lambda argv: json.dumps({}) if "--format" in argv else json.dumps([state]),
    )
    assert (
        sr.container_matches_issuance(
            "test", SimpleNamespace(image_id="sha256:test"), spec=spec, workspace=tmp_path
        )
        is matches
    )


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


def test_startup_egress_drift_names_init_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_load_spec", lambda _root: {})

    def refuse(*_args):
        raise runtime_spec.EgressDriftError(
            "Sandbox spec egress mode 'proxy' differs from host [sandbox] egress = 'unproxied'"
        )

    monkeypatch.setattr(runtime_spec, "validate", refuse)
    with pytest.raises(sr.SessionError) as raised:
        sr._validate_up_request(tmp_path)
    message = str(raised.value)
    assert "egress = 'unproxied'" in message
    assert "`booley init --seed`" in message
    assert "`booley session down`" in message


def test_vscode_prepare_names_init_seed_for_egress_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_refuse_outdated_vscode_containers", lambda *_args: None)

    def refuse(*_args):
        raise runtime_spec.EgressDriftError("Sandbox spec egress mode differs from host policy")

    monkeypatch.setattr(runtime_spec, "validate", refuse)
    with pytest.raises(sr.SessionError) as raised:
        sr._refuse_then_validate(tmp_path, {})
    message = str(raised.value)
    assert "`booley init --seed`" in message
    assert "Rebuild Container" in message
    assert "session refresh" not in message


def test_startup_host_config_error_names_file_without_teardown(tmp_path, monkeypatch):
    from booley.config.host_config import host_config_path

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    config = host_config_path()
    config.parent.mkdir(parents=True)
    config.write_text('[sandbox]\negress = "proxied"\n', encoding="utf-8")
    monkeypatch.setattr(sr, "_load_spec", lambda _root: {})
    with pytest.raises(sr.SessionError) as raised:
        sr._validate_up_request(tmp_path)
    message = str(raised.value)
    assert str(config) in message
    assert "sandbox.egress" in message
    assert "session down" not in message


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
    monkeypatch.setattr(sr, "_run", docker)
    monkeypatch.setattr(sr.idk, "container_exists", lambda name: False)
    monkeypatch.setattr(sr, "_relay_resources", lambda root: None)
    monkeypatch.setattr(sr, "_relay_objects_exist", lambda relay: False)
    with pytest.raises(sr.SessionError, match="identity disagrees"):
        sr._down_unlocked(project)
    documents["editor-id"]["State"]["Running"] = False
    with pytest.raises(sr.SessionError, match="identity disagrees"):
        sr._down_unlocked(project)
    del documents["editor-id"]
    assert sr._down_unlocked(project).claim_cleared
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
        "remoteEnv": {"BOOLEY_DIR": "/booley-project", "CLAUDE_CODE_OAUTH_TOKEN": "secret"},
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
        "env": {"BOOLEY_DIR": "/booley-project"},
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
    "state,expected",
    [
        ("managed", "booley session refresh"),
        ("licensed", "booley session refresh"),
        ("corrupt", "booley init --seed"),
        ("external", "rebuild/pull"),
        ("vscode", "stop the Sandbox in VS Code"),
        ("runtime", "booley session up --rebuild"),
        ("inside", "on the host"),
    ],
    ids=["managed", "licensed", "corrupt", "external", "vscode", "runtime", "inside"],
)
def test_drift_remedy_has_one_state_appropriate_workflow(tmp_path, monkeypatch, state, expected):
    from types import SimpleNamespace

    from booley.runtime import runtime_context, session_issuance, session_runtime

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: state == "inside")
    loads, probes = [], []

    def issuance(_root):
        loads.append(_root)
        if state == "corrupt":
            raise session_issuance.IssuanceUnavailableError("unreadable stamp")
        return SimpleNamespace(license_profile="site" if state == "licensed" else None)

    def vscode_owner(_root):
        probes.append(_root)
        return "editor" if state == "vscode" else None

    monkeypatch.setattr(session_issuance, "load_issued_snapshot", issuance)
    monkeypatch.setattr(session_issuance, "stamp_is_absent", lambda _root: False)
    monkeypatch.setattr(session_runtime, "vscode_session_container", vscode_owner)
    monkeypatch.setattr(
        session_runtime.project_image, "project_sandbox_image", lambda _root: "selected"
    )
    monkeypatch.setattr(
        session_runtime.project_image,
        "is_managed_sandbox_image",
        lambda *_args: state != "external",
    )
    remedy = session_runtime.sandbox_remedy(tmp_path, runtime_only=state == "runtime")
    assert expected in remedy
    if state in {"managed", "licensed", "vscode", "inside"}:
        assert remedy.count("booley session refresh") == (state in {"managed", "licensed"})
    if state in {"managed", "licensed"}:
        assert "init --seed" not in remedy
        assert "session down" not in remedy
    if state == "vscode":
        assert "booley init --force" in remedy and "Dev Containers: Rebuild Container" in remedy
    if state == "inside":
        assert "booley doctor" in remedy
    expected_loads = 0 if state == "inside" else 1
    assert (len(loads), len(probes)) == (
        expected_loads,
        0 if state in {"inside", "corrupt"} else 1,
    )


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
    assert "BOOLEY_DIR=/booley-project" in overrides
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
        _two_cli_spec(record="claude=auto,codex=api_key", **placeholder), workspace, "test"
    )

    assert command.environment["OPENAI_API_KEY"] == "SYNTHETIC_OPENAI"
    assert "OPENAI_API_KEY" in _flag_values(command, "-e")
    with pytest.raises(sr.SessionError, match="credential environment"):
        sr.docker_run_argv(_two_cli_spec(record="claude=auto", **placeholder), workspace, "test")


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
        sr.docker_run_argv(_two_cli_spec(record="claude=auto,bogus=auto"), workspace, "test")


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
    assert command_env["BOOLEY_DIR"] == "/booley-project"
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
        "BOOLEY_DIR=/booley-project",
        f"{_CREDENTIAL_KEY}=container-held-secret",
    ]
    inspect = sr._docker_stdout

    def stdout(argv: list[str]) -> str | None:
        # The fixture transport answers label/JSON inspections; add the image-ID format.
        if argv[-1] == "{{.Image}}":
            return fixture.state["Image"]
        return inspect(argv)

    monkeypatch.setattr(sr, "_docker_stdout", stdout)


def test_verified_headless_container_compares_only_exported_shell_env(
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
    fixture.state["Config"]["Env"][1] = "BOOLEY_DIR=/booley-project"
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
    profile = SimpleNamespace(
        server_ipv4="10.20.30.40",
        server_hostid="license-server-01",
        lmgrd_port=2100,
        vendor_port=2101,
    )
    monkeypatch.setattr(runtime_spec, "requested_license", lambda *_a, **_k: profile)
    relay_resources = sr._relay_resources(workspace)
    validate = Mock()
    monkeypatch.setattr(flexnet_docker, "validate_relay", validate)

    assert sr.verified_headless_container(workspace, _refresh_image()) == fixture.state["Id"]
    assert validate.call_args.args[:2] == (relay_resources, sr.session_container_name(workspace))
    assert validate.call_args.kwargs["image"] == fixture.issuance.relay_image_id

    # A stopped (or otherwise drifted) relay makes the matching Sandbox not current.
    validate.side_effect = flexnet_docker.RelayDockerError("license relay is not running")
    assert sr.verified_headless_container(workspace, _refresh_image()) is None


def test_remove_refresh_relay_removes_existing_relay_objects(workspace, monkeypatch):
    from booley.eda.provisioning.licensing import flexnet_docker

    remove = Mock()
    monkeypatch.setattr(flexnet_docker, "remove_relay", remove)
    relay = sr._relay_resources(workspace)

    monkeypatch.setattr(sr, "_relay_objects_exist", lambda _relay: False)
    sr.remove_refresh_relay(workspace)
    remove.assert_not_called()

    monkeypatch.setattr(sr, "_relay_objects_exist", lambda candidate: candidate == relay)
    sr.remove_refresh_relay(workspace)
    remove.assert_called_once_with(relay)


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


def test_sandbox_remedy_points_withdrawn_stamp_to_refresh(workspace, monkeypatch):
    from booley.runtime import runtime_context

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    state = SimpleNamespace(stamp="absent", owner=None, managed=True, licensed=False)

    def load(_root):
        if state.stamp != "present":
            raise runtime_spec.IssuanceUnavailableError("stamp unavailable")
        return SimpleNamespace(license_profile="site" if state.licensed else None)

    monkeypatch.setattr(runtime_spec, "load_issued_snapshot", load)
    monkeypatch.setattr(runtime_spec, "stamp_is_absent", lambda _root: state.stamp == "absent")
    monkeypatch.setattr(sr, "vscode_session_container", lambda _root: state.owner)
    monkeypatch.setattr(sr.project_image, "project_sandbox_image", lambda _root: "selected")
    monkeypatch.setattr(sr.project_image, "is_managed_sandbox_image", lambda *_args: state.managed)
    refresh = "run `booley session refresh` on the host"

    # Withdrawn (absent) stamp, managed image, no VS Code Sandbox: refresh repairs it.
    assert sr.sandbox_remedy(workspace) == refresh

    # A present but corrupt stamp still needs the reseed workflow.
    state.stamp = "corrupt"
    assert "booley init --seed" in sr.sandbox_remedy(workspace)

    # A VS Code owner or an external image keeps its workflow when the stamp is withdrawn.
    state.stamp = "absent"
    state.owner = "editor"
    assert "booley init --seed" in sr.sandbox_remedy(workspace)
    state.owner, state.managed = None, False
    assert "booley init --seed" in sr.sandbox_remedy(workspace)

    # With a stamp present, VS Code and external-image owners keep their workflows ...
    state.stamp, state.managed, state.owner = "present", True, "editor"
    assert "Dev Containers: Rebuild Container" in sr.sandbox_remedy(workspace)
    state.owner, state.managed = None, False
    assert "rebuild/pull the selected external image" in sr.sandbox_remedy(workspace)

    # ... and a licensed Sandbox no longer needs `session down` first.
    state.managed, state.licensed = True, True
    assert sr.sandbox_remedy(workspace) == refresh


def _prepare_refresh_start(wired, monkeypatch, *, licensed: bool):
    """A reissued spec, fake Docker and Sandbox identity probes for ``start_refreshed_sandbox``."""
    from booley.eda.provisioning.licensing import flexnet_docker

    workspace, run = wired
    name = sr.session_container_name(workspace)
    issuance = _test_issuance(workspace)
    issuance.image_id = _REFRESH_IMAGE_ID
    issuance.license_profile = "site" if licensed else None
    profile = SimpleNamespace(
        server_ipv4="10.20.30.40",
        server_hostid="license-server-01",
        lmgrd_port=2100,
        vendor_port=2101,
    )
    events: list[str] = []

    def execute(argv, **_kwargs):
        if argv[:2] == ["docker", "run"]:
            events.append("docker run")
        stdout = _REFRESH_IMAGE_ID if argv[-1] == "{{.Image}}" else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    run.side_effect = execute
    request = sr.UpRequest(
        _spec(),
        issuance,
        profile if licensed else None,
        name,
        runtime_spec.labels(issuance),
        sr._relay_resources(workspace),
    )
    monkeypatch.setattr(sr.idk, "container_exists", lambda _name: False)
    monkeypatch.setattr(sr.idk, "container_running", lambda _name: False)
    monkeypatch.setattr(
        sr, "_strict_refresh_container", lambda _name: {"Id": "new-id", "State": {"Running": True}}
    )
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
    candidates = Mock(return_value=[stale])
    remove = Mock()
    monkeypatch.setattr(sr, "_stopped_vscode_reconcile_candidates", candidates)
    monkeypatch.setattr(sr, "_remove_stopped_session_container", remove)

    assert (
        sr.start_refreshed_sandbox(fixture.workspace, fixture.request, _refresh_image())
        == "new-id"
    )

    remove.assert_not_called()
    assert fixture.events == ["docker run"]
    assert not any(stale in _argv_of(call) for call in fixture.run.call_args_list)


def _headless_state(workspace: Path, **label_overrides: str | None) -> dict:
    """Strict inspect data for a headless Sandbox this Project could own."""
    labels = {
        "booley.role": "interactive",
        "booley.project-id": sr.project_label_id(str(workspace.resolve())),
    }
    for key, value in label_overrides.items():
        if value is None:
            labels.pop(key, None)
        else:
            labels[key] = value
    state = _refresh_state(running=True, labels=labels)
    state["Id"] = "c" * 64
    return state


def test_remove_refresh_predecessor_refuses_unproven_container(workspace, monkeypatch):
    docker = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(sr, "_run", docker)
    cases = {
        "other project": {"booley.project-id": "someone-else"},
        "missing role": {"booley.role": None},
        "vscode folder": {"devcontainer.local_folder": str(workspace)},
    }
    for label, overrides in cases.items():
        state = _headless_state(workspace, **overrides)
        monkeypatch.setattr(sr, "_strict_refresh_container", lambda _name, s=state: s)
        with pytest.raises(sr.SessionError, match="not this Project's headless Sandbox"):
            sr.remove_refresh_predecessor(workspace)
        assert docker.call_count == 0, label


def test_remove_refresh_predecessor_refuses_container_without_identity(workspace, monkeypatch):
    docker = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(sr, "_run", docker)
    state = _headless_state(workspace)
    del state["Id"]
    monkeypatch.setattr(sr, "_strict_refresh_container", lambda _name: state)

    with pytest.raises(sr.SessionError, match="no immutable container identity"):
        sr.remove_refresh_predecessor(workspace)

    docker.assert_not_called()


def test_remove_refresh_predecessor_stops_then_removes_by_immutable_id(workspace, monkeypatch):
    state = _headless_state(workspace)
    calls: list[list[str]] = []

    def docker(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sr, "_run", docker)
    monkeypatch.setattr(sr, "_strict_refresh_container", lambda _name: state)

    assert sr.remove_refresh_predecessor(workspace) == state["Id"]
    assert calls == [["docker", "stop", state["Id"]], ["docker", "rm", "-f", state["Id"]]]

    calls.clear()
    monkeypatch.setattr(sr, "_strict_refresh_container", lambda _name: None)
    assert sr.remove_refresh_predecessor(workspace) is None
    assert calls == []
