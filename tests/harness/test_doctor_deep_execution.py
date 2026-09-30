"""Applicable execution is required even when health warnings are waived."""

from __future__ import annotations

import argparse
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import booley
from booley.harness import doctor
from booley.harness import doctor_deep as deep
from booley.runtime import runtime_context, sandbox_artifact
from tests.harness.test_doctor import _write_project

IMAGE = "sha256:" + "a" * 64


def _project(root):
    directory = _write_project(root, seed_interactive=False)
    return SimpleNamespace(project_root=root, project_dir=directory, booley_toml={"flows": {}})


def _artifact(image=IMAGE, *, version=None, container="container"):
    return sandbox_artifact.SandboxArtifact(image, container, version or booley.__version__, "")


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
    from booley.harness.doctor_waivers import load_doctor_waivers

    path = project.project_dir / "doctor-waivers.toml"
    path.write_text(
        'version=1\n[[waiver]]\ncheck="flow.fail-path-unvalidated"\n'
        'reason="Reviewed exclusion"\npermanent=true\n'
    )
    reporter = doctor._Reporter.create(load_doctor_waivers(project.project_dir))
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
    monkeypatch.setattr(sandbox_artifact, "observe_execution", lambda *_args, **_kw: _artifact())
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
    "after,reason",
    [
        (_artifact("sha256:" + "b" * 64), "sandbox-artifact-drift"),
        (_artifact(container="replacement"), "sandbox-artifact-drift"),
        (_artifact(version="other"), "booley-version-mismatch"),
        (sandbox_artifact.SandboxArtifact(), "image-identity-unavailable"),
    ],
)
def test_identity_drift_or_unknown_version_prevents_qualification(after, reason):
    assert reason in doctor._deep_identity_reasons(_artifact(), after, booley.__version__)


def test_flow_execution_is_pinned_to_inspected_container_id(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(sandbox_artifact, "observe", lambda *_args, **_kw: _artifact())
    runtime = doctor._DoctorFlowRuntime(
        tmp_path, "docker", "mutable-name", deep_artifact=_artifact()
    )
    argv = runtime.command(["python3", "-V"])
    assert "container" in argv
    assert "mutable-name" not in argv
    monkeypatch.setattr(
        sandbox_artifact, "observe", lambda *_args, **_kw: _artifact(container="new")
    )
    with pytest.raises(doctor.session_runtime.SessionError, match="changed during"):
        runtime.command(["python3", "-V"])


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
    from booley.harness import auto_doctor
    from booley.harness import booley as cli

    monkeypatch.setattr(cli, "_report_upgrade_before_session", lambda _: None)
    monkeypatch.setattr(doctor.session_runtime, "conflicting_vscode_session", lambda _: None)
    monkeypatch.setattr(doctor.session_runtime, "up", lambda *_args, **_kw: "sandbox")
    monkeypatch.setattr(
        auto_doctor, "due_reason", lambda _: "plain expired" if early == "scheduled" else None
    )
    monkeypatch.setattr(
        auto_doctor,
        "consume_changed_summary",
        lambda *_args, **_kw: "plain healthy" if early == "changed" else None,
    )
    monkeypatch.setattr(auto_doctor, "load_report", lambda _: {})
    observe = Mock(
        return_value=deep.evaluate_deep_status(
            deep.DeepState(), version=booley.__version__, image_id=IMAGE
        )
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
    assert reporter.agent_calls == [doctor._AgentCallRecord("developer probe", usage)]
    assert tracker.missing == (("developer-probe",) if storage_fails else ())
