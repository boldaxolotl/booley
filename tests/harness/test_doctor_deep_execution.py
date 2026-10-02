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


def _qualification_project(tmp_path, monkeypatch, policy, missing, health_failure):
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
        "_run_ticket_preflight_parity_checks",
    ):
        monkeypatch.setattr(doctor, name, lambda *_args, **_kw: None)
    monkeypatch.setattr(sandbox_artifact, "observe_execution", lambda *_args, **_kw: _artifact())
    monkeypatch.setattr(sandbox_artifact, "observe", lambda *_args, **_kw: _artifact())
    monkeypatch.setattr(
        developer_probe,
        "measure_developer_rss",
        lambda _project: developer_probe.ProbeMeasurement(
            None if missing == "developer" else 1024, missing != "developer", None
        ),
    )
    return audit


def _qualification_transport(monkeypatch, missing):
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
    "policy,missing,health_failure,expected",
    [
        ("true", None, False, deep.DeepOutcome.SUCCESS),
        ("false", None, False, deep.DeepOutcome.FAILED),
        (None, None, False, deep.DeepOutcome.FAILED),
        ("true", "developer", False, deep.DeepOutcome.INCOMPLETE),
        ("true", "core", False, deep.DeepOutcome.FAILED),
        ("true", None, True, deep.DeepOutcome.FAILED),
    ],
)
def test_runtime_policy_full_doctor_qualification(
    tmp_path, monkeypatch, policy, missing, health_failure, expected
):
    """Real orchestration, grading, tracker, persistence and identity qualification."""
    audit = _qualification_project(tmp_path, monkeypatch, policy, missing, health_failure)
    commands, completions = _qualification_transport(monkeypatch, missing)
    result = doctor.run_doctor_result(
        argparse.Namespace(deep=True, skip_agent_checks=False), tmp_path
    )
    assert result.health_evidence is True
    if policy == "true":
        assert any(
            item.severity == "note" and "host version not rechecked" in item.message
            for item in result.findings
        )
        assert not [
            item
            for item in result.findings
            if item.check_id == "git.worktree-portability" and item.severity == "warn"
        ]
    state = deep.load_deep_state(audit.project_dir)
    assert state.latest_completed_attempt.outcome is expected
    assert result.deep_status.current is (expected is deep.DeepOutcome.SUCCESS)
    assert result.clean is (policy == "true" and missing != "core" and not health_failure)
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
    assert state.latest_completed_attempt.complete is (missing is None)
    assert doctor._doctor_targets(audit, "sim") == ["sim_fast"]
    assert not doctor._flow_enabled(audit, "lint")
    assert not doctor._flow_enabled(audit, "synth")
    if expected is deep.DeepOutcome.SUCCESS:
        assert state.latest_completed_attempt.complete
        assert not state.latest_completed_attempt.reasons
