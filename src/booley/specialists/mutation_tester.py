"""MutationTesterSpecialist — proposal-locked, isolated mutation testing.

Cold start (no valid proposal lock):
  1. A read-only creator proposes N exact source replacements as JSON.
  2. Booley validates each replacement against the pristine source bytes.
  3. The untouched project is built and run as the campaign baseline.
  4. Each proposal is applied alone, built in its own directory, simulated,
     and then restored.  The compiler—not a partial HDL parser—decides whether
     the proposed variant is valid.
  5. Valid proposals are persisted in the lock for later reuse.

Warm reuse (lock matches current scope by content hash) skips only the creator.
The pristine baseline and every isolated mutant are rebuilt and observed again,
so a result never depends on stale instrumented RTL or selector-zero semantics.

Lock invariants — scope set + per-file content hash + schema_version.  Any
change wipes the lock on next invocation.  ``--regen-lock`` forces a wipe.

Exit codes:
  0 — detection rate >= threshold (pass)
  1 — detection rate < threshold (fail)
  2 — error (agent failure, infra error, lock baseline broken, ...)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

from booley.config import project_config
from booley.core.boundary import BoundaryError, as_int, as_str, require_int, require_str
from booley.core.models import AgentCallParams
from booley.dev_support import mutation_lock as lock_mod
from booley.dev_support.mutation_variants import MutationVariantError, MutationVariantPlan
from booley.dev_support.workspace_isolation import hide_opposite_sources
from booley.flows import artifacts as _artifacts
from booley.flows import edam as edam_layer
from booley.flows.sim import edam as sim_edam
from booley.flows.sim.flow import _SIM_RUN_HALVES, _resolve_run_cwd, _resolve_sim_sentinels
from booley.flows.target_campaign import (
    CampaignUnit,
    TargetCampaign,
    all_campaign_results_match,
    describe_target_campaign,
    resolve_target_campaign,
)
from booley.flows.target_criteria import CampaignScopeError
from booley.flows.target_test_suite import (
    NoRunnableTestsError,
    TargetTestSuite,
    require_runnable_target_test_suite,
)
from booley.fusesoc import fusesoc_registry
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS, McpToolResult
from booley.runtime.paths import refs_dir
from booley.runtime.platform_paths import posix_relpath
from booley.sim.cocotb_results import (
    STATE_OK,
    VERDICT_FAIL,
    VERDICT_PASS,
    CocotbResults,
    parse_results_line,
)
from booley.sim.sim_result import SIM_INFRA_ERROR_PREFIX, has_infra_error
from booley.targets.target import inspect_target

from .specialist import Specialist

logger = logging.getLogger(__name__)
#: Per-mutant sim-log cap. Deliberately small: this file is written once per
#: mutant per verification round, the mutant count has no upper bound, and a
#: chatty testbench can emit megabytes per run. What a reader needs from a
#: mutant log is the verdict and the last thing the design did, both in the
#: tail — the full transcript of a run that has already been graded is not
#: worth unbounded disk (cf. the 20 GB trace.fst incident).
_MUTANT_LOG_MAX_BYTES = 256 * 1024


def _mutation_guide_path() -> str:
    return str(refs_dir() / "rtl-mutation-testing.md")


def _infra_failure_reason(proc: subprocess.CompletedProcess) -> str:
    """The run-half's infra-failure reason, or ``""`` when the sim really ran.

    A run-half that never started the simulator (no built binary, missing
    cocotb install) prints ``[SIM_INFRA_ERROR] <reason>`` and exits non-zero.
    That exit code says nothing about the design, so both graders here — the
    cold-start verification round and the kill/survive sweep — must ask this
    first and treat a non-empty answer as "no observation" (SETUP-F-41).
    """
    combined = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 2 and "usage:" in combined and ": error:" in combined:
        return "runner argument parsing failed"
    if not has_infra_error(combined):
        return ""
    for line in combined.splitlines():
        if SIM_INFRA_ERROR_PREFIX in line:
            return line.split(SIM_INFRA_ERROR_PREFIX, 1)[1].strip()
    return "simulation harness failure"  # pragma: no cover - marker without a line


# Marker files (under the build dir) record the edalize binary dir relative to
# the project root and its resolved EDA-tool family. _run_elab writes them; the
# per-mutant run-many loop reads them so it never has to re-resolve the Target.
_EDALIZE_BINDIR_MARKER = ".booley_edalize_bindir"
_EDALIZE_EDA_TOOL_MARKER = ".booley_edalize_eda_tool"


# The creator can inspect source but cannot modify it or invoke shell tools.
# Booley alone materializes variants after validating the returned proposals.
def _configured_testbench_dirs(work_dir: Path) -> list[str]:
    """Return testbench source/include dirs (from the ``.core``) for prompt boundaries."""
    try:
        from booley.fusesoc.fusesoc_registry import source_dirs_from_core

        _rtl, tb_dirs, tb_incl = source_dirs_from_core(work_dir)
    except Exception:  # noqa: BLE001 — registry unavailable; default boundary
        return ["tb/"]
    from booley.runtime.shared_infra import source_dir_prefixes

    prefixes = source_dir_prefixes([*tb_dirs, *tb_incl], work_dir)
    return [prefix for prefix in prefixes if "\\" not in prefix] or ["tb/"]


_CREATOR_TOOLS = ["Read", "Grep", "Glob"]

_CREATOR_JSON_EXAMPLE = """\
```json
{
  "mutations": [
    {
      "index": 1,
      "category": "operator_change",
      "file": "mod_a.sv",
      "line": 42,
      "original_code": "a + b",
      "mutated_code": "a - b",
      "detectability_argument": "Flipping addition to subtraction corrupts every result"
    }
  ]
}
```"""

# ---------------------------------------------------------------------------
# Data classes (unchanged from previous flow — JSON shape is the same)
# ---------------------------------------------------------------------------


@dataclass
class MutationSpec:
    index: int
    category: str
    file: str
    line: int
    original_code: str
    mutated_code: str
    detectability_argument: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "category": self.category,
            "file": self.file,
            "line": self.line,
            "original_code": self.original_code,
            "mutated_code": self.mutated_code,
            "detectability_argument": self.detectability_argument,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MutationSpec:
        detectability_argument = as_str(d.get("detectability_argument", ""))
        if detectability_argument is None:
            raise BoundaryError("detectability_argument must be a string")
        return cls(
            index=require_int(d.get("index"), field="mutation index"),
            category=require_str(d, "category"),
            file=require_str(d, "file"),
            line=require_int(d.get("line"), field="mutation line"),
            original_code=require_str(d, "original_code"),
            mutated_code=require_str(d, "mutated_code"),
            detectability_argument=detectability_argument,
        )


@dataclass
class MutationResult:
    index: int
    detected: bool = False
    invalid: bool = False
    sim_output_snippet: str = ""
    #: Project-relative path of this mutant's full simulator output. The
    #: snippet above is 200 chars; for a mutant the testbench failed to kill
    #: there is no error text at all, so the log is the only place the run's
    #: behaviour can be inspected. Empty when the log could not be written.
    log_path: str = ""
    #: First public Target test whose verdict killed this mutant. Cocotb
    #: batches are recovered from their structured results line; classic
    #: Targets use the first failing campaign unit.
    first_killing_test: str = ""


@dataclass
class MutationTestRun:
    """Outcome of one test within a Target-wide mutation campaign."""

    test_name: str
    process: subprocess.CompletedProcess[str] | None = None
    timed_out: bool = False
    error: str = ""
    output: str = ""
    requires_cocotb_results: bool = False


@dataclass(frozen=True)
class VariantSuiteVerdict:
    """One complete mutant-suite classification from trustworthy evidence."""

    detected: bool = False
    first_killing_test: str = ""
    inconclusive_reason: str = ""


@dataclass
class MutationSummary:
    specs: list[MutationSpec] = field(default_factory=list)
    results: list[MutationResult] = field(default_factory=list)

    @property
    def detected_count(self) -> int:
        return sum(1 for r in self.results if r.detected and not r.invalid)

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
                    "category": spec.category,
                    "file": spec.file,
                    "line": spec.line,
                    "status": status,
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
    for rel in scope_files:
        path = Path(rel) if Path(rel).is_absolute() else work_dir / rel
        source = path.read_bytes()
        readable_files += 1
        source_bytes += len(source)
        source_lines += source.count(b"\n") + int(bool(source) and not source.endswith(b"\n"))
    formula_count = max(MIN_COUNT, min(MAX_COUNT, round(math.sqrt(source_lines or 1))))

    return {
        "method": "language_neutral_source_size",
        "readable_files": readable_files,
        "source_lines": source_lines,
        "source_bytes": source_bytes,
        "formula_count": formula_count,
        "MIN": MIN_COUNT,
        "MAX": MAX_COUNT,
    }


# ---------------------------------------------------------------------------
# Parsing helpers (unchanged: shared with cold-start agent output)
# ---------------------------------------------------------------------------


def parse_creator_output(output: str) -> list[MutationSpec]:
    """Extract mutation specs from creator agent output."""
    data = _extract_json(output)
    if data is None:
        return []
    mutations_raw = data.get("mutations", [])
    if not isinstance(mutations_raw, list):
        return []
    specs: list[MutationSpec] = []
    for item in mutations_raw:
        if isinstance(item, dict):
            try:
                specs.append(MutationSpec.from_dict(item))
            except (BoundaryError, KeyError, TypeError):
                logger.warning("Skipping malformed mutation spec: %s", item)
    return specs


def _sanitize_json_text(text: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _try_parse_json(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    sanitized = _sanitize_json_text(text)
    if sanitized != text:
        try:
            data = json.loads(sanitized)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract the first valid JSON object from agent output."""
    result = _try_parse_json(text)
    if result is not None:
        return result
    for match in re.finditer(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL):
        result = _try_parse_json(match.group(1))
        if result is not None:
            return result
    for i, ch in enumerate(text):
        if ch == "{":
            block = _extract_balanced_braces(text, i)
            if block:
                result = _try_parse_json(block)
                if result is not None:
                    return result
    return None


