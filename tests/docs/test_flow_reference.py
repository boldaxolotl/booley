"""Keep the public Flow reference and report schemas aligned with executable interfaces."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

import pytest
from tests.flows.sim.test_endpoint_record_lifecycle import _native_cycle_campaign
from tests.target_test_support import make_target_handle

from booley.flows.base import BooleyFlow
from booley.flows.builtin_cli import build_parser
from booley.flows.fpga.backends.vivado.metrics import FpgaMetrics
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintConfigResult, LintFlow, LintWarning
from booley.flows.mutation.flow import MutationFlow
from booley.flows.sim.build import BuildOutcome
from booley.flows.sim.coverage_campaign import CoverageRollup
from booley.flows.sim.elab_only import ElabOnlyTargetResult
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.record_presentation import _campaign_report_lines
from booley.flows.synth.flow import AsicSynthesizeFlow, SynthMetrics

REPO_ROOT = Path(__file__).resolve().parents[2]
USER_DOCS = REPO_ROOT / "docs" / "user"
# Shared Flow behavior lives in FLOW_REFERENCE.md; each Flow has its own page.
REFERENCE = USER_DOCS / "FLOW_REFERENCE.md"
FLOW_PAGES = {
    "sim": ("FLOW-SIM.md", "COVERAGE.md"),
    "lint": ("FLOW-LINT.md",),
    "synth": ("FLOW-SYNTH.md",),
    "fpga": ("FLOW-FPGA.md",),
    "mutation": ("FLOW-MUTATION.md",),
}
REPORTS = REPO_ROOT / "docs" / "internals" / "FLOW_REPORTS.md"
MCP_TOOLS = REPO_ROOT / "docs" / "internals" / "MCP-TOOLS.md"
FLOW_TYPES = (
    SimulateFlow,
    LintFlow,
    AsicSynthesizeFlow,
    FpgaImplFlow,
    MutationFlow,
)
FlowT = TypeVar("FlowT", bound=BooleyFlow)


def _reference_text(doc: Path = REFERENCE) -> str:
    return doc.read_text(encoding="utf-8")


def _flow_section(flow_name: str) -> str:
    """Return the text of a Flow's user-doc pages."""
    return "\n".join(_reference_text(USER_DOCS / page) for page in FLOW_PAGES[flow_name])


def _shared_section() -> str:
    """Return the shared Flow page."""
    return _reference_text()


def _report_flow_chapter(flow_name: str) -> str:
    """Return the ``## `<flow>` `` chapter of FLOW_REPORTS.md."""
    text = _reference_text(REPORTS)
    start = text.index(f"## `{flow_name}`")
    end = text.find("\n## ", start + 1)
    return text[start:] if end < 0 else text[start:end]


def _report_shared_chapter() -> str:
    """Return everything in FLOW_REPORTS.md before its first Flow chapter."""
    return _reference_text(REPORTS).split("\n## `sim`", maxsplit=1)[0]


def _documented_fields(flow_name: str) -> set[str]:
    """Identifiers named in code spans of the report-schema chapter for a Flow."""
    code_spans = re.findall(
        r"(?<!`)`([^`\n]+)`(?!`)",
        _report_shared_chapter() + _report_flow_chapter(flow_name),
    )
    return {
        identifier
        for code_span in code_spans
        for identifier in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", code_span)
    }


def _assert_documented(flow_name: str, payload: dict[str, Any]) -> None:
    # The identifier pattern in _documented_fields drops the ``$`` of keywords such as ``$schema``.
    missing = {key.removeprefix("$") for key in payload} - _documented_fields(flow_name)
    assert not missing, f"{flow_name} structured fields missing from reference: {sorted(missing)}"


def _configured_flow(
    flow_type: type[FlowT],
    tmp_path: Path,
    target: str,
) -> tuple[FlowT, Path]:
    report_dir = tmp_path / "reports"
    flow = flow_type()
    flow.parse_args(
        ["--target", target, "--work-dir", str(tmp_path), "--report-dir", str(report_dir)]
    )
    handle = make_target_handle(
        tmp_path,
        target,
        vlnv="::docs:0",
        drivable_by=(flow.name,),
        core_file=tmp_path / "docs.core",
    )
    flow._target_handles = {target: handle}  # type: ignore[attr-defined]
    return flow, report_dir


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("flow_type", FLOW_TYPES, ids=lambda flow_type: flow_type.name)
def test_flow_reference_lists_every_long_cli_option(flow_type: type[Any]) -> None:
    flow = flow_type()
    # Read the person-facing parser: its hidden (help=SUPPRESS) compatibility
    # aliases, such as --timeout-ms and --work-dir, are deliberately undocumented.
    parser_options = {
        option
        for action in build_parser(flow, human=True)._actions
        if action.help != argparse.SUPPRESS
        for option in action.option_strings
        if option.startswith("--")
    }
    documented_options = set(
        re.findall(r"--[a-z][a-z0-9-]*", _shared_section() + _flow_section(flow.name))
    )

    assert parser_options <= documented_options


