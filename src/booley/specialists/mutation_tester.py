"""MutationTesterSpecialist — lock-based mutation-testing Specialist.

Cold start (no valid lock):
  1. Creator agent designs N mutations, writes muxed RTL in scope files
     in-place, returns a JSON spec list.
  2. Harness builds the design once and verifies the muxes by running
     MUT_ID=0 (baseline) + one pinned non-zero MUT_ID. Both must pass.
  3. On verification failure: resume the creator session with the failure
     log and ask it to fix the muxed files.  Up to 3 rounds total.  A sweep
     that kills nothing is *not* a creator failure when every mux is in the
     source and the design echoed its +MUT_ID: that terminates immediately
     as a testbench coverage gap (SETUP-F-38).
  4. On success: copy the muxed scope files into the lock dir alongside
     lock.json and build_meta.json; revert the worktree.

Warm reuse (lock matches current scope by content hash):
  1. Swap locked muxed files into the worktree (remember to revert).
  2. Validate the build cache (docker_digest + muxed-file hashes); rebuild
     if stale.
  3. Sim MUT_ID=0 baseline sanity; abort with diagnostic on failure.
  4. Deterministic loop: sim MUT_ID=k for k in 1..N via --sim-only.
  5. Revert worktree.

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
from booley.core.boundary import as_int
from booley.core.models import AgentCallParams
from booley.dev_support import mutation_lock as lock_mod
from booley.dev_support.workspace_isolation import hide_opposite_sources
from booley.flows import artifacts as _artifacts
from booley.flows import edam as edam_layer
from booley.flows.sim import edam as sim_edam
from booley.flows.sim.flow import _SIM_RUN_HALVES, _resolve_run_cwd, _resolve_sim_sentinels
from booley.flows.target_campaign import (
    CampaignScopeError,
    CampaignUnit,
    NoRunnableTestsError,
    TargetCampaign,
    TargetTestSuite,
    describe_target_campaign,
    require_runnable_target_test_suite,
    resolve_target_campaign,
)
from booley.fusesoc import fusesoc_registry
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS, McpToolResult
from booley.runtime.paths import refs_dir
from booley.runtime.platform_paths import posix_relpath
from booley.sim.sim_result import SIM_INFRA_ERROR_PREFIX, has_infra_error

from .specialist import Specialist

logger = logging.getLogger(__name__)
_BUILD_INPUT_SUFFIXES = (".sv", ".v", ".svh", ".vh")

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
    if not has_infra_error(combined):
        return ""
    for line in combined.splitlines():
        if SIM_INFRA_ERROR_PREFIX in line:
            return line.split(SIM_INFRA_ERROR_PREFIX, 1)[1].strip()
    return "simulation harness failure"  # pragma: no cover - marker without a line


def _mut_guard_regex(mut_id: int) -> re.Pattern[str]:
    """Match the mux guard for mutation *mut_id* in muxed RTL.

    Accepts every form the mutation guide sanctions — ``mut_id == 3``,
    ``booley_mut_pkg::mut_id === 3``, ``mut_id == 32'd3`` — because its job is
    presence detection, not parsing.  Finding one guard per spec is the static
    half of the "the mutations really were applied" evidence (SETUP-F-38).
    """
    return re.compile(
        rf"mut_id\s*={{2,3}}\s*(?:[0-9]+\s*'\s*[sS]?[dDhHbBoO]\s*)?0*{mut_id}\b",
    )


# Marker files (under the build dir) record the edalize binary dir relative to
# the project root and its resolved EDA-tool family. _run_elab writes them; the
# per-mutant run-many loop reads them so it never has to re-resolve the Target.
_EDALIZE_BINDIR_MARKER = ".booley_edalize_bindir"
_EDALIZE_EDA_TOOL_MARKER = ".booley_edalize_eda_tool"


# Agent capability set — Edit/Read/Grep/Glob/Bash.  No simulation; the harness
# drives verification deterministically after the agent finishes.
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


_CREATOR_TOOLS = ["Edit", "Read", "Grep", "Glob", "Bash"]

_CREATOR_JSON_EXAMPLE = """\
```json
{
  "mutations": [
    {
      "index": 1,
      "mut_id": 1,
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


# Categories that are unrunnable under runtime mux selection.  The plan
# (Phase 3.4) requires hard rejection at spec validation — a forbidden
# category counts as a failed verification round.
_FORBIDDEN_CATEGORIES = frozenset(
    {
        "instance_swap",
        "instantiation_swap",
        "module_instantiation_swap",
        "module_swap",
        "port_width",
        "port_declaration",
        "declaration_change",
        "sensitivity_list",
        "trigger_reorder",
        "code_removal",
        "delete_always",
        "delete_assign",
        "clock_polarity",
        "reset_polarity",
    }
)


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
    mut_id: int = 0  # Defaults to index when missing — agent may omit.

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "mut_id": self.mut_id or self.index,
            "category": self.category,
            "file": self.file,
            "line": self.line,
            "original_code": self.original_code,
            "mutated_code": self.mutated_code,
            "detectability_argument": self.detectability_argument,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MutationSpec:
        return cls(
            index=d.get("index", 0),
            category=d.get("category", "unknown"),
            file=d.get("file", ""),
            line=d.get("line", 0),
            original_code=d.get("original_code", ""),
            mutated_code=d.get("mutated_code", ""),
            detectability_argument=d.get("detectability_argument", ""),
            mut_id=d.get("mut_id", d.get("index", 0)),
        )


@dataclass
class MutationResult:
    index: int
    detected: bool = False
    invalid: bool = False
    sim_output_snippet: str = ""
    #: The injected reader echoed this mutant's ``+MUT_ID`` during the run —
    #: runtime proof the selector reached the design (SETUP-F-38).
    selector_observed: bool = False
    #: Project-relative path of this mutant's full simulator output. The
    #: snippet above is 200 chars; for a mutant the testbench failed to kill
    #: there is no error text at all, so the log is the only place the run's
    #: behaviour can be inspected. Empty when the log could not be written.
    log_path: str = ""


@dataclass
class MutationTestRun:
    """Outcome of one test within a Target-wide mutation campaign."""

    test_name: str
    process: subprocess.CompletedProcess[str] | None = None
    timed_out: bool = False
    error: str = ""
    output: str = ""


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
                }
            )
        return classified


# ---------------------------------------------------------------------------
# RTL complexity scoring (unchanged from previous version)
# ---------------------------------------------------------------------------

_SV_KEYWORDS = frozenset(
    {
        "module",
        "endmodule",
        "input",
        "output",
        "inout",
        "wire",
        "reg",
        "logic",
        "assign",
        "always",
        "always_ff",
        "always_comb",
        "always_latch",
        "initial",
        "if",
        "else",
        "for",
        "while",
        "begin",
        "end",
        "case",
        "casez",
        "casex",
        "endcase",
        "function",
        "endfunction",
        "task",
        "endtask",
        "generate",
        "endgenerate",
        "parameter",
        "localparam",
        "integer",
        "genvar",
        "return",
        "typedef",
        "enum",
        "struct",
        "union",
        "packed",
        "signed",
        "unsigned",
        "import",
        "export",
        "interface",
        "endinterface",
        "class",
        "endclass",
    }
)


def _strip_sv_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def _extract_complexity_features(
    scope_files: list[str],
    work_dir: Path,
) -> dict[str, int]:
    """Count structural RTL constructs across *scope_files* (comments stripped).

    The ``ternary`` tally is corrected for ``casez``/``casex`` items, whose
    ``?`` wildcards would otherwise inflate the ternary count.
    """
    features: dict[str, int] = {
        "always_blocks": 0,
        "if_branches": 0,
        "case_statements": 0,
        "ternary": 0,
        "generate_blocks": 0,
        "instantiations": 0,
        "arithmetic_ops": 0,
        "comparison_ops": 0,
        "bitwise_ops": 0,
        "bit_selects": 0,
    }

    for fpath in scope_files:
        p = Path(fpath) if Path(fpath).is_absolute() else work_dir / fpath
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.warning("compute_rtl_complexity: cannot read %s", p)
            continue
        text = _strip_sv_comments(raw)

        features["always_blocks"] += len(re.findall(r"\balways(_ff|_comb|_latch)?\b", text))
        features["if_branches"] += len(re.findall(r"\b(?:if|else\s+if)\b", text))
        features["case_statements"] += len(re.findall(r"\bcase[zx]?\b", text))
        features["ternary"] += len(re.findall(r"\?", text))
        features["generate_blocks"] += len(re.findall(r"\b(?:genvar|generate)\b", text))

        for m in re.finditer(r"^\s*(\w+)\s+(\w+)\s*\(", text, re.MULTILINE):
            if m.group(1) not in _SV_KEYWORDS:
                features["instantiations"] += 1

        features["arithmetic_ops"] += len(re.findall(r"[+\-*/%]", text))
        features["comparison_ops"] += len(re.findall(r"[=!<>]=|(?<![<>])[<>](?![<>=])", text))
        features["bitwise_ops"] += len(
            re.findall(r"(?<![&])[&](?![&])|(?<![|])[|](?![|])|[^~]?[~^]", text)
        )
        features["bit_selects"] += len(
            re.findall(r"\[\w+\s*:\s*\w+\]|\[\w+\s*[+\-]\s*:\s*\w+\]", text)
        )

    casezx = (
        len(
            re.findall(
                r"\bcase[zx]\b",
                " ".join(
                    _strip_sv_comments(
                        Path(f if Path(f).is_absolute() else str(work_dir / f)).read_text(
                            encoding="utf-8", errors="replace"
                        )
                    )
                    for f in scope_files
                    if (Path(f) if Path(f).is_absolute() else work_dir / Path(f)).exists()
                ),
            )
        )
        if scope_files
        else 0
    )
    features["ternary"] = max(0, features["ternary"] - casezx * 5)
    return features


def _score_complexity(features: dict[str, int]) -> tuple[float, float, float]:
    """Combine feature counts into (linear_score, log_score, raw_score)."""
    linear_score = (
        features["always_blocks"] * 3
        + features["if_branches"] * 1
        + features["case_statements"] * 4
        + features["ternary"] * 1
        + features["generate_blocks"] * 2
        + features["instantiations"] * 2
    )
    log_score = (
        0.5 * math.log2(features["arithmetic_ops"] + 1)
        + 0.5 * math.log2(features["comparison_ops"] + 1)
        + 0.3 * math.log2(features["bitwise_ops"] + 1)
        + 0.3 * math.log2(features["bit_selects"] + 1)
    )
    return linear_score, log_score, linear_score + log_score


def compute_rtl_complexity(scope_files: list[str], work_dir: Path) -> dict:
    """Score RTL structural complexity to auto-scale mutation count."""
    K = 2.5
    MIN_COUNT = 3
    MAX_COUNT = 25

    features = _extract_complexity_features(scope_files, work_dir)
    linear_score, log_score, raw_score = _score_complexity(features)
    formula_count = max(MIN_COUNT, min(MAX_COUNT, round(raw_score / K)))

    return {
        "features": features,
        "linear_score": round(linear_score, 1),
        "log_score": round(log_score, 1),
        "raw_score": round(raw_score, 1),
        "formula_count": formula_count,
        "K": K,
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
            except (KeyError, TypeError):
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


def find_forbidden_specs(specs: list[MutationSpec]) -> list[MutationSpec]:
    """Return the subset of *specs* whose category is in the forbidden set.

    Spec validation runs before sim — a forbidden category is treated as a
    catastrophic verification failure (Phase 3.4).
    """

    def _normalize(cat: str) -> str:
        return cat.strip().lower().replace("-", "_").replace(" ", "_")

    return [s for s in specs if _normalize(s.category) in _FORBIDDEN_CATEGORIES]


# ---------------------------------------------------------------------------
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
    mutated_rtl_paths: dict[str, str],
) -> str:
    label = f"{spec.file}:{spec.line}"
    path = mutated_rtl_paths.get(spec.file)
    return f"[{label}]({quote(path, safe='/:._-')})" if path else label


def _markdown_table_cell(value: object) -> str:
    return " ".join(str(value).split()).replace("|", r"\|")


def generate_results_markdown(
    summary: MutationSummary,
    min_detected: int,
    *,
    mutated_rtl_paths: dict[str, str] | None = None,
) -> str:
    mutated_rtl_paths = mutated_rtl_paths or {}
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
                f"- **{_mutation_source_link(s, mutated_rtl_paths)}** ({s.category}): "
                f"`{s.original_code}` -> `{s.mutated_code}`{where}"
            )
        lines.append("")
    lines += [
        "| # | Mutation | Mutated RTL | Status | Snippet |",
        "|---|----------|-------------|--------|---------|",
    ]
    for c in classified:
        spec = specs_by_index[c["index"]]
        lines.append(
            f"| {c['index']} | {_markdown_table_cell(c['category'])}: "
            f"`{_markdown_table_cell(spec.original_code)}` → "
            f"`{_markdown_table_cell(spec.mutated_code)}` | "
            f"{_mutation_source_link(spec, mutated_rtl_paths)} | "
            f"{c['status']} | "
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
    #: binary, missing simulator) rather than because the creator's muxed RTL
    #: misbehaved. Retrying the creator cannot fix this, so the caller aborts
    #: instead of burning further rounds re-prompting it (SETUP-F-41a).
    infra_error: str = ""


@dataclass
class WorktreeFileSnapshot:
    """Pre-mutation contents for a file the tester may overwrite."""

    rel_path: str
    path: Path
    existed: bool
    content: bytes | None = None


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
    complexity: dict | None


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
    #: Nothing was killed, yet every mutation is provably live — the Target's
    #: tests simply never exercise this scope (SETUP-F-38).
    coverage_gap: bool = False
    #: What ``_mutation_evidence`` observed; reported so the verdict is auditable.
    evidence: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Specialist implementation
# ---------------------------------------------------------------------------


class MutationTesterSpecialist(Specialist):
    """Lock-based mutation testing."""

    name: str = "mutation_tester"
    description: str = (
        "Lock-based mutation testing: creator designs muxed RTL once, "
        "tester runs deterministic sim loop"
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
            help=(
                "Optional mutation-injection module override. By default the "
                "specialist derives the scoped module that contains the "
                "mutations; pass this only when the scope is ambiguous."
            ),
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
            help="Compute complexity score and print breakdown without running mutations",
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
        """Build the initial cold-start prompt for the creator agent.

        Reflects the new lock-based flow:
          - harness has already injected booley_mut_pkg + plusarg reader,
          - agent writes muxes in-place and never reverts,
          - harness verifies after the agent returns JSON.
        """
        scope_files = self._scope_files()
        scope_str = ", ".join(scope_files)
        count = self.args.count
        steer_section = ""
        if self.args.steer:
            steer_section = f"\n## Developer Agent Context\n\n{self.args.steer}\n"

        dut_top_module = self._dut_top_module() or "<dut_top>"
        classic_verilog = self._dut_top_path().suffix.lower() == ".v"
        selector = "mut_id" if classic_verilog else "booley_mut_pkg::mut_id"
        if classic_verilog:
            infrastructure = (
                f"The harness inserted a Verilog-2001 `integer mut_id` plusarg reader "
                f"inside `{dut_top_module}`. Only mutate that module; classic Verilog "
                "has no package namespace for sharing the selector with submodules."
            )
        else:
            infrastructure = (
                "The harness inserted `package booley_mut_pkg`, its import, and an "
                "initial `+MUT_ID=<k>` reader. Add `import booley_mut_pkg::*;` to "
                "other scope modules that use `mut_id`."
            )
        boundary_section = self._mutation_boundary_section()
        task_section = self._creator_task_section(count, dut_top_module, selector)
        tb_dirs = ", ".join(_configured_testbench_dirs(self.args.work_dir))

        return f"""You are a mutation testing CREATOR agent.
You MUST NOT read or edit any testbench files ({tb_dirs} or any configured
testbench source/include directory). This is an RTL-only mutation task.

Read the mutation testing guide at `{_mutation_guide_path()}` before starting.

## RTL Files in Scope

{scope_str}
{boundary_section}
## Harness-Provided Infrastructure

{infrastructure}

You MUST NOT modify, remove, or duplicate the harness blocks.

{task_section}
{steer_section}
## Output Format (MANDATORY)

Return a JSON object with a "mutations" array in the form below; `mut_id`
equals `index`.

{_CREATOR_JSON_EXAMPLE}
"""

    @staticmethod
    def _creator_task_section(count: int, dut_top_module: str, selector: str) -> str:
        return f"""## Task

Design {count} single-point RTL mutations that a reasonable testbench
SHOULD detect, written as runtime-selected muxes directly into the scope
files (in-place — DO NOT revert).  Each mutation is gated by
`{selector} == k`, so the baseline (`mut_id == 0`) runs the
original logic unchanged.

Read each scope file thoroughly, following submodule instantiations into
their source, to map the datapath, control logic, and output ports.

For each mutation k = 1 .. {count}:
1. Pick a single mutation site per the categories in the guide.
2. Wrap the site in a mux template from the guide.  Place a marker
   `// MUTATION #k: <original_code> -> <mutated_code>` above the muxed line.
