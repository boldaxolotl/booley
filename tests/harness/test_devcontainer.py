"""Tests for the Interactive Mode devcontainer spec generator (ADR 0018 WS0)."""

from __future__ import annotations

import json

import pytest

from booley.harness import devcontainer as dc

# ===========================================================================
# build_devcontainer_spec
# ===========================================================================


class TestBuildSpec:
    def test_core_runtime_fields(self):
        spec = dc.build_devcontainer_spec(dc.APP_NONE)
        assert spec["image"] == "booley-sandbox"
        assert spec["workspaceFolder"] == dc.WORK_DIR
        assert spec["workspaceMount"] == (
            f"source=${{localWorkspaceFolder}},target={dc.WORK_DIR},type=bind"
        )
        assert spec["remoteUser"] == "agent"
        # ADR 0028 Decision 11: container survives window close; the idle
        # reaper (not VS Code) is the sole lifecycle owner.
        assert spec["shutdownAction"] == "none"
        assert spec["updateRemoteUserUID"] is False

    def test_egress_network_and_reaper_label(self):
        spec = dc.build_devcontainer_spec(dc.APP_NONE)
        assert spec["runArgs"] == [
            "--network",
            dc.EGRESS_NETWORK,
            "--label",
            dc.INTERACTIVE_ROLE_LABEL,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(dc.SESSION_PIDS_LIMIT),
        ]

    def test_hardening_flags_present_for_every_app(self):
        """The Session Runtime hardening (ARCHITECTURE.md#security--trust-model)
        must guard the venue where the agent actually runs, for every app and
        regardless of the optional memory limit."""
        for app in dc.SUPPORTED_APPS:
            args = dc.build_devcontainer_spec(app, memory="8g")["runArgs"]
            assert args[args.index("--cap-drop") + 1] == "ALL"
            assert args[args.index("--security-opt") + 1] == "no-new-privileges"
            assert int(args[args.index("--pids-limit") + 1]) == dc.SESSION_PIDS_LIMIT

    def test_memory_limit_rendered_when_set(self):
        """ADR 0028 Decision 12: [sandbox] memory feeds a --memory run arg."""
        spec = dc.build_devcontainer_spec(dc.APP_NONE, memory="8g")
        args = spec["runArgs"]
        assert args[args.index("--memory") + 1] == "8g"

    def test_no_memory_limit_by_default(self):
        """Empty memory (the default) sets no limit — pre-0028 parity."""
        assert "--memory" not in dc.build_devcontainer_spec(dc.APP_NONE)["runArgs"]

    def test_oauth_token_forwarded_only_when_requested(self):
        """forward_oauth_token adds a ${localEnv:...} reference (never a baked
        value) for the Claude app, and nothing otherwise."""
        off = dc.build_devcontainer_spec(dc.APP_CLAUDE)["remoteEnv"]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in off
        on = dc.build_devcontainer_spec(dc.APP_CLAUDE, forward_oauth_token=True)["remoteEnv"]
        assert on["CLAUDE_CODE_OAUTH_TOKEN"] == "${localEnv:CLAUDE_CODE_OAUTH_TOKEN}"

    def test_oauth_token_not_forwarded_for_codex(self):
        """Codex ignores the Claude oauth token — never add it there."""
        env = dc.build_devcontainer_spec(dc.APP_CODEX, forward_oauth_token=True)["remoteEnv"]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

    def test_proxy_env_points_at_sidecar(self):
        env = dc.build_devcontainer_spec(dc.APP_NONE)["remoteEnv"]
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            assert env[key] == dc.PROXY_URL
        assert env["BOOLEY_MCP_MODE"] == "interactive"
        assert env["BOOLEY_PROJECT_DIR"] == dc.PROJECT_DIR_TARGET

    def test_agent_app_env_matches_app(self):
        assert (
            dc.build_devcontainer_spec(dc.APP_CLAUDE)["remoteEnv"]["BOOLEY_AGENT_APP"] == "claude"
        )
        assert dc.build_devcontainer_spec(dc.APP_NONE)["remoteEnv"]["BOOLEY_AGENT_APP"] == "none"

    def test_initialize_command_is_fixed_host_validator(self):
        for app in dc.SUPPORTED_APPS:
            assert dc.build_devcontainer_spec(app)["initializeCommand"] == [
                "booley",
                "session",
                "prepare",
                "--project-root",
                "${localWorkspaceFolder}",
            ]

    def test_project_dir_mount_default_source(self):
        spec = dc.build_devcontainer_spec(dc.APP_NONE)
        # Default is the co-located single-var form; nested defaults are NOT used
        # (the Dev Containers CLI mis-parses them into an invalid mount path).
        assert dc.DEFAULT_PROJECT_DIR_SOURCE == "${localWorkspaceFolder}/.booley_project"
        assert "${localEnv:" not in dc.DEFAULT_PROJECT_DIR_SOURCE
        assert spec["mounts"][0] == (
            f"source={dc.DEFAULT_PROJECT_DIR_SOURCE},target={dc.PROJECT_DIR_TARGET},type=bind"
        )

    def test_custom_project_dir_source(self):
        spec = dc.build_devcontainer_spec(
            dc.APP_NONE,
            project_dir_source="/host/proj/.booley_project",
        )
        assert spec["mounts"][0] == (
            f"source=/host/proj/.booley_project,target={dc.PROJECT_DIR_TARGET},type=bind"
        )

    def test_unknown_app_rejected(self):
        with pytest.raises(ValueError, match="unknown app"):
            dc.build_devcontainer_spec("emacs")

    def test_no_fixed_container_env_omits_container_env(self):
        spec = dc.build_devcontainer_spec(dc.APP_NONE)
        assert "containerEnv" not in spec

    def test_fixed_container_env_is_literal_host_policy(self):
        spec = dc.build_devcontainer_spec(
            dc.APP_NONE,
            fixed_container_env={"XILINXD_LICENSE_FILE": "2100@booley-license-xilinx"},
        )
        assert spec["containerEnv"] == {"XILINXD_LICENSE_FILE": "2100@booley-license-xilinx"}

    def test_local_timezone_is_forwarded_to_runtime_processes(self):
        spec = dc.build_devcontainer_spec(dc.APP_NONE, local_timezone="Asia/Tbilisi")

        assert spec["remoteEnv"]["BOOLEY_LOCAL_TIMEZONE"] == "Asia/Tbilisi"

    def test_host_skills_render_readonly_sidecar_binds(self):
        # [sandbox] mount_host_skills: each host skill rides in as its own
        # read-only bind at the sidecar; init has already resolved the real dir.
        spec = dc.build_devcontainer_spec(
            dc.APP_CLAUDE,
            host_skills=[("deslop", "/host/skills/deslop"), ("grill-me", "/host/x/grill-me")],
        )
        assert (
            f"source=/host/skills/deslop,target={dc.HOST_SKILLS_SIDECAR}/deslop,"
            "type=bind,readonly" in spec["mounts"]
        )
        assert (
            f"source=/host/x/grill-me,target={dc.HOST_SKILLS_SIDECAR}/grill-me,"
            "type=bind,readonly" in spec["mounts"]
        )

    def test_no_host_skills_adds_no_sidecar_bind(self):
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        assert not any(dc.HOST_SKILLS_SIDECAR in m for m in spec["mounts"])

    def test_mask_paths_render_readonly_empty_binds_over_work(self):
        # [sandbox] mask_paths: each entry hides a workspace subtree behind a
        # read-only bind of the always-empty host dir.
        spec = dc.build_devcontainer_spec(
            dc.APP_NONE,
            mask_paths=["secret/oracle", "notes.md"],
            mask_source="/home/u/.config/booley/empty-mask",
        )
        assert (
            f"source=/home/u/.config/booley/empty-mask,target={dc.WORK_DIR}/secret/oracle,"
            "type=bind,readonly" in spec["mounts"]
        )
        assert (
            f"source=/home/u/.config/booley/empty-mask,target={dc.WORK_DIR}/notes.md,"
            "type=bind,readonly" in spec["mounts"]
        )

    def test_mask_under_project_dir_masks_both_container_views(self):
        # .booley_project is visible TWICE (via /work and /booley-project), so
        # a subtree under it must be masked in both views or the bytes remain
        # readable through the second mount.
        spec = dc.build_devcontainer_spec(
            dc.APP_NONE,
            mask_paths=[".booley_project/oracle"],
            mask_source="/empty",
        )
        assert (
            f"source=/empty,target={dc.WORK_DIR}/.booley_project/oracle,type=bind,readonly"
            in spec["mounts"]
        )
        assert (
            f"source=/empty,target={dc.PROJECT_DIR_TARGET}/oracle,type=bind,readonly"
            in spec["mounts"]
        )

    def test_mask_mounts_come_after_the_project_dir_mount(self):
        # Mount order is the over-mount mechanism: docker applies
        # workspaceMount first, then `mounts` in order, so a mask listed
        # BEFORE the project-dir mount would be buried by it.
        spec = dc.build_devcontainer_spec(
            dc.APP_CLAUDE,
            mask_paths=[".booley_project/oracle", "lanes/b"],
            mask_source="/empty",
        )
        mounts = spec["mounts"]
        project_idx = mounts.index(
            f"source={dc.DEFAULT_PROJECT_DIR_SOURCE},target={dc.PROJECT_DIR_TARGET},type=bind"
        )
        mask_indices = [i for i, m in enumerate(mounts) if m.startswith("source=/empty,")]
        assert mask_indices  # both views + the plain /work mask
        assert all(i > project_idx for i in mask_indices)
        # Masks trail EVERY other mount (state volume, token seeds, ...).
        assert mask_indices == list(range(len(mounts) - len(mask_indices), len(mounts)))

    def test_mask_targets_are_normalized_posix(self):
        # Trailing slashes / "." segments must not leak into mount targets —
        # docker would treat "target=/work/x/" and "target=/work/x" as
        # different strings even though they name the same path.
        spec = dc.build_devcontainer_spec(
            dc.APP_NONE,
            mask_paths=["a/./b/", "c//d"],
            mask_source="/empty",
        )
        assert f"source=/empty,target={dc.WORK_DIR}/a/b,type=bind,readonly" in spec["mounts"]
        assert f"source=/empty,target={dc.WORK_DIR}/c/d,type=bind,readonly" in spec["mounts"]

    def test_no_mask_paths_leaves_spec_byte_identical(self):
        # The empty default must be a no-op: existing installs re-seed the
        # exact same spec they had before the knob existed.
        base = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        with_empty = dc.build_devcontainer_spec(dc.APP_CLAUDE, mask_paths=(), mask_source="")
        assert json.dumps(base) == json.dumps(with_empty)

    def test_mask_paths_without_mask_source_rejected(self):
        # Silently dropping masks would hand the agent exactly the visibility
        # the project configured away — misuse must be loud.
        with pytest.raises(ValueError, match="mask_source"):
            dc.build_devcontainer_spec(dc.APP_NONE, mask_paths=["secret"])


