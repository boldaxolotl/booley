"""Applicable execution is required even when health warnings are waived."""

from __future__ import annotations

import argparse
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import booley
from booley.audit.diagnostic_results import DiagnosticReport
from booley.harness import doctor
from booley.harness import doctor_deep as deep
from booley.runtime import health, runtime_context
from tests.flows.sim.test_verilator_build_reuse_campaign import flow_project
from tests.harness.test_doctor import _write_project

__all__ = ["flow_project"]

IMAGE = "sha256:" + "a" * 64


def _alias_doctor_project(flow_project, tmp_path, monkeypatch, host):
    import shutil
    import tomllib

    from booley.harness.setup.readiness import ProjectAudit
    from tests.conftest import require_symlinks, symlink_or_skip
    from tests.harness.test_doctor import _git_init

    require_symlinks(tmp_path)
    data = tmp_path / "external-project-data"
    shutil.copytree(flow_project / ".booley", data)
    alias = tmp_path / "booley-project"
    symlink_or_skip(alias, data, target_is_directory=True)
    config = data / "booley.toml"
    config.write_text(
        "[flows.sim]\nenabled=true\n[flows.lint]\nenabled=false\n"
        "[flows.synth]\nenabled=false\n[flows.fpga]\nenabled=false\n"
    )
    core = flow_project / "demo.core"
    core.write_text(
        core.read_text().replace(
            "      tool: verilator", "      tool: verilator\n      booley: {doctor: [sim]}"
        )
    )
    bad = data / "selftest/sim/bad-overlay/src/acme_lib_demo_1/rtl"
    bad.mkdir(parents=True)
    (bad / "tb.sv").write_text("module tb; // BAD\nendmodule\n")
    shutil.copytree(data, flow_project / ".booley", dirs_exist_ok=True)
    _git_init(flow_project)
    _git_init(data)
    audit = ProjectAudit(flow_project, data, tomllib.loads(config.read_text()), {}, "sim")
    monkeypatch.setattr(doctor.dc, "BOOLEY_DIR_TARGET", str(alias))
    from tests.runtime.test_sandbox_layout import configure_project_alias

    configure_project_alias(monkeypatch, alias, data)
    monkeypatch.setattr(doctor._DoctorFlowRuntime, "inside", property(lambda _self: not host))
    monkeypatch.setattr(doctor._DoctorFlowRuntime, "command", lambda _self, inner, **_kw: inner)
    return audit, data, alias


def _real_simulation_transport(monkeypatch, flow_project, alias, data, host):
    from pathlib import Path

    from booley.flows.sim.flow import SimulateFlow

    real_run = subprocess.run
    executions = []

    def run(argv, **kwargs):
        if "booley.flows.sim" not in argv:
            return real_run(argv, **kwargs)
        values = list(argv[argv.index("booley.flows.sim") + 1 :])
        emitted = Path(values[values.index("--report-dir") + 1])
        assert emitted.is_relative_to(alias if host else data)
        values[values.index("--work-dir") + 1] = str(flow_project)
        with monkeypatch.context() as scoped:
            for name, value in kwargs.get("env", {}).items():
                scoped.setenv(name, value)
            flow = SimulateFlow()
            outcome = flow.execute_cli(values).outcome
        executions.append(outcome)
        return subprocess.CompletedProcess(argv, outcome.exit_code, outcome.report_text, "")

    monkeypatch.setattr(doctor.subprocess, "run", run)
    return executions


def _assert_real_bad_simulation_report(outcome, build_failure):
    campaign = outcome.detail["campaigns"]["sim"]
    assert campaign["complete"] is True
    assert campaign["test_run_total"] == campaign["observation_total"] == 1
    assert campaign["test_runs"][0]["grade"] == "fail"
    observation = campaign["observations"][0]
    assert campaign["test_runs"][0]["test"] == observation["test"] == "first"
    if build_failure:
        assert observation["execution"] == "blocked_by_build"
        assert observation["failure_class"] == "design"
        assert "%Error: BAD source" in observation["detail"]["reason"]
    else:
        assert campaign["build_stage"] == []
        assert observation["execution"] == "completed"
        assert observation["functional"] == "fail"
        assert observation["failure_class"] != "infrastructure"