def test_flow_reference_distinguishes_target_owned_synth_mode() -> None:
    options = {
        option
        for action in build_parser(AsicSynthesizeFlow())._actions
        for option in action.option_strings
    }

    assert "--synth-mode" not in options
    assert "no per-call `--synth-mode` option" in _flow_section("synth")


def test_flow_reference_uses_the_executable_target_filter() -> None:
    assert "`booley targets --for <flow>`" in _shared_section()
    assert "booley targets --for-flow <flow>" not in _reference_text()


def test_sim_campaign_resume_granularity_stays_documented() -> None:
    section = _flow_section("sim")
    assert "One resume continues one Target" in section
    assert "- Only Test Runs without a result run again." in section
    flat = " ".join(section.split())
    assert "Finished batches are skipped; an interrupted batch reruns all its tests." in flat
    assert "Start a new `booley flow sim --coverage` run instead" in section
    assert "Resume never\n  re-runs failures" in section
    mcp = _reference_text(MCP_TOOLS)
    assert "maximum-32 `observations` preview" in mcp
    assert "`observation_total`" in mcp
    assert "`observations_truncated`" in mcp


def test_sim_configured_skip_control_stays_documented() -> None:
    section = " ".join(_flow_section("sim").split())
    config = " ".join(_reference_text(USER_DOCS / "TESTS.md").split())
    # FLOW-SIM says how --test interacts with skip; tests.toml owns the skip rules.
    assert "Naming Tests runs exactly those, skipped or not" in section
    assert "naming a skipped Test explicitly still runs it" in config
    assert "all-skip target fails preflight" in config


def test_simulation_build_and_dry_run_timeout_contract_is_documented() -> None:
    shared = " ".join(_shared_section().split())
    config = " ".join(_flow_section("sim").split())

    assert "[flows.sim].build_timeout_ms" in shared
    dry_run = " ".join(_reference_text(REPORTS).split())
    assert "recipe `build_timeout_ms`" in dry_run
    assert "Standalone units" in dry_run and "have no build field" in dry_run
    assert "defaults to `3600000` (one hour)" in config
    assert "independent fixed 600-second budget" in config


def test_sim_report_fields_stay_documented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The campaign helper relies on the isolation that tests/flows fixtures give.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / ".booley"))
    monkeypatch.setattr("booley.core.booley_dir._cache", None)
    # A coverage campaign writes the ordinary projection plus the coverage fields.
    _native_cycle_campaign(tmp_path, monkeypatch)
    (path,) = (tmp_path / "reports").glob("sim/*/targets/*/simulation.json")
    projection = _read_json(path)

    _assert_documented("sim", projection)
    _assert_documented("sim", projection["tests"][0])
    _assert_documented("sim", projection["test_runs"][0])
    _assert_documented("sim", projection["simulation_record_manifest"])
    _assert_documented("sim", projection["coverage_record"])


def test_sim_elab_only_report_fields_stay_documented(tmp_path: Path) -> None:
    sim, report_dir = _configured_flow(SimulateFlow, tmp_path, "sim_demo")
    with sim.publication_resources:
        sim._write_elab_only_target_report(
            ElabOnlyTargetResult(
                target="sim_demo",
                target_identity="vendor:library:demo:1.0#sim_demo",
                eda_tool="verilator",
                toplevel="demo",
                compile_command="make",
                fileset={"rtl": ["demo.sv"], "tb": ["tb_demo.sv"]},
                outcome=BuildOutcome(True, "pass", None, elapsed_s=0.1),
                log_path="run.log",
            )
        )
    _assert_documented("sim", _read_json(report_dir / "sim/1/targets/sim_demo/simulation.json"))


def test_lint_report_fields_stay_documented(tmp_path: Path) -> None:
    lint, _report_dir = _configured_flow(LintFlow, tmp_path, "lint_demo")
    lint._lint_invocation_dir = lint.reserve_invocation_dir()  # type: ignore[attr-defined]
    warning = LintWarning("RULE", "rtl.sv", 1, 2, "message", "lint_demo")
    lint_result = LintConfigResult(
        target="lint_demo",
        warnings=[warning],
        eda_tool="verilator",
        files_linted=1,
        toplevel="top",
        log_path="run.log",
    )
    lint_error = LintConfigResult(target="lint_broken", error="could not run")
    lint_path = lint._write_lint_report(
        ["lint_demo", "lint_broken"],
        [warning],
        0.1,
        errored=[lint_error],
        target_results=[lint_result, lint_error],
    )
    assert lint_path is not None
    lint_report = _read_json(lint_path)
    _assert_documented("lint", lint_report)
    _assert_documented("lint", lint_report["warnings"][0])
    _assert_documented("lint", lint_report["errors"][0])
    _assert_documented("lint", lint_report["target_results"][0])


