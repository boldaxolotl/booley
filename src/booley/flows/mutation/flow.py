"""MutationFlow — the Booley Flow ``booley flow mutation``: deterministic, isolated mutation testing.

A run needs no model and no agent CLI:

  1. The Mutant generator selects ``--count`` Mutants of the scope files for
     ``--seed`` (default 0): the same RTL, count and seed give the same Mutants.
  2. The untouched project is built and run as the campaign baseline; every
     test of the Target must pass on it.
  3. Each Mutant is applied only to private build source copies and simulated.
     The compiler decides whether a Mutant is valid; an invalid Mutant is
     reported and not scored.
  4. A Mutant is detected when a Target test fails on it or runs past its
     baseline-scaled time limit ("detected (timeout)").

The scope defaults to the Target's own RTL.  Every run rebuilds the baseline
and every Mutant, so a result never depends on stale builds.

Exit codes:
  0 — detected Mutants >= threshold (pass)
  1 — detected Mutants < threshold (fail)
  2 — error (generator, baseline or infrastructure failure, ...)
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar
from urllib.parse import quote

from booley.config.project_config import load_test_configuration_field, lookup_target_section
from booley.core.boundary import BoundaryError, as_int
from booley.flows import artifacts as _artifacts
from booley.flows.base import BuiltinFlow, SubprocessResult
from booley.flows.cli_help import CliHelp, SurfaceHelp
from booley.flows.flow_config import tb_top_for_target
from booley.flows.mutation import workspace
from booley.flows.mutation.build_sources import (
    materialize_build_mutation,
    validate_mutation_closure,
)
from booley.flows.mutation.cli import MutationArguments
from booley.flows.mutation.equivalence import generated_cpp, verilator_version
from booley.flows.mutation.generator import (
    GENERATOR_VERSION,
    Mutant,
    generate_mutants,
    parser_version,
)
from booley.flows.mutation.operators import (
    MutantGeneratorError,
    SourceParseError,
)
from booley.flows.mutation.request import MutationRequest
from booley.flows.mutation.score import default_min_detected
from booley.flows.mutation.snippet import detection_snippet, tail, tail_snippet
from booley.flows.mutation.variants import MutationVariantError, MutationVariantPlan
from booley.flows.sim.adapter_contract import AdapterKind, PreparedSimulationWork
from booley.flows.sim.backends.cocotb_results import (
    STATE_OK,
    VERDICT_FAIL,
    VERDICT_PASS,
    CocotbResults,
    parse_results_line,
)
from booley.flows.sim.build import (
    PreparedSimulationBuild,
    prepare_simulation_build,
    simulation_build_environment,
)
from booley.flows.sim.config import (
    literal_run_cwd_problem,
    resolve_run_cwd,
    resolve_sim_sentinels,
)
from booley.flows.sim.execution.composition import prepare_adapter_invocation
from booley.flows.sim.execution.engine import (
    launch_environment,
    simulation_plusargs,
    target_test_plan,
)
from booley.flows.sim.result import (
    SIM_INFRA_ERROR_PREFIX,
    has_infra_error,
    has_simulation_timeout,
)
from booley.flows.sim.run_guard import DEFAULT_SIM_TIME_GRACE_S, unstaged_runtime_input_diagnostic
from booley.flows.sim.runtime_inputs import (
    RuntimeInputError,
    declared_runtime_inputs,
    materialize_runtime_inputs,
)
from booley.flows.simulator_family import matching_eda_tool, normalize_eda_tool
from booley.flows.target_campaign import (
    CampaignUnit,
    TargetCampaign,
    all_campaign_results_match,
    describe_target_campaign,
    resolve_target_campaign,
)
from booley.flows.target_criteria import CampaignScopeError
from booley.flows.target_test_plan import TargetTestPlan, TestRunLaunch, batch_test_runs
from booley.flows.target_test_suite import (
    NoRunnableTestsError,
    TargetTestSuite,
    require_runnable_target_test_suite,
)
from booley.fusesoc import fusesoc_registry
from booley.runtime import job_slots
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    EndpointOutcome,
)
from booley.runtime.platform_paths import posix_relpath
from booley.runtime.python_artifacts import python_artifact_root, relocate_python_artifacts
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetInspection
from booley.targets.selection import TargetSectionConflictError


def _target_environment(work_dir: Path, identity: str) -> dict[str, str]:
    """The ``tests.toml`` env table of Target *identity* in the checkout at *work_dir*."""
    sections = load_test_configuration_field(work_dir, "env")
    return dict(lookup_target_section(sections, identity) or {})


logger = logging.getLogger(__name__)

# The clock that times each test run; tests replace it, never ``time.monotonic``.
_monotonic = time.monotonic

#: Source suffixes the generator and the variant applier accept.
_MUTATION_SUFFIXES = frozenset({".v", ".vh", ".sv", ".svh"})
#: ``file_type`` prefixes of Verilog and SystemVerilog sources.
_HDL_FILE_TYPES = ("verilogSource", "systemVerilogSource")
#: Tags (besides the testbench role) that take a Target input out of the default scope.
_UNMUTATED_FILE_TAGS = frozenset({"vendored", "generated"})

#: Said only when the Target's own RTL was the scope, where choosing files can fix the run.
_PASS_SCOPE_HINT = "pass --scope to choose the files to mutate"
#: Baseline-scaled Mutant time limit: ``max(10 x baseline, 30 s)``, capped at 300 s.
_MUTANT_TIMEOUT_FACTOR = 10
_MUTANT_TIMEOUT_FLOOR_S = 30
_MUTANT_TIMEOUT_CAP_S = 300


#: Per-mutant sim-log cap. Deliberately small: this file is written once per
#: mutant, the mutant count has no upper bound, and a chatty testbench can emit
#: megabytes per run. What a reader needs from a
#: mutant log is the verdict and the last thing the design did, both in the
#: tail — the full transcript of a run that has already been graded is not
#: worth unbounded disk (cf. the 20 GB trace.fst incident).
_MUTANT_LOG_MAX_BYTES = 256 * 1024


def _infra_failure_reason(proc: subprocess.CompletedProcess) -> str:
    """The run-half's infra-failure reason, or ``""`` when the sim really ran.

    A run-half that never started the simulator (no built binary, missing
    cocotb install) prints ``[SIM_INFRA_ERROR] <reason>`` and exits non-zero.
    That exit code says nothing about the design, so both graders here — the
    baseline run and the kill/survive sweep — must ask this
    first and treat a non-empty answer as "no observation" (SETUP-F-41).
    """
    combined = (proc.stdout or "") + (proc.stderr or "")
    if not has_infra_error(combined):
        # A declared copyto input the TB could not open is the same "no
        # observation" the Simulation Flow classifies as infrastructure.
        return unstaged_runtime_input_diagnostic(combined)
    for line in combined.splitlines():
        if SIM_INFRA_ERROR_PREFIX in line:
            return line.split(SIM_INFRA_ERROR_PREFIX, 1)[1].strip()
    return "simulation harness failure"  # pragma: no cover - marker without a line


# Marker files (under the build dir) record the edalize binary dir relative to
# the Project Root and its resolved EDA-tool family. _run_elab writes them; the
# per-mutant run-many loop reads them so it never has to re-resolve the Target.
_EDALIZE_BINDIR_MARKER = ".booley_edalize_bindir"
_EDALIZE_EDA_TOOL_MARKER = ".booley_edalize_eda_tool"
# JSON list of the Target's declared ``copyto`` runtime inputs (build-relative).
_RUNTIME_INPUTS_MARKER = ".booley_runtime_inputs"
# First line of ``verilator --version``, probed in the baseline build's own environment.
_VERILATOR_VERSION_MARKER = ".booley_verilator_version"


@dataclass(frozen=True)
class _PinnedBuild:
    """What a prebuilt simulation needs to launch: its dir and declared inputs."""

    rel: str
    runtime_inputs: tuple[str, ...]


def _mutant_work(
    adapter: AdapterKind,
    pinned: _PinnedBuild,
    work_dir: Path,
    timeout: int,
    plusargs: Sequence[str],
) -> PreparedSimulationWork:
    """The fields every mutant launch carries; callers add the adapter's own."""
    return PreparedSimulationWork(
        adapter=adapter,
        build_dir=pinned.rel,
        run_cwd=resolve_run_cwd(work_dir),
        timeout_s=max(1, timeout - 5),
        plusargs=tuple(plusargs),
        runtime_inputs=pinned.runtime_inputs,
    )


def _record_build_markers(
    build_path: Path, prepared: PreparedSimulationBuild, work_dir: Path
) -> str:
    """Write the bin-dir, EDA-tool and runtime-input markers; return the bin-dir."""
    rel = posix_relpath(prepared.build_root, work_dir)
    (build_path / _EDALIZE_BINDIR_MARKER).write_text(rel, encoding="utf-8")
    (build_path / _EDALIZE_EDA_TOOL_MARKER).write_text(prepared.eda_tool, encoding="utf-8")
    (build_path / _RUNTIME_INPUTS_MARKER).write_text(
        json.dumps(declared_runtime_inputs(prepared.resolved, work_dir)), encoding="utf-8"
    )
    return rel


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MutationSpec:
    """One generated Mutant, in the form the variant applier consumes."""

    index: int
    category: str  # the Mutation Operator's name
    file: str
    line: int
    original_code: str
    mutated_code: str
    mutant_id: str = ""  # the generator's stable ID of this Mutant

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "id": self.mutant_id,
            "category": self.category,
            "file": self.file,
            "line": self.line,
            "original_code": self.original_code,
            "mutated_code": self.mutated_code,
        }


@dataclass
class MutationResult:
    index: int
    detected: bool = False
    invalid: bool = False
    #: The failure or timeout line of the run that detected the Mutant, else the
    #: tail of a survivor's output (both capped at 200 chars), or the reason a
    #: Mutant was inconclusive or invalid.
    sim_output_snippet: str = ""
    #: Project-relative path of this mutant's full simulator output. The
    #: snippet above is short; for a mutant the testbench failed to kill
    #: there is no error text at all, so the log is the only place the run's
    #: behaviour can be inspected. Empty when the log could not be written.
    log_path: str = ""
    #: First public Target test whose verdict killed this mutant. Cocotb
    #: batches are recovered from their structured results line; classic
    #: Targets use the first failing campaign unit.
    first_killing_test: str = ""
    #: The Mutant was detected because a test ran past its time limit, not
    #: because a test failed.
    timed_out: bool = False


@dataclass
class MutationTestRun:
    """Outcome of one test within a Target-wide mutation campaign."""

    test_name: str
    process: subprocess.CompletedProcess[str] | None = None
    timed_out: bool = False
    error: str = ""
    output: str = ""
    requires_cocotb_results: bool = False
    #: Wall-clock seconds the test took; the baseline's scale the Mutant limits.
    duration_s: float = 0.0


@dataclass(frozen=True)
class VariantSuiteVerdict:
    """One complete mutant-suite classification from trustworthy evidence."""

    detected: bool = False
    first_killing_test: str = ""
    inconclusive_reason: str = ""
    timed_out: bool = False
    #: The run whose verdict this is; set only by ``_classify_variant_suite`` on
    #: a detection, so the report snippet comes from the run that killed the Mutant.
    detecting_run: MutationTestRun | None = field(default=None, compare=False, repr=False)


@dataclass
class MutationSummary:
    specs: list[MutationSpec] = field(default_factory=list)
    results: list[MutationResult] = field(default_factory=list)

    @property
    def detected_count(self) -> int:
        return sum(1 for r in self.results if r.detected and not r.invalid)

    @property
    def detected_timeout_count(self) -> int:
        """Detected Mutants whose detection was a timeout (a subset of ``detected_count``)."""
        return sum(1 for r in self.results if r.detected and r.timed_out and not r.invalid)

    @property
    def not_detected_count(self) -> int:
        return sum(1 for r in self.results if not r.detected and not r.invalid)

    def surviving_specs(self) -> list[MutationSpec]:
        """Valid mutants the testbench failed to kill — the real coverage gaps.

        The single most actionable output of a mutation run is *which* mutant
        survived and where; reporting only a count left it unrecoverable
        (QA_REPORT C2.4).
        """
        result_map = {r.index: r for r in self.results}
        return [
            spec
            for spec in self.specs
            if (r := result_map.get(spec.index)) is not None and not r.detected and not r.invalid
        ]

    @property
    def invalid_count(self) -> int:
        return sum(1 for r in self.results if r.invalid)

    def classify(self) -> list[dict[str, Any]]:
        classified: list[dict[str, Any]] = []
        result_map = {r.index: r for r in self.results}
        for spec in self.specs:
            r = result_map.get(spec.index)
            if r is None:
                status = "untested"
            elif r.invalid:
                status = "invalid"
            elif r.detected:
                status = "detected"
            else:
                status = "not_detected"
            classified.append(
                {
                    "index": spec.index,
                    "id": spec.mutant_id,
                    "category": spec.category,
                    "file": spec.file,
                    "line": spec.line,
                    "status": status,
                    "timed_out": bool(r and r.detected and r.timed_out),
                    "sim_output_snippet": r.sim_output_snippet if r else "",
                    "log": (r.log_path if r else "") or None,
                    "first_killing_test": (r.first_killing_test if r else "") or None,
                }
            )
        return classified