@pytest.mark.parametrize("host", [True, False])
@pytest.mark.parametrize("build_failure", [True, False])
def test_project_alias_doctor_executes_real_simulation(
    flow_project, tmp_path, monkeypatch, host, build_failure, capsys, request
):
    from booley.runtime.booley_dir import reset_cache

    reset_cache()
    request.addfinalizer(reset_cache)
    audit, data, alias = _alias_doctor_project(flow_project, tmp_path, monkeypatch, host)
    if not build_failure:
        (data / "selftest/sim/bad-overlay/src/acme_lib_demo_1/rtl/tb.sv").write_text(
            "module tb;\n// [SIM_RESULT] FAILED\nendmodule\n"
        )
    executions = _real_simulation_transport(monkeypatch, flow_project, alias, data, host)
    reporter, tracker = doctor._Reporter.create(), deep.DeepCheckTracker()
    doctor._run_deep_checks(
        audit,
        doctor._DoctorFlowRuntime(flow_project, "docker"),
        False,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
        completeness=tracker,
    )
    assert len(executions) == 3
    for outcome in executions:
        if outcome.exit_code == 2:
            assert "Invocation lock paths must not contain symlinks" in outcome.report_text
    assert [item.exit_code for item in executions] == [0, 0, 1]
    _assert_real_bad_simulation_report(executions[-1], build_failure)
    output = capsys.readouterr().out
    if build_failure:
        assert tracker.missing == ("sim.selftest.bad",)
        assert not reporter.result(0).clean
        assert "bad overlay no longer builds" in output
        assert str(data / "selftest/sim/bad-overlay") in output
        assert "%Error: BAD source" in output
        assert "correctly graded a failure" not in output
        assert any(
            finding.check_id == "flow.selftest-overlay-build-failed"
            for finding in reporter.findings
        )
    else:
        assert tracker.missing == ()
        assert reporter.result(0).clean
        assert "correctly graded a failure" in output
    reports = data / doctor._DOCTOR_TMP / "flow-reports"
    assert len(list(reports.glob("sim/*/targets/sim/campaign/manifest.json"))) == 3
    assert len(list(reports.glob("sim/*/progress.json"))) == 3


def _project(root):
    directory = _write_project(root, seed_interactive=False)
    return SimpleNamespace(project_root=root, booley_dir=directory, booley_toml={"flows": {}})


