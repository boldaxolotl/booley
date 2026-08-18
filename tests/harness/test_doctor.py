"""Tests for booley doctor setup audit."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from booley import runtime_context, selftest_overlay
from booley.harness import auth_token, developer_probe, doctor, doctor_stamp, session_runtime
from booley.harness import devcontainer as dc
from booley.project_dir import reset_cache, resolve_project_dir


def _write_project(
    root: Path,
    *,
    configs_text: str | None = None,
    sandbox_image: str | None = None,
    seed_interactive: bool = True,
    write_tickets: bool = True,
) -> Path:
    (root / "rtl").mkdir()
    (root / "tb").mkdir()
    project_dir = root / ".booley_project"
    project_dir.mkdir()
    sandbox_section = f'[sandbox]\nimage = "{sandbox_image}"\n' if sandbox_image else ""
    booley_toml = """
[project]
name = "unit"

[flows.sim]
default_target = "sim_fast"

[flows.lint]
default_target = "lint_fast"

[flows.elab]
default_target = "sim_fast"

[flows.synth]
default_target = "synth_fast"

[sources.rtl]
source_dirs = ["rtl"]
include_dirs = []

[sources.testbench]
source_dirs = ["tb"]
include_dirs = []

{sandbox_section}
""".lstrip().replace("{sandbox_section}", sandbox_section)
    (project_dir / "booley.toml").write_text(
        booley_toml,
        encoding="utf-8",
    )
    (project_dir / "configs.toml").write_text(
        configs_text
        or """
[fast]
defines = []
top_module = "dut"
tb_top = "tb"
tests = ["smoke", "full"]

[slow]
defines = ["SLOW"]
top_module = "dut"
tb_top = "tb"
tests = ["full"]
""".lstrip(),
        encoding="utf-8",
    )
    (project_dir / ".gitignore").write_text(".interactive_logs/\n", encoding="utf-8")
    # ADR 0039: a resolvable .core Target is mandatory — without one the core
    # audit hard-FAILs and every doctor E2E fixture here would go red.
    (root / "unit.core").write_text(
        "CAPI=2:\n"
        "name: ::unit:0\n"
        "filesets:\n"
        "  rtl:\n"
        "    files:\n"
        "      - rtl/dut.sv: {file_type: systemVerilogSource}\n"
        "  tb:\n"
        "    files:\n"
        "      - tb/tb.sv: {file_type: systemVerilogSource}\n"
        "    tags: [tb]\n"
        "targets:\n"
        "  sim_fast:\n"
        "    flow: sim\n"
        "    flow_options: {tool: verilator}\n"
        "    filesets: [rtl, tb]\n"
        "    toplevel: tb\n"
        "  lint_fast:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator}\n"
        "    filesets: [rtl]\n"
        "    toplevel: dut\n"
        "  synth_fast:\n"
        "    flow: generic\n"
        "    flow_options: {tool: yosys, arch: xilinx}\n"
        "    filesets: [rtl]\n"
        "    toplevel: dut\n",
        encoding="utf-8",
    )
    (root / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    (root / "tb" / "tb.sv").write_text(
        'module tb; initial $display("[SIM_RESULT] PASSED"); endmodule\n',
        encoding="utf-8",
    )
    if write_tickets:
        _write_tickets_tree(project_dir)
    if seed_interactive:
        _seed_interactive(root)
    return project_dir


def _write_tickets_tree(project_dir: Path) -> None:
    tickets_dir = project_dir / "tickets"
    for state in ("drafts", "queue", "active", "review", "done", "blocked", "waiting"):
        (tickets_dir / "board" / state).mkdir(parents=True, exist_ok=True)
    (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)
    (tickets_dir / "locks").mkdir(parents=True, exist_ok=True)


def _seed_interactive(root: Path) -> None:
    """Seed the ADR-0018 artifacts a healthy interactive setup has: an untracked
    devcontainer spec and the git info/exclude entries."""
    from booley.harness import devcontainer as dc

    dc.write_devcontainer(root, dc.build_devcontainer_spec(dc.APP_NONE))
    info_dir = root / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / "exclude").write_text(
        "/.devcontainer\n/.booley_project\n",
        encoding="utf-8",
    )


def _write_skills_home(root: Path) -> Path:
    home = root / "home"
    (home / ("." + "ag" + "ents") / "skills" / "one").mkdir(
        parents=True,
        exist_ok=True,
    )
    return home


def _patch_environment(
    monkeypatch,
    root: Path,
    project_dir: Path,
    *,
    mcp_tools: list[str] | None = None,
    mcp_payload: dict | None = None,
) -> list[list[str]]:
    reset_cache()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))
    # Flow tests exercise the host-side runtime path deterministically,
    # whatever machine the suite itself runs on.
    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(Path, "home", lambda: _write_skills_home(root))
    runtime = "doc" + "ker"
    monkeypatch.setattr(doctor.shutil, "which", lambda name: runtime if name == runtime else None)
    monkeypatch.setattr(doctor, "_docker_image_exists", lambda: True)
    monkeypatch.setattr(doctor.idk, "image_id", lambda image: image)
    # The broad Doctor fixture predates host issuance and keeps its concern on
    # orchestration. Exact stamp/authority behavior has dedicated tests below.
    monkeypatch.setattr(
        doctor,
        "_check_issued_session_runtime",
        lambda _project, _docker, passed, _skip, _fail: passed(
            "Session Runtime spec has valid host issuance (fixture)"
        ),
    )
    # Keep the suite hermetic: the host-clock check (F-5) probes an HTTP Date
    # header over the real network.
    monkeypatch.setattr(doctor, "_check_host_clock", lambda *a, **k: None)
    # Likewise the exact image-fingerprint probe: it hashes the real checkout
    # and inspects real docker labels — force "no verdict" so the flow tests
    # keep exercising the deterministic mtime fallback.
    monkeypatch.setattr(doctor, "source_fingerprint_mismatch", lambda _img: None)

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: PLR0911 — subprocess stub dispatches one return per mocked git/docker command
        calls.append([str(part) for part in cmd])
        if cmd[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=str(root / ".git"), stderr="")
        if cmd[:3] == ["git", "rev-parse", "--git-common-dir"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=str(root / ".git"), stderr="")
        if cmd[:3] == [sys.executable, "-c", "import booley.ticket_board"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "ls-files"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and "config" in cmd and "gc.worktreePruneExpire" in cmd:
            # Healthy default: the ADR 0028 worktree prune guard is set.
            return subprocess.CompletedProcess(cmd, 0, stdout="never\n", stderr="")
        if cmd[1:3] == ["run", "--rm"] and cmd[-2:] == ["id", "-u"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{os.getuid()}\n", stderr="")
        if cmd[1:3] == ["image", "inspect"] and "{{json .Config.Env}}" in cmd:
            # Healthy default: the sandbox image bakes the ADR 0028 runtime marker.
            return subprocess.CompletedProcess(
                cmd, 0, stdout='["BOOLEY_CONTAINER=1"]\n', stderr=""
            )
        if cmd[1:3] == ["image", "inspect"] and "--format" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="2099-01-01T00:00:00Z\n", stderr="")
        if "_discover_booley_mcp_tools" in " ".join(str(part) for part in cmd):
            payload = mcp_payload or {
                "tools": mcp_tools
                or [
                    "synth",
                    "bwave",
                    "coverage_analyst",
                    "elab",
                    "lint",
                    "mutation_tester",
                    "reviewer",
                    "sim",
                ],
                "errors": [],
                "logs_dir": "/work/.booley_project/.interactive_logs/session",
                "logs_dir_ok": True,
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
        if "CORE_RESOLVE_JSON" in " ".join(str(part) for part in cmd):
            # The deep .core-resolvability snippet (docker-wrapped): report the
            # fixture's Target as resolvable via the marker line it scrapes.
            verdicts = json.dumps(
                [{"name": name, "ok": True} for name in ("sim_fast", "lint_fast", "synth_fast")]
            )
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"[[CORE_RESOLVE_JSON]]{verdicts}\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    return calls


def test_doctor_prefers_linked_checkout_project_snapshot(tmp_path, monkeypatch):
    """F-25: Doctor config and design inputs must share one checkout."""
    canonical_root = tmp_path / "canonical"
    checkout_root = tmp_path / "ticket-checkout"
    canonical_root.mkdir()
    checkout_root.mkdir()
    canonical_dir = _write_project(canonical_root)
    checkout_dir = _write_project(checkout_root)
    checkout_toml = checkout_dir / "booley.toml"
    checkout_toml.write_text(
        checkout_toml.read_text(encoding="utf-8").replace('name = "unit"', 'name = "ticket"'),
        encoding="utf-8",
    )
    reset_cache()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(canonical_dir))
    assert resolve_project_dir() == canonical_dir.resolve()  # pre-warm session-global cache

    audit = doctor._check_project_setup(
        checkout_root,
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )

    assert audit is not None
    assert audit.project_dir == checkout_dir.resolve()
    assert audit.booley_toml["project"]["name"] == "ticket"


def test_doctor_default_runs_tool_dry_runs_and_notes_missing_guidance(
    tmp_path,
    monkeypatch,
    capsys,
):
    project_dir = _write_project(tmp_path)
    calls = _patch_environment(monkeypatch, tmp_path, project_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 0
    assert "NOTE  project guidance file missing" in output
    tool_calls = [call for call in calls if call[:3] == [sys.executable, "-m", "booley.flows.sim"]]
    assert tool_calls
    assert "--dry-run" in tool_calls[0]
    assert "--target" in tool_calls[0]
    assert tool_calls[0][tool_calls[0].index("--target") + 1] == "sim_fast"


def test_doctor_passes_interactive_checks_when_seeded(tmp_path, monkeypatch, capsys):
    project_dir = _write_project(tmp_path)  # seeds devcontainer + exclude
    _patch_environment(monkeypatch, tmp_path, project_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 0
    assert "devcontainer.json present and valid" in output
    assert "Booley files excluded from git" in output


def test_git_exclude_warnings_are_scoped_per_missing_entry(tmp_path):
    _git_init(tmp_path)
    (tmp_path / ".git" / "info" / "exclude").write_text("/.devcontainer\n", encoding="utf-8")
    findings: list[doctor.DoctorWarning] = []

    doctor._check_devcontainer_excludes(tmp_path, lambda _msg: None, findings.append)

    assert len(findings) == 1
    assert findings[0].check_id == "project.git-excludes-missing"
    assert findings[0].subject == ".booley_project"


def test_doctor_clean_run_records_freshness_stamp(tmp_path, monkeypatch):
    project_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, project_dir)
    # This broad integration fixture intentionally leaves several environmental
    # warnings active.  Exercise the real waiver path so the clean-run contract
    # remains zero *unwaived* warnings.
    (project_dir / "doctor-waivers.toml").write_text(
        """\
version = 1

[[waiver]]
check = "fusesoc.worktree-core-shadow"
reason = "The fixture does not model FUSESOC_IGNORE."
permanent = true

[[waiver]]
check = "interactive.docker-object-unhealthy"
reason = "The fixture's Docker inventory is deliberately empty."
permanent = true

[[waiver]]
check = "agent.backend-health"
reason = "The fixture uses a synthetic backend probe."
permanent = true

[[waiver]]
check = "project.core-file-untracked"
reason = "The fixture is not a committed project repository."
permanent = true
""",
        encoding="utf-8",
    )

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    assert rc == 0
    stamp = doctor_stamp.load_stamp(project_dir)
    assert stamp is not None
    assert stamp["fingerprint"] == doctor_stamp.compute_fingerprint(project_dir, tmp_path)
    # The stamp it just wrote must satisfy its own freshness check.
    assert doctor_stamp.check_stamp(project_dir, tmp_path) is None


def test_doctor_warning_run_does_not_record_freshness_stamp(tmp_path, monkeypatch):
    project_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, project_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    assert rc == 0  # WARN remains advisory to CLI callers.
    assert doctor_stamp.load_stamp(project_dir) is None


def test_doctor_failing_run_does_not_record_stamp(tmp_path, monkeypatch):
    project_dir = _write_project(tmp_path)
    # Missing asic_synthesize in the MCP probe is a hard FAIL (rc 1).
    _patch_environment(
        monkeypatch,
        tmp_path,
        project_dir,
        mcp_tools=["bwave", "elab", "lint", "sim"],
    )

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    assert rc == 1
    assert doctor_stamp.load_stamp(project_dir) is None


def test_doctor_fails_and_does_not_stamp_agent_app_drift(
    tmp_path,
    monkeypatch,
    capsys,
):
    project_dir = _write_project(tmp_path)
    toml_path = project_dir / "booley.toml"
    toml_path.write_text(
        toml_path.read_text(encoding="utf-8") + '\n[agent]\nprovider = "codex"\n',
        encoding="utf-8",
    )
    dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CLAUDE))
    _patch_environment(monkeypatch, tmp_path, project_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert "FAIL  devcontainer.json BOOLEY_AGENT_APP 'claude'" in output
    assert "[agent] provider 'codex'" in output
    assert doctor_stamp.load_stamp(project_dir) is None


def test_doctor_warns_when_devcontainer_missing(tmp_path, monkeypatch, capsys):
    project_dir = _write_project(tmp_path, seed_interactive=False)
    _patch_environment(monkeypatch, tmp_path, project_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    # Missing spec is a warning (Interactive Mode not seeded), not a hard failure.
    assert rc == 0
    assert "no .devcontainer/devcontainer.json" in output


def test_doctor_fails_when_devcontainer_tracked(tmp_path, monkeypatch, capsys):
    project_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, project_dir)
    # Make `git ls-files -- .devcontainer` report a tracked file.
    inner = doctor.subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "ls-files"] and ".devcontainer" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=".devcontainer/devcontainer.json\n", stderr=""
            )
        return inner(cmd, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert ".devcontainer/ is tracked by git" in output


def test_doctor_fails_when_mcp_probe_misses_required_tool(
    tmp_path,
    monkeypatch,
    capsys,
):
    project_dir = _write_project(tmp_path)
    _patch_environment(
        monkeypatch,
        tmp_path,
        project_dir,
        mcp_tools=["bwave", "elab", "lint", "sim"],
    )

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert "MCP missing required endpoint(s): synth" in output


def test_doctor_fails_when_mcp_probe_does_not_set_interactive_logs(
    tmp_path,
    monkeypatch,
    capsys,
):
    project_dir = _write_project(tmp_path)
    _patch_environment(
        monkeypatch,
        tmp_path,
        project_dir,
        mcp_payload={
            "tools": [
                "synth",
                "bwave",
                "coverage_analyst",
                "elab",
                "lint",
                "mutation_tester",
                "reviewer",
                "sim",
            ],
            "errors": [],
            "logs_dir": "/tmp/booley",
            "logs_dir_ok": False,
        },
    )

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert "MCP interactive log setup failed" in output


def test_doctor_fails_without_tickets_tree(tmp_path, monkeypatch, capsys):
    project_dir = _write_project(tmp_path, write_tickets=False)
    _patch_environment(monkeypatch, tmp_path, project_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert "tickets tree missing" in output


def test_doctor_reports_ticket_board_import_failure(tmp_path, monkeypatch, capsys):
    project_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, project_dir)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == [sys.executable, "-c", "import booley.ticket_board"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        if "_discover_booley_mcp_tools" in " ".join(str(part) for part in cmd):
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(
                    {
                        "tools": [
                            "synth",
                            "bwave",
                            "coverage_analyst",
                            "elab",
                            "lint",
                            "mutation_tester",
                            "reviewer",
                            "sim",
                        ],
                        "errors": [],
                        "logs_dir": "/work/.booley_project/.interactive_logs/session",
                        "logs_dir_ok": True,
                    }
                ),
                stderr="",
            )
        if cmd[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "rev-parse", "--git-dir"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=str(tmp_path / ".git"), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert "ticket_board package not importable" in output


def test_doctor_deep_runs_first_config_without_dry_run(tmp_path, monkeypatch):
    project_dir = _write_project(tmp_path)
    calls = _patch_environment(monkeypatch, tmp_path, project_dir)
    monkeypatch.setattr(doctor, "_synth_deep_report_error", lambda *args: "")

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=True), tmp_path)

    assert rc == 0
    # Two simulate invocations: the shallow dry-run (host) and the deep check.
    # Deep checks now run INSIDE the Session Runtime, so the
    # deep invocation is docker-wrapped rather than a bare host python call.
    sim_calls = [call for call in calls if "booley.flows.sim" in call]
    assert len(sim_calls) == 2
    dry_call, deep_call = sim_calls
    # Dry-run stays cheap on the host.
    assert dry_call[:3] == [sys.executable, "-m", "booley.flows.sim"]
    assert "--dry-run" in dry_call
    # Deep runs in-container: `docker run ... <image> python3 -m booley.flows.sim`.
    assert deep_call[0] == "doc" + "ker" and deep_call[1] == "run"
    assert "--dry-run" not in deep_call
    assert deep_call[deep_call.index("--target") + 1] == "sim_fast"


# ---------------------------------------------------------------------------
# Sandbox routing + in-container self-assertion (b5e8681 regression class):
# sandbox-semantics checks must run IN the sandbox, and a check that cannot
# (misrouted, or image missing) must FAIL loudly — never pass or SKIP.
# ---------------------------------------------------------------------------


def _tool_check_harness(tmp_path, monkeypatch, fake_run):
    """Drive ``_run_flow_check`` directly: stub subprocess + Target probing.

    Returns ``(project, calls)`` — *calls* records every argv *fake_run* saw.
    """
    from booley import fusesoc_registry

    (tmp_path / ".booley_project").mkdir(exist_ok=True)
    project = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=tmp_path / ".booley_project",
        # ADR 0039: the configs.toml first_target probe escape is gone —
        # the probed Target must be configured explicitly.
        booley_toml={"flows": {"sim": {"default_target": "fast"}}},
        configs_toml={"fast": {"defines": [], "tb_top": "tb", "tests": ["smoke"]}},
        first_target="fast",
    )
    monkeypatch.setattr(fusesoc_registry, "enumerate_targets", lambda _root: {})
    monkeypatch.setattr(doctor, "_docker_image_exists_by_name", lambda _img: True)

    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append([str(part) for part in cmd])
        return fake_run(cmd)

    monkeypatch.setattr(doctor.subprocess, "run", run)
    return project, calls


def _selection(backend: str = "builtin"):
    # `backend` survives as a shim: "builtin" maps to a plain enabled
    # selection; anything else rides legacy_backend (migration-error tests).
    from booley.flows.execution import ExecutionSelection

    if backend == "builtin":
        return ExecutionSelection()
    return ExecutionSelection(legacy_backend=backend)


def _run_flow_check(project, rec: _Rec, *, backend: str, dry_run: bool) -> None:
    doctor._run_flow_check(
        project,
        "sim",
        _selection(backend),
        dry_run=dry_run,
        docker_exe="doc" + "ker",
        timeout_s=10,
        verbose=False,
        _pass=rec.p,
        _warn=rec.w,
        _skip=rec.s,
        _fail=rec.f,
    )


@pytest.mark.parametrize(
    ("backend", "dry_run", "expect_docker"),
    [
        # builtin + sandbox: cheap dry-run stays on the host, the deep check
        # (real Flow + FuseSoC/Edalize/EDA toolchain) MUST run in the sandbox.
        ("builtin", True, False),
        ("builtin", False, True),
    ],
)
def test_deep_check_routing_truth_table(
    tmp_path,
    monkeypatch,
    backend,
    dry_run,
    expect_docker,
):
    """Dry checks run locally and deep checks run in the Session Runtime."""
    project, calls = _tool_check_harness(
        tmp_path,
        monkeypatch,
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr=""),
    )
    rec = _Rec()

    _run_flow_check(project, rec, backend=backend, dry_run=dry_run)

    assert rec.kinds() == {"pass"}
    assert len(calls) == 1
    cmd = calls[0]
    if expect_docker:
        assert cmd[:2] == ["doc" + "ker", "run"]
        # The wrapped inner command carries the in-container self-assertion.
        assert doctor._SANDBOX_GUARD_SCRIPT in cmd
        assert "booley.flows.sim" in cmd
    else:
        assert cmd[0] == sys.executable
        assert cmd[1] != "run"  # not a container invocation
        assert doctor._SANDBOX_GUARD_SCRIPT not in cmd


def test_sandbox_guard_execs_inside_and_refuses_outside(tmp_path):
    """Run the P3 guard through a real ``sh`` both ways.

    Inside (env injected by _docker_wrap): exec's the inner argv untouched.
    Outside (no env, no container markers): refuses with the reserved exit
    code + stderr marker that _sandbox_guard_failed() recognizes.
    """
    argv = [
        "sh",
        "-c",
        doctor._SANDBOX_GUARD_SCRIPT,
        "booley-sandbox-guard",
        "echo",
        "inner ran",
    ]

    inside = subprocess.run(
        argv,
        env={**os.environ, "BOOLEY_IN_SANDBOX": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert inside.returncode == 0
    assert inside.stdout.strip() == "inner ran"
    assert not doctor._sandbox_guard_failed(inside)

    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        pytest.skip("test host is itself a container; guard would rightly pass")
    outside = subprocess.run(
        argv,
        env={k: v for k, v in os.environ.items() if k != "BOOLEY_IN_SANDBOX"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert outside.returncode == doctor._SANDBOX_GUARD_EXIT
    assert doctor._SANDBOX_GUARD_MARKER in outside.stderr
    assert doctor._sandbox_guard_failed(outside)


def test_misrouted_sandbox_check_fails_loudly(tmp_path, monkeypatch):
    """P3: a guard refusal surfaces as a FAIL naming the misroute — never a
    silent pass, SKIP, or generic 'Flow failed' verdict."""
    refusal = f"{doctor._SANDBOX_GUARD_MARKER} refusing to run: not inside the sandbox"
    project, _calls = _tool_check_harness(
        tmp_path,
        monkeypatch,
        lambda cmd: subprocess.CompletedProcess(
            cmd,
            doctor._SANDBOX_GUARD_EXIT,
            stdout="",
            stderr=refusal,
        ),
    )
    rec = _Rec()

    _run_flow_check(project, rec, backend="builtin", dry_run=False)

    assert rec.kinds() == {"fail"}
    assert "executed OUTSIDE it" in rec.fails()[0]


def test_exit_97_without_marker_is_an_ordinary_failure(tmp_path, monkeypatch):
    """A Flow exiting 97 for its own reasons must NOT be reported as
    misrouting — the guard verdict requires the stderr marker too."""
    project, _calls = _tool_check_harness(
        tmp_path,
        monkeypatch,
        lambda cmd: subprocess.CompletedProcess(
            cmd,
            doctor._SANDBOX_GUARD_EXIT,
            stdout="",
            stderr="boom",
        ),
    )
    rec = _Rec()

    _run_flow_check(project, rec, backend="builtin", dry_run=False)

    assert rec.kinds() == {"fail"}
    assert "failed with exit 97" in rec.fails()[0]
    assert "OUTSIDE" not in rec.fails()[0]


def test_core_resolve_misroute_fails_loudly(tmp_path, monkeypatch):
    """P3 at the second _docker_wrap call site: the in-container .core
    resolver refusing the guard is a misroute FAIL, not a 'no verdict'."""
    refusal = f"{doctor._SANDBOX_GUARD_MARKER} refusing to run: not inside the sandbox"
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd,
            doctor._SANDBOX_GUARD_EXIT,
            stdout="",
            stderr=refusal,
        ),
    )
    project = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=tmp_path / ".booley_project",
        booley_toml={},
        configs_toml={"fast": {"defines": []}},
        first_target="fast",
    )
    rec = _Rec()

    doctor._run_core_resolve_in_docker(
        project,
        "doc" + "ker",
        "img",
        {"sim": object()},
        lambda name, vlnv: True,
        rec.p,
        rec.w,
        rec.f,
    )

    assert rec.kinds() == {"fail"}
    assert "executed OUTSIDE it" in rec.fails()[0]


def test_doctor_deep_fails_hard_when_sandbox_image_missing(
    tmp_path,
    monkeypatch,
    capsys,
):
    """P4: --deep with no sandbox image is a hard FAIL (with a rebuild hint),
    not a SKIP — the deep checks NEED the sandbox and cannot run at all."""
    project_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, project_dir)
    monkeypatch.setattr(doctor, "_docker_image_exists_by_name", lambda _img: False)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=True), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert "requires the sandbox image" in output
    assert "booley init --force" in output


def test_deep_check_skips_when_runtime_unavailable(tmp_path, monkeypatch):
    """QA-2: a docker-routed check without a container runtime SKIPs (cannot run
    here), matching the sibling container/interactive checks — it is NOT a hard
    FAIL, and the skip reason must not push the useless 'booley init --force'."""
    project, _calls = _tool_check_harness(
        tmp_path,
        monkeypatch,
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr=""),
    )
    rec = _Rec()

    # A host-launched deep check routes into Docker; docker_exe=None
    # models a host with no container runtime at all.
    doctor._run_flow_check(
        project,
        "sim",
        _selection("builtin"),
        dry_run=False,
        docker_exe=None,
        timeout_s=10,
        verbose=False,
        _pass=rec.p,
        _warn=rec.w,
        _skip=rec.s,
        _fail=rec.f,
    )

    assert rec.kinds() == {"skip"}
    skip_msg = next(m for lvl, m in rec.events if lvl == "skip")
    assert "runtime not available" in skip_msg
    assert "booley init --force" not in skip_msg


def test_deep_check_fails_when_runtime_present_but_image_missing(tmp_path, monkeypatch):
    """QA-2 flip side: with the runtime present but the sandbox image genuinely
    unbuilt, the docker-routed check still cannot run — that IS a hard FAIL (a
    missing/broken image must not read as a healthy setup)."""
    project, _calls = _tool_check_harness(
        tmp_path,
        monkeypatch,
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr=""),
    )
    monkeypatch.setattr(doctor, "_docker_image_exists_by_name", lambda _img: False)
    rec = _Rec()

    doctor._run_flow_check(
        project,
        "sim",
        _selection("builtin"),
        dry_run=False,
        docker_exe="doc" + "ker",
        timeout_s=10,
        verbose=False,
        _pass=rec.p,
        _warn=rec.w,
        _skip=rec.s,
        _fail=rec.f,
    )

    assert rec.kinds() == {"fail"}
    assert "requires the sandbox image" in rec.fails()[0]


def test_doctor_rejects_configs_without_defines(tmp_path, monkeypatch):
    project_dir = _write_project(
        tmp_path,
        configs_text="""
[bad]
top_module = "dut"
""".lstrip(),
    )
    _patch_environment(monkeypatch, tmp_path, project_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    assert rc == 1


def test_doctor_accepts_config_parameters(tmp_path, monkeypatch):
    project_dir = _write_project(
        tmp_path,
        configs_text="""
[fast]
defines = []
top_module = "dut"
tb_top = "tb"
tests = ["smoke"]

[fast.parameters]
WIDTH = 32
SECURE = true
MODE = { expr = "pkg::ModeFast" }
INIT = { string = "rom.mem" }
""".lstrip(),
    )
    _patch_environment(monkeypatch, tmp_path, project_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    assert rc == 0


def test_doctor_accepts_shared_test_lists(tmp_path, monkeypatch):
    project_dir = _write_project(
        tmp_path,
        configs_text="""
[test_lists]
rv_tests = ["smoke", "full"]

[fast]
defines = []
top_module = "dut"
tb_top = "tb"
test_list = "rv_tests"
""".lstrip(),
    )
    _patch_environment(monkeypatch, tmp_path, project_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    assert rc == 0


def test_doctor_rejects_plain_string_parameters(tmp_path, monkeypatch, capsys):
    project_dir = _write_project(
        tmp_path,
        configs_text="""
[bad]
defines = []

