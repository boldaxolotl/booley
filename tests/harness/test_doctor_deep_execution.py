"""Applicable execution is required even when health warnings are waived."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import booley
from booley.audit.diagnostic_results import DiagnosticReport, Severity
from booley.config import agent as agent_config
from booley.core.models import AgentResult
from booley.harness import doctor
from booley.harness import doctor_deep as deep
from booley.runtime import auth_token, health, issuance_invalidation
from booley.runtime.agent_errors import UsageLimitError, model_unavailable_error
from booley.runtime.runtime_context import RuntimeLocation
from tests.agent_call_fake import install_agent
from tests.booley_checkouts_support import LAYOUTS, linked_worktree
from tests.docker_cli_guard import route_bounded_capture_through_subprocess_run
from tests.flows.sim.test_verilator_build_reuse_record import flow_project
from tests.harness.doctor_host_support import (
    answer_doctor_commands,
    raise_child_peak_rss,
    serve_healthy_doctor_host,
)
from tests.harness.doctor_run_support import (
    git_init,
    run_doctor,
    runtime_setup,
    seed_sandbox_files,
    write_project,
)
from tests.harness.git_support import git_stdout
from tests.harness.test_doctor_selftest_builtin import _write_sim_bad_report
from tests.sandbox_venue_fake import place


@pytest.fixture(autouse=True)
def _doctor_probes_use_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """This module fakes the doctor probes through ``subprocess.run``."""
    route_bounded_capture_through_subprocess_run(monkeypatch, doctor)


__all__ = ["flow_project"]

IMAGE = "sha256:" + "a" * 64
_RELATIVE_WORKTREES = "worktree.useRelativePaths"


# The two alias tests below run a real Simulation Flow in-process for each Doctor Flow command, at
# the `_run_flow_audit` / `_run_deep_checks` seam with `_DoctorFlowRuntime` (the 8 private
# references, each commented): a whole Doctor run would also launch lint, synth, core resolution and
# the agent probes, which have no in-process stand-in, and the Project alias exists only inside an
# issued Sandbox mount, so `_DoctorFlowRuntime` is where each test chooses the venue.
def _alias_doctor_project(flow_project, tmp_path, monkeypatch, host, sim=""):
    import shutil
    import tomllib

    from booley.harness.setup.readiness import ProjectAudit
    from tests.conftest import require_symlinks, symlink_or_skip

    require_symlinks(tmp_path)
    data = tmp_path / "external-project-data"
    shutil.copytree(flow_project / ".booley", data)
    alias = tmp_path / "booley"
    symlink_or_skip(alias, data, target_is_directory=True)
    config = data / "booley.toml"
    config.write_text(
        f"[flows.sim]\nenabled=true\n{sim}[flows.lint]\nenabled=false\n"
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
    git_init(flow_project)
    git_init(data)
    audit = ProjectAudit(flow_project, data, tomllib.loads(config.read_text()), "sim")
    monkeypatch.setattr(doctor.dc, "BOOLEY_DIR_TARGET", str(alias))
    from tests.runtime.test_sandbox_layout import configure_project_alias

    configure_project_alias(monkeypatch, alias, data)
    monkeypatch.setattr(doctor._DoctorFlowRuntime, "inside", property(lambda _self: not host))

    def command(_self, inner, *, env=None):
        """Inside the Sandbox only the subprocess env reaches the Flow; from the host, docker -e."""
        if not host:
            return inner
        pairs = [part for name, value in (env or {}).items() for part in ("-e", f"{name}={value}")]
        return ["docker", "exec", *pairs, "booley-session", *inner]

    monkeypatch.setattr(doctor._DoctorFlowRuntime, "command", command)
    return audit, data, alias


def _real_simulation_transport(monkeypatch, flow_project, alias, data, host):
    from pathlib import Path

    from booley.flows.sim.flow import SimulateFlow

    real_run = subprocess.run
    executions = []
    # What each Doctor Flow run saw: its argv values, its environment, and the
    # Common Booley Directory its Resource History would be written to.
    launches = []

    def run(argv, **kwargs):
        if "booley.flows.sim" not in argv:
            return real_run(argv, **kwargs)
        values = list(argv[argv.index("booley.flows.sim") + 1 :])
        emitted = Path(values[values.index("--report-dir") + 1])
        assert emitted.is_relative_to(alias if host else data)
        values[values.index("--work-dir") + 1] = str(flow_project)
        env = dict(kwargs.get("env", {}))
        # A containerized Flow receives the docker ``-e NAME=VALUE`` pairs.
        pairs = [argv[i + 1] for i, part in enumerate(argv) if part == "-e"]
        env.update(pair.partition("=")[::2] for pair in pairs)
        launches.append((values, env))
        with monkeypatch.context() as scoped:
            for name, value in env.items():
                scoped.setenv(name, value)
            flow = SimulateFlow()
            outcome = flow.execute_cli(values).outcome
            from booley.runtime.booley_dir import resolve_common_booley_dir

            launches[-1] += (resolve_common_booley_dir(),)
        executions.append(outcome)
        return subprocess.CompletedProcess(argv, outcome.exit_code, outcome.report_text, "")

    monkeypatch.setattr(doctor.subprocess, "run", run)
    return executions, launches


@pytest.mark.parametrize("run_cwd", ["missing", "existing-literal", "templated"])
def test_plain_doctor_sim_dry_run_checks_literal_run_cwd(
    flow_project, tmp_path, monkeypatch, run_cwd, capsys, request
):
    from booley.presentation import colors
    from booley.runtime.booley_dir import reset_cache

    reset_cache()
    request.addfinalizer(reset_cache)
    configured = "runs/{attempt}" if run_cwd == "templated" else "run"
    audit, data, alias = _alias_doctor_project(
        flow_project, tmp_path, monkeypatch, False, sim=f'run_cwd = "{configured}"\n'
    )
    if run_cwd == "existing-literal":
        (flow_project / "run").mkdir()
    _real_simulation_transport(monkeypatch, flow_project, alias, data, False)
    reporter = doctor._Reporter.create()
    doctor._run_flow_audit(
        audit,
        doctor._DoctorFlowRuntime(flow_project, "docker"),
        reporter.pass_,
        reporter.note_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
        _diagnostics=reporter.diagnostics,
    )
    output = colors.strip_ansi(capsys.readouterr().out)
    if run_cwd == "missing":
        assert "✗  sim dry-run [sim] failed with exit 2" in output
        assert "literal run directory must already exist" in output
        assert "sim.run-cwd-missing" in {item.check_id for item in reporter.findings}
    else:
        assert "✓  sim dry-run [sim] passed" in output
        assert "sim dry-run [sim] failed" not in output


def _assert_real_bad_simulation_report(outcome, build_failure):
    simulation_record = outcome.detail["records"]["sim"]
    assert simulation_record["complete"] is True
    assert simulation_record["test_run_total"] == simulation_record["observation_total"] == 1
    assert simulation_record["test_runs"][0]["grade"] == "fail"
    observation = simulation_record["observations"][0]
    assert simulation_record["test_runs"][0]["test"] == observation["test"] == "first"
    if build_failure:
        assert observation["execution"] == "blocked_by_build"
        assert observation["failure_class"] == "design"
        assert "%Error: BAD source" in observation["detail"]["reason"]
    else:
        assert simulation_record["build_stage"] == []
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
        # The Flow reads the checkout's own Booley Directory (a copy here, the alias in
        # a real Sandbox), so the fixture changes both.
        for booley_dir in (data, flow_project / ".booley"):
            (booley_dir / "selftest/sim/bad-overlay/src/acme_lib_demo_1/rtl/tb.sv").write_text(
                "module tb;\n// [SIM_RESULT] FAILED\nendmodule\n"
            )
    executions, launches = _real_simulation_transport(monkeypatch, flow_project, alias, data, host)
    reporter, tracker = doctor._Reporter.create(), deep.DeepCheckTracker()
    doctor._run_deep_checks(
        audit,
        doctor._DoctorFlowRuntime(flow_project, "docker"),
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
        _diagnostics=reporter.diagnostics,
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
    reports = data / doctor.DOCTOR_TMP_DIR / "flow-reports"
    assert len(list(reports.glob("sim/*/targets/sim/record/manifest.json"))) == 3
    assert len(list(reports.glob("sim/*/progress.json"))) == 3
    _assert_doctor_runs_write_no_history(monkeypatch, launches)


def _assert_doctor_runs_write_no_history(monkeypatch, launches) -> None:
    """Doctor's three Flow runs add no Resource History record; the same run outside Doctor does."""
    from booley.flows.sim.flow import SimulateFlow
    from booley.runtime import resource_history

    assert len(launches) == 3
    for _values, _env, history_dir in launches:
        assert resource_history.load_records(history_dir) == []
    values, env, history_dir = launches[0]
    with monkeypatch.context() as scoped:
        for name, value in env.items():
            scoped.setenv(name, value)
        scoped.delenv("BOOLEY_INTERNAL_DOCTOR_RUN")
        assert SimulateFlow().execute_cli(values).outcome.exit_code == 0
    # (The control reuses Doctor's retained image, so it records no build.)
    phases = {record["key"]["phase"] for record in resource_history.load_records(history_dir)}
    assert "sim_run" in phases


