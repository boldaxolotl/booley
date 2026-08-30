"""CoverageAnalystSpecialist — hybrid coverage-measurement Specialist.

Architecture:
  Phase 1 (Mechanical): bwave stats extracts toggle/value data for all
    signals in scope.
  Phase 2 (Virtual Signal Creator): standard LLM with bwave agent capabilities identifies
    branch/expression conditions and tests each via virtual signals.
    Runs in parallel with Phase 3 when both are needed.
  Phase 3 (FSM Identifier): light LLM (no agent capabilities) identifies FSM registers and
    expected states from RTL source. Runs in parallel with Phase 2.
  Phase 4 (Coverage Reviewer): standard LLM (no agent capabilities) receives ALL data from
    prior phases. Applies toggle waivers and value classifications with full
    context. Unclassified signals default to "insufficient".
  Phase 5 (Scoring): deterministic Python computes per-criterion percentages,
    validates waiver names, and applies errored-branch threshold.

Exit codes:
  0 — all active criteria pass their thresholds
  1 — one or more criteria below threshold
  2 — error (infrastructure failure, bwave unavailable, etc.)
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from booley.bwave.contract import EXIT_USAGE, NO_MATCH_MARKER
from booley.config.project_config import lookup_target_section
from booley.core.boundary import as_int
from booley.core.models import AgentCallParams
from booley.dev_support.workspace_isolation import hide_opposite_sources
from booley.flows import edam as edam_layer
from booley.flows.sim import edam as sim_edam
from booley.flows.target_campaign import (
    TargetCampaign,
    describe_target_campaign,
    resolve_target_campaign,
)
from booley.flows.target_criteria import CampaignScopeError
from booley.flows.target_test_suite import NoRunnableTestsError
from booley.fusesoc import fusesoc_registry
from booley.mcp.base import (
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    McpToolResult,
    read_source_dirs_from_toml,
)
from booley.runtime.paths import native_bwave_binary
from booley.runtime.platform_paths import posix_relpath
from booley.runtime.shared_infra import derive_work_dir
from booley.sim.config import resolve_run_cwd, resolve_trace_args, resolve_trace_files
from booley.sim.trace_recipe import TraceMode
from booley.sim.trace_session import TraceSession, trace_cache_key

from .coverage_verilog_utils import (
    _build_rtl_name_map,  # noqa: F401  # re-exported for backward compatibility
    _canon_value,
    _extract_json_block,
    _find_signal,
    _is_numeric_verilog_literal,  # noqa: F401  # re-exported for backward compatibility
    _resolve_fsm_enum_names,
    _sanitize_fsm_registers,
    _signal_leaf,  # noqa: F401  # re-exported for backward compatibility
)
from .specialist import Specialist

logger = logging.getLogger(__name__)


def _bwave_stats_cmd() -> list[str] | None:
    """Return the `bwave stats --format json` prefix, or None if bwave is absent.

    Resolves the *native* binary, never the bare name: on PATH `bwave` is the
    Python wrapper, which injects query defaults (`--limit 5000`) that would
    silently truncate the stats this analysis scores coverage from.
    """
    found = native_bwave_binary()
    return [str(found), "stats", "--format", "json"] if found else None


def _make_transcript_path(transcript_dir: Path | None, label: str) -> Path | None:
    if transcript_dir is None:
        return None
    transcript_dir.mkdir(parents=True, exist_ok=True)
    return transcript_dir / f"{label}.jsonl"


# Agent capability set — read-only intent, Bash needed for `bwave`
_ANALYST_TOOLS = ["Read", "Grep", "Glob", "Bash"]

# Pre-filter: signals with fewer than this many unique values get sent to waiver specialist
_VALUE_DIVERSITY_THRESHOLD = 4

# Max characters per RTL file in waiver prompt (prevents token overflow)
_RTL_MAX_CHARS = 50_000

# Signals wider than this are excluded from value coverage — impossible to
# achieve sufficient diversity on 128-bit data buses.
_VALUE_MAX_WIDTH = 64


def _configured_testbench_source_dirs(work_dir: Path) -> list[str]:
    """Return configured testbench source dirs for read-boundary prompts."""
    parsed = read_source_dirs_from_toml(work_dir)
    tb_dirs = parsed[1] if parsed else ["tb"]
    from booley.runtime.shared_infra import source_dir_prefixes

    prefixes = source_dir_prefixes(tb_dirs, work_dir)
    return [prefix for prefix in prefixes if "\\" not in prefix] or ["tb/"]


def _extract_stats_signals(data: object) -> list[dict]:
    """Pull the `signals[]` array out of a `bwave stats --format json` payload.

    Handles two shapes:
    - v0.2 envelope: `{"$schema", "command", "data": {"signals": [...]}, ...}`
    - pre-v0.2 bare:  `{"signals": [...]}` or a top-level list.

    The fallbacks remain until we're confident every consumer is running a
    v0.2+ bwave binary. Returns `[]` for unrecognised shapes.
    """
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    inner = data.get("data")
    if isinstance(inner, dict):
        sigs = inner.get("signals")
        if isinstance(sigs, list):
            return sigs
    sigs = data.get("signals")
    return sigs if isinstance(sigs, list) else []


# Minimum remaining seconds to bother starting a costly phase
_MIN_PHASE_BUDGET = 60

# SV keywords that can occasionally appear immediately after a module
# identifier in non-instantiation positions; used by the DUT-instance scan
# to suppress false positives.  Not exhaustive — the scan tolerates a few
# false positives because the worst outcome is a redundant pre-flight
# error that points the developer at real TB instances anyway.
_SV_KEYWORDS = frozenset(
    {
        "if",
        "else",
        "for",
        "while",
        "case",
        "casex",
        "casez",
        "begin",
        "end",
        "module",
        "endmodule",
        "assign",
        "always",
        "always_comb",
        "always_ff",
        "always_latch",
        "initial",
        "function",
        "endfunction",
        "task",
        "endtask",
        "generate",
        "endgenerate",
        "package",
        "endpackage",
        "interface",
        "endinterface",
        "class",
        "endclass",
        "program",
        "endprogram",
        "import",
        "export",
        "typedef",
        "parameter",
        "localparam",
        "logic",
        "wire",
        "reg",
        "input",
        "output",
        "inout",
        "return",
    }
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SignalStats:
    """Parsed bwave --stats output for a single signal."""

    name: str
    transitions: int = 0
    value_hist: dict[str, int] = field(default_factory=dict)
    width: int = 1


@dataclass
class ReviewerResult:
    """Output from the coverage reviewer (Phase 4)."""

    toggle_waivers: list[str] = field(default_factory=list)
    value_classifications: dict[str, str] = field(default_factory=dict)
    value_waivers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    improvement_hints: list[str] = field(default_factory=list)


@dataclass
class PersistentWaivers:
    """Cached Phase 4 reviewer results, reused across re-runs."""

    toggle_waivers: dict[str, str] = field(default_factory=dict)
    value_waivers: dict[str, str] = field(default_factory=dict)
    value_classifications: dict[str, str] = field(default_factory=dict)
    scope_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "toggle_waivers": self.toggle_waivers,
            "value_waivers": self.value_waivers,
            "value_classifications": self.value_classifications,
            "scope_hash": self.scope_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersistentWaivers:
        tw = data.get("toggle_waivers", {})
        vw = data.get("value_waivers", {})
        vc = data.get("value_classifications", {})
        if not isinstance(tw, dict) or not isinstance(vw, dict) or not isinstance(vc, dict):
            raise TypeError("Expected dict fields in PersistentWaivers")
        return cls(
            toggle_waivers=tw,
            value_waivers=vw,
            value_classifications=vc,
            scope_hash=data.get("scope_hash", ""),
        )


def _compute_scope_hash(scope_arg: str, work_dir: Path | None = None) -> str:
    """Content-addressed hash of scope files — delegates to trace_cache_key.

    Unifies trace cache and waiver cache under one invalidation scheme.
    Normalizes scope arg (sort, strip, dedup) for order/whitespace insensitivity.
    """
    files = sorted(f.strip() for f in scope_arg.split(",") if f.strip())
    normalized_scope = ",".join(files)
    source_paths = []
    if work_dir:
        for f in files:
            p = work_dir / f
            if p.exists():
                source_paths.append(p)
    return trace_cache_key(source_paths, scope=normalized_scope)


@dataclass
class FsmResult:
    """Output from the FSM identifier (Phase 3)."""

    fsm_registers: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BranchResult:
    """Single branch/expression result from the branch specialist."""

    name: str
    expr: str
    met: bool = False
    reason: str = ""
    errored: bool = False
    error_msg: str = ""


@dataclass
class CoverageReport:
    """Final coverage report across all criteria types."""

    # Phase 1 raw data
    signal_stats: list[SignalStats] = field(default_factory=list)

    # Phase 2+3 outputs
    branch_results: list[BranchResult] = field(default_factory=list)
    expression_results: list[BranchResult] = field(default_factory=list)
    fsm_registers: list[dict[str, Any]] = field(default_factory=list)

    # Structural noise (excluded before scoring)
    structural_noise: list[SignalStats] = field(default_factory=list)

    # Phase 4 outputs
    toggle_waivers: list[str] = field(default_factory=list)
    value_classifications: dict[str, str] = field(default_factory=dict)
    value_waivers: list[str] = field(default_factory=list)
    reviewer_notes: list[str] = field(default_factory=list)
    improvement_hints: list[str] = field(default_factory=list)

    def toggle_score(self) -> dict[str, Any]:
        """Compute toggle coverage: (toggled + waived) / total."""
        total_signals = self.signal_stats
        if not total_signals:
            return {"pct": None, "met": 0, "total": 0, "waived": 0, "missed": []}

        toggled = [s for s in total_signals if s.transitions >= 2]
        waived_set = set(self.toggle_waivers)
        waived_count = sum(1 for s in total_signals if s.name in waived_set and s.transitions < 2)
        missed = [s.name for s in total_signals if s.transitions < 2 and s.name not in waived_set]

        total = len(total_signals)
        met_count = len(toggled) + waived_count
        pct = (met_count / total * 100) if total > 0 else 100.0
        return {
            "pct": round(pct, 1),
            "met": met_count,
            "total": total,
            "waived": waived_count,
            "missed": missed,
        }

    def fsm_score(self) -> dict[str, Any]:
        """Compute FSM coverage: visited states / expected states per tagged register."""
        if not self.fsm_registers:
            return {"pct": None, "met": 0, "total": 0, "registers": []}

        total_states = 0
        visited_states = 0
        registers_detail = []

        for reg in self.fsm_registers:
            sig_name = reg["signal"]
            expected = set(reg.get("expected_values", []))
            if not expected:
                continue

            # Find observed values from signal stats (fuzzy: leaf-name fallback).
            # merge_ambiguous=True unions value histograms across sub-instances
            # sharing the same leaf name — correct for module-level FSM coverage.
            observed: set[str] = set()
            match = _find_signal(self.signal_stats, sig_name, merge_ambiguous=True)
            if match is not None:
                observed = {_canon_value(str(k)) for k in match.value_hist}

            expected_canonical = {_canon_value(v) for v in expected}
            visited = expected_canonical & observed
            missing = expected_canonical - observed

            total_states += len(expected_canonical)
            visited_states += len(visited)
            registers_detail.append(
                {
                    "signal": sig_name,
                    "expected": len(expected_canonical),
                    "visited": len(visited),
                    "missing": sorted(missing),
                }
            )

        pct = (visited_states / total_states * 100) if total_states > 0 else 100.0
        return {
            "pct": round(pct, 1),
            "met": visited_states,
            "total": total_states,
            "registers": registers_detail,
        }

    def value_score(self) -> dict[str, Any]:
        """Compute value coverage: (sufficient + waived) / total."""
        if not self.value_classifications:
            return {"pct": None, "sufficient": 0, "total": 0, "waived": 0, "insufficient": []}

        waived_set = set(self.value_waivers)
        total = len(self.value_classifications)
        sufficient_count = sum(1 for v in self.value_classifications.values() if v == "sufficient")
        insufficient_sigs = [
            s for s, v in self.value_classifications.items() if v == "insufficient"
        ]
        waived_count = sum(1 for s in insufficient_sigs if s in waived_set)
        met_count = sufficient_count + waived_count
        insufficient = [s for s in insufficient_sigs if s not in waived_set]

        pct = (met_count / total * 100) if total > 0 else 100.0
        return {
            "pct": round(pct, 1),
            "sufficient": met_count,
            "total": total,
            "waived": waived_count,
            "insufficient": insufficient,
        }

    @staticmethod
    def _condition_score(results: list[BranchResult]) -> dict[str, Any]:
        """Compute condition coverage (branch or expression): met / (total - errored)."""
        if not results:
            return {"pct": None, "met": 0, "total": 0, "errored": 0, "missed": []}

        errored = sum(1 for r in results if r.errored)
        scoreable = [r for r in results if not r.errored]
        total = len(scoreable)
        met_count = sum(1 for r in scoreable if r.met)
        missed = [
            {"name": r.name, "expr": r.expr, "reason": r.reason} for r in scoreable if not r.met
        ]

        pct = (met_count / total * 100) if total > 0 else 100.0
        return {
            "pct": round(pct, 1),
            "met": met_count,
            "total": total,
            "errored": errored,
            "missed": missed,
        }

    def branch_score(self) -> dict[str, Any]:
        return self._condition_score(self.branch_results)

    def expression_score(self) -> dict[str, Any]:
        return self._condition_score(self.expression_results)

    def to_report_dict(self) -> dict[str, Any]:
        """Build the coverage_report.json structure."""
        toggle = self.toggle_score()
        fsm = self.fsm_score()
        value = self.value_score()
        branch = self.branch_score()
        expression = self.expression_score()
        d: dict[str, Any] = {
            "toggle": toggle if toggle["pct"] is not None else None,
            "fsm": fsm if fsm["pct"] is not None else None,
            "value": value if value["pct"] is not None else None,
            "branch": branch if branch["pct"] is not None else None,
            "expression": expression if expression["pct"] is not None else None,
        }
        if self.structural_noise:
            d["structural_noise"] = [s.name for s in self.structural_noise]
        if self.reviewer_notes:
            d["reviewer_notes"] = self.reviewer_notes
        if self.improvement_hints:
            d["improvement_hints"] = self.improvement_hints
        return d


# ---------------------------------------------------------------------------
# Specialist
# ---------------------------------------------------------------------------

_DEFAULT_MIN_TOGGLE = 90
_DEFAULT_MIN_FSM = 100
_DEFAULT_MIN_VALUE = 90
_DEFAULT_MIN_BRANCH = 80
_DEFAULT_MIN_EXPRESSION = 80

# Per-criterion scoring metadata driving the results-line loop in
# ``_build_coverage_result``: ``(criterion_key, display_label, score_key,
# na_reason)``. ``score_key`` indexes the scores/min/passes/errored_fail dicts
# built by ``_score_coverage_criteria``; ``na_reason`` is the "no data"
# explanation shown when a criterion has no measurable signals. Order matters:
# it fixes the emission order of the scoring report rows.
_COVERAGE_CRITERIA_TABLE = (
    ("coverage_toggle", "TOGGLE", "toggle", "no signals found"),
    ("coverage_fsm", "FSM", "fsm", "no FSM registers found"),
    ("coverage_value", "VALUE", "value", "no signals found"),
    ("coverage_branch", "BRANCH", "branch", "no branch conditions found"),
    ("coverage_expression", "EXPRESSION", "expression", "no expression conditions found"),
)


@dataclass(frozen=True)
class _TraceRunContext:
    """Resolved paths and runtime settings shared by trace-command builders."""

    eda_tool: str
    resolved: fusesoc_registry.ResolvedTarget
    build_dir: str
    trace_dir: Path
    work_dir: Path
    run_timeout: int
    trace_mode: TraceMode


_VSC_PROMPT_TEMPLATE = """You are a virtual signal creator. Your job is to define virtual signals
for RTL branch conditions and test them interactively using bwave.