class TestMaskPathsValidation:
    """mask_paths_error guards the [sandbox].mask_paths knob shape."""

    def test_accepts_absent_and_valid_relative_paths(self):
        assert dc.mask_paths_error(None) is None
        assert dc.mask_paths_error([]) is None
        assert (
            dc.mask_paths_error(["secret/oracle", ".booley_project/lanes/b", "notes.md"]) is None
        )

    def test_rejects_non_list_and_non_string_entries(self):
        assert "list of strings" in dc.mask_paths_error("secret")
        assert "list of strings" in dc.mask_paths_error(["ok", 3])

    def test_rejects_absolute_dotdot_and_empty_entries(self):
        # Absolute paths and ".." segments would turn the privacy knob into a
        # container-layout editor; "." / "" name the whole workspace.
        for entry in ["/etc", "../outside", "a/../b", ".", "", "win\\path"]:
            error = dc.mask_paths_error([entry])
            assert error is not None and "workspace-relative" in error, entry

    def test_rejects_masking_the_whole_project_dir(self):
        # The session reads booley.toml through /booley-project — masking the
        # entire dir bricks the runtime rather than hiding a secret.
        for entry in [".booley_project", ".booley_project/"]:
            error = dc.mask_paths_error([entry])
            assert error is not None and "subtrees" in error, entry


