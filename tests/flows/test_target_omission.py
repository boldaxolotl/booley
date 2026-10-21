"""Omitted Target policy through public built-in invocation boundaries."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import io
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from booley.flows.builtin_cli import build_cli_parser, execute_cli
from booley.flows.cli_selection import CliInvocation
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintConfigResult, LintFlow
from booley.flows.plan import FlowPlan
from booley.flows.sim.flow import SimulateFlow, _resolve_sim_campaign_work_units
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.mcp import server
from booley.mcp.flow_adapter import flow_schema
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.targets.catalog import TargetCatalog

FLOWS = {"sim": SimulateFlow, "lint": LintFlow, "synth": AsicSynthesizeFlow, "fpga": FpgaImplFlow}
CASES = [(entry, flow) for entry in ("cli", "mcp") for flow in FLOWS]
CASE_IDS = [f"{entry}-{flow}" for entry, flow in CASES]
BASELINE_METHODS = {
    name: cls._run_baseline_configs for name, cls in FLOWS.items() if name in {"synth", "fpga"}
}
TOOLS = {"sim": "verilator", "lint": "verilator", "synth": "yosys", "fpga": "vivado"}


def write_targets(root: Path, flow: str, count: int = 1, *, core: str = "demo") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".booley").mkdir(exist_ok=True)
    (root / ".booley/booley.toml").write_text("", encoding="utf-8")
    targets = "".join(
        f"  chosen{index if count > 1 else ''}:\n"
        f"    flow: {flow}\n    flow_options: {{tool: {TOOLS[flow]}}}\n"
        "    filesets: []\n    toplevel: tb\n"
        for index in range(count)
    )
    # An incompatible declaration and hidden Doctor declaration never count.
    targets += (
        "  incompatible:\n    flow: unknown\n    filesets: []\n    toplevel: tb\n"
        f"  hidden:\n    flow: {flow}\n    flow_options:\n"
        f"      tool: {TOOLS[flow]}\n      booley: {{doctor_selftest: true}}\n"
        "    filesets: []\n    toplevel: tb\n"
    )
    (root / f"{core}.core").write_text(
        f"CAPI=2:\nname: acme:lib:{core}:1\ntargets:\n{targets}", encoding="utf-8"
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    from booley.runtime import runtime_context

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    for name in ("BOOLEY_STATE_FILE", "BOOLEY_TICKET_FILE", "BOOLEY_LOGS_DIR", "BOOLEY_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(server, "_run_subprocess", _transport_child)
    return tmp_path


@pytest.fixture
def goal_mode(tmp_path, monkeypatch):
    # Import these helpers during execution: importing another directory's
    # conftest at collection time bypasses pytest's conftest registration.
    from types import SimpleNamespace

    from tests.goals.conftest import enter_goals, layout

    lifetime = layout.__wrapped__(tmp_path, monkeypatch)
    project = next(lifetime)
    try:
        yield SimpleNamespace(**vars(project), record=enter_goals(project))
    finally:
        lifetime.close()


# The expensive preparation/EDA leaves use deterministic spies. Public _run,
# Target preparation, simulation preparation and catalog discovery remain real.
@pytest.fixture
def executed(monkeypatch):
    seen = []

    def run_sim(self, targets):
        seen.extend(handle.identity for handle in self._selected_target_handles())
        assert targets == [handle.selector for handle in self._selected_target_handles()]
        return EndpointOutcome(report_text="RESULT: PASS")

    def run_lint(self, targets, prepared):
        seen.extend(handle.identity for handle in targets)
        return (
            [
                LintConfigResult(target=h.selector, returncode=0, eda_tool="verilator")
                for h in targets
            ],
            [],
        )

    def run_implementation(self, targets, *args):
        seen.extend(handle.identity for handle in self._selected_target_handles())
        assert targets == [handle.selector for handle in self._selected_target_handles()]
        return {}

    monkeypatch.setattr(
        SimulateFlow, "_plan_elaboration", lambda self, targets: FlowPlan("sim", "elab-only", ())
    )
    monkeypatch.setattr(SimulateFlow, "_execute_elab_only", run_sim)
    monkeypatch.setattr(LintFlow, "_plan_lint", lambda self, targets: FlowPlan("lint", "lint", ()))
    monkeypatch.setattr(LintFlow, "_prepare_lint_targets", lambda self, targets: ({}, []))
    monkeypatch.setattr(LintFlow, "_run_all_targets", run_lint)
    for cls, name in ((AsicSynthesizeFlow, "synth"), (FpgaImplFlow, "fpga")):
        monkeypatch.setattr(
            cls,
            f"_plan_{name}_implementation",
            lambda self, targets: FlowPlan(self.name, "implementation", ()),
        )
        monkeypatch.setattr(cls, "_run_baseline_configs", lambda self, *args: ({}, None))
        monkeypatch.setattr(cls, "_run_current_targets", run_implementation)
        monkeypatch.setattr(
            cls,
            "_aggregate_results",
            lambda self, *args: EndpointOutcome(report_text="RESULT: PASS"),
        )
    monkeypatch.setattr(
        AsicSynthesizeFlow,
        "_resolve_run_policy",
        lambda self, enabled: None if enabled else "synth is disabled",
    )
    return seen


async def _transport_child(command, *, env=None, **kwargs):
    # Exercise the actual unbound runner route selected by the MCP dispatcher.
    module = command[command.index("-m") + 1]
    name = module.rsplit(".", 1)[-1]
    assert name in FLOWS and module == f"booley.mcp.{name}"
    argv = command[command.index(module) + 1 :]
    output, errors = io.StringIO(), io.StringIO()
    flow = FLOWS[name]()
    flow._cli_invocation = CliInvocation(transport=True)
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        try:
            result = execute_cli(flow, argv)
            code = result.exit_code
        except SystemExit as exc:
            code = exc.code
    return code, output.getvalue(), errors.getvalue(), False


def invoke(entry, name, project, arguments=None):
    params = {"work_dir": str(project), **(arguments or {})}
    if name == "sim" and "resume_from" not in params:
        params.setdefault("mode", "elab_only")
    if entry == "cli":
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                result = execute_cli(FLOWS[name](), server._params_to_argv(params))
                code = result.exit_code
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue() + errors.getvalue()

    validation_errors = list(
        Draft202012Validator(flow_schema(FLOWS[name]())).iter_errors(
            {key: value for key, value in params.items() if key != "work_dir"}
        )
    )
    if validation_errors:
        return 2, "invalid MCP parameters: " + str(validation_errors[0].message)

    async def scenario():
        jobs = server._JobManager(server._McpLifetime(None, None))
        result = await server._dispatch_booley_mcp_tool(
            name, params, {"module": name, "is_flow": True}, collections.defaultdict(int), jobs
        )
        if name in server._ASYNC_JOB_MCP_TOOLS:
            # A fast child may complete inline; always poll terminal jobs as well.
            await asyncio.wait_for(asyncio.gather(*jobs._tasks.values()), timeout=5)
            assert len(jobs._tasks) == 1
            run_id = next(iter(jobs._tasks))
            result = await server._dispatch_poll({"run_id": run_id}, jobs)
        error = isinstance(result, server.McpDispatchResult) and result.is_error
        raw = result.value if isinstance(result, server.McpDispatchResult) else result
        blocks = raw[0] if isinstance(raw, tuple) else raw
        text = "\n".join(block.text for block in blocks)
        return (2 if error else 0), text

    return asyncio.run(scenario())


@pytest.mark.parametrize(("entry", "name"), CASES, ids=CASE_IDS)
def test_omitted_target_selects_and_announces_only_eligible(entry, name, project, executed):
    write_targets(project, name)
    code, text = invoke(entry, name, project)
    assert code == 0 and executed == ["acme:lib:demo:1#chosen"], (
        "sole eligible Target was not executed: " + text
    )
    assert text.count(f"{name}: --target omitted; selected Target chosen.") == 1


@pytest.mark.parametrize(
    ("entry", "name", "count"),
    [(entry, name, count) for entry, name in CASES for count in (0, 2)],
    ids=[f"{entry}-{name}-{label}" for entry, name in CASES for label in ("zero", "many")],
)
def test_omitted_target_rejects_cardinality(entry, name, count, project, executed, monkeypatch):
    from booley.flows.flow_session import FlowSession

    admitted, started = [], []

    def acquire_slot(self):
        admitted.append(True)
        return None, None

    monkeypatch.setattr(FlowSession, "_acquire_job_slot", acquire_slot)
    monkeypatch.setattr(
        "booley.flows.endpoint_session._write_display_event", lambda *args: started.append(args)
    )
    write_targets(project, name, count)
    code, text = invoke(entry, name, project)
    choices = "(none)" if count == 0 else "chosen0, chosen1"
    assert f"Eligible Targets: {choices}" in text, "eligible Target list missing: " + text
    assert code == 2 and executed == []
    assert admitted == started == []
    assert "--target omitted; selected Target" not in text


@pytest.mark.parametrize("name", FLOWS)
def test_mcp_target_schema_is_optional(name):
    schema = flow_schema(FLOWS[name]())
    assert "target" not in schema.get("required", []), "target must be optional"
    assert not list(Draft202012Validator(schema).iter_errors({}))


@pytest.mark.parametrize("name", FLOWS)
def test_help_states_cardinality_rule(name):
    flow = FLOWS[name]()
    text = " ".join(build_cli_parser(flow).format_help().split())
    assert all(
        word in text for word in ("exactly one", "eligible", "announced", "zero", "multiple")
    ), "help omits cardinality rule"
    description = flow_schema(flow)["properties"]["target"]["description"]
    assert all(
        word in description
        for word in ("exactly one", "eligible", "announced", "zero", "multiple")
    )
    assert ("manifest" in text) == (name == "sim")


def test_omitted_sim_target_budget_counts_default_suite(project):
    write_targets(project, "sim")
    (project / ".booley/tests.toml").write_text(
        '[chosen]\ntests = ["first", "second", "third"]\nselect = "+test={name}"\n',
        encoding="utf-8",
    )
    explicit = _resolve_sim_campaign_work_units(project, "chosen")
    assert explicit == 3
    assert _resolve_sim_campaign_work_units(project, "") == explicit, (
        "auto-selected suite was under-budgeted"
    )
    assert (
        _resolve_sim_campaign_work_units(project, "", ["first@x4"])
        == _resolve_sim_campaign_work_units(project, "chosen", ["first@x4"])
        == 4
    )


@pytest.mark.parametrize("entry", ("cli", "mcp"))
def test_omitted_target_with_resume_is_accepted(entry, project, monkeypatch):
    from types import SimpleNamespace

    write_targets(project, "sim")
    handle = TargetCatalog.build(project).select("chosen", for_flow="sim")
    manifest = SimpleNamespace(
        document={"work_items": (), "required_suite": {"names": [], "default_invocation": True}}
    )
    validated = SimpleNamespace(
        target_handles=(handle,),
        manifest=manifest,
        path=project / "manifest.json",
        candidate=SimpleNamespace(manifest=manifest),
        binding_for=lambda document: SimpleNamespace(handle=handle),
    )
    # The authenticated manifest boundary already owns an exact Target. Stub
    # its disk authentication and expensive execution, keeping public preflight.
    monkeypatch.setattr(
        "booley.flows.sim.flow.validate_resume_manifest", lambda *args, **kwargs: validated
    )
    observed = []

    def resume(self, selected, admission):
        assert selected is validated
        observed.append(selected.target_handles[0].identity)
        return EndpointOutcome(report_text="RESULT: PASS")

    monkeypatch.setattr(SimulateFlow, "_run_campaign_resume", resume)
    monkeypatch.setattr(
        TargetCatalog, "list", lambda *args, **kwargs: pytest.fail("resume discovered catalog")
    )
    for count in (0, 2):
        write_targets(project, "sim", count)
        code, text = invoke(entry, "sim", project, {"resume_from": str(validated.path)})
        assert code == 0, text
        assert "--target omitted" not in text
    assert observed == [handle.identity, handle.identity]


def test_automatic_selection_reuses_prepared_snapshot(project, monkeypatch):
    write_targets(project, "lint")
    flow = LintFlow()
    flow.parse_args(["--work-dir", str(project)])
    assert flow.prepare_target_endpoint() is None
    first = flow._selected_target_selection()
    assert first.automatic and first.target_arg == flow.args.target == "chosen"
    monkeypatch.setattr(TargetCatalog, "build", lambda *args: pytest.fail("repeated discovery"))
    assert flow._selected_target_selection() is first
    assert first.matches(project, "chosen")


def test_reused_flow_discards_automatic_selection(project, executed):
    write_targets(project, "lint")
    flow = LintFlow()
    first_request = flow.request_type(target="", work_dir=project)
    first = flow.execute(first_request)
    first_selection = flow._prepared_target_selection
    other = project / "other"
    write_targets(other, "lint", core="other")
    second_request = flow.request_type(target="", work_dir=other)
    second = flow.execute(second_request)
    assert first.exit_code == second.exit_code == 0
    assert flow._prepared_target_selection is not first_selection
    assert flow._prepared_target_selection.catalog.project_root == other
    assert executed == ["acme:lib:demo:1#chosen", "acme:lib:other:1#chosen"]
    assert first_request.target == second_request.target == ""


@pytest.mark.parametrize("name", FLOWS)
def test_typed_omission_preserves_caller_request(name, project, executed, capsys):
    write_targets(project, name)
    flow = FLOWS[name]()
    extras = {"mode": "elab_only"} if name == "sim" else {}
    request = flow.request_type(target="", work_dir=project, **extras)
    result = flow.execute(request)
    assert result.exit_code == 0 and executed == ["acme:lib:demo:1#chosen"]
    assert request.target == ""
    assert "--target omitted" not in capsys.readouterr().err


@pytest.mark.parametrize("quiet", (True, False), ids=("quiet", "normal"))
def test_selection_notice_is_once_for_quiet_and_normal_calls(quiet, project, executed, capsys):
    write_targets(project, "lint")
    flow = LintFlow()
    argv = ["--work-dir", str(project)] + (["--quiet"] if quiet else [])
    result = flow.execute_cli(argv)
    flow.context._publish_console_report(result.outcome)
    captured = capsys.readouterr()
    notice = "lint: --target omitted; selected Target chosen."
    assert captured.err.count(notice) == 1
    assert notice not in captured.out
    assert captured.err.rstrip().endswith(notice)


@pytest.mark.parametrize("failure", ("binding", "simulation", "coverage"))
def test_selection_notice_survives_preparation_failure(
    failure, project, executed, monkeypatch, capsys
):
    from booley.flows.flow_session import FlowSession

    write_targets(project, "sim")
    if failure == "binding":
        monkeypatch.setattr(
            FlowSession,
            "_criterion_binding_gate",
            lambda self: EndpointOutcome(exit_code=2, report_text="binding refused"),
        )
    elif failure == "simulation":
        monkeypatch.setattr(
            SimulateFlow,
            "_validate_prepared_selection",
            lambda *args: EndpointOutcome(exit_code=2, report_text="simulation refused"),
        )
    else:
        monkeypatch.setattr(SimulateFlow, "_validate_prepared_selection", lambda *args: None)
        monkeypatch.setattr(
            SimulateFlow,
            "_prepare_coverage",
            lambda self: EndpointOutcome(exit_code=2, report_text="coverage refused"),
        )
    monkeypatch.setattr(
        FlowSession, "_acquire_job_slot", lambda self: pytest.fail("admitted refusal")
    )
    notice = "sim: --target omitted; selected Target chosen."
    flow = SimulateFlow()
    extra = ["--coverage"] if failure == "coverage" else []
    flow._cli_invocation = CliInvocation(transport=True)
    result = flow.execute_cli(["--work-dir", str(project), *extra])
    captured = capsys.readouterr()
    assert result.exit_code == 2 and executed == []
    assert captured.err.count(notice) == 1 and captured.err.rstrip().endswith(notice)
    SimulateFlow().execute(
        SimulateFlow.request_type(target="", work_dir=project, coverage=failure == "coverage")
    )
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_resume_budget_does_not_discover_unrelated_target(project, monkeypatch):
    context = server.resolve_call_context({"work_dir": str(project)})
    estimates = []
    monkeypatch.setattr(
        "booley.flows.sim.flow._resolve_sim_campaign_work_units",
        lambda *args, **kwargs: pytest.fail("resume sized unrelated Target"),
    )
    for count in (0, 1, 2):
        write_targets(project, "sim", count)
        estimates.append(
            server._sim_mcp_tool_timeout_seconds({"resume_from": "manifest.json"}, 0, context)
        )
    assert len(set(estimates)) == 1


@pytest.mark.parametrize("name", ("synth", "fpga"))
def test_automatic_candidate_selection_is_not_repeated_in_baseline(
    name, project, executed, monkeypatch
):
    import importlib

    from booley.flows.implementation_comparison import (
        candidate_execution_refs,
        target_pair_plans_for_handles,
    )

    write_targets(project, name)
    baseline = project.parent / (project.name + "-baseline")
    write_targets(baseline, name)
    core = baseline / "demo.core"
    core.write_text(
        core.read_text().replace(
            "  incompatible:",
            f"  extra:\n    flow: {name}\n    flow_options: {{tool: {TOOLS[name]}}}\n    filesets: []\n    toplevel: tb\n  incompatible:",
        )
    )
    flow = FLOWS[name]()
    flow.parse_args(["--work-dir", str(project), "--baseline", "old"])
    assert flow.prepare_target_endpoint() is None
    handles = flow._selected_target_handles()
    pairs = target_pair_plans_for_handles({}, "unused_", handles, flow=name)
    assert flow.args.target == "chosen"
    flow._target_handles = {h.selector: h for h in handles}
    flow._target_execution_refs = candidate_execution_refs(handles, pairs)
    module = importlib.import_module(FLOWS[name].__module__)
    monkeypatch.setattr(
        module, "baseline_worktree", lambda *args, **kwargs: contextlib.nullcontext(baseline)
    )
    monkeypatch.setattr(module, "git_short_sha", lambda *args: "old")
    monkeypatch.setattr(module, "git_full_sha", lambda *args: "a" * 40)
    if name == "synth":
        monkeypatch.setattr(module, "_baseline_self_compare_warning", lambda *args: None)
    seen = []

    def execute_baseline(*args):
        assert flow.args.work_dir == baseline and flow.args.target == "chosen"
        seen.append(flow._target_execution_refs["chosen"].identity)
        assert flow._target_execution_refs["chosen"] == pairs[0].baseline
        return {}

    monkeypatch.setattr(flow, "_execute_baseline_pairs", execute_baseline)
    original_list = TargetCatalog.list

    def list_candidates(self, **kwargs):
        assert self.project_root != baseline, "baseline repeated automatic discovery"
        return original_list(self, **kwargs)

    monkeypatch.setattr(TargetCatalog, "list", list_candidates)
    result, sha = BASELINE_METHODS[name](flow, pairs)
    assert result == {} and sha == "old"
    assert seen == [handles[0].identity]
    assert flow.args.work_dir == project


def test_auto_selected_target_still_requires_goal_binding(goal_mode, monkeypatch, executed):
    from booley.goals.flow_execution import GoalFlowExecution
    from booley.runtime import runtime_context
    from tests.goals.conftest import bind

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    project = goal_mode.worktree
    monkeypatch.chdir(project)
    write_targets(project, "lint")
    flow = LintFlow()
    unbound = flow.execute(
        flow.request_type(target="", work_dir=project),
        adapter=GoalFlowExecution(bind(goal_mode, "unbound")),
    )
    assert unbound.exit_code == 2 and executed == []
    assert unbound.outcome.detail["acceptance_effect"] == "rejected_unbound"
    assert unbound.outcome.detail["unbound_targets"] == ["chosen"]
    core = project / "demo.core"
    core.write_text(
        core.read_text().replace("acme:lib:demo:1", "::top:0").replace("chosen", "top")
    )
    accepted = LintFlow().execute(
        LintFlow.request_type(target="", work_dir=project),
        adapter=GoalFlowExecution(bind(goal_mode, "bound")),
    )
    assert accepted.exit_code == 0
    assert executed == ["::top:0#top"]
    executed.clear()
    core.write_text(core.read_text().replace("top:", "chosen:"))
    diagnostic = LintFlow().execute(
        LintFlow.request_type(target="", work_dir=project, diagnostic=True),
        adapter=GoalFlowExecution(bind(goal_mode, "diagnostic")),
    )
    assert diagnostic.exit_code == 0 and executed


@pytest.mark.parametrize("count", (0, 1, 2))
@pytest.mark.parametrize("name", FLOWS)
def test_disabled_flow_preserves_selection_error_order(
    name, count, project, executed, monkeypatch
):
    write_targets(project, name, count)
    monkeypatch.setattr(FLOWS[name], "_flow_enabled", lambda self: False)
    code, text = invoke("cli", name, project)
    assert code == 2 and executed == []
    assert (f"{name} is disabled" in text) == (count == 1)
    assert ("Eligible Targets:" in text) == (count != 1)


@pytest.mark.parametrize("failed", (False, True))
def test_dry_run_notice_preserves_stdout_and_selection(project, executed, monkeypatch, failed):
    import json

    write_targets(project, "lint")
    plan = FlowPlan("lint", "lint", (), aggregate_errors=("invalid source",) if failed else ())
    monkeypatch.setattr(LintFlow, "_plan_lint", lambda self, targets: plan)
    outputs = []
    for explicit in (True, False):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = LintFlow().execute_cli(
                [
                    "--work-dir",
                    str(project),
                    "--dry-run",
                    *(["--target", "chosen"] if explicit else []),
                ]
            )
        assert result.exit_code == (2 if failed else 0)
        outputs.append(stdout.getvalue())
        assert ("--target omitted" in stderr.getvalue()) == (not explicit)
        assert "--target omitted" not in stdout.getvalue()
        if failed:
            json.loads(stdout.getvalue())
    assert outputs[0] == outputs[1]
    assert executed == []
    assert not (project / "runtime/flow-reports/lint.json").exists()


def test_qualified_duplicate_names_are_distinct_eligible_choices(project, executed):
    write_targets(project, "lint", core="alpha")
    write_targets(project, "lint", core="beta")
    code, text = invoke("cli", "lint", project)
    assert code == 2 and executed == []
    choices = [handle.selector for handle in TargetCatalog.build(project).list(for_flow="lint")]
    assert len(choices) == 2 and all("#chosen" in choice for choice in choices)
    assert f"Eligible Targets: {', '.join(choices)}" in text


def test_explicit_selection_has_no_automatic_notice(project, executed):
    write_targets(project, "lint")
    code, text = invoke("cli", "lint", project, {"target": "chosen"})
    assert code == 0 and executed
    assert "--target omitted" not in text


def test_sole_eligible_qualified_selector_is_installed(project, executed):
    write_targets(project, "lint")
    (project / "other.core").write_text(
        "CAPI=2:\nname: acme:lib:other:1\ntargets:\n"
        "  chosen:\n    flow: unknown\n    filesets: []\n    toplevel: tb\n",
        encoding="utf-8",
    )
    (handle,) = TargetCatalog.build(project).list(for_flow="lint")
    assert "#chosen" in handle.selector
    code, text = invoke("cli", "lint", project)
    assert code == 0 and executed == [handle.identity]
    assert f"selected Target {handle.selector}." in text
