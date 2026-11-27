"""Tests for the in-container MCP registrar (ADR 0018 WS4, ADR 0023 HTTP)."""

from __future__ import annotations

import builtins
import functools
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import tomli

from booley.harness import incontainer_register as entry
from booley.mcp import catalog
from booley.runtime import (
    auth_token,
    booley_dir,
    mount_history,
    paths,
    sandbox_layout,
    worktree_mount_repair,
)
from booley.runtime import devcontainer as dc
from booley.runtime import incontainer_setup as reg
from booley.runtime.worktree_mount_repair import MountRepair, RegistrationProblem
from tests.codex_config_support import count_parses, tomlkit_copy
from tests.harness.agent_auth_support import pin_venue
from tests.mcp_server_launch_fake import McpServerLaunchFake
from tests.t3code_fake import serve_t3_code, t3_listener, write_t3_runtime

_GATEWAY_KEYS = (
    "BOOLEY_GATEWAY_URL",
    "BOOLEY_GATEWAY_TOKEN",
    "BOOLEY_GATEWAY_TOKEN_COMMAND",
    "BOOLEY_GATEWAY_MODEL",
    "BOOLEY_GATEWAY_EFFORT",
)
_START_ENV = (
    *sorted(auth_token.CREDENTIAL_ENV_KEYS),
    *_GATEWAY_KEYS,
    "BOOLEY_AGENT_APP",
    "BOOLEY_AGENT_CLIS",
    "BOOLEY_PRIMARY_AUTH",
    "BOOLEY_CONTAINER",
    "BOOLEY_MCP_DEVELOPER_AGENT",
    "CODEX_HOME",
    "RTL_PROJECT_ROOT",
)
# Bound before the autouse stubs below replace them, for the tests of the UI start.
_REAL_ENSURE_T3_SERVER = reg.ensure_t3_server
_REAL_ENSURE_DASHBOARD_SERVER = getattr(reg, "ensure_dashboard_server", None)


@pytest.fixture(autouse=True)
def first_run(tmp_path_factory, monkeypatch):
    """Every test is a Sandbox start: a fresh home, Project and Booley Directory, no credential.

    The Project is the working directory and the Booley Directory is also the Sandbox's
    mount point, with no mount history: worktree repair runs and finds nothing to do. Its
    name is non-ASCII and astral, so every Codex trust key it gets must be a TOML, not a
    JSON, escape (#2258). The MCP server launch is the shared command fake; the automatic
    Doctor is a detached worker, so a stand-in records whether the server already served.
    """
    for name in _START_ENV:
        monkeypatch.delenv(name, raising=False)
    booley_dir.reset_cache()
    home, project, booley = (
        tmp_path_factory.mktemp(name)
        for name in ("home", "project", "booley-caf\u00e9-\U0001f600")
    )
    monkeypatch.chdir(project)
    for name, value in (("PWD", project), ("HOME", home), ("BOOLEY_DIR", booley)):
        monkeypatch.setenv(name, str(value))
    monkeypatch.setattr(sandbox_layout, "BOOLEY_DIR_TARGET", str(booley))
    # The server log's default is one fixed host path and no entry point sets it; each start
    # gets its own, with the real function running.
    log = str(home.parent / f"{home.name}-server.log")
    start = functools.partial(reg.ensure_http_server, log_path=log)
    monkeypatch.setattr(reg, "ensure_http_server", start)
    doctor: list[bool] = []
    with McpServerLaunchFake.installed(monkeypatch) as server:
        monkeypatch.setattr(
            entry, "launch_auto_doctor", lambda: doctor.append(server.listening) or "started"
        )
        keys = (str(project.resolve()), str(booley.resolve()))
        yield SimpleNamespace(
            home=home,
            project=project,
            root=keys[0],
            booley=keys[1],
            booley_dir=booley,
            keys=keys,
            server=server,
            doctor=doctor,
        )
    booley_dir.reset_cache()


@pytest.fixture(autouse=True)
def _t3_code_answers(monkeypatch):
    """No test here starts the real `t3` or `booley dashboard`: both already answer.

    The T3 Code fake replaces the port and descriptor probes and the `subprocess` that the
    MCP server launch fake drives, so only the T3 Code start tests install it, through `_t3_code`.
    """
    monkeypatch.setattr(reg, "ensure_t3_server", lambda _root: "running")
    monkeypatch.setattr(reg, "ensure_dashboard_server", lambda _root: "running", raising=False)


def _t3_code(monkeypatch, **kwargs):
    """Run the real T3 Code and Dashboard starts against the T3 Code fake; return the fake."""
    monkeypatch.setattr(reg, "ensure_t3_server", _REAL_ENSURE_T3_SERVER)
    monkeypatch.setattr(
        reg, "ensure_dashboard_server", _REAL_ENSURE_DASHBOARD_SERVER, raising=False
    )
    return serve_t3_code(monkeypatch, **kwargs)


def _main(capsys) -> tuple[int, str]:
    """Run the registrar hook; return its exit code and what it printed to stderr."""
    try:
        entry.main()
    except SystemExit as exited:
        return exited.code, capsys.readouterr().err
    return 0, capsys.readouterr().err


def _seed_codex(home, content):
    """Write *content* (str or bytes) as the Codex config, byte for byte."""
    path = reg.codex_config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path


def _files(home):
    """Every file and link under *home* (a deployed skill is a link to a directory)."""
    return [path for path in home.rglob("*") if path.is_symlink() or not path.is_dir()]


@pytest.mark.parametrize("existing", [None, 'model="existing"\n', "broken=["])
@pytest.mark.parametrize("failure", ["missing", "broken-import", "too-old", "no-version"])
def test_codex_incompatible_editor_fails_before_any_write(
    tmp_path, monkeypatch, existing, failure
):
    path = reg.codex_config_path(tmp_path)
    if existing is not None:
        _seed_codex(tmp_path, existing)
    real_import = builtins.__import__

    def import_editor(name, *args, **kwargs):
        if name == "tomlkit" and failure == "broken-import":
            raise ImportError("partial editor installation")
        return real_import(name, *args, **kwargs)

    editor = {
        "missing": None,
        "too-old": tomlkit_copy("0.13.2"),
        "no-version": tomlkit_copy("dev"),
    }.get(failure)
    monkeypatch.setattr(builtins, "__import__", import_editor)
    monkeypatch.setitem(sys.modules, "tomlkit", editor)

    with pytest.raises(RuntimeError, match=r"installation.*tomlkit>=0\.15\.2") as error:
        reg.register("codex", home=tmp_path)

    assert "repair this file" not in str(error.value)
    assert _files(tmp_path) == ([] if existing is None else [path])
    assert existing is None or path.read_text(encoding="utf-8") == existing


def test_codex_registration_accepts_a_suffixed_editor_version(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "tomlkit", tomlkit_copy("0.16.0rc1"))

    assert reg.register("codex", home=tmp_path).startswith("codex:written")


# ===========================================================================
# The registrar hook (main)
# ===========================================================================


def test_hook_starts_the_server_registers_and_then_launches_doctor(first_run, monkeypatch, capsys):
    monkeypatch.setenv("BOOLEY_AGENT_APP", "claude")
    monkeypatch.delenv("PWD")  # without its spelling, the hook trusts its working directory

    code, err = _main(capsys)

    assert code == 0
    assert "server:started t3:running" in err and "health:started claude:written" in err
    # Doctor checks the server, so it launches only once the server serves.
    assert first_run.doctor == [True]
    projects = _claude_config(first_run.home)["projects"]
    for key in first_run.keys:
        assert projects[key]["hasTrustDialogAccepted"] is True