class TestAppExtension:
    def test_claude_extension_pinned_workspace_side(self):
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        vscode = spec["customizations"]["vscode"]
        ext = "Anthropic.claude-code"
        assert vscode["extensions"] == [
            ext,
            "lramseyer.vaporview",
            "mshr-h.veriloghdl",
            "bitwisecook.tcl",
            "ms-vscode.live-server",
        ]
        assert vscode["settings"]["remote.extensionKind"] == {ext: ["workspace"]}
        assert vscode["settings"]["vaporview.wcp.enabled"] is True
        assert vscode["settings"]["vaporview.wcp.port"] == 54322

    def test_codex_extension(self):
        spec = dc.build_devcontainer_spec(dc.APP_CODEX)
        vscode = spec["customizations"]["vscode"]
        assert vscode["extensions"] == [
            "openai.chatgpt",
            "lramseyer.vaporview",
            "mshr-h.veriloghdl",
            "bitwisecook.tcl",
            "ms-vscode.live-server",
        ]
        assert vscode["settings"]["vaporview.wcp.enabled"] is True
        assert vscode["settings"]["vaporview.wcp.port"] == 54322

    def test_none_installs_no_agent_extension(self):
        # ADR 0035: waveform viewing isn't tied to an agent app, so "none"
        # (which previously emitted no customizations at all) carries the
        # app-agnostic extensions (VaporView + HDL highlighting) but no agent
        # extension. The WCP settings ride along unconditionally so bwave's
        # client can always reach the server.
        vscode = dc.build_devcontainer_spec(dc.APP_NONE)["customizations"]["vscode"]
        assert vscode["extensions"] == [
            "lramseyer.vaporview",
            "mshr-h.veriloghdl",
            "bitwisecook.tcl",
            "ms-vscode.live-server",
        ]
        assert vscode["settings"]["vaporview.wcp.enabled"] is True
        assert vscode["settings"]["vaporview.wcp.port"] == 54322

    def test_vaporview_extension_present_for_every_app(self):
        # The Waveform Viewer (ADR 0035) rides along regardless of agent app,
        # and its WCP server settings are always present.
        for app in dc.SUPPORTED_APPS:
            spec = dc.build_devcontainer_spec(app)
            vscode = spec["customizations"]["vscode"]
            assert "lramseyer.vaporview" in vscode["extensions"]
            assert vscode["settings"]["vaporview.wcp.enabled"] is True
            assert vscode["settings"]["vaporview.wcp.port"] == 54322

    def test_spec_installs_vaporview_detector(self):
        # The single detector booley doctor reads to surface a spec seeded
        # before the Waveform Viewer (ADR 0035) landed.
        for app in dc.SUPPORTED_APPS:
            assert dc.spec_installs_vaporview(dc.build_devcontainer_spec(app)) is True
        # Pre-ADR-0035 shapes: no customizations at all (app=none back then),
        # or an agent extension without VaporView / the WCP settings.
        assert dc.spec_installs_vaporview({}) is False
        legacy = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        legacy["customizations"]["vscode"]["extensions"] = ["Anthropic.claude-code"]
        assert dc.spec_installs_vaporview(legacy) is False

    def test_spec_installs_hdl_highlight_detector(self):
        # Doctor's detector for a spec seeded before the highlighting
        # extensions landed (extensions are spec-delivered, never image-baked).
        for app in dc.SUPPORTED_APPS:
            assert dc.spec_installs_hdl_highlight(dc.build_devcontainer_spec(app)) is True
        assert dc.spec_installs_hdl_highlight({}) is False
        # Any missing piece — either grammar extension or the constraint-file
        # associations that route .sdc/.xdc to Tcl — means a stale spec.
        legacy = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        legacy["customizations"]["vscode"]["extensions"] = [
            "Anthropic.claude-code",
            "lramseyer.vaporview",
        ]
        assert dc.spec_installs_hdl_highlight(legacy) is False
        for drop in ("mshr-h.veriloghdl", "bitwisecook.tcl"):
            spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
            spec["customizations"]["vscode"]["extensions"].remove(drop)
            assert dc.spec_installs_hdl_highlight(spec) is False
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        del spec["customizations"]["vscode"]["settings"]["files.associations"]["*.sdc"]
        assert dc.spec_installs_hdl_highlight(spec) is False

    def test_spec_installs_live_preview_detector(self):
        # Detect specs seeded before rendered HTML reports could be viewed in
        # the attached container window or before per-attach tunnel isolation
        # was added (F-14).
        for app in dc.SUPPORTED_APPS:
            assert dc.spec_installs_live_preview(dc.build_devcontainer_spec(app)) is True
        assert dc.spec_installs_live_preview({}) is False
        legacy = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        legacy["customizations"]["vscode"]["extensions"].remove("ms-vscode.live-server")
        assert dc.spec_installs_live_preview(legacy) is False
        for value in (None, True):
            stale = dc.build_devcontainer_spec(dc.APP_CLAUDE)
            settings = stale["customizations"]["vscode"]["settings"]
            if value is None:
                del settings["remote.restoreForwardedPorts"]
            else:
                settings["remote.restoreForwardedPorts"] = value
            assert dc.spec_installs_live_preview(stale) is False

        stale = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        del stale["customizations"]["vscode"]["settings"]["livePreview.portNumber"]
        assert dc.spec_installs_live_preview(stale) is False

        stale = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        stale["postAttachCommand"] = dc.vaporview_patch_command()
        assert dc.spec_installs_live_preview(stale) is False

    def test_live_preview_does_not_restore_stale_port_tunnels(self):
        # F-14: restored 3000/3001 tunnels can point the embedded browser at a
        # dead endpoint while Live Preview's newly allocated tunnel is healthy.
        for app in dc.SUPPORTED_APPS:
            settings = dc.build_devcontainer_spec(app)["customizations"]["vscode"]["settings"]
            assert settings["remote.restoreForwardedPorts"] is False
            assert settings["livePreview.portNumber"] == dc._LIVE_PREVIEW_SEED_PORT

    def test_live_preview_randomizer_runs_before_extension_patch(self):
        for app in dc.SUPPORTED_APPS:
            assert dc.build_devcontainer_spec(app)["postAttachCommand"] == (
                f"{dc.live_preview_port_command()} && {dc.vaporview_patch_command()}"
            )

    def test_constraint_files_associated_with_tcl(self):
        # SDC/XDC constraints are Tcl but the grammar doesn't claim the
        # suffixes; the spec's files.associations must, for every app.
        for app in dc.SUPPORTED_APPS:
            settings = dc.build_devcontainer_spec(app)["customizations"]["vscode"]["settings"]
            assert settings["files.associations"] == {"*.sdc": "tcl", "*.xdc": "tcl"}

    def test_python_terminal_autoactivation_disabled_for_every_app(self):
        # The runtime already has its Python stack system-wide.  Do not let a
        # synced Python extension discover a host-created workspace .venv and
        # inject a delayed activation command into interactive terminals.
        for app in dc.SUPPORTED_APPS:
            spec = dc.build_devcontainer_spec(app)
            settings = spec["customizations"]["vscode"]["settings"]
            assert settings["python-envs.terminal.autoActivationType"] == "off"
            assert settings["python.terminal.activateEnvironment"] is False
            assert dc.spec_disables_python_terminal_activation(spec) is True

    def test_python_terminal_autoactivation_detector_rejects_stale_specs(self):
        for key in dc._PYTHON_TERMINAL_SETTINGS:
            spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
            del spec["customizations"]["vscode"]["settings"][key]
            assert dc.spec_disables_python_terminal_activation(spec) is False

    def test_spec_installs_vaporview_rejects_wcp_drift(self):
        # Extension present but the WCP server disabled or on the wrong port
        # is just as dead to bwave's client as a missing install.
        for settings_patch in (
            {"vaporview.wcp.enabled": False},
            {"vaporview.wcp.port": 12345},
        ):
            spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
            spec["customizations"]["vscode"]["settings"].update(settings_patch)
            assert dc.spec_installs_vaporview(spec) is False

    def test_spec_installs_vaporview_requires_autostart_patch(self):
        # A spec that has the extension + WCP settings but predates the
        # postAttach auto-start patch is still dead to bwave: the setting is
        # application-scoped (Machine settings ignored) and the extension
        # activates lazily, so the server never binds. The guard must reject it
        # so doctor prompts a re-seed.
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        assert dc.spec_installs_vaporview(spec) is True
        del spec["postAttachCommand"]
        assert dc.spec_installs_vaporview(spec) is False
        spec["postAttachCommand"] = "echo not-the-patch"
        assert dc.spec_installs_vaporview(spec) is False

    def test_spec_wires_vaporview_autostart_postattach(self):
        # Every app (incl. "none") gets the manifest-patch hook on attach —
        # attach is the first lifecycle point where the extension exists on disk.
        for app in dc.SUPPORTED_APPS:
            spec = dc.build_devcontainer_spec(app)
            assert spec["postAttachCommand"] == dc.post_attach_command()
            assert "booley.incontainer_vaporview" in spec["postAttachCommand"]

    def test_extension_kind_pin_stays_agent_only(self):
        # VaporView's own manifest declares extensionKind ["workspace"] — it
        # must never appear in the remote.extensionKind pin.
        for app in (dc.APP_CLAUDE, dc.APP_CODEX):
            vscode = dc.build_devcontainer_spec(app)["customizations"]["vscode"]
            pin = vscode["settings"]["remote.extensionKind"]
            assert list(pin) == [dc._APP_EXTENSION[app]]


