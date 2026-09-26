"""Tests for booley doctor setup audit."""

from __future__ import annotations

import argparse
import ast
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

from booley import __version__
from booley.audit import (
    config_common,
    design_size,
    host_environment,
    project_schema,
    resource_policy,
)
from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity
from booley.fusesoc import fusesoc_registry, selftest_overlay, target_inspection
from booley.harness import developer_probe, doctor, doctor_stamp, host_diagnostics
from booley.runtime import (
    auth_token,
    runtime_context,
    session_issuance,
    session_runtime,
)
from booley.runtime import devcontainer as dc
from booley.runtime.project_dir import reset_cache
from booley.targets.catalog import TargetCatalog
from tests.diagnostic_helpers import (
    _issued_runtime_state,
    _Rec,
)


def test_doctor_inputs_use_condition_selected_target_sources(tmp_path: Path) -> None:
    (tmp_path / "conditional.core").write_text(
        "CAPI=2:\n"
        "name: acme:ip:conditional:1.0\n"
        "filesets:\n"
        "  sources:\n"
        "    files:\n"
        "      - tool_verilator ? (rtl/selected.sv)\n"
        "      - tool_icarus ? (rtl/inactive.sv)\n"
        "      - tool_verilator ? (tb/selected.sv): {tags: [tb]}\n"
        "targets:\n"
        "  sim:\n"
        "    flow: sim\n"
        "    flow_options: {tool: verilator}\n"
        "    filesets: [sources]\n"
        "    toplevel: selected\n",
        encoding="utf-8",
    )
    catalog = TargetCatalog.build(tmp_path)
    refs = {handle.name: handle for handle in catalog.list()}

    sources = doctor._CoreAuditInputs(catalog, refs).sources_for("sim")

    assert sources.rtl_source_files == ("rtl/selected.sv",)
    assert sources.tb_files == ("tb/selected.sv",)


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

[flows.lint]

[flows.synth]

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
        "    flow_options: {tool: verilator, booley: {doctor: [sim]}}\n"
        "    filesets: [rtl, tb]\n"
        "    toplevel: tb\n"
        "  lint_fast:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator, booley: {doctor: [lint]}}\n"
        "    filesets: [rtl]\n"
        "    toplevel: dut\n"
        "  synth_fast:\n"
        "    flow: generic\n"
        "    flow_options: {tool: yosys, arch: xilinx, booley: {doctor: [synth]}}\n"
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
    from booley.runtime import devcontainer as dc

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


def _patch_bootstrap_current(monkeypatch) -> None:
    report = DiagnosticReport((DiagnosticFinding(Severity.PASS, "Host Bootstrap docker: ok"),))
    monkeypatch.setattr(
        host_diagnostics,
        "inspect_host",
        lambda: host_diagnostics.HostDiagnosticResult(report, "docker"),
    )


def _patch_host_environment(monkeypatch, root: Path) -> None:
    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    # The suite itself may run under an agent CLI (CLAUDECODE, CODEX_HOME, ...);
    # a plain host shell is the baseline these fixtures model.
    monkeypatch.setattr(runtime_context, "agent_session_app", lambda: None)
    monkeypatch.setattr(Path, "home", lambda: _write_skills_home(root))
    runtime = "doc" + "ker"
    monkeypatch.setattr(doctor.shutil, "which", lambda name: runtime if name == runtime else None)
    _patch_bootstrap_current(monkeypatch)


def _patch_environment(
    monkeypatch,
    root: Path,
    project_dir: Path,
    *,
    mcp_tools: list[str] | None = None,
    mcp_payload: dict | None = None,
    runtime_booley_version: str = __version__,
) -> list[list[str]]:
    reset_cache()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))
    # Doctor tests exercise the host-side orchestration path deterministically,
    # whatever machine the suite itself runs on.
    _patch_host_environment(monkeypatch, root)
    monkeypatch.setattr(doctor.idk, "image_exists", lambda *_a, **_kw: True)
    monkeypatch.setattr(doctor.idk, "image_id", lambda image: image)

    # Orchestration tests substitute the public issuance interface. Runtime
    # owner tests separately exercise real authority and live-state policy.
    issuance, _spec, _labels, _state = _issued_runtime_state(root)
    issuance = replace(issuance, image=dc.SANDBOX_IMAGE)
    monkeypatch.setattr(session_issuance, "validate", lambda *_args: issuance)
    monkeypatch.setattr(session_issuance, "requested_license", lambda _root: None)
    monkeypatch.setattr(
        doctor.session_runtime,
        "up",
        lambda _root: "booley-session-test",
    )
    # Keep the suite hermetic: the host-clock check (F-5) probes an HTTP Date
    # header over the real network.
    monkeypatch.setattr(
        doctor.image_lifecycle,
        "reconcile",
        lambda _root, _intent: doctor.image_lifecycle.LifecycleResult(
            "booley-sandbox",
            "sha256:fixture",
            doctor.image_lifecycle.Status.CURRENT,
        ),
    )

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: PLR0911,PLR0912 — external-command boundary fixture
        calls.append([str(part) for part in cmd])
        if cmd[:2] == ["git", "-C"] and "--show-toplevel" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{root}\n", stderr="")
        if cmd[:2] == ["git", "-C"] and "config" in cmd and "core.autocrlf" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="false\n", stderr="")
        if cmd[:2] == ["git", "-C"] and "ls-files" in cmd and "--eol" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "-C"] and "status" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "-C"] and "diff" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
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
        if cmd[1:3] == ["ps", "-aq"]:
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
        if "import booley; print(booley.__version__)" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{runtime_booley_version}\n", stderr=""
            )
        if cmd[-3:] == ["python", "-m", "booley.mcp.probe"]:
            payload = mcp_payload or {
                "tools": mcp_tools
                or [
                    "synth",
                    "bwave",
                    "coverage_analyst",
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
            # selected Targets as resolvable via the marker line it scrapes.
            selected = json.loads(cmd[-1])
            verdicts = json.dumps(
                [{"selector": item["selector"], "ok": True} for item in selected]
            )
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"[[CORE_RESOLVE_JSON]]{verdicts}\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    return calls


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
    tool_calls = [call for call in calls if "booley.flows.sim" in call]
    assert tool_calls
    assert tool_calls[0][:2] == ["doc" + "ker", "exec"]
    assert "booley-session-test" in tool_calls[0]
    assert "--dry-run" in tool_calls[0]
    assert "--target" in tool_calls[0]
    assert tool_calls[0][tool_calls[0].index("--target") + 1] == "sim_fast"


def test_doctor_passes_interactive_checks_when_seeded(tmp_path, monkeypatch, capsys):
    project_dir = _write_project(tmp_path)  # seeds devcontainer + exclude
    _patch_environment(monkeypatch, tmp_path, project_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 0
    assert "devcontainer.json present and structurally current" in output
    assert "Booley files excluded from git" in output


def test_git_exclude_warnings_are_scoped_per_missing_entry(tmp_path):
    _git_init(tmp_path)
    (tmp_path / ".git" / "info" / "exclude").write_text("/.devcontainer\n", encoding="utf-8")
    findings: list[doctor.DoctorWarning] = []

    doctor._check_devcontainer_excludes(tmp_path, lambda _msg: None, findings.append)

    assert len(findings) == 1
    assert findings[0].check_id == "project.git-excludes-missing"
    assert findings[0].subject == ".booley_project"


@pytest.mark.parametrize("concise", [False, True])
def test_doctor_clean_run_records_freshness_stamp(tmp_path, monkeypatch, capsys, concise):
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

    rc = doctor.run_doctor(
        argparse.Namespace(verbose=False, deep=False, concise=concise),
        tmp_path,
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert ("PASS  " not in output) is concise
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
        mcp_tools=["bwave", "lint", "sim"],
    )

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    assert rc == 1
    assert doctor_stamp.load_stamp(project_dir) is None


def test_doctor_fails_when_issued_runtime_has_different_booley_version(
    tmp_path,
    monkeypatch,
    capsys,
):
    project_dir = _write_project(tmp_path)
    _patch_environment(
        monkeypatch,
        tmp_path,
        project_dir,
        runtime_booley_version="9.9.9",
    )

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert f"host Booley {__version__} != Sandbox Booley 9.9.9" in output
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
    # Configuration currency warns; the independent host issuance check fails.
    assert rc == 1
    assert "Sandbox host issuance is invalid" in output
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
        mcp_tools=["bwave", "lint", "sim"],
    )

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert "MCP missing required endpoint(s): synth" in output


def test_doctor_runs_supported_mcp_probe_entrypoint(tmp_path: Path) -> None:
    project_dir = tmp_path / ".booley_project"
    project = doctor.ProjectAudit(tmp_path, project_dir, {}, {}, "sim")

    command = doctor._mcp_probe_command(project, "docker", "booley:test")

    assert command[-3:] == ["python", "-m", "booley.mcp.probe"]
    assert "-c" not in command


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
    # Both the shallow dry-run and the deep check run in the Sandbox.
    sim_calls = [call for call in calls if "booley.flows.sim" in call]
    assert len(sim_calls) == 2
    dry_call, deep_call = sim_calls
    assert dry_call[:3] == ["doc" + "ker", "exec", "-e"]
    assert "booley-session-test" in dry_call
    assert "--dry-run" in dry_call
    assert deep_call[:3] == ["doc" + "ker", "exec", "-e"]
    assert "booley-session-test" in deep_call
    assert "--dry-run" not in deep_call
    assert deep_call[deep_call.index("--target") + 1] == "sim_fast"


def test_doctor_deep_surfaces_synthesis_warning_verdict(
    tmp_path,
    monkeypatch,
    capsys,
):
    project_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, project_dir)
    monkeypatch.setattr(doctor, "_synth_deep_report_error", lambda *args: "")
    base_run = doctor.subprocess.run

    def run_with_synth_warning(cmd, **kwargs):
        if "booley.flows.synth" in cmd and "--dry-run" not in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="RESULT: WARN -- timing VIOLATED (hold slack -0.182 ns)\n",
                stderr="",
            )
        return base_run(cmd, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "run", run_with_synth_warning)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=True), tmp_path)

    output = capsys.readouterr().out
    assert rc == 0
    assert "WARN  synth deep check [synth_fast] returned a WARN verdict" in output
    assert doctor_stamp.load_stamp(project_dir) is None