3. Distribute mutations across scope files — don't concentrate them all in
   `{dut_top_module}`.
4. After writing every mux, return the JSON spec list.

DO NOT run elaboration yourself, DO NOT call simulators, and DO NOT revert.
The harness builds the design once and verifies by running MUT_ID=0
(baseline) plus one pinned non-zero MUT_ID.

On a verification failure the harness resumes this session with the log to
fix the muxed files; then edit the source — do **not** return JSON again."""

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
        """Prompt for a creator session resume after a failed verification round.

        Forbidden-category failures need a new spec list; sim/elab failures
        only need source edits.  The two paths use different instructions
        so the agent doesn't waste tokens regenerating accepted specs.
        """
        if "forbidden" in outcome.reason.lower():
            return f"""Some of the mutation specs you returned use a forbidden category
that cannot run under runtime mux selection.

{outcome.reason}

{outcome.log_tail}

Replace those mutations with valid ones from the allowed templates in the
mutation testing guide (expression mutation, reset value, FSM next-state,
stuck-at, LHS swap, mux branch swap).  Update the muxed source files to
match and return a fresh JSON spec list in the same format as before.
"""
        return f"""Your muxed scope files failed verification.

- MUT_ID=0 baseline:  {"PASS" if outcome.baseline_passed else "FAIL"}
- Pinned MUT_ID:      {"PASS" if outcome.pinned_passed else "FAIL"}

Reason: {outcome.reason}

Sim log tail:

```
{outcome.log_tail}
```