@pytest.mark.parametrize(
    ("outcome", "status", "warning"),
    [
        (None, "current", None),
        ("{", "failed", "worktree pointers not repaired: malformed mount history"),
        (MountRepair(repaired=(Path("a"),)), "repaired", None),
        (MountRepair(pruned=(Path("a"),)), "repaired", None),
        (
            MountRepair(problems=(RegistrationProblem(Path("a"), Path("b"), "gone"),)),
            "problems",
            "worktree registration a (b): gone",
        ),
        (OSError("denied"), "failed", "worktree pointers not repaired: denied"),
    ],
    ids=["no-history", "malformed-history", "repaired", "pruned", "problems", "os-error"],
)
def test_hook_repairs_worktree_pointers_first_and_never_stops_on_them(
    first_run, monkeypatch, capsys, outcome, status, warning
):
    monkeypatch.setenv("BOOLEY_AGENT_APP", "claude")
    if isinstance(outcome, str):
        history = first_run.booley_dir / mount_history.MOUNT_HISTORY
        history.parent.mkdir()
        history.write_text(outcome, encoding="utf-8")
    repairs = []
    real_repair = worktree_mount_repair.repair_mount_registrations

    def repair(root):
        repairs.append((root, first_run.server.listening))
        # The repair's own tests drive real worktree registrations; these rows stand in
        # for the outcomes that need them, to test how the hook reports each one.
        if isinstance(outcome, Exception):
            raise outcome
        return outcome if isinstance(outcome, MountRepair) else real_repair(root)

    monkeypatch.setattr(worktree_mount_repair, "repair_mount_registrations", repair)

    code, err = _main(capsys)

    # From the hook's own directory, before the server starts.
    assert repairs == [(first_run.project, False)]
    assert code == 0 and f"worktrees:{status} server:started" in err
    assert (warning in err) if warning else "warning: worktree" not in err


@pytest.mark.parametrize("app", ["none", "", "gemini"])
def test_hook_without_an_agent_cli_starts_and_writes_nothing(first_run, monkeypatch, capsys, app):
    monkeypatch.setenv("BOOLEY_AGENT_APP", app)

    code, err = _main(capsys)

    assert code == 0 and "server:skipped" in err
    assert first_run.server.launches == []
    assert not list(first_run.home.iterdir())


@pytest.mark.parametrize(
    ("name", "registered", "fake", "expected"),
    [
        pytest.param(None, None, {}, ("started", 1, 0, "work", 0), id="registered"),
        pytest.param(None, "work", {}, ("started", 1, 0, "work", 0), id="already-registered"),
        pytest.param(
            None,
            None,
            {"add_replies": [(78, "not ready"), (0, "Added")]},
            ("started", 2, 0, None, 0),
            id="server-not-ready",
        ),
        pytest.param(
            None,
            None,
            {"add_replies": [(1, "boom")]},
            ("unregistered", 1, 0, None, 1),
            id="refused",
        ),
        pytest.param(
            None,
            None,
            {"add_replies": [OSError("boom")]},
            ("unregistered", 1, 0, None, 1),
            id="run-error",
        ),
        pytest.param("picorv32", None, {}, ("started", 1, 0, "picorv32", 0), id="titled"),
        pytest.param("picorv32", "work", {}, ("started", 1, 1, "picorv32", 0), id="retitled"),
        pytest.param(
            "picorv32", "picorv32", {}, ("started", 1, 1, "picorv32", 0), id="already-named"
        ),
        pytest.param(
            "picorv32",
            "work",
            {"rename_reply": (1, "boom")},
            ("started", 1, 1, "work", 1),
            id="rename-refused",
        ),
        pytest.param(
            "picorv32",
            "work",
            {"rename_reply": OSError("boom")},
            ("started", 1, 1, "work", 1),
            id="rename-run-error",
        ),
    ],
)
def test_ensure_t3_server_registers_the_project_under_its_name(
    tmp_path, monkeypatch, capsys, name, registered, fake, expected
):
    status, adds, renames, title, lines = expected
    project, booley = tmp_path / "work", tmp_path / ".booley"
    project.mkdir()
    booley.mkdir()
    if name:
        (booley / "booley.toml").write_bytes(f'[project]\nname = "{name}"\n'.encode())
    monkeypatch.setenv("BOOLEY_DIR", str(booley))
    registrations = {str(project): registered} if registered else {}
    volume = tmp_path / "t3-volume"
    t3 = _t3_code(monkeypatch, projects=registrations, volume=volume, **fake)
    stale = write_t3_runtime(volume, pid=4_194_305, port=3773, environment_id="env-fake")

    assert reg.ensure_t3_server(project) == status
    assert t3.replaced_records == [stale]  # untouched until T3 Code replaced it
    base = ["--base-dir", reg.T3_BASE_DIR]
    assert t3.renamed == [["t3", "project", "rename", *base, str(project), name]] * renames
    add = ["t3", "project", "add", *base, *(["--title", name] if name else []), str(project)]
    assert t3.added == [add] * adds
    assert t3.projects.get(str(project)) == title
    assert t3.checked == [["t3", "policy", "check", "/etc/t3code/policy.json"]]
    err = capsys.readouterr().err
    assert "boom" not in err and err.count("\n") == lines


@pytest.mark.skipif(sys.platform == "win32", reason="NTFS would allocate the sparse log files")
def test_t3_code_restarts_with_its_logs_cut_to_the_cap(tmp_path, monkeypatch, capsys):
    volume = tmp_path / "t3-volume"
    t3 = _t3_code(monkeypatch, volume=volume)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
    userdata = volume / "userdata"
    logs = userdata / "logs"
    data = ["statev2.sqlite", "statev2.sqlite-wal", "statev2.sqlite-shm", "secrets/pairing"]
    data = [userdata / name for name in data]
    for path in data:
        _write_dated(path, 0, path.name.encode())

    assert reg.ensure_t3_server(tmp_path) == "started"  # no log directory yet
    assert "not pruned" not in capsys.readouterr().err

    t3.running = False  # the container stopped
    snapshot = logs / "server-1-0.heapsnapshot"
    _write_dated(snapshot, 0, b"heap")
    oldest = logs / "provider" / "events.thread-a.log.1"
    newest = ["server.trace.ndjson.1", "terminals/term.log", "server.trace.ndjson"]
    newest = [logs / name for name in newest]
    for mtime, path in enumerate([oldest, *newest], start=1):
        _write_dated(path, mtime, size=60_000_000)

    assert reg.ensure_t3_server(tmp_path) == "started"
    assert {path for path in logs.rglob("*") if path.is_file()} == {*newest, snapshot}
    assert sum(path.stat().st_size for path in newest) <= 200_000_000
    assert [path.read_bytes() for path in data] == [path.name.encode() for path in data]
    assert snapshot.read_bytes() == b"heap"


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs a POSIX directory mode that stops the deletion",
)
def test_t3_code_starts_when_its_logs_cannot_be_pruned(tmp_path, monkeypatch, capsys):
    volume = tmp_path / "t3-volume"
    _t3_code(monkeypatch, volume=volume)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
    logs = volume / "userdata" / "logs"
    _write_dated(logs / "server.trace.ndjson", 1, size=250_000_000)
    logs.chmod(0o500)
    try:
        assert reg.ensure_t3_server(tmp_path) == "started"
    finally:
        logs.chmod(0o700)
    assert "T3 Code logs not pruned" in capsys.readouterr().err