[bad.parameters]
MODE = "pkg::ModeFast"
""".lstrip(),
    )
    _patch_environment(monkeypatch, tmp_path, project_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert "plain strings are not allowed" in output


def test_flow_command_uses_first_config_and_first_test(tmp_path):
    project_dir = _write_project(tmp_path)
    project = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=project_dir,
        booley_toml={},
        configs_toml={
            "fast": {
                "defines": [],
                "top_module": "dut",
                "tb_top": "tb",
                "tests": ["smoke", "full"],
            },
            "slow": {"defines": []},
        },
        first_target="fast",
    )

    cmd = doctor._flow_command(
        project,
        "sim",
        "fast",
        dry_run=False,
        use_docker=False,
        docker_exe=None,
        image="booley-sandbox",
    )

    assert cmd[:3] == [sys.executable, "-m", "booley.flows.sim"]
    assert cmd[cmd.index("--target") + 1] == "fast"
    # tb_top left the surface (ADR 0021) — sourced from the Target, not passed.
    assert "--tb-top" not in cmd
    assert cmd[cmd.index("--test") + 1] == "smoke"


def test_flow_command_passes_internal_selftest_kind_into_sandbox(tmp_path):
    from booley import selftest_overlay

    project_dir = _write_project(tmp_path)
    project = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=project_dir,
        booley_toml={},
        configs_toml={},
        first_target="sim_core",
    )

    cmd = doctor._flow_command(
        project,
        "sim",
        "sim_core",
        dry_run=False,
        use_docker=True,
        docker_exe="docker",
        image="booley-sandbox",
        doctor_selftest_kind="bad",
    )

    assert f"{selftest_overlay.INTERNAL_KIND_ENV}=bad" in cmd


def _tests_toml_project_audit(tmp_path, tests_toml_text: str) -> doctor.ProjectAudit:
    project_dir = _write_project(tmp_path)
    (project_dir / "tests.toml").write_text(tests_toml_text, encoding="utf-8")
    return doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=project_dir,
        booley_toml={},
        configs_toml={},
        first_target="sim_core",
    )


def test_flow_command_deep_smoke_pins_first_tests_toml_test(tmp_path):
    """A configs.toml-less (post-ADR-0022) project must still get a pinned
    ``--test``: without one the deep simulate smoke runs the Target's WHOLE
    list and times out on any large design (openc910: 16 full-chip sims)."""
    project = _tests_toml_project_audit(
        tmp_path,
        '[sim_core]\ntests = ["hello_world", "coremark"]\n',
    )

    cmd = doctor._flow_command(
        project,
        "sim",
        "sim_core",
        dry_run=False,
        use_docker=False,
        docker_exe=None,
        image="booley-sandbox",
    )

    assert cmd[cmd.index("--test") + 1] == "hello_world"


def test_flow_command_deep_smoke_skips_skipped_tests(tmp_path):
    """The smoke's one pinned test must be runnable: honor the tests.toml
    ``skip`` list so a known-hang or an always-fail selftest fixture at the
    head of the list can't turn the deep check into a guaranteed FAIL."""
    project = _tests_toml_project_audit(
        tmp_path,
        "[sim_core]\n"
        'tests = ["booley_selftest_bad", "hello_world"]\n'
        'skip = ["booley_selftest_bad"]\n',
    )

    cmd = doctor._flow_command(
        project,
        "sim",
        "sim_core",
        dry_run=False,
        use_docker=False,
        docker_exe=None,
        image="booley-sandbox",
    )

    assert cmd[cmd.index("--test") + 1] == "hello_world"


def test_flow_command_deep_smoke_resolves_vlnv_qualified_target(tmp_path):
    """ADR 0030 Targets may arrive VLNV-qualified; tests.toml keys are bare."""
    project = _tests_toml_project_audit(
        tmp_path,
        '[sim_core]\ntests = ["hello_world"]\n',
    )

    cmd = doctor._flow_command(
        project,
        "sim",
        "acme:ip:c910#sim_core",
        dry_run=False,
        use_docker=False,
        docker_exe=None,
        image="booley-sandbox",
    )

    assert cmd[cmd.index("--test") + 1] == "hello_world"


def test_flow_command_deep_smoke_all_skipped_falls_back_to_head(tmp_path):
    """All-skip misconfig: smoke the declared head rather than nothing
    (mirrors the Simulation Flow's never-run-zero-tests semantics)."""
    project = _tests_toml_project_audit(
        tmp_path,
        '[sim_core]\ntests = ["hello_world"]\nskip = ["hello_world"]\n',
    )

    cmd = doctor._flow_command(
        project,
        "sim",
        "sim_core",
        dry_run=False,
        use_docker=False,
        docker_exe=None,
        image="booley-sandbox",
    )

    assert cmd[cmd.index("--test") + 1] == "hello_world"


def test_probe_target_uses_configured_tool_target(tmp_path, monkeypatch):
    """ADR 0030: Doctor drives off [flows.<flow>].default_target, not a Flow scan — on a
    vendored repo a flow-match guess would pick an arbitrary upstream Target."""
    from booley import fusesoc_registry

    project = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=tmp_path / ".booley_project",
        booley_toml={
            "flows": {
                "lint": {"default_target": "ibex_top#lint"},
                # A comma list: doctor smoke-checks just the first token.
                "sim": {"default_target": "foo_sim, bar_sim"},
            }
        },
        configs_toml={"foo": {"defines": []}},
        first_target="foo",
    )
    core = tmp_path / ".booley_project" / "design.core"
    # .core Targets exist, but the CONFIGURED target is authoritative.
    refs = {
        "foo_sim": fusesoc_registry.TargetRef(
            name="foo_sim",
            vlnv="::design:0",
            core_file=core,
            eda_tool="verilator",
            flow="sim",
        ),
    }
    monkeypatch.setattr(fusesoc_registry, "enumerate_targets", lambda _root: refs)

    assert doctor._probe_target(project, "lint") == "ibex_top#lint"
    assert doctor._probe_target(project, "sim") == "foo_sim"
    # No [flows.synth].default_target and .core authored -> None (skip, no guess).
    assert doctor._probe_target(project, "synth") is None


def test_configured_target_reads_tools_table(tmp_path):
    """_configured_target reads [flows.<flow>].default_target, first of a comma list."""
    project = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=tmp_path / ".booley_project",
        booley_toml={
            "flows": {
                "lint": {"default_target": "lowrisc:ibex:ibex_top#lint, spare"},
            }
        },
        configs_toml={},
        first_target="",
    )
    assert doctor._configured_target(project, "lint") == "lowrisc:ibex:ibex_top#lint"
    # A Flow with no [flows.<flow>].default_target configured -> "".
    assert doctor._configured_target(project, "sim") == ""


def test_deep_timeout_honors_configured_timeout_ms(tmp_path):
    """--deep honors the Flow timeout and leaves synth finalization headroom.

    A large core that raised the Flow's own timeout_ms must not be spuriously
    killed by the shorter fixed deep budget; a lower/absent knob keeps the floor.
    """
    project = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=tmp_path / ".booley_project",
        booley_toml={
            "flows": {
                # Raised well above the 1800s asic floor -> honored (ms -> s).
                "synth": {"timeout_ms": 5400000},
                # Below the 900s simulate floor -> floor wins.
                "sim": {"timeout_ms": 60000},
                # Unparseable -> floor stands, no crash.
                "lint": {"timeout_ms": "oops"},
            }
        },
        configs_toml={},
        first_target="",
    )
    assert doctor._deep_timeout_s(project, "synth") == (
        5400 + doctor._SYNTH_DEEP_FINALIZE_MARGIN_S
    )
    assert doctor._deep_timeout_s(project, "sim") == doctor._DEEP_TIMEOUTS_S["sim"]
    assert doctor._deep_timeout_s(project, "lint") == doctor._DEEP_TIMEOUTS_S["lint"]
    # No knob at all -> the hardcoded floor.
    bare = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=tmp_path / ".booley_project",
        booley_toml={},
        configs_toml={},
        first_target="",
    )
    assert (
        doctor._deep_timeout_s(bare, "synth")
        == doctor._DEEP_TIMEOUTS_S["synth"] + doctor._SYNTH_DEEP_FINALIZE_MARGIN_S
    )


def test_validate_one_flow_table_rejects_non_string_target():
    """[flows.<flow>].default_target must be a string (a Target name / vlnv#name / list)."""
    fails: list[str] = []
    warns: list[str] = []

    def _fail(msg: str, fix: str = "") -> None:
        fails.append(msg)

    def _warn(msg: str) -> None:
        warns.append(msg)

    # A non-string target (e.g. a TOML array) fails validation.
    ok = doctor._validate_one_flow_table(
        "lint",
        {"default_target": ["a", "b"]},
        _warn,
        _fail,
    )
    assert ok is False
    assert any("[flows.lint].default_target must be a string" in m for m in fails)

    # A well-formed string target passes.
    fails.clear()
    ok = doctor._validate_one_flow_table(
        "lint",
        {"default_target": "ibex_top#lint"},
        _warn,
        _fail,
    )
    assert ok is True
    assert fails == []


def test_validate_one_flow_table_rejects_retired_target_key():
    fails: list[str] = []

    ok = doctor._validate_one_flow_table(
        "lint",
        {"target": "ibex_top#lint"},
        lambda _msg: None,
        lambda msg, fix="": fails.append(f"{msg} {fix}"),
    )

    assert ok is False
    assert any("[flows.lint].target is retired" in message for message in fails)
    assert any("[flows.lint].default_target" in message for message in fails)


def test_validate_one_flow_table_rejects_retired_selftest_table():
    fails: list[str] = []

    ok = doctor._validate_one_flow_table(
        "sim",
        {"selftest": {"good": "main", "bad": "known_bad"}},
        lambda _msg: None,
        lambda msg, fix="": fails.append(f"{msg} {fix}"),
    )

    assert ok is False
    assert any("[flows.sim.selftest] is retired" in message for message in fails)
    assert any("bad-overlay" in message for message in fails)


@pytest.mark.parametrize("key", ["builtin", "custom"])
def test_validate_flow_tables_rejects_retired_allowlists(key):
    fails: list[str] = []

    ok = doctor._validate_flow_tables(
        {"tools": {key: ["sim"]}},
        lambda _msg: None,
        lambda msg, fix="": fails.append(f"{msg} {fix}"),
    )

    assert ok is False
    assert any("retired" in message and "enabled = false" in message for message in fails)


def test_validate_one_flow_table_warns_on_set_but_ignored_knob():
    """A knob honored elsewhere but not by this Flow is flagged, not failed (F4)."""
    fails: list[str] = []
    warns: list[str] = []

    # lint does not read timeout_ms (only simulate/asic_synthesize do): warn.
    ok = doctor._validate_one_flow_table(
        "lint",
        {"timeout_ms": 900000},
        warns.append,
        lambda msg, fix="": fails.append(msg),
    )
    assert ok is True  # well-typed, just inert — a warning, never a failure
    assert any("[flows.lint].timeout_ms" in m and "ignores it" in m for m in warns)

    # simulate DOES read timeout_ms → no set-but-ignored warning.
    warns.clear()
    doctor._validate_one_flow_table(
        "sim",
        {"timeout_ms": 900000},
        warns.append,
        lambda msg, fix="": fails.append(msg),
    )
    assert not any("timeout_ms" in m for m in warns)


@pytest.mark.parametrize(
    ("knob", "reader", "non_reader", "value"),
    [
        ("sim_time_grace_s", "sim", "lint", 180),
        ("keep_build_dir", "elab", "sim", True),
        ("fail_on_timing_violation", "synth", "lint", True),
        ("warnings_as_errors", "lint", "sim", False),
        # trace_files declares the TB's own dump path; only simulate reads it.
        ("trace_files", "sim", "elab", ["fpu.vcd"]),
    ],
)
def test_selective_knob_is_registered_with_its_reader(knob, reader, non_reader, value):
    """Every selective knob warns under a Flow that ignores it, stays quiet under its own.

    A knob added to a Flow without an entry here is silently accepted anywhere,
    which is the exact failure mode ``_SELECTIVE_FLOW_KNOBS`` exists to prevent.
    """
    fails: list[str] = []
    warns: list[str] = []

    doctor._validate_one_flow_table(
        non_reader, {knob: value}, warns.append, lambda msg, fix="": fails.append(msg)
    )
    assert any(f"[flows.{non_reader}].{knob}" in m and "ignores it" in m for m in warns)

    warns.clear()
    doctor._validate_one_flow_table(
        reader, {knob: value}, warns.append, lambda msg, fix="": fails.append(msg)
    )
    assert not any(knob in m and "ignores it" in m for m in warns)


@pytest.mark.parametrize(
    ("flow_name", "knob", "value"),
    [
        ("synth", "flatten", True),
        ("synth", "frontend", "slang"),
        ("synth", "sdc", "constraints/top.sdc"),
        ("fpga", "part", "xc7a35tcsg324-1"),
        ("fpga", "out_of_context", True),
        ("fpga", "strategy", "Flow_PerfOptimized_high"),
    ],
)
def test_target_build_inputs_are_rejected_from_flow_tables(flow_name, knob, value):
    fails: list[str] = []

    ok = doctor._validate_one_flow_table(
        flow_name,
        {knob: value},
        lambda _msg: None,
        lambda msg, fix="": fails.append(f"{msg} {fix}"),
    )

    assert ok is False
    assert any(knob in message and ".core Target" in message for message in fails)


def test_validate_one_flow_table_pre_run_commands_shape():
    """[flows.sim].pre_run_commands must be a list of strings (ADR 0039)."""
    fails: list[str] = []
    warns: list[str] = []

    # A scalar (or a list with non-string entries) fails the shape check.
    ok = doctor._validate_one_flow_table(
        "sim",
        {"pre_run_commands": "make prep"},
        warns.append,
        lambda msg, fix="": fails.append(msg),
    )
    assert ok is False
    assert any("[flows.sim].pre_run_commands must be a" in m for m in fails)

    fails.clear()
    ok = doctor._validate_one_flow_table(
        "sim",
        {"pre_run_commands": ["make prep", 3]},
        warns.append,
        lambda msg, fix="": fails.append(msg),
    )
    assert ok is False

    # A well-formed list passes, with no inert-knob warning on simulate.
    fails.clear()
    warns.clear()
    ok = doctor._validate_one_flow_table(
        "sim",
        {"pre_run_commands": ["make prep CASE=$BOOLEY_TEST_NAME"]},
        warns.append,
        lambda msg, fix="": fails.append(msg),
    )
    assert ok is True
    assert fails == []
    assert not any("pre_run_commands" in m for m in warns)

    # Only simulate reads it: set on lint it is inert → warn, not fail.
    ok = doctor._validate_one_flow_table(
        "lint",
        {"pre_run_commands": ["make prep"]},
        warns.append,
        lambda msg, fix="": fails.append(msg),
    )
    assert ok is True
    assert any("[flows.lint].pre_run_commands" in m and "ignores it" in m for m in warns)


class TestRetiredCommercialEdaConfig:
    def test_retired_venue_fails_closed(self):
        from booley.eda.config import retired_config_error

        assert "retired" in retired_config_error({"flows": {"sim": {"venue": "host"}}})


class TestValidateAgentTable:
    """A typo'd provider is fatal, not advisory: `_parse_provider` raises rather
    than run a backend the project never chose, so every agent run dies with a
    BackendConfigError. Doctor must be where that surfaces."""

    @staticmethod
    def _run(agent_section):
        passes: list[str] = []
        fails: list[str] = []
        valid = doctor._validate_agent_table(
            {"agent": agent_section} if agent_section is not None else {},
            passes.append,
            lambda msg, fix="": fails.append(msg),
        )
        return valid, passes, fails

    def test_fails_on_an_invalid_provider(self):
        valid, _passes, fails = self._run({"provider": "cluade"})
        assert not valid
        assert any("cluade" in m for m in fails)

    def test_fails_when_agent_is_not_a_table(self):
        valid, _passes, fails = self._run("claude")
        assert not valid
        assert any("[agent] must be a table" in m for m in fails)

    def test_passes_and_names_a_valid_provider(self):
        valid, passes, fails = self._run({"provider": "codex"})
        assert valid and not fails
        assert any("codex" in m for m in passes)

    def test_rejects_the_retired_primary_alias(self):
        valid, _passes, fails = self._run({"primary": "claude"})
        assert not valid
        assert any("retired" in m for m in fails)

    @pytest.mark.parametrize("section", [None, {}])
    def test_absent_or_empty_agent_is_not_an_error(self, section):
        # The provider may legitimately come from BOOLEY_PRIMARY_PROVIDER or the
        # container's BOOLEY_AGENT_APP; only a present-and-wrong value fails.
        valid, passes, fails = self._run(section)
        assert valid and not fails and not passes


def test_validate_known_tables_warns_on_unknown_and_retired():
    """Unrecognized top-level booley.toml tables warn (typo/stale); known ones don't."""
    warns: list[str] = []

    def _warn(msg: str, fix: str = "") -> None:
        warns.append(msg)

    # Every canonical table is silent (derived — a second literal list here
    # would drift exactly like the one that lost [stealth]; see F-17).
    doctor._validate_known_tables(
        {table: {} for table in doctor._KNOWN_BOOLEY_TOML_TABLES},
        _warn,
    )
    assert warns == []

    # A retired table gets the targeted migration hint...
    warns.clear()
    doctor._validate_known_tables({"fusesoc": {"target_cores": ["x"]}}, _warn)
    assert any("[fusesoc]" in m and "ADR 0030" in m for m in warns)

    # ...and an outright typo gets the generic "ignored" warning.
    warns.clear()
    doctor._validate_known_tables({"toolz": {}}, _warn)
    assert any("[toolz]" in m and "ignored" in m for m in warns)


@pytest.mark.parametrize("mode", ["ask", "email", "file-only", "off"])
def test_validate_feedback_table_accepts_live_settings(mode):
    passes: list[str] = []
    fails: list[str] = []

    valid = doctor._validate_feedback_table(
        {
            "feedback": {
                "mode": mode,
                "redact_extra": ["codename"],
                "redact_identifiers": False,
            }
        },
        passes.append,
        lambda message, fix="": fails.append(message),
    )

    assert valid
    assert passes and not fails


@pytest.mark.parametrize(
    "feedback",
    [
        {"mode": "sometimes"},
        {"mode": []},
        {"redact_extra": "codename"},
        {"redact_identifiers": "false"},
    ],
)
def test_validate_feedback_table_rejects_invalid_settings(feedback):
    fails: list[str] = []

    valid = doctor._validate_feedback_table(
        {"feedback": feedback},
        lambda _message: None,
        lambda message, fix="": fails.append(message),
    )

    assert not valid
    assert fails


@pytest.mark.parametrize(
    ("stealth", "valid"),
    [
        ({"enabled": True, "ignore_native_cores": True}, True),
        ({"enabled": True, "ignore_native_cores": False}, True),
        ({"enabled": False, "ignore_native_cores": True}, False),
        ({"enabled": True, "ignore_native_cores": "yes"}, False),
    ],
)
def test_validate_stealth_native_core_ignore(stealth, valid):
    fails: list[str] = []

    result = doctor._validate_stealth_table(
        {"stealth": stealth},
        lambda message, fix="": fails.append(message),
    )

    assert result is valid
    assert bool(fails) is not valid


# Functions that return the WHOLE parsed booley.toml (not one section), so a
# `.get("x")` chained straight onto a call to one of them names a top-level
# table. Only three exist; a fourth would have to be added here, but the
# "no call sites found" guard below catches a wholesale rename.
_ROOT_TOML_LOADERS = frozenset({"_load_booley_toml", "_load_booley_config", "_load_rtl_config"})


def _tables_read_by_production_code() -> dict[str, str]:
    """Top-level booley.toml tables that live code reads, found by scanning
    ``src/booley`` for ``<root-loader>(...).get("<table>", ...)`` chains.

    Deliberately a lower bound: consumers that stash the dict in a local first
    are missed. Every hit, though, is a genuinely live table — enough to prove
    doctor's allowlist is not lying about one.
    """
    import booley

    found: dict[str, str] = {}
    for path in sorted(Path(booley.__file__).parent.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            inner = node.func.value
            if node.func.attr != "get" or not node.args or not isinstance(inner, ast.Call):
                continue
            if not isinstance(inner.func, ast.Name) or inner.func.id not in _ROOT_TOML_LOADERS:
                continue
            key = node.args[0]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                found.setdefault(key.value, f"{path.name}:{node.lineno}")
    return found


# A quoted shell heredoc (``<<'TAG' ... TAG``). Booley's shell scripts parse
# booley.toml by piping one of these into python3, so the Python scanner above —
# which only reads *.py — cannot see the tables they read.
# ``[^\n]*`` after the tag: the redirection can carry trailing words
# (``<<'PYEOF' 2>/dev/null``), which an anchored ``\n`` would miss entirely.
_SH_PY_HEREDOC_RE = re.compile(
    r"<<\s*'(?P<tag>\w+)'[^\n]*\n(?P<body>.*?)\n(?P=tag)$",
    re.DOTALL | re.MULTILINE,
)


def _is_tomllib_load(node: ast.AST) -> bool:
    """``tomllib.load(...)`` / ``tomllib.loads(...)`` — i.e. a whole parsed config."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("load", "loads")
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "tomllib"
    )


def _root_config_gets(tree: ast.AST):
    """Yield ``(table, lineno)`` for ``<var>.get("<table>")`` where *var* holds a
    whole parsed TOML document."""
    roots = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and _is_tomllib_load(node.value)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in roots
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            yield node.args[0].value, node.lineno


def _tables_read_by_shell_scripts() -> dict[str, str]:
    """Top-level booley.toml tables read by embedded Python in ``src/booley``
    shell scripts.

    Scoped to scripts that name booley.toml, so a heredoc parsing some other
    TOML can't inject its keys into this list. Only ``worktree_create.sh``
    qualifies today; it is also the only reader of ``[submodules].paths``, which
    is precisely why an audit that scanned Python alone declared the allowlist
    complete while doctor still warned that knob was ignored.
    """
    import booley

    found: dict[str, str] = {}
    for path in sorted(Path(booley.__file__).parent.rglob("*.sh")):
        text = path.read_text(encoding="utf-8")
        if "booley.toml" not in text:
            continue
        for match in _SH_PY_HEREDOC_RE.finditer(text):
            try:
                tree = ast.parse(match.group("body"))
            except SyntaxError:
                continue  # the heredoc isn't Python
            first_line = text[: match.start("body")].count("\n") + 1
            for table, lineno in _root_config_gets(tree):
                found.setdefault(table, f"{path.name}:{first_line + lineno - 1}")
    return found


class TestKnownTablesMatchLiveConfig:
    """F-17: ``[stealth]`` was honored by the commit-msg hook, written by
    ``booley init --scaffold``, documented — and still WARNed as "unrecognized
    ... its settings are ignored". These checks derive the table names from the
    live code and from the TOML Booley itself emits; a second hardcoded list
    would just drift the same way.
    """

    def test_every_table_read_by_production_code_is_recognized(self):
        tables = _tables_read_by_production_code()
        assert tables, "no root-loader .get() call sites found - _ROOT_TOML_LOADERS is stale"

        unknown = {
            table: where
            for table, where in tables.items()
            if table not in doctor._KNOWN_BOOLEY_TOML_TABLES
        }
        assert unknown == {}, f"live booley.toml tables doctor calls ignored: {unknown}"

    def test_every_table_read_by_a_shell_script_is_recognized(self):
        """Scanning Python alone is how ``[submodules].paths`` stayed unlisted:
        its only reader is a tomllib heredoc inside worktree_create.sh."""
        tables = _tables_read_by_shell_scripts()
        assert "submodules" in tables, (
            "worktree_create.sh's [submodules].paths reader is no longer visible "
            "to the scanner - the heredoc shape changed"
        )

        unknown = {
            table: where
            for table, where in tables.items()
            if table not in doctor._KNOWN_BOOLEY_TOML_TABLES
        }
        assert unknown == {}, f"live booley.toml tables doctor calls ignored: {unknown}"

    @pytest.mark.parametrize("asic", [True, False])
    @pytest.mark.parametrize("fpga_part", [None, "xc7a35tcpg236-1"])
    def test_scaffolded_booley_toml_is_fully_recognized(self, asic, fpga_part):
        """`booley init --scaffold` must not write a table its own doctor warns about."""
        from booley.harness.init_scaffold import ScaffoldChoices, _booley_toml

        body = _booley_toml(
            ScaffoldChoices(
                name="my_ip",
                sim_eda_tool="verilator",
                tb_style="sv",
                lint_eda_tool="verilator",
                asic=asic,
                fpga_part=fpga_part,
            )
        )

        emitted = set(tomllib.loads(body))
        assert emitted <= doctor._KNOWN_BOOLEY_TOML_TABLES, (
            f"scaffold emits unrecognized tables: {emitted - doctor._KNOWN_BOOLEY_TOML_TABLES}"
        )

    def test_setup_skill_template_is_fully_recognized(self):
        """Same contract for the booley.toml the setup skill hands to projects."""
        import booley

        template = (
            Path(booley.__file__).parent
            / "data"
            / "skills"
            / "booley-setup"
            / "BOOLEY_TEMPLATE.toml"
        )
        tables = set(tomllib.loads(template.read_text(encoding="utf-8")))

        assert tables <= doctor._KNOWN_BOOLEY_TOML_TABLES, (
            f"setup template offers unrecognized tables: "
            f"{tables - doctor._KNOWN_BOOLEY_TOML_TABLES}"
        )

    def test_known_and_retired_tables_do_not_overlap(self):
        """A table cannot be both live and retired — the retired hint would
        never fire, or the known set would silence a real migration."""
        assert not (doctor._KNOWN_BOOLEY_TOML_TABLES & set(doctor._RETIRED_BOOLEY_TOML_TABLES))


def test_probe_target_declines_without_configured_target(tmp_path, monkeypatch):
    """No .core Targets yet -> probe the configs.toml first_target."""
    from booley import fusesoc_registry

    project = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=tmp_path / ".booley_project",
        booley_toml={},
        configs_toml={"fast": {"defines": []}},
        first_target="fast",
    )
    monkeypatch.setattr(fusesoc_registry, "enumerate_targets", lambda _root: {})
    # ADR 0039: the configs.toml first_target escape is gone — with no
    # configured [flows.lint].default_target the probe declines rather than guessing.
    assert doctor._probe_target(project, "lint") is None


def test_doctor_uses_configured_sandbox_image(tmp_path, monkeypatch):
    project_dir = _write_project(
        tmp_path,
        sandbox_image="ibex-booley-sandbox:latest",
    )
    calls = _patch_environment(monkeypatch, tmp_path, project_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    assert rc == 0
    docker_runs = [
        call for call in calls if len(call) > 3 and call[0] == "docker" and call[1] == "run"
    ]
    assert docker_runs
    assert any("ibex-booley-sandbox:latest" in call for call in docker_runs)


class TestRiscvImageChecks:
    def _run(self, monkeypatch, *, failed_doc: str | None = None):
        calls: list[list[str]] = []
        passes: list[str] = []
        failures: list[tuple[str, str]] = []
        monkeypatch.setattr(doctor, "_image_env_value", lambda *_args: "riscv")

        def run(cmd, **_kwargs):
            calls.append(cmd)
            if failed_doc is not None and failed_doc in cmd:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(doctor.subprocess, "run", run)
        doctor._check_riscv_toolchain(
            "docker",
            "booley-sandbox-riscv",
            passes.append,
            lambda _message: None,
            lambda message, fix: failures.append((message, fix)),
        )
        return calls, passes, failures

    def test_checks_every_advertised_offline_document(self, monkeypatch):
        calls, passes, failures = self._run(monkeypatch)

        docs_call = next(call for call in calls if "riscv-isa-manual.html" in call)
        assert set(doctor._RISCV_DOC_FILES) <= set(docs_call)
        assert "RISC-V offline specs complete at $BOOLEY_RISCV_DOCS" in passes
        assert failures == []

    def test_incomplete_offline_document_set_fails(self, monkeypatch):
        _calls, passes, failures = self._run(
            monkeypatch, failed_doc="riscv-debug-specification.pdf"
        )

        assert "RISC-V offline specs complete at $BOOLEY_RISCV_DOCS" not in passes
        assert failures == [
            (
                "RISC-V offline specs complete at $BOOLEY_RISCV_DOCS",
                doctor._RISCV_IMAGE_FIX,
            )
        ]


def test_doctor_fails_when_interactive_logs_tracked(tmp_path, monkeypatch, capsys):
    project_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, project_dir)
    base_run = doctor.subprocess.run

    def run_with_tracked_logs(cmd, **kwargs):
        if cmd[:2] == ["git", "ls-files"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=".interactive_logs/session/a0f4\n",
                stderr="",
            )
        return base_run(cmd, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "run", run_with_tracked_logs)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert ".interactive_logs/ has 1 tracked file(s)" in output
    assert "git rm -r --cached .interactive_logs" in output


@pytest.mark.skipif(
    not hasattr(os, "getuid"),
    reason="uid probe is POSIX-only; doctor skips this check on Windows "
    "(no os.getuid, st_uid is always 0) — F-16",
)
def test_doctor_fails_on_container_uid_mismatch(tmp_path, monkeypatch, capsys):
    project_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, project_dir)
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    base_run = doctor.subprocess.run

    def run_with_bad_uid(cmd, **kwargs):
        if cmd[1:3] == ["run", "--rm"] and cmd[-2:] == ["id", "-u"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="4242\n", stderr="")
        return base_run(cmd, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "run", run_with_bad_uid)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert "container agent uid 4242 !=" in output


def test_doctor_skips_simulate_dry_run_when_tb_top_runtime_resolved(
    tmp_path,
    monkeypatch,
    capsys,
):
    configs_text = '[fast]\ndefines = []\ntop_module = "dut"\ntests = ["smoke"]\n'
    project_dir = _write_project(tmp_path, configs_text=configs_text)
    _patch_environment(monkeypatch, tmp_path, project_dir)
    base_run = doctor.subprocess.run

    def run_with_tb_top_error(cmd, **kwargs):
        if cmd[:3] == [sys.executable, "-m", "booley.flows.sim"]:
            return subprocess.CompletedProcess(
                cmd,
                2,
                stdout="",
                stderr="sim: --tb-top is required when running outside a state-backed run",
            )
        return base_run(cmd, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "run", run_with_tb_top_error)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 0
    assert "sim dry-run skipped - tb_top is resolved at runtime" in output


# ---------------------------------------------------------------------------
# .core audit (ADR 0022 Phases 6-7) — _run_core_audit driven directly
# ---------------------------------------------------------------------------


class _Rec:
    """Collects doctor check outcomes as (level, message) tuples.

    The signatures mirror ``_Reporter``'s exactly — ``warn_``/``fail_`` take an
    optional fix hint. A stub that accepted fewer args than the real reporter is
    what let the ``warn_(msg, fix)`` TypeError reach a user's `doctor` run: the
    checks that pass fix hints to ``_warn`` had no host-side coverage, so the
    crash only surfaced on a real cocotb project.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def p(self, m: str) -> None:
        self.events.append(("pass", m))

    def w(self, m: str, fix: str = "") -> None:
        self.events.append(("warn", m))

    def n(self, m: str) -> None:
        self.events.append(("note", m))

    def s(self, m: str) -> None:
        self.events.append(("skip", m))

    def f(self, m: str, fix: str = "") -> None:
        self.events.append(("fail", m))

    def fails(self) -> list[str]:
        return [m for lvl, m in self.events if lvl == "fail"]

    def kinds(self) -> set[str]:
        return {lvl for lvl, _ in self.events}


def test_host_doctor_rejects_unissued_session_spec(tmp_path, monkeypatch) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    (tmp_path / ".devcontainer").mkdir()
    (tmp_path / ".devcontainer" / "devcontainer.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    project = doctor.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")
    rec = _Rec()

    doctor._check_issued_session_runtime(project, "docker", rec.p, rec.s, rec.f)

    assert any("host issuance is invalid" in message for message in rec.fails())


def test_host_doctor_accepts_issued_spec_and_no_live_resources(tmp_path, monkeypatch) -> None:
    from booley.eda import runtime_spec

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
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    project = doctor.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")
    rec = _Rec()

    doctor._check_issued_session_runtime(project, "docker", rec.p, rec.s, rec.f)

    assert not rec.fails()
    assert any(
        "valid host issuance" in message for level, message in rec.events if level == "pass"
    )


def _issued_runtime_state(tmp_path: Path):
    from booley.eda import runtime_spec

    image = "sha256:" + "a" * 64
    issuance = runtime_spec.Issuance(
        version=runtime_spec.STAMP_VERSION,
        project_root=str(tmp_path),
        spec_sha256="b" * 64,
        image=image,
        image_id=image,
        keeper_image=runtime_spec.keeper_image(tmp_path),
        policy_revision=1,
        installation=None,
        license_profile=None,
        wrapper_sha256=None,
        relay_image_id=None,
        validator_sha256="d" * 64,
        file_sha256="c" * 64,
    )
    spec = {
        "image": image,
        "remoteUser": "agent",
        "workspaceFolder": "/work",
        "workspaceMount": "source=${localWorkspaceFolder},target=/work,type=bind",
        "mounts": [],
        "containerEnv": {},
        "remoteEnv": {},
        "runArgs": [
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "4096",
            "--network",
            "booley-egress",
        ],
    }
    labels = dict(item.split("=", 1) for item in runtime_spec.labels(issuance))
    state = {
        "Image": image,
        "Config": {
            "Image": image,
            "User": "agent",
            "WorkingDir": "/work",
            "Env": [],
            "Labels": labels,
        },
        "HostConfig": {
            "CapAdd": None,
            "CapDrop": ["ALL"],
            "Privileged": False,
            "PidMode": "",
            "IpcMode": "private",
            "UsernsMode": "",
            "Devices": [],
            "DeviceRequests": None,
            "PortBindings": {},
            "PublishAllPorts": False,
            "PidsLimit": 4096,
            "SecurityOpt": ["no-new-privileges"],
            "Memory": 0,
        },
        "NetworkSettings": {"Networks": {"booley-egress": {}}},
        "Mounts": [
            {
                "Destination": "/work",
                "Source": str(tmp_path),
                "Type": "bind",
                "RW": True,
            }
        ],
    }
    return issuance, spec, labels, state


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
    from booley.eda import runtime_spec

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
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "runtime-1\n", ""),
    )

    def inspect(argv):
        if argv[-1] == "{{json .Config.Labels}}":
            return json.dumps(labels)
        return json.dumps([state])

    monkeypatch.setattr(session_runtime, "_docker_stdout", inspect)
    project = doctor.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")
    rec = _Rec()

    doctor._check_issued_session_runtime(project, "docker", rec.p, rec.s, rec.f)

    assert any("state differs from current host issuance" in message for message in rec.fails())


