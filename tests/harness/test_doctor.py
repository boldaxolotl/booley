"""Tests for booley doctor setup audit."""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.audit import design_size, eda_environment, host_environment, project_schema
from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Findings, Severity
from booley.core.host_shell import quote_arg
from booley.flows.cli_selection import INVOCATION_ORIGIN_ENV
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.fusesoc import core_projection, selftest_overlay, target_inspection
from booley.harness import developer_probe, doctor, doctor_deep, host_diagnostics, web_isolation
from booley.harness.setup import readiness
from booley.presentation import colors
from booley.runtime import (
    auth_token,
    doctor_stamp,
    incontainer_setup,
    issuance_invalidation,
    project_gitignore,
    sandbox_apply,
    session_runtime,
)
from booley.runtime import devcontainer as dc
from booley.runtime import image_lifecycle as lifecycle
from booley.runtime.booley_dir import reset_cache, resolve_checkout_booley_dir
from booley.runtime.runtime_context import RuntimeLocation
from booley.runtime.sandbox_apply import UNKNOWN_SANDBOX_STEPS, Attachment
from booley.targets.catalog import TargetCatalog
from tests.agent_call_fake import install_agent
from tests.booley_checkouts_support import LAYOUTS, RUNTIMES, linked_worktree
from tests.conftest import add_git_command_config
from tests.devcontainer_spec_support import build_spec
from tests.diagnostic_helpers import _Rec
from tests.docker_cli_guard import route_docker_helper_through_subprocess_run
from tests.docker_host_support import fake_docker_host
from tests.git_command_fake import REAL_RUN
from tests.harness import test_session_runtime as resolution_fixtures
from tests.harness.doctor_environment_support import (
    BROWSER_RECORDS,
    answer_image_check,
    patch_doctor_environment,
    t3_pin,
)
from tests.harness.doctor_run_support import (
    finding_messages,
    git_init,
    run_doctor,
    runtime_setup,
    script_flow,
    set_venue,
    write_bad_overlay,
    write_project,
)
from tests.runtime.test_sandbox_remedy_workflows import (
    EXTERNAL,
    HEADLESS_REFRESH,
    VSCODE_INIT,
    VSCODE_SEED,
)
from tests.sandbox_venue_fake import place
from tests.subprocess_budget import run_bounded_child


@pytest.fixture(autouse=True)
def _docker_helper_uses_subprocess_run(monkeypatch):
    """This module fakes the Docker CLI through ``subprocess.run``."""
    route_docker_helper_through_subprocess_run(monkeypatch)


pytestmark = pytest.mark.usefixtures("isolated_git_attributes", "fake_sandbox_attachment")


project_sandbox_resolution = resolution_fixtures.project_sandbox_resolution
workspace = resolution_fixtures.workspace


def test_doctor_inputs_use_condition_selected_target_sources(tmp_path, monkeypatch):
    core = _core(
        "conditional",
        _target("sim_cond", filesets="sources", top="selected"),
        "  sources:\n    files:\n"
        "      - tool_verilator ? (rtl/selected.sv)\n"
        "      - tool_icarus ? (rtl/inactive.sv)\n"
        "      - tool_verilator ? (tb/selected.sv): {tags: [tb]}\n"
        "      - tool_icarus ? (tb/inactive.sv): {tags: [tb]}\n",
    )
    _core_project(
        tmp_path,
        monkeypatch,
        {
            "conditional.core": core,
            "rtl/selected.sv": "module selected; endmodule\n",
            "rtl/inactive.sv": "module inactive; endmodule\n",
            "tb/selected.sv": "module tb_selected; endmodule\n",
            "tb/inactive.sv": 'module tb_inactive; initial $display("[SIM_RESULT] PASSED"); endmodule\n',
        },
    )

    # The Icarus-only testbench carries the sentinel; Doctor must read the Verilator-selected one.
    assert list(_warnings(run_doctor(tmp_path), "sim.pass-sentinel-missing")) == ["sim_cond"]


def _answer_command(
    monkeypatch: pytest.MonkeyPatch,
    matches,
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> None:
    """Answer the commands *matches* selects; pass every other one to the current fake."""
    other_commands = doctor.subprocess.run

    def run(cmd, **kwargs):
        if matches([str(part) for part in cmd]):
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
        return other_commands(cmd, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "run", run)


def _forward_to_git(monkeypatch: pytest.MonkeyPatch, *tokens: str) -> None:
    """Run the Git commands naming any of *tokens* for real; every other one uses the current fake."""
    other_commands = doctor.subprocess.run

    def run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        if argv[:1] == ["git"] and any(token in argv for token in tokens):
            return REAL_RUN(cmd, **kwargs)
        return other_commands(cmd, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "run", run)


def _fixes(result: doctor.DoctorRunResult, text: str) -> list[str]:
    """Fix texts of the findings whose message contains *text*."""
    return [f.fix for f in result.findings if text in f.message]


def test_doctor_default_runs_tool_dry_runs_and_notes_missing_guidance(
    tmp_path, monkeypatch, capsys
):
    booley_dir = write_project(tmp_path)
    calls = patch_doctor_environment(monkeypatch, tmp_path, booley_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = colors.strip_ansi(capsys.readouterr().out)
    assert rc == 0
    assert "  ·  project guidance file missing" in output
    tool_calls = [call for call in calls if "booley.flows.sim" in call]
    assert tool_calls
    assert tool_calls[0][:2] == ["doc" + "ker", "exec"]
    assert "booley-session-test" in tool_calls[0]
    assert "--dry-run" in tool_calls[0]
    assert "--target" in tool_calls[0]
    assert tool_calls[0][tool_calls[0].index("--target") + 1] == "sim_fast"


@pytest.mark.parametrize(
    ("toplevel", "row"),
    [
        pytest.param("dut", "lint dry-run [lint_fast] passed", id="dut"),
        pytest.param(
            "no_such_top", "lint dry-run [lint_fast] failed with exit 2", id="no_such_top"
        ),
    ],
)
def test_plain_doctor_lint_dry_run_judges_the_toplevel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toplevel: str, row: str
) -> None:
    """Plain Doctor's lint dry-run runs the real Lint Flow, so an undeclared toplevel fails it."""
    from booley.flows.lint.flow import LintFlow

    booley_dir = write_project(tmp_path)
    core = tmp_path / "unit.core"
    original = core.read_bytes().replace(b"\r\n", b"\n")
    edited = original.replace(
        b"    filesets: [rtl]\n    toplevel: dut\n  synth_fast",
        f"    filesets: [rtl]\n    toplevel: {toplevel}\n  synth_fast".encode(),
    )
    assert (edited != original) == (toplevel != "dut")
    core.write_bytes(edited)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    other_commands = doctor.subprocess.run

    def run(cmd, **kwargs):
        argv = [str(part) for part in cmd]
        if "booley.flows.lint" not in argv:
            return other_commands(cmd, **kwargs)
        values = argv[argv.index("booley.flows.lint") + 1 :]
        values[values.index("--work-dir") + 1] = str(tmp_path)
        values[values.index("--report-dir") + 1] = str(tmp_path / "flow-reports")
        with monkeypatch.context() as scoped:
            scoped.setenv("BOOLEY_CONTAINER", "1")
            scoped.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
            place(scoped, RuntimeLocation.ISSUED_SANDBOX)
            outcome = LintFlow().execute_cli(values).outcome
        return subprocess.CompletedProcess(cmd, outcome.exit_code, outcome.report_text, "")

    monkeypatch.setattr(doctor.subprocess, "run", run)

    messages = finding_messages(run_doctor(tmp_path))

    assert row in messages


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode validation")
def test_doctor_default_reports_unsafe_private_store_as_flow_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_up = session_runtime.up_without_recovery
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    config_root = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    issuance_invalidation.prepare(str(tmp_path.resolve()))
    issuance_invalidation.cancel(str(tmp_path.resolve()))
    booley_config = config_root / "booley"
    booley_config.chmod(0o755)
    monkeypatch.setattr(doctor.session_runtime, "up_without_recovery", real_up)

    result = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    captured = capsys.readouterr()
    assert result == 1
    assert "  ✗  " in captured.out
    assert "could not enter the Sandbox" in captured.out
    assert str(booley_config) in captured.out
    assert "0755" in captured.out
    assert f"chmod 700 {booley_config}" in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_doctor_passes_interactive_checks_when_seeded(tmp_path, monkeypatch, capsys):
    booley_dir = write_project(tmp_path)  # seeds devcontainer + exclude
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 0
    assert "devcontainer.json matches the spec `booley init` would write" in output
    assert "Booley files excluded from git" in output


@pytest.mark.parametrize("concise", [False, True])
def test_doctor_clean_run_records_freshness_stamp(tmp_path, monkeypatch, capsys, concise):
    booley_dir = write_project(tmp_path)
    (tmp_path / ".git").mkdir(exist_ok=True)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    # This broad integration fixture intentionally leaves several environmental
    # warnings active.  Exercise the real waiver path so the clean-run contract
    # remains zero *unwaived* warnings.
    (booley_dir / "doctor-waivers.toml").write_text(
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
reason = "The fixture is not a committed RTL repository."
permanent = true
""",
        encoding="utf-8",
    )

    rc = doctor.run_doctor(
        argparse.Namespace(verbose=False, deep=False, concise=concise), tmp_path
    )

    assert rc == 0
    output = capsys.readouterr().out
    # A pass row is "  ✓  <message>"; the counts table's "✓ passed" row always prints.
    pass_row = "  ✓  devcontainer.json matches the spec `booley init` would write"
    assert (pass_row not in colors.strip_ansi(output)) is concise
    stamp = doctor_stamp.load_stamp(booley_dir)
    assert stamp is not None, output
    assert stamp["fingerprint"] == doctor_stamp.compute_fingerprint(booley_dir, tmp_path)
    # The stamp it just wrote must satisfy its own freshness check.
    assert doctor_stamp.check_stamp(booley_dir, tmp_path) is None


def test_doctor_warning_run_does_not_record_freshness_stamp(tmp_path, monkeypatch):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    assert rc == 0  # WARN remains advisory to CLI callers.
    assert doctor_stamp.load_stamp(booley_dir) is None


def test_doctor_failing_run_does_not_record_stamp(tmp_path, monkeypatch):
    booley_dir = write_project(tmp_path)
    # Missing asic_synthesize in the MCP probe is a hard FAIL (rc 1).
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir, mcp_tools=["bwave", "lint", "sim"])

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    assert rc == 1
    assert doctor_stamp.load_stamp(booley_dir) is None


def test_doctor_fails_when_issued_runtime_has_different_booley_identity(
    tmp_path, monkeypatch, capsys
):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir, runtime_booley_version="9.9.9")
    monkeypatch.setattr(
        doctor.inspection.idk,
        "compare_issued_build",
        lambda *_args, **_kwargs: doctor.inspection.image_identity.Comparison(
            doctor.inspection.image_identity.Status.MISMATCH,
            "version 1.0 -> 9.9.9; revision abc123 -> def456; "
            "wheel_source_fingerprint current -> stale",
        ),
    )

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert "revision abc123 -> def456" in output
    assert "wheel_source_fingerprint current -> stale" in output
    assert doctor_stamp.load_stamp(booley_dir) is None


def test_doctor_fails_and_does_not_stamp_agent_app_drift(tmp_path, monkeypatch, capsys):
    booley_dir = write_project(tmp_path)
    toml_path = booley_dir / "booley.toml"
    toml_path.write_text(
        toml_path.read_text(encoding="utf-8")
        .replace("[agent]\n", '[agent]\nprovider = "codex"\n')
        .replace('"subscription"', '"api_key"'),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fixture")
    dc.write_devcontainer(tmp_path, build_spec(dc.APP_CLAUDE))
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = colors.strip_ansi(capsys.readouterr().out)
    assert rc == 1
    assert "  ✗  devcontainer.json Agent CLIs ['claude'] (BOOLEY_AGENT_APP 'claude')" in output
    assert re.search(r"^  ✗ failed +1$", output, re.M)
    assert "[agent] clis ['codex']" in output
    assert doctor_stamp.load_stamp(booley_dir) is None


@pytest.mark.parametrize(
    ("auth_toml", "rows"),
    [
        (
            'auth = "auto"\n',
            [
                (
                    "claude: booley.toml [agent] auth 'auto' is retired; detected: subscription",
                    'replace auth = "auto" under [agent] with an [agent.auth] table that sets '
                    'claude = "subscription" in booley.toml, or re-run booley init',
                ),
                (
                    "codex: booley.toml [agent] auth 'auto' is retired; detected: api_key",
                    'replace auth = "auto" under [agent] with an [agent.auth] table that sets '
                    'codex = "api_key" in booley.toml, or re-run booley init',
                ),
            ],
        ),
        (
            '\n[agent.auth]\nclaude = "subscription"\ncodex = "auto"\n',
            [
                (
                    "codex: booley.toml [agent] auth 'auto' is retired; detected: api_key",
                    'set codex = "api_key" under [agent.auth] in booley.toml, '
                    "or re-run booley init",
                )
            ],
        ),
    ],
    ids=["string", "table"],
)
def test_doctor_names_the_value_for_a_retired_auto_auth(tmp_path, monkeypatch, auth_toml, rows):
    booley_dir = write_project(tmp_path)
    toml_path = booley_dir / "booley.toml"
    toml_path.write_text(
        toml_path.read_text(encoding="utf-8").replace(
            '[agent]\nauth = "subscription"\n',
            '[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n' + auth_toml,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fixture")
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)

    result = run_doctor(tmp_path)

    policy = [
        (f.message, f.fix)
        for f in result.findings
        if f.check_id == "agent.credential-policy" and f.severity is Severity.FAIL
    ]
    assert policy == rows
    messages = finding_messages(result)
    assert "project setup audit skipped - no valid project config" not in messages
    assert "worker backend health check skipped - set [agent] auth first" in messages
    assert not [m for m in messages if "skipped - project config invalid" in m]
    text = "\n".join(f"{f.message}\n{f.fix}" for f in result.findings)
    for absent in ("clis/default", "gateway", "booley doctor names"):
        assert absent not in text


def test_mcp_checks_report_spec_issued_for_other_cli_set(tmp_path, monkeypatch, capsys):
    booley_dir = write_project(tmp_path)
    toml_path = booley_dir / "booley.toml"
    toml_path.write_text(
        toml_path.read_text(encoding="utf-8").replace(
            "[agent]\n", '[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n'
        ),
        encoding="utf-8",
    )
    dc.write_devcontainer(tmp_path, build_spec(dc.APP_CLAUDE))
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = colors.strip_ansi(capsys.readouterr().out)
    assert rc == 1
    assert (
        "Agent CLIs ['claude'] (BOOLEY_AGENT_APP 'claude') != [agent] clis ['claude', 'codex']"
        in output
    )


def test_doctor_warns_when_devcontainer_missing(tmp_path, monkeypatch, capsys):
    booley_dir = write_project(tmp_path, seed_interactive=False)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    # Configuration currency warns; the independent host issuance check fails.
    assert rc == 1
    assert "Sandbox host issuance is invalid" in output
    assert "no .devcontainer/devcontainer.json" in output


def test_doctor_fails_when_devcontainer_tracked(tmp_path, monkeypatch, capsys):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
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


_MCP_PASS_ROW = "  ✓  MCP server exposes 5 MCP tool(s)"
_MCP_ADVISORY_ROW = (
    "  !  MCP missing documented interactive endpoint(s): mutation"
    "  interactive.mcp-tool-advisory-missing"
)
_MCP_TOOL_LIST_ROW = "      bwave, lint, sim, synth, reviewer"
_MCP_PROBE_TOOLS = ["bwave", "lint", "sim", "synth", "reviewer"]


def _full_run_rows_from(
    tmp_path: Path, monkeypatch, capsys, start_row: str, *, verbose: bool, probe_exit: int = 0
) -> tuple[int, list[str]]:
    """Run full Doctor in a fresh project; return its rc and the rows from *start_row* on."""
    tmp_path.mkdir()
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir, mcp_tools=_MCP_PROBE_TOOLS)
    if probe_exit:
        answer_other_commands = doctor.subprocess.run

        def failing_probe(cmd, **kwargs):
            if cmd[-3:] == ["python", "-m", "booley.mcp.probe"]:
                return subprocess.CompletedProcess(
                    cmd,
                    probe_exit,
                    stdout="",
                    stderr="Traceback (most recent call last):\n"
                    "ImportError: cannot import name x\nfinal line\n",
                )
            return answer_other_commands(cmd, **kwargs)

        monkeypatch.setattr(doctor.subprocess, "run", failing_probe)
    rc = doctor.run_doctor(argparse.Namespace(verbose=verbose, deep=False), tmp_path)
    lines = colors.strip_ansi(capsys.readouterr().out).splitlines()
    first = lines.index(start_row)
    return rc, lines[first : lines.index("T3 Code checks") + 1]


def test_doctor_mcp_probe_rows_and_exit_are_pinned(tmp_path, monkeypatch, capsys):
    """Rows and exit code of the MCP probe, captured from Doctor before the owner existed."""
    # Verbose: PASS, advisory WARN with its check ID, the tool list, then the next section.
    rc, rows = _full_run_rows_from(
        tmp_path / "v", monkeypatch, capsys, _MCP_PASS_ROW, verbose=True
    )
    assert (rc, rows) == (
        0,
        [_MCP_PASS_ROW, _MCP_ADVISORY_ROW, _MCP_TOOL_LIST_ROW, "", "T3 Code checks"],
    )
    # Not verbose: the tool list is hidden.
    rc, rows = _full_run_rows_from(
        tmp_path / "q", monkeypatch, capsys, _MCP_PASS_ROW, verbose=False
    )
    assert (rc, rows) == (0, [_MCP_PASS_ROW, _MCP_ADVISORY_ROW, "", "T3 Code checks"])
    # A failed probe: FAIL row, fix hint and the excerpt (always shown) in that order, exit 1.
    failed_row = "  ✗  MCP server probe failed with exit 3"
    rc, rows = _full_run_rows_from(
        tmp_path / "f", monkeypatch, capsys, failed_row, verbose=False, probe_exit=3
    )
    assert (rc, rows) == (
        1,
        [
            failed_row,
            "     → rebuild sandbox image",
            "      Traceback (most recent call last):",
            "      ImportError: cannot import name x",
            "      final line",
            "",
            "T3 Code checks",
        ],
    )


def test_full_run_doctor_answers_mcp_probe_through_owner_without_real_container(
    tmp_path, monkeypatch
):
    """Full Doctor reaches the probe only through the owner, answered by the fake."""
    booley_dir = write_project(tmp_path)
    calls = patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    inspected: list[object] = []
    real_inspect = doctor.server_inspection.inspect_mcp_server

    def spy(request):
        inspected.append(request)
        return real_inspect(request)

    monkeypatch.setattr(doctor.server_inspection, "inspect_mcp_server", spy)

    doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    assert len(inspected) == 1
    assert [call for call in calls if call[-3:] == ["python", "-m", "booley.mcp.probe"]]


def _history_git(repo: Path, *args: str) -> None:
    run_bounded_child(["git", *args], cwd=repo, check=True)


_COMPLETE_LOGICAL_SYNTH_REPORT = {
    "synth_mode": "logical",
    "termination": "completed",
    "ppa_complete": True,
    "area_source": "yosys_mapped",
    "timing_complete": False,
    "per_clock": [],
}
_CONTAINER_CLI = "doc" + "ker"


def _write_synth_report(report_dir: Path, report: dict, target: str = "fast") -> None:
    """Write *report* where Doctor reads a Target's deep synthesis report."""
    from booley.flows.implementation_publication import target_report_path

    path = target_report_path("synth", target, report_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")


def _write_complete_synth_report(booley_dir: Path, target: str = "synth_fast") -> None:
    """Write the terminal logical-synthesis report a real deep synth run leaves.

    It carries no ``peak_rss_mb``, so Doctor's memory calibration reports the
    measurement as unavailable instead of depending on host memory.
    """
    _write_synth_report(
        booley_dir / doctor.DOCTOR_TMP_DIR / "flow-reports", _COMPLETE_LOGICAL_SYNTH_REPORT, target
    )


def _deep_synth_writes_complete_report(monkeypatch, booley_dir: Path) -> None:
    """Make the faked Sandbox leave a complete report after each deep synth command."""
    base_run = doctor.subprocess.run

    def run(cmd, **kwargs):
        result = base_run(cmd, **kwargs)
        if "booley.flows.synth" in cmd and "--dry-run" not in cmd:
            _write_complete_synth_report(booley_dir)
        return result

    monkeypatch.setattr(doctor.subprocess, "run", run)


def test_doctor_deep_runs_first_config_without_dry_run(tmp_path, monkeypatch):
    # A leftover configs.toml is ignored: it pins no `--test` for the deep smoke.
    booley_dir = write_project(tmp_path)
    (booley_dir / "configs.toml").write_text(
        '[lint_fast]\ndefines = []\ntop_module = "dut"\ntests = ["smoke", "full"]\n',
        encoding="utf-8",
    )
    calls = patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    _deep_synth_writes_complete_report(monkeypatch, booley_dir)
    starts: list[Path] = []
    issue_session = doctor.session_runtime.up_without_recovery
    monkeypatch.setattr(
        doctor.session_runtime,
        "up_without_recovery",
        lambda root: starts.append(root) or issue_session(root),
    )
    launches = script_flow(monkeypatch, "sim", lambda _argv, _env: None)

    result = run_doctor(tmp_path, deep=True)

    assert result.exit_code == 0
    # One Sandbox entry serves every Flow command, dry and deep.
    assert len(starts) == 1
    assert len([c for c in calls if any(p.startswith("booley.flows.") for p in c)]) == 6
    dry, deep = [call for call in calls if "booley.flows.sim" in call]
    for argv in (dry, deep):
        assert argv[:3] == [_CONTAINER_CLI, "exec", "-e"]
        assert "booley-session-test" in argv
    assert "--dry-run" in dry
    assert "--dry-run" not in deep
    assert deep[deep.index("--target") + 1] == "sim_fast"
    assert "--test" not in deep
    assert "--tb-top" not in deep
    assert "--memory" not in deep
    assert f"{INVOCATION_ORIGIN_ENV}=transport" in deep
    assert "BOOLEY_INTERNAL_DOCTOR_RUN=1" in deep
    # Doctor's Flow runs write no Resource History records (#3235).
    assert [env["BOOLEY_INTERNAL_DOCTOR_RUN"] for _argv, env in launches] == ["1"]
    [synth_deep] = [
        call for call in calls if "booley.flows.synth" in call and "--dry-run" not in call
    ]
    assert synth_deep[:3] == [_CONTAINER_CLI, "exec", "-e"]
    assert "--memory" not in synth_deep


@pytest.mark.parametrize("waived", [False, True])
def test_doctor_deep_surfaces_synthesis_warning_verdict(tmp_path, monkeypatch, capsys, waived):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    if waived:
        (booley_dir / "doctor-waivers.toml").write_text(
            'version = 1\n\n[[waiver]]\ncheck = "flow.synth-deep-warning"\n'
            'subject = "synth_fast"\nreason = "Hold slack is reviewed."\npermanent = true\n',
            encoding="utf-8",
        )

    def warn_verdict(argv, _env):
        _write_complete_synth_report(booley_dir)
        return subprocess.CompletedProcess(
            argv, 0, stdout="RESULT: WARN -- timing VIOLATED (hold slack -0.182 ns)\n", stderr=""
        )

    script_flow(monkeypatch, "synth", warn_verdict)

    result = run_doctor(tmp_path, deep=True, verbose=waived, record_clean=True)

    output = colors.strip_ansi(capsys.readouterr().out)
    warn_message = "synth deep check [synth_fast] returned a WARN verdict"
    [finding] = [f for f in result.findings if f.check_id == "flow.synth-deep-warning"]
    assert finding.message == warn_message
    assert finding.subject == "synth_fast"
    assert result.exit_code == 0
    if waived:
        assert finding.severity is Severity.WAIVED
        assert finding.fix == "Hold slack is reviewed."
        assert output.index(f"  ~  {warn_message}") < output.index(
            "RESULT: WARN -- timing VIOLATED"
        )
        return
    assert finding.severity is Severity.WARN
    warn_row = f"  !  {warn_message}"
    lines = output.splitlines()
    [warn_index] = [i for i, line in enumerate(lines) if line.startswith(warn_row)]
    assert lines[warn_index + 1].startswith(
        "     → run `booley flow synth --target synth_fast` and resolve"
    )
    # Issue #885: a flushed running-step line precedes the long check, and the
    # summary states agent spend (the probe SKIPs host-side, so none).
    run_line = "  ▸  synth deep check [synth_fast] (real EDA run; timeout"
    assert output.index(run_line) < output.index(warn_row)
    assert "Agent-backed checks: no agent call ran (0 tokens, $0.00)." in output
    assert doctor_stamp.load_stamp(booley_dir) is None


def _healthy_doctor_backend(monkeypatch):
    from booley.runtime import agent_config

    monkeypatch.setattr(agent_config, "load_backend_config", lambda _root: None)
    monkeypatch.setattr(
        agent_config,
        "get_backend_config",
        lambda: SimpleNamespace(
            active_backend=SimpleNamespace(name="Codex", health_check=lambda: "")
        ),
    )


@pytest.mark.parametrize("deep", [False, True])
def test_doctor_backend_hint_matches_mode(tmp_path, monkeypatch, capsys, deep):
    from booley.harness import developer_probe

    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    _healthy_doctor_backend(monkeypatch)
    set_venue(monkeypatch, True)
    monkeypatch.setenv("BOOLEY_EGRESS", "unproxied")
    rss_calls, model_calls = [], []

    def measure(root):
        rss_calls.append(root)
        return developer_probe.ProbeMeasurement(
            _GIB, True, developer_probe.ProbeUsage(1, 0, 1, 0.0)
        )

    def probe(_root, model, **_kw):
        model_calls.append(model)
        return developer_probe.ProbeUsage(1, 0, 1, 0.0)

    monkeypatch.setattr(developer_probe, "measure_developer_rss", measure)
    monkeypatch.setattr(developer_probe, "probe_model_access", probe, raising=False)
    run_doctor(tmp_path, deep=deep)
    output = capsys.readouterr().out
    assert ("run `booley doctor --deep`" in output) is not deep
    assert ("worker backend configured locally: Codex" in output) is not deep
    assert ("Deep checks" in output) is deep
    assert (
        "Specialist provider live authorization check completed successfully" in output
    ) is deep
    # Plain Doctor never calls a model; deep Doctor probes each distinct Specialist model.
    assert rss_calls == ([tmp_path] if deep else [])
    assert bool(model_calls) is deep


@pytest.mark.parametrize("row", ["inside", "host", "unresolved"])
def test_sandbox_mcp_server_check_runs_only_inside_sandbox(row, tmp_path, monkeypatch, capsys):
    from booley.runtime import incontainer_setup

    runtime_setup(tmp_path, monkeypatch, inside=row != "host")
    monkeypatch.delenv("BOOLEY_AGENT_CLIS", raising=False)
    # `auto` with no published record is the unresolved choice.
    monkeypatch.setenv("BOOLEY_AGENT_APP", "auto" if row == "unresolved" else "codex")
    requests: list[incontainer_setup.HttpServerInspectionRequest] = []

    def inspect(request: incontainer_setup.HttpServerInspectionRequest) -> DiagnosticReport:
        assert row != "unresolved", "owner must not run"
        requests.append(request)
        findings = Findings()
        findings.fail("Sandbox MCP server not serving on 127.0.0.1:1", "rebuild the Sandbox")
        findings.detail("    log tail line", verbose_only=False)
        return findings.report()

    monkeypatch.setattr(incontainer_setup, "inspect_http_server", inspect)

    result = run_doctor(tmp_path)

    served = [f for f in result.findings if "Sandbox MCP server not serving" in f.message]
    if row == "inside":
        assert "log tail line" in capsys.readouterr().out
        assert [request.agent_clis for request in requests] == [("codex",)]
        assert [(f.severity, f.fix) for f in served] == [(Severity.FAIL, "rebuild the Sandbox")]
        assert result.exit_code == 1
    else:
        assert requests == [] and served == []
        assert not any(
            "Sandbox MCP server" in m for m in finding_messages(result) if "skipped" not in m
        )
    assert any("Sandbox MCP server check skipped" in m for m in finding_messages(result)) is (
        row == "unresolved"
    )


def test_doctor_deep_host_preserves_probe_skip(tmp_path, monkeypatch, capsys):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    _healthy_doctor_backend(monkeypatch)
    result = run_doctor(tmp_path, deep=True)
    output = capsys.readouterr().out
    assert "run `booley doctor --deep`" not in output
    assert "worker backend configured locally: Codex" not in output
    assert "Specialist provider memory probe: runs in-container" in output
    assert result.deep_status.current is False


def test_doctor_skip_agent_checks_omits_credentials_and_live_probe(tmp_path, monkeypatch, capsys):
    from booley.runtime import agent_config

    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    _deep_synth_writes_complete_report(monkeypatch, booley_dir)

    def unexpected_agent_check(*_args, **_kwargs):
        raise AssertionError("agent check should have been skipped")

    # The public owners the skipped checks would call: any call here means a check ran.
    monkeypatch.setattr(developer_probe, "measure_developer_rss", unexpected_agent_check)
    monkeypatch.setattr(
        developer_probe, "probe_model_access", unexpected_agent_check, raising=False
    )
    monkeypatch.setattr(auth_token, "credential_plan", unexpected_agent_check)
    monkeypatch.setattr(agent_config, "get_backend_config", unexpected_agent_check)

    rc = doctor.run_doctor(
        argparse.Namespace(verbose=False, deep=True, skip_agent_checks=True), tmp_path
    )

    output = capsys.readouterr().out
    assert rc == 0
    assert "agent credential checks skipped by --skip-agent-checks" in output
    assert "worker backend health check skipped by --skip-agent-checks" in output
    assert (
        "Specialist provider authorization and memory probe skipped by --skip-agent-checks"
        in output
    )
    assert "no agent call ran" in output
    assert "run `booley doctor --deep`" not in output
    assert doctor_stamp.load_stamp(booley_dir) is None


# ---------------------------------------------------------------------------
# Sandbox routing for Doctor Flow checks.
# ---------------------------------------------------------------------------


def _vlnv_core(name: str, targets: str) -> str:
    """CAPI2 text for core ``acme:ip:<name>`` with the given Target blocks."""
    return f"CAPI=2:\nname: acme:ip:{name}:0\nfilesets:\n{_RTL_TB}targets:\n{targets}"


def _fpga_core(*names: str, marked: bool = True) -> str:
    """A core whose generic Targets *names* are FPGA Doctor Targets when *marked*."""
    marker = ", booley: {doctor: [fpga]}" if marked else ""
    options = f"tool: verilator, part: xc7a35tcsg324-1{marker}"
    return _core("design", "".join(_target(n, "generic", options, "rtl", "dut") for n in names))


def _flow_argv_target(argv: list[str]) -> str:
    return argv[argv.index("--target") + 1]


@pytest.mark.parametrize("mounted", [False, True])
def test_deep_check_outcomes_are_graded_per_target(tmp_path, monkeypatch, capsys, mounted):
    targets = "".join(
        _target(name, doctor=True) for name in ("sim_exit", "sim_hang", "sim_noexec")
    )
    booley_dir = _core_project(
        tmp_path, monkeypatch, {"outcomes.core": _core("outcomes", targets)}
    )
    _deep_synth_writes_complete_report(monkeypatch, booley_dir)
    if mounted:
        monkeypatch.setattr(doctor.dc, "BOOLEY_DIR_TARGET", booley_dir.as_posix())

    def answer(argv, _env):
        target = _flow_argv_target(argv)
        if target == "sim_hang":
            raise subprocess.TimeoutExpired(argv, 1)
        if target == "sim_noexec":
            raise OSError("no exec")
        if target == "sim_exit":
            stderr = "\n".join(f"line {index}" for index in range(12))
            return subprocess.CompletedProcess(argv, 3, stdout="", stderr=stderr)
        return None

    script_flow(monkeypatch, "sim", answer)

    result = run_doctor(tmp_path, deep=True, record_clean=True)

    output = colors.strip_ansi(capsys.readouterr().out)
    report_dir = (
        ".booley/tmp/doctor/flow-reports (under the Project Root)"
        if mounted
        else str(booley_dir / "tmp" / "doctor" / "flow-reports")
    )
    deep_rows = [
        (f.severity, f.message, f.fix) for f in result.findings if "sim deep check" in f.message
    ]
    assert deep_rows == [
        (Severity.FAIL, "sim deep check [sim_exit] failed with exit 3", f"see {report_dir}"),
        (Severity.FAIL, "sim deep check [sim_hang] timed out after 4830s", f"see {report_dir}"),
        (
            Severity.FAIL,
            "sim deep check [sim_noexec] failed to start: no exec",
            f"see {report_dir}",
        ),
        (Severity.PASS, "sim deep check [sim_fast] passed", ""),
    ]
    # A failing run's excerpt always shows, right after its fix line.
    assert f"     → see {report_dir}\n      line 0\n      line 1\n" in output
    # A timeout and a launch failure leave their checks due; a graded exit does not.
    record = doctor_deep.load_deep_record(booley_dir)
    assert record is not None
    assert len([reason for reason in record.reasons if reason.startswith("sim.smoke.")]) == 2


@pytest.mark.parametrize(
    ("error", "fix"),
    [
        ("bad issuance", VSCODE_INIT + ", then retry"),
        (
            "Sandbox start refused: host is at capacity",
            "free Sandbox capacity using the command in the refusal, then retry",
        ),
    ],
    ids=["entry-failure", "capacity"],
)
def test_doctor_deep_fails_hard_when_issued_runtime_cannot_start(
    tmp_path, monkeypatch, fake_sandbox_attachment, error, fix
):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    fake_sandbox_attachment(Attachment.VSCODE)

    def fail_up(_root):
        raise doctor.session_runtime.SessionError(error)

    monkeypatch.setattr(doctor.session_runtime, "up_without_recovery", fail_up)

    result = run_doctor(tmp_path, deep=True)

    assert result.exit_code == 1
    entry_failures = [
        (f.severity, f.message, f.fix)
        for f in result.findings
        if f.message.startswith("sim deep check")
    ]
    assert entry_failures == [
        (Severity.FAIL, f"sim deep check [sim_fast] could not enter the Sandbox: {error}", fix)
    ]


def test_deep_check_skips_when_runtime_unavailable(tmp_path, monkeypatch):
    """QA-2: without a container runtime a deep check SKIPs and leaves deep checks due.

    It is not a hard FAIL, and the skip reason must not push the useless `booley init --force`.
    """
    booley_dir = write_project(tmp_path)
    write_bad_overlay(booley_dir)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    monkeypatch.setattr(
        host_diagnostics,
        "inspect_host",
        lambda _root: host_diagnostics.HostDiagnosticResult(DiagnosticReport(()), None),
    )

    result = run_doctor(tmp_path, deep=True, record_clean=True)

    skipped = "skipped - 'docker' runtime not available"
    checked = [
        f
        for f in result.findings
        if ("deep check" in f.message or "self-test" in f.message)
        and f.check_id != "flow.fail-path-unvalidated"
    ]
    assert [(f.severity, f.message) for f in checked] == [
        (Severity.SKIP, f"sim deep check [sim_fast] {skipped}"),
        (Severity.SKIP, f"lint deep check [lint_fast] {skipped}"),
        (Severity.SKIP, f"synth deep check [synth_fast] {skipped}"),
        (Severity.SKIP, f"sim self-test good case 'sim_fast' {skipped}"),
        (Severity.SKIP, f"sim self-test bad case 'sim_fast + bad overlay' {skipped}"),
    ]
    assert not any("booley init --force" in f"{f.message} {f.fix}" for f in checked)
    record = doctor_deep.load_deep_record(booley_dir)
    assert record is not None
    for flow in ("sim", "lint", "synth"):
        assert any(reason.startswith(f"{flow}.smoke.") for reason in record.reasons)


def test_deep_sim_smoke_pins_one_runnable_tests_toml_test(tmp_path, monkeypatch):
    """The deep smoke runs one runnable test: not the whole list, not a skipped test."""
    pinned = "".join(
        _target(n, doctor=True) for n in ("sim_core", "sim_pin", "sim_skip", "sim_allskip")
    )
    booley_dir = _core_project(
        tmp_path,
        monkeypatch,
        {
            "c910.core": _vlnv_core("c910", pinned),
            "other.core": _vlnv_core("other", _target("sim_core", doctor=True)),
            ".booley/tests.toml": (
                '[sim_core]\ntests = ["shared"]\n'
                '[sim_pin]\ntests = ["first", "second"]\n'
                '[sim_skip]\ntests = ["hang", "runnable"]\nskip = ["hang"]\n'
                '[sim_allskip]\ntests = ["only"]\nskip = ["only"]\n'
            ),
        },
    )
    _deep_synth_writes_complete_report(monkeypatch, booley_dir)
    launches = script_flow(monkeypatch, "sim", lambda _argv, _env: None)

    run_doctor(tmp_path, deep=True)

    smoke_tests = {
        _flow_argv_target(argv): argv[argv.index("--test") + 1] if "--test" in argv else None
        for argv, _env in launches
    }
    # Two cores share `sim_core`, so their selectors are VLNV-qualified (ADR 0030) while
    # tests.toml keys stay bare; an all-skip list still smokes its declared head.
    assert smoke_tests == {
        "c910#sim_core": "shared",
        "other#sim_core": "shared",
        "sim_pin": "first",
        "sim_skip": "runnable",
        "sim_allskip": "only",
        "sim_fast": None,
    }


_BARE_TIMEOUTS = ("1h 20m", "5m 00s", "33m 00s", "2h 03m")


@pytest.mark.parametrize(
    ("sim", "lint_ms", "synth_ms", "fpga_ms", "expected"),
    [
        ("", None, None, None, _BARE_TIMEOUTS),
        # A raised Flow timeout is honored (plus finalization headroom); a lower sim one shortens
        # the budget but never below the floor.
        (
            "timeout_ms = 60000\n",
            3_600_000,
            5_400_000,
            10_800_000,
            ("1h 11m", "1h 00m", "1h 33m", "3h 03m"),
        ),
        # Below the floors the floor wins; sim honors timeout_ms plus build_timeout_ms.
        (
            "timeout_ms = 5000\nbuild_timeout_ms = 7000\n",
            1000,
            None,
            60_000,
            ("15m 00s", "5m 00s", "33m 00s", "2h 03m"),
        ),
    ],
    ids=["bare", "raised", "floor"],
)
def test_deep_timeout_honors_configured_timeout_ms(
    tmp_path, monkeypatch, capsys, sim, lint_ms, synth_ms, fpga_ms, expected
):
    """--deep honors the Flow timeout and leaves synth finalization headroom (F5)."""
    fpga = "\n[flows.fpga]\n" + (f"timeout_ms = {fpga_ms}\n" if fpga_ms else "")
    booley_dir = _core_project(
        tmp_path, monkeypatch, {"design.core": _fpga_core("fpga_board")}, sim=sim, toml=fpga
    )
    for flow, timeout_ms in (("lint", lint_ms), ("synth", synth_ms)):
        if timeout_ms:
            _edit_booley_toml(
                booley_dir, f"[flows.{flow}]\n", f"[flows.{flow}]\ntimeout_ms = {timeout_ms}\n"
            )
    _deep_synth_writes_complete_report(monkeypatch, booley_dir)

    run_doctor(tmp_path, deep=True)

    output = colors.strip_ansi(capsys.readouterr().out)
    announced = [
        f"  ▸  {flow} deep check [{target}] (real EDA run; timeout {timeout})"
        for flow, target, timeout in zip(
            ("sim", "lint", "synth", "fpga"),
            ("sim_fast", "lint_fast", "synth_fast", "fpga_board"),
            expected,
            strict=True,
        )
    ]
    for row in announced:
        assert row in output


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
# "no call sites found" guard below catches a wholesale rename. These names are read from
# production source on purpose (the repository-text scan of #4359), not called.
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
    r"<<\s*'(?P<tag>\w+)'[^\n]*\n(?P<body>.*?)\n(?P=tag)$", re.DOTALL | re.MULTILINE
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
            if table
            not in project_schema.KNOWN_BOOLEY_TOML_TABLES
            | project_schema.IGNORED_BOOLEY_TOML_TABLES
        }
        assert unknown == {}, f"live booley.toml tables doctor calls ignored: {unknown}"

    def test_every_table_read_by_a_shell_script_is_recognized(self):
        """Configuration read by setup shell helpers remains recognized."""
        tables = _tables_read_by_shell_scripts()

        unknown = {
            table: where
            for table, where in tables.items()
            if table
            not in project_schema.KNOWN_BOOLEY_TOML_TABLES
            | project_schema.IGNORED_BOOLEY_TOML_TABLES
        }
        assert unknown == {}, f"live booley.toml tables doctor calls ignored: {unknown}"

    def test_submodule_table_is_read_by_python_setup(self):
        tables = _tables_read_by_production_code()
        assert "submodules" in tables

    @pytest.mark.parametrize("asic", [True, False])
    @pytest.mark.parametrize("fpga_part", [None, "xc7a35tcpg236-1"])
    def test_scaffolded_booley_toml_is_fully_recognized(self, asic, fpga_part):
        """`booley init --scaffold` must not write a table its own doctor warns about."""
        # Private on purpose: the scaffold's TOML text is also read by name from production
        # source (the repository-text scan of #4359).
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


def test_doctor_uses_configured_sandbox_image(tmp_path, monkeypatch):
    booley_dir = write_project(tmp_path, sandbox_image="ibex-booley-sandbox:latest")
    calls = patch_doctor_environment(monkeypatch, tmp_path, booley_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    assert rc == 0
    docker_runs = [
        call for call in calls if len(call) > 3 and call[0] == "docker" and call[1] == "run"
    ]
    assert docker_runs
    assert any("ibex-booley-sandbox:latest" in call for call in docker_runs)


def test_riscv_image_with_a_missing_document_fails_with_the_rebuild_fix(tmp_path, monkeypatch):
    """A RISC-V image missing an offline document FAILs; the other probes still pass."""
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    _answer_command(
        monkeypatch,
        lambda cmd: "{{json .Config.Env}}" in cmd,
        stdout='["BOOLEY_CONTAINER=1", "BOOLEY_SANDBOX_FLAVOR=riscv"]\n',
    )
    _answer_command(monkeypatch, lambda cmd: "riscv-debug-specification.pdf" in cmd, returncode=1)

    result = run_doctor(tmp_path)

    complete = "RISC-V offline specs complete at $BOOLEY_RISCV_DOCS"
    assert [(f.severity, f.fix) for f in result.findings if f.message == complete] == [
        (Severity.FAIL, eda_environment.RISCV_IMAGE_FIX)
    ]
    # Every-document and per-document coverage: tests/audit/test_eda_environment.py.
    assert "riscv32-unknown-elf-gcc" in finding_messages(result, Severity.PASS)


def test_doctor_fails_when_job_logs_tracked(tmp_path, monkeypatch, capsys):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    base_run = doctor.subprocess.run

    def run_with_tracked_logs(cmd, **kwargs):
        if cmd[:4] == ["git", "ls-files", "--", "job_logs"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="job_logs/session/a0f4\n", stderr="")
        return base_run(cmd, **kwargs)

    monkeypatch.setattr(doctor.subprocess, "run", run_with_tracked_logs)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert rc == 1
    assert "job_logs/ has 1 tracked file(s)" in output
    assert "git rm -r --cached job_logs" in output


def test_doctor_warns_about_leftover_interactive_logs_and_keeps_them(
    tmp_path, monkeypatch, capsys
):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    main_dir = tmp_path / "main-booley"
    leftover = main_dir / ".interactive_logs"
    (leftover / "s1").mkdir(parents=True)
    (leftover / "s1" / "a.log").write_bytes(b"old session\n")
    # A temp project has no separate main checkout; the check derives it from git metadata.
    monkeypatch.setattr(doctor, "resolve_common_booley_dir", lambda _root: main_dir)

    doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = " ".join(colors.strip_ansi(capsys.readouterr().out).split())
    command = (
        f"Remove-Item -LiteralPath '{leftover}' -Recurse -Force"
        if os.name == "nt"
        else f"rm -rf {leftover}"
    )
    assert f"{leftover} holds session logs from an earlier Booley version" in output
    assert f"delete it: `{command}`" in output
    assert (leftover / "s1" / "a.log").read_bytes() == b"old session\n"


@pytest.mark.skipif(
    not hasattr(os, "getuid"),
    reason="uid probe is POSIX-only; doctor skips this check on Windows "
    "(no os.getuid, st_uid is always 0) — F-16",
)
def test_doctor_fails_on_container_uid_mismatch(tmp_path, monkeypatch, capsys):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
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


def test_doctor_sim_dry_run_follows_tb_top_resolution(tmp_path, monkeypatch):
    # No static tb_top source exists: a leftover configs.toml pinning one does not stop the skip.
    booley_dir = write_project(tmp_path)
    (booley_dir / "configs.toml").write_text(
        '[lint_fast]\ndefines = []\ntop_module = "dut"\ntb_top = "tb"\n', encoding="utf-8"
    )
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    _answer_command(
        monkeypatch,
        lambda argv: "booley.flows.sim" in argv,
        returncode=2,
        stderr="sim: --tb-top is required when running outside a state-backed run",
    )

    result = run_doctor(tmp_path)

    message = "sim dry-run [sim_fast] skipped - tb_top is resolved at runtime"
    assert [f.severity for f in result.findings if f.message.startswith(message)] == [
        Severity.SKIP
    ]
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# .core audit (ADR 0022 Phases 6-7), driven through a whole Doctor run
# ---------------------------------------------------------------------------


_RTL_TB = "  rtl: {files: [rtl/dut.sv]}\n  tb: {files: [tb/tb.sv], tags: [tb]}\n"


def _core_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str | None],
    *,
    sim: str = "",
    toml: str = "",
) -> Path:
    """The `write_project` fixture plus *files* (None deletes), patched for a run.

    *sim* is TOML added to the fixture's `[flows.sim]` table; *toml* is appended to `booley.toml`.
    """
    booley_dir = write_project(tmp_path)
    config = booley_dir / "booley.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("[flows.sim]\n", f"[flows.sim]\n{sim}") + toml,
        encoding="utf-8",
    )
    for relative, text in files.items():
        path = tmp_path / relative
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    return booley_dir


