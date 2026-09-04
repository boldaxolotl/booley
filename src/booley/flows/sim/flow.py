"""SimulateFlow — BooleyFlow for running RTL simulation.

Runs RTL simulation for one or more configs via the Edalize sim flow + a
EDA-tool-specific run-half (sim.verilator_run / sim.iverilog_run).
Supports multi-config sequential execution, per-test filtering, dry-run mode,
cycle count extraction, and structured JSON reporting.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, cast

from booley.bwave.contract import decode_trace_metadata
from booley.config.project_config import (
    load_test_configuration_field,
    lookup_target_section,
)
from booley.core.boundary import BoundaryError, as_float, as_int, as_str_list
from booley.criteria.thresholds import has_relative_threshold
from booley.flows.run_log import RUN_LOG_NAME, run_log_is_current, write_run_log
from booley.flows.sim.config import resolve_run_cwd
from booley.flows.sim.result import parse_summary_line
from booley.flows.sim.run_guard import DEFAULT_SIM_TIME_GRACE_S
from booley.fusesoc import fusesoc_registry
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS, McpToolResult
from booley.runtime import job_slots
from booley.runtime.platform_paths import posix_relpath
from booley.runtime.timefmt import utc_now_rfc3339
from booley.targets.flow_names import config_section
from booley.targets.target import (
    TargetHandle,
    criterion_matches_target,
    inspect_target,
    select_target,
    select_targets,
)

from .. import artifacts, output_budget
from .. import edam as edam_layer
from ..base import BooleyFlow, SubprocessResult
from ..baseline_worktree import (
    BaselineWorktreeError,
    baseline_worktree,
    git_full_sha,
)
from ..eda_parsers import extract_error_gist
from ..flow_config import (
    _load_flow_config,
    tb_top_for_target,
)
from ..human_display import cap_target_items
from .adapter_transport import (
    AdapterTransportIdentity,
)
from .build import (
    BuildOutcome,
    PreparedSimulationBuild,
    SimulationBuildPreparationError,
    build_stage_script,
    classify_build_outcome,
    new_attempt_token,
    prepare_simulation_build,
    setup_failure_outcome,
)
from .execution import (
    DefaultSelection,
    NamedTests,
    SimulationExecution,
    SimulationOptions,
    SimulationTargetOutcome,
)
from .execution.artifacts import artifact_path_component as _artifact_path_component
from .execution.failures import find_missing_executable
from .standalone import StandaloneMixin, _StandaloneOutcome
from .target_tests import (
    NoRunnableTestsError,
    require_runnable_target_test_suite,
    resolve_target_test_suite,
)

logger = logging.getLogger(__name__)

# Default literal prefix for cycle count extraction from sim output.
_DEFAULT_CYCLE_SENTINEL = "[SIM_CYCLES]"
# Patterns that indicate elaboration/compilation failure (before sim ran).
# The `ERROR: <eda_tool> …` markers are Booley-echoed on build breakage (sandbox
# script).
_ELAB_FAIL_RE = re.compile(
    r"ERROR:\s*(Verilator elaboration failed|Verilator executable not found"
    r"|Verilator elaboration timed out"
    r"|iverilog compilation failed|iverilog compilation timed out)",
    re.IGNORECASE | re.MULTILINE,
)

# Pre-Run Commands failure marker (ADR 0039). The boundary wrapper makefile
# echoes it when [flows.sim].pre_run_commands exit nonzero (the
# BOOLEY_STAGE marker precedent), so _interpret_sim_result attributes the
# failure to the pre-run step instead of blaming the sim/build.
_PRERUN_FAIL_RE = re.compile(r"\[BOOLEY_PRERUN_FAIL rc=(-?\d+)\]")

class MissingExecutableError(RuntimeError):
    """A required EDA binary is absent — a Flow error, never a test failure.

    Carries the binary name so :meth:`SimulateFlow._missing_executable_result`
    can name it in the exit-2 verdict instead of inventing a per-test FAIL row
    for a test that never ran.
    """

    def __init__(self, binary: str, context: str = "") -> None:
        super().__init__(f"required executable not found: {binary}")
        self.binary = binary
        self.context = context


class SimulationBuildInfrastructureError(RuntimeError):
    """An authenticated build attempt ended without a design verdict."""

    def __init__(self, target: str, outcome: BuildOutcome) -> None:
        super().__init__(outcome.reason)
        self.target = target
        self.outcome = outcome


def _raise_if_missing_executable(text: str) -> None:
    """Escalate an absent-binary report in *text* to :class:`MissingExecutableError`.

    Called only where the run reached no verdict — a setup exception or a
    broken build half. A sim that actually ran and happened to echo
    "foo: command not found" from its own ``$system`` call is never inspected.
    """
    binary = find_missing_executable(text)
    if binary:
        raise MissingExecutableError(binary, context=text)


_DEFAULT_TIMEOUT_MS = 600_000
# Per-run disk budget for the sim run directory (SETUP-25) — on how much the dir
# GROWS during one run, not on its total size (F-23: run_cwd is routinely shared
# with the TB's staged input vectors, and charging those to the output budget
# killed every run after the first traced one). Chosen well above a normal traced
# run (an Ibex ibex_tracer wrote 272MB/run) yet below the 27GB runaway that once
# filled the disk; override via [flows.sim].max_rundir_bytes.
_DEFAULT_MAX_RUNDIR_BYTES = 5 * 1024**3  # 5 GiB
# Execution enablement is resolved once per run. Public simulation accepts only
# the open-source Verilator and Icarus run halves.
_TRACE_CLEANUP_MARGIN_S = 90

# Max error lines shown per test in the display box
_MAX_DISPLAY_ERRORS = 3

# Lines of simulator output kept in a TestResult's error_tail. The report shows
# a further-trimmed excerpt of it; the full output lives in run.log.
_ERROR_TAIL_LINES = 50

# The two reasons a run can end up INCONCLUSIVE, spelled out for the RESULT
# line. Reporting the first when the second happened is what made a fully
# passing traced run print "no pass/fail sentinel detected" while its own
# result.json recorded passed:true and 96 sentinel hits (fpu F-22).
_INCONCLUSIVE_NO_SENTINEL = (
    "no pass/fail sentinel detected, simulation exited cleanly. "
    "Read the full simulator output at the report's artifacts.log, and re-run "
    "with --trace to inspect the waveform with bwave. If the testbench prints "
    "its verdict in wording Booley does not recognize, declare it in "
    "[flows.sim].pass_sentinels / fail_sentinels."
)
_INCONCLUSIVE_NO_WAVEFORM = (
    "the simulation itself PASSED, but --trace produced no queryable waveform, "
    "so the trace could not be verified. This is a Flow-infrastructure failure, "
    "not a design defect — see the TRACE_INCIDENT file, and declare a custom "
    "testbench's dump path in [flows.sim].trace_files."
)

# The run-halves' "a queryable waveform actually landed" marker, carrying the
# store's path. simulate used to scrape it only for *presence* — leaving the
# artifact five directory levels deep under .runtime/edalize/simulate/ and
# findable only by someone who already knew the convention (F-35).
_TRACE_OK_RE = re.compile(r"^TRACE_OK:\s*(\S.*?)\s*$", re.MULTILINE)
_TRACE_METADATA_RE = re.compile(r"^TRACE_METADATA:\s*(\{.*\})\s*$", re.MULTILINE)

# Max failure-excerpt lines per test inside report_text. The MCP server
# tail-truncates the whole EDA-tool stdout to ~12KB (keeping the END), so one
# chatty failing target must not monopolize the surviving window — anything
# beyond this tail is pointed at the persisted run.log instead. This is the
# 12KB-budget default; the effective cap scales with a raised
# BOOLEY_MCP_MAX_STDOUT_BYTES (see output_budget.scaled).
_MAX_EXCERPT_LINES = 30

@dataclass
class TestResult:
    """Result of a single test execution."""

    name: str
    passed: bool
    elapsed_s: float = 0.0
    # Seconds of elapsed_s spent (re)building the sim model. The first test of
    # a run pays the whole edalize make after an RTL edit; reporting that
    # share keeps a "reset PASS 5.0s" from misleading timing-based triage when
    # the simulation itself took 0.1s.
    build_s: float = 0.0
    cycles: int | None = None
    # Typed status of the named Cycle Count record. ``cycles`` remains the
    # observational compatibility value; acceptance requires ``observed``.
    cycle_status: str = "missing"
    sva_errors: int = 0
    error_tail: str = ""
    timed_out: bool = False
    inconclusive: bool = False
    # WHY the run is inconclusive, in one clause. There are two very different
    # reasons and the report used to print only the first: "no pass/fail
    # sentinel detected" was stated verbatim on a run that scored 96 of them and
    # whose result.json said passed:true, because a *missing waveform* had
    # downgraded the verdict (fpu F-22). Empty when not inconclusive.
    inconclusive_reason: str = ""
    elab_failed: bool = False
    test_validated: bool = True
    # Waveform this run produced (--trace only): project-relative path and
    # on-disk size, so the artifact is reported rather than merely asserted to
    # exist (F-35). Empty on a non-traced run. A cocotb batch shares one
    # waveform, so it rides the first entry.
    trace_path: str = ""
    trace_bytes: int = 0
    trace_top_scope: str = ""
    trace_signal_count: int = 0
    trace_total_ticks: int = 0
    # Work-dir-relative immutable copy of this test's complete simulator
    # output. Empty only when no simulator output was available or no report
    # directory was requested.
    run_log_path: str = ""
    workload_snapshot: dict[str, Any] | None = None
    # Authenticated result of the build half for this execution attempt.
    # ``None`` means setup or Pre-Run Commands failed before make ran.
    build_outcome: BuildOutcome | None = None
    phase_timings_s: dict[str, float] = field(default_factory=dict)
    resources: dict[str, float | int | None] = field(default_factory=dict)


@dataclass
class TargetResult:
    """Aggregate result for one config."""

    target: str
    tb_top: str = ""
    # Raw resolved EDA tool (normally "verilator"/"icarus"), not the
    # normalize_eda_tool run-half family — observability, not parser dispatch.
    eda_tool: str = ""
    passed: bool = False
    elapsed_s: float = 0.0
    tests: list[TestResult] = field(default_factory=list)
    inconclusive: bool = False
    elab_failed: bool = False
    phase_timings_s: dict[str, float] = field(default_factory=dict)
    target_identity: str = ""
    diagnostics: tuple[str, ...] = ()


@dataclass
class ElabOnlyTargetResult:
    """One Target's compile/elaborate/link result in Simulation namespace."""

    target: str
    target_identity: str = ""
    eda_tool: str = ""
    toplevel: str = ""
    compile_command: str = ""
    fileset: dict[str, list[str]] = field(default_factory=dict)
    outcome: BuildOutcome = field(
        default_factory=lambda: BuildOutcome(False, None, "infrastructure")
    )
    log_path: str = ""


def _admissible_cycle_evidence(
    test: TestResult | None,
    revision: str,
) -> tuple[bool, str]:
    """Return whether one revision has passing, unambiguous named evidence."""
    if test is None:
        return False, f"{revision} named test result is missing"
    if not test.passed:
        return False, f"{revision} test did not pass"
    if test.cycle_status != "observed" or test.cycles is None:
        return False, f"{revision} Cycle Count observation is {test.cycle_status}"
    return True, ""


def _target_progress_detail(result: TargetResult) -> dict[str, Any]:
    """Compact durable checkpoint entry for one completed Target."""
    detail = {
        "target_identity": result.target_identity,
        "passed": result.passed,
        "inconclusive": result.inconclusive,
        "elapsed_s": result.elapsed_s,
        "tests": len(result.tests),
        "tests_passed": sum(1 for test in result.tests if test.passed),
        "phase_timings_s": dict(result.phase_timings_s),
    }
    build_stage = [_build_outcome_entry(test.build_outcome) for test in result.tests]
    if any(entry is not None for entry in build_stage):
        detail["build_stage"] = [entry for entry in build_stage if entry is not None]
    return detail