Fix the muxed files in the scope so both MUT_ID=0 and the pinned MUT_ID
compile and complete cleanly.  Do **not** return JSON again — your spec
list has already been accepted; only edit the source files.
"""

    @staticmethod
    def _build_zero_detection_retry_prompt(
        summary: MutationSummary,
        min_detected: int,
        evidence: dict[str, Any] | None = None,
    ) -> str:
        """Prompt the creator after a sweep proves every mutation escaped.

        Only reached when the harness could *not* confirm the mutations were
        live — a confirmed-live zero-kill sweep is a testbench coverage gap and
        terminates instead of re-prompting (SETUP-F-38).
        """
        examples = "\n".join(
            f"- #{spec.index} ({spec.category}) {spec.file}:{spec.line}: "
            f"`{spec.original_code}` -> `{spec.mutated_code}`"
            for spec in summary.specs[:10]
        )
        valid_count = summary.detected_count + summary.not_detected_count
        return f"""The full mutation sweep ran all {valid_count} mutation(s), and the
testbench killed 0 of them.

- Killed:   0/{valid_count}
- Required: {min_detected}/{valid_count} (the --min-detected threshold)

The harness could not confirm the mutations were live
({_describe_evidence(evidence or {})}), which points at the runtime mutation
insertion: the nonzero MUT_ID branches are missing, equivalent to the
baseline, unreachable, checking the wrong mut_id signal, or otherwise
preserving original behavior.

Current mutation specs:
{examples}

Fix the muxed source files so every selected MUT_ID branch genuinely differs
from the original default branch and can corrupt an observable DUT output.
Do not edit the testbench, do not weaken the baseline branch, and do not make
nonzero branches pass by falling through to original behavior.