def _core(name: str, targets: str, filesets: str = _RTL_TB) -> str:
    """CAPI2 text for core *name* with the given fileset and Target blocks."""
    return f"CAPI=2:\nname: ::{name}:0\nfilesets:\n{filesets}targets:\n{targets}"


def _target(
    name: str,
    flow: str = "sim",
    options: str = "tool: verilator",
    filesets: str = "rtl, tb",
    top: str = "tb",
    *,
    doctor: bool = False,
) -> str:
    """A Target block; *doctor* marks it as the Doctor Target of its Flow."""
    marker = f", booley: {{doctor: [{flow}]}}" if doctor else ""
    return (
        f"  {name}:\n    flow: {flow}\n    flow_options: {{{options}{marker}}}\n"
        f"    filesets: [{filesets}]\n    toplevel: {top}\n"
    )


def _rows(result: doctor.DoctorRunResult, severity: Severity, text: str) -> list[str]:
    """Messages of *severity* that contain *text* (a Target name in quotes, a phrase)."""
    return [f.message for f in result.findings if f.severity is severity and text in f.message]


def _warnings(result: doctor.DoctorRunResult, check_id: str) -> dict[str, str]:
    """Subject to message of the active WARN rows with *check_id*."""
    return {
        str(f.subject): f.message
        for f in result.findings
        if f.severity is Severity.WARN and f.check_id == check_id
    }


class TestCoreAudit:
    def test_project_without_a_core_fails(self, tmp_path, monkeypatch):
        # ADR 0039: a resolvable .core Target is a precondition, so a Project
        # without one is a setup failure, not a skip.
        _core_project(tmp_path, monkeypatch, {"unit.core": None})

        assert _rows(run_doctor(tmp_path), Severity.FAIL, "project has no .core")

    def test_each_unusable_target_fails(self, tmp_path, monkeypatch):
        all_fs = _RTL_TB + "  all: {files: [rtl/dut.sv, tb/tb.sv]}\n"
        design = _core(
            "design",
            _target("sim_untagged", filesets="all") + _target("sim_empty", filesets=""),
            all_fs,
        )

        def defines(label, shapes):
            return _core(
                f"defines_{label}",
                "".join(
                    _target(
                        f"sim_{name}", options=f"tool: verilator, defines: {shape}", doctor=True
                    )
                    for name, shape in shapes
                ),
            )

        _core_project(
            tmp_path,
            monkeypatch,
            {
                "design.core": design,
                # No Flow applies `flow_options.defines`: the key fails by presence, even
                # when null. A null value also fails CAPI2 validation, which makes FuseSoC
                # ignore its whole core, so it needs a core of its own.
                "defines.core": defines(
                    "values", (("map", "{A: 1}"), ("list", "[A, B=2]"), ("string", "A"))
                ),
                "defines_null.core": defines("null", (("null", ""),)),
                ".booley/tests.toml": '[sim_fast]\nselect = "not-a-plusarg with spaces"\n',
            },
        )

        result = run_doctor(tmp_path)

        failures = {f.message: f.fix for f in result.findings if f.severity is Severity.FAIL}
        assert _rows(
            result, Severity.FAIL, "sim Target 'sim_untagged' has files but none tagged tags:[tb]"
        )
        assert _rows(result, Severity.FAIL, "sim Target 'sim_empty' stages zero files")
        for name in ("map", "list", "string", "null"):
            [fix] = [
                fix
                for message, fix in failures.items()
                if f"#sim_{name}'" in message and "flow_options.defines" in message
            ]
            assert "vlogdefine" in fix
        assert _rows(result, Severity.FAIL, "tests.toml invalid")

    def test_sim_verdict_rows_name_the_right_remedy(self, tmp_path, monkeypatch):
        fs = (
            _RTL_TB
            + "  tb_quiet: {files: [tb/quiet.sv], tags: [tb]}\n"
            + "  tb_py: {files: [tb/test_dut.py], tags: [tb]}\n"
            # Only the last of these testbench files carries the sentinel; the first is absent.
            + "  tb_split: {files: [tb/absent.sv, tb/quiet.sv, tb/tb.sv], tags: [tb]}\n"
        )
        cocotb = "tool: verilator, cocotb_module: test_dut"
        design = _core(
            "design",
            _target("sim_quiet", filesets="rtl, tb_quiet")
            # F-8: a Python TB that has not declared its cocotb module yet.
            + _target("sim_py", filesets="rtl, tb_py", top="dut")
            + _target("sim_split", filesets="rtl, tb_split")
            # B2: a cocotb TB gets its verdict from results.xml, never a sentinel.
            + _target("sim_cocotb", options=cocotb, filesets="rtl, tb_py", top="dut")
            + _target("lint_dup", flow="lint", filesets="rtl", top="dut"),
            fs,
        )
        # ADR 0030: two distinct cores may declare the same bare Target name.
        other = _core("other", _target("lint_dup", flow="lint", filesets="rtl", top="dut"))
        _core_project(
            tmp_path,
            monkeypatch,
            {
                "design.core": design,
                "other.core": other,
                "tb/quiet.sv": "// a testbench with no verdict marker\n",
                "tb/test_dut.py": "async def test_reset(dut): pass\n",
            },
        )

        result = run_doctor(tmp_path)

        missing = _warnings(result, "sim.pass-sentinel-missing")
        assert set(missing) == {"sim_quiet", "sim_py"}
        assert "refs/sim_result_sentinel.sv" in missing["sim_quiet"]
        assert "cocotb_module" not in missing["sim_quiet"]
        assert "cocotb_module" in missing["sim_py"]
        assert "refs/sim_result_sentinel.sv" not in missing["sim_py"]
        assert not [m for m in _rows(result, Severity.PASS, "sim_cocotb") if "sentinel" in m]
        for name in ("sim_fast", "sim_split"):
            assert _rows(
                result, Severity.PASS, f"sim Target '{name}' TB emits a recognized pass sentinel"
            )
        # A Verilator lint Target has no testbench to tag, and the duplicate name is no collision.
        for text in ("lint_fast", "sim_py", "sim_cocotb", "collision"):
            assert not _rows(result, Severity.FAIL, text)
        assert _rows(result, Severity.PASS, ".core Targets enumerated: lint_dup,")

    def test_configured_pass_sentinel_replaces_the_builtin_one(self, tmp_path, monkeypatch):
        design = _core(
            "design",
            _target("sim_custom", filesets="rtl, tb_custom"),
            _RTL_TB + "  tb_custom: {files: [tb/custom.sv], tags: [tb]}\n",
        )
        _core_project(
            tmp_path,
            monkeypatch,
            {"design.core": design, "tb/custom.sv": "ALL TESTS PASSED.\n"},
            sim='pass_sentinels = ["ALL TESTS PASSED."]\n',
        )

        result = run_doctor(tmp_path)

        assert _rows(
            result, Severity.PASS, "sim Target 'sim_custom' TB emits a recognized pass sentinel"
        )
        assert _rows(result, Severity.NOTE, "uses explicitly configured pass sentinel(s)")

    @pytest.mark.parametrize("waived", [False, True], ids=["warn", "waived"])
    def test_selected_native_dependency_reaches_the_report(self, tmp_path, monkeypatch, waived):
        fs = _RTL_TB + "  cpp: {files: [tb/harness.cc], file_type: cppSource}\n"
        files = {
            "design.core": _core(
                "design", _target("sim_cpp", filesets="rtl, tb, cpp", doctor=True), fs
            ),
            "tb/harness.cc": "#include <libelf.h>\n",
        }
        if waived:
            files[".booley/doctor-waivers.toml"] = (
                "version = 1\n\n[[waiver]]\n"
                'check = "sandbox.native-dependency-missing"\n'
                'subject = "libelf-dev"\n'
                'reason = "image built elsewhere"\n'
                "permanent = true\n"
            )
        _core_project(tmp_path, monkeypatch, files)

        result = run_doctor(tmp_path)

        rows = [f for f in result.findings if f.check_id == "sandbox.native-dependency-missing"]
        assert [(f.severity, f.subject) for f in rows] == [
            (Severity.WAIVED if waived else Severity.WARN, "libelf-dev")
        ]
        assert "tb/harness.cc" in rows[0].message
        assert result.counts["waived"] == int(waived)
        if waived:
            messages = [f.message for f in result.findings]
            assert messages[messages.index(rows[0].message) - 1].startswith("tests.toml not")

    def test_each_root_target_partition_is_read_once(self, tmp_path, monkeypatch):
        core = _core(
            "design",
            _target("sim", doctor=True) + _target("lint", flow="lint", filesets="rtl", top="dut"),
        )
        (tmp_path / "rtl").mkdir()
        (tmp_path / "tb").mkdir()
        (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
        (tmp_path / "tb" / "tb.sv").write_text(
            '$display("[SIM_RESULT] PASSED");\n', encoding="utf-8"
        )
        (tmp_path / "design.core").write_text(core, encoding="utf-8")
        (tmp_path / ".booley").mkdir()
        project = doctor.ProjectAudit(
            project_root=tmp_path,
            booley_dir=tmp_path / ".booley",
            booley_toml={},
            first_target="x",
        )
        original = doctor.TargetCatalog.inspect
        calls: list[str] = []

        def inspect(self, handle):
            result = original(self, handle)
            calls.append(handle.name)
            return result

        monkeypatch.setattr(doctor.TargetCatalog, "inspect", inspect)
        rec = _Rec()

        # Private: a whole Doctor run's Flow audit, design-size and native-dependency
        # phases inspect the same Targets too, so only a direct core-audit call isolates
        # the one-read-per-partition count.
        doctor._run_core_audit(project, rec.p, rec.w, rec.s, rec.f, _diagnostics=rec.d)

        assert rec.fails() == []
        # The core audit reads each root partition once; the native-dependency
        # source collection runs last and reads the seed Target (sim) again.
        assert sorted(calls[:-1]) == ["lint", "sim"]
        assert calls[-1] == "sim"


# ---------------------------------------------------------------------------
# .core setup-gap checks added for the PICORV32 onboarding pass: cheap-pass
# CAPI2 schema, yosys-arch hint, untracked-file warning, backend-but-no-Target
# near-miss, and the multi-line output excerpt.
# ---------------------------------------------------------------------------


def test_target_metadata_warnings_name_each_offending_target(tmp_path, monkeypatch):
    fs = _RTL_TB + "  cpp: {files: [sim/main.cpp], file_type: cppSource, tags: [tb]}\n"
    design = _core(
        "design",
        # `--main` and `--binary` are Verilator's tracer-less auto main.
        _target(
            "sim_auto",
            options="tool: verilator, verilator_options: [--main]",
            filesets="rtl",
            top="dut",
        )
        + _target(
            "sim_bin",
            options="tool: verilator, verilator_options: [--binary]",
            filesets="rtl",
            top="dut",
        )
        # No `--main`: the Target owns a cppSource `--exe` main, so it can trace.
        + _target("sim_exe", filesets="rtl, cpp", top="dut")
        + _target("sim_ic", options="tool: icarus, iverilog_options: [-g2012]", filesets="rtl, tb")
        + _target(
            "lint_main",
            flow="lint",
            options="tool: verilator, verilator_options: [--main]",
            filesets="rtl",
            top="dut",
        )
        + _target(
            "synth_noarch", flow="generic", options="tool: yosys", filesets="rtl", top="dut"
        ),
        fs,
    )
    _core_project(
        tmp_path,
        monkeypatch,
        {"design.core": design, "sim/main.cpp": "int main() { return 0; }\n"},
    )

    result = run_doctor(tmp_path)

    [trace] = [f for f in result.findings if f.check_id == "sim.trace-unavailable"]
    assert trace.severity is Severity.WARN
    assert trace.subject == "sim_auto, sim_bin"
    assert "inconclusive (exit 1)" in trace.message
    assert " passes" not in trace.message
    assert "+trace and +tracefile=<file>" in trace.message
    assert "[flows.sim].trace_args" in trace.message
    assert _rows(result, Severity.PASS, "2 Verilator sim Target(s) trace-capable")
    arch = [f for f in result.findings if f.check_id == "core.yosys-arch-missing"]
    assert [f.subject for f in arch] == ["synth_noarch"]
    assert "flow_options.arch" in arch[0].message and "requires" in arch[0].message
    assert _rows(
        result, Severity.PASS, "synth Target 'synth_fast' (yosys) declares flow_options.arch"
    )


def test_trace_warning_without_any_trace_capable_target(tmp_path, monkeypatch):
    core = _core(
        "design",
        _target(
            "sim_auto",
            options="tool: verilator, verilator_options: [--main]",
            filesets="missing",
            top="dut",
        ),
        "  missing: {files: [nowhere/dut.sv]}\n",
    )
    # No Target owns a main and no referenced file exists, so no trace-capable row appears.
    _core_project(tmp_path, monkeypatch, {"unit.core": None, "design.core": core})

    result = run_doctor(tmp_path)

    assert list(_warnings(result, "sim.trace-unavailable")) == ["sim_auto"]
    assert not _rows(result, Severity.PASS, "trace-capable")


def test_icarus_sv_language_mode_by_selection_and_flow(tmp_path, monkeypatch):
    sv = "tool: icarus"
    fs = _RTL_TB + "  rtl_v: {files: [rtl/dut.v], file_type: verilogSource}\n"
    design = _core(
        "design",
        # A sim Target's verdict source is dead without -g2012, so it fails.
        _target("sim_ic_bare", options=sv, doctor=True)
        # A lint Target's SV-mode gate is advisory, so it warns.
        + _target("lint_ic_bare", flow="lint", options=sv, filesets="rtl", top="dut", doctor=True)
        + _target("lint_ic_unsel", flow="lint", options=sv, filesets="rtl", top="dut")
        + _target("sim_ic_2012", options="tool: icarus, iverilog_options: [-g2012]")
        # -g2005-sv also enables SV, so a working .core is not nagged to churn.
        + _target("sim_ic_2005sv", options="tool: icarus, iverilog_options: [-g2005-sv]")
        # A .v-only Icarus Target keeps the right default generation.
        + _target("sim_ic_v", options=sv, filesets="rtl_v", top="dut"),
        fs,
    )
    _core_project(
        tmp_path, monkeypatch, {"design.core": design, "rtl/dut.v": "module dut; endmodule\n"}
    )

    result = run_doctor(tmp_path)

    sv_rows = [
        f for f in result.findings if "-g2012" in f.message or "SV language flag" in f.message
    ]
    names = (
        "sim_ic_bare",
        "lint_ic_bare",
        "lint_ic_unsel",
        "sim_ic_2012",
        "sim_ic_2005sv",
        "sim_ic_v",
    )
    assert {n for n in names if any(f"'{n}'" in f.message for f in sv_rows)} == set(names) - {
        "sim_ic_v"
    }
    [fail] = [f for f in sv_rows if f.severity is Severity.FAIL]
    assert "'sim_ic_bare'" in fail.message and "design.core" in fail.fix and "-g2012" in fail.fix
    assert [f.subject for f in sv_rows if f.severity is Severity.WARN] == ["lint_ic_bare"]
    [note] = [f for f in sv_rows if f.severity is Severity.NOTE]
    assert "'lint_ic_unsel'" in note.message and "not selected" in note.message
    assert {f.message.split("'")[1] for f in sv_rows if f.severity is Severity.PASS} == {
        "sim_ic_2012",
        "sim_ic_2005sv",
    }


def test_toplevel_interface_ports_warn_only_for_non_sim_toplevels(tmp_path, monkeypatch):
    # F-7: a toplevel with interface ports cannot elaborate standalone; a sim Target's
    # testbench instantiates the interfaces itself.
    iface = (
        "module eth_mac (\n    input wire logic clk,\n"
        "    taxi_axis_if.snk s_axis_tx,\n    taxi_axis_if.src m_axis_rx\n);\nendmodule\n"
    )
    commented = (
        "module eth_commented (\n    input wire logic clk\n"
        "    // taxi_axis_if.snk s_axis_tx,   <- supplied by the wrapper below\n);\nendmodule\n"
    )
    typed = (
        "module eth_typed (\n    input wire logic clk,\n"
        "    input my_cfg_t cfg,\n    output wire logic done\n);\nendmodule\n"
    )
    fs = _RTL_TB + "".join(
        f"  {n}: {{files: [rtl/{n}.sv]}}\n" for n in ("iface", "commented", "typed")
    )
    design = _core(
        "design",
        _target("lint_iface", flow="lint", filesets="iface", top="eth_mac")
        + _target("sim_iface", filesets="iface, tb", top="eth_mac")
        + _target("lint_commented", flow="lint", filesets="commented", top="eth_commented")
        + _target("lint_typed", flow="lint", filesets="typed", top="eth_typed")
        + _target("lint_missing", flow="lint", filesets="typed", top="not_in_the_files"),
        fs,
    )
    _core_project(
        tmp_path,
        monkeypatch,
        {
            "design.core": design,
            "rtl/iface.sv": iface,
            "rtl/commented.sv": commented,
            "rtl/typed.sv": typed,
        },
    )

    result = run_doctor(tmp_path)

    assert _warnings(result, "core.interface-toplevel").keys() == {"lint_iface"}
    message = _warnings(result, "core.interface-toplevel")["lint_iface"]
    assert "taxi_axis_if.snk s_axis_tx" in message
    assert "cannot be elaborated standalone" in message
    # A commented-out interface port and a plain user-typed port are not interfaces.
    standalone = {
        f.message.split("'")[1]
        for f in result.findings
        if f.severity is Severity.PASS and "elaborates standalone" in f.message
    }
    assert standalone == {"lint_fast", "synth_fast", "lint_commented", "lint_typed"}


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


_MISPLACED_BOOLEY_CORE = """\
CAPI=2:
name: acme:ip:picorv32_impl:0
filesets:
  rtl:
    files: [rtl/top.sv: {file_type: systemVerilogSource}]
targets:
  lint_core:
    flow: lint
    flow_options: {tool: verilator, booley: {doctor: [lint]}}
    filesets: [rtl]
    toplevel: top
  synth_core:
    flow: generic
    flow_options: {tool: yosys, arch: xilinx, booley: {doctor: [synth]}}
    filesets: [rtl]
    toplevel: top
  fpga_core:
    flow: generic
    flow_options: {tool: vivado, part: xc7a35tcsg324-1}
    booley: {doctor: [fpga]}
    filesets: [rtl]
    toplevel: top
"""


_OLD = "2000-01-01T00:00:00Z"
_NEW = "2099-01-01T00:00:00Z"
_REBUILD = "on the host run `booley init`, then use Dev Containers: Rebuild Container"
_MISMATCH = doctor.image_identity.Comparison(
    doctor.image_identity.Status.MISMATCH, "revision old -> new"
)


def _staleness_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    created: dict[str, str | None],
    image: str | None = None,
    invalid: bool = False,
    dockerfile_mtime: float | None = None,
    bakes: doctor.image_identity.Comparison | None = None,
    owner: Attachment | None = None,
    managed: bool = True,
) -> doctor.DoctorRunResult:
    """Run Doctor on a Project whose Sandbox Images carry the given build times.

    *created* maps ``base``, ``derived`` or an image name to its ``{{json .Created}}`` answer;
    ``None`` makes that image absent.
    """
    booley_dir = write_project(tmp_path, sandbox_image=image)
    if invalid:
        (booley_dir / "booley.toml").write_text("[project\n", encoding="utf-8")
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    names = {"base": doctor.DOCKER_IMAGE, "derived": doctor.pi.project_image_name(tmp_path)}
    for role, stamp in created.items():
        name = names.get(role, role)
        _answer_command(
            monkeypatch,
            lambda cmd, name=name: "{{json .Created}}" in cmd and name in cmd,
            returncode=0 if stamp else 1,
            stdout=f'"{stamp}"\n' if stamp else "",
        )
    if dockerfile_mtime is not None:
        _write_project_dockerfile(tmp_path, dockerfile_mtime)
    if bakes is not None:
        monkeypatch.setattr(doctor.idk, "compare_issued_build", lambda *_a, **_k: bakes)
    monkeypatch.setattr(doctor.session_runtime, "sandbox_attachment", lambda _root: owner)
    monkeypatch.setattr(doctor.session_runtime, "image_is_managed", lambda _root: managed)
    return run_doctor(tmp_path)