def _build_outcome_entry(outcome: BuildOutcome | None) -> dict[str, Any] | None:
    """JSON shape for one authenticated Simulation build attempt."""
    if outcome is None:
        return None
    return {
        "ran": outcome.ran,
        "verdict": outcome.verdict,
        "failure_class": outcome.failure_kind,
        "returncode": outcome.returncode,
        "elapsed_s": round(outcome.elapsed_s, 3),
        "timed_out": outcome.timed_out,
        "peak_rss_mb": outcome.peak_rss_mb,
        "oom_kill_delta": outcome.oom_kill_delta,
        "terminal_record": outcome.terminal_record,
        "reason": outcome.reason,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace *path* atomically so a poll never observes partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


@dataclass(frozen=True)
class CycleObservation:
    """Typed result of parsing Cycle Count records for one named test."""

    status: str
    value: int | None = None


def _cycle_records(
    output: str,
    sentinels: list[str],
) -> tuple[list[tuple[str, str]], list[str], bool]:
    """Collect named and legacy payloads without deciding admissibility."""
    named: list[tuple[str, str]] = []
    legacy: list[str] = []
    malformed = False
    for line in output.splitlines():
        matched = next((sentinel for sentinel in sentinels if sentinel in line), None)
        if matched is None:
            continue
        parts = line.split(matched, 1)[1].strip().split()
        if len(parts) == 1:
            legacy.append(parts[0])
        elif len(parts) >= 2:
            named.append((" ".join(parts[:-1]), parts[-1]))
        else:
            malformed = True
    return named, legacy, malformed


def parse_cycle_observation(
    output: str,
    test_name: str,
    cycle_sentinels: list[str] | None = None,
) -> CycleObservation:
    """Parse named/default/configured records without discarding ambiguity."""
    sentinels = [s for s in (cycle_sentinels or [_DEFAULT_CYCLE_SENTINEL]) if s]
    sentinels.sort(key=len, reverse=True)
    named, legacy, malformed = _cycle_records(output, sentinels)

    matches = [record for record in named if record[0] == test_name]
    observation = CycleObservation("missing")
    if malformed:
        observation = CycleObservation("malformed")
    elif len(matches) > 1:
        observation = CycleObservation("duplicate")
    elif len(matches) == 1:
        count = matches[0][1]
        if not re.fullmatch(r"[0-9]+", count):
            observation = CycleObservation("malformed")
        else:
            observation = CycleObservation("observed", int(count))
    elif named:
        observation = CycleObservation("wrong_test")
    elif len(legacy) > 1:
        observation = CycleObservation("duplicate")
    elif len(legacy) == 1:
        if not re.fullmatch(r"[0-9]+", legacy[0]):
            observation = CycleObservation("malformed")
        else:
            observation = CycleObservation("legacy", int(legacy[0]))
    return observation


def parse_cycles(
    output: str,
    test_name: str,
    cycle_sentinels: list[str] | None = None,
    *,
    allow_legacy: bool = True,
) -> int | None:
    """Compatibility value wrapper around :func:`parse_cycle_observation`."""
    observation = parse_cycle_observation(output, test_name, cycle_sentinels)
    if observation.status == "observed" or (allow_legacy and observation.status == "legacy"):
        return observation.value
    return None


def _build_run_script(
    build_cmd: list[str],
    marker: str,
    run_line: str,
    sim_env: dict[str, str] | None = None,
    *,
    attempt_token: str | None = None,
) -> str:
    """Compose the one-subprocess build+run shell script for a sandbox sim.

    On build failure, echo the canonical marker the legacy runner printed (raw
    verilator/iverilog errors don't carry it) so _ELAB_FAIL_RE still tags
    elaboration failures; ``exit 1`` keeps the run from firing after a broken
    build. shlex.join quotes every element (e.g. Icarus's
    ``EXTRA_OPTIONS="+a +b"``), so the only shell metacharacters are ours.

    The make and run halves are bracketed with GNU ``date`` nanosecond stamps
    and POSIX-shell arithmetic so the per-test report can split "model
    (re)build" from simulator wall time. The first test after an RTL edit
    otherwise absorbs the whole rebuild and misleads timing triage.

    *sim_env* (tests.toml ``env``, F-5) is exported ahead of both halves: this
    single shell IS the sim's parent process on every sandbox path
    (verilator/icarus/cocotb), so exporting here is the one place that reaches
    the simulator without a per-run-half flag. Values are ``shlex.quote``d.
    """
    del marker  # retained in the private signature while callers/tests migrate
    return build_stage_script(
        build_cmd,
        attempt_token or new_attempt_token(),
        run_line=run_line,
        environment=sim_env,
    )


def parse_sva_errors(output: str) -> int:
    """Count SVA assertion errors in sim output."""
    try:
        from booley.flows.sim.result import count_sva_errors

        return count_sva_errors(output)
    except ImportError:
        # Fallback: count lines with common SVA error markers
        count = 0
        for line in output.splitlines():
            if "Assertion" in line and ("FAILED" in line or "ERROR" in line):
                count += 1
        return count


def _resolve_pre_run_commands(work_dir: Path | None = None) -> list[str]:
    """Project shell lines run before each sim run.

    ``[flows.sim].pre_run_commands`` (ADR 0039) carries a per-test
    non-RTL build step — e.g. compiling the selected test's firmware image
    with a cross-GCC — which the once-per-worktree post-setup hook cannot
    express. The lines run under the ``BOOLEY_*`` env contract assembled by
    :meth:`SimulateFlow._pre_run_env` before each run. Empty when unset.
    """
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        cfg = _load_rtl_config(work_dir)
        if cfg:
            val = config_section(cfg.get("flows", {}), "sim").get("pre_run_commands")
            return as_str_list(val)
    except ImportError:
        pass
    return []


def _resolve_max_rundir_bytes(work_dir: Path | None = None) -> int:
    """Per-run disk budget for the sim run directory, from booley.toml.

    ``[flows.sim].max_rundir_bytes`` caps how much the run directory may
    grow *during one run* before it is killed (SETUP-25: a default-on testbench
    tracer / ``$dumpfile`` / ``$fwrite`` sink once left 27GB and filled the disk,
    killing an in-flight synth). Growth, not total size — see
    :class:`booley.flows.sim.run_guard.DiskBudgetGuard` for why (F-23). ``0`` disables
    the guard. Forwarded to the builtin
    run-halves as ``--max-rundir-bytes``; mirrors the other ``_resolve_*`` knob
    readers. Defaults to :data:`_DEFAULT_MAX_RUNDIR_BYTES` when unset.
    """
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        cfg = _load_rtl_config(work_dir)
        if cfg:
            val = config_section(cfg.get("flows", {}), "sim").get("max_rundir_bytes")
            configured = as_int(val, _DEFAULT_MAX_RUNDIR_BYTES)
            return configured if configured is not None else _DEFAULT_MAX_RUNDIR_BYTES
    except ImportError:
        pass
    return _DEFAULT_MAX_RUNDIR_BYTES


def _resolve_sim_timeout_ms(work_dir: Path | None = None) -> int:
    """Per-test simulation timeout in ms, from booley.toml ``[flows.sim]``.

    ``[flows.sim].timeout_ms`` lets a project set a persistent per-test sim
    budget in ``booley.toml`` (mirrors ``[flows.synth].timeout_ms``), so
    a heavy core (e.g. a 400+MB ``vvp`` whose cold rebuild+run exceeds the 600s
    default under load) need not raise it on every call. Precedence lives in the
    caller: an explicit ``--timeout`` arg wins over this knob, which wins over
    :data:`_DEFAULT_TIMEOUT_MS`. Non-positive / unparseable values fall back.
    """
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        cfg = _load_rtl_config(work_dir)
        if cfg:
            val = config_section(cfg.get("flows", {}), "sim").get("timeout_ms")
            configured = as_int(val, _DEFAULT_TIMEOUT_MS)
            return max(1, configured if configured is not None else _DEFAULT_TIMEOUT_MS)
    except ImportError:
        pass
    return _DEFAULT_TIMEOUT_MS


def _resolve_sim_time_grace_s(work_dir: Path | None = None) -> float:
    """Frozen-simulator-clock watchdog grace, from booley.toml (F-25).

    ``[flows.sim].sim_time_grace_s`` bounds how long a cocotb run may sit
    at *exactly* 0.00 ns of simulation time before Booley aborts it with the
    run-loop-mismatch diagnosis instead of burning the whole ``timeout_ms``
    budget (ravenoc: cocotb 1.5.1's VPI loaded fine under Verilator 5.046, then
    no timed callback ever fired — 600 s of wall clock, zero sim time). ``0``
    disables the watchdog; anything else is a wall-clock second count.
    Defaults to :data:`run_guard.DEFAULT_SIM_TIME_GRACE_S`.
    """
    try:
        from booley.runtime.shared_infra import _load_rtl_config

        cfg = _load_rtl_config(work_dir)
        if cfg:
            val = config_section(cfg.get("flows", {}), "sim").get("sim_time_grace_s")
            configured = as_float(val, DEFAULT_SIM_TIME_GRACE_S)
            return max(
                0.0,
                configured if configured is not None else DEFAULT_SIM_TIME_GRACE_S,
            )
    except ImportError:
        pass
    return DEFAULT_SIM_TIME_GRACE_S


def _resolve_cycle_sentinels(work_dir: Path | None = None) -> list[str]:
    """Cycle-count prefixes from ``booley.toml [flows.sim]``.

    Each configured literal must be followed by a test name and a decimal
    integer as the line's final field. An empty list means the parser uses the
    built-in ``[SIM_CYCLES]`` prefix.
    """
    root = work_dir if work_dir is not None else Path.cwd()
    raw = _load_flow_config("sim", root).get("cycle_sentinels")
    return [sentinel for sentinel in as_str_list(raw) if sentinel]


def _get_test_names(work_dir: Path | str | None = None) -> dict[str, list[str]]:
    """Load test names from project config. Empty dict on failure."""
    try:
        if work_dir is not None:
            return load_test_configuration_field(work_dir, "tests")
        from booley.config.project_config import TEST_NAMES

        return TEST_NAMES
    except ImportError:
        return {}


def _get_test_skips(work_dir: Path | str | None = None) -> dict[str, list[str]]:
    """Load per-target known-hang skip lists (tests.toml ``skip``).

    Empty dict on failure — a project that declares no skips runs every test.
    """
    try:
        if work_dir is not None:
            return load_test_configuration_field(work_dir, "skip")
        from booley.config.project_config import TEST_SKIP

        return TEST_SKIP
    except ImportError:
        return {}


def _get_test_envs(work_dir: Path | str | None = None) -> dict[str, dict[str, str]]:
    """Per-Target simulator environment (tests.toml ``env``, F-5).

    Empty dict on failure — a Target that declares no ``env`` runs with the
    inherited environment, exactly as before the knob existed.
    """
    try:
        if work_dir is not None:
            return load_test_configuration_field(work_dir, "env")
        from booley.config.project_config import TEST_ENV

        return TEST_ENV
    except ImportError:
        return {}


def _get_test_selects(work_dir: Path | str | None = None) -> dict[str, str]:
    """Per-target explicit ``select`` templates (tests.toml ``select``).

    Only *explicitly declared* templates appear here (the rendering default,
    ``DEFAULT_TEST_SELECT``, does not) — exactly the set A2 must reject on a
    Cocotb Target, where selection is the ``COCOTB_TEST_FILTER`` env var and a
    plusarg template is a config contradiction. Empty dict on failure.
    """
    try:
        if work_dir is not None:
            return load_test_configuration_field(work_dir, "select")
        from booley.config.project_config import TEST_SELECT

        return TEST_SELECT
    except ImportError:
        return {}


def _filter_tests(tests: list[str], substring: str) -> list[str]:
    """Filter test names by substring match."""
    return [t for t in tests if substring in t]


def _selected_test_work_units(
    target: str,
    test_names: Mapping[str, list[str]],
    configured_skips: Mapping[str, list[str]],
    test_selector: str | None,
    skip_arg: str | None,
) -> int:
    """Simulator processes a non-cocotb Target will launch, at least one."""
    available = list(lookup_target_section(test_names, target) or [])
    skips = set(lookup_target_section(configured_skips, target) or [])
    if skip_arg:
        skips.update(item.strip() for item in skip_arg.split(",") if item.strip())
    if test_selector:
        matched = _filter_tests(available, test_selector)
        if not matched:
            return 1
        selected = [test for test in matched if test not in skips]
        return len(selected or matched)
    if not available:
        return 1
    selected = [test for test in available if test not in skips]
    return len(selected or available)


def _resolve_sim_campaign_work_units(
    work_dir: Path,
    target_arg: str,
    test_selector: str | None = None,
    skip_arg: str | None = None,
) -> int:
    """Count sequential simulator processes selected by one sim invocation.

    Cocotb batches a Target's selected tests into one process. Native HDL
    Targets launch one process per selected test. This is deliberately a cheap
    preflight read used by the outer MCP watchdog; validation remains the
    child Flow's responsibility.
    """
    targets = [item.strip() for item in target_arg.split(",") if item.strip()]
    if not targets:
        return 1
    try:
        cocotb_modules = fusesoc_registry.target_cocotb_modules(work_dir)
    except Exception:  # noqa: BLE001 — watchdog sizing degrades to native-HDL counting
        cocotb_modules = {}
    test_names = _get_test_names(work_dir)
    configured_skips = _get_test_skips(work_dir)
    units = 0
    for target in targets:
        if lookup_target_section(cocotb_modules, target):
            units += 1
            continue
        units += _selected_test_work_units(
            target,
            test_names,
            configured_skips,
            test_selector,
            skip_arg,
        )
    return max(1, units)


def _format_duration(seconds: float) -> str:
    """A duration that stays legible from ten milliseconds to hours.

    Cocotb unit tests finish in 10-20 ms, and ``f"{s:.1f}s"`` renders every one
    of them as ``0.0s`` — a whole suite looks instantaneous and per-test timing
    triage is impossible (F-39). Sub-second runs therefore get millisecond
    resolution; anything longer keeps the familiar one-decimal seconds, so long
    runs stay readable.
    """
    seconds = max(0.0, seconds)
    return f"{seconds * 1000:.0f}ms" if seconds < 1.0 else f"{seconds:.1f}s"


def _format_bytes(size: int) -> str:
    """Compact human size for a trace artifact (F-35)."""
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _trace_artifact(combined: str, work_dir: Path | str) -> tuple[str, int]:
    """``(display path, bytes)`` of the waveform this run produced (F-35).

    Reads the run-half's ``TRACE_OK:`` marker — the same line the trace
    enforcement already scrapes for presence — and turns it into something the
    reader can open. The path is rendered project-relative when it lives under
    the work dir; the size is best-effort (a boundary run's store may sit on a
    path this process cannot stat) and reported as 0 when unknown.
    """
    matches = _TRACE_OK_RE.findall(combined)
    if not matches:
        return "", 0
    path = Path(matches[-1])
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    display = str(path)
    try:
        relative = posix_relpath(path, work_dir)
    except (ValueError, OSError):  # different drive on Windows / unreadable cwd
        relative = ".."
    if not relative.startswith(".."):  # outside the project: absolute is clearer
        display = relative
    return display, size


def _trace_metadata(combined: str) -> tuple[str, int, int]:
    """Return ``(top_scope, signal_count, total_ticks)`` from the run-half."""
    matches = _TRACE_METADATA_RE.findall(combined)
    if not matches:
        return "", 0, 0
    try:
        metadata = decode_trace_metadata(matches[-1])
        return metadata.display_scope, metadata.signal_count, metadata.total_ticks
    except (BoundaryError, json.JSONDecodeError):
        return "", 0, 0


def _trace_line(test: TestResult) -> str | None:
    """The ``trace:`` report line for *test*, or None when it produced none."""
    if not test.trace_path:
        return None
    details = []
    if test.trace_bytes:
        details.append(_format_bytes(test.trace_bytes))
    if test.trace_signal_count:
        details.append(f"{test.trace_signal_count} signals")
    if test.trace_top_scope:
        details.append(f"scope {test.trace_top_scope}")
    if test.trace_total_ticks:
        details.append(f"{test.trace_total_ticks} ticks")
    suffix = f" ({', '.join(details)})" if details else ""
    return f"  trace: {test.trace_path}{suffix}"


def _build_display_lines(
    results: list[TargetResult],
    total_elapsed: float,
) -> list[str]:
    """Build rich display lines for the terminal Flow box."""
    targets_passed = sum(1 for r in results if r.passed)
    lines: list[str] = [
        f"{targets_passed}/{len(results)} targets passed, {total_elapsed:.1f}s",
    ]

    visible_results, omitted_line = cap_target_items(results)
    for cr in visible_results:
        lines.extend(_target_display_lines(cr))

    if omitted_line:
        lines.append(omitted_line)

    return lines


def _target_display_lines(result: TargetResult) -> list[str]:
    """Build the final display block for one completed Target."""
    icon = "?" if result.inconclusive else ("✓" if result.passed else "✗")
    tests_passed = sum(1 for test in result.tests if test.passed)
    tests_str = f"{tests_passed}/{len(result.tests)} tests" if len(result.tests) > 1 else ""
    elapsed = _format_duration(result.elapsed_s)
    suffix = f" ({tests_str})  {elapsed}" if tests_str else f"  {elapsed}"
    lines = [f"{icon} {result.target}{suffix}"]
    if not result.passed:
        _append_test_failure_lines(result.tests, lines)
    return lines


def _append_test_failure_lines(tests: list[TestResult], lines: list[str]) -> None:
    """Append per-test failure detail lines to the display output."""
    for tr in tests:
        if tr.passed:
            continue
        sva = f"  {tr.sva_errors} SVA errors" if tr.sva_errors else ""
        cycles = f"  {tr.cycles:,} cyc" if tr.cycles is not None else ""
        build = f"  (incl. {tr.build_s:.0f}s build)" if tr.build_s >= 1 else ""
        lines.append(f"  FAIL {tr.name:<20s} {_format_duration(tr.elapsed_s)}{build}{cycles}{sva}")

        if tr.error_tail:
            _append_error_excerpt(tr.error_tail, lines)


def _test_report_entry(test: TestResult) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": test.name,
        "passed": test.passed,
        "verdict": _test_verdict(test),
        "timed_out": test.timed_out,
        # F-39: 3 decimals — a 12ms cocotb test rounded to 0.0 at 1 decimal,
        # which erased every sub-second duration from the structured report.
        "elapsed_s": round(test.elapsed_s, 3),
        "build_s": round(test.build_s, 3),
        "cycles": test.cycles,
        "cycle_observation": test.cycle_status,
        "sva_errors": test.sva_errors,
        "error_tail": test.error_tail,
        "test_validated": test.test_validated,
        "phase_timings_s": dict(test.phase_timings_s),
        "resources": dict(test.resources),
    }
    if test.trace_path:
        entry["trace_path"] = test.trace_path
        entry["trace_bytes"] = test.trace_bytes
        entry["trace_top_scope"] = test.trace_top_scope
        entry["trace_signal_count"] = test.trace_signal_count
        entry["trace_total_ticks"] = test.trace_total_ticks
    if test.run_log_path:
        entry["artifacts"] = {"run_log": test.run_log_path}
    if test.workload_snapshot:
        entry["workload_fingerprint"] = test.workload_snapshot.get("fingerprint")
    if not test.test_validated:
        entry["validation_note"] = "test name was not validated against configs.toml"
    return entry


