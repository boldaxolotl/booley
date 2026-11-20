"""Keep the public Flow reference and report schemas aligned with executable interfaces."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.builtin_cli import build_parser
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintFlow
from booley.flows.mutation.flow import MutationFlow
from booley.flows.sim.coverage_record import CoverageRollup
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.record_presentation import _simulation_record_report_lines
from booley.flows.synth.flow import AsicSynthesizeFlow

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
FLOW_TYPES = (
    SimulateFlow,
    LintFlow,
    AsicSynthesizeFlow,
    FpgaImplFlow,
    MutationFlow,
)


def _reference_text(doc: Path = REFERENCE) -> str:
    return doc.read_text(encoding="utf-8")


def _flow_section(flow_name: str) -> str:
    """Return the text of a Flow's user-doc pages."""
    return "\n".join(_reference_text(USER_DOCS / page) for page in FLOW_PAGES[flow_name])


def _shared_section() -> str:
    """Return the shared Flow page."""
    return _reference_text()


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


def test_simulation_record_resume_granularity_stays_documented() -> None:
    section = _flow_section("sim")
    assert "One resume continues one Target" in section
    assert "- Only Test Runs without a result run again." in section
    flat = " ".join(section.split())
    assert "Finished batches are skipped; an interrupted batch reruns all its tests." in flat
    assert "Start a new `booley flow sim --coverage` run instead" in section
    assert "Resume never\n  re-runs failures" in section


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
    assert "defaults to `3600000` (one hour)" in config
    assert "independent fixed 600-second budget" in config


def test_synth_help_lists_live_policy_keys():
    from booley.audit.flow_schema import SELECTIVE_FLOW_KNOBS

    policy = AsicSynthesizeFlow.description.split("[flows.synth]")[1]
    assert not re.search(r"\btarget\b", policy, re.IGNORECASE)
    for key in (
        "enabled",
        "timeout_ms",
        "expected_latches",
        "expected_comb_loops",
        "expected_removed_comb_loops",
        "expected_removed_multi_driven",
        "fail_on_timing_violation",
        "timing_report_paths",
    ):
        assert key in policy
        if key != "enabled":
            assert "synth" in SELECTIVE_FLOW_KNOBS[key]
    assert "timing_report_paths" in (USER_DOCS / "FLOW-SYNTH.md").read_text(encoding="utf-8")


def test_structural_allowance_knobs_documented_per_flow() -> None:
    """Each Flow's user page documents its own allowance keys in ``## Configuration``."""
    expected = {
        "FLOW-SYNTH.md": (
            "expected_latches",
            "expected_comb_loops",
            "expected_removed_comb_loops",
            "expected_removed_multi_driven",
        ),
        "FLOW-FPGA.md": ("expected_latches", "expected_comb_loops"),
    }
    for page, keys in expected.items():
        text = (USER_DOCS / page).read_text(encoding="utf-8")
        configuration = text.split("\n## Configuration", 1)[1].split("\n## ", 1)[0]
        for key in keys:
            assert key in configuration, (page, key)


def test_flow_reference_documents_the_target_omission_rule() -> None:
    text = " ".join(REFERENCE.read_text(encoding="utf-8").split())
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
_CARD_PATH_LABELS = ("manifest", "report", "Coverage Record")


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
    coverage_record = SimpleNamespace(
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
        manifest_path=root / "sim/12/targets/sim_soc/record/manifest.json",
        aggregate_grade="pass",
        coverage_reference=object(),
        observations=(),
        acceptance_facts=SimpleNamespace(document={"campaign_id": "…"}),
    )

    rendered = _simulation_record_report_lines(
        (outcome,),
        {"sim_soc": coverage_record},
        trailing_lines=None,
        report_path=root / "sim/12/report.json",
    )[0].splitlines()

    documented = next(
        block for block in _text_blocks("COVERAGE.md") if "Coverage Record:" in block
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
    r"^      log: /work/\.booley/flow-reports/sim/\d+/targets/\w+/record/work-items/"
    r"\d{4}-[^/\s]+/attempts/\d{4}-[^/\s]+/execution-evidence/run\.log$"
)
_CARD_WAVEFORM = re.compile(
    r"^      waveform: /work/\.booley/flow-reports/sim/\d+/targets/\w+/record/work-items/"
    r"\d{4}-[^/\s]+/attempts/\d{4}-[^/\s]+/execution-evidence/trace\.fst$"
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
