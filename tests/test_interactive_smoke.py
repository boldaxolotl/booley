"""Interactive Mode smoke tests — Phase 1.3 of ADR 0012.

These tests exercise Booley Flows and MCP server entry points from a "no ticket"
context: no ``BOOLEY_SLUG``, no ``BOOLEY_STATE_FILE``, no development
state file on disk.  They protect the Interactive Mode happy path from
regressions caused by future state-file assumptions slipping into Flow
code paths.

Real simulator subprocesses are mocked out (``--dry-run`` for ``simulate``)
so the tests run in milliseconds and need no EDA tooling installed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from booley.flows.execution import ExecutionSelection
from booley.flows.sim.flow import SimulateFlow
from booley.mcp.base import EXIT_ERROR, EXIT_SUCCESS

_BOOLEY_ENV_VARS = (
    "BOOLEY_SLUG",
    "BOOLEY_STATE_FILE",
    "BOOLEY_LOGS_DIR",
    "BOOLEY_PROJECT_ROOT",
)


@pytest.fixture(autouse=True)
def _clear_ticket_env() -> None:
    """Strip ticket-mode env vars so each test runs in a clean Interactive
    Mode context — the harness sets these in Ticket Mode; their presence
    would silently re-enable state-file gating and defeat the test.

    Uses a manual save/restore (rather than ``monkeypatch.delenv``) because
    a couple of the tests below intentionally mutate ``os.environ`` via
    ``_maybe_configure_interactive_logs_dir``, and ``monkeypatch`` does not
    track direct ``os.environ`` writes — without this fixture they leaked
    into adjacent test modules and corrupted ``reviewer``'s session-file
    persistence path.
    """
    saved = {k: os.environ.get(k) for k in _BOOLEY_ENV_VARS}
    for k in _BOOLEY_ENV_VARS:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _make_interactive_simulate(
    tmp_path: Path,
    *,
    config: str = "lite",
    tb_top: str | None = "alu_tb",
    extra_args: list[str] | None = None,
) -> SimulateFlow:
    """Build a SimulateFlow exactly as the MCP dispatch path would.

    Crucially: no state file, no BOOLEY_SLUG/BOOLEY_STATE_FILE env vars.
    """
    report_dir = tmp_path / "reports"
    # tb_top left the surface (ADR 0021); it comes from the resolved Target.
    # The `tb_top` kwarg is retained for call-site compat — tests that assert a
    # specific tb_top stub `_tb_top_for_target` rather than passing an arg.
    _ = tb_top
    argv = [
        "--work-dir",
        str(tmp_path),
        "--report-dir",
        str(report_dir),
        "--target",
        config,
    ]
    if extra_args:
        argv.extend(extra_args)

    tool = SimulateFlow()
    tool.parse_args(argv)
    tool.read_state()
    return tool


# ---------------------------------------------------------------------------
# Validation: missing args produce clean errors, not AttributeError
# ---------------------------------------------------------------------------


class TestRequiredArgsValidation:
    """``simulate`` must reject missing required args with a useful message
    instead of crashing with ``AttributeError`` deep in the call chain."""

    def test_missing_config_returns_error(self, tmp_path: Path) -> None:
        tool = _make_interactive_simulate(tmp_path, config="")
        result = tool._run()
        assert result.exit_code == EXIT_ERROR
        assert "--target" in result.report_text
        assert "booley targets" in result.report_text


# ---------------------------------------------------------------------------
# Happy path: dry-run sim completes from a no-ticket context
# ---------------------------------------------------------------------------


class TestNoTicketDryRun:
    """End-to-end smoke: a fresh CWD with no state file can still drive
    ``simulate --dry-run`` to a clean exit."""

    @patch(
        "booley.flows.sim.flow._get_test_names",
        return_value={"lite": ["smoke", "stress"]},
    )
    @patch.object(
        SimulateFlow,
        "_resolve_execution",
        return_value=ExecutionSelection(),
    )
    def test_dry_run_succeeds_without_state_file(
        self,
        _backend,
        _tests,
        tmp_path: Path,
        capsys,
    ) -> None:
        # A minimal sim `.core` lets the edalize dry-run show a real
        # `fusesoc run --setup` command. enumerate_targets is a YAML read — no
        # fusesoc execution and no source files needed; the dry-run is
        # side-effect-free (slice-4 rework — no more legacy-path / resolve stub).
        (tmp_path / "sim.core").write_text(
            "CAPI=2:\n"
            "name: ::sim_demo:0\n"
            "targets:\n"
            "  lite:\n"
            "    flow: sim\n"
            "    flow_options:\n"
            "      tool: verilator\n",
            encoding="utf-8",
        )
        tool = _make_interactive_simulate(
            tmp_path,
            config="lite",
            extra_args=["--dry-run"],
        )
        # Sanity: confirm we really are running without a state file —
        # this is the property the test is protecting.
        assert tool._state is not None
        assert tool._state._file_path is None
        assert not os.environ.get("BOOLEY_SLUG")

        result = tool._run()
        assert result.exit_code == EXIT_SUCCESS, result.report_text

        commands = json.loads(capsys.readouterr().out)
        assert len(commands) == 2  # one per test
        for cmd in commands:
            # The preview shows the fusesoc --setup command without executing it.
            script = " ".join(cmd)
            assert "--setup" in script
            assert "--target lite" in script
            assert "sim_demo" in script  # the resolved vlnv from the .core


# ---------------------------------------------------------------------------
# MCP server: BOOLEY_LOGS_DIR setup
# ---------------------------------------------------------------------------


class TestInteractiveLogsDir:
    """The MCP server promotes ``.booley_project/.interactive_logs/<id>/``
    to ``BOOLEY_LOGS_DIR`` when (a) the env var is unset and (b) a
    ``.booley_project/`` is present in CWD."""

    def test_sets_logs_dir_when_project_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from booley.mcp import server as mcp_server

        (tmp_path / ".booley_project").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)

        mcp_server._maybe_configure_interactive_logs_dir()

        logs_dir = os.environ.get("BOOLEY_LOGS_DIR", "")
        assert logs_dir, "BOOLEY_LOGS_DIR should have been set"
        logs_path = Path(logs_dir)
        assert logs_path.is_dir()
        assert ".interactive_logs" in logs_path.parts
        assert logs_path.parent.name == ".interactive_logs"
        assert logs_path.parent.parent.name == ".booley_project"

    def test_noop_when_logs_dir_already_set(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from booley.mcp import server as mcp_server

        (tmp_path / ".booley_project").mkdir()
        monkeypatch.chdir(tmp_path)
        existing = tmp_path / "ticket_logs"
        existing.mkdir()
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(existing))

        mcp_server._maybe_configure_interactive_logs_dir()

        assert os.environ["BOOLEY_LOGS_DIR"] == str(existing)

    def test_noop_when_no_project_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from booley.mcp import server as mcp_server

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)

        mcp_server._maybe_configure_interactive_logs_dir()

        assert not os.environ.get("BOOLEY_LOGS_DIR")


# ---------------------------------------------------------------------------
# asic_synthesize: --baseline is rejected in Interactive Mode
# ---------------------------------------------------------------------------


class TestAsicSynthesizeBaselineInteractive:
    """`--baseline` compares against a past git ref. It materializes that ref in
    a throwaway ``git worktree`` (never an in-place checkout), so it works in
    Interactive Mode (no ``BOOLEY_SLUG``) WITHOUT disturbing the user's tree."""

    def test_baseline_uses_worktree_and_leaves_tree_untouched(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import subprocess

        from booley.flows.synth.flow import AsicSynthesizeFlow, SynthMetrics
        from booley.mcp.base import McpToolResult

        monkeypatch.delenv("BOOLEY_SLUG", raising=False)

        # Real git repo with two commits so HEAD~1 resolves to a distinct ref.
        repo = tmp_path

        def git(*a: str) -> None:
            subprocess.run(
                ["git", *a],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "Test")
        marker = repo / "rtl.v"
        marker.write_text("// baseline version\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "baseline")
        marker.write_text("// current version\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "current")
        # An UNCOMMITTED working-tree edit that an in-place checkout would clobber.
        marker.write_text("// uncommitted edits — MUST survive\n", encoding="utf-8")
        before = marker.read_text(encoding="utf-8")

        tool = AsicSynthesizeFlow()
        tool.parse_args(
            [
                "--work-dir",
                str(repo),
                "--target",
                "default",
                "--baseline",
                "HEAD~1",
            ]
        )
        tool.read_state()

        seen_work_dirs: list[Path] = []

        def fake_single(target: str, *, baseline: bool = False):
            # Capture the tree the baseline run reads from (the worktree, not repo).
            seen_work_dirs.append(Path(tool.args.work_dir))
            return SynthMetrics(returncode=0), "baseline output"

        with patch.object(tool, "_run_single_config", side_effect=fake_single):
            results, short_sha = tool._run_baseline_configs(["default"])

        # It succeeded (no McpToolResult error) and resolved the baseline ref.
        assert not isinstance(results, McpToolResult)
        assert "default" in results
        assert short_sha  # HEAD~1 resolved to a short sha

        # The baseline ran inside a throwaway worktree, not the project root.
        assert seen_work_dirs and seen_work_dirs[0] != repo
        assert ".baseline-wt-" in str(seen_work_dirs[0])

        # The user's uncommitted working-tree edit is intact...
        assert marker.read_text(encoding="utf-8") == before
        # ...work_dir was restored, and the worktree was cleaned up.
        assert Path(tool.args.work_dir) == repo
        assert not seen_work_dirs[0].exists()


# ---------------------------------------------------------------------------
# bwave: register → query --list round-trip
# ---------------------------------------------------------------------------


class TestBwaveRoundTrip:
    """bwave is genuinely standalone — its session file lives under the
    user cache, not the ticket state.  These tests confirm that property
    holds: register from a fresh CWD, then query observes the registration."""

    def test_register_persists_into_session_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from booley.bwave import cli as bwave

        # Redirect the session file to tmp_path so the test doesn't pollute
        # the user's real cache.
        session_file = tmp_path / "bwave_sessions.json"
        monkeypatch.setattr(bwave, "SESSION_FILE", session_file)

        fake_trace = tmp_path / "test.fst"
        fake_trace.write_bytes(b"")

        alias = bwave._register_trace(fake_trace, alias="smoke")

        assert alias == "smoke"
        sessions = bwave._load_sessions()
        assert "smoke" in sessions
        assert sessions["smoke"]["trace"] == str(fake_trace.resolve())
        # _last alias mirror is part of the contract
        assert "_last" in sessions
        assert sessions["_last"]["trace"] == str(fake_trace.resolve())

    def test_query_resolves_registered_alias(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After register, `query @alias --list` should resolve the alias to
        the registered trace path. We mock the bwave subprocess so the test
        doesn't need the binary, but verify the command shape."""
        from booley.bwave import cli as bwave

        session_file = tmp_path / "bwave_sessions.json"
        monkeypatch.setattr(bwave, "SESSION_FILE", session_file)

        fake_trace = tmp_path / "test.fst"
        fake_trace.write_bytes(b"")
        bwave._register_trace(fake_trace, alias="smoke")

        captured: list[list[str]] = []
        monkeypatch.setattr(bwave, "_run", lambda cmd: captured.append(cmd) or 0)
        monkeypatch.setattr(bwave, "_bwave_cmd", lambda: ["bwave"])

        # Simulate `bwave query @smoke --list`. cmd_query propagates the
        # subprocess exit code via sys.exit(), so catch SystemExit(0).
        import argparse

        args = argparse.Namespace(extra=["@smoke", "--list"])
        with pytest.raises(SystemExit) as exc_info:
            bwave.cmd_query(args)
        assert exc_info.value.code == 0

        assert len(captured) == 1
        cmd = captured[0]
        assert "bwave" in cmd
        # v0.2: legacy `--list` is rewritten to the `list` subcommand.
        assert "list" in cmd
        # The resolved trace path must appear in the command
        assert str(fake_trace.resolve()) in cmd


# ---------------------------------------------------------------------------
# booley init: Interactive Mode devcontainer seeding (ADR 0018)
# ---------------------------------------------------------------------------


class TestInitInteractive:
    """`_step_interactive` writes the untracked devcontainer spec, excludes
    Booley files, and (when docker is present) creates the long-lived objects.
    Host MCP registration (ADR 0012) was removed."""

    @pytest.fixture(autouse=True)
    def _pin_runtime_image(self, monkeypatch):
        """Unit tests pin deterministically without requiring a local image."""
        from booley.eda import runtime_spec

        def pin_image(spec):
            spec["image"] = "sha256:" + "a" * 64
            return spec["image"]

        monkeypatch.setattr(runtime_spec, "pin_image", pin_image)
        monkeypatch.setattr(runtime_spec, "seal", lambda _project, _spec: None)
        monkeypatch.setattr(runtime_spec, "issue", lambda _project, _spec, _path: None)

    def _ctx(self, root):
        from booley.harness import init_cmd

        return init_cmd.InitContext(
            project_root=root, check_only=False, force=False, verbose=False, interactive=False
        )

    def test_writes_untracked_spec_and_excludes(self, tmp_path, monkeypatch):
        import subprocess

        from booley.harness import init_cmd

        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        (tmp_path / ".booley_project").mkdir()

        # No docker; isolate from host app detection / creds.
        monkeypatch.setattr(init_cmd.shutil, "which", lambda _n: None)
        monkeypatch.setattr(init_cmd, "_select_interactive_app", lambda *_: "none")

        ctx = self._ctx(tmp_path)
        init_cmd._step_interactive(ctx)

        spec = tmp_path / ".devcontainer" / "devcontainer.json"
        assert spec.is_file()
        data = json.loads(spec.read_text(encoding="utf-8"))
        assert data["remoteUser"] == "agent"
        # Excluded via git info/exclude, not .gitignore.
        assert not (tmp_path / ".gitignore").exists()
        exclude = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert "/.devcontainer" in exclude and "/.booley_project" in exclude
        assert "/.claude" in exclude
        # No host MCP artifacts.
        assert not (tmp_path / ".mcp.json").exists()
        statuses = {r.name: r.status for r in ctx.results}
        assert statuses.get("interactive") in {"ok", "warn"}

    def test_mounts_setup_pdk_cache_read_only(self, tmp_path, monkeypatch):
        import subprocess

        from booley.harness import init_cmd
        from booley.harness.nangate_pdk import CONTAINER_ROOT

        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        (tmp_path / ".booley_project").mkdir()
        pdk = tmp_path / "host-cache" / "pdk"
        pdk.mkdir(parents=True)
        monkeypatch.setattr(init_cmd.shutil, "which", lambda _n: None)
        monkeypatch.setattr(init_cmd, "_select_interactive_app", lambda *_: "none")

        init_cmd._step_interactive(self._ctx(tmp_path), nangate_pdk_root=pdk)

        spec = json.loads(
            (tmp_path / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
        )
        assert (
            f"source={init_cmd.docker_mount_path(pdk)},target={CONTAINER_ROOT},"
            "type=bind,readonly" in spec["mounts"]
        )

    def test_session_spec_reissue_preserves_setup_pdk(self, tmp_path, monkeypatch):
        from booley.harness import init_cmd

        pdk = tmp_path / "host-cache" / "pdk"
        pdk.mkdir(parents=True)
        seen: list[Path | None | object] = []
        monkeypatch.setattr(init_cmd, "_step_nangate_pdk", lambda _ctx: pdk)
        monkeypatch.setattr(
            init_cmd,
            "_step_interactive",
            lambda _ctx, *, nangate_pdk_root: seen.append(nangate_pdk_root),
        )

        init_cmd.reissue_session_spec(tmp_path)

        assert seen == [pdk]

    def test_seeds_mask_mounts_from_sandbox_knob(self, tmp_path, monkeypatch):
        # [sandbox].mask_paths: each entry becomes a read-only empty bind over
        # its /work view — and over the /booley-project view for a
        # .booley_project subtree — and init creates the empty source dir the
        # binds need (docker --mount refuses a missing bind source).
        import subprocess

        from booley.harness import init_cmd

        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        pd = tmp_path / ".booley_project"
        pd.mkdir()
        (pd / "booley.toml").write_text(
            '[sandbox]\nmask_paths = ["secret/oracle", ".booley_project/lanes/b"]\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.setattr(init_cmd.shutil, "which", lambda _n: None)
        monkeypatch.setattr(init_cmd, "_select_interactive_app", lambda *_: "none")

        init_cmd._step_interactive(self._ctx(tmp_path))

        empty = tmp_path / "xdg" / "booley" / "empty-mask"
        assert empty.is_dir() and not any(empty.iterdir())
        spec = json.loads(
            (tmp_path / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
        )
        source = init_cmd.docker_mount_path(empty)
        assert f"source={source},target=/work/secret/oracle,type=bind,readonly" in spec["mounts"]
        assert (
            f"source={source},target=/work/.booley_project/lanes/b,type=bind,readonly"
            in spec["mounts"]
        )
        assert (
            f"source={source},target=/booley-project/lanes/b,type=bind,readonly" in spec["mounts"]
        )

    def test_invalid_mask_paths_reported_and_ignored(self, tmp_path, monkeypatch, capsys):
        # Whole-knob rejection (mirrors passthrough_env): a partially applied
        # mask list would leave the user believing a path is hidden.
        import subprocess

        from booley.harness import init_cmd

        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        pd = tmp_path / ".booley_project"
        pd.mkdir()
        (pd / "booley.toml").write_text(
            '[sandbox]\nmask_paths = ["ok/path", "../escape"]\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.setattr(init_cmd.shutil, "which", lambda _n: None)
        monkeypatch.setattr(init_cmd, "_select_interactive_app", lambda *_: "none")

        init_cmd._step_interactive(self._ctx(tmp_path))

        out = capsys.readouterr().out
        assert "mask_paths" in out and "no paths will be masked" in out
        spec = json.loads(
            (tmp_path / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
        )
        # Nothing masked — including the valid-looking entry.
        assert not any("empty-mask" in m for m in spec["mounts"])

    def test_refuses_tracked_devcontainer(self, tmp_path, monkeypatch):
        import subprocess

        from booley.harness import init_cmd

        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "t@t"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "t"],
            capture_output=True,
            check=True,
        )
        dc = tmp_path / ".devcontainer"
        dc.mkdir()
        (dc / "devcontainer.json").write_text("{}", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", ".devcontainer"], capture_output=True, check=True
        )

        ctx = self._ctx(tmp_path)
        init_cmd._step_interactive(ctx)
        statuses = {r.name: r.status for r in ctx.results}
        assert statuses.get("interactive") == "err"

    def test_check_only_writes_nothing(self, tmp_path, monkeypatch):
        import subprocess

        from booley.harness import init_cmd

        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        monkeypatch.setattr(init_cmd, "_select_interactive_app", lambda *_: "none")
        ctx = init_cmd.InitContext(
            project_root=tmp_path, check_only=True, force=False, verbose=False, interactive=False
        )
        init_cmd._step_interactive(ctx)
        assert not (tmp_path / ".devcontainer").exists()
        statuses = {r.name: r.status for r in ctx.results}
        assert statuses.get("interactive") == "warn"

    def test_app_selection_prefers_claude(self, monkeypatch):
        from booley.harness import init_cmd

        monkeypatch.setattr(init_cmd, "_detect_claude_code", lambda: True)
        monkeypatch.setattr(init_cmd, "_detect_codex", lambda: True)
        assert init_cmd._select_interactive_app() == "claude"
        monkeypatch.setattr(init_cmd, "_detect_claude_code", lambda: False)
        assert init_cmd._select_interactive_app() == "codex"
        monkeypatch.setattr(init_cmd, "_detect_codex", lambda: False)
        assert init_cmd._select_interactive_app() == "none"

    def test_app_selection_honors_project_provider(self, tmp_path, monkeypatch):
        from booley.harness import init_cmd

        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        (project_dir / "booley.toml").write_text(
            '[agent]\nprovider = "codex"\nauth = "subscription"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(init_cmd, "_detect_claude_code", lambda: True)
        monkeypatch.setattr(init_cmd, "_detect_codex", lambda: True)

        assert init_cmd._select_interactive_app(tmp_path) == "codex"

    def test_detect_vscode_cli_on_path(self, monkeypatch):
        """A `code`-style CLI on PATH yields ('cli', '<name> <version>')."""
        from booley.harness import init_cmd

        monkeypatch.setattr(
            init_cmd.shutil, "which", lambda n: "/usr/bin/code" if n == "code" else None
        )
        monkeypatch.setattr(
            init_cmd.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout="1.99.0\n", stderr=""),
        )
        state, detail = init_cmd._detect_vscode()
        assert state == "cli"
        assert detail == "code 1.99.0"

    def test_detect_vscode_gui_only(self, tmp_path, monkeypatch):
        """No CLI but a GUI config dir yields ('gui', <dir name>)."""
        from booley.harness import init_cmd

        monkeypatch.setattr(init_cmd.shutil, "which", lambda _n: None)
        (tmp_path / "Code").mkdir()
        monkeypatch.setattr(init_cmd, "_vscode_config_dirs", lambda: [tmp_path / "Code"])
        assert init_cmd._detect_vscode() == ("gui", "Code")

    def test_detect_vscode_missing(self, monkeypatch):
        """No CLI and no config dir yields ('missing', '')."""
        from booley.harness import init_cmd

        monkeypatch.setattr(init_cmd.shutil, "which", lambda _n: None)
        monkeypatch.setattr(init_cmd, "_vscode_config_dirs", lambda: [])
        assert init_cmd._detect_vscode() == ("missing", "")

    def test_seed_only_fails_closed_without_private_project_state(self, tmp_path, monkeypatch):
        """`--seed` cannot invent the writable host Project-data authority."""
        import argparse
        import subprocess

        from booley.harness import init_cmd

        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        monkeypatch.setattr(init_cmd.shutil, "which", lambda _n: None)
        monkeypatch.setattr(init_cmd, "_step_eda_tool_detection", lambda _ctx: True)
        monkeypatch.setattr(init_cmd, "_select_interactive_app", lambda *_: "none")

        args = argparse.Namespace(seed=True, check_only=False, force=False, verbose=False)
        rc = init_cmd.run_init(args, tmp_path)

        assert rc == 2
        assert not (tmp_path / ".devcontainer" / "devcontainer.json").exists()
        assert not (tmp_path / ".booley_project").exists()