def _test_verdict(test: TestResult) -> str:
    """Return a stable machine-readable per-test verdict."""
    if test.passed:
        return "pass"
    if test.timed_out:
        return "timeout"
    if test.inconclusive:
        return "inconclusive"
    if test.elab_failed:
        return "elab_error"
    return "fail"


_ERROR_MARKERS = (
    "%Error",
    "FATAL",
    "Assertion",
    "TIMEOUT",
    "Timeout",
    "ERROR",
    "FAIL",
    "$fatal",
    "MISMATCH",
    "mismatch",
)
# Noise patterns that obscure real errors in fallback output
_NOISE_RE = re.compile(r"warning:|^VCD info:|^FST info:", re.IGNORECASE)


#: Lines kept after the last salient one in a failure excerpt. A testbench
#: prints its diagnosis *around* the marker line — fpu's mutation run read
#: ``f32_le / TEST FAILED / REFERENCE=1 CALCULATED=0``, and only the middle line
#: carries a marker — so the excerpt must not stop at the match.
_EXCERPT_CONTEXT_AFTER = 5

#: Lines of the run's TRUE tail an excerpt always keeps, however far above them
#: the last marker sits. The markers are case-sensitive and coarse, so the line
#: that actually states the verdict often carries none of them: a TB printing
#: ``ERROR: unable to open coverage db`` at t=1 and ``Result: 12 tests failed``
#: 5,000 lines later would otherwise render six lines about the coverage db and
#: amputate the failure summary — strictly worse than the blind tail it replaced.
_EXCERPT_TAIL_LINES = 10


def select_error_lines(text: str, limit: int) -> list[str]:
    """The most relevant ``<= limit`` lines of *text* for a failure excerpt.

    A blind tail is right only when the failure happens to be at the end. On a
    run whose testbench keeps printing after the error — or one that never
    failed at all — the tail is pure noise: fpu F-28 rendered 30 lines of
    ``TEST SUCCEEDED`` as the "error output" of an INCONCLUSIVE run. So anchor
    the window on the LAST line carrying an :data:`_ERROR_MARKERS` marker and
    keep :data:`_EXCERPT_CONTEXT_AFTER` lines past it for the surrounding
    diagnosis — but never at the cost of the end of the log: the last
    :data:`_EXCERPT_TAIL_LINES` lines are always kept too, with an explicit
    elision between the two when they do not meet. Falls back to the
    noise-filtered tail when nothing is salient (the inconclusive case, where
    "what did it actually print" IS the answer).
    """
    lines = text.strip().splitlines()
    if not lines:
        return []
    salient = [i for i, ln in enumerate(lines) if any(m in ln for m in _ERROR_MARKERS)]
    if not salient:
        non_noise = [ln for ln in lines if ln.strip() and not _NOISE_RE.search(ln)]
        return (non_noise or lines)[-limit:]

    end = min(len(lines), salient[-1] + 1 + _EXCERPT_CONTEXT_AFTER)
    if end >= len(lines):
        return lines[max(0, end - limit) : end]  # window already reaches the end
    if limit <= _EXCERPT_TAIL_LINES + 1:
        # Too small a budget to carry both halves and announce the gap; the end
        # of the log is the half we must not lose.
        return lines[-limit:]
    tail_n = min(_EXCERPT_TAIL_LINES, len(lines) - end)
    head = lines[max(0, end - (limit - tail_n - 1)) : end]
    omitted = len(lines) - end - tail_n
    if omitted <= 0:  # the two halves touch — no gap to announce
        return lines[max(0, len(lines) - limit) :]
    selected = [*head, f"... ({omitted} lines omitted) ...", *lines[-tail_n:]]
    first = lines[salient[0]]
    if first not in selected:
        selected = [first, *selected]
    return selected[:limit]


def _append_error_excerpt(error_tail: str, lines: list[str]) -> None:
    """Append a salient excerpt of error output to display lines."""
    err_lines = [ln.strip() for ln in error_tail.splitlines() if ln.strip()]
    salient = [ln for ln in err_lines if any(m in ln for m in _ERROR_MARKERS)]
    if salient:
        to_show = salient[:_MAX_DISPLAY_ERRORS]
    else:
        # Filter out compiler/EDA-tool noise before falling back to tail
        non_noise = [ln for ln in err_lines if not _NOISE_RE.search(ln)]
        to_show = (non_noise or err_lines)[-_MAX_DISPLAY_ERRORS:]
    for el in to_show:
        display = el[:97] + "..." if len(el) > 100 else el
        lines.append(f"    {display}")
    remaining = len(salient) - _MAX_DISPLAY_ERRORS if salient else 0
    if remaining > 0:
        lines.append(f"    ... {remaining} more error(s)")


def _determine_pass_fail(
    combined: str,
    proc: SubprocessResult,
) -> tuple[bool, bool]:
    """Determine pass/fail from sim output and process result.

    Returns (passed, inconclusive).
    """
    # count_sva_errors already excludes harness trace-incident lines so a
    # missing waveform is not miscounted as a DUT SVA failure (QA_REPORT B5.1).
    sva_errors = parse_sva_errors(combined)

    try:
        summary = parse_summary_line(combined)
    except ValueError:
        summary = None

    if summary is not None:
        passed = summary["passed"]
        inconclusive = summary.get("inconclusive", False)
    elif proc.returncode == 0 and sva_errors == 0:
        # No summary, clean exit — inconclusive
        passed, inconclusive = False, True
    else:
        passed, inconclusive = False, False

    if proc.timed_out:
        passed, inconclusive = False, False

    return passed, inconclusive


def _test_status_line(tr: TestResult) -> str:
    """One-line test status (name, PASS/FAIL/INCO, cycle count, elapsed).

    Shared between the streamed per-target detail and the end-of-report
    headline block so the cycle-count lines survive tail-truncation verbatim.
    """
    if tr.inconclusive:
        status = "INCO"
    elif tr.passed:
        status = "PASS"
    else:
        status = "FAIL"
    cycles_str = f"{tr.cycles:>8,} cycles" if tr.cycles is not None else "             "
    # Keep the compact display silent for sub-second warm-cache builds; the
    # millisecond value remains available in structured phase telemetry.
    build_str = f"  (incl. {tr.build_s:.0f}s build)" if tr.build_s >= 1 else ""
    # F-39: millisecond resolution below 1s, so 10-20ms cocotb tests stop all
    # rendering as an indistinguishable "0.0s".
    elapsed_str = _format_duration(tr.elapsed_s)
    return f"  {tr.name:<20s} {status}   {cycles_str}  {elapsed_str:>7s}{build_str}"


def _cocotb_verdict_names(
    selected: list[str],
    results: Any,
    target: str,
) -> list[str]:
    """The names a cocotb batch must produce a verdict for.

    The selected set when there is one; otherwise whatever results.xml reported
    (an unfiltered run), falling back to the Target itself so a batch that
    produced nothing at all still has exactly one entry to hang a verdict on.
    """
    if selected:
        return list(selected)
    if results is not None and results.tests:
        return [t.name for t in results.tests]
    return [target]


def _append_test_output_line(
    tr: TestResult,
    output_lines: list[str],
    run_log_fresh: bool = False,
) -> None:
    """Append a formatted output line (and error tail) for a single test result.

    *run_log_fresh* — the caller vouches that THIS invocation wrote the
    target's run.log. Only then does the omission marker say "see run.log";
    a benchmark sweep found stale pointers sending agents to a leftover log
    from an earlier build that read "TEST PASSED" while the EDA tool reported a
    failure.
    """
    output_lines.append(_test_status_line(tr))
    trace = _trace_line(tr)
    if trace:
        output_lines.append(trace)
    if tr.error_tail:
        # Bound the excerpt so one verbose failure can't eat the whole MCP
        # stdout window; the full output survives in the persisted run.log.
        # The cap scales with a raised BOOLEY_MCP_MAX_STDOUT_BYTES budget.
        max_lines = output_budget.scaled(_MAX_EXCERPT_LINES)
        tail_lines = tr.error_tail.splitlines()
        shown = tail_lines[-max_lines:]
        omitted = len(tail_lines) - len(shown)
        # An INCONCLUSIVE run did not fail — it produced no verdict at all, and
        # its output is very often 30 lines of the testbench happily succeeding.
        # Labelling that "error output" presents noise as a diagnosis (F-28).
        if tr.inconclusive:
            head = f"  --- output tail (no pass/fail sentinel found, last {len(shown)} lines) ---"
            foot = "  --- end output tail ---"
        else:
            head = f"  --- error output (last {len(shown)} lines) ---"
            foot = "  --- end error output ---"
        output_lines.append(head)
        if omitted > 0:
            pointer = ", see run.log" if run_log_fresh else ""
            output_lines.append(f"  ... ({omitted} lines omitted{pointer})")
        output_lines.append("\n".join(shown))
        output_lines.append(foot)


def _append_batch_output_lines(
    test_results: list[TestResult],
    output_lines: list[str],
    run_log_fresh: bool = False,
) -> None:
    """Per-test status lines, with a *shared* error tail printed only once.

    A cocotb batch runs every selected test in one simulator process, so a
    failure of the process itself — an import error, a segfault, no results.xml
    — hands all of them the very same run-level error. Echoing it once per test
    drowned the summary in nine copies of one message and made a batch-level
    failure read like nine independent ones (F-6). Print it once, and say how
    many tests it accounts for.
    """
    shared = Counter(tr.error_tail for tr in test_results if tr.error_tail)
    printed: set[str] = set()
    for tr in test_results:
        if tr.error_tail in printed:
            output_lines.append(_test_status_line(tr))  # status only; error already shown
            continue
        if tr.error_tail:
            printed.add(tr.error_tail)
        _append_test_output_line(tr, output_lines, run_log_fresh)
        count = shared.get(tr.error_tail, 0)
        if count > 1:
            output_lines.append(
                f"  (same error for all {count} selected tests — the batch failed as a whole)"
            )


