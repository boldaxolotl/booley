"""Tests for the Sandbox devcontainer spec generator (ADR 0018 WS0)."""

from __future__ import annotations

import json

import pytest

from booley.runtime import devcontainer as dc
from booley.runtime import vaporview
from booley.runtime.dashboard_extension import VSIX_PATH
from booley.runtime.incontainer_git_identity import GitIdentity, git_identity_environment
from tests.devcontainer_spec_support import build_spec

# ===========================================================================
# build_devcontainer_spec
# ===========================================================================


class TestBuildSpec:
    @pytest.mark.parametrize("app", dc.SUPPORTED_APPS)
    def test_git_identity_precedes_registration_on_create_and_start(self, app):
        registrar = dc.mcp_post_start_command()
        expected_tail = f"{dc.git_identity_command()} && {registrar}"

        spec = build_spec(app, mcp_start_command=registrar)

        for key in ("postCreateCommand", "postStartCommand"):
            assert spec[key].split("; ")[-1] == expected_tail

    def test_core_runtime_fields(self):
        spec = build_spec(dc.APP_NONE)
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

    @pytest.mark.parametrize("app", dc.SUPPORTED_APPS)
    @pytest.mark.parametrize(
        ("options", "memory_args"),
        [
            ({}, []),
            ({"memory": "8g"}, ["--memory", "8g"]),
            ({"memory": "8g", "memory_swap": "9g"}, ["--memory", "8g", "--memory-swap", "9g"]),
        ],
    )
    def test_run_args_harden_label_and_limit_every_app(self, app, options, memory_args):
        """The Sandbox hardening (ARCHITECTURE.md#security--trust-model) must guard the
        venue where the agent actually runs; ADR 0087 adds the optional memory limits."""
        assert build_spec(app, **options)["runArgs"] == [
            "--init",
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
            *memory_args,
        ]

    def test_proxy_env_points_at_sidecar(self):
        env = build_spec(dc.APP_NONE)["remoteEnv"]
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            assert env[key] == dc.PROXY_URL
        assert env["BOOLEY_MCP_DEVELOPER_AGENT"] == "1"
        assert env["BOOLEY_DIR"] == dc.BOOLEY_DIR_TARGET

    def test_initialize_command_is_fixed_host_validator(self):
        for app in dc.SUPPORTED_APPS:
            assert build_spec(app)["initializeCommand"] == [
                "booley",
                "session",
                "prepare",
                "--project",
                "${localWorkspaceFolder}",
            ]

    @pytest.mark.parametrize(
        ("options", "source"),
        [
            # The default is the co-located single-variable form: the Dev Containers CLI
            # mis-parses nested defaults. User docs and the CI demo scripts name the literals.
            ({}, "${localWorkspaceFolder}/.booley"),
            ({"booley_dir_source": "/host/proj/.booley"}, "/host/proj/.booley"),
        ],
    )
    def test_booley_dir_mounts_at_booley(self, options, source):
        spec = build_spec(dc.APP_NONE, **options)
        assert spec["mounts"][0] == f"source={source},target=/booley,type=bind"
        assert spec["remoteEnv"]["BOOLEY_DIR"] == "/booley"
        assert (
            spec["containerEnv"]["BOOLEY_COMPILER_CACHE_ROOT"]
            == "/booley/.runtime/compiler-cache/ccache"
        )

    def test_unknown_app_rejected(self):
        with pytest.raises(ValueError, match="unknown app"):
            build_spec("emacs")

    @pytest.mark.parametrize("fixed", [{}, {"XILINXD_LICENSE_FILE": "2100@booley-license-xilinx"}])
    def test_container_env_is_cache_identity_and_literal_host_policy(self, fixed):
        spec = build_spec(dc.APP_NONE, fixed_container_env=fixed)
        assert spec["containerEnv"] == {
            **git_identity_environment(GitIdentity("Dev", "dev@localhost")),
            **fixed,
            "BOOLEY_COMPILER_CACHE_ROOT": "/booley/.runtime/compiler-cache/ccache",
        }

    def test_local_timezone_is_forwarded_to_runtime_processes(self):
        spec = build_spec(dc.APP_NONE, local_timezone="Asia/Tbilisi")

        assert spec["remoteEnv"]["BOOLEY_LOCAL_TIMEZONE"] == "Asia/Tbilisi"

    @pytest.mark.parametrize(
        "skills", [[], [("deslop", "/host/skills/deslop"), ("grill-me", "/host/x/grill-me")]]
    )
    def test_host_skills_render_readonly_sidecar_binds(self, skills):
        # [sandbox] mount_host_skills: each host skill rides in as its own
        # read-only bind at the sidecar; init has already resolved the real dir.
        spec = build_spec(dc.APP_CLAUDE, host_skills=skills)
        assert [m for m in spec["mounts"] if dc.HOST_SKILLS_SIDECAR in m] == [
            f"source={source},target={dc.HOST_SKILLS_SIDECAR}/{name},type=bind,readonly"
            for name, source in skills
        ]

    @pytest.mark.parametrize(
        ("mask_paths", "targets"),
        [
            (
                ["secret/oracle", "notes.md"],
                [f"{dc.WORK_DIR}/secret/oracle", f"{dc.WORK_DIR}/notes.md"],
            ),
            # .booley is visible TWICE (via /work and /booley), so a subtree under it
            # must be masked in both views or the bytes stay readable through the other.
            (
                [".booley/oracle"],
                [f"{dc.WORK_DIR}/.booley/oracle", f"{dc.BOOLEY_DIR_TARGET}/oracle"],
            ),
            # docker treats "target=/work/x/" and "target=/work/x" as different strings.
            (["a/./b/", "c//d"], [f"{dc.WORK_DIR}/a/b", f"{dc.WORK_DIR}/c/d"]),
        ],
    )
    def test_mask_paths_render_readonly_empty_binds(self, mask_paths, targets):
        # [sandbox] mask_paths: each entry hides a workspace subtree behind a
        # read-only bind of the always-empty host dir.
        spec = build_spec(dc.APP_NONE, mask_paths=mask_paths, mask_source="/empty")
        for target in targets:
            assert f"source=/empty,target={target},type=bind,readonly" in spec["mounts"]

    def test_mask_mounts_come_after_the_booley_dir_mount(self):
        # Mount order is the over-mount mechanism: docker applies
        # workspaceMount first, then `mounts` in order, so a mask listed
        # BEFORE the project-dir mount would be buried by it.
        spec = build_spec(
            dc.APP_CLAUDE, mask_paths=[".booley/oracle", "lanes/b"], mask_source="/empty"
        )
        mounts = spec["mounts"]
        project_idx = mounts.index(
            f"source={dc.DEFAULT_BOOLEY_DIR_SOURCE},target={dc.BOOLEY_DIR_TARGET},type=bind"
        )
        mask_indices = [i for i, m in enumerate(mounts) if m.startswith("source=/empty,")]
        assert mask_indices  # both views + the plain /work mask
        assert all(i > project_idx for i in mask_indices)
        # Masks trail EVERY other mount (state volume, token seeds, ...).
        assert mask_indices == list(range(len(mounts) - len(mask_indices), len(mounts)))

    def test_unproxied_spec_joins_routed_network_without_proxy_environment(self):
        spec = build_spec(dc.APP_NONE, egress="unproxied")
        networks = [
            spec["runArgs"][i + 1] for i, a in enumerate(spec["runArgs"]) if a == "--network"
        ]
        assert networks == [dc.UNPROXIED_NETWORK]
        proxy_keys = {"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY"}
        assert not proxy_keys & set(spec["remoteEnv"])
        assert spec["containerEnv"]["BOOLEY_EGRESS"] == "unproxied"
        assert "BOOLEY_EGRESS" not in spec["remoteEnv"]

    @pytest.mark.parametrize(
        "options", [{"egress": "proxy"}, {"mask_paths": (), "mask_source": ""}]
    )
    def test_default_options_leave_spec_byte_identical(self, options):
        # A default must be a no-op: existing installs re-seed the exact same
        # spec they had before the knob existed.
        assert json.dumps(build_spec(dc.APP_CLAUDE)) == json.dumps(
            build_spec(dc.APP_CLAUDE, **options)
        )

    def test_mask_paths_without_mask_source_rejected(self):
        # Silently dropping masks would hand the agent exactly the visibility
        # the project configured away — misuse must be loud.
        with pytest.raises(ValueError, match="mask_source"):
            build_spec(dc.APP_NONE, mask_paths=["secret"])