Return a fresh JSON mutation spec list matching the updated muxes.
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
    def _specs_outside_scope(
        specs: list[MutationSpec], scope_files: list[str], work_dir: Path
    ) -> list[MutationSpec]:
        """Return creator specs whose claimed edit path is not authorized."""
        allowed = {fusesoc_registry.canonical_project_path(work_dir, path) for path in scope_files}
        return [
            spec
            for spec in specs
            if fusesoc_registry.canonical_project_path(work_dir, spec.file) not in allowed
        ]

    @staticmethod
    def _canonicalize_spec_paths(specs: list[MutationSpec], work_dir: Path) -> None:
        """Normalize validated creator paths for downstream artifact lookups."""
        for spec in specs:
            spec.file = fusesoc_registry.canonical_project_path(work_dir, spec.file)

    def _validate_scope_against_target(self, scope_files: list[str]) -> McpToolResult | None:
        """Fail fast when a ``--scope`` entry isn't a source file of ``--target``.

        The creator agent faithfully writes muxed RTL into whatever path it is
        handed, so a plausible-but-wrong scope (classically the stealth-cores
        mirror ``.booley_project/cores/rtl/foo.sv`` instead of the repo-relative
        ``rtl/foo.sv``) is only discovered ~3 creator rounds later as a terse
        "elaboration of muxed RTL failed" — after ~12 min / ~$4.50 of agent
        time. Resolving the scope against the Target's fileset up front (a
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
            resolved = list(
                fusesoc_registry.target_source_files(
                    self.args.work_dir,
                    target,
                    include_dependencies=True,
                ).rtl_source_files
            )
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
        """Resolve the mutation-injection module from an override or RTL scope."""
        explicit = getattr(self.args, "dut_top", None)
        if explicit:
            return explicit
        modules_by_file: dict[str, set[str]] = {}
        source_text: dict[str, str] = {}
        module_re = re.compile(r"\bmodule\s+(?:automatic\s+)?([A-Za-z_]\w*)\b")
        for rel_path in self._scope_files():
            path = Path(self.args.work_dir) / rel_path
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            stripped = _strip_sv_comments(text)
            modules = set(module_re.findall(stripped))
            if modules:
                modules_by_file[rel_path] = modules
                source_text[rel_path] = stripped
        declared = set().union(*modules_by_file.values()) if modules_by_file else set()
        if len(declared) == 1:
            return next(iter(declared))

        instantiated = {
            module
            for module in declared
            if any(
                re.search(rf"\b{re.escape(module)}\s+(?:#\s*\([^;]*\)\s*)?\w+\s*\(", text)
                for rel_path in modules_by_file
                for text in [source_text[rel_path]]
            )
        }
        roots = declared - instantiated
        return next(iter(roots)) if len(roots) == 1 else ""

    def _dut_files(self) -> list[str]:
        """Resolve the DUT source files to inject mutation muxes into.

        Priority: explicit ``--dut-files`` arg (Interactive Mode) > the RTL
        (non-``tb``) source files of the resolved sim Target, read straight from
        the ``.core`` (ADR 0022 dec 13). The ``.core`` read is used rather than a
        resolved Target because the mux swap must precede ``resolve_target``
        (Unit A.3 ordering). Empty list when neither is available.

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
            return list(
                fusesoc_registry.target_source_files(
                    self.args.work_dir,
                    target,
                    include_dependencies=True,
                ).rtl_source_files
            )
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
        if not self._dut_top_module():
            return McpToolResult(
                exit_code=EXIT_FAILURE,
                report_text=(
                    "mutation_tester: could not derive one injection module "
                    "from --scope. Pass --dut-top to resolve the ambiguity."
                ),
            )
        if not self._dut_files():
            return McpToolResult(
                exit_code=EXIT_FAILURE,
                report_text=(
                    "mutation_tester: --dut-files is required when running "
                    "outside a ticket (space-separated DUT source files)."
                ),
            )
        return None

    def _dut_top_path(self) -> Path:
        """Resolve the worktree path to the DUT top source file.

        Raises a user-facing RuntimeError when the scope has no unambiguous
        injection module.
        """
        top = self._dut_top_module()
        if not top:
            raise RuntimeError(
                "cannot run mutation_tester: no unique injection module could "
                "be derived from --scope. Pass --dut-top to select one.",
            )
        path = lock_mod.find_dut_top_file(
            top,
            self._dut_files(),
            self.args.work_dir,
        )
        if path is None:
            raise RuntimeError(
                f"cannot locate DUT top file: no DUT source file in "
                f"{self._dut_files()} declares 'module {top}' and none has "
                f"a matching basename stem.  Correct --dut-top / --dut-files or "
                f"the sim Target's filesets.",
            )
        return path

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def _resolve_count(
        self,
        scope_files: list[str],
        work_dir: Path,
    ) -> tuple[int, dict | None, int, bool]:
        """Resolve the mutation count (int | "auto").

        Returns ``(count, complexity, formula_count, auto_mode)`` and emits the
        matching progress line for either the auto-scaled or fixed-count path.
        """
        if self.args.count == "auto":
            complexity = compute_rtl_complexity(scope_files, work_dir)
            formula_count = complexity["formula_count"]
            timeout = getattr(self.args, "timeout", 1800)
            budget_cap = max(3, timeout // 150)
            count = min(formula_count, budget_cap)
            auto_mode = True
        else:
            count = self.args.count
            complexity = None
            formula_count = count
            budget_cap = count
            auto_mode = False

        if auto_mode:
            self.emit_progress(
                f"auto-scaled: {count} mutations "
                f"(complexity {formula_count}, budget cap {budget_cap})",
            )
        else:
            self.emit_progress(f"target: {count} mutations")
        return count, complexity, formula_count, auto_mode

    def _run(self) -> McpToolResult:
        if campaign_error := self._apply_campaign_defaults():
            return campaign_error
        work_dir = self.args.work_dir
        report_dir = self.args.report_dir
        target = self.args.target
        scope_files = self._scope_files()

        # Fail fast: a --scope path that isn't a resolved source of --target
        # (e.g. a stealth-cores mirror) would elaborate to nothing, but only
        # after a full creator round. Reject it in <1s before any agent work.
        scope_err = self._validate_scope_against_target(scope_files)
        if scope_err is not None:
            return scope_err

        # Resolve the run-half up front (a subprocess-free .core read): an
        # undrivable Target must cost one YAML parse, never three creator
        # rounds ending in a misattributed "baseline broken" (SETUP-F-40).
        try:
            self._validate_target_runner(target, work_dir)
            self.cocotb_target(target, work_dir)
            self._target_test_suite(target)
        except UnsupportedSimTargetError as exc:
            return McpToolResult(exit_code=EXIT_ERROR, report_text=str(exc))

        # --- Resolve count (int | "auto") ---
        count, complexity, formula_count, auto_mode = self._resolve_count(
            scope_files,
            work_dir,
        )

        # --- Dry-run early exit ---
        if getattr(self.args, "dry_run", False):
            if complexity is None:
                complexity = compute_rtl_complexity(scope_files, work_dir)
            output = json.dumps(complexity, indent=2)
            print(output)
            return McpToolResult(exit_code=EXIT_SUCCESS, report_text=output)

        min_detected = self.args.min_detected if self.args.min_detected is not None else count
        self.args.count = count  # stamp resolved value for prompt + telemetry

        # --- Cold vs warm decision ---
        if getattr(self.args, "regen_lock", False):
            logger.info("--regen-lock requested: wiping existing lock dir")
            lock_mod.wipe_lock()
            self._clear_session_id(self.SESSION_KEY)

        scope_hashes = lock_mod.compute_scope_hashes(scope_files, work_dir)
        existing_lock = lock_mod.load_lock()
        is_warm = existing_lock is not None and lock_mod.is_lock_valid(
            existing_lock, scope_files, scope_hashes
        )

        # The runtime choice of N is bounded by what the lock contains
        # — running with N=20 against a lock of 10 mutations would mean
        # half the requested sims have no muxed branch.  Force a regen
        # when the locked count is smaller.
        if is_warm and existing_lock.count < count:
            logger.info(
                "lock has %d mutations but %d requested — forcing cold start",
                existing_lock.count,
                count,
            )
            lock_mod.wipe_lock()
            self._clear_session_id(self.SESSION_KEY)
            existing_lock = None
            is_warm = False

        plan = MutationRunPlan(
            scope_files=scope_files,
            scope_hashes=scope_hashes,
            work_dir=work_dir,
            target=target,
            report_dir=report_dir,
            min_detected=min_detected,
            count=count,
            auto_mode=auto_mode,
            formula_count=formula_count,
            complexity=complexity,
        )
        if is_warm:
            return self._run_warm(existing_lock, plan)

        return self._run_cold(plan)

    # ------------------------------------------------------------------
    # Cold start
    # ------------------------------------------------------------------

    def _run_cold(self, plan: MutationRunPlan) -> McpToolResult:
        """Cold-start flow: agent designs, harness verifies, lock is written."""
        self.emit_progress("cold start: harness injection + creator phase")

        # 1. Resolve and inject the harness into the DUT top file.
        try:
            dut_top_path = self._dut_top_path()
        except RuntimeError as exc:
            return McpToolResult(exit_code=EXIT_ERROR, report_text=str(exc))

        cleanup_files = self._cleanup_file_set(plan.scope_files, plan.work_dir)
        cleanup_snapshot = self._snapshot_worktree_files(cleanup_files, plan.work_dir)
        # Baseline of already-dirty tracked files: the git rollback net (below)
        # must revert what THIS run dirties, never pre-existing WIP.
        pre_dirty = self._git_modified_tracked(plan.work_dir)
        try:
            lock_mod.inject_mut_harness(dut_top_path, self._dut_top_module())
        except lock_mod.MutHarnessInjectionError as exc:
            self._restore_worktree_snapshot(cleanup_snapshot)
            self._revert_stray_tracked_edits(plan.work_dir, pre_dirty, keep=[])
            return McpToolResult(exit_code=EXIT_ERROR, report_text=str(exc))

        # Single rollback path covers every early-exit below — including a
        # turn-capped/timed-out/crashing creator (QA-11). The content snapshot
        # restores the enumerated candidate files; the git net then reverts any
        # OTHER tracked file the creator strayed into.
        result: McpToolResult | None = None
        try:
            result = self._cold_with_harness(plan, dut_top_path, pre_dirty)
        finally:
            lock_mod.remove_mut_harness(dut_top_path)
            self._restore_worktree_snapshot(cleanup_snapshot)
            self._revert_stray_tracked_edits(plan.work_dir, pre_dirty, keep=[])
        assert result is not None
        self._add_residue_warning(result, set(cleanup_files))
        return result

    def _cold_with_harness(  # noqa: PLR0915, PLR0912 — cold-path verification loop with harness setup, resume, scope-guard, and teardown
        self,
        plan: MutationRunPlan,
        dut_top_path: Path,
        pre_dirty: set[str] | None = None,
    ) -> McpToolResult:
        work_dir = plan.work_dir
        target = plan.target
        min_detected = plan.min_detected
        # 2. Verification loop with creator session resume on failure.
        specs: list[MutationSpec] = []
        last_outcome: VerificationOutcome | None = None
        creator_elapsed = 0.0
        tester_elapsed = 0.0
        verification_rounds_used = 0
        final_summary: MutationSummary | None = None
        # Kept for the failure path: a run that never reaches a verdict still
        # has a tally and a mutation list worth persisting (SETUP-F-39).
        last_summary: MutationSummary | None = None
        coverage_gap = False
        evidence: dict[str, Any] = {}

        creator_prompt = self._build_creator_prompt()
        retry_prompt: str | None = None

        for round_idx in range(1, self.MAX_VERIFICATION_ROUNDS + 1):
            verification_rounds_used = round_idx
            self.emit_progress(
                f"cold round {round_idx}/{self.MAX_VERIFICATION_ROUNDS}",
            )

            # On round 1 we run a fresh creator that emits JSON; subsequent
            # rounds resume the session and only edit source.
            is_first_round = round_idx == 1
            prompt = creator_prompt if is_first_round else (retry_prompt or "")

            try:
                with hide_opposite_sources(work_dir, "rtl"):
                    new_specs, elapsed = self._invoke_creator(
                        prompt,
                        resume=not is_first_round,
                        attempt=round_idx,
                    )
            except Exception as exc:
                logger.exception("Creator invocation failed on round %d", round_idx)
                return McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=f"creator agent invocation failed: {exc}",
                )
            creator_elapsed += elapsed

            # Scope enforcement (QA-11): the creator may ONLY write the scope
            # files (muxes) + the DUT top (harness import). Revert anything it
            # strayed into so out-of-scope mutations never reach the build, the
            # lock, or strand the tree. Hard boundary, checked every round.
            allowed_writes = self._cleanup_file_set(plan.scope_files, work_dir)
            strays = self._revert_stray_tracked_edits(
                work_dir,
                pre_dirty,
                keep=allowed_writes,
            )
            if strays:
                logger.warning(
                    "mutation_tester: creator wrote %d file(s) outside --scope "
                    "(%s) — reverted; --scope is a hard boundary",
                    len(strays),
                    ", ".join(strays[:5]),
                )
                self.emit_progress(
                    f"scope guard: reverted {len(strays)} out-of-scope edit(s)",
                )

            # Round 1 must produce specs.  Retry rounds may produce a fresh
            # JSON (when the failure was forbidden-category) or no JSON
            # (when the failure was sim/elab and the prompt said source-only).
            if is_first_round:
                if not new_specs:
                    return McpToolResult(
                        exit_code=EXIT_ERROR,
                        report_text=("creator agent returned no parseable mutation specs"),
                        detail=_failure_detail(
                            phase="creator_output",
                            reason="creator agent returned no parseable mutation specs",
                            specs=[],
                            work_dir=work_dir,
                            verification_rounds=round_idx,
                        ),
                    )
                specs = new_specs
            elif new_specs:
                specs = new_specs

            outside = self._specs_outside_scope(specs, plan.scope_files, work_dir)
            if outside:
                paths = sorted({spec.file for spec in outside})
                last_outcome = VerificationOutcome(
                    ok=False,
                    baseline_passed=False,
                    pinned_passed=False,
                    log_tail="out-of-scope mutation specs rejected: " + ", ".join(paths),
                    reason="mutation spec escaped --scope",
                )
                evidence = {"mutations_applied": False, "rejected_out_of_scope": paths}
                self._write_round_log(round_idx, last_outcome)
                if round_idx < self.MAX_VERIFICATION_ROUNDS:
                    retry_prompt = self._build_retry_prompt(last_outcome)
                    continue
                break
            self._canonicalize_spec_paths(specs, work_dir)

            # Phase 3.4 — forbidden-category gate (spec validation).
            forbidden = find_forbidden_specs(specs)
            if forbidden:
                forbidden_summary = ", ".join(f"#{s.index} ({s.category})" for s in forbidden)
                last_outcome = VerificationOutcome(
                    ok=False,
                    baseline_passed=False,
                    pinned_passed=False,
                    log_tail=(
                        f"forbidden categories present: {forbidden_summary}.\n"
                        "These cannot run under runtime mux selection — choose "
                        "expression-level mutations from the allowed templates."
                    ),
                    reason="forbidden category in spec list",
                )
                self._write_round_log(round_idx, last_outcome)
                if round_idx < self.MAX_VERIFICATION_ROUNDS:
                    retry_prompt = self._build_retry_prompt(last_outcome)
                    continue
                break

            # 3. Build + verify under the lock build dir.
            build_path = lock_mod.build_dir()
            build_path.mkdir(parents=True, exist_ok=True)
            outcome = self._verify_round(
                specs=specs,
                target=target,
                work_dir=work_dir,
                build_path=build_path,
                round_idx=round_idx,
            )
            last_outcome = outcome
            # The creator cannot fix a broken simulator. Abort now instead of
            # re-prompting it for MAX_VERIFICATION_ROUNDS (SETUP-F-41a).
            if outcome.infra_error:
                return McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"mutation_tester aborted on round {round_idx}: "
                        f"{outcome.reason}\nThe mutations were not graded — this "
                        "is a build-infrastructure problem, not a defect in the "
                        f"generated mutations.\n{outcome.log_tail}"
                    ),
                    detail=_failure_detail(
                        phase="verification_infra",
                        reason=outcome.reason,
                        specs=specs,
                        work_dir=work_dir,
                        verification_rounds=round_idx,
                        log_tail=outcome.log_tail,
                    ),
                )
            if outcome.ok:
                results, sweep_elapsed = self._run_sim_sweep(
                    specs=specs,
                    target=target,
                    work_dir=work_dir,
                    build_path=lock_mod.build_dir(),
                    tb_top=self.args.tb_top,
                )
                tester_elapsed += sweep_elapsed
                summary = MutationSummary(specs=specs, results=results)
                last_summary = summary
                valid_count = summary.detected_count + summary.not_detected_count
                zero_detected = (
                    min_detected > 0 and valid_count > 0 and summary.detected_count == 0
                )
                if not zero_detected:
                    final_summary = summary
                    break

                # 0 killed is only the creator's problem when the mutations
                # weren't really live. With the muxes in the source and the
                # selector echoed by the sim, the correct verdict is "this
                # Target's tests don't cover this scope" — re-prompting the
                # creator for it burned 3 rounds / $2.62 (SETUP-F-38).
                evidence = self._mutation_evidence(
                    specs,
                    summary,
                    plan.scope_files,
                    work_dir,
                )
                if self._is_coverage_gap(summary, min_detected, evidence):
                    self.emit_progress(
                        "0 killed with every mutation verified live: testbench coverage gap",
                    )
                    final_summary = summary
                    coverage_gap = True
                    break

                last_outcome = VerificationOutcome(
                    ok=False,
                    baseline_passed=True,
                    pinned_passed=True,
                    log_tail=(
                        f"the sweep applied {valid_count} mutation(s) and the "
                        "tests killed 0 of them, and the muxes could not be "
                        f"confirmed live ({_describe_evidence(evidence)}). The "
                        "nonzero MUT_ID branches are likely missing, equivalent "
                        "to baseline, unreachable, or checking the wrong "
                        "mutation selector."
                    ),
                    reason=f"sweep killed 0 of {valid_count} applied mutations",
                )
                self._write_round_log(round_idx, last_outcome)
                if round_idx < self.MAX_VERIFICATION_ROUNDS:
                    retry_prompt = self._build_zero_detection_retry_prompt(
                        summary,
                        min_detected,
                        evidence,
                    )
                    continue
                break
            if round_idx < self.MAX_VERIFICATION_ROUNDS:
                retry_prompt = self._build_retry_prompt(outcome)

        if final_summary is None:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    f"creator mutation generation failed after "
                    f"{self.MAX_VERIFICATION_ROUNDS} rounds: "
                    f"{last_outcome.reason if last_outcome else 'no outcome'}"
                ),
                detail=_failure_detail(
                    phase="cold_verification",
                    reason=last_outcome.reason if last_outcome else "no outcome",
                    specs=specs,
                    work_dir=work_dir,
                    summary=last_summary,
                    min_detected=min_detected,
                    count=plan.count,
                    verification_rounds=verification_rounds_used,
                    log_tail=last_outcome.log_tail if last_outcome else "",
                    evidence=evidence,
                ),
            )

        # 4. Persist artifacts to the lock dir + build the success result.
        return self._finalize_cold_run(
            plan,
            specs=specs,
            final_summary=final_summary,
            tester_elapsed=tester_elapsed,
            creator_elapsed=creator_elapsed,
            verification_rounds_used=verification_rounds_used,
            coverage_gap=coverage_gap,
            evidence=evidence,
        )

    def _finalize_cold_run(
        self,
        plan: MutationRunPlan,
        *,
        specs: list[MutationSpec],
        final_summary: MutationSummary,
        tester_elapsed: float,
        creator_elapsed: float,
        verification_rounds_used: int,
        coverage_gap: bool = False,
        evidence: dict[str, Any] | None = None,
    ) -> McpToolResult:
        """Persist the verified lock + build the cold-start success result."""
        scope_files = plan.scope_files
        host_file = specs[0].file if specs else (scope_files[0] if scope_files else "")
        self._persist_lock(
            scope_files=scope_files,
            scope_hashes=plan.scope_hashes,
            specs=specs,
            host_file=host_file,
            work_dir=plan.work_dir,
        )
        # Save build_meta so the next warm invocation can reuse the elab.
        muxed_hashes = self._muxed_lock_hashes(scope_files)
        build_inputs = self._build_input_hashes(
            work_dir=plan.work_dir,
            target=plan.target,
            scope_files=scope_files,
            muxed_hashes=muxed_hashes,
        )
        docker_digest = lock_mod.get_docker_digest()
        lock_mod.save_build_meta(muxed_hashes, docker_digest, build_inputs)

        # Session can be cleared — verification passed, no more retries.
        self._clear_session_id(self.SESSION_KEY)

        persisted = lock_mod.load_lock()
        summary = final_summary
        self.emit_progress(
            f"cold done: {summary.detected_count}/"
            f"{summary.detected_count + summary.not_detected_count} detected",
        )
        return self._build_run_result(
            plan,
            RunResultInputs(
                summary=summary,
                count=plan.count,
                tester_elapsed=tester_elapsed,
                creator_elapsed=creator_elapsed,
                reused_lock=False,
                lock_created_at=persisted.created_at if persisted else None,
                verification_rounds=verification_rounds_used,
                build_cached=False,
                coverage_gap=coverage_gap,
                evidence=evidence or {},
            ),
        )

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

    def _verify_round(
        self,
        specs: list[MutationSpec],
        target: str,
        work_dir: Path,
        build_path: Path,
        round_idx: int,
    ) -> VerificationOutcome:
        """Compile once + sim MUT_ID=0 and one pinned k.  Persist a log."""
        # 1. Elab into the build dir (fresh — drop any prior contents to
        # avoid Verilator's stale-makefile pitfalls).
        if build_path.exists():
            shutil.rmtree(build_path, ignore_errors=True)
        build_path.mkdir(parents=True, exist_ok=True)
        elab_proc = self._run_elab(target, work_dir, build_path)
        if elab_proc.returncode != 0:
            outcome = VerificationOutcome(
                ok=False,
                baseline_passed=False,
                pinned_passed=False,
                log_tail=_tail(elab_proc.stdout + elab_proc.stderr, 50),
                reason="elaboration of muxed RTL failed",
            )
            self._write_round_log(round_idx, outcome)
            return outcome

        # 2. Baseline (MUT_ID=0).  An infra failure here is NOT the creator's
        # fault: blaming its (correct) mutations and re-prompting for two more
        # rounds cost $8.71 / 31 min on a 5-mutation run (SETUP-F-41a).
        baseline_runs = self._run_target_test_suite(
            target,
            work_dir,
            build_path,
            self.args.tb_top,
            mut_id=0,
        )
        baseline_output = self._suite_output(baseline_runs)
        infra = self._suite_infra_reason(baseline_runs)
        if infra:
            return self._infra_outcome_text(round_idx, infra, baseline_output)
        baseline_passed = self._baseline_suite_passed(baseline_runs)

        # 3. Pinned non-zero MUT_ID.  Pin k = ceil(N/2) so we hit a mutation
        # roughly in the middle of the index range — index 1 in edge cases.
        n = len(specs)
        pinned_k = max(1, math.ceil(n / 2)) if n > 0 else 1
        pinned_runs = self._run_target_test_suite(
            target,
            work_dir,
            build_path,
            self.args.tb_top,
            mut_id=pinned_k,
        )
        pinned_infra = self._suite_infra_reason(pinned_runs)
        if pinned_infra:
            return self._infra_outcome_text(
                round_idx,
                pinned_infra,
                self._suite_output(pinned_runs),
            )
        # "Pass" for pinned means: the simulator really ran, completed, and did
        # not hang.  A failing testbench under a real mutation is FINE — what
        # matters is that a run happened.  Grading is fail-CLOSED: only an
        # observed outcome counts, so the infra check above must have cleared
        # first (an unbuilt binary used to grade as a pass, and in a real sweep
        # would have been scored as a kill — SETUP-F-41b).
        pinned_log = self._suite_output(pinned_runs)
        pinned_passed = all(not run.timed_out and not run.error for run in pinned_runs)

        ok = baseline_passed and pinned_passed
        log_tail = _tail(baseline_output, 25) + "\n---\n" + _tail(pinned_log, 25)
        reason = ""
        if not baseline_passed:
            reason = "MUT_ID=0 baseline did not pass — default branches incorrect"
        elif not pinned_passed:
            reason = f"pinned MUT_ID={pinned_k} crashed or hung"
        outcome = VerificationOutcome(
            ok=ok,
            baseline_passed=baseline_passed,
            pinned_passed=pinned_passed,
            log_tail=log_tail,
            reason=reason,
        )
        self._write_round_log(round_idx, outcome)
        return outcome

    def _infra_outcome(
        self,
        round_idx: int,
        reason: str,
        proc: subprocess.CompletedProcess,
    ) -> VerificationOutcome:
        """Record a harness (not design) failure; the caller must not retry."""
        outcome = VerificationOutcome(
            ok=False,
            baseline_passed=False,
            pinned_passed=False,
            log_tail=_tail((proc.stdout or "") + (proc.stderr or ""), 50),
            reason=f"simulation harness failure (not a mutation defect): {reason}",
            infra_error=reason,
        )
        self._write_round_log(round_idx, outcome)
        return outcome

    def _infra_outcome_text(
        self,
        round_idx: int,
        reason: str,
        output: str,
    ) -> VerificationOutcome:
        """Record a Target-suite harness failure without inventing a process."""
        outcome = VerificationOutcome(
            ok=False,
            baseline_passed=False,
            pinned_passed=False,
            log_tail=_tail(output, 50),
            reason=f"simulation harness failure (not a mutation defect): {reason}",
            infra_error=reason,
        )
        self._write_round_log(round_idx, outcome)
        return outcome

    def _write_round_log(
        self,
        round_idx: int,
        outcome: VerificationOutcome,
    ) -> None:
        # The verification-rounds dir is a fixed lock-relative location, so
        # resolve it here rather than threading it through every caller.
        round_logs_dir = lock_mod.verification_rounds_dir()
        round_logs_dir.mkdir(parents=True, exist_ok=True)
        (round_logs_dir / f"round_{round_idx}.log").write_text(
            f"baseline_passed={outcome.baseline_passed}\n"
            f"pinned_passed={outcome.pinned_passed}\n"
            f"reason={outcome.reason}\n"
            f"---log_tail---\n{outcome.log_tail}\n",
            encoding="utf-8",
        )

    def _persist_lock(
        self,
        scope_files: list[str],
        scope_hashes: dict[str, str],
        specs: list[MutationSpec],
        host_file: str,
        work_dir: Path,
    ) -> None:
        """Copy muxed scope files into the lock dir and write lock.json."""
        ld = lock_mod.lock_dir()
        ld.mkdir(parents=True, exist_ok=True)
        muxed_basenames: list[str] = []
        for rel in scope_files:
            src = work_dir / rel
            dst = lock_mod.muxed_path(rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            muxed_basenames.append(dst.relative_to(ld).as_posix())
        # Persist the package text for traceability (not loaded back). Read the
        # DUT top — still harness-injected at this point — so the copy inherits
        # the design's time declarations the same way the compiled package did
        # (SETUP-F-37).  This is the package *body* only: for a `timescale-style
        # DUT the directive is emitted above the package by the injector, so the
        # saved file is deliberately a subset of what the sim compiled.
        try:
            dut_text = self._dut_top_path().read_text(encoding="utf-8")
        except (RuntimeError, OSError):
            dut_text = ""
        lock_mod.pkg_path().write_text(
            lock_mod.generate_mut_pkg(dut_text),
            encoding="utf-8",
        )
        meta = lock_mod.LockMeta(
            schema_version=lock_mod.LOCK_SCHEMA_VERSION,
            created_at=lock_mod.now_iso(),
            scope=list(scope_files),
            scope_hashes=scope_hashes,
            count=len(specs),
            host_file=host_file,
            mutations=[s.to_dict() for s in specs],
            muxed_files=muxed_basenames,
            pkg_file=lock_mod.MUT_PKG_FILENAME,
            docker_digest=lock_mod.get_docker_digest(),
        )
        lock_mod.save_lock(meta)

    def _muxed_lock_hashes(self, scope_files: list[str]) -> dict[str, str]:
        """Hash every muxed file in the lock dir, keyed by scope-relative path."""
        return {
            rel: lock_mod._hash_file(lock_mod.muxed_path(rel))
            for rel in scope_files
            if lock_mod.muxed_path(rel).exists()
        }

    def _build_input_hashes(
        self,
        work_dir: Path,
        target: str,
        scope_files: list[str],
        muxed_hashes: dict[str, str],
    ) -> dict[str, str]:
        """Hash simulation inputs that affect the compiled mutation build.

        The mutation lock itself is keyed by original RTL scope contents.  The
        compiled ``sim.vvp`` also depends on the current testbench and any
        non-mutated RTL helpers, so warm reuse must invalidate when those files
        change.  Hash after muxed scope files have been swapped into *work_dir*
        so the snapshot matches the actual elaboration inputs.
        """
        hashes: dict[str, str] = {
            "__config__": target,
            # A Cocotb Target has no SV testbench top, so --tb-top is legitimately
            # absent there; normalize to "" rather than let None into the hash.
            "__tb_top__": self.args.tb_top or "",
        }
        for rel, digest in sorted(muxed_hashes.items()):
            hashes[f"__muxed__:{rel}"] = digest

        for root in _build_input_roots(work_dir):
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix not in _BUILD_INPUT_SUFFIXES:
                    continue
                rel = path.relative_to(work_dir).as_posix()
                hashes[rel] = lock_mod._hash_file(path)
        for config_file in _build_input_config_files(work_dir):
            if config_file.is_file():
                hashes[f"__config_file__:{config_file.name}"] = lock_mod._hash_file(config_file)
        return hashes

    # ------------------------------------------------------------------
    # Warm reuse
    # ------------------------------------------------------------------

    def _run_warm(
        self,
        lock: lock_mod.LockMeta,
        plan: MutationRunPlan,
    ) -> McpToolResult:
        """Warm-reuse path: muxed files already designed, just sim them."""
        scope_files = plan.scope_files
        work_dir = plan.work_dir
        target = plan.target
        count = plan.count
        self.emit_progress(
            f"warm reuse: lock from {lock.created_at}, {lock.count} mutations",
        )
        if self.args.steer:
            logger.warning(
                "--steer is ignored on warm reuse; use --regen-lock to apply",
            )

        # Resolve specs from lock metadata.
        specs = [MutationSpec.from_dict(d) for d in lock.mutations][:count]
        evidence: dict[str, Any] = {}

        # 1. Inject harness into DUT top (cleaned up in finally).
        try:
            dut_top_path = self._dut_top_path()
        except RuntimeError as exc:
            return McpToolResult(exit_code=EXIT_ERROR, report_text=str(exc))
        cleanup_files = self._cleanup_file_set(scope_files, work_dir)
        cleanup_snapshot = self._snapshot_worktree_files(cleanup_files, work_dir)
        pre_dirty = self._git_modified_tracked(work_dir)
        try:
            lock_mod.inject_mut_harness(dut_top_path, self._dut_top_module())
        except lock_mod.MutHarnessInjectionError as exc:
            self._restore_worktree_snapshot(cleanup_snapshot)
            self._revert_stray_tracked_edits(work_dir, pre_dirty, keep=[])
            return McpToolResult(exit_code=EXIT_ERROR, report_text=str(exc))

        try:
            # 2. Swap muxed files into the worktree.
            self._swap_muxed_in(scope_files, work_dir)

            # 3. Build cache validation (rebuild the muxed sim if stale).
            build_path, build_cached, rebuild_error = self._ensure_warm_build(
                scope_files=scope_files,
                work_dir=work_dir,
                target=target,
            )
            if rebuild_error is not None:
                return rebuild_error

            # 4. Baseline sanity (MUT_ID=0).
            baseline_runs = self._run_target_test_suite(
                target,
                work_dir,
                build_path,
                self.args.tb_top,
                mut_id=0,
            )
            baseline_output = self._suite_output(baseline_runs)
            infra = self._suite_infra_reason(baseline_runs)
            if infra:
                # Same distinction as the cold path: an unrunnable simulator is
                # not a broken lock, and --regen-lock would not fix it.
                return McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        "warm reuse: simulation harness failure (not a mutation "
                        f"defect): {infra}\n"
                        f"{_tail(baseline_output, 30)}"
                    ),
                )
            if not self._baseline_suite_passed(baseline_runs):
                return McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        "warm reuse: lock appears intact but MUT_ID=0 baseline "
                        "is broken — pass --regen-lock to force a fresh "
                        "creator run.\n"
                        f"{_tail(baseline_output, 30)}"
                    ),
                )

            # 5. Deterministic loop k = 1 .. count.
            results, tester_elapsed = self._run_sim_sweep(
                specs=specs,
                target=target,
                work_dir=work_dir,
                build_path=build_path,
                tb_top=self.args.tb_top,
            )
            # Collect the mux/selector evidence while the muxed files are still
            # swapped in — the rollback below erases the static half of it.
            evidence = self._mutation_evidence(
                specs,
                MutationSummary(specs=specs, results=results),
                scope_files,
                work_dir,
            )
        finally:
            lock_mod.remove_mut_harness(dut_top_path)
            self._restore_worktree_snapshot(cleanup_snapshot)
            self._revert_stray_tracked_edits(work_dir, pre_dirty, keep=[])

        summary = MutationSummary(specs=specs, results=results)
        self.emit_progress(
            f"warm reuse done: {summary.detected_count}/"
            f"{summary.detected_count + summary.not_detected_count} detected",
        )
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
                build_cached=build_cached,
                coverage_gap=self._is_coverage_gap(summary, plan.min_detected, evidence),
                evidence=evidence,
            ),
        )

    def _ensure_warm_build(
        self,
        *,
        scope_files: list[str],
        work_dir: Path,
        target: str,
    ) -> tuple[Path, bool, McpToolResult | None]:
        """Validate (and rebuild, if stale) the cached muxed sim for warm reuse.

        Returns ``(build_path, build_cached, error)``.  ``error`` is a
        :class:`McpToolResult` when a stale-cache rebuild fails; the caller returns
        it verbatim.  On success ``error`` is ``None``.
        """
        current_muxed_hashes = self._muxed_lock_hashes(scope_files)
        build_inputs = self._build_input_hashes(
            work_dir=work_dir,
            target=target,
            scope_files=scope_files,
            muxed_hashes=current_muxed_hashes,
        )
        docker_digest = lock_mod.get_docker_digest()
        build_path = lock_mod.build_dir()
        build_cached = lock_mod.is_build_cache_valid(
            current_muxed_hashes,
            docker_digest,
            build_inputs,
        )
        if not build_cached:
            self.emit_progress("warm reuse: rebuilding (cache stale)")
            if build_path.exists():
                shutil.rmtree(build_path, ignore_errors=True)
            build_path.mkdir(parents=True, exist_ok=True)
            elab = self._run_elab(target, work_dir, build_path)
            if elab.returncode != 0:
                return (
                    build_path,
                    build_cached,
                    McpToolResult(
                        exit_code=EXIT_ERROR,
                        report_text=(
                            "warm reuse: rebuild of muxed RTL failed "
                            "(use --regen-lock to force a fresh creator run).\n"
                            f"{_tail(elab.stdout + elab.stderr, 30)}"
                        ),
                    ),
                )
            lock_mod.save_build_meta(
                current_muxed_hashes,
                docker_digest,
                build_inputs,
            )
        return build_path, build_cached, None

    def _swap_muxed_in(self, scope_files: list[str], work_dir: Path) -> None:
        """Overwrite each scope file with its muxed counterpart from the lock."""
        for rel in scope_files:
            src = lock_mod.muxed_path(rel)
            if not src.exists():
                raise RuntimeError(
                    f"lock is missing muxed file for {rel} at {src}; pass --regen-lock to rebuild",
                )
            dst = work_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # ------------------------------------------------------------------
    # Sim / elab subprocess helpers
    # ------------------------------------------------------------------

    def _run_elab(
        self,
        target: str,
        work_dir: Path,
        build_path: Path,
    ) -> subprocess.CompletedProcess:
        """Build the muxed sim binary **once** via FuseSoC/Edalize (Unit A.3).

        Replaces ``run_sim_batch --elab-only``: ``resolve_target`` (``run
        --setup``) copy-stages the *already-muxed* worktree sources — the
        caller swaps them in first, a load-bearing ordering so edalize stages
        the mutated RTL (ADR 0022) — into *build_path*, then ``make`` compiles
        the selected EDA toolchain's simulation image with the mutation muxes baked
        in. The resolved binary dir and EDA-tool family are recorded in markers so
        the per-mutant run-many loop reuses the build without re-resolving.
        Sources now come from the ``.core``, so the legacy ``BOOLEY_SIM_*``
        source-pinning env is no longer needed.

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
        except Exception as exc:  # noqa: BLE001 — isolate resolve failure; surfaced as a returncode-1 CompletedProcess
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
        mut_id: int,
        timeout: int,
        test_names: tuple[str, ...],
    ) -> list[str]:
        """Build the :mod:`booley.sim.cocotb_run` invocation for one mutant.

        A Cocotb Target's binary is driven from Python over VPI, so the run
        needs cocotb's ``COCOTB_TEST_MODULES``/filter environment — which only
        the cocotb run-half knows how to assemble. Reusing it (rather than
        exec'ing ``Vtop`` bare) is what makes MUT_ID=0 a *real* baseline
        (SETUP-F-40). ``+MUT_ID`` still rides in as a plusarg: cocotb's own
        verilator ``main`` forwards argv to ``Verilated::commandArgs``, and vvp
        takes plusargs after the image, so ``$value$plusargs`` resolves either
        way.

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
            "--tool",
            cocotb.eda_tool,
            "--cocotb-module",
            cocotb.module,
            "--timeout",
            str(max(1, timeout - 5)),
            "--plusarg",
            f"MUT_ID={mut_id}",
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
        mut_id: int,
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
            "--plusarg",
            f"MUT_ID={mut_id}",
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
        mut_id: int,
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
            "--plusarg",
            f"MUT_ID={mut_id}",
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
        mut_id: int,
        timeout: int = 300,
        test_name: str | None = None,
        cocotb_tests: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess:
        """Run the prebuilt sim once with ``+MUT_ID=<k>`` (Unit A.3).

        Replaces ``run_sim_batch --sim-only``: what ``_run_elab`` built persists
        in *build_path*, so each mutant just re-invokes it (~0.1s, Q5) with the
        mutation selector plusarg and no trace. Which run-half drives it depends
        on the Target: a Cocotb Target goes through :mod:`booley.sim.cocotb_run`
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
                mut_id=mut_id,
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
                    mut_id=mut_id,
                    timeout=timeout,
                    test_name=test_name,
                )
            elif eda_tool == "verilator":
                cmd = self._verilator_sim_cmd(
                    rel=rel,
                    target=target,
                    work_dir=work_dir,
                    tb_top=tb_top,
                    mut_id=mut_id,
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
        mut_id: int,
        timeout: int = 300,
    ) -> list[MutationTestRun]:
        """Run one mutation selector against the Target's complete test suite."""
        campaign = self._campaign_for_target(target)
        batched = self.cocotb_target(target, work_dir) is not None

        def _run_unit(unit: CampaignUnit) -> MutationTestRun:
            try:
                proc = self._run_sim_pinned(
                    target,
                    work_dir,
                    build_path,
                    tb_top,
                    mut_id=mut_id,
                    timeout=timeout,
                    test_name=unit.test_name,
                    cocotb_tests=unit.selected_tests,
                )
                return MutationTestRun(
                    test_name=unit.display_name,
                    process=proc,
                    output=(proc.stdout or "") + (proc.stderr or ""),
                )
            except subprocess.TimeoutExpired as exc:
                return MutationTestRun(
                    test_name=unit.display_name,
                    timed_out=True,
                    output=_timeout_output(exc),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return MutationTestRun(test_name=unit.display_name, error=str(exc))

        return list(campaign.execute(_run_unit, batched=batched).values)

    @staticmethod
    def _suite_output(runs: list[MutationTestRun]) -> str:
        """Render per-test output without losing which test produced it."""
        return "\n".join(f"===== {run.test_name} =====\n{run.output or run.error}" for run in runs)

    @staticmethod
    def _suite_infra_reason(runs: list[MutationTestRun]) -> str:
        """Return the first inconclusive suite outcome, or an empty string."""
        for run in runs:
            if run.error:
                return f"{run.test_name}: {run.error}"
            if run.timed_out:
                return f"{run.test_name}: simulation timed out"
            if run.process is not None and (reason := _infra_failure_reason(run.process)):
                return f"{run.test_name}: {reason}"
        return ""

    @staticmethod
    def _baseline_suite_passed(runs: list[MutationTestRun]) -> bool:
        """A baseline passes only when every Target test completes successfully."""
        return bool(runs) and all(
            not run.timed_out
            and not run.error
            and run.process is not None
            and not _infra_failure_reason(run.process)
            and run.process.returncode == 0
            for run in runs
        )

    def _persist_mutant_log(self, mut_id: int, output: str) -> str:
        """Write one mutant's full simulator output; return its relative path.

        Lands as ``mutant_<mut_id>.log`` under
        :func:`mutation_lock.mutant_logs_dir`. Every mutant re-invokes the same
        prebuilt binary in the same build dir, so its ``run.log`` would be
        overwritten by the next mutant a tenth of a second later — this is the
        only durable per-mutant record. Keyed by ``mut_id`` (the selector the
        design actually saw), not the list index, so the filename matches what
        the report and the ``+MUT_ID`` echo both name.

        Capped at :data:`_MUTANT_LOG_MAX_BYTES` per mutant, tail-kept: a
        ``$display``-per-cycle testbench times this by the mutant count (which
        has no upper bound) and again by up to
        :data:`MAX_VERIFICATION_ROUNDS` sweeps. The tail is where the verdict
        and the failure wording live, so it is the half worth keeping — the
        same trade :func:`sim_result.write_run_log` makes.

        Best-effort: a log-write failure returns ``""`` and the sweep carries
        on. Losing a log must never cost a mutation run its verdict.
        """
        from booley.sim.sim_result import _cap_log_bytes

        try:
            log_dir = lock_mod.mutant_logs_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / f"mutant_{mut_id}.log"
            data = _cap_log_bytes(
                output.encode("utf-8", errors="replace"),
                _MUTANT_LOG_MAX_BYTES,
            )
            path.write_bytes(data)
        except OSError:
            logger.debug("could not persist mutant log for MUT_ID=%s", mut_id, exc_info=True)
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

    def _run_sim_sweep(
        self,
        specs: list[MutationSpec],
        target: str,
        work_dir: Path,
        build_path: Path,
        tb_top: str,
    ) -> tuple[list[MutationResult], float]:
        """Run the deterministic MUT_ID=1..N sweep against the prebuilt sim."""
        start = time.monotonic()
        # A previous sweep's logs must not outlive it — see _reset_mutant_logs.
        self._reset_mutant_logs()
        results: list[MutationResult] = []
        for spec in specs:
            self.emit_progress(
                f"sim mutation {spec.index}/{len(specs)} (MUT_ID={spec.mut_id or spec.index})",
            )
            mut_id = spec.mut_id or spec.index
            runs = self._run_target_test_suite(
                target,
                work_dir,
                build_path,
                tb_top,
                mut_id=mut_id,
            )
            combined = self._suite_output(runs)
            infra = self._suite_infra_reason(runs)
            detected = any(
                run.timed_out
                or (
                    run.process is not None
                    and not _infra_failure_reason(run.process)
                    and run.process.returncode != 0
                )
                for run in runs
            )
            invalid = bool(infra) and not detected
            snippet = f"sim infra error: {infra}" if invalid else _tail(combined, 5)[-200:]
            results.append(
                MutationResult(
                    index=spec.index,
                    invalid=invalid,
                    detected=detected,
                    sim_output_snippet=snippet,
                    selector_observed=(f"{lock_mod.MUT_ECHO_PREFIX}{mut_id}" in combined),
                    log_path=self._persist_mutant_log(mut_id, combined),
                )
            )
        return results, time.monotonic() - start

    # ------------------------------------------------------------------
    # Worktree cleanup
    # ------------------------------------------------------------------

    def _mutation_evidence(
        self,
        specs: list[MutationSpec],
        summary: MutationSummary,
        scope_files: list[str],
        work_dir: Path,
    ) -> dict[str, Any]:
        """Collect the evidence that separates a coverage gap from a broken harness.

        A 0-killed sweep has exactly two explanations, and blaming the wrong one
        costs three paid creator rounds on a run that could never pass
        (SETUP-F-38).  They are told apart by two observations, both made while
        the muxed sources are still on disk:

          - *static*: every spec's ``mut_id == k`` guard is present in the
            files the sweep mutated, so the muxes were written and survived the
            scope guard;
          - *runtime*: every spec's own run echoed ``+MUT_ID=k`` back from the
            injected reader, so the selector actually reached the design on
            each of them — not just on the one run that happened to survive.

        Both true ⇒ the mutations ran and the tests said nothing: a testbench
        coverage gap.  Either false ⇒ the muxes or the plusarg path are broken,
        which is what the creator can (and should) be asked to fix.

        Must be called before the worktree rollback — afterwards the scope
        files are back to their pristine, mux-free contents.
        """
        text_parts: list[str] = []
        for rel in self._cleanup_file_set(scope_files, work_dir):
            path = work_dir / rel
            if path.is_file():
                text_parts.append(path.read_text(encoding="utf-8", errors="replace"))
        muxed_text = "\n".join(text_parts)

        missing = [
            spec.index
            for spec in specs
            if not _mut_guard_regex(spec.mut_id or spec.index).search(muxed_text)
        ]
        by_index = {r.index: r for r in summary.results}
        observed = sorted(
            spec.index
            for spec in specs
            if (r := by_index.get(spec.index)) is not None and r.selector_observed
        )
        return {
            "muxes_found": len(specs) - len(missing),
            "muxes_missing": missing,
            "selector_observed": observed,
            # Fail-closed: no spec list, a missing mux, or *any* mutant whose
            # selector was never echoed means we cannot claim the mutations ran.
            # "at least one echo" would not do: runs that die on
            # [SIM_INFRA_ERROR] are graded invalid and drop out of valid_count,
            # so a sweep where 9 of 10 mutants never really executed still
            # reaches "0 killed" with one lone echo — and would then be
            # certified as a coverage gap on the strength of a single run.
            "mutations_applied": bool(specs) and not missing and len(observed) == len(specs),
        }

    @staticmethod
    def _is_coverage_gap(
        summary: MutationSummary,
        min_detected: int,
        evidence: dict[str, Any],
    ) -> bool:
        """True when nothing was killed *and* every mutation was provably live."""
        valid_count = summary.detected_count + summary.not_detected_count
        zero_killed = min_detected > 0 and valid_count > 0 and summary.detected_count == 0
        return zero_killed and bool(evidence.get("mutations_applied"))

    def _cleanup_file_set(self, scope_files: list[str], work_dir: Path) -> list[str]:
        """Return canonical scope files plus DUT top, without duplicates."""
        files_to_restore = [
            fusesoc_registry.canonical_project_path(work_dir, path) for path in scope_files
        ]
        try:
            top = self._dut_top_path()
            files_to_restore.append(fusesoc_registry.canonical_project_path(work_dir, top))
        except RuntimeError:
            pass
        return list(dict.fromkeys(files_to_restore))

    def _snapshot_worktree_files(
        self,
        rel_paths: list[str],
        work_dir: Path,
    ) -> dict[str, WorktreeFileSnapshot]:
        """Capture exact pre-mutation contents, including ignored RTL files."""
        snapshots: dict[str, WorktreeFileSnapshot] = {}
        for rel in rel_paths:
            path = work_dir / rel
            if path.exists():
                snapshots[rel] = WorktreeFileSnapshot(
                    rel_path=rel,
                    path=path,
                    existed=True,
                    content=path.read_bytes(),
                )
            else:
                snapshots[rel] = WorktreeFileSnapshot(
                    rel_path=rel,
                    path=path,
                    existed=False,
                )
        return snapshots

    def _restore_worktree_snapshot(
        self,
        snapshots: dict[str, WorktreeFileSnapshot],
    ) -> None:
        """Restore or remove files touched by mutation testing."""
        for snapshot in snapshots.values():
            try:
                if snapshot.existed:
                    snapshot.path.parent.mkdir(parents=True, exist_ok=True)
                    snapshot.path.write_bytes(snapshot.content or b"")
                elif snapshot.path.exists():
                    snapshot.path.unlink()
            except OSError as exc:
                raise RuntimeError(
                    f"failed to restore mutation tester file {snapshot.rel_path}: {exc}"
                ) from exc

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
    # The content snapshot (``_snapshot_worktree_files``) only restores the
    # files we can enumerate up front (scope + DUT top). A turn-capped /
    # timed-out / crashing creator agent can write muxes into *other* tracked
    # files it was never supposed to touch, and those get stranded. These
    # helpers give a hard guarantee: any tracked file the run dirtied — beyond
    # the caller-protected ``keep`` set and the pre-existing WIP baseline — is
    # restored to its pre-run (index) state via git.
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
        auto_mode, formula_count, complexity = (
            plan.auto_mode,
            plan.formula_count,
            plan.complexity,
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
            detail["complexity"] = complexity
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
        artifact_dir = self.reserve_invocation_dir() if plan.report_dir else None
        mutated_rtl = self._preserve_mutated_rtl(plan.scope_files, artifact_dir)
        detail["mutated_rtl_files"] = self._mutated_rtl_detail(mutated_rtl)
        self._attach_campaign_directories(detail, artifact_dir)
        if artifact_dir is None:
            return
        specs_path = artifact_dir / "mutation-specs.md"
        specs_path.write_text(generate_specs_markdown(inputs.summary.specs), encoding="utf-8")
        results_path = artifact_dir / "mutation-results.md"
        source_links = {
            source: posix_relpath(path, artifact_dir) for source, path in mutated_rtl.items()
        }
        results_path.write_text(
            generate_results_markdown(
                inputs.summary,
                plan.min_detected,
                mutated_rtl_paths=source_links,
            ),
            encoding="utf-8",
        )
        if plan.complexity and not inputs.reused_lock:
            (artifact_dir / "complexity-breakdown.json").write_text(
                json.dumps(plan.complexity, indent=2),
                encoding="utf-8",
            )
        _artifacts.merge_artifacts(
            detail,
            _artifacts.artifacts_block(
                self.args.work_dir,
                specs=specs_path,
                results=results_path,
            ),
        )

    def _attach_campaign_directories(
        self,
        detail: dict[str, Any],
        artifact_dir: Path | None,
    ) -> None:
        mutated_dir = (
            artifact_dir / "mutated-rtl" if artifact_dir else lock_mod.lock_dir() / "muxed"
        )
        _artifacts.merge_artifacts(
            detail,
            _artifacts.artifacts_block(
                self.args.work_dir,
                dirs={
                    "mutated_rtl": mutated_dir,
                    "mutant_logs": lock_mod.mutant_logs_dir(),
                    "verification_rounds": lock_mod.verification_rounds_dir(),
                },
            ),
        )

    def _preserve_mutated_rtl(
        self,
        scope_files: list[str],
        artifact_dir: Path | None,
    ) -> dict[str, Path]:
        """Return inspectable muxed RTL, copying it into a durable run when possible."""
        preserved: dict[str, Path] = {}
        muxed_root = lock_mod.lock_dir() / "muxed"
        for scope_file in scope_files:
            source = lock_mod.muxed_path(scope_file)
            if not source.is_file():
                continue
            if artifact_dir is None:
                preserved[scope_file] = source
                continue
            destination = artifact_dir / "mutated-rtl" / source.relative_to(muxed_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            preserved[scope_file] = destination
        return preserved

    def _mutated_rtl_detail(self, mutated_rtl: dict[str, Path]) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for source, path in mutated_rtl.items():
            relative = _artifacts.relative(path, self.args.work_dir)
            if relative is not None:
                rows.append({"source": source, "path": relative})
        return rows

    @staticmethod
    def _artifact_display_lines(detail: dict[str, Any]) -> list[str]:
        lines = [
            f"mutated RTL: {row['path']}"
            for row in detail.get("mutated_rtl_files", [])
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
    """One-line, human-readable rendering of ``_mutation_evidence``."""
    if not evidence:
        return "no mux/selector evidence collected"
    missing = evidence.get("muxes_missing") or []
    observed = evidence.get("selector_observed") or []
    found = evidence.get("muxes_found", 0)
    parts = [f"{found} mux guard(s) found in the muxed source"]
    if missing:
        parts.append("no guard for mutation(s) " + ", ".join(f"#{i}" for i in missing))
    # Render the echo count as a fraction: a partial tally (say 1/10) is the
    # symptom of a sweep that fell over mid-way, and is what tells the reader
    # apart from a harness that never worked at all.
    total = found + len(missing)
    parts.append(
        f"+MUT_ID echoed by the design on {len(observed)}/{total} run(s)"
        if observed
        else "the design never echoed +MUT_ID on any run"
    )
    return "; ".join(parts)


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
                "mutated_rtl": lock_mod.lock_dir() / "muxed",
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


def _build_input_roots(work_dir: Path) -> list[Path]:
    """Return source/include roots (from the ``.core``) that affect sim elaboration."""
    try:
        from booley.fusesoc.fusesoc_registry import source_dirs_from_core

        rtl_dirs, tb_dirs, tb_incl = source_dirs_from_core(work_dir)
    except Exception:  # noqa: BLE001 — registry unavailable; legacy defaults
        rtl_dirs, tb_dirs, tb_incl = ["rtl"], ["tb"], []
    raw_roots = [*rtl_dirs, *tb_dirs, *tb_incl]
    roots: list[Path] = []
    for raw in raw_roots:
        if not isinstance(raw, str) or not raw.strip():
            continue
        root = Path(raw)
        if not root.is_absolute():
            root = work_dir / root
        if root not in roots:
            roots.append(root)
    return roots


def _build_input_config_files(work_dir: Path) -> list[Path]:
    """Return config files whose edits invalidate mutation build cache."""
    return [
        work_dir / "booley.toml",
        work_dir / "configs.toml",
        work_dir / ".booley_project" / "booley.toml",
        work_dir / ".booley_project" / "configs.toml",
    ]


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