def _project(root):
    directory = write_project(root, seed_interactive=False)
    return SimpleNamespace(
        project_root=root,
        booley_dir=directory,
        common_booley_dir=directory,
        booley_toml={"flows": {}},
    )


def _host_project(root, monkeypatch, build=None):
    """A Project (from *build*) in a real Git repository, on a host that answers as healthy."""
    git_init(root)
    project = (build or _project)(root)
    seed_sandbox_files(root)
    serve_healthy_doctor_host(monkeypatch, root)
    return project


def _deep_run(project, *, skip_agents=False):
    """A whole deep Doctor run that records its attempt; return the result and the record."""
    result = run_doctor(
        project.project_root, deep=True, skip_agent_checks=skip_agents, record_clean=True
    )
    return result, deep.load_deep_record(project.booley_dir)


def test_missing_targets_are_incomplete_even_without_health_warning(tmp_path, monkeypatch):
    project = _host_project(tmp_path, monkeypatch)
    # No Target carries a `doctor:` marker, and the optional FPGA Flow is configured.
    core = tmp_path / "unit.core"
    core.write_bytes(re.sub(rb", booley: \{[^}]*\}", b"", core.read_bytes()))
    config = project.booley_dir / "booley.toml"
    config.write_text(config.read_text(encoding="utf-8") + "\n[flows.fpga]\n", encoding="utf-8")

    result, record = _deep_run(project)

    assert {
        "sim.doctor-targets",
        "lint.doctor-targets",
        "synth.doctor-targets",
        "fpga.doctor-targets",
        "sim.selftest.good",
        "lint.selftest.bad",
    } <= set(record.reasons)
    skipped = [f.message for f in result.findings if "no Doctor Target selected" in f.message]
    assert skipped == [
        f"{flow} deep check skipped - no Doctor Target selected"
        for flow in ("sim", "lint", "synth", "fpga")
    ]