class TestMaskPathsValidation:
    """mask_paths_error guards the [sandbox].mask_paths knob shape."""

    @pytest.mark.parametrize("paths", [None, [], ["secret/oracle", ".booley/lanes/b", "notes.md"]])
    def test_accepts_absent_and_valid_relative_paths(self, paths):
        assert dc.mask_paths_error(paths) is None

    @pytest.mark.parametrize(
        ("paths", "message"),
        [
            ("secret", "list of strings"),
            (["ok", 3], "list of strings"),
            # Absolute paths and ".." segments would turn the privacy knob into a
            # container-layout editor; "." / "" name the whole workspace.
            *(
                ([entry], "workspace-relative")
                for entry in ["/etc", "../outside", "a/../b", ".", "", "win\\path"]
            ),
            # The session reads booley.toml through /booley: masking the entire dir
            # bricks the runtime rather than hiding a secret.
            ([".booley"], "subtrees"),
            ([".booley/"], "subtrees"),
        ],
    )
    def test_rejects_unsafe_entries(self, paths, message):
        error = dc.mask_paths_error(paths)
        assert error is not None and message in error


class TestAppExtension:
    def test_claude_spec_installs_no_agent_extension(self):
        vscode = build_spec(dc.APP_CLAUDE)["customizations"]["vscode"]
        assert vscode["extensions"] == [
            vaporview.BUNDLED_VSIX,
            dc.HDL_VSIX,
            dc.LIVE_PREVIEW_VSIX,
            VSIX_PATH,
        ]
        assert "remote.extensionKind" not in vscode["settings"]
        # The viewer bridge drives VaporView; its own WCP endpoint stays off.
        assert not any(key.startswith("vaporview.wcp.") for key in vscode["settings"])
        assert vscode["settings"]["vaporview.customColor5"] == "#e5484d"
        assert vscode["settings"]["vaporview.customColor6"] == "#3b82f6"
        assert vscode["settings"]["vaporview.customColor7"] == "#2da44e"

    def test_codex_spec_installs_no_agent_extension(self):
        vscode = build_spec(dc.APP_CODEX)["customizations"]["vscode"]
        assert vscode["extensions"] == [
            vaporview.BUNDLED_VSIX,
            dc.HDL_VSIX,
            dc.LIVE_PREVIEW_VSIX,
            VSIX_PATH,
        ]
        assert "remote.extensionKind" not in vscode["settings"]

    def test_generated_spec_omits_tcl_highlighting(self):
        # The HDL extension owns .sdc/.xdc; no Tcl grammar or association is needed.
        for app in dc.SUPPORTED_APPS:
            vscode = build_spec(app)["customizations"]["vscode"]
            assert "files.associations" not in vscode["settings"]

    def test_generated_spec_installs_bundled_hdl_highlight_only(self):
        for app in dc.SUPPORTED_APPS:
            extensions = build_spec(app)["customizations"]["vscode"]["extensions"]
            assert dc.HDL_VSIX in extensions
            assert "mshr-h.veriloghdl" not in extensions

    def test_none_installs_no_agent_extension(self):
        # Waveform viewing isn't tied to an agent app, so "none" carries the
        # app-agnostic extensions (VaporView, its Booley bridge host, HDL
        # highlighting) but no agent extension.
        vscode = build_spec(dc.APP_NONE)["customizations"]["vscode"]
        assert vscode["extensions"] == [
            vaporview.BUNDLED_VSIX,
            dc.HDL_VSIX,
            dc.LIVE_PREVIEW_VSIX,
            VSIX_PATH,
        ]

    def test_vaporview_presentation_settings_for_every_app(self):
        # The Waveform Viewer (ADR 0078) palette rides along regardless of agent
        # app; the per-app extension lists above pin the VSIX entries.
        for app in dc.SUPPORTED_APPS:
            spec = build_spec(app)
            vscode = spec["customizations"]["vscode"]
            # private: VaporView's presentation settings have no public reader; T3 retires
            # this test with VaporView
            assert {
                key: vscode["settings"][key] for key in dc._VAPORVIEW_PRESENTATION_SETTINGS
            } == dc._VAPORVIEW_PRESENTATION_SETTINGS  # private: as above

    def test_live_preview_does_not_restore_stale_port_tunnels(self):
        # F-14: restored 3000/3001 tunnels can point the embedded browser at a
        # dead endpoint while Live Preview's newly allocated tunnel is healthy.
        for app in dc.SUPPORTED_APPS:
            settings = build_spec(app)["customizations"]["vscode"]["settings"]
            assert settings["remote.restoreForwardedPorts"] is False
            # private: the Live Preview seed port has no public name
            assert settings["livePreview.portNumber"] == dc._LIVE_PREVIEW_SEED_PORT

    def test_live_preview_randomizer_runs_before_extension_patch(self):
        for app in dc.SUPPORTED_APPS:
            assert build_spec(app)["postAttachCommand"] == (
                f"{dc.live_preview_port_command()} && {dc.vaporview_patch_command()}"
            )

    def test_python_terminal_autoactivation_disabled_for_every_app(self):
        # The runtime already has its Python stack system-wide.  Do not let a
        # synced Python extension discover a host-created workspace .venv and
        # inject a delayed activation command into interactive terminals.
        for app in dc.SUPPORTED_APPS:
            spec = build_spec(app)
            settings = spec["customizations"]["vscode"]["settings"]
            assert settings["python-envs.terminal.autoActivationType"] == "off"
            assert settings["python.terminal.activateEnvironment"] is False

    def test_spec_wires_vaporview_autostart_postattach(self):
        # Every app (incl. "none") gets the patch hook on attach — attach is
        # the first lifecycle point where the extension exists on disk.
        for app in dc.SUPPORTED_APPS:
            spec = build_spec(app)
            assert spec["postAttachCommand"] == dc.post_attach_command()
            assert "booley.runtime.incontainer_vaporview" in spec["postAttachCommand"]