def _write_dated(path: Path, mtime: int, content: bytes = b"", *, size: int | None = None) -> None:
    """Write *path* with a fixed modification time; *size* extends it sparsely."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as file:
        file.write(content)
        if size is not None:
            file.truncate(size)
    os.utime(path, (mtime, mtime))


@pytest.mark.parametrize(
    ("listener", "pid", "port_offset", "status"),
    [
        pytest.param("env-own", "live", 0, "running", id="own"),
        pytest.param("env-other", "live", 0, "port-taken", id="other-base"),
        pytest.param("not-t3", "live", 0, "port-taken", id="not-t3"),
        pytest.param("drip", "live", 0, "port-taken", id="drip"),
        pytest.param("banner", "live", 0, "port-taken", id="banner"),
        pytest.param("nested", "live", 0, "port-taken", id="nested"),
        pytest.param("oversized", "live", 0, "port-taken", id="oversized"),
        pytest.param("env-own", None, 0, "port-taken", id="no-record"),
        pytest.param("env-own", 4_194_305, 0, "port-taken", id="dead-pid"),
        pytest.param("env-own", "live", 1, "port-taken", id="wrong-port"),
    ],
)
def test_only_its_own_t3_code_on_the_port_counts_as_running(
    first_run, tmp_path, monkeypatch, capsys, listener, pid, port_offset, status
):
    volume = tmp_path / "t3-volume"
    monkeypatch.setattr(reg, "T3_BASE_DIR", str(volume))
    monkeypatch.setattr(reg, "ensure_t3_server", _REAL_ENSURE_T3_SERVER)
    with t3_listener(listener) as port:
        monkeypatch.setattr(reg, "T3_PORT", port)  # a test cannot bind T3 Code's fixed port
        write_t3_runtime(
            volume,
            pid=os.getpid() if pid == "live" else pid,
            port=port + port_offset,
            environment_id="env-own",
        )

        assert reg.ensure_t3_server(first_run.project) == status

    assert ("held by another process" in capsys.readouterr().err) == (status == "port-taken")


@pytest.mark.parametrize(
    ("reply", "reported"),
    [((77, "allowedDrivers: unknown driver"), "unknown driver"), (OSError("no t3"), "OSError")],
    ids=["rejected", "run-error"],
)
def test_a_rejected_t3_code_policy_stops_t3_serve(tmp_path, monkeypatch, capsys, reply, reported):
    t3 = _t3_code(monkeypatch, check_reply=reply)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))

    assert reg.ensure_t3_server(tmp_path) == "policy-rejected"
    assert t3.served == []
    err = capsys.readouterr().err
    assert "t3 policy check" in err and reported in err


def test_a_t3_code_failure_never_stops_registration(tmp_path, monkeypatch, capsys):
    _t3_code(monkeypatch).spawn_error = OSError("no t3")
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
    monkeypatch.setenv("BOOLEY_AGENT_APP", "claude")
    monkeypatch.setattr(reg, "ensure_http_server", lambda: "running")
    monkeypatch.setattr(reg, "register", lambda _app, *, trust_paths=(): "claude:current")
    monkeypatch.setattr(entry, "sandbox_trust_paths", lambda _start: ())
    monkeypatch.setattr(entry, "observe_upgrade", lambda: "current")
    monkeypatch.setattr(entry, "launch_auto_doctor", lambda: "started")

    entry.main()

    assert re.search(r"t3:failed dashboard:failed .* claude:current", capsys.readouterr().err)


@pytest.mark.parametrize("no_ui", [False, True], ids=["ui", "no-ui"])
def test_the_start_step_starts_the_dashboard_once_unless_no_ui(
    first_run, tmp_path, monkeypatch, capsys, no_ui
):
    t3 = _t3_code(monkeypatch, volume=tmp_path / "t3-volume")
    monkeypatch.setenv("BOOLEY_AGENT_APP", "claude")
    if no_ui:
        monkeypatch.setenv("BOOLEY_NO_UI", "1")

    errors = [_main(capsys)[1], _main(capsys)[1]]

    command = [sys.executable, "-m", "booley.harness.booley", "dashboard"]
    assert t3.dashboards == ([] if no_ui else [command])
    assert t3.dashboard_cwds == ([] if no_ui else [first_run.project])
    statuses = ("off", "off") if no_ui else ("started", "running")
    for status, err in zip(statuses, errors, strict=True):
        assert f" dashboard:{status} " in err


def test_no_agent_cli_skips_t3_code(tmp_path, monkeypatch, capsys):
    t3 = _t3_code(monkeypatch)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
    monkeypatch.delenv("BOOLEY_AGENT_APP", raising=False)
    monkeypatch.setattr(entry, "observe_upgrade", lambda: "current")
    monkeypatch.setattr(entry, "launch_auto_doctor", lambda: "started")

    entry.main()

    assert "server:skipped t3:skipped " in capsys.readouterr().err
    assert t3.served == []


# ===========================================================================
# Claude — ~/.claude.json mcpServers entry
# ===========================================================================


@pytest.mark.parametrize(
    "servers",
    [
        None,
        # Pre-ADR-0023 registrations spawned a per-session stdio child that a container
        # resume never re-spawns: rewritten to the URL.
        {"booley": {"command": "python", "args": ["-m", "booley.mcp.server"]}},
        {"other": {"command": "x"}},
    ],
)
def test_claude_entry_is_upserted_once_keeping_other_servers(tmp_path, servers):
    path = reg.claude_config_path(tmp_path)
    if servers is not None:
        path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")

    assert reg.upsert_claude(path) is True
    assert reg.upsert_claude(path) is False

    written = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
    assert written["booley"] == {"type": "http", "url": reg.http_url(), "timeout": 7200000}
    assert written.get("other") == (servers or {}).get("other")


# ===========================================================================
# ensure_http_server — start-if-absent for the loopback HTTP server
# ===========================================================================


def test_a_serving_server_is_not_started_again(first_run):
    first_run.server.serve()

    assert reg.ensure_http_server() == "running"
    assert first_run.server.launches == []


def test_started_server_is_the_developer_agents_detached_surface(first_run, tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_NESTED_AGENT", "1")
    for name in ("BOOLEY_NESTED_MCP_TOOLS", "BOOLEY_MCP_TOOLS"):
        monkeypatch.setenv(name, "submit_run_report")

    assert reg.ensure_http_server(log_path=str(tmp_path / "server.log")) == "started"

    [(argv, options)] = first_run.server.launches
    assert argv[-2:] == ["--transport", "http"]
    # Detached: it must outlive the postStartCommand shell.
    assert options["start_new_session"] is True
    child_env = options["env"]
    assert child_env["BOOLEY_MCP_DEVELOPER_AGENT"] == "1"
    assert not {"BOOLEY_NESTED_AGENT", "BOOLEY_NESTED_MCP_TOOLS", "BOOLEY_MCP_TOOLS"} & set(
        child_env
    )
    assert catalog.status_visible(catalog.Exposure.from_mapping(child_env))


@pytest.mark.parametrize(
    ("outcome", "reason"), [("exits", "exited with 3"), ("unlaunchable", "spawn failed: no exec")]
)
def test_a_server_that_cannot_start_is_failed_not_raised(
    first_run, tmp_path, capsys, outcome, reason
):
    first_run.server.outcome = outcome

    assert reg.ensure_http_server(log_path=str(tmp_path / "server.log")) == "failed"
    assert reason in capsys.readouterr().err


# ===========================================================================
# Skill deployment into the per-app skills dir
# ===========================================================================


@pytest.fixture
def skill_sources(tmp_path, monkeypatch):
    """Where packaged skills and the mounted host skills come from, for the tmp_path home."""
    packaged = tmp_path / "packaged"
    # The packaged skills live in the installed package; no setting points them elsewhere.
    monkeypatch.setattr(paths, "skills_dir", lambda: packaged)
    # The host-side spec mounts host skills here; registration must read the same place.
    host = tmp_path / Path(dc.HOST_SKILLS_SIDECAR).relative_to(dc.AGENT_HOME)
    return {"packaged": (reg.deploy_skills, packaged), "host": (reg.deploy_host_skills, host)}


def _skills(source, *names):
    for name in names:
        (source / name).mkdir(parents=True)
        (source / name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")


@pytest.mark.parametrize("kind", ["packaged", "host"])
@pytest.mark.parametrize(("app", "where"), [("claude", ".claude"), ("codex", ".agents")])
def test_skills_are_linked_where_the_app_reads_them(tmp_path, skill_sources, kind, app, where):
    deploy, source = skill_sources[kind]
    _skills(source, "booley-a", "booley-b")
    (source / "not-a-skill").mkdir()
    target = tmp_path / where / "skills"
    target.mkdir(parents=True)
    # The skills dir persists across rebuilds: a removed skill's dangling link is pruned.
    (target / "booley-gone").symlink_to(tmp_path / "removed-skill")

    assert deploy(app, tmp_path) == 2
    assert deploy(app, tmp_path) == 0

    assert sorted(child.name for child in target.iterdir()) == ["booley-a", "booley-b"]
    assert (target / "booley-a" / "SKILL.md").is_file()


@pytest.mark.parametrize("kind", ["packaged", "host"])
def test_no_skill_is_linked_without_a_source_or_a_known_app(tmp_path, skill_sources, kind):
    deploy, source = skill_sources[kind]

    assert deploy("claude", tmp_path) == 0
    _skills(source, "booley-a")
    assert deploy("none", tmp_path) == 0
    assert not (tmp_path / ".claude").exists()


def test_a_packaged_skill_wins_a_name_clash_with_a_host_skill(tmp_path, skill_sources):
    for _deploy, source in skill_sources.values():
        _skills(source, "shared")

    assert reg.deploy_skills("claude", tmp_path) == 1
    assert reg.deploy_host_skills("claude", tmp_path) == 0

    packaged = skill_sources["packaged"][1] / "shared"
    assert (tmp_path / ".claude" / "skills" / "shared").resolve() == packaged.resolve()


def test_a_skill_that_cannot_be_linked_skips_only_itself(tmp_path, skill_sources, monkeypatch):
    _skills(skill_sources["packaged"][1], "booley-a", "booley-b")
    real_symlink_to = Path.symlink_to

    def failing_first(self, target, target_is_directory=False):
        if self.name == "booley-a":
            raise OSError("read-only file system")
        real_symlink_to(self, target, target_is_directory)

    monkeypatch.setattr(Path, "symlink_to", failing_first)

    assert reg.deploy_skills("claude", tmp_path) == 1
    assert [child.name for child in (tmp_path / ".claude" / "skills").iterdir()] == ["booley-b"]


# ===========================================================================
# apply_stored_credential — the `booley auth` sidecar seed
# ===========================================================================
#
# The container-side delivery of the rotation-free credential: the route that reaches
# VS Code's "Reopen in Container", where the spec's ${localEnv:...} reference resolves empty.

_CREDENTIAL_VARS = {"claude": "CLAUDE_CODE_OAUTH_TOKEN", "codex": "OPENAI_API_KEY"}


def _store_credential(home, app):
    """Mount the `booley auth` credential sidecar for *app* in *home*."""
    (home / auth_token.TOKEN_SEED_BASENAME[app]).write_text("SYNTHETIC_STORED\n", encoding="utf-8")


def _credential_file(home, app):
    return reg.claude_settings_path(home) if app == "claude" else reg.codex_auth_path(home)


@pytest.mark.parametrize("app", ["claude", "codex"])
@pytest.mark.parametrize("exported", [None, "", "SYNTHETIC_EXPORTED"], ids=["no", "empty", "set"])
def test_stored_credential_is_applied_unless_one_is_exported(tmp_path, monkeypatch, app, exported):
    # VS Code resolves an absent ${localEnv:...} to "": that falls through to the store.
    # A real export wins, whichever way Claude layers settings env over the process env.
    if exported is not None:
        monkeypatch.setenv(_CREDENTIAL_VARS[app], exported)
    _store_credential(tmp_path, app)

    assert reg.apply_stored_credential(app, tmp_path) == "written"
    assert reg.apply_stored_credential(app, tmp_path) == "current"

    path = _credential_file(tmp_path, app)
    written = json.loads(path.read_text(encoding="utf-8"))
    expected = {_CREDENTIAL_VARS[app]: exported or "SYNTHETIC_STORED"}
    assert (written["env"] if app == "claude" else written) == expected
    # The file holds a secret: owner-only, like the host store.
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    ("app", "existing", "expected"),
    [
        (
            "claude",
            {"model": "opus", "env": {"FOO": "bar"}},
            {
                "model": "opus",
                "env": {"FOO": "bar", "CLAUDE_CODE_OAUTH_TOKEN": "SYNTHETIC_STORED"},
            },
        ),
        # After the postStart creds-seed copy: a stored key deliberately replaces the login.
        ("codex", {"tokens": {"access_token": "x"}}, {"OPENAI_API_KEY": "SYNTHETIC_STORED"}),
    ],
)
def test_stored_credential_lands_in_the_existing_file(tmp_path, app, existing, expected):
    path = _credential_file(tmp_path, app)
    path.parent.mkdir()
    path.write_text(json.dumps(existing), encoding="utf-8")
    _store_credential(tmp_path, app)

    reg.apply_stored_credential(app, tmp_path)

    assert json.loads(path.read_text(encoding="utf-8")) == expected


@pytest.mark.parametrize(
    ("app", "existing", "status", "kept"),
    [
        ("claude", None, "none", None),
        # `booley auth --clear` and a rebuild: the dead token on the persistent volume
        # would otherwise override the fresh subscription login forever.
        (
            "claude",
            {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "dead", "FOO": "bar"}},
            "cleared",
            {"env": {"FOO": "bar"}},
        ),
        ("codex", {"OPENAI_API_KEY": "dead"}, "cleared", None),
        # Not the single-key file Booley writes: a user's own login stays.
        (
            "codex",
            {"OPENAI_API_KEY": "k", "tokens": None},
            "none",
            {"OPENAI_API_KEY": "k", "tokens": None},
        ),
        ("none", None, "none", None),
    ],
)
def test_without_a_credential_only_booleys_entry_is_removed(tmp_path, app, existing, status, kept):
    path = _credential_file(tmp_path, app)
    if existing is not None:
        path.parent.mkdir()
        path.write_text(json.dumps(existing), encoding="utf-8")

    assert reg.apply_stored_credential(app, tmp_path) == status

    assert _files(tmp_path) == ([] if kept is None else [path])
    assert kept is None or json.loads(path.read_text(encoding="utf-8")) == kept


@pytest.mark.parametrize("app", ["claude", "codex"])
def test_policy_switch_clears_only_managed_credential_entries(tmp_path, monkeypatch, app):
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "auto")
    sidecar = tmp_path / auth_token.TOKEN_SEED_BASENAME[app]
    sidecar.write_text("SYNTHETIC", encoding="utf-8")
    assert reg.apply_stored_credential(app, tmp_path) == "written"
    policy = "api_key" if app == "claude" else "subscription"
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", policy)
    reg.apply_stored_credential(app, tmp_path)
    if app == "claude":
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in reg.claude_settings_path(tmp_path).read_text(
            encoding="utf-8"
        )
    else:
        assert not reg.codex_auth_path(tmp_path).exists()


def test_api_policy_refuses_user_login_without_deleting_it(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "api_key")
    login = tmp_path / ".claude" / ".credentials.json"
    login.parent.mkdir()
    login.write_text('{"claudeAiOauth":{"accessToken":"SYNTHETIC_USER"}}', encoding="utf-8")
    before = login.read_bytes()
    settings = reg.claude_settings_path(tmp_path)
    settings.write_text(
        '{"env":{"CLAUDE_CODE_OAUTH_TOKEN":"SYNTHETIC_MANAGED"},"theme":"dark"}', encoding="utf-8"
    )
    settings_before = settings.read_bytes()
    with pytest.raises(ValueError, match="conflicts") as caught:
        reg.apply_stored_credential("claude", tmp_path)
    assert "SYNTHETIC" not in str(caught.value)
    assert login.read_bytes() == before
    assert settings.read_bytes() == settings_before


# ===========================================================================
# Permission mode (settings.json)
# ===========================================================================


@pytest.mark.parametrize(
    "settings",
    [
        None,
        # The state volume persists: a mode an older Booley or a hand edit left is pulled back.
        {"permissions": {"defaultMode": "acceptEdits"}},
        {"model": "opus", "permissions": {"deny": ["Bash(rm -rf /)"]}},
    ],
)
def test_claude_sessions_launch_in_bypass_mode_beside_the_credential(tmp_path, settings):
    """The container IS the sandbox; Claude Code offers bypassPermissions only to a session
    launched in it, so settings.json is the one lever that reaches every door."""
    _store_credential(tmp_path, "claude")
    path = reg.claude_settings_path(tmp_path)
    if settings is not None:
        path.parent.mkdir()
        path.write_text(json.dumps(settings), encoding="utf-8")

    assert "claude:written" in (first := reg.register("claude", home=tmp_path))
    assert "cred:written perm:written" in first

    written = json.loads(path.read_text(encoding="utf-8"))
    # Both writers rewrite settings.json wholesale: neither may drop the other's keys.
    assert written["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "SYNTHETIC_STORED"
    assert written["permissions"]["defaultMode"] == "bypassPermissions"
    user_denied = (settings or {}).get("permissions", {}).get("deny", [])
    assert written["permissions"]["deny"] == [*user_denied, "WebFetch", "WebSearch"]
    # Without this the one-time disclaimer blocks a fresh state volume.
    assert written["skipDangerousModePermissionPrompt"] is True
    assert written.get("model") == (settings or {}).get("model")
    assert path.stat().st_mode & 0o077 == 0
    assert "claude:current" in (again := reg.register("claude", home=tmp_path))
    assert "cred:current perm:current" in again


# ===========================================================================
# Codex registration — one document step for MCP and permissions
# ===========================================================================

_FORM_TEMPLATES = {
    "dotted": "TBL.other=true\nTBL.KEY=false\n",
    "section": '["TBL"] # keep header\nother=true\n"KEY"=false # keep choice\n',
    "split": "[TBL]\nother=true\n# about other\n[other]\nx=1\n[TBL.sub]\ny=2\n",
    "inline": 'TBL = {other = {text = "a,b=}", values = [1, 2]}, KEY = false} # keep header\n',
    "inline-toml11": 'TBL = {\n # before\n other = {text="{a,b}", values=[1,2]}, # between\n}\n',
}
_CODEX_FORMS = {
    "absent": (None, ()),
    "empty": ("", ()),
    "stale-stdio-table": (
        '# my comment\n[user]\nname = "a"\n\n[mcp_servers.booley]\ncommand = "python"\n'
        'args = ["-m", "booley.mcp.server"]\n\n[other]\nkey = 1\n',
        ("[user]", "[other]"),
    ),
    "stale-quoted-header-no-final-newline": (
        '["mcp_servers"."booley"]\ncommand="old"\n["other"] # retain header\nx=1',
        (),
    ),
    "stale-with-env-subtable": (
        '[mcp_servers.booley]\ncommand="old"\n[mcp_servers.booley.env]\nA="b"\n[after]\nz=1\n',
        (),
    ),
    "stale-dotted-root": (
        'mcp_servers.booley.command="old"\nmcp_servers.other.url="http://other"\n',
        (),
    ),
    "stale-dotted-in-section": (
        '[mcp_servers]\nbooley.command="old"\nother.url="http://other"\n',
        (),
    ),
    "notice-subtable-only": ('[notice.model_migrations]\n"a" = "b"\n', ()),
    "features-subtable-only": ("[features.sub]\nx = 1\n", ()),
    "old-current-entry": ("features.mcp_2026_07_28=true\nSECTION", ("SECTION",)),
    "preference-true": ('"suppress_unstable_features_warning" = true # keep\n', ()),
    "preference-false": ('"suppress_unstable_features_warning" = false # keep\n', ()),
    "preference-decoy-table": (
        "[features] # flags\nsuppress_unstable_features_warning=false\n",
        (),
    ),
    "preference-decoy-comment": ("# suppress_unstable_features_warning = false\n", ()),
    "comment-introducing-next-table": (
        "a = 1\n\n# about other\n[other]\nx = 1\n",
        ("# about other\n[other]",),
    ),
    "unrelated-array-and-string": (
        "items = [\n[1, 2],\n[3]\n]\n# retain array\n"
        'description = """\n[features]\nmcp_2026_07_28 = false\n"""\n',
        ("items = [\n[1, 2],\n[3]\n]\n",),
    ),
    "unicode-line-separators": (
        '# keep\u2028approval_policy="on-request"\nmodel="a"\n# keep\u2029web_search="x"\n'
        '# keep\u0085sandbox_mode="y"\n',
        (),
    ),
    "downgraded-permissions": (
        'approval_policy="on-request"\nsandbox_mode="workspace-write"\n'
        "[notice]\nhide_full_access_warning=false\nhide_rate_limit_model_nudge = true\n",
        (),
    ),
    "crlf": ('# retain CRLF\r\nmodel="existing"\r\n', ('# retain CRLF\r\nmodel="existing"\r\n',)),
    "crlf-no-final-newline": ('model="existing"\r\n# tail', ('model="existing"\r\n# tail',)),
    "no-final-newline": ('model="existing"', ()),
}
for _table, _key in (("features", "mcp_2026_07_28"), ("notice", "hide_full_access_warning")):
    for _name, _template in _FORM_TEMPLATES.items():
        _CODEX_FORMS[f"{_table}-{_name}"] = (
            _template.replace("TBL", _table).replace("KEY", _key),
            ("# about other\n[other]",) if _name == "split" else (),
        )


def _registered(original):
    """The parsed config a registration must produce from the parsed *original*."""
    servers = {k: v for k, v in original.get("mcp_servers", {}).items() if k != "booley"}
    return {
        **original,
        "suppress_unstable_features_warning": original.get(
            "suppress_unstable_features_warning", True
        ),
        "approval_policy": "never",
        "sandbox_mode": "danger-full-access",
        "web_search": "disabled",
        "features": {**original.get("features", {}), "mcp_2026_07_28": True},
        "notice": {**original.get("notice", {}), "hide_full_access_warning": True},
        "mcp_servers": {
            **servers,
            "booley": {"url": reg.http_url(), "tool_timeout_sec": 7200},
        },
    }


@pytest.mark.parametrize(("source", "keeps"), _CODEX_FORMS.values(), ids=_CODEX_FORMS)
def test_codex_registration_forms(tmp_path, source, keeps):
    path = reg.codex_config_path(tmp_path)
    text = (source or "").replace("SECTION", reg.codex_section())
    if source is not None:
        _seed_codex(tmp_path, text)

    assert reg.register("codex", home=tmp_path).startswith("codex:written")

    body = path.read_bytes().decode()
    assert tomli.loads(body) == _registered(tomli.loads(text))
    for kept in (
        *re.findall(r"#[^\n]*", text),
        *(k.replace("SECTION", reg.codex_section()) for k in keeps),
    ):
        assert kept in body
    if source is None:
        assert path.stat().st_mode & 0o777 == 0o600
    assert not reg.claude_settings_path(tmp_path).exists()
    assert "codex:current" in reg.register("codex", home=tmp_path)
    assert path.read_bytes().decode() == body


def test_codex_current_config_is_not_republished(tmp_path):
    reg.register("codex", home=tmp_path)
    path = reg.codex_config_path(tmp_path)
    os.utime(path, ns=(10**9, 10**9))
    before = path.read_bytes()

    assert "codex:current" in reg.register("codex", home=tmp_path)

    assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, 10**9)


def test_codex_registration_parses_config_once(tmp_path, monkeypatch):
    source = 'model="existing"\nnotice.other=true\nother={time=12:34, escaped="\\e\\x41"}\n'
    _seed_codex(tmp_path, source)
    with count_parses(monkeypatch, source) as parses:
        reg.register("codex", home=tmp_path)
        assert parses() == 1
    parsed = tomli.loads(reg.codex_config_path(tmp_path).read_text(encoding="utf-8"))
    assert parsed["notice"] == {"other": True, "hide_full_access_warning": True}
    assert parsed["other"] == tomli.loads(source)["other"]


@pytest.mark.parametrize("failing_publication", [0, 1])
def test_codex_publication_failure_preserves_destination(
    tmp_path, monkeypatch, failing_publication
):
    existing = 'model="existing"\n'
    path = _seed_codex(tmp_path, existing)
    path.chmod(0o640)
    replaced = []
    real_replace = type(path).replace

    def replace(source, destination):
        assert destination == path
        if len(replaced) == failing_publication:
            raise OSError("interrupted publication")
        replaced.append(destination)
        real_replace(source, destination)

    monkeypatch.setattr(type(path), "replace", replace)
    with pytest.raises(OSError, match="interrupted publication"):
        reg.register("codex", home=tmp_path)

    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["model"] == "existing"
    assert ("approval_policy" in parsed) is False
    assert ("mcp_servers" in parsed) is bool(failing_publication)
    assert path.stat().st_mode & 0o777 == 0o640
    assert list(path.parent.iterdir()) == [path]


def test_codex_atomic_publication_preserves_symlink_and_permissions(tmp_path):
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    target = tmp_path / "shared-config.toml"
    target.write_text('model="existing"\n', encoding="utf-8")
    target.chmod(0o640)
    path.symlink_to(target)

    reg.register("codex", home=tmp_path)

    parsed = tomllib.loads(target.read_text(encoding="utf-8"))
    assert (parsed["suppress_unstable_features_warning"], parsed["approval_policy"]) == (
        True,
        "never",
    )
    assert parsed["model"] == "existing"
    assert path.is_symlink()
    assert target.stat().st_mode & 0o777 == 0o640


def test_codex_editor_failure_during_an_edit_is_a_named_error(tmp_path, monkeypatch):
    from tomlkit.exceptions import TOMLKitError

    path = _seed_codex(tmp_path, 'model="existing"\n')
    # private: no valid config makes tomlkit fail mid-edit (#5397's plan lists the layouts
    # tried), so the edit helper stands in for an editor that does.
    monkeypatch.setattr(reg, "_set_codex_table_key", Mock(side_effect=TOMLKitError("no fit")))

    with pytest.raises(ValueError, match=re.escape(str(path))) as error:
        reg.register("codex", home=tmp_path)

    assert "no fit" in str(error.value) and "repair this file and retry" in str(error.value)
    assert _files(tmp_path) == [path] and path.read_text(encoding="utf-8") == 'model="existing"\n'


def test_codex_unusable_mcp_migration_is_refused_before_any_write(tmp_path, monkeypatch):
    path = _seed_codex(tmp_path, 'model="existing"\n')
    # No valid config makes the appended section miss its settings; an incomplete
    # section stands in for a migration that does.
    monkeypatch.setattr(reg, "codex_section", lambda: '[mcp_servers.booley]\nurl = "http://x"\n')

    with pytest.raises(ValueError, match="did not produce current settings"):
        reg.register("codex", home=tmp_path)

    assert _files(tmp_path) == [path] and path.read_text(encoding="utf-8") == 'model="existing"\n'


def test_codex_credential_failure_does_not_publish_permissions(tmp_path, monkeypatch):
    path = _seed_codex(tmp_path, 'approval_policy="on-request"\n')
    login = reg.codex_auth_path(tmp_path)
    login.write_text('{"tokens": {"access_token": "SYNTHETIC"}}', encoding="utf-8")
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "api_key")

    with pytest.raises(ValueError, match="Codex login conflicts with API-key policy"):
        reg.register("codex", home=tmp_path)
    failed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert failed["approval_policy"] == "on-request"
    assert failed["mcp_servers"]["booley"]["url"] == reg.http_url()

    login.unlink()
    assert "perm:written" in reg.register("codex", home=tmp_path)

    healed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert healed["approval_policy"] == "never"
    assert healed["mcp_servers"] == failed["mcp_servers"]


def test_codex_publish_refuses_invalid_text_by_default(tmp_path):
    """The gateway removal path relies on this check; the other writers validate their own text."""
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir()
    path.write_bytes(b'model="existing"\n')
    # private: the removal path only ever produces valid text, so no public call can hand
    # the publisher an invalid one.
    with pytest.raises(ValueError, match=re.escape(str(path))):
        reg._publish_codex_config(path, "notice=true\n")
    assert path.read_text(encoding="utf-8") == 'model="existing"\n'


# ===========================================================================
# First-run state — no onboarding, login or folder-trust prompt (#2258)
# ===========================================================================


def _claude_config(home):
    return json.loads(reg.claude_config_path(home).read_text(encoding="utf-8"))


def _claude_settings(home):
    return json.loads(reg.claude_settings_path(home).read_text(encoding="utf-8"))


def _codex_config(home):
    return tomllib.loads(reg.codex_config_path(home).read_text(encoding="utf-8"))


def _write_claude_config(home, data):
    reg.claude_config_path(home).write_text(json.dumps(data), encoding="utf-8")


def _assert_claude_trusted(config, first_run):
    for key in first_run.keys:
        assert config["projects"][key]["hasTrustDialogAccepted"] is True


@pytest.mark.parametrize("credential", [None, "oauth-token", "login", "api-key"])
def test_claude_first_run_prompts_are_skipped_only_with_a_credential(
    first_run, monkeypatch, credential
):
    if credential == "oauth-token":
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "SYNTHETIC")
    elif credential == "api-key":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC")
    elif credential == "login":
        login = first_run.home / ".claude" / ".credentials.json"
        login.parent.mkdir()
        login.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "SYNTHETIC"}}), encoding="utf-8"
        )

    reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    config = _claude_config(first_run.home)
    _assert_claude_trusted(config, first_run)
    assert config["hasSeenAutoDefaultNudge"] is True
    onboarded = credential is not None
    assert ("hasCompletedOnboarding" in config) is onboarded
    assert _claude_settings(first_run.home).get("theme") == ("dark" if onboarded else None)
    # Claude's "use this API key?" dialog is the billing consent: never pre-answered.
    assert "customApiKeyResponses" not in config


def test_claude_first_run_keeps_every_user_choice(first_run, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "SYNTHETIC")
    seed = {
        "hasCompletedOnboarding": False,
        "hasSeenAutoDefaultNudge": False,
        "autoUpdates": False,
        "cachedGrowthBookFeatures": {"gate": 1},
        "projects": {
            "/host/checkout": {"hasTrustDialogAccepted": True, "allowedTools": ["Bash(ls:*)"]},
            first_run.root: {"hasTrustDialogAccepted": False, "allowedTools": ["Read"]},
            first_run.booley: {"allowedTools": ["Read"]},
        },
    }
    _write_claude_config(first_run.home, seed)
    settings = reg.claude_settings_path(first_run.home)
    settings.parent.mkdir()
    settings.write_text(json.dumps({"theme": "light"}), encoding="utf-8")

    reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    config = _claude_config(first_run.home)
    seed["projects"][first_run.booley]["hasTrustDialogAccepted"] = True
    assert {key: config[key] for key in seed} == seed
    assert _claude_settings(first_run.home)["theme"] == "light"


def test_claude_legacy_theme_blocks_default_theme(first_run, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "SYNTHETIC")
    _write_claude_config(first_run.home, {"theme": "light-daltonized"})

    reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    assert "theme" not in _claude_settings(first_run.home)
    config = _claude_config(first_run.home)
    assert config["theme"] == "light-daltonized"
    assert config["hasCompletedOnboarding"] is True


def test_sandbox_trust_paths_cover_linked_booley_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / "unmounted"))
    real_root = tmp_path / "real-project"
    real_root.mkdir()
    spelled_root = tmp_path / "spelled-project"
    spelled_root.symlink_to(real_root, target_is_directory=True)
    linked_target = tmp_path / "outside-booley"
    linked_target.mkdir()
    (real_root / ".booley").symlink_to(linked_target, target_is_directory=True)

    keys = entry.sandbox_trust_paths(spelled_root)

    assert str(real_root.resolve()) in keys
    assert str(spelled_root.absolute()) in keys
    assert str(linked_target.resolve()) in keys
    assert str(spelled_root / ".booley") in keys
    assert str(tmp_path / "unmounted") not in keys


def test_booley_directory_symlink_to_an_ancestor_is_not_trusted(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").symlink_to(tmp_path, target_is_directory=True)

    keys = entry.sandbox_trust_paths(project)

    assert str(tmp_path.resolve()) not in keys
    assert str(project.resolve()) in keys


_RETRY = "repair this file and retry"


@pytest.mark.parametrize(
    ("app", "where", "existing", "reason"),
    [
        ("claude", reg.claude_config_path, "{not json", "restart the Sandbox"),
        ("claude", reg.claude_settings_path, "{ not json", "restart the Sandbox"),
        ("claude", reg.claude_config_path, '{"projects": []}', "projects must be an object"),
        ("claude", reg.claude_config_path, '{"projects": null}', "projects must be an object"),
        ("claude", reg.claude_config_path, '{"projects": {"/x": "?"}}', "projects./x must be"),
        ("claude", reg.claude_config_path, '{"mcpServers": null}', "mcpServers must be"),
        ("codex", reg.codex_config_path, 'bad = "', _RETRY),
        ("codex", reg.codex_config_path, 'suppress_unstable_features_warning="bad"', _RETRY),
        ("codex", reg.codex_config_path, "features=true\n", _RETRY),
        ("codex", reg.codex_config_path, "notice=true\n", _RETRY),
        ("codex", reg.codex_config_path, 'notice="x"\n', _RETRY),
        ("codex", reg.codex_config_path, "[notice]\nx=1\nx=2\n", _RETRY),
        ("codex", reg.codex_config_path, "mcp_servers = 1\n", _RETRY),
        ("codex", reg.codex_config_path, 'mcp_servers = {other = {url="http://o"}}\n', _RETRY),
        (
            "codex",
            reg.codex_config_path,
            'mcp_servers = {booley = {command="old"}}\n',
            "inline mcp_servers table",
        ),
        (
            "codex",
            reg.codex_config_path,
            'mcp_servers = {booley = {command="old"}, other = {url="http://other"}}\n',
            "inline mcp_servers table",
        ),
        ("codex", reg.codex_config_path, 'projects = "x"\n', "projects must be a table"),
    ],
)
def test_an_invalid_user_config_is_refused_before_any_write(
    first_run, app, where, existing, reason
):
    path = where(first_run.home)
    path.parent.mkdir(exist_ok=True)
    path.write_text(existing, encoding="utf-8")
    # A mounted host skill: deploying it before validation would link it (f1999f6bd).
    _skills(first_run.home / Path(dc.HOST_SKILLS_SIDECAR).relative_to(dc.AGENT_HOME), "host-a")
    before = sorted(_files(first_run.home))

    with pytest.raises(ValueError, match="repair") as refused:
        reg.register(app, home=first_run.home, trust_paths=("/x", *first_run.keys))

    assert str(path) in str(refused.value) and reason in str(refused.value)
    assert sorted(_files(first_run.home)) == before
    assert path.read_text(encoding="utf-8") == existing


def _snapshot(home):
    """Bytes and mtime of every file under *home*."""
    return {
        str(path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(home.rglob("*"))
        if path.is_file()
    }


def test_repeat_registration_is_a_no_op(first_run, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")
    for app in ("claude", "codex"):
        first = reg.register(app, home=first_run.home, trust_paths=first_run.keys)
        assert "first-run:written" in first

    claude = _claude_config(first_run.home)
    _assert_claude_trusted(claude, first_run)
    assert claude["hasCompletedOnboarding"] is True
    assert _claude_settings(first_run.home)["theme"] == "dark"
    assert _codex_config(first_run.home)["projects"][first_run.root]["trust_level"] == "trusted"
    before = _snapshot(first_run.home)

    for app in ("claude", "codex"):
        again = reg.register(app, home=first_run.home, trust_paths=first_run.keys)
        assert "first-run:current" in again

    assert _snapshot(first_run.home) == before


def _trust_layout(first_run, existing):
    """Seed the Codex config with *existing* (``{root}`` filled in) and return its path."""
    # JSON string escaping is valid TOML basic-string escaping (Windows backslashes).
    return _seed_codex(first_run.home, existing.replace('"{root}"', json.dumps(first_run.root)))


@pytest.mark.parametrize(
    ("existing", "trusted"),
    [
        (None, "both"),
        ('# note\n[projects."{root}"]\nother = 1\n', "both"),
        ('model = "x"\n', "both"),
        ('model = "x"', "both"),
        ('projects."{root}".other = 1\n', "both"),
        ('[projects]\n"{root}".other = 1\n', "both"),
        # A user's own trust level, even untrusted, wins.
        ('# notes\n[projects."{root}"]\ntrust_level = "untrusted"  # keep out\n', "booley"),
        # An inline table cannot take a new entry: both are skipped, registration goes on.
        ('projects = { "/elsewhere" = { trust_level = "trusted" } }\n', "none"),
    ],
    ids=[
        "absent",
        "existing-table",
        "new-table",
        "new-table-no-final-newline",
        "root-dotted-entry",
        "dotted-entry-in-table",
        "user-trust-level",
        "inline-table",
    ],
)
def test_codex_trust_layouts(first_run, capsys, existing, trusted):
    path = reg.codex_config_path(first_run.home)
    if existing is not None:
        _trust_layout(first_run, existing)

    status = reg.register("codex", home=first_run.home, trust_paths=first_run.keys)

    body = path.read_text(encoding="utf-8")
    projects = tomllib.loads(body)["projects"]
    expected = {"both": first_run.keys, "booley": (first_run.booley,), "none": ()}[trusted]
    assert [k for k in first_run.keys if projects.get(k, {}).get("trust_level") == "trusted"] == [
        *expected
    ]
    for kept in re.findall(r'#[^\n]*|trust_level = "untrusted"', existing or ""):
        assert kept in body
    if "other = 1" in (existing or ""):
        assert projects[first_run.root]["other"] == 1
    warnings = capsys.readouterr().err
    skipped = trusted == "none"
    assert all(key in warnings and "by hand" in warnings for key in first_run.keys) is skipped
    assert f"first-run:{'current' if skipped else 'written'}" in status
    assert "first-run:current" in reg.register(
        "codex", home=first_run.home, trust_paths=first_run.keys
    )
    assert path.read_text(encoding="utf-8") == body


def test_codex_trust_skips_a_path_whose_edit_fails(first_run, monkeypatch, capsys):
    from tomlkit.exceptions import TOMLKitError

    path = _trust_layout(first_run, '[projects."{root}"]\nother = 1\n')
    reg.register("codex", home=first_run.home)  # MCP and permissions current: no edit left
    # private: no valid layout fails one path's edit and takes the next path's (#5397's
    # plan lists the layouts tried).
    monkeypatch.setattr(reg, "_set_codex_table_key", Mock(side_effect=TOMLKitError("no fit")))

    status = reg.register("codex", home=first_run.home, trust_paths=first_run.keys)

    assert "first-run:written" in status

    projects = tomllib.loads(path.read_text(encoding="utf-8"))["projects"]
    assert projects[first_run.root] == {"other": 1}
    assert projects[first_run.booley] == {"trust_level": "trusted"}
    warnings = capsys.readouterr().err
    assert first_run.root in warnings and first_run.booley not in warnings


# ===========================================================================
# Per-CLI Codex gateway in a Booley-issued Sandbox (#2897)
# ===========================================================================

_GATEWAY_URL = "https://gw.example/v1"
_ISSUED_RECORD = "claude=subscription,codex=gateway"


def _gateway_start(monkeypatch, *, app, record, gateway_values=True):
    """Prepare a container start of the registrar hook; ``record=None`` is a hand-written spec."""
    monkeypatch.setenv("BOOLEY_AGENT_APP", app)
    pin_venue(monkeypatch, inside=True)
    if gateway_values:
        monkeypatch.setenv("BOOLEY_GATEWAY_URL", _GATEWAY_URL)
        monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", "SYNTHETIC_GATEWAY_TOKEN")
    if record is None:
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    else:
        monkeypatch.setenv("BOOLEY_AGENT_CLIS", record)
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "subscription")


def _assert_gateway_hidden(home):
    env_map = _claude_settings(home)["env"]
    assert [env_map[key] for key in _GATEWAY_KEYS] == [""] * len(_GATEWAY_KEYS)
    assert env_map["BOOLEY_HIDDEN_GATEWAY"] == "1"
    return env_map


@pytest.mark.parametrize("exported", [True, False], ids=["exported", "vs-code-without-exports"])
def test_issued_gateway_start_routes_codex_and_hides_the_gateway_from_claude(
    first_run, monkeypatch, capsys, exported
):
    _gateway_start(monkeypatch, app="claude", record=_ISSUED_RECORD, gateway_values=exported)
    settings_path = reg.claude_settings_path(first_run.home)
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({"env": {"USER_KEY": "keep"}}), encoding="utf-8")

    code, err = _main(capsys)

    assert code == 0
    # Blanked even when unset: VS Code may export them on a later attach.
    assert _assert_gateway_hidden(first_run.home)["USER_KEY"] == "keep"
    assert "claude-gateway:hidden" in err
    assert ("gateway:written" if exported else "gateway:invalid") in err
    if exported:
        codex = _codex_config(first_run.home)
        assert codex["model_provider"] == "booley_gateway"
        assert codex["model_providers"]["booley_gateway"]["base_url"] == _GATEWAY_URL


def test_gateway_hiding_replaces_a_misshaped_env_map_once(first_run, monkeypatch):
    _gateway_start(monkeypatch, app="codex", record=_ISSUED_RECORD)
    path = reg.claude_settings_path(first_run.home)
    path.parent.mkdir()
    path.write_text(json.dumps({"env": ["not", "a", "map"], "theme": "dark"}), encoding="utf-8")

    assert reg.hide_gateway_from_claude(first_run.home) == "hidden"
    _assert_gateway_hidden(first_run.home)
    assert _claude_settings(first_run.home)["theme"] == "dark"
    os.utime(path, ns=(10**9, 10**9))

    assert reg.hide_gateway_from_claude(first_run.home) == "hidden"
    assert path.stat().st_mtime_ns == 10**9  # nothing changed, so the file is not rewritten


def test_unreadable_claude_settings_refuses_a_gateway_start(first_run, monkeypatch, capsys):
    _gateway_start(monkeypatch, app="codex", record=_ISSUED_RECORD)
    path = reg.claude_settings_path(first_run.home)
    path.parent.mkdir()
    path.write_text("{ not json", encoding="utf-8")

    code, err = _main(capsys)

    assert code == 1 and "registration refused" in err
    assert path.read_text(encoding="utf-8") == "{ not json"


@pytest.mark.parametrize(
    ("where", "broken"),
    [
        (reg.claude_config_path, '{"mcpServers": ["not", "an", "object"]}'),
        (reg.codex_config_path, "broken = ["),
    ],
    ids=["claude", "codex"],
)
def test_a_refused_registration_still_hides_the_gateway(
    first_run, monkeypatch, capsys, where, broken
):
    _gateway_start(monkeypatch, app="claude", record=_ISSUED_RECORD)
    path = where(first_run.home)
    path.parent.mkdir(exist_ok=True)
    path.write_text(broken, encoding="utf-8")

    code, err = _main(capsys)

    assert code == 1 and "registration refused" in err
    _assert_gateway_hidden(first_run.home)


def test_handwritten_gateway_start_leaves_claude_settings_alone(first_run, monkeypatch):
    """Guard: a hand-written Claude-app spec (no record) keeps Claude's environment."""
    _gateway_start(monkeypatch, app="claude", record=None)

    entry.main()

    assert "env" not in _claude_settings(first_run.home)
    assert not reg.codex_config_path(first_run.home).exists()