class SimulateFlow(StandaloneMixin, BooleyFlow):
    """Run RTL simulation for one or more Targets."""

    name: str = "sim"
    description: str = (
        "Run RTL simulation for one or more Targets. Set elab_only=true to "
        "compile, elaborate, and link the ordinary untraced simulator image "
        "without running tests (CLI: --elab-only; --build-only is an alias). "
        "Do NOT use --trace for initial pass/fail checks — tracing adds "
        "overhead and is only useful after a failure, when you need waveforms "
        "for debugging via the B-Wave (`bwave`) MCP tool."
    )
    code_modifying: bool = False

    def __init__(self) -> None:
        self._prepared_builds: dict[str, PreparedSimulationBuild] = {}
        self._build_attempt_tokens: dict[str, str] = {}
        self._adapter_attempts: dict[str, AdapterTransportIdentity] = {}
        super().__init__()

    # Simulation is always admitted as a heavy Session Runtime job.
    def _resolve_job_class(self) -> str:
        """Simulation is a heavy Session Runtime workload."""
        return job_slots.CLASS_HEAVY

    satisfies: ClassVar[list[str]] = [
        "elab_pass",
        "elaborate_standalone",
        "sim_pass",
        "cycle_count",
    ]
    satisfies_args: ClassVar[dict[str, str]] = {
        "elab_pass": "--elab-only",
        "elaborate_standalone": "--elab-only --standalone",
    }
    # MCP server wraps the whole eda_tool subprocess.  Keep that outer budget
    # long enough for the child sim timeout plus one non-FIFO trace retry.
    default_timeout: ClassVar[int] = (_DEFAULT_TIMEOUT_MS // 1000) * 2 + _TRACE_CLEANUP_MARGIN_S

    def _add_args(self, parser: Any) -> None:
        # tb_top left the surface (ADR 0021): a sim Target's `toplevel` IS its
        # TB top, so it comes from the resolved Target (tb_top_for_target), not
        # a per-call arg.
        self._add_elaboration_args(parser)
        parser.add_argument(
            "--test",
            default=None,
            help="Run specific test by name (substring match)",
        )
        parser.add_argument(
            "--skip",
            default=None,
            help="Comma-separated test names to exclude (exact match). Adds to "
            "any [flows.sim] / tests.toml 'skip' list. Use to dodge "
            "known-hanging tests that burn the full wall-clock budget.",
        )
        self._add_run_control_args(parser)

    @staticmethod
    def _add_elaboration_args(parser: Any) -> None:
        """Add the compile-only Simulation mode and its optional sweep."""
        parser.add_argument(
            "--elab-only",
            "--build-only",
            dest="elab_only",
            action="store_true",
            help="Compile, elaborate, and link the ordinary untraced simulation "
            "image without running tests, Cocotb Python, Pre-Run Commands, or "
            "tracing. --build-only is a permanent alias.",
        )
        parser.add_argument(
            "--standalone",
            action="store_true",
            help="With --elab-only, also check every RTL module from its "
            "declaring file using [flows.sim].standalone_frontend.",
        )

    @staticmethod
    def _add_run_control_args(parser: Any) -> None:
        """Add run-stage tracing, reporting, cleanup, and timeout controls."""
        parser.add_argument(
            "--trace",
            action="store_true",
            help="Enable waveform trace (debugging only — do not use for pass/fail checks)",
        )
        parser.add_argument(
            "--result-verbosity",
            choices=["compact", "full"],
            default="compact",
            help="Cocotb result detail on stdout; full XML/JSON artifacts are always retained",
        )
        # --trace-scope left the surface (ADR 0022, 2026-06-23): the --trace
        # overlay .core traces the full hierarchy at a fixed depth, so there is no
        # per-call scope knob. Scoping, when a specialist needs it, lives on the
        # specialist's own surface, not the built-in simulate one.
        parser.add_argument(
            "--no-kill",
            action="store_true",
            help="Skip zombie process cleanup",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print commands as JSON without executing",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=None,
            help="Per-test timeout in milliseconds. Precedence: this arg > "
            "[flows.sim].timeout_ms in booley.toml > 600000 default.",
        )

    def _build_command(self) -> list[str]:
        # Not used — _run() is overridden for multi-config logic
        return []

    def _interpret_result(self, result: SubprocessResult) -> McpToolResult:
        # Not used — _run() handles interpretation directly
        return McpToolResult()

    def _effective_timeout_ms(self) -> int:
        """Resolve the per-test timeout in ms.

        Precedence: explicit ``--timeout`` CLI/MCP arg > ``[flows.sim]``
        ``timeout_ms`` in booley.toml (F4) > :data:`_DEFAULT_TIMEOUT_MS`. Mirrors
        ``AsicSynthesizeFlow._timeout_ms`` so a large core can persist a higher
        sim budget instead of passing ``--timeout`` on every call.
        """
        if self.args.timeout is not None:
            return max(1, int(self.args.timeout))
        return _resolve_sim_timeout_ms(Path(self.args.work_dir))

    def _get_timeout(self) -> int:
        """Wrapper timeout in seconds, derived from the effective timeout (ms).

        The child runner owns the actual simulator wall-clock budget.  When
        tracing is on, keep the wrapper alive long enough for FIFO closure,
        bwave finalization, and report writing after the sim budget expires.
        """
        timeout_s = self._effective_timeout_ms() // 1000
        if self.args.trace:
            timeout_s += _TRACE_CLEANUP_MARGIN_S
        return timeout_s

    def _cocotb_module_for_target(self, target: str) -> str | None:
        """The Target's declared ``cocotb_module``, from a cheap ``.core`` read.

        ADR 0034 decision 2: cocotb-ness is detected from the Target's flow
        options, never marked in tests.toml.
        This cheap read backs validation, dry-run previews and the batch-vs-loop
        dispatch *before* resolution; the run itself re-reads the *resolved*
        flow options (``ResolvedTarget.cocotb_module``) as the authority
        (ADR 0022 decision 6's enumerate-vs-resolve line). ``None`` for a
        non-cocotb Target or when the read fails (degrades to the SV path).
        """
        try:
            handle = self._target_handle(target)
            options = inspect_target(self.args.work_dir, handle).flow_options
        except Exception:  # noqa: BLE001 — best-effort cheap read; degrades to non-cocotb
            return None
        module = options.get("cocotb_module")
        return module if isinstance(module, str) and module else None

    def is_cocotb_target(self, target: str) -> bool:
        """True when *target* declares a ``cocotb_module`` (a Cocotb Target)."""
        return self._cocotb_module_for_target(target) is not None

    def _target_sim_env(self, target: str) -> dict[str, str]:
        """*target*'s declared simulator environment (tests.toml ``env``, F-5).

        The Booley answer to an env-parameterized testbench — a cocotb module
        that branches on ``os.getenv("FLAVOR")``, or an SV TB whose C++ main
        reads a config var. Neither existing mechanism covers it:
        ``pre_run_commands`` run in a *separate* shell whose exports die with
        it, and the test filter selects tests, not configuration. Without this
        knob a port has to edit a testbench it does not own.

        Per-Target because that is where the variance lives: the same cocotb
        module run under two RTL flavours is two Targets, and each declares its
        own value. ``{}`` when the Target declares none — the run then inherits
        the ambient environment exactly as before.
        """
        return dict(lookup_target_section(_get_test_envs(self.args.work_dir), target) or {})

    def _sim_env_preview_lines(self, target: str) -> list[str]:
        """Shell export lines for the Target's configured simulator environment."""
        return [
            f"export {name}={shlex.quote(value)}"
            for name, value in self._target_sim_env(target).items()
        ]


    def _validate_interactive_args(
        self,
        targets: list[str],
    ) -> McpToolResult | None:
        """Interactive-Mode guard: a Target must be selected.

        ``tb_top`` left the surface (ADR 0021) — a sim Target's ``toplevel`` is
        its TB top, so it comes from the resolved Target (via
        ``tb_top_for_target``), not a per-call ``--tb-top``. Nothing to validate
        here beyond config selection; the Target resolution downstream raises a
        clear error if a selected Target is malformed.
        """
        if not targets:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text=(
                    "sim: --target is required when multiple or zero "
                    "Targets are available. Pass --target <name>; available "
                    "Targets are the .core sim Targets in .booley_project/."
                ),
            )
        return None

    def _resolve_run_targets(
        self,
    ) -> McpToolResult | tuple[list[str], dict[str, list[str]]]:
        """Resolve the execution selection and requested Targets for this run.

        Returns a terminal ``McpToolResult`` on the first validation error;
        otherwise ``(targets, test_names_map)`` for the caller to continue with.
        """
        # The Target owns its top and trace hierarchy; validate selection here.
        if not self._flow_enabled():
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="sim is disabled ([flows.sim].enabled = false).",
            )
        targets_or_error = self._resolve_requested_targets()
        if isinstance(targets_or_error, McpToolResult):
            return targets_or_error
        targets = targets_or_error
        err = self._validate_interactive_args(targets)
        if err is not None:
            return err
        return targets, _get_test_names(self.args.work_dir)

    def _maybe_dispatch_special_run(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> McpToolResult | None:
        """Handle validation + the dry-run path; ``None`` means "continue".

        Validates ``--test`` the way ``--target`` is already validated
        (``resolve_target_selection`` raises ``UnknownTargetError``) — an
        unknown name here otherwise falls through to a plusarg-less run of
        the TB's default test and reports a false PASS (a CI green on a typo).
        """
        test_error = self._validate_test_selector(targets, test_names_map)
        if test_error is not None:
            return test_error

        cocotb_error = self._validate_cocotb_targets(targets)
        if cocotb_error is not None:
            return cocotb_error

        runnable_error = self._validate_runnable_tests(targets, test_names_map)
        if runnable_error is not None:
            return runnable_error

        if self.args.dry_run:
            return self._handle_dry_run(targets, test_names_map)

        return None

    def _run(self) -> McpToolResult:  # noqa: PLR0911, PLR0912, PLR0915 — linear multi-Target orchestration
        """Execute simulation across configs and tests."""
        mode_error = self._validate_mode_args()
        if mode_error is not None:
            return mode_error
        if self.args.elab_only:
            return self._run_elab_only()
        total_start = time.monotonic()
        resolution_started = total_start
        resolved = self._resolve_run_targets()
        if isinstance(resolved, McpToolResult):
            return resolved
        targets, test_names_map = resolved
        resolution_s = time.monotonic() - resolution_started

        special_result = self._maybe_dispatch_special_run(targets, test_names_map)
        if special_result is not None:
            return special_result

        all_results: list[TargetResult] = []
        output_lines: list[str] = []
        overall_pass = True
        # The report writer composes the per-target compile command from the
        # same inputs a --dry-run uses; stash the resolved test-name map so it
        # doesn't have to re-derive it.
        self._test_names_map = test_names_map
        # Reserve the final report directory before the first long Target.
        # Checkpoints and the eventual report.json then share one invocation,
        # even when the outer watchdog kills the process between Targets.
        self.reserve_invocation_dir()
        self._write_progress_report(targets, all_results, phase="starting")

        baseline_result = self._run_cycle_count_baselines(targets, test_names_map)
        if isinstance(baseline_result, McpToolResult):
            return baseline_result
        self._baseline_results = baseline_result

        for target in targets:
            try:
                target_result = self._run_resolved_target(
                    target,
                    test_names_map,
                    output_lines,
                )
            except MissingExecutableError as exc:
                # F-32: no simulator ran, so nothing was observed about the
                # design. Abandon the sweep and report a Flow error — every
                # remaining Target would hit the same missing binary.
                return self._missing_executable_result(exc, target)
            except SimulationBuildInfrastructureError as exc:
                return self._build_infrastructure_result(exc)
            self._attach_workload_snapshots(target_result)
            all_results.append(target_result)
            if not target_result.passed:
                overall_pass = False
            self._persist_target_outcome(target_result)
            self._write_progress_report(targets, all_results, phase="running")
            if len(targets) > 1:
                for line in _target_display_lines(target_result):
                    self.emit_completion(line, repeats_at_end=True)

        total_elapsed = time.monotonic() - total_start
        report_text = self._format_summary(all_results, output_lines, overall_pass)

        # Run-level eda_tool report key (base.write_report): the unique raw
        # EDA tools this run resolved to, ", "-joined in resolution order.
        eda_tools = [r.eda_tool for r in all_results if r.eda_tool]
        if eda_tools:
            self._eda_tool = ", ".join(dict.fromkeys(eda_tools))

        targets_passed = sum(1 for r in all_results if r.passed)
        any_elab_failed = any(r.elab_failed for r in all_results)
        detail: dict[str, Any] = {
            "targets": len(all_results),
            "targets_passed": targets_passed,
            "elapsed_s": round(total_elapsed, 1),
            "resolution_s": round(resolution_s, 3),
            "phase_timings_s": {
                result.target: dict(result.phase_timings_s) for result in all_results
            },
            "elaboration": {
                result.target: [
                    entry
                    for test in result.tests
                    if (entry := _build_outcome_entry(test.build_outcome)) is not None
                ]
                for result in all_results
            },
            "cycle_counts": [
                {
                    "target": result.target,
                    "target_identity": result.target_identity,
                    "test": test.name,
                    "verdict": _test_verdict(test),
                    "cycles": test.cycles,
                    "observation": test.cycle_status,
                }
                for result in all_results
                for test in result.tests
            ],
        }
        if any_elab_failed:
            detail["elab_failed"] = True
        # Per-target artifact pointers, keyed by target so a multi-target run
        # stays unambiguous. This is the copy that survives into the MCP
        # ``structuredContent`` (base.write_report carries ``detail`` through),
        # which the stdout headline block does not.
        for r in all_results:
            block = self._artifacts_for(r.target, r)
            if block:
                # ``report`` too: the detail copy is what an agent receives over
                # MCP, and without it the block names every file the run wrote
                # except the one holding the rest of the numbers.
                if self.args.report_dir is not None:
                    block["report"] = posix_relpath(
                        self.args.report_dir / f"sim_{r.target}.json",
                        self.args.work_dir,
                    )
                detail.setdefault("artifacts", {})[r.target] = block
        self._write_progress_report(
            targets,
            all_results,
            phase="complete",
            complete=True,
        )
        return McpToolResult(
            exit_code=EXIT_SUCCESS if overall_pass else EXIT_FAILURE,
            criterion_key=f"sim_pass_{targets[0]}" if len(targets) == 1 else "",
            criterion_met=overall_pass,
            display_lines=_build_display_lines(all_results, total_elapsed),
            detail=detail,
            report_text=report_text,
        )

    def _validate_mode_args(self) -> McpToolResult | None:
        """Reject run-stage arguments that have no meaning in elab-only mode."""
        if self.args.standalone and not self.args.elab_only:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="sim: --standalone requires --elab-only; add --elab-only or remove --standalone.",
            )
        if not self.args.elab_only:
            return None
        conflicts = (
            ("--test", self.args.test is not None),
            ("--skip", self.args.skip is not None),
            ("--trace", self.args.trace),
            ("--result-verbosity full", self.args.result_verbosity == "full"),
            ("--no-kill", self.args.no_kill),
        )
        for argument, active in conflicts:
            if active:
                return McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"sim: {argument} conflicts with --elab-only; remove "
                        f"{argument} or omit --elab-only."
                    ),
                )
        return None

    def _run_elab_only(self) -> McpToolResult:
        """Compile, elaborate, and link selected Simulation Targets without tests."""
        preflight = self._elab_only_preflight()
        if isinstance(preflight, McpToolResult):
            return preflight
        targets = preflight
        results = self._run_elab_only_campaign(targets)
        missing = self._elab_only_missing_executable(results)
        if missing is not None:
            target, exc = missing
            result = self._missing_executable_result(exc, target)
            result.detail["mode"] = "elab_only"
            result.detail["targets"] = [
                self._elab_only_detail(target_result) for target_result in results
            ]
            artifacts = {
                target_result.target: {"log": target_result.log_path}
                for target_result in results
                if target_result.log_path
            }
            if artifacts:
                result.detail["artifacts"] = artifacts
            self._write_elab_only_progress(targets, results, phase="complete", complete=True)
            return result
        exit_code, standalone = self._run_optional_standalone(targets, results)
        return self._elab_only_result(targets, results, exit_code, standalone)

    @staticmethod
    def _elab_only_missing_executable(
        results: list[ElabOnlyTargetResult],
    ) -> tuple[str, MissingExecutableError] | None:
        """Return the first absent binary from an inconclusive build attempt."""
        for result in results:
            if result.outcome.failure_kind != "infrastructure":
                continue
            binary = find_missing_executable(result.outcome.output)
            if binary is not None:
                return result.target, MissingExecutableError(
                    binary,
                    context=result.outcome.output,
                )
        return None

    def _elab_only_preflight(self) -> list[str] | McpToolResult:
        """Validate compile-only mode and return its selected Targets."""
        if not self._flow_enabled():
            return McpToolResult(
                exit_code=EXIT_ERROR,
                report_text="sim is disabled ([flows.sim].enabled = false).",
                detail={"mode": "elab_only"},
            )
        targets_or_error = self._resolve_requested_targets()
        if isinstance(targets_or_error, McpToolResult):
            targets_or_error.detail["mode"] = "elab_only"
            return targets_or_error
        targets = targets_or_error
        target_error = self._validate_interactive_args(targets)
        if target_error is not None:
            target_error.detail["mode"] = "elab_only"
            return target_error
        if self.args.dry_run:
            return self._handle_elab_only_dry_run(targets)
        return targets

    def _run_elab_only_campaign(
        self,
        targets: list[str],
    ) -> list[ElabOnlyTargetResult]:
        """Run and checkpoint each requested build-only Target."""
        self.reserve_invocation_dir()
        results: list[ElabOnlyTargetResult] = []
        self._write_elab_only_progress(targets, results, phase="starting")
        for target in targets:
            result = self._run_one_elab_only(target)
            results.append(result)
            self._record_elab_only_criterion(result)
            self._write_elab_only_target_report(result)
            if self.state._file_path is not None:
                self.state.save()
            self._write_elab_only_progress(targets, results, phase="running")
        return results

    def _run_optional_standalone(
        self,
        targets: list[str],
        results: list[ElabOnlyTargetResult],
    ) -> tuple[int, _StandaloneOutcome | None]:
        """Merge the optional module sweep into the campaign exit status."""
        exit_code = self._elab_only_exit_code(results)
        if not self._standalone_requested():
            return exit_code, None
        standalone = self._run_standalone_check(
            targets,
            primary_ok=all(result.outcome.passed for result in results),
        )
        if standalone.eda_tool_failed:
            exit_code = EXIT_ERROR
        elif not standalone.passed and exit_code == EXIT_SUCCESS:
            exit_code = EXIT_FAILURE
        return exit_code, standalone

    def _elab_only_result(
        self,
        targets: list[str],
        results: list[ElabOnlyTargetResult],
        exit_code: int,
        standalone: _StandaloneOutcome | None,
    ) -> McpToolResult:
        """Compose the final compile-only report and MCP result."""
        passed = sum(result.outcome.passed for result in results)
        verdict = {EXIT_SUCCESS: "PASS", EXIT_FAILURE: "FAIL", EXIT_ERROR: "ERROR"}[exit_code]
        lines = [self._elab_only_result_line(result) for result in results]
        if standalone is not None:
            lines.extend(standalone.lines)
        lines += ["", f"RESULT: {verdict} ({passed}/{len(results)})"]
        report_text = "\n".join(lines)
        print(report_text)
        eda_tools = [result.eda_tool for result in results if result.eda_tool]
        if eda_tools:
            self._eda_tool = ", ".join(dict.fromkeys(eda_tools))
        detail: dict[str, Any] = {
            "mode": "elab_only",
            "targets": [self._elab_only_detail(result) for result in results],
        }
        artifacts = {
            result.target: {"log": result.log_path} for result in results if result.log_path
        }
        if artifacts:
            detail["artifacts"] = artifacts
        display_lines = [
            f"{result.target}: {self._elab_only_status(result)}" for result in results
        ]
        if standalone is not None:
            detail["standalone"] = standalone.detail
            display_lines.append(standalone.display)
        self._write_elab_only_progress(targets, results, phase="complete", complete=True)
        return McpToolResult(
            exit_code=exit_code,
            criterion_key=(f"elab_pass_{targets[0]}" if len(targets) == 1 else ""),
            criterion_met=len(results) == 1 and results[0].outcome.passed,
            display_lines=display_lines,
            detail=detail,
            report_text=report_text,
        )

    def _run_one_elab_only(self, target: str) -> ElabOnlyTargetResult:
        """Run one canonical untraced Simulation build and archive its output."""
        started = time.monotonic()
        work_root = edam_layer.work_root_for(self.args.work_dir, "sim", target)
        self._open_run_log(target, work_root)
        prepared = self._prepare_elab_only_target(target, started)
        if isinstance(prepared, ElabOnlyTargetResult):
            return prepared
        self._register_prepared_build(target, prepared)
        return self._execute_elab_only_build(target, prepared)

    def _prepare_elab_only_target(
        self,
        target: str,
        started: float,
    ) -> PreparedSimulationBuild | ElabOnlyTargetResult:
        """Prepare one Target or return its expected setup-error result."""
        try:
            return prepare_simulation_build(
                self._target_handle(target),
                environment=self._target_sim_env(target),
            )
        except SimulationBuildPreparationError as exc:
            logger.debug("sim elab-only setup failed for %s", target, exc_info=True)
            outcome = setup_failure_outcome(
                f"setup failed: {exc}",
                elapsed_s=time.monotonic() - started,
            )
            result = ElabOnlyTargetResult(target=target, outcome=outcome)
            result.log_path = self._persist_elab_only_log(target, outcome.output)
            return result

    def _register_prepared_build(
        self,
        target: str,
        prepared: PreparedSimulationBuild,
    ) -> None:
        """Register prepared Target state used by reports and optional sweeps."""
        self._prepared_builds[target] = prepared
        self._remember_resolved_target(target, prepared.resolved)
        self._record_run_log_dir(target, prepared.build_root)
        self._record_eda_tool(target, prepared.eda_tool)

    def _execute_elab_only_build(
        self,
        target: str,
        prepared: PreparedSimulationBuild,
    ) -> ElabOnlyTargetResult:
        """Execute and classify one already-prepared build-only Target."""
        token = new_attempt_token()
        command = [
            "sh",
            "-c",
            build_stage_script(
                prepared.make_argv,
                token,
                environment=prepared.environment,
            ),
        ]
        proc = self._execute_boundary(
            command,
            timeout=max(1, self._effective_timeout_ms() // 1000),
        )
        outcome = classify_build_outcome(proc, token)
        result = ElabOnlyTargetResult(
            target=target,
            target_identity=prepared.target_identity,
            eda_tool=prepared.eda_tool,
            toplevel=prepared.toplevel,
            compile_command=shlex.join(command),
            fileset={name: list(paths) for name, paths in prepared.fileset.items()},
            outcome=outcome,
        )
        result.log_path = self._persist_elab_only_log(target, outcome.output)
        return result

    def _persist_elab_only_log(self, target: str, output: str) -> str:
        """Archive complete build output outside the mutable shared cache."""
        invocation_dir = self.reserve_invocation_dir()
        if invocation_dir is not None:
            log_dir = invocation_dir / "artifacts" / _artifact_path_component(f"sim_{target}")
        else:
            token = new_attempt_token()[:12]
            log_dir = edam_layer.work_root_for(self.args.work_dir, "sim", target) / (
                f"elab-only-{token}"
            )
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            path = write_run_log(log_dir, output, max_bytes=None)
        except OSError:
            logger.debug("could not persist elab-only log for %s", target, exc_info=True)
            return ""
        return posix_relpath(path, self.args.work_dir)

    def _record_elab_only_criterion(self, result: ElabOnlyTargetResult) -> None:
        """Write elaboration evidence only when a real design verdict exists."""
        if self.args.state_file is None or result.outcome.verdict is None:
            return
        self.set_criterion(
            f"elab_pass_{result.target}",
            result.outcome.passed,
            source_target=result.target,
            detail={
                "mode": "elab_only",
                "target": result.target,
                "elapsed_s": round(result.outcome.elapsed_s, 3),
                "error_gist": (
                    extract_error_gist(result.outcome.output)
                    if result.outcome.design_failed
                    else ""
                ),
            },
        )

    @staticmethod
    def _elab_only_exit_code(results: list[ElabOnlyTargetResult]) -> int:
        if any(result.outcome.failure_kind == "infrastructure" for result in results):
            return EXIT_ERROR
        if any(result.outcome.design_failed for result in results):
            return EXIT_FAILURE
        return EXIT_SUCCESS

    @staticmethod
    def _elab_only_status(result: ElabOnlyTargetResult) -> str:
        if result.outcome.passed:
            return "PASS"
        return "FAIL" if result.outcome.design_failed else "ERROR"

    def _elab_only_result_line(self, result: ElabOnlyTargetResult) -> str:
        status = self._elab_only_status(result)
        line = f"[sim:elab-only] {result.target} {status} {result.outcome.elapsed_s:.1f}s"
        if not result.outcome.passed and result.outcome.reason:
            line += f" — {result.outcome.reason}"
        if result.log_path:
            line += f" (log: {result.log_path})"
        return line

    def _elab_only_detail(self, result: ElabOnlyTargetResult) -> dict[str, Any]:
        return {
            "target": result.target,
            "target_identity": result.target_identity,
            "eda_tool": result.eda_tool,
            "toplevel": result.toplevel,
            "compile_command": result.compile_command,
            "fileset": result.fileset,
            "elapsed_s": round(result.outcome.elapsed_s, 3),
            "passed": result.outcome.passed,
            "verdict": result.outcome.verdict,
            "failure_class": result.outcome.failure_kind,
            "reason": result.outcome.reason,
            "log": result.log_path,
        }

    def _write_elab_only_target_report(self, result: ElabOnlyTargetResult) -> None:
        report_dir = self.args.report_dir
        if report_dir is None:
            return
        report = {
            "flow": "sim",
            "mode": "elab_only",
            "timestamp": utc_now_rfc3339(),
            **self._elab_only_detail(result),
        }
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"sim_{result.target}.json"
        invocation_dir = self.reserve_invocation_dir()
        if invocation_dir is not None:
            _atomic_write_json(invocation_dir / "targets" / path.name, report)
        _atomic_write_json(path, report)

    def _write_elab_only_progress(
        self,
        targets: list[str],
        results: list[ElabOnlyTargetResult],
        *,
        phase: str,
        complete: bool = False,
    ) -> None:
        """Checkpoint an Elaboration Check campaign after every Target."""
        invocation_dir = self.reserve_invocation_dir()
        if invocation_dir is None:
            return
        completed = [result.target for result in results]
        payload = {
            "flow": self.name,
            "mode": "elab_only",
            "run_id": os.environ.get("BOOLEY_RUN_ID", ""),
            "timestamp": utc_now_rfc3339(),
            "phase": phase,
            "complete": complete,
            "targets": list(targets),
            "completed_targets": completed,
            "pending_targets": [target for target in targets if target not in completed],
            "detail": {result.target: self._elab_only_detail(result) for result in results},
        }
        _atomic_write_json(invocation_dir / "progress.json", payload)

    def _handle_elab_only_dry_run(self, targets: list[str]) -> McpToolResult:
        commands = {target: self._elab_only_dry_command(target) for target in targets}
        print(json.dumps(commands, indent=2))
        return McpToolResult(
            exit_code=EXIT_SUCCESS,
            report_text=f"Dry run: {len(commands)} elab-only build command(s)",
            detail={"mode": "elab_only", "commands": commands},
        )

    def _elab_only_dry_command(self, target: str) -> list[str]:
        build_root = edam_layer.work_root_for(self.args.work_dir, "sim", target)
        try:
            handle = self._target_handle(target)
            setup = fusesoc_registry.setup_command(
                handle.selector,
                project_root=handle.project_root,
                build_root=build_root,
                vlnv=handle.vlnv,
            )
        except fusesoc_registry.TargetResolutionError as exc:
            return [f"ERROR: sim elab-only dry-run: {exc}"]
        rel = edam_layer.relpath_for_make(build_root, self.args.work_dir)
        parts = [
            *self._sim_env_preview_lines(target),
            shlex.join(setup),
            shlex.join(edam_layer.make_command(rel)),
        ]
        return ["sh", "-c", " && ".join(parts)]

    def _cycle_baseline_selection(
        self, targets: list[str]
    ) -> tuple[str | None, list[str], str | None]:
        """Return the pinned ref and selected Targets needing relative evidence."""
        from booley.flows.recipe_evidence import BASELINE_REF_PARAM

        refs: set[str] = set()
        selected: list[str] = []
        handles = {target: self._target_handle(target) for target in targets}
        for key, entry in self.state.criteria.items():
            params = entry.params or {}
            target = next(
                (
                    selector
                    for selector, handle in handles.items()
                    if criterion_matches_target(
                        params,
                        identity=handle.identity,
                        selector=handle.selector,
                    )
                ),
                None,
            )
            if not key.startswith("cycle_count_") or target is None:
                continue
            ref = params.get(BASELINE_REF_PARAM)
            if not has_relative_threshold(params) or not isinstance(ref, str) or not ref:
                continue
            refs.add(ref)
            if target not in selected:
                selected.append(target)
        if not refs:
            return None, [], None
        if len(refs) != 1:
            return (
                None,
                selected,
                "sim: selected Cycle Count criteria carry conflicting baseline refs",
            )
        ref = next(iter(refs))
        resolved = git_full_sha(ref, Path(self.args.work_dir))
        if resolved is None:
            return (
                ref,
                selected,
                "sim: Cycle Count ticket baseline ref cannot be resolved to a commit",
            )
        return resolved, selected, None

    def _run_cycle_count_baselines(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> dict[str, TargetResult] | McpToolResult:
        """Run each relative Cycle Count Target once in a throwaway baseline tree."""
        baseline_ref, baseline_targets, error = self._cycle_baseline_selection(targets)
        if error is not None:
            return McpToolResult(exit_code=EXIT_ERROR, report_text=error)
        if baseline_ref is None:
            return {}
        project_root = Path(self.args.work_dir)
        expected_identities = {
            target: self._target_handle(target).identity for target in baseline_targets
        }
        results: dict[str, TargetResult] = {}
        try:
            with baseline_worktree(project_root, baseline_ref) as worktree:
                self.args.work_dir = worktree
                try:
                    for target in baseline_targets:
                        baseline_handle = self._target_handle(target)
                        expected_identity = expected_identities[target]
                        if baseline_handle.identity != expected_identity:
                            raise BaselineWorktreeError(
                                f"Cycle Count baseline selector {target!r} resolves to "
                                f"{baseline_handle.identity!r}, expected {expected_identity!r}"
                            )
                        result = self._run_target(
                            target,
                            self._tb_top_for_target(target),
                            test_names_map,
                            [],
                        )
                        result.target_identity = baseline_handle.identity
                        results[result.target_identity] = result
                        self._attach_workload_snapshots(result)
                finally:
                    self.args.work_dir = project_root
        except (
            MissingExecutableError,
            SimulationBuildInfrastructureError,
            BaselineWorktreeError,
            fusesoc_registry.FuseSocError,
        ) as exc:
            self.args.work_dir = project_root
            return self._cycle_baseline_failure(exc, baseline_targets)
        return results

    def _cycle_baseline_failure(
        self,
        exc: (
            MissingExecutableError
            | SimulationBuildInfrastructureError
            | BaselineWorktreeError
            | fusesoc_registry.FuseSocError
        ),
        targets: list[str],
    ) -> McpToolResult:
        """Translate baseline setup and execution failures into Flow results."""
        if isinstance(exc, MissingExecutableError):
            return self._missing_executable_result(exc, targets[0])
        if isinstance(exc, SimulationBuildInfrastructureError):
            return self._build_infrastructure_result(exc)
        if isinstance(exc, BaselineWorktreeError):
            return McpToolResult(exit_code=EXIT_ERROR, report_text=f"sim: {exc}")
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=f"sim: Cycle Count baseline Target selection failed: {exc}",
        )

    def _missing_executable_result(
        self,
        exc: MissingExecutableError,
        target: str,
    ) -> McpToolResult:
        """Grade an absent EDA binary as a Flow error (exit 2), not a failure.

        The verdict channel must not say "test FAIL" when no test ever ran
        (F-32): with ``fusesoc`` hidden from PATH the old path printed
        ``run_test_001 FAIL 0.0s``, and with ``verilator`` hidden it printed
        ``sim_basic FAIL (0/1 tests)`` under an "elaboration failed" banner
        that named the wrong stage entirely. Both read, to a triage agent, as a
        broken design. The criterion is deliberately left unset — a run that
        observed nothing may not grade anything.
        """
        message = (
            f"sim: required executable {exc.binary!r} was not found in the "
            f"Session Runtime while running Target {target!r}. No "
            "simulation ran, so there is no pass/fail verdict about the design. "
            f"Install {exc.binary!r} (or put it on PATH) in the Session Runtime "
            "and re-run; `booley doctor` checks the toolchain."
        )
        tail = "\n".join(exc.context.strip().splitlines()[-15:])
        report_text = f"{message}\n\n--- output tail ---\n{tail}" if tail else message
        print(report_text)
        return McpToolResult(
            exit_code=EXIT_ERROR,
            display_lines=[f"Flow error: {exc.binary} not found"],
            detail={
                "eda_tool_error": "missing_executable",
                "missing_executable": exc.binary,
                "target": target,
            },
            report_text=report_text,
        )

    @staticmethod
    def _build_infrastructure_result(
        exc: SimulationBuildInfrastructureError,
    ) -> McpToolResult:
        """Report a no-verdict build outcome without changing Criteria."""
        outcome = exc.outcome
        message = (
            f"sim: build infrastructure failed for Target {exc.target!r}: "
            f"{outcome.reason}. No simulation ran, so there is no pass/fail "
            "verdict about the design. Inspect the build log and re-run."
        )
        tail = "\n".join(outcome.output.strip().splitlines()[-15:])
        report_text = f"{message}\n\n--- output tail ---\n{tail}" if tail else message
        print(report_text)
        return McpToolResult(
            exit_code=EXIT_ERROR,
            display_lines=[f"Flow error: {exc.target} build infrastructure failed"],
            detail={
                "eda_tool_error": "build_infrastructure",
                "target": exc.target,
                "build_stage": _build_outcome_entry(outcome),
            },
            report_text=report_text,
        )

    def _resolve_requested_targets(self) -> list[str] | McpToolResult:
        handles = select_targets(self.args.work_dir, self.args.target, for_flow="sim")
        self._target_handles = {handle.selector: handle for handle in handles}
        targets = [handle.selector for handle in handles]
        if targets:
            return targets
        return McpToolResult(
            exit_code=EXIT_ERROR,
            report_text=(
                "sim: --target is required. Pass --target <name> (or a "
                "comma-separated list); list available Targets with `booley targets`."
            ),
        )

    def _target_handle(self, target: str) -> TargetHandle:
        """Return the selected handle for the active checkout.

        Cycle-count baselines intentionally materialize another checkout, so
        they reselect and verify the planned selector in that checkout.
        """
        root = Path(self.args.work_dir).resolve()
        selected = getattr(self, "_target_handles", {}).get(target)
        if selected is not None and selected.project_root == root:
            return selected
        return select_target(root, target, for_flow="sim")

    def _tb_top_for_target(self, target: str, resolved: Any = None) -> str:
        if resolved is None:
            return inspect_target(
                self.args.work_dir,
                self._target_handle(target),
            ).toplevel
        return tb_top_for_target(
            target,
            self.args.work_dir,
            resolved=resolved,
        )

    def _record_sim_criterion(self, target_result: TargetResult) -> None:
        """Set per-config sim_pass criterion (skip when inconclusive).

        No-ops outside a ticket run (Interactive / standalone mode, where
        ``state_file`` is unset): there is no criteria registry to satisfy, so
        the write would only auto-create an unknown optional ``sim_pass_<target>``
        criterion — the benign-but-noisy DEBUG line the sandbox agent sees on
        every bare ``simulate`` run.
        """
        if self.args.state_file is None:
            return
        if target_result.inconclusive:
            return
        crit_key = f"sim_pass_{target_result.target}"
        selected = [test.name for test in target_result.tests]
        declared = list(
            lookup_target_section(
                getattr(self, "_test_names_map", None) or _get_test_names(self.args.work_dir),
                target_result.target,
            )
            or []
        )
        complete_suite = not self.args.test and (not declared or set(selected) == set(declared))
        passed_tests = [test.name for test in target_result.tests if test.passed]
        failed_tests = [
            test.name for test in target_result.tests if not test.passed and not test.inconclusive
        ]
        skipped_tests = self._skipped_tests(
            target_result.target,
            getattr(self, "_test_names_map", None) or _get_test_names(self.args.work_dir),
        )
        self.set_criterion(
            crit_key,
            target_result.passed,
            source_target=target_result.target,
            detail={
                "tests_passed": sum(1 for t in target_result.tests if t.passed),
                "tests_total": len(target_result.tests),
                "test_selector": self.args.test or ("all" if complete_suite else "partial"),
                "registry_tests": declared,
                "selected_tests": selected,
                "passed_tests": passed_tests,
                "failed_tests": failed_tests,
                "skipped_tests": skipped_tests,
            },
        )

    def _record_cycle_count_criteria(self, target_result: TargetResult) -> None:
        """Grade every declared Criterion bound to this Target and named test."""
        if self.args.state_file is None:
            return
        tests = {test.name: test for test in target_result.tests}
        baseline_tests = self._baseline_cycle_tests(target_result)
        for key, entry in self.state.criteria.items():
            params = entry.params or {}
            if not key.startswith("cycle_count_") or not criterion_matches_target(
                params,
                identity=target_result.target_identity,
                selector=target_result.target,
            ):
                continue
            test_name = params.get("test")
            current = tests.get(test_name) if isinstance(test_name, str) else None
            relative = has_relative_threshold(params)
            baseline = (
                baseline_tests.get(test_name) if relative and isinstance(test_name, str) else None
            )
            met, reason = _admissible_cycle_evidence(current, "current")
            if met and relative:
                met, reason = _admissible_cycle_evidence(baseline, "baseline")
            detail = {
                "target": target_result.target,
                "target_identity": target_result.target_identity,
                "test": test_name,
                "cycles": current.cycles if current is not None else None,
                "baseline_cycles": baseline.cycles if baseline is not None else None,
                "cycle_observation": current.cycle_status if current is not None else "missing",
                "baseline_observation": (
                    baseline.cycle_status
                    if baseline is not None
                    else ("not_required" if not relative else "missing")
                ),
                "workload_snapshot": (current.workload_snapshot if current is not None else None),
                "baseline_workload_snapshot": (
                    baseline.workload_snapshot if baseline is not None else None
                ),
            }
            if reason:
                detail["reason"] = reason
            self.set_criterion(
                key,
                met,
                source_target=target_result.target,
                detail=detail,
            )

    def _baseline_cycle_tests(self, result: TargetResult) -> dict[str, TestResult]:
        """Index baseline cycle evidence for the result's durable Target."""
        key = result.target_identity or result.target
        baseline = getattr(self, "_baseline_results", {}).get(key)
        if not isinstance(baseline, TargetResult):
            return {}
        return {test.name: test for test in baseline.tests}

    def _remember_resolved_target(self, target: str, resolved: Any) -> None:
        """Keep the resolved EDAM projection long enough to snapshot its inputs."""
        if not hasattr(self, "_resolved_targets"):
            self._resolved_targets: dict[str, Any] = {}
        self._resolved_targets[target] = resolved

    def _attach_workload_snapshots(self, result: TargetResult) -> None:
        """Attach a stable declared-input snapshot to each named test result."""
        from booley.flows.sim.workload import build_workload_snapshot

        resolved = getattr(self, "_resolved_targets", {}).get(result.target)
        if resolved is None:
            return
        work_dir = Path(self.args.work_dir).resolve()
        run_cwd = resolve_run_cwd(work_dir)
        resolved_run_cwd = (work_dir / run_cwd).resolve()
        try:
            normalized_run_cwd = resolved_run_cwd.relative_to(work_dir).as_posix()
        except ValueError:
            normalized_run_cwd = "<outside-worktree>"
        controls = {
            "cycle_sentinels": _resolve_cycle_sentinels(self.args.work_dir),
            "pre_run_commands": _resolve_pre_run_commands(self.args.work_dir),
            "run_cwd": normalized_run_cwd,
            "environment": self._target_sim_env(result.target),
            "select": lookup_target_section(_get_test_selects(self.args.work_dir), result.target),
            "skip": list(
                lookup_target_section(_get_test_skips(self.args.work_dir), result.target) or []
            ),
        }
        for test in result.tests:
            test.workload_snapshot = build_workload_snapshot(
                work_dir,
                result.target,
                test.name,
                resolved,
                controls=controls,
            )

    def _record_run_log_dir(self, target: str, build_root: Path | str) -> None:
        """Take ownership of *target*'s ``run.log`` for this invocation.

        The run-half and Flow-side result paths both write it into the resolved
        Edalize build directory next to result.json. Called at prepare time —
        the only moment the resolved build dir is known — where it both
        remembers the dir (read back by
        :meth:`_headline_lines`) and opens the log FRESH: until
        :func:`write_run_log` lands at the end of the run, the file otherwise
        still holds the previous run's bytes, and anyone tailing it during
        the wait reads that old verdict as live progress (F-26).

        The open itself is :meth:`BooleyFlow._open_run_log`'s job.
        """
        if not hasattr(self, "_run_log_dirs"):
            self._run_log_dirs: dict[str, Path] = {}
        log_dir = Path(build_root)
        self._run_log_dirs[target] = log_dir
        self._open_run_log(target, log_dir)

    def _run_log_is_fresh(self, target: str) -> bool:
        """True when *target*'s run.log holds THIS invocation's output.

        Guards the ``log:`` pointer and the "see run.log" excerpt marker: a
        build that broke before the run-half started leaves the log at the
        header :meth:`_record_run_log_dir` opened it with, and pointing at it
        sends the reader nowhere useful — while before F-26 it sent them to a
        stale "TEST PASSED" (benchmark finding, ≥6/57 failure cases). The
        verdict comes from the log's own run header, so it holds for every
        writer: Booley-side on the boundary path, the run-halves' own child
        process in the sandbox.
        """
        log_dir = getattr(self, "_run_log_dirs", {}).get(target)
        return log_dir is not None and run_log_is_current(log_dir)

    def _record_eda_tool(self, target: str, eda_tool: str | None) -> None:
        """Remember *target*'s raw resolved EDA tool (e.g. ``"verilator"``).

        Raw — not the ``normalize_eda_tool`` run-half family — because this
        feeds report observability (TargetResult.eda_tool → per-target report
        + the run-level ``eda_tool`` key), not parser dispatch. Recorded at
        prepare time, like :meth:`_record_run_log_dir`.
        """
        if not hasattr(self, "_target_eda_tools"):
            self._target_eda_tools: dict[str, str] = {}
        self._target_eda_tools[target] = eda_tool or ""

    def _run_log_pointer(self, target: str) -> str | None:
        """Project-relative ``run.log`` path for *target*, or None if unknown.

        Unknown for targets whose Edalize resolution never ran (setup
        failure before the prepare half recorded the build dir) — and None
        when the log on disk is NOT from this invocation
        (:meth:`_run_log_is_fresh`): a stale pointer is worse than none.
        """
        log_dir = getattr(self, "_run_log_dirs", {}).get(target)
        if log_dir is None or not self._run_log_is_fresh(target):
            return None
        return posix_relpath(log_dir / RUN_LOG_NAME, self.args.work_dir)

    def _artifacts_for(self, target: str, result: TargetResult | None = None) -> dict[str, object]:
        """Durable files this run left for *target*, as project-relative paths.

        Everything the run-half writes lands in the resolved build root: it is
        both ``--bin-dir`` and (by default) the run-half's ``--work-dir``, so
        run.log, result.json, cocotb's results.xml and the whole trace family
        are siblings there. Absent files are dropped by
        :func:`artifacts.artifacts_block`, so a non-traced run simply carries
        no trace keys rather than three dead pointers.

        The WHOLE block is gated on :meth:`_run_log_is_fresh`, not just the
        ``log`` key. ``begin_run_log`` truncates run.log at the start of a run,
        but nothing clears its siblings — result.json, results.xml, trace.fst,
        trace_incident.txt all survive from run to run in a reused build root.
        So a build that dies before the run-half starts would otherwise report
        the PREVIOUS run's ``passed: true`` result.json under this run's
        verdict, which is F-26 verbatim, one file over. They all come from the
        same run-half: if it never ran, none of them are ours to cite.

        A trace store that landed under a non-default name (a project's own
        ``trace_files`` dump path, F-22) is taken from *result*'s parsed
        ``TRACE_OK`` path when one is available, since ``trace.fst`` is only
        the default.
        """
        log_dir = getattr(self, "_run_log_dirs", {}).get(target)
        if log_dir is None or not self._run_log_is_fresh(target):
            return {}
        traces = [t.trace_path for t in (result.tests if result else []) if t.trace_path]
        return artifacts.artifacts_block(
            self.args.work_dir,
            log=log_dir / RUN_LOG_NAME,
            result=log_dir / "result.json",
            results_xml=log_dir / "results.xml",
            cocotb_results_json=log_dir / "cocotb_results.json",
            # The run-half reports the store it actually produced; fall back to
            # the conventional name when this run parsed no TRACE_OK marker.
            trace=traces[0] if traces else log_dir / "trace.fst",
            trace_status=log_dir / "trace_status.json",
            trace_incident=log_dir / "trace_incident.txt",
        )

    def _headline_lines(self, all_results: list[TargetResult]) -> list[str]:
        """Compact per-target verdict block, emitted at the END of report_text.

        The MCP server tail-truncates the EDA tool's stdout (keeping the end), so
        everything that must always survive — each target's verdict, per-test
        cycle counts, SVA error count, timeout flag, and (on failure) the
        run.log pointer plus the build-context lines — is repeated here in
        compact form after all verbose detail. A passing target stays clean:
        the pointer/build lines exist to be acted on, and a benchmark sweep
        showed agents shelling out for exactly this context only on failures.
        """
        lines = ["", "--- summary ---"]
        for r in all_results:
            if r.inconclusive:
                verdict = "INCONCLUSIVE"
            else:
                verdict = "PASS" if r.passed else "FAIL"
            tests_passed = sum(1 for t in r.tests if t.passed)
            sva_errors = sum(t.sva_errors for t in r.tests)
            flags = f", sva_errors={sva_errors}"
            if any(t.timed_out for t in r.tests):
                flags += ", TIMED OUT"
            lines.append(
                f"[sim] {r.target} (session-runtime): {verdict} "
                f"({tests_passed}/{len(r.tests)} tests{flags}, "
                f"{_format_duration(r.elapsed_s)})"
            )
            lines.extend(_test_status_line(t) for t in r.tests)
            # F-35: the waveform's path+size must survive tail-truncation too —
            # it is the whole point of having asked for --trace.
            lines.extend(ln for ln in (_trace_line(t) for t in r.tests) if ln)
            failing = not r.passed or any(not t.passed for t in r.tests)
            if not failing:
                continue
            pointer = self._run_log_pointer(r.target)  # fresh-log-guarded
            if pointer:
                lines.append(f"  log: {pointer}")
            lines.extend(f"  {ln}" for ln in self._build_context_lines(r.target))
        return lines

    def _build_context_lines(self, target: str) -> list[str]:
        """≤2 compact failure-card lines naming the build config (best-effort).

        The invisible half of most sim failures (benchmark finding, 47/57
        cases): the composed compile command (the edalize make line — e.g. a
        missing ``-g2012``) and the fileset size (a ``.scr`` missing the
        testbench). One line each; the full file list lives in the per-target
        report JSON. Empty on any composition failure — context must never
        fail the EDA tool.
        """
        lines: list[str] = []
        build = self._compile_command_str(target)
        if build:
            lines.append(f"build: {build}")
        fileset = self._fileset_for_report(target)
        if fileset is not None:
            total = len(fileset["rtl"]) + len(fileset["tb"])
            lines.append(f"fileset: {total} files ({len(fileset['tb'])} tb)")
        return lines

    def _compile_command_str(self, target: str) -> str | None:
        """The composed setup+build+run command line for *target*, or None.

        Uses the production execution preview so reports and ``--dry-run``
        cannot diverge. Optional report metadata remains best-effort.
        """
        cache: dict[str, str | None] = getattr(self, "_compile_command_cache", {})
        if not hasattr(self, "_compile_command_cache"):
            self._compile_command_cache = cache
        if target in cache:
            return cache[target]
        command: str | None = None
        try:
            test_names_map = getattr(self, "_test_names_map", None) or _get_test_names(
                self.args.work_dir
            )
            tests = self._resolve_tests_to_run(target, test_names_map)
            preview = self._simulation_execution().preview(
                self._target_handle(target), self._execution_selection(tests)
            )
            scripts = [item[2] for item in preview.commands if item[:2] == ("sh", "-c")]
            command = "\n".join(scripts) or None
        except Exception:  # noqa: BLE001 — optional report metadata is best-effort
            logger.debug("could not compose compile command for %s", target, exc_info=True)
        cache[target] = command
        return command

    def _fileset_for_report(self, target: str) -> dict[str, list[str]] | None:
        """*target*'s declared source fileset, split rtl/tb, or None.

        Uses the canonical pre-setup Target inspection, including FuseSoC's
        condition evaluation and dependency closure. Cached per Target and
        best-effort like :meth:`_compile_command_str`.
        """
        cache: dict[str, dict[str, list[str]] | None] = getattr(self, "_fileset_cache", {})
        if not hasattr(self, "_fileset_cache"):
            self._fileset_cache = cache
        if target in cache:
            return cache[target]
        fileset: dict[str, list[str]] | None = None
        try:
            inspection = inspect_target(self.args.work_dir, self._target_handle(target))
            fileset = {
                "rtl": list(inspection.rtl_files),
                "tb": list(inspection.tb_files),
            }
        except Exception:  # noqa: BLE001 — optional report metadata is best-effort
            logger.debug("could not read fileset for %s", target, exc_info=True)
        cache[target] = fileset
        return fileset

    def _format_summary(
        self,
        all_results: list[TargetResult],
        output_lines: list[str],
        overall_pass: bool,
    ) -> str:
        """Assemble report_text: verbose detail first, headline block LAST.

        Truncation-resilient ordering: the MCP server tail-truncates the whole
        EDA-tool stdout to ~12KB keeping the END, so the compact per-target
        headline block (and the RESULT verdict after it) is emitted at the
        very end — one chatty failing target can no longer push another
        target's verdict or cycle counts out of the surviving window.
        """
        lines = [*output_lines, *self._headline_lines(all_results)]
        any_inconclusive = any(r.inconclusive for r in all_results)
        targets_passed = sum(1 for r in all_results if r.passed)
        if any_inconclusive:
            # Report the reason the RUN recorded, never a fixed sentence: a
            # trace-verification failure and a missing sentinel are different
            # problems with different fixes (F-22).
            reasons = {
                t.inconclusive_reason
                for r in all_results
                for t in r.tests
                if t.inconclusive_reason
            }
            detail = "; ".join(sorted(reasons)) or _INCONCLUSIVE_NO_SENTINEL
            lines.append(f"\nRESULT: INCONCLUSIVE — {detail}")
        elif overall_pass:
            lines.append(f"\nRESULT: PASS ({targets_passed}/{len(all_results)} targets)")
        else:
            lines.append(f"\nRESULT: FAIL ({targets_passed}/{len(all_results)} targets)")
        report_text = "\n".join(lines)
        # Surface the summary (incl. the RESULT line) on stdout so a passing
        # run is not silent. base.py only echoes report_text on failure, so
        # without this the happy-path RESULT: PASS never reaches the user —
        # elaborate/lint already print their summary the same way. The MCP
        # renderer dedupes: it drops its own report_text section when this
        # print already carries it verbatim, so keep the print.
        print(report_text)
        return report_text


    def _run_resolved_target(
        self,
        target: str,
        test_names_map: dict[str, list[str]],
        output_lines: list[str],
    ) -> TargetResult:
        """Resolve target metadata, then include that work in setup timing."""
        resolution_started = time.monotonic()
        tb_top = self._tb_top_for_target(target)
        resolution_s = time.monotonic() - resolution_started
        result = self._run_target(target, tb_top, test_names_map, output_lines)
        result.elapsed_s = round(result.elapsed_s + resolution_s, 3)
        timings = result.phase_timings_s
        timings["setup"] = round(timings.get("setup", 0.0) + resolution_s, 3)
        timings["execution_total"] = result.elapsed_s
        return result

    def _run_target(
        self,
        target: str,
        tb_top: str,
        test_names_map: dict[str, list[str]],
        output_lines: list[str],
    ) -> TargetResult:
        """Execute one selected Target through the deep Simulation boundary."""
        del tb_top
        tests_to_run = self._resolve_tests_to_run(target, test_names_map)
        skipped = self._skipped_tests(target, test_names_map)
        if skipped:
            output_lines.append(f"  (skipped {len(skipped)}: {', '.join(skipped)})")
        selection = self._execution_selection(tests_to_run)
        execution = self._simulation_execution()
        outcome = execution.run(self._target_handle(target), selection)
        result = self._project_execution_outcome(outcome)
        output_lines.append(f"[sim] {target} (session-runtime)")
        output_lines.extend(result.diagnostics)
        output_lines.extend(self._pre_run_output_lines(outcome))
        for test in result.tests:
            _append_test_output_line(test, output_lines, self._run_log_is_fresh(target))
        if len(result.tests) > 1:
            passed = sum(1 for test in result.tests if test.passed)
            output_lines.append(f"  --- {passed}/{len(result.tests)} passed ---")
        return result

    def _simulation_execution(self) -> SimulationExecution:
        """Compose the execution boundary with the Flow's process transport."""
        override = getattr(self, "_simulation_execution_override", None)
        if override is not None:
            return cast(SimulationExecution, override)
        return SimulationExecution(
            invoke=self._execute_boundary,
            artifact_root=self.args.report_dir,
            options=SimulationOptions(
                trace=self.args.trace,
                timeout_ms=int(self.args.timeout) if self.args.timeout is not None else None,
                result_verbosity=self.args.result_verbosity,
            ),
        )

    @staticmethod
    def _execution_selection(tests: list[str | None]) -> NamedTests | DefaultSelection:
        """Translate the legacy suite representation before crossing the seam."""
        names = tuple(test for test in tests if test is not None)
        return NamedTests(names) if names else DefaultSelection()

    def _project_execution_outcome(self, outcome: SimulationTargetOutcome) -> TargetResult:
        """Project immutable execution evidence into the compatibility report model."""
        if outcome.infrastructure_failure is not None:
            detail = (
                outcome.infrastructure_failure.detail or outcome.infrastructure_failure.message
            )
            missing = (
                outcome.infrastructure_failure.missing_executable
                or find_missing_executable(detail)
            )
            if missing:
                raise MissingExecutableError(missing, detail)
            build = outcome.builds[-1] if outcome.builds else setup_failure_outcome(detail)
            if not build.reason:
                build = replace(build, reason=detail, output=detail)
            raise SimulationBuildInfrastructureError(outcome.target, build)
        tests = [self._project_execution_test(test) for test in outcome.tests]
        self._record_execution_artifacts(outcome)
        return TargetResult(
            target=outcome.target,
            tb_top=outcome.toplevel,
            eda_tool=outcome.eda_tool,
            passed=outcome.passed,
            elapsed_s=round(outcome.elapsed_s, 1),
            tests=tests,
            inconclusive=outcome.verdict == "inconclusive",
            elab_failed=any(test.elab_failed for test in tests),
            target_identity=outcome.target_identity,
            diagnostics=tuple(f"  (note: {note})" for note in outcome.diagnostics),
            phase_timings_s=dict(outcome.phase_timings_s),
        )

    def _project_execution_test(self, outcome: Any) -> TestResult:
        """Build one compatibility test row from immutable execution evidence."""
        trace = next((item for item in outcome.artifacts if item.kind == "trace"), None)
        return TestResult(
            name=outcome.name,
            passed=outcome.passed,
            elapsed_s=outcome.elapsed_s,
            build_s=outcome.build_s,
            cycles=outcome.cycles,
            cycle_status=outcome.cycle_status,
            sva_errors=outcome.sva_errors,
            error_tail=outcome.error_tail,
            timed_out=outcome.timed_out,
            inconclusive=outcome.inconclusive,
            inconclusive_reason=outcome.reason,
            elab_failed=outcome.elab_failed,
            test_validated=outcome.test_validated,
            trace_path=(
                artifacts.relative(Path(trace.path), self.args.work_dir)
                if trace is not None
                else ""
            ),
            trace_bytes=trace.size if trace is not None else 0,
            trace_top_scope=trace.top_scope if trace is not None else "",
            trace_signal_count=trace.signal_count if trace is not None else 0,
            trace_total_ticks=trace.total_ticks if trace is not None else 0,
            run_log_path=(
                artifacts.relative(Path(outcome.run_log_path), self.args.work_dir)
                if outcome.run_log_path
                else ""
            ),
            workload_snapshot=dict(outcome.workload_snapshot or {}),
            build_outcome=outcome.build,
            phase_timings_s=dict(outcome.phase_timings_s),
            resources=dict(outcome.resources),
        )

    def _record_execution_artifacts(self, outcome: SimulationTargetOutcome) -> None:
        """Expose validated execution artifacts to existing report projection."""
        run_log = next(
            (item for item in outcome.artifacts if item.kind == "live_run_log"),
            None,
        )
        if run_log is not None:
            self._run_log_dirs = getattr(self, "_run_log_dirs", {})
            self._run_log_dirs[outcome.target] = Path(run_log.path).parent
        if outcome.eda_tool:
            self._record_eda_tool(outcome.target, outcome.eda_tool)

    @staticmethod
    def _pre_run_output_lines(outcome: SimulationTargetOutcome) -> list[str]:
        """Render execution-owned Pre-Run evidence for compatibility output."""
        return [
            f"  pre_run_commands ({len(item.commands)} line(s)) for "
            f"{', '.join(item.test_names) or outcome.target}: {item.status} "
            f"in {item.elapsed_s:.1f}s"
            for item in outcome.pre_runs
        ]

    def _effective_skips(self, target: str) -> set[str]:
        """Test names to exclude for *target*: tests.toml ``skip`` union ``--skip``.

        The config list (``TEST_SKIP``) carries a project's durable known-hangs;
        the ``--skip`` arg adds ad-hoc ones for a single call. Both match test
        names exactly (unlike ``--test``'s substring include-filter).
        """
        skips = set(lookup_target_section(_get_test_skips(self.args.work_dir), target) or [])
        if self.args.skip:
            skips.update(s.strip() for s in self.args.skip.split(",") if s.strip())
        return skips

    def _validate_test_selector(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> McpToolResult | None:
        """Reject an unknown ``--test`` before any sim runs (built-in path).

        A target that declares a test list (tests.toml ``tests``) but whose list
        has no substring match for ``--test`` is a typo, not a selector: running
        it would emit no selection plusarg and silently execute the testbench's
        default test — reporting a false PASS.
        Mirrors the ``UnknownTargetError`` contract ``--target`` already gets.

        Skipped for a target with no declared test list: there the ``--test``
        value is a raw passthrough the TB owns (matching
        ``resolve_target_selection``'s transitional skip when the Target list is
        unknown). Returns an ``EXIT_ERROR`` McpToolResult on the first offending
        target, else ``None``.
        """
        selector = self.args.test
        if not selector:
            return None
        for target in targets:
            available = lookup_target_section(test_names_map, target) or []
            if available and not _filter_tests(available, selector):
                return McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"sim: --test {selector!r} matched no test for "
                        f"target {target!r}. Declared tests: "
                        f"{', '.join(available)}. (Running it would silently "
                        "execute the testbench's default test and report a "
                        "false PASS.)"
                    ),
                )
        return None

    def _validate_cocotb_targets(self, targets: list[str]) -> McpToolResult | None:
        """Reject a plusarg ``select`` template on a Cocotb Target (A2).

        On a Cocotb Target test selection is the ``COCOTB_TEST_FILTER`` env var
        Booley builds from the ``tests`` list — a plusarg ``select`` template is
        a config contradiction, rejected up front (a setup-time error, ADR 0034
        decision 5) rather than silently ignored. ``skip`` works unchanged.
        Returns an ``EXIT_ERROR`` McpToolResult on the first offending target,
        else ``None``.
        """
        selects = _get_test_selects(self.args.work_dir)
        for target in targets:
            if lookup_target_section(selects, target) is not None and self.is_cocotb_target(
                target
            ):
                return McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"sim: tests.toml [{target}] declares a `select` "
                        f"plusarg template, but {target!r} is a Cocotb Target "
                        "(its .core flow options declare a cocotb_module). "
                        "Cocotb test selection is driven by the "
                        "COCOTB_TEST_FILTER environment variable built from "
                        "the `tests` list — remove the `select` key."
                    ),
                )
        return None

    def _validate_runnable_tests(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> McpToolResult | None:
        """Reject Targets whose skip policy excludes every declared test."""
        if self.args.test:
            return None  # an explicit selector deliberately overrides skips
        for target in targets:
            available = list(lookup_target_section(test_names_map, target) or [])
            try:
                require_runnable_target_test_suite(
                    target,
                    test_names={target: available},
                    test_skips={target: list(self._effective_skips(target))},
                )
            except NoRunnableTestsError as exc:
                return McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"sim: {exc}. Tests are excluded by tests.toml `skip` or "
                        "--skip. Remove a skip "
                        "or explicitly select one test with --test."
                    ),
                )
        return None

    def _resolve_tests_to_run(
        self,
        target: str,
        test_names_map: dict[str, list[str]],
    ) -> list[str | None]:
        """Resolve the list of tests to run for a target, minus skip list.

        Skips (tests.toml ``skip`` + ``--skip``) prune known-hanging tests so
        they don't each burn the full per-test wall-clock budget. An explicit
        ``--test`` selector that matches *only* skipped tests still runs them —
        naming a test by hand is a clear override of the skip list. When every
        known test is skipped, pre-run validation rejects the Target rather
        than passing vacuously or executing known-hanging tests.
        """
        if self.args.test:
            available_tests = lookup_target_section(test_names_map, target) or []
            skips = self._effective_skips(target)
            matched = _filter_tests(available_tests, self.args.test)
            # No match: a declared-but-unmatched name is rejected up front by
            # _validate_test_selector, so this branch is only reached when the
            # target declares no test list — a raw passthrough the TB owns.
            if not matched:
                return [self.args.test]
            kept: list[str | None] = [t for t in matched if t not in skips]
            # Explicit --test naming only skipped tests overrides the skip list.
            return kept or cast(list[str | None], matched)
        if self.args.skip:
            available_tests = lookup_target_section(test_names_map, target) or []
            if not available_tests:
                return [None]
            kept: list[str | None] = [
                test for test in available_tests if test not in self._effective_skips(target)
            ]
            return kept
        suite = resolve_target_test_suite(
            target,
            test_names=test_names_map,
            test_skips=_get_test_skips(self.args.work_dir),
        )
        return list(suite.tests)

    def _skipped_tests(
        self,
        target: str,
        test_names_map: dict[str, list[str]],
    ) -> list[str]:
        """Known tests actually excluded from this run (for the run note).

        Mirrors :meth:`_resolve_tests_to_run`'s decision so the display can
        report what was skipped — silent truncation reads as "ran everything".
        Empty when nothing was skipped or when ``--test`` overrode the skip list.
        """
        if self.args.test or self.args.skip:
            run = set(self._resolve_tests_to_run(target, test_names_map))
            skips = self._effective_skips(target)
            available = lookup_target_section(test_names_map, target) or []
            return [t for t in available if t in skips and t not in run]
        return list(
            resolve_target_test_suite(
                target,
                test_names=test_names_map,
                test_skips=_get_test_skips(self.args.work_dir),
            ).skipped
        )

    def _handle_dry_run(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> McpToolResult:
        """Render side-effect-free previews through the execution boundary."""
        commands: list[list[str]] = []
        for target in targets:
            tests = self._resolve_tests_to_run(target, test_names_map)
            preview = self._simulation_execution().preview(
                self._target_handle(target),
                self._execution_selection(tests),
            )
            commands.extend([list(command) for command in preview.commands])

        print(json.dumps(commands, indent=2))
        return McpToolResult(
            exit_code=EXIT_SUCCESS,
            report_text=f"Dry run: {len(commands)} command(s)",
            detail={"commands": commands},
        )

    def _write_target_report(self, result: TargetResult, *, complete: bool = True) -> None:
        """Write one Target's verdict, build context, and artifact pointers."""
        report_dir = self.args.report_dir
        if report_dir is None:
            return
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"sim_{result.target}.json"
        report = self._target_report_payload(result, report_path, complete=complete)
        invocation_dir = self.reserve_invocation_dir()
        if invocation_dir is not None:
            _atomic_write_json(invocation_dir / "targets" / report_path.name, report)
        _atomic_write_json(report_path, report)

    def _target_report_payload(
        self,
        result: TargetResult,
        report_path: Path,
        *,
        complete: bool,
    ) -> dict[str, Any]:
        """Compose best-effort build context around one Target verdict."""
        report: dict[str, Any] = {
            "flow": self.name,
            "target": result.target,
            "target_identity": result.target_identity,
            "tb_top": result.tb_top,
            "eda_tool": result.eda_tool,
            "timestamp": utc_now_rfc3339(),
            "elapsed_s": result.elapsed_s,
            "phase_timings_s": dict(result.phase_timings_s),
            "passed": result.passed,
            "complete": complete,
            "tests": [_test_report_entry(t) for t in result.tests],
        }
        build_stage = [_build_outcome_entry(test.build_outcome) for test in result.tests]
        if any(entry is not None for entry in build_stage):
            report["build_stage"] = [entry for entry in build_stage if entry is not None]
        run_id = os.environ.get("BOOLEY_RUN_ID", "")
        if run_id:
            report["run_id"] = run_id
        compile_command = self._compile_command_str(result.target)
        if compile_command is not None:
            report["compile_command"] = compile_command
        fileset = self._fileset_for_report(result.target)
        if fileset is not None:
            report["fileset"] = fileset
        # ``report`` names the file being written right now: it does not exist
        # yet, so it is added directly rather than through artifacts_block's
        # exists() filter. By the time anyone reads this JSON, it does.
        artifacts = self._artifacts_for(result.target, result)
        artifacts["report"] = posix_relpath(report_path, self.args.work_dir)
        report["artifacts"] = artifacts
        return report

    def _write_progress_report(
        self,
        targets: list[str],
        results: list[TargetResult],
        *,
        phase: str,
        complete: bool = False,
    ) -> None:
        """Checkpoint a long campaign after every completed Target."""
        invocation_dir = self.reserve_invocation_dir()
        if invocation_dir is None:
            return
        completed = [result.target for result in results]
        payload: dict[str, Any] = {
            "flow": self.name,
            "run_id": os.environ.get("BOOLEY_RUN_ID", ""),
            "timestamp": utc_now_rfc3339(),
            "phase": phase,
            "complete": complete,
            "targets": list(targets),
            "completed_targets": completed,
            "pending_targets": [target for target in targets if target not in completed],
            "detail": {result.target: _target_progress_detail(result) for result in results},
        }
        _atomic_write_json(invocation_dir / "progress.json", payload)

    def _persist_target_outcome(self, result: TargetResult) -> None:
        """Durably record one terminal Target before starting the next."""
        started = time.monotonic()
        # Publish an explicit recovery checkpoint before Criteria mutation.
        # A retry can safely replace it; readers never mistake it for a fully
        # committed target when interruption prevents the final publication.
        self._write_target_report(result, complete=False)
        self._record_elab_criterion(result)
        self._record_sim_criterion(result)
        self._record_cycle_count_criteria(result)
        if self.state._file_path is not None:
            self.state.save()
        publication_s = round(time.monotonic() - started, 3)
        result.phase_timings_s["publication"] = publication_s
        execution_s = result.phase_timings_s.get("execution_total", result.elapsed_s)
        result.phase_timings_s["total"] = round(execution_s + publication_s, 3)
        self._write_target_report(result, complete=True)

    def _record_elab_criterion(self, result: TargetResult) -> None:
        """Record authenticated full-Simulation build-stage evidence."""
        if self.args.state_file is None:
            return
        outcomes = [test.build_outcome for test in result.tests if test.build_outcome is not None]
        if not outcomes:
            return
        if any(outcome.design_failed for outcome in outcomes):
            met = False
        elif any(outcome.verdict is None for outcome in outcomes):
            return
        else:
            met = all(outcome.passed for outcome in outcomes)
        attempts = [
            entry for outcome in outcomes if (entry := _build_outcome_entry(outcome)) is not None
        ]
        self.set_criterion(
            f"elab_pass_{result.target}",
            met,
            source_target=result.target,
            detail={"mode": "simulation", "target": result.target, "attempts": attempts},
        )


if __name__ == "__main__":
    SimulateFlow().cli()