@pytest.mark.parametrize(
    ("app", "option", "source", "seed"),
    [
        ("claude", "login_source", "/home/u/.claude/.credentials.json", ".claude-creds-seed.json"),
        ("codex", "login_source", "/home/u/.codex/auth.json", ".codex-auth-seed.json"),
        ("claude", "token_source", "/home/u/token", ".booley-oauth-token-seed"),
        ("codex", "token_source", "/home/u/api-key", ".booley-api-key-seed"),
        ("claude", "config_source", "/home/u/.claude.json", ".claude-config-seed.json"),
    ],
)
def test_seed_is_bound_readonly_beside_the_state_dir(app, option, source, seed):
    # Seeds are read-only sidecars, never the real file, so the in-container refresh
    # can rewrite the writable copy. The persisted state volume owns ~/.<app> and would
    # shadow a seed nested in it, and stays the last mount so it nests under the binds.
    spec = build_spec(app, **{option: source})
    assert f"source={source},target={dc.AGENT_HOME}/{seed},type=bind,readonly" in spec["mounts"]
    assert not any(f"target={dc.AGENT_HOME}/.{app}/" in m for m in spec["mounts"])
    assert "type=volume" in spec["mounts"][-1]
    assert f"{dc.AGENT_HOME}/.{app}" in dc.spec_volume_targets(spec)


