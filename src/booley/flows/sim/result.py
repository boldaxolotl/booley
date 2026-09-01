"""Shared simulation result parsing and structured summary.

Single source of truth for determining pass/fail from simulator output.
The sim run-halves (run_iverilog_sim.py, verilator_run.py) and the batch
developer (run_sim_batch.py) import from here instead of maintaining
duplicate parsing logic.

The run-half emits a [SIM_SUMMARY] JSON line; the batch developer
parses it.  If the line is missing (crash/timeout), the batch falls back
to raw-output scanning via the same functions.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import NotRequired, TypedDict

from booley.core.boundary import (
    BoundaryError,
    as_str_list,
    require_bool,
    require_dict,
    require_int,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentinels
# ---------------------------------------------------------------------------

SIM_RESULT_PASSED = "[SIM_RESULT] PASSED"
SIM_RESULT_FAILED = "[SIM_RESULT] FAILED"
SIM_SUMMARY_PREFIX = "[SIM_SUMMARY] "


class SimSummary(TypedDict):
    """Validated payload transported by the ``[SIM_SUMMARY]`` sentinel."""

    passed: bool
    sva_errors: NotRequired[int]
    vrfc_warnings: NotRequired[list[str]]
    inconclusive: NotRequired[bool]


#: Marker a run-half prints when the *harness* failed rather than the design:
#: no built binary in the build dir, a missing simulator, a broken cocotb
#: install. Such a run produced **no observation at all**, so a caller that
#: grades sim outcomes (mutation_tester's kill/survive decision) must treat it
#: as "unknown", never as a verdict — a nonzero exit from a missing executable
#: is not a detected mutation (SETUP-F-41b). Distinct from ``[SIM_RESULT]``,
#: which always reports something the simulator actually observed.
SIM_INFRA_ERROR_PREFIX = "[SIM_INFRA_ERROR]"


def format_infra_error(reason: str) -> str:
    """Render *reason* as the one-line infra-failure marker (see above)."""
    return f"{SIM_INFRA_ERROR_PREFIX} {reason}"


def has_infra_error(output: str) -> bool:
    """True when *output* carries a run-half infra-failure marker."""
    return SIM_INFRA_ERROR_PREFIX in output


# ---------------------------------------------------------------------------
# Output scanners
# ---------------------------------------------------------------------------


def parse_sim_verdict(
    output: str,
    tail_lines: int | None = None,
    *,
    pass_sentinels: list[str] | None = None,
    fail_sentinels: list[str] | None = None,
) -> bool | None:
    """Determine pass/fail from simulation output via sentinel substrings.

    Returns True (passed), False (failed), or None (no sentinel found).

    Scans the WHOLE output by default (``tail_lines=None``). Capping the scan
    to a fixed tail silently loses the verdict when the sim prints more than
    that many trailing lines — a bug this parser shipped twice (22b4dcc,
    2fe716c). Pass an explicit ``tail_lines`` only when a caller genuinely
    wants to ignore all but the last N lines; leave it None for correctness.

    By default the built-in ``[SIM_RESULT] PASSED`` / ``[SIM_RESULT] FAILED``
    markers are used. Callers may override with project-configured sentinels
    (``booley.toml [flows.sim] pass_sentinels / fail_sentinels``) so users
    keep their own testbench wording instead of editing it. A match on any FAIL
    sentinel wins over any PASS sentinel (fail-safe, matches scan_sim_sentinel).
    """
    pass_s = pass_sentinels if pass_sentinels else [SIM_RESULT_PASSED]
    fail_s = fail_sentinels if fail_sentinels else [SIM_RESULT_FAILED]
    lines = output.splitlines()
    scan = lines if tail_lines is None else lines[-tail_lines:]

    if any(any(s in line for s in fail_s) for line in scan):
        return False
    if any(any(s in line for s in pass_s) for line in scan):
        return True

    return None


# The EDA tool names Booley's own halves put in front of the markers below —
# ``sim_edam._ELAB_FAIL_MARKERS`` ("Verilator elaboration failed", "iverilog
# compilation failed", "xcelium/vcs elaboration failed") plus the run-halves'
# own "<EDA-tool> simulation timed out" / "Verilator executable V<top> not found".
# A closed list, deliberately: spelling these as ``\w+`` swallows testbench
# prose. "ERROR: DUT compilation failed at time 100" is a TB talking about the
# *design*; treating it as infra drops it from the SVA count and turns a FAIL
# with rc=0 into an INCONCLUSIVE — a false-PASS-adjacent lie.
_HARNESS_EDA_TOOL = r"(?i:verilator|iverilog|icarus|xcelium|xrun|vcs|cocotb)"

# Harness-emitted *infrastructure* markers — every ``ERROR:`` line Booley's own
# build/run/trace/guard machinery prints. They all begin "ERROR: ...", which the
# Icarus-style "^ERROR:" SVA rule below would miscount as a DUT assertion
# failure: the fabricated sva_errors=1 that laundered a trace-infra failure into
# a design FAIL (QA_REPORT B5.1), and the same lie told by a build that never
# ran ("ERROR: Verilator elaboration failed (rc=2)" reported as one SVA error on
# a machine with no verilator installed — fpu F-30). An SVA count is a statement
# about the *design*; a run that never reached the design has zero of them.
_HARNESS_INFRA_MARKER_RE = re.compile(
    r"^(?:"
    r"TRACE_OK:|TRACE_INCIDENT:|\[SIM_INFRA_ERROR\]"
    # trace pipeline (B5.1)
    r"|ERROR: (?:--)?trace requested but "
    # build half: "<EDA-tool> elaboration failed" / "iverilog compilation failed"
    rf"|ERROR: {_HARNESS_EDA_TOOL} (?:elaboration|compilation) (?:failed|timed out)"
    # missing binary: "Verilator executable Vtop not found in ..."
    rf"|ERROR: {_HARNESS_EDA_TOOL} executable \S+ not found"
    # run half's own wall-clock kill: "<EDA-tool> simulation timed out (900s)"
    rf"|ERROR: {_HARNESS_EDA_TOOL} simulation timed out"
    # run_guard watchdogs: disk budget, frozen simulator clock
    r"|(?:ERROR: )?simulation killed:"
    # run_guard's fatal missing-$readmemh abort
    r"|ERROR: missing \$readmem"
    r")"
)


def is_harness_infra_line(line: str) -> bool:
    """True when *line* is a Booley harness/Flow-infrastructure marker, not DUT output.

    The single predicate the SVA counters consult before applying their generic
    "a leading ``ERROR:`` is an assertion failure" rule.
    """
    return bool(_HARNESS_INFRA_MARKER_RE.match(line))


def count_sva_errors(output: str) -> int:
    """Count SVA assertion error/fatal messages in simulator output."""
    count = (
        output.count("$error")
        + output.count("] Error:")
        + output.count("$fatal")
        + output.count("Fatal:")
    )
    for line in output.splitlines():
        if is_harness_infra_line(line):
            continue  # harness/Flow-infrastructure marker, not a DUT assertion (B5.1, F-30)
        # SVA assertion lines OR Icarus "ERROR: ..." at line start
        if ("Assertion" in line and ("FAILED" in line or "ERROR" in line)) or re.match(
            r"^ERROR:\s", line
        ):
            count += 1
    return count


# Xcelium (xrun) error lines. The front-end EDA tools prefix diagnostics as
# ``xmvlog: *E,CODE (...)`` / ``xmelab: *E,CODE: ...`` / ``xmsim: *E,CODE: ...``
# (``*F,`` for fatals); SVA assertion failures carry an ``*E,ASRT…`` code (e.g.
# ``ASRTST``). Patterns frozen against real xrun(64) 21.03-s001 logs in the
# Phase B calibration loop (ADR 0025, 2026-07). Observed verdict table:
#   compile error   xmvlog: *E,SVILTY (file,l|c): …  + xrun: *E,VLGERR   exit 1
#   elab error      xmelab: *E,NOUNIT/*E,CUVUNF: …   + xrun: *E,ELBERR   exit 1
#   SVA failure     xmsim: *E,ASRTST (file,l): (time T) Assertion … has failed
#                   — sim continues; xrun exits 1, TB sentinel may still PASS
#   $fatal          xmsim: *F,FATSEV (file,l): (time T).  — xrun exits 2
#   SIGTERM/kill    xmsim: *W,NCTERM: Simulation received SIGTERM … (warning,
#                   not counted; the non-zero exit code drives the verdict)
#   TB data FAIL    plain $finish — exit 0; only [SIM_RESULT] FAILED marks it
_XCELIUM_EDA_TOOL_ERROR_RE = re.compile(r"^\s*(?:xmvlog|xmelab|xmsim|xrun)\s*:\s*\*[EF],\w+")
_XCELIUM_ASSERT_RE = re.compile(r"\*[EF],ASRT\w*")


def count_sva_errors_xcelium(output: str) -> int:
    """Count error/assertion-failure lines in Xcelium (xrun) output.

    The Xcelium analogue of :func:`count_sva_errors`: xrun logs spell errors as
    ``<EDA-tool>: *E,CODE`` lines rather than Verilator/Icarus wording, so the
    generic scanner misses them. TB-emitted generic markers (a sentinel TB's
    own ``Assertion … FAILED`` / leading ``ERROR:`` lines) still count, so a
    simulator-agnostic testbench keeps its verdict under xrun.
    """
    count = 0
    for line in output.splitlines():
        if is_harness_infra_line(line):
            continue  # harness/Flow-infrastructure marker, not a DUT assertion (F-30)
        if _XCELIUM_EDA_TOOL_ERROR_RE.match(line) or _XCELIUM_ASSERT_RE.search(line):
            count += 1
            continue
        if ("Assertion" in line and ("FAILED" in line or "ERROR" in line)) or re.match(
            r"^ERROR:\s", line
        ):
            count += 1
    return count


# VCS (Synopsys) error lines. The front-end EDA tools (vlogan/vcs) tag diagnostics
# as ``Error-[TAG]`` / ``Fatal-[TAG]`` block headers; runtime severity tasks in
# the built simv (``$error``/``$fatal``/failed SVA default action) print
# ``Error: "file.sv", 42: ...`` / ``Fatal: ...`` lines. Build-half patterns
# frozen against real vcs X-2025.06-1 logs (Phase D, ADR 0025): Error-[TAG]
# headers confirmed (ICPD_INIT), Warning-[TAG]/Lint-[TAG] not counted, license
# failures emit no tagged lines (exit code carries the verdict). Runtime
# Error:/Fatal: patterns PROVISIONAL until a licensed simv run is captured.
_VCS_EDA_TOOL_ERROR_RE = re.compile(r"^\s*(?:Error|Fatal)-\[[^\]]+\]")
_VCS_RUNTIME_ERROR_RE = re.compile(r"^\s*(?:Error|Fatal):\s")


def count_sva_errors_vcs(output: str) -> int:
    """Count error/assertion-failure lines in VCS (simv/vlogan/vcs) output.

    The VCS analogue of :func:`count_sva_errors_xcelium`: VCS spells EDA-tool
    diagnostics as ``Error-[TAG]`` block headers and runtime severity-task
    output as ``Error:``/``Fatal:``-prefixed lines, so the generic scanner
    misses them. TB-emitted generic markers (``Assertion … FAILED`` / leading
    ``ERROR:`` lines) still count, so a simulator-agnostic testbench keeps its
    verdict under VCS.
    """
    count = 0
    for line in output.splitlines():
        if is_harness_infra_line(line):
            continue  # harness/Flow-infrastructure marker, not a DUT assertion (F-30)
        if _VCS_EDA_TOOL_ERROR_RE.match(line) or _VCS_RUNTIME_ERROR_RE.match(line):
            count += 1
            continue
        if ("Assertion" in line and ("FAILED" in line or "ERROR" in line)) or re.match(
            r"^ERROR:\s", line
        ):
            count += 1
    return count


def extract_vrfc_warnings(output: str) -> list[str]:
    """Extract VRFC 10-3380 forward-reference warning lines."""
    return [line for line in output.splitlines() if "VRFC 10-3380" in line]


# ---------------------------------------------------------------------------
# Structured summary (emitted by lower-level runner, parsed by batch)
# ---------------------------------------------------------------------------


def format_summary(
    passed: bool,
    sva_errors: int = 0,
    vrfc_warnings: list[str] | None = None,
    inconclusive: bool = False,
) -> str:
    """Build a [SIM_SUMMARY] JSON line for stdout emission."""
    payload: SimSummary = {
        "passed": passed,
        "sva_errors": sva_errors,
    }
    if vrfc_warnings:
        payload["vrfc_warnings"] = vrfc_warnings
    if inconclusive:
        payload["inconclusive"] = True
    return SIM_SUMMARY_PREFIX + json.dumps(payload, separators=(",", ":"))


def write_result_json(
    work_dir: str | Path,
    passed: bool,
    sva_errors: int = 0,
    first_error: str = "",
    returncode: int = 0,
    inconclusive: bool = False,
) -> None:
    """Write a persistent result.json to the sim work directory.

    Survives stdout truncation — agents can always read this file. Carries
    only the structured verdict (plus a 500-char first_error); the full raw
    output is persisted separately by :func:`booley.flows.run_log.write_run_log`
    as ``run.log``
    in the same directory.
    """
    from pathlib import Path

    payload: dict[str, object] = {
        "passed": passed,
        "sva_errors": sva_errors,
        "returncode": returncode,
    }
    if first_error:
        payload["first_error"] = first_error[:500]
    if inconclusive:
        payload["inconclusive"] = True
    Path(work_dir, "result.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def parse_summary_line(output: str) -> SimSummary | None:
    """Extract and parse the [SIM_SUMMARY] JSON line from captured output.

    Returns the parsed dict on success, None if the line is missing.
    Raises ValueError if the line is present but malformed — either invalid
    JSON, a non-object JSON value (list/scalar), or an object missing the
    mandatory ``passed`` key.  Callers index ``passed`` directly, so the
    shape is enforced here at the subprocess-output boundary.
    """
    for line in reversed(output.splitlines()):
        if line.startswith(SIM_SUMMARY_PREFIX):
            try:
                raw_payload: object = json.loads(line[len(SIM_SUMMARY_PREFIX) :])
            except (json.JSONDecodeError, ValueError) as e:
                raise ValueError(f"Malformed SIM_SUMMARY JSON: {line!r}") from e
            try:
                payload = require_dict(raw_payload, field="SIM_SUMMARY")
            except BoundaryError as e:
                raise ValueError(f"Malformed SIM_SUMMARY shape: {line!r}") from e
            if "passed" not in payload:
                raise ValueError(f"Malformed SIM_SUMMARY shape: {line!r}")
            try:
                summary: SimSummary = {"passed": require_bool(payload, "passed")}
                if "sva_errors" in payload:
                    summary["sva_errors"] = require_int(payload["sva_errors"], field="sva_errors")
                if "vrfc_warnings" in payload:
                    warnings = as_str_list(payload["vrfc_warnings"])
                    if warnings != payload["vrfc_warnings"]:
                        raise BoundaryError("vrfc_warnings must be a string list")
                    summary["vrfc_warnings"] = warnings
                if "inconclusive" in payload:
                    summary["inconclusive"] = require_bool(payload, "inconclusive")
            except BoundaryError as e:
                raise ValueError(f"Malformed SIM_SUMMARY shape: {line!r}") from e
            return summary
    return None