_MATCH = doctor.image_identity.Comparison(doctor.image_identity.Status.MATCH)
_UNKNOWN = doctor.image_identity.Comparison(doctor.image_identity.Status.UNKNOWN)
_CUSTOM = {"image": "openc910-booley-sandbox"}
# row: (image build times, run options, warn | pass | silent, text the check's row has or lacks)
_STALENESS_ROWS = {
    # An invalid Project is the only way a run reaches the base image check.
    "base-stale": ({"base": _OLD}, {"invalid": True}, "warn", "predates Dockerfile"),
    "base-fresh": ({"base": _NEW}, {"invalid": True}, "pass", "is newer than Dockerfile"),
    "derived-stale": ({"base": _NEW, "derived": _OLD}, {}, "warn", "predates its base"),
    "derived-fresh": ({"base": _OLD, "derived": _NEW}, {}, "pass", "newer than its base"),
    "derived-base-absent": ({"base": None, "derived": _OLD}, {}, "silent", "its base"),
    "custom-stale": (
        {_CUSTOM["image"]: _OLD},
        {**_CUSTOM, "dockerfile_mtime": 2e9},
        "warn",
        "predates",
    ),
    "custom-fresh": (
        {_CUSTOM["image"]: _NEW},
        {**_CUSTOM, "dockerfile_mtime": 1000.0},
        "pass",
        "newer than its Dockerfile",
    ),
    "custom-no-dockerfile": ({_CUSTOM["image"]: _OLD}, _CUSTOM, "silent", "custom sandbox image"),
    "user-managed": (
        {"base": _NEW, "derived": _OLD, "custom/user-image:latest": _OLD},
        {"image": "custom/user-image:latest"},
        "silent",
        "bakes current Booley|custom sandbox image|project image",
    ),
    # A generated image is never judged against the project Dockerfile, even when that file is newer.
    "derived-skips-dockerfile": (
        {"base": _NEW, "derived": _NEW},
        {"dockerfile_mtime": 2e9},
        "silent",
        "custom sandbox image",
    ),
    "bakes-mismatch": ({}, {"bakes": _MISMATCH}, "warn", "revision old -> new"),
    "bakes-match": ({}, {"bakes": _MATCH}, "pass", "bakes current Booley sources"),
    "bakes-unknown": ({}, {"bakes": _UNKNOWN}, "silent", "bakes current Booley|stale code"),
}


@pytest.mark.parametrize("row", _STALENESS_ROWS)
def test_sandbox_image_staleness(row, tmp_path, monkeypatch):
    created, options, kind, text = _STALENESS_ROWS[row]
    bound = kind == "warn" and row != "base-stale"
    result = _staleness_run(
        tmp_path,
        monkeypatch,
        created=created,
        owner=Attachment.VSCODE if bound else None,
        **options,
    )

    warns = finding_messages(result, Severity.WARN, check_id="sandbox.image-stale")
    passes = finding_messages(result, Severity.PASS)
    if kind == "silent":
        assert not [m for m in warns + passes if re.search(text, m)]
    elif kind == "pass":
        assert warns == []
        assert any(text in message for message in passes)
    else:
        (warning,) = warns
        assert text in warning
        if row == "base-stale":
            assert warning.endswith(UNKNOWN_SANDBOX_STEPS)
        else:
            assert _REBUILD in warning
            assert not any(
                w in warning for w in ("--force", "session refresh", "will not rebuild")
            )


@pytest.mark.parametrize(
    ("owner", "managed", "steps"),
    [
        (Attachment.VSCODE, True, VSCODE_INIT),
        (Attachment.HEADLESS, True, HEADLESS_REFRESH),
        (Attachment.VSCODE, False, EXTERNAL + VSCODE_SEED),
    ],
    ids=["vscode", "headless", "own-image"],
)
def test_image_stale_names_each_owner_workflow(owner, managed, steps, tmp_path, monkeypatch):
    result = _staleness_run(
        tmp_path,
        monkeypatch,
        created={"base": _NEW, "derived": _OLD},
        owner=owner,
        managed=managed,
    )

    (warning,) = finding_messages(result, Severity.WARN, check_id="sandbox.image-stale")
    assert warning.endswith(steps + ".")


_UNADOPTED = "the selected image is built but this Project has not adopted it"
_ERROR = lifecycle.ImageLifecycleError("Cannot connect to the Docker daemon")


@pytest.mark.parametrize(
    ("kwargs", "reason", "outcome"),
    [
        (
            {"status": lifecycle.Status.STALE, "diagnostics": ("pins changed",) * 2},
            "pins changed",
            "warn",
        ),
        ({"status": lifecycle.Status.STALE}, _UNADOPTED, "warn"),
        ({"status": lifecycle.Status.CURRENT, "selected_id": "sha256:other"}, _UNADOPTED, "warn"),
        ({"status": lifecycle.Status.CURRENT}, None, "pass"),
        ({"raises": _ERROR}, "Cannot connect", "skip"),
        ({"raises": OSError("unreadable input")}, "unreadable input", "skip"),
    ],
)
def test_doctor_reports_sandbox_image_provenance(kwargs, reason, outcome, tmp_path, monkeypatch):
    patch_doctor_environment(monkeypatch, tmp_path, write_project(tmp_path))
    answer_image_check(monkeypatch, **kwargs)
    result = run_doctor(tmp_path)
    found = {
        "warn": finding_messages(result, Severity.WARN, check_id="sandbox.image-stale"),
        "pass": [m for m in finding_messages(result, Severity.PASS) if "provenance matches" in m],
        "skip": [m for m in finding_messages(result, Severity.SKIP) if "provenance check" in m],
    }
    assert {k: len(v) for k, v in found.items()} == {k: int(k == outcome) for k in found}
    if reason:
        assert reason in found[outcome][0]
    if outcome == "warn":
        assert _REBUILD in found["warn"][0]


# ---------------------------------------------------------------------------
# Custom [sandbox].image freshness vs .booley/docker/Dockerfile (item 5)
# ---------------------------------------------------------------------------


def _write_project_dockerfile(proj: Path, mtime: float) -> Path:
    docker_dir = proj / ".booley" / "docker"
    docker_dir.mkdir(parents=True, exist_ok=True)
    dockerfile = docker_dir / "Dockerfile"
    dockerfile.write_text("FROM booley-sandbox\n", encoding="utf-8")
    os.utime(dockerfile, (mtime, mtime))
    return dockerfile


# ---------------------------------------------------------------------------
# Large-design advisory for --deep budgets (item 3 / F5)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Worktree prune guard (ADR 0028 Decision 10) — _check_worktree_prune_guard
# ---------------------------------------------------------------------------


def _raise_file_not_found(*_args: object) -> None:
    raise FileNotFoundError


@pytest.mark.parametrize("row", ["never", "unset", "wrong-value", "not-a-repo"])
def test_worktree_prune_guard(row, tmp_path, monkeypatch):
    """Worktrees hold container paths; a host `git gc` prunes them unless the guard is `never`."""
    project = tmp_path / "it's a project"
    project.mkdir()
    booley_dir = write_project(project)
    patch_doctor_environment(monkeypatch, project, booley_dir)
    answers = {
        "never": {"stdout": "never\n"},
        "unset": {"returncode": 1},
        "wrong-value": {"stdout": "3.months.ago\n"},
    }
    if row == "not-a-repo":
        _answer_command(
            monkeypatch,
            lambda cmd: (
                cmd[:3] == ["git", "-C", str(project)] and cmd[-2:] == ["rev-parse", "--git-dir"]
            ),
            returncode=128,
        )
    else:
        _answer_command(monkeypatch, lambda cmd: "gc.worktreePruneExpire" in cmd, **answers[row])

    result = run_doctor(project)

    fails = [f for f in result.findings if "gc.worktreePruneExpire" in f.message]
    if row == "never":
        assert [f.severity for f in fails] == [Severity.PASS]
    elif row == "not-a-repo":
        assert fails == []
        assert "worktree prune guard: Project Root is not a git repo" in finding_messages(
            result, Severity.SKIP
        )
    else:
        (fail,) = fails
        assert fail.severity is Severity.FAIL
        assert fail.fix == f"git -C {quote_arg(str(project))} config gc.worktreePruneExpire never"
        assert ("'3.months.ago'" in fail.message) is (row == "wrong-value")


_SHADOW_CORE = "CAPI=2:\nname: ::demo:0\n"


@pytest.mark.parametrize("row", ["marker", "no-worktree", "stale-core", "authored-stealth-core"])
def test_worktree_core_shadow_guard(row, tmp_path, monkeypatch):
    """A stale worktree .core under .booley/ shadows the root source unless FUSESOC_IGNORE is set."""
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    core_dirs = {"stale-core": "worktrees/scalar_1bfe1733", "authored-stealth-core": "cores"}
    if row == "marker":
        (booley_dir / "FUSESOC_IGNORE").write_text("", encoding="utf-8")
    if row in core_dirs:
        core_dir = booley_dir / core_dirs[row]
        core_dir.mkdir(parents=True)
        (core_dir / "design.core").write_text(_SHADOW_CORE, encoding="utf-8")

    result = run_doctor(tmp_path)

    if row == "marker":
        assert any(
            m.startswith("FUSESOC_IGNORE present") for m in finding_messages(result, Severity.PASS)
        )
        assert (
            finding_messages(result, Severity.WARN, check_id="fusesoc.worktree-core-shadow") == []
        )
    else:
        (warning,) = finding_messages(
            result, Severity.WARN, check_id="fusesoc.worktree-core-shadow"
        )
        assert ("can shadow the source at the Project Root" in warning) is (row == "stale-core")
        assert ("future worktree" in warning) is (row != "stale-core")


@pytest.fixture
def git_versions(monkeypatch):
    """Set the Git versions the host and the Sandbox Image report.

    The installed host and Sandbox Image Git versions are host state no test can set.
    One answer repeats; several are returned in order (host, then Sandbox Image).
    """

    def set_versions(*answers):
        queue = iter(answers)
        monkeypatch.setattr(readiness, "_git_version_at", lambda *_a: next(queue, answers[-1]))

    return set_versions


def _git_config(repository: Path, key: str, value: str | None) -> None:
    """Set a repository-local Git key; a ``None`` value sets nothing."""
    if value is not None:
        run_bounded_child(
            ["git", "-C", str(repository), "config", "--local", key, value], check=True
        )


def _git_file_config(config_file: Path, key: str, value: str) -> None:
    run_bounded_child(["git", "config", "--file", str(config_file), key, value], check=True)


_RELATIVE = "worktree.useRelativePaths"
_OPT_IN = {"core.repositoryFormatVersion": "1", "extensions.relativeWorktrees": "true"}


def _git_configs(repository: Path, configs: dict[str, str]) -> None:
    for key, value in configs.items():
        _git_config(repository, key, value)


def _register_worktree(worktree: Path, administration: Path, *, absolute: bool = False) -> None:
    """Write the two metadata files linking *worktree* and its administration directory."""
    worktree.mkdir(parents=True, exist_ok=True)
    administration.mkdir(parents=True, exist_ok=True)
    pointer = administration if absolute else Path(os.path.relpath(administration, worktree))
    reverse = (
        worktree / ".git" if absolute else Path(os.path.relpath(worktree / ".git", administration))
    )
    # Git writes forward slashes on every platform and finds a worktree by the "/.git" suffix.
    (worktree / ".git").write_bytes(f"gitdir: {pointer.as_posix()}\n".encode())
    (administration / "gitdir").write_bytes(f"{reverse.as_posix()}\n".encode())


class TestWorktreePortability:
    def _project(self, root: Path):
        booley_dir = root / ".booley"
        booley_dir.mkdir(exist_ok=True)
        return SimpleNamespace(
            project_root=root, booley_dir=booley_dir, common_booley_dir=booley_dir
        )

    def _inspect(self, project, docker, runtime) -> list[DiagnosticFinding]:
        request = readiness.WorktreePortabilityRequest(project, docker, "image", runtime)
        return list(readiness.inspect_worktree_portability(request).findings)

    @pytest.mark.parametrize("runtime", RUNTIMES)
    @pytest.mark.parametrize("layout", LAYOUTS)
    def test_lists_main_registry_from_linked_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout, runtime
    ):
        """The worktree registry is Common Booley State: a worktree sees main's."""
        main, worktree = linked_worktree(tmp_path, monkeypatch, layout=layout, runtime=runtime)
        registered = {main / ".booley" / "worktrees" / ("s1" if layout == "snapshot" else "p1")}
        project = readiness.ProjectAudit(
            project_root=worktree,
            booley_dir=resolve_checkout_booley_dir(worktree),
            booley_toml={},
            first_target="",
        )

        findings = self._inspect(project, None, False)

        listed = {
            Path(item.message.split(": ", 1)[0])
            for item in findings
            if "worktree metadata" in item.message
        }
        assert registered <= listed

    @pytest.mark.parametrize(
        ("configs", "versions", "docker", "fails", "warns"),
        [
            ({_RELATIVE: "true"}, [(2, 53, 0)], "docker", 0, ()),
            (
                {_RELATIVE: "false"},
                [(2, 53, 0), (2, 47, 9)],
                "docker",
                0,
                ("container-only", "do not run host `git worktree prune`"),
            ),
            (_OPT_IN, [(2, 53, 0), (2, 47, 9)], "docker", 1, ("repository format",)),
            (_OPT_IN, [(2, 53, 0)], None, 0, ("could not be verified",)),
        ],
        ids=["capable", "old-sandbox-before-opt-in", "old-side-after-opt-in", "no-docker-probe"],
    )
    def test_measured_git_versions(
        self, tmp_path, git_versions, configs, versions, docker, fails, warns
    ):
        git_init(tmp_path)
        _git_configs(tmp_path, configs)
        git_versions(*versions)

        findings = self._inspect(self._project(tmp_path), docker, False)

        failures = [item.message for item in findings if item.severity is Severity.FAIL]
        warnings = [item.message for item in findings if item.severity is Severity.WARN]
        assert len(failures) == fails
        assert bool(warnings) is bool(warns)
        for fragment in warns:
            assert any(fragment in message for message in warnings + failures)
        if not warns:
            assert any("worktree.useRelativePaths=true" in item.message for item in findings)

    @pytest.mark.parametrize("version", [(2, 48, 0), (2, 53, 0)])
    def test_runtime_true_policy_infers_historical_host_capability(
        self, tmp_path, git_versions, version
    ):
        git_init(tmp_path)
        _git_config(tmp_path, _RELATIVE, "true")
        git_versions(version)
        findings = self._inspect(self._project(tmp_path), None, True)
        assert any(
            item.severity is Severity.NOTE and "host version not rechecked" in item.message
            for item in findings
        )
        assert not [item for item in findings if item.severity is Severity.WARN]

    def test_hand_written_sandbox_notes_unknown_host_capability(
        self, tmp_path, monkeypatch, git_versions
    ):
        git_init(tmp_path)
        git_versions((2, 53, 0))
        place(monkeypatch, RuntimeLocation.HAND_WRITTEN_SANDBOX)
        findings = self._inspect(self._project(tmp_path), None, True)
        (note,) = [i for i in findings if "host Git capability is unknown" in i.message]
        assert note.severity is Severity.NOTE
        assert note.message.endswith("in a Sandbox Booley did not create")
        assert note.fix == "if a host also opens this checkout, run `booley doctor` there"

    @pytest.mark.parametrize("policy", ["false", None, "invalid"])
    def test_runtime_nontrue_policy_remains_unknown(self, tmp_path, git_versions, policy):
        git_init(tmp_path)
        _git_config(tmp_path, _RELATIVE, policy)
        git_versions((2, 53, 0))
        findings = self._inspect(self._project(tmp_path), None, True)
        assert any("host Git capability is unknown" in item.message for item in findings)
        assert any("container-only" in item.message for item in findings)
        assert not any(item.severity is Severity.NOTE for item in findings)
        if policy is None:
            assert any("Sandbox Git 2.53.0 supports" in item.message for item in findings)

    @pytest.mark.parametrize(
        "root_policy,data_policy",
        [("true", "true"), ("true", "false"), ("true", None), ("false", "true"), (None, "true")],
    )
    def test_runtime_paired_repository_policy(
        self, tmp_path, git_versions, root_policy, data_policy
    ):
        git_init(tmp_path)
        project = self._project(tmp_path)
        git_init(project.booley_dir)
        for repository, policy in ((tmp_path, root_policy), (project.booley_dir, data_policy)):
            _git_config(repository, _RELATIVE, policy)
        git_versions((2, 53, 0))
        findings = self._inspect(project, None, True)
        warnings = [item for item in findings if item.severity is Severity.WARN]
        assert bool(warnings) is (root_policy != "true" or data_policy != "true")
        if root_policy == "true" and data_policy != "true":
            scoped = [item for item in warnings if item.subject == str(project.booley_dir)]
            assert len(scoped) == 1
            assert "container-only" in scoped[0].message
            assert "do not run host `git worktree prune`" in scoped[0].message

    @pytest.mark.parametrize("version", [(2, 47, 9), None])
    @pytest.mark.parametrize("extension", [False, True])
    def test_runtime_true_policy_still_checks_sandbox(
        self, tmp_path, git_versions, version, extension
    ):
        git_init(tmp_path)
        _git_config(tmp_path, _RELATIVE, "true")
        if extension:
            _git_configs(tmp_path, _OPT_IN)
        git_versions(version)
        findings = self._inspect(self._project(tmp_path), None, True)
        assert any(item.severity is Severity.NOTE for item in findings)
        assert any(
            "Sandbox Git" in item.message and item.severity is Severity.WARN for item in findings
        )
        assert bool([item for item in findings if item.severity is Severity.FAIL]) is (
            extension and version is not None
        )
        if extension and version is None:
            assert any("one Git side could not be verified" in item.message for item in findings)

    @pytest.mark.parametrize(
        "host,image",
        [
            (None, (2, 53, 0)),
            ((2, 47, 9), (2, 53, 0)),
            ((2, 53, 0), None),
            ((2, 53, 0), (2, 47, 9)),
        ],
    )
    def test_host_true_policy_never_replaces_measured_versions(
        self, tmp_path, git_versions, host, image
    ):
        git_init(tmp_path)
        _git_config(tmp_path, _RELATIVE, "true")
        git_versions(host, image)
        findings = self._inspect(self._project(tmp_path), "docker", False)
        assert any(item.severity is Severity.WARN for item in findings)
        assert not any(item.severity is Severity.NOTE for item in findings)

    def _nonlocal_policy_project(self, tmp_path, monkeypatch, source):
        project = self._project(tmp_path)
        foreign = tmp_path / "foreign"
        git_init(foreign)
        _git_config(foreign, _RELATIVE, "true")
        config = tmp_path / "foreign-config"
        config.write_text("[worktree]\nuseRelativePaths = true\n")
        if source == "global":
            monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
            monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        elif source == "environment":
            add_git_command_config(monkeypatch, "worktree.useRelativePaths", "true")
        elif source == "file":
            monkeypatch.setenv("GIT_CONFIG", str(config))
        elif source == "worktree":
            run_bounded_child(
                ["git", "-C", str(tmp_path), "config", "extensions.worktreeConfig", "true"],
                check=True,
            )
            run_bounded_child(
                ["git", "-C", str(tmp_path), "config", "--worktree", _RELATIVE, "true"], check=True
            )
        elif source == "foreign":
            monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
        else:
            checkout = tmp_path / "unreachable"
            checkout.mkdir()
            (checkout / ".git").write_text(f"gitdir: {tmp_path / 'missing-host-admin'}\n")
            project = self._project(checkout)
        return project

    @pytest.mark.parametrize(
        "source", ["global", "environment", "file", "worktree", "foreign", "unreachable"]
    )
    def test_runtime_nonlocal_true_is_not_host_evidence(
        self, tmp_path, monkeypatch, git_versions, source
    ):
        from booley.harness.setup import git_hooks

        git_init(tmp_path)
        project = self._nonlocal_policy_project(tmp_path, monkeypatch, source)
        assert (
            git_hooks.read_local_config(project.project_root, git_hooks.WORKTREE_RELATIVE_KEY)
            is None
        )
        git_versions((2, 53, 0))
        findings = self._inspect(project, None, True)
        assert any("host Git capability is unknown" in item.message for item in findings)

    def test_runtime_stale_manual_policy_is_historical_evidence(self, tmp_path, git_versions):
        from booley.harness.setup import git_hooks
        from tests.harness.setup.test_git_hooks import _ctx

        git_init(tmp_path)
        _git_config(tmp_path, _RELATIVE, "true")
        _git_configs(tmp_path, _OPT_IN)
        ctx = _ctx(tmp_path)
        # Reaching the refused-downgrade state through `booley init` needs two installed Git
        # versions; this call covers git_hooks arcs no other test reaches.
        git_hooks._step_worktree_link_policy(
            ctx, host_git_version=(2, 47, 9), sandbox_git_version=(2, 53, 0)
        )
        assert ctx.results[-1].detail == "incompatible Git downgrade"
        assert git_hooks.read_local_config(tmp_path, git_hooks.WORKTREE_RELATIVE_KEY) == "true"
        git_versions((2, 53, 0))
        findings = self._inspect(self._project(tmp_path), None, True)
        assert any(item.severity is Severity.NOTE for item in findings)
        assert not any(item.severity is Severity.WARN for item in findings)

    @pytest.mark.parametrize("failure", ["unparseable", "nonzero", "timeout", "oserror"])
    def test_runtime_version_failure_keeps_real_local_evidence(
        self, tmp_path, monkeypatch, failure
    ):
        from booley.harness.setup import git_hooks

        git_init(tmp_path)
        _git_config(tmp_path, _RELATIVE, "true")
        real_run = subprocess.run

        def run(argv, **kwargs):
            if argv == ["git", "--version"]:
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(argv, 30)
                if failure == "oserror":
                    raise OSError("unavailable")
                return subprocess.CompletedProcess(
                    argv, int(failure == "nonzero"), "invalid version", ""
                )
            return real_run(argv, **kwargs)

        monkeypatch.setattr(readiness.subprocess, "run", run)
        findings = self._inspect(self._project(tmp_path), None, True)
        assert git_hooks.read_local_config(tmp_path, git_hooks.WORKTREE_RELATIVE_KEY) == "true"
        assert any(item.severity is Severity.NOTE for item in findings)
        assert any("Sandbox Git could not be verified" in item.message for item in findings)

    @pytest.mark.parametrize("hazard", ["format", "metadata", "extension-only"])
    def test_runtime_policy_does_not_hide_repository_hazards(
        self, tmp_path, monkeypatch, git_versions, hazard
    ):
        git_init(tmp_path)
        project = self._project(tmp_path)
        if hazard != "extension-only":
            _git_config(tmp_path, _RELATIVE, "true")
        if hazard in ("format", "extension-only"):
            _git_configs(tmp_path, _OPT_IN)
        if hazard == "format":
            # The format diagnostic boundary remains authoritative even when
            # a Git implementation exposes malformed format text.
            read_config = readiness.read_local_config
            monkeypatch.setattr(
                readiness,
                "read_local_config",
                lambda repository, key: (
                    "invalid"
                    if key == "core.repositoryFormatVersion"
                    else read_config(repository, key)
                ),
            )
        elif hazard == "metadata":
            _register_worktree(
                tmp_path / "t3" / "ticket",
                tmp_path / ".git" / "worktrees" / "ticket",
                absolute=True,
            )
        git_versions((2, 53, 0))
        findings = self._inspect(project, None, True)
        if hazard == "format":
            assert any(item.severity is Severity.FAIL for item in findings)
        elif hazard == "metadata":
            assert any(
                item.severity is Severity.WARN and "absolute .git pointer" in item.message
                for item in findings
            )
        else:
            assert any("host Git capability is unknown" in item.message for item in findings)

    def test_unlistable_registry_warns_with_git_error(self, tmp_path):
        report = readiness.Findings()
        readiness._inspect_live_worktrees(self._project(tmp_path), report)
        assert any("cannot list worktrees" in item.message for item in report.report().findings)

    @pytest.mark.parametrize(
        "row",
        ["absolute", "relative", "unreadable", "worktree-config", "config-unreadable", "paired"],
    )
    def test_live_worktree_metadata(self, tmp_path, monkeypatch, row):
        git_init(tmp_path)
        project = self._project(tmp_path)
        # Git's registry decides what is a worktree, wherever T3 Code put it.
        outer = tmp_path / "t3" / "ticket"
        (project.booley_dir / "worktrees" / "not-a-worktree").mkdir(parents=True)
        administration = tmp_path / ".git" / "worktrees" / "ticket"
        _register_worktree(outer, administration, absolute=row == "absolute")
        if row == "unreadable":
            (outer / ".git").write_text("", encoding="utf-8")
            monkeypatch.setattr(readiness, "resolve_checkout_booley_dir", _raise_file_not_found)
        if row in ("worktree-config", "config-unreadable"):
            _git_file_config(
                administration / "config.worktree",
                "core.worktree",
                "../../../" + outer.relative_to(tmp_path).as_posix(),
            )
        if row == "config-unreadable":
            real_run = subprocess.run

            def run(argv, **kwargs):
                if "--file" in argv:
                    raise OSError("unavailable")
                return real_run(argv, **kwargs)

            monkeypatch.setattr(readiness.subprocess, "run", run)
        paired = outer / ".booley"
        if row == "paired":
            _register_worktree(paired, tmp_path / "paired-admin")
            _git_file_config(
                tmp_path / "paired-admin" / "config.worktree",
                "core.hooksPath",
                str(tmp_path / "paired-admin" / "hooks"),
            )

        findings = self._inspect(project, None, False)

        warnings = [
            item.message
            for item in findings
            if item.severity is Severity.WARN and "worktree metadata" in item.message
        ]
        passes = [item.message for item in findings if item.severity is Severity.PASS]
        problem = {
            "absolute": "absolute .git pointer",
            "unreadable": "unreadable worktree registration",
            "paired": "absolute core.hooksPath",
        }.get(row)
        if problem is None:
            assert warnings == []
            assert f"{outer}: worktree metadata is relative" in passes
        else:
            assert any(problem in message for message in warnings)
        if row == "absolute":
            assert any(f"{outer}" in m and "absolute reverse gitdir" in m for m in warnings)
        if row == "paired":
            assert any(str(paired) in message for message in warnings)