def test_disabled_capabilities_and_fpga_exclusion_do_not_require_checks(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(doctor, "_flow_enabled", lambda *_args: False)
    tracker = deep.DeepCheckTracker()
    reporter = doctor._Reporter.create()
    doctor._run_deep_checks(
        project,
        doctor._DoctorFlowRuntime(tmp_path, None),
        False,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
        completeness=tracker,
    )
    assert tracker.missing == ()
    assert reporter.result(0).clean


def test_missing_targets_are_incomplete_even_without_health_warning(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(doctor, "_doctor_targets", lambda *_args: [])
    tracker = deep.DeepCheckTracker()
    reporter = doctor._Reporter.create()
    doctor._run_deep_checks(
        project,
        doctor._DoctorFlowRuntime(tmp_path, None),
        False,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
        completeness=tracker,
    )
    assert "sim.doctor-targets" in tracker.missing
    assert "sim.selftest.good" in tracker.missing
    assert "lint.selftest.bad" in tracker.missing


def test_waived_missing_selftest_warning_cannot_supply_execution(tmp_path, monkeypatch):
    project = _project(tmp_path)
    from booley.audit.doctor_waivers import load_doctor_waivers

    path = project.booley_dir / "doctor-waivers.toml"
    path.write_text(
        'version=1\n[[waiver]]\ncheck="flow.fail-path-unvalidated"\n'
        'reason="Reviewed exclusion"\npermanent=true\n'
    )
    reporter = doctor._Reporter.create(load_doctor_waivers(project.booley_dir))
    tracker = deep.DeepCheckTracker()
    monkeypatch.setattr(doctor, "_flow_enabled", lambda _project, flow: flow == "sim")
    doctor._run_selftest_checks(
        project,
        doctor._DoctorFlowRuntime(tmp_path, None),
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
        completeness=tracker,
    )
    assert reporter.result(0).clean
    assert reporter.counts["waived"] == 1
    assert tracker.missing == ("sim.selftest.bad", "sim.selftest.good")


@pytest.mark.parametrize("skip_agents", [False, True])
def test_clean_host_deep_probe_is_incomplete_and_omitted_agents_are_named(
    tmp_path, monkeypatch, skip_agents
):
    project = _project(tmp_path)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(
        doctor, "_observe_sandbox_identity", lambda *_args: (IMAGE, booley.__version__)
    )
    monkeypatch.setattr(doctor, "_run_deep_checks", lambda *_args, **_kw: None)
    monkeypatch.setattr(doctor, "_run_core_resolve_checks", lambda *_args, **_kw: None)
    reporter = doctor._Reporter.create(
        profile=doctor._DoctorProfile.from_args(argparse.Namespace(skip_agent_checks=skip_agents))
    )
    summary = doctor._run_deep_phase(
        project, "docker", doctor._DoctorFlowRuntime(tmp_path, "docker"), False, reporter
    )
    assert reporter.result(0).clean
    assert not summary.complete
    assert "developer-probe" in summary.missing_checks
    assert ("agent-checks" in summary.missing_checks) is skip_agents


@pytest.mark.parametrize(
    "identity,reason",
    [
        ((IMAGE, "other"), "booley-version-mismatch"),
        ((None, booley.__version__), "image-identity-unavailable"),
        ((IMAGE, None), "sandbox-version-unavailable"),
    ],
)
def test_start_identity_controls_qualification(tmp_path, monkeypatch, identity, reason):
    observe = Mock(return_value=identity)
    monkeypatch.setattr(doctor, "_observe_sandbox_identity", observe)
    monkeypatch.setattr(doctor, "_run_deep_checks", lambda *_args, **_kw: None)
    monkeypatch.setattr(doctor, "_run_core_resolve_checks", lambda *_args, **_kw: None)
    runtime = doctor._DoctorFlowRuntime(tmp_path, "docker", "sandbox")
    summary = doctor._run_deep_phase(
        _project(tmp_path), "docker", runtime, False, doctor._Reporter.create()
    )
    assert summary.identity_reasons == (reason,)
    assert not summary.complete
    observe.assert_called_once_with(tmp_path, "sandbox")


def test_inside_local_core_resolution_completes_without_docker(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(
        doctor, "_project_target_matrix", lambda _: SimpleNamespace(seed_targets=("sim_fast",))
    )
    commands = []

    def run(argv, **_kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            "[[CORE_RESOLVE_JSON]]" + json.dumps([{"selector": "sim_fast", "ok": True}]),
            "",
        )

    monkeypatch.setattr(doctor.subprocess, "run", run)
    reporter = doctor._Reporter.create()
    tracker = deep.DeepCheckTracker()
    doctor._run_core_resolve_checks(
        project,
        None,
        reporter.pass_,
        reporter.skip_,
        reporter.fail_,
        flow_runtime=doctor._DoctorFlowRuntime(tmp_path, None),
        completeness=tracker,
    )
    assert tracker.missing == ()
    assert commands[0][:2] == ["python3", "-c"]
    assert reporter.result(0).clean


def test_host_fusesoc_fallback_cannot_certify_a_sandbox(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(
        doctor, "_project_target_matrix", lambda _: SimpleNamespace(seed_targets=("sim_fast",))
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/usr/bin/fusesoc")
    monkeypatch.setattr(
        doctor.subprocess, "run", Mock(side_effect=AssertionError("host resolution"))
    )
    reporter, tracker = doctor._Reporter.create(), deep.DeepCheckTracker()
    doctor._run_core_resolve_checks(
        project,
        None,
        reporter.pass_,
        reporter.skip_,
        reporter.fail_,
        flow_runtime=doctor._DoctorFlowRuntime(tmp_path, None),
        completeness=tracker,
    )
    assert tracker.missing
    assert reporter.result(0).clean


@pytest.mark.parametrize(
    "entries", [[], [{"selector": "other", "ok": True}], [{"selector": "selected", "ok": "yes"}]]
)
def test_core_completion_requires_all_selected_verdicts(entries):
    reporter = doctor._Reporter.create()
    proc = subprocess.CompletedProcess([], 0, "[[CORE_RESOLVE_JSON]]" + json.dumps(entries), "")
    assert (
        doctor._grade_core_resolution(proc, {"selected": None}, reporter.pass_, reporter.fail_)
        == ()
    )
    assert not reporter.result(1).clean


@pytest.mark.parametrize("early", ["scheduled", "changed", "quiet"])
def test_session_up_reports_deep_once_through_all_health_branches(
    tmp_path, monkeypatch, capsys, early
):
    from booley.harness import booley as cli

    monkeypatch.setattr(cli, "_report_upgrade_before_session", lambda _: None)
    monkeypatch.setattr(
        doctor.session_runtime,
        "ensure",
        lambda *_args, **_kw: doctor.session_runtime.EnsureResult("sandbox", False, True),
    )
    monkeypatch.setattr(
        health, "due_reason", lambda _: "plain expired" if early == "scheduled" else None
    )
    healthy = health.HealthSnapshot(DiagnosticReport(), "clean", tmp_path / "last.log", None)
    monkeypatch.setattr(
        health,
        "consume_changed_snapshot",
        lambda *_args, **_kw: healthy if early == "changed" else None,
    )
    observe = Mock(
        return_value=deep.evaluate_deep_status(None, version=booley.__version__, image_id=IMAGE)
    )
    monkeypatch.setattr(doctor, "observe_deep_status", observe)
    assert cli._session_up(argparse.Namespace(), tmp_path) == 0
    assert capsys.readouterr().out.count("deep Doctor due:") == 1
    observe.assert_called_once_with(tmp_path, container="sandbox")


@pytest.mark.parametrize("storage_fails", [False, True])
def test_probe_cost_accounting_and_completeness_are_independent(
    tmp_path, monkeypatch, storage_fails
):
    from booley.harness import developer_probe

    project = _project(tmp_path)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    usage = developer_probe.ProbeUsage(12, 0, 3, 0.001)
    monkeypatch.setattr(
        developer_probe,
        "measure_developer_rss",
        lambda _: developer_probe.ProbeMeasurement(1024, True, usage),
    )
    if storage_fails:
        monkeypatch.setattr(
            developer_probe, "record_measurement", Mock(side_effect=OSError("unwritable"))
        )
    reporter, tracker = doctor._Reporter.create(), deep.DeepCheckTracker()
    doctor._track_developer_probe(project, reporter, tracker)
    assert reporter.agent_calls == [doctor._AgentCallRecord("agent probe", usage)]
    assert tracker.missing == (("developer-probe",) if storage_fails else ())


def _qualification_measurement(missing):
    from booley.harness import developer_probe

    if missing == "developer":
        raise developer_probe.ProbeError("process accounting unavailable", agent_failure=False)
    return developer_probe.ProbeMeasurement(1024, True, None)


def _qualification_project(tmp_path, monkeypatch, policy, missing, health_failure, data_policy):
    import tomllib

    from booley.harness import developer_probe
    from booley.harness.setup import git_hooks, readiness
    from tests.harness.test_doctor import _git_init

    _git_init(tmp_path)
    directory = _write_project(tmp_path, seed_interactive=False)
    # Select exactly one real Simulation Target and exclude unrelated Flow families.
    core = tmp_path / "unit.core"
    core.write_text(core.read_text().split("  lint_fast:")[0])
    config = tomllib.loads((directory / "booley.toml").read_text())
    config["flows"]["lint"]["enabled"] = False
    config["flows"]["synth"]["enabled"] = False
    config["flows"]["sim"]["enabled"] = True
    audit = readiness.ProjectAudit(tmp_path, directory, config, {}, "sim_fast")
    overlay = directory / "selftest" / "sim" / "bad-overlay"
    overlay.mkdir(parents=True)
    (overlay / "bad.sv").write_text("invalid fixture for boundary transport\n")
    assert git_hooks._set_local_config(tmp_path, policy) is None
    if data_policy is not None:
        _git_init(directory)
        assert git_hooks._set_local_config(directory, data_policy) is None
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(readiness, "_git_version_at", lambda *_args: (2, 53, 0))
    monkeypatch.setattr(doctor, "_run_project_phase", lambda *_args, **_kw: (None, audit))

    def health(_project, _runtime, _verbose, reporter, _progress):
        if health_failure:
            reporter.fail_("unrelated project health failed", "repair project")

    monkeypatch.setattr(doctor, "_run_flow_and_core_phase", health)
    for name in (
        "_check_runtime_location",
        "_check_memory_invariant",
        "_run_container_checks",
        "_run_mcp_checks",
        "_run_project_checks",
    ):
        monkeypatch.setattr(doctor, name, lambda *_args, **_kw: None)
    monkeypatch.setenv("BOOLEY_SANDBOX_IMAGE_ID", IMAGE)

    monkeypatch.setattr(
        developer_probe,
        "measure_developer_rss",
        lambda _project: _qualification_measurement(missing),
    )
    return audit


def _qualification_transport(monkeypatch, missing, project):
    real_run = subprocess.run
    commands = []
    completions = {}
    real_completed = deep.DeepCheckTracker.completed

    def completed(tracker, identifier, value):
        completions[identifier] = value
        real_completed(tracker, identifier, value)

    monkeypatch.setattr(deep.DeepCheckTracker, "completed", completed)

    def run(argv, **kwargs):
        if argv[0] == "git":
            return real_run(argv, **kwargs)
        if len(argv) > 2 and argv[1] == "-c" and "[[CORE_RESOLVE_JSON]]" in argv[2]:
            commands.append("core")
            if missing == "core":
                return subprocess.CompletedProcess(argv, 0, "malformed core result", "")
            payload = json.loads(argv[-1])
            return subprocess.CompletedProcess(
                argv,
                0,
                "[[CORE_RESOLVE_JSON]]"
                + json.dumps([{"selector": item["selector"], "ok": True} for item in payload]),
                "",
            )
        if "booley.flows.sim" in argv:
            kind = kwargs.get("env", {}).get(doctor.selftest_overlay.INTERNAL_KIND_ENV)
            commands.append(kind or "smoke")
            if kind == "bad":
                from tests.harness.test_doctor_selftest_builtin import _write_sim_bad_report

                _write_sim_bad_report(argv, project=project)
            return subprocess.CompletedProcess(
                argv,
                1 if kind == "bad" else 0,
                "[SIM_RESULT] FAILED" if kind == "bad" else "[SIM_RESULT] PASSED",
                "",
            )
        raise AssertionError(f"unexpected external command: {argv}")

    monkeypatch.setattr(doctor.subprocess, "run", run)
    return commands, completions


@pytest.mark.parametrize(
    "policy,missing,health_failure,data_policy,expected",
    [
        ("true", None, False, None, deep.DeepOutcome.SUCCESS),
        ("true", None, False, "true", deep.DeepOutcome.SUCCESS),
        ("true", None, False, "false", deep.DeepOutcome.FAILED),
        ("false", None, False, None, deep.DeepOutcome.FAILED),
        (None, None, False, None, deep.DeepOutcome.FAILED),
        ("true", "developer", False, None, deep.DeepOutcome.INCOMPLETE),
        ("true", "core", False, None, deep.DeepOutcome.FAILED),
        ("true", None, True, None, deep.DeepOutcome.FAILED),
    ],
)
def test_runtime_policy_full_doctor_qualification(
    tmp_path, monkeypatch, policy, missing, health_failure, data_policy, expected
):
    """Real orchestration, grading, tracker, persistence and identity qualification."""
    audit = _qualification_project(
        tmp_path, monkeypatch, policy, missing, health_failure, data_policy
    )
    commands, completions = _qualification_transport(monkeypatch, missing, audit)
    result = doctor.run_doctor_result(
        argparse.Namespace(deep=True, skip_agent_checks=False), tmp_path
    )
    assert result.health_evidence is True
    if policy == "true":
        assert any(
            item.severity == "note" and "host version not rechecked" in item.message
            for item in result.findings
        )
        assert bool(
            [
                item
                for item in result.findings
                if item.check_id == "git.worktree-portability" and item.severity == "warn"
            ]
        ) is (data_policy == "false")
    record = deep.load_deep_record(audit.booley_dir)
    assert record.outcome is expected
    blocking = {f.check_id for f in result.diagnostics.active_findings if f.check_id}
    if expected is deep.DeepOutcome.FAILED:
        extra = {"core.resolve.d97e8ce47f651215"} if missing == "core" else set()
        assert record.reasons == tuple(sorted(blocking | extra))
        assert [r.code for r in result.deep_status.reasons] == ["latest-attempt-failed"]
    elif expected is deep.DeepOutcome.INCOMPLETE:
        assert record.reasons == ("developer-probe",)
    else:
        assert not record.reasons
    assert result.deep_status.current is (expected is deep.DeepOutcome.SUCCESS)
    assert result.clean is (
        policy == "true" and data_policy != "false" and missing != "core" and not health_failure
    )
    assert commands == ["smoke", "good", "bad", "core"]
    assert set(completions) == {
        *([] if missing == "developer" else ["developer-probe"]),
        "sim.smoke.d97e8ce47f651215",
        "sim.selftest.good",
        "sim.selftest.bad",
        *([] if missing == "core" else ["core.resolve.d97e8ce47f651215"]),
    }
    assert ("developer-probe" in completions) is (missing != "developer")
    assert all(value for key, value in completions.items() if key != "developer-probe")
    assert doctor._doctor_targets(audit, "sim") == ["sim_fast"]
    assert not doctor._flow_enabled(audit, "lint")
    assert not doctor._flow_enabled(audit, "synth")


def test_pending_review_qualifies_once_and_ack_preserves_deep(tmp_path, monkeypatch):
    from booley.harness import upgrade_review
    from tests.harness.test_upgrade_review import _changelog

    monkeypatch.setattr(booley, "__version__", "2.0.0")
    audit = _qualification_project(tmp_path, monkeypatch, "true", None, False, None)
    upgrade_review.observe(audit.booley_dir, current_version="1.0.0")

    def project_phase(_root, reporter, **_kwargs):
        doctor._check_upgrade_review(audit.booley_dir, reporter)
        return None, audit

    monkeypatch.setattr(doctor, "_run_project_phase", project_phase)
    commands, completions = _qualification_transport(monkeypatch, None, audit)
    plain = doctor.run_doctor_result(argparse.Namespace(deep=False), tmp_path)
    assert [f.check_id for f in plain.diagnostics.active_findings] == ["upgrade.review-pending"]
    assert not plain.clean and plain.exit_code == 0
    result = doctor.run_doctor_result(argparse.Namespace(deep=True), tmp_path)
    assert not result.clean and result.exit_code == 0
    assert result.deep_status.current
    assert deep.load_deep_record(audit.booley_dir).outcome is deep.DeepOutcome.SUCCESS
    before = deep.deep_stamp_path(audit.booley_dir).read_bytes()
    upgrade_review.acknowledge(audit.booley_dir, "2.0.0", packaged_changelog=_changelog(tmp_path))
    final = doctor.run_doctor_result(argparse.Namespace(deep=False), tmp_path)
    assert final.clean and final.deep_status.current
    assert deep.deep_stamp_path(audit.booley_dir).read_bytes() == before
    assert commands == ["smoke", "good", "bad", "core"]
    assert len(completions) == 5


@pytest.mark.parametrize("waived,missing", [(False, None), (True, None), (True, "developer")])
def test_real_qualification_applies_warning_waiver_without_inventing_execution(
    tmp_path, monkeypatch, waived, missing
):
    audit = _qualification_project(tmp_path, monkeypatch, "false", missing, False, None)
    if waived:
        (audit.booley_dir / "doctor-waivers.toml").write_text(
            'version=1\n[[waiver]]\ncheck="git.worktree-portability"\n'
            'reason="Reviewed policy exception"\npermanent=true\n'
        )
    commands, _completions = _qualification_transport(monkeypatch, missing, audit)
    result = doctor.run_doctor_result(argparse.Namespace(deep=True), tmp_path)
    record = deep.load_deep_record(audit.booley_dir)
    assert result.deep_status.current is (waived and missing is None)
    assert record.outcome is (
        deep.DeepOutcome.FAILED
        if not waived
        else deep.DeepOutcome.SUCCESS
        if missing is None
        else deep.DeepOutcome.INCOMPLETE
    )
    assert ("git.worktree-portability" in record.reasons) is (not waived)
    assert result.counts["waived"] == (3 if waived else 0)
    assert commands == ["smoke", "good", "bad", "core"]