def test_host_doctor_accepts_vscode_managed_runtime_state(tmp_path, monkeypatch) -> None:
    from booley.eda import runtime_spec

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
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "runtime-1\n", ""),
    )

    def inspect(argv):
        if argv[-1] == "{{json .Config.Labels}}":
            return json.dumps(labels)
        return json.dumps([state])

    monkeypatch.setattr(session_runtime, "_docker_stdout", inspect)
    project = doctor.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")
    rec = _Rec()

    doctor._check_issued_session_runtime(project, "docker", rec.p, rec.s, rec.f)

    assert not rec.fails()
    assert any("state matches" in message for level, message in rec.events if level == "pass")


def test_in_runtime_doctor_executes_mounted_vivado_policy_branch(tmp_path, monkeypatch) -> None:
    from booley.eda import vivado

    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    wrapper_bytes = b"booley-vivado-wrapper"
    wrapper_digest = hashlib.sha256(wrapper_bytes).hexdigest()
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
    monkeypatch.setattr(doctor, "_check_runtime_isolation", lambda *_args: True)
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
    monkeypatch.setattr(vivado, "wrapper_sha256", lambda: wrapper_digest)
    monkeypatch.setattr(
        doctor.os,
        "access",
        lambda path, mode: path == Path(vivado.CONTAINER_TARGET) / "Vivado" / "bin" / "vivado",
    )
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, f"vivado v{vivado.SUPPORTED_VERSION}\n", ""
        ),
    )
    project = doctor.ProjectAudit(
        tmp_path,
        project_dir,
        {"eda": {"vivado": {"provisioning": "host"}}},
        {},
        "sim",
    )
    rec = _Rec()

    doctor._check_issued_session_runtime(project, None, rec.p, rec.s, rec.f)

    assert not rec.fails()
    assert any(
        f"mounted Vivado {vivado.SUPPORTED_VERSION}" in message
        for level, message in rec.events
        if level == "pass"
    )


def _audit(root: Path) -> _Rec:
    pd = root / ".booley_project"
    pd.mkdir(exist_ok=True)
    project = doctor.ProjectAudit(
        project_root=root,
        project_dir=pd,
        booley_toml={},
        configs_toml={"x": {}},
        first_target="x",
    )
    rec = _Rec()
    doctor._run_core_audit(project, rec.p, rec.w, rec.s, rec.f)
    return rec


_CLEAN_SIM_CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv]}
  tb: {files: [tb/tb_dut.sv], tags: [tb]}
targets:
  sim:
    flow: sim
    flow_options: {tool: verilator}
    filesets: [rtl, tb]
    toplevel: tb_dut
"""

_COCOTB_SIM_CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv]}
  tb: {files: [tb/test_dut.py], tags: [tb]}
targets:
  sim:
    flow: sim
    flow_options: {tool: verilator, cocotb_module: test_dut}
    filesets: [rtl, tb]
    toplevel: dut
"""