class TestAuthMount:
    def test_claude_creds_seeded_into_writable_path_on_create(self):
        # Plain cp (not cp -n) re-seeds the freshest host token on every create;
        # chmod 600 keeps it agent-private after the copy.
        spec = build_spec(
            dc.APP_CLAUDE,
            login_source="/home/u/.claude/.credentials.json",
            mcp_start_command="python -m booley.harness.incontainer_register",
        )
        pc = spec["postCreateCommand"]
        assert f"cp {dc.AGENT_HOME}/.claude-creds-seed.json" in pc
        assert f"chmod 600 {dc.AGENT_HOME}/.claude/.credentials.json" in pc
        # Must be a fresh copy each create — no no-clobber on the rotating token.
        assert f"cp -n {dc.AGENT_HOME}/.claude-creds-seed.json" not in pc

    def test_codex_auth_is_copied_to_a_writable_file(self):
        # Codex REWRITES ~/.codex/auth.json when a subscription login's tokens
        # refresh. A read-only bind on that path makes the refresh write fail and
        # 401s the session, so the writable copy is made on both create and start.
        spec = build_spec(dc.APP_CODEX, login_source="/home/u/.codex/auth.json")
        for hook in (spec["postCreateCommand"], spec["postStartCommand"]):
            assert (
                f"cp {dc.AGENT_HOME}/.codex-auth-seed.json {dc.AGENT_HOME}/.codex/auth.json"
                in hook
            )
        assert f"chmod 600 {dc.AGENT_HOME}/.codex/auth.json" in spec["postCreateCommand"]

    @pytest.mark.parametrize("app", dc.SUPPORTED_APPS)
    def test_no_seed_mounts_without_sources(self, app):
        # project-dir mount, plus the persistent state volume for an agent app
        mounts = build_spec(app)["mounts"]
        assert len(mounts) == (1 if app == dc.APP_NONE else 2)
        assert all("token-seed" not in m and "api-key-seed" not in m for m in mounts)

    @pytest.mark.parametrize("option", ["login_source", "token_source"])
    def test_app_none_ignores_credential_sources(self, option):
        # "none" has no auth target and no persistent state dir.
        assert len(build_spec(dc.APP_NONE, **{option: "/whatever"})["mounts"]) == 1


