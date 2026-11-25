"""Developer Agent session smoke tests — Phase 1.3 of ADR 0012.

These tests exercise Booley Flows and MCP server entry points from a "no ticket"
context: no ``BOOLEY_SLUG``, no ``BOOLEY_STATE_FILE``, no development
state file on disk.  They protect the Developer Agent session happy path from
regressions caused by future state-file assumptions slipping into Flow
code paths, and ``booley init``'s Sandbox step that seeds such a session.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from booley.config.host_config import host_config_path
from booley.core.booley_dir import reset_cache
from booley.flows.implementation_comparison import target_pair_plans_for_handles
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.harness import init_cmd
from booley.harness.nangate_pdk import CONTAINER_ROOT
from booley.mcp.base import EXIT_ERROR, EXIT_SUCCESS
from booley.runtime import auth_token, devcontainer
from booley.runtime.endpoint_execution import ExecutionResult
from booley.targets.catalog import TargetCatalog
from tests.conftest import symlink_or_skip
from tests.docker_cli_guard import docker_cli_absent
from tests.git_support import git, repository
from tests.harness.init_agent_support import empty_host, seed_init, stub_host_images
from tests.harness.session_docker_fake import headless_session_entry
from tests.path_tool_fake import PathToolFake
from tests.session_issuance_support import SANDBOX_IMAGE_ID, HostDocker, serve_host_docker
from tests.target_test_support import write_sim_preview_project

_BOOLEY_ENV_VARS = (
    "BOOLEY_CONTROL_PROJECT_ROOT",
    "BOOLEY_SLUG",
    "BOOLEY_STATE_FILE",
    "BOOLEY_LOGS_DIR",
    "BOOLEY_PROJECT_ROOT",
    "BOOLEY_DIR",
)


@pytest.fixture(autouse=True)
def _clear_ticket_env() -> None:
    """Strip ticket-mode env vars so each test runs in a clean
    Developer Agent session context — the harness sets these in Ticket Mode;
    their presence would silently re-enable state-file gating and defeat the test.

    Uses a manual save/restore (rather than ``monkeypatch.delenv``) because
    the job-logs test below mutates ``os.environ`` through the MCP server, and
    ``monkeypatch`` does not track direct ``os.environ`` writes — without this
    fixture they leaked into adjacent test modules and corrupted ``reviewer``'s
    session-file persistence path.
    """
    saved = {k: os.environ.get(k) for k in _BOOLEY_ENV_VARS}
    for k in _BOOLEY_ENV_VARS:
        os.environ.pop(k, None)
    reset_cache()
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_cache()


def _project_with_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, targets: dict[str, list[str]]
) -> None:
    """Create a no-ticket Project with the sim *targets* and chdir into it."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.chdir(tmp_path)
    write_sim_preview_project(tmp_path, targets)


def _execute(tmp_path: Path, **request: Any) -> ExecutionResult:
    """Run ``SimulateFlow.execute`` the way the MCP dispatch path does."""
    return SimulateFlow().execute(
        SimRequest(work_dir=tmp_path, report_dir=tmp_path / "reports", **request)
    )


def test_simulate_without_a_target_names_the_eligible_ones(tmp_path, monkeypatch) -> None:
    _project_with_targets(tmp_path, monkeypatch, {"lite": [], "full": []})
    result = _execute(tmp_path)
    assert result.exit_code == EXIT_ERROR
    assert "--target is required" in result.outcome.report_text
    assert "Eligible Targets: full, lite" in result.outcome.report_text


def test_dry_run_succeeds_without_state_file(tmp_path, monkeypatch) -> None:
    _project_with_targets(tmp_path, monkeypatch, {"lite": ["smoke", "stress"]})
    result = _execute(tmp_path, target="lite", dry_run=True)

    assert result.exit_code == EXIT_SUCCESS, result.outcome.report_text
    assert result.outcome.report_text == "lite: 2 pending work item(s)"  # one per Test
    assert result.outcome.detail["mutated"] is False
    assert not (tmp_path / "reports/sim/1").exists()