def test_doctor_skip_agent_checks_omits_credentials_and_live_probe(
    tmp_path,
    monkeypatch,
    capsys,
):
    project_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, project_dir)
    monkeypatch.setattr(doctor, "_synth_deep_report_error", lambda *args: "")

    def unexpected_agent_check(*_args, **_kwargs):
        raise AssertionError("agent check should have been skipped")

    monkeypatch.setattr(doctor, "_check_agent_auth_token", unexpected_agent_check)
    monkeypatch.setattr(doctor, "_check_oauth_token", unexpected_agent_check)
    monkeypatch.setattr(doctor, "_check_subscription_creds_health", unexpected_agent_check)
    monkeypatch.setattr(doctor, "_check_agent_backend_health", unexpected_agent_check)
    monkeypatch.setattr(doctor, "_run_developer_probe", unexpected_agent_check)

    rc = doctor.run_doctor(
        argparse.Namespace(verbose=False, deep=True, skip_agent_checks=True),
        tmp_path,
    )

    output = capsys.readouterr().out
    assert rc == 0
    assert "agent credential checks skipped by --skip-agent-checks" in output
    assert "worker backend health check skipped by --skip-agent-checks" in output
    assert "developer authorization probe skipped by --skip-agent-checks" in output
    assert doctor_stamp.load_stamp(project_dir) is None


# ---------------------------------------------------------------------------
# Sandbox routing for Doctor Flow checks.
# ---------------------------------------------------------------------------


def _tool_check_harness(tmp_path, monkeypatch, fake_run):
    """Drive ``_run_flow_check`` directly: stub subprocess + Target probing.

    Returns ``(project, calls)`` — *calls* records every argv *fake_run* saw.
    """
    (tmp_path / ".booley_project").mkdir(exist_ok=True)
    project = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=tmp_path / ".booley_project",
        booley_toml={"flows": {"sim": {}}},
        configs_toml={"fast": {"defines": [], "tb_top": "tb", "tests": ["smoke"]}},
        first_target="fast",
    )
    monkeypatch.setattr(doctor.session_runtime, "up", lambda _root: "booley-session-test")

    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append([str(part) for part in cmd])
        return fake_run(cmd)

    monkeypatch.setattr(doctor.subprocess, "run", run)
    return project, calls


def _run_flow_check(project, rec: _Rec, *, dry_run: bool) -> None:
    doctor._run_flow_check(
        project,
        "sim",
        target="fast",
        dry_run=dry_run,
        flow_runtime=doctor._DoctorFlowRuntime(project.project_root, "doc" + "ker"),
        timeout_s=10,
        verbose=False,
        _pass=rec.p,
        _warn=rec.w,
        _skip=rec.s,
        _fail=rec.f,
    )


def test_doctor_flow_runtime_starts_once_and_reuses_container(tmp_path, monkeypatch):
    _set_venue(monkeypatch, False)
    starts = []

    def up(root):
        starts.append(root)
        return "booley-session-test"

    monkeypatch.setattr(doctor.session_runtime, "up", up)
    runtime = doctor._DoctorFlowRuntime(tmp_path, "docker")

    first = runtime.command(["python3", "-V"])
    second = runtime.command(["booley", "doctor"])

    assert starts == [tmp_path]
    assert "booley-session-test" in first
    assert "booley-session-test" in second


@pytest.mark.parametrize("dry_run", [True, False])
def test_deep_check_routing_truth_table(
    tmp_path,
    monkeypatch,
    dry_run,
):
    """Dry and deep Flow checks both run in the Sandbox."""
    project, calls = _tool_check_harness(
        tmp_path,
        monkeypatch,
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr=""),
    )
    rec = _Rec()

    _run_flow_check(project, rec, dry_run=dry_run)

    assert rec.kinds() == {"pass"}
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:3] == ["doc" + "ker", "exec", "-e"]
    assert "booley-session-test" in cmd
    assert "booley.flows.sim" in cmd
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


def test_session_runtime_startup_failure_fails_loudly(tmp_path, monkeypatch):
    project, _calls = _tool_check_harness(
        tmp_path,
        monkeypatch,
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        doctor.session_runtime,
        "up",
        lambda _root: (_ for _ in ()).throw(doctor.session_runtime.SessionError("bad issuance")),
    )
    rec = _Rec()

    _run_flow_check(project, rec, dry_run=False)

    assert rec.kinds() == {"fail"}
    assert "could not enter the Sandbox" in rec.fails()[0]
    assert "bad issuance" in rec.fails()[0]


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

    _run_flow_check(project, rec, dry_run=False)

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
        {
            "sim": fusesoc_registry.TargetRef(
                name="sim",
                vlnv="::sim:0",
                core_file=tmp_path / "sim.core",
            )
        },
        rec.p,
        rec.f,
    )

    assert rec.kinds() == {"fail"}
    assert "executed OUTSIDE it" in rec.fails()[0]


def test_deep_core_resolution_only_runs_doctor_selected_targets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    targets = [
        "  sim_selected:\n"
        "    flow: sim\n"
        "    flow_options: {tool: verilator, booley: {doctor: [sim]}}\n",
        "  fpga_selected:\n"
        "    flow: generic\n"
        "    flow_options: {tool: vivado, booley: {doctor: [fpga]}}\n",
    ]
    targets.extend(
        f"  vendored_{index:03d}:\n    flow: sim\n    flow_options: {{tool: verilator}}\n"
        for index in range(200)
    )
    (tmp_path / "design.core").write_text(
        "CAPI=2:\nname: ::design:0\ntargets:\n" + "".join(targets),
        encoding="utf-8",
    )
    project = doctor.ProjectAudit(tmp_path, project_dir, {}, {}, "")
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(doctor, "_docker_image_exists_by_name", lambda _image: False)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/fusesoc")
    monkeypatch.setattr(
        doctor.fusesoc_registry,
        "resolve_target_handle",
        lambda handle, **_kwargs: calls.append((handle.name, handle.vlnv)),
    )
    rec = _Rec()

    doctor._run_core_resolve_checks(
        project,
        None,
        rec.p,
        rec.s,
        rec.f,
    )

    assert calls == [("fpga_selected", "::design:0"), ("sim_selected", "::design:0")]
    assert rec.kinds() == {"pass"}
    assert len(rec.events) == 2


def test_sandbox_core_resolution_receives_only_canonical_selected_targets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    (tmp_path / "first.core").write_text(
        "CAPI=2:\nname: acme:ip:first:1\ntargets:\n"
        "  smoke:\n"
        "    flow: sim\n"
        "    flow_options: {tool: verilator, booley: {doctor: [sim]}}\n",
        encoding="utf-8",
    )
    (tmp_path / "second.core").write_text(
        "CAPI=2:\nname: acme:ip:second:1\ntargets:\n"
        "  smoke:\n"
        "    flow: generic\n"
        "    flow_options: {tool: yosys, booley: {doctor: [synth]}}\n"
        "  unused:\n"
        "    flow: sim\n"
        "    flow_options: {tool: verilator}\n",
        encoding="utf-8",
    )
    project = doctor.ProjectAudit(tmp_path, project_dir, {}, {}, "")
    seen: list[dict[str, str]] = []

    def fake_run(cmd, **_kwargs):
        payload = json.loads(cmd[-1]) if len(cmd) == 4 else []
        seen.extend(payload)
        verdict = [{"selector": item["selector"], "ok": True} for item in payload]
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=f"[[CORE_RESOLVE_JSON]]{json.dumps(verdict)}\n",
            stderr="",
        )

    monkeypatch.setattr(doctor, "_docker_image_exists_by_name", lambda _image: True)
    monkeypatch.setattr(doctor, "_docker_wrap", lambda _exe, _image, _root, inner: inner)
    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    rec = _Rec()

    doctor._run_core_resolve_checks(project, "docker", rec.p, rec.s, rec.f)

    assert [(item["selector"], item["name"], item["vlnv"]) for item in seen] == [
        ("first#smoke", "smoke", "acme:ip:first:1"),
        ("second#smoke", "smoke", "acme:ip:second:1"),
    ]
    assert rec.kinds() == {"pass"}


def test_selected_target_dependency_resolution_failure_is_a_deep_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    (tmp_path / "design.core").write_text(
        "CAPI=2:\nname: acme:ip:top:1\nfilesets:\n"
        "  rtl: {depend: [acme:ip:missing]}\n"
        "targets:\n"
        "  sim_top:\n"
        "    flow: sim\n"
        "    filesets: [rtl]\n"
        "    flow_options: {tool: verilator, booley: {doctor: [sim]}}\n",
        encoding="utf-8",
    )
    project = doctor.ProjectAudit(tmp_path, project_dir, {}, {}, "")
    monkeypatch.setattr(doctor, "_docker_image_exists_by_name", lambda _image: False)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/fusesoc")

    def fail_resolution(name, **kwargs):
        assert (name, kwargs["vlnv"]) == ("sim_top", "acme:ip:top:1")
        raise fusesoc_registry.TargetResolutionError("missing dependency acme:ip:missing")

    monkeypatch.setattr(doctor.fusesoc_registry, "_resolve_target", fail_resolution)
    rec = _Rec()

    doctor._run_core_resolve_checks(project, None, rec.p, rec.s, rec.f)

    assert rec.kinds() == {"fail"}
    assert "sim_top" in rec.fails()[0]


