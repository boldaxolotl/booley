"""Whole-run helpers for the Doctor tests: a fixture Project, the run, and its findings.

``enter_sandbox`` fakes the Sandbox venue check and the Sandbox image contents; ``set_venue``
and ``runtime_setup`` place the run on the host or in the Sandbox; ``script_flow`` answers a
Flow's commands.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import DiagnosticFinding, Severity
from booley.harness import activity_hooks, doctor, web_isolation
from booley.runtime import dashboard_extension, project_gitignore, runtime_context
from booley.runtime import devcontainer as dc
from booley.runtime.runtime_context import RuntimeLocation
from tests.devcontainer_spec_support import build_spec
from tests.harness.doctor_environment_support import patch_doctor_environment
from tests.sandbox_venue_fake import place
from tests.subprocess_budget import run_bounded_child


def run_doctor(
    root: Path,
    *,
    deep: bool = False,
    verbose: bool = False,
    concise: bool = False,
    skip_agent_checks: bool = False,
    record_clean: bool = False,
) -> doctor.DoctorRunResult:
    """Run the whole Doctor on a Project the host fixtures or `write_project` set up."""
    args = argparse.Namespace(
        verbose=verbose, deep=deep, concise=concise, skip_agent_checks=skip_agent_checks
    )
    return doctor.run_doctor_result(args, root, record_clean=record_clean)


def enter_sandbox(
    monkeypatch: pytest.MonkeyPatch, location: RuntimeLocation = RuntimeLocation.ISSUED_SANDBOX
) -> None:
    """Make the host fixtures' Doctor run inside the Sandbox, healthy apart from the test."""
    place(monkeypatch, location)
    # The next three stand in for files the Sandbox Image installs, which a test host lacks.
    monkeypatch.setattr(web_isolation, "policy_error", lambda: None)
    monkeypatch.setattr(activity_hooks, "hooks_error", lambda: None)
    monkeypatch.setattr(dashboard_extension, "installed_error", lambda: None)


def findings(result: doctor.DoctorRunResult, check_id: str) -> list[DiagnosticFinding]:
    """The result's findings with *check_id*."""
    return [finding for finding in result.findings if finding.check_id == check_id]


def write_project(
    root: Path,
    *,
    sandbox_image: str | None = None,
    seed_interactive: bool = True,
    write_tickets: bool = True,
) -> Path:
    (root / "rtl").mkdir()
    (root / "tb").mkdir()
    booley_dir = root / ".booley"
    booley_dir.mkdir()
    # A set memory limit keeps the clean fixture free of the unset-memory WARN.
    sandbox_section = '[sandbox]\nmemory = "16g"\n'
    if sandbox_image:
        sandbox_section += f'image = "{sandbox_image}"\n'
    booley_toml = """
[project]
name = "unit"

[flows.sim]

[flows.lint]

[flows.synth]

{sandbox_section}
[agent]
auth = "subscription"
""".lstrip().replace("{sandbox_section}", sandbox_section)
    (booley_dir / "booley.toml").write_text(booley_toml, encoding="utf-8")
    (booley_dir / ".gitignore").write_text(project_gitignore.PROJECT_GITIGNORE, encoding="utf-8")
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
        for name in ("board", "state", "logs", "locks"):
            (booley_dir / "tickets" / name).mkdir(parents=True, exist_ok=True)
    if seed_interactive:
        seed_sandbox_files(root)
    return booley_dir


def seed_sandbox_files(root: Path) -> None:
    """Seed the ADR-0018 artifacts a healthy interactive setup has: an untracked
    devcontainer spec and the git info/exclude entries."""
    dc.write_devcontainer(root, build_spec(dc.APP_NONE, image=doctor.pi.project_image_name(root)))
    info_dir = root / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / "exclude").write_text("/.devcontainer\n/.booley\n", encoding="utf-8")


def finding_messages(
    result: doctor.DoctorRunResult,
    severity: Severity | None = None,
    *,
    check_id: str | None = None,
) -> list[str]:
    """Messages of *result*'s findings, filtered by severity and check ID."""
    return [
        finding.message
        for finding in result.findings
        if (severity is None or finding.severity is severity)
        and (check_id is None or finding.check_id == check_id)
    ]


def script_flow(monkeypatch, flow, answer):
    """Answer each non-dry-run ``booley.flows.<flow>`` command with ``answer(argv, env)``.

    *answer* returns a ``CompletedProcess``, raises, or returns None to use the current fake.
    Returns the list of ``(argv, env)`` launches.
    """
    other_commands = doctor.subprocess.run
    launches: list[tuple[list[str], dict[str, str]]] = []

    def run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        if f"booley.flows.{flow}" in argv and "--dry-run" not in argv:
            env = dict(kwargs.get("env") or {})
            launches.append((argv, env))
            answered = answer(argv, env)
            if answered is not None:
                return answered
        return other_commands(cmd, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "run", run)
    return launches


def write_bad_overlay(booley_dir: Path) -> None:
    """Add the sim known-bad fixture that arms Doctor's sim self-tests."""
    overlay = booley_dir / "selftest" / "sim" / "bad-overlay" / "firmware.hex"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("broken\n", encoding="utf-8")


def git_init(root: Path) -> None:
    run_bounded_child(["git", "init", "-q", str(root)], check=True)


def set_venue(monkeypatch, inside: bool) -> None:
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: inside)
    # The suite inherits the shell it runs in, and an agent CLI exports the
    # markers _check_host_agent_session keys off — a run started from Claude
    # Code would otherwise record different events than one from a plain
    # terminal. Pin them off; the agent case is set explicitly where wanted.
    _set_agent_session(monkeypatch, None)


_REAL_AGENT_SESSION_APP = runtime_context.agent_session_app


_AGENT_SESSION_ENV = {"claude": "CLAUDECODE", "codex": "CODEX_SANDBOX"}


def _set_agent_session(monkeypatch, app: str | None) -> None:
    """Simulate being spawned from *app*'s shell (None = a plain terminal)."""
    # Full-run fixtures stub the lookup; detection reads the real environment here.
    monkeypatch.setattr(runtime_context, "agent_session_app", _REAL_AGENT_SESSION_APP)
    for marker in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX", "CODEX_HOME"):
        monkeypatch.delenv(marker, raising=False)
    if app is not None:
        monkeypatch.setenv(_AGENT_SESSION_ENV[app], "1")


def runtime_setup(
    tmp_path, monkeypatch, *, inside, marker=True, app=None, slots="slots", valid=True
):
    """Set up a Project for a Doctor run in or out of the Sandbox; return its recorded commands."""
    booley_dir = write_project(tmp_path)
    calls = patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    if not valid:
        (booley_dir / "booley.toml").write_text("[project\n", encoding="utf-8")
    set_venue(monkeypatch, inside)
    _set_agent_session(monkeypatch, app)
    if inside:
        monkeypatch.setenv("BOOLEY_EGRESS", "unproxied")  # keeps the egress check off real DNS
        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / slots))
        if marker:
            monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        else:
            monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    return calls