# ---------------------------------------------------------------------------
# Language-neutral source-size budgeting
# ---------------------------------------------------------------------------


def compute_source_size_budget(scope_files: list[str], work_dir: Path) -> dict:
    """Choose an auto budget from source size without interpreting HDL syntax."""
    MIN_COUNT = 3
    MAX_COUNT = 25
    source_bytes = 0
    source_lines = 0
    readable_files = 0
    files = []
    for rel in scope_files:
        path = Path(rel) if Path(rel).is_absolute() else work_dir / rel
        source = path.read_bytes()
        readable_files += 1
        source_bytes += len(source)
        lines = source.count(b"\n") + int(bool(source) and not source.endswith(b"\n"))
        source_lines += lines
        files.append({"path": rel, "source_lines": lines, "source_bytes": len(source)})
    formula_count = max(MIN_COUNT, min(MAX_COUNT, round(math.sqrt(source_lines or 1))))

    return {
        "files": files,
        "method": "language_neutral_source_size",
        "readable_files": readable_files,
        "source_lines": source_lines,
        "source_bytes": source_bytes,
        "formula_count": formula_count,
        "MIN": MIN_COUNT,
        "MAX": MAX_COUNT,
    }


# ---------------------------------------------------------------------------
# Artifact generation (markdown summaries)
# ---------------------------------------------------------------------------

#: Said once after the survivors: why the default threshold is not "all".
EQUIVALENT_MUTANT_NOTE = (
    "Some surviving Mutants may be equivalent (they change no observable behavior), "
    "which is why the default threshold is 80 % of the count, not all of it."
)


def generate_specs_markdown(specs: list[MutationSpec]) -> str:
    lines = [
        "# Mutation Specifications",
        "",
        "| # | ID | Operator | File:Line | Original | Mutated |",
        "|---|----|----------|-----------|----------|---------|",
    ]
    for s in specs:
        lines.append(
            f"| {s.index} | {s.mutant_id} | {s.category} | "
            f"{s.file}:{s.line} | "
            f"`{_markdown_table_cell(s.original_code)}` | "
            f"`{_markdown_table_cell(s.mutated_code)}` |"
        )
    return "\n".join(lines) + "\n"


def _mutation_source_link(
    spec: MutationSpec,
    variant_paths: dict[int, str],
) -> str:
    label = f"{spec.file}:{spec.line}"
    path = variant_paths.get(spec.index)
    return f"[{label}]({quote(path, safe='/:._-')})" if path else label


def _markdown_table_cell(value: object) -> str:
    return " ".join(str(value).split()).replace("|", r"\|")


def _status_label(row: dict[str, Any]) -> str:
    """The human wording of a classified Mutant's status."""
    if row["timed_out"]:
        return "detected (timeout)"
    return str(row["status"]).replace("_", " ")


def _invalid_mutants_line(invalid_count: int) -> str:
    """Say that invalid Mutants use up the threshold's margin (they can never be detected)."""
    return (
        f"{invalid_count} invalid Mutants could not be judged and count as not "
        "detected toward the threshold."
    )


def _identity_lines(identity: Mapping[str, Any]) -> list[str]:
    """Markdown lines naming everything that reproduces this run's Mutants."""
    lines = [
        "## Identity",
        "",
        f"- Seed: {identity['seed']}",
        f"- Count: {identity['count']}",
        f"- Generator version: {identity['generator_version']}",
        f"- pyslang version: {identity['pyslang_version']}",
    ]
    if "verilator_version" in identity:
        lines.append(f"- Verilator version: {identity['verilator_version']}")
    lines.append("- Scope file hashes:")
    lines += [f"  - `{name}`: {digest}" for name, digest in identity["scope_hashes"].items()]
    return [*lines, ""]


def generate_results_markdown(
    summary: MutationSummary,
    min_detected: int,
    *,
    variant_paths: dict[int, str] | None = None,
    identity: Mapping[str, Any] | None = None,
) -> str:
    variant_paths = variant_paths or {}
    classified = summary.classify()
    specs_by_index = {spec.index: spec for spec in summary.specs}
    valid = summary.detected_count + summary.not_detected_count
    status = "PASS" if summary.detected_count >= min_detected else "FAIL"
    lines = [
        "# Mutation Test Results",
        "",
        f"Detected: {summary.detected_count}/{valid} "
        f"(required: {min_detected}/{valid}) -- **{status}**",
        "",
        f"- Detected: {summary.detected_count}",
        f"- Detected (timeout): {summary.detected_timeout_count} (included in Detected)",
        f"- Not detected: {summary.not_detected_count}",
        f"- Invalid: {summary.invalid_count}",
        "",
    ]
    if summary.invalid_count:
        lines += [_invalid_mutants_line(summary.invalid_count), ""]
    # Call out the survivors explicitly: the actionable output of the run.
    survivors = summary.surviving_specs()
    if survivors:
        # Each survivor cites its own sim log: a killed mutant leaves a failure
        # message to read, a survivor leaves a clean passing run, so the log is
        # the only way to see what the design actually did under the mutation.
        log_by_index = {r.index: r.log_path for r in summary.results}
        lines += ["## Not detected", ""]
        for s in survivors:
            log = log_by_index.get(s.index)
            where = f" — sim log: {log}" if log else ""
            lines.append(
                f"- **{_mutation_source_link(s, variant_paths)}** ({s.category}): "
                f"`{s.original_code}` -> `{s.mutated_code}`{where}"
            )
        lines += ["", EQUIVALENT_MUTANT_NOTE, ""]
    lines += [
        "| # | Mutation | Mutated RTL | Status | First killing test | Snippet |",
        "|---|----------|-------------|--------|--------------------|---------|",
    ]
    for c in classified:
        spec = specs_by_index[c["index"]]
        lines.append(
            f"| {c['index']} | {_markdown_table_cell(c['category'])}: "
            f"`{_markdown_table_cell(spec.original_code)}` → "
            f"`{_markdown_table_cell(spec.mutated_code)}` | "
            f"{_mutation_source_link(spec, variant_paths)} | "
            f"{_status_label(c)} | "
            f"{_markdown_table_cell(c['first_killing_test'] or '')} | "
            f"{_markdown_table_cell(c['sim_output_snippet'][:60])} |"
        )
    lines.append("")
    if identity is not None:
        lines += _identity_lines(identity)
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Run configuration
# ---------------------------------------------------------------------------


class UnsupportedSimTargetError(RuntimeError):
    """The mutation loop cannot drive this sim Target, and says why.

    Raised at ``_run`` time (before any build) so an unsupported Target costs
    one ``.core`` read. The message is surfaced verbatim as the Flow's
    ``report_text`` — it must name the Target and the concrete reason
    (SETUP-F-40).
    """


@dataclass(frozen=True)
class CocotbSimTarget:
    """The cocotb identity of a sim Target (ADR 0034).

    ``module`` is the Target's declared ``cocotb_module``; ``eda_tool`` is the
    normalized run-half family (``icarus``/``verilator``). Absence of this
    object means the Target is a classic SV Target driven through the pinned
    ``V<top>`` binary.
    """

    module: str
    eda_tool: str


@dataclass
class MutationRunPlan:
    """Resolved run configuration.

    Bundles the scope + budget cluster computed once in ``_run`` and threaded
    unchanged down to result assembly, instead of re-listing the fields at
    every hop.
    """

    scope_files: list[str]
    scope_hashes: dict[str, str]
    work_dir: Path
    target: str
    tb_top: str
    report_dir: Path | None
    min_detected: int
    count: int
    auto_mode: bool
    formula_count: int
    source_size_budget: dict | None
    seed: int = 0
    #: ``min_detected`` came from the caller or the criterion, so a generator
    #: shortfall does not recompute it.
    explicit_min_detected: bool = True
    #: The scope is the Target's own RTL because neither ``--scope`` nor a criterion named
    #: one, so only then does "pass --scope" fit a failure.
    scope_defaulted: bool = False


@dataclass
class RunResultInputs:
    """Per-run outcome fed to ``_build_run_result``.

    The run-specific counterpart to :class:`MutationRunPlan`: values produced by
    the run (timings, the summary, the identity) that the plan config alone
    cannot supply.
    """

    summary: MutationSummary
    count: int  # mutations actually run this invocation (may be < plan.count)
    tester_elapsed: float
    variants: MutationVariantPlan
    #: What reproduces the Mutants: scope hashes, generator and pyslang
    #: versions, seed and requested count.
    identity: dict[str, Any] = field(default_factory=dict)
    #: The note when fewer Mutants were scored than requested.
    shortfall: str | None = None
    #: Nothing was killed, yet every mutation is provably live — the Target's
    #: tests simply never exercise this scope (SETUP-F-38).
    coverage_gap: bool = False
    #: Exact-replacement evidence reported so the verdict is auditable.
    evidence: dict[str, Any] = field(default_factory=dict)
    #: IDs of the Mutants dropped for building the baseline's C++, in drop order.
    #: The Record's detail lists them; the report never does.
    equivalent_dropped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GeneratedMutants:
    """The run's candidate Mutants in pick order, with the plan that resolved the first ones.

    ``variants`` anchors the first ``plan.count`` candidates; ``with_proposals`` anchors a
    replacement against the same pristine source.
    """

    plan: MutationRunPlan
    variants: MutationVariantPlan
    identity: dict[str, Any]
    ranked: tuple[Mutant, ...]


@dataclass(frozen=True)
class Baseline:
    """What the pristine baseline run learned that the Mutant sweep needs."""

    timeouts: Mapping[str, int]  # each test's Mutant time limit
    #: Digests of the C++ Verilator generated for the baseline; ``None`` on a Target not
    #: simulated with Verilator, where no Mutant is compared.
    generated_cpp: Mapping[str, str] | None = None
    verilator_version: str | None = None


@dataclass(frozen=True)
class VariantSweep:
    """The Mutants a sweep kept and scored, numbered 1..n, and the ones it dropped."""

    specs: list[MutationSpec]
    results: list[MutationResult]
    dropped: list[str]  # IDs of the Mutants whose build equals the baseline's
    elapsed: float
    inconclusive: str  # why a Mutant's outcome cannot be trusted; empty when all can


# ---------------------------------------------------------------------------
# Flow implementation
# ---------------------------------------------------------------------------