def _write_cocotb_project(
    root: Path,
    *,
    tb_body: str,
    tests: list[str] | None = None,
) -> None:
    """A minimal project whose only sim Target is a Cocotb Target (ADR 0034).

    *tb_body* is the Python testbench; *tests* (if given) is the tests.toml
    ``tests = [...]`` list declared on the Target.
    """
    (root / "design.core").write_text(_COCOTB_SIM_CORE, encoding="utf-8")
    (root / "rtl").mkdir(exist_ok=True)
    (root / "tb").mkdir(exist_ok=True)
    (root / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    (root / "tb" / "test_dut.py").write_text(tb_body, encoding="utf-8")
    pd = root / ".booley_project"
    pd.mkdir(exist_ok=True)
    if tests is not None:
        names = ", ".join(f'"{t}"' for t in tests)
        (pd / "tests.toml").write_text(f"[sim]\ntests = [{names}]\n", encoding="utf-8")


class TestCoreAudit:
    def test_no_cores_is_a_hard_fail(self, tmp_path: Path):
        # ADR 0039: a resolvable .core Target is a precondition — a project
        # without one is a setup failure, never a skip.
        rec = _audit(tmp_path)
        assert any("project has no .core" in m for m in rec.fails())

    def test_clean_core_passes(self, tmp_path: Path):
        (tmp_path / "design.core").write_text(_CLEAN_SIM_CORE, encoding="utf-8")
        rec = _audit(tmp_path)
        assert rec.fails() == []
        assert any("TB fileset tagged" in m for lvl, m in rec.events if lvl == "pass")
        assert any("security validation passed" in m for lvl, m in rec.events)

    def _cpp_target_project(self, tmp_path: Path, include_line: str) -> doctor.ProjectAudit:
        (tmp_path / "design.core").write_text(
            """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv]}
  tb:
    files: [tb/tb_dut.sv, tb/dpi_memutil.cc]
    tags: [tb]
  cpp: {files: [tb/harness.cc]}
targets:
  sim:
    flow: sim
    flow_options: {tool: verilator}
    filesets: [rtl, tb, cpp]
    toplevel: tb_dut
""",
            encoding="utf-8",
        )
        (tmp_path / "tb").mkdir(exist_ok=True)
        (tmp_path / "tb" / "harness.cc").write_text(include_line, encoding="utf-8")
        pd = tmp_path / ".booley_project"
        pd.mkdir(exist_ok=True)
        return doctor.ProjectAudit(
            project_root=tmp_path,
            project_dir=pd,
            booley_toml={"flows": {"sim": {"default_target": "sim"}}},
            configs_toml={"sim": {}},
            first_target="sim",
        )

    def test_native_dependency_is_flagged(self, tmp_path: Path):
        """F-9: Ibex's dpi_memutil.cc includes <libelf.h> and links -lelf.

        Requirements baking is Python-only, so the derived image shipped
        only the runtime libelf.so.1 — no header, no linker input — and the
        failure surfaced deep inside a simulation build.
        """
        project = self._cpp_target_project(tmp_path, "#include <libelf.h>\n")
        rec = _Rec()
        doctor._audit_native_dependencies(project, rec.p, rec.w)

        warns = [m for lvl, m in rec.events if lvl == "warn"]
        assert any("libelf-dev" in m for m in warns)
        assert any("tb/harness.cc" in m for m in warns)
        # Advisory only — this is a curated hint list, not a resolver.
        assert rec.fails() == []

    def test_clean_cpp_sources_pass(self, tmp_path: Path):
        project = self._cpp_target_project(
            tmp_path, "#include <cstdint>\n#include <verilated.h>\n"
        )
        rec = _Rec()
        doctor._audit_native_dependencies(project, rec.p, rec.w)

        assert [m for lvl, m in rec.events if lvl == "warn"] == []
        assert any("no known-missing native build" in m for lvl, m in rec.events if lvl == "pass")

    def test_native_dependency_declared_in_project_dockerfile_passes(self, tmp_path: Path):
        project = self._cpp_target_project(tmp_path, "#include <libelf.h>\n")
        docker_dir = project.project_dir / "docker"
        docker_dir.mkdir()
        (docker_dir / "Dockerfile").write_text(
            "FROM booley-sandbox-riscv\n"
            "RUN apt-get update \\\n"
            "    && apt-get install -y --no-install-recommends libelf-dev\n",
            encoding="utf-8",
        )
        rec = _Rec()

        doctor._audit_native_dependencies(project, rec.p, rec.w)

        assert [m for lvl, m in rec.events if lvl == "warn"] == []
        assert any("libelf-dev" in m for lvl, m in rec.events if lvl == "pass")

    def test_native_dependency_in_dockerfile_comment_still_warns(self, tmp_path: Path):
        project = self._cpp_target_project(tmp_path, "#include <libelf.h>\n")
        docker_dir = project.project_dir / "docker"
        docker_dir.mkdir()
        (docker_dir / "Dockerfile").write_text(
            "FROM booley-sandbox-riscv\n# install libelf-dev later\n",
            encoding="utf-8",
        )
        rec = _Rec()

        doctor._audit_native_dependencies(project, rec.p, rec.w)

        assert any("libelf-dev" in m for lvl, m in rec.events if lvl == "warn")

    def test_headers_present_in_the_base_image_are_not_flagged(self, tmp_path: Path):
        """zlib1g-dev is installed in the base sandbox — warning would be noise."""
        project = self._cpp_target_project(tmp_path, "#include <zlib.h>\n")
        rec = _Rec()
        doctor._audit_native_dependencies(project, rec.p, rec.w)
        assert [m for lvl, m in rec.events if lvl == "warn"] == []

    def test_project_without_cpp_sources_is_silent(self, tmp_path: Path):
        (tmp_path / "design.core").write_text(_CLEAN_SIM_CORE, encoding="utf-8")
        pd = tmp_path / ".booley_project"
        pd.mkdir(exist_ok=True)
        project = doctor.ProjectAudit(
            project_root=tmp_path,
            project_dir=pd,
            booley_toml={"flows": {"sim": {"default_target": "sim"}}},
            configs_toml={"sim": {}},
            first_target="sim",
        )
        rec = _Rec()
        doctor._audit_native_dependencies(project, rec.p, rec.w)
        assert rec.events == []

    def test_in_scope_script_is_advisory_not_a_hard_fail(self, tmp_path: Path):
        """F-10: doctor's Scope is synthetic, so it cannot hard-fail on it.

        Ibex's upstream `check_tool_requirements.core` references a script
        under `util/`, which doctor classes as writable — so the audit failed
        the whole setup gate over a generator the configured Targets never
        invoke. The binding check is the per-ticket Scope at commit time.
        """
        (tmp_path / "design.core").write_text(
            _CLEAN_SIM_CORE
            + """\
generators:
  check_reqs:
    command: rtl/check_tool_requirements.py
""",
            encoding="utf-8",
        )
        (tmp_path / "rtl").mkdir(exist_ok=True)
        (tmp_path / "rtl" / "check_tool_requirements.py").write_text("#!/usr/bin/env python3\n")

        rec = _audit(tmp_path)

        assert rec.fails() == []
        warns = [m for lvl, m in rec.events if lvl == "warn"]
        assert any("in_scope_script" in m for m in warns)
        # The advisory must say why it is not a gate, or it reads as a bug.
        assert any("per-ticket" in m for m in warns)

    def test_structural_core_violation_still_hard_fails(self, tmp_path: Path):
        """Scope-independent violations are properties of the .core itself."""
        (tmp_path / "design.core").write_text(
            """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv]}
  tb: {files: [tb/tb_dut.sv], tags: [tb]}
targets:
  sim:
    flow: sim
    flow_options: {tool: verilator}
    filesets: [rtl, tb]
    toplevel: tb_dut
parameters:
  width:
    datatype: expression
    paramtype: vlogparam
""",
            encoding="utf-8",
        )
        rec = _audit(tmp_path)
        assert any("expr_param" in m for m in rec.fails())

    def test_untagged_tb_sim_fails(self, tmp_path: Path):
        (tmp_path / "design.core").write_text(
            """\
CAPI=2:
name: ::demo:0
filesets:
  all: {files: [rtl/dut.sv, tb/tb_dut.sv]}
targets:
  sim:
    flow: sim
    flow_options: {tool: verilator}
    filesets: [all]
    toplevel: tb_dut
""",
            encoding="utf-8",
        )
        rec = _audit(tmp_path)
        assert any("tags:[tb]" in m for m in rec.fails())

    def test_sim_target_staging_zero_files_fails(self, tmp_path: Path):
        # The untagged-TB predicate needs RTL files to fire; a sim Target
        # whose filesets stage NOTHING used to slip past it and die at run
        # time with a toplevel-not-found error blamed on the design.
        (tmp_path / "design.core").write_text(
            """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv]}
  tb: {files: [tb/tb_dut.sv], tags: [tb]}
targets:
  sim:
    flow: sim
    flow_options: {tool: verilator}
    filesets: []
    toplevel: tb_dut
""",
            encoding="utf-8",
        )
        rec = _audit(tmp_path)
        assert any("stages zero files" in m for m in rec.fails())

    def test_icarus_sim_core_missing_g2012_fails_full_audit(self, tmp_path: Path):
        # Wiring check: the -g2012 language-mode audit runs as part of the
        # core audit, not only when invoked directly.
        (tmp_path / "design.core").write_text(
            _CLEAN_SIM_CORE.replace("tool: verilator", "tool: icarus"),
            encoding="utf-8",
        )
        rec = _audit(tmp_path)
        assert any("-g2012" in m for m in rec.fails())

    def test_icarus_sim_core_with_g2012_full_audit_clean(self, tmp_path: Path):
        (tmp_path / "design.core").write_text(
            _CLEAN_SIM_CORE.replace(
                "flow_options: {tool: verilator}",
                "flow_options: {tool: icarus, iverilog_options: [-g2012]}",
            ),
            encoding="utf-8",
        )
        rec = _audit(tmp_path)
        assert rec.fails() == []
        assert any("SV language flag" in m for lvl, m in rec.events if lvl == "pass")

    def test_verilator_lint_target_not_flagged_for_missing_tb(self, tmp_path: Path):
        # verilator backs both sim and lint; a flow:lint Target has no TB and
        # must NOT trip the tagged-TB check (regression: gate on Flow, not EDA tool).
        (tmp_path / "design.core").write_text(
            """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv]}
targets:
  lint:
    flow: lint
    flow_options: {tool: verilator}
    filesets: [rtl]
    toplevel: dut
""",
            encoding="utf-8",
        )
        rec = _audit(tmp_path)
        assert rec.fails() == []

    def _write_tb(self, root: Path, body: str, *, dump: bool = False) -> None:
        (root / "rtl").mkdir(exist_ok=True)
        (root / "tb").mkdir(exist_ok=True)
        (root / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
        (root / "tb" / "tb_dut.sv").write_text(body, encoding="utf-8")
        if dump:
            (root / "tb" / "booley_vcd_dump.sv").write_text(
                "module booley_vcd_dump; endmodule\n", encoding="utf-8"
            )

    def test_missing_pass_sentinel_warns(self, tmp_path: Path):
        (tmp_path / "design.core").write_text(_CLEAN_SIM_CORE, encoding="utf-8")
        self._write_tb(tmp_path, "// a testbench with no verdict marker\n")
        rec = _audit(tmp_path)
        warns = [m for lvl, m in rec.events if lvl == "warn"]
        assert any("no configured pass sentinel" in m for m in warns)
        # A missing dump module is NOT flagged: the trace overlay supplies it
        # from refs/, so it need not sit in the tracked fileset (Stealth Mode).
        assert not any("trace module" in m for m in warns)
        assert rec.fails() == []

    def test_builtin_sentinel_and_trace_module_pass(self, tmp_path: Path):
        core = _CLEAN_SIM_CORE.replace(
            "tb: {files: [tb/tb_dut.sv], tags: [tb]}",
            "tb: {files: [tb/tb_dut.sv, tb/booley_vcd_dump.sv], tags: [tb]}",
        )
        (tmp_path / "design.core").write_text(core, encoding="utf-8")
        self._write_tb(tmp_path, '$display("[SIM_RESULT] PASSED");\n', dump=True)
        rec = _audit(tmp_path)
        assert any(
            "emits a recognized pass sentinel" in m for lvl, m in rec.events if lvl == "pass"
        )
        assert not any(
            "pass sentinel" in m or "trace module" in m for lvl, m in rec.events if lvl == "warn"
        )

    def test_configured_sentinel_honored(self, tmp_path: Path):
        (tmp_path / "design.core").write_text(_CLEAN_SIM_CORE, encoding="utf-8")
        self._write_tb(tmp_path, "ALL TESTS PASSED.\n")
        pd = tmp_path / ".booley_project"
        pd.mkdir(exist_ok=True)
        project = doctor.ProjectAudit(
            project_root=tmp_path,
            project_dir=pd,
            booley_toml={"flows": {"sim": {"pass_sentinels": ["ALL TESTS PASSED."]}}},
            configs_toml={"x": {}},
            first_target="x",
        )
        rec = _Rec()
        doctor._run_core_audit(project, rec.p, rec.w, rec.s, rec.f)
        assert any(
            "emits a recognized pass sentinel" in m for lvl, m in rec.events if lvl == "pass"
        )

    def test_cocotb_target_is_exempt_from_the_sentinel_check(self, tmp_path: Path):
        # B2: a cocotb Target's verdict comes from results.xml — sentinel
        # scanning is bypassed outright (ADR 0034 dec 6) — and its TB is Python,
        # so it *cannot* carry a $display sentinel. Warning "a passing run will
        # read as INCONCLUSIVE" is false (taxi scored 14/14 PASS with no
        # sentinel) and its fix hint is unfollowable. Check must not fire.
        _write_cocotb_project(tmp_path, tb_body="async def test_reset(dut): pass\n")
        rec = _audit(tmp_path)
        assert not any("pass sentinel" in m for lvl, m in rec.events)
        assert rec.fails() == []

    def test_non_cocotb_sim_target_still_gets_the_sentinel_check(self, tmp_path: Path):
        # The exemption is keyed on cocotb_module, so a plain SV sim Target in
        # the same project must still be audited.
        (tmp_path / "design.core").write_text(_CLEAN_SIM_CORE, encoding="utf-8")
        self._write_tb(tmp_path, "// a testbench with no verdict marker\n")
        rec = _audit(tmp_path)
        warns = [m for lvl, m in rec.events if lvl == "warn"]
        assert any("no configured pass sentinel" in m for m in warns)
        # The SV remedy is the right hint here — no cocotb noise for an SV TB.
        assert any("refs/sim_result_sentinel.sv" in m for m in warns)
        assert not any("cocotb_module" in m for m in warns)

    def test_python_tb_without_cocotb_module_hints_cocotb_module(self, tmp_path: Path):
        # F-8: a .py TB fileset without cocotb_module is a cocotb TB that has
        # not declared its module yet. The sentinel warning must name the real
        # remedy (declare cocotb_module, ADR 0034) — "add a $display sentinel"
        # is unfollowable advice for a Python testbench.
        core = _COCOTB_SIM_CORE.replace(", cocotb_module: test_dut", "")
        (tmp_path / "design.core").write_text(core, encoding="utf-8")
        (tmp_path / "rtl").mkdir(exist_ok=True)
        (tmp_path / "tb").mkdir(exist_ok=True)
        (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
        (tmp_path / "tb" / "test_dut.py").write_text(
            "async def test_reset(dut): pass\n", encoding="utf-8"
        )
        rec = _audit(tmp_path)
        warns = [m for lvl, m in rec.events if lvl == "warn"]
        assert any("no configured pass sentinel" in m and "cocotb_module" in m for m in warns)
        # The SV-only fix hint must not be the advice for a Python TB.
        assert not any("refs/sim_result_sentinel.sv" in m for m in warns)
        assert rec.fails() == []

    def test_duplicate_name_across_distinct_cores_is_legal(self, tmp_path: Path):
        # ADR 0030: identity is per-(VLNV, name); two distinct cores sharing a
        # bare Target name is legal FuseSoC (ibex: 'lint' on 54 cores), so the
        # audit must NOT fail with a collision — enumerate_targets exposes a
        # first-wins view instead of raising.
        lint_core = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv]}
targets:
  lint:
    flow: lint
    flow_options: {tool: verilator}
    filesets: [rtl]
    toplevel: dut
"""
        (tmp_path / "a.core").write_text(lint_core, encoding="utf-8")
        (tmp_path / "b.core").write_text(
            lint_core.replace("::demo:0", "::demo2:0"), encoding="utf-8"
        )
        rec = _audit(tmp_path)
        assert not any("collision" in m for m in rec.fails())
        assert any(".core Targets enumerated" in m for lvl, m in rec.events if lvl == "pass")

    def test_fpga_hook_fails_security(self, tmp_path: Path):
        (tmp_path / "impl.core").write_text(
            """\
CAPI=2:
name: ::demo:0
targets:
  impl:
    flow_options: {tool: vivado}
    hooks:
      post_build: [run_bitstream]
""",
            encoding="utf-8",
        )
        rec = _audit(tmp_path)
        assert any("[fpga_hook]" in m for m in rec.fails())

    def test_bad_tests_toml_fails(self, tmp_path: Path):
        (tmp_path / "design.core").write_text(_CLEAN_SIM_CORE, encoding="utf-8")
        pd = tmp_path / ".booley_project"
        pd.mkdir(exist_ok=True)
        (pd / "tests.toml").write_text(
            '[sim]\nselect = "not-a-plusarg with spaces"\n', encoding="utf-8"
        )
        rec = _audit(tmp_path)
        assert any("tests.toml invalid" in m for m in rec.fails())


# ===========================================================================
# Interactive Mode persistent state-volume note
# ===========================================================================


class TestStateVolumeCheck:
    """_check_interactive_state_volumes surfaces orphans for pruning."""

    @staticmethod
    def _project(root: Path) -> doctor.ProjectAudit:
        pd = root / ".booley_project"
        pd.mkdir(exist_ok=True)
        return doctor.ProjectAudit(
            project_root=root,
            project_dir=pd,
            booley_toml={},
            configs_toml={"x": {}},
            first_target="x",
        )

    def _run(self, tmp_path, monkeypatch, vols, *, verbose=False) -> _Rec:
        from booley.harness import interactive_docker as idk

        monkeypatch.setattr(idk, "state_volumes", lambda: vols)
        rec = _Rec()
        doctor._check_interactive_state_volumes(
            self._project(tmp_path),
            "docker",
            verbose,
            rec.p,
            rec.w,
            rec.s,
        )
        return rec

    def test_skips_without_runtime(self, tmp_path):
        rec = _Rec()
        doctor._check_interactive_state_volumes(
            self._project(tmp_path),
            None,
            False,
            rec.p,
            rec.w,
            rec.s,
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
        self._run(
            tmp_path,
            monkeypatch,
            [
                "booley-codex-state-someoldproject",
            ],
            verbose=True,
        )
        assert "booley-codex-state-someoldproject" in capsys.readouterr().out


class TestWcpServerCheck:
    """_check_wcp_server probes the live VaporView WCP port, not just the spec.

    The gap it exists to close: a devcontainer.json that asks for the viewer
    correctly (so every static check passes) still leaves the server dark on the
    first window of a fresh container, and only `bwave gui` ever finds out.
    """

    @staticmethod
    def _project(root: Path) -> doctor.ProjectAudit:
        pd = root / ".booley_project"
        pd.mkdir(exist_ok=True)
        return doctor.ProjectAudit(
            project_root=root,
            project_dir=pd,
            booley_toml={},
            configs_toml={"x": {}},
            first_target="x",
        )

    def _run(
        self,
        tmp_path,
        monkeypatch,
        *,
        docker="docker",
        container="vsc-proj-abc-uid",
        listening: bool | None = True,
        attached: bool | None = True,
        in_container: bool = False,
        probe=None,
    ) -> _Rec:
        from booley import runtime_context
        from booley.harness import session_runtime

        monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: in_container)
        monkeypatch.setattr(session_runtime, "vscode_session_container", lambda root: container)
        monkeypatch.setattr(doctor, "_wcp_port_listening", probe or (lambda argv, port: listening))
        monkeypatch.setattr(doctor, "_vscode_extension_host_running", lambda argv: attached)
        rec = _Rec()

        def fail(message, fix=""):
            rec.events.append(("fail", f"{message}\n{fix}"))

        doctor._check_wcp_server(self._project(tmp_path), docker, rec.p, rec.s, fail)
        return rec

    def test_passes_when_port_answers(self, tmp_path, monkeypatch):
        rec = self._run(tmp_path, monkeypatch)
        assert rec.kinds() == {"pass"}

    def test_fails_when_port_is_dark(self, tmp_path, monkeypatch):
        rec = self._run(tmp_path, monkeypatch, listening=False)
        assert rec.kinds() == {"fail"}
        failure = next(m for lvl, m in rec.events if lvl == "fail")
        # The fix nobody guesses: reload, not rebuild.
        assert "Reload Window" in failure
        assert "do not run 'WCP: Start Server'" in failure
        assert "vsc-proj-abc-uid" in failure

    def test_skips_without_runtime(self, tmp_path, monkeypatch):
        rec = self._run(tmp_path, monkeypatch, docker=None)
        assert rec.kinds() == {"skip"}

    def test_skips_without_vscode_container(self, tmp_path, monkeypatch):
        # A headless `booley session up` container has no extension host and so
        # no WCP server by design — never a finding.
        rec = self._run(tmp_path, monkeypatch, container=None)
        assert rec.kinds() == {"skip"}
        assert any("no VS Code devcontainer" in m for _, m in rec.events)

    def test_skips_when_devcontainer_has_no_attached_extension_host(self, tmp_path, monkeypatch):
        rec = self._run(tmp_path, monkeypatch, attached=False, listening=False)
        assert rec.kinds() == {"skip"}
        assert any("no VS Code extension host attached" in m for _, m in rec.events)

    def test_skips_when_probe_cannot_run(self, tmp_path, monkeypatch):
        rec = self._run(tmp_path, monkeypatch, listening=None)
        assert rec.kinds() == {"skip"}

    def test_in_container_probes_loopback_without_docker(self, tmp_path, monkeypatch):
        """Inside the sandbox there is no docker and no container name to find —
        the probe must still run, against the same socket `bwave gui` dials."""
        seen: list[list[str]] = []

        def probe(argv, port):
            seen.append(argv)
            return False

        rec = self._run(tmp_path, monkeypatch, docker=None, in_container=True, probe=probe)
        assert seen == [[]]  # no `docker exec` prefix
        assert rec.kinds() == {"fail"}


class TestWcpPortProbe:
    """_wcp_port_listening turns a TCP connect into pass/fail/unknown."""

    @staticmethod
    def _fake_run(monkeypatch, outcome):
        """Replace subprocess.run; `outcome` is a return code or an exception."""

        def run(argv, **kwargs):
            if isinstance(outcome, Exception):
                raise outcome
            return subprocess.CompletedProcess(argv, outcome, "", "")

        monkeypatch.setattr(doctor.subprocess, "run", run)

    def test_true_on_zero_exit(self, monkeypatch):
        self._fake_run(monkeypatch, 0)
        assert doctor._wcp_port_listening(["docker", "exec", "c"], 54322) is True

    def test_false_on_refused(self, monkeypatch):
        self._fake_run(monkeypatch, 1)
        assert doctor._wcp_port_listening(["docker", "exec", "c"], 54322) is False

    def test_none_when_unrunnable(self, monkeypatch):
        self._fake_run(monkeypatch, FileNotFoundError("docker"))
        assert doctor._wcp_port_listening(["docker", "exec", "c"], 54322) is None

    def test_none_on_timeout(self, monkeypatch):
        self._fake_run(monkeypatch, subprocess.TimeoutExpired("docker", 20))
        assert doctor._wcp_port_listening(["docker", "exec", "c"], 54322) is None


# ===========================================================================
# Stale devcontainer.json detection (predates the home-state persistence fix)
# ===========================================================================


class TestDevcontainerSpecStaleness:
    """_check_devcontainer_spec must surface a spec that loses history on rebuild."""

    def _run(
        self,
        root: Path,
        monkeypatch,
        image: str | None = None,
        declared_provider: str | None = None,
    ) -> _Rec:
        from booley.harness import devcontainer as dc

        # Isolate from git; tracking is exercised by other tests.
        monkeypatch.setattr(doctor, "_devcontainer_tracked", lambda p: False)
        # Isolate from the developer machine's own `booley auth` store — the
        # token-seed drift branch reads it, and a real stored token would flip
        # the fresh-spec tests below from pass to warn.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg-isolated"))
        monkeypatch.setattr(doctor.idk, "image_id", lambda image: image)
        rec = _Rec()
        doctor._check_devcontainer_spec(
            root,
            image or dc.SANDBOX_IMAGE,
            declared_provider,
            rec.p,
            rec.w,
            rec.f,
            _note=rec.n,
        )
        return rec

    def test_fresh_claude_spec_passes(self, tmp_path, monkeypatch):
        from booley.harness import devcontainer as dc

        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CLAUDE))
        rec = self._run(tmp_path, monkeypatch)
        assert rec.kinds() == {"pass"}

    def test_verified_pdk_without_spec_mount_warns(self, tmp_path, monkeypatch):
        from booley.harness import devcontainer as dc

        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CLAUDE))
        monkeypatch.setattr(doctor.nangate_pdk, "is_ready", lambda: True)

        rec = self._run(tmp_path, monkeypatch)

        assert rec.fails() == []
        assert any(
            level == "warn" and "/opt/pdk" in message and "synthesis" in message
            for level, message in rec.events
        )

    def test_verified_pdk_with_spec_mount_passes(self, tmp_path, monkeypatch):
        from booley.harness import devcontainer as dc

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
        from booley.harness import devcontainer as dc

        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CLAUDE))
        rec = self._run(tmp_path, monkeypatch, image="openc910-booley-sandbox:latest")
        assert rec.fails() == []
        assert any(
            lvl == "warn" and "openc910-booley-sandbox:latest" in m and "--seed" in m
            for lvl, m in rec.events
        )

    def test_matching_custom_image_passes(self, tmp_path, monkeypatch):
        # Spec built for the same custom image the project configures: no drift.
        from booley.harness import devcontainer as dc

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
        # Inside an issued Session Runtime Docker is intentionally absent. The
        # spec is already pinned to an immutable ID, but the configured tag
        # cannot be resolved there; string-comparing the ID to the tag would be
        # a false stale-image warning.
        immutable_id = "sha256:" + "a" * 64
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE, image=immutable_id)
        dc.write_devcontainer(tmp_path, spec)
        monkeypatch.setattr(doctor, "_devcontainer_tracked", lambda p: False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-isolated"))
        monkeypatch.setattr(doctor.idk, "image_id", lambda image: None)
        rec = _Rec()

        doctor._check_devcontainer_spec(
            tmp_path,
            dc.SANDBOX_IMAGE,
            None,
            rec.p,
            rec.w,
            rec.f,
            _note=rec.n,
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
        monkeypatch.setattr(doctor, "_devcontainer_tracked", lambda p: False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-isolated"))
        monkeypatch.setattr(doctor.idk, "image_id", lambda image: current_id)
        rec = _Rec()

        doctor._check_devcontainer_spec(
            tmp_path,
            dc.SANDBOX_IMAGE,
            None,
            rec.p,
            rec.w,
            rec.f,
            _note=rec.n,
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
        from booley.harness import devcontainer as dc

        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CLAUDE))
        rec = self._run(tmp_path, monkeypatch, declared_provider=dc.APP_CODEX)
        assert any("BOOLEY_AGENT_APP" in m and "codex" in m for m in rec.fails())
        assert not any(lvl == "warn" for lvl, _ in rec.events)

    def test_agent_app_matching_declared_provider_passes(self, tmp_path, monkeypatch):
        from booley.harness import devcontainer as dc

        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CODEX))
        rec = self._run(tmp_path, monkeypatch, declared_provider=dc.APP_CODEX)
        assert rec.kinds() == {"pass"}

    def test_undeclared_provider_mutes_the_app_drift_warn(self, tmp_path, monkeypatch):
        # No [agent] provider: the seeder falls back to host detection, so
        # there is nothing the on-disk app can be drift-checked against.
        from booley.harness import devcontainer as dc

        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CLAUDE))
        rec = self._run(tmp_path, monkeypatch, declared_provider=None)
        assert rec.kinds() == {"pass"}

    def test_agent_app_drift_reported_over_missing_state_volume(self, tmp_path, monkeypatch):
        # A mismatched spec still mounts a volume for the app it *names*, so the
        # persistence check would pass and hide the real problem. Order matters:
        # the app drift must be what the user is told to fix.
        from booley.harness import devcontainer as dc

        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE)
        assert dc.spec_state_is_persisted(spec) is True  # the misleading "all good"
        dc.write_devcontainer(tmp_path, spec)
        rec = self._run(tmp_path, monkeypatch, declared_provider=dc.APP_CODEX)
        assert rec.fails() and all("BOOLEY_AGENT_APP" in m for m in rec.fails())
        assert not any(lvl == "warn" for lvl, _ in rec.events)

    def test_stale_claude_spec_warns_not_fails(self, tmp_path, monkeypatch):
        from booley.harness import devcontainer as dc

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
        from booley.harness import auth_token
        from booley.harness import devcontainer as dc

        dc.write_devcontainer(tmp_path, dc.build_devcontainer_spec(dc.APP_CLAUDE))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        auth_token.store_token("sk-ant-oat01-stored")
        rec = _Rec()
        monkeypatch.setattr(doctor, "_devcontainer_tracked", lambda p: False)
        monkeypatch.setattr(doctor.idk, "image_id", lambda image: image)
        doctor._check_devcontainer_spec(tmp_path, dc.SANDBOX_IMAGE, None, rec.p, rec.w, rec.f)
        assert rec.fails() == []
        assert any(lvl == "warn" and "booley auth" in m and "--seed" in m for lvl, m in rec.events)

    def test_stored_token_with_seed_mount_passes(self, tmp_path, monkeypatch):
        from booley.harness import auth_token
        from booley.harness import devcontainer as dc

        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        path = auth_token.store_token("sk-ant-oat01-stored")
        spec = dc.build_devcontainer_spec(dc.APP_CLAUDE, token_seed_source=str(path))
        dc.write_devcontainer(tmp_path, spec)
        rec = _Rec()
        monkeypatch.setattr(doctor, "_devcontainer_tracked", lambda p: False)
        monkeypatch.setattr(doctor.idk, "image_id", lambda image: image)
        doctor._check_devcontainer_spec(tmp_path, dc.SANDBOX_IMAGE, None, rec.p, rec.w, rec.f)
        assert rec.kinds() == {"pass"}

    def test_pre_adr_0035_spec_without_vaporview_warns(self, tmp_path, monkeypatch):
        # A spec seeded before the Waveform Viewer landed installs no VaporView
        # and pins no WCP settings; an image rebuild never fixes that, so the
        # agent's scoped `bwave gui` fails in every session — surface it.
        # Exact shape hit live on a real project 2026-07-14.
        from booley.harness import devcontainer as dc

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
        from booley.harness import devcontainer as dc

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


# ---------------------------------------------------------------------------
# .core setup-gap checks added for the PICORV32 onboarding pass: cheap-pass
# CAPI2 schema, yosys-arch hint, untracked-file warning, backend-but-no-Target
# near-miss, and the multi-line output excerpt.
# ---------------------------------------------------------------------------


class _Collector:
    """Records doctor check callbacks so a helper can be asserted on directly."""

    def __init__(self) -> None:
        self.passed: list[str] = []
        self.warned: list[str] = []
        self.noted: list[str] = []
        self.skipped: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def _pass(self, msg: str) -> None:
        self.passed.append(msg)

    def _warn(self, msg: str, fix: str = "") -> None:
        self.warned.append(msg)

    def _note(self, msg: str) -> None:
        self.noted.append(msg)

    def _skip(self, msg: str) -> None:
        self.skipped.append(msg)

    def _fail(self, msg: str, fix: str = "") -> None:
        self.failed.append((msg, fix))


_GOOD_CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}
targets:
  lint: {flow: lint, flow_options: {tool: verilator}, filesets: [rtl], toplevel: dut}
"""


def _write_core(root: Path, text: str, name: str = "design.core") -> Path:
    core = root / name
    core.write_text(text, encoding="utf-8")
    return core


def _verilator_sim_ref(core, name="sim_x", vlnv="::demo:0"):
    from booley import fusesoc_registry as fr

    return {
        name: fr.TargetRef(name=name, vlnv=vlnv, core_file=core, eda_tool="verilator", flow="sim")
    }


def test_verilator_sim_with_auto_main_cannot_trace_warns(tmp_path):
    # The classic trap: `--main` = Verilator's auto main = no tracer.
    core = _write_core(
        tmp_path,
        """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}
targets:
  sim_x:
    flow: sim
    flow_options: {tool: verilator, verilator_options: [--main, --timing, -Wno-fatal]}
    filesets: [rtl]
    toplevel: dut_tb
""",
    )
    c = _Collector()
    doctor._check_sim_traceable(tmp_path, _verilator_sim_ref(core), c._pass, c._warn)
    assert any("cannot trace" in m and "sim_x" in m for m in c.warned)
    assert not c.passed


def test_verilator_sim_with_binary_flag_cannot_trace_warns(tmp_path):
    # `--binary` is `--main --exe --build` — same tracerless auto main.
    core = _write_core(
        tmp_path,
        """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}
targets:
  sim_x:
    flow: sim
    flow_options: {tool: verilator, verilator_options: [--binary, --timing]}
    filesets: [rtl]
    toplevel: dut_tb
""",
    )
    c = _Collector()
    doctor._check_sim_traceable(tmp_path, _verilator_sim_ref(core), c._pass, c._warn)
    assert any("cannot trace" in m for m in c.warned)


def test_verilator_sim_with_exe_main_is_trace_capable(tmp_path):
    # No --main/--binary → the Target owns a cppSource --exe main → traceable.
    core = _write_core(
        tmp_path,
        """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}
  tb_cpp: {files: [sim/main.cpp: {file_type: cppSource}], tags: [tb]}
targets:
  sim_x:
    flow: sim
    flow_options: {tool: verilator, verilator_options: [--timing, -Wno-fatal]}
    filesets: [rtl, tb_cpp]
    toplevel: dut_tb
""",
    )
    c = _Collector()
    doctor._check_sim_traceable(tmp_path, _verilator_sim_ref(core), c._pass, c._warn)
    assert not c.warned
    assert any("trace-capable" in m for m in c.passed)


def test_sim_traceable_check_ignores_non_verilator_and_non_sim(tmp_path):
    # Icarus/xcelium/vcs self-heal (overlay supplies the dump module); lint
    # Targets have no TB. Neither should be flagged.
    from booley import fusesoc_registry as fr

    core = _write_core(
        tmp_path,
        """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}
targets:
  sim_ic: {flow: sim, flow_options: {tool: icarus}, filesets: [rtl], toplevel: dut_tb}
  lint: {flow: lint, flow_options: {tool: verilator, verilator_options: [--main]}, filesets: [rtl], toplevel: dut}
""",
    )
    refs = {
        "sim_ic": fr.TargetRef(
            name="sim_ic", vlnv="::demo:0", core_file=core, eda_tool="icarus", flow="sim"
        ),
        "lint": fr.TargetRef(
            name="lint", vlnv="::demo:0", core_file=core, eda_tool="verilator", flow="lint"
        ),
    }
    c = _Collector()
    doctor._check_sim_traceable(tmp_path, refs, c._pass, c._warn)
    assert not c.warned
    assert not c.passed  # no Verilator *sim* Target at all → silent


class TestIcarusSvLanguageMode:
    """Icarus + .sv sources without -g2012: iverilog defaults to Verilog-2005,
    so every compile dies on the first `logic`/`always_ff` with a syntax error
    that points at healthy RTL. FAIL for sim Targets (the verdict source is
    guaranteed dead), WARN for lint/elaborate shapes (advisory gates)."""

    _SV_CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {{files: [rtl/dut.sv: {{file_type: systemVerilogSource}}]}}
  tb: {{files: [tb/tb_dut.sv: {{file_type: systemVerilogSource}}], tags: [tb]}}
targets:
  {name}:
    flow: {flow}
    flow_options: {flow_options}
    filesets: [rtl, tb]
    toplevel: tb_dut
"""

    def _refs(self, core, name, flow):
        from booley import fusesoc_registry as fr

        return {
            name: fr.TargetRef(
                name=name, vlnv="::demo:0", core_file=core, eda_tool="icarus", flow=flow
            )
        }

    def test_sim_target_missing_g2012_fails(self, tmp_path):
        core = _write_core(
            tmp_path,
            self._SV_CORE.format(name="sim_x", flow="sim", flow_options="{tool: icarus}"),
        )
        c = _Collector()
        doctor._check_icarus_sv_language_mode(
            tmp_path, self._refs(core, "sim_x", "sim"), c._pass, c._warn, c._fail
        )
        assert len(c.failed) == 1
        msg, fix = c.failed[0]
        assert "sim_x" in msg and "-g2012" in msg
        assert "-g2012" in fix and core.name in fix  # actionable: names the file
        assert not c.warned

    def test_lint_target_missing_g2012_warns_not_fails(self, tmp_path):
        core = _write_core(
            tmp_path,
            self._SV_CORE.format(name="lint_x", flow="lint", flow_options="{tool: icarus}"),
        )
        c = _Collector()
        doctor._check_icarus_sv_language_mode(
            tmp_path, self._refs(core, "lint_x", "lint"), c._pass, c._warn, c._fail
        )
        assert not c.failed
        assert any("lint_x" in m and "-g2012" in m for m in c.warned)

    def test_unselected_target_missing_g2012_is_a_note(self, tmp_path):
        core = _write_core(
            tmp_path,
            self._SV_CORE.format(name="lint_x", flow="lint", flow_options="{tool: icarus}"),
        )
        c = _Collector()
        notes = []

        doctor._check_icarus_sv_language_mode(
            tmp_path,
            self._refs(core, "lint_x", "lint"),
            c._pass,
            c._warn,
            c._fail,
            _note=notes.append,
            selected_targets={"::other:0#lint_other"},
        )

        assert not c.failed and not c.warned
        assert any("not selected" in msg and "lint_x" in msg for msg in notes)

    def test_sim_target_with_g2012_passes(self, tmp_path):
        core = _write_core(
            tmp_path,
            self._SV_CORE.format(
                name="sim_x",
                flow="sim",
                flow_options="{tool: icarus, iverilog_options: [-g2012]}",
            ),
        )
        c = _Collector()
        doctor._check_icarus_sv_language_mode(
            tmp_path, self._refs(core, "sim_x", "sim"), c._pass, c._warn, c._fail
        )
        assert not c.failed and not c.warned
        assert any("SV language flag" in m for m in c.passed)

    def test_older_sv_spellings_accepted(self, tmp_path):
        # -g2005-sv / -g2009 also enable SV — a working .core is not nagged
        # into churn just because it predates the -g2012 recommendation.
        core = _write_core(
            tmp_path,
            self._SV_CORE.format(
                name="sim_x",
                flow="sim",
                flow_options="{tool: icarus, iverilog_options: [-g2005-sv]}",
            ),
        )
        c = _Collector()
        doctor._check_icarus_sv_language_mode(
            tmp_path, self._refs(core, "sim_x", "sim"), c._pass, c._warn, c._fail
        )
        assert not c.failed and not c.warned

    def test_plain_verilog_and_non_icarus_targets_are_silent(self, tmp_path):
        from booley import fusesoc_registry as fr

        core = _write_core(
            tmp_path,
            """\
CAPI=2:
name: ::demo:0
filesets:
  rtl_v: {files: [rtl/dut.v: {file_type: verilogSource}]}
  rtl_sv: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}
targets:
  sim_v: {flow: sim, flow_options: {tool: icarus}, filesets: [rtl_v], toplevel: dut}
  sim_ver: {flow: sim, flow_options: {tool: verilator}, filesets: [rtl_sv], toplevel: dut}
""",
        )
        refs = {
            "sim_v": fr.TargetRef(
                name="sim_v", vlnv="::demo:0", core_file=core, eda_tool="icarus", flow="sim"
            ),
            "sim_ver": fr.TargetRef(
                name="sim_ver", vlnv="::demo:0", core_file=core, eda_tool="verilator", flow="sim"
            ),
        }
        c = _Collector()
        doctor._check_icarus_sv_language_mode(tmp_path, refs, c._pass, c._warn, c._fail)
        # .v-only icarus target: the default generation is correct; verilator
        # targets are out of scope entirely.
        assert not c.failed and not c.warned and not c.passed


class TestToplevelInterfacePorts:
    """F-7: a toplevel with SV interface ports cannot elaborate standalone, so
    lint and synth die on an interface parameter mismatch that reads like a bug
    in the IP. Statically detectable from the port list."""

    _IFACE_DUT = """\
