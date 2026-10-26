"""--deep fail-path self-test must run on the BUILTIN flow (ADR 0039).

The QA-4/QA-5 fixture mechanism was gated `is_project_native`, so a builtin
project's conventional fail-path fixtures silently never ran. This was found
by the C910 re-port gate, where doctor --deep reported green without ever
proving the fail path. The gate is now backend-agnostic: only a disabled Flow
(enabled = false) skips.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from booley.fusesoc import selftest_overlay
from booley.harness import doctor
from booley.runtime import runtime_context
from tests.diagnostic_helpers import _require_doctor_warning


class _Rec:
    def __init__(self):
        self.events: list[tuple[str, str]] = []

    def p(self, msg):
        self.events.append(("pass", msg))

    def w(self, msg, fix: str = ""):
        _require_doctor_warning(msg)
        self.events.append(("warn", msg))

    def s(self, msg):
        self.events.append(("skip", msg))

    def f(self, msg, fix=""):
        self.events.append(("fail", msg))

    def fails(self):
        return [m for lvl, m in self.events if lvl == "fail"]


def _set_venue(monkeypatch, inside: bool) -> None:
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: inside)


def _runtime(project, docker_exe: str | None = None):
    return doctor._DoctorFlowRuntime(project.project_root, docker_exe)


def _audit(
    tmp_path,
    *,
    enabled: bool | None = None,
    fixture: bool = True,
):
    pd = tmp_path / ".booley"
    pd.mkdir(exist_ok=True)
    sim: dict = {}
    if enabled is not None:
        sim["enabled"] = enabled
    (pd / "tests.toml").write_text('[sim_core]\ntests = ["hello_world"]\n', encoding="utf-8")
    if fixture:
        overlay = pd / "selftest" / "sim" / "bad-overlay" / "firmware.hex"
        overlay.parent.mkdir(parents=True)
        overlay.write_text("broken\n", encoding="utf-8")
    (tmp_path / "unit.core").write_text(
        "CAPI=2:\n"
        "name: booley::unit:0\n"
        "targets:\n"
        "  sim_core:\n"
        "    flow: sim\n"
        "    flow_options: {tool: verilator, booley: {doctor: [sim]}}\n",
        encoding="utf-8",
    )
    return doctor.ProjectAudit(
        project_root=tmp_path,
        booley_dir=pd,
        booley_toml={"flows": {"sim": sim, "lint": {"enabled": False}}},
        configs_toml={"sim_core": {}},
        first_target="sim_core",
    )


def _simulation_failure_report(target="sim_core", test="hello_world", variant=None):
    name = test + (f"+{variant}" if variant else "") + "@1" if test else None
    return {
        "$schema": "booley.simulation-report/v3",
        "flow": "sim",
        "exit_code": 1,
        "detail": {
            "campaigns": {
                target: {
                    "complete": True,
                    "build_stage": [],
                    "test_run_total": 1,
                    "test_runs_truncated": False,
                    "observation_total": 1,
                    "observations_truncated": False,
                    "test_runs": [
                        {
                            "name": name,
                            "test": test,
                            "variant": variant,
                            "seed": 1,
                            "args": [],
                            "env": {},
                            "fingerprint": "sha256:" + "a" * 64,
                            "grade": "fail",
                        }
                    ],
                    "observations": [
                        {
                            "test": test,
                            "execution": "completed",
                            "failure_class": "design",
                            "functional": "fail",
                            "assertions": "clean",
                            "detail": {"reason": ""},
                        }
                    ],
                }
            }
        },
    }


def _write_sim_bad_report(cmd, *, project=None):
    # Host transports render container paths; use the owning Project when supplied.
    root = (
        project.booley_dir / doctor.DOCTOR_TMP_DIR / "flow-reports"
        if project
        else Path(cmd[cmd.index("--report-dir") + 1])
    )
    target = cmd[cmd.index("--target") + 1]
    test = cmd[cmd.index("--test") + 1] if "--test" in cmd else None
    root.mkdir(parents=True, exist_ok=True)
    (root / "sim.json").write_text(json.dumps(_simulation_failure_report(target, test)))


def _run(monkeypatch, project, exit_by_kind):
    # In-container in-place execution path (F-17): no docker wrap needed.
    _set_venue(monkeypatch, True)

    def fake_run(cmd, **kwargs):
        kind = kwargs["env"][selftest_overlay.INTERNAL_KIND_ENV]
        if kind == "bad" and "booley.flows.sim" in cmd:
            _write_sim_bad_report(cmd, project=project)
        return subprocess.CompletedProcess(cmd, exit_by_kind[kind], stdout="", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    rec = _Rec()
    doctor._run_selftest_checks(project, _runtime(project), rec.p, rec.w, rec.s, rec.f)
    return rec


def test_builtin_selftest_runs(tmp_path, monkeypatch):
    # Default backend (builtin, no key at all) with conventional fixtures → the
    # good/bad pair executes and grades.
    rec = _run(monkeypatch, _audit(tmp_path), {"good": 0, "bad": 1})
    assert not rec.fails()
    assert any("correctly graded a failure" in m for _, m in rec.events)


def test_builtin_false_pass_is_caught(tmp_path, monkeypatch):
    rec = _run(monkeypatch, _audit(tmp_path), {"good": 0, "bad": 0})
    assert any("FALSE-PASSED" in m for m in rec.fails())


def test_good_and_bad_runs_pin_the_same_smoke_test(tmp_path, monkeypatch):
    seen_tests: list[str] = []
    _set_venue(monkeypatch, True)

    def fake_run(cmd, **kwargs):
        seen_tests.append(cmd[cmd.index("--test") + 1])
        kind = kwargs["env"][selftest_overlay.INTERNAL_KIND_ENV]
        if kind == "bad" and "booley.flows.sim" in cmd:
            _write_sim_bad_report(cmd, project=project)
        return subprocess.CompletedProcess(cmd, {"good": 0, "bad": 1}[kind], stdout="", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    rec = _Rec()
    project = _audit(tmp_path)
    doctor._run_selftest_checks(project, _runtime(project), rec.p, rec.w, rec.s, rec.f)

    assert not rec.fails()
    assert seen_tests == ["hello_world", "hello_world"]


def test_builtin_without_fixtures_warns(tmp_path, monkeypatch):
    _set_venue(monkeypatch, False)
    rec = _Rec()
    project = _audit(tmp_path, fixture=False)
    doctor._run_selftest_checks(project, _runtime(project, "docker"), rec.p, rec.w, rec.s, rec.f)
    assert not rec.fails()
    assert any("fail-path unvalidated" in m for lvl, m in rec.events if lvl == "warn")


def _lint_audit(tmp_path, *, fixture: bool = True):
    pd = tmp_path / ".booley"
    pd.mkdir(exist_ok=True)
    (tmp_path / "unit.core").write_text(
        "CAPI=2:\n"
        "name: booley::unit:0\n"
        "targets:\n"
        "  lint_core:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator, booley: {doctor: [lint]}}\n",
        encoding="utf-8",
    )
    if fixture:
        (tmp_path / "doctor-selftest.core").write_text(
            "CAPI=2:\n"
            "name: booley::doctor-selftest:0\n"
            "targets:\n"
            "  lint_selftest_bad:\n"
            "    flow: lint\n"
            "    flow_options: {tool: verilator, booley: {doctor_selftest: true}}\n",
            encoding="utf-8",
        )
    return doctor.ProjectAudit(
        project_root=tmp_path,
        booley_dir=pd,
        booley_toml={
            "flows": {
                "sim": {"enabled": False},
                "lint": {},
            }
        },
        configs_toml={},
        first_target="",
    )


def test_lint_selftest_uses_conventional_bad_target(tmp_path, monkeypatch):
    seen_targets: list[str] = []
    _set_venue(monkeypatch, True)

    def fake_run(cmd, **kwargs):
        seen_targets.append(cmd[cmd.index("--target") + 1])
        kind = kwargs["env"][selftest_overlay.INTERNAL_KIND_ENV]
        if kind == "bad" and "booley.flows.sim" in cmd:
            _write_sim_bad_report(cmd, project=project)
        return subprocess.CompletedProcess(cmd, {"good": 0, "bad": 1}[kind], stdout="", stderr="")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    rec = _Rec()
    project = _lint_audit(tmp_path)
    doctor._run_selftest_checks(project, _runtime(project), rec.p, rec.w, rec.s, rec.f)

    assert not rec.fails()
    assert seen_targets == ["lint_core", "lint_selftest_bad"]


def test_lint_without_conventional_bad_target_warns(tmp_path, monkeypatch):
    _set_venue(monkeypatch, False)
    rec = _Rec()
    project = _lint_audit(tmp_path, fixture=False)

    doctor._run_selftest_checks(project, _runtime(project, "docker"), rec.p, rec.w, rec.s, rec.f)

    warns = [m for level, m in rec.events if level == "warn"]
    lint_warn = next(m for m in warns if m.startswith("lint fail-path unvalidated"))
    assert "lint_selftest_bad" in lint_warn
    assert "dedicated tracked .core" in lint_warn


def test_lint_unmarked_bad_target_warns(tmp_path, monkeypatch):
    project = _lint_audit(tmp_path, fixture=False)
    (tmp_path / "unmarked.core").write_text(
        "CAPI=2:\nname: booley::unmarked:0\ntargets:\n"
        "  lint_selftest_bad:\n"
        "    flow: lint\n"
        "    flow_options: {tool: verilator}\n",
        encoding="utf-8",
    )
    _set_venue(monkeypatch, False)
    rec = _Rec()

    doctor._run_selftest_checks(project, _runtime(project, "docker"), rec.p, rec.w, rec.s, rec.f)

    assert any(
        "booley.doctor_selftest metadata" in message
        for level, message in rec.events
        if level == "warn"
    )


def test_disabled_flow_skips_silently(tmp_path, monkeypatch):
    _set_venue(monkeypatch, False)
    rec = _Rec()
    project = _audit(tmp_path, enabled=False)
    doctor._run_selftest_checks(project, _runtime(project, "docker"), rec.p, rec.w, rec.s, rec.f)
    # The disabled simulate emits nothing — not even the fixtures nag. (The
    # unconfigured-but-active lint still gets its own unvalidated WARN.)
    assert not rec.fails()
    assert not any("sim" in m for _, m in rec.events)


def test_unvalidated_warning_names_the_sim_footprint_tradeoff(tmp_path, monkeypatch):
    """fpu F-20: enabling a selftest means putting a known-bad fixture
    somewhere, and nothing said so — the advisory read as a free win, so the
    port hand-proved its fail paths and left the WARN standing, unexplained."""
    _set_venue(monkeypatch, False)
    rec = _Rec()
    project = _audit(tmp_path, fixture=False)

    doctor._run_selftest_checks(project, _runtime(project, "docker"), rec.p, rec.w, rec.s, rec.f)

    warns = [m for lvl, m in rec.events if lvl == "warn"]
    sim_warn = next(m for m in warns if m.startswith("sim fail-path unvalidated"))
    # Simulation's project-owned overlay stays out of public Flow behavior.
    assert "Footprint:" in sim_warn
    assert ".booley/selftest/sim/bad-overlay/" in sim_warn


def test_footprint_note_exists_for_every_selftest_tool():
    """A Flow added to the self-test registry without a note would silently lose it."""
    assert set(doctor._SELFTEST_FLOWS) <= set(doctor._SELFTEST_FOOTPRINT_NOTE)


def _evidence_mutations(report, campaign, observation):
    return {
        "incomplete": (campaign, "complete", False),
        "wrong-exit": (report, "exit_code", 0),
        "wrong-flow": (report, "flow", "lint"),
        "wrong-schema": (report, "$schema", "booley.simulation-report/v2"),
        "truncated": (campaign, "observations_truncated", True),
        "rows-truncated": (campaign, "test_runs_truncated", True),
        "wrong-count": (campaign, "test_run_total", 2),
        "wrong-observation-count": (campaign, "observation_total", 2),
        "bool-count": (campaign, "test_run_total", True),
        "mismatched-pair": (observation, "test", "other"),
        "timeout": (observation, "execution", "timed_out"),
        "crash": (observation, "execution", "crashed"),
        "infra": (observation, "failure_class", "infrastructure"),
        "inconclusive": (observation, "functional", "inconclusive"),
        "nonfailing-row": (campaign["test_runs"][0], "grade", "pass"),
        "build-stage": (campaign, "build_stage", [{"failure_class": "infrastructure"}]),
        "wrong-variant": (campaign["test_runs"][0], "variant", "other"),
        "wrong-name": (campaign["test_runs"][0], "name", "other@1"),
        "malformed-detail": (report, "detail", []),
        "malformed-row": (campaign, "test_runs", [42]),
        "malformed-observation": (campaign, "observations", [42]),
    }


def _evidence_case(kind):
    report = _simulation_failure_report()
    campaign = report["detail"]["campaigns"]["sim_core"]
    observation = campaign["observations"][0]
    changes = _evidence_mutations(report, campaign, observation)
    if kind in changes:
        owner, key, value = changes[kind]
        owner[key] = value
    elif kind == "missing-rows":
        del campaign["test_runs"]
    elif kind == "wrong-target":
        report["detail"]["campaigns"]["other"] = report["detail"]["campaigns"].pop("sim_core")
    elif kind == "wrong-test":
        campaign["test_runs"][0]["test"] = observation["test"] = "other"
    elif kind == "build-failed":
        observation.update(
            execution="blocked_by_build",
            failure_class="design",
            functional="not_run",
            detail={"reason": "%Error: stale ports"},
        )
    elif kind == "unpaired-failures":
        second_row = dict(campaign["test_runs"][0], grade="pass")
        second_observation = dict(observation)
        observation["functional"] = "pass"
        campaign["test_runs"].append(second_row)
        campaign["observations"].append(second_observation)
        campaign.update(test_run_total=2, observation_total=2)
    elif kind == "dirty-assertions":
        observation.update(functional="pass", assertions="dirty")
    return report


@pytest.mark.parametrize(
    "kind",
    [
        "behavioral",
        "dirty-assertions",
        "build-failed",
        "missing",
        "malformed",
        "scalar",
        "incomplete",
        "wrong-exit",
        "wrong-flow",
        "wrong-schema",
        "truncated",
        "rows-truncated",
        "wrong-count",
        "wrong-observation-count",
        "bool-count",
        "mismatched-pair",
        "timeout",
        "crash",
        "infra",
        "inconclusive",
        "nonfailing-row",
        "build-stage",
        "missing-rows",
        "wrong-target",
        "wrong-test",
        "wrong-variant",
        "wrong-name",
        "malformed-detail",
        "malformed-row",
        "malformed-observation",
        "unpaired-failures",
        "stale",
    ],
)
def test_sim_bad_requires_own_selected_behavioral_evidence(tmp_path, monkeypatch, kind, capsys):
    project = _audit(tmp_path)
    _set_venue(monkeypatch, True)
    path = doctor._selftest_report_path(project)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_simulation_failure_report()))

    def run(cmd, **kwargs):
        bad = kwargs["env"][selftest_overlay.INTERNAL_KIND_ENV] == "bad"
        if bad:
            assert not path.exists(), "prior report must be removed before executing bad case"
            if kind not in {"missing", "stale"}:
                text = (
                    "{"
                    if kind == "malformed"
                    else json.dumps(42 if kind == "scalar" else _evidence_case(kind))
                )
                path.write_text(text)
        return subprocess.CompletedProcess(cmd, 1 if bad else 0, "", "")

    monkeypatch.setattr(doctor.subprocess, "run", run)
    reporter, tracker = doctor._Reporter.create(), doctor.doctor_deep.DeepCheckTracker()
    doctor._run_selftest_checks(
        project,
        _runtime(project),
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
        completeness=tracker,
    )
    accepted = kind in {"behavioral", "dirty-assertions"}
    assert tracker.missing == (() if accepted else ("sim.selftest.bad",))
    assert reporter.counts["fail"] == 0
    assert reporter.counts["warn"] == (0 if accepted else 1)
    if not accepted:
        expected = (
            "flow.selftest-overlay-build-failed"
            if kind == "build-failed"
            else "flow.selftest-evidence-unvalidated"
        )
        assert reporter.findings[-1].check_id == expected
    if kind == "build-failed":
        output = capsys.readouterr().out
        assert str(project.booley_dir / "selftest/sim/bad-overlay") in output
        assert "%Error: stale ports" in output


@pytest.mark.parametrize(
    "selected,test,variant",
    [
        (None, None, None),
        (None, "discovered_cocotb", None),
        ("cocotb_test", "cocotb_test", None),
        ("hello_world+fast", "hello_world", "fast"),
    ],
)
def test_selected_test_identity_accepts_actual_failure(tmp_path, selected, test, variant):
    project = _audit(tmp_path)
    path = doctor._selftest_report_path(project)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_simulation_failure_report(test=test, variant=variant)))
    rec = _Rec()
    accepted = doctor._grade_sim_bad_selftest(
        project,
        doctor._SelftestCase("sim_core", selected, "case"),
        subprocess.CompletedProcess([], 1, "", ""),
        "bad sim",
        rec.p,
        rec.w,
    )
    assert accepted
    assert rec.events == [("pass", "bad sim correctly graded a failure")]


def test_waived_runtime_evidence_warning_still_incomplete(tmp_path, monkeypatch):
    from booley.audit.doctor_waivers import load_doctor_waivers

    project = _audit(tmp_path)
    (project.booley_dir / "doctor-waivers.toml").write_text(
        'version=1\n[[waiver]]\ncheck="flow.selftest-evidence-unvalidated"\n'
        'reason="Reviewed exclusion"\npermanent=true\n'
    )
    _set_venue(monkeypatch, True)
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 1 if kw["env"][selftest_overlay.INTERNAL_KIND_ENV] == "bad" else 0, "", ""
        ),
    )
    reporter = doctor._Reporter.create(load_doctor_waivers(project.booley_dir))
    tracker = doctor.doctor_deep.DeepCheckTracker()
    doctor._run_selftest_checks(
        project,
        _runtime(project),
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
        completeness=tracker,
    )
    assert reporter.result(0).clean
    assert reporter.counts["waived"] == 1
    assert tracker.missing == ("sim.selftest.bad",)


def test_unremovable_report_fails_without_executing_bad_case(tmp_path, monkeypatch):
    project = _audit(tmp_path)
    doctor._selftest_report_path(project).mkdir(parents=True)
    _set_venue(monkeypatch, True)
    executions = []

    def run(cmd, **kwargs):
        executions.append(kwargs["env"][selftest_overlay.INTERNAL_KIND_ENV])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(doctor.subprocess, "run", run)
    rec = _Rec()
    doctor._run_selftest_checks(project, _runtime(project), rec.p, rec.w, rec.s, rec.f)
    assert executions == ["good"]
    assert any("cannot clear prior report" in message for message in rec.fails())