class TestTokenSeedMount:
    """The `booley auth` credential rides in as its own read-only sidecar.

    The remoteEnv ${localEnv:...} route can't deliver it to VS Code's "Reopen
    in Container" (VS Code resolves localEnv against its own process env), so
    the stored file is mounted for incontainer_register to apply on every
    container start.
    """

    def test_token_value_never_baked_into_spec(self):
        # The spec must carry only the mount reference — never file contents.
        spec = build_spec(
            dc.APP_CLAUDE,
            token_source="/home/u/.config/booley/claude-oauth-token",
            credentials={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-synthetic"},
        )
        assert "sk-ant-" not in json.dumps(spec)


class TestStateVolume:
    @pytest.mark.parametrize("app", [dc.APP_CLAUDE, dc.APP_CODEX])
    def test_persists_the_app_home(self, app):
        vol = build_spec(app)["mounts"][-1]
        assert f"source=booley-{app}-state-${{localWorkspaceFolderBasename}}" in vol
        assert f"target={dc.AGENT_HOME}/.{app}" in vol
        assert "type=volume" in vol

    def test_none_has_no_state_volume(self):
        spec = build_spec(dc.APP_NONE)
        assert all("type=volume" not in m for m in spec["mounts"])

    def test_state_volume_name_format(self):
        # The single name format shared with booley doctor's orphan check; the spec's
        # volume must use it with the CLI's basename placeholder.
        assert dc.state_volume_name("claude", "myproj") == "booley-claude-state-myproj"
        assert dc.state_volume_name("codex", "myproj") == "booley-codex-state-myproj"
        assert dc.state_volume_name("none", "myproj") is None
        expected = dc.state_volume_name("claude", "${localWorkspaceFolderBasename}")
        assert f"source={expected}" in build_spec(dc.APP_CLAUDE)["mounts"][-1]

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
        assert dc.spec_agent_app(build_spec(app)) == app

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


class TestConfigSeed:
    _SRC = "/home/u/.claude.json"
    _MCP = "python -m booley.harness.incontainer_register"

    def test_postcreate_seeds_before_registrar(self):
        # cp must precede the registrar so upsert_claude merges into the
        # host-derived config, not an empty one.
        spec = build_spec(dc.APP_CLAUDE, config_source=self._SRC, mcp_start_command=self._MCP)
        pc = spec["postCreateCommand"]
        assert pc.index("cp -n") < pc.index(self._MCP)
        assert f"{dc.AGENT_HOME}/.claude-config-seed.json" in pc
        assert f"{dc.AGENT_HOME}/.claude.json" in pc
        # The CONFIG seed stays create-only. Identity is refreshed before the
        # registrar on every start so copied host Git config cannot win.
        assert spec["postStartCommand"] == f"{dc.git_identity_command()} && {self._MCP}"
        assert "cp -n" not in spec["postStartCommand"]

    def test_poststart_reseeds_credentials(self):
        # The host OAuth token rotates: a container resumed days after create
        # would otherwise run every agent session against a dead token and fail
        # with "Not logged in". postStart must re-copy the live host creds bind.
        spec = build_spec(
            dc.APP_CLAUDE,
            login_source=self._SRC,
            config_source=self._SRC,
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
        spec = build_spec(dc.APP_CLAUDE, config_source=self._SRC)
        assert spec["postCreateCommand"].startswith("cp -n")
        assert spec["postStartCommand"] == dc.git_identity_command()

    def test_no_seed_for_codex(self):
        # Only Claude caches these grants; Codex gets no seed even with a source.
        spec = build_spec(dc.APP_CODEX, config_source=self._SRC)
        assert all("claude-config-seed" not in m for m in spec["mounts"])

    def test_no_seed_without_source(self):
        spec = build_spec(dc.APP_CLAUDE, mcp_start_command=self._MCP)
        assert all("claude-config-seed" not in m for m in spec["mounts"])
        assert "cp -n" not in spec["postCreateCommand"]


class TestMcpStartCommand:
    def test_identity_hooks_remain_without_registration(self):
        spec = build_spec(dc.APP_NONE)
        assert spec["postStartCommand"] == dc.git_identity_command()
        assert spec["postCreateCommand"] == dc.git_identity_command()

    def test_post_start_command_runs_registrar(self):
        # ADR 0023: the registrar starts the loopback HTTP server and writes
        # the URL registration — re-run on every container start incl. resume.
        assert dc.mcp_post_start_command() == "python -m booley.harness.incontainer_register"


# ===========================================================================
# render + write
# ===========================================================================


class TestRenderAndWrite:
    def test_render_round_trips(self):
        spec = build_spec(dc.APP_CLAUDE)
        text = dc.render_devcontainer_json(spec)
        assert text.endswith("\n")
        assert json.loads(text) == spec

    def test_write_creates_then_overwrites_the_file(self, tmp_path):
        spec = build_spec(dc.APP_NONE)
        path = dc.write_devcontainer(tmp_path, spec)
        assert path == tmp_path / ".devcontainer" / "devcontainer.json"
        assert json.loads(path.read_text(encoding="utf-8")) == spec

        dc.write_devcontainer(tmp_path, build_spec(dc.APP_CODEX))
        assert json.loads(path.read_text(encoding="utf-8"))["name"].endswith("(codex)")


def test_project_resource_name_identity_bytes_remain_stable(tmp_path):
    import hashlib
    import os

    from booley.runtime import devcontainer

    expected = hashlib.sha256(os.path.normcase(str(tmp_path.resolve())).encode()).hexdigest()[:32]
    assert devcontainer.canonical_project_id(tmp_path) == expected
    assert expected in devcontainer.state_volume_mount("claude", expected)


def test_subscription_keeps_the_unsuffixed_login_volume(tmp_path, monkeypatch):
    from booley.runtime import auth_token

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    specs = [
        dc.build_devcontainer_spec(
            "claude",
            project_id="one-project",
            cli_seeds=[dc.CliSeeds(auth_token.credential_plan("claude", mode))],
        )
        for mode in (auth_token.VISITOR_AUTH, "subscription", "api_key")
    ]
    volumes = [next(mount for mount in spec["mounts"] if "type=volume" in mount) for spec in specs]
    assert volumes[0] == volumes[1]
    assert "-subscription" not in volumes[1]
    assert volumes[2] != volumes[0]
    assert "-api_key," in volumes[2]
    legacy = dc.state_volume_mount("claude", "one-project", "subscription", legacy=True)
    assert legacy is not None
    assert "-subscription," in legacy
    assert dc.legacy_subscription_clis([legacy, volumes[0]], "one-project") == {"claude"}
    assert dc.legacy_subscription_clis([volumes[0]], "one-project") == frozenset()
    assert dc.legacy_subscription_clis([legacy], "other-project") == frozenset()