`default_nettype none
module eth_mac #(
    parameter DATA_W = 64
)
(
    input  wire logic  clk,
    input  wire logic  rst,
    taxi_axis_if.snk   s_axis_tx,
    taxi_axis_if.src   m_axis_rx
);
  localparam KEEP_W = s_axis_tx.KEEP_W;
endmodule
"""

    _FLAT_DUT = """\
module eth_mac_wrap (
    input  wire logic        clk,
    input  wire logic [63:0] tx_data,
    output wire logic [63:0] rx_data
);
endmodule
"""

    def _project(self, tmp_path: Path, rtl: str, toplevel: str, flow: str = "lint"):
        (tmp_path / "rtl").mkdir(exist_ok=True)
        (tmp_path / "rtl" / "dut.sv").write_text(rtl, encoding="utf-8")
        core = _write_core(
            tmp_path,
            f"""\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {{files: [rtl/dut.sv: {{file_type: systemVerilogSource}}]}}
targets:
  {flow}: {{flow: {flow}, flow_options: {{tool: verilator}}, filesets: [rtl], \
toplevel: {toplevel}}}
""",
        )
        from booley import fusesoc_registry as fr

        refs = {
            flow: fr.TargetRef(
                name=flow, vlnv="::demo:0", core_file=core, eda_tool="verilator", flow=flow
            )
        }
        return refs

    def test_interface_ports_on_lint_toplevel_warn(self, tmp_path: Path):
        refs = self._project(tmp_path, self._IFACE_DUT, "eth_mac")
        rec = _Rec()
        doctor._check_toplevel_interface_ports(tmp_path, refs, rec.p, rec.w)
        warns = [m for lvl, m in rec.events if lvl == "warn"]
        assert len(warns) == 1
        # Names the offending ports, and says what to do about it.
        assert "taxi_axis_if.snk s_axis_tx" in warns[0]
        assert "cannot be elaborated standalone" in warns[0]

    def test_flat_port_toplevel_passes(self, tmp_path: Path):
        refs = self._project(tmp_path, self._FLAT_DUT, "eth_mac_wrap")
        rec = _Rec()
        doctor._check_toplevel_interface_ports(tmp_path, refs, rec.p, rec.w)
        assert rec.kinds() == {"pass"}

    def test_sim_target_is_exempt(self, tmp_path: Path):
        """A cocotb/HDL TB instantiates the interfaces itself — not a finding."""
        refs = self._project(tmp_path, self._IFACE_DUT, "eth_mac", flow="sim")
        rec = _Rec()
        doctor._check_toplevel_interface_ports(tmp_path, refs, rec.p, rec.w)
        assert rec.events == []

    def test_commented_out_port_is_not_a_finding(self, tmp_path: Path):
        rtl = """\
module eth_mac_wrap (
    input wire logic clk
    // taxi_axis_if.snk s_axis_tx,   <- supplied by the wrapper below
);
endmodule
"""
        refs = self._project(tmp_path, rtl, "eth_mac_wrap")
        rec = _Rec()
        doctor._check_toplevel_interface_ports(tmp_path, refs, rec.p, rec.w)
        assert rec.kinds() == {"pass"}

    def test_plain_typed_port_is_not_mistaken_for_an_interface(self, tmp_path: Path):
        """`my_cfg_t cfg` is an ordinary user-typed port, not an interface —
        crying wolf on every typed port would be worse than no check at all."""
        rtl = """\
module eth_mac_wrap (
    input  wire logic clk,
    input  my_cfg_t   cfg,
    output wire logic done
);
endmodule
"""
        refs = self._project(tmp_path, rtl, "eth_mac_wrap")
        rec = _Rec()
        doctor._check_toplevel_interface_ports(tmp_path, refs, rec.p, rec.w)
        assert rec.kinds() == {"pass"}

    def test_missing_toplevel_source_says_nothing(self, tmp_path: Path):
        """The toplevel isn't among the Target's files — nothing to assert."""
        refs = self._project(tmp_path, self._FLAT_DUT, "some_other_module")
        rec = _Rec()
        doctor._check_toplevel_interface_ports(tmp_path, refs, rec.p, rec.w)
        assert rec.events == []


_BAD_DEPEND_CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  tb:
    files: [tb/tb.sv: {file_type: systemVerilogSource}]
    depend: not_a_list
targets:
  sim: {flow: sim, filesets: [tb]}
"""


def _schema_audit(tmp_path, booley_toml=None) -> doctor.ProjectAudit:
    return doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=tmp_path / ".booley_project",
        booley_toml=booley_toml or {},
        configs_toml={},
        first_target="",
    )


def test_check_core_schema_flags_scalar_depend(tmp_path):
    """A schema violation in a core the project drives is a FAIL."""
    _write_core(tmp_path, _BAD_DEPEND_CORE)
    project = _schema_audit(tmp_path, {"flows": {"sim": {"default_target": "sim"}}})
    c = _Collector()
    doctor._check_core_schema(project, tmp_path, c._pass, c._warn, c._fail)
    assert any("depend must be array" in msg for msg, _ in c.failed)


def test_check_core_setup_hazards_fails_selected_provider(tmp_path):
    core = _write_core(
        tmp_path,
        """\
CAPI=2:
name: ::demo:0
provider: {name: github, user: acme, repo: demo}
targets:
  sim: {flow: sim, flow_options: {tool: verilator}}
""",
    )
    project = _schema_audit(tmp_path, {"flows": {"sim": {"default_target": "sim"}}})
    rec = _Rec()
    doctor._check_core_setup_hazards(project, tmp_path, rec.p, rec.f, _note=rec.n)
    assert len(rec.fails()) == 1
    assert core.name in rec.fails()[0]
    assert "remote fetch" in rec.fails()[0]


def test_check_core_setup_hazards_notes_unselected_provider(tmp_path):
    _write_core(
        tmp_path,
        """\
CAPI=2:
name: ::demo:0
provider: {name: github, user: acme, repo: demo}
targets:
  sim: {flow: sim, flow_options: {tool: verilator}}
""",
    )
    rec = _Rec()
    doctor._check_core_setup_hazards(_schema_audit(tmp_path), tmp_path, rec.p, rec.f, _note=rec.n)
    assert not rec.fails()
    assert any(
        "no configured Target selects it" in msg for level, msg in rec.events if level == "note"
    )


def test_check_core_setup_hazards_fails_provider_in_dependency_closure(tmp_path):
    _write_core(
        tmp_path,
        """\
CAPI=2:
name: acme:demo:top:0
filesets:
  rtl:
    depend: [acme:demo:dep]
targets:
  sim: {flow: sim, flow_options: {tool: verilator}, filesets: [rtl]}
""",
        name="top.core",
    )
    dep = _write_core(
        tmp_path,
        """\
CAPI=2:
name: acme:demo:dep:0
provider: {name: github, user: acme, repo: dep}
targets:
  default: {}
""",
        name="dep.core",
    )
    project = _schema_audit(tmp_path, {"flows": {"sim": {"default_target": "sim"}}})
    rec = _Rec()
    doctor._check_core_setup_hazards(project, tmp_path, rec.p, rec.f, _note=rec.n)
    assert len(rec.fails()) == 1
    assert dep.name in rec.fails()[0]


def test_check_core_schema_vendored_core_is_a_note(tmp_path):
    """The same violation in a core no configured Target selects is a NOTE:
    FuseSoC skips the core at resolve, the configured flows are unaffected, and
    'fix the .core' is unfollowable for a pristine upstream checkout (the
    YosysHQ picorv32.core ships a bare ``depend:``)."""
    _write_core(tmp_path, _BAD_DEPEND_CORE)
    project = _schema_audit(tmp_path)  # no [flows.*].default_target selects ::demo
    c = _Collector()
    doctor._check_core_schema(project, tmp_path, c._pass, c._warn, c._fail, _note=c._note)
    assert not c.failed
    assert not c.warned
    assert any("depend must be array" in m and "vendored" in m for m in c.noted)


def test_check_core_schema_state_zone_core_fails_even_unconfigured(tmp_path):
    """State-zone cores (ADR 0036) are always Booley-authored — a schema
    violation there is ours to fix regardless of configuration."""
    state_cores = tmp_path / ".booley_project" / "cores"
    state_cores.mkdir(parents=True)
    _write_core(state_cores, _BAD_DEPEND_CORE)
    project = _schema_audit(tmp_path)
    c = _Collector()
    doctor._check_core_schema(project, tmp_path, c._pass, c._warn, c._fail)
    assert any("depend must be array" in msg for msg, _ in c.failed)


def test_check_core_schema_passes_clean_core(tmp_path):
    _write_core(tmp_path, _GOOD_CORE)
    c = _Collector()
    doctor._check_core_schema(_schema_audit(tmp_path), tmp_path, c._pass, c._warn, c._fail)
    assert not c.failed
    assert any("schema valid" in m for m in c.passed)


def test_yosys_target_without_arch_warns(tmp_path):
    from booley import fusesoc_registry as fr

    core = _write_core(
        tmp_path,
        """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}
targets:
  synth: {flow: generic, flow_options: {tool: yosys}, filesets: [rtl], toplevel: dut}
""",
    )
    refs = {
        "synth": fr.TargetRef(
            name="synth", vlnv="::demo:0", core_file=core, eda_tool="yosys", flow="generic"
        )
    }
    c = _Collector()
    doctor._check_yosys_targets_have_arch(tmp_path, refs, c._pass, c._warn)
    assert any("flow_options.arch" in m and "requires" in m for m in c.warned)


def test_yosys_target_with_arch_passes(tmp_path):
    from booley import fusesoc_registry as fr

    core = _write_core(
        tmp_path,
        """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}
targets:
  synth: {flow: generic, flow_options: {tool: yosys, arch: xilinx}, filesets: [rtl], toplevel: dut}
""",
    )
    refs = {
        "synth": fr.TargetRef(
            name="synth", vlnv="::demo:0", core_file=core, eda_tool="yosys", flow="generic"
        )
    }
    c = _Collector()
    doctor._check_yosys_targets_have_arch(tmp_path, refs, c._pass, c._warn)
    assert not c.warned
    assert any("declares flow_options.arch" in m for m in c.passed)


def test_untracked_referenced_file_warns(tmp_path):
    # A real git repo: firmware.hex referenced by the .core but not tracked.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    (tmp_path / "firmware").mkdir()
    (tmp_path / "firmware" / "firmware.hex").write_text("00\n", encoding="utf-8")
    _write_core(
        tmp_path,
        """\
CAPI=2:
name: ::demo:0
filesets:
  rtl:
    files:
      - rtl/dut.sv: {file_type: systemVerilogSource}
      - firmware/firmware.hex: {file_type: user}
targets:
  sim: {flow: sim, filesets: [rtl]}
""",
    )
    subprocess.run(["git", "add", "rtl/dut.sv", "design.core"], cwd=tmp_path, check=True)
    c = _Collector()
    doctor._check_core_files_tracked(tmp_path, c._pass, c._warn)
    assert any("untracked" in m and "firmware.hex" in m for m in c.warned)


def test_all_referenced_files_tracked_passes(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    _write_core(tmp_path, _GOOD_CORE)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    c = _Collector()
    doctor._check_core_files_tracked(tmp_path, c._pass, c._warn)
    assert not c.warned


def test_submodule_files_count_as_tracked(tmp_path):
    """A .core referencing a vendored submodule's file is NOT the untracked trap.

    `git ls-files` stops at the gitlink, so every file inside the submodule read
    as untracked and the warning's own claim ("a fresh clone will lack them")
    was false: `git submodule update` restores exactly these.
    """
    dep = tmp_path / "dep"
    dep.mkdir()
    (dep / "vendor.sv").write_text("module vendor; endmodule\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=dep, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dep, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "dep"],
        cwd=dep,
        check=True,
    )

    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(dep), "lib/dep"],
        cwd=proj,
        check=True,
    )
    _write_core(
        proj,
        """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [lib/dep/vendor.sv: {file_type: systemVerilogSource}]}
targets:
  sim: {flow: sim, filesets: [rtl]}
""",
    )
    subprocess.run(["git", "add", "design.core"], cwd=proj, check=True)

    c = _Collector()
    doctor._check_core_files_tracked(proj, c._pass, c._warn)
    assert not c.warned
    assert c.passed

    # The real trap still fires inside a submodule: a file the submodule's own
    # repo does not track genuinely vanishes on a fresh `submodule update`.
    (proj / "lib" / "dep" / "generated.hex").write_text("00\n", encoding="utf-8")
    _write_core(
        proj,
        """\
CAPI=2:
name: ::demo:0
filesets:
  rtl:
    files:
      - lib/dep/vendor.sv: {file_type: systemVerilogSource}
      - lib/dep/generated.hex: {file_type: user}
targets:
  sim: {flow: sim, filesets: [rtl]}
""",
    )
    c2 = _Collector()
    doctor._check_core_files_tracked(proj, c2._pass, c2._warn)
    assert any("generated.hex" in m and "untracked" in m for m in c2.warned)


def test_stealth_core_files_judged_against_state_repo(tmp_path):
    """ADR 0036: a stealth core's files are tracked by the project dir's OWN
    repo — asking the host repo about them is guaranteed noise. Reach-through
    symlinks count as tracked for the content behind them (a clone ships the
    link, and the link's target is host-tracked upstream source)."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    state_cores = tmp_path / ".booley_project" / "cores"
    state_cores.mkdir(parents=True)
    (state_cores / "fw").mkdir()
    (state_cores / "fw" / "boot.hex").write_text("00\n", encoding="utf-8")
    (state_cores / "rtl").symlink_to("../../rtl")  # reach-through to host RTL
    _write_core(
        state_cores,
        """\
CAPI=2:
name: ::demo-booley:0
filesets:
  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}
  fw:
    files:
      - fw/boot.hex: {file_type: user, copyto: boot.hex}
targets:
  sim: {flow: sim, filesets: [rtl, fw]}
""",
    )
    state_dir = tmp_path / ".booley_project"
    subprocess.run(["git", "init", "-q"], cwd=state_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=state_dir, check=True)

    c = _Collector()
    doctor._check_core_files_tracked(tmp_path, c._pass, c._warn)
    assert not c.warned

    # The vendored-data trap still fires inside the state zone: a fw image the
    # nested repo does NOT track would vanish from a fresh clone.
    subprocess.run(["git", "rm", "--cached", "-q", "cores/fw/boot.hex"], cwd=state_dir, check=True)
    c2 = _Collector()
    doctor._check_core_files_tracked(tmp_path, c2._pass, c2._warn)
    assert any("boot.hex" in m and "untracked" in m for m in c2.warned)


def _readmem_repo(tmp_path) -> None:
    """A repo whose TB boots via $readmemh from a memory image NOT in any .core."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tb").mkdir()
    (tmp_path / "tb" / "tb.sv").write_text(
        'module tb; reg [7:0] mem[0:255];\n  initial $readmemh("fw/boot.vmem", mem);\nendmodule\n',
        encoding="utf-8",
    )
    (tmp_path / "fw").mkdir()
    (tmp_path / "fw" / "boot.vmem").write_text("00\n", encoding="utf-8")
    _write_core(
        tmp_path,
        """\
CAPI=2:
name: ::demo:0
filesets:
  tb:
    files:
      - tb/tb.sv: {file_type: systemVerilogSource}
targets:
  sim: {flow: sim, filesets: [tb]}
""",
    )


def test_readmemh_untracked_image_warns(tmp_path):
    # SETUP-12: the memory image is $readmemh'd but listed in no .core fileset,
    # and it is untracked (gitignored-style). _check_core_files_tracked can't see
    # it; the readmemh check must.
    _readmem_repo(tmp_path)
    subprocess.run(["git", "add", "tb/tb.sv", "design.core"], cwd=tmp_path, check=True)
    c = _Collector()
    doctor._check_readmemh_targets_tracked(tmp_path, c._pass, c._warn)
    assert any("$readmemh" in m and "boot.vmem" in m and "untracked" in m for m in c.warned)


def test_readmemh_tracked_image_passes(tmp_path):
    _readmem_repo(tmp_path)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    c = _Collector()
    doctor._check_readmemh_targets_tracked(tmp_path, c._pass, c._warn)
    assert not c.warned
    assert any("$readmemh-referenced image" in m for m in c.passed)


def test_readmemh_constructed_path_ignored(tmp_path):
    # A path built from a plusarg / format specifier can't be resolved statically.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tb").mkdir()
    (tmp_path / "tb" / "tb.sv").write_text(
        "module tb; string f;\n"
        '  initial begin $value$plusargs("mem=%s", f); $readmemh(f, mem); end\n'
        '  initial $readmemh({basedir, "/x.vmem"}, mem2);\n'
        "endmodule\n",
        encoding="utf-8",
    )
    _write_core(
        tmp_path,
        """\
CAPI=2:
name: ::demo:0
filesets:
  tb: {files: [tb/tb.sv: {file_type: systemVerilogSource}]}
targets:
  sim: {flow: sim, filesets: [tb]}
""",
    )
    # Neither call is a resolvable literal → no targets, no warning.
    assert doctor._readmem_literal_targets(tmp_path) == []
    c = _Collector()
    doctor._check_readmemh_targets_tracked(tmp_path, c._pass, c._warn)
    assert not c.warned and not c.passed


def _artifact_repo(tmp_path, *, with_source: bool, vendored: bool = False) -> None:
    """A git repo whose .core references a tracked firmware.hex; source optional.

    ``vendored`` tags the hex ``tags: [vendored]`` in the .core (the explicit
    "upstream blob, no rebuildable source" opt-out of the heuristic — the
    CAPI2-valid marker, not the fusesoc-rejected bare ``vendored: true`` key).
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    (tmp_path / "firmware").mkdir()
    (tmp_path / "firmware" / "firmware.hex").write_text("00\n", encoding="utf-8")
    if with_source:
        # sibling C + assembly source → the hex is a build output, not a blob.
        (tmp_path / "firmware" / "hello.c").write_text("int main(){}\n", encoding="utf-8")
        (tmp_path / "firmware" / "start.S").write_text(".global _start\n", encoding="utf-8")
    hex_attrs = "{file_type: user, tags: [vendored]}" if vendored else "{file_type: user}"
    _write_core(
        tmp_path,
        f"""\
CAPI=2:
name: ::demo:0
filesets:
  rtl:
    files:
      - rtl/dut.sv: {{file_type: systemVerilogSource}}
      - firmware/firmware.hex: {hex_attrs}
targets:
  sim: {{flow: sim, filesets: [rtl]}}
""",
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)


def test_committed_build_artifact_with_sibling_source_warns(tmp_path):
    _artifact_repo(tmp_path, with_source=True)
    c = _Collector()
    doctor._check_committed_build_artifacts(tmp_path, c._pass, c._warn)
    assert any("built from in-repo source" in m and "firmware.hex" in m for m in c.warned)


def test_committed_opaque_blob_without_source_stays_quiet(tmp_path):
    # A vendored hex with no sibling source may be the only option — don't nag.
    _artifact_repo(tmp_path, with_source=False)
    c = _Collector()
    doctor._check_committed_build_artifacts(tmp_path, c._pass, c._warn)
    assert not c.warned


def test_vendored_annotated_artifact_stays_quiet_despite_sibling_source(tmp_path):
    # `tags: [vendored]` is the maintainer asserting the blob is upstream-shipped
    # with no rebuildable in-repo source — exempt even with sibling C/asm around.
    _artifact_repo(tmp_path, with_source=True, vendored=True)
    c = _Collector()
    doctor._check_committed_build_artifacts(tmp_path, c._pass, c._warn)
    assert not c.warned


def test_untracked_build_artifact_not_flagged_as_committed(tmp_path):
    # The artifact exists with sibling source but is NOT git-tracked → nothing to
    # nag about (the untracked trap is _check_core_files_tracked's job, not this).
    _artifact_repo(tmp_path, with_source=True)
    subprocess.run(
        ["git", "rm", "-q", "--cached", "firmware/firmware.hex"], cwd=tmp_path, check=True
    )
    c = _Collector()
    doctor._check_committed_build_artifacts(tmp_path, c._pass, c._warn)
    assert not c.warned


def test_repo_footprint_scaffolding_warns(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "firmware").mkdir()
    # A Booley-branded note committed at depth — must be caught (glob crosses `/`).
    (tmp_path / "firmware" / "README.booley.md").write_text("notes\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    c = _Collector()
    doctor._check_repo_footprint(tmp_path, c._pass, c._warn)
    assert any("scaffolding" in m and "README.booley.md" in m for m in c.warned)


def test_repo_footprint_clean_passes(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    c = _Collector()
    doctor._check_repo_footprint(tmp_path, c._pass, c._warn)
    assert not c.warned
    assert any("no Booley scaffolding" in m for m in c.passed)


def test_no_target_match_detail_names_near_miss(tmp_path, monkeypatch):
    from booley import fusesoc_registry as fr

    project = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=tmp_path / ".booley_project",
        booley_toml={},
        configs_toml={},
        first_target="",
    )
    refs = {
        "foo_sim": fr.TargetRef(
            name="foo_sim",
            vlnv="::d:0",
            core_file=tmp_path / "d.core",
            eda_tool="verilator",
            flow="sim",
        )
    }
    monkeypatch.setattr(fr, "enumerate_targets", lambda _root: refs)
    detail = doctor._no_target_match_detail(project, "synth")
    # ADR 0030: the gap is a missing [flows.<flow>].default_target; name the authored
    # Targets (with Flow/EDA tool) so the user can copy one into the config.
    assert "foo_sim" in detail
    assert "[flows.synth].default_target" in detail
    assert "can never run" in detail


def test_print_text_excerpt_keeps_reason_line(capsys):
    # The fusesoc parse-error reason lands on the line AFTER "Ignoring file X:";
    # a first-line-only excerpt dropped it — the excerpt must retain it.
    text = (
        "INFO: setting up\n"
        "WARNING: Parse error. Ignoring file /work/picorv32.core:\n"
        "data.filesets.tb.depend must be array\n"
    )
    doctor._print_text_excerpt(text)
    out = capsys.readouterr().out
    assert "Ignoring file" in out
    assert "must be array" in out


def _derived_project_audit(root: Path) -> doctor.ProjectAudit:
    """Minimal ProjectAudit whose project_root yields a known generated image name."""
    return doctor.ProjectAudit(
        project_root=root,
        project_dir=root / ".booley_project",
        booley_toml={},
        configs_toml={},
        first_target="",
    )


def test_derived_image_freshness_warns_when_derived_predates_base(tmp_path, monkeypatch):
    # A project image built FROM an older base freezes stale Booley layers; a
    # base rebuild does not propagate. Doctor must flag that drift.
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    image = doctor.pi.project_image_name(proj)  # "myproj-booley-sandbox"
    created = {doctor.DOCKER_IMAGE: 2000.0, image: 1000.0}  # derived older than base
    monkeypatch.setattr(doctor, "_image_created_epoch", lambda _exe, img: created.get(img))

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_derived_image_freshness(project, "docker", image, passes.append, warns.append)
    assert warns and "predates its base" in warns[0]
    assert not passes


def test_derived_image_freshness_passes_when_derived_newer(tmp_path, monkeypatch):
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    image = doctor.pi.project_image_name(proj)
    created = {doctor.DOCKER_IMAGE: 1000.0, image: 2000.0}  # derived newer than base
    monkeypatch.setattr(doctor, "_image_created_epoch", lambda _exe, img: created.get(img))

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_derived_image_freshness(project, "docker", image, passes.append, warns.append)
    assert passes and "newer than its base" in passes[0]
    assert not warns


def test_derived_image_freshness_skips_user_managed_image(tmp_path, monkeypatch):
    # An image name that isn't the auto-generated one is user-managed (may not
    # even derive from the base): doctor must stay silent, not misjudge it.
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    monkeypatch.setattr(
        doctor,
        "_image_created_epoch",
        lambda _exe, _img: (_ for _ in ()).throw(AssertionError("must not inspect")),
    )

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_derived_image_freshness(
        project,
        "docker",
        "custom/user-image:latest",
        passes.append,
        warns.append,
    )
    assert not warns and not passes


def test_derived_image_freshness_silent_when_base_absent(tmp_path, monkeypatch):
    # Base image missing (or unreadable): nothing to compare, degrade to silence
    # rather than a false stale verdict.
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    image = doctor.pi.project_image_name(proj)
    created = {image: 1000.0}  # base absent
    monkeypatch.setattr(doctor, "_image_created_epoch", lambda _exe, img: created.get(img))

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_derived_image_freshness(project, "docker", image, passes.append, warns.append)
    assert not warns and not passes


# ---------------------------------------------------------------------------
# Custom [sandbox].image freshness vs .booley_project/docker/Dockerfile (item 5)
# ---------------------------------------------------------------------------


def _write_project_dockerfile(proj: Path, mtime: float) -> Path:
    docker_dir = proj / ".booley_project" / "docker"
    docker_dir.mkdir(parents=True, exist_ok=True)
    dockerfile = docker_dir / "Dockerfile"
    dockerfile.write_text("FROM booley-sandbox\n", encoding="utf-8")
    os.utime(dockerfile, (mtime, mtime))
    return dockerfile


def test_custom_image_freshness_warns_when_image_predates_dockerfile(tmp_path, monkeypatch):
    # A hand-named [sandbox].image built from an edited-but-not-rebuilt Dockerfile
    # runs the old toolchain; neither sibling freshness check covers it.
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    _write_project_dockerfile(proj, mtime=2000.0)
    project = _derived_project_audit(proj)
    monkeypatch.setattr(doctor, "_image_created_epoch", lambda _exe, _img: 1000.0)

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_custom_image_freshness(
        project,
        "docker",
        "openc910-booley-sandbox",
        passes.append,
        warns.append,
    )
    assert warns and "predates" in warns[0]
    assert not passes


def test_custom_image_freshness_passes_when_image_newer(tmp_path, monkeypatch):
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    _write_project_dockerfile(proj, mtime=1000.0)
    project = _derived_project_audit(proj)
    monkeypatch.setattr(doctor, "_image_created_epoch", lambda _exe, _img: 2000.0)

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_custom_image_freshness(
        project,
        "docker",
        "openc910-booley-sandbox",
        passes.append,
        warns.append,
    )
    assert passes and "newer than its Dockerfile" in passes[0]
    assert not warns


def test_custom_image_freshness_skips_base_and_generated(tmp_path, monkeypatch):
    # The base and auto-generated derived images are covered by sibling checks —
    # this one must stay out of their lane (and never even inspect them).
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    _write_project_dockerfile(proj, mtime=9999.0)
    project = _derived_project_audit(proj)
    monkeypatch.setattr(
        doctor,
        "_image_created_epoch",
        lambda _exe, _img: (_ for _ in ()).throw(AssertionError("must not inspect")),
    )

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_custom_image_freshness(
        project,
        "docker",
        doctor.DOCKER_IMAGE,
        passes.append,
        warns.append,
    )
    doctor._check_custom_image_freshness(
        project,
        "docker",
        doctor.pi.project_image_name(proj),
        passes.append,
        warns.append,
    )
    assert not warns and not passes


def test_custom_image_freshness_silent_without_project_dockerfile(tmp_path, monkeypatch):
    # A custom image built from an external Dockerfile (none under .booley_project)
    # has nothing to compare against: degrade to silence, not a false verdict.
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    monkeypatch.setattr(doctor, "_image_created_epoch", lambda _exe, _img: 1000.0)

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_custom_image_freshness(
        project,
        "docker",
        "openc910-booley-sandbox",
        passes.append,
        warns.append,
    )
    assert not warns and not passes


# ---------------------------------------------------------------------------
# Large-design advisory for --deep budgets (item 3 / F5)
# ---------------------------------------------------------------------------


def test_design_size_notes_a_large_design(tmp_path):
    """A NOTE, not a WARN: design scale is a fact with nothing to fix."""
    proj = tmp_path / "big"
    rtl = proj / "rtl"
    rtl.mkdir(parents=True)
    # Cross the file-count threshold with small files.
    for i in range(doctor._LARGE_DESIGN_FILES + 5):
        (rtl / f"mod_{i}.sv").write_text("module m; endmodule\n", encoding="utf-8")
    project = _derived_project_audit(proj)

    notes: list[str] = []
    passes: list[str] = []
    doctor._check_design_size(project, passes.append, notes.append)
    assert notes and "large design" in notes[0] and "--deep" in notes[0]
    assert not passes


def test_large_design_does_not_count_as_a_doctor_warning(tmp_path, capsys):
    """The tier is what the user reads: a big design must not inflate the
    warning count that `finish()` prints (that count is the actionable one)."""
    proj = tmp_path / "big"
    rtl = proj / "rtl"
    rtl.mkdir(parents=True)
    for i in range(doctor._LARGE_DESIGN_FILES + 5):
        (rtl / f"mod_{i}.sv").write_text("module m; endmodule\n", encoding="utf-8")

    reporter = doctor._Reporter.create()
    doctor._check_design_size(_derived_project_audit(proj), reporter.pass_, reporter.note_)
    assert reporter.counts["note"] == 1
    assert reporter.counts["warn"] == 0
    assert "NOTE  large design" in capsys.readouterr().out


def test_design_size_passes_for_small_design_and_skips_pruned_dirs(tmp_path):
    proj = tmp_path / "small"
    (proj / "rtl").mkdir(parents=True)
    (proj / "rtl" / "core.sv").write_text("module m; endmodule\n" * 10, encoding="utf-8")
    # HDL under a pruned dir (e.g. vendored build output) must NOT be counted.
    vendored = proj / "build" / "gen"
    vendored.mkdir(parents=True)
    for i in range(doctor._LARGE_DESIGN_FILES + 50):
        (vendored / f"g_{i}.v").write_text("module g; endmodule\n", encoding="utf-8")
    project = _derived_project_audit(proj)

    notes: list[str] = []
    passes: list[str] = []
    doctor._check_design_size(project, passes.append, notes.append)
    assert passes and "design size" in passes[0]
    assert not notes
    # Exactly the one non-pruned HDL file was counted.
    files, _loc = doctor._design_size(proj)
    assert files == 1


def test_design_size_scopes_to_configured_target_in_large_monorepo(tmp_path):
    proj = tmp_path / "monorepo"
    rtl = proj / "rtl"
    rtl.mkdir(parents=True)
    (rtl / "selected.sv").write_text("module selected; endmodule\n", encoding="utf-8")
    unrelated = proj / "unrelated"
    unrelated.mkdir()
    for index in range(doctor._LARGE_DESIGN_FILES + 5):
        (unrelated / f"large_{index}.sv").write_text("module large; endmodule\n", encoding="utf-8")
    (proj / "design.core").write_text(
        """CAPI=2:
name: ::small:0
filesets:
  rtl:
    files: [rtl/selected.sv]
targets:
  sim_small:
    filesets: [rtl]
    toplevel: selected
""",
        encoding="utf-8",
    )
    project = replace(
        _derived_project_audit(proj),
        booley_toml={"flows": {"sim": {"default_target": "sim_small"}}},
    )

    passes: list[str] = []
    notes: list[str] = []
    doctor._check_design_size(project, passes.append, notes.append)

    assert not notes
    assert passes and "configured Target filesets: ~1 HDL files" in passes[0]


# ---------------------------------------------------------------------------
# Host-source-vs-image drift (SETUP-22) — _check_image_bakes_current_booley
# ---------------------------------------------------------------------------


def test_image_bakes_current_booley_warns_on_fingerprint_mismatch(tmp_path, monkeypatch):
    # Exact path: the build-fingerprint label differs from the checkout hash →
    # WARN without ever consulting the mtime heuristic.
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    image = doctor.pi.project_image_name(proj)
    monkeypatch.setattr(doctor, "source_fingerprint_mismatch", lambda _img: True)

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_image_bakes_current_booley(project, "docker", image, passes.append, warns.append)
    assert warns and "build-fingerprint label differs" in warns[0]
    assert not passes


def test_image_bakes_current_booley_passes_on_fingerprint_match(tmp_path, monkeypatch):
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    image = doctor.pi.project_image_name(proj)
    monkeypatch.setattr(doctor, "source_fingerprint_mismatch", lambda _img: False)
    # The mtime fallback must not run; poison it to prove that.
    monkeypatch.setattr(doctor, "_image_created_epoch", lambda _exe, _img: 1 / 0)

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_image_bakes_current_booley(project, "docker", image, passes.append, warns.append)
    assert passes and "exactly this checkout" in passes[0]
    assert not warns


def test_image_bakes_current_booley_warns_when_source_newer(tmp_path, monkeypatch):
    # Host Booley source edited after the image was built → sandbox runs stale
    # code (egress locked, can't pip-install the fix). Doctor must flag it.
    # Fingerprint gives no verdict here (e.g. unlabeled image) → mtime fallback.
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    image = doctor.pi.project_image_name(proj)
    monkeypatch.setattr(doctor, "source_fingerprint_mismatch", lambda _img: None)
    monkeypatch.setattr(doctor, "_image_created_epoch", lambda _exe, _img: 1000.0)
    monkeypatch.setattr(doctor, "_booley_package_dir", lambda: tmp_path / "src")
    monkeypatch.setattr(doctor, "_newest_source_mtime", lambda _p: 2000.0)  # newer than image

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_image_bakes_current_booley(project, "docker", image, passes.append, warns.append)
    assert warns and "stale code" in warns[0]
    assert not passes


def test_image_bakes_current_booley_passes_when_image_newer(tmp_path, monkeypatch):
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    image = doctor.pi.project_image_name(proj)
    monkeypatch.setattr(doctor, "source_fingerprint_mismatch", lambda _img: None)
    monkeypatch.setattr(doctor, "_image_created_epoch", lambda _exe, _img: 3000.0)
    monkeypatch.setattr(doctor, "_booley_package_dir", lambda: tmp_path / "src")
    monkeypatch.setattr(doctor, "_newest_source_mtime", lambda _p: 2000.0)  # older than image

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_image_bakes_current_booley(project, "docker", image, passes.append, warns.append)
    assert passes and "no older than the host" in passes[0]
    assert not warns


def test_image_bakes_current_booley_skips_user_managed(tmp_path, monkeypatch):
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    monkeypatch.setattr(
        doctor,
        "_image_created_epoch",
        lambda _exe, _img: (_ for _ in ()).throw(AssertionError("must not inspect")),
    )
    warns: list[str] = []
    passes: list[str] = []
    doctor._check_image_bakes_current_booley(
        project,
        "docker",
        "custom/user-image:latest",
        passes.append,
        warns.append,
    )
    assert not warns and not passes


def test_image_bakes_current_booley_silent_when_undeterminable(tmp_path, monkeypatch):
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    image = doctor.pi.project_image_name(proj)
    monkeypatch.setattr(doctor, "_image_created_epoch", lambda _exe, _img: None)  # image absent
    monkeypatch.setattr(doctor, "_booley_package_dir", lambda: tmp_path / "src")
    monkeypatch.setattr(doctor, "_newest_source_mtime", lambda _p: 2000.0)

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_image_bakes_current_booley(project, "docker", image, passes.append, warns.append)
    assert not warns and not passes


# ---------------------------------------------------------------------------
# Worktree prune guard (ADR 0028 Decision 10) — _check_worktree_prune_guard
# ---------------------------------------------------------------------------


def _git_init(root: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", str(root)],
        capture_output=True,
        check=True,
    )


class TestWorktreePruneGuard:
    """In-container worktrees hold container paths; a host `git gc` would
    prune their registrations unless gc.worktreePruneExpire=never is set."""

    def test_not_a_repo_skips(self, tmp_path: Path):
        rec = _Rec()
        doctor._check_worktree_prune_guard(tmp_path, rec.p, rec.s, rec.f)
        assert rec.kinds() == {"skip"}

    def test_unset_fails_with_exact_fix(self, tmp_path: Path):
        _git_init(tmp_path)
        rec = _Rec()
        fixes: list[str] = []

        def fail(msg: str, fix: str = "") -> None:
            rec.f(msg)
            fixes.append(fix)

        doctor._check_worktree_prune_guard(tmp_path, rec.p, rec.s, fail)
        assert rec.kinds() == {"fail"}
        assert "gc.worktreePruneExpire" in rec.fails()[0]
        assert fixes == [
            f"git -C {tmp_path} config gc.worktreePruneExpire never",
        ]

    def test_wrong_value_fails(self, tmp_path: Path):
        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "gc.worktreePruneExpire", "3.months.ago"],
            capture_output=True,
            check=True,
        )
        rec = _Rec()
        doctor._check_worktree_prune_guard(tmp_path, rec.p, rec.s, rec.f)
        assert rec.kinds() == {"fail"}
        assert "'3.months.ago'" in rec.fails()[0]

    def test_never_passes(self, tmp_path: Path):
        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "gc.worktreePruneExpire", "never"],
            capture_output=True,
            check=True,
        )
        rec = _Rec()
        doctor._check_worktree_prune_guard(tmp_path, rec.p, rec.s, rec.f)
        assert rec.kinds() == {"pass"}


class TestWorktreeCoreShadowGuard:
    """A stale worktree .core under .booley_project/ shadows the repo-root
    source in FuseSoC's --cores-root scan unless a FUSESOC_IGNORE marker is
    present (dropped by `booley init` / worktree_create.sh)."""

    def _warns(self, rec: _Rec) -> list[str]:
        return [m for lvl, m in rec.events if lvl == "warn"]

    def test_marker_present_passes(self, tmp_path: Path):
        (tmp_path / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
        rec = _Rec()
        doctor._check_worktree_core_shadow_guard(tmp_path, rec.p, rec.w)
        assert rec.kinds() == {"pass"}

    def test_missing_marker_no_worktree_soft_warns(self, tmp_path: Path):
        rec = _Rec()
        doctor._check_worktree_core_shadow_guard(tmp_path, rec.p, rec.w)
        assert rec.kinds() == {"warn"}
        assert "future ticket worktree" in self._warns(rec)[0]

    def test_missing_marker_with_stale_core_escalates(self, tmp_path: Path):
        wt = tmp_path / "worktrees" / "scalar_1bfe1733"
        wt.mkdir(parents=True)
        (wt / "design.core").write_text("CAPI=2:\nname: ::demo:0\n", encoding="utf-8")
        rec = _Rec()
        doctor._check_worktree_core_shadow_guard(tmp_path, rec.p, rec.w)
        assert rec.kinds() == {"warn"}
        assert "can shadow the repo-root source" in self._warns(rec)[0]

    def test_authored_stealth_cores_are_not_shadow_threats(self, tmp_path: Path):
        # ADR 0036: .booley_project/cores/ holds authored sources scanned
        # deliberately — with no other .core around they soft-warn, not escalate.
        stealth = tmp_path / "cores"
        stealth.mkdir()
        (stealth / "design.core").write_text("CAPI=2:\nname: ::demo:0\n", encoding="utf-8")
        rec = _Rec()
        doctor._check_worktree_core_shadow_guard(tmp_path, rec.p, rec.w)
        assert rec.kinds() == {"warn"}
        assert "future ticket worktree" in self._warns(rec)[0]


class TestStealthCoresCheck:
    """ADR 0036 contract: authored cores live in .booley_project/cores/ (and
    nowhere else in the state dir), and never share a VLNV with a repo core."""

    def _project(self, tmp_path: Path) -> tuple[Path, Path]:
        root = tmp_path / "repo"
        project_dir = root / ".booley_project"
        (project_dir / "cores").mkdir(parents=True)
        return root, project_dir

    def test_clean_layout_passes(self, tmp_path: Path):
        root, project_dir = self._project(tmp_path)
        (root / "repo.core").write_text("CAPI=2:\nname: ::repo:0\n", encoding="utf-8")
        (project_dir / "cores" / "s.core").write_text(
            "CAPI=2:\nname: ::stealth:0\n", encoding="utf-8"
        )
        rec = _Rec()
        doctor._check_stealth_cores(root, project_dir, rec.p, rec.f)
        assert rec.kinds() == {"pass"}

    def test_stranded_core_fails_with_move_hint(self, tmp_path: Path):
        # The original stealth-project layout: authored cores directly under
        # .booley_project/ — structurally skipped, so Targets silently vanish.
        root, project_dir = self._project(tmp_path)
        (project_dir / "stranded.core").write_text("CAPI=2:\nname: ::s:0\n", encoding="utf-8")
        rec = _Rec()
        doctor._check_stealth_cores(root, project_dir, rec.p, rec.f)
        assert "stranded" in rec.fails()[0]
        assert "stranded.core" in rec.fails()[0]

    def test_worktree_copies_are_not_stranded(self, tmp_path: Path):
        root, project_dir = self._project(tmp_path)
        wt = project_dir / "worktrees" / "t1"
        wt.mkdir(parents=True)
        (wt / "copy.core").write_text("CAPI=2:\nname: ::c:0\n", encoding="utf-8")
        bl = project_dir / ".baseline-wt-7-abc"
        bl.mkdir()
        (bl / "copy.core").write_text("CAPI=2:\nname: ::c:0\n", encoding="utf-8")
        rec = _Rec()
        doctor._check_stealth_cores(root, project_dir, rec.p, rec.f)
        assert rec.kinds() == {"pass"}

    def test_private_registry_copies_are_not_stranded(self, tmp_path: Path):
        root, project_dir = self._project(tmp_path)
        registry = project_dir / "tmp" / "fusesoc-isolated-cores"
        registry.mkdir(parents=True)
        (registry / "copy.core").write_text("CAPI=2:\nname: ::c:0\n", encoding="utf-8")
        rec = _Rec()

        doctor._check_stealth_cores(root, project_dir, rec.p, rec.f)

        assert rec.kinds() == {"pass"}

    def test_cross_root_collision_fails(self, tmp_path: Path):
        root, project_dir = self._project(tmp_path)
        (root / "repo.core").write_text("CAPI=2:\nname: ::dup:0\n", encoding="utf-8")
        (project_dir / "cores" / "s.core").write_text("CAPI=2:\nname: ::dup:1\n", encoding="utf-8")
        rec = _Rec()
        doctor._check_stealth_cores(root, project_dir, rec.p, rec.f)
        assert any("both core roots" in msg for msg in rec.fails())

    def test_hidden_cores_require_stealth_mode(self, tmp_path: Path):
        root, project_dir = self._project(tmp_path)
        (project_dir / "cores" / "s.core").write_text(
            "CAPI=2:\nname: ::stealth:0\n", encoding="utf-8"
        )
        rec = _Rec()

        doctor._check_stealth_cores(root, project_dir, rec.p, rec.f, stealth_enabled=False)

        assert any("stealth mode is disabled" in msg for msg in rec.fails())

    def test_stealth_projection_is_repaired(self, tmp_path: Path):
        root, project_dir = self._project(tmp_path)
        (project_dir / "booley.toml").write_text("[stealth]\nenabled = true\n", encoding="utf-8")
        (project_dir / "cores" / "s.core").write_text(
            "CAPI=2:\nname: ::stealth:0\n", encoding="utf-8"
        )
        rec = _Rec()

        doctor._check_stealth_cores(
            root,
            project_dir,
            rec.p,
            rec.f,
            stealth_enabled=True,
            repair=True,
        )

        assert rec.fails() == []
        assert (root / ".booley-projected-s.core").is_file()


# ---------------------------------------------------------------------------
# Ticket Board self-heal (ADR 0028 Decision 11) — _check_board_orphans
# ---------------------------------------------------------------------------


def _seed_board(tmp_path: Path) -> Path:
    """Create a minimal tickets tree; return the tickets dir."""
    tickets = tmp_path / "tickets"
    for state in ("queue", "active", "blocked"):
        (tickets / "board" / state).mkdir(parents=True, exist_ok=True)
    (tickets / "logs").mkdir(parents=True, exist_ok=True)
    return tickets


def _seed_active_ticket(tickets: Path, slug: str = "stuck") -> None:
    (tickets / "board" / "active" / f"{slug}.md").write_text(
        "---\nsummary: Stuck ticket\n---\n",
        encoding="utf-8",
    )
    lock_dir = tickets / "logs" / slug
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "ticket.lock").write_text("99999", encoding="utf-8")


class TestBoardOrphanSelfHeal:
    """active/ tickets whose owner PID is dead must be recovered by doctor
    (in-container only: ticket PIDs are container-scoped under ADR 0028)."""

    def test_host_side_skips(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr("booley.runtime_context.inside_session_runtime", lambda: False)
        rec = _Rec()
        doctor._check_board_orphans(tmp_path, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"skip"}

    def test_no_active_tickets_passes(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr("booley.runtime_context.inside_session_runtime", lambda: True)
        tickets = _seed_board(tmp_path)
        monkeypatch.setenv("TICKETS_DIR", str(tickets))
        rec = _Rec()
        doctor._check_board_orphans(tmp_path, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"pass"}

    def test_dead_pid_recovered_with_warn(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr("booley.runtime_context.inside_session_runtime", lambda: True)
        tickets = _seed_board(tmp_path)
        _seed_active_ticket(tickets)
        monkeypatch.setenv("TICKETS_DIR", str(tickets))
        monkeypatch.setattr(
            "booley.ticket_board.helpers.is_pid_alive",
            lambda _pid: False,
        )
        board_calls: list[list[str]] = []

        def fake_board(_root, args, **_kw):
            board_calls.append(list(args))
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        monkeypatch.setattr("booley.harness.booley._run_board", fake_board)

        rec = _Rec()
        doctor._check_board_orphans(tmp_path, rec.p, rec.w, rec.s)

        assert rec.kinds() == {"warn"}
        assert any("recovered 1" in m for lvl, m in rec.events if lvl == "warn")
        # The recovery went through the board: blocked with a note.
        assert any(args[0] == "block" and "stuck" in args for args in board_calls)

    def test_live_pid_passes(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr("booley.runtime_context.inside_session_runtime", lambda: True)
        tickets = _seed_board(tmp_path)
        _seed_active_ticket(tickets)
        monkeypatch.setenv("TICKETS_DIR", str(tickets))
        monkeypatch.setattr(
            "booley.ticket_board.helpers.is_pid_alive",
            lambda _pid: True,
        )
        rec = _Rec()
        doctor._check_board_orphans(tmp_path, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"pass"}
        assert any("live owner PIDs" in m for _lvl, m in rec.events)

    def test_read_only_reports_dead_pid_without_moving_ticket(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr("booley.runtime_context.inside_session_runtime", lambda: True)
        tickets = _seed_board(tmp_path)
        _seed_active_ticket(tickets)
        monkeypatch.setenv("TICKETS_DIR", str(tickets))
        monkeypatch.setattr("booley.ticket_board.helpers.is_pid_alive", lambda _pid: False)
        board_calls: list[list[str]] = []
        monkeypatch.setattr(
            "booley.harness.booley._run_board",
            lambda _root, args, **_kw: board_calls.append(list(args)),
        )
        rec = _Rec()

        doctor._check_board_orphans(tmp_path, rec.p, rec.w, rec.s, repair=False)

        assert rec.kinds() == {"warn"}
        assert "found 1 orphaned" in rec.events[0][1]
        assert board_calls == []


# ---------------------------------------------------------------------------
# ADR 0028 Decision 12 — memory invariant, runtime checks, developer probe
# ---------------------------------------------------------------------------

_GIB = 1024**3


def _adr28_project(tmp_path: Path, *, booley_toml: dict | None = None) -> doctor.ProjectAudit:
    pd = tmp_path / ".booley_project"
    pd.mkdir(exist_ok=True)
    return doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=pd,
        booley_toml=booley_toml or {},
        configs_toml={},
        first_target="x",
    )


def _fake_cgroup(tmp_path: Path, monkeypatch, text: str) -> None:
    path = tmp_path / "memory.max"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(doctor, "_CGROUP_MEM_LIMIT_PATHS", (path,))


def _set_venue(monkeypatch, inside: bool) -> None:
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: inside)
    # The suite inherits the shell it runs in, and an agent CLI exports the
    # markers _check_host_agent_session keys off — a run started from Claude
    # Code would otherwise record different events than one from a plain
    # terminal. Pin them off; the agent case is set explicitly where wanted.
    _set_agent_session(monkeypatch, None)


def _set_agent_session(monkeypatch, app: str | None) -> None:
    """Simulate being spawned from *app*'s shell (None = a plain terminal)."""
    markers = dict(runtime_context._AGENT_SESSION_MARKERS)
    for app_markers in markers.values():
        for marker in app_markers:
            monkeypatch.delenv(marker, raising=False)
    if app is not None:
        monkeypatch.setenv(markers[app][0], "1")


class TestMemoryInvariant:
    """ADR 0028 D12: container_mem >= max_heavy*4g + max_tickets*orch + 2g."""

    def test_warn_shows_arithmetic_with_1g_fallback(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, True)
        _fake_cgroup(tmp_path, monkeypatch, str(6 * _GIB))  # 6g < 8g required
        rec = _Rec()
        doctor._check_memory_invariant(_adr28_project(tmp_path), rec.p, rec.w, rec.s)
        assert rec.kinds() == {"warn"}
        warn = rec.events[0][1]
        assert (
            "6g < 1x4g + 2x1g + 2g = 8g — raise the devcontainer memory or lower [jobs] caps"
        ) in warn

    def test_pass_when_cgroup_limit_covers_caps(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, True)
        _fake_cgroup(tmp_path, monkeypatch, str(10 * _GIB))  # 10g >= 8g
        rec = _Rec()
        doctor._check_memory_invariant(_adr28_project(tmp_path), rec.p, rec.w, rec.s)
        assert rec.kinds() == {"pass"}
        assert "10g ≥ 1x4g + 2x1g + 2g = 8g" in rec.events[0][1]

    def test_unlimited_cgroup_passes_with_note(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, True)
        _fake_cgroup(tmp_path, monkeypatch, "max")
        rec = _Rec()
        doctor._check_memory_invariant(_adr28_project(tmp_path), rec.p, rec.w, rec.s)
        assert rec.kinds() == {"pass"}
        assert "unlimited" in rec.events[0][1]

    def test_v1_unlimited_sentinel_passes(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, True)
        _fake_cgroup(tmp_path, monkeypatch, str(1 << 62))  # v1 "no limit"
        rec = _Rec()
        doctor._check_memory_invariant(_adr28_project(tmp_path), rec.p, rec.w, rec.s)
        assert rec.kinds() == {"pass"}
        assert "unlimited" in rec.events[0][1]

    def test_absent_cgroup_files_pass_as_unlimited(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, True)
        monkeypatch.setattr(
            doctor,
            "_CGROUP_MEM_LIMIT_PATHS",
            (tmp_path / "nope",),
        )
        rec = _Rec()
        doctor._check_memory_invariant(_adr28_project(tmp_path), rec.p, rec.w, rec.s)
        assert rec.kinds() == {"pass"}

    def test_measured_value_overrides_1g_fallback(self, tmp_path, monkeypatch):
        # 7g limit: fallback needs 1x4 + 2x1 + 2 = 8g (WARN); a measured
        # 0.5g developer needs 1x4 + 2x0.5 + 2 = 7g (PASS).
        _set_venue(monkeypatch, True)
        _fake_cgroup(tmp_path, monkeypatch, str(7 * _GIB))
        project = _adr28_project(tmp_path)

        rec = _Rec()
        doctor._check_memory_invariant(project, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"warn"}
        assert "1g developer fallback" in rec.events[0][1]

        developer_probe.record_measurement(project.project_dir, _GIB // 2)
        rec = _Rec()
        doctor._check_memory_invariant(project, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"pass"}
        assert "2x0.5g" in rec.events[0][1]
        assert "measured developer RSS" in rec.events[0][1]

    def test_caps_come_from_jobs_table(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, False)
        toml = {
            "sandbox": {"memory": "11g"},
            "jobs": {"max_heavy": 2, "max_tickets": 1},
        }
        rec = _Rec()
        doctor._check_memory_invariant(
            _adr28_project(tmp_path, booley_toml=toml),
            rec.p,
            rec.w,
            rec.s,
        )
        assert rec.kinds() == {"pass"}  # 2x4 + 1x1 + 2 = 11g exactly
        assert "2x4g + 1x1g + 2g = 11g" in rec.events[0][1]

    def test_host_side_skips_without_sandbox_memory(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, False)
        rec = _Rec()
        doctor._check_memory_invariant(_adr28_project(tmp_path), rec.p, rec.w, rec.s)
        assert rec.kinds() == {"skip"}
        assert "no [sandbox] memory" in rec.events[0][1]

    def test_host_side_warns_from_sandbox_memory(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, False)
        toml = {"sandbox": {"memory": "6g"}}
        rec = _Rec()
        doctor._check_memory_invariant(
            _adr28_project(tmp_path, booley_toml=toml),
            rec.p,
            rec.w,
            rec.s,
        )
        assert rec.kinds() == {"warn"}
        assert "[sandbox] memory 6g < 1x4g + 2x1g + 2g = 8g" in rec.events[0][1]

    def test_unparseable_sandbox_memory_warns(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, False)
        toml = {"sandbox": {"memory": "lots"}}
        rec = _Rec()
        doctor._check_memory_invariant(
            _adr28_project(tmp_path, booley_toml=toml),
            rec.p,
            rec.w,
            rec.s,
        )
        assert rec.kinds() == {"warn"}
        assert "unparseable" in rec.events[0][1]

    def test_skips_without_project(self, monkeypatch):
        rec = _Rec()
        doctor._check_memory_invariant(None, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"skip"}


class TestVenueCheck:
    """ADR 0028: BOOLEY_CONTAINER marker + slot store, on both venues."""

    def test_container_marker_and_slot_store_pass(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, True)
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "slots"))
        rec = _Rec()
        doctor._check_runtime_location(None, "booley-sandbox", rec.p, rec.w, rec.s, rec.f)
        assert rec.fails() == []
        passes = [m for lvl, m in rec.events if lvl == "pass"]
        assert any("BOOLEY_CONTAINER=1 set" in m for m in passes)
        assert any("slot store root writable" in m for m in passes)

    def test_fallback_only_detection_warns_with_rebuild_fix(
        self,
        tmp_path,
        monkeypatch,
    ):
        _set_venue(monkeypatch, True)  # detected — but not via the env marker
        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "slots"))
        rec = _Rec()
        doctor._check_runtime_location(None, "booley-sandbox", rec.p, rec.w, rec.s, rec.f)
        warns = [m for lvl, m in rec.events if lvl == "warn"]
        assert any("/.dockerenv fallback" in m for m in warns)
        assert any("base AND derived sandbox images" in m for m in warns)
        # The slot store half still runs.
        assert any("slot store root writable" in m for lvl, m in rec.events if lvl == "pass")

    def test_unwritable_slot_store_fails(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, True)
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        blocked = tmp_path / "file-not-dir"
        blocked.write_text("", encoding="utf-8")  # mkdir under a file → OSError
        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(blocked / "slots"))
        rec = _Rec()
        doctor._check_runtime_location(None, "booley-sandbox", rec.p, rec.w, rec.s, rec.f)
        assert any("slot store root not writable" in m for m in rec.fails())

    def test_host_side_passes_when_image_bakes_marker(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, False)

        def fake_run(cmd, **kwargs):
            assert "{{json .Config.Env}}" in cmd
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='["PATH=/usr/bin", "BOOLEY_CONTAINER=1"]\n',
                stderr="",
            )

        monkeypatch.setattr(doctor.subprocess, "run", fake_run)
        monkeypatch.setattr(doctor, "_docker_image_exists_by_name", lambda _image: True)
        rec = _Rec()
        doctor._check_runtime_location("docker", "booley-sandbox", rec.p, rec.w, rec.s, rec.f)
        assert rec.kinds() == {"pass"}
        assert any("bakes BOOLEY_CONTAINER=1" in m for lvl, m in rec.events if lvl == "pass")

    def test_host_side_warns_when_marker_missing(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, False)

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='["PATH=/usr/bin"]\n',
                stderr="",
            )

        monkeypatch.setattr(doctor.subprocess, "run", fake_run)
        monkeypatch.setattr(doctor, "_docker_image_exists_by_name", lambda _image: True)
        rec = _Rec()
        doctor._check_runtime_location("docker", "booley-sandbox", rec.p, rec.w, rec.s, rec.f)
        assert rec.fails() == []
        warns = [m for lvl, m in rec.events if lvl == "warn"]
        assert any("base AND derived sandbox images" in m for m in warns)

    def test_host_side_skips_when_image_absent(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, False)

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such image")

        monkeypatch.setattr(doctor.subprocess, "run", fake_run)
        rec = _Rec()
        doctor._check_runtime_location("docker", "booley-sandbox", rec.p, rec.w, rec.s, rec.f)
        assert rec.kinds() == {"pass", "skip"}  # the host-shell note, then the skip
        assert any("runtime marker check skipped" in m for lvl, m in rec.events if lvl == "skip")

    def test_host_side_skips_without_docker(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, False)
        rec = _Rec()
        doctor._check_runtime_location(None, "booley-sandbox", rec.p, rec.w, rec.s, rec.f)
        assert rec.kinds() == {"pass", "skip"}


class TestHostAgentSession:
    """An agent on the host gets no Booley Flows and no error — Doctor must say so.

    MCP registration is container-side (booley.incontainer_register runs from
    the devcontainer hooks), so a host-launched agent has no `booley` MCP
    server at all. Nothing else reports that absence.
    """

    def test_plain_host_shell_passes_quietly(self, monkeypatch):
        _set_venue(monkeypatch, False)
        rec = _Rec()
        doctor._check_host_agent_session(rec.p, rec.w)
        assert rec.kinds() == {"pass"}
        assert "Session Runtime" in rec.events[0][1]

    @pytest.mark.parametrize("app", ["claude", "codex"])
    def test_agent_on_host_warns_and_names_the_way_in(self, monkeypatch, app):
        _set_venue(monkeypatch, False)
        _set_agent_session(monkeypatch, app)
        rec = _Rec()
        doctor._check_host_agent_session(rec.p, rec.w)
        assert rec.kinds() == {"warn"}
        warn = rec.events[0][1]
        assert app in warn
        assert "booley_status" in warn  # the specific MCP tool the guidance mandates

    def test_agent_inside_container_does_not_warn(self, tmp_path, monkeypatch):
        """In the Session Runtime the MCP tools do exist — no note either way."""
        _set_venue(monkeypatch, True)
        _set_agent_session(monkeypatch, "claude")
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "slots"))
        rec = _Rec()
        doctor._check_runtime_location(None, "booley-sandbox", rec.p, rec.w, rec.s, rec.f)
        assert not any("HOST" in m for _lvl, m in rec.events)


class TestGuidanceVenueNote:
    """Guidance that names Booley Flows must scope them to the Session Runtime.

    The repo root's CLAUDE.md link resolves on the host too, so an unscoped
    file tells a host-side agent to call MCP tools that do not exist there.
    """

    def _canon(self, tmp_path: Path, body: str) -> Path:
        canon = tmp_path / "AGENTS.md"
        canon.write_text(body, encoding="utf-8")
        return canon

    def test_scoped_guidance_passes(self, tmp_path):
        canon = self._canon(
            tmp_path,
            "- The MCP tools below exist only inside the Session Runtime.\n"
            "- At the start of a tab, call `booley_status`.\n",
        )
        rec = _Rec()
        doctor._check_guidance_runtime_note(canon, rec.p, rec.w)
        assert rec.kinds() == {"pass"}

    def test_unscoped_btool_guidance_warns(self, tmp_path):
        canon = self._canon(tmp_path, "- At the start of a tab, call `booley_status`.\n")
        rec = _Rec()
        doctor._check_guidance_runtime_note(canon, rec.p, rec.w)
        assert rec.kinds() == {"warn"}
        assert "host-side agent session" in rec.events[0][1]

    def test_guidance_without_btools_is_silent(self, tmp_path):
        """Nothing to scope — a project may legitimately not mention them."""
        canon = self._canon(tmp_path, "# AGENTS.md\n\n- Project purpose: a UART.\n")
        rec = _Rec()
        doctor._check_guidance_runtime_note(canon, rec.p, rec.w)
        assert rec.events == []

    def test_shipped_template_satisfies_the_check(self, tmp_path):
        """The template doctor's fix hint points at must itself pass the check."""
        from booley.paths import skills_dir

        template = skills_dir() / "booley-setup" / "AGENTS_TEMPLATE.md"
        canon = self._canon(tmp_path, template.read_text(encoding="utf-8"))
        rec = _Rec()
        doctor._check_guidance_runtime_note(canon, rec.p, rec.w)
        assert rec.kinds() == {"pass"}

    def test_automatic_profile_reports_missing_links_without_creating_them(
        self, tmp_path, monkeypatch
    ):
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        self._canon(project_dir, "# Project guidance\n")
        project = doctor.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")
        monkeypatch.setattr(
            doctor,
            "ensure_guidance_links",
            lambda *_a, **_kw: pytest.fail("read-only Doctor repaired links"),
        )
        rec = _Rec()

        doctor._check_agents_md(project, rec.p, rec.w, repair=False)

        assert rec.kinds() == {"warn"}
        assert not (tmp_path / "AGENTS.md").exists()
        assert not (tmp_path / "CLAUDE.md").exists()

    def test_manual_profile_repairs_guidance_links(self, tmp_path, monkeypatch):
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        self._canon(project_dir, "# Project guidance\n")
        project = doctor.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")
        calls = []
        monkeypatch.setattr(
            doctor,
            "ensure_guidance_links",
            lambda root, data: calls.append((root, data)),
        )
        rec = _Rec()

        doctor._check_agents_md(project, rec.p, rec.w)

        assert calls == [(tmp_path, project_dir)]
        assert any("root links ensured" in message for _kind, message in rec.events)


class TestCheckDocker:
    """QA-3: the container-runtime check must not FAIL inside the Session Runtime.

    In-container there is no nested container runtime and Booley Flows run
    directly, so a missing runtime is expected — SKIP, don't FAIL.
    """

    def test_skips_in_container(self, monkeypatch):
        _set_venue(monkeypatch, True)
        # Even if no runtime is on PATH, in-container it must not FAIL.
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        rec = _Rec()
        result = doctor._check_docker(rec.p, rec.s, rec.f)
        assert result is None
        assert rec.kinds() == {"skip"}
        assert not rec.fails()

    def test_fails_on_host_without_runtime(self, monkeypatch):
        _set_venue(monkeypatch, False)
        monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
        rec = _Rec()
        result = doctor._check_docker(rec.p, rec.s, rec.f)
        assert result is None
        assert "container runtime not on PATH" in rec.fails()

    def test_passes_on_host_with_running_runtime(self, monkeypatch):
        _set_venue(monkeypatch, False)
        runtime = "doc" + "ker"
        monkeypatch.setattr(
            doctor.shutil,
            "which",
            lambda name: runtime if name == doctor._CONTAINER_CLI else None,
        )
        monkeypatch.setattr(
            doctor.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""),
        )
        rec = _Rec()
        result = doctor._check_docker(rec.p, rec.s, rec.f)
        assert result == runtime
        assert rec.kinds() == {"pass"}


class TestElaborateValidateOrOptOut:
    """QA-6: elaborate must be validated when exposed, or explicitly opted out.

    The old bug: elaborate was hard-required on the MCP surface but never
    smoke-tested, so a broken exposed elaborate (no adapter → generic FuseSoC
    path that can't build a non-FuseSoC design) passed setup.
    """

    def _audit_with(self, tmp_path, booley_toml):
        pd = tmp_path / ".booley_project"
        pd.mkdir(exist_ok=True)
        return doctor.ProjectAudit(
            project_root=tmp_path,
            project_dir=pd,
            booley_toml=booley_toml,
            configs_toml={"x": {}},
            first_target="x",
        )

    def test_elaborate_not_in_base_required(self):
        assert "elab" not in doctor._BASE_REQUIRED_MCP_TOOLS
        assert "bwave" in doctor._BASE_REQUIRED_MCP_TOOLS

    def test_active_by_default(self, tmp_path):
        # No [flows] config at all → default surface exposes elaborate.
        assert doctor._elaborate_active(self._audit_with(tmp_path, {})) is True

    def test_opt_out_by_enabled_false(self, tmp_path):
        p = self._audit_with(
            tmp_path,
            {"flows": {"elab": {"enabled": False}}},
        )
        assert doctor._elaborate_active(p) is False

    def test_required_mcp_tracks_activeness(self, tmp_path):
        active = self._audit_with(tmp_path, {"flows": {"elab": {}}})
        assert "elab" in doctor._required_mcp_tools(active)
        opted_out = self._audit_with(tmp_path, {"flows": {"elab": {"enabled": False}}})
        assert "elab" not in doctor._required_mcp_tools(opted_out)

    def test_deep_check_skips_when_opted_out(self, tmp_path):
        p = self._audit_with(tmp_path, {"flows": {"elab": {"enabled": False}}})
        rec = _Rec()
        doctor._run_elaborate_deep_check(
            p,
            None,
            False,
            rec.p,
            rec.w,
            rec.s,
            rec.f,
        )
        assert rec.kinds() == {"skip"}
        assert "opt-out" in rec.events[0][1]
        assert not rec.fails()


class TestAdvisoryMcpTools:
    """Specialists are expected unless their own ``enabled`` flag is false."""

    _SKILL_DEFAULT_BUILTIN = (
        "sim",
        "elab",
        "lint",
        "synth",
        "submit_run_report",
    )

    def _project(self, tmp_path, endpoint_config):
        pd = tmp_path / ".booley_project"
        pd.mkdir(exist_ok=True)
        flows = {
            name: config
            for name, config in endpoint_config.items()
            if name in {"sim", "elab", "lint", "synth", "fpga"}
        }
        mcp_tools = {name: config for name, config in endpoint_config.items() if name not in flows}
        return doctor.ProjectAudit(
            project_root=tmp_path,
            project_dir=pd,
            booley_toml={"flows": flows, "mcp_tools": mcp_tools},
            configs_toml={},
            first_target="",
        )

    def test_default_expects_all_specialists(self, tmp_path):
        p = self._project(tmp_path, {})
        assert doctor._advisory_mcp_tools(p) == set(doctor._ADVISORY_INTERACTIVE_MCP_TOOLS)

    def test_explicitly_disabled_specialist_is_not_expected(self, tmp_path):
        p = self._project(tmp_path, {"reviewer": {"enabled": False}})
        assert doctor._advisory_mcp_tools(p) == {"mutation_tester"}

    def _skill_default_endpoint_config(self, *, reviewer=False):
        """Every flow and optional specialist explicitly disabled."""
        endpoint_config = {}
        for name in ("sim", "elab", "lint", "synth"):
            endpoint_config[name] = {"enabled": False}
        endpoint_config["reviewer"] = {"enabled": reviewer}
        endpoint_config["mutation_tester"] = {"enabled": False}
        return endpoint_config

    def _payload(self, mcp_tool_names):
        return {"tools": list(mcp_tool_names), "errors": [], "logs_dir_ok": True}

    def test_payload_check_is_clean_on_the_skill_default(self, tmp_path):
        # End to end through _check_mcp_tool_payload: a first-run project whose
        # MCP surface (correctly) lacks the specialists must not WARN.
        p = self._project(tmp_path, self._skill_default_endpoint_config())
        rec = _Rec()
        doctor._check_mcp_tool_payload(
            p, self._payload([*self._SKILL_DEFAULT_BUILTIN, "bwave"]), False, rec.p, rec.w, rec.f
        )
        assert rec.kinds() == {"pass"}

    def test_payload_check_warns_on_enabled_but_absent_specialist(self, tmp_path):
        p = self._project(tmp_path, self._skill_default_endpoint_config(reviewer=True))
        rec = _Rec()
        doctor._check_mcp_tool_payload(
            p, self._payload([*self._SKILL_DEFAULT_BUILTIN, "bwave"]), False, rec.p, rec.w, rec.f
        )
        warns = [m for lvl, m in rec.events if lvl == "warn"]
        assert any("reviewer" in m for m in warns)
        assert not any("mutation_tester" in m for m in warns)


class TestFailPathSelfTest:
    """QA-4/QA-5: --deep proves a verification Flow can DETECT a bad design.

    Exit contract: good => 0 (pass); bad => 1 (graded fail). A bad that exits 0 is
    a false pass (QA-4); a bad that exits 2 is an infra error masking the failure
    (QA-5). Both must FAIL setup.
    """

    def _audit(self, tmp_path, fixture=True):
        pd = tmp_path / ".booley_project"
        pd.mkdir(exist_ok=True)
        (pd / "tests.toml").write_text('[sim_core]\ntests = ["hello_world"]\n', encoding="utf-8")
        if fixture:
            overlay = pd / "selftest" / "sim" / "bad-overlay" / "firmware.hex"
            overlay.parent.mkdir(parents=True)
            overlay.write_text("broken\n", encoding="utf-8")
        return doctor.ProjectAudit(
            project_root=tmp_path,
            project_dir=pd,
            booley_toml={
                "flows": {
                    "sim": {"default_target": "sim_core"},
                    "lint": {"enabled": False},
                }
            },
            configs_toml={"sim_core": {}},
            first_target="sim_core",
        )

    def _run(self, monkeypatch, project, exit_by_kind):
        """Drive _run_selftest_checks with a stub Flow that returns per-kind rc."""
        _set_venue(monkeypatch, False)

        def fake_run(cmd, **kwargs):
            kind = kwargs["env"][selftest_overlay.INTERNAL_KIND_ENV]
            return subprocess.CompletedProcess(cmd, exit_by_kind[kind], stdout="", stderr="")

        monkeypatch.setattr(doctor.subprocess, "run", fake_run)
        monkeypatch.setattr(doctor, "_docker_image_exists_by_name", lambda _image: True)
        rec = _Rec()
        doctor._run_selftest_checks(project, "docker", rec.p, rec.w, rec.s, rec.f)
        return rec

    def test_healthy_selftest_passes(self, tmp_path, monkeypatch):
        # good -> 0 (pass), bad -> 1 (graded fail): both correct.
        rec = self._run(monkeypatch, self._audit(tmp_path), {"good": 0, "bad": 1})
        assert not rec.fails()
        assert any("correctly graded a failure" in m for _, m in rec.events)

    def test_false_pass_on_bad_is_caught(self, tmp_path, monkeypatch):
        # QA-4: bad case exits 0 -> false pass -> FAIL.
        rec = self._run(monkeypatch, self._audit(tmp_path), {"good": 0, "bad": 0})
        assert any("FALSE-PASSED" in m for m in rec.fails())

    def test_infra_error_on_bad_is_caught(self, tmp_path, monkeypatch):
        # QA-5: bad case exits 2 (contract_error) instead of a graded fail -> FAIL.
        rec = self._run(monkeypatch, self._audit(tmp_path), {"good": 0, "bad": 2})
        assert any("infra error" in m for m in rec.fails())

    def test_good_case_must_pass(self, tmp_path, monkeypatch):
        rec = self._run(monkeypatch, self._audit(tmp_path), {"good": 1, "bad": 1})
        assert any("did not pass" in m for m in rec.fails())

    def test_missing_selftest_warns_not_fails(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, False)
        rec = _Rec()
        doctor._run_selftest_checks(
            self._audit(tmp_path, fixture=False),
            "docker",
            rec.p,
            rec.w,
            rec.s,
            rec.f,
        )
        assert not rec.fails()
        assert any("fail-path unvalidated" in m for _, m in rec.events if _ == "warn")

    def test_flow_runs_in_place_inside_session_runtime(self, tmp_path, monkeypatch):
        # F-17 / ADR 0028: inside the Session Runtime there is no docker — the
        # container IS the sandbox — so the self-test must exec the Flow
        # in-place (this interpreter, no docker wrap, no SKIP), mirroring
        # _flow_check_routing and how Session Runtime Flows themselves execute
        # in-container. Pre-fix this skipped with "'docker'
        # runtime not available", forcing the final --deep gate onto the host.
        _set_venue(monkeypatch, True)
        seen_cmds: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            seen_cmds.append(cmd)
            kind = kwargs["env"][selftest_overlay.INTERNAL_KIND_ENV]
            rc = {"good": 0, "bad": 1}[kind]
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

        monkeypatch.setattr(doctor.subprocess, "run", fake_run)
        project = self._audit(tmp_path)
        rec = _Rec()
        # docker_exe=None: no nested container runtime exists by design.
        doctor._run_selftest_checks(project, None, rec.p, rec.w, rec.s, rec.f)
        assert not rec.fails()
        assert not any(lvl == "skip" for lvl, _ in rec.events)
        assert any("correctly graded a failure" in m for _, m in rec.events)
        # In-place = this interpreter drives the Flow module directly.
        assert seen_cmds and all(cmd[0] == sys.executable for cmd in seen_cmds)

    def test_host_deep_check_still_skips_without_docker(self, tmp_path, monkeypatch):
        # On the HOST a Session Runtime self-test genuinely needs Docker
        # runtime; a host without Docker stays a SKIP (unchanged by F-17).
        _set_venue(monkeypatch, False)
        project = self._audit(tmp_path)
        rec = _Rec()
        doctor._run_selftest_checks(project, None, rec.p, rec.w, rec.s, rec.f)
        skips = [m for lvl, m in rec.events if lvl == "skip"]
        assert skips and all("runtime not available" in m for m in skips)
        assert not rec.fails()

    def test_builtin_backend_gets_the_selftest_too(self, tmp_path, monkeypatch):
        # ADR 0039: the fixture mechanism is backend-agnostic — a builtin
        # simulate without a conventional overlay is nagged (fail-path unproven),
        # not silently skipped (the C910 re-port gate found the old
        # project-native-only gate letting builtin --deep report green
        # without ever running the fail path). Fuller coverage lives in
        # test_doctor_selftest_builtin.py.
        pd = tmp_path / ".booley_project"
        pd.mkdir(exist_ok=True)
        p = doctor.ProjectAudit(
            project_root=tmp_path,
            project_dir=pd,
            booley_toml={
                "flows": {
                    "sim": {"default_target": "x"},
                    "lint": {"enabled": False},
                }
            },
            configs_toml={"x": {}},
            first_target="x",
        )
        rec = _Rec()
        doctor._run_selftest_checks(p, "docker", rec.p, rec.w, rec.s, rec.f)
        assert not rec.fails()
        warns = [m for lvl, m in rec.events if lvl == "warn"]
        assert any("sim fail-path unvalidated" in m for m in warns)


class TestDeveloperProbe:
    """doctor --deep measures the developer memory term (ADR 0028 D12)."""

    def test_record_and_load_round_trip(self, tmp_path):
        pd = tmp_path / ".booley_project"
        pd.mkdir()
        path = developer_probe.record_measurement(pd, 3 * _GIB // 2)
        assert path == pd / "runtime" / "developer_probe.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["developer_peak_rss_bytes"] == 3 * _GIB // 2
        assert "measured_at" in data
        assert developer_probe.load_measurement(pd) == 3 * _GIB // 2

    def test_load_measurement_tolerates_garbage(self, tmp_path):
        pd = tmp_path / ".booley_project"
        assert developer_probe.load_measurement(pd) is None  # absent
        path = developer_probe.probe_path(pd)
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        assert developer_probe.load_measurement(pd) is None
        path.write_text('{"developer_peak_rss_bytes": -3}', encoding="utf-8")
        assert developer_probe.load_measurement(pd) is None

    def test_probe_skips_host_side(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, False)
        rec = _Rec()
        doctor._run_developer_probe(_adr28_project(tmp_path), rec.p, rec.s, rec.f)
        assert rec.kinds() == {"skip"}
        assert "in-container" in rec.events[0][1]

    def test_probe_records_and_invariant_reads_it(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, True)
        monkeypatch.setattr(
            developer_probe,
            "measure_developer_rss",
            lambda root: (2 * _GIB, True),
        )
        project = _adr28_project(tmp_path)
        rec = _Rec()
        doctor._run_developer_probe(project, rec.p, rec.s, rec.f)
        assert rec.kinds() == {"pass"}
        assert "recorded to" in rec.events[0][1]
        assert developer_probe.load_measurement(project.project_dir) == 2 * _GIB

        # The invariant now uses the measurement: 1x4 + 2x2 + 2 = 10g.
        _fake_cgroup(tmp_path, monkeypatch, str(9 * _GIB))
        rec = _Rec()
        doctor._check_memory_invariant(project, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"warn"}
        assert "9g < 1x4g + 2x2g + 2g = 10g" in rec.events[0][1]

    def test_probe_failure_is_skip_not_crash(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, True)

        def boom(_root):
            raise developer_probe.ProbeError("probe agent failed: no auth")

        monkeypatch.setattr(developer_probe, "measure_developer_rss", boom)
        project = _adr28_project(tmp_path)
        rec = _Rec()
        doctor._run_developer_probe(project, rec.p, rec.s, rec.f)
        assert rec.kinds() == {"skip"}
        assert "1g fallback" in rec.events[0][1]
        assert developer_probe.load_measurement(project.project_dir) is None

    def test_agent_call_failure_is_fail_not_skip(self, tmp_path, monkeypatch):
        """Regression (2026-07-23): the developer agent crashed at launch on
        expired creds in EVERY ticket run, but the probe degraded it to a SKIP
        and doctor stayed green. An agent-call failure must fail loud."""
        _set_venue(monkeypatch, True)

        def boom(_root):
            raise developer_probe.ProbeError(
                "probe agent failed: OAuth session expired", agent_failure=True
            )

        monkeypatch.setattr(developer_probe, "measure_developer_rss", boom)
        rec = _Rec()
        doctor._run_developer_probe(_adr28_project(tmp_path), rec.p, rec.s, rec.f)
        assert rec.kinds() == {"fail"}
        assert "every ticket agent will fail" in rec.events[0][1]

    def test_measure_uses_child_rusage(self, tmp_path, monkeypatch):
        from booley.harness import agent as agent_mod
        from booley.harness import config as config_mod
        from booley.harness.models import AgentResult

        monkeypatch.setattr(config_mod, "load_models_config", lambda root: None)

        class _Cfg:
            def model_for_tier(self, tier):
                assert tier == "light"  # cheapest real call
                return "test-model"

        monkeypatch.setattr(config_mod, "get_backend_config", _Cfg)

        seen: list = []

        async def fake_call(params, **_kw):
            seen.append(params)
            return AgentResult(output="OK")

        monkeypatch.setattr(agent_mod, "call_agent", fake_call)

        class _RU:
            def __init__(self, kib):
                self.ru_maxrss = kib

        readings = iter([_RU(100_000), _RU(300_000)])
        peak, exact = developer_probe.measure_developer_rss(
            tmp_path,
            getrusage=lambda _who: next(readings),
        )
        assert peak == 300_000 * 1024
        assert exact is True
        assert seen[0].max_turns == 1  # a 1-turn trivial probe, no tools
        assert seen[0].allowed_agent_capabilities == []

    def test_measure_flags_upper_bound_without_delta(self, tmp_path, monkeypatch):
        from booley.harness import agent as agent_mod
        from booley.harness import config as config_mod
        from booley.harness.models import AgentResult

        monkeypatch.setattr(config_mod, "load_models_config", lambda root: None)

        class _Cfg:
            def model_for_tier(self, tier):
                return "test-model"

        monkeypatch.setattr(config_mod, "get_backend_config", _Cfg)

        async def fake_call(params, **_kw):
            return AgentResult(output="OK")

        monkeypatch.setattr(agent_mod, "call_agent", fake_call)

        class _RU:
            def __init__(self, kib):
                self.ru_maxrss = kib

        readings = iter([_RU(400_000), _RU(400_000)])  # earlier child was bigger
        peak, exact = developer_probe.measure_developer_rss(
            tmp_path,
            getrusage=lambda _who: next(readings),
        )
        assert peak == 400_000 * 1024  # safe upper bound
        assert exact is False

    def test_measure_wraps_agent_failure_in_probe_error(self, tmp_path, monkeypatch):
        from booley.harness import config as config_mod

        def boom(_root):
            raise RuntimeError("no backend configured")

        monkeypatch.setattr(config_mod, "load_models_config", boom)
        with pytest.raises(developer_probe.ProbeError, match="no backend"):
            developer_probe.measure_developer_rss(tmp_path)


class TestHostClockCheck:
    """F-5: doctor warns when the host clock is skewed from an HTTP Date header."""

    @staticmethod
    def _run_check(monkeypatch, *, offset_s=None, unreachable=False):
        import email.utils
        import urllib.error
        from datetime import timedelta

        class _Headers:
            def __init__(self, date_str):
                self._date = date_str

            def get(self, _key, default=""):
                return self._date or default

        class _Resp:
            def __init__(self, date_str):
                self.headers = _Headers(date_str)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=0):
            if unreachable:
                raise urllib.error.URLError("offline")
            remote = doctor.datetime.now(doctor.UTC) - timedelta(seconds=offset_s)
            return _Resp(email.utils.format_datetime(remote))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        passes, warns, skips = [], [], []
        doctor._check_host_clock(passes.append, warns.append, skips.append)
        return passes, warns, skips

    def test_aligned_clock_passes(self, monkeypatch):
        passes, warns, skips = self._run_check(monkeypatch, offset_s=0)
        assert passes and not warns and not skips

    def test_skewed_clock_warns_with_fix_hint(self, monkeypatch):
        # Host 4h ahead of the reference (i.e. reference is 4h in the past
        # relative to local now -> skew positive -> "ahead of").
        passes, warns, _skips = self._run_check(monkeypatch, offset_s=4 * 3600)
        assert warns and not passes
        assert "ahead of" in warns[0]
        assert "w32tm /resync" in warns[0]

    def test_clock_behind_warns_behind(self, monkeypatch):
        passes, warns, _skips = self._run_check(monkeypatch, offset_s=-4 * 3600)
        assert warns and not passes
        assert "behind" in warns[0]

    def test_offline_skips(self, monkeypatch):
        passes, warns, skips = self._run_check(monkeypatch, unreachable=True)
        assert skips and not passes and not warns


# ===========================================================================
# Flow Target configured — cheap pass (F-9)
# ===========================================================================


def _audit_with_flows(tmp_path: Path, flows: dict) -> doctor.ProjectAudit:
    project_dir = _write_project(tmp_path)
    return doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=project_dir,
        booley_toml={"flows": flows},
        configs_toml={},
        first_target="",
    )


class TestCheckFlowTarget:
    """An enabled Flow with no Target can never run. That used to
    surface only under --deep, and BOOLEY_TEMPLATE shipped every project into
    exactly that state for elaborate."""

    def _fails(self, project, flow_name) -> list[tuple[str, str]]:
        fails: list[tuple[str, str]] = []
        doctor._check_flow_target(project, flow_name, lambda m, f="": fails.append((m, f)))
        return fails

    def test_configured_target_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor.fusesoc_registry, "enumerate_targets", lambda _r: {})
        project = _audit_with_flows(tmp_path, {"lint": {"default_target": "lint"}})
        assert self._fails(project, "lint") == []

    def test_missing_target_fails_and_names_the_key(self, tmp_path, monkeypatch):
        refs = {
            "lint": doctor.fusesoc_registry.TargetRef(
                name="lint",
                vlnv="::x:0",
                core_file=tmp_path / "x.core",
                eda_tool="verilator",
                flow="lint",
            )
        }
        monkeypatch.setattr(doctor.fusesoc_registry, "enumerate_targets", lambda _r: refs)
        project = _audit_with_flows(tmp_path, {"elab": {}})

        fails = self._fails(project, "elab")
        assert len(fails) == 1
        msg, fix = fails[0]
        assert "no Target" in msg
        assert "'lint' (flow=lint, EDA tool=verilator)" in msg  # copyable candidate
        assert "[flows.elab].default_target" in fix
        assert "enabled = false" in fix


class TestCheckElaborateSetup:
    def _run(self, project) -> tuple[list, list, list]:
        passes, skips, fails = [], [], []
        doctor._check_elaborate_setup(
            project,
            passes.append,
            skips.append,
            lambda m, f="": fails.append(m),
        )
        return passes, skips, fails

    def test_exposed_without_target_fails_in_the_cheap_pass(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            doctor.fusesoc_registry,
            "enumerate_targets",
            lambda _r: {
                "lint": doctor.fusesoc_registry.TargetRef(
                    name="lint", vlnv="::x:0", core_file=tmp_path / "x.core"
                )
            },
        )
        project = _audit_with_flows(tmp_path, {"elab": {}})
        _passes, _skips, fails = self._run(project)
        assert fails and "elab" in fails[0]

    def test_enabled_false_is_a_recorded_opt_out(self, tmp_path):
        project = _audit_with_flows(
            tmp_path,
            {"elab": {"enabled": False}},
        )
        _passes, skips, fails = self._run(project)
        assert not fails
        assert skips and "opt-out" in skips[0]

    def test_configured_target_passes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor.fusesoc_registry, "enumerate_targets", lambda _r: {})
        project = _audit_with_flows(
            tmp_path,
            {"elab": {"default_target": "lint"}},
        )
        passes, _skips, fails = self._run(project)
        assert not fails
        assert passes and "lint" in passes[0]


# ===========================================================================
# Legacy tools-section .core targets (F-6)
# ===========================================================================


class TestCheckLegacyCoreTargets:
    _LEGACY_CORE = """\
CAPI=2:
name: ::oc_i2c:0
filesets:
  rtl: {files: [i2c.v], file_type: verilogSource}
targets:
  sim:
    filesets: [rtl]
    default_tool: icarus
    tools: {icarus: {iverilog_options: []}}
"""

    def _run(self, tmp_path: Path, text: str, booley_toml=None):
        (tmp_path / "i2c.v").write_text("module i2c; endmodule\n", encoding="utf-8")
        (tmp_path / "design.core").write_text(text, encoding="utf-8")
        refs = doctor.fusesoc_registry.enumerate_targets(tmp_path)
        project = _schema_audit(tmp_path, booley_toml)
        passes, warns, notes = [], [], []
        doctor._check_legacy_core_targets(
            project,
            tmp_path,
            refs,
            passes.append,
            warns.append,
            _note=notes.append,
        )
        return passes, warns, notes

    def test_legacy_target_warns_with_the_rewrite(self, tmp_path):
        # The core hosts a configured Target → it's the project's own; the
        # warning names the rewrite.
        passes, warns, notes = self._run(
            tmp_path, self._LEGACY_CORE, {"flows": {"sim": {"default_target": "sim"}}}
        )
        assert not passes
        assert not notes
        assert len(warns) == 1
        assert "legacy FuseSoC tools-section target" in warns[0]
        assert "flow_options: {tool: icarus}" in warns[0]  # upstream EDA-tool field
        assert "CORE_TEMPLATE.yaml" in warns[0]

    def test_legacy_target_in_vendored_core_states_consequence_only(self, tmp_path):
        """No configured Target selects the core → it's vendored upstream
        content; 'rewrite the .core' is unfollowable there (ADR 0036 keeps the
        host repo byte-identical to upstream), so the warning just states the
        consequence."""
        passes, warns, notes = self._run(tmp_path, self._LEGACY_CORE)
        assert not passes
        assert not warns
        assert len(notes) == 1
        assert "legacy FuseSoC tools-section target" in notes[0]
        assert "not selected by this project" in notes[0]
        assert "CORE_TEMPLATE.yaml" not in notes[0]

    def test_vendored_legacy_targets_roll_up_to_one_line(self, tmp_path):
        # F-10: a vendored core with several legacy Targets must not spam one
        # WARN per Target (picorv32.core cost 4) — they roll up to a single line
        # that lists every offending name.
        multi = """\
CAPI=2:
name: ::oc_i2c:0
filesets:
  rtl: {files: [i2c.v], file_type: verilogSource}
targets:
  sim:
    filesets: [rtl]
    default_tool: icarus
    tools: {icarus: {iverilog_options: []}}
  lint:
    filesets: [rtl]
    default_tool: verilator
    tools: {verilator: {verilator_options: []}}
  synth:
    filesets: [rtl]
    default_tool: yosys
    tools: {yosys: {yosys_options: []}}
"""
        passes, warns, notes = self._run(tmp_path, multi)
        assert not passes
        assert not warns
        assert len(notes) == 1  # one rolled-up line, not three
        assert "3 legacy FuseSoC tools-section targets" in notes[0]
        for name in ("sim", "lint", "synth"):
            assert name in notes[0]
        assert "not selected by this project" in notes[0]

    def test_only_selected_target_in_native_core_warns(self, tmp_path):
        """Selecting one Target does not make every sibling an active finding."""
        multi = """\
CAPI=2:
name: ::oc_i2c:0
filesets:
  rtl: {files: [i2c.v], file_type: verilogSource}
targets:
  sim_old:
    filesets: [rtl]
    default_tool: icarus
    tools: {icarus: {iverilog_options: []}}
  lint_old:
    filesets: [rtl]
    default_tool: verilator
    tools: {verilator: {verilator_options: []}}
"""
        _passes, warns, notes = self._run(
            tmp_path, multi, {"flows": {"sim": {"default_target": "sim_old"}}}
        )
        assert len(warns) == 1
        assert "Target 'sim_old'" in warns[0]
        assert len(notes) == 1
        assert "lint_old" in notes[0]
        assert "sim_old" not in notes[0]

    def test_duplicate_bare_names_in_distinct_cores_are_both_audited(self, tmp_path):
        second = tmp_path / "second.core"
        second.write_text(self._LEGACY_CORE.replace("::oc_i2c:0", "::other:0"), encoding="utf-8")
        _passes, warns, notes = self._run(tmp_path, self._LEGACY_CORE)
        assert not warns
        assert len(notes) == 2
        assert all("sim" in note for note in notes)

    def test_flow_api_core_passes(self, tmp_path):
        text = """\
CAPI=2:
name: ::x:0
filesets:
  rtl: {files: [i2c.v], file_type: verilogSource}
targets:
  sim:
    filesets: [rtl]
    flow: sim
    flow_options: {tool: icarus}
    toplevel: tb
"""
        passes, warns, notes = self._run(tmp_path, text)
        assert passes and not warns and not notes


class TestCheckTargetNaming:
    """The <axis>_<subject> convention — WARN, and only on cores the project owns."""

    _CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [dut.v], file_type: verilogSource}
targets:
  {targets}
"""

    def _run(self, tmp_path: Path, target_block: str, booley_toml=None):
        (tmp_path / "dut.v").write_text("module dut; endmodule\n", encoding="utf-8")
        (tmp_path / "design.core").write_text(
            self._CORE.replace("{targets}", target_block), encoding="utf-8"
        )
        refs = doctor.fusesoc_registry.enumerate_targets(tmp_path)
        project = _schema_audit(tmp_path, booley_toml)
        passes, warns = [], []
        doctor._check_target_naming(project, tmp_path, refs, passes.append, warns.append)
        return passes, warns

    _MISNAMED = """\
soc_sim:
    filesets: [rtl]
    flow: sim
    flow_options: {tool: verilator}
    toplevel: tb
"""

    def test_owned_core_with_legacy_suffix_warns_and_names_the_rename(self, tmp_path):
        passes, warns = self._run(
            tmp_path, self._MISNAMED, {"flows": {"sim": {"default_target": "soc_sim"}}}
        )
        assert not passes
        assert len(warns) == 1
        assert "no axis prefix" in warns[0]
        assert "rename it 'sim_soc'" in warns[0]
        # The rename is never a one-file edit; the warning has to say so.
        assert "tests.toml" in warns[0]

    def test_vendored_core_is_left_alone(self, tmp_path):
        """Renaming an upstream Target is unfollowable — ADR 0036 keeps the host
        repo byte-identical to upstream, so a name it never selects is not ours."""
        passes, warns = self._run(tmp_path, self._MISNAMED)
        assert warns == []
        assert passes

    def test_conformant_names_pass(self, tmp_path):
        block = """\
sim_soc:
    filesets: [rtl]
    flow: sim
    flow_options: {tool: verilator}
    toplevel: tb
"""
        passes, warns = self._run(
            tmp_path, block, {"flows": {"sim": {"default_target": "sim_soc"}}}
        )
        assert warns == []
        assert any("<axis>_<subject>" in m for m in passes)

    def test_wiring_beats_the_legacy_suffix(self, tmp_path):
        """A `*_synth` Target wired to fpga_impl means fpga, whatever it is called.

        Some projects declare synth Targets as `flow: lint` resolution vehicles, so
        the declared flow is no evidence at all — the wiring is.
        """
        block = """\
soc_synth:
    filesets: [rtl]
    flow: lint
    flow_options: {tool: verilator}
    toplevel: dut
"""
        _, warns = self._run(tmp_path, block, {"flows": {"fpga": {"default_target": "soc_synth"}}})
        assert "rename it 'fpga_soc'" in warns[0]

    def test_legacy_asic_prefix_is_renamed_to_synth(self, tmp_path):
        block = """\
asic_core:
    filesets: [rtl]
    flow: generic
    flow_options: {tool: yosys, arch: xilinx}
    toplevel: dut
"""
        _, warns = self._run(
            tmp_path, block, {"flows": {"synth": {"default_target": "asic_core"}}}
        )
        assert "rename it 'synth_core'" in warns[0]

    def test_selected_bare_name_uses_flow_wiring_for_rename(self, tmp_path):
        block = """\
sim_ok:
    filesets: [rtl]
    flow: sim
    flow_options: {tool: verilator}
    toplevel: tb
  soc:
    filesets: [rtl]
    flow: sim
    flow_options: {tool: verilator}
    toplevel: tb
"""
        _, warns = self._run(tmp_path, block, {"flows": {"sim": {"default_target": "soc"}}})
        assert len(warns) == 1
        assert "Target 'soc'" in warns[0]
        assert "rename it 'sim_soc'" in warns[0]


class TestCheckStrayDefaultTargets:
    """`default:` is dependency plumbing — dead weight on a core with no dependents."""

    _STANDALONE = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [dut.v], file_type: verilogSource}
targets:
  default:
    filesets: [rtl]
  sim_x:
    filesets: [rtl]
    flow: sim
    flow_options: {tool: verilator}
    toplevel: tb
"""

    def _state_core(self, tmp_path: Path, name: str, text: str) -> None:
        """Write into `.booley_project/cores/` — always Booley-authored (ADR 0036)."""
        cores = tmp_path / ".booley_project" / "cores"
        cores.mkdir(parents=True, exist_ok=True)
        (cores / name).write_text(text, encoding="utf-8")

    def _run(self, tmp_path: Path):
        (tmp_path / "dut.v").write_text("module dut; endmodule\n", encoding="utf-8")
        project = _schema_audit(tmp_path)
        passes, warns = [], []
        doctor._check_stray_default_targets(project, tmp_path, passes.append, warns.append)
        return passes, warns

    def test_default_on_a_core_with_no_dependents_warns(self, tmp_path):
        self._state_core(tmp_path, "demo.core", self._STANDALONE)
        passes, warns = self._run(tmp_path)
        assert not passes
        assert len(warns) == 1
        assert "declares a 'default:' Target" in warns[0]
        assert "no other .core depends on it" in warns[0]

    def test_default_on_a_depended_on_core_is_left_alone(self, tmp_path):
        """FuseSoC builds a dependency core through `default`; without one it
        contributes ZERO filesets, silently. That `default` is load-bearing."""
        self._state_core(tmp_path, "demo.core", self._STANDALONE)
        self._state_core(
            tmp_path,
            "top.core",
            """\
CAPI=2:
name: ::top:0
filesets:
  rtl: {files: [dut.v], file_type: verilogSource, depend: ["::demo"]}
targets:
  sim_top:
    filesets: [rtl]
    flow: sim
    flow_options: {tool: verilator}
    toplevel: tb
""",
        )
        passes, warns = self._run(tmp_path)
        assert warns == []
        assert passes

    def test_core_without_a_default_passes(self, tmp_path):
        self._state_core(
            tmp_path,
            "demo.core",
            self._STANDALONE.replace("  default:\n    filesets: [rtl]\n", ""),
        )
        passes, warns = self._run(tmp_path)
        assert warns == []
        assert passes

    def test_vendored_core_is_exempt(self, tmp_path):
        """Same core, outside the state zone and unwired → not the project's to edit."""
        (tmp_path / "demo.core").write_text(self._STANDALONE, encoding="utf-8")
        _, warns = self._run(tmp_path)
        assert warns == []


class TestVeribleLintGate:
    """ADR 0033: the Verible check requires a lint Target selecting Verible."""

    _CORE = """\
CAPI=2:
name: ::x:0
filesets:
  rtl: {files: [i2c.v], file_type: verilogSource}
targets:
  lint:
    filesets: [rtl]
    flow: lint
    flow_options: {tool: verilator}
    toplevel: top
  lint_style:
    filesets: [rtl]
    flow: lint
    flow_options: {tool: %s}
    toplevel: top
"""

    def _project(self, tmp_path, eda_tool: str) -> doctor.ProjectAudit:
        (tmp_path / "i2c.v").write_text("module i2c; endmodule\n", encoding="utf-8")
        (tmp_path / "design.core").write_text(self._CORE % eda_tool, encoding="utf-8")
        (tmp_path / ".booley_project").mkdir(exist_ok=True)
        return doctor.ProjectAudit(
            project_root=tmp_path,
            project_dir=tmp_path / ".booley_project",
            booley_toml={},
            configs_toml={},
            first_target="lint",
        )

    def test_verible_target_enables_check(self, tmp_path):
        assert doctor._project_declares_verible_lint(self._project(tmp_path, "verible"))

    def test_verilator_only_project_is_never_nagged(self, tmp_path):
        assert not doctor._project_declares_verible_lint(self._project(tmp_path, "verilator"))

    def test_no_project_no_check(self):
        assert not doctor._project_declares_verible_lint(None)

    def test_verible_sim_target_does_not_count(self, tmp_path):
        """Only flow: lint Targets gate the check (EDA tool alone is not intent)."""
        (tmp_path / "i2c.v").write_text("module i2c; endmodule\n", encoding="utf-8")
        (tmp_path / "design.core").write_text(
            self._CORE.replace(
                "flow: lint\n    flow_options: {tool: %s}",
                "flow: sim\n    flow_options: {tool: %s}",
            )
            % "verible",
            encoding="utf-8",
        )
        (tmp_path / ".booley_project").mkdir(exist_ok=True)
        project = doctor.ProjectAudit(
            project_root=tmp_path,
            project_dir=tmp_path / ".booley_project",
            booley_toml={},
            configs_toml={},
            first_target="lint",
        )
        assert not doctor._project_declares_verible_lint(project)


class TestCocotbFactoryTests:
    """B6: TestFactory-generated names cannot be grepped for as `def <name>(`.

    ``TestFactory(...).generate_tests()`` registers run_test_001… in the module
    namespace at import time, so the static check is structurally blind to them
    and reported all 14/14 of taxi's tests as missing. Not verifiable ⇒ report
    it as not verifiable, not as a finding.
    """

    _FACTORY_TB = """\
import cocotb
from cocotb.regression import TestFactory


async def run_test(dut, data_width=8):
    pass


factory = TestFactory(run_test)
factory.add_option("data_width", [8, 16])
factory.generate_tests()
"""

    def test_factory_generated_names_are_reported_unverifiable_not_missing(
        self,
        tmp_path: Path,
    ):
        _write_cocotb_project(
            tmp_path,
            tb_body=self._FACTORY_TB,
            tests=["run_test_001", "run_test_002"],
        )
        rec = _audit(tmp_path)
        assert not any("not found as functions" in m for lvl, m in rec.events)
        assert any(
            "cannot be verified statically" in m and "factory" in m
            for lvl, m in rec.events
            if lvl == "skip"
        )
        assert rec.fails() == []

    def test_hand_written_names_still_warn_when_absent(self, tmp_path: Path):
        # The softening is gated on the factory call: a module WITHOUT one that
        # names a test it does not define is still a real, greppable mismatch.
        _write_cocotb_project(
            tmp_path,
            tb_body="import cocotb\n\n\nasync def test_reset(dut):\n    pass\n",
            tests=["test_reset", "test_typo"],
        )
        rec = _audit(tmp_path)
        warns = [m for lvl, m in rec.events if lvl == "warn"]
        assert any("not found as functions" in m and "test_typo" in m for m in warns)
        assert not any("test_reset" in m for m in warns)

    def test_all_names_present_still_passes(self, tmp_path: Path):
        _write_cocotb_project(
            tmp_path,
            tb_body="import cocotb\n\n\nasync def test_reset(dut):\n    pass\n",
            tests=["test_reset"],
        )
        rec = _audit(tmp_path)
        assert any("tests.toml names all present" in m for lvl, m in rec.events if lvl == "pass")


class TestDeepLintFindingsAreNotToolFailure:
    """B3: `doctor --deep`'s lint smoke asked "did lint exit 0?", but lint exits
    1 on a WARN verdict (findings). Any project with a single lint warning could
    therefore never pass --deep, with a FAIL block that literally embedded
    "RESULT: WARN (58 warnings)" — a statement that the flow works."""

    @staticmethod
    def _result(returncode: int):
        return subprocess.CompletedProcess(
            args=["lint"],
            returncode=returncode,
            stdout="RESULT: WARN (58 warnings)\n",
            stderr="",
        )

    def test_lint_exit_1_is_findings_not_breakage(self):
        assert doctor._is_lint_findings_exit("lint", self._result(1))

    def test_lint_exit_1_with_fatal_report_is_breakage(self):
        result = subprocess.CompletedProcess(
            args=["lint"],
            returncode=1,
            stdout="RESULT: FAIL — lint_eth: %Error: undefined variable\n",
            stderr="",
        )
        assert not doctor._is_lint_findings_exit("lint", result)

    def test_lint_exit_1_without_a_verdict_is_breakage(self):
        result = subprocess.CompletedProcess(
            args=["lint"],
            returncode=1,
            stdout="",
            stderr="%Error: failed to elaborate\n",
        )
        assert not doctor._is_lint_findings_exit("lint", result)

    def test_lint_exit_2_is_still_breakage(self):
        # EXIT_ERROR: invalid backend, unresolvable config, --parse_fatal syntax
        # error, unhandled exception. The flow does NOT work — keep failing.
        assert not doctor._is_lint_findings_exit("lint", self._result(2))

    def test_lint_exit_0_is_not_a_findings_exit(self):
        assert not doctor._is_lint_findings_exit("lint", self._result(0))

    def test_simulate_exit_1_is_not_downgraded(self):
        # simulate's exit 1 also covers elab_error — a genuine setup break that
        # the deep smoke exists to catch. The carve-out is lint-only.
        assert not doctor._is_lint_findings_exit("sim", self._result(1))
        assert not doctor._is_lint_findings_exit("synth", self._result(1))


class TestReporterAcceptsFixHints:
    """The original crash: `_check_cocotb_targets` hands `_warn` a fix hint, but
    `_Reporter.warn_` took only a message -> TypeError, aborting `doctor` on any
    cocotb project whose tests.toml names are factory-generated."""

    def test_warn_accepts_a_fix_hint(self, capsys):
        reporter = doctor._Reporter.create()
        reporter.warn_(doctor.warning("test.example", "something is off"), "do this to fix it")
        out = capsys.readouterr().out
        assert "something is off" in out
        assert "do this to fix it" in out
        assert reporter.counts["warn"] == 1

    def test_warn_without_a_fix_hint_prints_no_empty_fix_line(self, capsys):
        reporter = doctor._Reporter.create()
        reporter.warn_(doctor.warning("test.example", "just a warning"))
        assert "fix:" not in capsys.readouterr().out

    def test_plain_warning_without_stable_id_is_rejected(self):
        reporter = doctor._Reporter.create()
        with pytest.raises(TypeError, match="stable check ID"):
            reporter.warn_("just a warning")


class TestDisplayReportDir:
    """Deep-check FAIL hints must be readable where the user reads them.

    Doctor may run inside the Session Runtime, where the project dir is the
    /booley-project bind mount; printing that verbatim sent a host-side user
    ls-ing a path that only exists in the container (the real files sit at
    <repo>/.booley_project/tmp/...).
    """

    def test_container_mount_is_rendered_repo_relative(self):
        from types import SimpleNamespace

        from booley.harness.devcontainer import PROJECT_DIR_TARGET

        project = SimpleNamespace(project_dir=Path(PROJECT_DIR_TARGET))
        report_dir = Path(PROJECT_DIR_TARGET) / "tmp" / "doctor" / "flow-reports"
        hint = doctor._display_report_dir(project, report_dir)
        assert hint == ".booley_project/tmp/doctor/flow-reports (under the repo root)"

    def test_host_path_is_printed_verbatim(self, tmp_path: Path):
        from types import SimpleNamespace

        project = SimpleNamespace(project_dir=tmp_path / ".booley_project")
        report_dir = tmp_path / ".booley_project" / "tmp" / "doctor" / "flow-reports"
        assert doctor._display_report_dir(project, report_dir) == str(report_dir)

    def test_project_dir_override_is_not_rewritten(self, tmp_path: Path):
        # An explicit [project].dir override is a real host path — rewriting it
        # repo-relative would point at files that are not there.
        from types import SimpleNamespace

        project = SimpleNamespace(project_dir=tmp_path / "elsewhere")
        report_dir = tmp_path / "elsewhere" / "tmp" / "doctor" / "flow-reports"
        assert doctor._display_report_dir(project, report_dir) == str(report_dir)


class TestLineEndingsCheck:
    """`booley init` fixes CRLF checkouts, but a git config reset or a fresh
    Windows clone can drift back — doctor re-asks every run."""

    @staticmethod
    def _repo(root: Path, *, autocrlf: str) -> None:
        subprocess.run(["git", "init", "-q", str(root)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "core.autocrlf", autocrlf],
            capture_output=True,
            check=True,
        )

    @staticmethod
    def _commit(root: Path, name: str, data: bytes) -> None:
        (root / name).write_bytes(data)
        subprocess.run(
            ["git", "-C", str(root), "add", "-f", name], capture_output=True, check=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                "commit",
                "-qm",
                "c",
            ],
            capture_output=True,
            check=True,
        )

    def test_lf_tree_with_autocrlf_off_passes(self, tmp_path: Path):
        self._repo(tmp_path, autocrlf="false")
        self._commit(tmp_path, "a.v", b"module a;\nendmodule\n")
        c = _Collector()

        doctor._check_line_endings(tmp_path, c._pass, c._warn, c._skip, c._fail)

        assert c.passed and not c.warned and not c.failed

    def test_crlf_tree_fails_with_the_fix_command(self, tmp_path: Path):
        # Ticket Mode is broken right now: the container reads every one of
        # these as modified.
        self._repo(tmp_path, autocrlf="true")
        self._commit(tmp_path, "a.v", b"module a;\nendmodule\n")
        (tmp_path / "a.v").unlink()
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "--", "a.v"],
            capture_output=True,
            check=True,
        )
        c = _Collector()

        doctor._check_line_endings(tmp_path, c._pass, c._warn, c._skip, c._fail)

        assert len(c.failed) == 1
        assert "--fix-line-endings" in c.failed[0][1]

    def test_autocrlf_true_with_lf_tree_warns(self, tmp_path: Path):
        # Nothing is broken yet — the next checkout is what breaks it.
        self._repo(tmp_path, autocrlf="true")
        c = _Collector()

        doctor._check_line_endings(tmp_path, c._pass, c._warn, c._skip, c._fail)

        assert c.warned and not c.failed

    def test_minus_text_payload_is_not_a_finding(self, tmp_path: Path):
        # A `*.bat -text` file is stored CRLF and checked out CRLF deliberately:
        # byte-identical to the index, so it is clean in the container too.
        # Flagging it would be the false positive that hit taxi.
        self._repo(tmp_path, autocrlf="false")
        self._commit(tmp_path, ".gitattributes", b"* text eol=lf\n*.bat -text\n")
        self._commit(tmp_path, "run.bat", b"@echo off\r\n")
        c = _Collector()

        doctor._check_line_endings(tmp_path, c._pass, c._warn, c._skip, c._fail)

        assert c.passed and not c.warned and not c.failed

    def test_missing_gitattributes_rule_alone_is_silent(self, tmp_path: Path):
        # Most vendored upstreams (the pristine picorv32 among them) will never
        # carry the rule, and on an LF host it changes nothing. Warning here
        # would be unfollowable advice on every Linux project.
        self._repo(tmp_path, autocrlf="false")
        self._commit(tmp_path, "a.v", b"module a;\nendmodule\n")
        c = _Collector()

        doctor._check_line_endings(tmp_path, c._pass, c._warn, c._skip, c._fail)

        assert not c.warned

    def test_not_a_git_repo_skips(self, tmp_path: Path):
        c = _Collector()

        doctor._check_line_endings(tmp_path, c._pass, c._warn, c._skip, c._fail)

        assert c.skipped and not c.failed


# ===========================================================================
# _check_subscription_creds_health — expired-creds gap (2026-07-23 incident)
# ===========================================================================


class TestSubscriptionCredsHealth:
    """A PRESENT-but-DEAD subscription login must not sail through green.

    Regression: the container's .credentials.json sat wedged at expiresAt: 0
    (and the seed sidecar was inode-pinned to a pre-refresh snapshot), every
    ticket crashed at launch — and doctor, checking existence only, was green.
    """

    _EXPIRED = json.dumps({"claudeAiOauth": {"expiresAt": 0}})
    _VALID = json.dumps({"claudeAiOauth": {"expiresAt": 4102444800000}})  # 2100-01-01

    @pytest.fixture
    def creds_home(self, tmp_path, monkeypatch):
        """Isolated HOME/XDG with Claude detected and no auth env leaking in."""
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
        for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(doctor, "_detect_claude_code", lambda: True)
        _set_venue(monkeypatch, False)
        return home

    def _run(self, provider: str = auth_token.APP_CLAUDE) -> _Rec:
        rec = _Rec()
        doctor._check_subscription_creds_health(provider, rec.p, rec.w)
        return rec

    def test_codex_project_skips_the_claude_only_check(self, creds_home):
        # Claude's expiry field has no Codex counterpart; a codex project must
        # not be judged by a login it never uses.
        (creds_home / ".claude" / ".credentials.json").write_text(self._EXPIRED)
        assert self._run(auth_token.APP_CODEX).events == []

    def test_expired_login_without_rotation_free_token_warns(self, creds_home):
        (creds_home / ".claude" / ".credentials.json").write_text(self._EXPIRED)
        rec = self._run()
        assert rec.kinds() == {"warn"}
        assert "expired" in rec.events[0][1]

    def test_expired_login_with_stored_token_is_harmless(self, creds_home):
        (creds_home / ".claude" / ".credentials.json").write_text(self._EXPIRED)
        auth_token.store_token("sk-ant-oat01-x")
        rec = self._run()
        assert rec.kinds() == {"pass"}
        assert "harmless" in rec.events[0][1]

    def test_valid_login_passes_with_expiry_date(self, creds_home):
        (creds_home / ".claude" / ".credentials.json").write_text(self._VALID)
        rec = self._run()
        assert rec.kinds() == {"pass"}
        assert "valid until" in rec.events[0][1]

    def test_absent_or_unparseable_login_is_silent(self, creds_home):
        assert self._run().events == []  # absent
        (creds_home / ".claude" / ".credentials.json").write_text("not json")
        assert self._run().events == []  # unparseable: presence checks own it

    def test_expired_seed_sidecar_warns_in_container(self, creds_home, monkeypatch):
        _set_venue(monkeypatch, True)
        seed = creds_home / ".claude-creds-seed.json"
        seed.write_text(self._EXPIRED)
        monkeypatch.setitem(dc._APP_CREDS_SEED_TARGET, auth_token.APP_CLAUDE, str(seed))
        rec = self._run()
        assert "warn" in rec.kinds()
        assert any("pinned" in m for _, m in rec.events)

    def test_expired_seed_with_stored_token_is_quiet(self, creds_home, monkeypatch):
        _set_venue(monkeypatch, True)
        seed = creds_home / ".claude-creds-seed.json"
        seed.write_text(self._EXPIRED)
        monkeypatch.setitem(dc._APP_CREDS_SEED_TARGET, auth_token.APP_CLAUDE, str(seed))
        auth_token.store_token("sk-ant-oat01-x")
        rec = self._run()
        assert "warn" not in rec.kinds()


class TestNoDockerSkipReason:
    """fpu F-19: in-container, `[--] container checks skipped - runtime or
    sandbox image not available` reads as a fault. It isn't — the sandbox has
    no nested Docker on purpose, and these checks probe the HOST's images."""

    def test_in_container_says_not_applicable(self, monkeypatch):
        _set_venue(monkeypatch, True)

        msg = doctor._no_docker_skip_reason()

        assert "already inside the Session Runtime" in msg
        assert "nested Docker" in msg
        assert "not available" not in msg  # the old "something's broken" phrasing

    def test_on_host_says_no_runtime_found(self, monkeypatch):
        _set_venue(monkeypatch, False)

        msg = doctor._no_docker_skip_reason()

        assert "no Docker/Podman runtime found" in msg
        assert "Session Runtime" not in msg

    def test_container_checks_use_the_reason(self, monkeypatch, capsys):
        from booley.harness import web_isolation

        _set_venue(monkeypatch, True)
        monkeypatch.setattr(web_isolation, "policy_error", lambda: None)
        rec = _Rec()

        doctor._run_container_checks(
            None, None, "booley-sandbox", False, rec.p, rec.w, rec.s, rec.f
        )

        assert any(
            "already inside the Session Runtime" in m for lvl, m in rec.events if lvl == "skip"
        )
        assert any(
            "provider-side web access disabled" in m for lvl, m in rec.events if lvl == "pass"
        )


class TestSynthHeavyTargetCalibration:
    def test_host_deep_container_uses_configured_memory_limit(self, tmp_path):
        project = _adr28_project(
            tmp_path,
            booley_toml={"sandbox": {"memory": "24g"}},
        )
        cmd = doctor._flow_command(
            project,
            "synth",
            "asic_full",
            dry_run=False,
            use_docker=True,
            docker_exe="docker",
            image="booley-sandbox",
        )
        assert cmd[cmd.index("--memory") + 1] == "24g"

    def test_multi_target_matrix_uses_explicit_heaviest_target(self, tmp_path):
        project = _adr28_project(
            tmp_path,
            booley_toml={
                "flows": {
                    "synth": {
                        "default_target": "asic_small,asic_full",
                        "calibration_target": "asic_full",
                    }
                }
            },
        )
        rec = _Rec()
        assert doctor._check_synth_calibration_target(project, rec.p, rec.w, rec.f)
        assert doctor._probe_target(project, "synth") == "asic_full"
        assert rec.kinds() == {"pass"}

    def test_multi_target_matrix_warns_when_heaviest_is_unreviewed(self, tmp_path):
        project = _adr28_project(
            tmp_path,
            booley_toml={"flows": {"synth": {"default_target": "asic_small,asic_full"}}},
        )
        rec = _Rec()
        assert not doctor._check_synth_calibration_target(project, rec.p, rec.w, rec.f)
        assert doctor._probe_target(project, "synth") is None
        assert rec.kinds() == {"warn"}
        assert "no heaviest calibration Target" in rec.events[0][1]

    def test_measured_heavy_peak_overrides_undersized_reservation(self, tmp_path, monkeypatch):
        from booley.harness import synth_probe

        _set_venue(monkeypatch, False)
        project = _adr28_project(
            tmp_path,
            booley_toml={
                "sandbox": {"memory": "22g"},
                "jobs": {"heavy_memory": "16g", "max_tickets": 2},
            },
        )
        synth_probe.record_measurement(project.project_dir, "asic_full", 15.8 * 1024)
        rec = _Rec()
        doctor._check_memory_invariant(project, rec.p, rec.w, rec.s)
        # 15.8 GiB + 15%, rounded up = 19 GiB; + 2x1 GiB agents + 2 GiB
        # headroom requires 23 GiB, so the nominal 22 GiB container is unsafe.
        assert rec.kinds() == {"warn"}
        assert "1x19g + 2x1g + 2g = 23g" in rec.events[0][1]
        assert "measured on asic_full" in rec.events[0][1]

    def test_calibration_for_previous_heaviest_target_is_stale(self, tmp_path, monkeypatch):
        from booley.harness import synth_probe

        _set_venue(monkeypatch, False)
        project = _adr28_project(
            tmp_path,
            booley_toml={
                "sandbox": {"memory": "32g"},
                "flows": {
                    "synth": {
                        "default_target": "asic_small,asic_full",
                        "calibration_target": "asic_full",
                    }
                },
            },
        )
        synth_probe.record_measurement(project.project_dir, "asic_small", 7 * 1024)
        rec = _Rec()

        doctor._check_memory_invariant(project, rec.p, rec.w, rec.s)

        assert rec.kinds() == {"warn"}
        assert "not the current heaviest Target 'asic_full'" in rec.events[0][1]