class TestAuthMount:
    def test_claude_auth_mounted_readonly_at_seed_sidecar(self):
        # Claude's creds are bound read-only at the sidecar SEED path (outside
        # ~/.claude/) — never directly onto the real file — so the in-container
        # refresh can rewrite the writable copy without hitting an RO mount.
        spec = dc.build_devcontainer_spec(
            dc.APP_CLAUDE,
            auth_token_source="/home/u/.claude/.credentials.json",
        )
        auth = spec["mounts"][1]
        assert "source=/home/u/.claude/.credentials.json" in auth
        assert f"target={dc.AGENT_HOME}/.claude-creds-seed.json" in auth
        assert auth.endswith(",readonly")
        # The real credentials path must NOT be a mount target (it lives on the
        # writable state volume; the seed is copied onto it at create time).
        assert all(
            f"target={dc.AGENT_HOME}/.claude/.credentials.json" not in m for m in spec["mounts"]
        )

    def test_claude_creds_seeded_into_writable_path_on_create(self):
        # Plain cp (not cp -n) re-seeds the freshest host token on every create;
        # chmod 600 keeps it agent-private after the copy.
        spec = dc.build_devcontainer_spec(
            dc.APP_CLAUDE,
            auth_token_source="/home/u/.claude/.credentials.json",
            mcp_start_command="python -m booley.incontainer_register",
        )
        pc = spec["postCreateCommand"]
        assert f"cp {dc.AGENT_HOME}/.claude-creds-seed.json" in pc
        assert f"chmod 600 {dc.AGENT_HOME}/.claude/.credentials.json" in pc
        # Must be a fresh copy each create — no no-clobber on the rotating token.
        assert f"cp -n {dc.AGENT_HOME}/.claude-creds-seed.json" not in pc

    def test_codex_auth_is_seeded_not_bound_readonly(self):
        # Codex REWRITES ~/.codex/auth.json when a subscription login's tokens
        # refresh. A read-only bind on that path makes the refresh write fail and
        # 401s the session, so Codex gets the same sidecar-seed + writable-copy
        # treatment as Claude.
        spec = dc.build_devcontainer_spec(
            dc.APP_CODEX,
            auth_token_source="/home/u/.codex/auth.json",
        )
        auth_mount = spec["mounts"][1]
        assert f"target={dc.AGENT_HOME}/.codex-auth-seed.json" in auth_mount
        assert auth_mount.endswith(",readonly")  # the SEED is read-only, not the target
        # ...and the writable copy is made on both create and start.
        for hook in (spec["postCreateCommand"], spec["postStartCommand"]):
            assert (
                f"cp {dc.AGENT_HOME}/.codex-auth-seed.json {dc.AGENT_HOME}/.codex/auth.json"
                in hook
            )
        assert f"chmod 600 {dc.AGENT_HOME}/.codex/auth.json" in spec["postCreateCommand"]

    def test_no_auth_when_source_absent(self):
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        # project-dir mount + persistent state volume (no auth)
        assert len(spec["mounts"]) == 2
        assert all("type=bind" in m or "type=volume" in m for m in spec["mounts"])

    def test_no_auth_for_app_none(self):
        # "none" has no auth target and no persistent state dir, so only the
        # project-dir mount remains.
        spec = dc.build_devcontainer_spec(
            dc.APP_NONE,
            auth_token_source="/whatever",
        )
        assert len(spec["mounts"]) == 1