class MutationFlow(BuiltinFlow[MutationRequest]):
    """Deterministic mutation testing of one Simulation Target; it runs no model."""

    request_type = MutationRequest
    argument_adapter = MutationArguments

    name: str = "mutation"
    description: str = (
        "Deterministic mutation testing: seeded Mutants from fixed Mutation "
        "Operators, each built and tested in isolation"
    )
    code_modifying: bool = False

    target_required: bool = True
    target_help: str = SurfaceHelp(
        "Exactly one Simulation Target. The CLI accepts one --target value; "
        "MCP accepts one Target selector string.",
        cli=(
            "The one Simulation Target whose tests judge each mutation. "
            "List available Targets with `booley targets --for sim`."
        ),
    )
    cli_help: ClassVar[CliHelp | None] = CliHelp(
        summary=(
            "Measure how well the testbench catches bugs. Small deliberate faults "
            "(Mutants) are generated deterministically from the Target's RTL (or the "
            "files in --scope), the Simulation Target's tests run against each one, "
            "and the report says how many were detected. No model is involved: the "
            "same RTL, --count and --seed give the same Mutants. Use it once the "
            "tests pass, to find checks the testbench is missing. Mutants are built "
            "in private staged copies; your sources are not changed."
        ),
        example="booley flow mutation --target sim_soc --count 10 --seed 0",
    )
    # A run builds and simulates, yet keeps the admission class it always had.
    JOB_CLASS: ClassVar[str | None] = job_slots.CLASS_LIGHT
    default_timeout: ClassVar[int] = 1800
    satisfies: ClassVar[list[str]] = ["mutation_score"]
    #: Set by ``_apply_campaign_defaults``: no ``--scope`` and no criterion scope was given.
    _scope_defaulted: bool = False

    # ------------------------------------------------------------------
    # Command-backed Flow hooks — _run() drives the campaign directly.
    # ------------------------------------------------------------------

    def _build_command(self) -> list[str]:
        """Not used — MutationFlow overrides _run()."""
        return []

    def _interpret_result(self, result: SubprocessResult) -> EndpointOutcome:
        """Not used — MutationFlow overrides _run()."""
        return EndpointOutcome()

    # ------------------------------------------------------------------
    # Scope / DUT helpers
    # ------------------------------------------------------------------

    def _scope_files(self) -> list[str]:
        """The scope's canonical paths, each once, at its first position."""
        return list(
            dict.fromkeys(
                fusesoc_registry.canonical_project_path(self.args.work_dir, raw)
                for value in self.args.scope.split(",")
                if (raw := value.strip())
            )
        )

    def _resolve_campaign(self, scope: str | None) -> TargetCampaign | EndpointOutcome | None:
        """Resolve this Target's campaign for *scope*; ``None`` when no scope is known."""
        try:
            return resolve_target_campaign(
                self.args.target,
                self.satisfies,
                self.state.criteria,
                explicit_scope=scope,
                **self._registered_tests(self.args.target),
                enforce_scope_match=(
                    self.state.strict_criteria and not getattr(self.args, "diagnostic", False)
                ),
            )
        except CampaignScopeError as exc:
            if exc.reason == "missing":
                return None
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=("mutation: --scope does not match the sealed Target criterion scope"),
            )
        except UnsupportedSimTargetError as exc:
            return EndpointOutcome(exit_code=EXIT_ERROR, report_text=str(exc))
        except (NoRunnableTestsError, TargetSectionConflictError) as exc:
            return EndpointOutcome(exit_code=EXIT_ERROR, report_text=f"mutation: {exc}")

    def _apply_campaign_defaults(self) -> EndpointOutcome | None:
        """Default mutation controls from this Target's criterion parameters.

        With neither ``--scope`` nor a criterion scope, the scope is the Target's own RTL.
        """
        if self.args.scope is not None and not any(
            entry.strip() for entry in self.args.scope.split(",")
        ):
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=(
                    "mutation: --scope is empty; name at least one RTL file, "
                    "or omit --scope to mutate the Target's own RTL"
                ),
            )
        campaign = self._resolve_campaign(self.args.scope)
        self._scope_defaulted = campaign is None
        if campaign is None:
            default_scope = self._default_scope()
            if not default_scope:
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"mutation: Target {self.args.target!r} has no mutable RTL "
                        "(only testbench, vendored, generated or non-Verilog inputs); "
                        "pass --scope."
                    ),
                )
            campaign = self._resolve_campaign(",".join(default_scope))
        if isinstance(campaign, EndpointOutcome):
            return campaign
        assert campaign is not None, "an explicit scope is never missing"
        self._target_campaign = campaign
        self.args.scope = campaign.scope_arg
        params = campaign.params_for("mutation_score")
        if self.args.count is None:
            if params.get("auto") is True:
                self.args.count = "auto"
            else:
                self.args.count = params.get("total", 10)
        if self.args.min_detected is None and params.get("min_detected") is not None:
            self.args.min_detected = as_int(params.get("min_detected"))
        return None

    def _default_scope(self) -> list[str]:
        """The Project's own mutable RTL in the Target's closure, in Target order.

        "Own RTL" spans the ``depend`` closure, not only the root core's
        filesets: in a layered Project the root fileset owns the harness while
        the DUT arrives through a dependency (see :meth:`_dut_files`).  Testbench,
        vendored and generated files, include headers, inputs outside the Project
        and non-Verilog inputs are not mutable.  A Target that cannot be
        inspected has no scope; the run's own validation reports why.
        """
        inspection = self._inspect_target()
        if inspection is None:
            return []
        inputs = inspection.inputs
        unmutable = fusesoc_registry.files_tagged(self.args.work_dir, _UNMUTATED_FILE_TAGS)
        scope: dict[str, None] = {}  # ordered set
        for item in inputs:
            path = fusesoc_registry.canonical_project_path(self.args.work_dir, item.path)
            if (
                item.file_type.startswith(_HDL_FILE_TYPES)
                and not item.is_include
                and "tb" not in item.tags
                and path not in unmutable
                and not Path(path).is_absolute()
                and Path(path).suffix.lower() in _MUTATION_SUFFIXES
            ):
                scope[path] = None
        return list(scope)

    def _validate_scope_against_target(self, scope_files: list[str]) -> EndpointOutcome | None:
        """Fail fast when a ``--scope`` entry isn't a source file of ``--target``.

        A plausible-but-wrong scope (classically the stealth-cores mirror
        ``.booley/cores/rtl/foo.sv`` instead of the repo-relative
        ``rtl/foo.sv``) would otherwise be discovered only after Mutant
        generation. Resolving the scope against the Target's fileset up front (a
        subprocess-free ``.core`` read, <1s) turns that into an instant,
        self-correcting error with a "did you mean" hint.

        Every refusal is an ``ERROR`` (exit 2): the input is unusable, so no Mutant
        was generated and no test ran.  Inspection failures and empty source sets
        refuse before build work.
        """
        for entry in scope_files:
            if problem := self._scope_entry_problem(entry):
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=f"mutation: --scope entry '{entry}' {problem}",
                )
        target = getattr(self.args, "target", "") or ""
        if not target:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="mutation: scope validation requires a Target",
            )
        try:
            catalog = TargetCatalog.build(self.args.work_dir)
            inspection = catalog.inspect(catalog.select(target))
            resolved = list(inspection.rtl_files)
            resolved_set = {
                fusesoc_registry.canonical_project_path(self.args.work_dir, path)
                for path in resolved
            }
            testbench_set = {
                fusesoc_registry.canonical_project_path(self.args.work_dir, item.path)
                for item in inspection.inputs
                if "tb" in item.tags
            }
        except Exception as exc:  # noqa: BLE001 — boundary diagnostic refuses before model/build
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"mutation: cannot inspect Target '{target}' for scope {scope_files}: {exc}",
            )
        if not resolved:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"mutation: Target '{target}' has no RTL sources for scope {scope_files}",
            )
        by_base = {Path(r).name: r for r in resolved}
        for entry in scope_files:
            problem = self._target_entry_problem(
                entry, target, resolved, resolved_set, testbench_set, by_base
            )
            if problem:
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=f"mutation: --scope entry '{entry}' {problem}",
                )
        return None

    def _target_entry_problem(
        self,
        entry: str,
        target: str,
        resolved: list[str],
        resolved_set: set[str],
        testbench_set: set[str],
        by_base: dict[str, str],
    ) -> str | None:
        """Why *entry* is not a mutable source of *target*, or ``None``.

        A file the Target lists but that is gone from disk is refused here too, so a
        dry run fails exactly where the real run's generator would.
        """
        suggestion = by_base.get(Path(entry).name)
        hint = f" Did you mean '{suggestion}'?" if suggestion and suggestion != entry else ""
        if not (self.args.work_dir / entry).exists():
            return f"does not exist.{hint}"
        if entry in resolved_set:
            return None
        if entry in testbench_set:
            return f"is a testbench file of Target '{target}'; testbench files are never mutated"
        return (
            f"does not match any source file resolved for config '{target}'.{hint} "
            f"Resolved sources: {', '.join(resolved)}"
        )

    def _scope_entry_problem(self, entry: str) -> str | None:
        """Why *entry* cannot be mutated whatever the Target is, or ``None``.

        The text completes ``--scope entry '<entry>' ...``.
        """
        if Path(entry).is_absolute():  # canonicalization leaves only outside paths absolute
            return "is outside the Project"
        if (self.args.work_dir / entry).is_dir():
            return "is a directory; name the RTL files in it"
        if Path(entry).suffix.lower() not in _MUTATION_SUFFIXES:
            return "is not an HDL RTL mutation source"
        return None

    def _dut_files(self) -> list[str]:
        """Resolve the Target RTL closure, reported by the dry-run preview.

        The read spans the ``depend`` closure, not just the root core's own
        filesets. In a layered repo the sim Target's root fileset owns the
        harness while the DUT arrives transitively — Ibex's
        ``ibex_top_tracing.sv`` is a dependency of the Target that
        instantiates it — so a root-only read finds no DUT at all and
        mutation fails before injection (F-27).
        """
        target = getattr(self.args, "target", "") or ""
        if not target:
            return []
        try:
            catalog = TargetCatalog.build(self.args.work_dir)
            return list(catalog.inspect(catalog.select(target)).rtl_files)
        except Exception:  # noqa: BLE001 — best-effort source-file lookup; degrades to an empty list
            return []

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def _resolve_count(
        self,
        scope_files: list[str],
        work_dir: Path,
    ) -> tuple[int, dict | None, int, bool]:
        """Resolve the mutation count (int | "auto").

        Returns ``(count, source_size_budget, formula_count, auto_mode)`` and emits the
        matching progress line.
        """
        if self.args.count == "auto":
            source_size_budget = compute_source_size_budget(scope_files, work_dir)
            formula_count = source_size_budget["formula_count"]
            timeout = (self._timeout_ms() + 999) // 1000
            budget_cap = max(3, timeout // 150)
            source_size_budget.update(timeout_seconds=timeout, budget_cap=budget_cap)
            count = min(formula_count, budget_cap)
            auto_mode = True
        else:
            count = self.args.count
            source_size_budget = None
            formula_count = count
            budget_cap = count
            auto_mode = False

        if auto_mode:
            self.emit_progress(
                f"auto-scaled: {count} Mutants "
                f"(source-size formula {formula_count}, budget cap {budget_cap})",
            )
        else:
            self.emit_progress(f"target: {count} Mutants")
        return count, source_size_budget, formula_count, auto_mode

    def _run(self) -> EndpointOutcome:
        if campaign_error := self._apply_campaign_defaults():
            return campaign_error
        prepared = self._prepare_run_plan()
        if isinstance(prepared, EndpointOutcome):
            return prepared
        result = self._run_campaign(prepared)
        self._add_residue_warning(result, prepared.scope_hashes, prepared.work_dir)
        return result

    def _prepare_run_plan(self) -> MutationRunPlan | EndpointOutcome:
        work_dir = self.args.work_dir
        target = self.args.target
        scope_files = self._scope_files()
        if validation_error := self._validate_run_inputs(target, work_dir, scope_files):
            return validation_error
        try:
            tb_top = tb_top_for_target(target, work_dir, resolved=None)
        except (OSError, fusesoc_registry.FuseSocError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"mutation: could not derive Target toplevel: {exc}",
            )
        if not tb_top:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="mutation: selected Target does not declare a toplevel",
            )
        count, source_size_budget, formula_count, auto_mode = self._resolve_count(
            scope_files,
            work_dir,
        )
        explicit_min_detected = self.args.min_detected is not None
        min_detected = (
            self.args.min_detected if explicit_min_detected else default_min_detected(count)
        )
        if count_error := self._validate_count(count, min_detected, self.args.seed):
            return count_error
        plan = MutationRunPlan(
            scope_files=scope_files,
            scope_hashes=workspace.compute_scope_hashes(scope_files, work_dir),
            work_dir=work_dir,
            target=target,
            tb_top=tb_top,
            report_dir=self.args.report_dir,
            min_detected=min_detected,
            count=count,
            auto_mode=auto_mode,
            formula_count=formula_count,
            source_size_budget=source_size_budget,
            seed=self.args.seed,
            explicit_min_detected=explicit_min_detected,
            scope_defaulted=self._scope_defaulted,
        )
        if getattr(self.args, "dry_run", False):
            return self._dry_run_preview(plan)
        self.args.count = count
        return plan

    def _dry_run_preview(self, plan: MutationRunPlan) -> EndpointOutcome:
        """Describe the validated mutation campaign without executing it."""
        budget = plan.source_size_budget or compute_source_size_budget(
            plan.scope_files,
            plan.work_dir,
        )
        summary = (
            f"mutation dry-run: target={plan.target}; "
            f"scope={len(plan.scope_files)} file(s); top={plan.tb_top}; "
            f"count={plan.count}; min_detected={plan.min_detected}; seed={plan.seed}"
        )
        lines = (
            self._auto_count_explanation(plan, budget)
            if plan.auto_mode
            else [f"count source: fixed; count={plan.count} (not auto-derived)"]
        )
        lines.append(summary)
        result = self._dry_run_outcome(
            summary="\n".join(lines),
            detail={
                "target": plan.target,
                "scope": plan.scope_files,
                "tb_top": plan.tb_top,
                "rtl_closure": self._dut_files(),
                "tests": [unit.display_name for unit in self._target_campaign.execution_units()],
                "count": plan.count,
                "min_detected": plan.min_detected,
                "seed": plan.seed,
                "source_size_budget": budget,
            },
        )
        result.display_lines = lines
        result.summary = summary
        return result

    @staticmethod
    def _dry_run_outcome(*, summary: str, detail: dict[str, Any]) -> EndpointOutcome:
        """Build the successful, non-acceptance dry-run result."""
        return EndpointOutcome(
            exit_code=EXIT_SUCCESS,
            report_text=summary,
            display_lines=[summary],
            detail={"mode": "dry_run", **detail},
        )

    def _auto_count_explanation(self, plan: MutationRunPlan, budget: dict) -> list[str]:
        """Render the source formula and time cap that resolved this preview's count."""
        total = budget["source_lines"]
        files = [
            f"  {f['path']}: {f['source_lines']} lines ({f['source_bytes']} bytes)"
            for f in budget["files"]
        ]
        return [
            "source-size breakdown (source lines drive the count):",
            *files,
            f"total: {total} lines -> clamp(round(sqrt({total} or 1)), 3, 25) = {plan.formula_count}",
            f"time cap: max(3, {budget['timeout_seconds']}s // 150) = {budget['budget_cap']}",
            f"count (auto): min({plan.formula_count}, {budget['budget_cap']}) = {plan.count}",
        ]

    @staticmethod
    def _validate_count(count: int, min_detected: int, seed: int) -> EndpointOutcome | None:
        if count <= 0:
            return EndpointOutcome(exit_code=EXIT_ERROR, report_text="--count must be positive")
        if seed < 0:
            return EndpointOutcome(
                exit_code=EXIT_ERROR, report_text="--seed must be a non-negative integer"
            )
        if min_detected < 0 or min_detected > count:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="--min-detected must be between 0 and --count",
            )
        return None

    def _validate_run_inputs(
        self,
        target: str,
        work_dir: Path,
        scope_files: list[str],
    ) -> EndpointOutcome | None:
        if len([part for part in target.split(",") if part.strip()]) != 1:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="mutation: --target must select exactly one Target",
            )
        if scope_error := self._validate_scope_against_target(scope_files):
            return scope_error
        try:
            self._validate_target_runner(target, work_dir)
            self.cocotb_target(target, work_dir)
            self._target_test_suite(target)
        except UnsupportedSimTargetError as exc:
            return EndpointOutcome(exit_code=EXIT_ERROR, report_text=str(exc))
        return None

    # ------------------------------------------------------------------
    # Campaign
    # ------------------------------------------------------------------

    def _run_campaign(self, plan: MutationRunPlan) -> EndpointOutcome:
        """Generate the Mutants, build and run the baseline, then sweep every Mutant."""
        # The retired Specialist kept its scratch tree under its own name; nothing reads it.
        shutil.rmtree(workspace.legacy_runtime_dir(), ignore_errors=True)
        # A smaller --count than the last run must not leave its extra builds behind.
        shutil.rmtree(workspace.builds_dir(), ignore_errors=True)
        shutil.rmtree(workspace.variants_dir(), ignore_errors=True)
        generated = self._generate(plan)
        if isinstance(generated, EndpointOutcome):
            return generated
        plan = generated.plan
        baseline = self._run_pristine_baseline(plan)
        if isinstance(baseline, EndpointOutcome):
            return baseline
        sweep = self._run_variant_sweep(plan, generated, baseline)
        if not sweep.specs:
            return self._no_scorable_mutants(plan, sweep)
        count = len(sweep.specs)
        if not plan.explicit_min_detected:
            # Fewer Mutants than requested lower the default threshold with the count.
            plan = replace(plan, min_detected=default_min_detected(count))
        summary = MutationSummary(specs=sweep.specs, results=sweep.results)
        variants = generated.variants.with_proposals(sweep.specs)
        identity = dict(generated.identity)
        if baseline.verilator_version is not None:
            identity["verilator_version"] = baseline.verilator_version
        inputs = RunResultInputs(
            summary=summary,
            count=count,
            tester_elapsed=sweep.elapsed,
            variants=variants,
            identity=identity,
            shortfall=_shortfall_message(count, plan.count),
            coverage_gap=self._is_variant_coverage_gap(summary, plan.min_detected),
            evidence=self._variant_evidence(variants),
            equivalent_dropped=sweep.dropped,
        )
        if sweep.inconclusive:
            return self._variant_infra_error(plan, inputs, sweep.inconclusive)
        self.emit_progress(
            f"done: {summary.detected_count}/"
            f"{summary.detected_count + summary.not_detected_count} detected"
        )
        return self._build_run_result(plan, inputs)

    def _inspect_target(self) -> TargetInspection | None:
        """The Target's declared inputs and parameters, or ``None`` when it cannot be read."""
        try:
            catalog = TargetCatalog.build(self.args.work_dir)
            return catalog.inspect(catalog.select(self.args.target))
        except fusesoc_registry.FuseSocError:
            return None

    def _generator_inputs(self) -> tuple[dict[str, str], list[str]]:
        """The preprocessor ``defines`` and ``include_dirs`` the Target's build gives its sources.

        Include directories are those of the Project's non-testbench include
        headers, as :attr:`ResolvedTarget.rtl_include_dirs` takes them.
        """
        inspection = self._inspect_target()
        if inspection is None:
            return {}, []
        defines: dict[str, str] = {}
        for name, spec in inspection.parameters.items():
            if not isinstance(spec, Mapping) or spec.get("paramtype") != "vlogdefine":
                continue
            default = spec.get("default")
            if default is True:
                defines[name] = ""
            elif default not in (False, None, ""):
                defines[name] = str(default)
        include_dirs: dict[str, None] = {}  # ordered set
        for item in inspection.inputs:
            if item.is_include and "tb" not in item.tags:
                path = fusesoc_registry.canonical_project_path(self.args.work_dir, item.path)
                if not Path(path).is_absolute():
                    include_dirs[PurePosixPath(path).parent.as_posix()] = None
        return defines, list(include_dirs)

    def _generate(self, plan: MutationRunPlan) -> GeneratedMutants | EndpointOutcome:
        """Select the run's Mutants with the deterministic generator and resolve their variants."""
        defines, include_dirs = self._generator_inputs()
        try:
            identity = {
                "scope_hashes": plan.scope_hashes,
                "generator_version": GENERATOR_VERSION,
                "pyslang_version": parser_version(),
                "seed": plan.seed,
                "count": plan.count,
            }
            selection = generate_mutants(
                plan.work_dir,
                plan.scope_files,
                plan.count,
                plan.seed,
                defines=defines,
                include_dirs=include_dirs,
            )
            if not selection.mutants:
                return self._generation_failure(
                    plan, "no Mutation Operator site in the scope files", _no_site_hint(plan)
                )
            specs = [
                _spec_of(index, mutant) for index, mutant in enumerate(selection.mutants, start=1)
            ]
            variants = MutationVariantPlan.resolve(specs, plan.work_dir, plan.scope_files)
        except SourceParseError as exc:
            files = ", ".join(name for name, _ in exc.failures)
            hint = (
                f"check that {files} parse with the defines "
                f"({_names_or_none([f'{k}={v}' if v else k for k, v in defines.items()])}) and "
                f"include directories ({_names_or_none(include_dirs)}) the Target gives them"
            )
            return self._generation_failure(plan, str(exc), hint)
        except (MutantGeneratorError, BoundaryError, MutationVariantError) as exc:
            return self._generation_failure(plan, str(exc), None)
        return GeneratedMutants(plan, variants, identity, selection.ranked)

    @staticmethod
    def _generation_failure(
        plan: MutationRunPlan, reason: str, hint: str | None
    ) -> EndpointOutcome:
        """The ``ERROR`` for Mutants that cannot be generated: *reason*, then the fitting *hint*."""
        shown = reason if hint is None else f"{reason}\n{hint}"
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=f"mutation: cannot generate Mutants: {shown}",
            detail=_failure_detail(
                phase="mutant_generation",
                reason=shown,
                specs=[],
                work_dir=plan.work_dir,
            ),
        )

    def _run_pristine_baseline(self, plan: MutationRunPlan) -> Baseline | EndpointOutcome:
        """Build and run the byte-identical project before any Mutant is applied.

        Returns each test's Mutant time limit, scaled from its baseline duration, and, on a
        Verilator Target, the C++ Verilator generated and its version.
        """
        if (run_cwd_error := self._baseline_run_cwd_error(plan)) is not None:
            return run_cwd_error
        build_path = workspace.baseline_build_dir()
        shutil.rmtree(build_path, ignore_errors=True)
        build_path.mkdir(parents=True, exist_ok=True)
        elab = self._run_elab(plan.target, plan.work_dir, build_path)
        elab_output = (elab.stdout or "") + (elab.stderr or "")
        if elab.returncode != 0:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="mutation baseline did not elaborate from pristine source\n"
                + tail(elab_output, 50),
                detail=_failure_detail(
                    phase="baseline_elaboration",
                    reason="pristine source did not elaborate",
                    specs=[],
                    work_dir=plan.work_dir,
                    log_tail=tail(elab_output, 50),
                ),
            )

        cpp, version = self._baseline_verilator_build(plan.work_dir, build_path)
        runs = self._run_target_test_suite(
            plan.target,
            plan.work_dir,
            build_path,
            plan.tb_top,
        )
        output = self._suite_output(runs)
        self._persist_baseline_log(output)
        inconclusive = self._suite_inconclusive_reason(runs)
        if inconclusive:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"mutation baseline was inconclusive: {inconclusive}\n"
                + tail(output, 50),
            )
        if not self._baseline_suite_passed(runs):
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="mutation baseline failed on pristine source\n" + tail(output, 50),
            )
        timeouts = {run.test_name: _mutant_timeout(run.duration_s) for run in runs}
        return Baseline(timeouts, cpp, version)

    @staticmethod
    def _baseline_run_cwd_error(plan: MutationRunPlan) -> EndpointOutcome | None:
        """The error that ends the run when the Target's literal run directory is unusable."""
        run_cwd_problem = literal_run_cwd_problem(plan.work_dir)
        if run_cwd_problem is None:
            return None
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=f"mutation baseline cannot run: {run_cwd_problem}",
            detail=_failure_detail(
                phase="baseline_run_cwd",
                reason=run_cwd_problem,
                specs=[],
                work_dir=plan.work_dir,
                log_tail="",
            ),
        )

    @staticmethod
    def _baseline_verilator_build(
        work_dir: Path, build_path: Path
    ) -> tuple[dict[str, str] | None, str | None]:
        """The baseline's generated C++ digests and Verilator version, or ``(None, None)``.

        Only a Target simulated with Verilator has either; a Target that is not leaves
        every Mutant to be simulated.
        """
        tool_marker = build_path / _EDALIZE_EDA_TOOL_MARKER
        if (
            not tool_marker.is_file()
            or tool_marker.read_text(encoding="utf-8").strip() != "verilator"
        ):
            return None, None
        generated = work_dir / (build_path / _EDALIZE_BINDIR_MARKER).read_text(encoding="utf-8")
        version = (build_path / _VERILATOR_VERSION_MARKER).read_text(encoding="utf-8")
        return generated_cpp(generated, build_path, work_dir), version

    def _variant_infra_error(
        self,
        plan: MutationRunPlan,
        inputs: RunResultInputs,
        reason: str,
    ) -> EndpointOutcome:
        """The run has no trustworthy verdict: ``inputs`` holds only the Mutants it kept."""
        detail = _failure_detail(
            phase="variant_infrastructure",
            reason=reason,
            specs=inputs.summary.specs,
            work_dir=plan.work_dir,
            summary=inputs.summary,
            min_detected=plan.min_detected,
            requested=plan.count,
        )
        detail["equivalent_dropped"] = inputs.equivalent_dropped
        self._attach_campaign_artifacts(plan, inputs, detail)
        report_text = f"mutation campaign produced no trustworthy verdict: {reason}"
        artifact_lines = self._artifact_display_lines(detail)
        if artifact_lines:
            report_text += "\n\nArtifacts:\n" + "\n".join(f"  {line}" for line in artifact_lines)
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=report_text,
            detail=detail,
            display_lines=artifact_lines,
        )

    def _no_scorable_mutants(self, plan: MutationRunPlan, sweep: VariantSweep) -> EndpointOutcome:
        """Every candidate built the baseline's C++ or was not scored: nothing to report."""
        reason = "no candidate Mutant changes what the Target builds"
        detail = _failure_detail(
            phase="mutant_selection", reason=reason, specs=[], work_dir=plan.work_dir
        )
        detail["equivalent_dropped"] = sweep.dropped
        hint = (
            f"none of the Mutants of {', '.join(plan.scope_files)} changes what Target "
            f"'{plan.target}' builds; scope files whose modules '{plan.tb_top}' elaborates"
        )
        if plan.scope_defaulted:
            hint += f"; {_PASS_SCOPE_HINT}"
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=f"mutation: {reason}\n{hint}",
            detail=detail,
        )

    @staticmethod
    def _variant_evidence(variants: MutationVariantPlan) -> dict[str, Any]:
        return {
            "mode": "isolated_exact_replacement",
            "mutations_applied": True,
            "source_fingerprint": variants.source_fingerprint(),
            "resolved": [
                {
                    "index": mutation.index,
                    "file": mutation.file,
                    "line": mutation.line,
                    "start_byte": mutation.start,
                    "end_byte": mutation.end,
                }
                for mutation in variants.mutations
            ],
        }

    @staticmethod
    def _is_variant_coverage_gap(summary: MutationSummary, min_detected: int) -> bool:
        valid = summary.detected_count + summary.not_detected_count
        return min_detected > 0 and valid > 0 and summary.detected_count == 0

    def _run_elab(
        self,
        target: str,
        work_dir: Path,
        build_path: Path,
        mutation: tuple[MutationVariantPlan, int] | None = None,
    ) -> subprocess.CompletedProcess:
        """Build pristine source or one replacement in private exported copies."""
        try:
            _handle, prepared = self._resolve_elab_target(target, work_dir, build_path, mutation)
        except (
            Exception  # noqa: BLE001 — isolate resolve failure; surface as return code 1
        ) as exc:
            return subprocess.CompletedProcess(
                args=["fusesoc", "run", "--setup", "--target", target],
                returncode=1,
                stdout="",
                stderr=f"FuseSoC target resolution failed: {exc}",
            )
        configured_tool = prepared.eda_tool
        self.record_eda_tool(target, configured_tool)
        _record_build_markers(build_path, prepared, work_dir)
        # Mutants compile through the same managed Project compiler cache as
        # ordinary Simulation; unchanged generated C++ hits across variants.
        build_environment = {**os.environ, **simulation_build_environment(prepared)}
        if mutation is None and configured_tool == "verilator":
            # Only the baseline's version is recorded: a Mutant builds with the same tool.
            (build_path / _VERILATOR_VERSION_MARKER).write_text(
                verilator_version(build_environment), encoding="utf-8"
            )
        return subprocess.run(
            list(prepared.make_argv),
            cwd=work_dir,
            env=relocate_python_artifacts(
                build_environment,
                python_artifact_root(build_environment, build_path / "python-artifacts"),
                pytest_scope=f"mutation-build:{build_path.resolve()}",
            ),
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )

    @staticmethod
    def _resolve_elab_target(
        target: str,
        work_dir: Path,
        build_path: Path,
        mutation: tuple[MutationVariantPlan, int] | None = None,
    ) -> tuple[Any, PreparedSimulationBuild]:
        """Resolve one mutation Target without swallowing tool-authority errors."""
        catalog = TargetCatalog.build(work_dir)
        handle = catalog.select(target, for_flow="sim")
        mirror_root: Path | None = None
        private_library: dict[str, fusesoc_registry.CoreLibraryPlan] = {}
        if mutation is not None:
            validate_mutation_closure(catalog.core_closure((handle,)) or frozenset())
            # The shared isolated registry names every source absolutely, so a
            # mutant would have no private copy: give this variant its own.
            # (The pristine baseline gets its mirror from the simulation build.)
            mirror_plan = fusesoc_registry.build_library_plan(handle.project_root, build_path)
            if mirror_plan is not None:
                private_library["core_library"] = mirror_plan
                mirror_root = build_path / "core-mirror"
        prepared = prepare_simulation_build(
            handle,
            build_root=build_path,
            lane_kind="unreserved",
            **private_library,
            # The Target's env table is a build input of the Simulation Flow;
            # a mutant must build and run under the same one.
            environment=_target_environment(work_dir, handle.identity),
        )
        if mutation is not None:
            materialize_build_mutation(
                mutation[0], mutation[1], work_dir, prepared.resolved, mirror_root=mirror_root
            )
        return handle, prepared

    def cocotb_target(self, target: str, work_dir: Path) -> CocotbSimTarget | None:
        """Resolve *target*'s cocotb identity, or ``None`` for a classic Target.

        ADR 0034 decision 2: cocotb-ness lives in the Target's ``.core`` flow
        options, so this is a subprocess-free YAML read — cheap enough to run
        before any build, which is exactly where the mutation loop needs
        it (a Cocotb Target must never reach the ``V<top>`` run-half).

        Raises :class:`UnsupportedSimTargetError` for a Cocotb Target the mutation
        loop cannot drive, so the caller fails fast with the reason named
        rather than producing a meaningless score.
        """
        try:
            handle = TargetCatalog.build(work_dir).select(target, for_flow="sim")
        except fusesoc_registry.FuseSocError as exc:
            raise UnsupportedSimTargetError(
                f"mutation: cannot select Simulation Target {target!r}: {exc}"
            ) from exc
        module = handle.cocotb_module
        if not module:
            return None
        eda_tool = self.target_eda_tool(target, work_dir)
        if eda_tool not in ("icarus", "verilator"):
            raise UnsupportedSimTargetError(
                f"mutation: Target {target!r} is a Cocotb Target "
                f"(cocotb_module={module!r}) that resolves to {eda_tool!r}. "
                "The cocotb run-half supports icarus and verilator only — "
                "point --target at an icarus or verilator Target."
            )
        return CocotbSimTarget(module=module, eda_tool=eda_tool)

    def target_eda_tool(
        self,
        target: str,
        work_dir: Path,
        build_path: Path | None = None,
    ) -> str:
        """Return the run-half family for *target*, preferring resolved build metadata."""
        declared = self._declared_sim_tool(target, work_dir)
        marker = build_path / _EDALIZE_EDA_TOOL_MARKER if build_path is not None else None
        if marker is not None and marker.exists():
            configured = self._supported_sim_tool(
                marker.read_text(encoding="utf-8").strip(), target
            )
            if configured != declared:
                raise UnsupportedSimTargetError(
                    f"mutation: Target {target!r} declared {declared!r} but cached "
                    f"build metadata names {configured!r}"
                )
            return configured
        return declared

    def _declared_sim_tool(self, target: str, work_dir: Path) -> str:
        """Return the selected Target's supported declared simulator."""
        try:
            declared = TargetCatalog.build(work_dir).select(target, for_flow="sim").eda_tool
        except fusesoc_registry.FuseSocError as exc:
            raise UnsupportedSimTargetError(
                f"mutation: cannot select Simulation Target {target!r}: {exc}"
            ) from exc
        return self._supported_sim_tool(declared, target)

    @classmethod
    def _matching_sim_tool(
        cls,
        declared: str | None,
        configured: str | None,
        target: str,
    ) -> str:
        """Authenticate configured simulator metadata against the Target declaration."""
        try:
            family = matching_eda_tool(declared, configured, target=target)
        except ValueError as exc:
            raise UnsupportedSimTargetError(f"mutation: {exc}") from exc
        return cls._supported_sim_tool(family, target)

    @staticmethod
    def _supported_sim_tool(eda_tool: str | None, target: str) -> str:
        """Normalize one mutation-capable simulator without choosing a default."""
        try:
            family = normalize_eda_tool(eda_tool)
        except ValueError as exc:
            raise UnsupportedSimTargetError(
                f"mutation: Target {target!r} has unsupported simulator metadata {eda_tool!r}"
            ) from exc
        if family not in {"icarus", "verilator"}:
            raise UnsupportedSimTargetError(
                f"mutation: Target {target!r} resolves to {family!r}; "
                "the mutation run-many loop supports icarus and verilator only"
            )
        return family

    def _validate_target_runner(self, target: str, work_dir: Path) -> None:
        """Reject Target toolchains whose prebuilt image this loop cannot drive."""
        eda_tool = self.target_eda_tool(target, work_dir)
        if eda_tool not in ("icarus", "verilator"):
            raise UnsupportedSimTargetError(
                f"mutation: Target {target!r} resolves to {eda_tool!r}. "
                "The mutation run-many loop supports Icarus and Verilator only — "
                "point --target at an icarus or verilator Target."
            )

    def _pinned_build(self, target: str, work_dir: Path, build_path: Path) -> _PinnedBuild:
        """The prebuilt sim's dir (relative to *work_dir*) and declared runtime inputs."""
        bindir = build_path / _EDALIZE_BINDIR_MARKER
        inputs = build_path / _RUNTIME_INPUTS_MARKER
        if bindir.exists() and inputs.exists():
            return _PinnedBuild(
                bindir.read_text(encoding="utf-8").strip(),
                tuple(json.loads(inputs.read_text(encoding="utf-8"))),
            )
        if build_path.name.startswith("mutant_"):
            raise UnsupportedSimTargetError(
                "private mutation build marker missing; rerun the mutation sweep"
            )
        if fusesoc_registry.native_cores_ignored(work_dir):
            # Re-resolving would clear the built image under *build_path*
            # (the private mirror is rendered into it) and mark it built.
            raise UnsupportedSimTargetError(
                "baseline build marker missing; rerun the mutation sweep"
            )
        # Defensive: baseline marker lost — re-resolve.
        _handle, prepared = self._resolve_elab_target(target, work_dir, build_path)
        rel = _record_build_markers(build_path, prepared, work_dir)
        return _PinnedBuild(rel, declared_runtime_inputs(prepared.resolved, work_dir))

    def _target_identity(self, target: str) -> str:
        """The identity (``vlnv#name``) of *target* in the checkout this run mutates."""
        try:
            return TargetCatalog.build(self.args.work_dir).select(target, for_flow="sim").identity
        except fusesoc_registry.FuseSocError as exc:
            raise UnsupportedSimTargetError(f"mutation: {exc}") from exc

    def _registered_tests(self, target: str) -> dict[str, Any]:
        """*target*'s identity plus the Test registry and skips of the mutated checkout.

        Read from that checkout's ``tests.toml``, the same file the launch of
        each Test Run reads, so the suite and its arguments cannot disagree.
        """
        work_dir = self.args.work_dir
        return {
            "identity": self._target_identity(target),
            "test_names": load_test_configuration_field(work_dir, "tests"),
            "test_skips": load_test_configuration_field(work_dir, "skip"),
        }

    def _target_test_suite(self, target: str) -> TargetTestSuite:
        """Return every runnable test declared for *target*."""
        try:
            return require_runnable_target_test_suite(target, **self._registered_tests(target))
        except (NoRunnableTestsError, TargetSectionConflictError) as exc:
            raise UnsupportedSimTargetError(f"mutation: {exc}") from exc

    def _campaign_for_target(self, target: str) -> TargetCampaign:
        """Return the resolved campaign or a suite-only compatibility view."""
        campaign = getattr(self, "_target_campaign", None)
        if campaign is not None and campaign.target == target:
            return campaign
        try:
            criteria = self.state.criteria
        except RuntimeError:
            criteria = {}
        return describe_target_campaign(
            target,
            criterion_keys=self.satisfies,
            criteria=criteria,
            **self._registered_tests(target),
        )

    def _default_test_plan(
        self, target: str, work_dir: Path, adapter: str
    ) -> tuple[TargetTestPlan, Mapping[str, Any]]:
        """The Test Run plan of *target* and the parameters its ``.core`` declares.

        Built the way the Simulation Flow builds it, against the resolved
        Target, so a Test Variant argument that overrides a Target plusarg is
        refused here too.
        """
        try:
            catalog = TargetCatalog.build(work_dir)
            handle = catalog.select(target, for_flow="sim")
            parameters = catalog.inspect(handle).parameters
            return target_test_plan(handle, adapter, parameters), parameters
        except (fusesoc_registry.FuseSocError, ValueError) as exc:
            raise UnsupportedSimTargetError(f"mutation: {exc}") from exc

    def _default_test_run_launch(
        self,
        target: str,
        work_dir: Path,
        adapter: str,
        test_names: tuple[str, ...],
    ) -> tuple[TestRunLaunch, list[str]]:
        """Launch inputs and simulator arguments of *test_names* as default Test Runs.

        A mutation run grades the Target's declared suite, so it applies
        exactly what a plain Simulation Flow run of the same Tests applies
        (ADR 0072): the Target's plusargs, ``select``, the default Test
        Variant's arguments and environment, and the seed, rendered by the
        Simulation Flow's own functions.
        """
        plan, parameters = self._default_test_plan(target, work_dir, adapter)
        try:
            launch = plan.launch(plan.default_runs_for(test_names))
            return launch, simulation_plusargs(launch, parameters)
        except ValueError as exc:
            raise UnsupportedSimTargetError(f"mutation: {exc}") from exc

    def _cocotb_suite_batches(
        self, target: str, work_dir: Path, campaign: TargetCampaign
    ) -> tuple[tuple[str, ...], ...]:
        """The Cocotb processes that run *campaign*'s suite, as the Simulation Flow batches it.

        Default Test Variants that differ in environment never share a
        process, so such a suite takes several.
        """
        tests = tuple(test for test in campaign.suite.tests if test is not None)
        plan, _parameters = self._default_test_plan(target, work_dir, "cocotb")
        batches = batch_test_runs(plan.default_runs_for(tests), one_process_per_run=False)
        return tuple(tuple(run.test for run in batch if run.test is not None) for batch in batches)

    def _classic_eda_tool(self, target: str, work_dir: Path, build_path: Path) -> str:
        """The simulator of a classic (non-Cocotb) Target the mutation loop can drive."""
        eda_tool = self.target_eda_tool(target, work_dir, build_path)
        if eda_tool not in {"icarus", "verilator"}:
            raise UnsupportedSimTargetError(
                f"mutation: cached Target {target!r} resolves to unsupported "
                f"EDA toolchain {eda_tool!r}"
            )
        return eda_tool

    def _pinned_sim_command(
        self,
        target: str,
        work_dir: Path,
        build_path: Path,
        tb_top: str,
        *,
        pinned: _PinnedBuild,
        timeout: int = 300,
        test_name: str | None = None,
        cocotb_tests: tuple[str, ...] = (),
    ) -> tuple[list[str], TestRunLaunch]:
        """The pinned adapter command of one mutant and its launch inputs.

        The command carries a prepared work document of the fields a mutant
        launch has always passed. Declared ``copyto`` inputs are part of it, so
        the adapter classifies a missing ``$readmemh`` file as the Flow
        does. Paths stay relative to *work_dir* (the run cwd); the adapter
        resolves them absolute before switching to its run cwd. The adapter
        times out a hair before the hard subprocess kill so a clean TIMEOUT
        marker lands in the captured output for the sweep's hang detection.
        One Cocotb invocation runs one batch: Tests that share an environment
        and a seed (ADR 0072); ``tests.toml`` ``select`` plusarg templates do
        not apply to Cocotb Targets (ADR 0034 decision 2).
        """
        cocotb = self.cocotb_target(target, work_dir)
        if cocotb is not None:
            launch, plusargs = self._default_test_run_launch(
                target, work_dir, "cocotb", cocotb_tests
            )
            work = replace(
                _mutant_work("cocotb", pinned, work_dir, timeout, plusargs),
                eda_tool=cocotb.eda_tool,
                cocotb_module=cocotb.module,
                tests=cocotb_tests,
                cocotb_seed=launch.seed,
                sim_time_grace_s=DEFAULT_SIM_TIME_GRACE_S,
            )
            return prepare_adapter_invocation(work, python=sys.executable), launch
        eda_tool = self._classic_eda_tool(target, work_dir, build_path)
        names = (test_name,) if test_name is not None else ()
        launch, plusargs = self._default_test_run_launch(target, work_dir, eda_tool, names)
        pass_sentinels, fail_sentinels = resolve_sim_sentinels(work_dir)
        adapter: AdapterKind = "icarus" if eda_tool == "icarus" else "verilator"
        work = replace(
            _mutant_work(adapter, pinned, work_dir, timeout, plusargs),
            top=tb_top if eda_tool == "verilator" else "",
            pass_sentinels=tuple(pass_sentinels),
            fail_sentinels=tuple(fail_sentinels),
        )
        return prepare_adapter_invocation(work, python=sys.executable), launch

    def _run_sim_pinned(
        self,
        target: str,
        work_dir: Path,
        build_path: Path,
        tb_top: str,
        *,
        timeout: int = 300,
        test_name: str | None = None,
        cocotb_tests: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess:
        """Run the simulation image built in *build_path*.

        The Target's declared ``copyto`` inputs are exposed in ``run_cwd`` for
        the run and removed afterwards, as the Simulation Flow does.
        """
        pinned = self._pinned_build(target, work_dir, build_path)
        cmd, launch = self._pinned_sim_command(
            target,
            work_dir,
            build_path,
            tb_top,
            pinned=pinned,
            timeout=timeout,
            test_name=test_name,
            cocotb_tests=cocotb_tests,
        )
        marker = build_path / _EDALIZE_EDA_TOOL_MARKER
        if marker.is_file():
            self.record_eda_tool(target, marker.read_text(encoding="utf-8").strip())
        with materialize_runtime_inputs(
            work_dir / pinned.rel, work_dir / resolve_run_cwd(work_dir), pinned.runtime_inputs
        ):
            return subprocess.run(
                cmd,
                cwd=work_dir,
                env=relocate_python_artifacts(
                    {
                        **os.environ,
                        **launch_environment(
                            _target_environment(work_dir, self._target_identity(target)),
                            launch.environment,
                        ),
                    },
                    python_artifact_root(os.environ, build_path / "python-artifacts"),
                    pytest_scope=f"mutation-run:{build_path.resolve()}",
                ),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )

    def _run_target_test_suite(
        self,
        target: str,
        work_dir: Path,
        build_path: Path,
        tb_top: str,
        *,
        timeouts: Mapping[str, int] | None = None,
    ) -> list[MutationTestRun]:
        """Run one built source variant against the Target's complete test suite.

        ``timeouts`` maps a test's display name to its wall-clock limit in
        seconds; the baseline passes none and gets the cap for every test.
        """
        campaign = self._campaign_for_target(target)
        batched = self.cocotb_target(target, work_dir) is not None
        batches = self._cocotb_suite_batches(target, work_dir, campaign) if batched else None

        def _launch(unit: CampaignUnit) -> MutationTestRun:
            try:
                proc = self._run_sim_pinned(
                    target,
                    work_dir,
                    build_path,
                    tb_top,
                    timeout=(timeouts or {}).get(unit.display_name, _MUTANT_TIMEOUT_CAP_S),
                    test_name=unit.test_name,
                    cocotb_tests=unit.selected_tests,
                )
                return MutationTestRun(
                    test_name=unit.display_name,
                    process=proc,
                    output=(proc.stdout or "") + (proc.stderr or ""),
                    requires_cocotb_results=batched,
                )
            except subprocess.TimeoutExpired as exc:
                return MutationTestRun(
                    test_name=unit.display_name,
                    timed_out=True,
                    output=_timeout_output(exc),
                    requires_cocotb_results=batched,
                )
            except RuntimeInputError as exc:
                return MutationTestRun(
                    test_name=unit.display_name,
                    error=f"Simulation runtime input setup failed: {exc}",
                    requires_cocotb_results=batched,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return MutationTestRun(
                    test_name=unit.display_name,
                    error=str(exc),
                    requires_cocotb_results=batched,
                )

        def _run_unit(unit: CampaignUnit) -> MutationTestRun:
            started = _monotonic()
            run = _launch(unit)
            run.duration_s = _monotonic() - started
            return run

        return list(campaign.execute(_run_unit, batched=batched, batches=batches).values)

    @staticmethod
    def _suite_output(runs: list[MutationTestRun]) -> str:
        """Render per-test output without losing which test produced it."""
        return "\n".join(f"===== {run.test_name} =====\n{run.output or run.error}" for run in runs)

    @staticmethod
    def _suite_inconclusive_reason(runs: list[MutationTestRun]) -> str:
        """Reject outcomes that cannot establish pass or fail for every test."""
        for run in runs:
            verdict = MutationFlow._classify_mutation_run(
                run,
                timeout_is_inconclusive=True,
            )
            if verdict.inconclusive_reason:
                return verdict.inconclusive_reason
        return ""

    @staticmethod
    def _classify_mutation_run(
        run: MutationTestRun,
        *,
        timeout_is_inconclusive: bool,
    ) -> VariantSuiteVerdict:
        early = MutationFlow._preclassify_mutation_run(
            run,
            timeout_is_inconclusive=timeout_is_inconclusive,
        )
        if early is not None:
            return early
        parsed = parse_results_line(run.output)
        if parsed is not None:
            return MutationFlow._classify_cocotb_run(run, parsed)
        if run.requires_cocotb_results:
            return VariantSuiteVerdict(
                inconclusive_reason=f"{run.test_name}: cocotb result line is missing or malformed"
            )
        assert run.process is not None
        return VariantSuiteVerdict(
            detected=run.process.returncode != 0,
            first_killing_test=run.test_name if run.process.returncode != 0 else "",
        )

    @staticmethod
    def _preclassify_mutation_run(
        run: MutationTestRun,
        *,
        timeout_is_inconclusive: bool,
    ) -> VariantSuiteVerdict | None:
        prefix = f"{run.test_name}: "
        if run.error:
            return VariantSuiteVerdict(inconclusive_reason=prefix + run.error)
        if run.timed_out or has_simulation_timeout(run.output):
            if timeout_is_inconclusive:
                return VariantSuiteVerdict(inconclusive_reason=prefix + "simulation timed out")
            return VariantSuiteVerdict(
                detected=True, first_killing_test=run.test_name, timed_out=True
            )
        if run.process is None:
            return VariantSuiteVerdict(
                inconclusive_reason=prefix + "runner produced no process result"
            )
        if infra := _infra_failure_reason(run.process):
            return VariantSuiteVerdict(inconclusive_reason=prefix + infra)
        return None

    @staticmethod
    def _classify_cocotb_run(
        run: MutationTestRun,
        parsed: CocotbResults,
    ) -> VariantSuiteVerdict:
        prefix = f"{run.test_name}: "
        if parsed.state != STATE_OK:
            return VariantSuiteVerdict(
                inconclusive_reason=prefix + f"cocotb result state is {parsed.state}"
            )
        unresolved = [
            test.name for test in parsed.tests if test.status not in (VERDICT_PASS, VERDICT_FAIL)
        ]
        if unresolved:
            return VariantSuiteVerdict(
                inconclusive_reason=prefix + f"no verdict for {', '.join(unresolved)}"
            )
        failed = next(
            (test.name for test in parsed.tests if test.status == VERDICT_FAIL),
            "",
        )
        if failed:
            return VariantSuiteVerdict(detected=True, first_killing_test=failed)
        if run.process.returncode != 0:
            return VariantSuiteVerdict(
                inconclusive_reason=prefix + "runner exited non-zero without a failing test"
            )
        return VariantSuiteVerdict()

    @staticmethod
    def _classify_variant_suite(runs: list[MutationTestRun]) -> VariantSuiteVerdict:
        """Classify one mutant once so status and first-kill evidence cannot drift."""
        verdicts = [
            MutationFlow._classify_mutation_run(
                run,
                timeout_is_inconclusive=False,
            )
            for run in runs
        ]
        reasons = [
            verdict.inconclusive_reason for verdict in verdicts if verdict.inconclusive_reason
        ]
        if reasons:
            return VariantSuiteVerdict(inconclusive_reason="; ".join(reasons))
        detected = [
            (verdict, run) for verdict, run in zip(verdicts, runs, strict=True) if verdict.detected
        ]
        if not detected:
            return VariantSuiteVerdict()
        # A test that failed outright outranks one that merely timed out.
        verdict, run = min(detected, key=lambda pair: pair[0].timed_out)
        return replace(verdict, detecting_run=run)

    @staticmethod
    def _baseline_suite_passed(runs: list[MutationTestRun]) -> bool:
        """A baseline passes only when every Target test completes successfully."""
        return all_campaign_results_match(
            runs,
            lambda run: (
                not run.timed_out
                and not run.error
                and run.process is not None
                and not _infra_failure_reason(run.process)
                and run.process.returncode == 0
            ),
        )

    def _persist_mutant_log(self, index: int, output: str) -> str:
        """Write one isolated variant's simulator output."""
        return self._persist_capped_log(
            workspace.mutant_logs_dir() / f"mutant_{index}.log",
            output,
            f"mutation {index}",
        )

    def _persist_baseline_log(self, output: str) -> str:
        """Persist this invocation's pristine-baseline output."""
        return self._persist_capped_log(workspace.baseline_log_path(), output, "baseline")

    def _persist_capped_log(self, path: Path, output: str, label: str) -> str:
        """Best-effort capped transcript persistence shared by all variants."""
        from booley.flows.run_log import cap_log_bytes

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                cap_log_bytes(
                    output.encode("utf-8", errors="replace"),
                    _MUTANT_LOG_MAX_BYTES,
                )
            )
        except OSError:
            logger.debug("could not persist %s log", label, exc_info=True)
            return ""
        return _artifacts.relative(path, self.args.work_dir) or ""

    def _reset_mutant_logs(self) -> None:
        """Drop the previous sweep's mutant logs before this one writes any.

        Without this a run with a smaller ``--count`` leaves the earlier
        run's ``mutant_7.log … mutant_25.log`` in place, physically
        indistinguishable from this run's — and the ``mutant_logs`` artifact
        key advertises the whole directory, which would make it a "present but
        wrong" pointer under a contract that says pointers are never wrong.

        Best-effort, like every other log operation here.
        """
        with contextlib.suppress(OSError):
            shutil.rmtree(workspace.mutant_logs_dir(), ignore_errors=True)

    def _run_variant_sweep(
        self,
        plan: MutationRunPlan,
        generated: GeneratedMutants,
        baseline: Baseline,
    ) -> VariantSweep:
        """Build and run candidates in pick order until ``plan.count`` Mutants are kept.

        Each candidate takes the next free number and build directory.  One whose build
        equals the baseline's is dropped and its number goes to the next candidate; running
        out of candidates leaves fewer Mutants than requested.
        """
        start = time.monotonic()
        self._reset_mutant_logs()
        specs: list[MutationSpec] = []
        results: list[MutationResult] = []
        dropped: list[str] = []
        inconclusive: list[str] = []
        for mutant in generated.ranked:
            if len(specs) == plan.count:
                break
            spec = _spec_of(len(specs) + 1, mutant)
            self.emit_progress(f"build and test Mutant {spec.index}/{plan.count}")
            variants = generated.variants.with_proposals([spec])
            result, reason = self._run_one_variant(plan, spec, variants, baseline)
            if result is None:
                dropped.append(spec.mutant_id)
                continue
            specs.append(spec)
            results.append(result)
            if reason:
                inconclusive.append(f"mutation #{spec.index}: {reason}")
        elapsed = time.monotonic() - start
        return VariantSweep(specs, results, dropped, elapsed, "; ".join(inconclusive))

    def _run_one_variant(
        self,
        plan: MutationRunPlan,
        spec: MutationSpec,
        variants: MutationVariantPlan,
        baseline: Baseline,
    ) -> tuple[MutationResult | None, str]:
        """Build and test one Mutant; ``None`` for a Mutant that builds the baseline's C++."""
        build_path = workspace.variant_build_dir(spec.index)
        shutil.rmtree(build_path, ignore_errors=True)
        build_path.mkdir(parents=True, exist_ok=True)
        elab = self._run_elab(plan.target, plan.work_dir, build_path, (variants, spec.index))
        elab_output = (elab.stdout or "") + (elab.stderr or "")
        if elab.returncode != 0:
            return self._invalid_build(spec, elab_output), ""
        if self._builds_the_baseline(plan.work_dir, build_path, baseline):
            shutil.rmtree(build_path, ignore_errors=True)
            return None, ""
        runs = self._run_target_test_suite(
            plan.target,
            plan.work_dir,
            build_path,
            plan.tb_top,
            timeouts=baseline.timeouts,
        )
        combined = self._suite_output(runs)
        verdict = self._classify_variant_suite(runs)
        snippet = self._variant_snippet(verdict, combined)
        return (
            MutationResult(
                index=spec.index,
                invalid=bool(verdict.inconclusive_reason),
                detected=verdict.detected,
                sim_output_snippet=snippet,
                log_path=self._persist_mutant_log(spec.index, combined),
                first_killing_test=verdict.first_killing_test,
                timed_out=verdict.timed_out,
            ),
            verdict.inconclusive_reason,
        )

    @staticmethod
    def _variant_snippet(verdict: VariantSuiteVerdict, combined: str) -> str:
        """The report snippet of a scored Mutant: its detecting run's failure, else the tail."""
        if verdict.inconclusive_reason:
            return f"sim outcome inconclusive: {verdict.inconclusive_reason}"
        if verdict.detecting_run is not None:
            run = verdict.detecting_run
            return detection_snippet(
                run.test_name,
                run.output,
                timed_out=verdict.timed_out,
                killing_test=verdict.first_killing_test,
            )
        return tail_snippet(combined)

    def _invalid_build(self, spec: MutationSpec, elab_output: str) -> MutationResult:
        """The result of a Mutant whose build failed: reported ``invalid``, never scored."""
        return MutationResult(
            index=spec.index,
            invalid=True,
            sim_output_snippet="variant did not elaborate: " + tail_snippet(elab_output),
            log_path=self._persist_mutant_log(spec.index, elab_output),
        )

    @staticmethod
    def _builds_the_baseline(work_dir: Path, build_path: Path, baseline: Baseline) -> bool:
        """True if the Mutant's generated C++ equals the baseline's, so no test can tell them apart.

        Never true without a baseline to compare with (a Target not simulated with
        Verilator) or when the Mutant's build has no recognizable Verilator layout.
        """
        if baseline.generated_cpp is None:
            return False
        generated = work_dir / (build_path / _EDALIZE_BINDIR_MARKER).read_text(encoding="utf-8")
        return generated_cpp(generated, build_path, work_dir) == baseline.generated_cpp

    @staticmethod
    def _scope_residue(baseline: Mapping[str, str], work_dir: Path) -> list[str]:
        """Return the scope files whose content the run changed, sorted.

        ``baseline`` maps each scope file to its content hash captured before
        the run (``MutationRunPlan.scope_hashes``). Residue is a difference
        from that snapshot, not from git's index: an uncommitted edit that
        predates the run and survives it untouched is not residue. A file that
        disappeared counts, and no git repository is needed.
        """
        current = workspace.compute_scope_hashes(list(baseline), work_dir)
        return sorted(rel for rel, digest in baseline.items() if current[rel] != digest)

    def _add_residue_warning(
        self,
        result: EndpointOutcome,
        baseline: Mapping[str, str],
        work_dir: Path,
    ) -> None:
        """Warn, naming the files, when the run left scope content changed."""
        residue = self._scope_residue(baseline, work_dir)
        if not residue:
            return
        warning_prefix = "  WARNING: worktree not clean after sim sweep"
        lines = result.report_text.splitlines()
        result_index = next(
            (index for index, line in enumerate(lines) if line.startswith("RESULT: ")),
            len(lines),
        )
        lines.insert(result_index, f"{warning_prefix}: {', '.join(residue)}")
        result.report_text = "\n".join(lines)

    # ------------------------------------------------------------------
    # Result assembly
    # ------------------------------------------------------------------

    def _build_run_result(
        self,
        plan: MutationRunPlan,
        inputs: RunResultInputs,
    ) -> EndpointOutcome:
        # Unpack the plan config + run outcome the assembly below reads.
        min_detected, target = plan.min_detected, plan.target
        auto_mode, formula_count, source_size_budget = (
            plan.auto_mode,
            plan.formula_count,
            plan.source_size_budget,
        )
        summary, count = inputs.summary, inputs.count

        valid_count = summary.detected_count + summary.not_detected_count
        passed = summary.detected_count >= min_detected

        detail = _mutation_detail(summary, min_detected, plan.count, valid_count)
        detail.update(
            {
                "target": target,
                "tests": list(self._campaign_for_target(target).suite.display_names),
                "seed": plan.seed,
                "identity": inputs.identity,
                "detected_timeout": summary.detected_timeout_count,
            }
        )
        if inputs.shortfall:
            detail["shortfall"] = inputs.shortfall
        detail["equivalent_dropped"] = inputs.equivalent_dropped
        if auto_mode:
            detail["auto"] = True
            detail["formula_count"] = formula_count
            detail["budget_capped"] = count < formula_count
            detail["source_size_budget"] = source_size_budget
        if inputs.evidence:
            detail["evidence"] = inputs.evidence
        if inputs.coverage_gap:
            # Machine-readable twin of the report_text diagnosis, so a triage
            # reader can tell this apart from a broken harness (SETUP-F-38).
            detail["coverage_gap"] = True
            detail["diagnosis"] = "scope_not_covered_by_target_tests"

        # Per-mutant status + sim-log pointer. Only the failure path used to
        # carry this; on a PASS the surviving mutants (there can be survivors
        # in a run that still clears min_detected) were reported as a bare
        # tally with no way back to what they did.
        detail["classified"] = summary.classify()

        self._attach_campaign_artifacts(plan, inputs, detail)

        criterion_key = f"mutation_score_{target}"
        self.set_criterion(
            criterion_key,
            passed,
            detail=detail,
            source_target=target,
        )

        # Display lines. Both formatters read straight off (plan, inputs) and
        # recompute valid_count internally, so the explicit arg fan-out is gone.
        display = self._format_display_line(plan, inputs)
        report_text = self._format_report_text(plan, inputs)
        artifact_lines = self._artifact_display_lines(detail)
        if artifact_lines:
            report_text += "\n\nArtifacts:\n" + "\n".join(f"  {line}" for line in artifact_lines)

        return EndpointOutcome(
            exit_code=EXIT_SUCCESS if passed else EXIT_FAILURE,
            criterion_key=criterion_key,
            criterion_met=passed,
            detail=detail,
            report_text=report_text,
            display_lines=[display, *artifact_lines],
        )

    def _attach_campaign_artifacts(
        self,
        plan: MutationRunPlan,
        inputs: RunResultInputs,
        detail: dict[str, Any],
    ) -> None:
        invocation_dir = self.reserve_invocation_dir() if plan.report_dir else None
        if invocation_dir is None:
            variant_root = workspace.variants_dir()
            shutil.rmtree(variant_root, ignore_errors=True)
            variants = inputs.variants.write_variants(variant_root)
            detail["variant_files"] = self._variant_detail(
                variants,
                variant_root,
                variant_root,
            )
            self._attach_campaign_directories(detail, None)
            return

        staging_dir = invocation_dir / ".campaign.tmp"
        campaign_dir = invocation_dir / "campaign"
        staging_dir.mkdir()
        variants = inputs.variants.write_variants(staging_dir / "variants")
        specs_path = staging_dir / "mutation-specs.md"
        specs_path.write_text(generate_specs_markdown(inputs.summary.specs), encoding="utf-8")
        files = self._stage_campaign_files(plan, inputs, staging_dir, campaign_dir, variants)
        self._write_campaign_manifest(plan, inputs, staging_dir, **files)
        staging_dir.replace(campaign_dir)
        detail["variant_files"] = self._variant_detail(variants, staging_dir, campaign_dir)
        detail["classified"] = inputs.summary.classify()
        self._attach_campaign_directories(detail, campaign_dir)
        _artifacts.merge_artifacts(
            detail,
            _artifacts.artifacts_block(
                self.args.work_dir,
                manifest=campaign_dir / "manifest.json",
                specs=campaign_dir / "mutation-specs.md",
                results=campaign_dir / "mutation-results.md",
            ),
        )

    def _stage_campaign_files(
        self,
        plan: MutationRunPlan,
        inputs: RunResultInputs,
        staging_dir: Path,
        campaign_dir: Path,
        variants: dict[int, Path],
    ) -> dict[str, Path | None]:
        if plan.source_size_budget:
            (staging_dir / "source-size-budget.json").write_text(
                json.dumps(plan.source_size_budget, indent=2), encoding="utf-8"
            )
        baseline_log = self._copy_campaign_file(
            workspace.baseline_log_path(), staging_dir / "baseline.log"
        )
        mutant_logs = self._copy_campaign_dir(
            workspace.mutant_logs_dir(), staging_dir / "mutant-logs"
        )
        self._retarget_log_paths(inputs.summary, mutant_logs, campaign_dir, plan.work_dir)
        links = {index: posix_relpath(path, staging_dir) for index, path in variants.items()}
        (staging_dir / "mutation-results.md").write_text(
            generate_results_markdown(
                inputs.summary,
                plan.min_detected,
                variant_paths=links,
                identity=inputs.identity,
            ),
            encoding="utf-8",
        )
        return {"baseline_log": baseline_log, "mutant_log_dir": mutant_logs}

    @staticmethod
    def _retarget_log_paths(
        summary: MutationSummary,
        log_dir: Path | None,
        campaign_dir: Path,
        work_dir: Path,
    ) -> None:
        if log_dir is None:
            return
        for result in summary.results:
            staged = log_dir / Path(result.log_path).name if result.log_path else None
            if staged is not None and staged.is_file():
                result.log_path = posix_relpath(
                    campaign_dir / "mutant-logs" / staged.name, work_dir
                )

    def _write_campaign_manifest(
        self,
        plan: MutationRunPlan,
        inputs: RunResultInputs,
        staging_dir: Path,
        **files: Path | None,
    ) -> None:
        manifest = self._campaign_manifest(plan, inputs, **files)
        temporary = staging_dir / ".manifest.json.tmp"
        temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        temporary.replace(staging_dir / "manifest.json")

    @staticmethod
    def _copy_campaign_file(source: Path, destination: Path) -> Path | None:
        if not source.is_file():
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination

    @staticmethod
    def _copy_campaign_dir(source: Path, destination: Path) -> Path | None:
        if not source.is_dir():
            return None
        shutil.copytree(source, destination)
        return destination

    def _campaign_manifest(
        self,
        plan: MutationRunPlan,
        inputs: RunResultInputs,
        *,
        baseline_log: Path | None,
        mutant_log_dir: Path | None,
    ) -> dict[str, Any]:
        mutants = inputs.summary.classify()
        for row in mutants:
            if mutant_log_dir is not None and row.get("log"):
                row["log"] = f"mutant-logs/{Path(str(row['log'])).name}"
            spec = next(spec for spec in inputs.summary.specs if spec.index == row["index"])
            row["variant"] = f"variants/mutant_{spec.index}/{spec.file}"
        return {
            "schema_version": 2,
            "run_id": os.environ.get("BOOLEY_RUN_ID", ""),
            "target": plan.target,
            "eda_tool": self.target_eda_tool(
                plan.target, plan.work_dir, workspace.baseline_build_dir()
            ),
            "source_fingerprint": inputs.variants.source_fingerprint(),
            "scope_hashes": plan.scope_hashes,
            "identity": inputs.identity,
            "baseline": {
                "status": "passed",
                "log": baseline_log.name if baseline_log is not None else None,
            },
            "mutants": mutants,
            "artifacts": {
                "specs": "mutation-specs.md",
                "results": "mutation-results.md",
                "variants": "variants",
            },
        }

    def _attach_campaign_directories(
        self,
        detail: dict[str, Any],
        artifact_dir: Path | None,
    ) -> None:
        variants_dir = artifact_dir / "variants" if artifact_dir else workspace.variants_dir()
        mutant_logs = artifact_dir / "mutant-logs" if artifact_dir else workspace.mutant_logs_dir()
        _artifacts.merge_artifacts(
            detail,
            _artifacts.artifacts_block(
                self.args.work_dir,
                dirs={
                    "variants": variants_dir,
                    "mutant_logs": mutant_logs,
                },
            ),
        )

    def _variant_detail(
        self,
        variants: dict[int, Path],
        staging_dir: Path,
        campaign_dir: Path,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, path in variants.items():
            final_path = campaign_dir / path.relative_to(staging_dir)
            relative = _artifacts.relative(final_path, self.args.work_dir)
            if relative is not None:
                rows.append({"index": index, "path": relative})
        return rows

    @staticmethod
    def _artifact_display_lines(detail: dict[str, Any]) -> list[str]:
        lines = [
            f"mutation variant: {row['path']}"
            for row in detail.get("variant_files", [])
            if isinstance(row, dict) and isinstance(row.get("path"), str)
        ]
        artifacts = detail.get("artifacts")
        if isinstance(artifacts, dict) and isinstance(artifacts.get("results"), str):
            lines.append(f"mutation report: {artifacts['results']}")
        return lines

    @staticmethod
    def _format_display_line(
        plan: MutationRunPlan,
        inputs: RunResultInputs,
    ) -> str:
        summary = inputs.summary
        valid_count = summary.detected_count + summary.not_detected_count
        base = f"{summary.detected_count}/{valid_count} detected (need {plan.min_detected})"
        if inputs.coverage_gap:
            base += " [coverage gap: Target's tests never exercise this scope]"
        suffix = f" [seed {plan.seed}]"
        if plan.auto_mode and inputs.count < plan.formula_count:
            suffix += f" [auto: {plan.formula_count} requested, {inputs.count} budget-capped]"
        return base + suffix

    @staticmethod
    def _identity_header(inputs: RunResultInputs) -> str:
        """The first report line: how many Mutants ran and what reproduces them."""
        identity = inputs.identity
        verilator = f", {identity['verilator_version']}" if "verilator_version" in identity else ""
        return (
            f"[mutation_test] {len(inputs.summary.specs)} Mutants "
            f"(seed {identity['seed']}, count {identity['count']}, "
            f"generator {identity['generator_version']}, pyslang {identity['pyslang_version']}"
            f"{verilator})"
        )

    def _format_report_text(
        self,
        plan: MutationRunPlan,
        inputs: RunResultInputs,
    ) -> str:
        summary = inputs.summary
        min_detected = plan.min_detected
        valid_count = summary.detected_count + summary.not_detected_count
        passed = summary.detected_count >= min_detected
        gate = "PASS" if passed else "FAIL"
        source = "explicit" if plan.explicit_min_detected else "default 80% of --count"
        lines: list[str] = [self._identity_header(inputs)]
        if inputs.shortfall:
            lines.append(f"[mutation_test] {inputs.shortfall}")
        lines.append(
            f"[mutation_test] sim sweep: {len(summary.results)}/{len(summary.specs)} "
            f"tested              {inputs.tester_elapsed / 60:.0f}m",
        )
        lines.append(
            f"  {summary.detected_count} detected ({summary.detected_timeout_count} timeout), "
            f"{summary.invalid_count} invalid, "
            f"{summary.not_detected_count} not detected",
        )
        lines.append(
            f"  detected: {summary.detected_count}/{valid_count} "
            f"(required: {min_detected}/{valid_count}, {source})",
        )
        if summary.invalid_count:
            lines.append(f"  {_invalid_mutants_line(summary.invalid_count)}")
        if inputs.coverage_gap:
            # Name the diagnosis instead of leaving a bare 0/N to be misread as
            # a broken mutation harness (SETUP-F-38).
            lines.extend(
                [
                    "  DIAGNOSIS: this scope is not covered by the Target's tests.",
                    f"    Evidence: {_describe_evidence(inputs.evidence)} — every "
                    "mutation was applied and reached the design, and the tests "
                    "still passed.",
                    "    Nothing here is a mutation-harness defect. "
                    "Either extend the Target's stimulus to exercise the scope, "
                    "run --scope against a Target whose tests drive it, or lower "
                    "--min-detected if a partial score is acceptable.",
                ],
            )
        # Name every survivor (file:line + the code change): the one datum the
        # run exists to produce (QA_REPORT C2.4).
        for spec in summary.surviving_specs():
            orig = " ".join(spec.original_code.split())
            mut = " ".join(spec.mutated_code.split())
            lines.append(
                f"  NOT DETECTED [{spec.category}] {spec.file}:{spec.line}: {orig!r} -> {mut!r}"
            )
        if summary.surviving_specs():
            lines.append(f"  {EQUIVALENT_MUTANT_NOTE}")
        reason = f"{summary.detected_count}/{valid_count} detected" + (
            "" if passed else f", need {min_detected}"
        )
        if inputs.coverage_gap:
            reason += "; scope not covered by this Target's tests"
        lines.extend(["", f"RESULT: {gate} ({reason})"])
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _describe_evidence(evidence: dict[str, Any]) -> str:
    """One-line summary of exact-replacement evidence."""
    if not evidence:
        return "no variant evidence collected"
    resolved = evidence.get("resolved") or []
    fingerprint = evidence.get("source_fingerprint", "unknown source")
    return f"{len(resolved)} isolated exact replacement(s) from {fingerprint}"


def _no_site_hint(plan: MutationRunPlan) -> str:
    """What to do when the scope files hold no spot a Mutation Operator changes."""
    if plan.scope_defaulted:
        return _PASS_SCOPE_HINT
    return (
        "the scope files hold no spot a Mutation Operator changes; name RTL files that hold logic"
    )


def _names_or_none(names: Sequence[str]) -> str:
    """*names* comma-joined, or ``none``."""
    return ", ".join(names) or "none"


def _failure_detail(
    *,
    phase: str,
    reason: str,
    specs: list[MutationSpec],
    work_dir: Path,
    summary: MutationSummary | None = None,
    min_detected: int = 0,
    requested: int = 0,
    log_tail: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Structured detail for a run that never reached a verdict.

    A failed run used to write a report with ``detail: {}``,
    stranding the tally and the mutation list in the transcripts where no
    downstream reader looks (SETUP-F-39).  Everything the run did learn goes
    here, whatever the exit code.
    """
    detail: dict[str, Any] = {
        "failed": True,
        "phase": phase,
        "reason": reason,
        "mutations": [s.to_dict() for s in specs],
    }
    if summary is not None:
        valid_count = summary.detected_count + summary.not_detected_count
        detail.update(_mutation_detail(summary, min_detected, requested, valid_count))
        detail["classified"] = summary.classify()
    if evidence:
        detail["evidence"] = evidence
    if log_tail:
        detail["log_tail"] = log_tail
    # Pointers on the failure path too. ``classified`` above cites a log per
    # mutant, and a failed run is where a reader most needs the entry points to
    # them — a block that appears only on success is worse than none, because
    # its absence reads as "this run produced nothing".
    _artifacts.merge_artifacts(
        detail,
        _artifacts.artifacts_block(
            work_dir,
            dirs={
                "variants": workspace.variants_dir(),
                "mutant_logs": workspace.mutant_logs_dir(),
            },
        ),
    )
    return detail


def _spec_of(index: int, mutant: Mutant) -> MutationSpec:
    """The generator's ``mutant`` as the Mutant numbered ``index`` in the run."""
    return MutationSpec(
        index,
        mutant.operator,
        mutant.file,
        mutant.line,
        mutant.original_code,
        mutant.mutated_code,
        mutant.id,
    )


def _mutation_detail(
    summary: MutationSummary,
    min_detected: int,
    requested: int,
    valid_count: int,
) -> dict[str, Any]:
    """The tally fields.

    ``total_requested`` is the ``--count``; ``total_scored`` the Mutants kept (invalid included).
    """
    return {
        "detected": summary.detected_count,
        "not_detected": summary.not_detected_count,
        "invalid": summary.invalid_count,
        "min_detected": min_detected,
        "total_requested": requested,
        "total_scored": len(summary.specs),
        "total_valid": valid_count,
    }


def _shortfall_message(scored: int, requested: int) -> str | None:
    """The note when ``scored`` Mutants fall short of ``requested``, else None.

    One text for every cause (a generator shortfall or an equivalent drop), and it
    claims nothing about how many candidates exist.
    """
    if scored >= requested:
        return None
    return f"only {scored} Mutants could be scored ({requested} requested)"


def _mutant_timeout(baseline_s: float) -> int:
    """The wall-clock limit of one test on a Mutant, in whole seconds.

    ``max(10 x the test's baseline duration, 30 s)``, capped at 300 s: a Mutant
    that makes the design loop forever is cut off long before the cap, while a
    slow test still gets room.
    """
    scaled = math.ceil(_MUTANT_TIMEOUT_FACTOR * baseline_s)
    return min(_MUTANT_TIMEOUT_CAP_S, max(_MUTANT_TIMEOUT_FLOOR_S, scaled))


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
    """Whatever the killed process had written before the timeout fired.

    ``TimeoutExpired`` carries the partial streams, but typed as ``bytes |
    str | None`` depending on how the run was launched — normalize both, and
    tolerate a run that produced nothing at all.
    """
    parts: list[str] = []
    for stream in (exc.stdout, exc.stderr):
        if not stream:
            continue
        parts.append(stream.decode("utf-8", "replace") if isinstance(stream, bytes) else stream)
    return "".join(parts)


if __name__ == "__main__":
    MutationFlow().cli()