# ===========================================================================
# Every allowed Agent CLI is registered, each under its own policy (#2756)
# ===========================================================================


def _multi_cli_start(monkeypatch, *, app, record, primary_auth):
    """A container start of the registrar hook with *record* and *primary_auth*.

    ``record=None`` is a hand-written spec. No gateway values are exported, so a
    ``gateway`` policy is judged only by what the registrar writes.
    """
    _gateway_start(monkeypatch, app=app, record=record, gateway_values=False)
    if primary_auth is None:
        monkeypatch.delenv("BOOLEY_PRIMARY_AUTH", raising=False)
    else:
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", primary_auth)


def _booley_mcp_entries(home):
    """Which CLIs carry the ``booley`` MCP entry in *home*'s user config."""

    def has_entry(read, key):
        try:
            return "booley" in read(home).get(key, {})
        except (OSError, ValueError):  # a missing or deliberately broken config has no entry
            return False

    return {
        "claude": has_entry(_claude_config, "mcpServers"),
        "codex": has_entry(_codex_config, "mcp_servers"),
    }


def test_issued_sandbox_registers_every_listed_cli(first_run, monkeypatch, capsys):
    _multi_cli_start(
        monkeypatch, app="claude", record="claude=auto,codex=auto", primary_auth="auto"
    )

    code, err = _main(capsys)

    assert code == 0
    assert _booley_mcp_entries(first_run.home) == {"claude": True, "codex": True}
    linked = [
        sorted(link.name for link in (first_run.home / app / "skills").iterdir())
        for app in (".claude", ".agents")
    ]
    assert linked[0] and linked[0] == linked[1]
    assert "claude:written" in err and "codex:written" in err


