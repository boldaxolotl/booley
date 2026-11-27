"""Omitted Target policy through public built-in invocation boundaries."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import io
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from booley.flows.builtin_cli import build_cli_parser, execute_cli
from booley.flows.cli_selection import CliInvocation
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintConfigResult, LintFlow
from booley.flows.plan import FlowPlan
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.watchdog_sizing import resolve_simulation_record_work_units
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.mcp import server
from booley.mcp.flow_adapter import flow_schema
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.runtime.runtime_context import RuntimeLocation
from booley.targets.catalog import PreparedTargetSelection, TargetCatalog
from tests.memory_budget_support import fail_budget, pin_budget
from tests.sandbox_venue_fake import place

FLOWS = {"sim": SimulateFlow, "lint": LintFlow, "synth": AsicSynthesizeFlow, "fpga": FpgaImplFlow}
CASES = [(entry, flow) for entry in ("cli", "mcp") for flow in FLOWS]
CASE_IDS = [f"{entry}-{flow}" for entry, flow in CASES]
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
    from booley.flows import endpoint_admission

    monkeypatch.chdir(tmp_path)
    place(monkeypatch, RuntimeLocation.ISSUED_SANDBOX)
    # Seat Caps are covered by the admission tests; these tests are about Target omission.
    monkeypatch.setattr(endpoint_admission, "required_seats", lambda _tools, _config: ())
    for name in ("BOOLEY_STATE_FILE", "BOOLEY_TICKET_FILE", "BOOLEY_LOGS_DIR", "BOOLEY_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    # Every MCP call returns its Job's run id at once; `invoke` polls it.
    monkeypatch.setenv("BOOLEY_MCP_JOB_INLINE_WAIT_SECONDS", "0")
    # The MCP tool's public boundary is the SDK server over a live session; the
    # subprocess runner is the seam that keeps schema, argv and Job handling real.
    monkeypatch.setattr(server, "_run_subprocess", _transport_child)
    return tmp_path


@pytest.fixture
def no_admission(monkeypatch):
    # No public observer fails at the admission call itself.
    monkeypatch.setattr(
        "booley.flows.endpoint_admission._acquire_memory_reservation",
        lambda *_a, **_k: pytest.fail("admitted refusal"),
    )


def _record_selection(seen, flow, targets):
    # The selection a leaf receives is not public; it is the narrowest read of the
    # Targets the real `_run` chose.
    handles = flow._selected_target_handles()
    seen.extend(handle.identity for handle in handles)
    assert targets == [handle.selector for handle in handles]


# The expensive preparation/EDA leaves use deterministic spies. Public _run,
# Target preparation, simulation preparation and catalog discovery remain real.
# A public run plans and launches EDA tools, which the test hosts lack; these
# leaves are the narrowest seams under each Flow's real `_run`.
@pytest.fixture
def executed(monkeypatch):
    seen = []

    def run_sim(self, targets):
        _record_selection(seen, self, targets)
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
        _record_selection(seen, self, targets)
        return {}

    monkeypatch.setattr(
        SimulateFlow, "_plan_elaboration", lambda self, targets: FlowPlan("sim", "elab-only", ())
    )
    monkeypatch.setattr(SimulateFlow, "_execute_elab_only", run_sim)
    monkeypatch.setattr(LintFlow, "_plan_lint", lambda self, targets: FlowPlan("lint", "lint", ()))
    monkeypatch.setattr(LintFlow, "_prepare_lint_targets", lambda self, targets: ({}, []))
    monkeypatch.setattr(LintFlow, "_run_all_targets", run_lint)
    for cls in (AsicSynthesizeFlow, FpgaImplFlow):
        monkeypatch.setattr(
            cls,
            "_plan_implementation",
            lambda self, targets: FlowPlan(self.name, "implementation", ()),
        )
        monkeypatch.setattr(cls, "_run_current_targets", run_implementation)
        monkeypatch.setattr(
            cls,
            "_aggregate_results",
            lambda self, *args: EndpointOutcome(report_text="RESULT: PASS"),
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
                # Builds the argv the MCP server builds, so both routes agree.
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
        # The dispatchers are the closest seam that keeps schema, argv, sizing, Job
        # handling and polling real without a live SDK session.
        jobs = server._JobManager(server._McpLifetime(None, None))
        result = await server._dispatch_booley_mcp_tool(
            name,
            params,
            {"module": name, "is_flow": True, "memory_admitted": True},
            collections.defaultdict(int),
            jobs,
        )
        started = "\n".join(block.text for block in result)
        # The call returns at once with the Job's run id; poll it to the result.
        run_id = re.search(r"run_id=([\w-]+)", started)
        assert run_id, started
        result = await server._dispatch_special_mcp_tool(
            "booley_poll", {"run_id": run_id[1]}, jobs, []
        )
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
def test_omitted_target_rejects_cardinality(entry, name, count, project, executed, no_admission):
    write_targets(project, name, count)
    code, text = invoke(entry, name, project)
    choices = "(none)" if count == 0 else "chosen0, chosen1"
    assert f"Eligible Targets: {choices}" in text, "eligible Target list missing: " + text
    assert code == 2 and executed == []
    assert "--target omitted; selected Target" not in text


@pytest.mark.parametrize("name", FLOWS)
def test_help_states_cardinality_rule(name):
    flow = FLOWS[name]()
    text = " ".join(build_cli_parser(flow).format_help().split())
    description = flow_schema(flow)["properties"]["target"]["description"]
    rule = ("exactly one", "eligible", "announced", "zero", "multiple")
    assert all(word in text and word in description for word in rule), "help omits the rule"
    assert ("manifest" in text) == (name == "sim")


def test_omitted_sim_target_budget_counts_default_suite(project):
    write_targets(project, "sim")
    (project / ".booley/tests.toml").write_text(
        '[chosen]\ntests = ["first", "second", "third"]\nselect = "+test={name}"\n',
        encoding="utf-8",
    )
    explicit = resolve_simulation_record_work_units(project, "chosen")
    assert explicit == 3
    assert resolve_simulation_record_work_units(project, "") == explicit, (
        "auto-selected suite was under-budgeted"
    )
    assert (
        resolve_simulation_record_work_units(project, "", ["first@x4"])
        == resolve_simulation_record_work_units(project, "chosen", ["first@x4"])
        == 4
    )


@pytest.mark.parametrize("entry", ("cli", "mcp"))
def test_omitted_target_with_resume_is_accepted(entry, project, monkeypatch):
    from types import SimpleNamespace

    write_targets(project, "sim")
    handle = TargetCatalog.build(project).select("chosen", for_flow="sim")
    target = dict.fromkeys(("vlnv", "name", "role", "revision"), "") | {"selector": "chosen"}
    manifest = SimpleNamespace(
        document={"target": target, "workload": {"trace": False}, "work_items": ()}
        | {"required_suite": {"names": [], "default_invocation": True}}
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

    def resume(self, selected):
        assert selected.resume is validated
        observed.append(selected.resume.target_handles[0].identity)
        return EndpointOutcome(report_text="RESULT: PASS")

    # Resuming runs EDA tools and needs an authenticated manifest.
    monkeypatch.setattr(SimulateFlow, "_resume_simulation_record", resume)
    monkeypatch.setattr(
        TargetCatalog, "list", lambda *args, **kwargs: pytest.fail("resume discovered catalog")
    )
    monkeypatch.setattr(
        "booley.flows.sim.watchdog_sizing.resolve_simulation_record_work_units",
        lambda *args, **kwargs: pytest.fail("resume sized unrelated Target"),
    )
    for count in (0, 2):
        write_targets(project, "sim", count)
        code, text = invoke(entry, "sim", project, {"resume_from": str(validated.path)})
        assert code == 0, text
        assert "--target omitted" not in text
    assert observed == [handle.identity, handle.identity]


@pytest.mark.parametrize("name", FLOWS)
def test_automatic_selection_discovers_targets_once(name, project, executed, monkeypatch):
    write_targets(project, name)
    resolves = []
    resolve = PreparedTargetSelection.resolve.__func__

    def counted(cls, *args, **kwargs):
        resolves.append(args)
        return resolve(cls, *args, **kwargs)

    monkeypatch.setattr(PreparedTargetSelection, "resolve", classmethod(counted))
    code, text = invoke("cli", name, project)
    assert code == 0 and executed == ["acme:lib:demo:1#chosen"], text
    assert len(resolves) == 1, resolves


def test_reused_flow_discards_automatic_selection(project, executed):
    write_targets(project, "lint")
    flow = LintFlow()
    first_request = flow.request_type(target="", work_dir=project)
    first = flow.execute(first_request)
    other = project / "other"
    write_targets(other, "lint", core="other")
    second_request = flow.request_type(target="", work_dir=other)
    second = flow.execute(second_request)
    assert first.exit_code == second.exit_code == 0
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
    # A second publication must not print the notice again; only the endpoint
    # object can republish, no public entry point does.
    flow._publish_console_report(result.outcome)
    captured = capsys.readouterr()
    notice = "lint: --target omitted; selected Target chosen."
    assert captured.err.count(notice) == 1
    assert notice not in captured.out
    assert captured.err.rstrip().endswith(notice)


@pytest.mark.parametrize(
    ("argv", "typed"),
    ((["--test", "nope"], {"test": ("nope",)}), (["--coverage"], {"coverage": True})),
    ids=("unknown-test", "coverage"),
)
def test_selection_notice_survives_preparation_failure(
    argv, typed, project, executed, no_admission, capsys
):
    write_targets(project, "sim")
    notice = "sim: --target omitted; selected Target chosen."
    flow = SimulateFlow()
    # Both refuse publicly before any EDA tool runs; only the CLI transport
    # sets `_cli_invocation`, and it spawns a process.
    flow._cli_invocation = CliInvocation(transport=True)
    result = flow.execute_cli(["--work-dir", str(project), *argv])
    captured = capsys.readouterr()
    assert result.exit_code == 2 and executed == []
    assert captured.err.count(notice) == 1 and captured.err.rstrip().endswith(notice)
    SimulateFlow().execute(SimulateFlow.request_type(target="", work_dir=project, **typed))
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("name", ("synth", "fpga"))
def test_baseline_runs_the_automatic_candidates_pair(name, project, executed, monkeypatch):
    from booley.flows import implementation_flow

    write_targets(project, name)
    baseline = project.parent / (project.name + "-baseline")
    write_targets(baseline, name)
    core = baseline / "demo.core"
    extra = f"  extra:\n    flow: {name}\n    flow_options: {{tool: {TOOLS[name]}}}\n"
    core.write_text(
        core.read_text().replace(
            "  incompatible:", extra + "    filesets: []\n    toplevel: tb\n  incompatible:"
        )
    )
    monkeypatch.setattr(
        implementation_flow, "baseline_worktree", lambda *a, **k: contextlib.nullcontext(baseline)
    )
    monkeypatch.setattr(implementation_flow, "git_short_sha", lambda *args: "old")
    monkeypatch.setattr(implementation_flow, "git_full_sha", lambda *args: "a" * 40)
    seen = []

    def execute_baseline(self, pairs, *args):
        assert self.args.work_dir == baseline and self.args.target == "chosen"
        seen.extend(pair.baseline.identity for pair in pairs)
        return {}

    # Running the baseline pair needs the EDA tools.
    monkeypatch.setattr(FLOWS[name], "_execute_baseline_pairs", execute_baseline)
    listed = TargetCatalog.list

    def list_candidates(self, **kwargs):
        assert self.project_root != baseline, "baseline repeated automatic discovery"
        return listed(self, **kwargs)

    monkeypatch.setattr(TargetCatalog, "list", list_candidates)
    code, text = invoke("cli", name, project, {"baseline": "old"})
    assert code == 0, text
    assert seen == executed == ["acme:lib:demo:1#chosen"]


@pytest.mark.parametrize("count", (0, 1, 2))
@pytest.mark.parametrize("name", FLOWS)
def test_disabled_flow_preserves_selection_error_order(
    name, count, project, executed, monkeypatch
):
    write_targets(project, name, count)
    (project / ".booley/booley.toml").write_text(f"[flows.{name}]\nenabled = false\n")
    code, text = invoke("cli", name, project)
    assert code == 2 and executed == []
    assert (f"{name} is disabled" in text) == (count == 1)
    assert ("Eligible Targets:" in text) == (count != 1)


@pytest.mark.parametrize("enabled", (True, False))
@pytest.mark.parametrize("name", FLOWS)
def test_only_a_job_that_runs_reports_its_memory_warning(
    name, enabled, project, executed, monkeypatch
):
    write_targets(project, name)
    if name in ("synth", "fpga"):
        # Empty history: the Job's first run carries the missing-measurement note.
        pin_budget(monkeypatch, 1000)
    else:
        fail_budget(monkeypatch)
    if not enabled:
        (project / ".booley/booley.toml").write_text(f"[flows.{name}]\nenabled = false\n")
    code, text = invoke("cli", name, project)
    assert (code, "this Job ran alone" in text) == ((0, True) if enabled else (2, False)), text


@pytest.mark.parametrize("failed", (False, True))
def test_dry_run_notice_preserves_stdout_and_selection(project, executed, monkeypatch, failed):
    import json

    write_targets(project, "lint")
    plan = FlowPlan("lint", "lint", (), aggregate_errors=("invalid source",) if failed else ())
    # A dry run of a real lint plan needs verilator; this pins the plan only.
    monkeypatch.setattr(LintFlow, "_plan_lint", lambda self, targets: plan)
    outputs = []
    for explicit in (True, False):
        target = ["--target", "chosen"] if explicit else []
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = LintFlow().execute_cli(["--work-dir", str(project), "--dry-run", *target])
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