# ---------------------------------------------------------------------------
# ADR 0028 Decision 12 — memory invariant, runtime checks, developer probe
# ---------------------------------------------------------------------------

_GIB = 1024**3


def _adr28_project(tmp_path: Path, *, booley_toml: dict | None = None) -> doctor.ProjectAudit:
    pd = tmp_path / ".booley"
    pd.mkdir(exist_ok=True)
    return doctor.ProjectAudit(
        project_root=tmp_path, booley_dir=pd, booley_toml=booley_toml or {}, first_target="x"
    )


def _edit_booley_toml(booley_dir: Path, old: str, new: str) -> None:
    path = booley_dir / "booley.toml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


@pytest.mark.parametrize(
    "row",
    [
        "set",
        "unset",
        "unparseable",
        "no-project",
        "inside-applied",
        "inside-unaligned",
        "inside-unapplied",
        "inside-unreadable",
        "inside-hand-written-unreadable",
        "inside-hand-written-unapplied",
    ],
)
def test_sandbox_memory(row, tmp_path, monkeypatch):
    """ADR 0087: ``[sandbox].memory`` sizes the Memory Budget; inside the Sandbox, the live limit does."""
    booley_dir = _setup_sandbox_memory_row(row, tmp_path, monkeypatch)
    if row == "unset":
        _edit_booley_toml(booley_dir, 'memory = "16g"\n', "")
    elif row == "unparseable":
        _edit_booley_toml(booley_dir, '"16g"', '"lots"')
    elif row == "no-project":
        (booley_dir / "booley.toml").write_text("[project\n", encoding="utf-8")
    elif row == "inside-unaligned":
        _edit_booley_toml(booley_dir, '"16g"', '"17179869185"')
    elif row.endswith("unapplied"):
        _edit_booley_toml(booley_dir, '"16g"', '"2290m"')

    result = run_doctor(tmp_path)

    if row.startswith("inside-"):
        _assert_inside_sandbox_memory(row, result)
        return
    warns = finding_messages(result, Severity.WARN, check_id="sandbox.memory-unset")
    drift = finding_messages(result, Severity.WARN, check_id="sandbox.memory-drift")
    assert drift == []
    if row == "set":
        assert warns == []
        passes = finding_messages(result, Severity.PASS)
        assert any(m.startswith("[sandbox].memory = 16") for m in passes)
        assert not any("the Sandbox's memory limit" in m for m in passes)
    elif row == "no-project":
        assert warns == []
        assert "sandbox memory skipped - no valid project config" in finding_messages(
            result, Severity.SKIP
        )
    else:
        (warning,) = warns
        assert "the Memory Budget falls back to installed memory" in warning
        assert ("unparseable" in warning) is (row == "unparseable")
        assert "memory_budget" not in warning


def _setup_sandbox_memory_row(row, tmp_path, monkeypatch):
    """Set up a Project and a fake cgroup limit file; return the Booley Directory."""
    if row.startswith("inside-"):
        sandbox = (
            RuntimeLocation.HAND_WRITTEN_SANDBOX
            if "hand-written" in row
            else RuntimeLocation.ISSUED_SANDBOX
        )
        runtime_setup(tmp_path, monkeypatch, inside=True, sandbox=sandbox)
        booley_dir = tmp_path / ".booley"
    else:
        booley_dir = write_project(tmp_path)
        patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    cgroup = tmp_path / "memory.max"
    monkeypatch.setattr(doctor.resource_policy, "CGROUP_MEMORY_LIMIT_PATHS", (cgroup,))
    if not row.endswith("unreadable"):
        # The host row's 8g proves host Doctor ignores the live limit.
        cgroup.write_text(f"{(8 if row == 'set' else 16) * _GIB}\n", encoding="utf-8")
    return booley_dir


def _assert_inside_sandbox_memory(row, result):
    warns = finding_messages(result, Severity.WARN, check_id="sandbox.memory-unset")
    drift = finding_messages(result, Severity.WARN, check_id="sandbox.memory-drift")
    passes = finding_messages(result, Severity.PASS)
    live_pass = "[sandbox].memory = 16g: the Sandbox's memory limit, which sizes the Memory Budget"
    assert warns == []
    if row in {"inside-applied", "inside-unaligned"}:
        assert drift == []
        assert live_pass in passes
    elif row.startswith("inside-hand-written"):
        assert drift == []
        (note,) = [m for m in finding_messages(result, Severity.NOTE) if "cannot be applied" in m]
        assert "which Booley did not create: the Memory Budget " in note
        assert note.endswith(
            "uses installed memory"
            if row.endswith("unreadable")
            else "uses this Sandbox's own memory limit of 16g, set by its devcontainer spec"
        )
        assert "on the host" not in note + _fixes(result, note)[0]
    elif row == "inside-unapplied":
        (warning,) = drift
        assert "'2290m'" in warning
        assert "still 16g" in warning
        assert "`booley session refresh`" in _fixes(result, warning)[0]
    else:
        (warning,) = drift
        assert "no readable memory limit" in warning
        assert not any(m.startswith("[sandbox].memory") for m in passes)
        assert _fixes(result, warning) == [f"recreate the Sandbox: {UNKNOWN_SANDBOX_STEPS}"]


def test_sandbox_swap(tmp_path, monkeypatch):
    """ADR 0087: Doctor warns on a ``[sandbox].swap`` the Docker host cannot back."""
    for row in ("fits", "over-host-swap", "over-host-total", "unparseable", "host-unknown"):
        root = tmp_path / row
        root.mkdir()
        _assert_sandbox_swap_row(row, root, monkeypatch)


def _assert_sandbox_swap_row(row, tmp_path, monkeypatch):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    # memory 16g on a host with 32g of memory and 16g of swap (the fake default).
    if row == "over-host-swap":
        _edit_booley_toml(booley_dir, 'memory = "16g"\n', 'memory = "16g"\nswap = "20g"\n')
    elif row == "over-host-total":
        fake_docker_host(monkeypatch, memory_gib=8, swap_gib=4)
        _edit_booley_toml(booley_dir, 'memory = "16g"\n', 'memory = "16g"\nswap = "2g"\n')
    elif row == "unparseable":
        _edit_booley_toml(booley_dir, 'memory = "16g"\n', 'memory = "16g"\nswap = "lots"\n')
    elif row == "host-unknown":

        def unreadable(_image):
            raise doctor.pi.DockerImageError("probe failed")

        monkeypatch.setattr(doctor.pi, "probe_docker_host_memory", unreadable, raising=False)

    result = run_doctor(tmp_path)

    warns = finding_messages(result, Severity.WARN, check_id="sandbox.swap-unbacked")
    if row == "fits":
        assert warns == []
        assert (
            "[sandbox].swap: the Sandbox may swap up to 4g beyond its memory"
            in finding_messages(result, Severity.PASS)
        )
    elif row == "host-unknown":
        assert warns == []
        assert any(
            "the Docker host's swap is unknown: probe failed" in m
            for m in finding_messages(result, Severity.SKIP)
        )
    else:
        assert len(warns) == 1
        warning = warns[0]
        expected = {
            "over-host-swap": "20g is more than the 16g of swap the Docker host has",
            "over-host-total": "18g is more than the 12g of memory plus swap",
            "unparseable": "'lots' is unparseable",
        }[row]
        assert expected in warning


def test_hand_written_sandbox_doctor_names_no_host_steps(tmp_path, monkeypatch):
    """A Sandbox Booley did not create is rebuilt from its own spec: no finding names a host step."""
    runtime_setup(tmp_path, monkeypatch, inside=True, sandbox=RuntimeLocation.HAND_WRITTEN_SANDBOX)
    _edit_booley_toml(tmp_path / ".booley", 'memory = "16g"\n', 'memory = "16g"\nswap = "20g"\n')
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 8388608 kB\nSwapTotal: 0 kB\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "_PROC_MEMINFO", meminfo)

    result = run_doctor(tmp_path)

    assert any(
        "Booley did not create this Sandbox, so [sandbox].swap is not applied here" in m
        for m in finding_messages(result, Severity.SKIP)
    )
    assert finding_messages(result, Severity.WARN, check_id="sandbox.swap-unbacked") == []
    for finding in result.findings:
        if finding.check_id == "sandbox.git-identity-override":
            continue  # names the hand-written path beside the issued one
        text = f"{finding.message}\n{finding.fix}".split("or on the host:")[0]
        for host_step in ("session refresh", "session up", "on the host", "Rebuild Container"):
            assert host_step not in text, finding


def test_sandbox_swap_warns_on_a_non_string_setting(tmp_path, monkeypatch):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    _edit_booley_toml(booley_dir, 'memory = "16g"\n', 'memory = "16g"\nswap = 0\n')
    result = run_doctor(tmp_path)
    (warning,) = finding_messages(result, Severity.WARN, check_id="sandbox.swap-unbacked")
    assert "swap = 0 is not a string" in warning


def test_sandbox_swap_skips_without_memory_or_project(tmp_path, monkeypatch):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    _edit_booley_toml(booley_dir, 'memory = "16g"\n', "")
    result = run_doctor(tmp_path)
    assert any(
        "no swap limit without a parseable" in m for m in finding_messages(result, Severity.SKIP)
    )

    (booley_dir / "booley.toml").write_text("[project\n", encoding="utf-8")
    result = run_doctor(tmp_path)
    assert "sandbox swap skipped - no valid project config" in finding_messages(
        result, Severity.SKIP
    )


def test_sandbox_swap_reads_the_host_inside_the_sandbox(tmp_path, monkeypatch):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 8388608 kB\nSwapTotal: 0 kB\n", encoding="utf-8")
    place(monkeypatch, RuntimeLocation.ISSUED_SANDBOX)
    monkeypatch.setattr(doctor, "_PROC_MEMINFO", meminfo)
    host, why = doctor._read_host_memory(None, "img")
    assert why == ""
    assert (host.memory_bytes, host.swap_bytes) == (8 * 1024**3, 0)

    meminfo.write_text("nothing\n", encoding="utf-8")
    host, why = doctor._read_host_memory(None, "img")
    assert host is None
    assert "cannot read /proc/meminfo" in why

    place(monkeypatch, RuntimeLocation.HOST)
    assert doctor._read_host_memory(None, "img") == (None, "Docker is not available")


def _prepare_row(monkeypatch, row):
    if row == "inside-marker":
        # The Sandbox owns these probes; answer them healthy.
        monkeypatch.setattr(web_isolation, "policy_error", lambda: None)
    elif row in {"host-no-marker", "host-unreadable-env"}:
        env = '["PATH=/usr/bin"]\n' if row == "host-no-marker" else "null\n"
        _answer_command(monkeypatch, lambda cmd: "{{json .Config.Env}}" in cmd, stdout=env)
    elif row == "host-image-absent":
        _answer_command(monkeypatch, lambda cmd: "{{json .Config.Env}}" in cmd, returncode=1)
    elif row == "host-no-runtime":
        report = DiagnosticReport((DiagnosticFinding(Severity.PASS, "Host Bootstrap: ok"),))
        monkeypatch.setattr(
            host_diagnostics,
            "inspect_host",
            lambda _root: host_diagnostics.HostDiagnosticResult(report, None),
        )


def test_runtime_marker_fix_for_a_hand_written_sandbox(tmp_path, monkeypatch):
    runtime_setup(
        tmp_path,
        monkeypatch,
        inside=True,
        marker=False,
        sandbox=RuntimeLocation.HAND_WRITTEN_SANDBOX,
    )

    (warning,) = finding_messages(
        run_doctor(tmp_path), Severity.WARN, check_id="sandbox.runtime-marker"
    )

    assert warning.endswith(
        "set BOOLEY_CONTAINER=1 in this Sandbox's devcontainer spec (containerEnv), "
        "then rebuild the Sandbox"
    )


@pytest.mark.parametrize(
    "row",
    [
        "inside-marker",
        "inside-fallback",
        "inside-unwritable",
        "inside-no-project",
        "host-marker",
        "host-no-marker",
        "host-unreadable-env",
        "host-image-absent",
        "host-no-runtime",
    ],
)
def test_runtime_location(row, tmp_path, monkeypatch):
    """ADR 0028: the BOOLEY_CONTAINER marker and the slot store, on both venues."""
    options = {
        "inside-marker": {"app": "claude"},
        "inside-fallback": {"marker": False},
        "inside-no-project": {"valid": False},
        "inside-unwritable": {"slots": "file-not-dir/slots"},
    }.get(row, {})
    if row == "inside-unwritable":
        (tmp_path / "file-not-dir").write_text("", encoding="utf-8")  # mkdir under a file fails
    calls = runtime_setup(tmp_path, monkeypatch, inside=row.startswith("inside"), **options)
    _prepare_row(monkeypatch, row)

    result = run_doctor(tmp_path)

    passes = finding_messages(result, Severity.PASS)
    warns = finding_messages(result, Severity.WARN, check_id="sandbox.runtime-marker")
    skips = finding_messages(result, Severity.SKIP)
    if row == "inside-marker":
        assert any("BOOLEY_CONTAINER=1 set" in m for m in passes)
        assert "agent provider-side web access disabled" in passes
        assert any("Memory Reservation store root writable" in m for m in passes)
        assert any("already inside the Sandbox" in m and "nested Docker" in m for m in skips)
        assert not any("HOST" in m for m in finding_messages(result))
    elif row == "inside-fallback":
        (warning,) = warns
        assert "/.dockerenv fallback" in warning
        assert "recreate the Sandbox: in VS Code, " in warning
        assert "--force" not in warning
        assert any("Memory Reservation store root writable" in m for m in passes)
    elif row == "inside-unwritable":
        assert any(
            "Memory Reservation store root not writable" in m
            for m in finding_messages(result, Severity.FAIL)
        )
    elif row == "inside-no-project":
        assert "Sandbox Git identity checks skipped - project config invalid" in skips
    elif row == "host-marker":
        assert any("bakes BOOLEY_CONTAINER=1" in m for m in passes)
        assert warns == []
    elif row in {"host-no-marker", "host-unreadable-env"}:
        (warning,) = warns
        assert "recreate the Sandbox: in VS Code, " in warning
        assert not any("runtime marker" in m for m in finding_messages(result, Severity.FAIL))
    elif row == "host-image-absent":
        assert any("runtime marker check skipped" in m for m in skips)
    else:
        assert any("no Docker/Podman runtime found" in m for m in skips)
        assert not [call for call in calls if call[0] == "None"]


def test_runtime_location_names_the_container_host_override(tmp_path, monkeypatch):
    runtime_setup(tmp_path, monkeypatch, inside=False)
    place(monkeypatch, RuntimeLocation.HOST, container_host=True)
    result = run_doctor(tmp_path)
    assert any("BOOLEY_CONTAINER=0 set" in m for m in finding_messages(result, Severity.PASS))
    assert finding_messages(result, Severity.WARN, check_id="sandbox.runtime-marker") == []


@pytest.mark.parametrize("app", [None, "claude", "codex"])
def test_host_agent_session(app, tmp_path, monkeypatch):
    """A host agent gets no Booley Flows and no error: Doctor explains the Sandbox boundary."""
    runtime_setup(tmp_path, monkeypatch, inside=False, app=app)

    result = run_doctor(tmp_path)

    (finding,) = [f for f in result.findings if f.check_id == "session.agent-on-host"]
    assert finding.severity is Severity.PASS
    assert "Sandbox" in finding.message
    if app is not None:
        for fragment in (
            app,
            "Project Setup",
            "booley_status",
            "booley session up",
            "pairing link",
            "T3 Code thread",
        ):
            assert fragment in finding.message
        assert "Reopen in Container" not in finding.message


_CUSTOM_FLOW = (
    "from booley.flows.base import BooleyFlow\n\n\n"
    "class CustomFlow(BooleyFlow):\n"
    '    name = "custom_flow"\n'
    '    description = "A project Flow."\n'
)


@pytest.mark.parametrize("row", ["custom-and-specialist", "none", "builtin-only"])
def test_disabled_endpoints_note(row, tmp_path, monkeypatch):
    """One NOTE names disabled Specialists and custom Flows, never a built-in Flow."""
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    disabled = {
        "custom-and-specialist": ("flows.custom_flow", "flows.lint", "specialists.reviewer"),
        "builtin-only": ("flows.lint",),
    }.get(row, ())
    for name in disabled:
        if name == "flows.lint":  # the fixture Project already declares this table
            _edit_booley_toml(booley_dir, "[flows.lint]\n", "[flows.lint]\nenabled = false\n")
        else:
            toml = booley_dir / "booley.toml"
            toml.write_bytes(toml.read_bytes() + f"\n[{name}]\nenabled = false\n".encode())
    if row == "custom-and-specialist":
        (booley_dir / "mcp_tools").mkdir()
        (booley_dir / "mcp_tools" / "custom_flow.py").write_text(_CUSTOM_FLOW, encoding="utf-8")

    result = run_doctor(tmp_path)

    notes = [m for m in finding_messages(result, Severity.NOTE) if "MCP endpoints disabled" in m]
    expected = ["MCP endpoints disabled in booley.toml: custom_flow, reviewer"]
    assert notes == (expected if row == "custom-and-specialist" else [])


def _patch_developer_probe(monkeypatch, measure=None) -> None:
    """Stub the two provider-backed probes a deep run inside the Sandbox would make."""
    healthy = developer_probe.ProbeMeasurement(
        _GIB, True, developer_probe.ProbeUsage(1, 0, 1, 0.0)
    )
    monkeypatch.setattr(developer_probe, "measure_developer_rss", measure or (lambda _r: healthy))
    monkeypatch.setattr(
        developer_probe,
        "probe_model_access",
        lambda *_args, **_kw: developer_probe.ProbeUsage(100, 0, 5, 0.001),
        raising=False,
    )


def _selftest_run(tmp_path, monkeypatch, capsys, exit_by_kind, *, inside=False, stderr=""):
    """Run a whole deep Doctor with the sim fixture, answering each self-test launch by kind.

    *exit_by_kind* maps ``good`` / ``bad`` to an exit code or ``"timeout"``. Returns the result
    and one ``(argv, env, printed)`` per launch, *printed* being the text shown before it.
    """
    from tests.harness.test_doctor_selftest_builtin import _write_sim_bad_report

    if inside:
        runtime_setup(tmp_path, monkeypatch, inside=True)
        _patch_developer_probe(monkeypatch)
    else:
        patch_doctor_environment(monkeypatch, tmp_path, write_project(tmp_path))
    booley_dir = tmp_path / ".booley"
    write_bad_overlay(booley_dir)
    _deep_synth_writes_complete_report(monkeypatch, booley_dir)
    printed: list[str] = []

    def answer(argv, env):
        kind = env.get(selftest_overlay.INTERNAL_KIND_ENV)
        if kind is None:
            return None
        printed.append(colors.strip_ansi(capsys.readouterr().out))
        if exit_by_kind[kind] == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        if kind == "bad" and exit_by_kind[kind] == 1:
            _write_sim_bad_report(argv, project=SimpleNamespace(booley_dir=booley_dir))
        return subprocess.CompletedProcess(
            argv, exit_by_kind[kind], stdout="", stderr=stderr if kind == "bad" else ""
        )

    launches = script_flow(monkeypatch, "sim", answer)
    result = run_doctor(tmp_path, deep=True)
    selftest = [(argv, env) for argv, env in launches if selftest_overlay.INTERNAL_KIND_ENV in env]
    return result, [(argv, env, text) for (argv, env), text in zip(selftest, printed, strict=True)]


_GOOD_CASE = "sim self-test good case 'sim_fast'"
_BAD_CASE = "sim self-test bad case 'sim_fast + bad overlay'"
_INFRA_STDERR = "contract_error: finding with an empty file\nreason: the report has no verdict\n"


@pytest.mark.parametrize(
    ("exit_by_kind", "stderr", "expected", "shown"),
    [
        (
            {"good": 1, "bad": 0},
            "",
            [
                (Severity.FAIL, f"{_GOOD_CASE} did not pass (exit 1)"),
                (Severity.FAIL, f"{_BAD_CASE} FALSE-PASSED (exit 0) on a known-bad design"),
            ],
            (),
        ),
        (
            {"good": 0, "bad": 2},
            _INFRA_STDERR,
            [
                (Severity.PASS, f"{_GOOD_CASE} passes"),
                (
                    Severity.FAIL,
                    f"{_BAD_CASE} returned an infra error (exit 2) instead of a graded failure",
                ),
            ],
            # The tool's own output reaches the user, its reason line after the first included.
            tuple(_INFRA_STDERR.splitlines()),
        ),
        (
            {"good": "timeout", "bad": 1},
            "",
            [
                (Severity.FAIL, f"{_GOOD_CASE} timed out after 4830s"),
                (Severity.PASS, f"{_BAD_CASE} correctly graded a failure"),
            ],
            # The remedy names the stage-specific knobs (084f0000b).
            ("[flows.sim].build_timeout_ms", "[flows.sim].timeout_ms"),
        ),
    ],
    ids=["good-fails-bad-false-passes", "bad-infra-error", "good-timeout"],
)
def test_deep_selftest_grades_each_fail_path(
    tmp_path, monkeypatch, capsys, exit_by_kind, stderr, expected, shown
):
    """QA-4/QA-5: --deep proves a verification Flow can DETECT a bad design."""
    result, launches = _selftest_run(tmp_path, monkeypatch, capsys, exit_by_kind, stderr=stderr)

    # Each launch drained the text printed before it.
    output = "".join(text for _argv, _env, text in launches)
    output += colors.strip_ansi(capsys.readouterr().out)
    rows = [(f.severity, f.message) for f in result.findings if "sim self-test" in f.message]
    assert rows == expected
    assert all(text in output for text in shown)
    # The kind reaches the Flow inside the Sandbox as a `docker exec -e` pair.
    for (argv, _env, _text), kind in zip(launches, ("good", "bad"), strict=True):
        assert argv[:3] == [_CONTAINER_CLI, "exec", "-e"]
        assert f"{selftest_overlay.INTERNAL_KIND_ENV}={kind}" in argv