@pytest.mark.parametrize("default", ["claude-default", "codex-default"])
def test_each_listed_cli_applies_its_own_recorded_policy(first_run, monkeypatch, capsys, default):
    if default == "claude-default":
        # Codex's API-key policy must not be judged under the default's subscription policy.
        _multi_cli_start(
            monkeypatch,
            app="claude",
            record="claude=subscription,codex=api_key",
            primary_auth="subscription",
        )
        monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_CODEX_KEY")
    else:
        # Claude's subscription login must be found although the default is on the gateway.
        _multi_cli_start(
            monkeypatch,
            app="codex",
            record="codex=gateway,claude=subscription",
            primary_auth="gateway",
        )
        login = first_run.home / ".claude" / ".credentials.json"
        login.parent.mkdir()
        login.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "SYNTHETIC_ACCESS"}}), encoding="utf-8"
        )

    code, err = _main(capsys)

    assert code == 0 and "refused" not in err
    if default == "claude-default":
        auth_path = reg.codex_auth_path(first_run.home)
        assert json.loads(auth_path.read_text(encoding="utf-8")) == {
            "OPENAI_API_KEY": "SYNTHETIC_CODEX_KEY"
        }
    else:
        assert _claude_config(first_run.home)["hasCompletedOnboarding"] is True


