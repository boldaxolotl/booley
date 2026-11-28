"""--deep fail-path self-test must run on the BUILTIN flow (ADR 0039).

The QA-4/QA-5 fixture mechanism was gated `is_project_native`, so a builtin
project's conventional fail-path fixtures silently never ran. This was found
by the C910 re-port gate, where doctor --deep reported green without ever
proving the fail path. The gate is now backend-agnostic: only a disabled Flow
(enabled = false) skips.

Every test here is a whole deep Doctor run on the shared fixture Project; the
Flow launches are the answered boundary.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.fusesoc import selftest_overlay
from booley.harness import doctor
from booley.harness import doctor_deep as deep
from booley.runtime import issuance_invalidation
from tests.harness.doctor_host_support import serve_healthy_doctor_host
from tests.harness.doctor_run_support import (
    git_init,
    run_doctor,
    script_flow,
    write_bad_overlay,
    write_project,
)


def _simulation_failure_report(target="sim_fast", test="smoke", variant=None):
    name = test + (f"+{variant}" if variant else "") + "@1" if test else None
    return {
        "$schema": "booley.simulation-report/v3",
        "flow": "sim",
        "exit_code": 1,
        "detail": {
            "records": {
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


def _evidence_mutations(report, simulation_record, observation):
    return {
        "incomplete": (simulation_record, "complete", False),
        "wrong-exit": (report, "exit_code", 0),
        "wrong-flow": (report, "flow", "lint"),
        "wrong-schema": (report, "$schema", "booley.simulation-report/v2"),
        "truncated": (simulation_record, "observations_truncated", True),
        "rows-truncated": (simulation_record, "test_runs_truncated", True),
        "wrong-count": (simulation_record, "test_run_total", 2),
        "wrong-observation-count": (simulation_record, "observation_total", 2),
        "bool-count": (simulation_record, "test_run_total", True),
        "mismatched-pair": (observation, "test", "other"),
        "timeout": (observation, "execution", "timed_out"),
        "crash": (observation, "execution", "crashed"),
        "infra": (observation, "failure_class", "infrastructure"),
        "inconclusive": (observation, "functional", "inconclusive"),
        "nonfailing-row": (simulation_record["test_runs"][0], "grade", "pass"),
        "build-stage": (simulation_record, "build_stage", [{"failure_class": "infrastructure"}]),
        "wrong-variant": (simulation_record["test_runs"][0], "variant", "other"),
        "wrong-name": (simulation_record["test_runs"][0], "name", "other@1"),
        "malformed-detail": (report, "detail", []),
        "malformed-row": (simulation_record, "test_runs", [42]),
        "malformed-observation": (simulation_record, "observations", [42]),
    }


def _evidence_case(kind):
    report = _simulation_failure_report()
    simulation_record = report["detail"]["records"]["sim_fast"]
    observation = simulation_record["observations"][0]
    changes = _evidence_mutations(report, simulation_record, observation)
    if kind in changes:
        owner, key, value = changes[kind]
        owner[key] = value
    elif kind == "missing-rows":
        del simulation_record["test_runs"]
    elif kind == "wrong-target":
        report["detail"]["records"]["other"] = report["detail"]["records"].pop("sim_fast")
    elif kind == "wrong-test":
        simulation_record["test_runs"][0]["test"] = observation["test"] = "other"
    elif kind == "build-failed":
        observation.update(
            execution="blocked_by_build",
            failure_class="design",
            functional="not_run",
            detail={"reason": "%Error: stale ports"},
        )
    elif kind == "unpaired-failures":
        second_row = dict(simulation_record["test_runs"][0], grade="pass")
        second_observation = dict(observation)
        observation["functional"] = "pass"
        simulation_record["test_runs"].append(second_row)
        simulation_record["observations"].append(second_observation)
        simulation_record.update(test_run_total=2, observation_total=2)
    elif kind == "dirty-assertions":
        observation.update(functional="pass", assertions="dirty")
    elif kind == "retired-records-field":
        report["detail"]["campaigns"] = report["detail"].pop("records")
    return report


def _setup(tmp_path, monkeypatch, *, tests='["smoke", "full"]', overlay=True, sim_enabled=True):
    """A healthy whole-run Project; *tests* is the `sim_fast` list `tests.toml` selects from."""
    git_init(tmp_path)
    booley_dir = write_project(tmp_path)
    serve_healthy_doctor_host(monkeypatch, tmp_path)
    if tests is not None:
        (booley_dir / "tests.toml").write_text(f"[sim_fast]\ntests = {tests}\n", encoding="utf-8")
    if overlay:
        write_bad_overlay(booley_dir)
    if not sim_enabled:
        config = booley_dir / "booley.toml"
        text = config.read_text(encoding="utf-8")
        config.write_text(
            text.replace("[flows.sim]", "[flows.sim]\nenabled = false"), encoding="utf-8"
        )
    return booley_dir


def _report_path(booley_dir):
    return booley_dir / doctor.DOCTOR_TMP_DIR / "flow-reports" / "sim.json"


def _deep_run(booley_dir, monkeypatch, on_bad=None):
    """Run a whole deep Doctor, answering each self-test launch by kind.

    A good case exits 0; a bad case exits 1 after ``on_bad(argv, report_path)`` (by default
    the accepted failure report). Returns the result and one ``(flow, kind, argv, prior)`` per
    launch, *prior* being whether the sim report existed when it started.
    """
    report = _report_path(booley_dir)
    launches = []

    def answer(argv, env):
        kind = env.get(selftest_overlay.INTERNAL_KIND_ENV)
        if kind is None:
            return None
        flow = "sim" if "booley.flows.sim" in argv else "lint"
        launches.append((flow, kind, argv, report.exists()))
        if kind == "bad" and flow == "sim":
            (on_bad or _accepted_report)(argv, report)
        return subprocess.CompletedProcess(argv, 1 if kind == "bad" else 0, "", "")

    def _accepted_report(argv, _report):
        _write_sim_bad_report(argv, project=SimpleNamespace(booley_dir=booley_dir))

    script_flow(monkeypatch, "sim", answer)
    script_flow(monkeypatch, "lint", answer)
    result = run_doctor(booley_dir.parent, deep=True, record_clean=True)
    return result, launches


def _reasons(booley_dir):
    return deep.load_deep_record(booley_dir).reasons


def _sim_rows(result):
    return [
        (f.severity.value, f.check_id)
        for f in result.findings
        if "sim self-test" in f.message or "sim fail-path" in f.message
    ]


_KINDS = [
    "behavioral",
    "dirty-assertions",
    "build-failed",
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
    "retired-records-field",
    "stale",
    "missing-waived",
    "unremovable",
]
_ACCEPTED = {"behavioral", "dirty-assertions"}
_UNVALIDATED = ("warn", "flow.selftest-evidence-unvalidated")


@pytest.mark.parametrize("kind", _KINDS)
def test_sim_bad_case_requires_its_own_behavioral_failure(tmp_path, monkeypatch, capsys, kind):
    """The bad case passes only on its own selected behavioral failure, never a stale report."""
    booley_dir = _setup(tmp_path, monkeypatch)
    report = _report_path(booley_dir)
    report.parent.mkdir(parents=True)
    if kind == "unremovable":
        report.mkdir()
    else:
        report.write_text(json.dumps(_simulation_failure_report()))
    if kind == "missing-waived":
        (booley_dir / "doctor-waivers.toml").write_text(
            'version=1\n[[waiver]]\ncheck="flow.selftest-evidence-unvalidated"\n'
            'reason="Reviewed exclusion"\npermanent=true\n'
        )

    def on_bad(_argv, path):
        if kind in {"stale", "missing-waived"}:
            return  # the Flow leaves no report behind
        text = (
            "{"
            if kind == "malformed"
            else json.dumps(42 if kind == "scalar" else _evidence_case(kind))
        )
        path.write_text(text)

    result, launches = _deep_run(booley_dir, monkeypatch, on_bad)

    good = ("pass", None)
    bad = {
        "build-failed": ("warn", "flow.selftest-overlay-build-failed"),
        "unremovable": ("fail", None),
        "behavioral": ("pass", None),
        "dirty-assertions": ("pass", None),
        "missing-waived": ("waived", "flow.selftest-evidence-unvalidated"),
    }.get(kind, _UNVALIDATED)
    rows = _sim_rows(result)
    assert rows[0] == good
    assert rows[1:] == [bad]
    assert [(flow, case) for flow, case, _argv, _prior in launches] == (
        [("sim", "good")] if kind == "unremovable" else [("sim", "good"), ("sim", "bad")]
    )
    # The prior report is gone before the bad case runs.
    priors = [prior for _flow, case, _argv, prior in launches if case == "bad"]
    assert priors == ([] if kind == "unremovable" else [False])
    assert ("sim.selftest.bad" in _reasons(booley_dir)) is (kind not in _ACCEPTED)
    assert result.counts["waived"] == int(kind == "missing-waived")
    if kind == "build-failed":
        output = capsys.readouterr().out
        assert str((booley_dir / "selftest/sim/bad-overlay").resolve()) in output
        assert "%Error: stale ports" in output
    if kind == "behavioral":
        tests = [argv[argv.index("--test") + 1] for _flow, _kind, argv, _prior in launches]
        assert tests == ["smoke", "smoke"]


@pytest.mark.parametrize("test", [None, "discovered_cocotb"], ids=["no-test", "discovered-test"])
def test_sim_bad_case_accepts_the_unselected_test_identity(tmp_path, monkeypatch, test):
    """Without a selected Test the bad case grades whichever Test the Flow discovered."""
    booley_dir = _setup(tmp_path, monkeypatch, tests=None)

    def on_bad(_argv, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_simulation_failure_report(test=test)))

    result, launches = _deep_run(booley_dir, monkeypatch, on_bad)

    assert _sim_rows(result) == [("pass", None), ("pass", None)]
    assert all("--test" not in argv for _flow, _kind, argv, _prior in launches)
    assert "sim.selftest.bad" not in _reasons(booley_dir)


@pytest.mark.parametrize(
    ("selected", "test", "variant", "accepted"),
    [
        ("cocotb_test", "cocotb_test", None, True),
        ("hello_world", "hello_world", "fast", False),
    ],
    ids=["test", "variant-not-selected"],
)
def test_sim_bad_case_grades_the_selected_test_and_variant(
    tmp_path, monkeypatch, selected, test, variant, accepted
):
    """The bad case is graded on the run carrying the selected Test and its Variant."""
    booley_dir = _setup(tmp_path, monkeypatch, tests=json.dumps([selected]))

    def on_bad(_argv, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_simulation_failure_report(test=test, variant=variant)))

    result, launches = _deep_run(booley_dir, monkeypatch, on_bad)

    assert [argv[argv.index("--test") + 1] for _f, _k, argv, _p in launches] == [selected] * 2
    assert _sim_rows(result)[1:] == [("pass", None) if accepted else _UNVALIDATED]
    assert ("sim.selftest.bad" in _reasons(booley_dir)) is not accepted


@pytest.mark.parametrize(
    ("case", "hints"),
    [
        ("sim", (".booley/selftest/sim/bad-overlay/", "Footprint:")),
        ("sim-waived", (".booley/selftest/sim/bad-overlay/",)),
        ("lint", ("lint_selftest_bad", "dedicated tracked .core")),
        ("lint-waived", ("lint_selftest_bad",)),
        ("lint-unmarked", ("booley.doctor_selftest metadata",)),
        ("sim-disabled", ()),
    ],
)
def test_missing_fail_path_fixture_warns_and_keeps_deep_due(tmp_path, monkeypatch, case, hints):
    """A waiver stops the warning counting, but the self-tests still cannot run (#4248)."""
    flow = case.split("-")[0]
    booley_dir = _setup(tmp_path, monkeypatch, overlay=False, sim_enabled=case != "sim-disabled")
    if case.endswith("waived"):
        (booley_dir / "doctor-waivers.toml").write_text(
            'version=1\n[[waiver]]\ncheck="flow.fail-path-unvalidated"\n'
            'reason="Reviewed exclusion"\npermanent=true\n'
        )
    if case == "lint-unmarked":
        (tmp_path / "unmarked.core").write_text(
            "CAPI=2:\nname: booley::unmarked:0\ntargets:\n  lint_selftest_bad:\n"
            "    flow: lint\n    flow_options: {tool: verilator}\n",
            encoding="utf-8",
        )

    result, launches = _deep_run(booley_dir, monkeypatch)

    assert launches == []
    reasons = _reasons(booley_dir)
    warnings = [f for f in result.findings if f.check_id == "flow.fail-path-unvalidated"]
    if case == "sim-disabled":
        assert [f.message.split()[0] for f in warnings] == ["lint"]
        assert not _sim_rows(result)
        assert not [reason for reason in reasons if reason.startswith("sim.selftest")]
        return
    (warning,) = [f for f in warnings if f.message.startswith(f"{flow} fail-path unvalidated")]
    assert warning.severity.value == ("waived" if case.endswith("waived") else "warn")
    for text in (
        "The setup agent authors the known-bad fixture.",
        f"deep Doctor stays due until {flow} has its known-bad fixture",
        *hints,
    ):
        assert text in warning.message
    assert {f"{flow}.selftest.good", f"{flow}.selftest.bad"} <= set(reasons)


def test_lint_selftest_runs_the_conventional_bad_target(tmp_path, monkeypatch):
    booley_dir = _setup(tmp_path, monkeypatch)
    (tmp_path / "doctor-selftest.core").write_text(
        "CAPI=2:\nname: booley::doctor-selftest:0\ntargets:\n  lint_selftest_bad:\n"
        "    flow: lint\n    flow_options: {tool: verilator, booley: {doctor_selftest: true}}\n",
        encoding="utf-8",
    )

    result, launches = _deep_run(booley_dir, monkeypatch)

    lint = [
        (kind, argv[argv.index("--target") + 1])
        for flow, kind, argv, _ in launches
        if flow == "lint"
    ]
    assert lint == [("good", "lint_fast"), ("bad", "lint_selftest_bad")]
    rows = [
        (f.severity.value, f.message) for f in result.findings if "lint self-test" in f.message
    ]
    assert rows == [
        ("pass", "lint self-test good case 'lint_fast' passes"),
        ("pass", "lint self-test bad case 'lint_selftest_bad' correctly graded a failure"),
    ]
    assert not [reason for reason in _reasons(booley_dir) if reason.startswith("lint.selftest")]


def test_pending_recovery_skips_both_sim_selftests(tmp_path, monkeypatch):
    """A pending Sandbox recovery skips the self-tests instead of failing them (#5253)."""
    booley_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    issuance_invalidation.prepare(str(tmp_path.resolve()))

    result, launches = _deep_run(booley_dir, monkeypatch)

    assert launches == []
    skipped = [
        (f.severity.value, f.message) for f in result.findings if "sim self-test" in f.message
    ]
    assert skipped == [
        ("skip", f"sim self-test {case} skipped - Sandbox recovery is pending on this host")
        for case in ("good case 'smoke'", "bad case 'smoke + bad overlay'")
    ]
    assert {"sim.selftest.good", "sim.selftest.bad"} <= set(_reasons(booley_dir))