@pytest.mark.parametrize(
    ("identity", "skip_agents", "reasons"),
    [
        (f"{IMAGE}\nother\n", False, {"booley-version-mismatch"}),
        (f"\n{booley.__version__}\n", False, {"image-identity-unavailable"}),
        (f"{IMAGE}\nnot a version!\n", False, {"sandbox-version-unavailable"}),
        (f"{IMAGE}\n{booley.__version__}\n", False, set()),
        (f"{IMAGE}\n{booley.__version__}\n", True, {"agent-checks"}),
    ],
    ids=["version-mismatch", "no-image", "bad-version", "clean", "agents-skipped"],
)
def test_deep_attempt_names_what_it_could_not_qualify(
    tmp_path, monkeypatch, identity, skip_agents, reasons
):
    """The Sandbox identity probe and the omitted agent checks decide what a run qualifies."""
    project = _host_project(tmp_path, monkeypatch)

    def answer(argv, _env):
        # Only the probe that enters the Sandbox container reports its identity.
        if "booley-session-test" in argv and argv[-4:-1] == ["python3", "-I", "-c"]:
            return subprocess.CompletedProcess(argv, 0, identity, "")
        return None

    answer_doctor_commands(monkeypatch, answer)
    _result, record = _deep_run(project, skip_agents=skip_agents)

    named = {
        "booley-version-mismatch",
        "image-identity-unavailable",
        "sandbox-version-unavailable",
        "agent-checks",
    }
    assert named & set(record.reasons) == reasons
    # A host run makes no developer probe, so it can never qualify.
    assert "developer-probe" in record.reasons
    assert record.image_id == (IMAGE if identity.startswith(IMAGE) else None)