def test_selected_target_that_no_longer_resolves_fails_before_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    project = doctor.ProjectAudit(tmp_path, project_dir, {}, {}, "")
    matrix = MagicMock(seed_targets=("removed_target",))
    monkeypatch.setattr(doctor, "_project_target_matrix", lambda _project: matrix)
    rec = _Rec()

    doctor._run_core_resolve_checks(project, None, rec.p, rec.s, rec.f)

    assert rec.kinds() == {"fail"}
    assert "removed_target" in rec.fails()[0]


def test_doctor_deep_fails_hard_when_issued_runtime_cannot_start(
    tmp_path,
    monkeypatch,
    capsys,
):
    project_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, project_dir)

    def fail_up(_root):
        raise doctor.session_runtime.SessionError("sandbox image is not built")

    monkeypatch.setattr(doctor.session_runtime, "up", fail_up)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=True), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert "could not enter the Sandbox" in output
    assert "sandbox image is not built" in output


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

    doctor._run_flow_check(
        project,
        "sim",
        target="fast",
        dry_run=False,
        flow_runtime=doctor._DoctorFlowRuntime(project.project_root, None),
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


def test_deep_check_fails_when_issued_runtime_is_invalid(tmp_path, monkeypatch):
    project, _calls = _tool_check_harness(
        tmp_path,
        monkeypatch,
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr=""),
    )

    def fail_up(_root):
        raise doctor.session_runtime.SessionError("issued spec is stale")

    monkeypatch.setattr(doctor.session_runtime, "up", fail_up)
    rec = _Rec()

    doctor._run_flow_check(
        project,
        "sim",
        target="fast",
        dry_run=False,
        flow_runtime=doctor._DoctorFlowRuntime(project.project_root, "doc" + "ker"),
        timeout_s=10,
        verbose=False,
        _pass=rec.p,
        _warn=rec.w,
        _skip=rec.s,
        _fail=rec.f,
    )

    assert rec.kinds() == {"fail"}
    assert "issued spec is stale" in rec.fails()[0]


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


def test_flow_command_uses_first_config_and_first_test(tmp_path, monkeypatch):
    _set_venue(monkeypatch, True)
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
        flow_runtime=doctor._DoctorFlowRuntime(tmp_path, None),
    )

    assert cmd[:3] == [sys.executable, "-m", "booley.flows.sim"]
    assert cmd[cmd.index("--target") + 1] == "fast"
    # tb_top left the surface (ADR 0021) — sourced from the Target, not passed.
    assert "--tb-top" not in cmd
    assert cmd[cmd.index("--test") + 1] == "smoke"


def test_flow_command_passes_internal_selftest_kind_into_session(tmp_path, monkeypatch):
    from booley.fusesoc import selftest_overlay

    project_dir = _write_project(tmp_path)
    project = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=project_dir,
        booley_toml={},
        configs_toml={},
        first_target="sim_core",
    )
    _set_venue(monkeypatch, False)
    monkeypatch.setattr(doctor.session_runtime, "up", lambda _root: "booley-session-test")

    cmd = doctor._flow_command(
        project,
        "sim",
        "sim_core",
        dry_run=False,
        flow_runtime=doctor._DoctorFlowRuntime(tmp_path, "docker"),
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


def test_flow_command_deep_smoke_pins_first_tests_toml_test(tmp_path, monkeypatch):
    """A configs.toml-less (post-ADR-0022) project must still get a pinned
    ``--test``: without one the deep simulate smoke runs the Target's WHOLE
    list and times out on any large design (openc910: 16 full-chip sims)."""
    project = _tests_toml_project_audit(
        tmp_path,
        '[sim_core]\ntests = ["hello_world", "coremark"]\n',
    )
    _set_venue(monkeypatch, True)

    cmd = doctor._flow_command(
        project,
        "sim",
        "sim_core",
        dry_run=False,
        flow_runtime=doctor._DoctorFlowRuntime(tmp_path, None),
    )

    assert cmd[cmd.index("--test") + 1] == "hello_world"


def test_flow_command_deep_smoke_skips_skipped_tests(tmp_path, monkeypatch):
    """The smoke's one pinned test must be runnable: honor the tests.toml
    ``skip`` list so a known-hang or an always-fail selftest fixture at the
    head of the list can't turn the deep check into a guaranteed FAIL."""
    project = _tests_toml_project_audit(
        tmp_path,
        "[sim_core]\n"
        'tests = ["booley_selftest_bad", "hello_world"]\n'
        'skip = ["booley_selftest_bad"]\n',
    )
    _set_venue(monkeypatch, True)

    cmd = doctor._flow_command(
        project,
        "sim",
        "sim_core",
        dry_run=False,
        flow_runtime=doctor._DoctorFlowRuntime(tmp_path, None),
    )

    assert cmd[cmd.index("--test") + 1] == "hello_world"


def test_flow_command_deep_smoke_resolves_vlnv_qualified_target(tmp_path, monkeypatch):
    """ADR 0030 Targets may arrive VLNV-qualified; tests.toml keys are bare."""
    project = _tests_toml_project_audit(
        tmp_path,
        '[sim_core]\ntests = ["hello_world"]\n',
    )
    _set_venue(monkeypatch, True)

    cmd = doctor._flow_command(
        project,
        "sim",
        "acme:ip:c910#sim_core",
        dry_run=False,
        flow_runtime=doctor._DoctorFlowRuntime(tmp_path, None),
    )

    assert cmd[cmd.index("--test") + 1] == "hello_world"


def test_flow_command_deep_smoke_all_skipped_falls_back_to_head(tmp_path, monkeypatch):
    """All-skip misconfig: smoke the declared head rather than nothing
    (mirrors the Simulation Flow's never-run-zero-tests semantics)."""
    project = _tests_toml_project_audit(
        tmp_path,
        '[sim_core]\ntests = ["hello_world"]\nskip = ["hello_world"]\n',
    )
    _set_venue(monkeypatch, True)

    cmd = doctor._flow_command(
        project,
        "sim",
        "sim_core",
        dry_run=False,
        flow_runtime=doctor._DoctorFlowRuntime(tmp_path, None),
    )

    assert cmd[cmd.index("--test") + 1] == "hello_world"


def test_doctor_targets_come_from_core_metadata_and_keep_all(tmp_path):
    """Doctor runs every Target that opts into a Flow; it never picks a first one."""
    (tmp_path / "design.core").write_text(
        "CAPI=2:\n"
        "name: ::design:0\n"
        "targets:\n"
        "  sim_fast:\n"
        "    flow: sim\n"
        "    flow_options: {tool: verilator, booley: {doctor: [sim]}}\n"
        "  sim_full:\n"
        "    flow: sim\n"
        "    flow_options: {tool: icarus, booley: {doctor: [sim]}}\n"
        "  lint_unselected:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator}\n",
        encoding="utf-8",
    )
    project = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=tmp_path / ".booley_project",
        booley_toml={"flows": {}},
        configs_toml={},
        first_target="",
    )

    assert doctor._doctor_targets(project, "sim") == ["sim_fast", "sim_full"]
    assert doctor._doctor_targets(project, "lint") == []
    assert doctor._project_target_matrix(project).seed_targets == ("sim_fast", "sim_full")


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


def test_validate_known_tables_warns_on_unknown_and_retired():
    """Unrecognized top-level booley.toml tables warn (typo/stale); known ones don't."""
    # Every canonical table is silent (derived — a second literal list here
    # would drift exactly like the one that lost [stealth]; see F-17).
    audit = project_schema.audit_known_tables(
        {table: {} for table in project_schema.KNOWN_BOOLEY_TOML_TABLES}
    )
    assert audit.findings == ()

    # A retired table gets the targeted migration hint...
    audit = project_schema.audit_known_tables({"fusesoc": {"target_cores": ["x"]}})
    assert any(
        "[fusesoc]" in item.message and "ADR 0030" in item.message for item in audit.findings
    )

    # ...and an outright typo gets the generic "ignored" warning.
    audit = project_schema.audit_known_tables({"toolz": {}})
    assert any("[toolz]" in item.message and "ignored" in item.message for item in audit.findings)


def test_validate_feedback_table_accepts_live_settings():
    audit = project_schema.audit_feedback_table(
        {
            "feedback": {
                "redact_extra": ["codename"],
                "redact_identifiers": False,
            }
        }
    )

    assert audit.is_valid
    assert any(
        item.severity is config_common.ConfigFindingSeverity.PASS for item in audit.findings
    )


@pytest.mark.parametrize(
    "feedback",
    [
        {"redact_extra": "codename"},
        {"redact_identifiers": "false"},
    ],
)
def test_validate_feedback_table_rejects_invalid_settings(feedback):
    audit = project_schema.audit_feedback_table({"feedback": feedback})

    assert not audit.is_valid
    assert audit.findings