class TestTokenSeedMount:
    """The `booley auth` credential rides in as its own read-only sidecar.

    The remoteEnv ${localEnv:...} route can't deliver it to VS Code's "Reopen
    in Container" (VS Code resolves localEnv against its own process env), so
    the stored file is mounted for incontainer_register to apply on every
    container start.
    """

    def test_claude_token_seed_mounted_readonly_outside_state_dir(self):
        spec = dc.build_devcontainer_spec(
            dc.APP_CLAUDE,
            token_seed_source="/home/u/.config/booley/claude-oauth-token",
        )
        seed = next(m for m in spec["mounts"] if "oauth-token-seed" in m)
        assert "source=/home/u/.config/booley/claude-oauth-token" in seed
        assert f"target={dc.AGENT_HOME}/.booley-oauth-token-seed" in seed
        assert seed.endswith(",readonly")
        # Sibling of ~/.claude, never nested under it: the state volume owns
        # that dir and would shadow a seed placed inside it.
        assert f"target={dc.AGENT_HOME}/.claude/" not in seed

    def test_codex_token_seed_mounted_readonly_outside_state_dir(self):
        spec = dc.build_devcontainer_spec(
            dc.APP_CODEX,
            token_seed_source="/home/u/.config/booley/openai-api-key",
        )
        seed = next(m for m in spec["mounts"] if "api-key-seed" in m)
        assert f"target={dc.AGENT_HOME}/.booley-api-key-seed" in seed
        assert seed.endswith(",readonly")
        assert f"target={dc.AGENT_HOME}/.codex/" not in seed

    def test_no_token_seed_by_default(self):
        for app in dc.SUPPORTED_APPS:
            spec = dc.build_devcontainer_spec(app)
            assert all("token-seed" not in m and "api-key-seed" not in m for m in spec["mounts"])

    def test_no_token_seed_for_app_none(self):
        spec = dc.build_devcontainer_spec(dc.APP_NONE, token_seed_source="/whatever")
        assert len(spec["mounts"]) == 1

    def test_token_value_never_baked_into_spec(self):
        # The spec must carry only the mount reference — never file contents.
        spec = dc.build_devcontainer_spec(
            dc.APP_CLAUDE,
            token_seed_source="/home/u/.config/booley/claude-oauth-token",
            forward_oauth_token=True,
        )
        assert "sk-ant-" not in json.dumps(spec)

    def test_spec_mounts_token_seed_detector(self):
        # The single detector booley doctor reads to surface a spec seeded
        # before the credential was stored.
        with_seed = dc.build_devcontainer_spec(dc.APP_CLAUDE, token_seed_source="/t")
        assert dc.spec_mounts_token_seed(with_seed) is True
        without = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        assert dc.spec_mounts_token_seed(without) is False
        assert dc.spec_mounts_token_seed(dc.build_devcontainer_spec(dc.APP_NONE)) is None

    def test_spec_mounts_target_detector(self):
        spec = dc.build_devcontainer_spec(
            dc.APP_NONE,
            trusted_eda_mounts=(("/host/pdk", "/opt/pdk"),),
        )

        assert dc.spec_mounts_target(spec, "/opt/pdk") is True
        assert dc.spec_mounts_target(spec, "/missing") is False