# sha256(selector)[:16], the stable completeness identifier Doctor requires per Target.
_SIM_OK_ID = "core.resolve.1527659c7ec1081d"
_SIM_BAD_ID = "core.resolve.6febf8d38b2fe65d"


def _core_resolution_project(root):
    """A Project whose Doctor selects ``sim_ok`` and ``sim_bad`` but not ``sim_vendor``."""
    project = _project(root)
    selected = "flow: sim\n    flow_options: {tool: verilator, booley: {doctor: [sim]}}\n"
    unselected = "flow: sim\n    flow_options: {tool: verilator}\n"
    shape = "    filesets: [rtl, tb]\n    toplevel: tb\n"
    (root / "unit.core").write_bytes(
        (
            "CAPI=2:\nname: ::unit:0\nfilesets:\n"
            "  rtl:\n    files:\n      - rtl/dut.sv: {file_type: systemVerilogSource}\n"
            "  tb:\n    files:\n      - tb/tb.sv: {file_type: systemVerilogSource}\n"
            "    tags: [tb]\ntargets:\n"
            f"  sim_ok:\n    {selected}{shape}"
            f"  sim_bad:\n    {selected}{shape}"
            f"  sim_vendor:\n    {unselected}{shape}"
        ).encode()
    )
    return project


@pytest.mark.parametrize("runtime", ["available", "recovery-pending"])
def test_core_resolution_requires_and_completes_one_identifier_per_selected_target(
    tmp_path, monkeypatch, runtime
):
    """Deep completeness names exactly the selected Targets and completes each verdict."""
    project = _host_project(tmp_path, monkeypatch, _core_resolution_project)
    if runtime == "recovery-pending":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        issuance_invalidation.prepare(str(tmp_path.resolve()))
    payloads = []

    def answer(argv, _env):
        if "[[CORE_RESOLVE_JSON]]" not in argv[-2]:
            return None
        payload = json.loads(argv[-1])
        payloads.append([item["selector"] for item in payload])
        verdicts = [
            {"selector": item["selector"], "ok": item["selector"] == "sim_ok", "err": "no"}
            for item in payload
        ]
        out = "[[CORE_RESOLVE_JSON]]" + json.dumps(verdicts)
        return subprocess.CompletedProcess(argv, 0, out, "")

    answer_doctor_commands(monkeypatch, answer)
    result, record = _deep_run(project)

    rows = {(f.severity.value, f.message) for f in result.findings if ".core" in f.message}
    identifiers = {_SIM_OK_ID, _SIM_BAD_ID}
    if runtime == "available":
        assert payloads == [["sim_bad", "sim_ok"]], "the unselected Target is never resolved"
        assert {
            ("pass", ".core Target 'sim_ok' resolves"),
            ("fail", ".core Target 'sim_bad' fails to resolve"),
        } <= rows
        assert identifiers.isdisjoint(record.reasons)
    else:
        assert payloads == [], "an unavailable runtime dispatches nothing"
        skip = (
            "skip",
            ".core resolvability skipped - Sandbox recovery is pending on this host",
        )
        assert skip in rows
        assert identifiers <= set(record.reasons)


_MODEL_CONFIG = """
[agent]
clis = ["claude", "codex"]
default = "claude"

[models]
reviewer = "gpt-6-astra"
coverage_analyst = "claude-opus-5-5"
"""
_MODEL_LIST_CONFIG = _MODEL_CONFIG.replace(
    '"gpt-6-astra"', '["claude-opus-5-5", "gpt-6-luna"]'
).replace(
    'coverage_analyst = "claude-opus-5-5"', 'coverage_analyst = ["claude-opus-5-5", "gpt-6-luna"]'
)
_INTERNAL = "claude-sonnet-5-5"
_ACCEPTED = AgentResult(input_tokens=5, output_tokens=1, cost_usd=0.001)