def test_lint_warning_row_fields_stay_documented() -> None:
    warning_row = next(
        line
        for line in _report_flow_chapter("lint").splitlines()
        if line.startswith("| `warnings[]` |")
    )
    assert "`targets`" in warning_row
    assert "`eda_tools`" in warning_row


def test_synth_report_fields_stay_documented(tmp_path: Path) -> None:
    synth, report_dir = _configured_flow(AsicSynthesizeFlow, tmp_path, "synth_demo")
    current_synth = SynthMetrics(
        area_um2=10.0,
        area_source="openroad",
        area_kge=1.0,
        cells=10,
        wns_ns=-1.0,
        reg2reg_slack_ns=0.0,
        reg2reg_fmax_mhz=100.0,
        failure_output="failure",
        run_evidence={"run_id": "current"},
        synth_mode="physical",
        openroad_reuse={
            "producer_run_id": "synth-1",
            "producer_started_at": "2026-11-06T10:00:00Z",
        },
    )
    baseline_synth = SynthMetrics(
        area_um2=8.0,
        area_source="openroad",
        area_kge=0.8,
        cells=8,
        run_evidence={"run_id": "baseline"},
        synth_mode="physical",
    )
    synth._write_target_report("synth_demo", current_synth, baseline_synth, "main")
    synth_path = next(report_dir.glob("synth_*.json"))
    synth_report = _read_json(synth_path)
    _assert_documented("synth", synth_report)
    _assert_documented("synth", synth_report["baseline"])
    _assert_documented("synth", synth_report["conditions"])


def test_fpga_report_fields_stay_documented(tmp_path: Path) -> None:
    fpga, report_dir = _configured_flow(FpgaImplFlow, tmp_path, "fpga_demo")
    current_fpga = FpgaMetrics(
        lut_count=10,
        ff_count=20,
        wns_ns=0.1,
        whs_ns=0.1,
        cached=True,
        cache_fingerprint="cache",
        failure_output="failure",
        log_path="run.log",
        dirs={"build": "build"},
        cache_consumer_run_id="consumer",
        recipe_fingerprint="recipe",
        recipe_snapshot={"target": "fpga_demo"},
        run_evidence={"run_id": "current"},
    )
    baseline_fpga = FpgaMetrics(
        lut_count=9,
        ff_count=19,
        wns_ns=0.2,
        whs_ns=0.2,
        recipe_fingerprint="baseline-recipe",
        recipe_snapshot={"target": "fpga_demo"},
        run_evidence={"run_id": "baseline"},
    )
    fpga_report = fpga._target_report_payload(
        "fpga_demo",
        current_fpga,
        baseline_fpga,
        "main",
        report_dir / "fpga_fpga_demo.json",
    )
    _assert_documented("fpga", fpga_report)
    _assert_documented("fpga", fpga_report["metrics"])
    _assert_documented("fpga", fpga_report["baseline_metrics"])


def test_synth_help_lists_live_policy_keys():
    from booley.audit.flow_schema import SELECTIVE_FLOW_KNOBS

    policy = AsicSynthesizeFlow.description.split("[flows.synth]")[1]
    assert not re.search(r"\btarget\b", policy, re.IGNORECASE)
    for key in (
        "enabled",
        "timeout_ms",
        "expected_latches",
        "fail_on_timing_violation",
        "timing_report_paths",
    ):
        assert key in policy
        if key != "enabled":
            assert "synth" in SELECTIVE_FLOW_KNOBS[key]
    assert "timing_report_paths" in (USER_DOCS / "FLOW-SYNTH.md").read_text(encoding="utf-8")


def test_target_omission_rule_matches_usage_and_reference() -> None:
    cli_reference = USER_DOCS / "CLI-REFERENCE.md"
    for document in (REFERENCE, cli_reference):
        text = " ".join(document.read_text(encoding="utf-8").split())
        assert all(
            fragment in text
            for fragment in (
                "exactly one eligible Target",
                "zero or multiple",
                "announced",
                "resume",
            )
        ), "documented Target omission rule missing"
    assert "Every run needs `--target`" not in REFERENCE.read_text(encoding="utf-8")


_DOC_REPORTS_ROOT = "/work/.booley/flow-reports"
_CARD_PATH_LABELS = ("manifest", "report", "coverage campaign")