def test_validate_feedback_table_rejects_removed_mode_setting():
    audit = project_schema.audit_feedback_table({"feedback": {"mode": "off"}})

    assert not audit.is_valid
    assert audit.findings[0].message == "booley.toml [feedback].mode was removed and is ignored"
    assert "delete [feedback].mode" in audit.findings[0].fix


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
    audit = project_schema.audit_stealth_table({"stealth": stealth})

    assert audit.is_valid is valid
    assert bool(audit.findings) is not valid


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
    TOML can't inject its keys into this list. This complements the Python
    scanner for configuration still consumed by setup shell helpers.
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
            if table not in project_schema.KNOWN_BOOLEY_TOML_TABLES
        }
        assert unknown == {}, f"live booley.toml tables doctor calls ignored: {unknown}"

    def test_every_table_read_by_a_shell_script_is_recognized(self):
        """Configuration read by setup shell helpers remains recognized."""
        tables = _tables_read_by_shell_scripts()

        unknown = {
            table: where
            for table, where in tables.items()
            if table not in project_schema.KNOWN_BOOLEY_TOML_TABLES
        }
        assert unknown == {}, f"live booley.toml tables doctor calls ignored: {unknown}"

    def test_submodule_table_is_read_by_python_setup(self):
        tables = _tables_read_by_production_code()
        assert "submodules" in tables

    @pytest.mark.parametrize("asic", [True, False])
    @pytest.mark.parametrize("fpga_part", [None, "xc7a35tcpg236-1"])
    def test_scaffolded_booley_toml_is_fully_recognized(self, asic, fpga_part):
        """`booley init --scaffold` must not write a table its own doctor warns about."""
        from booley.harness.setup.scaffold import ScaffoldChoices, _booley_toml

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
        assert emitted <= project_schema.KNOWN_BOOLEY_TOML_TABLES, (
            f"scaffold emits unrecognized tables: "
            f"{emitted - project_schema.KNOWN_BOOLEY_TOML_TABLES}"
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

        assert tables <= project_schema.KNOWN_BOOLEY_TOML_TABLES, (
            f"setup template offers unrecognized tables: "
            f"{tables - project_schema.KNOWN_BOOLEY_TOML_TABLES}"
        )

    def test_known_and_retired_tables_do_not_overlap(self):
        """A table cannot be both live and retired — the retired hint would
        never fire, or the known set would silence a real migration."""
        assert not (
            project_schema.KNOWN_BOOLEY_TOML_TABLES
            & set(project_schema.RETIRED_BOOLEY_TOML_TABLES)
        )


def test_doctor_targets_do_not_fall_back_to_configs_first_target(tmp_path):
    project = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=tmp_path / ".booley_project",
        booley_toml={},
        configs_toml={"fast": {"defines": []}},
        first_target="fast",
    )
    assert doctor._doctor_targets(project, "lint") == []


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
        if cmd[1:3] == ["ps", "-aq"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
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
        if "booley.flows.sim" in cmd:
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
    assert "sim dry-run [sim_fast] skipped - tb_top is resolved at runtime" in output


# ---------------------------------------------------------------------------
# .core audit (ADR 0022 Phases 6-7) — _run_core_audit driven directly
# ---------------------------------------------------------------------------


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
    flow_options:
      tool: verilator
      booley: {doctor: [sim]}
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

    def test_each_root_target_partition_is_read_once(self, tmp_path: Path, monkeypatch):
        core = _CLEAN_SIM_CORE.replace(
            "targets:\n",
            "targets:\n"
            "  lint:\n"
            "    flow: lint\n"
            "    flow_options: {tool: verilator}\n"
            "    filesets: [rtl]\n"
            "    toplevel: dut\n",
        )
        (tmp_path / "design.core").write_text(core, encoding="utf-8")
        (tmp_path / "rtl").mkdir()
        (tmp_path / "tb").mkdir()
        (tmp_path / "rtl" / "dut.sv").write_text(
            "module dut; endmodule\n",
            encoding="utf-8",
        )
        (tmp_path / "tb" / "tb_dut.sv").write_text(
            '$display("[SIM_RESULT] PASSED");\n',
            encoding="utf-8",
        )
        original = doctor.TargetCatalog.inspect
        calls: list[str] = []

        def inspect(self, handle):
            result = original(self, handle)
            calls.append(handle.name)
            return result

        monkeypatch.setattr(doctor.TargetCatalog, "inspect", inspect)
        monkeypatch.setattr(doctor, "_audit_native_dependencies", lambda *_args: None)

        rec = _audit(tmp_path)

        assert rec.fails() == []
        assert sorted(calls) == ["lint", "sim"]

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
    flow_options: {tool: verilator, booley: {doctor: [sim]}}
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
            booley_toml={"flows": {"sim": {}}},
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
            booley_toml={"flows": {"sim": {}}},
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
                "      tool: verilator\n      booley: {doctor: [sim]}",
                "      tool: icarus\n"
                "      iverilog_options: [-g2012]\n"
                "      booley: {doctor: [sim]}",
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
        extension_state=None,
        extension_probe=None,
    ) -> _Rec:
        from booley.runtime import runtime_context
        from booley.runtime.vaporview import ExtensionState

        monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: in_container)
        monkeypatch.setattr(session_runtime, "vscode_session_container", lambda root: container)
        monkeypatch.setattr(doctor, "_wcp_port_listening", probe or (lambda argv, port: listening))
        monkeypatch.setattr(doctor, "_vscode_extension_host_running", lambda argv: attached)
        monkeypatch.setattr(
            doctor,
            "_vaporview_extension_state",
            extension_probe or (lambda argv: extension_state or ExtensionState.INSTALLED),
        )
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

    def test_dark_port_with_missing_extension_leads_with_install(self, tmp_path, monkeypatch):
        from booley.runtime.vaporview import ExtensionState

        rec = self._run(
            tmp_path,
            monkeypatch,
            listening=False,
            extension_state=ExtensionState.MISSING,
        )

        failure = next(m for lvl, m in rec.events if lvl == "fail")
        assert "VaporView extension is not installed" in failure
        assert "Install from VSIX" in failure
        assert "postAttach VaporView patch only takes effect" not in failure

    def test_dark_port_with_unknown_extension_state_reports_uncertainty(
        self, tmp_path, monkeypatch
    ):
        from booley.runtime.vaporview import ExtensionState

        rec = self._run(
            tmp_path,
            monkeypatch,
            listening=False,
            extension_state=ExtensionState.UNKNOWN,
        )

        failure = next(m for lvl, m in rec.events if lvl == "fail")
        assert "could not determine whether VaporView is installed" in failure
        assert "VaporView extension is not installed" not in failure

    def test_listening_port_does_not_probe_extension_state(self, tmp_path, monkeypatch):
        rec = self._run(
            tmp_path,
            monkeypatch,
            listening=True,
            extension_probe=lambda argv: pytest.fail("extension state must not be probed"),
        )

        assert rec.kinds() == {"pass"}

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


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (0, "INSTALLED"),
        (3, "MISSING"),
        (4, "UNKNOWN"),
        (1, "UNKNOWN"),
    ],
)
def test_vaporview_extension_probe_preserves_state_and_prefix(monkeypatch, returncode, expected):
    from booley.runtime.vaporview import ExtensionState

    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", run)

    state = doctor._vaporview_extension_state(["docker", "exec", "session"])

    assert state is getattr(ExtensionState, expected)
    assert seen == [
        ["docker", "exec", "session", "python3", "-c", doctor._VAPORVIEW_STATE_PROBE_SOURCE]
    ]


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
    from booley.fusesoc import fusesoc_registry as fr

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
    from booley.fusesoc import fusesoc_registry as fr

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
        catalog = TargetCatalog.build(core.parent)
        return {name: catalog.select(name)}

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
        catalog = TargetCatalog.build(core.parent)
        refs = {handle.name: handle for handle in catalog.list()}
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
        catalog = TargetCatalog.build(core.parent)
        return {flow: catalog.select(flow)}

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
  sim:
    flow: sim
    flow_options: {booley: {doctor: [sim]}}
    filesets: [tb]
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
    project = _schema_audit(tmp_path, {"flows": {"sim": {}}})
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
  sim:
    flow: sim
    flow_options: {tool: verilator, booley: {doctor: [sim]}}