def test_deep_selftest_runs_in_place_inside_the_sandbox(tmp_path, monkeypatch, capsys):
    """F-17 / ADR 0028: inside the Sandbox the self-test runs this interpreter, not Docker."""
    ticket_vars = {
        "BOOLEY_SLUG": "active-ticket",
        "BOOLEY_TICKET_FILE": "/ticket.md",
        "BOOLEY_LOGS_DIR": "/ticket-logs",
        "BOOLEY_RUNTIME_DIR": "/ticket-logs/.runtime",
        "BOOLEY_EXECUTION_ID": "resume-generation",
    }
    for key, value in ticket_vars.items():
        monkeypatch.setenv(key, value)

    result, launches = _selftest_run(
        tmp_path, monkeypatch, capsys, {"good": 0, "bad": 1}, inside=True
    )

    rows = [(f.severity, f.message) for f in result.findings if "sim self-test" in f.message]
    assert rows == [
        (Severity.PASS, f"{_GOOD_CASE} passes"),
        (Severity.PASS, f"{_BAD_CASE} correctly graded a failure"),
    ]
    assert [argv[0] for argv, _env, _text in launches] == [sys.executable] * 2
    # A self-test must not inherit the ticket's acceptance context.
    assert all(ticket_vars.keys().isdisjoint(env) for _argv, env, _text in launches)
    # The case is announced, flushed, before its long run starts.
    for (_argv, _env, text), case in zip(launches, (_GOOD_CASE, _BAD_CASE), strict=True):
        assert text.rstrip().splitlines()[-1] == f"  ▸  {case} (real EDA run; timeout 1h 20m)"


def _rusage(*kib: int):
    """A ``getrusage`` stand-in reporting the child peak RSS *kib* readings in turn."""
    readings = iter([SimpleNamespace(ru_maxrss=value) for value in kib])
    return lambda _who: next(readings)


class TestDeveloperProbe:
    """doctor --deep measures the developer memory term (ADR 0028 D12)."""

    @pytest.fixture
    def probe_backend(self, monkeypatch):
        """Patch the backend config the probe reads; return ``install_agent`` for its script."""
        from booley.runtime import agent_config as config_mod

        class _Cfg:
            def __init__(self):
                self.settings = self
                self.effort_pin = None

            def internal_model(self):
                return "test-model"

            def cli_for_model_id(self, model):
                return None

        monkeypatch.setattr(config_mod, "load_backend_config", lambda root: None)
        monkeypatch.setattr(config_mod, "get_backend_config", _Cfg)
        return lambda script: install_agent(monkeypatch, script)

    @pytest.mark.parametrize(
        ("row", "expected", "summary"),
        [
            (
                "measured",
                [
                    (Severity.PASS, "provider CLI peak RSS measured: 2g"),
                    (
                        Severity.PASS,
                        "Specialist provider live authorization check completed successfully",
                    ),
                ],
                "2 agent calls, 1,334 input tokens (1,000 cached), 61 output tokens, cost $0.0041.",
            ),
            (
                "limitation",
                [
                    (
                        Severity.SKIP,
                        "Specialist provider memory probe skipped - probe agent failed: no auth",
                    )
                ],
                "no agent call ran (0 tokens, $0.00).",
            ),
            (
                "agent-failure",
                [
                    (
                        Severity.FAIL,
                        "Specialist provider probe could not complete a trivial call"
                        " — every Specialist using this provider will fail the same way"
                        " at launch: probe agent failed: OAuth session expired",
                    )
                ],
                # Regression (2026-07-23): the developer agent crashed at launch on expired
                # credentials in every ticket run, but the probe degraded it to a SKIP.
                "no agent call ran (0 tokens, $0.00).",
            ),
            (
                "unknown-usage",
                [
                    (
                        Severity.FAIL,
                        "Specialist provider probe could not complete a trivial call"
                        " — every Specialist using this provider will fail the same way"
                        " at launch: dead",
                    )
                ],
                "1 agent call, 0 input tokens (0 cached), 0 output tokens, cost $0.0000;"
                " usage unknown for 1 failed call.",
            ),
        ],
    )
    def test_deep_probe_outcomes(self, tmp_path, monkeypatch, capsys, row, expected, summary):
        runtime_setup(tmp_path, monkeypatch, inside=True)
        _deep_synth_writes_complete_report(monkeypatch, tmp_path / ".booley")
        printed_before_call: list[str] = []
        errors = {
            "limitation": developer_probe.ProbeError("probe agent failed: no auth"),
            "agent-failure": developer_probe.ProbeError(
                "probe agent failed: OAuth session expired", agent_failure=True
            ),
            "unknown-usage": developer_probe.ProbeError(
                "dead", agent_failure=True, agent_called=True
            ),
        }

        def measure(_root):
            printed_before_call.append(capsys.readouterr().out)
            if row in errors:
                raise errors[row]
            return developer_probe.ProbeMeasurement(
                2 * _GIB, True, developer_probe.ProbeUsage(1234, 1000, 56, 0.0031)
            )

        _patch_developer_probe(monkeypatch, measure)

        result = run_doctor(tmp_path, deep=True)

        output = colors.strip_ansi(capsys.readouterr().out)
        rows = [
            (f.severity, f.message)
            for f in result.findings
            if "provider CLI peak RSS" in f.message or "Specialist provider" in f.message
        ]
        assert rows == expected
        assert f"Agent-backed checks: {summary}" in output
        if row == "measured":
            # Issue #885: the running-step line precedes the (slow) provider call.
            announced = colors.strip_ansi(printed_before_call[0]).rstrip().splitlines()[-1]
            assert announced.startswith(
                "  ▸  provider-backed check: Specialist provider memory and authorization probe ("
            )

    def test_usage_limit_row_names_the_limit_and_reset_without_auth_advice(
        self, tmp_path, monkeypatch, probe_backend
    ):
        from booley.runtime.agent_errors import UsageLimitError
        from tests.runtime.test_execution_ports import FAR_RESET_MESSAGE

        probe_backend(UsageLimitError(FAR_RESET_MESSAGE, provider="claude"))
        runtime_setup(tmp_path, monkeypatch, inside=True)
        _deep_synth_writes_complete_report(monkeypatch, tmp_path / ".booley")

        real = developer_probe.measure_developer_rss
        _patch_developer_probe(monkeypatch, lambda root, **_: real(root, getrusage=_rusage(0, 0)))

        result = run_doctor(tmp_path, deep=True)

        rows = [f for f in result.findings if "Specialist provider" in f.message]
        assert [(f.severity, f.message) for f in rows] == [
            (
                Severity.SKIP,
                f"Specialist provider memory probe skipped - probe agent failed: {FAR_RESET_MESSAGE}",
            )
        ]
        assert "auth" not in f"{rows[0].message} {rows[0].fix}".lower()

    def test_long_check_announcement_flushes_stdout(self, monkeypatch):
        events: list[str] = []

        class _Writer:
            def write(self, text):
                events.append("write")
                return len(text)

            def flush(self):
                events.append("flush")

        monkeypatch.setattr(sys, "stdout", _Writer())
        # Private seam: the flush is only observable with a writer that records it; a whole
        # run's captured stdout looks the same flushed or not.
        doctor._announce_long_check("x", "y")
        assert "write" in events
        assert events[-1] == "flush"

    def test_developer_probe_reports_without_a_completeness_tracker(self, tmp_path, monkeypatch):
        set_venue(monkeypatch, True)
        _patch_developer_probe(monkeypatch)
        rec = _Rec()
        # Private seam: the only caller always passes a tracker, so no whole run takes the
        # tracker-less return.
        record = doctor._run_developer_probe(_adr28_project(tmp_path), rec.p, rec.s, rec.f)
        assert record is not None
        assert rec.kinds() == {"pass"}

    def test_agent_settings_load_defaults_without_a_booley_toml(self, tmp_path):
        """The probe reads agent settings of a Project that has no config file yet."""
        from booley.config import agent as agent_settings

        agent_settings.set_agent_settings(None)
        try:
            loaded = agent_settings.load_agent_settings(tmp_path, booley_dir=tmp_path / ".booley")
        finally:
            agent_settings.set_agent_settings(None)
        assert loaded.sandbox == agent_settings.SandboxConfig()
        assert dict(loaded.specialist_models) == {}

    @pytest.mark.parametrize("outcome", ["accepted", "refused", "timeout"])
    def test_probe_model_access_grades_the_provider_answer(self, tmp_path, monkeypatch, outcome):
        from booley.config.agent import AgentSettings
        from booley.core.models import AgentResult
        from booley.runtime import agent_config as config_mod
        from booley.runtime.agent_errors import ModelUnavailableError

        loaded = []
        monkeypatch.setattr(
            config_mod, "load_backend_config", lambda root, **kw: loaded.append(kw)
        )
        monkeypatch.setattr(
            config_mod, "get_backend_config", lambda: SimpleNamespace(settings=AgentSettings())
        )
        agent = install_agent(
            monkeypatch,
            ModelUnavailableError(
                model="some-model", provider="codex", settings=(), detail="model_not_found"
            )
            if outcome == "refused"
            else AgentResult(output="OK", input_tokens=7, timed_out=outcome == "timeout"),
        )

        if outcome == "accepted":
            usage = developer_probe.probe_model_access(
                tmp_path, "some-model", booley_dir=tmp_path / "bd"
            )
            assert ([call.model for call in agent.calls], usage.input_tokens) == (
                ["some-model"],
                7,
            )
            assert loaded == [{"booley_dir": tmp_path / "bd"}]  # same config as the model list
            return
        with pytest.raises(developer_probe.ProbeError) as excinfo:
            developer_probe.probe_model_access(tmp_path, "some-model")
        assert excinfo.value.agent_called is True
        if outcome == "refused":
            assert isinstance(excinfo.value.__cause__, ModelUnavailableError)
        else:
            assert excinfo.value.usage.input_tokens == 7  # a timeout never reads as acceptance

    @pytest.mark.parametrize("site", ["call", "rusage", "timeout", "no_rss"])
    def test_measure_post_call_errors_carry_usage(self, tmp_path, probe_backend, site):
        from booley.core.models import AgentResult

        probe_backend(
            RuntimeError("dead backend")
            if site == "call"
            else AgentResult(output="OK", input_tokens=10, timed_out=site == "timeout")
        )
        calls = {"n": 0}

        def getrusage(_who):
            calls["n"] += 1
            if calls["n"] == 2 and site == "rusage":
                raise OSError("no accounting")
            return SimpleNamespace(ru_maxrss=0 if site == "no_rss" else 100_000)

        with pytest.raises(developer_probe.ProbeError) as excinfo:
            developer_probe.measure_developer_rss(tmp_path, getrusage=getrusage)
        assert excinfo.value.agent_called is True
        if site == "call":
            assert excinfo.value.usage is None
        else:
            assert excinfo.value.usage.input_tokens == 10

    def test_measure_pre_call_error_reports_no_agent_call(self, tmp_path, monkeypatch):
        from booley.runtime import agent_config as config_mod

        def boom(_root):
            raise RuntimeError("no backend configured")

        monkeypatch.setattr(config_mod, "load_backend_config", boom)
        with pytest.raises(developer_probe.ProbeError, match="no backend") as excinfo:
            developer_probe.measure_developer_rss(tmp_path)
        assert excinfo.value.agent_called is False

    @pytest.mark.parametrize(
        ("readings", "peak_kib", "exact"),
        [((100_000, 300_000), 300_000, True), ((400_000, 400_000), 400_000, False)],
        ids=["delta", "upper-bound"],
    )
    def test_measure_reports_the_child_rss_delta(
        self, tmp_path, probe_backend, readings, peak_kib, exact
    ):
        """Without a delta (an earlier child was bigger) the peak is a safe upper bound."""
        from booley.core.models import AgentResult

        agent = probe_backend(
            AgentResult(
                output="OK", input_tokens=120, cached_tokens=30, output_tokens=7, cost_usd=0.0031
            )
        )
        measurement = developer_probe.measure_developer_rss(tmp_path, getrusage=_rusage(*readings))
        assert measurement.peak_rss_bytes == peak_kib * 1024
        assert measurement.exact is exact
        assert measurement.usage == developer_probe.ProbeUsage(120, 30, 7, 0.0031)
        assert agent.calls[0].max_turns == 1  # a 1-turn trivial probe, no tools
        assert agent.calls[0].allowed_agent_capabilities == []


# =============================================================


# Flow Target configured — cheap pass (F-9)
# =============================================================


# =============================================================
def test_doctor_reports_no_target_style_advisories(tmp_path, monkeypatch):
    """No Flow acts on a tools-section Target, a Target name or an unused `default:`."""
    targets = (
        "  default:\n    filesets: [rtl]\n"
        "  soc_old:\n    filesets: [rtl]\n    flow_options: {booley: {doctor: [sim]}}\n"
        "    default_tool: icarus\n    tools: {icarus: {}}\n"
        + _target("soc_lint", flow="lint", filesets="rtl", top="dut", doctor=True)
    )
    fs = "  rtl: {files: [rtl/dut.sv], file_type: systemVerilogSource}\n"
    _core_project(tmp_path, monkeypatch, {"demo.core": _core("demo", targets, fs)})

    result = run_doctor(tmp_path)

    assert _rows(result, Severity.PASS, "soc_lint, soc_old") != []  # the .core audit ran
    advisories = ("tools-section", "<axis>_<subject>", "no axis prefix", "'default:'")
    assert [f.message for f in result.findings if any(a in f.message for a in advisories)] == []


class TestReporterAcceptsFixHints:
    def test_plain_warning_without_stable_id_is_rejected(self):
        # Private seam: no check passes a plain string, so no whole run reaches this guard.
        reporter = doctor._Reporter.create()
        with pytest.raises(TypeError, match="stable check ID"):
            reporter.warn_("just a warning")


def test_concise_reporter_suppresses_only_pass_rendering(tmp_path, monkeypatch, capsys):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    _answer_command(
        monkeypatch, lambda argv: "booley.flows.lint" in argv, returncode=3, stderr="boom"
    )

    result = run_doctor(tmp_path, concise=True)

    lines = colors.strip_ansi(capsys.readouterr().out).splitlines()
    # The counts table's "✓ passed" row always prints; only the pass message rows are hidden.
    assert not any(line.startswith("  ✓  ") for line in lines)
    assert "  ·  project guidance file missing; run setup guidance when ready" in lines
    assert any(
        line.startswith("  !  ") and "fusesoc.worktree-core-shadow" in line for line in lines
    )
    assert any(line.startswith("  ·  ") and " skipped - " in line for line in lines)
    assert "  ✗  lint dry-run [lint_fast] failed with exit 3" in lines
    [passed] = [line for line in lines if re.fullmatch(r"  ✓ passed +\d+", line)]
    assert int(passed.split()[-1]) == result.counts["pass"] > 0
    # A WARN without a fix hint prints no empty hint line.
    assert not any(line.strip() == "→" for line in lines)
    assert result.exit_code == 1


def _inspect_line_endings(root: Path, booley_dir: Path | None = None) -> DiagnosticReport:
    """Run the line-ending owner the way Doctor does, returning its report."""
    return readiness.inspect_line_endings(readiness.LineEndingInspectionRequest(root, booley_dir))


def _findings(report: DiagnosticReport, severity: Severity) -> list[DiagnosticFinding]:
    return [finding for finding in report.findings if finding.severity is severity]


# =============================================================


# _check_subscription_creds_health — expired-creds gap (2026-07-23 incident)
# =============================================================


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
        set_venue(monkeypatch, False)
        return home

    def _run(self, provider: str = auth_token.APP_CLAUDE) -> _Rec:
        rec = _Rec()
        # Private: each case needs its own credential-file state, read through this one check (not yet moved onto the public run).
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

    @pytest.mark.parametrize("seeded", [False, True], ids=["unseeded", "seeded"])
    def test_expired_login_fix_follows_seed(self, creds_home, monkeypatch, tmp_path, seeded):
        """Only a seeded Sandbox (or the host) can blame host rotation / offer `booley auth`."""
        creds = creds_home / ".claude" / ".credentials.json"
        creds.write_text(self._EXPIRED)
        seed = tmp_path / "seed-creds.json"
        # Private: the seed path is a fixed in-Sandbox path under the agent home; a host test cannot create it.
        monkeypatch.setitem(dc._APP_CREDS_SEED_TARGET, auth_token.APP_CLAUDE, str(seed))
        if seeded:
            seed.write_text(self._VALID)  # valid seed: the seed-expired WARN stays quiet
        set_venue(monkeypatch, True)
        # Private: the seeded-vs-unseeded fix text is chosen inside this one check, whose inputs are credential files a whole run cannot vary per case.
        reporter = doctor._Reporter.create()

        doctor._check_subscription_creds_health(
            auth_token.APP_CLAUDE, reporter.pass_, reporter.warn_
        )

        (finding,) = [f for f in reporter.findings if f.severity is Severity.WARN]
        assert finding.check_id == "agent.subscription-credential-expired"
        message = finding.message.replace(str(creds), "")
        if seeded:
            assert "host has rotated" in message
            assert "booley auth --cli claude" in finding.fix
        else:
            assert "host" not in message and "rotat" not in message
            assert "booley auth" not in finding.fix and "/login" in finding.fix

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

    def test_expiry_clock_preserves_finding_and_transcript(self, creds_home, monkeypatch, capsys):
        from datetime import UTC, datetime

        now = datetime(2026, 10, 8, 10, 30, tzinfo=UTC).timestamp()
        monkeypatch.setenv("BOOLEY_LOCAL_TIMEZONE", "+04:00")
        monkeypatch.setattr(doctor, "wall_clock", lambda: now)
        place(monkeypatch, RuntimeLocation.HOST, container_host=True)
        # Private: these stubs isolate the expiry display from the other credential checks (not yet moved onto the public run).
        monkeypatch.setenv("BOOLEY_AGENT_APP", auth_token.APP_CLAUDE)
        monkeypatch.delenv("BOOLEY_AGENT_CLIS", raising=False)
        monkeypatch.setattr(doctor, "_check_agent_auth_token", lambda *_a, **_k: None)
        monkeypatch.setattr(doctor, "_check_oauth_token", lambda *_a, **_k: None)
        monkeypatch.setattr(doctor, "_check_auth_recorded", lambda *_a: None)
        published = []
        monkeypatch.setattr(
            doctor.health, "publish_result", lambda _root, **kw: published.append(kw)
        )
        path = auth_token.subscription_creds_path(auth_token.APP_CLAUDE)
        for hour, human in ((11, "15:00:00"), (10, "14:00:00")):
            expiry = datetime(2026, 10, 8, hour, tzinfo=UTC).timestamp()
            path.write_text(json.dumps({"claudeAiOauth": {"expiresAt": expiry * 1000}}))
            # Private: the expiry transcript needs the stubs above, which a whole run cannot isolate.
            reporter = doctor._Reporter.create()
            doctor._run_agent_credential_checks(
                SimpleNamespace(booley_toml={"agent": {"default": "claude", "auth": "visitor"}}),
                reporter,
            )
            result = reporter.result(0)
            legacy = f"{hour + 4:02d}:00 · 08 OCT 2026"
            expected = (
                f"subscription login valid until {legacy}"
                if hour == 11
                else f"subscription login at {path} is expired ({legacy}) and refresh "
                "fails once the host has rotated the shared refresh token — agents then crash at launch"
            )
            assert result.findings[0].message == expected, (
                "doctor expiry display/record contract missing"
            )
            assert human in capsys.readouterr().out, (
                "doctor expiry display/record contract missing"
            )
            doctor.record_manual_result(creds_home, result)
            assert published[-1]["diagnostics"].findings[0].message == expected
            assert (
                published[-1]["transcript"]
                == f"{result.findings[0].severity.value.upper()}: {expected}\n"
            )

    def test_absent_or_unparseable_login_is_silent(self, creds_home):
        assert self._run().events == []  # absent
        (creds_home / ".claude" / ".credentials.json").write_text("not json")
        assert self._run().events == []  # unparseable: presence checks own it

    def test_expired_seed_sidecar_warns_in_container(self, creds_home, monkeypatch):
        set_venue(monkeypatch, True)
        seed = creds_home / ".claude-creds-seed.json"
        seed.write_text(self._EXPIRED)
        # Private: the in-Sandbox seed path is a fixed path under the agent home that a host test cannot create.
        monkeypatch.setitem(dc._APP_CREDS_SEED_TARGET, auth_token.APP_CLAUDE, str(seed))
        rec = self._run()
        assert "warn" in rec.kinds()
        assert any("pinned" in m for _, m in rec.events)

    def test_expired_seed_with_stored_token_is_quiet(self, creds_home, monkeypatch):
        set_venue(monkeypatch, True)
        seed = creds_home / ".claude-creds-seed.json"
        seed.write_text(self._EXPIRED)
        # Private: same fixed in-Sandbox seed path as above.
        monkeypatch.setitem(dc._APP_CREDS_SEED_TARGET, auth_token.APP_CLAUDE, str(seed))
        auth_token.store_token("sk-ant-oat01-x")
        rec = self._run()
        assert "warn" not in rec.kinds()


@pytest.mark.parametrize("verbose", [False, True])
def test_flow_check_doctor_output_is_stable(tmp_path, monkeypatch, capsys, verbose):
    """Characterization: dry-run rows, hints, and excerpts print as before the report move.

    A failing dry-run always shows its excerpt; a passing one shows a single
    excerpt line only under ``--verbose``.
    """
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    sim_stderr = "\n".join(
        ["sim: starting", "sim: elaborating", "sim: linking"]
        + [f"sim: note {index}" for index in range(4)]
        + ["%Error: undefined signal foo", "sim: reason follows"]
        + [f"sim: trailer {index}" for index in range(4)]
    )
    _answer_command(
        monkeypatch,
        lambda argv: "booley.flows.sim" in argv and "--dry-run" in argv,
        returncode=3,
        stderr=sim_stderr,
    )
    _answer_command(
        monkeypatch,
        lambda argv: "booley.flows.lint" in argv and "--dry-run" in argv,
        stdout="\n".join(["lint: ok line one", "lint: line two", "lint: line three"]),
    )

    result = run_doctor(tmp_path, verbose=verbose)

    text = colors.strip_ansi(capsys.readouterr().out).replace(str(tmp_path), "<root>")
    lines = text.splitlines()
    start = lines.index(_FLOW_FAIL_ROWS[0])
    end = lines.index("  ✓  synth executes in the Sandbox")
    assert lines[start:end] == [
        *_FLOW_FAIL_ROWS,
        *_LINT_ROWS,
        *(_LINT_VERBOSE_EXCERPT if verbose else []),
    ]
    assert result.exit_code == 1


_SYNTH_EVIDENCE_REPORTS: dict[str, dict | str | None] = {
    "s_logical": _COMPLETE_LOGICAL_SYNTH_REPORT,
    "s_no_ppa": {**_COMPLETE_LOGICAL_SYNTH_REPORT, "ppa_complete": False},
    "s_unfinished": {**_COMPLETE_LOGICAL_SYNTH_REPORT, "termination": "timeout"},
    "s_unmapped": {**_COMPLETE_LOGICAL_SYNTH_REPORT, "area_source": "other"},
    "s_unknown_mode": {**_COMPLETE_LOGICAL_SYNTH_REPORT, "synth_mode": "unknown"},
    "s_physical_untimed": {**_COMPLETE_LOGICAL_SYNTH_REPORT, "synth_mode": "physical"},
    "s_physical_no_clock": {
        **_COMPLETE_LOGICAL_SYNTH_REPORT,
        "synth_mode": "physical",
        "timing_complete": True,
    },
    "s_physical": {
        **_COMPLETE_LOGICAL_SYNTH_REPORT,
        "synth_mode": "physical",
        "timing_complete": True,
        "per_clock": [{"clock": "clk"}],
    },
    "s_legacy": {"ppa_complete": True, "timing_complete": True, "per_clock": [{"clock": "clk"}]},
    "s_legacy_untimed": {"ppa_complete": True},
    "s_not_object": "[]",
    "s_malformed": "[",
    "s_missing": None,
}


def _incomplete(reason):
    return (Severity.FAIL, f"produced incomplete synthesis evidence: {reason}")


@pytest.mark.parametrize(
    ("utilization", "toml", "message"),
    [
        (150, "", "dry-run [synth_dut] failed with exit 2"),
        (50, '[flows.synth.timing]\nengine = "sta"\n', "build input(s): timing"),
    ],
)
def test_doctor_fails_synth_configure_errors(tmp_path, monkeypatch, utilization, toml, message):
    options = "tool: yosys, arch: xilinx, synth_mode: physical, booley: {doctor: [synth]}, "
    options += f"advanced_settings_openroad: {{utilization_pct: {utilization}}}"
    sets = "  rtl: {files: [rtl/dut.sv]}\n  sdc: {files: [dut.sdc], file_type: SDC}\n"
    core = _core("synth", _target("synth_dut", "generic", options, "rtl, sdc", "dut"), sets)
    _core_project(
        tmp_path, monkeypatch, {"unit.core": None, "synth.core": core, "dut.sdc": ""}, toml=toml
    )
    real_run = doctor.subprocess.run

    def run(cmd, **kwargs):
        if "booley.flows.synth" not in cmd or "--dry-run" not in cmd:
            return real_run(cmd, **kwargs)
        a = [str(tmp_path) if "/" in c else c for c in cmd]
        out = AsicSynthesizeFlow().execute_cli(a[a.index("booley.flows.synth") + 1 :]).outcome
        return subprocess.CompletedProcess(cmd, out.exit_code, "", out.report_text)

    monkeypatch.setattr(doctor.subprocess, "run", run)
    assert _rows(run_doctor(tmp_path), Severity.FAIL, message)


def test_deep_synthesis_evidence_is_graded_per_target(tmp_path, monkeypatch, capsys):
    """Deep synth passes only with terminal PPA (and, if physical, timing) evidence."""
    from booley.flows.implementation_publication import target_report_path

    options = "tool: yosys, arch: xilinx, booley: {doctor: [synth]}"
    targets = "".join(
        _target(name, "generic", options, "rtl", "dut") for name in _SYNTH_EVIDENCE_REPORTS
    )
    booley_dir = _core_project(
        tmp_path, monkeypatch, {"evidence.core": _core("evidence", targets)}
    )
    _deep_synth_writes_complete_report(monkeypatch, booley_dir)
    report_dir = booley_dir / doctor.DOCTOR_TMP_DIR / "flow-reports"

    def write_report(argv, _env):
        target = _flow_argv_target(argv)
        report = _SYNTH_EVIDENCE_REPORTS.get(target)
        if isinstance(report, dict):
            _write_synth_report(report_dir, report, target)
        elif isinstance(report, str):
            path = target_report_path("synth", target, report_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report, encoding="utf-8")

    script_flow(monkeypatch, "synth", write_report)

    result = run_doctor(tmp_path, deep=True)

    graded = {
        match[1]: (f.severity, match[2].split(" (")[0])
        for f in result.findings
        if (match := re.fullmatch(r"synth deep check \[(s_\w+)\] (.*)", f.message, re.S))
    }
    dry_runs = [f.severity for f in result.findings if "synth dry-run [s_" in f.message]
    assert dry_runs == [Severity.PASS] * len(_SYNTH_EVIDENCE_REPORTS)
    passed = (Severity.PASS, "passed")
    unreadable = _incomplete("report is unreadable")
    no_timing = _incomplete("no completed constrained-clock timing result")
    assert graded == {
        "s_logical": passed,
        "s_no_ppa": _incomplete("ppa_complete is not true"),
        "s_unfinished": _incomplete("logical synthesis did not complete"),
        "s_unmapped": _incomplete("no completed Yosys mapped-area result"),
        "s_unknown_mode": _incomplete("unknown synthesis mode 'unknown'"),
        "s_physical_untimed": no_timing,
        "s_physical_no_clock": no_timing,
        "s_physical": passed,
        "s_legacy": passed,
        "s_legacy_untimed": no_timing,
        "s_not_object": _incomplete("report is not a JSON object"),
        "s_malformed": unreadable,
        "s_missing": unreadable,
    }
    # A Flow that is not synth needs no report: lint's deep check passes without one.
    assert "lint deep check [lint_fast] passed" in finding_messages(result, Severity.PASS)