def _text_blocks(page: str) -> list[str]:
    """The bodies of the ``text`` fences in a user-doc page."""
    return re.findall(r"```text\n(.*?)```", _reference_text(USER_DOCS / page), re.DOTALL)


def _as_documented(lines: list[str], root: Path) -> list[str]:
    """Show the card's paths as the docs do: rooted at ``/work/.booley/flow-reports``."""
    shown: list[str] = []
    for line in lines:
        label, _, value = line.strip().partition(": ")
        if label in _CARD_PATH_LABELS:
            relative = Path(value).relative_to(root.resolve()).as_posix()
            shown.append(f"  {label}: {_DOC_REPORTS_ROOT}/{relative}")
        else:
            shown.append(line)
    return shown


def _rollup(metric: str, covered: int, eligible: int) -> CoverageRollup:
    return CoverageRollup(
        metric=metric,
        semantics="booley.coverage-semantics/v1",
        total_points=eligible,
        eligible_points=eligible,
        covered_points=covered,
        waived_points=0,
        percent=round(covered * 100 / eligible, 2),
    )


def test_coverage_card_example_matches_rendered_card(tmp_path: Path) -> None:
    root = tmp_path / "flow-reports"
    campaign = SimpleNamespace(
        collection={"status": "complete", "diagnostics": ()},
        evaluation={
            "status": "pass",
            "diagnostics": (),
            "metrics": (),
            "thresholds": {"branch": 80},
        },
        rollups=(
            _rollup("line", 923, 1000),
            _rollup("branch", 81, 100),
            _rollup("toggle", 642, 1000),
        ),
        source_closure={},
        findings=(),
    )
    outcome = SimpleNamespace(
        target={"selector": "sim_soc"},
        manifest_path=root / "sim/12/targets/sim_soc/campaign/manifest.json",
        aggregate_grade="pass",
        coverage_reference=object(),
        observations=(),
        acceptance_facts=SimpleNamespace(document={"campaign_id": "…"}),
    )

    rendered = _campaign_report_lines(
        (outcome,),
        {"sim_soc": campaign},
        trailing_lines=None,
        report_path=root / "sim/12/report.json",
    )[0].splitlines()

    documented = next(
        block for block in _text_blocks("COVERAGE.md") if "coverage campaign:" in block
    )
    assert _as_documented(rendered, root) == documented.splitlines()


def test_sim_card_examples_name_the_run_report(tmp_path: Path) -> None:
    root = tmp_path / "flow-reports"
    flow = SimulateFlow()
    flow._args = SimpleNamespace(report_dir=root, dry_run=False)
    flow._reserved_invocation_dir = root / "sim/12"
    with flow.publication_resources:
        destination = flow._report_destination()
    assert destination == (root / "sim/12/report.json").resolve()
    expected = _as_documented([f"  report: {destination}"], root)[0]

    cards = [
        line
        for page in FLOW_PAGES["sim"]
        for block in _text_blocks(page)
        for line in block.splitlines()
        if line.startswith("  report:")
    ]
    assert cards
    assert set(cards) == {expected}


_CARD_ROW = re.compile(r"^    (PASS|FAIL|ERROR|INCONCLUSIVE)\s")
_CARD_LOG = re.compile(
    r"^      log: /work/\.booley/flow-reports/sim/\d+/targets/\w+/campaign/work-items/"
    r"\d{4}-[^/\s]+/attempts/\d{4}-[^/\s]+/evidence/\d{4}-run\.log$"
)
_CARD_WAVEFORM = re.compile(
    r"^      waveform: /work/\.booley/flow-reports/sim/\d+/targets/\w+/campaign/work-items/"
    r"\d{4}-[^/\s]+/attempts/\d{4}-[^/\s]+/evidence/\d{4}-[^/\s]+\.fst$"
)


def test_sim_reading_results_example_shows_evidence_paths_without_a_search_recipe() -> None:
    page = _reference_text(USER_DOCS / "FLOW-SIM.md")
    section = page.split("\n## Reading results\n", 1)[1].split("\n## ", 1)[0]

    graded = ""
    logs: list[str] = []
    waveforms: list[str] = []
    for line in section.splitlines():
        row = _CARD_ROW.match(line)
        if row:
            graded = row.group(1)
        elif line.startswith("      log:"):
            # Only a Test Run that did not pass names its log, under its own row.
            assert _CARD_LOG.match(line), line
            assert graded not in {"", "PASS"}, line
            logs.append(graded)
        elif line.startswith("      waveform:"):
            assert _CARD_WAVEFORM.match(line), line
            assert graded, line
            waveforms.append(graded)

    assert logs == ["FAIL"]
    assert "PASS" in waveforms
    # The card names the files; the guide teaches no hunt through the work items.
    assert "grep" not in section
    assert "work-items/*" not in section
    assert "result.json" not in section