""",
    )
    project = _schema_audit(tmp_path, {"flows": {"sim": {}}})
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
        "no Doctor Target selects it" in msg for level, msg in rec.events if level == "note"
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
  sim:
    flow: sim
    flow_options: {tool: verilator, booley: {doctor: [sim]}}
    filesets: [rtl]
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
    project = _schema_audit(tmp_path, {"flows": {"sim": {}}})
    rec = _Rec()
    doctor._check_core_setup_hazards(project, tmp_path, rec.p, rec.f, _note=rec.n)
    assert len(rec.fails()) == 1
    assert dep.name in rec.fails()[0]


def test_check_core_schema_vendored_core_is_a_note(tmp_path):
    """The same violation in a core no configured Target selects is a NOTE:
    FuseSoC skips the core at resolve, the configured flows are unaffected, and
    'fix the .core' is unfollowable for a pristine upstream checkout (the
    YosysHQ picorv32.core ships a bare ``depend:``)."""
    _write_core(
        tmp_path,
        _BAD_DEPEND_CORE.replace("    flow_options: {booley: {doctor: [sim]}}\n", ""),
    )
    project = _schema_audit(tmp_path)
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
    from booley.fusesoc import fusesoc_registry as fr

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
    from booley.fusesoc import fusesoc_registry as fr

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


def test_check_doctor_targets_names_available_targets_when_none_selected(tmp_path):
    project = doctor.ProjectAudit(
        project_root=tmp_path,
        project_dir=tmp_path / ".booley_project",
        booley_toml={},
        configs_toml={},
        first_target="",
    )
    (tmp_path / "d.core").write_text(
        "CAPI=2:\nname: ::d:0\ntargets:\n  foo_sim: {flow: sim}\n",
        encoding="utf-8",
    )
    fails: list[tuple[str, str]] = []
    assert not doctor._check_doctor_targets(
        project, "synth", lambda msg, fix="": fails.append((msg, fix))
    )
    assert "foo_sim" in fails[0][0]
    assert "flow_options.booley.doctor" in fails[0][1]


def _fpga_doctor_project(
    root: Path,
    *,
    marked: bool,
    fpga_table: dict[str, object] | None = None,
) -> doctor.ProjectAudit:
    metadata = ", booley: {doctor: [fpga]}" if marked else ""
    (root / "design.core").write_text(
        "CAPI=2:\nname: ::design:0\ntargets:\n"
        "  fpga_board:\n"
        "    flow: generic\n"
        f"    flow_options: {{tool: verilator, part: xc7a35tcsg324-1{metadata}}}\n",
        encoding="utf-8",
    )
    flows: dict[str, object] = {
        "sim": {"enabled": False},
        "lint": {"enabled": False},
        "synth": {"enabled": False},
    }
    if fpga_table is not None:
        flows["fpga"] = fpga_table
    return doctor.ProjectAudit(
        project_root=root,
        project_dir=root / ".booley_project",
        booley_toml={"flows": flows},
        configs_toml={},
        first_target="",
    )


def _run_isolated_flow_audit(project, monkeypatch):
    rec = _Rec()
    calls: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(doctor, "_check_design_size", lambda *args, **kwargs: None)
    monkeypatch.setattr(doctor, "_check_flow_runtime_reality", lambda *args, **kwargs: None)

    def record(_project, flow_name, **kwargs):
        calls.append((flow_name, kwargs["target"], kwargs["dry_run"]))

    monkeypatch.setattr(doctor, "_run_flow_check", record)
    doctor._run_flow_audit(
        project,
        doctor._DoctorFlowRuntime(project.project_root, None),
        False,
        rec.p,
        rec.n,
        rec.w,
        rec.s,
        rec.f,
    )
    return rec, calls


def test_flow_audit_dry_runs_marked_fpga_target_without_flow_table(tmp_path, monkeypatch):
    project = _fpga_doctor_project(tmp_path, marked=True)

    rec, calls = _run_isolated_flow_audit(project, monkeypatch)

    assert calls == [("fpga", "fpga_board", True)]
    assert not rec.fails()


def test_flow_audit_fails_enabled_fpga_table_without_marked_target(tmp_path, monkeypatch):
    project = _fpga_doctor_project(tmp_path, marked=False, fpga_table={})

    rec, calls = _run_isolated_flow_audit(project, monkeypatch)

    assert calls == []
    assert any("fpga has no Doctor Target" in message for message in rec.fails())


def test_flow_audit_skips_explicitly_disabled_marked_fpga_target(tmp_path, monkeypatch):
    project = _fpga_doctor_project(tmp_path, marked=True, fpga_table={"enabled": False})

    rec, calls = _run_isolated_flow_audit(project, monkeypatch)

    assert calls == []
    assert ("skip", "fpga disabled in booley.toml") in rec.events


def test_flow_audit_skips_fpga_when_not_configured_or_marked(tmp_path, monkeypatch):
    project = _fpga_doctor_project(tmp_path, marked=False)

    rec, calls = _run_isolated_flow_audit(project, monkeypatch)

    assert calls == []
    assert any(
        level == "skip" and "fpga not applicable" in message for level, message in rec.events
    )


def test_fpga_runtime_probe_checks_vivado_not_resolution_tool(tmp_path):
    project = _fpga_doctor_project(tmp_path, marked=True)

    assert doctor._runtime_probe_binaries(project, ["fpga_board"], flow_name="fpga") == ["vivado"]


def test_fpga_deep_notice_names_marked_target_and_manual_command(tmp_path):
    project = _fpga_doctor_project(tmp_path, marked=True)
    skips: list[str] = []

    doctor._run_fpga_impl_deep_notice(project, skips.append)

    assert skips == [
        "fpga deep smoke [fpga_board] skipped - a full FPGA implementation is too "
        "slow for --deep; smoke it manually end-to-end: booley flow fpga --target fpga_board"
    ]


def test_marked_fpga_axis_requires_fpga_mcp_endpoint(tmp_path):
    project = _fpga_doctor_project(tmp_path, marked=True)

    assert "fpga" in doctor._required_mcp_tools(project)


def test_disabled_fpga_axis_does_not_require_fpga_mcp_endpoint(tmp_path):
    project = _fpga_doctor_project(tmp_path, marked=True, fpga_table={"enabled": False})

    assert "fpga" not in doctor._required_mcp_tools(project)


def test_marked_fpga_target_with_non_fpga_axis_fails_compatibility(tmp_path):
    project = _fpga_doctor_project(tmp_path, marked=True)
    core = tmp_path / "design.core"
    core.write_text(
        core.read_text(encoding="utf-8").replace("fpga_board", "synth_board"),
        encoding="utf-8",
    )
    rec = _Rec()

    assert doctor._check_doctor_targets(project, "fpga", rec.f) == []
    assert any("incompatible Doctor Flow 'fpga'" in message for message in rec.fails())


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
    for i in range(design_size.LARGE_DESIGN_FILES + 5):
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
    for i in range(design_size.LARGE_DESIGN_FILES + 5):
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
    for i in range(design_size.LARGE_DESIGN_FILES + 50):
        (vendored / f"g_{i}.v").write_text("module g; endmodule\n", encoding="utf-8")
    project = _derived_project_audit(proj)

    notes: list[str] = []
    passes: list[str] = []
    doctor._check_design_size(project, passes.append, notes.append)
    assert passes and "design size" in passes[0]
    assert not notes
    # Exactly the one non-pruned HDL file was counted.
    audit = design_size.analyze_design_size(proj, project.project_dir, ())
    assert audit.hdl_files == 1


def test_design_size_scopes_to_configured_target_in_large_monorepo(tmp_path):
    proj = tmp_path / "monorepo"
    rtl = proj / "rtl"
    rtl.mkdir(parents=True)
    (rtl / "selected.sv").write_text("module selected; endmodule\n", encoding="utf-8")
    unrelated = proj / "unrelated"
    unrelated.mkdir()
    for index in range(design_size.LARGE_DESIGN_FILES + 5):
        (unrelated / f"large_{index}.sv").write_text("module large; endmodule\n", encoding="utf-8")
    (proj / "design.core").write_text(
        """CAPI=2:
name: ::small:0
filesets:
  rtl:
    files: [rtl/selected.sv]
targets:
  sim_small:
    flow: sim
    flow_options: {booley: {doctor: [sim]}}
    filesets: [rtl]
    toplevel: selected
""",
        encoding="utf-8",
    )
    project = _derived_project_audit(proj)

    passes: list[str] = []
    notes: list[str] = []
    doctor._check_design_size(project, passes.append, notes.append)

    assert not notes
    assert passes and "Doctor Target filesets: ~1 HDL files" in passes[0]


# ---------------------------------------------------------------------------
# Host-source-vs-image drift (SETUP-22) — _check_image_bakes_current_booley
# ---------------------------------------------------------------------------


def _image_lifecycle_result(image: str, status) -> object:
    return doctor.image_lifecycle.LifecycleResult(image, "sha256:test", status)


def test_image_bakes_current_booley_warns_on_fingerprint_mismatch(tmp_path, monkeypatch):
    # Exact path: the build-fingerprint label differs from the checkout hash →
    # WARN without ever consulting the mtime heuristic.
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    image = doctor.pi.project_image_name(proj)
    monkeypatch.setattr(
        doctor.image_lifecycle,
        "reconcile",
        lambda *_args: _image_lifecycle_result(image, doctor.image_lifecycle.Status.STALE),
    )

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_image_bakes_current_booley(project, "docker", image, passes.append, warns.append)
    assert warns and "image provenance differs" in warns[0]
    assert not passes


def test_image_bakes_current_booley_passes_on_fingerprint_match(tmp_path, monkeypatch):
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    image = doctor.pi.project_image_name(proj)
    monkeypatch.setattr(
        doctor.image_lifecycle,
        "reconcile",
        lambda *_args: _image_lifecycle_result(image, doctor.image_lifecycle.Status.CURRENT),
    )
    # The mtime fallback must not run; poison it to prove that.
    monkeypatch.setattr(doctor, "_image_created_epoch", lambda _exe, _img: 1 / 0)

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_image_bakes_current_booley(project, "docker", image, passes.append, warns.append)
    assert passes and "exactly this checkout" in passes[0]
    assert not warns


def test_image_bakes_current_booley_warns_when_provenance_is_stale(tmp_path, monkeypatch):
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    image = doctor.pi.project_image_name(proj)
    monkeypatch.setattr(
        doctor.image_lifecycle,
        "reconcile",
        lambda *_args: _image_lifecycle_result(image, doctor.image_lifecycle.Status.STALE),
    )

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_image_bakes_current_booley(project, "docker", image, passes.append, warns.append)
    assert warns and "stale code" in warns[0]
    assert not passes


def test_image_bakes_current_booley_passes_when_provenance_is_current(tmp_path, monkeypatch):
    proj = tmp_path / "myproj"
    (proj / ".booley_project").mkdir(parents=True)
    project = _derived_project_audit(proj)
    image = doctor.pi.project_image_name(proj)
    monkeypatch.setattr(
        doctor.image_lifecycle,
        "reconcile",
        lambda *_args: _image_lifecycle_result(image, doctor.image_lifecycle.Status.CURRENT),
    )

    warns: list[str] = []
    passes: list[str] = []
    doctor._check_image_bakes_current_booley(project, "docker", image, passes.append, warns.append)
    assert passes and "exactly this checkout" in passes[0]
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
    monkeypatch.setattr(
        doctor.image_lifecycle,
        "reconcile",
        lambda *_args: _image_lifecycle_result(image, doctor.image_lifecycle.Status.EXTERNAL),
    )

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
        monkeypatch.setattr("booley.runtime.runtime_context.inside_session_runtime", lambda: False)
        rec = _Rec()
        doctor._check_board_orphans(tmp_path, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"skip"}

    def test_no_active_tickets_passes(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr("booley.runtime.runtime_context.inside_session_runtime", lambda: True)
        tickets = _seed_board(tmp_path)
        monkeypatch.setenv("TICKETS_DIR", str(tickets))
        rec = _Rec()
        doctor._check_board_orphans(tmp_path, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"pass"}

    def test_dead_pid_recovered_with_warn(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr("booley.runtime.runtime_context.inside_session_runtime", lambda: True)
        tickets = _seed_board(tmp_path)
        _seed_active_ticket(tickets)
        monkeypatch.setenv("TICKETS_DIR", str(tickets))
        monkeypatch.setattr(
            "booley.harness.orphan_handler.is_pid_alive",
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
        monkeypatch.setattr("booley.runtime.runtime_context.inside_session_runtime", lambda: True)
        tickets = _seed_board(tmp_path)
        _seed_active_ticket(tickets)
        monkeypatch.setenv("TICKETS_DIR", str(tickets))
        monkeypatch.setattr(
            "booley.harness.orphan_handler.is_pid_alive",
            lambda _pid: True,
        )
        rec = _Rec()
        doctor._check_board_orphans(tmp_path, rec.p, rec.w, rec.s)
        assert rec.kinds() == {"pass"}
        assert any("live owner PIDs" in m for _lvl, m in rec.events)

    def test_read_only_reports_dead_pid_without_moving_ticket(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr("booley.runtime.runtime_context.inside_session_runtime", lambda: True)
        tickets = _seed_board(tmp_path)
        _seed_active_ticket(tickets)
        monkeypatch.setenv("TICKETS_DIR", str(tickets))
        monkeypatch.setattr("booley.harness.orphan_handler.is_pid_alive", lambda _pid: False)
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
    monkeypatch.setattr(resource_policy, "CGROUP_MEMORY_LIMIT_PATHS", (path,))


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
            resource_policy,
            "CGROUP_MEMORY_LIMIT_PATHS",
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

    MCP registration is container-side (booley.harness.incontainer_register runs from
    the devcontainer hooks), so a host-launched agent has no `booley` MCP
    server at all. Nothing else reports that absence.
    """

    def test_plain_host_shell_passes_quietly(self, monkeypatch):
        _set_venue(monkeypatch, False)
        rec = _Rec()
        doctor._check_host_agent_session(rec.p, rec.w)
        assert rec.kinds() == {"pass"}
        assert "Sandbox" in rec.events[0][1]

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
        """In the Sandbox the MCP tools do exist — no note either way."""
        _set_venue(monkeypatch, True)
        _set_agent_session(monkeypatch, "claude")
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "slots"))
        rec = _Rec()
        doctor._check_runtime_location(None, "booley-sandbox", rec.p, rec.w, rec.s, rec.f)
        assert not any("HOST" in m for _lvl, m in rec.events)