def _fpga_deep_run(tmp_path, monkeypatch, *, names, toml, marked=True):
    """Whole deep run with FPGA Targets *names*; returns the result, launches and record reasons."""

    def answer(argv, _env):
        target = _flow_argv_target(argv)
        if target.endswith("hang"):
            raise subprocess.TimeoutExpired(argv, 1)
        failed = target.endswith("fail")
        return subprocess.CompletedProcess(
            argv, int(failed), stdout="RESULT: FAIL" if failed else "RESULT: PASS", stderr=""
        )

    booley_dir = _core_project(
        tmp_path, monkeypatch, {"design.core": _fpga_core(*names, marked=marked)}, toml=toml
    )
    _deep_synth_writes_complete_report(monkeypatch, booley_dir)
    launches = script_flow(monkeypatch, "fpga", answer)
    result = run_doctor(tmp_path, deep=True, record_clean=True)
    record = doctor_deep.load_deep_record(booley_dir)
    assert record is not None
    return result, [argv for argv, _env in launches], record.reasons


def test_deep_fpga_check_runs_each_marked_target(tmp_path, monkeypatch, capsys):
    """Deep Doctor runs the FPGA Flow for real on each marked Target and reports it."""
    result, launches, reasons = _fpga_deep_run(
        tmp_path,
        monkeypatch,
        names=("fpga_pass", "fpga_fail", "fpga_hang"),
        toml="\n[flows.fpga]\n",
    )

    output = colors.strip_ansi(capsys.readouterr().out)
    assert [
        (f.severity, f.message) for f in result.findings if f.message.startswith("fpga deep check")
    ] == [
        (Severity.FAIL, "fpga deep check [fpga_fail] failed with exit 1"),
        (Severity.FAIL, "fpga deep check [fpga_hang] timed out after 7380s"),
        (Severity.PASS, "fpga deep check [fpga_pass] passed"),
    ]
    assert all("--no-cache" in argv and "--dry-run" not in argv for argv in launches)
    assert len(launches) == 3
    assert "booley flow fpga --target" not in output
    # A failed run leaves deep checks due either way; a timeout also leaves its check incomplete.
    assert len([reason for reason in reasons if reason.startswith("fpga.smoke.")]) == 1


@pytest.mark.parametrize("case", ["disabled", "not-applicable"])
def test_deep_fpga_check_absent_when_flow_disabled_or_not_applicable(tmp_path, monkeypatch, case):
    """No FPGA deep check, and no FPGA requirement, when the Flow is off or unused."""
    disabled = case == "disabled"
    result, launches, reasons = _fpga_deep_run(
        tmp_path,
        monkeypatch,
        names=("fpga_board",),
        toml="\n[flows.fpga]\nenabled = false\n" if disabled else "",
        marked=disabled,
    )

    rows = [f.message for f in result.findings if f.message.startswith("fpga deep check")]
    assert rows == (["fpga deep check skipped - disabled"] if disabled else [])
    assert launches == []
    assert not any(reason.startswith("fpga.") for reason in reasons)


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

    # Private: it counts CoreManager instances and dependency resolutions per
    # `_CoreAuditInputs`; a whole run also builds the Flow audit's and the native
    # dependency owner's catalogs and inspectors, so the count is not observable there.
    inputs = doctor._CoreAuditInputs(catalog, refs)
    for name in target_names:
        assert inputs.sources_for(name).rtl_source_files == ("rtl/dut.sv",)

    assert len(managers) == 1
    assert resolutions == 1


# ---------------------------------------------------------------------------
# sim run_cwd probe (issue #881)
# ---------------------------------------------------------------------------


def _git_commit_all(root: Path, message: str = "add") -> None:
    """Commit everything staged under ``root`` with a throwaway identity."""
    run_bounded_child(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", message],
        cwd=root,
        check=True,
    )


def _committed_repo(root: Path) -> None:
    """A git repo with one unrelated committed file, so HEAD exists."""
    git_init(root)
    (root / "README").write_text("x\n", encoding="utf-8")
    run_bounded_child(["git", "add", "README"], cwd=root, check=True)
    _git_commit_all(root)


_MAIN = """\
#include "verilated_vcd_c.h"
int main(int argc, char** argv) {{
  VerilatedVcdC* tfp = new VerilatedVcdC;
  {body}
}}
"""


def _dump_project(tmp_path, monkeypatch, opens, *, sim):
    """A Project whose Targets each own the C++ main in *opens*: name to (text, options, tool)."""
    fs = _RTL_TB
    targets = ""
    files = {}
    for name, (text, options, tool) in opens.items():
        fs += f"  {name}_cpp: {{files: [sim/{name}.cpp], file_type: cppSource, tags: [tb]}}\n"
        targets += _target(
            f"sim_{name}",
            options=f"tool: {tool}, verilator_options: {options}",
            filesets=f"rtl, {name}_cpp",
            top="dut",
        )
        files[f"sim/{name}.cpp"] = text
    files["design.core"] = _core("design", targets, fs)
    _core_project(tmp_path, monkeypatch, files, sim=sim)


def _open(path, options="[--timing, --exe]", tool="verilator"):
    return (_MAIN.format(body=f'tfp->open("{path}");'), options, tool)


@pytest.mark.parametrize(
    ("sim", "opens", "dumps", "fix"),
    [
        pytest.param(
            'run_cwd = "."\n',
            {"undeclared": _open("sim.vcd")},
            "sim_undeclared opens sim.vcd",
            '["sim.vcd"]',
            id="unconfigured",
        ),
        pytest.param(
            'run_cwd = "."\ntrace_files = ["declared.vcd", "*.fst", "/unrelated/sim.vcd"]\n',
            {
                "undeclared": _open("sim.vcd"),
                "declared": _open("declared.vcd"),
                "wave": _open("wave.fst"),
                # `*` does not cross a directory separator.
                "nested": _open("nested/deep.fst"),
                "dynamic": (_MAIN.format(body="tfp->open(dump.c_str());"), "[--exe]", "verilator"),
                "line_comment": (
                    _MAIN.format(body='// tfp->open("sim.vcd");'),
                    "[--exe]",
                    "verilator",
                ),
                "block_comment": (
                    _MAIN.format(body='/* tfp->open("sim.vcd"); */'),
                    "[--exe]",
                    "verilator",
                ),
                # `--main` Targets are left to the trace-unavailable check.
                "auto_main": _open("sim.vcd", options="[--main, --timing]"),
                "no_tracer": (
                    'int main() { std::ifstream f; f.open("fixture.vcd"); }\n',
                    "[--exe]",
                    "verilator",
                ),
                "icarus": _open("sim.vcd", options="[]", tool="icarus"),
            },
            "sim_nested opens nested/deep.fst; sim_undeclared opens sim.vcd",
            # The existing entries stay; the two undeclared literals follow, sorted.
            '["declared.vcd", "*.fst", "/unrelated/sim.vcd", "nested/deep.fst", "sim.vcd"]',
            id="literal",
        ),
        pytest.param(
            'run_cwd = "/sim_home"\ntrace_files = ["sim.vcd", "**/*.vcd"]\n',
            {"absolute": _open("/sim_home/sim.vcd"), "nested": _open("nested/sim.vcd")},
            None,
            None,
            id="absolute-cwd",
        ),
        pytest.param(
            'run_cwd = "runs/{attempt}"\ntrace_files = ["a.vcd", "../b.vcd", "*.fst"]\n',
            {"a": _open("a.vcd"), "b": _open("b.vcd"), "c": _open("c.fst")},
            "sim_b opens b.vcd",
            '["a.vcd", "../b.vcd", "*.fst", "b.vcd"]',
            id="templated",
        ),
    ],
)
def test_owned_main_dump_declaration(tmp_path, monkeypatch, sim, opens, dumps, fix):
    _dump_project(tmp_path, monkeypatch, opens, sim=sim)

    result = run_doctor(tmp_path)

    undeclared = [f for f in result.findings if f.check_id == "sim.trace-dump-undeclared"]
    if dumps is None:
        assert undeclared == []
        assert _rows(
            result, Severity.PASS, "2 Verilator sim Target(s) declare their C++ main's dump"
        )
        return
    [finding] = undeclared
    assert f"({dumps})" in finding.message
    assert finding.fix == f"add to [flows.sim] in booley.toml: trace_files = {fix}"


def test_containment_report_alias_and_symlinks(tmp_path, monkeypatch):
    root, alias = tmp_path / "selected", tmp_path / "alias"
    for directory in (root, alias):
        (directory / "reports").mkdir(parents=True)
    monkeypatch.setattr(doctor.dc, "BOOLEY_DIR_TARGET", root.as_posix())
    original = Path.samefile
    calls = []

    def samefile(path, other):
        if {path, Path(other)} == {root, alias}:
            calls.append(path)
            return True
        return original(path, other)

    monkeypatch.setattr(Path, "samefile", samefile)
    project = SimpleNamespace(booley_dir=root, project_root=tmp_path)

    def hint(path):
        # Private seam: the mount alias is `Path.samefile` filesystem identity a whole run
        # cannot set up.
        return doctor._display_report_dir(project, path)

    assert hint(alias / "reports") == ".booley/reports (under the Project Root)"
    assert calls
    (root / "internal").symlink_to(root / "reports", target_is_directory=True)
    assert hint(root / "internal") == ".booley/reports (under the Project Root)"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    assert hint(root / "escape") == str(root / "escape")
    assert hint(outside) == str(outside)


def test_containment_report_rebased_alias_child_symlink(tmp_path, monkeypatch):
    root, alias = tmp_path / "root", tmp_path / "alias"
    for directory in (root, alias):
        directory.mkdir()
        (directory / "target").mkdir()
    (alias / "input").mkdir()
    (root / "input").symlink_to(alias / "target", target_is_directory=True)
    original = Path.samefile
    monkeypatch.setattr(
        Path, "samefile", lambda p, q: {p, Path(q)} == {root, alias} or original(p, q)
    )
    monkeypatch.setattr(doctor.dc, "BOOLEY_DIR_TARGET", root.as_posix())
    project = SimpleNamespace(booley_dir=root, project_root=tmp_path)

    def hint(path):
        # Private seam: the mount alias is `Path.samefile` filesystem identity a whole run
        # cannot set up.
        return doctor._display_report_dir(project, path)

    assert hint(alias / "input") == ".booley/target (under the Project Root)"
    (root / "target").rmdir()
    (root / "real").mkdir()
    (root / "target").symlink_to(root / "real", target_is_directory=True)
    assert hint(alias / "input") == ".booley/real (under the Project Root)"


def _probed_binaries(project_root, target, flow_name) -> list[str]:
    """Return the binaries the Flow runtime owner probes for *target* (host path, all present)."""
    owner = importlib.import_module("booley.flows.runtime_reality")
    probed: list[list[str]] = []

    def sandbox_command(argv):
        probed.append(argv)
        return ["true"]

    request = owner.FlowRuntimeInspectionRequest(
        flow_name=flow_name,
        targets=(target,),
        project_root=project_root,
        runtime_available=True,
        inside_sandbox=False,
        sandbox_command=sandbox_command,
    )
    owner.inspect_flow_runtime_reality(request)
    return [argv[-1].removeprefix("command -v ") for argv in probed]


@pytest.mark.parametrize(
    ("config", "item", "fix", "severity"),
    [
        ("[notifications]\nenabled = true\n", "[notifications]", "delete this table", "WARN"),
        ("[mcp_tools.reviewer]\nenabled = false\n", "[mcp_tools]", "[specialists.*]", "FAIL"),
        (
            "[mcp_tools.submit_run_report]\nenabled = false\n",
            "[mcp_tools]",
            "remove protocol utility settings",
            "FAIL",
        ),
        (
            "[mcp_tools.project_check]\nenabled = false\n",
            "[mcp_tools]",
            "filename with '_'",
            "FAIL",
        ),
        ('[tools]\nbuiltin = ["sim"]\n', "[tools]", "[flows.*]", "FAIL"),
        ('[tools]\ncustom = ["reviewer"]\n', "[tools]", "[specialists.*]", "FAIL"),
        ('[flows.sim]\ntarget = "sim_core"\n', ".target", "explicit target", "FAIL"),
        (
            '[flows.sim]\ndefault_target = "sim_core"\n',
            ".default_target",
            "explicit target",
            "FAIL",
        ),
        (
            '[flows.sim]\ncalibration_target = "sim_core"\n',
            ".calibration_target",
            "doctor",
            "FAIL",
        ),
        (
            '[flows.sim.selftest]\ngood = "sim_core"\n',
            "[flows.sim.selftest]",
            "delete the table",
            "FAIL",
        ),
        ('[feedback]\nmode = "off"\n', "[feedback].mode", "delete [feedback].mode", "FAIL"),
        ('[interactive]\napp = "codex"\n', "[interactive].app", "[agent].default", "FAIL"),
    ],
)
def test_doctor_renders_removed_documentation_item_with_fix(
    tmp_path, monkeypatch, capsys, config, item, fix, severity
):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    (booley_dir / "booley.toml").write_text(
        '[project]\nname = "unit"\n[agent]\nauth = "subscription"\n' + config
    )

    result = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = colors.strip_ansi(capsys.readouterr().out)
    assert result == (0 if severity == "WARN" else 1)
    glyph = {"WARN": "!", "FAIL": "✗"}[severity]
    assert any(line.startswith(f"  {glyph}  ") and item in line for line in output.splitlines())
    assert fix in output


def test_doctor_warns_on_unused_sources_and_preflight_checks(tmp_path, monkeypatch, capsys):
    """Doctor renders WARNs for the retired `[sources]` table and `preflight_checks`."""
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    toml_path = booley_dir / "booley.toml"
    text = toml_path.read_text(encoding="utf-8")
    assert 'name = "unit"\n' in text
    text = text.replace('name = "unit"\n', 'name = "unit"\npreflight_checks = ["lint"]\n', 1)
    toml_path.write_text(text + '\n[sources.testbench]\nsource_dirs = ["tb"]\n', encoding="utf-8")

    result = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = colors.strip_ansi(capsys.readouterr().out)
    warn_lines = [line for line in output.splitlines() if line.startswith("  !  ")]
    assert result == 0
    assert any("[sources]" in line and ".core" in line for line in warn_lines)
    assert any("[project].preflight_checks" in line for line in warn_lines)


@pytest.mark.parametrize("key", ["idle_timeout_seconds", "max_sessions", "egress_allowlist"])
@pytest.mark.parametrize("table", ["interactive", "sandbox"])
def test_doctor_renders_each_project_policy_migration(tmp_path, monkeypatch, capsys, key, table):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    value = '["example.com"]' if key == "egress_allowlist" else "600"
    (booley_dir / "booley.toml").write_text(f"[{table}]\n{key} = {value}\n")

    assert doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path) == 1

    output = capsys.readouterr().out
    assert f"[{table}]" in output
    assert key in output
    assert "[sandbox]" in output
    assert "config.toml" in output


@pytest.mark.parametrize(
    ("key", "value", "replacement"),
    [
        ("timing_engine", "openroad", "synth_mode = physical or logical"),
        ("yosys", {}, "advanced_settings_yosys"),
        ("openroad", {}, "advanced_settings_openroad"),
    ],
)
@pytest.mark.parametrize("marked", [True, False])
def test_doctor_renders_synthesis_target_migration(
    tmp_path, monkeypatch, capsys, key, value, replacement, marked
):
    import yaml

    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    core = tmp_path / "unit.core"
    document = yaml.safe_load(core.read_text().removeprefix("CAPI=2:\n"))
    document["targets"]["synth_fast"]["flow_options"][key] = value
    if not marked:
        document["targets"]["synth_fast"]["flow_options"].pop("booley")
    core.write_text("CAPI=2:\n" + yaml.safe_dump(document))

    doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = capsys.readouterr().out
    assert f"flow_options.{key} is retired" in output
    assert replacement in output


def test_project_checks_run_custom_endpoint_checks_and_no_criteria_check(tmp_path, monkeypatch):
    project = tmp_path / ".booley"
    (project / "mcp_tools").mkdir(parents=True)
    (project / "booley.toml").write_bytes(b"[stealth]\nenabled = false\n")
    (project / "criteria.toml").write_bytes(b'[lint_clean]\ndescription = "x"\n')
    (project / "mcp_tools" / "lint.py").write_bytes(
        b"class Lint(McpTool):\n    name = 'lint'\n    description = 'x'\n"
    )
    for name in (
        "_check_ticket_leftovers",
        "_check_handle_map_size",
        "_check_project_gitignore",
        "_check_project_trackability",
        "_check_git_state",
        "_check_repo_footprint",
        "_check_disabled_endpoints",
        "_check_agent_backend_health",
    ):
        monkeypatch.setattr(doctor, name, lambda *args, **kwargs: None)
    reporter = doctor._Reporter.create()
    doctor._run_project_checks(doctor.ProjectAudit(tmp_path, project, {}, None), reporter)
    findings = reporter.findings or []
    assert [f for f in findings if f.severity == "fail"] == []
    skipped = [f for f in findings if f.severity == "warn"]
    assert [(f.check_id, f.subject) for f in skipped] == [
        ("project.custom-endpoint-skipped", "lint.py")
    ]
    assert not any(re.search(r"(?i)criteri", f"{f.message} {f.fix}") for f in findings)


def test_missing_pinned_credential_fails_even_without_cli(tmp_path, monkeypatch):
    from booley.runtime import auth_token

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # Private: a host with no Agent CLI installed is host state the whole-run fixture does not set (not yet moved onto the public run).
    monkeypatch.setattr(doctor, "_agent_app_installed", lambda _: False)
    for key in auth_token.CREDENTIAL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    audit = doctor.ProjectAudit(
        project_root=tmp_path,
        booley_dir=tmp_path,
        booley_toml={"agent": {"provider": "claude", "auth": "api_key"}},
        first_target="",
    )
    # Private: a Project with no installed Agent CLI is not reachable through a whole run (see the stub above).
    reporter = doctor._Reporter.create()
    doctor._run_agent_credential_checks(audit, reporter)
    assert any(
        f.severity.value == "fail" and f.check_id == "agent.credential-missing"
        for f in reporter.findings
    )


def test_seed_mount_policy_uses_same_project_policy_as_credential_report(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "subscription")
    audit = doctor.ProjectAudit(
        project_root=tmp_path,
        booley_dir=tmp_path / ".booley",
        booley_toml={"agent": {"provider": "claude", "auth": "api_key"}},
        first_target="",
    )
    # Private: pins the audited policy name the credential report renders; not yet moved onto the public run.
    assert doctor._agent_audit(audit, None).policy("claude") == "api_key"


def _seed_mount_policy_project(tmp_path, monkeypatch, app, policy):
    from booley.runtime import auth_token

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in auth_token.CREDENTIAL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    data = tmp_path / "project" / ".booley"
    data.mkdir(parents=True)
    (data / "booley.toml").write_text(f'[agent]\nprovider="{app}"\nauth="{policy}"\n')
    auth_token.store_token("SYNTHETIC", app)
    return data


def test_seed_mount_policy_unavailable_fails_without_comparing(tmp_path, monkeypatch):
    for damage in (
        "invalid-policy",
        "ambiguous-seed",
        "directory-conflict",
        "non-default-ambiguous-seed",
    ):
        (tmp_path / damage).mkdir()
        _assert_unavailable_policy_fails_without_comparing(tmp_path / damage, monkeypatch, damage)


def _assert_unavailable_policy_fails_without_comparing(tmp_path, monkeypatch, damage):
    from booley.runtime import inspection

    data = _seed_mount_policy_project(tmp_path, monkeypatch, "claude", "subscription")
    if damage == "non-default-ambiguous-seed":
        (data / "booley.toml").write_text(
            '[agent]\nclis = ["claude", "codex"]\ndefault = "codex"\nauth = "subscription"\n'
        )
    spec = build_spec("codex" if damage == "non-default-ambiguous-seed" else "claude")
    dc.write_devcontainer(data.parent, spec)
    if damage == "invalid-policy":
        (data / "booley.toml").write_text('[agent]\nauth="invalid"\n')
    elif damage in ("ambiguous-seed", "non-default-ambiguous-seed"):
        (Path.home() / ".claude.json").write_text('{"primaryApiKey":"SYNTHETIC_SECRET"}')
    else:
        data.rename(data.with_name("moved"))
        data.write_text("directory conflict")
    report = inspection.Findings()
    built = []
    # Private: the public `inspect_runtime` report also carries issuance findings that FAIL for these unsigned specs; `_check_devcontainer_spec` is the only seam that returns the seed-mount policy alone.
    monkeypatch.setattr(inspection, "_image_current", lambda *args: True)
    inspection._check_devcontainer_spec(
        inspection.RuntimeInspectionRequest(
            data.parent,
            "test-image",
            None,
            expected_spec=lambda image: built.append(image) or spec,
        ),
        report,
    )
    findings = report.report().findings
    assert any(
        f.severity.value == "fail" and "expectation is unavailable" in f.message for f in findings
    )
    assert built == []
    assert not any(f.severity.value == "pass" for f in findings)
    assert "SYNTHETIC_SECRET" not in str(findings)


@pytest.mark.parametrize("app", ["none", "unknown"])
def test_seed_mount_policy_no_app_does_not_inspect_invalid_policy(tmp_path, monkeypatch, app):
    from booley.runtime import inspection

    data = _seed_mount_policy_project(tmp_path, monkeypatch, "claude", "invalid")
    spec = {"remoteEnv": {"BOOLEY_AGENT_APP": app}}
    dc.write_devcontainer(data.parent, spec)
    request = inspection.RuntimeInspectionRequest(
        data.parent, "test-image", None, expected_spec=lambda _image: spec
    )
    report = inspection.Findings()
    # Private: the invalid-policy spec is a devcontainer state `booley init` never writes, so only this check can be shown not to inspect it.
    inspection._check_devcontainer_spec(request, report)
    assert {f.severity.value for f in report.report().findings} == {"pass"}


@pytest.mark.parametrize("policy", ["subscription", "api_key"])
def test_seed_mount_policy_state_partitions_belong_to_same_project(tmp_path, monkeypatch, policy):
    from booley.runtime import inspection

    own = dc.state_volume_name("claude", dc.canonical_project_id(tmp_path), policy)
    foreign = "booley-claude-state-foreign-project"
    monkeypatch.setattr(inspection.idk, "state_volumes", lambda: [own, foreign])
    monkeypatch.setattr(inspection.idk, "issued_image_tags", lambda: [])
    report = inspection.inspect_retained_resources(tmp_path, "docker")
    assert any(f.severity.value == "pass" and own in f.message for f in report.findings)
    assert any(
        "1 interactive state volume(s) from other projects" in f.message for f in report.findings
    )
    assert all(own not in detail.message for detail in report.details)