def _model_probe_run(
    tmp_path,
    monkeypatch,
    request,
    *,
    config=_MODEL_CONFIG,
    refused=(),
    failing=(),
    gateway=False,
    scratch_gone=False,
):
    """A whole deep Doctor run in the Sandbox whose Specialists run on the models in *config*.

    reviewer on Codex (gpt-6-astra) and coverage_analyst on Claude (claude-opus-5-5); the
    internal model (claude-sonnet-5-5) is the memory probe's own. The provider *refuses* the
    models in *refused* as the backends do and fails the calls on those in *failing*. With
    *scratch_gone* the temp directory vanishes after the memory probe, before any model probe.
    Returns the result, the model probes' models, every agent call and the deep record.
    """
    git_init(tmp_path)
    runtime_setup(tmp_path, monkeypatch, inside=True)
    booley_dir = tmp_path / ".booley"
    toml = booley_dir / "booley.toml"
    # The fixture's own `[agent]` table would duplicate the one in *config*.
    base = toml.read_text(encoding="utf-8").replace('[agent]\nauth = "subscription"\n', "")
    toml.write_text(base + config, encoding="utf-8")
    for name in (
        "BOOLEY_PRIMARY_AUTH",
        "BOOLEY_PRIMARY_PROVIDER",
        "BOOLEY_GATEWAY_MODEL",
        "BOOLEY_GATEWAY_EFFORT",
    ):
        monkeypatch.delenv(name, raising=False)
    if gateway:
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
        monkeypatch.setenv("BOOLEY_GATEWAY_URL", "https://gateway.example/v1")
        monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", "SYNTHETIC_GATEWAY_TOKEN")
    agent_config.set_agent_settings(None)
    request.addfinalizer(lambda: agent_config.set_agent_settings(None))

    def answer(params):
        if scratch_gone and params.label == "doctor-developer-probe":
            monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "gone"))
        if params.model in refused:
            raise model_unavailable_error(params.model, params.agent_cli, "raw provider body")
        if params.model in failing:
            raise RuntimeError("boom")
        return _ACCEPTED

    calls = install_agent(monkeypatch, answer).calls
    raise_child_peak_rss(monkeypatch)
    result, record = _deep_run(SimpleNamespace(project_root=tmp_path, booley_dir=booley_dir))
    probed = [call.model for call in calls if call.label == "doctor-model-probe"]
    return result, probed, calls, record


def _messages(result, severity, text):
    return [f.message for f in result.findings if f.severity is severity and text in f.message]


@pytest.mark.parametrize(
    ("row", "probed", "failures", "incomplete"),
    [
        ("accepted", ["claude-opus-5-5", "gpt-6-astra"], [], ()),
        (
            "refused",
            ["claude-opus-5-5", "gpt-6-astra"],
            [
                (
                    "codex provider refused model 'gpt-6-astra'",
                    "`[models] reviewer`",
                    "Protected Input",
                )
            ],
            (),
        ),
        (
            "internal-refused",
            ["gpt-6-astra"],
            [(_INTERNAL, "[models] coverage_analyst")],
            ("developer-probe",),
        ),
        ("provider-probe-failed", [], [], ("developer-probe", "model-access")),
        (
            "model-probe-failed",
            None,
            [("probe could not complete a trivial call: model probe failed: boom",)],
            ("model-access",),
        ),
        ("gateway", ["claude-opus-5-5"], [], ()),
        ("scratch-unusable", [], [], ("model-access",)),
    ],
)
def test_deep_probes_each_specialist_model(
    tmp_path, monkeypatch, request, row, probed, failures, incomplete
):
    config = _MODEL_CONFIG
    refused, failing = (), ()
    if row == "refused":
        refused = ("gpt-6-astra",)
    elif row == "internal-refused":
        # coverage_analyst runs on the internal model, so its key is the one a refusal names.
        config, refused = (
            _MODEL_CONFIG.replace('"claude-opus-5-5"', f'"{_INTERNAL}"'),
            (_INTERNAL,),
        )
    elif row == "provider-probe-failed":
        failing = (_INTERNAL,)
    elif row == "model-probe-failed":
        failing = ("claude-opus-5-5", "gpt-6-astra")
    result, seen, calls, record = _model_probe_run(
        tmp_path,
        monkeypatch,
        request,
        config=config,
        refused=refused,
        failing=failing,
        gateway=row == "gateway",
        scratch_gone=row == "scratch-unusable",
    )

    # The internal model is covered by the memory probe; every other model gets one call.
    if probed is None:
        assert len(seen) == 1, "a failed model probe spends nothing more"
    else:
        assert sorted(seen) == probed
    assert len(calls) == 1 + len(seen)
    assert {"developer-probe", "model-access"} & set(record.reasons) == set(incomplete)
    found = [f for f in result.findings if f.severity is Severity.FAIL and "model" in f.message]
    assert len(found) == len(failures)
    for finding, texts in zip(found, failures, strict=True):
        assert all(text in finding.message for text in texts)
    if row == "internal-refused":
        assert "check agent auth" not in found[0].fix
    if row == "scratch-unusable":
        # No model call was made, so the probe skips and stops instead of failing.
        assert len(_messages(result, Severity.SKIP, "probe skipped - model probe failed")) == 1
    if row == "accepted":
        passed = " ".join(_messages(result, Severity.PASS, "accepted by the provider"))
        assert "gpt-6-astra accepted by the provider ([models] reviewer)" in passed
        assert "claude-opus-5-5 accepted by the provider ([models] coverage_analyst)" in passed