@pytest.mark.parametrize("layout", ["project", "configured", "no-project"])
def test_server_start_keeps_job_logs_in_the_booley_directory(tmp_path, monkeypatch, layout):
    from booley.mcp import server as mcp_server

    if layout != "no-project":
        (tmp_path / ".booley").mkdir()
    configured = tmp_path / "ticket_logs"
    if layout == "configured":
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(configured))
    monkeypatch.chdir(tmp_path)

    # private: the server's only entry point, `main()`, then serves MCP on stdio until EOF.
    mcp_server._maybe_configure_job_logs_dir()

    logs = os.environ.get("BOOLEY_LOGS_DIR")
    if layout == "project":
        assert Path(logs).is_dir()
        assert Path(logs).parent == (tmp_path / ".booley" / "job_logs").resolve()
    else:
        assert logs == (str(configured) if layout == "configured" else None)


@pytest.mark.parametrize("layout", ["no-project", "missing-main", "unresolvable"])
def test_probe_reports_no_log_location_without_a_usable_booley_directory(
    tmp_path, monkeypatch, request, layout
):
    from booley.config.agent import set_agent_settings
    from booley.mcp import server as mcp_server

    request.addfinalizer(lambda: set_agent_settings(None))
    monkeypatch.chdir(tmp_path)
    if layout == "missing-main":
        monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / "gone"))
    elif layout == "unresolvable":
        # An unreadable Booley Directory also stops the catalog build: only the lookup fails.
        monkeypatch.setattr(mcp_server, "resolve_booley_dir", Mock(side_effect=OSError("denied")))

    payload = mcp_server.build_mcp_probe_payload()

    assert (payload["logs_dir"], payload["logs_dir_writable"]) == ("", False)