@pytest.mark.parametrize("tagged", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_inferred_cpp_harness_does_not_mask_explicit_tb_tag_audit(
    tmp_path, monkeypatch, tagged, legacy
):
    from tests.test_target_catalog import _category_sim_core

    _core_project(tmp_path, monkeypatch, {})
    _category_sim_core(tmp_path)
    core = tmp_path / "design.core"
    text = core.read_text().replace(
        "  broken_synth:\n    default_tool: yosys\n    filesets: [undefined]\n", ""
    )
    if legacy:
        text = text.replace(
            "    flow: sim\n    flow_options: {tool: verilator}", "    default_tool: verilator"
        )
    if not tagged:
        text = text.replace(", tags: [tb]", "")
    core.write_text(text)

    result = run_doctor(tmp_path)

    harness = _warnings(result, "sim.harness-untagged")
    assert any(
        "shared/main.cpp" in message and "tags:[tb]" in message for message in harness.values()
    )
    assert bool(_rows(result, Severity.FAIL, "none tagged tags:[tb]")) == (
        not tagged and not legacy
    )


def test_conditional_dependency_tb_tags_do_not_report_untagged_harness(tmp_path, monkeypatch):
    _core_project(
        tmp_path,
        monkeypatch,
        {
            "dut.sv": "module dut; endmodule\n",
            "tb.sv": 'module tb; initial $display("PASS"); endmodule\n',
            "design.core": "CAPI=2:\nname: ::conditional_top:0\nfilesets:\n"
            '  rtl: {files: [dut.sv], file_type: systemVerilogSource, depend: ["::conditional_tb:0"]}\n'
            "targets:\n  sim:\n    flow: sim\n    flow_options: {tool: verilator}\n"
            "    filesets: [rtl]\n    toplevel: tb\n",
            "dependency.core": "CAPI=2:\nname: ::conditional_tb:0\nfilesets:\n"
            "  tb: {files: [tb.sv], file_type: systemVerilogSource, tags: [tb]}\n"
            'targets:\n  default: {filesets: ["tool_verilator ? (tb)"]}\n',
        },
    )

    result = run_doctor(tmp_path)

    assert not _rows(result, Severity.FAIL, "none tagged tags:[tb]")
    assert not _warnings(result, "sim.harness-untagged")


def test_untagged_cpp_warning_does_not_mislabel_hdl_from_another_target(tmp_path, monkeypatch):
    from tests.test_target_catalog import _category_sim_core

    _core_project(tmp_path, monkeypatch, {})
    _category_sim_core(tmp_path)
    core = tmp_path / "design.core"
    text = (
        core.read_text()
        .replace("  broken_synth:\n    default_tool: yosys\n    filesets: [undefined]\n", "")
        .replace("files: [shared/dut.sv]", "files: [shared/dut.sv, shared/tb.sv]")
    )
    text += (
        "  sim_other:\n    flow: sim\n    flow_options: {tool: verilator}\n"
        "    filesets: [rtl, harness]\n    toplevel: tb\n"
    )
    core.write_text(text)

    harness = _warnings(run_doctor(tmp_path), "sim.harness-untagged").values()

    assert any("shared/main.cpp" in message for message in harness)
    assert all("shared/tb.sv" not in message for message in harness)


def test_explicitly_tagged_cpp_harness_is_not_reported_untagged(tmp_path, monkeypatch):
    from tests.test_target_catalog import _category_sim_core

    _core_project(tmp_path, monkeypatch, {})
    _category_sim_core(tmp_path)
    (tmp_path / "shared" / "tagged.cpp").write_text("// source\n")
    core = tmp_path / "design.core"
    core.write_text(
        core.read_text()
        .replace(
            "  harness:\n",
            "  tagged: {files: [shared/tagged.cpp], file_type: cppSource, tags: [tb]}\n  harness:\n",
        )
        .replace("filesets: [rtl, tb, harness]", "filesets: [rtl, tb, harness, tagged]")
    )

    harness = _warnings(run_doctor(tmp_path), "sim.harness-untagged").values()

    assert any("shared/main.cpp" in message for message in harness)
    assert all("shared/tagged.cpp" not in message for message in harness)


def _copyto_project(tmp_path, monkeypatch, inputs, *, run_cwd):
    """`ip/input.core` copying each of *inputs* (destination -> source bytes) to its
    `copyto` destination, and a Verilator sim and lint Target that depend on it."""
    files = {}
    entries = ""
    for index, (destination, content) in enumerate(inputs.items()):
        files[f"ip/payload{index}.mem"] = content
        entries += f"      - payload{index}.mem: {{copyto: {destination}}}\n"
    files["ip/input.core"] = (
        "CAPI=2:\nname: acme:ip:input:1.0\nfilesets:\n"
        f"  data:\n    file_type: user\n    files:\n{entries}"
        "targets:\n  default:\n    filesets: [data]\n"
    )
    files["design.core"] = (
        "CAPI=2:\nname: acme:ip:design:1.0\nfilesets:\n  data:\n    depend: [acme:ip:input:1.0]\n"
        "targets:\n"
        + _target("sim_copy", filesets="data", top="dut")
        + _target("lint_copy", flow="lint", filesets="data", top="dut")
    )
    return _core_project(tmp_path, monkeypatch, files, sim=f'run_cwd = "{run_cwd}"\n')


def test_sim_copyto_collision_warns_with_paths_and_remedies(tmp_path, monkeypatch):
    # One input per destination: differing bytes, identical bytes, an absent file, a
    # parent that is a symlink or a file or missing, and an invalid `copyto`.
    _copyto_project(
        tmp_path,
        monkeypatch,
        dict.fromkeys(
            (
                "collide.mem",
                "same.mem",
                "absent.mem",
                "link/x.mem",
                "file/x.mem",
                "missing/x.mem",
                "../escape.mem",
            ),
            "source",
        ),
        run_cwd=".",
    )
    (tmp_path / "collide.mem").write_text("other", encoding="utf-8")
    (tmp_path / "same.mem").write_text("source", encoding="utf-8")
    (tmp_path / "file").write_text("obstruction", encoding="utf-8")
    (tmp_path / "elsewhere").mkdir()
    flagged = ["collide.mem", "file/x.mem"]
    try:  # A host that forbids symlinks (Windows without Developer Mode) loses only this row.
        (tmp_path / "link").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
        flagged.append("link/x.mem")
    except OSError:
        pass

    result = run_doctor(tmp_path)

    collisions = {
        f.subject: f for f in result.findings if f.check_id == "sim.runtime-input-collision"
    }
    assert collisions.keys() == {f"sim_copy:{tmp_path / name}" for name in flagged}
    finding = collisions[f"sim_copy:{tmp_path / 'collide.mem'}"]
    assert "lint_copy" not in finding.message
    assert str(tmp_path / "ip" / "payload0.mem") in finding.message
    assert str(tmp_path / "collide.mem") in finding.message
    assert all(word in finding.fix.lower() for word in ("rename", "remove", "run_cwd"))


@pytest.mark.parametrize(
    ("run_cwd", "setup", "collides"),
    [
        ("runs", "runs/runtime.mem", True),
        # `{{` is a literal brace in a run_cwd template.
        ("{{escaped}}", "{{escaped}}/runtime.mem", True),
        # A template resolves per run, so a lookalike at the literal path is no collision.
        ("runs/{attempt}", "runs/{attempt}/runtime.mem", False),
        ("{unknown_placeholder}", "runtime.mem", False),
        # The configured cwd is a file; the run-cwd check owns that failure.
        ("runtime.mem", "runtime.mem", False),
        (".", "runtime.mem", False),
    ],
    ids=["literal", "escaped", "template", "unknown-placeholder", "cwd-is-file", "unreadable"],
)
def test_sim_copyto_run_cwd_decides_the_collision(tmp_path, monkeypatch, run_cwd, setup, collides):
    _copyto_project(tmp_path, monkeypatch, {"runtime.mem": "source"}, run_cwd=run_cwd)
    destination = tmp_path / setup
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("collision", encoding="utf-8")
    if run_cwd == ".":

        def unreadable(*_args, **_kwargs):
            raise PermissionError("unreadable input")

        monkeypatch.setattr("booley.flows.sim.runtime_inputs.filecmp.cmp", unreadable)

    result = run_doctor(tmp_path)

    assert bool(_warnings(result, "sim.runtime-input-collision")) is collides


@pytest.mark.skipif(os.name == "nt", reason="directory symlink requires privilege")
def test_doctor_fails_on_booley_dir_pin_mismatch(tmp_path, monkeypatch, capsys):
    """An unpinned or retargeted linked entry is one FAIL; Doctor then stops."""
    from booley.core import booley_dir_pin
    from booley.harness import booley as cli

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    for name in ("ext-a", "ext-b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "booley.toml").write_text("[project]\n", encoding="utf-8")
    entry = project / ".booley"
    entry.symlink_to(tmp_path / "ext-a", target_is_directory=True)
    args = argparse.Namespace(verbose=False, deep=False)

    unpinned = doctor.run_doctor_result(args, project)
    booley_dir_pin.write_pin(project, booley_dir_pin.current_target(entry))
    entry.unlink()
    entry.symlink_to(tmp_path / "ext-b", target_is_directory=True)
    retargeted = doctor.run_doctor_result(args, project)

    for result, remedy in ((unpinned, "booley init"), (retargeted, "--repin-booley-dir")):
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.severity is Severity.FAIL
        assert finding.check_id == "project.booley-dir-pin"
        assert remedy in finding.fix
        assert finding.fix not in finding.message  # the remedy is reported once, as the fix
        assert result.exit_code == 1

    def run_cli(*argv: str, cwd: Path) -> tuple[int, str]:
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(sys, "argv", ["booley", *argv])
        capsys.readouterr()
        return cli.main(), capsys.readouterr().err

    # Doctor is let past the guard to report the FAIL; every other command is refused.
    assert run_cli("doctor", cwd=project)[0] == 1
    for command in (("session", "status"), ("targets",)):
        code, stderr = run_cli(*command, cwd=project)
        assert code == 2
        assert "ERROR:" in stderr and "--repin-booley-dir" in stderr

    # An ancestor's untrusted link is not this checkout's FAIL to report.
    inner = project / "inner"
    inner.mkdir()
    run_bounded_child(["git", "init", str(inner)], check=True)
    assert run_cli("doctor", cwd=inner)[0] == 2


class TestDoctorCommonStateFromWorktree:
    """Doctor's Common Booley State (probes, Dockerfile, Ticket Board) lives in main."""

    @pytest.fixture
    def worktree_project(self, tmp_path, monkeypatch):
        main, worktree = linked_worktree(tmp_path, monkeypatch, layout="snapshot", runtime="host")
        project = readiness.ProjectAudit(
            project_root=worktree,
            booley_dir=resolve_checkout_booley_dir(worktree),
            booley_toml={"sandbox": {"memory": "100g"}},
            first_target="",
        )
        return main / ".booley", project

    def test_project_dockerfile_is_read_from_main(self, worktree_project, monkeypatch):
        common, project = worktree_project
        _write_project_dockerfile(common.parent, mtime=2000.0)
        # Private: reaches the custom-image check from a linked worktree, a layout the whole-run fixture does not model (not yet moved onto the public run).
        monkeypatch.setattr(doctor, "_image_created_epoch", lambda _exe, _img: 1000.0)
        warns: list[str] = []

        doctor._check_custom_image_freshness(
            project, "docker", "custom", lambda _m: None, warns.append
        )

        assert warns and "predates" in warns[0]

    def test_ticket_board_leftovers_and_handle_maps_are_read_from_main(
        self, worktree_project, monkeypatch, capsys
    ):
        common, project = worktree_project
        (common / "tickets" / "board").mkdir(parents=True)
        (common / "tickets" / "board" / "T-1.json").write_text("{}", encoding="utf-8")
        stores: list[Path] = []
        real_store = doctor.handle_maps.HandleStore
        monkeypatch.setattr(
            doctor.handle_maps,
            "HandleStore",
            lambda directory, **kwargs: (
                stores.append(directory) or real_store(directory, **kwargs)
            ),
        )
        for name in (
            # Private: a linked-worktree Project needs the five Project checks below stubbed out; the whole run does not model that layout (not yet moved onto the public run).
            "_check_project_gitignore",
            "_check_project_trackability",
            "_check_git_state",
            "_check_repo_footprint",
            "_check_custom_endpoints",
        ):
            monkeypatch.setattr(doctor, name, lambda *_args, **_kwargs: None)
        reporter = doctor._Reporter.create()
        reporter.agent_check_enabled = lambda _name: False  # type: ignore[method-assign]

        doctor._run_project_checks(project, reporter)

        assert (
            f"leftover Ticket Board data in {common / 'tickets' / 'board'}"
            in capsys.readouterr().out
        )
        assert stores and set(stores) == {common}


# ---------------------------------------------------------------------------
# Relocated Booley Directory ([project].dir = "tools/booley", issue #2919)
# ---------------------------------------------------------------------------


def _relocated_project(tmp_path: Path, *, stealth: bool = False) -> tuple[Path, Path]:
    """Return ``(root, booley_dir)`` for a Git checkout whose Booley Directory is relocated."""

    root = tmp_path / "rtl"
    root.mkdir()
    _history_git(root, "init", "-q", "-b", "main")
    (root / "booley.toml").write_bytes(b'[project]\ndir = "tools/booley"\n')
    project = root / "tools" / "booley"
    project.mkdir(parents=True)
    flag = "true" if stealth else "false"
    (project / "booley.toml").write_bytes(f"[stealth]\nenabled = {flag}\n".encode())
    reset_cache()
    return root, project


def test_stealth_exclude_check_requires_relocated_entry(tmp_path):
    root, project = _relocated_project(tmp_path, stealth=True)
    exclude = root / ".git/info/exclude"
    exclude.write_bytes(b"/.devcontainer\n/.booley\n/tools/booley\n")
    request = doctor.inspection.InteractiveGitRequest(root, project)
    assert _findings(doctor.inspection.inspect_interactive_git(request), Severity.WARN) == []

    # Only the default spelling present: the relocated entry is reported missing.
    exclude.write_bytes(b"/.devcontainer\n/.booley\n")
    findings = _findings(doctor.inspection.inspect_interactive_git(request), Severity.WARN)
    assert [f.subject for f in findings] == ["tools/booley"]

    # The writers also hide `.booley`, so Doctor requires it beside the relocated entry.
    exclude.write_bytes(b"/.devcontainer\n/tools/booley\n")
    findings = _findings(doctor.inspection.inspect_interactive_git(request), Severity.WARN)
    assert [f.subject for f in findings] == [".booley"]


def test_sandbox_commands_pass_relocated_booley_dir(tmp_path, monkeypatch):
    from types import SimpleNamespace

    root, _project = _relocated_project(tmp_path)

    # In-Sandbox Doctor sees the relocated directory at /work/tools/booley.
    sandbox_dir = Path("/work/tools/booley")
    in_sandbox = SimpleNamespace(booley_dir=sandbox_dir, project_root=root)
    # `/work` exists only in the Sandbox, so stub the containment lookup. Resolve
    # like the real lookup does: on Windows a drive-less path gains a drive, and
    # the code compares against the resolved Booley Directory.
    monkeypatch.setattr(doctor, "contains", lambda path, booley_dir: Path(path).resolve())
    hint = doctor._display_report_dir(in_sandbox, sandbox_dir / "tmp" / "r")
    assert hint == "tools/booley/tmp/r (under the Project Root)"


# ---------------------------------------------------------------------------
# Doctor Flow check reports.
# ---------------------------------------------------------------------------


_FLOW_FAIL_ROWS = [
    "  ✗  sim dry-run [sim_fast] failed with exit 3",
    f"     → see {Path('<root>') / '.booley' / doctor.DOCTOR_TMP_DIR / 'flow-reports'}",
    "      %Error: undefined signal foo",
    "      sim: reason follows",
    "      … (11 more line(s); see the report dir)",
]
_LINT_ROWS = [
    "  ✓  lint executes in the Sandbox",
    "  ✓  lint: 'verilator' on the Sandbox PATH",
    "  ✓  lint dry-run [lint_fast] passed",
]
_LINT_VERBOSE_EXCERPT = ["      lint: ok line one", "      … (2 more line(s); see the report dir)"]


def test_invalid_endpoint_config_is_left_to_the_endpoint_check(tmp_path):
    """A disabled name no endpoint carries makes the registry raise: stay silent, never crash."""
    # An invalid Project never reaches this check in a run, so the guard needs a direct call.
    notes: list[str] = []
    audit = doctor.ProjectAudit(
        project_root=tmp_path,
        booley_dir=tmp_path / ".booley",
        booley_toml={"specialists": {"typo": {"enabled": False}}},
        first_target="",
    )

    doctor._check_disabled_endpoints(audit, notes.append)

    assert notes == []


@pytest.mark.parametrize(
    ("location", "where", "refresh"),
    [
        ("HOST", "booley init", ", or run booley init"),
        (
            "HAND_WRITTEN_SANDBOX",
            "booley init in this Sandbox",
            ", or run booley init in this Sandbox",
        ),
        ("ISSUED_SANDBOX", "booley init on the host", ""),
    ],
    ids=["host", "hand-written", "issued"],
)
def test_init_fix_hints_name_where_to_run_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str, where: str, refresh: str
) -> None:
    """Plain `booley init` fix hints follow the runtime location, in Doctor and readiness."""
    _core_project(
        tmp_path,
        monkeypatch,
        {".booley/.gitignore": None, ".booley/cores/hidden.core": "CAPI=2:\nname: ::hidden:0\n"},
        toml="\n[stealth]\nenabled = true\n",
    )
    place(monkeypatch, RuntimeLocation[location])
    monkeypatch.setenv("BOOLEY_EGRESS", "unproxied")

    result = run_doctor(tmp_path)

    missing = ".booley/.gitignore missing; transient Booley state may be tracked"
    assert _warnings(result, "project.gitignore") == {"None": missing}
    assert _fixes(result, missing) == [where]
    assert _warnings(result, "stealth.projections-not-current") == {
        "None": "stealth core projections are missing or stale: missing .booley-projected-hidden.core"
    }
    assert _fixes(result, "missing or stale") == [f"run any Booley Flow to refresh them{refresh}"]
    projection = "stealth core projections are not excluded from host git status"
    assert _fixes(result, projection) == [
        f"run {where} to add the generated projection pattern to .git/info/exclude"
    ]


_REFRESH = "run any Booley Flow to refresh them, or run booley init"
_STALE_ROW = (
    "warn",
    "stealth.projections-not-current",
    "stealth core projections are missing or stale: missing .booley-projected-b.core, "
    "stale .booley-projected-a.core",
    _REFRESH,
)
# #5290: no Flow can remove a copy whose authored core is gone, so its hint names the delete.
_ORPHAN_ROW = (
    "warn",
    "stealth.projections-not-current",
    "stealth core projections outlive their authored cores: .booley-projected-c.core",
    "delete .booley-projected-c.core from the Project Root",
)
_FOREIGN_ROW = (
    "fail",
    None,
    "foreign files block stealth core projections: .booley-projected-b.core",
    "move or delete each listed file; Booley writes its projection there on the next Flow run",
)


@pytest.mark.parametrize(
    ("case", "rows", "exit_code"),
    [
        ("drift", [_STALE_ROW, _ORPHAN_ROW], 0),
        (
            "waived",
            [
                ("waived", *row[1:3], "Accepted for the #5252 regression test.")
                for row in (_STALE_ROW, _ORPHAN_ROW)
            ],
            0,
        ),
        (
            "foreign",
            [
                (
                    "warn",
                    "stealth.projections-not-current",
                    "stealth core projections are missing or stale: stale .booley-projected-a.core",
                    _REFRESH,
                ),
                _FOREIGN_ROW,
            ],
            1,
        ),
        (
            "unrenderable",
            [
                (
                    "fail",
                    None,
                    "could not inspect stealth core projections: authoritative core lacks "
                    "leading CAPI=2: marker: {core}",
                    "fix the authored core, then run any Booley Flow to refresh the "
                    "projections, or run booley init",
                )
            ],
            1,
        ),
    ],
    ids=["drift", "waived", "foreign", "unrenderable"],
)
def test_stealth_projection_rows_split_refreshable_from_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    rows: list[tuple[str, str | None, str, str]],
    exit_code: int,
) -> None:
    """Missing or stale copies WARN (waivable); a foreign file or bad core still FAILs."""
    names = ("a", "b", "c")
    cores = {f".booley/cores/{n}.core": f"CAPI=2:\nname: ::{n}:0\n" for n in names}
    booley_dir = _core_project(tmp_path, monkeypatch, cores, toml="\n[stealth]\nenabled = true\n")
    core_projection.reconcile_projected_cores(tmp_path)
    git_init(tmp_path)
    exclude = tmp_path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(f"/{core_projection.PROJECTED_CORE_GLOB}\n", encoding="utf-8")
    body = "CAPI=2:\nname: ::a:1\n" if case != "unrenderable" else "name: ::a:1\n"
    (booley_dir / "cores" / "a.core").write_text(body, encoding="utf-8")
    projected_b = tmp_path / ".booley-projected-b.core"
    if case == "foreign":
        projected_b.write_text("CAPI=2:\nname: ::b:0\n", encoding="utf-8")
    elif case != "unrenderable":
        projected_b.unlink()
    if case in {"drift", "waived"}:
        (booley_dir / "cores" / "c.core").unlink()
    if case == "waived":
        (booley_dir / "doctor-waivers.toml").write_text(
            'version = 1\n[[waiver]]\ncheck = "stealth.projections-not-current"\n'
            'reason = "Accepted for the #5252 regression test."\npermanent = true\n',
            encoding="utf-8",
        )

    result = run_doctor(tmp_path)

    shown = [
        (f.severity.value, f.check_id, f.message, f.fix)
        for f in result.findings
        if "stealth core projections" in f.message
        and f.severity is not Severity.SKIP
        and "excluded" not in f.message
    ]
    assert shown == [
        (sev, cid, msg.replace("{core}", str(booley_dir / "cores" / "a.core")), fix)
        for sev, cid, msg, fix in rows
    ]
    assert result.exit_code == exit_code
    skip = "skipped - stealth core projections are not current"
    assert [m for m in finding_messages(result) if m.endswith(skip)] == [
        f"Flow dry-runs {skip}",
        f".core audit {skip}",
    ]


_FIXED_EVIDENCE_PATTERNS = (
    "/reviewer-evidence/",
    "/findings.jsonl",
    "/findings.jsonl.tmp",
    "/BOOLEY-FEEDBACK.md",
    "/setup-evidence/",
    "/PARITY-REPORT.md",
)


def test_project_gitignore_names_each_missing_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doctor names every missing pattern, the fixed evidence ones included, and never rewrites."""
    dropped = ("mcp-tool-reports/", "reviewer-state/", *_FIXED_EVIDENCE_PATTERNS)
    kept = [
        pattern
        for pattern in project_gitignore.PROJECT_GITIGNORE_PATTERNS
        if not pattern.startswith("tickets/") and pattern not in dropped
    ]
    original = "".join(f"{pattern}\n" for pattern in kept)
    booley_dir = _core_project(tmp_path, monkeypatch, {".booley/.gitignore": original})

    result = run_doctor(tmp_path)

    (message,) = _warnings(result, "project.gitignore").values()
    expected = project_gitignore.missing_gitignore_patterns(original)
    assert len(expected) == 13
    assert all(pattern in message for pattern in expected)
    assert _fixes(result, "pattern(s)") == ["booley init"]
    assert (booley_dir / ".gitignore").read_text(encoding="utf-8") == original


def _respelled_policy(spelling: dict[str, str]) -> str:
    patterns = project_gitignore.PROJECT_GITIGNORE_PATTERNS
    return "".join(f"{spelling.get(pattern, pattern)}\n" for pattern in patterns)


@pytest.mark.parametrize(
    ("spelling", "missing"),
    [
        pytest.param(
            {"tickets/board/": "/tickets/board/", "tickets/state/": "/tickets/state/"},
            [],
            id="anchored-nested",
        ),
        pytest.param({"worktrees/": "/worktrees/"}, ["worktrees/"], id="anchored-top-level"),
        pytest.param(
            {p: p.lstrip("/") for p in _FIXED_EVIDENCE_PATTERNS},
            list(_FIXED_EVIDENCE_PATTERNS),
            id="unanchored-evidence",
        ),
    ],
)
def test_missing_gitignore_patterns_compare_anchored_spellings(
    spelling: dict[str, str], missing: list[str]
) -> None:
    # `worktrees/` matches at any depth; `/worktrees/` only at the top; a slash inside makes both
    # spellings equivalent.
    content = _respelled_policy(spelling)

    assert sorted(project_gitignore.missing_gitignore_patterns(content)) == sorted(missing)


_VERIBLE_CONTAINER_ROW = "verible-verilog-lint (a .core lint Target selects verible)"


@pytest.mark.parametrize(
    "configs",
    ['[fast]\ndefines = []\ntb_top = "tb"\ntests = ["smoke"]\n', "[broken"],
    ids=["valid", "malformed"],
)
def test_doctor_warns_once_that_configs_toml_is_ignored(tmp_path, monkeypatch, configs):
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    before = run_doctor(tmp_path)
    (booley_dir / "configs.toml").write_text(configs, encoding="utf-8")

    after = run_doctor(tmp_path)

    def other_rows(result):
        return [
            (f.severity, f.message) for f in result.findings if "configs.toml" not in f.message
        ]

    assert [f for f in before.findings if "configs.toml" in f.message] == []
    mentions = [f for f in after.findings if "configs.toml" in f.message]
    assert [(f.severity, f.check_id) for f in mentions] == [
        (Severity.WARN, "config.configs-toml-ignored")
    ]
    assert other_rows(after) == other_rows(before)


def test_doctor_targets_come_from_core_metadata_and_keep_all(tmp_path, monkeypatch):
    """Doctor runs every Target that opts into a Flow; it never picks a first one."""
    core = tmp_path / "unit.core"
    design = _core(
        "design",
        _target("sim_full", options="tool: icarus, iverilog_options: [-g2012]", doctor=True),
    )
    _core_project(tmp_path, monkeypatch, {"design.core": design, "demo.core": _BAD_DEPEND_CORE})
    # `lint_fast` loses its marker: no Doctor Target is left for the lint Flow.
    unmarked = core.read_text(encoding="utf-8").replace(
        "{tool: verilator, booley: {doctor: [lint]}}", "{tool: verilator}"
    )
    core.write_text(unmarked, encoding="utf-8")

    result = run_doctor(tmp_path)

    messages = finding_messages(result)
    assert "sim dry-run [sim_fast] passed" in messages
    assert "sim dry-run [sim_full] passed" in messages
    # No fallback to a first Target.
    assert not [m for m in messages if m.startswith("lint dry-run")]
    (missing,) = _rows(result, Severity.FAIL, "lint has no Doctor Target")
    assert missing.startswith("lint has no Doctor Target; available Targets: ")
    assert "lint_fast" in missing
    (fix,) = _fixes(result, missing)
    assert "flow_options.booley.doctor" in fix
    # FuseSoC rejects demo.core, but an unrelated rejected core must not turn the FAIL into a SKIP.
    assert _rows(result, Severity.FAIL, ".core schema demo.core")
    assert _rows(result, Severity.SKIP, "lint has no Doctor Target") == []


def test_every_known_booley_toml_table_is_silent():
    """A live table missing from the known set would warn (F-17).

    The set is derived: a second literal list here would drift exactly like the one that lost
    `[stealth]`.
    """
    audit = project_schema.audit_known_tables(
        {table: {} for table in project_schema.KNOWN_BOOLEY_TOML_TABLES}
    )

    assert audit.findings == ()


def test_misplaced_booley_key_yields_one_fail_and_skips_sibling_dry_runs(tmp_path, monkeypatch):
    """A `booley:` mapping beside `flow_options` makes FuseSoC ignore the whole core.

    The run reports that once, names core/Target/key and the fix, and does not dry-run (and
    FAIL) every sibling Target of the ignored core.
    """
    _core_project(
        tmp_path,
        monkeypatch,
        {
            "unit.core": None,
            "rtl/top.sv": "module top; endmodule\n",
            "picorv32_impl.core": _MISPLACED_BOOLEY_CORE,
        },
        sim="enabled = false\n",
        toml="\n[flows.fpga]\n",
    )

    result = run_doctor(tmp_path)

    fails = [f for f in result.findings if f.severity is Severity.FAIL]
    (rejection,) = [f for f in fails if "picorv32_impl.core" in f.message]
    assert "fpga_core" in rejection.message
    assert "booley" in rejection.message
    assert "targets.fpga_core.flow_options" in rejection.fix
    # A dry-run through the fake would report `passed` rows for these Targets.
    skipped = "skipped - FuseSoC rejects picorv32_impl.core; see the .core schema FAIL"
    assert [m for m in finding_messages(result) if "dry-run [" in m] == [
        f"lint dry-run [lint_core] {skipped} under FuseSoC .core checks",
        f"synth dry-run [synth_core] {skipped} under FuseSoC .core checks",
    ]
    assert _rows(
        result, Severity.SKIP, "fpga has no Doctor Target - FuseSoC rejects picorv32_impl.core"
    )


def test_repo_footprint_scaffolding_warns(tmp_path, monkeypatch):
    # A Booley-branded note committed at depth must be caught: the glob crosses `/`.
    _history_git(tmp_path, "init", "-q")
    (tmp_path / "firmware").mkdir()
    (tmp_path / "firmware" / "README.booley.md").write_text("notes\n", encoding="utf-8")
    _history_git(tmp_path, "add", "firmware")
    _core_project(tmp_path, monkeypatch, {})
    _forward_to_git(monkeypatch, "*.booley.md")

    result = run_doctor(tmp_path)

    (message,) = _warnings(result, "project.scaffolding-tracked").values()
    assert "firmware/README.booley.md" in message


_FPGA_CORE_TEXT = (
    "  {name}:\n    flow: generic\n"
    "    flow_options: {{tool: verilator, part: xc7a35tcsg324-1{marker}}}\n"
    "    filesets: [rtl]\n    toplevel: dut\n"
)
_FPGA_NOT_APPLICABLE = "fpga not applicable - no [flows.fpga] table or marked Doctor Target"
_NO_FPGA_ENDPOINT = "MCP missing required endpoint(s): fpga"


@pytest.mark.parametrize(
    ("target", "toml", "severity", "row", "mcp_fail", "execution"),
    [
        pytest.param(
            "marked",
            "",
            Severity.FAIL,
            "fpga: Vivado batch startup",
            True,
            ["fpga dry-run [fpga_board] passed", "fpga: Vivado batch startup"],
            id="marked-no-table",
        ),
        pytest.param(
            "unmarked",
            "\n[flows.fpga]\n",
            Severity.FAIL,
            "fpga has no Doctor Target; available Targets: fpga_board",
            True,
            [],
            id="unmarked-table",
        ),
        pytest.param(
            "marked",
            "\n[flows.fpga]\nenabled = false\n",
            Severity.SKIP,
            "fpga disabled in booley.toml",
            False,
            [],
            id="marked-disabled",
        ),
        pytest.param(
            "unmarked", "", Severity.SKIP, _FPGA_NOT_APPLICABLE, False, [], id="unmarked-no-table"
        ),
        pytest.param(
            "synth_board",
            "",
            Severity.FAIL,
            "Target 'synth_board' selects incompatible Doctor Flow 'fpga' "
            "(CAPI2 flow=generic, EDA tool=verilator)",
            True,
            [],
            id="incompatible-axis",
        ),
    ],
)
def test_fpga_flow_audit_follows_table_and_marker(
    tmp_path, monkeypatch, target, toml, severity, row, mcp_fail, execution
):
    name = "synth_board" if target == "synth_board" else "fpga_board"
    marker = "" if target == "unmarked" else ", booley: {doctor: [fpga]}"
    core = _core(
        "design",
        _FPGA_CORE_TEXT.format(name=name, marker=marker),
        "  rtl: {files: [rtl/dut.sv]}\n",
    )
    _core_project(tmp_path, monkeypatch, {"design.core": core}, toml=toml)

    result = run_doctor(tmp_path)

    assert _rows(result, severity, row)
    # The probe payload of the fixture lacks the `fpga` endpoint.
    assert bool(_rows(result, Severity.FAIL, _NO_FPGA_ENDPOINT)) is mcp_fail
    # A disabled, not applicable or Target-less FPGA axis runs nothing; the Vivado startup owner
    # (never the per-binary probe) answers for a marked one.
    executed = sorted(
        m for m in finding_messages(result) if m.startswith(("fpga dry-run", "fpga: "))
    )
    assert len(executed) == len(execution)
    assert all(m.startswith(prefix) for m, prefix in zip(executed, sorted(execution), strict=True))


@pytest.mark.parametrize(
    ("tree", "row"),
    [
        pytest.param(
            "large", "large design (Doctor Target filesets: ~2 HDL files", id="large-target"
        ),
        pytest.param(
            "unrelated",
            "design size (Doctor Target filesets: ~2 HDL files / ~2 LOC) within --deep smoke budgets",
            id="unrelated-tree",
        ),
    ],
)
def test_design_size_counts_only_doctor_target_filesets(tmp_path, monkeypatch, tree, row):
    """A large Doctor Target is a NOTE, never a WARN; a large unrelated tree is not counted."""
    files: dict[str, str | None]
    if tree == "large":
        files = {
            "rtl/dut.sv": "module dut; endmodule\n" + "wire w;\n" * design_size.LARGE_DESIGN_LOC
        }
    else:
        files = {
            f"unrelated/large_{i}.sv": "module large; endmodule\n"
            for i in range(design_size.LARGE_DESIGN_FILES + 5)
        }
    _core_project(tmp_path, monkeypatch, files)

    result = run_doctor(tmp_path)

    (size_row,) = [
        m for m in finding_messages(result) if "design size" in m or "large design" in m
    ]
    assert size_row.startswith(row)
    assert (tree == "large") is bool(_rows(result, Severity.NOTE, "large design"))
    assert not _rows(result, Severity.WARN, "large design")


@pytest.mark.parametrize(
    ("snippet", "present", "absent"),
    [
        pytest.param(
            b'\n[jobs]\nmax_heavy = 2\nheavy_memory = "8g"\n',
            (
                "booley.toml sets retired [jobs] key max_heavy (line 17)",
                "delete [jobs].max_heavy (line 17); memory admission replaced it",
                "retired [jobs] key heavy_memory (line 18)",
                # Doctor did not stop at the config: later checks still ran.
                "project guidance file missing",
            ),
            (),
            id="max-heavy",
        ),
        pytest.param(
            b"\n[jobs]\nqueue_max = 8\n",
            (
                "booley.toml sets retired [jobs] key queue_max (line 17)",
                "delete [jobs].queue_max (line 17); the Job queue has no limit:"
                " a Job waits until it runs or is cancelled",
            ),
            ("memory admission replaced it",),
            id="queue-max",
        ),
        pytest.param(
            b"\n[jobs]\nmax_light = 2\n",
            (
                "booley.toml sets retired [jobs] key max_light (line 17)",
                "delete [jobs].max_light (line 17);"
                " Specialists and the Mutation Flow are admitted by memory",
            ),
            ("memory admission replaced it",),
            id="max-light",
        ),
        pytest.param(
            b'\n[jobs]\nmemory_budget = "24g"\n',
            (
                "booley.toml sets retired [jobs] key memory_budget (line 17)",
                "delete [jobs].memory_budget (line 17);",
                "set [sandbox].memory instead",
            ),
            ("memory admission replaced it",),
            id="memory-budget",
        ),
    ],
)
def test_retired_jobs_key_fails_with_its_own_fix(
    tmp_path, monkeypatch, capsys, snippet, present, absent
):
    """ADR 0087: Doctor reports a retired ``[jobs]`` key as a FAIL and keeps checking."""
    booley_dir = write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    toml = booley_dir / "booley.toml"
    toml.write_bytes(toml.read_bytes() + snippet)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = colors.strip_ansi(capsys.readouterr().out)
    assert rc != 0
    assert all(text in output for text in present)
    assert not any(text in output for text in absent)


_LINT_WARN = "RESULT: WARN (58 warnings)\n"
_PARSE_ERROR = (
    "INFO: setting up\n"
    "WARNING: Parse error. Ignoring file /work/picorv32.core:\n"
    "data.filesets.tb.depend must be array\n"
)


@pytest.mark.parametrize(
    ("flow", "returncode", "stdout", "stderr", "severity", "row"),
    [
        pytest.param(
            "lint",
            1,
            _LINT_WARN,
            "",
            Severity.PASS,
            "lint deep check [lint_fast] ran end-to-end; the design has lint findings "
            "(exit 1 = WARN), which belong to `booley lint`",
            id="lint-findings",
        ),
        pytest.param(
            "lint",
            1,
            "RESULT: FAIL — lint_eth: %Error: undefined variable\n",
            "",
            Severity.FAIL,
            "lint deep check [lint_fast] failed with exit 1",
            id="lint-fatal-report",
        ),
        pytest.param(
            "lint",
            1,
            "",
            "%Error: failed to elaborate\n",
            Severity.FAIL,
            "lint deep check [lint_fast] failed with exit 1",
            id="lint-no-verdict",
        ),
        pytest.param(
            "lint",
            2,
            _LINT_WARN,
            _PARSE_ERROR,
            Severity.FAIL,
            "lint deep check [lint_fast] failed with exit 2",
            id="lint-exit-2",
        ),
        pytest.param(
            "sim",
            1,
            _LINT_WARN,
            "",
            Severity.FAIL,
            "sim deep check [sim_fast] failed with exit 1",
            id="sim-exit-1",
        ),
    ],
)
def test_deep_lint_findings_are_not_tool_failure(
    tmp_path, monkeypatch, capsys, flow, returncode, stdout, stderr, severity, row
):
    """B3 (Taxi F-31): lint exits 1 on a WARN verdict, which --deep must not read as breakage.

    Only lint's exit 1 with a WARN verdict is a findings run: a fatal report, a missing verdict,
    exit 2 and every other Flow's exit 1 still FAIL, and the FAIL prints the last line of the
    tool's output (a first-line-only excerpt dropped the reason after `Ignoring file X:`).
    """
    _core_project(tmp_path, monkeypatch, {})
    _answer_command(
        monkeypatch,
        lambda cmd: f"booley.flows.{flow}" in cmd and "--dry-run" not in cmd,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )

    result = run_doctor(tmp_path, deep=True)

    assert row in _rows(result, severity, row)
    if severity is Severity.FAIL:
        assert (stderr or stdout).splitlines()[-1] in capsys.readouterr().out


_LINT_DECLARATIONS = {
    "lint": "flow: lint\n    flow_options: {{tool: {tool}, {marker}}}",
    "sim": "flow: sim\n    flow_options: {{tool: {tool}, {marker}}}",
    "default_tool": "default_tool: {tool}\n    flow_options: {{{marker}}}",
    "mixed": "flow: lint\n    default_tool: {tool}\n    flow_options: {{{marker}}}",
}


def _lint_style_project(tmp_path, monkeypatch, eda_tool, declaration, marker):
    """A Project whose `lint.core` holds the lint Target `lint_style`; *marker* may be empty."""
    text = _LINT_DECLARATIONS[declaration].format(tool=eda_tool, marker=marker)
    core = (
        "CAPI=2:\nname: acme:ip:lint:1.0\nfilesets:\n  rtl: {files: [rtl/dut.sv]}\ntargets:\n"
        f"  lint_style:\n    {text}\n    filesets: [rtl]\n    toplevel: dut\n"
    )
    _core_project(tmp_path, monkeypatch, {"lint.core": core})


_LINT_POLICY_ROWS = [
    ("verible", "lint", True, "verible-verilog-lint", True),
    ("veriblelint", "lint", True, "verible-verilog-lint", True),
    ("verilator", "lint", True, "verilator", False),
    ("not-verible", "lint", False, None, False),
    ("slang", "lint", False, None, False),
    ("veriblelint", "default_tool", False, None, False),
    ("verible", "default_tool", True, "verible-verilog-lint", True),
    ("veriblelint", "sim", False, None, False),
    ("verilator", "mixed", False, "verilator", False),
    ("verible", "mixed", False, "verible-verilog-lint", False),
    ("veriblelint", "mixed", False, "verible-verilog-lint", False),
]


@pytest.mark.parametrize(
    ("eda_tool", "declaration", "compatible", "binary", "verible"),
    _LINT_POLICY_ROWS,
    ids=[f"{row[0]}-{row[1]}" for row in _LINT_POLICY_ROWS],
)
def test_authored_lint_doctor_eda_tool_policy(
    tmp_path, monkeypatch, eda_tool, declaration, compatible, binary, verible
):
    """A marked lint Target selects Verible only through its Flow's EDA tool (f27325c2a)."""
    _lint_style_project(tmp_path, monkeypatch, eda_tool, declaration, "booley: {doctor: [lint]}")

    result = run_doctor(tmp_path)

    incompatible = "Target 'lint_style' selects incompatible Doctor Flow 'lint'"
    assert (not _rows(result, Severity.FAIL, incompatible)) is compatible
    assert bool(_rows(result, Severity.PASS, _VERIBLE_CONTAINER_ROW)) is verible
    if binary is not None:
        # The probe owner answers for the Project Root; the Doctor run's fake is not its Sandbox.
        monkeypatch.setattr(doctor.subprocess, "run", REAL_RUN)
        assert _probed_binaries(tmp_path, "lint_style", "lint") == [binary]