class TestStateVolume:
    def test_claude_persists_claude_home(self):
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        vol = spec["mounts"][-1]
        assert "source=booley-claude-state-${localWorkspaceFolderBasename}" in vol
        assert f"target={dc.AGENT_HOME}/.claude" in vol
        assert "type=volume" in vol

    def test_codex_persists_codex_home(self):
        spec = dc.build_devcontainer_spec(dc.APP_CODEX)
        vol = spec["mounts"][-1]
        assert "source=booley-codex-state-${localWorkspaceFolderBasename}" in vol
        assert f"target={dc.AGENT_HOME}/.codex" in vol
        assert "type=volume" in vol

    def test_none_has_no_state_volume(self):
        spec = dc.build_devcontainer_spec(dc.APP_NONE)
        assert all("type=volume" not in m for m in spec["mounts"])

    def test_codex_creds_seed_sits_outside_state_dir(self):
        # The seed must be a SIBLING of ~/.codex, never nested under it: the state
        # volume owns that dir and would shadow a seed placed inside it, leaving
        # the copy with nothing to copy from.
        spec = dc.build_devcontainer_spec(
            dc.APP_CODEX,
            auth_token_source="/home/u/.codex/auth.json",
        )
        seed_mount = spec["mounts"][1]
        assert f"target={dc.AGENT_HOME}/.codex-auth-seed.json" in seed_mount
        assert f"target={dc.AGENT_HOME}/.codex/" not in seed_mount
        assert "type=volume" in spec["mounts"][2]  # state volume still mounted last

    def test_claude_creds_seed_sits_outside_state_dir(self):
        # The Claude creds seed must be a sibling of ~/.claude, not nested under
        # it, else the state volume would shadow it.
        spec = dc.build_devcontainer_spec(
            dc.APP_CLAUDE,
            auth_token_source="/home/u/.claude/.credentials.json",
        )
        seed = next(m for m in spec["mounts"] if "claude-creds-seed" in m)
        assert f"target={dc.AGENT_HOME}/.claude/" not in seed

    def test_state_volume_name_format(self):
        # The single name format shared with booley doctor's orphan check.
        assert dc.state_volume_name("claude", "myproj") == "booley-claude-state-myproj"
        assert dc.state_volume_name("codex", "myproj") == "booley-codex-state-myproj"

    def test_state_volume_name_none_for_appless(self):
        assert dc.state_volume_name("none", "myproj") is None

    def test_mount_name_matches_helper(self):
        # The spec's volume name must equal state_volume_name with the CLI's
        # basename placeholder — so doctor and the spec never diverge.
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        expected = dc.state_volume_name("claude", "${localWorkspaceFolderBasename}")
        assert f"source={expected}" in spec["mounts"][-1]

    def test_canonical_project_identity_separates_same_basename(self, tmp_path):
        first = tmp_path / "one" / "project"
        second = tmp_path / "two" / "project"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        first_id = dc.canonical_project_id(first)
        second_id = dc.canonical_project_id(second)
        assert first_id != second_id
        assert dc.state_volume_name("claude", first_id) != dc.state_volume_name(
            "claude", second_id
        )


