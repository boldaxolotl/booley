"""Tests for session_runtime: headless Session Runtime lifecycle (F-4).

The spec -> `docker run` translation is the part that must not drift from
`devcontainer.build_devcontainer_spec`, so it is tested against a real spec
rather than a hand-written dict.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from booley.eda.provisioning import runtime_spec
from booley.harness import devcontainer as dc
from booley.harness import session_runtime as sr


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "i2c"
    ws.mkdir()
    return ws


def _spec(**kwargs) -> dict:
    defaults = {
        "project_dir_source": "/c/ws/i2c/.booley_project",
        "mcp_start_command": dc.mcp_post_start_command(),
    }
    return dc.build_devcontainer_spec("claude", **{**defaults, **kwargs})


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

    def test_workspace_and_project_dir_are_mounted(self, workspace: Path):
        from booley.runtime.platform_paths import docker_mount_path

        mounts = _flag_values(_argv(_spec(), workspace), "--mount")
        assert f"source={docker_mount_path(workspace)},target=/work,type=bind" in mounts
        assert "source=/c/ws/i2c/.booley_project,target=/booley-project,type=bind" in mounts

    def test_readonly_credential_mounts_survive_translation(self, workspace: Path):
        spec = _spec(auth_token_source="/c/home/.credentials.json")
        mounts = _flag_values(_argv(spec, workspace), "--mount")
        creds = [m for m in mounts if "claude-creds-seed" in m]
        assert creds and creds[0].endswith(",readonly")

    def test_state_volume_basename_is_substituted(self, workspace: Path):
        mounts = _flag_values(_argv(_spec(), workspace), "--mount")
        assert "source=booley-claude-state-i2c,target=/home/agent/.claude,type=volume" in mounts

    def test_remote_env_becomes_e_flags(self, workspace: Path):
        env = _flag_values(_argv(_spec(), workspace), "-e")
        assert "BOOLEY_MCP_MODE=interactive" in env
        assert "BOOLEY_PROJECT_DIR=/booley-project" in env
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

    def test_reaper_can_own_the_headless_container(self, workspace: Path):
        # The whole point of reusing the spec: same label as the VS Code container.
        assert dc.INTERACTIVE_ROLE_LABEL in _argv(_spec(), workspace)

    def test_no_image_is_a_session_error(self, workspace: Path):
        with pytest.raises(sr.SessionError, match="no 'image'"):
            sr.docker_run_argv({}, workspace, "x")

    def test_oauth_token_is_resolved_not_passed_literally(self, workspace: Path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-tok")
        spec = _spec(forward_oauth_token=True)
        env = _flag_values(_argv(spec, workspace), "-e")
        assert "CLAUDE_CODE_OAUTH_TOKEN=sk-tok" in env


class TestHookArgv:
    def test_runs_the_hook_string_through_a_shell(self):
        argv = sr.hook_argv("c1", "a; b")
        assert argv == ["docker", "exec", "c1", "bash", "-lc", "a; b"]

    def test_post_create_hook_from_a_real_spec_is_a_shell_string(self):
        spec = _spec(auth_token_source="/c/home/.credentials.json")
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
        project_data_source=str(workspace / ".booley_project"),
    )


def _vscode_labels(
    workspace: Path,
    issuance: SimpleNamespace,
    *,
    spec_digest: str = "current-spec",
) -> dict[str, str]:
    from booley.eda.provisioning import runtime_spec

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
    from booley.eda.provisioning import runtime_spec

    _write_spec(workspace, _spec())
    monkeypatch.setattr(sr, "_docker_stdout", docker_stdout)
    monkeypatch.setattr(
        runtime_spec,
        "authorized_project_data_source",
        lambda _path: workspace / ".booley_project",
    )
    monkeypatch.setattr(runtime_spec, "validate", lambda *_args: issuance)
    monkeypatch.setattr(runtime_spec, "authenticate", lambda *_args: issuance)
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


class TestPrepareMigration:
    @pytest.fixture(autouse=True)
    def _authenticated_issuance(self, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from booley.eda.provisioning import runtime_spec

        monkeypatch.setattr(runtime_spec, "authenticate", lambda *_args: _test_issuance(workspace))

    def test_malformed_mount_inventory_fails_loudly(self) -> None:
        with pytest.raises(sr.SessionError, match=r"cannot inspect bind mounts.*current-vscode"):
            sr._container_has_unavailable_bind("current-vscode", {"Mounts": None})

    def test_unmappable_windows_daemon_bind_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sr, "host_path_from_docker_mount", lambda _source: None)
        state = {"Mounts": [{"Type": "bind", "Source": "/run/desktop/mnt/host/wsl/x"}]}

        assert not sr._container_has_unavailable_bind("current-vscode", state)

    def test_missing_legacy_inspection_is_fail_closed_and_idempotent(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        container = sr._LegacyVscodeContainer("legacy-vscode", "container-id")
        monkeypatch.setattr(sr, "_docker_stdout", lambda _argv: None)
        monkeypatch.setattr(
            sr,
            "_run",
            lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
        )

        assert sr._inspected_running({}) is None
        assert sr._legacy_vscode_identity({}, workspace, {}) is None
        sr._stop_legacy_vscode_container(container)
        assert "no longer present" in sr._quiesced_validation_recovery(container)
        sr._remove_quiesced_legacy_container(container)

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
        from booley.eda.provisioning import runtime_spec

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
            lambda _path: workspace / ".booley_project",
        )
        monkeypatch.setattr(runtime_spec, "validate", lambda *_args: issuance)
        monkeypatch.setattr(sr, "_preflight", lambda *_args, **_kwargs: None)
        remove = Mock()
        monkeypatch.setattr(sr, "_run", remove)

        with pytest.raises(sr.SessionError, match=r"running Session Runtime.*older host issuance"):
            sr.prepare(workspace)

        remove.assert_not_called()

    def test_headless_session_container_is_never_reconciled_for_vscode(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.eda.provisioning import runtime_spec

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
            lambda _path: workspace / ".booley_project",
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
        from booley.eda.provisioning import runtime_spec

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
            lambda _path: workspace / ".booley_project",
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
        from booley.eda.provisioning import runtime_spec

        _write_spec(workspace, _spec())
        source = "/host/skills/renamed-skill"
        target = f"{dc.HOST_SKILLS_SIDECAR}/example-skill"
        monkeypatch.setattr(
            runtime_spec,
            "authorized_project_data_source",
            lambda _path: workspace / ".booley_project",
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
        assert "booley init --seed" in message

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
            "booley.eda.provisioning.runtime_spec.authorized_project_data_source",
            lambda _path: workspace / ".booley_project",
        )
        monkeypatch.setattr("booley.eda.provisioning.runtime_spec.validate", validate)

        with pytest.raises(sr.SessionError, match="cannot safely migrate"):
            sr.prepare(workspace)

        validate.assert_not_called()

    def test_running_dual_bind_container_allows_validation(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_spec(workspace, _spec())
        source = str(workspace / ".booley_project")
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
                        "Destination": "/work/.booley_project",
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
            "booley.eda.provisioning.runtime_spec.authorized_project_data_source",
            lambda _path: workspace / ".booley_project",
        )
        monkeypatch.setattr(
            "booley.eda.provisioning.runtime_spec.validate", lambda *_args: issuance
        )
        monkeypatch.setattr(
            "booley.eda.provisioning.runtime_spec.requested_license", lambda _path, **_kwargs: None
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
            "booley.eda.provisioning.runtime_spec.authorized_project_data_source",
            lambda _path: workspace / ".booley_project",
        )
        monkeypatch.setattr("booley.eda.provisioning.runtime_spec.validate", validate)

        with pytest.raises(sr.SessionError, match="cannot inventory"):
            sr.prepare(workspace)

        validate.assert_not_called()

    def test_source_transition_to_local_blocks_external_legacy_container(
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
            "booley.eda.provisioning.runtime_spec.authorized_project_data_source",
            lambda _path: workspace / ".booley_project",
        )

        with pytest.raises(sr.SessionError, match="cannot safely migrate"):
            sr.prepare(workspace)

    def test_authenticated_legacy_vscode_container_is_stopped_validated_then_removed(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.eda.provisioning import runtime_spec

        _write_spec(workspace, _spec())
        issuance = _test_issuance(workspace)
        container_id = "a" * 64
        running = {
            "Id": container_id,
            "State": {"Running": True},
            "Config": {"Labels": _vscode_labels(workspace, issuance)},
            "Mounts": [{"Destination": "/work", "Type": "bind", "RW": True}],
        }
        stopped = {**running, "State": {"Running": False}}
        events: list[str] = []
        monkeypatch.setattr(
            runtime_spec,
            "authenticate",
            lambda *_args: events.append("authenticate") or issuance,
        )
        monkeypatch.setattr(
            runtime_spec,
            "validate",
            lambda *_args: events.append("validate") or issuance,
        )
        monkeypatch.setattr(runtime_spec, "requested_license", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            sr,
            "_strict_running_interactive_states",
            lambda: [("nifty_wright", json.dumps([running]))],
        )
        monkeypatch.setattr(sr, "_strict_all_interactive_states", lambda *_args: [])
        monkeypatch.setattr(
            sr,
            "_docker_stdout",
            lambda argv: (
                json.dumps([stopped]) if argv == ["docker", "inspect", container_id] else None
            ),
        )

        def run(argv: list[str], **_kwargs):
            events.append(argv[1])
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(sr, "_run", run)
        monkeypatch.setattr(sr, "_preflight", lambda *_args, **_kwargs: None)

        assert sr.prepare(workspace) == "current-spec"
        assert events == ["authenticate", "stop", "validate", "rm"]

    def test_post_stop_validation_failure_names_recovery_command(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from booley.eda.provisioning import runtime_spec

        _write_spec(workspace, _spec())
        issuance = _test_issuance(workspace)
        container_id = "b" * 64
        running = {
            "Id": container_id,
            "State": {"Running": True},
            "Config": {"Labels": _vscode_labels(workspace, issuance)},
            "Mounts": [{"Destination": "/work", "Type": "bind", "RW": True}],
        }
        stopped = {**running, "State": {"Running": False}}
        monkeypatch.setattr(runtime_spec, "authenticate", lambda *_args: issuance)
        monkeypatch.setattr(
            runtime_spec,
            "validate",
            Mock(side_effect=runtime_spec.RuntimeSpecError("spec changed after stop")),
        )
        monkeypatch.setattr(
            sr,
            "_strict_running_interactive_states",
            lambda: [("nifty_wright", json.dumps([running]))],
        )
        monkeypatch.setattr(
            sr,
            "_docker_stdout",
            lambda argv: (
                json.dumps([stopped]) if argv == ["docker", "inspect", container_id] else None
            ),
        )
        run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        monkeypatch.setattr(sr, "_run", run)

        with pytest.raises(sr.SessionError) as caught:
            sr.prepare(workspace)

        assert "spec changed after stop" in str(caught.value)
        assert f"docker start {container_id}" in str(caught.value)
        assert [call.args[0] for call in run.call_args_list] == [["docker", "stop", container_id]]

    def test_multiple_authenticated_legacy_containers_are_not_stopped(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        issuance = _test_issuance(workspace)

        def state(container_id: str) -> str:
            return json.dumps(
                [
                    {
                        "Id": container_id,
                        "State": {"Running": True},
                        "Config": {"Labels": _vscode_labels(workspace, issuance)},
                        "Mounts": [{"Destination": "/work", "Type": "bind", "RW": True}],
                    }
                ]
            )

        monkeypatch.setattr(
            sr,
            "_strict_running_interactive_states",
            lambda: [("first", state("c" * 64)), ("second", state("d" * 64))],
        )
        run = Mock()
        monkeypatch.setattr(sr, "_run", run)

        with pytest.raises(sr.SessionError, match="multiple or ambiguous"):
            sr._quiesce_legacy_vscode_container(workspace, workspace / ".booley_project", issuance)

        run.assert_not_called()

    def test_windows_host_paths_normalize_slashes_and_drive_case(self) -> None:
        assert sr._same_host_path(
            "c:/workplace/picorv32/.devcontainer/devcontainer.json",
            Path(r"C:\workplace\picorv32\.devcontainer\devcontainer.json"),
        )


# ---------------------------------------------------------------------------
# up() / down() / status()
# ---------------------------------------------------------------------------


def _write_spec(workspace: Path, spec: dict) -> None:
    dc.write_devcontainer(workspace, spec)


@pytest.fixture
def wired(workspace: Path, request: pytest.FixtureRequest):
    """A workspace with a spec on disk and mocked external boundaries."""
    from booley.harness import image_lifecycle

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
        project_data_source=str(workspace / ".booley_project"),
    )
    with (
        patch.object(sr.idk, "network_exists", return_value=True),
        patch.object(sr.idk, "image_exists", return_value=True),
        lifecycle_reconcile,
        patch(
            "booley.eda.provisioning.runtime_spec.validate",
            return_value=issuance,
        ),
        patch("booley.eda.provisioning.runtime_spec.authenticate", return_value=issuance),
        patch(
            "booley.eda.provisioning.runtime_spec.authorized_project_data_source",
            return_value=workspace / ".booley_project",
        ),
        patch.object(sr, "_strict_running_interactive_states", return_value=[]),
        patch.object(sr, "_container_matches_issuance", return_value=True),
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


def _recorded_parked(**overrides) -> sr.ParkedSession:
    values = {
        "name": "session",
        "backup": "backup",
        "was_running": False,
        "project_id": "project-id",
        "reconnect_egress": False,
        "container_id": "prior-id",
        "image_id": "sha256:prior",
        "egress_network_id": None,
    }
    values.update(overrides)
    return sr.ParkedSession(**values)


class TestRefreshContainerTransactions:
    def test_recovery_preserves_predecessor_when_rename_did_not_land(self):
        parked = sr.ParkedSession(
            "session",
            "backup",
            True,
            project_id="project-id",
            reconnect_egress=True,
            container_id="prior-id",
            image_id="sha256:prior",
            egress_network_id="egress-id",
        )
        predecessor = {
            "Id": "prior-id",
            "Image": "sha256:prior",
            "Config": {
                "Labels": {
                    "booley.role": "interactive",
                    "booley.project-id": "project-id",
                }
            },
            "State": {"Running": False},
            "NetworkSettings": {"Networks": {dc.EGRESS_NETWORK: {"NetworkID": "egress-id"}}},
        }
        with (
            patch.object(sr, "_strict_refresh_container", side_effect=[predecessor, None]),
            patch.object(sr, "_remove_session_candidate") as remove,
            patch.object(sr, "_start_session_container") as start,
        ):
            sr.restore_refresh_session(parked)

        remove.assert_not_called()
        start.assert_called_once_with("session")

    def test_recovery_rejects_reused_egress_network_name(self):
        parked = sr.ParkedSession(
            "session",
            "backup",
            False,
            project_id="project-id",
            reconnect_egress=True,
            container_id="prior-id",
            image_id="sha256:prior",
            egress_network_id="expected-egress-id",
        )
        predecessor = {
            "Id": "prior-id",
            "Image": "sha256:prior",
            "Config": {
                "Labels": {
                    "booley.role": "interactive",
                    "booley.project-id": "project-id",
                }
            },
            "State": {"Running": False},
            "NetworkSettings": {
                "Networks": {dc.EGRESS_NETWORK: {"NetworkID": "reused-network-id"}}
            },
        }
        with (
            patch.object(sr, "_strict_refresh_container", side_effect=[predecessor, None]),
            pytest.raises(sr.SessionError, match="network identity"),
        ):
            sr.restore_refresh_session(parked)

    def test_recovery_preserves_unproven_canonical_container(self):
        parked = sr.ParkedSession(
            "session",
            "backup",
            False,
            project_id="project-id",
            container_id="prior-id",
            image_id="sha256:prior",
        )
        candidate = _refresh_state()
        candidate["Id"] = "unknown-id"
        candidate["Image"] = "sha256:unknown"
        predecessor = _refresh_state()
        predecessor["Id"] = "prior-id"
        predecessor["Image"] = "sha256:prior"
        with (
            patch.object(sr, "_strict_refresh_container", side_effect=[candidate, predecessor]),
            patch.object(sr, "_remove_session_candidate") as remove,
            pytest.raises(sr.SessionError, match=r"cannot prove.*replacement"),
        ):
            sr.restore_refresh_session(parked)

        remove.assert_not_called()

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

    @pytest.mark.parametrize(
        ("field", "invalid", "message"),
        [
            ("Id", "different-id", "recorded refresh predecessor"),
            ("Image", "sha256:different", "wrong predecessor image"),
        ],
    )
    def test_predecessor_verification_rejects_identity_drift(self, field, invalid, message):
        state = _refresh_state()
        state.update({"Id": "prior-id", "Image": "sha256:prior", field: invalid})

        with pytest.raises(sr.SessionError, match=message):
            sr._assert_refresh_predecessor("session", state, _recorded_parked())

    def test_egress_verification_rejects_non_object_network_state(self):
        state = _refresh_state(networks={dc.EGRESS_NETWORK: "invalid"})

        with pytest.raises(sr.SessionError, match="invalid egress network state"):
            sr._validate_refresh_egress(_recorded_parked(), state)

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

    def test_refresh_parking_without_egress_only_uses_shared_primitive(self):
        parked = sr.ParkedSession("session", "backup", False)
        with patch.object(sr, "_park_session_container") as park:
            sr._park_refresh_container(parked)

        park.assert_called_once_with(parked)

    def test_refresh_parking_reports_egress_detach_failure(self):
        parked = sr.ParkedSession("session", "backup", False, reconnect_egress=True)
        failed = subprocess.CompletedProcess([], 1, "", "network busy")
        with (
            patch.object(sr, "_park_session_container"),
            patch.object(sr, "_run", return_value=failed),
            pytest.raises(sr.SessionError, match="network busy"),
        ):
            sr._park_refresh_container(parked)

    @pytest.mark.parametrize(
        ("state", "message"),
        [
            (None, "disappeared"),
            (_refresh_state(running=True), "still running"),
            (
                _refresh_state(networks={dc.EGRESS_NETWORK: {}}),
                "still attached to egress",
            ),
        ],
    )
    def test_refresh_park_verification_rejects_incomplete_park(self, state, message):
        parked = sr.ParkedSession(
            "session",
            "backup",
            True,
            project_id="project-id",
            reconnect_egress=True,
        )
        with (
            patch.object(sr, "_strict_refresh_container", return_value=state),
            pytest.raises(sr.SessionError, match=message),
        ):
            sr._verify_refresh_park(parked)

    def test_incomplete_park_restores_original_when_rename_never_landed(self):
        parked = sr.ParkedSession(
            "session",
            "backup",
            True,
            project_id="project-id",
        )
        with (
            patch.object(
                sr,
                "_strict_refresh_container",
                side_effect=[None, _refresh_state(running=False)],
            ),
            patch.object(sr, "_start_session_container") as start,
        ):
            sr._restore_incomplete_park(parked)

        start.assert_called_once_with("session")

    def test_incomplete_park_reports_reconnect_failure(self):
        parked = sr.ParkedSession(
            "session",
            "backup",
            True,
            project_id="project-id",
            reconnect_egress=True,
        )
        failed = subprocess.CompletedProcess([], 1, "", "network missing")
        with (
            patch.object(sr, "_strict_refresh_container", return_value=_refresh_state()),
            patch.object(sr, "_run", return_value=failed),
            pytest.raises(sr.SessionError, match="network missing"),
        ):
            sr._restore_incomplete_park(parked)

    def test_incomplete_park_verifies_reconnect_landed(self):
        parked = _recorded_parked(reconnect_egress=True, egress_network_id="egress-id")
        state = _refresh_state()
        state.update({"Id": "prior-id", "Image": "sha256:prior"})
        connected = subprocess.CompletedProcess([], 0, "", "")
        with (
            patch.object(sr, "_strict_refresh_container", side_effect=[state, state]),
            patch.object(sr, "_run", return_value=connected),
            pytest.raises(sr.SessionError, match="did not reconnect"),
        ):
            sr._restore_incomplete_park(parked)

    def test_incomplete_park_rejects_unauthorized_egress(self):
        parked = _recorded_parked()
        state = _refresh_state(networks={dc.EGRESS_NETWORK: {}})
        state.update({"Id": "prior-id", "Image": "sha256:prior"})
        with (
            patch.object(sr, "_strict_refresh_container", return_value=state),
            pytest.raises(sr.SessionError, match="unauthorized egress"),
        ):
            sr._restore_incomplete_park(parked)

    def test_incomplete_park_rejects_canonical_egress_drift(self):
        parked = _recorded_parked(reconnect_egress=True)
        state = _refresh_state()
        state.update({"Id": "prior-id", "Image": "sha256:prior"})
        with (
            patch.object(sr, "_strict_refresh_container", side_effect=[None, state]),
            pytest.raises(sr.SessionError, match="egress state changed"),
        ):
            sr._restore_incomplete_park(parked)

    def test_refresh_plan_rejects_labels_that_differ_from_issuance(self, tmp_path: Path):
        with (
            patch.object(sr, "_strict_refresh_container", return_value=_refresh_state()),
            patch.object(sr, "_refresh_project_id", return_value="project-id"),
            patch.object(
                runtime_spec,
                "labels",
                return_value=("booley.project-id=project-id", "required=yes"),
            ),
            pytest.raises(sr.SessionError, match="labels differ"),
        ):
            sr.plan_session_refresh(tmp_path, SimpleNamespace())

    def test_refresh_parking_returns_none_when_session_is_absent(self, tmp_path: Path):
        with patch.object(sr, "_strict_refresh_container", return_value=None):
            assert sr.park_session_for_refresh(tmp_path, SimpleNamespace()) is None

    def test_restore_refresh_removes_candidate_before_restoring_backup(self):
        parked = sr.ParkedSession(
            "session",
            "backup",
            True,
            project_id="project-id",
        )
        with (
            patch.object(sr, "_strict_refresh_container", return_value=_refresh_state()),
            patch.object(sr, "_refresh_candidate_matches", return_value=True),
            patch.object(sr, "_remove_session_candidate") as remove,
            patch.object(sr, "_restore_incomplete_park") as restore,
        ):
            sr.restore_refresh_session(parked, candidate_issuance=SimpleNamespace())

        remove.assert_called_once_with("session")
        restore.assert_called_once_with(parked)

    def test_restore_rejects_two_copies_of_recorded_predecessor(self):
        parked = _recorded_parked()
        predecessor = _refresh_state()
        predecessor.update({"Id": "prior-id", "Image": "sha256:prior"})
        with (
            patch.object(sr, "_strict_refresh_container", return_value=predecessor),
            pytest.raises(sr.SessionError, match="both canonical and recovery"),
        ):
            sr.restore_refresh_session(parked)

    def test_restore_rejects_canonical_predecessor_egress_drift(self):
        parked = _recorded_parked(reconnect_egress=True)
        predecessor = _refresh_state()
        predecessor.update({"Id": "prior-id", "Image": "sha256:prior"})
        with (
            patch.object(sr, "_strict_refresh_container", side_effect=[predecessor, None]),
            pytest.raises(sr.SessionError, match="egress state changed"),
        ):
            sr.restore_refresh_session(parked)

    @pytest.mark.parametrize(
        ("state", "message"),
        [
            (None, "is missing"),
            (_refresh_state(running=True), "running state is incorrect"),
            (_refresh_state(), "egress state is incorrect"),
        ],
    )
    def test_restored_predecessor_verification_rejects_state_drift(self, state, message):
        reconnect = message == "egress state is incorrect"
        parked = sr.ParkedSession(
            "session", "backup", False, project_id="project-id", reconnect_egress=reconnect
        )
        with (
            patch.object(sr, "_strict_refresh_container", return_value=state),
            pytest.raises(sr.SessionError, match=message),
        ):
            sr.verify_restored_refresh_session(parked)

    def test_validate_blocks_while_refresh_recovery_is_pending(self, tmp_path: Path):
        with (
            patch("booley.harness.session_refresh.has_pending_refresh", return_value=True),
            patch.object(sr, "_load_spec") as load_spec,
            pytest.raises(sr.SessionError, match="recovery is pending"),
        ):
            sr.validate(tmp_path)

        load_spec.assert_not_called()

    def test_discard_refresh_session_validates_and_removes_backup(self):
        parked = sr.ParkedSession(
            "session",
            "backup",
            False,
            project_id="project-id",
        )
        with (
            patch.object(sr, "_strict_refresh_container", return_value=_refresh_state()),
            patch.object(sr, "_remove_refresh_predecessor") as discard,
        ):
            sr.discard_refresh_session(parked)

        discard.assert_called_once_with(parked.backup)

    def test_durable_refresh_cleanup_reports_predecessor_removal_failure(self):
        parked = sr.ParkedSession(
            "session",
            "backup",
            False,
            project_id="project-id",
            container_id="prior-id",
            image_id="sha256:prior",
        )
        state = _refresh_state()
        state["Id"] = "prior-id"
        state["Image"] = "sha256:prior"
        failed = subprocess.CompletedProcess([], 1, "", "container busy")
        with (
            patch.object(sr, "_strict_refresh_container", return_value=state),
            patch.object(sr, "_run", return_value=failed),
            pytest.raises(sr.SessionError, match="container busy"),
        ):
            sr.discard_refresh_session(parked)

    def test_discard_refresh_candidate_removes_licensed_relay(self, tmp_path: Path):
        issuance = SimpleNamespace(license_profile="vivado", relay_image_id=None)
        relay = object()
        with (
            patch.object(sr, "_strict_refresh_container", return_value=_refresh_state()),
            patch.object(sr, "_refresh_project_id", return_value="project-id"),
            patch.object(sr, "_refresh_candidate_matches", return_value=True),
            patch.object(sr, "_remove_session_candidate") as remove,
            patch.object(sr, "_relay_resources", return_value=relay),
            patch.object(sr, "_remove_license_relay") as remove_relay,
        ):
            sr.discard_refresh_candidate(tmp_path, issuance)

        remove.assert_called_once_with(sr.session_container_name(tmp_path))
        remove_relay.assert_called_once_with(relay)

    def test_discard_refresh_candidate_preserves_unproven_identity(self, tmp_path: Path):
        issuance = SimpleNamespace(license_profile=None, relay_image_id=None)
        with (
            patch.object(sr, "_strict_refresh_container", return_value=_refresh_state()),
            patch.object(sr, "_refresh_project_id", return_value="project-id"),
            patch.object(sr, "_refresh_candidate_matches", return_value=False),
            patch.object(sr, "_remove_session_candidate") as remove,
            pytest.raises(sr.SessionError, match=r"cannot prove.*replacement"),
        ):
            sr.discard_refresh_candidate(tmp_path, issuance)

        remove.assert_not_called()

    def test_rebuild_rejects_existing_recovery_container(self):
        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            pytest.raises(sr.SessionError, match="recovery container"),
        ):
            sr._park_session_for_rebuild("session")


class TestUp:
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
        from booley.harness import image_lifecycle

        workspace, _run = wired
        project_dir = workspace / ".booley_project"
        project_dir.mkdir()
        (project_dir / "booley.toml").write_text(
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
            patch.object(sr, "_container_matches_issuance", return_value=False),
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
        assert hooks == [dc.mcp_post_start_command()]

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
        exists = iter([True, False, False, True])
        with (
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
                expected_payload_fingerprint="payload-123",
            )

        argvs = [_argv_of(c) for c in run.call_args_list]
        assert ["docker", "rename", name, backup] in argvs
        assert ["docker", "rm", "-f", name] in argvs
        assert ["docker", "rename", backup, name] in argvs
        assert ["docker", "rm", "-f", backup] not in argvs

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
                expected_payload_fingerprint="payload-123",
            )

        assert ["docker", "rm", "-f", name] in [_argv_of(call) for call in run.call_args_list]

    def test_fresh_refresh_rejects_unverified_labels_or_networks(self, wired):
        workspace, run = wired
        name = sr.session_container_name(workspace)
        exists = iter([False, False, True])
        with (
            patch.object(sr.idk, "container_exists", side_effect=lambda _n: next(exists)),
            patch.object(sr, "_container_matches_issuance", return_value=False),
            patch.object(sr, "verify_refreshed_session") as verify_payload,
            pytest.raises(sr.SessionError, match="labels or network topology"),
        ):
            sr.up(workspace, rebuild=True, expected_image_id="sha256:fresh")

        verify_payload.assert_not_called()
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

    def test_licensed_refresh_fails_before_parking_existing_runtime(self, wired):
        workspace, run = wired
        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr, "_requested_issued_license", return_value=object()),
            pytest.raises(sr.SessionError, match="licensed relay topology"),
        ):
            sr.up(workspace, rebuild=True)

        assert not any(_argv_of(call)[:2] == ["docker", "rename"] for call in run.call_args_list)

    def test_refresh_parking_detaches_running_session_from_egress(self, wired):
        workspace, run = wired
        name = sr.session_container_name(workspace)
        backup = f"{name}-pre-refresh"
        initial = _refresh_state(running=True, networks={dc.EGRESS_NETWORK: {}})
        parked_state = _refresh_state(labels=initial["Config"]["Labels"])
        with (
            patch.object(
                sr,
                "_strict_refresh_container",
                side_effect=[initial, None, parked_state],
            ),
            patch.object(sr, "_refresh_project_id", return_value="project-id"),
            patch.object(
                runtime_spec,
                "labels",
                return_value=(
                    "booley.project-id=project-id",
                    "booley.license-profile=none",
                ),
            ),
            patch.object(sr, "_relay_objects_exist", return_value=False),
        ):
            parked = sr.park_session_for_refresh(
                workspace,
                SimpleNamespace(license_profile=None, relay_image_id=None),
            )

        assert parked == sr.ParkedSession(
            name,
            backup,
            True,
            project_id="project-id",
            reconnect_egress=True,
        )
        argvs = [_argv_of(call) for call in run.call_args_list]
        assert ["docker", "stop", name] in argvs
        assert ["docker", "rename", name, backup] in argvs
        assert ["docker", "network", "disconnect", dc.EGRESS_NETWORK, backup] in argvs

    def test_refresh_restore_reconnects_exact_parked_session(self, wired):
        workspace, run = wired
        name = sr.session_container_name(workspace)
        parked = sr.ParkedSession(
            name,
            f"{name}-pre-refresh",
            True,
            project_id="project-id",
            reconnect_egress=True,
        )
        state = {
            "Config": {
                "Labels": {
                    "booley.role": "interactive",
                    "booley.project-id": "project-id",
                }
            },
            "State": {"Running": False},
            "NetworkSettings": {"Networks": {}},
        }
        reconnected = {
            **state,
            "NetworkSettings": {"Networks": {dc.EGRESS_NETWORK: {}}},
        }
        with patch.object(
            sr,
            "_strict_refresh_container",
            side_effect=[None, state, reconnected],
        ):
            sr.restore_refresh_session(parked)

        argvs = [_argv_of(call) for call in run.call_args_list]
        assert [
            "docker",
            "network",
            "connect",
            dc.EGRESS_NETWORK,
            parked.backup,
        ] in argvs
        assert ["docker", "rename", parked.backup, name] in argvs
        assert ["docker", "start", name] in argvs

    def test_refresh_licensed_snapshot_refuses_before_docker_mutation(self, wired):
        workspace, run = wired
        state = {
            "Config": {
                "Labels": {
                    "booley.role": "interactive",
                    "booley.project-id": "project-id",
                    "booley.license-profile": "vivado",
                }
            },
            "State": {"Running": True},
            "NetworkSettings": {"Networks": {dc.EGRESS_NETWORK: {}}},
        }
        run.reset_mock()
        with (
            patch.object(sr, "_strict_refresh_container", return_value=state),
            patch.object(sr, "_refresh_project_id", return_value="project-id"),
            patch.object(
                runtime_spec,
                "labels",
                return_value=(
                    "booley.project-id=project-id",
                    "booley.license-profile=vivado",
                ),
            ),
            pytest.raises(sr.SessionError, match="licensed relay topology"),
        ):
            sr.park_session_for_refresh(
                workspace,
                SimpleNamespace(license_profile="vivado", relay_image_id="sha256:relay"),
            )

        run.assert_not_called()

    def test_refresh_parking_reports_trigger_and_failed_compensation(self, wired):
        workspace, _run = wired
        name = sr.session_container_name(workspace)
        state = {
            "Config": {
                "Labels": {
                    "booley.role": "interactive",
                    "booley.project-id": "project-id",
                    "booley.license-profile": "none",
                }
            },
            "State": {"Running": True},
            "NetworkSettings": {"Networks": {dc.EGRESS_NETWORK: {}}},
        }
        parking_error = sr.SessionError("egress detach failed")
        with (
            patch.object(sr, "_strict_refresh_container", side_effect=[state, None]),
            patch.object(sr, "_refresh_project_id", return_value="project-id"),
            patch.object(
                runtime_spec,
                "labels",
                return_value=(
                    "booley.project-id=project-id",
                    "booley.license-profile=none",
                ),
            ),
            patch.object(sr, "_relay_objects_exist", return_value=False),
            patch.object(sr, "_park_refresh_container", side_effect=parking_error),
            patch.object(
                sr,
                "_restore_incomplete_park",
                side_effect=sr.SessionError("restore rename failed"),
            ),
            pytest.raises(sr.SessionError, match="rollback was incomplete") as raised,
        ):
            sr.park_session_for_refresh(
                workspace,
                SimpleNamespace(license_profile=None, relay_image_id=None),
            )

        assert "egress detach failed" in str(raised.value)
        assert "restore rename failed" in str(raised.value)
        assert name in str(raised.value)
        assert raised.value.__cause__ is parking_error

    def test_image_override_cannot_bypass_host_issued_spec(self, wired):
        workspace, _run = wired
        with pytest.raises(sr.SessionError, match="cannot bypass"):
            sr.up(workspace, image_override="fresh-booley-image")

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
                "booley.eda.provisioning.runtime_spec.requested_license",
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
                "booley.eda.provisioning.runtime_spec.requested_license",
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
                "booley.eda.provisioning.runtime_spec.requested_license",
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
                "booley.eda.provisioning.runtime_spec.requested_license",
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
                "booley.eda.provisioning.runtime_spec.requested_license",
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
                "booley.eda.provisioning.runtime_spec.requested_license",
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
                "booley.eda.provisioning.runtime_spec.requested_license",
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
            assert sr.down(workspace) is True
        remove.assert_called_once_with(relay)


class TestPreflight:
    def test_missing_network_names_booley_init(self, workspace: Path):
        _write_spec(workspace, _spec())
        with (
            patch.object(sr.idk, "network_exists", return_value=False),
            pytest.raises(sr.SessionError, match="booley init"),
        ):
            sr.up(workspace)

    def test_missing_image_names_booley_init(self, workspace: Path):
        _write_spec(workspace, _spec())
        with (
            patch(
                "booley.eda.provisioning.runtime_spec.validate",
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
            patch("booley.eda.provisioning.runtime_spec.requested_license", return_value=profile),
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


class TestImageDriftWarning:
    """F-6: [sandbox].image changed after init leaves the untracked spec frozen
    on the old image, and `session up` would silently start it. doctor already
    WARNs on the drift; `up` must surface it inline (warn only, never rewrite
    the spec)."""

    def _set_toml_image(self, workspace: Path, image: str) -> None:
        pdir = workspace / ".booley_project"
        pdir.mkdir()
        (pdir / "booley.toml").write_text(f'[sandbox]\nimage = "{image}"\n', encoding="utf-8")

    def _up_running(self, workspace: Path) -> None:
        with (
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr.idk, "container_running", return_value=True),
        ):
            sr.up(workspace)

    def test_spec_vs_toml_drift_warns_and_names_the_fix(self, wired, caplog, monkeypatch):
        from booley.runtime.project_dir import reset_cache

        workspace, _run = wired
        # The spec on disk says the default 'booley-sandbox'; the toml has
        # since been pointed at a project image.
        self._set_toml_image(workspace, "i2c-booley-sandbox")
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(workspace / ".booley_project"))
        reset_cache()

        self._up_running(workspace)

        assert "!= [sandbox].image 'i2c-booley-sandbox'" in caplog.text
        assert "booley init --seed" in caplog.text
        assert "booley session down" in caplog.text

    def test_matching_image_is_silent(self, wired, caplog):
        workspace, _run = wired
        self._set_toml_image(workspace, "booley-sandbox")  # == spec image

        self._up_running(workspace)

        assert "[sandbox].image" not in caplog.text

    def test_pinned_digest_matching_configured_tag_is_silent(
        self, workspace: Path, caplog, monkeypatch
    ):
        from booley.harness import init_cmd

        digest = "sha256:" + "a" * 64
        monkeypatch.setattr(
            init_cmd, "project_sandbox_image", lambda _root: "booley-sandbox-riscv"
        )
        monkeypatch.setattr(
            sr.idk,
            "image_id",
            lambda image: digest if image in {digest, "booley-sandbox-riscv"} else None,
        )

        sr._warn_on_image_drift({"image": digest}, workspace)

        assert "[sandbox].image" not in caplog.text

    def test_pinned_digest_different_from_configured_tag_warns(
        self, workspace: Path, caplog, monkeypatch
    ):
        from booley.harness import init_cmd

        digest = "sha256:" + "a" * 64
        monkeypatch.setattr(
            init_cmd, "project_sandbox_image", lambda _root: "booley-sandbox-riscv"
        )
        monkeypatch.setattr(
            sr.idk,
            "image_id",
            lambda image: digest if image == digest else "sha256:" + "b" * 64,
        )

        sr._warn_on_image_drift({"image": digest}, workspace)

        assert "!= [sandbox].image 'booley-sandbox-riscv'" in caplog.text

    def test_no_project_config_is_silent(self, wired, caplog):
        # No .booley_project at all: the resolver falls back to the base image,
        # which is exactly what the generated spec carries — no drift.
        workspace, _run = wired

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

    def test_mismatch_warns_and_names_the_fix(self, workspace, caplog):
        from booley.harness import image_lifecycle

        result = image_lifecycle.LifecycleResult(
            "booley-sandbox", "sha256:old", image_lifecycle.Status.STALE
        )
        with patch.object(image_lifecycle, "reconcile", return_value=result) as reconcile:
            sr._warn_on_stale_booley_bake(workspace)

        reconcile.assert_called_once_with(
            image_lifecycle.ProjectImageScope(workspace),
            image_lifecycle.Intent.CHECK,
        )
        assert "stale Booley code" in caplog.text
        assert "booley session refresh" in caplog.text

    def test_external_image_is_silent(self, workspace, caplog):
        from booley.harness import image_lifecycle

        result = image_lifecycle.LifecycleResult(
            "custom/image", None, image_lifecycle.Status.EXTERNAL
        )
        with patch.object(image_lifecycle, "reconcile", return_value=result):
            sr._warn_on_stale_booley_bake(workspace)
        assert "stale Booley code" not in caplog.text

    def test_match_is_silent(self, workspace, caplog):
        from booley.harness import image_lifecycle

        result = image_lifecycle.LifecycleResult(
            "booley-sandbox", "sha256:new", image_lifecycle.Status.CURRENT
        )
        with patch.object(image_lifecycle, "reconcile", return_value=result):
            sr._warn_on_stale_booley_bake(workspace)
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
        with patch.object(sr.idk, "container_exists", return_value=False):
            assert sr.down(workspace) is False

    def test_down_never_removes_the_issuance_image_keeper(self, workspace: Path):
        relay = SimpleNamespace(relay_container="relay")
        with (
            patch.object(sr, "_relay_resources", return_value=relay),
            patch.object(sr, "_relay_objects_exist", return_value=False),
            patch.object(sr.idk, "container_exists", return_value=True),
            patch.object(sr, "_run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0)
            assert sr.down(workspace) is True
        commands = [_argv_of(call) for call in run.call_args_list]
        assert ["docker", "stop", sr.session_container_name(workspace)] in commands
        assert not any(command[:3] == ["docker", "image", "rm"] for command in commands)

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


class TestConflictingVscodeSession:
    """VS Code's container and ours mount the same home-state volume rw, so a
    second live container puts two agents on one set of credentials."""

    def _patch_docker(self, monkeypatch, names: str, folders: dict[str, str]):
        def fake_run(argv, **_kw):
            if argv[:2] == ["docker", "ps"]:
                return subprocess.CompletedProcess([], 0, names, "")
            if argv[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess([], 0, folders.get(argv[2], ""), "")
            return subprocess.CompletedProcess([], 1, "", "")

        monkeypatch.setattr(sr, "_run", fake_run)

    def test_detects_vscode_container_for_this_folder(self, workspace: Path, monkeypatch):
        self._patch_docker(monkeypatch, "goofy_ellis\n", {"goofy_ellis": str(workspace)})
        assert sr.conflicting_vscode_session(workspace) == "goofy_ellis"

    def test_ignores_our_own_container(self, workspace: Path, monkeypatch):
        ours = sr.session_container_name(workspace)
        self._patch_docker(monkeypatch, f"{ours}\n", {ours: str(workspace)})
        assert sr.conflicting_vscode_session(workspace) is None

    def test_ignores_a_vscode_container_for_another_folder(self, workspace: Path, monkeypatch):
        self._patch_docker(monkeypatch, "other\n", {"other": str(workspace.parent / "elsewhere")})
        assert sr.conflicting_vscode_session(workspace) is None

    def test_drive_letter_case_does_not_defeat_the_match(self, workspace: Path, monkeypatch):
        # Dev Containers stamps the label with whatever case the host used.
        self._patch_docker(monkeypatch, "vsc\n", {"vsc": str(workspace).upper()})
        assert sr.conflicting_vscode_session(workspace) == "vsc"

    def test_unlabelled_container_is_not_a_conflict(self, workspace: Path, monkeypatch):
        self._patch_docker(monkeypatch, "plain\n", {"plain": ""})
        assert sr.conflicting_vscode_session(workspace) is None

    def test_docker_failure_is_not_a_conflict(self, workspace: Path, monkeypatch):
        monkeypatch.setattr(
            sr, "_run", lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "")
        )
        assert sr.conflicting_vscode_session(workspace) is None

    def test_strict_probe_fails_closed_when_docker_inventory_fails(
        self, workspace: Path, monkeypatch
    ):
        monkeypatch.setattr(sr, "_docker_stdout", lambda *_args: None)

        with pytest.raises(sr.SessionError, match="cannot inventory"):
            sr.strict_conflicting_vscode_session(workspace)

    def test_strict_probe_detects_vscode_container(self, workspace: Path, monkeypatch):
        raw = json.dumps(
            [
                {
                    "Config": {
                        "Labels": {
                            "booley.role": "interactive",
                            "devcontainer.local_folder": str(workspace),
                        }
                    }
                }
            ]
        )
        monkeypatch.setattr(
            sr,
            "_strict_running_interactive_states",
            lambda: [("vscode-owned", raw)],
        )

        assert sr.strict_conflicting_vscode_session(workspace) == "vscode-owned"

    def test_strict_probe_ignores_our_container(self, workspace: Path, monkeypatch):
        ours = sr.session_container_name(workspace)
        monkeypatch.setattr(
            sr,
            "_strict_running_interactive_states",
            lambda: [(ours, json.dumps([_refresh_state()]))],
        )

        assert sr.strict_conflicting_vscode_session(workspace) is None

    def test_strict_probe_rejects_incomplete_container_labels(self, workspace: Path, monkeypatch):
        raw = json.dumps([{"Config": {"Labels": None}}])
        monkeypatch.setattr(
            sr,
            "_strict_running_interactive_states",
            lambda: [("unknown", raw)],
        )

        with pytest.raises(sr.SessionError, match="incomplete inspection"):
            sr.strict_conflicting_vscode_session(workspace)


class TestSessionsOnStaleImage:
    """F-9: `booley init` rebuilds the tag, but a container born from the old
    image keeps serving it — and the tag being unchanged is what hides that. The
    probe therefore compares resolved image IDs, and must stay silent (never
    raise) on any Docker hiccup."""

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

    def test_missing_docker_is_silent(self, workspace: Path, monkeypatch):
        def no_docker(*_a, **_k):
            raise FileNotFoundError("docker")

        monkeypatch.setattr(sr, "_run", no_docker)
        assert sr.sessions_on_stale_image(workspace, self.IMAGE) == []

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
        with (
            patch.object(sr, "up", return_value="booley-session-x"),
            patch("booley.harness.runtime_attachment.run_command") as run,
        ):
            run.return_value = SimpleNamespace(exit_code=0)
            sr.enter(workspace, cmd, tty=False)
        assert run.call_args.args[2] == cmd
        assert run.call_args.kwargs == {"tty": False}


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
            patch.object(sr, "up", return_value="booley-session-x"),
            patch("booley.harness.runtime_attachment.run_command") as run,
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

    def test_refresh_configures_progress_before_reconciling_image(self, tmp_path: Path):
        from booley.harness import auto_doctor, booley, session_refresh
        from booley.harness.booley import _build_parser
        from booley.harness.image_lifecycle import LifecycleResult, Status

        args = _build_parser().parse_args(["session", "refresh"])
        result = LifecycleResult("booley-sandbox", "sha256:fresh", Status.CHANGED)
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
            patch.object(auto_doctor, "due_reason", return_value=None),
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
        enter.assert_called_once_with(tmp_path, ["echo", "ready"], tty=True)

        with (
            patch.object(sr, "down", return_value=False),
            patch.object(sr, "status", return_value="stopped"),
            patch.object(sr, "validate", return_value="valid"),
            patch.object(sr, "prepare", return_value="prepared"),
        ):
            assert booley._session_down(SimpleNamespace(), tmp_path) == 0
            assert booley._session_status(SimpleNamespace(), tmp_path) == 0
            assert booley._session_validate(SimpleNamespace(), tmp_path) == 0
            assert booley._session_prepare(SimpleNamespace(), tmp_path) == 0

        assert capsys.readouterr().out.splitlines() == [
            "no Session Runtime container for this folder",
            "stopped",
            "valid",
            "prepared",
        ]

    def test_refresh_refuses_active_vscode_before_reconciling_image(self, tmp_path: Path):
        from booley.harness import booley, session_refresh
        from booley.harness.booley import _build_parser

        args = _build_parser().parse_args(["session", "refresh"])
        with (
            patch.object(
                sr,
                "strict_conflicting_vscode_session",
                return_value="vscode-owned",
            ),
            patch.object(session_refresh, "inspect_refreshable_session_image") as refresh,
        ):
            assert booley._cmd_session(args, tmp_path) == 2

        refresh.assert_not_called()

    def test_refresh_runs_host_bootstrap_then_rebuilds_selected_flavor(
        self, tmp_path: Path, monkeypatch
    ):
        from booley.harness import bootstrap, init_cmd
        from booley.harness.image_lifecycle import (
            Intent,
            LifecycleResult,
            ProjectImageScope,
            Status,
        )

        expected = LifecycleResult(
            "booley-sandbox-riscv",
            "sha256:fresh",
            Status.CHANGED,
            changed_images=("booley-sandbox", "booley-sandbox-riscv"),
        )
        base = LifecycleResult(
            "booley-sandbox",
            "sha256:base",
            Status.CHANGED,
            changed_images=("booley-sandbox",),
        )
        calls = []
        bootstrap_result = bootstrap.BootstrapResult(
            Intent.REFRESH,
            (bootstrap.BootstrapFinding("host", bootstrap.BootstrapState.CHANGED, "ready"),),
            base_image=base,
        )
        monkeypatch.setattr(
            init_cmd,
            "reconcile_bootstrap",
            lambda intent, *, verbose=False: (
                calls.append(("bootstrap", intent, verbose)) or bootstrap_result
            ),
        )
        monkeypatch.setattr(
            init_cmd,
            "reconcile_images",
            lambda root, intent, *, verbose=False: (
                calls.append((root, intent, verbose)) or expected
            ),
        )

        assert init_cmd.refresh_session_image(tmp_path, verbose=True) is expected
        assert calls == [
            (ProjectImageScope(tmp_path), Intent.CHECK, True),
            ("bootstrap", Intent.REFRESH, True),
            (ProjectImageScope(tmp_path, base), Intent.REFRESH, True),
        ]

    def test_refresh_fails_when_host_bootstrap_cannot_converge(self, tmp_path: Path, monkeypatch):
        from booley.harness import bootstrap, init_cmd
        from booley.harness.image_lifecycle import Intent, LifecycleResult, Status

        monkeypatch.setattr(
            init_cmd,
            "reconcile_images",
            lambda *_args, **_kwargs: LifecycleResult(
                "booley-sandbox", "sha256:old", Status.CURRENT
            ),
        )
        monkeypatch.setattr(
            init_cmd,
            "reconcile_bootstrap",
            lambda *_args, **_kwargs: bootstrap.BootstrapResult(
                Intent.REFRESH,
                (
                    bootstrap.BootstrapFinding(
                        "proxy", bootstrap.BootstrapState.ERROR, "foreign collision"
                    ),
                ),
            ),
        )

        with pytest.raises(RuntimeError, match="foreign collision"):
            init_cmd.refresh_session_image(tmp_path)

    def test_refresh_refuses_user_managed_image(self, tmp_path: Path, monkeypatch):
        from booley.harness import init_cmd
        from booley.harness.image_lifecycle import LifecycleResult, Status

        monkeypatch.setattr(
            init_cmd,
            "reconcile_images",
            lambda *_args, **_kwargs: LifecycleResult(
                "registry.example/custom:latest", None, Status.EXTERNAL
            ),
        )
        with pytest.raises(RuntimeError, match="user-managed"):
            init_cmd.refresh_session_image(tmp_path)

    def test_spec_snapshot_restores_issuance_and_keeper(self, tmp_path: Path, monkeypatch):
        from booley.eda.provisioning import runtime_spec
        from booley.harness import init_cmd

        spec_path = dc.devcontainer_path(tmp_path)
        spec_path.parent.mkdir(parents=True)
        old_id = "sha256:" + "a" * 64
        old_spec = json.dumps({"image": old_id}).encode()
        spec_path.write_bytes(old_spec)
        stamp_path = tmp_path / "host-stamp.json"
        stamp_path.write_bytes(b"old stamp\n")
        monkeypatch.setattr(runtime_spec, "stamp_path", lambda _root: stamp_path)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            init_cmd.subprocess,
            "run",
            lambda argv, **_kwargs: (
                calls.append(argv) or subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            ),
        )

        snapshot = init_cmd.capture_session_spec(tmp_path)
        spec_path.write_text('{"image": "sha256:new"}', encoding="utf-8")
        stamp_path.write_text("new stamp\n", encoding="utf-8")
        init_cmd.restore_session_spec(tmp_path, snapshot)

        assert spec_path.read_bytes() == old_spec
        assert stamp_path.read_bytes() == b"old stamp\n"
        assert calls == [["docker", "tag", old_id, runtime_spec.keeper_image(tmp_path)]]

    def test_keeper_failure_still_restores_spec_and_stamp(self, tmp_path: Path, monkeypatch):
        from booley.eda.provisioning import runtime_spec
        from booley.harness import init_cmd

        spec_path = dc.devcontainer_path(tmp_path)
        spec_path.parent.mkdir(parents=True)
        old_id = "sha256:" + "a" * 64
        old_spec = json.dumps({"image": old_id}).encode()
        spec_path.write_bytes(old_spec)
        stamp_path = tmp_path / "host-stamp.json"
        stamp_path.write_bytes(b"old stamp\n")
        monkeypatch.setattr(runtime_spec, "stamp_path", lambda _root: stamp_path)
        monkeypatch.setattr(
            init_cmd.subprocess,
            "run",
            lambda argv, **_kwargs: subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="tag failed"
            ),
        )
        snapshot = init_cmd.capture_session_spec(tmp_path)
        spec_path.write_text('{"image": "sha256:new"}', encoding="utf-8")
        stamp_path.write_text("new stamp\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match="Session Image keeper: tag failed"):
            init_cmd.restore_session_spec(tmp_path, snapshot)

        assert spec_path.read_bytes() == old_spec
        assert stamp_path.read_bytes() == b"old stamp\n"

    def test_keeper_process_error_still_restores_snapshot_files(self, tmp_path: Path, monkeypatch):
        from booley.harness import init_cmd

        spec_path = tmp_path / "devcontainer.json"
        stamp_path = tmp_path / "stamp.json"
        snapshot = init_cmd.SessionSpecSnapshot(
            spec_path,
            b"old spec",
            0o644,
            stamp_path,
            b"old stamp",
            0o600,
            "sha256:old",
        )

        def missing_docker(*_args, **_kwargs):
            raise OSError("docker missing")

        monkeypatch.setattr(init_cmd.subprocess, "run", missing_docker)

        with pytest.raises(RuntimeError, match="Session Image keeper: docker missing"):
            init_cmd.restore_session_spec(tmp_path, snapshot)

        assert spec_path.read_bytes() == b"old spec"
        assert stamp_path.read_bytes() == b"old stamp"

    def test_snapshot_file_failures_are_aggregated(self, tmp_path: Path, monkeypatch):
        from booley.harness import init_cmd

        snapshot = init_cmd.SessionSpecSnapshot(
            tmp_path / "devcontainer.json",
            b"old spec",
            0o644,
            tmp_path / "stamp.json",
            b"old stamp",
            0o600,
            None,
        )
        restore = Mock(side_effect=[OSError("spec busy"), None])
        monkeypatch.setattr(init_cmd, "_restore_snapshot_file", restore)

        with pytest.raises(RuntimeError, match="Session spec: spec busy"):
            init_cmd.restore_session_spec(tmp_path, snapshot)

        assert restore.call_count == 2

    def test_runtime_probe_uses_isolated_import_and_exact_payload(self, tmp_path: Path):
        image_id = "sha256:" + "a" * 64
        completed = subprocess.CompletedProcess([], 0, stdout="payload-123\n", stderr="")
        with (
            patch.object(sr, "_docker_stdout", return_value=image_id),
            patch.object(sr, "_run", return_value=completed) as run,
        ):
            sr.verify_refreshed_session(tmp_path, image_id, "payload-123")

        argv = run.call_args.args[0]
        assert argv[:3] == ["docker", "exec", sr.session_container_name(tmp_path)]
        assert argv[3:6] == ["python3", "-I", "-c"]

    def test_runtime_probe_rejects_payload_mismatch(self, tmp_path: Path):
        image_id = "sha256:" + "a" * 64
        completed = subprocess.CompletedProcess([], 0, stdout="old-payload\n", stderr="")
        with (
            patch.object(sr, "_docker_stdout", return_value=image_id),
            patch.object(sr, "_run", return_value=completed),
            pytest.raises(sr.SessionError, match="payload does not match"),
        ):
            sr.verify_refreshed_session(tmp_path, image_id, "payload-123")

    def test_down_up_never_announces_persisted_stale_doctor_findings(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from booley.harness import auto_doctor, booley
        from booley.harness.booley import _build_parser

        monkeypatch.setattr(sr, "down", lambda _root: True)
        monkeypatch.setattr(sr, "session_container_name", lambda _root: "session")
        down_args = _build_parser().parse_args(["session", "down"])
        assert booley._cmd_session(down_args, tmp_path) == 0
        capsys.readouterr()

        monkeypatch.setattr(sr, "conflicting_vscode_session", lambda _root: None)
        monkeypatch.setattr(sr, "up", lambda *_args, **_kwargs: "session")
        monkeypatch.setattr(auto_doctor, "due_reason", lambda _root: "Doctor inputs changed")
        monkeypatch.setattr(
            auto_doctor,
            "consume_changed_summary",
            lambda *_args, **_kwargs: pytest.fail("stale Doctor summary was consumed"),
        )
        up_args = _build_parser().parse_args(["session", "up"])

        assert booley._cmd_session(up_args, tmp_path) == 0
        output = capsys.readouterr()
        assert "Automatic Doctor is running" in output.err
        assert "Doctor inputs changed" in output.err
        assert "Session Runtime ready: session" in output.out