class TestAdvisoryMcpTools:
    """Specialists are expected unless their own ``enabled`` flag is false."""

    _SKILL_DEFAULT_BUILTIN = (
        "sim",
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
            if name in {"sim", "lint", "synth", "fpga"}
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
        for name in ("sim", "lint", "synth"):
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
        (tmp_path / "selftest.core").write_text(
            "CAPI=2:\n"
            "name: ::selftest:0\n"
            "targets:\n"
            "  sim_core:\n"
            "    flow: sim\n"
            "    flow_options: {tool: verilator, booley: {doctor: [sim]}}\n",
            encoding="utf-8",
        )
        return doctor.ProjectAudit(
            project_root=tmp_path,
            project_dir=pd,
            booley_toml={
                "flows": {
                    "sim": {},
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
        monkeypatch.setattr(doctor.session_runtime, "up", lambda _root: "booley-session-test")
        rec = _Rec()
        runtime = doctor._DoctorFlowRuntime(project.project_root, "docker")
        doctor._run_selftest_checks(project, runtime, rec.p, rec.w, rec.s, rec.f)
        return rec

    def test_healthy_selftest_passes(self, tmp_path, monkeypatch):
        # good -> 0 (pass), bad -> 1 (graded fail): both correct.
        rec = self._run(monkeypatch, self._audit(tmp_path), {"good": 0, "bad": 1})
        assert not rec.fails()
        assert any("correctly graded a failure" in m for _, m in rec.events)

    def test_selftests_cannot_inherit_ticket_acceptance_context(self, tmp_path, monkeypatch):
        _set_venue(monkeypatch, True)
        ticket_vars = {
            "BOOLEY_SLUG": "active-ticket",
            "BOOLEY_TICKET_FILE": "/ticket.md",
            "BOOLEY_STATE_FILE": "/ticket-logs/.runtime/booley_state.json",
            "BOOLEY_LOGS_DIR": "/ticket-logs",
            "BOOLEY_RUNTIME_DIR": "/ticket-logs/.runtime",
            "BOOLEY_EXECUTION_ID": "resume-generation",
        }
        for key, value in ticket_vars.items():
            monkeypatch.setenv(key, value)
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append((cmd, kwargs["env"]))
            kind = kwargs["env"][selftest_overlay.INTERNAL_KIND_ENV]
            return subprocess.CompletedProcess(
                cmd,
                {"good": 0, "bad": 1}[kind],
                stdout="",
                stderr="",
            )

        monkeypatch.setattr(doctor.subprocess, "run", fake_run)
        project = self._audit(tmp_path)
        rec = _Rec()

        doctor._run_selftest_checks(
            project,
            doctor._DoctorFlowRuntime(project.project_root, None),
            rec.p,
            rec.w,
            rec.s,
            rec.f,
        )

        assert not rec.fails()
        assert len(seen) == 2
        for cmd, env in seen:
            assert "--diagnostic" in cmd
            assert ticket_vars.keys().isdisjoint(env)

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
            doctor._DoctorFlowRuntime(tmp_path, "docker"),
            rec.p,
            rec.w,
            rec.s,
            rec.f,
        )
        assert not rec.fails()
        assert any("fail-path unvalidated" in m for _, m in rec.events if _ == "warn")

    def test_flow_runs_in_place_inside_session_runtime(self, tmp_path, monkeypatch):
        # F-17 / ADR 0028: inside the Sandbox there is no docker — the
        # container IS the sandbox — so the self-test must exec the Flow
        # in-place (this interpreter, no docker wrap, no SKIP), mirroring
        # _flow_check_routing and how Sandbox Flows themselves execute
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
        runtime = doctor._DoctorFlowRuntime(project.project_root, None)
        doctor._run_selftest_checks(project, runtime, rec.p, rec.w, rec.s, rec.f)
        assert not rec.fails()
        assert not any(lvl == "skip" for lvl, _ in rec.events)
        assert any("correctly graded a failure" in m for _, m in rec.events)
        # In-place = this interpreter drives the Flow module directly.
        assert seen_cmds and all(cmd[0] == sys.executable for cmd in seen_cmds)

    def test_host_deep_check_still_skips_without_docker(self, tmp_path, monkeypatch):
        # On the HOST a Sandbox self-test genuinely needs Docker
        # runtime; a host without Docker stays a SKIP (unchanged by F-17).
        _set_venue(monkeypatch, False)
        project = self._audit(tmp_path)
        rec = _Rec()
        runtime = doctor._DoctorFlowRuntime(project.project_root, None)
        doctor._run_selftest_checks(project, runtime, rec.p, rec.w, rec.s, rec.f)
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
        (tmp_path / "builtin.core").write_text(
            "CAPI=2:\n"
            "name: ::builtin:0\n"
            "targets:\n"
            "  x:\n"
            "    flow: sim\n"
            "    flow_options: {booley: {doctor: [sim]}}\n",
            encoding="utf-8",
        )
        p = doctor.ProjectAudit(
            project_root=tmp_path,
            project_dir=pd,
            booley_toml={
                "flows": {
                    "sim": {},
                    "lint": {"enabled": False},
                }
            },
            configs_toml={"x": {}},
            first_target="x",
        )
        rec = _Rec()
        runtime = doctor._DoctorFlowRuntime(p.project_root, "docker")
        doctor._run_selftest_checks(p, runtime, rec.p, rec.w, rec.s, rec.f)
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
        from booley.harness.models import AgentResult
        from booley.runtime import agent as agent_mod
        from booley.runtime import agent_config as config_mod

        monkeypatch.setattr(config_mod, "load_backend_config", lambda root: None)

        class _Cfg:
            def __init__(self):
                self.settings = self

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
        from booley.harness.models import AgentResult
        from booley.runtime import agent as agent_mod
        from booley.runtime import agent_config as config_mod

        monkeypatch.setattr(config_mod, "load_backend_config", lambda root: None)

        class _Cfg:
            def __init__(self):
                self.settings = self

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
        from booley.runtime import agent_config as config_mod

        def boom(_root):
            raise RuntimeError("no backend configured")

        monkeypatch.setattr(config_mod, "load_backend_config", boom)
        with pytest.raises(developer_probe.ProbeError, match="no backend"):
            developer_probe.measure_developer_rss(tmp_path)


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


class TestCheckDoctorTargets:
    """Every enabled Flow needs at least one explicitly marked .core Target."""

    def test_marked_target_passes(self, tmp_path):
        project = _audit_with_flows(tmp_path, {"lint": {}})
        rec = _Rec()
        assert doctor._check_doctor_targets(project, "lint", rec.f) == ["lint_fast"]
        assert not rec.fails()

    def test_unmarked_target_fails_and_names_metadata(self, tmp_path):
        (tmp_path / "x.core").write_text(
            "CAPI=2:\nname: ::x:0\ntargets:\n"
            "  lint: {flow: lint, flow_options: {tool: verilator}}\n",
            encoding="utf-8",
        )
        project = doctor.ProjectAudit(
            project_root=tmp_path,
            project_dir=tmp_path / ".booley_project",
            booley_toml={"flows": {"sim": {}}},
            configs_toml={},
            first_target="",
        )
        fails: list[tuple[str, str]] = []
        assert (
            doctor._check_doctor_targets(
                project, "sim", lambda msg, fix="": fails.append((msg, fix))
            )
            == []
        )
        assert "no Doctor Target" in fails[0][0]
        assert "flow_options.booley.doctor" in fails[0][1]


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
        refs = {handle.name: handle for handle in TargetCatalog.build(tmp_path).list()}
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
        selected = self._LEGACY_CORE.replace(
            "    default_tool: icarus",
            "    flow_options: {booley: {doctor: [sim]}}\n    default_tool: icarus",
        )
        passes, warns, notes = self._run(tmp_path, selected, {"flows": {"sim": {}}})
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
            tmp_path,
            multi.replace(
                "  sim_old:\n    filesets: [rtl]",
                "  sim_old:\n    flow_options: {booley: {doctor: [sim]}}\n    filesets: [rtl]",
            ),
            {"flows": {"sim": {}}},
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
        refs = {handle.name: handle for handle in TargetCatalog.build(tmp_path).list()}
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
        selected = self._MISNAMED.replace(
            "flow_options: {tool: verilator}",
            "flow_options: {tool: verilator, booley: {doctor: [sim]}}",
        )
        passes, warns = self._run(tmp_path, selected, {"flows": {"sim": {}}})
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
    flow_options: {tool: verilator, booley: {doctor: [sim]}}
    toplevel: tb
"""
        passes, warns = self._run(tmp_path, block, {"flows": {"sim": {}}})
        assert warns == []
        assert any("<axis>_<subject>" in m for m in passes)

    def test_doctor_metadata_beats_the_legacy_suffix(self, tmp_path):
        """Doctor metadata owns the naming axis, whatever the Target is called."""
        block = """\
soc_sim:
    filesets: [rtl]
    flow: generic
    flow_options: {tool: yosys, booley: {doctor: [synth]}}
    toplevel: dut
"""
        _, warns = self._run(tmp_path, block, {"flows": {"synth": {}}})
        assert "rename it 'synth_soc'" in warns[0]

    def test_legacy_asic_prefix_is_renamed_to_synth(self, tmp_path):
        block = """\
asic_core:
    filesets: [rtl]
    flow: generic
    flow_options: {tool: yosys, arch: xilinx, booley: {doctor: [synth]}}
    toplevel: dut
"""
        _, warns = self._run(tmp_path, block, {"flows": {"synth": {}}})
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
    flow_options: {tool: verilator, booley: {doctor: [sim]}}
    toplevel: tb
"""
        _, warns = self._run(tmp_path, block, {"flows": {"sim": {}}})
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
    """B6: generated cocotb names cannot be grepped for as `def <name>(`.

    ``TestFactory(...).generate_tests()`` and ``@cocotb.parametrize`` register
    rendered names in the module namespace at import time, so the static check
    is structurally blind to them. Not verifiable ⇒ report it as not
    verifiable, not as a finding.
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

    _PARAMETRIZED_TB = """\
import cocotb


@cocotb.test()
@cocotb.parametrize(("ifg", [12, 0]))
async def run_test_tx(dut, ifg=12):
    pass
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

    def test_parametrized_names_are_reported_unverifiable_not_missing(
        self,
        tmp_path: Path,
    ):
        _write_cocotb_project(
            tmp_path,
            tb_body=self._PARAMETRIZED_TB,
            tests=["run_test_tx/ifg=12", "run_test_tx/ifg=0"],
        )
        rec = _audit(tmp_path)
        assert not any("not found as functions" in m for lvl, m in rec.events)
        assert any(
            "cannot be verified statically" in m and "parameterized" in m
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


def test_concise_reporter_suppresses_only_pass_rendering(capsys) -> None:
    reporter = doctor._Reporter.create(concise=True)

    reporter.pass_("healthy Target")
    reporter.note_("informational detail")
    reporter.warn_(doctor.warning("test.concise", "actionable warning"), "repair warning")
    reporter.skip_("optional probe unavailable")
    reporter.fail_("broken runtime", "repair runtime")
    exit_code = reporter.finish()

    output = capsys.readouterr().out
    assert "PASS  healthy Target" not in output
    assert "NOTE  informational detail" in output
    assert "WARN  actionable warning" in output
    assert "SKIP  optional probe unavailable" in output
    assert "FAIL  broken runtime" in output
    assert "1 passed, 1 warning(s)" in output
    assert reporter.counts == {
        "pass": 1,
        "fail": 1,
        "warn": 1,
        "waived": 0,
        "note": 1,
        "skip": 1,
    }
    assert reporter.findings is not None
    assert [finding.severity for finding in reporter.findings] == [
        "pass",
        "note",
        "warn",
        "skip",
        "fail",
    ]
    assert exit_code == 1


class TestDisplayReportDir:
    """Deep-check FAIL hints must be readable where the user reads them.

    Doctor may run inside the Sandbox, where the project dir is the
    /booley-project bind mount; printing that verbatim sent a host-side user
    ls-ing a path that only exists in the container (the real files sit at
    <repo>/.booley_project/tmp/...).
    """

    def test_container_mount_is_rendered_repo_relative(self):
        from types import SimpleNamespace

        from booley.runtime.devcontainer import PROJECT_DIR_TARGET

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

    def test_lf_tree_with_stale_crlf_index_stat_fails(self, tmp_path: Path):
        self._repo(tmp_path, autocrlf="true")
        self._commit(tmp_path, "a.v", b"module a;\nendmodule\n")
        (tmp_path / "a.v").unlink()
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "--", "a.v"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "false"],
            capture_output=True,
            check=True,
        )
        (tmp_path / "a.v").write_bytes(b"module a;\nendmodule\n")
        assert (
            subprocess.run(
                ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=no"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            == " M a.v\n"
        )
        assert (
            subprocess.run(
                ["git", "-C", str(tmp_path), "diff", "--quiet"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        c = _Collector()

        doctor._check_line_endings(tmp_path, c._pass, c._warn, c._skip, c._fail)

        assert len(c.failed) == 1
        assert "stale" in c.failed[0][0]
        assert "booley init" in c.failed[0][1]

    def test_crlf_tree_fails_with_the_init_remediation(self, tmp_path: Path):
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
        assert "booley init" in c.failed[0][1]
        assert "--fix-line-endings" not in c.failed[0][1]

    def test_autocrlf_true_with_lf_tree_warns(self, tmp_path: Path):
        # Nothing is broken yet — the next checkout is what breaks it.
        self._repo(tmp_path, autocrlf="true")
        c = _Collector()

        doctor._check_line_endings(tmp_path, c._pass, c._warn, c._skip, c._fail)

        assert c.warned and not c.failed

    @pytest.mark.parametrize("true_value", ["yes", "on", "1"])
    def test_autocrlf_true_alias_with_lf_tree_warns(self, tmp_path: Path, true_value: str):
        self._repo(tmp_path, autocrlf=true_value)
        c = _Collector()

        doctor._check_line_endings(tmp_path, c._pass, c._warn, c._skip, c._fail)

        assert c.warned and not c.failed and not c.passed

    def test_explicit_crlf_checkout_policy_is_not_a_finding(self, tmp_path: Path):
        self._repo(tmp_path, autocrlf="false")
        self._commit(tmp_path, ".gitattributes", b"*.txt text eol=crlf\n")
        self._commit(tmp_path, "intentional.txt", b"alpha\nbeta\n")
        (tmp_path / "intentional.txt").unlink()
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "--", "intentional.txt"],
            capture_output=True,
            check=True,
        )
        c = _Collector()

        doctor._check_line_endings(tmp_path, c._pass, c._warn, c._skip, c._fail)

        assert c.passed and not c.warned and not c.failed

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

    def test_nested_crlf_failure_names_project_data_repository(self, tmp_path: Path):
        self._repo(tmp_path, autocrlf="false")
        self._commit(tmp_path, "a.v", b"module a;\nendmodule\n")
        project_dir = tmp_path / ".booley_project"
        hooks = project_dir / "hooks"
        hooks.mkdir(parents=True)
        self._repo(project_dir, autocrlf="true")
        self._commit(project_dir, "hooks/post-setup.sh", b"#!/bin/sh\nset -euo pipefail\n")
        hook = hooks / "post-setup.sh"
        hook.unlink()
        subprocess.run(
            ["git", "-C", str(project_dir), "checkout", "--", "hooks/post-setup.sh"],
            capture_output=True,
            check=True,
        )
        c = _Collector()

        doctor._check_line_endings(
            tmp_path,
            c._pass,
            c._warn,
            c._skip,
            c._fail,
            project_dir=project_dir,
        )

        assert len(c.failed) == 1
        assert "project data" in c.failed[0][0]
        assert str(project_dir) in c.failed[0][0]
        assert any("project checkout" in message for message in c.passed)

    def test_nested_autocrlf_warning_has_project_data_subject(self, tmp_path: Path):
        self._repo(tmp_path, autocrlf="false")
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        self._repo(project_dir, autocrlf="true")
        reporter = doctor._Reporter.create()

        doctor._check_line_endings(
            tmp_path,
            reporter.pass_,
            reporter.warn_,
            reporter.skip_,
            reporter.fail_,
            project_dir=project_dir,
        )

        assert reporter.findings is not None
        warnings = [finding for finding in reporter.findings if finding.severity == "warn"]
        assert len(warnings) == 1
        assert warnings[0].check_id == "git.autocrlf-risk"
        assert warnings[0].subject == "project-data"
        assert str(project_dir) in warnings[0].message

    def test_unset_local_autocrlf_warning_names_project_checkout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        global_config = tmp_path / "global.gitconfig"
        global_config.write_text("[core]\n\tautocrlf = false\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        self._repo(tmp_path, autocrlf="false")
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "--unset", "core.autocrlf"],
            capture_output=True,
            check=True,
        )
        reporter = doctor._Reporter.create()

        doctor._check_line_endings(
            tmp_path,
            reporter.pass_,
            reporter.warn_,
            reporter.skip_,
            reporter.fail_,
        )

        assert reporter.findings is not None
        warnings = [finding for finding in reporter.findings if finding.severity == "warn"]
        assert len(warnings) == 1
        assert warnings[0].check_id == "git.autocrlf-risk"
        assert warnings[0].subject == "project-checkout"
        assert "not set locally" in warnings[0].message

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            (
                "autocrlf-unreadable",
                "could not read core.autocrlf as a Git Boolean",
            ),
            (
                "local-autocrlf-unreadable",
                "could not read repo-local core.autocrlf",
            ),
            ("eol-scan-unreadable", "could not read `git ls-files --eol`"),
        ],
    )
    def test_unreadable_repository_report_maps_failed_probe(
        self,
        tmp_path: Path,
        code: str,
        expected: str,
    ):
        from booley.harness.setup.line_endings import (
            LineEndingObservation,
            LineEndingObservationCode,
            LineEndingRepository,
            LineEndingStatus,
            RepositoryLineEndingReport,
        )

        collector = _Collector()
        repository = LineEndingRepository("project-checkout", tmp_path)
        report = RepositoryLineEndingReport(
            repository,
            LineEndingStatus.UNSAFE,
            (LineEndingObservation(LineEndingObservationCode(code)),),
            (),
        )

        doctor._report_repository_line_endings(
            report,
            collector._pass,
            collector._warn,
            collector._fail,
        )

        assert len(collector.warned) == 1
        assert expected in collector.warned[0]

    def test_unreadable_index_comparison_report_warns(self, tmp_path: Path):
        from booley.harness.setup.line_endings import (
            LineEndingObservation,
            LineEndingObservationCode,
            LineEndingRepository,
            LineEndingStatus,
            RepositoryLineEndingReport,
        )

        collector = _Collector()
        repository = LineEndingRepository("project-checkout", tmp_path)
        report = RepositoryLineEndingReport(
            repository,
            LineEndingStatus.UNSAFE,
            (
                LineEndingObservation(
                    LineEndingObservationCode.STATUS_UNREADABLE,
                    detail="git diff timed out",
                ),
            ),
            (),
        )

        doctor._report_repository_line_endings(
            report,
            collector._pass,
            collector._warn,
            collector._fail,
        )

        assert len(collector.warned) == 1
        assert "git diff timed out" in collector.warned[0]

    def test_crlf_failure_precedes_candidate_warning(self, tmp_path: Path):
        from booley.harness.setup.line_endings import (
            LineEndingObservation,
            LineEndingObservationCode,
            LineEndingRepository,
            LineEndingStatus,
            RepositoryLineEndingReport,
        )

        collector = _Collector()
        report = RepositoryLineEndingReport(
            LineEndingRepository("project-checkout", tmp_path),
            LineEndingStatus.UNSAFE,
            (
                LineEndingObservation(LineEndingObservationCode.CRLF_MISMATCH, count=1),
                LineEndingObservation(
                    LineEndingObservationCode.CANDIDATE_UNSAFE,
                    detail="hard-linked candidate",
                ),
            ),
            (),
        )

        doctor._report_repository_line_endings(
            report,
            collector._pass,
            collector._warn,
            collector._fail,
        )

        assert len(collector.failed) == 1
        assert not collector.warned

    def test_autocrlf_check_id_precedes_unreadable_status(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from booley.harness.setup.line_endings import (
            LineEndingObservation,
            LineEndingObservationCode,
            LineEndingReport,
            LineEndingRepository,
            LineEndingStatus,
            RepositoryLineEndingReport,
        )

        repository = LineEndingRepository("project-checkout", tmp_path)
        report = LineEndingReport(
            LineEndingStatus.UNSAFE,
            (
                RepositoryLineEndingReport(
                    repository,
                    LineEndingStatus.UNSAFE,
                    (
                        LineEndingObservation(LineEndingObservationCode.AUTOCRLF_EFFECTIVE_TRUE),
                        LineEndingObservation(
                            LineEndingObservationCode.STATUS_UNREADABLE,
                            detail="git diff timed out",
                        ),
                    ),
                    (),
                ),
            ),
            (),
        )
        monkeypatch.setattr(
            doctor, "reconcile_project_line_endings", lambda *_args, **_kwargs: report
        )
        reporter = doctor._Reporter.create()

        doctor._check_line_endings(
            tmp_path,
            reporter.pass_,
            reporter.warn_,
            reporter.skip_,
            reporter.fail_,
        )

        assert reporter.findings is not None
        assert len(reporter.findings) == 1
        assert reporter.findings[0].check_id == "git.autocrlf-risk"
        assert reporter.findings[0].severity == "warn"


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
        monkeypatch.setattr(
            host_environment,
            "inspect_agent_installation",
            lambda _app: host_environment.AgentInstallation(True, None),
        )
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

        assert "already inside the Sandbox" in msg
        assert "nested Docker" in msg
        assert "not available" not in msg  # the old "something's broken" phrasing

    def test_on_host_says_no_runtime_found(self, monkeypatch):
        _set_venue(monkeypatch, False)

        msg = doctor._no_docker_skip_reason()

        assert "no Docker/Podman runtime found" in msg
        assert "Sandbox" not in msg

    def test_container_checks_use_the_reason(self, monkeypatch, capsys):
        from booley.harness import web_isolation

        _set_venue(monkeypatch, True)
        monkeypatch.setattr(web_isolation, "policy_error", lambda: None)
        rec = _Rec()

        doctor._run_container_checks(
            None, None, "booley-sandbox", False, rec.p, rec.w, rec.s, rec.f
        )

        assert any("already inside the Sandbox" in m for lvl, m in rec.events if lvl == "skip")
        assert any(
            "provider-side web access disabled" in m for lvl, m in rec.events if lvl == "pass"
        )


class TestSynthHeavyTargetCalibration:
    def _project(self, tmp_path, *, marked=("asic_small", "asic_full"), booley_toml=None):
        def target(name: str) -> str:
            metadata = ", booley: {doctor: [synth]}" if name in marked else ""
            return f"  {name}:\n    flow: generic\n    flow_options: {{tool: yosys{metadata}}}\n"

        (tmp_path / "synth.core").write_text(
            "CAPI=2:\nname: ::synth:0\ntargets:\n" + target("asic_small") + target("asic_full"),
            encoding="utf-8",
        )
        return _adr28_project(tmp_path, booley_toml=booley_toml)

    def test_host_deep_uses_issued_session_resources(self, tmp_path, monkeypatch):
        project = _adr28_project(
            tmp_path,
            booley_toml={"sandbox": {"memory": "24g"}},
        )
        _set_venue(monkeypatch, False)
        monkeypatch.setattr(doctor.session_runtime, "up", lambda _root: "booley-session-test")
        cmd = doctor._flow_command(
            project,
            "synth",
            "asic_full",
            dry_run=False,
            flow_runtime=doctor._DoctorFlowRuntime(tmp_path, "docker"),
        )
        assert cmd[:2] == ["docker", "exec"]
        assert "booley-session-test" in cmd
        assert "--memory" not in cmd

    def test_multi_target_matrix_uses_every_marked_target(self, tmp_path):
        project = self._project(tmp_path)
        assert doctor._doctor_targets(project, "synth") == ["asic_full", "asic_small"]

    def test_unmarked_synth_targets_fail_selection(self, tmp_path):
        project = self._project(tmp_path, marked=())
        rec = _Rec()
        assert doctor._check_doctor_targets(project, "synth", rec.f) == []
        assert rec.kinds() == {"fail"}

    def test_measured_heavy_peak_overrides_undersized_reservation(self, tmp_path, monkeypatch):
        from booley.harness import synth_probe

        _set_venue(monkeypatch, False)
        project = self._project(
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
        project = self._project(
            tmp_path,
            marked=("asic_full",),
            booley_toml={
                "sandbox": {"memory": "32g"},
            },
        )
        synth_probe.record_measurement(project.project_dir, "asic_small", 7 * 1024)
        rec = _Rec()

        doctor._check_memory_invariant(project, rec.p, rec.w, rec.s)

        assert rec.kinds() == {"warn"}
        assert "unselected Target 'asic_small'" in rec.events[0][1]


def test_doctor_reuses_one_target_source_inspector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_names = [f"lint_{index}" for index in range(100)]
    targets = "".join(
        f"  {name}:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator}\n"
        "    filesets: [rtl]\n"
        "    toplevel: dut\n"
        for name in target_names
    )
    (tmp_path / "design.core").write_text(
        "CAPI=2:\n"
        "name: acme:ip:design:1.0\n"
        "filesets:\n"
        "  rtl:\n"
        "    files: [rtl/dut.sv]\n"
        "targets:\n"
        f"{targets}",
        encoding="utf-8",
    )
    catalog = TargetCatalog.build(tmp_path)
    refs = {handle.name: handle for handle in catalog.list()}
    managers: list[object] = []
    resolutions = 0
    real_manager = target_inspection.CoreManager
    real_get_depends = real_manager.get_depends

    def counting_manager(*args, **kwargs):
        manager = real_manager(*args, **kwargs)
        managers.append(manager)
        return manager

    def counting_get_depends(self, *args, **kwargs):
        nonlocal resolutions
        resolutions += 1
        return real_get_depends(self, *args, **kwargs)

    monkeypatch.setattr(target_inspection, "CoreManager", counting_manager)
    monkeypatch.setattr(real_manager, "get_depends", counting_get_depends)

    inputs = doctor._CoreAuditInputs(catalog, refs)
    for name in target_names:
        assert inputs.sources_for(name).rtl_source_files == ("rtl/dut.sv",)

    assert len(managers) == 1
    assert resolutions == 1