# ===========================================================================
# spec_agent_app (which app a spec configures — the root of every drift below)
# ===========================================================================


class TestSpecAgentApp:
    @pytest.mark.parametrize("app", [dc.APP_CLAUDE, dc.APP_CODEX, dc.APP_NONE])
    def test_round_trips_the_built_app(self, app):
        assert dc.spec_agent_app(dc.build_devcontainer_spec(app)) == app

    @pytest.mark.parametrize(
        "spec",
        [
            {},
            {"remoteEnv": {}},
            {"remoteEnv": "not-a-table"},
            {"remoteEnv": {"BOOLEY_AGENT_APP": 42}},
        ],
    )
    def test_absent_or_malformed_is_none(self, spec):
        assert dc.spec_agent_app(spec) is None


# ===========================================================================
# spec_state_is_persisted (stale-spec detector shared with booley doctor)
# ===========================================================================


class TestSpecStateIsPersisted:
    def test_freshly_built_claude_spec_persists(self):
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        assert dc.spec_state_is_persisted(spec) is True

    def test_freshly_built_codex_spec_persists(self):
        spec = dc.build_devcontainer_spec(dc.APP_CODEX)
        assert dc.spec_state_is_persisted(spec) is True

    def test_app_none_is_not_applicable(self):
        # No home-state dir to persist -> None (nothing to check), not False.
        spec = dc.build_devcontainer_spec(dc.APP_NONE)
        assert dc.spec_state_is_persisted(spec) is None

    def test_stale_claude_spec_missing_volume_is_false(self):
        # Reproduce a pre-fix spec: the claude app, but the state volume dropped.
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        spec["mounts"] = [m for m in spec["mounts"] if "type=volume" not in m]
        assert dc.spec_state_is_persisted(spec) is False

    def test_volume_at_wrong_target_is_false(self):
        # A volume mount that does not target the app's state dir does not count.
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        spec["mounts"] = [m for m in spec["mounts"] if "type=volume" not in m] + [
            "source=other,target=/home/agent/.cache,type=volume"
        ]
        assert dc.spec_state_is_persisted(spec) is False

    def test_absent_or_unknown_app_is_not_applicable(self):
        assert dc.spec_state_is_persisted({"mounts": []}) is None
        assert (
            dc.spec_state_is_persisted({"remoteEnv": {"BOOLEY_AGENT_APP": "bogus"}, "mounts": []})
            is None
        )

    def test_accepts_object_form_mounts(self):
        # Dev Containers also permits object-form mounts; detector must handle it.
        spec = {
            "remoteEnv": {"BOOLEY_AGENT_APP": dc.APP_CLAUDE},
            "mounts": [
                {"source": "v", "target": f"{dc.AGENT_HOME}/.claude", "type": "volume"},
            ],
        }
        assert dc.spec_state_is_persisted(spec) is True