def test_baseline_uses_worktree_and_leaves_tree_untouched(tmp_path: Path) -> None:
    """`--baseline` synthesizes a past ref in a throwaway ``git worktree``, so it works in a
    Developer Agent session (no ``BOOLEY_SLUG``) without disturbing the user's tree."""
    from booley.flows.synth.flow import AsicSynthesizeFlow, SynthMetrics
    from booley.runtime.endpoint_execution import EndpointOutcome

    repo = repository(tmp_path / "repo")
    marker = repo / "rtl.v"
    marker.write_text("// baseline version\n", encoding="utf-8")
    cores_dir = repo / ".booley" / "cores"
    cores_dir.mkdir(parents=True)
    (cores_dir / "interactive.core").write_text(
        "CAPI=2:\nname: ::interactive:0\ntargets:\n  synth_default:\n"
        "    flow: generic\n    flow_options: {tool: yosys}\n    toplevel: dut\n",
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline")
    marker.write_text("// current version\n", encoding="utf-8")
    git(repo, "commit", "-qam", "current")
    # An UNCOMMITTED working-tree edit that an in-place checkout would clobber.
    marker.write_text("// uncommitted edits — MUST survive\n", encoding="utf-8")
    before = marker.read_text(encoding="utf-8")

    tool = AsicSynthesizeFlow()
    tool.parse_args(["--work-dir", str(repo), "--target", "synth_default", "--baseline", "HEAD~1"])
    seen_work_dirs: list[Path] = []

    def fake_single(target: str, *, baseline: bool = False):
        # Capture the tree the baseline run reads from (the worktree, not repo).
        seen_work_dirs.append(Path(tool.args.work_dir))
        return SynthMetrics(returncode=0), "baseline output"

    # private: the public `execute` runs Yosys for both refs; only the baseline step, which
    # owns the worktree, is driven here, with the Yosys run replaced.
    with patch.object(tool, "_run_single_config", side_effect=fake_single):
        handle = TargetCatalog.build(repo).select("synth_default", for_flow="synth")
        plans = target_pair_plans_for_handles((handle,), flow="synth")
        tool._target_pairs = plans
        results, short_sha = tool._run_baseline_configs(plans)

    assert not isinstance(results, EndpointOutcome), results.report_text
    assert "synth_default" in results
    assert short_sha  # HEAD~1 resolved to a short sha
    assert seen_work_dirs and ".baseline-wt-" in str(seen_work_dirs[0])
    assert marker.read_text(encoding="utf-8") == before
    assert Path(tool.args.work_dir) == repo
    assert not seen_work_dirs[0].exists()


# booley init: the Sandbox step (ADR 0018), with Sandbox Issuance run for real against
# the shared in-process Docker host of `tests/session_issuance_support.py`.


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HostDocker:
    """An empty host whose Docker holds the Sandbox Image and no container or network."""
    return serve_host_docker(empty_host(tmp_path, monkeypatch), monkeypatch)


@pytest.fixture
def project(host: HostDocker, tmp_path: Path) -> Path:
    """A Git checkout with its Booley Directory."""
    root = repository(tmp_path / "project")
    (root / ".booley").mkdir()
    return root


def _sandbox(project: Path, *, check_only: bool = False, **options: Any) -> init_cmd.StepResult:
    """Run ``booley init``'s Sandbox step once and return what it recorded."""
    ctx = init_cmd.InitContext(project_root=project, check_only=check_only, interactive=False)
    # private: `run_init` reaches this step only behind Host Bootstrap, the Sandbox Image
    # and the agent-selection write, reports it only as printed lines, and passes full init's
    # `init_will_create_booley_dir` only on a full run; the two `run_init` tests below
    # drive it whole.
    init_cmd._step_sandbox(ctx, **options)
    return ctx.results[-1]


def _spec(project: Path) -> dict[str, Any]:
    return json.loads(devcontainer.devcontainer_path(project).read_text(encoding="utf-8"))


def _tree(root: Path) -> dict[Path, tuple[bytes, int, int] | None]:
    """Every file (content, mode, mtime) and directory under *root*."""
    tree: dict[Path, tuple[bytes, int, int] | None] = {}
    for path in root.rglob("*"):
        info = path.stat()
        tree[path] = (
            (path.read_bytes(), info.st_mode, info.st_mtime_ns) if path.is_file() else None
        )
    return tree


def test_spec_is_untracked_and_excluded(project: Path, monkeypatch) -> None:
    # Stealth must be explicit for the Booley Directory to join the exclusions.
    (project / ".booley" / "booley.toml").write_bytes(b"[stealth]\nenabled = true\n")
    # Only the host's `booley` launcher stays on PATH: neither Docker nor Git.
    monkeypatch.setenv("PATH", os.environ["PATH"].split(os.pathsep)[0])

    result = _sandbox(project)

    assert (result.status, result.detail) == ("ok", "app=none")
    assert _spec(project)["remoteUser"] == "agent"
    assert not (project / ".gitignore").exists()
    exclude = (project / ".git" / "info" / "exclude").read_text(encoding="utf-8").split()
    assert {"/.devcontainer", "/.booley", "/.claude"} <= set(exclude)
    assert not (project / ".mcp.json").exists()


def test_spec_mounts_the_setup_pdk_cache_read_only(project: Path, tmp_path: Path) -> None:
    pdk = tmp_path / "host-cache" / "pdk"
    pdk.mkdir(parents=True)

    _sandbox(project, nangate_pdk_root=pdk)

    mount = f"source={init_cmd.docker_mount_path(pdk)},target={CONTAINER_ROOT},type=bind,readonly"
    assert mount in _spec(project)["mounts"]


def test_spec_applies_the_host_egress_mode(project: Path) -> None:
    host_config_path().parent.mkdir(parents=True, exist_ok=True)
    host_config_path().write_text(
        '[sandbox]\negress = "unproxied"\n', encoding="utf-8", newline="\n"
    )

    _sandbox(project)

    spec = _spec(project)
    assert devcontainer.UNPROXIED_NETWORK in spec["runArgs"]
    assert spec["containerEnv"]["BOOLEY_EGRESS"] == "unproxied"
    assert "HTTP_PROXY" not in spec["remoteEnv"]


def test_mask_paths_become_read_only_empty_binds(project: Path) -> None:
    # Each entry hides its /work view, and the /booley view of a .booley subtree; init
    # creates the empty source dir (docker --mount refuses a missing bind source).
    (project / ".booley" / "booley.toml").write_text(
        '[sandbox]\nmask_paths = ["secret/oracle", ".booley/lanes/b"]\n',
        encoding="utf-8",
        newline="\n",
    )

    _sandbox(project)

    empty = auth_token.config_dir() / "empty-mask"
    assert empty.is_dir() and not any(empty.iterdir())
    source = init_cmd.docker_mount_path(empty)
    for target in ("/work/secret/oracle", "/work/.booley/lanes/b", "/booley/lanes/b"):
        assert f"source={source},target={target},type=bind,readonly" in _spec(project)["mounts"]


def test_invalid_mask_paths_reported_and_ignored(project: Path, capsys) -> None:
    # Whole-knob rejection (mirrors passthrough_env): a partially applied
    # mask list would leave the user believing a path is hidden.
    (project / ".booley" / "booley.toml").write_text(
        '[sandbox]\nmask_paths = ["ok/path", "../escape"]\n', encoding="utf-8", newline="\n"
    )

    _sandbox(project)

    out = capsys.readouterr().out
    assert "mask_paths" in out and "no paths will be masked" in out
    assert not any("empty-mask" in mount for mount in _spec(project)["mounts"])


@pytest.mark.parametrize("check_only", [False, True])
def test_refuses_tracked_devcontainer(project: Path, check_only: bool) -> None:
    (project / ".devcontainer").mkdir()
    (project / ".devcontainer" / "devcontainer.json").write_text("{}", encoding="utf-8")
    git(project, "add", ".devcontainer")

    assert _sandbox(project, check_only=check_only).status == "err"


def _remove_spec(project: Path, _docker: HostDocker) -> None:
    devcontainer.devcontainer_path(project).unlink()


def _stale_spec(project: Path, _docker: HostDocker) -> None:
    devcontainer.devcontainer_path(project).write_bytes(b"{}\n")


def _drop_exclusion(project: Path, _docker: HostDocker) -> None:
    exclude = project / ".git" / "info" / "exclude"
    exclude.write_bytes(exclude.read_bytes().replace(b"/.claude\n", b""))


def _stopped_runtime(project: Path, docker: HostDocker) -> None:
    stopped = headless_session_entry("stopped", project, running=False)
    docker.containers["booley-session-"] = json.dumps(stopped)


def _orphaned_relay(_project: Path, docker: HostDocker) -> None:
    docker.networks.add("booley-license-private-")


@pytest.mark.parametrize(
    ("issued", "damage", "check_only", "status", "detail"),
    [
        (True, None, False, "current", "current"),
        (True, None, True, "current", "current"),
        (False, None, True, "warn", "specification/issuance"),
        (True, _remove_spec, True, "warn", "specification/issuance"),
        (True, _stale_spec, True, "warn", "specification/issuance"),
        (True, _drop_exclusion, True, "warn", "Git exclusions"),
        (True, _stopped_runtime, True, "warn", "stopped Sandbox resources"),
        (True, _orphaned_relay, True, "warn", "orphaned license relay"),
    ],
    ids=["rerun", "current", "never-issued", "no-spec", "stale", "exclusion", "runtime", "relay"],
)
def test_rerun_reports_pending_work_and_check_only_does_none_of_it(
    host, project, tmp_path, monkeypatch, issued, damage, check_only, status, detail
):
    (project / ".booley" / "booley.toml").write_bytes(b'[sandbox]\nmask_paths = ["secret"]\n')
    if issued:
        _sandbox(project)
    if damage is not None:
        damage(project, host)
    PathToolFake.install(tmp_path / "bin", "docker", monkeypatch)  # the relay is looked for
    for path in tmp_path.rglob("*"):  # an old timestamp, so any rewrite shows
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    before, host.calls[:] = _tree(tmp_path), []

    result = _sandbox(project, check_only=check_only)

    assert result.status == status and detail in result.detail
    assert _tree(tmp_path) == before
    assert host.mutations == []


@pytest.mark.parametrize(
    ("entry", "override", "creates", "owned"),
    [
        (None, None, True, True),
        (None, "<own>", True, True),
        (None, "", True, True),
        (None, None, False, False),
        (None, "relative/project-data", True, False),
        (None, "/external/project-data", True, False),
        ("file", "<own>", True, False),
        ("symlink", "<own>", True, False),
        ("unreadable", "<own>", True, False),
        ("dir", "<own>", True, False),
    ],
)
def test_missing_project_data_is_pending_only_when_full_init_creates_it(
    host, tmp_path, monkeypatch, entry, override, creates, owned
):
    project = repository(tmp_path / "project")
    target = init_cmd.booley_dir_for_init(project)
    if override is not None:
        monkeypatch.setenv("BOOLEY_DIR", str(target) if override == "<own>" else override)
    if entry == "file":
        target.write_text("not a directory", encoding="utf-8")
    elif entry == "symlink":
        (tmp_path / "project-data").mkdir()
        symlink_or_skip(target, tmp_path / "project-data", target_is_directory=True)
    elif entry == "dir":
        target.mkdir()
        host.image = None  # the preview fails: no Sandbox Image to pin
    elif entry == "unreadable":
        real_lstat = Path.lstat
        monkeypatch.setattr(
            Path, "lstat", lambda path: _deny(path) if path == target else real_lstat(path)
        )

    result = _sandbox(
        project,
        check_only=True,
        nangate_pdk_root=tmp_path / "pdk",
        init_will_create_booley_dir=creates,
    )

    pending = (result.status, result.detail) == ("warn", "Booley Directory would be created first")
    assert pending is owned
    assert entry != "dir" or result.status == "err"


def _deny(_path: Path) -> os.stat_result:
    raise PermissionError("denied")


def _current_image(project: Path) -> init_cmd.LifecycleResult:
    return init_cmd.LifecycleResult(
        selected_reference=init_cmd.pi.project_image_name(project),
        selected_id=SANDBOX_IMAGE_ID,
        status=init_cmd.ImageLifecycleStatus.CURRENT,
    )


@pytest.mark.parametrize(
    ("seed", "image", "rc", "verdict"),
    [
        (False, True, 1, "pending or unresolved"),
        (False, False, 2, "failed"),
        (True, None, 2, None),
    ],
    ids=["check-only", "check-only-without-image", "seed-on-a-refused-host"],
)
def test_init_on_a_fresh_checkout_writes_nothing(
    tmp_path, monkeypatch, capsys, seed, image, rc, verdict
):
    """Check-only reports the Sandbox as pending on the Directory; a refused seed stops.

    The seed row keeps the real Host Bootstrap, which refuses this host (a source
    checkout without Docker) before any write.
    """
    empty_host(tmp_path, monkeypatch)
    docker_cli_absent(monkeypatch)
    project = repository(tmp_path / "project")
    if image is not None:
        stub_host_images(monkeypatch, _current_image(project) if image else None)

    assert seed_init(project, seed=seed, check_only=not seed, provider="codex") == rc

    assert [path.name for path in project.iterdir()] == [".git"]
    out = capsys.readouterr().out
    if verdict is not None:
        assert "interactive — Booley Directory would be created first" in out
        assert f"Checked initialization: {verdict}" in out


@pytest.mark.parametrize("check_only", [False, True])
@pytest.mark.parametrize("explicit_override", [False, True])
def test_seed_without_project_data_fails_closed(
    host, tmp_path, monkeypatch, capsys, explicit_override, check_only
):
    project = repository(tmp_path / "project")
    booley_dir = init_cmd.booley_dir_for_init(project)
    if explicit_override:
        monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))
    monkeypatch.setattr(init_cmd.nangate_pdk, "is_ready", lambda: True)
    monkeypatch.setattr(init_cmd.nangate_pdk, "cache_root", lambda: tmp_path / "pdk")
    stub_host_images(monkeypatch, _current_image(project))

    assert seed_init(project, check_only=check_only, provider="codex") == 2
    failed = "interactive — " if check_only else "agent_config — Booley Directory missing"
    assert f"✗  {failed}" in capsys.readouterr().out
    assert not booley_dir.exists()
    assert not devcontainer.devcontainer_path(project).exists()


@pytest.mark.parametrize("provider", [None, "codex"])
def test_sandbox_app_is_the_declared_provider_never_an_installed_cli(
    project, tmp_path, monkeypatch, provider
):
    for cli in ("claude", "codex"):
        PathToolFake.install(tmp_path / "bin", cli, monkeypatch)
    if provider is not None:
        (project / ".booley" / "booley.toml").write_text(
            f'[agent]\nprovider = "{provider}"\nauth = "subscription"\n',
            encoding="utf-8",
            newline="\n",
        )

    assert _sandbox(project).detail == f"app={provider or 'none'}"