@pytest.mark.parametrize("broken", ["claude", "codex"])
def test_one_cli_refusal_leaves_the_other_registered(first_run, monkeypatch, capsys, broken):
    _multi_cli_start(
        monkeypatch, app="claude", record="claude=auto,codex=auto", primary_auth="auto"
    )
    if broken == "claude":
        reg.claude_config_path(first_run.home).write_text("{not json", encoding="utf-8")
    else:
        _seed_codex(first_run.home, "broken = [")
    other = "codex" if broken == "claude" else "claude"

    code, err = _main(capsys)

    # The rest of the hook still runs, to help diagnose the refusal.
    assert code == 1 and first_run.doctor == [True]
    assert _booley_mcp_entries(first_run.home)[other] is True
    assert f"registration refused: {broken}:" in err
    assert f"{broken}:refused" in err
    assert f"{other}:written" in err


@pytest.mark.parametrize(
    "spec",
    [
        ("claude", None, None),
        ("codex", None, None),
        ("codex", "codex=auto", "auto"),
    ],
    ids=["handwritten-claude", "handwritten-codex", "issued-codex"],
)
def test_single_cli_sandbox_registers_only_that_cli(first_run, monkeypatch, capsys, spec):
    """Guard: a Sandbox allowing one CLI registers it alone and prints no set suffix."""
    app, record, primary_auth = spec
    _multi_cli_start(monkeypatch, app=app, record=record, primary_auth=primary_auth)

    code, err = _main(capsys)

    assert code == 0
    expected = {"claude": False, "codex": False}
    expected[app] = True
    assert _booley_mcp_entries(first_run.home) == expected
    assert re.search(rf"(?<![-\w]){app}:written", err)
    other = "codex" if app == "claude" else "claude"
    assert not re.search(rf"(?<![-\w]){other}:", err)
    assert "clis:" not in err