@pytest.mark.parametrize(
    ("row", "logged_in", "refused", "probed"),
    [
        ("first-accepted", ("claude", "codex"), (), ["claude-opus-5-5"]),
        (
            "refused-falls-back",
            ("claude", "codex"),
            ("claude-opus-5-5",),
            ["claude-opus-5-5", "gpt-6-luna"],
        ),
        ("no-login-falls-back", ("codex",), (), ["gpt-6-luna"]),
        (
            "none-works",
            ("claude", "codex"),
            ("claude-opus-5-5", "gpt-6-luna"),
            ["claude-opus-5-5", "gpt-6-luna"],
        ),
    ],
)
def test_deep_probes_each_model_list_entry(
    tmp_path, monkeypatch, request, row, logged_in, refused, probed
):
    """A Specialist's model list passes on its first entry, warns on a fallback, fails with none."""
    # Only *logged_in* have a login, as the model fallback list sees it.
    monkeypatch.setattr(auth_token, "cli_logged_in", lambda cli, *_a, **_kw: cli in logged_in)

    result, seen, _calls, record = _model_probe_run(
        tmp_path, monkeypatch, request, config=_MODEL_LIST_CONFIG, refused=refused
    )

    assert seen == probed  # one probe serves both lists
    assert "model-access" not in record.reasons
    names = ("reviewer", "coverage_analyst")
    warnings = _messages(result, Severity.WARN, "Specialist model list for")
    failures = _messages(result, Severity.FAIL, "No model in")
    passes = " ".join(_messages(result, Severity.PASS, "Specialist model list for"))
    if row == "first-accepted":
        assert not warnings + failures
        for name in names:
            assert f"Specialist model list for [models] {name}: claude-opus-5-5 accepted" in passes
    elif row == "none-works":
        assert not warnings
        (reviewer,) = [f for f in failures if "`[models] reviewer`" in f]
        assert "No model in `[models] reviewer` could run" in reviewer
        assert "'claude-opus-5-5' (refused by the provider)" in reviewer
        assert "'gpt-6-luna' (refused by the provider)" in reviewer
        assert len(failures) == 2
    else:
        why = (
            "refused by the provider"
            if row == "refused-falls-back"
            else "Agent CLI claude is not logged in"
        )
        assert len(warnings) == 2
        assert not failures
        for warning in warnings:
            assert "falls back to gpt-6-luna" in warning
            assert f"claude-opus-5-5 ({why})" in warning


@pytest.mark.parametrize("early", ["scheduled", "changed", "quiet"])
def test_session_up_reports_deep_once_through_all_health_branches(
    tmp_path, monkeypatch, capsys, early
):
    from booley.harness import booley as cli

    # The Sandbox start and the three health reads below are replaced because the session step
    # reaches them only through Docker and the host's health log, which a test cannot provide.
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
    # Private: `main()` would resolve the host Session context through Docker first, so the test
    # enters the session step directly, as `tests/harness/test_booley.py` does.
    assert cli._session_up(argparse.Namespace(), tmp_path) == 0
    assert capsys.readouterr().out.count("deep Doctor due:") == 1
    observe.assert_called_once_with(tmp_path, container="sandbox")