def _extract_balanced_braces(text: str, start: int) -> str | None:
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, min(start + 50_000, len(text))):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            if in_string:
                escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# Artifact generation (markdown summaries; unchanged shape)
# ---------------------------------------------------------------------------


def generate_specs_markdown(specs: list[MutationSpec]) -> str:
    lines = [
        "# Mutation Specifications",
        "",
        "| # | Category | File:Line | Original | Mutated | Detectability |",
        "|---|----------|-----------|----------|---------|---------------|",
    ]
    for s in specs:
        lines.append(
            f"| {s.index} | {s.category} | "
            f"{s.file}:{s.line} | "
            f"`{s.original_code}` | `{s.mutated_code}` | "
            f"{s.detectability_argument} |"
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


def generate_results_markdown(
    summary: MutationSummary,
    min_detected: int,
    *,
    variant_paths: dict[int, str] | None = None,
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
        f"- Coverage gaps: {summary.not_detected_count}",
        f"- Invalid: {summary.invalid_count}",
        "",
    ]
    # Call out the surviving mutants explicitly — the actionable output of the
    # run (QA_REPORT C2.4). Each is a real testbench coverage gap: this exact
    # code change went undetected.
    survivors = summary.surviving_specs()
    if survivors:
        # Each survivor cites its own sim log: a killed mutant leaves a failure
        # message to read, a survivor leaves a clean passing run, so the log is
        # the only way to see what the design actually did under the mutation.
        log_by_index = {r.index: r.log_path for r in summary.results}
        lines.append("## Surviving mutants (coverage gaps)")
        lines.append("")
        for s in survivors:
            log = log_by_index.get(s.index)
            where = f" — sim log: {log}" if log else ""
            lines.append(
                f"- **{_mutation_source_link(s, variant_paths)}** ({s.category}): "
                f"`{s.original_code}` -> `{s.mutated_code}`{where}"
            )
        lines.append("")
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
            f"{c['status']} | "
            f"{_markdown_table_cell(c['first_killing_test'] or '')} | "
            f"{_markdown_table_cell(c['sim_output_snippet'][:60])} |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Verification round outcome (used by cold-start)
# ---------------------------------------------------------------------------


class UnsupportedSimTargetError(RuntimeError):
    """The mutation loop cannot drive this sim Target, and says why.

    Raised at ``_run`` time (before any agent work) so an unsupported Target
    costs one ``.core`` read instead of three creator rounds. The message is
    surfaced verbatim as the Specialist's ``report_text`` — it must name the Target
    and the concrete reason (SETUP-F-40).
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
class VerificationOutcome:
    """Result of one cold-start verification round."""

    ok: bool
    baseline_passed: bool
    pinned_passed: bool
    log_tail: str = ""
    reason: str = ""
    #: Non-empty when the round failed because the *harness* broke (no built
    #: binary, missing simulator) rather than because a proposed variant
    #: misbehaved. Retrying the creator cannot fix this, so the caller aborts
    #: instead of burning further rounds re-prompting it (SETUP-F-41a).
    infra_error: str = ""


@dataclass
class MutationRunPlan:
    """Resolved run configuration, shared across the cold + warm chains.

    Bundles the scope + budget cluster computed once in ``_run`` and threaded
    unchanged through ``_run_cold`` / ``_run_warm`` down to result assembly,
    instead of re-listing all ten fields at every hop.
    """

    scope_files: list[str]
    scope_hashes: dict[str, str]
    work_dir: Path
    target: str
    report_dir: Path | None
    min_detected: int
    count: int
    auto_mode: bool
    formula_count: int
    source_size_budget: dict | None


@dataclass
class RunResultInputs:
    """Per-run outcome fed to ``_build_run_result``.

    The run-specific counterpart to :class:`MutationRunPlan`: values produced by
    an individual cold or warm run (timings, cache/reuse flags, the summary)
    that the plan config alone cannot supply.
    """

    summary: MutationSummary
    count: int  # mutations actually run this invocation (may be < plan.count)
    tester_elapsed: float
    creator_elapsed: float
    reused_lock: bool
    lock_created_at: str | None
    verification_rounds: int
    build_cached: bool
    variants: MutationVariantPlan
    #: Nothing was killed, yet every mutation is provably live — the Target's
    #: tests simply never exercise this scope (SETUP-F-38).
    coverage_gap: bool = False
    #: Exact-replacement evidence reported so the verdict is auditable.
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreparedMutationRound:
    """Creator output resolved far enough to decide whether to test or retry."""

    specs: list[MutationSpec]
    variants: MutationVariantPlan | None
    creator_elapsed: float
    retry_prompt: str = ""
    error: McpToolResult | None = None
    failure: VerificationOutcome | None = None


@dataclass(frozen=True)
class ColdVariantOutcome:
    """Completed cold variant sweep ready for durable result assembly."""

    specs: list[MutationSpec]
    summary: MutationSummary
    variants: MutationVariantPlan
    creator_elapsed: float
    tester_elapsed: float
    verification_rounds: int


# ---------------------------------------------------------------------------
# Specialist implementation
# ---------------------------------------------------------------------------


class MutationTesterSpecialist(Specialist):
    """Lock-based mutation testing."""

    name: str = "mutation_tester"
    description: str = (
        "Proposal-locked mutation testing: creator selects exact replacements, "
        "tester builds isolated variants"
    )
    code_modifying: bool = False
    announce_success_report: bool = True
    min_model: str = "standard"
    default_timeout: int = 1800
    min_timeout: int = 1200
    satisfies: ClassVar[list[str]] = ["mutation_score"]

    # Cross-invocation session key for creator agent resume during retry rounds.
    SESSION_KEY: ClassVar[str] = "mutation_tester_creator"

    # Maximum verification rounds during cold start (1 happy path + 2 retries).
    MAX_VERIFICATION_ROUNDS: ClassVar[int] = 3

    def _add_agent_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--scope",
            default=None,
            help="Comma-separated RTL files to mutate; defaults from the Target criterion",
        )
        parser.add_argument(
            "--tb-top",
            default=None,
            help="Testbench top module name. Defaults to the resolved sim Target's toplevel.",
        )
        parser.add_argument(
            "--dut-top",
            default=None,
            help="Optional DUT top hint included in the read-only creator prompt.",
        )
        parser.add_argument(
            "--dut-files",
            nargs="+",
            default=None,
            help=(
                "DUT source files (space-separated). Optional: defaults to the "
                "RTL (non-tb) source files of the --target Target, resolved from "
                "the .core in either mode. Pass explicitly only to override that "
                "or when the Target cannot be resolved."
            ),
        )

        def _count_type(v: str) -> int | str:
            return v if v.strip().lower() == "auto" else int(v)

        parser.add_argument(
            "--count",
            type=_count_type,
            default=None,
            help="Number of mutations: integer or 'auto' (default: 10)",
        )
        parser.add_argument(
            "--min-detected",
            type=int,
            default=None,
            help=(
                "Pass threshold: minimum mutations that must be killed "
                "(default: all of --count, i.e. all-or-nothing). Lower it to "
                "accept a partial mutation score."
            ),
        )
        parser.add_argument(
            "--steer",
            default=None,
            help="Developer Agent context for mutation targeting",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Compute the source-size budget without running mutations",
        )
        parser.add_argument(
            "--regen-lock",
            action="store_true",
            help="Wipe the mutation lock dir and re-run the creator agent from scratch.",
        )

    # ------------------------------------------------------------------
    # Abstract method stubs — _run() drives the flow directly.
    # ------------------------------------------------------------------

    def _build_prompt(self) -> str:
        raise NotImplementedError("MutationTesterSpecialist overrides _run() directly")

    def _interpret_output(self, output: str, structured: dict | None) -> McpToolResult:
        raise NotImplementedError("MutationTesterSpecialist overrides _run() directly")

    # ------------------------------------------------------------------
    # Prompt building — cold start
    # ------------------------------------------------------------------

    def _build_creator_prompt(self) -> str:
        """Build the read-only proposal prompt for a cold start."""
        scope_files = self._scope_files()
        scope_str = ", ".join(scope_files)
        count = self.args.count
        steer_section = ""
        if self.args.steer:
            steer_section = f"\n## Developer Agent Context\n\n{self.args.steer}\n"

        boundary_section = self._mutation_boundary_section()
        task_section = self._creator_task_section(count)
        tb_dirs = ", ".join(_configured_testbench_dirs(self.args.work_dir))

        return f"""You are a mutation testing CREATOR agent.
You are read-only. Do not edit any file and do not run shell commands or
simulators. You MUST NOT read testbench files ({tb_dirs} or any configured
testbench source/include directory). Inspect only the authorized RTL scope.

Read the mutation testing guide at `{_mutation_guide_path()}` before starting.

## RTL Files in Scope

{scope_str}
{boundary_section}
{task_section}
{steer_section}
## Output Format (MANDATORY)

Return a JSON object with a "mutations" array in the form below. `original_code`
must be the exact UTF-8 source slice beginning on `line`; Booley locates that
slice byte-for-byte and rejects missing or ambiguous proposals.

{_CREATOR_JSON_EXAMPLE}
"""

    @staticmethod
    def _creator_task_section(count: int) -> str:
        return f"""## Task

Design {count} single-point RTL mutations that a reasonable testbench
SHOULD detect. Return proposals only; do not modify the source.

Read each scope file thoroughly, following submodule instantiations into
their source, to map the datapath, control logic, and output ports.

For each mutation k = 1 .. {count}:
1. Pick a single mutation site per the categories in the guide.
2. Copy the exact source text to replace into `original_code`, preserving its
   whitespace, punctuation, and capitalization. Set `line` to its first line.
3. Put only the replacement text in `mutated_code`; do not add muxes, markers,
   packages, selectors, or surrounding context.
4. Distribute proposals across the authorized scope files.

Booley applies one exact replacement at a time, compiles that isolated source
variant, runs the complete Target suite, and restores the pristine bytes. The
untouched project—not a selector branch—is the campaign baseline."""

    def _mutation_boundary_section(self) -> str:
        top = self._dut_top_module()
        files = self._dut_files()
        if not top and not files:
            return ""
        lines = ["\n## Mutation Boundary\n"]
        if top:
            lines.append(f"- Top module: `{top}`")
        if files:
            lines.append(f"- DUT files: {', '.join(files)}")
        lines.append("")
        return "\n".join(lines)

    def _build_retry_prompt(self, outcome: VerificationOutcome) -> str:
        """Ask for a complete replacement proposal list after validation fails."""
        return f"""Your mutation proposals failed exact replacement validation or
isolated compilation.

Reason: {outcome.reason}

Diagnostic tail:

```
{outcome.log_tail}
```

Do not edit any file. Return a complete fresh JSON mutation list. Every
`original_code` must be copied exactly from the declared starting line, every
replacement must differ, and every proposal must remain a single source edit.
"""

    # ------------------------------------------------------------------
    # Scope / DUT helpers
    # ------------------------------------------------------------------

    def _scope_files(self) -> list[str]:
        return [
            fusesoc_registry.canonical_project_path(self.args.work_dir, raw)
            for value in self.args.scope.split(",")
            if (raw := value.strip())
        ]

    def _apply_campaign_defaults(self) -> McpToolResult | None:
        """Default mutation controls from this Target's criterion parameters."""
        key = f"mutation_score_{self.args.target}"
        try:
            campaign = resolve_target_campaign(
                self.args.target,
                self.satisfies,
                self.state.criteria,
                explicit_scope=self.args.scope,
            )
        except CampaignScopeError:
            return McpToolResult(
                exit_code=EXIT_FAILURE,
                report_text=(
                    f"mutation_tester: {key} must declare scope: [rtl/file.sv, ...] "
                    "or the invocation must pass --scope."
                ),
            )
        except NoRunnableTestsError as exc:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"mutation_tester: {exc}",
            )
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

    @staticmethod
    def _canonicalize_spec_paths(specs: list[MutationSpec], work_dir: Path) -> None:
        """Normalize validated creator paths for downstream artifact lookups."""
        for spec in specs:
            spec.file = fusesoc_registry.canonical_project_path(work_dir, spec.file)

    def _validate_scope_against_target(self, scope_files: list[str]) -> McpToolResult | None:
        """Fail fast when a ``--scope`` entry isn't a source file of ``--target``.

        A plausible-but-wrong scope (classically the stealth-cores mirror
        ``.booley_project/cores/rtl/foo.sv`` instead of the repo-relative
        ``rtl/foo.sv``) would otherwise be discovered only after proposal
        validation. Resolving the scope against the Target's fileset up front (a
        subprocess-free ``.core`` read, <1s) turns that into an instant,
        self-correcting error with a "did you mean" hint.

        Fails open (returns ``None``) when the target can't be resolved or its
        source list is empty — Interactive-Mode ``--dut-files`` invocations author
        no ``.core``, and must keep running.
        """
        target = getattr(self.args, "target", "") or ""
        if not target:
            return None
        try:
            resolved = list(inspect_target(self.args.work_dir, target).rtl_files)
        except Exception:  # noqa: BLE001 — unresolvable target: fail open, let downstream report
            return None
        if not resolved:
            return None
        resolved_set = set(resolved)
        by_base = {Path(r).name: r for r in resolved}
        for entry in scope_files:
            if entry in resolved_set:
                continue
            suggestion = by_base.get(Path(entry).name)
            hint = f" Did you mean '{suggestion}'?" if suggestion else ""
            return McpToolResult(
                exit_code=EXIT_FAILURE,
                report_text=(
                    f"mutation_tester: --scope entry '{entry}' does not match any "
                    f"source file resolved for config '{target}'.{hint} "
                    f"Resolved sources: {', '.join(resolved)}"
                ),
            )
        return None

    def _dut_top_module(self) -> str:
        """Return an explicit top hint without inspecting HDL syntax."""
        return getattr(self.args, "dut_top", None) or ""

    def _dut_files(self) -> list[str]:
        """Resolve DUT source files exposed as creator prompt context.

        Priority: explicit ``--dut-files`` arg (Interactive Mode) > the RTL
        (non-``tb``) source files of the resolved sim Target, read straight from
        the ``.core`` (ADR 0022 dec 13). The ``.core`` read is used rather than a
        resolved Target to keep this lookup subprocess-free. Empty list when
        neither is available.

        The read spans the ``depend`` closure, not just the root core's own
        filesets. In a layered repo the sim Target's root fileset owns the
        harness while the DUT arrives transitively — Ibex's
        ``ibex_top_tracing.sv`` is a dependency of the Target that
        instantiates it — so a root-only read finds no DUT at all and
        mutation fails before injection (F-27).
        """
        explicit = getattr(self.args, "dut_files", None)
        if explicit:
            return [
                fusesoc_registry.canonical_project_path(self.args.work_dir, path)
                for path in explicit
            ]
        target = getattr(self.args, "target", "") or ""
        if not target:
            return []
        try:
            return list(inspect_target(self.args.work_dir, target).rtl_files)
        except Exception:  # noqa: BLE001 — best-effort source-file lookup; degrades to an empty list
            return []

    def _validate_interactive_args(self) -> McpToolResult | None:
        """Reject invocations whose Target or mutation scope is incomplete.

        ``--tb-top`` is waived for a Cocotb Target: its binary is ``Vtop`` and
        the testbench is a Python module, so there is no SV testbench top to
        name and demanding one blocks an otherwise runnable Target (SETUP-F-40).
        """
        target = getattr(self.args, "target", "") or ""
        if not target.strip():
            return McpToolResult(
                exit_code=EXIT_FAILURE,
                report_text=(
                    "mutation_tester: --target is required. Pass --target "
                    "<name>; it names a Target in the project's .core file "
                    "(list them with `booley targets`)."
                ),
            )
        try:
            self._validate_target_runner(target, self.args.work_dir)
            is_cocotb = self.cocotb_target(target, self.args.work_dir) is not None
        except UnsupportedSimTargetError as exc:
            return McpToolResult(exit_code=EXIT_ERROR, report_text=str(exc))
        if not is_cocotb and not getattr(self.args, "tb_top", None):
            return McpToolResult(
                exit_code=EXIT_FAILURE,
                report_text=(
                    "mutation_tester: --tb-top is required when running "
                    "outside a ticket (testbench top module, e.g. 'tb')."
                ),
            )
        return None

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
        matching progress line for either the auto-scaled or fixed-count path.
        """
        if self.args.count == "auto":
            source_size_budget = compute_source_size_budget(scope_files, work_dir)
            formula_count = source_size_budget["formula_count"]
            timeout = getattr(self.args, "timeout", 1800)
            budget_cap = max(3, timeout // 150)
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
                f"auto-scaled: {count} mutations "
                f"(source-size formula {formula_count}, budget cap {budget_cap})",
            )
        else:
            self.emit_progress(f"target: {count} mutations")
        return count, source_size_budget, formula_count, auto_mode

    def _run(self) -> McpToolResult:
        if campaign_error := self._apply_campaign_defaults():
            return campaign_error
        prepared = self._prepare_run_plan()
        if isinstance(prepared, McpToolResult):
            return prepared
        existing_lock = self._load_reusable_lock(prepared)
        if existing_lock is not None:
            return self._run_warm(existing_lock, prepared)
        return self._run_cold(prepared)

    def _prepare_run_plan(self) -> MutationRunPlan | McpToolResult:
        work_dir = self.args.work_dir
        target = self.args.target
        scope_files = self._scope_files()
        if validation_error := self._validate_run_inputs(target, work_dir, scope_files):
            return validation_error
        count, source_size_budget, formula_count, auto_mode = self._resolve_count(
            scope_files,
            work_dir,
        )
        if getattr(self.args, "dry_run", False):
            budget = source_size_budget or compute_source_size_budget(scope_files, work_dir)
            output = json.dumps(budget, indent=2)
            print(output)
            return McpToolResult(exit_code=EXIT_SUCCESS, report_text=output)
        self.args.count = count
        return MutationRunPlan(
            scope_files=scope_files,
            scope_hashes=lock_mod.compute_scope_hashes(scope_files, work_dir),
            work_dir=work_dir,
            target=target,
            report_dir=self.args.report_dir,
            min_detected=(self.args.min_detected if self.args.min_detected is not None else count),
            count=count,
            auto_mode=auto_mode,
            formula_count=formula_count,
            source_size_budget=source_size_budget,
        )

    def _validate_run_inputs(
        self,
        target: str,
        work_dir: Path,
        scope_files: list[str],
    ) -> McpToolResult | None:
        if scope_error := self._validate_scope_against_target(scope_files):
            return scope_error
        try:
            self._validate_target_runner(target, work_dir)
            self.cocotb_target(target, work_dir)
            self._target_test_suite(target)
        except UnsupportedSimTargetError as exc:
            return McpToolResult(exit_code=EXIT_ERROR, report_text=str(exc))
        return None

    def _load_reusable_lock(self, plan: MutationRunPlan) -> lock_mod.LockMeta | None:
        if getattr(self.args, "regen_lock", False):
            logger.info("--regen-lock requested: wiping existing lock dir")
            lock_mod.wipe_lock()
            self._clear_session_id(self.SESSION_KEY)
        existing_lock = lock_mod.load_lock()
        if existing_lock is None:
            return None
        if not lock_mod.is_lock_valid(
            existing_lock,
            plan.scope_files,
            plan.scope_hashes,
        ):
            logger.info("mutation proposal lock is stale — wiping before cold start")
            lock_mod.wipe_lock()
            return None
        if existing_lock.count < plan.count:
            logger.info(
                "lock has %d mutations but %d requested — forcing cold start",
                existing_lock.count,
                plan.count,
            )
            lock_mod.wipe_lock()
            self._clear_session_id(self.SESSION_KEY)
            return None
        return existing_lock

    # ------------------------------------------------------------------
    # Cold start
    # ------------------------------------------------------------------

    def _run_cold(self, plan: MutationRunPlan) -> McpToolResult:
        """Create exact proposals, then build the pristine and mutant variants."""
        self.emit_progress("cold start: read-only mutation proposal phase")
        with contextlib.suppress(OSError):
            shutil.rmtree(lock_mod.verification_rounds_dir(), ignore_errors=True)
            shutil.rmtree(lock_mod.variants_dir(), ignore_errors=True)
        pre_dirty = self._git_modified_tracked(plan.work_dir)
        result: McpToolResult | None = None
        try:
            result = self._create_and_run_variants(plan, pre_dirty)
        finally:
            self._revert_stray_tracked_edits(plan.work_dir, pre_dirty, keep=[])
        assert result is not None
        self._add_residue_warning(result, set(plan.scope_files))
        return result

    def _create_and_run_variants(
        self,
        plan: MutationRunPlan,
        pre_dirty: set[str] | None,
    ) -> McpToolResult:
        baseline_error = self._run_pristine_baseline(plan)
        if baseline_error is not None:
            return baseline_error

        outcome = self._run_cold_variant_rounds(plan, pre_dirty)
        if isinstance(outcome, McpToolResult):
            return outcome
        return self._complete_cold_campaign(plan, outcome)

    def _run_cold_variant_rounds(
        self,
        plan: MutationRunPlan,
        pre_dirty: set[str] | None,
    ) -> ColdVariantOutcome | McpToolResult:
        creator_prompt = self._build_creator_prompt()
        retry_prompt = ""
        creator_elapsed = 0.0
        tester_elapsed = 0.0
        last_specs: list[MutationSpec] = []
        last_summary: MutationSummary | None = None
        last_failure: VerificationOutcome | None = None
        for round_idx in range(1, self.MAX_VERIFICATION_ROUNDS + 1):
            prompt = creator_prompt if round_idx == 1 else retry_prompt
            prepared = self._prepare_mutation_round(plan, prompt, round_idx, pre_dirty)
            creator_elapsed += prepared.creator_elapsed
            if prepared.error is not None:
                return prepared.error
            last_specs = prepared.specs
            if prepared.variants is None:
                last_failure = prepared.failure
                retry_prompt = prepared.retry_prompt
                continue

            variants = prepared.variants
            results, elapsed, infra = self._run_variant_sweep(plan, prepared.specs, variants)
            tester_elapsed += elapsed
            last_summary = MutationSummary(specs=prepared.specs, results=results)
            if infra:
                return self._variant_infra_error(
                    plan,
                    prepared.specs,
                    round_idx,
                    infra,
                    last_summary,
                    variants=variants,
                )
            if last_summary.invalid_count:
                last_failure = self._invalid_variant_failure(round_idx, results)
                retry_prompt = self._build_retry_prompt(last_failure)
                continue
            return ColdVariantOutcome(
                specs=prepared.specs,
                summary=last_summary,
                variants=variants,
                creator_elapsed=creator_elapsed,
                tester_elapsed=tester_elapsed,
                verification_rounds=round_idx,
            )
        return self._proposal_rounds_exhausted(plan, last_specs, last_summary, last_failure)

    def _prepare_mutation_round(
        self,
        plan: MutationRunPlan,
        prompt: str,
        round_idx: int,
        pre_dirty: set[str] | None,
    ) -> PreparedMutationRound:
        self.emit_progress(f"proposal round {round_idx}/{self.MAX_VERIFICATION_ROUNDS}")
        specs, elapsed, error = self._proposal_round(plan, prompt, round_idx, pre_dirty)
        if error is not None:
            return PreparedMutationRound([], None, elapsed, error=error)
        if not specs:
            return PreparedMutationRound(
                [], None, elapsed, error=self._creator_output_error(plan, round_idx)
            )
        self._canonicalize_spec_paths(specs, plan.work_dir)
        variants, reason = self._resolve_proposals(specs, plan)
        if not reason:
            return PreparedMutationRound(specs, variants, elapsed)
        failure = self._proposal_failure(round_idx, reason)
        return PreparedMutationRound(
            specs,
            None,
            elapsed,
            retry_prompt=self._build_retry_prompt(failure),
            failure=failure,
        )

    @staticmethod
    def _creator_output_error(plan: MutationRunPlan, round_idx: int) -> McpToolResult:
        reason = "creator agent returned no parseable mutation specs"
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=reason,
            detail=_failure_detail(
                phase="creator_output",
                reason=reason,
                specs=[],
                work_dir=plan.work_dir,
                verification_rounds=round_idx,
            ),
        )

    def _invalid_variant_failure(
        self,
        round_idx: int,
        results: list[MutationResult],
    ) -> VerificationOutcome:
        invalid = "; ".join(
            f"#{result.index}: {result.sim_output_snippet}" for result in results if result.invalid
        )
        return self._proposal_failure(
            round_idx,
            f"isolated variant(s) did not compile or yield a verdict: {invalid}",
        )

    def _complete_cold_campaign(
        self,
        plan: MutationRunPlan,
        outcome: ColdVariantOutcome,
    ) -> McpToolResult:
        self._persist_lock(plan, outcome.specs)
        self._clear_session_id(self.SESSION_KEY)
        summary = outcome.summary
        self.emit_progress(
            f"cold done: {summary.detected_count}/"
            f"{summary.detected_count + summary.not_detected_count} detected"
        )
        persisted = lock_mod.load_lock()
        return self._build_run_result(
            plan,
            RunResultInputs(
                summary=summary,
                count=len(outcome.specs),
                tester_elapsed=outcome.tester_elapsed,
                creator_elapsed=outcome.creator_elapsed,
                reused_lock=False,
                lock_created_at=persisted.created_at if persisted else None,
                verification_rounds=outcome.verification_rounds,
                build_cached=False,
                variants=outcome.variants,
                coverage_gap=self._is_variant_coverage_gap(summary, plan.min_detected),
                evidence=self._variant_evidence(outcome.variants),
            ),
        )

    @staticmethod
    def _resolve_proposals(
        specs: list[MutationSpec],
        plan: MutationRunPlan,
    ) -> tuple[MutationVariantPlan | None, str]:
        expected = set(range(1, plan.count + 1))
        actual = {spec.index for spec in specs}
        if len(specs) != plan.count or actual != expected:
            return None, f"expected mutation indices 1..{plan.count}; got {sorted(actual)}"
        try:
            return MutationVariantPlan.resolve(specs, plan.work_dir, plan.scope_files), ""
        except MutationVariantError as exc:
            return None, str(exc)

    def _proposal_round(
        self,
        plan: MutationRunPlan,
        prompt: str,
        round_idx: int,
        pre_dirty: set[str] | None,
    ) -> tuple[list[MutationSpec], float, McpToolResult | None]:
        try:
            with hide_opposite_sources(plan.work_dir, "rtl"):
                specs, elapsed = self._invoke_creator(
                    prompt, resume=round_idx > 1, attempt=round_idx
                )
        except Exception as exc:
            logger.exception("Creator invocation failed on round %d", round_idx)
            error = McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"creator agent invocation failed: {exc}",
            )
            return [], 0.0, error
        strays = self._revert_stray_tracked_edits(plan.work_dir, pre_dirty, keep=[])
        if strays:
            logger.warning(
                "read-only mutation creator wrote %d file(s) (%s) — reverted",
                len(strays),
                ", ".join(strays[:5]),
            )
            self.emit_progress(f"scope guard: reverted {len(strays)} creator edit(s)")
        return specs, elapsed, None

    def _invoke_creator(
        self,
        prompt: str,
        *,
        resume: bool,
        attempt: int,
    ) -> tuple[list[MutationSpec], float]:
        """Invoke the creator agent.  Returns (parsed_specs, elapsed_s)."""
        start = time.monotonic()
        params = AgentCallParams(
            prompt=prompt,
            model=self._resolve_model(),
            cwd=self.args.work_dir,
            allowed_agent_capabilities=_CREATOR_TOOLS,
            system_prompt=None,
            output_format=None,
            max_turns=self.args.max_turns,
            timeout_seconds=int(self.args.timeout * 0.8),
            transcript_path=_make_transcript_path(
                self.args.transcript_dir,
                f"mutation_creator_round{attempt}",
            ),
            label=f"mutation_creator_round{attempt}",
            needs_skills=False,
        )
        if resume:
            sid = self._load_session_id(self.SESSION_KEY)
            if sid:
                params = self._build_resume_params(params, sid)
            else:
                logger.warning(
                    "no persisted session_id for resume on round %d",
                    attempt,
                )

        result = self._invoke_agent_with_resume(params)
        # _last_session_id was set inside _invoke_agent; persist for next round.
        self._persist_session_id(self.SESSION_KEY)

        # Always try to parse — the retry path for forbidden-category fails
        # asks the agent to emit a fresh spec list, while sim/elab retries
        # ask it to edit source only.  Empty parse on a resume round is
        # the normal "no JSON, just source edits" case.
        raw_output = result.output if hasattr(result, "output") else str(result)
        specs = parse_creator_output(raw_output)
        return specs, time.monotonic() - start

    def _run_pristine_baseline(self, plan: MutationRunPlan) -> McpToolResult | None:
        """Build and run the byte-identical project before any proposal is applied."""
        build_path = lock_mod.baseline_build_dir()
        shutil.rmtree(build_path, ignore_errors=True)
        build_path.mkdir(parents=True, exist_ok=True)
        elab = self._run_elab(plan.target, plan.work_dir, build_path)
        elab_output = (elab.stdout or "") + (elab.stderr or "")
        if elab.returncode != 0:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="mutation baseline did not elaborate from pristine source\n"
                + _tail(elab_output, 50),
                detail=_failure_detail(
                    phase="baseline_elaboration",
                    reason="pristine source did not elaborate",
                    specs=[],
                    work_dir=plan.work_dir,
                    log_tail=_tail(elab_output, 50),
                ),
            )

        runs = self._run_target_test_suite(
            plan.target,
            plan.work_dir,
            build_path,
            self.args.tb_top,
        )
        output = self._suite_output(runs)
        self._persist_baseline_log(output)
        inconclusive = self._suite_inconclusive_reason(runs)
        if inconclusive:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"mutation baseline was inconclusive: {inconclusive}\n"
                + _tail(output, 50),
            )
        if not self._baseline_suite_passed(runs):
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="mutation baseline failed on pristine source\n" + _tail(output, 50),
            )
        return None

    def _proposal_failure(self, round_idx: int, reason: str) -> VerificationOutcome:
        outcome = VerificationOutcome(
            ok=False,
            baseline_passed=True,
            pinned_passed=False,
            reason=reason,
            log_tail=reason,
        )
        self._write_round_log(round_idx, outcome)
        return outcome

    def _variant_infra_error(
        self,
        plan: MutationRunPlan,
        specs: list[MutationSpec],
        round_idx: int,
        reason: str,
        summary: MutationSummary,
        *,
        variants: MutationVariantPlan | None = None,
    ) -> McpToolResult:
        variants = variants or MutationVariantPlan.resolve(specs, plan.work_dir, plan.scope_files)
        detail = _failure_detail(
            phase="variant_infrastructure",
            reason=reason,
            specs=specs,
            work_dir=plan.work_dir,
            summary=summary,
            min_detected=plan.min_detected,
            count=plan.count,
            verification_rounds=round_idx,
        )
        inputs = RunResultInputs(
            summary=summary,
            count=len(specs),
            tester_elapsed=0.0,
            creator_elapsed=0.0,
            reused_lock=round_idx == 0,
            lock_created_at=None,
            verification_rounds=round_idx,
            build_cached=False,
            variants=variants,
            evidence=self._variant_evidence(variants),
        )
        self._attach_campaign_artifacts(plan, inputs, detail)
        report_text = f"mutation campaign produced no trustworthy verdict: {reason}"
        artifact_lines = self._artifact_display_lines(detail)
        if artifact_lines:
            report_text += "\n\nArtifacts:\n" + "\n".join(f"  {line}" for line in artifact_lines)
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=report_text,
            detail=detail,
            display_lines=artifact_lines,
        )

    def _proposal_rounds_exhausted(
        self,
        plan: MutationRunPlan,
        specs: list[MutationSpec],
        summary: MutationSummary | None,
        outcome: VerificationOutcome | None,
    ) -> McpToolResult:
        reason = outcome.reason if outcome else "no valid mutation proposals"
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=(
                f"creator mutation proposals failed after {self.MAX_VERIFICATION_ROUNDS} "
                f"rounds: {reason}"
            ),
            detail=_failure_detail(
                phase="proposal_validation",
                reason=reason,
                specs=specs,
                work_dir=plan.work_dir,
                summary=summary,
                min_detected=plan.min_detected,
                count=plan.count,
                verification_rounds=self.MAX_VERIFICATION_ROUNDS,
                log_tail=outcome.log_tail if outcome else "",
            ),
        )

    def _write_round_log(
        self,
        round_idx: int,
        outcome: VerificationOutcome,
    ) -> None:
        round_logs_dir = lock_mod.verification_rounds_dir()
        round_logs_dir.mkdir(parents=True, exist_ok=True)
        (round_logs_dir / f"round_{round_idx}.log").write_text(
            f"baseline_passed={outcome.baseline_passed}\n"
            f"variants_valid={outcome.pinned_passed}\n"
            f"reason={outcome.reason}\n"
            f"---log_tail---\n{outcome.log_tail}\n",
            encoding="utf-8",
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

    @staticmethod
    def _persist_lock(plan: MutationRunPlan, specs: list[MutationSpec]) -> None:
        lock_mod.save_lock(
            lock_mod.LockMeta(
                schema_version=lock_mod.LOCK_SCHEMA_VERSION,
                created_at=lock_mod.now_iso(),
                scope=list(plan.scope_files),
                scope_hashes=plan.scope_hashes,
                count=len(specs),
                mutations=[spec.to_dict() for spec in specs],
            )
        )

    def _run_warm(
        self,
        lock: lock_mod.LockMeta,
        plan: MutationRunPlan,
    ) -> McpToolResult:
        """Reuse validated proposals while rebuilding every isolated variant."""
        self.emit_progress(
            f"warm reuse: proposal lock from {lock.created_at}, {lock.count} mutations"
        )
        try:
            specs = [MutationSpec.from_dict(item) for item in lock.mutations][: plan.count]
            self._canonicalize_spec_paths(specs, plan.work_dir)
            variants = MutationVariantPlan.resolve(specs, plan.work_dir, plan.scope_files)
        except (BoundaryError, MutationVariantError) as exc:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"mutation proposal lock is stale or invalid: {exc}; use --regen-lock",
            )

        baseline_error = self._run_pristine_baseline(plan)
        if baseline_error is not None:
            return baseline_error
        results, tester_elapsed, infra = self._run_variant_sweep(plan, specs, variants)
        summary = MutationSummary(specs=specs, results=results)
        if infra:
            return self._variant_infra_error(
                plan,
                specs,
                0,
                infra,
                summary,
                variants=variants,
            )
        if summary.invalid_count:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    "warm reuse: one or more isolated variants no longer compile; "
                    "use --regen-lock to replace the proposals"
                ),
                detail=_failure_detail(
                    phase="warm_variant_validation",
                    reason="isolated variant did not compile or yield a verdict",
                    specs=specs,
                    work_dir=plan.work_dir,
                    summary=summary,
                    min_detected=plan.min_detected,
                    count=plan.count,
                    verification_rounds=0,
                ),
            )

        evidence = self._variant_evidence(variants)
        return self._build_run_result(
            plan,
            RunResultInputs(
                summary=summary,
                count=len(specs),
                tester_elapsed=tester_elapsed,
                creator_elapsed=0.0,
                reused_lock=True,
                lock_created_at=lock.created_at,
                verification_rounds=0,
                build_cached=False,
                variants=variants,
                coverage_gap=self._is_variant_coverage_gap(summary, plan.min_detected),
                evidence=evidence,
            ),
        )

    def _run_elab(
        self,
        target: str,
        work_dir: Path,
        build_path: Path,
    ) -> subprocess.CompletedProcess:
        """Build the source currently present in the worktree.

        The caller chooses whether that source is pristine or has one exact
        replacement applied.  FuseSoC copy-stages that snapshot into
        *build_path*, so every variant has an independent simulation image.
        The resolved binary dir and EDA-tool family are recorded in markers
        for the matching run step.

        Returns the ``make`` :class:`~subprocess.CompletedProcess`; if FuseSoC
        resolution itself fails, a synthetic ``rc=1`` result carries the error
        so the callers' ``returncode`` / ``stdout+stderr`` checks are unchanged.
        """
        try:
            resolved = fusesoc_registry.resolve_target(
                target,
                project_root=work_dir,
                build_root=build_path,
            )
        except (
            Exception  # noqa: BLE001 — isolate resolve failure; surface as return code 1
        ) as exc:
            return subprocess.CompletedProcess(
                args=["fusesoc", "run", "--setup", "--target", target],
                returncode=1,
                stdout="",
                stderr=f"FuseSoC target resolution failed: {exc}",
            )
        rel = edam_layer.relpath_for_make(resolved.build_root, work_dir)
        (build_path / _EDALIZE_BINDIR_MARKER).write_text(rel, encoding="utf-8")
        eda_tool = sim_edam.normalize_eda_tool(getattr(resolved, "eda_tool", None))
        (build_path / _EDALIZE_EDA_TOOL_MARKER).write_text(eda_tool, encoding="utf-8")
        return subprocess.run(
            edam_layer.make_command(rel),
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )

    def cocotb_target(self, target: str, work_dir: Path) -> CocotbSimTarget | None:
        """Resolve *target*'s cocotb identity, or ``None`` for a classic Target.

        ADR 0034 decision 2: cocotb-ness lives in the Target's ``.core`` flow
        options, so this is a subprocess-free YAML read — cheap enough to run
        before any agent work, which is exactly where the mutation loop needs
        it (a Cocotb Target must never reach the ``V<top>`` run-half).

        Raises :class:`UnsupportedSimTargetError` for a Cocotb Target the mutation
        loop cannot drive, so the caller fails fast with the reason named
        rather than producing a meaningless score.
        """
        try:
            module = fusesoc_registry.target_cocotb_modules(work_dir).get(target)
        except Exception:  # noqa: BLE001 — best-effort .core read; a classic Target is the safe default
            return None
        if not module:
            return None
        eda_tool = self.target_eda_tool(target, work_dir)
        if eda_tool not in ("icarus", "verilator"):
            raise UnsupportedSimTargetError(
                f"mutation_tester: Target {target!r} is a Cocotb Target "
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
        marker = build_path / _EDALIZE_EDA_TOOL_MARKER if build_path is not None else None
        if marker is not None and marker.exists():
            return sim_edam.normalize_eda_tool(marker.read_text(encoding="utf-8").strip())
        try:
            declared = project_config.lookup_target_section(
                fusesoc_registry.target_eda_tools(work_dir),
                target,
            )
        except Exception:  # noqa: BLE001 — best-effort .core read; legacy default is Verilator
            declared = None
        return sim_edam.normalize_eda_tool(declared)

    def _validate_target_runner(self, target: str, work_dir: Path) -> None:
        """Reject Target toolchains whose prebuilt image this loop cannot drive."""
        eda_tool = self.target_eda_tool(target, work_dir)
        if eda_tool not in ("icarus", "verilator"):
            raise UnsupportedSimTargetError(
                f"mutation_tester: Target {target!r} resolves to {eda_tool!r}. "
                "The mutation run-many loop supports Icarus and Verilator only — "
                "point --target at an icarus or verilator Target."
            )

    def _bin_dir_rel(self, target: str, work_dir: Path, build_path: Path) -> str:
        """The edalize build dir, relative to *work_dir*, for the prebuilt sim."""
        marker = build_path / _EDALIZE_BINDIR_MARKER
        if marker.exists():
            return marker.read_text(encoding="utf-8").strip()
        # Defensive: marker lost (e.g. external cleanup) — re-resolve.
        resolved = fusesoc_registry.resolve_target(
            target,
            project_root=work_dir,
            build_root=build_path,
        )
        return edam_layer.relpath_for_make(resolved.build_root, work_dir)

    @staticmethod
    def _target_test_suite(target: str) -> TargetTestSuite:
        """Return every runnable test declared for *target*."""
        try:
            return require_runnable_target_test_suite(target)
        except NoRunnableTestsError as exc:
            raise UnsupportedSimTargetError(f"mutation_tester: {exc}") from exc

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
        )

    def _cocotb_sim_cmd(
        self,
        *,
        cocotb: CocotbSimTarget,
        rel: str,
        target: str,
        work_dir: Path,
        timeout: int,
        test_names: tuple[str, ...],
    ) -> list[str]:
        """Build the :mod:`booley.sim.cocotb_run` invocation for one mutant.

        A Cocotb Target's binary is driven from Python over VPI, so the run
        needs cocotb's ``COCOTB_TEST_MODULES``/filter environment — which only
        the cocotb run-half knows how to assemble.

        Cocotb batches the Target's complete resolved suite into one process.
        ``tests.toml`` ``select`` plusarg templates do not apply to Cocotb
        Targets (ADR 0034 decision 2).
        """
        cmd = [
            sys.executable,
            "-m",
            "booley.sim.cocotb_run",
            "--build-dir",
            rel,
            "--eda-tool",
            cocotb.eda_tool,
            "--cocotb-module",
            cocotb.module,
            "--timeout",
            str(max(1, timeout - 5)),
        ]
        cmd.extend(f"--test={test_name}" for test_name in test_names)
        run_cwd = _resolve_run_cwd(work_dir)
        if run_cwd:
            cmd += ["--run-cwd", run_cwd]
        return cmd

    def _verilator_sim_cmd(
        self,
        *,
        rel: str,
        target: str,
        work_dir: Path,
        tb_top: str,
        timeout: int,
        test_name: str | None,
    ) -> list[str]:
        """Build the pinned ``V<top>`` invocation for one mutant (classic path).

        Paths stay relative to *work_dir* (the run cwd); verilator_run resolves
        them absolute before switching to ``--run-cwd``. The binary self-times-out
        a hair before the hard subprocess kill so a clean TIMEOUT marker lands in
        the captured output for the sweep's hang detection.
        """
        cmd = [
            sys.executable,
            "-m",
            "booley.sim.verilator_run",
            "--bin-dir",
            rel,
            "--top",
            tb_top,
            "--timeout",
            str(max(1, timeout - 5)),
        ]
        tests = project_config.lookup_target_section(project_config.TEST_NAMES, target) or []
        if test_name is not None:
            selector = project_config.render_test_selector(
                target,
                tests.index(test_name),
                test_name,
            )
            cmd.append(f"--plusarg={selector.removeprefix('+')}")
        pass_sentinels, fail_sentinels = _resolve_sim_sentinels(work_dir)
        cmd.extend(f"--pass-sentinel={sentinel}" for sentinel in pass_sentinels)
        cmd.extend(f"--fail-sentinel={sentinel}" for sentinel in fail_sentinels)
        run_cwd = _resolve_run_cwd(work_dir)
        if run_cwd:
            cmd += ["--run-cwd", run_cwd]
        return cmd

    def _icarus_sim_cmd(
        self,
        *,
        rel: str,
        target: str,
        work_dir: Path,
        timeout: int,
        test_name: str | None,
    ) -> list[str]:
        """Build the pinned ``vvp`` invocation for one classic Icarus mutant."""
        cmd = [
            sys.executable,
            "-m",
            _SIM_RUN_HALVES["icarus"],
            "--build-dir",
            rel,
            "--timeout",
            str(max(1, timeout - 5)),
        ]
        tests = project_config.lookup_target_section(project_config.TEST_NAMES, target) or []
        if test_name is not None:
            selector = project_config.render_test_selector(
                target,
                tests.index(test_name),
                test_name,
            )
            cmd.append(f"--plusarg={selector.removeprefix('+')}")
        pass_sentinels, fail_sentinels = _resolve_sim_sentinels(work_dir)
        cmd.extend(f"--pass-sentinel={sentinel}" for sentinel in pass_sentinels)
        cmd.extend(f"--fail-sentinel={sentinel}" for sentinel in fail_sentinels)
        run_cwd = _resolve_run_cwd(work_dir)
        if run_cwd:
            cmd += ["--run-cwd", run_cwd]
        return cmd

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

        Which run-half drives it depends on the Target: a Cocotb Target goes
        through :mod:`booley.sim.cocotb_run`
        (it needs cocotb's VPI environment); classic Targets use the run-half
        matching the resolved toolchain (``iverilog_run`` for Icarus,
        ``verilator_run`` for Verilator). Both exit non-zero on a FAIL verdict,
        which the sweep reads as "mutation detected". Returns the run
        :class:`~subprocess.CompletedProcess`.
        """
        rel = self._bin_dir_rel(target, work_dir, build_path)
        cocotb = self.cocotb_target(target, work_dir)
        if cocotb is not None:
            cmd = self._cocotb_sim_cmd(
                cocotb=cocotb,
                rel=rel,
                target=target,
                work_dir=work_dir,
                timeout=timeout,
                test_names=cocotb_tests,
            )
        else:
            eda_tool = self.target_eda_tool(target, work_dir, build_path)
            if eda_tool == "icarus":
                cmd = self._icarus_sim_cmd(
                    rel=rel,
                    target=target,
                    work_dir=work_dir,
                    timeout=timeout,
                    test_name=test_name,
                )
            elif eda_tool == "verilator":
                cmd = self._verilator_sim_cmd(
                    rel=rel,
                    target=target,
                    work_dir=work_dir,
                    tb_top=tb_top,
                    timeout=timeout,
                    test_name=test_name,
                )
            else:
                raise UnsupportedSimTargetError(
                    f"mutation_tester: cached Target {target!r} resolves to unsupported "
                    f"EDA toolchain {eda_tool!r}"
                )
        return subprocess.run(
            cmd,
            cwd=work_dir,
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
        timeout: int = 300,
    ) -> list[MutationTestRun]:
        """Run one built source variant against the Target's complete test suite."""
        campaign = self._campaign_for_target(target)
        batched = self.cocotb_target(target, work_dir) is not None

        def _run_unit(unit: CampaignUnit) -> MutationTestRun:
            try:
                proc = self._run_sim_pinned(
                    target,
                    work_dir,
                    build_path,
                    tb_top,
                    timeout=timeout,
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
            except (OSError, subprocess.SubprocessError) as exc:
                return MutationTestRun(
                    test_name=unit.display_name,
                    error=str(exc),
                    requires_cocotb_results=batched,
                )

        return list(campaign.execute(_run_unit, batched=batched).values)

    @staticmethod
    def _suite_output(runs: list[MutationTestRun]) -> str:
        """Render per-test output without losing which test produced it."""
        return "\n".join(f"===== {run.test_name} =====\n{run.output or run.error}" for run in runs)

    @staticmethod
    def _suite_inconclusive_reason(runs: list[MutationTestRun]) -> str:
        """Reject outcomes that cannot establish pass or fail for every test."""
        for run in runs:
            verdict = MutationTesterSpecialist._classify_mutation_run(
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
        early = MutationTesterSpecialist._preclassify_mutation_run(
            run,
            timeout_is_inconclusive=timeout_is_inconclusive,
        )
        if early is not None:
            return early
        parsed = parse_results_line(run.output)
        if parsed is not None:
            return MutationTesterSpecialist._classify_cocotb_run(run, parsed)
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
        if run.timed_out:
            if timeout_is_inconclusive:
                return VariantSuiteVerdict(inconclusive_reason=prefix + "simulation timed out")
            return VariantSuiteVerdict(detected=True, first_killing_test=run.test_name)
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
            MutationTesterSpecialist._classify_mutation_run(
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
        return next((verdict for verdict in verdicts if verdict.detected), VariantSuiteVerdict())

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
            lock_mod.mutant_logs_dir() / f"mutant_{index}.log",
            output,
            f"mutation {index}",
        )

    def _persist_baseline_log(self, output: str) -> str:
        """Persist this invocation's pristine-baseline output."""
        return self._persist_capped_log(lock_mod.baseline_log_path(), output, "baseline")

    def _persist_capped_log(self, path: Path, output: str, label: str) -> str:
        """Best-effort capped transcript persistence shared by all variants."""
        from booley.sim.sim_result import _cap_log_bytes

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                _cap_log_bytes(
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
        generation's ``mutant_7.log … mutant_25.log`` in place, physically
        indistinguishable from this run's — and the ``mutant_logs`` artifact
        key advertises the whole directory, which would make it a "present but
        wrong" pointer under a contract that says pointers are never wrong.
        The lock dir survives across runs by design (that is the point of the
        lock), so this cannot ride on ``wipe_lock``.

        Best-effort, like every other log operation here.
        """
        with contextlib.suppress(OSError):
            shutil.rmtree(lock_mod.mutant_logs_dir(), ignore_errors=True)

    def _run_variant_sweep(
        self,
        plan: MutationRunPlan,
        specs: list[MutationSpec],
        variants: MutationVariantPlan,
    ) -> tuple[list[MutationResult], float, str]:
        """Build and run every exact replacement in complete isolation."""
        start = time.monotonic()
        self._reset_mutant_logs()
        results: list[MutationResult] = []
        inconclusive: list[str] = []
        for spec in specs:
            self.emit_progress(f"build and test mutation {spec.index}/{len(specs)}")
            result, reason = self._run_one_variant(plan, spec, variants)
            results.append(result)
            if reason:
                inconclusive.append(f"mutation #{spec.index}: {reason}")
        return results, time.monotonic() - start, "; ".join(inconclusive)

    def _run_one_variant(
        self,
        plan: MutationRunPlan,
        spec: MutationSpec,
        variants: MutationVariantPlan,
    ) -> tuple[MutationResult, str]:
        build_path = lock_mod.variant_build_dir(spec.index)
        shutil.rmtree(build_path, ignore_errors=True)
        build_path.mkdir(parents=True, exist_ok=True)
        with variants.applied(spec.index):
            elab = self._run_elab(plan.target, plan.work_dir, build_path)
            elab_output = (elab.stdout or "") + (elab.stderr or "")
            if elab.returncode != 0:
                return (
                    MutationResult(
                        index=spec.index,
                        invalid=True,
                        sim_output_snippet="variant did not elaborate: "
                        + _tail(elab_output, 5)[-200:],
                        log_path=self._persist_mutant_log(spec.index, elab_output),
                    ),
                    "",
                )
            runs = self._run_target_test_suite(
                plan.target,
                plan.work_dir,
                build_path,
                self.args.tb_top,
            )
        combined = self._suite_output(runs)
        verdict = self._classify_variant_suite(runs)
        snippet = (
            f"sim outcome inconclusive: {verdict.inconclusive_reason}"
            if verdict.inconclusive_reason
            else _tail(combined, 5)[-200:]
        )
        return (
            MutationResult(
                index=spec.index,
                invalid=bool(verdict.inconclusive_reason),
                detected=verdict.detected,
                sim_output_snippet=snippet,
                log_path=self._persist_mutant_log(spec.index, combined),
                first_killing_test=verdict.first_killing_test,
            ),
            verdict.inconclusive_reason,
        )

    def _verify_clean_worktree(self, owned_files: set[str] | None = None) -> bool:
        """True if the sweep left no residue in the files it is responsible for.

        Scoped to the DUT/scope files the sweep actually rewrites (QA_REPORT
        C2.2): a bare ``git diff`` flagged ANY pre-existing, unrelated edit in
        the worktree, producing a false "worktree not clean" warning that
        pointed the user at the wrong file. With no owned files known (no
        specs), fall back to the whole-tree check.
        """
        cmd = ["git", "diff", "--name-only"]
        files = sorted(f for f in (owned_files or set()) if f)
        if files:
            cmd += ["--", *files]
        try:
            result = subprocess.run(
                cmd,
                cwd=self.args.work_dir,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            # A warning is a claim that residue exists, not that the check was
            # unavailable. Only a successful diff naming files proves it.
            return result.returncode != 0 or not result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return True

    def _add_residue_warning(self, result: McpToolResult, owned_files: set[str]) -> None:
        """Add the cold-run cleanup warning only after rollback has completed."""
        if self._verify_clean_worktree(owned_files):
            return
        warning = "  WARNING: worktree not clean after sim sweep"
        if warning in result.report_text:
            return
        lines = result.report_text.splitlines()
        result_index = next(
            (index for index, line in enumerate(lines) if line.startswith("RESULT: ")),
            len(lines),
        )
        lines.insert(result_index, warning)
        result.report_text = "\n".join(lines)

    # ------------------------------------------------------------------
    # Git-based rollback net + scope boundary enforcement (QA-11)
    #
    # The creator has no write capability, but this rollback net is a defense
    # against a misconfigured agent transport: any tracked file the run dirtied
    # beyond pre-existing WIP is restored to its index state via git.
    # ------------------------------------------------------------------

    def _run_git(self, args: list[str], work_dir: Path) -> subprocess.CompletedProcess | None:
        """Run a git command in *work_dir*; ``None`` when git is unavailable."""
        try:
            return subprocess.run(
                ["git", *args],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

    def _git_modified_tracked(self, work_dir: Path) -> set[str] | None:
        """Worktree-relative paths of tracked files git reports as modified.

        Excludes untracked files (``--untracked-files=no``) so we only ever act
        on files git can restore. Returns ``None`` when git is unavailable or
        *work_dir* is not a repo — callers then rely on the content snapshot
        alone.
        """
        proc = self._run_git(
            ["status", "--porcelain", "--untracked-files=no"],
            work_dir,
        )
        if proc is None or proc.returncode != 0:
            return None
        modified: set[str] = set()
        for line in proc.stdout.splitlines():
            if len(line) < 4:
                continue
            path = line[3:]
            # Renames/copies render as "old -> new"; the new path is what exists.
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            modified.add(path.strip().strip('"'))
        return modified

    def _revert_stray_tracked_edits(
        self,
        work_dir: Path,
        pre_dirty: set[str] | None,
        keep: list[str],
    ) -> list[str]:
        """Restore tracked files this run dirtied outside *keep* to pre-run state.

        ``pre_dirty`` is the set of tracked files already modified *before* the
        run (captured by :meth:`_git_modified_tracked`); those are pre-existing
        WIP and are never touched. ``keep`` names files the caller restores by
        other means (the content snapshot) and so should be left alone here.
        Everything else that became modified during the run is ``git checkout``-ed
        back to the index. Returns the reverted paths.

        No-op (``[]``) when git is unavailable — the content snapshot remains
        the guard for the enumerated candidate set.
        """
        if pre_dirty is None:
            return []
        now_modified = self._git_modified_tracked(work_dir)
        if now_modified is None:
            return []
        stray = sorted(now_modified - pre_dirty - set(keep))
        if not stray:
            return []
        proc = self._run_git(["checkout", "--", *stray], work_dir)
        if proc is None or proc.returncode != 0:
            logger.error(
                "mutation_tester: failed to git-revert stray edits %s (%s)",
                stray,
                proc.stderr.strip() if proc else "git unavailable",
            )
            return []
        return stray

    # ------------------------------------------------------------------
    # Result assembly
    # ------------------------------------------------------------------

    def _build_run_result(
        self,
        plan: MutationRunPlan,
        inputs: RunResultInputs,
    ) -> McpToolResult:
        # Unpack the plan config + run outcome the assembly below reads.
        min_detected, target = plan.min_detected, plan.target
        auto_mode, formula_count, source_size_budget = (
            plan.auto_mode,
            plan.formula_count,
            plan.source_size_budget,
        )
        summary, count = inputs.summary, inputs.count
        reused_lock, lock_created_at = inputs.reused_lock, inputs.lock_created_at
        verification_rounds, build_cached = inputs.verification_rounds, inputs.build_cached

        valid_count = summary.detected_count + summary.not_detected_count
        passed = summary.detected_count >= min_detected

        detail = _mutation_detail(summary, min_detected, count, valid_count)
        detail.update(
            {
                "target": target,
                "tests": list(self._campaign_for_target(target).suite.display_names),
                "reused_lock": reused_lock,
                "lock_created_at": lock_created_at,
                "verification_rounds": verification_rounds,
                "build_cached": build_cached,
            }
        )
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

        return McpToolResult(
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
            variant_root = lock_mod.variants_dir()
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
        if plan.source_size_budget and not inputs.reused_lock:
            (staging_dir / "source-size-budget.json").write_text(
                json.dumps(plan.source_size_budget, indent=2), encoding="utf-8"
            )
        baseline_log = self._copy_campaign_file(
            lock_mod.baseline_log_path(), staging_dir / "baseline.log"
        )
        mutant_logs = self._copy_campaign_dir(
            lock_mod.mutant_logs_dir(), staging_dir / "mutant-logs"
        )
        self._retarget_log_paths(inputs.summary, mutant_logs, campaign_dir, plan.work_dir)
        links = {index: posix_relpath(path, staging_dir) for index, path in variants.items()}
        (staging_dir / "mutation-results.md").write_text(
            generate_results_markdown(inputs.summary, plan.min_detected, variant_paths=links),
            encoding="utf-8",
        )
        verification = self._copy_verification_rounds(
            inputs.verification_rounds, staging_dir / "verification-rounds"
        )
        return {
            "baseline_log": baseline_log,
            "mutant_log_dir": mutant_logs,
            "verification_dir": verification,
        }

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

    @staticmethod
    def _copy_verification_rounds(count: int, destination: Path) -> Path | None:
        source = lock_mod.verification_rounds_dir()
        copied = False
        for round_idx in range(1, count + 1):
            round_log = source / f"round_{round_idx}.log"
            if not round_log.is_file():
                continue
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(round_log, destination / round_log.name)
            copied = True
        return destination if copied else None

    def _campaign_manifest(
        self,
        plan: MutationRunPlan,
        inputs: RunResultInputs,
        *,
        baseline_log: Path | None,
        mutant_log_dir: Path | None,
        verification_dir: Path | None,
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
                plan.target, plan.work_dir, lock_mod.baseline_build_dir()
            ),
            "source_fingerprint": inputs.variants.source_fingerprint(),
            "scope_hashes": plan.scope_hashes,
            "baseline": {
                "status": "passed",
                "log": baseline_log.name if baseline_log is not None else None,
            },
            "mutants": mutants,
            "artifacts": {
                "specs": "mutation-specs.md",
                "results": "mutation-results.md",
                "variants": "variants",
                "verification_rounds": (
                    verification_dir.name if verification_dir is not None else None
                ),
            },
        }

    def _attach_campaign_directories(
        self,
        detail: dict[str, Any],
        artifact_dir: Path | None,
    ) -> None:
        variants_dir = artifact_dir / "variants" if artifact_dir else lock_mod.variants_dir()
        mutant_logs = artifact_dir / "mutant-logs" if artifact_dir else lock_mod.mutant_logs_dir()
        verification_rounds = (
            artifact_dir / "verification-rounds"
            if artifact_dir
            else lock_mod.verification_rounds_dir()
        )
        _artifacts.merge_artifacts(
            detail,
            _artifacts.artifacts_block(
                self.args.work_dir,
                dirs={
                    "variants": variants_dir,
                    "mutant_logs": mutant_logs,
                    "verification_rounds": verification_rounds,
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
        if inputs.reused_lock:
            suffix = (
                f" [reused lock from {inputs.lock_created_at}]"
                if inputs.build_cached
                else f" [reused lock + rebuilt from {inputs.lock_created_at}]"
            )
        else:
            suffix = " [cold: agent designed]"
        if plan.auto_mode and inputs.count < plan.formula_count:
            suffix += f" [auto: {plan.formula_count} requested, {inputs.count} budget-capped]"
        return base + suffix

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
        lines: list[str] = []
        if inputs.reused_lock:
            cache_tag = "build cached" if inputs.build_cached else "rebuilt"
            lines.append(f"[mutation_test] warm reuse ({cache_tag})")
        else:
            lines.append(
                f"[mutation_test] cold start: creator {inputs.creator_elapsed / 60:.0f}m, "
                f"{inputs.verification_rounds} verification round(s)",
            )
        lines.append(
            f"[mutation_test] sim sweep: {len(summary.results)}/{len(summary.specs)} "
            f"tested              {inputs.tester_elapsed / 60:.0f}m",
        )
        lines.append(
            f"  {summary.detected_count} detected, "
            f"{summary.invalid_count} invalid, "
            f"{summary.not_detected_count} coverage gap",
        )
        lines.append(
            f"  detected: {summary.detected_count}/{valid_count} "
            f"(required: {min_detected}/{valid_count}, set by --min-detected)",
        )
        if inputs.coverage_gap:
            # Name the diagnosis instead of leaving a bare 0/N to be misread as
            # a broken mutation harness (SETUP-F-38).
            lines.extend(
                [
                    "  DIAGNOSIS: this scope is not covered by the Target's tests.",
                    f"    Evidence: {_describe_evidence(inputs.evidence)} — every "
                    "mutation was applied and reached the design, and the tests "
                    "still passed.",
                    "    Nothing here is a mutation-harness or creator defect. "
                    "Either extend the Target's stimulus to exercise the scope, "
                    "run --scope against a Target whose tests drive it, or lower "
                    "--min-detected if a partial score is acceptable.",
                ],
            )
        # Name every surviving mutant (file:line + the code change). Reporting a
        # bare "N coverage gap" left the one datum the run exists to produce
        # unrecoverable (QA_REPORT C2.4).
        for spec in summary.surviving_specs():
            orig = " ".join(spec.original_code.split())
            mut = " ".join(spec.mutated_code.split())
            lines.append(
                f"  SURVIVED [{spec.category}] {spec.file}:{spec.line}: {orig!r} -> {mut!r}"
            )
        # Scope the residue check to the files the sweep actually mutates, so an
        # unrelated pre-existing edit elsewhere in the tree is not misattributed
        # to the sweep (QA_REPORT C2.2).
        swept_files = {spec.file for spec in summary.specs if spec.file}
        if inputs.reused_lock and not self._verify_clean_worktree(swept_files):
            lines.append("  WARNING: worktree not clean after sim sweep")
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


def _failure_detail(
    *,
    phase: str,
    reason: str,
    specs: list[MutationSpec],
    work_dir: Path,
    summary: MutationSummary | None = None,
    min_detected: int = 0,
    count: int = 0,
    verification_rounds: int = 0,
    log_tail: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Structured detail for a run that never reached a verdict.

    A failed run used to write ``mutation_tester.json`` with ``detail: {}``,
    stranding the tally and the mutation list in the transcripts where no
    downstream reader looks (SETUP-F-39).  Everything the run did learn goes
    here, whatever the exit code.
    """
    detail: dict[str, Any] = {
        "failed": True,
        "phase": phase,
        "reason": reason,
        "verification_rounds": verification_rounds,
        "mutations": [s.to_dict() for s in specs],
    }
    if summary is not None:
        valid_count = summary.detected_count + summary.not_detected_count
        detail.update(_mutation_detail(summary, min_detected, count, valid_count))
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
                "variants": lock_mod.variants_dir(),
                "mutant_logs": lock_mod.mutant_logs_dir(),
                "verification_rounds": lock_mod.verification_rounds_dir(),
            },
        ),
    )
    return detail


def _mutation_detail(
    summary: MutationSummary,
    min_detected: int,
    count: int,
    valid_count: int,
) -> dict[str, Any]:
    return {
        "detected": summary.detected_count,
        "not_detected": summary.not_detected_count,
        "invalid": summary.invalid_count,
        "min_detected": min_detected,
        "total_requested": count,
        "total_valid": valid_count,
    }


def _make_transcript_path(transcript_dir: Path | None, label: str) -> Path | None:
    if transcript_dir is None:
        return None
    transcript_dir.mkdir(parents=True, exist_ok=True)
    return transcript_dir / f"{label}.jsonl"


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


def _tail(text: str, max_lines: int) -> str:
    """Return the last *max_lines* of *text* as a single string."""
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


if __name__ == "__main__":
    MutationTesterSpecialist().cli()