You MUST NOT read any testbench files ({tb_dirs} or any configured testbench
source directory).

## Step 1: Learn bwave syntax

Run `bwave --help` to learn the syntax for `--virtual` signals.

## Step 2: Read RTL and identify conditions

Read all RTL files in scope and identify top-level if/else conditions:
{scope_str}
{spec_section}
## Available Signals (from Phase 1)

These signals are available in the trace:
{signal_list_str}

{mode_section}
## Target Trace Files

{trace_path_str}

## Step 3: Test Each Expression

For each branch/expression, query every Target trace above with bwave and union
the observed values before deciding coverage:

    bwave --stats --format json --virtual "br_name = *signal >= 'd16" -s br_name TRACE_FILE

A branch is **met** if both 0 and 1 appear across the combined value_hist of
the complete Target test suite.

**Rules:**
- 5 attempts max per expression. If it still errors, mark as "errored" and move on.
- Do NOT waive or excuse missed branches — just report the facts.
- Skip trivial reset/clock conditions.

## Step 4: Output Results

Output a JSON object with your findings:

```json
{{
  "branch_results": [
    {{"name": "br_fifo_full", "expr": "bwave --virtual \\"br_fifo_full = *count >= 'd8\\" -s br_fifo_full", "met": true, "reason": "both 0 and 1 observed"}},
    {{"name": "br_enable", "expr": "bwave --virtual \\"br_enable = *en\\" -s br_enable", "met": false, "reason": "never false (0)"}}
  ],
  "expression_results": [
    {{"name": "expr_a_gt_b", "expr": "bwave --virtual \\"expr_a_gt_b = *a > *b\\" -s expr_a_gt_b", "met": true, "reason": "both values observed"}},
    {{"name": "expr_err", "expr": "...", "met": false, "reason": "", "errored": true, "error_msg": "bwave syntax error after 5 attempts"}}
  ]
}}
```
"""


def _trace_test_plusargs(target: str, test: str | None) -> list[str]:
    """Render the test-selection plusarg for a coverage trace run (decision 16).

    Resolves one internally selected Target test to its declared index and
    renders the tests.toml ``select`` template
    (e.g. ``+test_id=3``) via :func:`project_config.render_test_selector`.
    Returns ``[]`` when no test is named or it doesn't resolve, so the binary
    runs its default test (raw passthrough) — matching the legacy contract.
    """
    if not test:
        return []
    from booley.config.project_config import (
        TEST_NAMES,
        lookup_target_section,
        render_test_selector,
        resolve_test_name,
    )

    try:
        idx = resolve_test_name(target, test)
    except ValueError:
        return []
    names = lookup_target_section(TEST_NAMES, target) or []
    return [render_test_selector(target, idx, names[idx])]


class CoverageAnalystSpecialist(Specialist):
    """Hybrid mechanical + specialist coverage measurement (v3)."""

    name: str = "coverage_analyst"
    description: str = (
        "Measures signal-level coverage via mechanical extraction + LLM specialists "
        "for waivers and branch analysis"
    )
    code_modifying: bool = False
    min_model: str = "standard"
    default_timeout: int = 1200
    min_timeout: int = 600
    satisfies: ClassVar[list[str]] = [
        "coverage_toggle",
        "coverage_fsm",
        "coverage_value",
        "coverage_branch",
        "coverage_expression",
    ]
    satisfies_args: ClassVar[dict[str, str]] = {}
    # Nested-MCP allowlist lives in booley.runtime.nested_mcp_capabilities.

    def _is_cocotb_target(self) -> bool:
        """True when the selected Target declares a ``cocotb_module`` (ADR 0034).

        Cheap ``.core`` read, mirroring simulate's detection: for a Cocotb
        Target the DUT *is* the toplevel, so wrapper-instance hierarchy
        conventions do not apply.
        """
        try:
            modules = fusesoc_registry.target_cocotb_modules(self.args.work_dir)
        except Exception:  # noqa: BLE001 — best-effort cheap read; degrades to non-cocotb
            return False
        return modules.get(self.args.target) is not None

    def _validate_interactive_args(self) -> McpToolResult | None:
        """Reject missing args in Interactive Mode with a clear message.

        The hierarchy discovery and sim invocation assume ``--tb-top`` is
        populated; the sim wrapper requires ``--target``.

        E3 (ADR 0034): for a Cocotb Target the testbench top *is* the
        Target's ``toplevel`` (DUT-as-toplevel), so
        ``--tb-top`` is not required — it defaults from the ``.core``.
        """
        if not getattr(self.args, "tb_top", None) and self._is_cocotb_target():
            from booley.flows.flow_config import tb_top_for_target

            try:
                self.args.tb_top = tb_top_for_target(
                    self.args.target,
                    self.args.work_dir,
                    resolved=None,
                )
            except Exception:  # noqa: BLE001 — fall through to the hard requirement
                logger.debug("cocotb tb_top default failed", exc_info=True)
        if not getattr(self.args, "tb_top", None):
            return McpToolResult(
                exit_code=EXIT_FAILURE,
                report_text=(
                    "coverage_analyst: --tb-top is required when running "
                    "outside a ticket (testbench top module name, e.g. 'tb')."
                ),
            )
        if not getattr(self.args, "target", "").strip():
            return McpToolResult(
                exit_code=EXIT_FAILURE,
                report_text=(
                    "coverage_analyst: --target is required. Pass --target "
                    "<name>; it names a Target in the project's .core file "
                    "(list them with `booley targets`)."
                ),
            )
        return None

    def _add_agent_args(self, parser) -> None:
        parser.add_argument(
            "--tb-top",
            default=None,
            help="Testbench top module name. Defaults to the resolved sim Target's toplevel.",
        )
        parser.add_argument(
            "--scope",
            default=None,
            help="Comma-separated RTL files; defaults from this Target's criteria",
        )
        # --hierarchy-scope removed: auto-derived glob always used to prevent
        # developer from narrowing scope to trivially pass coverage.
        parser.add_argument(
            "--criteria",
            default=None,
            help="Comma-separated coverage types to evaluate (toggle,fsm,value,branch,expression). "
            "Use to re-run only failed types.",
        )
        parser.add_argument(
            "--reset-waivers",
            action="store_true",
            default=False,
            help="Discard cached waivers from previous runs and re-evaluate all signals from scratch",
        )

    # _agent_lock initialized per-instance in _run() to avoid serializing concurrent runs

    def _invoke_agent(self, params: AgentCallParams, on_event: Any = None) -> Any:
        from .specialist import _call_agent_sync

        if on_event is None:
            on_event = self._make_streaming_callback()
        result = _call_agent_sync(params, on_event=on_event)
        with self._agent_lock:
            self._total_input_tokens += getattr(result, "input_tokens", 0)
            self._total_output_tokens += getattr(result, "output_tokens", 0)
            self._total_cached_tokens += getattr(result, "cached_tokens", 0)
            self._total_cache_create_tokens += getattr(result, "cache_create_tokens", 0)
            self._total_cost_usd += getattr(result, "cost_usd", 0.0)
            self._last_session_id = getattr(result, "session_id", None)
        return result

    # --- Abstract method stubs (not used — _run() drives phases directly) ---

    def _build_prompt(self) -> str:
        raise NotImplementedError("CoverageAnalystSpecialist overrides _run() directly")

    def _interpret_output(self, output: str, structured: dict | None) -> McpToolResult:
        raise NotImplementedError("CoverageAnalystSpecialist overrides _run() directly")

    # --- Scope-to-hierarchy mapping (Step 2) ---

    def _derive_hierarchy_glob(self) -> str:
        """Build broad module-based globs; trace discovery narrows them."""
        modules = self._scope_modules()
        if not modules:
            return "*"
        return ",".join(f"*{module}.*" for module in modules)

    @staticmethod
    def _extract_modules_from_scope(scope: str) -> list[str]:
        """Extract deduplicated module stems from a --scope string."""
        modules = []
        for f in (s.strip() for s in scope.split(",") if s.strip()):
            stem = Path(f).stem.replace(".", "_")
            if stem not in modules:
                modules.append(stem)
        return modules

    def _scope_modules(self) -> list[str]:
        """Read actual module declarations from scope, falling back to stems."""
        modules: list[str] = []
        module_re = re.compile(r"\bmodule\s+(?:automatic\s+)?([A-Za-z_]\w*)\b")
        for raw_path in self.args.scope.split(","):
            rel_path = raw_path.strip()
            if not rel_path:
                continue
            try:
                text = (Path(getattr(self.args, "work_dir", ".")) / rel_path).read_text(
                    encoding="utf-8-sig",
                    errors="replace",
                )
            except OSError:
                continue
            for module in module_re.findall(self._strip_sv_comments(text)):
                if module not in modules:
                    modules.append(module)
        return modules or self._extract_modules_from_scope(self.args.scope)

    @staticmethod
    def _find_unrepresented_modules(
        stats: list[SignalStats],
        modules: list[str],
    ) -> list[str]:
        """Return module stems with no matching signals in *stats*.

        A module is "represented" if any signal's parent hierarchy has a
        component ending with the module stem (same heuristic as the
        suffix-anchored glob).
        """
        missing = []
        for mod in modules:
            found = any(
                any(p.endswith(mod) for p in s.name.rsplit(".", 1)[0].split("."))
                for s in stats
                if "." in s.name
            )
            if not found:
                missing.append(mod)
        return missing

    _GENERIC_DUT_NAMES = frozenset({"uut", "dut", "UUT", "DUT"})

    def _trace_parent_scopes(self, trace_file: Path) -> dict[str, int]:
        """Run ``bwave stats -s "*"`` and tally unique parent (instance) scopes.

        Each signal's leaf name is stripped to its parent scope; the returned
        dict maps ``scope -> signal count``.  Empty dict on any failure
        (bwave missing/timeout/non-zero, bad JSON, no signals/scopes).
        """
        prefix = _bwave_stats_cmd()
        if prefix is None:
            return {}
        cmd = [*prefix, "-s", "*", str(trace_file)]
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.args.work_dir,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {}
        if proc.returncode != 0:
            return {}

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {}

        signals = _extract_stats_signals(data)
        if not signals:
            return {}

        # Build set of unique parent scopes (strip leaf signal name)
        scopes: dict[str, int] = {}
        for sig in signals:
            name = sig.get("name", sig.get("path", ""))
            name = re.sub(r"\[.*\]$", "", name)
            if "." in name:
                scope = name.rsplit(".", 1)[0]
                scopes[scope] = scopes.get(scope, 0) + 1
        return scopes

    def _discover_dut_scope(
        self,
        trace_file: Path,
        modules: list[str],
    ) -> str | None:
        """Search the trace for the DUT hierarchy when the suffix-anchored
        glob missed.  Returns a bwave glob like ``tb.uu_alu.*`` or None.

        Strategy: run ``--stats --format json -s "*"`` to
        get every signal, extract unique instance scopes, then score each
        scope against the target module stems.
        """
        scopes = self._trace_parent_scopes(trace_file)
        if not scopes:
            return None

        discovered: set[str] = set()
        for stem in modules:
            # Pass 1: scope component ends with the module stem
            # e.g. stem="aes128_encrypt" matches scope "tb.uu_aes128_encrypt"
            stem_candidates = []
            for scope, count in scopes.items():
                parts = scope.split(".")
                if any(p.endswith(stem) for p in parts):
                    stem_candidates.append((scope, count))

            # Pass 2: generic instance names (uut/dut) under a testbench
            # whose name contains the stem or is an abbreviation of it
            # (e.g. tb_aes128_dec → aes128_dec is a prefix of aes128_decrypt)
            generic_candidates = []
            for scope, count in scopes.items():
                parts = scope.split(".")
                has_stem_parent = any(self._stem_matches_component(stem, p) for p in parts)
                has_generic_leaf = parts[-1] in self._GENERIC_DUT_NAMES
                if has_stem_parent and has_generic_leaf:
                    generic_candidates.append((scope, count))

            # A generic child is stronger evidence than its matching TB parent.
            # Keep every such child, but do not accidentally analyze the wrapper
            # merely because its name also contains the module stem.
            candidates = generic_candidates or stem_candidates
            discovered.update(scope for scope, _count in candidates)

        if not discovered:
            return None
        # Analyze every validated matching instance. Selecting a single path
        # would make coverage depend on arbitrary hierarchy ordering.
        return ",".join(f"{scope}.*" for scope in sorted(discovered))

    @staticmethod
    def _stem_matches_component(stem: str, component: str) -> bool:
        """Check if a module stem matches a hierarchy component.

        Matches on: substring containment (original behavior), or
        prefix/abbreviation after stripping tb_ prefix (handles
        e.g. tb_aes128_dec matching module aes128_decrypt).
        """
        if stem in component:
            return True
        bare = component
        if bare.startswith("tb_"):
            bare = bare[3:]
        elif bare.endswith("_tb"):
            bare = bare[:-3]
        if len(bare) < 4 or len(stem) < 4:
            return False
        shorter, longer = (bare, stem) if len(bare) <= len(stem) else (stem, bare)
        return longer.startswith(shorter) and len(shorter) >= len(longer) * 0.6

    @staticmethod
    def _pick_dut_scope(candidates: list[tuple[str, int]]) -> str | None:
        """From candidate (scope, signal_count) pairs, pick the DUT.

        Filters out testbench-level scopes and picks the deepest scope
        with the most signals.
        """
        non_tb = [
            (s, c)
            for s, c in candidates
            if not (
                s.split(".")[-1].endswith("_tb")
                or s.split(".")[-1].startswith("tb_")
                or s.split(".")[-1].startswith("tb")
            )
        ]
        pool = non_tb if non_tb else candidates
        # Prefer deepest hierarchy (most dots), then most signals
        pool.sort(key=lambda sc: (sc[0].count("."), sc[1]), reverse=True)
        return pool[0][0] if pool else None

    def _available_top_scopes(self, trace_file: Path) -> list[str]:
        """Return top-level scope names from the trace for error messages."""
        prefix = _bwave_stats_cmd()
        if prefix is None:
            return []
        cmd = [*prefix, "-s", "*", str(trace_file)]
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.args.work_dir,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
        if proc.returncode != 0:
            return []
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []
        signals = _extract_stats_signals(data)
        tops: set[str] = set()
        for sig in signals:
            name = sig.get("name", sig.get("path", ""))
            if "." in name:
                tops.add(name.split(".")[0])
        return sorted(tops)

    # --- Phase 1: Mechanical measurement (Step 3) ---

    def _run_bwave_stats(
        self,
        trace_file: Path,
        hierarchy_glob: str,
    ) -> tuple[list[SignalStats], str | None, bool]:
        """Single bwave --stats invocation.  Returns (stats, err, is_infra)."""
        prefix = _bwave_stats_cmd()
        if prefix is None:
            logger.error("bwave binary not found")
            return [], "bwave binary not found", True
        cmd = list(prefix)
        for raw_pat in hierarchy_glob.split(","):
            pat = raw_pat.strip()
            if pat:
                cmd.extend(["-s", pat])
        cmd.append(str(trace_file))
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.args.work_dir,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.error("bwave stats timed out (120s)")
            return [], "bwave stats timed out (120s)", True
        except FileNotFoundError:
            logger.error("bwave binary not found")
            return [], "bwave binary not found", True

        if proc.returncode != 0:
            stderr_text = proc.stderr.strip()
            # bwave exits EXIT_USAGE when every -s pattern misses (loud-fail
            # contract). For this caller an all-miss is not a bwave failure —
            # it is the Stage-1 "wrong hierarchy glob" outcome that the
            # discovery fallback in _run_mechanical_measurement exists to
            # recover from, so report it as plain empty stats and let the
            # fallback run. Code and marker are pinned cross-process in
            # bwave.contract / test_contract.
            if proc.returncode == EXIT_USAGE and NO_MATCH_MARKER in stderr_text.lower():
                logger.info("bwave stats: no signals match glob '%s'", hierarchy_glob)
                return [], None, False
            msg = f"bwave stats failed (rc={proc.returncode}): {stderr_text}"
            logger.error(msg)
            return [], msg, False

        stats = self._parse_bwave_stats(proc.stdout)
        return stats, None, False

    def _run_mechanical_measurement(
        self, trace_dir: Path
    ) -> tuple[list[SignalStats], str | None, bool]:
        """Run bwave stats to extract signal statistics mechanically.

        Three-stage hierarchy resolution:
          1. Suffix-anchored glob (*{stem}.*) — catches prefixed instances.
          2. Discovery fallback — searches trace for DUT hierarchy.
          3. Graceful failure with available scopes listed.

        Returns (stats, error_message, is_infra). is_infra=True for
        infrastructure failures (timeout, missing binary) that warrant exit-2;
        False for recoverable failures (no trace, wrong scope) that should
        be exit-1 so the developer can adjust and retry.
        """
        trace_file = self._find_trace_file(trace_dir)
        if trace_file is None:
            return [], f"No trace file found in {trace_dir}", False

        hierarchy_glob = self._derive_hierarchy_glob()
        stats, err, is_infra = self._run_bwave_stats(trace_file, hierarchy_glob)

        # Infra failure or bwave error — propagate immediately
        if is_infra or err is not None:
            return stats, err, is_infra

        # Stage 1 hit — but check for partial matches across multi-module scopes.
        if stats:
            self._fill_missing_module_stats(stats, trace_file)
            return stats, None, False

        # Stage 2: discovery fallback
        modules = self._scope_modules()
        if modules:
            discovered = self._discover_dut_scope(trace_file, modules)
            if discovered:
                logger.info("Hierarchy discovery found DUT scope: %s", discovered)
                stats, err, is_infra = self._run_bwave_stats(trace_file, discovered)
                if stats:
                    return stats, None, False

        # Stage 3: graceful failure with actionable diagnostics
        top_scopes = self._available_top_scopes(trace_file)
        scope_hint = f" Available top-level scopes: {', '.join(top_scopes)}" if top_scopes else ""
        msg = (
            f"No signals matched hierarchy glob '{hierarchy_glob}' "
            f"and discovery fallback found no DUT instance for "
            f"scope '{self.args.scope}'.{scope_hint}"
        )
        return [], msg, False

    def _fill_missing_module_stats(self, stats: list[SignalStats], trace_file: Path) -> None:
        """Discovery-fill stats for multi-module scopes where some modules had no match."""
        modules = self._scope_modules()
        if len(modules) <= 1:
            return
        missing = self._find_unrepresented_modules(stats, modules)
        for mod in missing:
            discovered = self._discover_dut_scope(trace_file, [mod])
            if discovered:
                logger.info("Partial match: discovery found scope for %s: %s", mod, discovered)
                extra, extra_err, _ = self._run_bwave_stats(trace_file, discovered)
                if extra and not extra_err:
                    stats.extend(extra)

    @staticmethod
    def _parse_bwave_stats(stdout: str) -> list[SignalStats]:
        """Parse bwave --stats JSON output into SignalStats list."""
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse bwave stats JSON: %s", exc)
            return []

        signals = _extract_stats_signals(data)
        results = []
        for sig in signals:
            if not isinstance(sig, dict):
                continue
            results.append(
                SignalStats(
                    name=sig.get("name", sig.get("path", "")),
                    transitions=sig.get("transitions", 0),
                    value_hist=sig.get("value_hist", {}),
                    width=sig.get("width", 1),
                )
            )
        return results

    def _pre_filter_for_waiver(
        self, stats: list[SignalStats]
    ) -> tuple[list[SignalStats], list[SignalStats]]:
        """Pre-filter signals for waiver specialist input.

        Returns (toggle_failures, low_value_diversity).
        Signals wider than _VALUE_MAX_WIDTH are excluded from value coverage.
        """
        toggle_failures = [s for s in stats if s.transitions < 2]
        low_diversity = [
            s
            for s in stats
            if len(s.value_hist) < _VALUE_DIVERSITY_THRESHOLD and s.width <= _VALUE_MAX_WIDTH
        ]
        return toggle_failures, low_diversity

    # --- Structural noise filter (simulator-dependent) ---

    _PARAM_RE = re.compile(r"\b(?:parameter|localparam)\s+(?:\w+\s+)?(?:\[.*?\]\s+)?(\w+)")
    _GENVAR_DECL_RE = re.compile(r"\bgenvar\s+([^;]+)")
    # [N]. in the hierarchy = generate-array instance (not a leaf bit-select)
    _GENERATE_SCOPE_RE = re.compile(r"\[\d+\]\.")

    def _filter_structural_noise(
        self,
        stats: list[SignalStats],
        scope_files: list[str],
    ) -> tuple[list[SignalStats], list[SignalStats]]:
        """Remove simulator instrumentation artifacts from coverage stats.

        Returns (filtered_stats, excluded_signals).
        """
        work_dir = Path(self.args.work_dir)
        # ADR 0022 decision 8: the run-half family comes from the Target's EDA tool
        # (read cheaply from the .core), not the boundary-named backend.
        eda_tool = sim_edam.normalize_eda_tool(
            fusesoc_registry.target_eda_tools(work_dir).get(self.args.target)
        )

        # Collect all design RTL files: scope file directories may contain
        # submodule sources whose params/localparams must also be filtered.
        # TODO: replace directory glob with elaboration manifest once we have
        # proper RTL hierarchy parsing (e.g. from Verilator's V*__Syms.h).
        design_files: set[Path] = set()
        for f in scope_files:
            parent = (work_dir / f).parent
            if parent.is_dir():
                design_files.update(parent.glob("*.sv"))
                design_files.update(parent.glob("*.svh"))
                design_files.update(parent.glob("*.v"))
                design_files.update(parent.glob("*.vh"))

        param_names: set[str] = set()
        genvar_names: set[str] = set()
        for full_path in design_files:
            try:
                content = full_path.read_text(encoding="utf-8")
                param_names.update(self._PARAM_RE.findall(content))
                for decl in self._GENVAR_DECL_RE.findall(content):
                    genvar_names.update(w.strip() for w in decl.split(",") if w.strip())
            except OSError:
                pass

        decl_names = param_names | genvar_names
        kept: list[SignalStats] = []
        excluded: list[SignalStats] = []
        for s in stats:
            leaf_name = s.name.rsplit(".", 1)[-1] if "." in s.name else s.name
            # Strip bit-select suffix for leaf comparison
            bracket = leaf_name.find("[")
            if bracket != -1:
                leaf_name = leaf_name[:bracket]

            is_ivl_artifact = eda_tool == "icarus" and "$ivl_for_loop" in s.name
            is_declared_const = (
                s.transitions <= 1
                and leaf_name in decl_names
                and (leaf_name in genvar_names or leaf_name == leaf_name.upper())
            )
            is_generate_scope = (
                eda_tool == "icarus"
                and s.transitions <= 1
                and len(s.value_hist) <= 1
                and self._GENERATE_SCOPE_RE.search(s.name)
            )

            if is_ivl_artifact or is_declared_const or is_generate_scope:
                excluded.append(s)
            else:
                kept.append(s)

        return kept, excluded

    def _resolve_light_model(self) -> str:
        """Resolve to the light (Sonnet-class) model for cheap specialist calls."""
        try:
            from booley.config.settings import get_backend_config

            cfg = get_backend_config()
            return cfg.model_for_tier("light")
        except (ImportError, AttributeError):
            return "claude-sonnet-5"

    def _get_active_criteria(self) -> set[str]:
        """Determine which coverage criteria are active for this run."""
        active = {criterion.base_key for criterion in self._campaign_for_tests().criteria}
        # If nothing in state, assume all are active
        active = active if active else set(self.satisfies)

        # Intersect with --criteria CLI filter if provided
        cli_criteria = getattr(self.args, "criteria", None)
        if cli_criteria:
            _valid_names = {"toggle", "fsm", "value", "branch", "expression"}
            requested = set()
            for raw_name in cli_criteria.split(","):
                name = raw_name.strip()
                if name in _valid_names:
                    requested.add(f"coverage_{name}")
                elif name:
                    logger.warning("Ignoring unknown --criteria value: %s", name)
            if requested:
                active = active & requested

        return active

    def _target_criterion_key(self, base_key: str) -> str:
        """Return the criterion key owned by this invocation's Target."""
        return f"{base_key}_{self.args.target}"

    # --- RTL source cache (shared by phases 3 + 4) ---

    def _read_rtl_sources(self) -> str:
        """Read RTL files in scope and return formatted context string.

        Cached: call once in _run() and pass to prompt builders.
        """
        scope_files = [f.strip() for f in self.args.scope.split(",") if f.strip()]
        rtl_context = ""
        for f in scope_files[:3]:
            full_path = Path(self.args.work_dir) / f
            if full_path.exists():
                try:
                    content = full_path.read_text(encoding="utf-8")
                    if len(content) > _RTL_MAX_CHARS:
                        content = content[:_RTL_MAX_CHARS] + "\n... (truncated)"
                    rtl_context += f"\n### {f}\n```systemverilog\n{content}\n```\n"
                except OSError:
                    pass
        return rtl_context

    # --- Phase 2: Virtual Signal Creator ---

    def _run_virtual_signal_creator(
        self,
        work_dir: Path,
        trace_dir: Path,
        signal_stats: list[SignalStats],
        active_criteria: set[str],
        timeout_seconds: int | None = None,
    ) -> tuple[list[BranchResult], list[BranchResult]]:
        """Phase 2: identify branch/expression conditions and test them via bwave."""
        need_branch = "coverage_branch" in active_criteria
        need_expression = "coverage_expression" in active_criteria

        if not need_branch and not need_expression:
            return [], []

        model = self._resolve_model()
        prompt = self._build_vsc_prompt(trace_dir, signal_stats, need_branch, need_expression)

        logger.info(
            "Phase 2: Virtual signal creator (branch=%s, expression=%s)",
            need_branch,
            need_expression,
        )

        try:
            with hide_opposite_sources(work_dir, "rtl"):
                result = self._invoke_agent(
                    AgentCallParams(
                        prompt=prompt,
                        model=model,
                        cwd=work_dir,
                        allowed_agent_capabilities=_ANALYST_TOOLS,
                        system_prompt=None,
                        output_format=None,
                        max_turns=self.args.max_turns,
                        timeout_seconds=timeout_seconds or int(self.args.timeout * 0.7),
                        transcript_path=_make_transcript_path(
                            self.args.transcript_dir,
                            "virtual_signal_creator",
                        ),
                        label="virtual_signal_creator",
                        needs_skills=False,
                    )
                )
        except Exception:
            logger.exception("Virtual signal creator failed")
            self._phase_errors.update({"coverage_branch", "coverage_expression"})
            return [], []

        raw = result.output if hasattr(result, "output") else str(result)
        # Fail closed on unparseable output — same treatment as an agent
        # exception. Otherwise a sub-agent that reports via a native agent-capability call
        # (or emits prose) yields empty results that silently score as PASS.
        if _extract_json_block(raw) is None:
            logger.error(
                "Virtual signal creator produced no parseable JSON — "
                "failing branch/expression coverage closed",
            )
            self._phase_errors.update({"coverage_branch", "coverage_expression"})
            return [], []
        return self._parse_vsc_output(raw, need_branch, need_expression)

    @staticmethod
    def _vsc_mode_section(need_branch: bool, need_expression: bool) -> str:
        """VSC prompt: the mode header (branch+expr / branch-only / expr-only)."""
        if need_branch and need_expression:
            return """## Mode: Branch + Expression

For each branch condition, define a virtual signal AND decompose into sub-expressions.
- Branch: the top-level if/else condition as a single virtual signal
- Expression: atomic sub-expressions within that condition (e.g., `a && b` → test `a` and `b` separately)
"""
        if need_branch:
            return """## Mode: Branch Only

For each top-level if/else condition in the RTL, define a virtual signal.
"""
        return """## Mode: Expression Only

For each branch condition, decompose into atomic sub-expressions and test each.
"""

    def _build_vsc_prompt(
        self,
        trace_dir: Path,
        signal_stats: list[SignalStats],
        need_branch: bool,
        need_expression: bool,
    ) -> str:
        """Build prompt for virtual signal creator (Phase 2)."""
        scope_files = [f.strip() for f in self.args.scope.split(",") if f.strip()]
        scope_str = "\n".join(f"- `{f}`" for f in scope_files)
        trace_dirs = getattr(self, "_trace_dirs", [trace_dir])
        trace_files = [
            trace_file
            for candidate in trace_dirs
            if (trace_file := self._find_trace_file(candidate)) is not None
        ]
        trace_paths = "\n".join(f"- `{path}`" for path in trace_files) or "- `<trace_file>`"
        instruction = getattr(self.args, "instruction", "")
        spec_section = f"\n## Spec / Context\n\n{instruction}\n" if instruction else ""
        tb_dirs = ", ".join(_configured_testbench_source_dirs(self.args.work_dir))
        signal_lines = [f"  - `{s.name}` (width: {s.width})" for s in signal_stats[:100]]
        if len(signal_stats) > 100:
            signal_lines.append(f"  ... and {len(signal_stats) - 100} more")
        return _VSC_PROMPT_TEMPLATE.format(
            tb_dirs=tb_dirs,
            scope_str=scope_str,
            spec_section=spec_section,
            signal_list_str="\n".join(signal_lines),
            mode_section=self._vsc_mode_section(need_branch, need_expression),
            trace_path_str=trace_paths,
        )

    @staticmethod
    def _parse_vsc_output(
        raw: str,
        need_branch: bool,
        need_expression: bool,
    ) -> tuple[list[BranchResult], list[BranchResult]]:
        """Parse virtual signal creator output — same format as branch specialist."""
        data = _extract_json_block(raw)
        if data is None:
            logger.warning("Failed to parse virtual signal creator output")
            return [], []

        branch_results = []
        if need_branch:
            for item in data.get("branch_results", []):
                if isinstance(item, dict):
                    branch_results.append(
                        BranchResult(
                            name=item.get("name", ""),
                            expr=item.get("expr", ""),
                            met=item.get("met", False),
                            reason=item.get("reason", ""),
                            errored=item.get("errored", False),
                            error_msg=item.get("error_msg", ""),
                        )
                    )

        expression_results = []
        if need_expression:
            for item in data.get("expression_results", []):
                if isinstance(item, dict):
                    expression_results.append(
                        BranchResult(
                            name=item.get("name", ""),
                            expr=item.get("expr", ""),
                            met=item.get("met", False),
                            reason=item.get("reason", ""),
                            errored=item.get("errored", False),
                            error_msg=item.get("error_msg", ""),
                        )
                    )

        return branch_results, expression_results

    # --- Phase 3: FSM Identifier ---

    def _run_fsm_identifier(
        self,
        work_dir: Path,
        rtl_context: str,
        timeout_seconds: int = 120,
    ) -> FsmResult:
        """Phase 3: identify FSM registers and expected states from RTL."""
        model = self._resolve_light_model()
        prompt = self._build_fsm_prompt(rtl_context)

        logger.info("Phase 3: FSM identifier")

        try:
            result = self._invoke_agent(
                AgentCallParams(
                    prompt=prompt,
                    model=model,
                    cwd=work_dir,
                    allowed_agent_capabilities=[],
                    system_prompt=None,
                    output_format=None,
                    max_turns=1,
                    timeout_seconds=timeout_seconds,
                    transcript_path=_make_transcript_path(
                        self.args.transcript_dir,
                        "fsm_identifier",
                    ),
                    label="fsm_identifier",
                    needs_skills=False,
                )
            )
        except Exception:
            logger.exception("FSM identifier failed")
            self._phase_errors.add("coverage_fsm")
            return FsmResult()

        raw = result.output if hasattr(result, "output") else str(result)
        if _extract_json_block(raw) is None:
            logger.error(
                "FSM identifier produced no parseable JSON — failing FSM coverage closed",
            )
            self._phase_errors.add("coverage_fsm")
            return FsmResult()
        return self._parse_fsm_output(raw)

    def _build_fsm_prompt(self, rtl_context: str) -> str:
        """Build prompt for FSM identifier — RTL embedded, no agent capabilities needed."""
        return f"""You are an FSM identification specialist. Analyze the RTL source below and
identify all finite state machine (FSM) registers and their expected state values.

## RTL Source
{rtl_context}

## Task

Find all FSM state registers by looking for:
- Registers used in case/casez/casex statements
- Registers with enum or localparam-defined state values
- Registers that follow state-machine patterns (next-state logic)

For each FSM register, list ALL expected state values from the RTL (case labels,
localparam definitions, enum values).

## Output Format (MANDATORY)

Respond with ONLY a JSON object:
```json
{{
  "fsm_registers": [
    {{"signal": "state_reg_name", "expected_values": ["'d0", "'d1", "'d2"]}},
    {{"signal": "another_fsm", "expected_values": ["'d0", "'d1", "'d2"]}}
  ]
}}
```

Rules:
- Use the register name as it appears in the RTL (no hierarchy prefix)
- List ALL expected states from case statements and localparams
- ALWAYS use Verilog numeric literals ('d, 'h, 'b) for expected_values, even when
  the RTL uses enum names or localparam names — resolve them to their numeric values
  (e.g. if `localparam IDLE = 2'd0;` then use "'d0", NOT "IDLE")
- If no FSM registers are found, return {{"fsm_registers": []}}
"""

    @staticmethod
    def _parse_fsm_output(raw: str) -> FsmResult:
        """Parse FSM identifier JSON output.

        Boundary (Principle 5): the LLM controls the shape, so every register
        is normalized to a dict with a string ``signal`` and a list of string
        ``expected_values`` here. Downstream scoring (`fsm_score`,
        `_resolve_fsm_enum_names`) then trusts these types unconditionally.
        """
        data = _extract_json_block(raw)
        if data is None:
            logger.warning("Failed to parse FSM identifier output")
            return FsmResult()

        return FsmResult(
            fsm_registers=_sanitize_fsm_registers(data.get("fsm_registers")),
        )

    # --- Phase 4: Coverage Reviewer ---

    _COVERAGE_REVIEWER_SESSION_KEY = "coverage_reviewer"

    def _run_coverage_reviewer(
        self,
        toggle_failures: list[SignalStats],
        low_diversity: list[SignalStats],
        branch_results: list[BranchResult],
        expression_results: list[BranchResult],
        fsm_result: FsmResult,
        work_dir: Path,
        rtl_context: str,
        timeout_seconds: int = 180,
        active_criteria: set[str] | None = None,
        *,
        resume_session_id: str | None = None,
    ) -> ReviewerResult:
        """Phase 4: review all results with full context, apply waivers and classifications.

        When *resume_session_id* is provided the agent resumes its prior
        conversation and receives only the new residual signals.
        """
        model = self._resolve_model()

        if resume_session_id is not None:
            prompt = self._build_reviewer_resume_prompt(
                toggle_failures,
                low_diversity,
                active_criteria=active_criteria,
            )
        else:
            prompt = self._build_reviewer_prompt(
                toggle_failures,
                low_diversity,
                branch_results,
                expression_results,
                fsm_result,
                rtl_context,
                active_criteria=active_criteria,
            )

        logger.info(
            "Phase 4: Coverage reviewer (%d toggle failures, %d low-diversity signals, resume=%s)",
            len(toggle_failures),
            len(low_diversity),
            resume_session_id is not None,
        )

        params = AgentCallParams(
            prompt=prompt,
            model=model,
            cwd=work_dir,
            allowed_agent_capabilities=[],
            system_prompt=None,
            output_format=None,
            max_turns=1,
            timeout_seconds=timeout_seconds,
            transcript_path=_make_transcript_path(
                self.args.transcript_dir,
                "coverage_reviewer",
            ),
            label="coverage_reviewer",
            needs_skills=False,
        )
        if resume_session_id is not None:
            params = self._build_resume_params(params, resume_session_id)

        try:
            result = self._invoke_agent_with_resume(params)
        except Exception:
            logger.exception("Coverage reviewer failed")
            self._phase_errors.add("coverage_value")
            return ReviewerResult()

        self._persist_session_id(self._COVERAGE_REVIEWER_SESSION_KEY)

        raw = result.output if hasattr(result, "output") else str(result)
        if _extract_json_block(raw) is None:
            logger.error(
                "Coverage reviewer produced no parseable JSON — failing value coverage closed",
            )
            self._phase_errors.add("coverage_value")
            return ReviewerResult()
        return self._parse_reviewer_output(raw)

    def _build_reviewer_resume_prompt(
        self,
        toggle_failures: list[SignalStats],
        low_diversity: list[SignalStats],
        *,
        active_criteria: set[str] | None = None,
    ) -> str:
        """Build a compact prompt for resumed coverage reviewer sessions.

        The agent already has RTL context and branch/FSM data from the prior
        session — only the new residual signals are sent.
        """
        sections: list[str] = [
            "## Additional Signals for Waiver Evaluation",
            "",
            "Evaluate these additional signals for waiver eligibility, "
            "applying the same reasoning and criteria as your prior decisions.",
            "",
        ]
        if toggle_failures:
            sections.append(f"### New Toggle Failures ({len(toggle_failures)})")
            for sig in toggle_failures:
                sections.append(f"  - {sig.name}: toggled={sig.transitions}")
            sections.append("")
        if low_diversity:
            sections.append(f"### New Low-Diversity Signals ({len(low_diversity)})")
            for sig in low_diversity:
                sections.append(f"  - {sig.name}: values={len(sig.value_hist)}")
            sections.append("")
        if active_criteria:
            sections.append(f"Active criteria: {', '.join(sorted(active_criteria))}")
            sections.append("")
        sections.append("""\
Return ONLY valid JSON matching this schema:
```json
{
  "toggle_waivers": ["signal_constant_by_design"],
  "value_classifications": {
    "signal_name": "sufficient",
    "another_signal": "insufficient"
  },
  "value_waivers": ["signal_with_intentionally_low_diversity"],
  "notes": ["Brief explanation of key waiver decisions"],
  "improvement_hints": ["Concrete testbench change to improve coverage"]
}
```

Do not emit prose sections or uppercase headings; the parser reads only the
lowercase JSON keys above.
""")
        return "\n".join(sections)

    @staticmethod
    def _reviewer_toggle_section(
        toggle_failures: list[SignalStats],
        active_criteria: set[str] | None,
    ) -> str:
        """Reviewer prompt: toggle-failure section (empty if toggle not active)."""
        if not (
            toggle_failures and (active_criteria is None or "coverage_toggle" in active_criteria)
        ):
            return ""
        sig_list = "\n".join(
            f"- `{s.name}` (transitions: {s.transitions})" for s in toggle_failures[:50]
        )
        return f"""## Toggle Failures

These signals failed toggle coverage (< 2 transitions). Determine which can be
legitimately waived (constant by design, reset-only, clock-gated, unused outputs, etc.).

Signals:
{sig_list}
"""

    @staticmethod
    def _reviewer_value_section(
        low_diversity: list[SignalStats],
        active_criteria: set[str] | None,
    ) -> str:
        """Reviewer prompt: low-diversity value section (empty if value not active)."""
        if not (
            low_diversity and (active_criteria is None or "coverage_value" in active_criteria)
        ):
            return ""
        sig_list = "\n".join(
            f"- `{s.name}` (unique values: {len(s.value_hist)}, hist: {dict(list(s.value_hist.items())[:8])})"
            for s in low_diversity[:50]
        )
        return f"""## Value Diversity — CLASSIFY EVERY SIGNAL

These signals have low unique value diversity. You MUST classify EVERY signal below
as either "sufficient" or "insufficient". No signal may be left unclassified.

- "sufficient" — limited values are expected by design (enable, flag, small enum)
- "insufficient" — should have more value diversity for proper testing

Signals:
{sig_list}
"""

    @staticmethod
    def _reviewer_branch_section(
        branch_results: list[BranchResult],
        expression_results: list[BranchResult],
    ) -> str:
        """Reviewer prompt: branch/expression results section (empty if none)."""
        if not (branch_results or expression_results):
            return ""
        lines = []
        for br in branch_results:
            status = "MET" if br.met else ("ERRORED" if br.errored else "MISSED")
            lines.append(f"- [BRANCH] `{br.name}`: {status} — {br.reason or br.error_msg}")
        for ex in expression_results:
            status = "MET" if ex.met else ("ERRORED" if ex.errored else "MISSED")
            lines.append(f"- [EXPR] `{ex.name}`: {status} — {ex.reason or ex.error_msg}")
        return f"""## Branch/Expression Results

{chr(10).join(lines)}
"""

    @staticmethod
    def _reviewer_fsm_section(fsm_result: FsmResult) -> str:
        """Reviewer prompt: identified-FSM-registers section (empty if none)."""
        if not fsm_result.fsm_registers:
            return ""
        lines = []
        for reg in fsm_result.fsm_registers:
            lines.append(
                f"- `{reg.get('signal', '?')}`: expected states {reg.get('expected_values', [])}"
            )
        return f"""## FSM Registers Identified

{chr(10).join(lines)}
"""

    def _build_reviewer_prompt(
        self,
        toggle_failures: list[SignalStats],
        low_diversity: list[SignalStats],
        branch_results: list[BranchResult],
        expression_results: list[BranchResult],
        fsm_result: FsmResult,
        rtl_context: str,
        active_criteria: set[str] | None = None,
    ) -> str:
        """Build prompt for coverage reviewer — full context, no agent capabilities."""
        spec_section = ""
        if hasattr(self.args, "instruction") and self.args.instruction:
            spec_section = f"\n## Spec\n{self.args.instruction}\n"

        # Toggle failures section (skip if toggle not active)
        toggle_section = self._reviewer_toggle_section(toggle_failures, active_criteria)
        # Low-diversity value signals section (skip if value not active)
        value_section = self._reviewer_value_section(low_diversity, active_criteria)
        # Branch/expression results section
        branch_section = self._reviewer_branch_section(branch_results, expression_results)
        # FSM results section
        fsm_section = self._reviewer_fsm_section(fsm_result)

        return f"""You are a coverage reviewer. You have full context from all prior phases.
Your job is to apply waivers and value classifications with informed judgment.
{spec_section}
## RTL Source
{rtl_context}

{toggle_section}
{value_section}
{branch_section}
{fsm_section}

## Output Format (MANDATORY)

Respond with ONLY a JSON object:
```json
{{
  "toggle_waivers": ["signal_constant_by_design", "signal_unused"],
  "value_classifications": {{
    "signal_name": "sufficient",
    "another_signal": "insufficient"
  }},
  "value_waivers": ["signal_with_intentionally_low_diversity"],
  "notes": ["Brief explanation of key waiver decisions"],
  "improvement_hints": ["Concrete testbench change to improve coverage"]
}}
```

Rules:
- toggle_waivers: signals that GENUINELY never toggle by design (constants, resets, clock-gated, genvar loop indices)
- Genvar loop indices (i, j, k, etc. from generate-for blocks) are elaboration-time constants — always waive toggle, classify value as "sufficient"
- value_classifications: MUST include an entry for EVERY low-diversity signal listed above
- value_waivers: signals where low value diversity is expected by design (narrow enums, flags)
- Binary control signals (enable, valid, ready) are "sufficient"
- notes: brief explanations of your reasoning for key decisions
- improvement_hints: for any signal/branch/expression that is NOT waived and NOT meeting coverage, \
provide a concrete, actionable testbench modification. Examples: "Drive `cfg_mode` to all 4 enum \
values across separate test phases", "Add a stimulus sequence that triggers the else-branch of \
the overflow check at line 42", "Toggle `chip_select` during an active transaction to cover the \
abort path". Omit this field or leave empty if all criteria are already met.
"""

    @staticmethod
    def _parse_reviewer_output(raw: str) -> ReviewerResult:
        """Parse coverage reviewer JSON output."""
        data = _extract_json_block(raw)
        if data is None:
            logger.warning("Failed to parse coverage reviewer output")
            return ReviewerResult()

        return ReviewerResult(
            toggle_waivers=data.get("toggle_waivers", []),
            value_classifications=data.get("value_classifications", {}),
            value_waivers=data.get("value_waivers", []),
            notes=data.get("notes", []),
            improvement_hints=data.get("improvement_hints", []),
        )

    # --- Phase 5: Scoring ---

    def _set_coverage_criterion(
        self,
        criterion: str,
        passed: bool,
        detail_key: str,
        score: dict,
        *,
        error: str | None = None,
    ) -> None:
        """Record one Target-scoped coverage verdict in development state."""
        detail = {detail_key: score, "target": self.args.target}
        if error is not None:
            detail["error"] = error
        self.set_criterion(
            self._target_criterion_key(criterion),
            passed,
            detail=detail,
            source_target=self.args.target,
        )

    def _emit_criterion_result(
        self,
        active: set[str],
        results_lines: list[str],
        row: tuple[str, str, str, str],
        scored: dict,
    ) -> None:
        """Set one criterion + append its results line (skips inactive criteria).

        ``row`` is one entry of ``_COVERAGE_CRITERIA_TABLE``:
        ``(criterion, label, detail_key, na_reason)``. The dynamic score data
        (score dict, pass verdict, threshold, errored-fail flag) is pulled from
        ``scored`` — the ``_score_coverage_criteria`` result — by ``detail_key``.

        Mirrors the original per-criterion block verbatim: pct → PASS/FAIL row,
        phase-error → ERROR row, otherwise N/A → PASS.  The ``(>50% errored)``
        suffix is used only by branch/expression.
        """
        criterion, label, detail_key, na_reason = row
        if criterion not in active:
            return
        score = scored["scores"][detail_key]
        passed = scored["passes"][detail_key]
        threshold = scored["min"][detail_key]
        errored_fail = scored["errored_fail"].get(detail_key, False)
        if score["pct"] is not None:
            self._set_coverage_criterion(criterion, passed, detail_key, score)
            suffix = " (>50% errored)" if errored_fail else ""
            results_lines.append(
                f"  {label}: {score['pct']:.0f}% (need {threshold}%) — "
                f"{'PASS' if passed else 'FAIL'}{suffix}"
            )
        elif criterion in self._phase_errors:
            self._set_coverage_criterion(
                criterion,
                False,
                detail_key,
                score,
                error="phase_failed",
            )
            results_lines.append(f"  {label}: ERROR (phase failed) — FAIL")
        else:
            self._set_coverage_criterion(criterion, True, detail_key, score)
            results_lines.append(f"  {label}: N/A ({na_reason}) — PASS")

    def _score_coverage_criteria(self, report: CoverageReport, active: set[str]) -> dict:
        """Compute per-criterion scores, pass/fail verdicts, and overall all_pass."""
        # Resolve thresholds from ticket params (state) or hardcoded defaults
        min_toggle = self._resolve_threshold("coverage_toggle", _DEFAULT_MIN_TOGGLE)
        min_fsm = self._resolve_threshold("coverage_fsm", _DEFAULT_MIN_FSM)
        min_value = self._resolve_threshold("coverage_value", _DEFAULT_MIN_VALUE)
        min_branch = self._resolve_threshold("coverage_branch", _DEFAULT_MIN_BRANCH)
        min_expression = self._resolve_threshold("coverage_expression", _DEFAULT_MIN_EXPRESSION)

        # Compute scores
        toggle = report.toggle_score()
        fsm = report.fsm_score()
        value = report.value_score()
        branch = report.branch_score()
        expression = report.expression_score()

        # Determine pass/fail per criterion (only active criteria affect the result)
        def _check(score: dict, threshold: int, criterion: str) -> bool:
            pct = score["pct"]
            if pct is None:
                return criterion not in self._phase_errors
            return pct >= threshold

        toggle_pass = _check(toggle, min_toggle, "coverage_toggle")
        fsm_pass = _check(fsm, min_fsm, "coverage_fsm")
        value_pass = _check(value, min_value, "coverage_value")
        branch_pass = _check(branch, min_branch, "coverage_branch")
        expr_pass = _check(expression, min_expression, "coverage_expression")

        # Errored-branch threshold: if >50% of branches errored, fail the criterion
        def _majority_errored(score: dict) -> bool:
            errored = score.get("errored", 0)
            total_with_errored = score.get("total", 0) + errored
            return total_with_errored > 0 and errored / total_with_errored > 0.5

        branch_errored_fail = branch["pct"] is not None and _majority_errored(branch)
        expr_errored_fail = expression["pct"] is not None and _majority_errored(expression)

        if branch_errored_fail:
            branch_pass = False
        if expr_errored_fail:
            expr_pass = False

        # Compute all_pass from active criteria with data OR phase errors
        criterion_passes = {
            "coverage_toggle": (toggle_pass, toggle["pct"]),
            "coverage_fsm": (fsm_pass, fsm["pct"]),
            "coverage_value": (value_pass, value["pct"]),
            "coverage_branch": (branch_pass, branch["pct"]),
            "coverage_expression": (expr_pass, expression["pct"]),
        }
        all_pass = all(
            passed
            for key, (passed, pct) in criterion_passes.items()
            if key in active and (pct is not None or key in self._phase_errors)
        )
        return {
            "scores": {
                "toggle": toggle,
                "fsm": fsm,
                "value": value,
                "branch": branch,
                "expression": expression,
            },
            "min": {
                "toggle": min_toggle,
                "fsm": min_fsm,
                "value": min_value,
                "branch": min_branch,
                "expression": min_expression,
            },
            "passes": {
                "toggle": toggle_pass,
                "fsm": fsm_pass,
                "value": value_pass,
                "branch": branch_pass,
                "expression": expr_pass,
            },
            "errored_fail": {"branch": branch_errored_fail, "expression": expr_errored_fail},
            "all_pass": all_pass,
        }

    def _build_coverage_result(
        self,
        report: CoverageReport,
        output_lines: list[str],
        active: set[str],
    ) -> McpToolResult:
        """Score all criteria independently and return final result."""
        report_dict = report.to_report_dict()
        suite = self._campaign_for_tests().suite
        report_dict["target"] = self.args.target
        report_dict["tests"] = list(suite.display_names)
        report_dict["trace_dirs"] = [str(path) for path in getattr(self, "_trace_dirs", [])]
        scored = self._score_coverage_criteria(report, active)
        scores = scored["scores"]
        all_pass = scored["all_pass"]
        toggle, fsm = scores["toggle"], scores["fsm"]
        value, branch, expression = scores["value"], scores["branch"], scores["expression"]

        results_lines: list[str] = []
        # Set criteria independently with per-type detail (order fixed by the table)
        for row in _COVERAGE_CRITERIA_TABLE:
            self._emit_criterion_result(active, results_lines, row, scored)

        # Build output
        status = "PASS" if all_pass else "FAIL"
        exit_code = EXIT_SUCCESS if all_pass else EXIT_FAILURE

        output_lines.append("[coverage] scoring:")
        output_lines.extend(results_lines)
        output_lines.append("")
        output_lines.append(f"RESULT: {status}")

        report_text = "\n".join(output_lines)

        self._write_report_artifact(report_dict)

        display_parts = []
        for label, score in [
            ("TOG", toggle),
            ("FSM", fsm),
            ("VAL", value),
            ("BR", branch),
            ("EXPR", expression),
        ]:
            if score["pct"] is not None:
                display_parts.append(f"{label}: {score['pct']:.0f}%")

        active_keys = [self._target_criterion_key(k) for k in self.satisfies if k in active]
        return McpToolResult(
            exit_code=exit_code,
            criterion_key=(
                active_keys[0] if active_keys else self._target_criterion_key("coverage_toggle")
            ),
            criterion_met=all_pass,
            detail=report_dict,
            report_text=report_text,
            display_lines=[" | ".join(display_parts)] if display_parts else [],
        )

    def _resolve_threshold(self, criterion_key: str, default: int) -> int:
        """Resolve threshold: ticket params (min_pct) > hardcoded default.

        ``min_pct`` is ticket/LLM-supplied (Principle 5 boundary): coerce via
        ``core.boundary.as_int`` and fall back to ``default`` when it is
        missing or non-numeric rather than letting a bad param crash the
        whole coverage run. ``as_int`` also rejects the bool trap
        (``int(True) == 1`` would otherwise sail through silently).
        """
        params = self._campaign_for_tests().params_for(criterion_key)
        if params:
            state_val = params.get("min_pct")
            if state_val is not None:
                coerced = as_int(state_val)
                if coerced is not None:
                    return coerced
                logger.warning(
                    "Ignoring non-numeric min_pct=%r for %s; using default %d",
                    state_val,
                    criterion_key,
                    default,
                )
        return default

    def _write_report_artifact(self, report_dict: dict[str, Any]) -> None:
        """Write coverage_report.json to report_dir if configured."""
        report_dir = self.args.report_dir
        if not report_dir:
            return
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "coverage_report.json").write_text(
            json.dumps(report_dict, indent=2),
            encoding="utf-8",
        )

    _WAIVERS_FILENAME = "coverage_waivers.json"

    def _load_persistent_waivers(self, scope_hash: str) -> PersistentWaivers | None:
        """Load cached waivers from report_dir. Returns None if missing, corrupt, or stale."""
        report_dir = self.args.report_dir
        if not report_dir:
            return None
        path = report_dir / self._WAIVERS_FILENAME
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pw = PersistentWaivers.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            logger.warning("Corrupt %s — discarding", self._WAIVERS_FILENAME)
            return None
        if pw.scope_hash != scope_hash:
            logger.info("Scope changed (hash mismatch) — discarding cached waivers")
            self._clear_session_id(self._COVERAGE_REVIEWER_SESSION_KEY)
            return None
        return pw

    def _save_persistent_waivers(self, pw: PersistentWaivers) -> None:
        """Write merged waivers to report_dir."""
        report_dir = self.args.report_dir
        if not report_dir:
            return
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / self._WAIVERS_FILENAME).write_text(
            json.dumps(pw.to_dict(), indent=2),
            encoding="utf-8",
        )

    # --- Main execution ---

    def _report_structural_noise(
        self,
        structural_noise: list[SignalStats],
        output_lines: list[str],
    ) -> None:
        """Emit progress + output lines summarising excluded structural noise."""
        ivl_count = sum(1 for s in structural_noise if "$ivl_for_loop" in s.name)
        gen_count = sum(
            1
            for s in structural_noise
            if "$ivl_for_loop" not in s.name and self._GENERATE_SCOPE_RE.search(s.name)
        )
        elab_count = len(structural_noise) - ivl_count - gen_count
        parts = []
        if elab_count:
            parts.append(f"{elab_count} params/genvar")
        if ivl_count:
            parts.append(f"{ivl_count} ivl_for_loop")
        if gen_count:
            parts.append(f"{gen_count} generate-scope")
        detail = ", ".join(parts)
        self.emit_progress(
            f"structural noise: excluded {len(structural_noise)} signals ({detail})"
        )
        output_lines.append(f"  structural noise: excluded {len(structural_noise)} ({detail})")

    def _run_vsc_and_fsm(
        self,
        work_dir: Path,
        trace_dir: Path,
        stats: list[SignalStats],
        active_criteria: set[str],
        rtl_context: str,
        vsc_timeout: int,
        fsm_timeout: int,
        output_lines: list[str],
    ) -> tuple[list[BranchResult], list[BranchResult], FsmResult]:
        """Phases 2+3: run VSC and FSM identifier (parallel when both needed)."""
        # VSC/FSM need is fully determined by the active criteria set — derive
        # it here rather than threading redundant flags through the signature.
        need_vsc = "coverage_branch" in active_criteria or "coverage_expression" in active_criteria
        need_fsm = "coverage_fsm" in active_criteria

        branch_results: list[BranchResult] = []
        expression_results: list[BranchResult] = []
        fsm_result = FsmResult()

        if need_vsc and need_fsm:
            output_lines.append(
                "[coverage] phases 2+3: virtual signal creator + FSM identifier (parallel)"
            )
            self.emit_progress("phases 2+3: VSC + FSM identifier (parallel)")
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                vsc_future = pool.submit(
                    self._run_virtual_signal_creator,
                    work_dir,
                    trace_dir,
                    stats,
                    active_criteria,
                    vsc_timeout,
                )
                fsm_future = pool.submit(
                    self._run_fsm_identifier,
                    work_dir,
                    rtl_context,
                    fsm_timeout,
                )
                branch_results, expression_results = vsc_future.result()
                fsm_result = fsm_future.result()
        else:
            if need_vsc:
                output_lines.append("[coverage] phase 2: virtual signal creator")
                self.emit_progress("phase 2: virtual signal creator")
                branch_results, expression_results = self._run_virtual_signal_creator(
                    work_dir,
                    trace_dir,
                    stats,
                    active_criteria,
                    vsc_timeout,
                )
            else:
                output_lines.append("[coverage] phase 2: skipped (no branch/expression criteria)")

            if need_fsm:
                output_lines.append("[coverage] phase 3: FSM identifier")
                self.emit_progress("phase 3: FSM identifier")
                fsm_result = self._run_fsm_identifier(work_dir, rtl_context, fsm_timeout)
            else:
                output_lines.append("[coverage] phase 3: skipped (no FSM criteria)")

        return branch_results, expression_results, fsm_result

    def _run_reviewer_phase(  # noqa: PLR0912, PLR0915 — the phase-4 reviewer + waiver merge in one linear pass
        self,
        work_dir: Path,
        stats: list[SignalStats],
        toggle_failures: list[SignalStats],
        low_diversity: list[SignalStats],
        branch_results: list[BranchResult],
        expression_results: list[BranchResult],
        fsm_result: FsmResult,
        rtl_context: str,
        active_criteria: set[str],
        t0: float,
        output_lines: list[str],
    ) -> tuple[ReviewerResult, list[str], dict[str, str], list[str]]:
        """Phase 4: coverage reviewer with persistent-waiver load/merge/persist.

        Returns ``(reviewer_result, valid_toggle_waivers, value_classifications,
        valid_value_waivers)`` for the report builder.
        """
        valid_toggle_waivers: list[str] = []
        value_classifications: dict[str, str] = {}
        valid_value_waivers: list[str] = []

        # Phase 4: Coverage reviewer (with persistent waiver support)
        need_toggle_review = "coverage_toggle" in active_criteria and toggle_failures
        need_value_review = "coverage_value" in active_criteria and low_diversity

        # Load cached waivers (hash includes file contents for staleness detection)
        scope_hash = _compute_scope_hash(self.args.scope, work_dir)
        cached_waivers: PersistentWaivers | None = None
        if not getattr(self.args, "reset_waivers", False):
            cached_waivers = self._load_persistent_waivers(scope_hash)
            if cached_waivers:
                logger.info(
                    "Loaded %d cached toggle waivers, %d value classifications",
                    len(cached_waivers.toggle_waivers),
                    len(cached_waivers.value_classifications),
                )

        # Subtract already-waived signals from reviewer inputs
        residual_toggle = list(toggle_failures)
        residual_low_div = list(low_diversity)
        if cached_waivers:
            cached_toggle_names = set(cached_waivers.toggle_waivers)
            cached_value_names = set(cached_waivers.value_classifications)
            residual_toggle = [s for s in toggle_failures if s.name not in cached_toggle_names]
            residual_low_div = [s for s in low_diversity if s.name not in cached_value_names]

        need_toggle_review = need_toggle_review and bool(residual_toggle)
        need_value_review = need_value_review and bool(residual_low_div)

        reviewer_result = ReviewerResult()
        remaining = self.args.timeout - (time.monotonic() - t0)
        if not need_toggle_review and not need_value_review:
            output_lines.append(
                "[coverage] phase 4: skipped (no toggle/value signals need review)"
            )
        elif remaining < _MIN_PHASE_BUDGET:
            logger.warning("Only %.0fs remaining — skipping phase 4", remaining)
            output_lines.append(f"[coverage] phase 4: skipped (only {remaining:.0f}s remaining)")
            self._phase_errors.add("coverage_value")
        else:
            reviewer_timeout = min(180, max(int(remaining - 10), 30))
            output_lines.append("[coverage] phase 4: coverage reviewer")
            self.emit_progress("phase 4: coverage reviewer")
            resume_sid = (
                self._load_session_id(self._COVERAGE_REVIEWER_SESSION_KEY)
                if cached_waivers is not None
                else None
            )
            reviewer_result = self._run_coverage_reviewer(
                residual_toggle,
                residual_low_div,
                branch_results,
                expression_results,
                fsm_result,
                work_dir,
                rtl_context,
                reviewer_timeout,
                active_criteria=active_criteria,
                resume_session_id=resume_sid,
            )
            output_lines.append(
                f"  {len(reviewer_result.toggle_waivers)} toggle waivers, "
                f"{len(reviewer_result.value_classifications)} value classifications, "
                f"{len(reviewer_result.value_waivers)} value waivers",
            )
            self.emit_progress(
                f"phase 4 done: {len(reviewer_result.toggle_waivers)} waivers, "
                f"{len(reviewer_result.value_classifications)} classifications",
            )

            # Post-reviewer: unclassified signals default to "insufficient"
            evaluated_names = {s.name for s in residual_low_div}
            value_classifications = dict(reviewer_result.value_classifications)
            for sig_name in evaluated_names:
                if sig_name not in value_classifications:
                    value_classifications[sig_name] = "insufficient"

            # Validate waiver signal names
            known_signals = {s.name for s in stats}
            valid_toggle_waivers = [
                w for w in reviewer_result.toggle_waivers if w in known_signals
            ]
            valid_value_waivers = [
                w for w in reviewer_result.value_waivers if w in value_classifications
            ]
            if len(valid_toggle_waivers) < len(reviewer_result.toggle_waivers):
                logger.warning(
                    "Dropped %d invalid toggle waiver names",
                    len(reviewer_result.toggle_waivers) - len(valid_toggle_waivers),
                )
            if len(valid_value_waivers) < len(reviewer_result.value_waivers):
                logger.warning(
                    "Dropped %d invalid value waiver names",
                    len(reviewer_result.value_waivers) - len(valid_value_waivers),
                )

        # Merge cached waivers with new results (validate against current signals)
        known_signals = {s.name for s in stats}
        if cached_waivers:
            for sig in cached_waivers.toggle_waivers:
                if sig not in valid_toggle_waivers and sig in known_signals:
                    valid_toggle_waivers.append(sig)
            for sig, cls in cached_waivers.value_classifications.items():
                if sig not in value_classifications:
                    value_classifications[sig] = cls
            for sig in cached_waivers.value_waivers:
                if sig not in valid_value_waivers and sig in value_classifications:
                    valid_value_waivers.append(sig)

        # Persist merged waivers for future runs
        merged_pw = PersistentWaivers(
            toggle_waivers=dict.fromkeys(valid_toggle_waivers, "waived"),
            value_waivers=dict.fromkeys(valid_value_waivers, "waived"),
            value_classifications=dict(value_classifications),
            scope_hash=scope_hash,
        )
        self._save_persistent_waivers(merged_pw)

        return (
            reviewer_result,
            valid_toggle_waivers,
            value_classifications,
            valid_value_waivers,
        )

    def _validate_scope_and_ensure_trace(
        self,
        trace_dir: Path,
        work_dir: Path,
    ) -> tuple[list[str], McpToolResult | None]:
        """Validate ``--scope`` files and (re-)produce a fresh trace.

        Returns ``(scope_files, error)``; *error* is set (and *scope_files* may
        be incomplete) when the run must stop here.
        """
        scope_files = [f.strip() for f in self.args.scope.split(",") if f.strip()]
        missing = [f for f in scope_files if not (work_dir / f).exists()]
        if missing:
            logger.warning("Scope files not found: %s", missing)
        if not scope_files or len(missing) == len(scope_files):
            return scope_files, McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"No valid scope files: {scope_files}",
            )

        # Invalidate stale tmpdir cache BEFORE running fresh sim — on POSIX
        # the FIFO pipeline writes the .fst store directly to tmpdir, so invalidating
        # AFTER would delete the freshly-produced trace.
        self._invalidate_trace_cache(trace_dir)
        sim_result = self._ensure_trace(trace_dir, work_dir)
        return scope_files, sim_result

    def _ensure_target_traces(
        self,
        work_dir: Path,
    ) -> tuple[list[str], list[Path], McpToolResult | None]:
        """Produce traces for every runnable test owned by the selected Target."""
        try:
            campaign = self._campaign_for_tests()
        except NoRunnableTestsError as exc:
            return (
                [],
                [],
                McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=f"coverage_analyst: {exc}",
                ),
            )
        units = campaign.execution_units(batched=self._is_cocotb_target())
        trace_dirs: list[Path] = []
        scope_files: list[str] = []
        for unit in units:
            self._coverage_test = unit.test_name
            trace_dir = self._find_trace_dir(unit.test_name)
            scope_files, error = self._validate_scope_and_ensure_trace(trace_dir, work_dir)
            if error is not None:
                error.report_text = f"coverage test {unit.display_name}: {error.report_text}"
                return scope_files, trace_dirs, error
            trace_dirs.append(trace_dir)
        self._coverage_test = None
        return scope_files, trace_dirs, None

    @staticmethod
    def _merge_signal_stats(stats_by_trace: list[list[SignalStats]]) -> list[SignalStats]:
        """Union signal evidence across traces from the same Target."""
        merged: dict[str, SignalStats] = {}
        for trace_stats in stats_by_trace:
            for signal in trace_stats:
                current = merged.setdefault(
                    signal.name,
                    SignalStats(name=signal.name, width=signal.width),
                )
                current.width = max(current.width, signal.width)
                current.transitions += signal.transitions
                for value, count in signal.value_hist.items():
                    current.value_hist[value] = current.value_hist.get(value, 0) + count
        return list(merged.values())

    def _run_phase1_measurement_for_suite(
        self,
        trace_dirs: list[Path],
        scope_files: list[str],
        output_lines: list[str],
    ) -> tuple[list, list, list[str], list[str], McpToolResult | None]:
        """Measure every Target trace and score their aggregate evidence."""
        output_lines.append(
            f"[coverage] phase 1: mechanical measurement ({len(trace_dirs)} trace(s))"
        )
        measured: list[list[SignalStats]] = []
        for trace_dir in trace_dirs:
            stats, error, is_infra = self._run_mechanical_measurement(trace_dir)
            if not stats:
                return (
                    [],
                    [],
                    [],
                    [],
                    McpToolResult(
                        exit_code=EXIT_ERROR if is_infra else EXIT_FAILURE,
                        report_text=f"Phase 1 failed for {trace_dir}: {error or 'unknown'}",
                    ),
                )
            measured.append(stats)
        stats = self._merge_signal_stats(measured)
        stats, structural_noise = self._filter_structural_noise(stats, scope_files)
        toggle_failures, low_diversity = self._pre_filter_for_waiver(stats)
        output_lines.append(f"  {len(stats)} aggregate signals measured")
        output_lines.append(
            f"  {len(toggle_failures)} toggle failures, {len(low_diversity)} low-diversity signals"
        )
        return stats, structural_noise, toggle_failures, low_diversity, None

    def _run_phase1_measurement(
        self,
        trace_dir: Path,
        scope_files: list[str],
        output_lines: list[str],
    ) -> tuple[list, list, list[str], list[str], McpToolResult | None]:
        """Phase 1: mechanical measurement + structural-noise filter + waiver pre-filter.

        Returns ``(stats, structural_noise, toggle_failures, low_diversity, error)``.
        """
        logger.info("Phase 1: Mechanical measurement via bwave --stats")
        output_lines.append("[coverage] phase 1: mechanical measurement")
        self.emit_progress("phase 1: mechanical measurement (bwave --stats)")
        stats, phase1_err, phase1_infra = self._run_mechanical_measurement(trace_dir)
        if not stats:
            return (
                [],
                [],
                [],
                [],
                McpToolResult(
                    exit_code=EXIT_ERROR if phase1_infra else EXIT_FAILURE,
                    report_text=f"Phase 1 failed: {phase1_err or 'unknown'}",
                ),
            )
        output_lines.append(f"  {len(stats)} signals measured")

        # Structural noise filter (simulator-dependent)
        stats, structural_noise = self._filter_structural_noise(stats, scope_files)
        if structural_noise:
            self._report_structural_noise(structural_noise, output_lines)

        toggle_failures, low_diversity = self._pre_filter_for_waiver(stats)
        self.emit_progress(
            f"phase 1 done: {len(stats)} signals, "
            f"{len(toggle_failures)} toggle failures, "
            f"{len(low_diversity)} low-diversity",
        )
        output_lines.append(
            f"  {len(toggle_failures)} toggle failures, {len(low_diversity)} low-diversity signals",
        )
        return stats, structural_noise, toggle_failures, low_diversity, None

    def _run_vsc_and_fsm_with_logging(
        self,
        work_dir: Path,
        trace_dir: Path,
        stats: list,
        active_criteria: object,
        rtl_context: object,
        vsc_timeout: int,
        fsm_timeout: int,
        output_lines: list[str],
    ) -> tuple[list, list, FsmResult]:
        """Run Phases 2+3 (VSC + FSM identifier) and log their combined result."""
        branch_results, expression_results, fsm_result = self._run_vsc_and_fsm(
            work_dir,
            trace_dir,
            stats,
            active_criteria,
            rtl_context,
            vsc_timeout,
            fsm_timeout,
            output_lines,
        )
        output_lines.append(
            f"  {len(branch_results)} branch results, "
            f"{len(expression_results)} expression results, "
            f"{len(fsm_result.fsm_registers)} FSM registers",
        )
        self.emit_progress(
            f"phases 2+3 done: {len(branch_results)} branch, "
            f"{len(expression_results)} expr, "
            f"{len(fsm_result.fsm_registers)} FSM regs",
        )
        return branch_results, expression_results, fsm_result

    def _run_phases_2_to_4(
        self,
        work_dir: Path,
        trace_dir: Path,
        stats: list,
        toggle_failures: list[str],
        low_diversity: list[str],
        rtl_context: object,
        active_criteria: object,
        t0: float,
        output_lines: list[str],
    ) -> tuple[list, list, FsmResult, list[str], dict[str, str], list[str], object]:
        """Phases 2-4: Virtual Signal Creator + FSM Identifier + coverage reviewer.

        Skipped (with an empty result) when too little of the run budget
        remains after Phase 1. Returns ``(branch_results, expression_results,
        fsm_result, valid_toggle_waivers, value_classifications,
        valid_value_waivers, reviewer_result)``.
        """
        branch_results: list[BranchResult] = []
        expression_results: list[BranchResult] = []
        fsm_result = FsmResult()
        valid_toggle_waivers: list[str] = []
        value_classifications: dict[str, str] = {}
        valid_value_waivers: list[str] = []

        remaining = self.args.timeout - (time.monotonic() - t0)
        if remaining < _MIN_PHASE_BUDGET:
            logger.warning("Only %.0fs remaining after Phase 1 — skipping phases 2-4", remaining)
            output_lines.append(
                f"[coverage] phases 2-4: skipped (only {remaining:.0f}s remaining)"
            )
            self._phase_errors.update(
                {"coverage_branch", "coverage_expression", "coverage_fsm", "coverage_value"}
            )
            return (
                branch_results,
                expression_results,
                fsm_result,
                valid_toggle_waivers,
                value_classifications,
                valid_value_waivers,
                reviewer_result,  # noqa: F821 — intentionally unbound on the skip path (pre-existing)
            )

        vsc_timeout = min(int(remaining - 30), max(int(self.args.timeout * 0.7), 120))
        fsm_timeout = min(120, max(int(remaining - 30), 30))

        branch_results, expression_results, fsm_result = self._run_vsc_and_fsm_with_logging(
            work_dir,
            trace_dir,
            stats,
            active_criteria,
            rtl_context,
            vsc_timeout,
            fsm_timeout,
            output_lines,
        )

        # Phase 4: Coverage reviewer + persistent waiver merge/persist.
        (
            reviewer_result,
            valid_toggle_waivers,
            value_classifications,
            valid_value_waivers,
        ) = self._run_reviewer_phase(
            work_dir,
            stats,
            toggle_failures,
            low_diversity,
            branch_results,
            expression_results,
            fsm_result,
            rtl_context,
            active_criteria,
            t0,
            output_lines,
        )
        return (
            branch_results,
            expression_results,
            fsm_result,
            valid_toggle_waivers,
            value_classifications,
            valid_value_waivers,
            reviewer_result,
        )

    def _init_run_state(self) -> tuple[float, Path, list[str]]:
        """Set up per-run state shared across all five phases.

        Returns ``(t0, work_dir, output_lines)``.
        """
        # Per-instance lock — avoids serializing concurrent Specialist instances
        self._agent_lock = threading.Lock()
        self._phase_errors: set[str] = set()
        return (
            time.monotonic(),
            Path(self.args.work_dir),
            [],
        )

    def _run(self) -> McpToolResult:
        """Five-phase coverage analysis."""
        err = self._check_prerequisites()
        if err:
            return err
        if campaign_error := self._apply_campaign_defaults():
            return campaign_error

        t0, work_dir, output_lines = self._init_run_state()

        # Validate --scope files early + ensure one trace per Target test.
        scope_files, trace_dirs, scope_or_trace_err = self._ensure_target_traces(work_dir)
        if scope_or_trace_err:
            return scope_or_trace_err
        self._trace_dirs = trace_dirs

        # Phase 1: Mechanical measurement
        stats, structural_noise, toggle_failures, low_diversity, phase1_err = (
            self._run_phase1_measurement_for_suite(trace_dirs, scope_files, output_lines)
        )
        if phase1_err:
            return phase1_err

        # Read RTL sources once for phases 3 + 4
        rtl_context = self._read_rtl_sources()

        # Phases 2+3: Virtual Signal Creator + FSM Identifier (parallel when both needed)
        active_criteria = self._get_active_criteria()

        (
            branch_results,
            expression_results,
            fsm_result,
            valid_toggle_waivers,
            value_classifications,
            valid_value_waivers,
            reviewer_result,
        ) = self._run_phases_2_to_4(
            work_dir,
            trace_dirs[0],
            stats,
            toggle_failures,
            low_diversity,
            rtl_context,
            active_criteria,
            t0,
            output_lines,
        )

        # Resolve symbolic enum/localparam names in FSM expected_values
        resolved_fsm = _resolve_fsm_enum_names(fsm_result.fsm_registers, rtl_context)

        # Build report
        report = CoverageReport(
            signal_stats=stats,
            structural_noise=structural_noise,
            branch_results=branch_results,
            expression_results=expression_results,
            fsm_registers=resolved_fsm,
            toggle_waivers=valid_toggle_waivers,
            value_classifications=value_classifications,
            value_waivers=valid_value_waivers,
            reviewer_notes=reviewer_result.notes,
            improvement_hints=reviewer_result.improvement_hints,
        )

        # Phase 5: Scoring
        output_lines.append("[coverage] phase 5: scoring")
        return self._build_coverage_result(report, output_lines, active_criteria)

    def _apply_campaign_defaults(self) -> McpToolResult | None:
        """Resolve one consistent RTL scope from Target-specific criteria."""
        try:
            campaign = resolve_target_campaign(
                self.args.target,
                self.satisfies,
                self.state.criteria,
                explicit_scope=self.args.scope,
            )
        except CampaignScopeError as exc:
            reason = (
                "no scope is declared"
                if exc.reason == "missing"
                else "criteria declare conflicting scopes"
            )
            return McpToolResult(
                exit_code=EXIT_FAILURE,
                report_text=(
                    f"coverage_analyst: {reason} for Target {self.args.target!r}. "
                    "Declare the same scope: [rtl/file.sv, ...] on its coverage "
                    "criteria or pass --scope."
                ),
            )
        except NoRunnableTestsError as exc:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"coverage_analyst: {exc}",
            )
        self._target_campaign = campaign
        self.args.scope = campaign.scope_arg
        return None

    def _campaign_for_tests(self) -> TargetCampaign:
        """Return the resolved campaign or a suite-only compatibility view."""
        campaign = getattr(self, "_target_campaign", None)
        if campaign is not None:
            return campaign
        try:
            criteria = self.state.criteria
        except RuntimeError:
            criteria = {}
        return describe_target_campaign(
            self.args.target,
            criterion_keys=self.satisfies,
            criteria=criteria,
        )

    def _check_prerequisites(self) -> McpToolResult | None:
        """Verify the native bwave binary is installed.

        PATH is not the test: the `bwave` on PATH is the Python wrapper, which
        is present whenever booley is — the binary behind it is what coverage
        measurement actually runs.
        """
        if native_bwave_binary() is None:
            logger.error("bwave binary not found")
            return McpToolResult(exit_code=EXIT_ERROR, report_text="bwave binary not found")
        return None

    def _derive_trace_scope(self) -> str:
        """Trace the full hierarchy so post-processing can discover RTL scopes."""
        return ""

    # Matches unguarded $dumpfile / $dumpvars system task calls.  Word boundary
    # avoids partial hits like $dumpfileBlah.  Comments are filtered by caller.
    _TB_DUMP_CALL_RE: ClassVar[re.Pattern[str]] = re.compile(r"\$(dumpfile|dumpvars)\b")

    # SV comment strippers — preserve newline count so post-strip offsets map
    # back to original line numbers for error reporting.
    _SV_LINE_COMMENT_RE: ClassVar[re.Pattern[str]] = re.compile(r"//[^\n]*")
    _SV_BLOCK_COMMENT_RE: ClassVar[re.Pattern[str]] = re.compile(r"/\*.*?\*/", re.DOTALL)

    # `module <name>` / `endmodule` markers for hierarchy tracking.  SV modules
    # don't nest (interfaces/programs/packages aren't tracked — false positives
    # are tolerable since this is an optional pre-flight, not authoritative).
    _SV_MODULE_OPEN_RE: ClassVar[re.Pattern[str]] = re.compile(r"\bmodule\s+(\w+)\b")
    _SV_MODULE_CLOSE_RE: ClassVar[re.Pattern[str]] = re.compile(r"\bendmodule\b")

    @classmethod
    def _scan_tb_for_dump_calls(
        cls,
        work_dir: Path,
    ) -> list[tuple[str, int, str, str]]:
        """Scan TB sources for $dumpfile/$dumpvars calls.

        TB-level dump calls override the harness-managed +tracefile path, so
        the VCD lands somewhere bwave isn't watching and the run surfaces as
        an opaque "no trace file found".  We catch this deterministically so
        the developer gets a precise fix target (file:line) instead of
        having to infer cause from a vague trace-missing error.

        Returns list of (relpath, lineno, snippet, kind) tuples.
        """
        from booley.runtime.shared_infra import get_tb_dirs

        tb_dirs, _ = get_tb_dirs()
        matches: list[tuple[str, int, str, str]] = []
        for tb_dir in tb_dirs:
            for sv in sorted(tb_dir.rglob("*.sv")):
                try:
                    text = sv.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    m = cls._TB_DUMP_CALL_RE.search(line)
                    if not m:
                        continue
                    # Skip lines where the match is inside a // comment
                    comment_idx = line.find("//")
                    if comment_idx != -1 and comment_idx < m.start():
                        continue
                    try:
                        rel = sv.resolve().relative_to(work_dir.resolve())
                        relpath = str(rel).replace("\\", "/")
                    except ValueError:
                        relpath = str(sv).replace("\\", "/")
                    matches.append((relpath, lineno, line.strip(), m.group(1)))
        return matches

    @classmethod
    def _strip_sv_comments(cls, text: str) -> str:
        """Strip // and /* */ comments while preserving newline positions.

        Block comments are replaced with newlines-only so line numbers in the
        stripped text still map to the original source.  Line comments stop
        before the trailing \\n, so they preserve numbering trivially.
        """

        def _replace_block(m: re.Match[str]) -> str:
            return "\n" * m.group(0).count("\n")

        text = cls._SV_BLOCK_COMMENT_RE.sub(_replace_block, text)
        return cls._SV_LINE_COMMENT_RE.sub("", text)

    @classmethod
    def _extract_module_bodies(
        cls,
        text: str,
    ) -> list[tuple[str, int, int]]:
        """Return (module_name, body_start, body_end) tuples for each module.

        Assumes flat (non-nested) module structure — true for SV modules but
        not for programs/interfaces.  Unmatched `module` without `endmodule`
        is dropped (truncated/malformed source).
        """
        bodies: list[tuple[str, int, int]] = []
        pos = 0
        while True:
            m = cls._SV_MODULE_OPEN_RE.search(text, pos)
            if not m:
                break
            close = cls._SV_MODULE_CLOSE_RE.search(text, m.end())
            if not close:
                break
            bodies.append((m.group(1), m.start(), close.end()))
            pos = close.end()
        return bodies

    @staticmethod
    def _module_containing(
        bodies: list[tuple[str, int, int]],
        pos: int,
    ) -> str:
        """Return the name of the module body containing `pos`, else ''."""
        for name, start, end in bodies:
            if start <= pos < end:
                return name
        return ""

    @classmethod
    def _scan_tb_for_dut_instances(
        cls,
        work_dir: Path,
        dut_top_module: str,
    ) -> list[tuple[str, int, str, str]]:
        """Scan TB sources for instantiations of ``dut_top_module``.

        Returns (relpath, lineno, instance_name, containing_module) tuples.

        Best-effort regex parser: handles unparameterized and parameterized
        (``#(...)`` with one level of nested parens) instantiations.  Comments
        are stripped first.  `module <name>` declarations themselves cannot
        match because the instance-name capture requires ``\\w+\\s*\\(`` to
        follow the module identifier — a declaration has ``(`` immediately
        after the name.  Falls through to elab-time diagnostics for cases
        this parser can't recognise (deeply nested parameter blocks, macro
        expansions, instance arrays in unusual forms).
        """
        if not dut_top_module:
            return []

        # <module> [#(<params with 1-level nested parens>)] <instance> (
        inst_re = re.compile(
            rf"\b{re.escape(dut_top_module)}\b"
            r"\s*"
            r"(?:#\s*\((?:[^()]+|\([^()]*\))*\)\s*)?"
            r"(\w+)\s*\(",
            re.DOTALL,
        )

        from booley.runtime.shared_infra import get_tb_dirs

        tb_dirs, _ = get_tb_dirs()
        matches: list[tuple[str, int, str, str]] = []
        for tb_dir in tb_dirs:
            for sv in sorted(tb_dir.rglob("*.sv")):
                try:
                    text = sv.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                stripped = cls._strip_sv_comments(text)
                bodies = cls._extract_module_bodies(stripped)
                try:
                    rel = sv.resolve().relative_to(work_dir.resolve())
                    relpath = str(rel).replace("\\", "/")
                except ValueError:
                    relpath = str(sv).replace("\\", "/")
                for m in inst_re.finditer(stripped):
                    inst_name = m.group(1)
                    # SV keywords occasionally follow a module identifier in
                    # non-instantiation positions (rare, but cheap guard).
                    if inst_name in _SV_KEYWORDS:
                        continue
                    lineno = stripped.count("\n", 0, m.start()) + 1
                    container = cls._module_containing(bodies, m.start())
                    matches.append((relpath, lineno, inst_name, container))
        return matches

    def _resolve_trace_target(
        self,
        fusesoc_registry: Any,
        edam_layer: Any,
        work_dir: Path,
    ) -> tuple[Any, TraceMode]:
        """Resolve one isolated traced Target and its coherent trace mode."""
        build_root = edam_layer.work_root_for(
            work_dir,
            "coverage",
            self.args.target,
            variant="trace",
        )
        overlay = fusesoc_registry.write_trace_overlay(
            self.args.target,
            project_root=work_dir,
        )
        try:
            resolved = fusesoc_registry.resolve_target(
                self.args.target,
                project_root=work_dir,
                build_root=build_root,
                vlnv=overlay.vlnv,
            )
            return resolved, overlay.mode
        finally:
            overlay.cleanup()

    def _cocotb_trace_run_cmd(
        self,
        context: _TraceRunContext,
        cocotb_module: str,
    ) -> tuple[list[str], str]:
        """Build one traced Cocotb invocation for the Target suite."""
        suite = self._campaign_for_tests().suite
        run_cmd = [
            "python3",
            "-m",
            "booley.sim.cocotb_run",
            "--build-dir",
            context.build_dir,
            "--eda-tool",
            context.eda_tool,
            "--cocotb-module",
            cocotb_module,
            "--work-dir",
            posix_relpath(context.trace_dir, context.work_dir),
            "--timeout",
            str(context.run_timeout),
            "--trace",
        ]
        run_cmd.extend(f"--test={test}" for test in suite.tests if test is not None)
        return run_cmd, f"{context.eda_tool} cocotb compilation failed"

    @staticmethod
    def _hdl_trace_run_cmd(
        context: _TraceRunContext,
    ) -> tuple[list[str], str]:
        """Build one traced native-HDL simulator invocation."""
        is_icarus = context.eda_tool == "icarus"
        module = "booley.sim.iverilog_run" if is_icarus else "booley.sim.verilator_run"
        build_option = "--build-dir" if is_icarus else "--bin-dir"
        run_cmd = [
            "python3",
            "-m",
            module,
            build_option,
            context.build_dir,
        ]
        if not is_icarus:
            run_cmd.extend(
                (
                    "--top",
                    context.resolved.toplevel,
                    "--trace-mode",
                    context.trace_mode.value,
                )
            )
            run_cmd.extend(
                f"--trace-arg={argument}" for argument in resolve_trace_args(context.work_dir)
            )
        run_cmd.extend(f"--trace-file={path}" for path in resolve_trace_files(context.work_dir))
        run_cmd.extend(
            (
                "--work-dir",
                posix_relpath(context.trace_dir, context.work_dir),
                "--timeout",
                str(context.run_timeout),
                "--trace",
            )
        )
        marker = "iverilog compilation failed" if is_icarus else "Verilator elaboration failed"
        return run_cmd, marker

    def _trace_run_cmd(
        self,
        context: _TraceRunContext,
    ) -> tuple[list[str], str]:
        """Select the traced run-half for the Target's simulator family."""
        cocotb_modules = fusesoc_registry.target_cocotb_modules(context.work_dir)
        cocotb_module = lookup_target_section(cocotb_modules, self.args.target)
        if cocotb_module:
            fusesoc_registry.validate_cocotb_trace_mode(
                self.args.target,
                context.trace_mode,
            )
            return self._cocotb_trace_run_cmd(
                context,
                str(cocotb_module),
            )
        return self._hdl_trace_run_cmd(context)

    def _add_trace_run_options(
        self,
        run_cmd: list[str],
        trace_scope: str,
        run_cwd: str | None,
    ) -> None:
        """Append scope, cwd, and selected-test options to a run-half command."""
        if trace_scope:
            run_cmd += ["--trace-scope", trace_scope]
        if run_cwd:
            run_cmd += ["--run-cwd", run_cwd]
        selected_test = getattr(self, "_coverage_test", None)
        run_cmd.extend(
            f"--plusarg={plusarg}"
            for plusarg in _trace_test_plusargs(self.args.target, selected_test)
        )

    def _build_edalize_trace_cmd(
        self,
        work_dir: Path,
        trace_dir: Path,
        trace_scope: str,
        run_timeout: int,
    ) -> list[str]:
        """Resolve the Target and return its guarded build + traced-run command."""
        eda_tool = sim_edam.normalize_eda_tool(
            fusesoc_registry.target_eda_tools(work_dir).get(self.args.target)
        )
        resolved, trace_mode = self._resolve_trace_target(
            fusesoc_registry,
            edam_layer,
            work_dir,
        )
        build_dir = edam_layer.relpath_for_make(resolved.build_root, work_dir)
        build_cmd = edam_layer.make_command(build_dir)
        run_cwd = resolve_run_cwd(work_dir)
        context = _TraceRunContext(
            eda_tool=eda_tool,
            resolved=resolved,
            build_dir=build_dir,
            trace_dir=trace_dir,
            work_dir=work_dir,
            run_timeout=run_timeout,
            trace_mode=trace_mode,
        )
        run_cmd, marker = self._trace_run_cmd(context)
        self._add_trace_run_options(run_cmd, trace_scope, run_cwd)
        script = (
            f"{shlex.join(build_cmd)} "
            f'|| {{ echo "ERROR: {marker} (rc=$?)"; exit 1; }}\n'
            f"{shlex.join(run_cmd)}"
        )
        return ["sh", "-c", script]

    def _check_tb_dump_calls(self, work_dir: Path) -> McpToolResult | None:
        """Pre-flight: reject unguarded TB ``$dumpfile``/``$dumpvars`` calls.

        These hijack the harness's ``+tracefile`` path so bwave finds no trace
        and the run dies with a vague message; catch it early with a precise,
        deterministically-fixable error.  Returns an error McpToolResult or None.
        """
        dump_calls = self._scan_tb_for_dump_calls(work_dir)
        if not dump_calls:
            return None
        locations = "\n".join(
            f"  {relpath}:{lineno}: ${kind} — {snippet[:120]}"
            for relpath, lineno, snippet, kind in dump_calls
        )
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=(
                f"Testbench contains {len(dump_calls)} unguarded "
                "$dumpfile/$dumpvars call(s); these override the harness's "
                "+tracefile path so bwave finds no trace.  Strip them or "
                "guard behind a plusarg, e.g.:\n"
                '  initial if ($test$plusargs("dump")) begin\n'
                '    $dumpfile("trace.vcd"); $dumpvars(0, dut);\n'
                "  end\n"
                f"Found at:\n{locations}"
            ),
        )

    def _diagnose_trace_run_failure(
        self,
        proc: subprocess.CompletedProcess,
    ) -> McpToolResult:
        """Turn a non-zero traced-sim run into a structured error McpToolResult."""
        # run_sim_batch merges stderr→stdout, so check both streams
        detail = proc.stderr.strip() or proc.stdout.strip()
        # Distinguish a trace-pipeline stall (infra issue: the bwave
        # FIFO/sim went catatonic) from a generic sim failure so the
        # blocking message is actionable.
        if "bwave trace pipeline stalled" in detail:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    "Traced simulation aborted: bwave trace pipeline stalled "
                    "(sim produced no waveform bytes for >90s; both sim and "
                    "bwave killed). This is an infra-level stall, not "
                    f"a coverage failure. tail:\n{detail[-500:]}"
                ),
            )
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=f"Traced simulation failed (rc={proc.returncode}): {detail[-500:]}",
        )

    def _ensure_trace(self, trace_dir: Path, work_dir: Path) -> McpToolResult | None:
        """Run traced simulation if no trace file exists. Returns error McpToolResult or None."""
        # Pre-flight: TB-level $dumpfile/$dumpvars hijack the +tracefile path
        # the harness sets up, so bwave finds nothing and the run dies with a
        # vague "no trace file" message.  Catch it early with a precise,
        # deterministically-fixable error.
        err = self._check_tb_dump_calls(work_dir)
        if err is not None:
            return err

        trace_scope = self._derive_trace_scope()
        trace_timeout = max(int(self.args.timeout * 0.6), 300)
        # Both simulators trace through the edalize build + their EDA-tool-specific
        # run-half (verilator_run / iverilog_run); _build_edalize_trace_cmd is
        # EDA-tool-aware. The legacy run_sim_batch path is retired.
        try:
            cmd = self._build_edalize_trace_cmd(
                work_dir,
                trace_dir,
                trace_scope,
                trace_timeout,
            )
        except (
            Exception  # noqa: BLE001 — isolate arbitrary adapter/configure failures
        ) as exc:  # isolate EDAM/configure failure and surface it as an error McpToolResult
            logger.debug("coverage EDAM/configure failed for %s", self.args.target, exc_info=True)
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=f"Traced simulation setup failed: {exc}",
            )

        logger.info("Running traced simulation (timeout=%ds): %s", trace_timeout, cmd)
        try:
            proc = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=trace_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return McpToolResult(
                exit_code=EXIT_ERROR, report_text=f"Traced simulation timed out ({trace_timeout}s)"
            )

        if proc.returncode != 0:
            return self._diagnose_trace_run_failure(proc)

        # Verify trace now exists.  rc=0 + no trace is a real failure mode
        # (bwave missing/crashed, scope produced no signals, FIFO never
        # drained, ...) but the sim's own stdout has the diagnostic.  Surface
        # its tail so the developer gets something actionable instead of a
        # bare "no trace file found".
        if not trace_dir.is_dir() or self._find_trace_file(trace_dir) is None:
            detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
            tail = f"\nsim tail:\n{detail[-800:]}" if detail else ""
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    f"Traced simulation completed (rc=0) but no trace file "
                    f"found in {trace_dir}.{tail}"
                ),
            )
        return None

    def _find_trace_dir(self, test_name: str | None = None) -> Path:
        """Locate the simulation work directory containing the trace file.

        Must match run_sim_batch's path derivation: configs with empty test
        lists drop the test name from the work directory path.
        """
        from booley.config.project_config import TEST_NAMES

        target = self.args.target
        test = test_name
        resolved_test = None
        declared_tests = lookup_target_section(TEST_NAMES, target) or []
        if test and declared_tests:
            resolved_test = test
        return derive_work_dir(
            Path(self.args.work_dir),
            "sim",
            target,
            top_module=self.args.tb_top,
            test_name=resolved_test,
        )

    @staticmethod
    def _find_trace_file(trace_dir: Path) -> Path | None:
        """Find trace file via TraceSession (tmpdir cache, auto-convert)."""
        return TraceSession(trace_dir).find()

    @staticmethod
    def _invalidate_trace_cache(trace_dir: Path) -> None:
        """Delete cached stores in tmpdir so next find picks up fresh data."""
        TraceSession(trace_dir).invalidate()


if __name__ == "__main__":
    CoverageAnalystSpecialist().cli()