def test_issued_auto_record_starts_in_the_visitor_mode(first_run, monkeypatch, capsys):
    from booley.config.agent import sandbox_cli_policy

    _multi_cli_start(monkeypatch, app="claude", record="claude=auto", primary_auth="auto")
    settings = reg.claude_settings_path(first_run.home)
    settings.parent.mkdir(parents=True, exist_ok=True)
    # A pinned subscription policy refuses this key; the visitor mode keeps it.
    settings.write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "SYNTHETIC_KEY"}}), encoding="utf-8"
    )

    code, err = _main(capsys)

    assert code == 0 and "refused" not in err
    assert "claude:written" in err
    assert sandbox_cli_policy("claude") == "visitor"


def test_subscription_refusal_on_shared_volume_names_the_file(first_run, monkeypatch, capsys):
    _multi_cli_start(
        monkeypatch, app="codex", record="codex=subscription", primary_auth="subscription"
    )
    auth_path = reg.codex_auth_path(first_run.home)
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text(
        json.dumps({"OPENAI_API_KEY": "SYNTHETIC_KEY", "tokens": {"access_token": "SYNTHETIC"}}),
        encoding="utf-8",
    )

    code, err = _main(capsys)

    assert code == 1
    assert "auth.json" in err
    assert "recreate the Sandbox" not in err

    auth_path.unlink()
    code, err = _main(capsys)

    assert code == 0 and "refused" not in err
    assert "codex:current" in err


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"env": {"ANTHROPIC_AUTH_TOKEN": "SYNTHETIC_KEY"}}, "env.ANTHROPIC_AUTH_TOKEN"),
        ({"env": {"ANTHROPIC_API_KEY": "SYNTHETIC_KEY"}}, "env.ANTHROPIC_API_KEY"),
        ({"env": "SYNTHETIC"}, "env is not an object"),
        ({"apiKeyHelper": "/bin/true"}, "remove apiKeyHelper"),
    ],
)
def test_subscription_refuses_a_claude_bearer_kept_in_settings(
    first_run, monkeypatch, capsys, payload, expected
):
    _multi_cli_start(
        monkeypatch, app="claude", record="claude=subscription", primary_auth="subscription"
    )
    settings = reg.claude_settings_path(first_run.home)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(payload), encoding="utf-8")

    code, err = _main(capsys)

    assert code == 1
    assert expected in err and "settings.json" in err
    assert "recreate the Sandbox" not in err