def test_unmarked_verible_lint_target_still_gets_the_container_row(tmp_path, monkeypatch):
    """The Verible gate reads every lint Target, not only the ones marked for Doctor."""
    _lint_style_project(tmp_path, monkeypatch, "verible", "lint", "")

    assert _rows(run_doctor(tmp_path), Severity.PASS, _VERIBLE_CONTAINER_ROW)


def _line_endings_repo(
    root: Path, autocrlf: str, files: tuple[tuple[str, bytes], ...] = ()
) -> None:
    """A Git repository at *root* with `core.autocrlf` set and *files* committed."""
    run_bounded_child(["git", "init", "-q", str(root)], check=True)
    run_bounded_child(["git", "-C", str(root), "config", "core.autocrlf", autocrlf], check=True)
    for name, data in files:
        (root / name).write_bytes(data)
        run_bounded_child(["git", "-C", str(root), "add", "-f", name], check=True)
    if files:
        identity = ["-c", "user.name=t", "-c", "user.email=t@t"]
        run_bounded_child(["git", "-C", str(root), *identity, "commit", "-qm", "c"], check=True)


def _recheckout(root: Path, name: str) -> None:
    """Delete *name* and check it out again under the repository's current line-ending rules."""
    (root / name).unlink()
    run_bounded_child(["git", "-C", str(root), "checkout", "--", name], check=True)


_LF_FILE = (("a.v", b"module a;\nendmodule\n"),)


@pytest.mark.parametrize(
    ("files", "recheckout"),
    [
        pytest.param(_LF_FILE, None, id="lf-tree"),
        pytest.param(
            ((".gitattributes", b"*.txt text eol=crlf\n"), ("intentional.txt", b"alpha\nbeta\n")),
            "intentional.txt",
            id="explicit-crlf-policy",
        ),
        # A `*.bat -text` payload is stored and checked out CRLF on purpose: byte-identical to
        # the index, so clean in the container too (the false positive that hit taxi).
        pytest.param(
            ((".gitattributes", b"* text eol=lf\n*.bat -text\n"), ("run.bat", b"@echo off\r\n")),
            None,
            id="minus-text-payload",
        ),
    ],
)
def test_container_safe_checkout_passes(tmp_path: Path, files, recheckout):
    _line_endings_repo(tmp_path, "false", files)
    if recheckout:
        _recheckout(tmp_path, recheckout)

    report = _inspect_line_endings(tmp_path)

    assert _findings(report, Severity.PASS)
    assert not _findings(report, Severity.WARN)
    assert not _findings(report, Severity.FAIL)


@pytest.mark.parametrize("stale_index_stat", [True, False], ids=["stale-index-stat", "crlf-tree"])
def test_crlf_checkout_fails_with_the_init_fix(tmp_path: Path, stale_index_stat: bool):
    """Ticket Mode is broken right now: the container reads every one of these as modified."""
    _line_endings_repo(tmp_path, "true", _LF_FILE)
    _recheckout(tmp_path, "a.v")
    if stale_index_stat:
        run_bounded_child(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "false"], check=True
        )
        (tmp_path / "a.v").write_bytes(b"module a;\nendmodule\n")
        # Git still lists the file as modified although `git diff` sees no change.
        status = ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=no"]
        assert run_bounded_child(status, check=True).stdout == " M a.v\n"
        assert run_bounded_child(["git", "-C", str(tmp_path), "diff", "--quiet"]).returncode == 0

    (failed,) = _findings(_inspect_line_endings(tmp_path), Severity.FAIL)

    assert ("stale" in failed.message) is stale_index_stat
    assert "booley init" in failed.fix
    assert "--fix-line-endings" not in failed.fix


@pytest.mark.parametrize("true_value", ["true", "yes", "on", "1"])
def test_autocrlf_true_warns_before_a_checkout_breaks(tmp_path: Path, true_value: str):
    # Nothing is broken yet: the next checkout is what breaks it.
    _line_endings_repo(tmp_path, true_value)

    report = _inspect_line_endings(tmp_path)

    assert [finding.severity for finding in report.findings] == [Severity.WARN]


def test_nested_crlf_failure_names_project_data_repository(tmp_path: Path):
    _line_endings_repo(tmp_path, "false", _LF_FILE)
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    hook = ("hooks/post-setup.sh", b"#!/bin/sh\nset -euo pipefail\n")
    (booley_dir / "hooks").mkdir()
    _line_endings_repo(booley_dir, "true", (hook,))
    _recheckout(booley_dir, hook[0])

    report = _inspect_line_endings(tmp_path, booley_dir)

    (failed,) = _findings(report, Severity.FAIL)
    assert "Booley Directory" in failed.message
    assert str(booley_dir) in failed.message
    assert any("Project Root" in f.message for f in _findings(report, Severity.PASS))


def test_nested_autocrlf_warning_has_project_data_subject(tmp_path: Path):
    _line_endings_repo(tmp_path, "false")
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    _line_endings_repo(booley_dir, "true")

    (warning,) = _findings(_inspect_line_endings(tmp_path, booley_dir), Severity.WARN)

    assert warning.check_id == "git.autocrlf-risk"
    assert warning.subject == "project-data"
    assert str(booley_dir) in warning.message


def test_unset_local_autocrlf_warning_names_project_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text("[core]\n\tautocrlf = false\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    _line_endings_repo(tmp_path, "false")
    run_bounded_child(
        ["git", "-C", str(tmp_path), "config", "--unset", "core.autocrlf"], check=True
    )

    (warning,) = _findings(_inspect_line_endings(tmp_path), Severity.WARN)

    assert warning.check_id == "git.autocrlf-risk"
    assert warning.subject == "project-checkout"
    assert "not set locally" in warning.message


@pytest.mark.parametrize(
    ("actionable", "expected"),
    [
        pytest.param("CRLF_MISMATCH", [(Severity.FAIL, None)], id="crlf-mismatch"),
        pytest.param(
            "AUTOCRLF_EFFECTIVE_TRUE", [(Severity.WARN, "git.autocrlf-risk")], id="autocrlf-true"
        ),
    ],
)
def test_actionable_line_ending_finding_precedes_unreadable_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, actionable: str, expected
):
    from booley.harness.setup.line_endings import (
        LineEndingObservation,
        LineEndingObservationCode,
        LineEndingReport,
        LineEndingRepository,
        LineEndingStatus,
        RepositoryLineEndingReport,
    )

    observations = (
        LineEndingObservation(LineEndingObservationCode[actionable]),
        LineEndingObservation(
            LineEndingObservationCode.STATUS_UNREADABLE, detail="git diff timed out"
        ),
    )
    repository = LineEndingRepository("project-checkout", tmp_path)
    report = LineEndingReport(
        LineEndingStatus.UNSAFE,
        (RepositoryLineEndingReport(repository, LineEndingStatus.UNSAFE, observations, ()),),
        (),
    )
    monkeypatch.setattr(readiness, "reconcile_project_line_endings", lambda *_a, **_kw: report)

    findings = _inspect_line_endings(tmp_path).findings

    assert [(finding.severity, finding.check_id) for finding in findings] == expected


@pytest.mark.parametrize(
    ("stealth", "location", "worktree_gone"),
    [
        pytest.param(True, ".gitattributes", False, id="stealth-gitattributes"),
        pytest.param(True, ".git/info/attributes", False, id="stealth-info-attributes"),
        pytest.param(False, ".gitattributes", False, id="plain-gitattributes"),
        pytest.param(False, ".git/info/attributes", False, id="plain-info-attributes"),
        pytest.param(True, None, True, id="stealth-worktree-gone"),
    ],
)
def test_line_endings_leave_retired_attributes_state_unreported(
    tmp_path: Path, stealth: bool, location: str | None, worktree_gone: bool
):
    """Doctor neither rewrites Booley-written attribute rules nor reports them (8cf8b14c0)."""
    from booley.harness.setup.line_endings import LineEndingMode, reconcile_project_line_endings

    root = tmp_path / "repo"
    root.mkdir()
    _line_endings_repo(root, "false", ((".gitignore", b".booley/\n"),))
    run_bounded_child(["git", "-C", str(root), "config", "core.eol", "lf"], check=True)
    data = root / ".booley"
    data.mkdir()
    (data / "booley.toml").write_text(f"[stealth]\nenabled = {str(stealth).lower()}\n")
    rule = b"* text=auto eol=lf\n"
    if location is not None:
        written = root / location
        written.parent.mkdir(exist_ok=True)
        written.write_bytes(rule)
        reconcile_project_line_endings(root, data, mode=LineEndingMode.REPAIR)
    if worktree_gone:
        # A registered worktree whose directory is gone used to leave its attributes unknown.
        sibling = tmp_path / "sibling"
        run_bounded_child(
            ["git", "-C", str(root), "worktree", "add", "-q", str(sibling), "-b", "side"],
            check=True,
        )
        shutil.rmtree(sibling)

    report = _inspect_line_endings(root, data)

    if location is not None:
        assert written.read_bytes() == rule
    assert len(_findings(report, Severity.PASS)) == 1
    assert not _findings(report, Severity.WARN) and not _findings(report, Severity.FAIL)


def _run_cwd_layout(root: Path, *steps: str) -> None:
    """Build `run` under *root*: a `file`, or a `dir` with a placeholder, `add`ed, `commit`ted."""
    for step in steps:
        if step == "file":
            (root / "run").write_text("file\n", encoding="utf-8")
        elif step == "dir":
            (root / "run").mkdir()
            (root / "run" / ".gitkeep").write_text("", encoding="utf-8")
        elif step == "add":
            run_bounded_child(["git", "add", "-f", "run/.gitkeep"], cwd=root, check=True)
        else:
            _git_commit_all(root, "placeholder")


def _run_cwd_rows(result: doctor.DoctorRunResult) -> list[DiagnosticFinding]:
    return [
        f
        for f in result.findings
        if (f.check_id or "").startswith("sim.run-cwd") or f.message.startswith("sim run_cwd")
    ]


@pytest.mark.parametrize(
    ("steps", "sim", "expected"),
    [
        pytest.param(
            (), 'run_cwd = "run"\n', ("warn", "sim.run-cwd-missing", "run"), id="missing"
        ),
        pytest.param(
            ("file",),
            'run_cwd = "run"\n',
            ("warn", "sim.run-cwd-missing", "not a directory"),
            id="regular-file",
        ),
        pytest.param(
            ("dir",), 'run_cwd = "run"\n', ("warn", "sim.run-cwd-untracked", ""), id="untracked"
        ),
        # Only the committed tree counts: a placeholder staged in the index is not enough.
        pytest.param(
            ("dir", "add"), 'run_cwd = "run"\n', ("warn", "sim.run-cwd-untracked", ""), id="staged"
        ),
        pytest.param(
            ("dir", "add", "commit"),
            'run_cwd = "run"\n',
            ("pass", "", "sim run_cwd run is committed"),
            id="committed",
        ),
        pytest.param((), 'run_cwd = "run"\nenabled = false\n', None, id="disabled"),
        pytest.param((), 'run_cwd = "."\n', None, id="current-directory"),
        pytest.param((), 'run_cwd = "runs/{attempt}"\n', None, id="templated"),
        pytest.param((), 'run_cwd = "runs/{bogus}"\n', None, id="unknown-template"),
    ],
)
def test_sim_run_cwd_preflight(tmp_path, monkeypatch, steps, sim, expected):
    _committed_repo(tmp_path)
    _run_cwd_layout(tmp_path, *steps)
    _core_project(tmp_path, monkeypatch, {}, sim=sim)
    # The check asks Git for the committed tree; a staged placeholder must not satisfy it.
    _forward_to_git(monkeypatch, "ls-tree", "ls-files")

    rows = _run_cwd_rows(run_doctor(tmp_path))

    if expected is None:
        assert rows == []
        return
    (row,) = rows
    severity, check_id, text = expected
    assert (str(row.severity), row.check_id or "") == (severity, check_id)
    assert text in row.message
    if check_id == "sim.run-cwd-missing":
        assert str(tmp_path / "run") in row.message and row.subject == "run"


def test_sim_run_cwd_inside_nested_project_repo_uses_the_innermost_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host repo ignores ``.booley/``; its own repo owns the placeholder."""
    _committed_repo(tmp_path)
    booley_dir = _core_project(tmp_path, monkeypatch, {}, sim='run_cwd = ".booley/runs/sim"\n')
    _committed_repo(booley_dir)
    (booley_dir / "runs" / "sim").mkdir(parents=True)
    (booley_dir / "runs" / "sim" / ".gitkeep").write_text("", encoding="utf-8")
    run_bounded_child(["git", "add", "-f", "runs/sim/.gitkeep"], cwd=booley_dir, check=True)
    _git_commit_all(booley_dir, "placeholder")
    _forward_to_git(monkeypatch, "ls-tree", "ls-files")

    (row,) = _run_cwd_rows(run_doctor(tmp_path))

    assert (str(row.severity), row.message) == (
        "pass",
        "sim run_cwd .booley/runs/sim is committed",
    )


_NO_SEAT_CAP = "vivado has no Seat Cap: every licensed EDA tool the Project uses needs one"


@pytest.mark.parametrize(
    ("toml", "rows"),
    [
        pytest.param("\n[flows.fpga]\n", [(Severity.FAIL, _NO_SEAT_CAP)], id="fpga-flow"),
        # `provisioning = "image"`: host provisioning is a schema error on Windows and hides the check.
        pytest.param(
            '\n[eda.vivado]\nprovisioning = "image"\n',
            [(Severity.FAIL, _NO_SEAT_CAP)],
            id="vivado-declared",
        ),
        pytest.param(
            "\n[flows.fpga]\n[jobs.seats]\nvivado = 2\n",
            [(Severity.PASS, "Seat Cap: vivado = 2")],
            id="capped",
        ),
        # Vivado is declared but the FPGA Flow is off: Booley provisions none, no seat is owed.
        pytest.param(
            '\n[flows.fpga]\nenabled = false\n[eda.vivado]\nprovisioning = "image"\n',
            [],
            id="vivado-fpga-disabled",
        ),
        pytest.param("", [], id="nothing-licensed"),
    ],
)
def test_seat_cap_check(tmp_path, monkeypatch, toml, rows):
    """ADR 0087: every licensed EDA tool in use needs a Seat Cap."""
    _core_project(tmp_path, monkeypatch, {}, toml=toml)

    result = run_doctor(tmp_path)

    assert [(f.severity, f.message) for f in result.findings if "Seat Cap" in f.message] == rows
    if rows and rows[0][0] is Severity.FAIL:
        (fix,) = _fixes(result, "Seat Cap")
        assert "[jobs.seats] vivado = <seats you own>" in fix


def test_seat_cap_check_leaves_malformed_seats_to_the_schema_audit(tmp_path):
    # Private: no run reaches this guard. The schema audit rejects a malformed [jobs.seats] first,
    # and Doctor then runs with no Project.
    rec = _Rec()
    malformed = {"flows": {"fpga": {"enabled": True}}, "jobs": {"seats": {"vivado": 0}}}

    doctor._check_seat_caps(_adr28_project(tmp_path, booley_toml=malformed), rec.p, rec.f)

    assert rec.events == []


def test_nonstealth_doctor_reports_selectively_ignored_config(tmp_path, monkeypatch):
    _history_git(tmp_path, "init", "-q")
    excludes = tmp_path / "global ignores"
    excludes.write_bytes(b"# local policy\n.booley/booley.toml\n")
    _history_git(tmp_path, "config", "core.excludesFile", str(excludes))
    # The fixture's `/.booley` exclude would hide every input and mask the selective rule.
    _core_project(
        tmp_path,
        monkeypatch,
        {".git/info/exclude": "/.devcontainer\n"},
        toml="\n[stealth]\nenabled = false\n",
    )
    _forward_to_git(monkeypatch, "check-ignore")

    result = run_doctor(tmp_path)

    warned = _warnings(result, "project.inputs-ignored")
    (message,) = warned.values()
    assert ".booley/booley.toml (1 path(s))" in message
    assert f"{excludes}:2:" in message
    assert not _warnings(result, "goals.history-ignored")
    (fix,) = _fixes(result, ".booley/booley.toml (1 path(s))")
    assert "edit the reported" in fix
    assert excludes.read_text() == "# local policy\n.booley/booley.toml\n"


@pytest.mark.parametrize(
    ("inside", "records", "message"),
    [
        (
            False,
            (0, "1.2.3.4\n1.2.3.4\n"),
            "browser is the Sandbox Image's headless Chrome 1.2.3.4",
        ),
        (True, (0, "9.9.9.9\n1.2.3.4\n"), "Chrome 9.9.9.9, the Sandbox Image ships 1.2.3.4"),
        (True, (1, "1.2.3.4\n"), "browser check failed: exited 1: cat: no such file"),
        (False, (0, "1.2.3.4\n"), "browser check failed: printed 1 lines, not two versions"),
    ],
    ids=["host-match", "inside-mismatch", "inside-unrecorded", "host-short"],
)
def test_doctor_checks_the_preview_browser_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inside: bool,
    records: tuple[int, str],
    message: str,
) -> None:
    runtime_setup(tmp_path, monkeypatch, inside=inside)
    if inside:
        _prepare_row(monkeypatch, "inside-marker")
    asked: list[list[str]] = []

    def cat_records(cmd: list[str]) -> bool:
        if cmd[-3:] != BROWSER_RECORDS:
            return False
        asked.append(cmd)
        return True

    _answer_command(
        monkeypatch,
        cat_records,
        returncode=records[0],
        stdout=records[1],
        stderr="cat: no such file\n",
    )

    result = run_doctor(tmp_path)

    (finding,) = [f for f in result.findings if f.message.startswith("T3 Code's preview")]
    assert message in finding.message
    passed = message.startswith("browser is")
    assert str(finding.severity) == ("pass" if passed else "fail")
    assert passed or finding.fix.startswith("recreate the Sandbox: ")
    (argv,) = asked
    assert argv == BROWSER_RECORDS if inside else argv[1:3] == ["run", "--rm"]


@pytest.mark.parametrize(
    ("inside", "reply", "returncode"),
    [
        pytest.param(False, "t3 v{pin}\n", 0, id="host-pinned"),
        pytest.param(True, "t3 v0.0.1\n", 0, id="inside-other"),
        pytest.param(False, "", 127, id="host-missing"),
    ],
)
def test_doctor_checks_the_pinned_t3_code_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inside: bool, reply: str, returncode: int
) -> None:
    runtime_setup(tmp_path, monkeypatch, inside=inside)
    if inside:
        _prepare_row(monkeypatch, "inside-marker")
        # The probe of the Sandbox's own T3 Code port.
        monkeypatch.setattr(incontainer_setup, "t3_state", lambda: "running")
    pin = t3_pin()
    asked: list[list[str]] = []

    def version_command(cmd: list[str]) -> bool:
        if cmd[-2:] != ["t3", "--version"]:
            return False
        asked.append(cmd)
        return True

    _answer_command(
        monkeypatch,
        version_command,
        returncode=returncode,
        stdout=reply.format(pin=pin),
        stderr="pulled\nt3: not found\n",
    )

    result = run_doctor(tmp_path)

    ((severity, message, fix),) = [
        (str(f.severity), f.message, f.fix)
        for f in result.findings
        if f.message.startswith("T3 Code") and "installed" in f.message
    ]
    (argv,) = asked
    if inside:
        assert argv == ["t3", "--version"]
        assert (severity, message) == (
            "fail",
            f"T3 Code {pin} is not installed: t3 --version reported t3 v0.0.1",
        )
        assert fix == f"recreate the Sandbox: {sandbox_apply.in_sandbox_steps()}"
        assert "T3 Code answers on 127.0.0.1:3773 in the Sandbox" in finding_messages(result)
        assert "T3 Code relay check skipped - it runs from the host" in finding_messages(result)
        return
    assert argv[1:3] == ["run", "--rm"]
    assert argv[4:] == ["t3", "--version"]
    assert "T3 Code relay check not applicable - the Sandbox is not running" in finding_messages(
        result
    )
    if returncode:
        assert (severity, message) == (
            "fail",
            f"T3 Code {pin} installed check failed: t3 --version exited 127: t3: not found",
        )
        assert fix.startswith("rerun `booley doctor`; if it fails again, recreate the Sandbox: ")
        assert result.exit_code == 1
    else:
        assert (severity, message) == (
            "pass",
            f"T3 Code {pin} installed (the version the manifest pins)",
        )


def test_a_missing_t3_binary_fails_the_install_check(tmp_path: Path) -> None:
    from booley.runtime import t3_inspection

    command = [str(tmp_path / "t3"), "--version"]
    (finding,) = t3_inspection.inspect_install(command, lambda: "steps").findings

    assert "installed check failed: t3 --version could not run: " in finding.message
    assert finding.fix == "rerun `booley doctor`; if it fails again, recreate the Sandbox: steps"


@pytest.mark.parametrize(
    ("row", "skipped"),
    [
        ("invalid-config", "checks skipped - project config invalid"),
        ("host-no-runtime", "checks skipped - no Docker/Podman runtime found on this host"),
        ("image-missing", "version check skipped - Sandbox Image missing"),
    ],
)
def test_doctor_skips_the_t3_code_checks_it_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: str, skipped: str
) -> None:
    runtime_setup(tmp_path, monkeypatch, inside=False, valid=row != "invalid-config")
    _prepare_row(monkeypatch, row)
    if row == "image-missing":
        monkeypatch.setattr(doctor.idk, "image_exists", lambda *_a, **_kw: False)

    assert f"T3 Code {skipped}" in finding_messages(run_doctor(tmp_path), Severity.SKIP)


def test_doctor_reports_a_t3_version_command_that_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_setup(tmp_path, monkeypatch, inside=False)

    def time_out(cmd: list[str]) -> bool:
        if cmd[-2:] == ["t3", "--version"]:
            raise subprocess.TimeoutExpired(cmd, 30)
        return False

    _answer_command(monkeypatch, time_out)

    failures = finding_messages(run_doctor(tmp_path), Severity.FAIL)

    (message,) = [m for m in failures if m.startswith("T3 Code")]
    assert "installed check failed: t3 --version could not run: " in message