def _qualification_project(tmp_path, monkeypatch, policy, missing, health_failure, data_policy):
    import tomllib

    from booley.harness.setup import readiness

    git_init(tmp_path)
    directory = write_project(tmp_path, seed_interactive=False)
    # Select exactly one real Simulation Target and exclude unrelated Flow families.
    core = tmp_path / "unit.core"
    core.write_text(core.read_text().split("  lint_fast:")[0])
    config = tomllib.loads((directory / "booley.toml").read_text())
    config["flows"]["lint"]["enabled"] = False
    config["flows"]["synth"]["enabled"] = False
    config["flows"]["sim"]["enabled"] = True
    audit = readiness.ProjectAudit(tmp_path, directory, config, "sim_fast")
    overlay = directory / "selftest" / "sim" / "bad-overlay"
    overlay.mkdir(parents=True)
    (overlay / "bad.sv").write_text("invalid fixture for boundary transport\n")
    # A fresh repository leaves the policy unset, which is the `None` row.
    if policy is not None:
        git_stdout(tmp_path, "config", "--local", _RELATIVE_WORKTREES, policy)
    if data_policy is not None:
        git_init(directory)
        git_stdout(directory, "config", "--local", _RELATIVE_WORKTREES, data_policy)
    place(monkeypatch, RuntimeLocation.ISSUED_SANDBOX)
    route_bounded_capture_through_subprocess_run(monkeypatch, readiness)
    # A qualifying (SUCCESS) outcome needs a clean run, and a whole Sandbox run on a test host
    # always reports the Sandbox Image's missing files and EDA tools. So these phases are replaced:
    # the outcome then reflects only the deep checks under test.
    monkeypatch.setattr(doctor, "_run_project_phase", lambda *_args, **_kw: (None, audit))

    def health(_project, _runtime, reporter, _progress):
        if health_failure:
            reporter.fail_("unrelated project health failed", "repair project")

    monkeypatch.setattr(doctor, "_run_flow_and_core_phase", health)
    for name in (
        "_check_runtime_location",
        "_check_sandbox_memory",
        "_check_sandbox_swap",
        "_run_container_checks",
        "_run_mcp_checks",
        "_run_project_checks",
    ):
        monkeypatch.setattr(doctor, name, lambda *_args, **_kw: None)
    monkeypatch.setenv("BOOLEY_SANDBOX_IMAGE_ID", IMAGE)

    def answer_call(_params):
        if missing == "developer":
            # A hit usage cap is the agent error that skips the probe instead of failing Doctor.
            raise UsageLimitError("usage cap reached", "claude")
        return AgentResult(input_tokens=1, output_tokens=1)

    install_agent(monkeypatch, answer_call)
    raise_child_peak_rss(monkeypatch)
    return audit


def _qualification_transport(monkeypatch, missing, project):
    commands = []

    def answer(argv, env):
        if argv[:2] == ["git", "--version"]:
            return subprocess.CompletedProcess(argv, 0, "git version 2.53.0\n", "")
        if argv[0] == "git":
            return None  # the Project lives in a real repository
        if len(argv) > 2 and argv[1] == "-c" and "[[CORE_RESOLVE_JSON]]" in argv[2]:
            commands.append("core")
            if missing == "core":
                return subprocess.CompletedProcess(argv, 0, "malformed core result", "")
            payload = json.loads(argv[-1])
            verdicts = [{"selector": item["selector"], "ok": True} for item in payload]
            return subprocess.CompletedProcess(
                argv, 0, "[[CORE_RESOLVE_JSON]]" + json.dumps(verdicts), ""
            )
        if "booley.flows.sim" in argv:
            kind = env.get(doctor.selftest_overlay.INTERNAL_KIND_ENV)
            commands.append(kind or "smoke")
            if kind == "bad":
                _write_sim_bad_report(argv, project=project)
            return subprocess.CompletedProcess(
                argv,
                1 if kind == "bad" else 0,
                "[SIM_RESULT] FAILED" if kind == "bad" else "[SIM_RESULT] PASSED",
                "",
            )
        raise AssertionError(f"unexpected external command: {argv}")

    answer_doctor_commands(monkeypatch, answer)
    return commands


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
    commands = _qualification_transport(monkeypatch, missing, audit)
    result = run_doctor(tmp_path, deep=True, record_clean=True)
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
    if any(not f.check_id for f in result.diagnostics.active_findings):
        blocking.add("unidentified-failure")
    if expected is deep.DeepOutcome.FAILED:
        extra = {"core.resolve.d97e8ce47f651215"} if missing == "core" else set()
        assert record.reasons == tuple(sorted(blocking | extra))
        assert [r.code for r in result.deep_status.reasons] == ["latest-attempt-failed"]
    elif expected is deep.DeepOutcome.INCOMPLETE:
        assert record.reasons == ("developer-probe", "model-access")
    else:
        assert not record.reasons
    assert result.deep_status.current is (expected is deep.DeepOutcome.SUCCESS)
    assert result.clean is (
        policy == "true" and data_policy != "false" and missing != "core" and not health_failure
    )
    assert commands == ["smoke", "good", "bad", "core"]