class TestConfigSeed:
    _SRC = "/home/u/.claude.json"
    _MCP = "python -m booley.incontainer_register"

    def test_seed_mounted_readonly_at_sidecar(self):
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE, config_seed_source=self._SRC)
        seed = next(m for m in spec["mounts"] if "claude-config-seed" in m)
        assert f"source={self._SRC}" in seed
        assert f"target={dc.AGENT_HOME}/.claude-config-seed.json" in seed
        assert seed.endswith(",readonly")

    def test_seed_sits_outside_the_state_dir(self):
        # The seed must NOT nest under the persisted ~/.claude volume, else it
        # would be shadowed; it lives at ~/.claude-config-seed.json (a sibling).
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE, config_seed_source=self._SRC)
        seed = next(m for m in spec["mounts"] if "claude-config-seed" in m)
        assert f"target={dc.AGENT_HOME}/.claude/" not in seed

    def test_state_volume_still_last_with_seed(self):
        # Ordering contract: the persisted state volume stays the final mount so
        # it nests under the deeper auth bind (and spec_state_is_persisted holds).
        spec = dc.build_devcontainer_spec(
            dc.APP_CLAUDE,
            auth_token_source="/home/u/.claude/.credentials.json",
            config_seed_source=self._SRC,
        )
        assert "type=volume" in spec["mounts"][-1]
        assert dc.spec_state_is_persisted(spec) is True

    def test_postcreate_seeds_before_registrar(self):
        # cp must precede the registrar so upsert_claude merges into the
        # host-derived config, not an empty one.
        spec = dc.build_devcontainer_spec(
            dc.APP_CLAUDE,
            config_seed_source=self._SRC,
            mcp_start_command=self._MCP,
        )
        pc = spec["postCreateCommand"]
        assert pc.index("cp -n") < pc.index(self._MCP)
        assert f"{dc.AGENT_HOME}/.claude-config-seed.json" in pc
        assert f"{dc.AGENT_HOME}/.claude.json" in pc
        # With no credential source there is nothing to re-seed on start, so
        # postStart is just the registrar. The CONFIG seed stays create-only.
        assert spec["postStartCommand"] == self._MCP
        assert "cp -n" not in spec["postStartCommand"]

    def test_poststart_reseeds_credentials(self):
        # The host OAuth token rotates: a container resumed days after create
        # would otherwise run every agent session against a dead token and fail
        # with "Not logged in". postStart must re-copy the live host creds bind.
        spec = dc.build_devcontainer_spec(
            dc.APP_CLAUDE,
            auth_token_source=self._SRC,
            config_seed_source=self._SRC,
            mcp_start_command=self._MCP,
        )
        ps = spec["postStartCommand"]
        creds_target = f"{dc.AGENT_HOME}/.claude/.credentials.json"
        assert f"cp {dc.AGENT_HOME}/.claude-creds-seed.json {creds_target}" in ps
        assert "chmod 600" in ps
        # Plain cp, never `cp -n`: a stale copy must be CLOBBERED by the fresh one.
        assert "cp -n /home/agent/.claude-creds-seed.json" not in ps
        # Re-seed lands before the registrar, and the registrar still runs.
        assert ps.index(creds_target) < ps.index(self._MCP)
        # The config seed remains create-time only (cp -n, never on start).
        assert "claude-config-seed" not in ps

    def test_postcreate_seed_only_when_no_mcp(self):
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE, config_seed_source=self._SRC)
        assert spec["postCreateCommand"].startswith("cp -n")
        assert "postStartCommand" not in spec

    def test_no_seed_for_codex(self):
        # Only Claude caches these grants; Codex gets no seed even with a source.
        spec = dc.build_devcontainer_spec(dc.APP_CODEX, config_seed_source=self._SRC)
        assert all("claude-config-seed" not in m for m in spec["mounts"])

    def test_no_seed_without_source(self):
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE, mcp_start_command=self._MCP)
        assert all("claude-config-seed" not in m for m in spec["mounts"])
        assert "cp -n" not in spec["postCreateCommand"]


class TestMcpStartCommand:
    def test_registration_set_on_both_create_and_start(self):
        # postCreateCommand registers before the first session (avoids the race);
        # postStartCommand repeats it on resume/rebuild.
        spec = dc.build_devcontainer_spec(
            dc.APP_NONE,
            mcp_start_command="python -m booley.incontainer_register",
        )
        assert spec["postCreateCommand"] == "python -m booley.incontainer_register"
        assert spec["postStartCommand"] == "python -m booley.incontainer_register"

    def test_registration_hooks_omitted_by_default(self):
        spec = dc.build_devcontainer_spec(dc.APP_NONE)
        assert "postStartCommand" not in spec
        assert "postCreateCommand" not in spec

    def test_post_start_command_runs_registrar(self):
        # ADR 0023: the registrar starts the loopback HTTP server and writes
        # the URL registration — re-run on every container start incl. resume.
        assert dc.mcp_post_start_command() == "python -m booley.incontainer_register"


# ===========================================================================
# render + write
# ===========================================================================


class TestRenderAndWrite:
    def test_render_round_trips(self):
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        text = dc.render_devcontainer_json(spec)
        assert text.endswith("\n")
        assert json.loads(text) == spec

    def test_write_creates_file(self, tmp_path):
        spec = dc.build_devcontainer_spec(dc.APP_NONE)
        path = dc.write_devcontainer(tmp_path, spec)
        assert path == tmp_path / ".devcontainer" / "devcontainer.json"
        assert json.loads(path.read_text(encoding="utf-8")) == spec

    def test_write_overwrites_existing(self, tmp_path):
        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_NONE))
        path = dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CODEX))
        assert json.loads(path.read_text(encoding="utf-8"))["name"].endswith("(codex)")
