"""Doctor warning paths reach the real reporter with stable, waivable check IDs (#2123)."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.audit.diagnostic_results import Severity
from booley.audit.doctor_waivers import load_doctor_waivers
from booley.harness import doctor
from booley.runtime import runtime_context

_SIM_TARGET = "sim_cpp"


def _write_waiver(booley_dir: Path, check_id: str, subject: str | None) -> None:
    """Write one permanent waiver for *check_id* (and *subject* when given)."""
    subject_line = f'subject = "{subject}"\n' if subject else ""
    (booley_dir / "doctor-waivers.toml").write_text(
        "version = 1\n[[waiver]]\n"
        f'check = "{check_id}"\n{subject_line}'
        'reason = "Accepted for the #2123 regression test."\npermanent = true\n'
    )


def _run_in_sandbox_container_checks(monkeypatch, reporter, *, hooks: str | None, ext: str | None):
    """Drive the in-Sandbox container checks with stubbed runtime probes."""
    from booley.harness import activity_hooks, web_isolation
    from booley.runtime import dashboard_extension

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    # The egress probe only runs in a marked Sandbox; keep this test off real DNS.
    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    monkeypatch.setattr(web_isolation, "policy_error", lambda: None)
    monkeypatch.setattr(activity_hooks, "hooks_error", lambda: hooks)
    monkeypatch.setattr(dashboard_extension, "installed_error", lambda: ext)
    doctor._run_container_checks(
        None,
        None,
        "booley-sandbox",
        False,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
    )


def _untagged_cpp_project(root: Path) -> doctor.ProjectAudit:
    """A sim Target whose C++ harness file is in an untagged fileset."""
    (root / "dut.sv").write_text("module dut; endmodule\n")
    (root / "main.cpp").write_text("int main() { return 0; }\n")
    (root / "design.core").write_text(
        "CAPI=2:\nname: ::cpp_harness:0\nfilesets:\n"
        "  rtl: {files: [dut.sv], file_type: systemVerilogSource}\n"
        "  harness: {files: [main.cpp], file_type: cppSource}\n"
        f"targets:\n  {_SIM_TARGET}:\n    flow: sim\n    flow_options: {{tool: verilator}}\n"
        "    filesets: [rtl, harness]\n    toplevel: dut\n"
    )
    booley_dir = root / ".booley"
    booley_dir.mkdir(exist_ok=True)
    return doctor.ProjectAudit(
        project_root=root,
        booley_dir=booley_dir,
        booley_toml={},
        configs_toml={"x": {}},
        first_target="x",
    )


def _emit(case: str, tmp_path, monkeypatch, reporter):
    if case == "hooks":
        _run_in_sandbox_container_checks(monkeypatch, reporter, hooks="hook missing", ext=None)
    elif case == "ext":
        _run_in_sandbox_container_checks(
            monkeypatch, reporter, hooks=None, ext="extension missing"
        )
    else:
        project = _untagged_cpp_project(tmp_path)
        doctor._run_core_audit(
            project,
            reporter.pass_,
            reporter.warn_,
            reporter.skip_,
            reporter.fail_,
            _note=reporter.note_,
        )


_CASES = [
    pytest.param("hooks", "sandbox.activity-hooks-unavailable", None, id="activity-hooks"),
    pytest.param("ext", "sandbox.dashboard-extension-unavailable", None, id="dashboard-extension"),
    pytest.param("sim", "sim.harness-untagged", _SIM_TARGET, id="sim-harness"),
]


@pytest.mark.parametrize(("case", "check_id", "subject"), _CASES)
def test_warning_reaches_real_reporter_with_check_id(
    tmp_path, monkeypatch, capsys, case, check_id, subject
):
    reporter = doctor._Reporter.create()
    _emit(case, tmp_path, monkeypatch, reporter)
    warns = [
        f for f in reporter.findings if f.severity is Severity.WARN and f.check_id == check_id
    ]
    assert [f.subject for f in warns] == [subject]
    tag = f"{check_id}:{subject}" if subject else check_id
    assert tag in capsys.readouterr().out


@pytest.mark.parametrize(("case", "check_id", "subject"), _CASES)
def test_warning_check_id_is_waivable(tmp_path, monkeypatch, case, check_id, subject):
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    _write_waiver(booley_dir, check_id, subject)
    reporter = doctor._Reporter.create(load_doctor_waivers(booley_dir))
    _emit(case, tmp_path, monkeypatch, reporter)
    assert [f.check_id for f in reporter.findings if f.severity is Severity.WAIVED] == [check_id]
    assert not [
        f for f in reporter.findings if f.check_id == check_id and f.severity is Severity.WARN
    ]