def test_pending_review_qualifies_once_and_ack_preserves_deep(tmp_path, monkeypatch):
    from booley.harness import upgrade_review
    from tests.harness.test_upgrade_review import _changelog

    monkeypatch.setattr(booley, "__version__", "2.0.0")
    audit = _qualification_project(tmp_path, monkeypatch, "true", None, False, None)
    upgrade_review.observe(audit.booley_dir, current_version="1.0.0")

    # Private: this phase is replaced as in `_qualification_project`, keeping only the upgrade
    # review the test is about.
    def project_phase(_root, reporter, **_kwargs):
        doctor._check_upgrade_review(audit.booley_dir, reporter)
        return None, audit

    monkeypatch.setattr(doctor, "_run_project_phase", project_phase)
    commands = _qualification_transport(monkeypatch, None, audit)
    plain = run_doctor(tmp_path, record_clean=True)
    assert [f.check_id for f in plain.diagnostics.active_findings] == ["upgrade.review-pending"]
    assert not plain.clean and plain.exit_code == 0
    result = run_doctor(tmp_path, deep=True, record_clean=True)
    assert not result.clean and result.exit_code == 0
    assert result.deep_status.current
    assert deep.load_deep_record(audit.booley_dir).outcome is deep.DeepOutcome.SUCCESS
    before = deep.deep_stamp_path(audit.booley_dir).read_bytes()
    upgrade_review.acknowledge(audit.booley_dir, "2.0.0", packaged_changelog=_changelog(tmp_path))
    final = run_doctor(tmp_path, record_clean=True)
    assert final.clean and final.deep_status.current
    assert deep.deep_stamp_path(audit.booley_dir).read_bytes() == before
    assert commands == ["smoke", "good", "bad", "core"]


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
    commands = _qualification_transport(monkeypatch, missing, audit)
    result = run_doctor(tmp_path, deep=True, record_clean=True)
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


@pytest.mark.parametrize("layout", LAYOUTS)
def test_core_resolve_snippet_builds_in_the_checkout_from_a_worktree(
    tmp_path, monkeypatch, layout
):
    """The in-Sandbox snippet keeps per-checkout build roots even when ``BOOLEY_DIR`` names main."""
    from booley.fusesoc import fusesoc_registry
    from booley.runtime import core_resolution
    from booley.targets.catalog import TargetCatalog

    _main, worktree = linked_worktree(tmp_path, monkeypatch, layout=layout, runtime="sandbox")
    build_roots = []
    monkeypatch.chdir(worktree)
    monkeypatch.setattr(
        TargetCatalog, "build", staticmethod(lambda _root: SimpleNamespace(select=lambda s: s))
    )
    monkeypatch.setattr(
        fusesoc_registry,
        "resolve_target_handle",
        lambda _handle, *, build_root: build_roots.append(build_root),
    )
    payload = json.dumps([{"selector": "sim_fast", "build_key": "key"}])
    monkeypatch.setattr("sys.argv", ["-c", payload])

    # Private: the snippet is source the Sandbox runs; no host entry executes it in-process.
    exec(compile(core_resolution._CORE_RESOLVE_SNIPPET, "<snippet>", "exec"), {})

    assert build_roots == [worktree / ".booley" / "runtime/doctor/core-resolve" / "key"]
