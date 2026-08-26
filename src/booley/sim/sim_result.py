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
import os
import re
import time
from pathlib import Path
from typing import NotRequired, TypedDict

from booley.core.boundary import (
    BoundaryError,
    as_str_list,
    require_bool,
    require_dict,
    require_int,
)
from booley.runtime.timefmt import utc_now_rfc3339

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
    output is persisted separately by :func:`write_run_log` as ``run.log``
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


# The raw-output log's filename inside the sim work dir. CONTRACT: the
# simulate summary prints ``<work_dir>/run.log`` as the place to read the
# full output back — renaming this breaks that pointer.
RUN_LOG_NAME = "run.log"

#: First line of every run.log Booley opens, written at run START by
#: :func:`begin_run_log`. It names the run that OWNS the file, so anyone
#: tailing during a long run sees THIS run's identity instead of the previous
#: run's leftover bytes (a stale "TEST PASSED" tail read as live progress
#: burned a real debugging session — ravenoc F-26).
RUN_LOG_HEADER_PREFIX = "[BOOLEY RUN_LOG]"

#: Prefix of every "this run has not finished yet" body line — both the
#: freshly-opened :data:`RUN_LOG_PENDING` marker and the live-progress
#: heartbeat :func:`write_run_log_progress` replaces it with. The single
#: in-flight signal :func:`run_log_is_current` reads, so adding progress
#: reporting cannot accidentally make a mid-run log look finished.
RUN_LOG_IN_PROGRESS_PREFIX = "(run in progress"

#: Body a freshly opened run.log carries until the run produces output. Its
#: presence is the "no output yet" signal :func:`run_log_is_current` reads.
RUN_LOG_PENDING = f"{RUN_LOG_IN_PROGRESS_PREFIX} — the full output lands here when it finishes)"

#: Fallback run identity for Flow invocations outside the async-job dispatch
#: (no ``BOOLEY_RUN_ID``): PID plus process start second, so two sequential
#: runs from the same shell never collide even if the PID is recycled.
_PROCESS_RUN_TOKEN = f"pid{os.getpid()}-{int(time.time())}"


def current_run_token() -> str:
    """Identity of the Flow invocation that owns the run.logs it opens.

    The async-job dispatch layer exports ``BOOLEY_RUN_ID`` into every Flow
    child (ADR 0027), which is exactly the identity an agent polls with — use
    it when present so a header and a poll name the same run.
    """
    return os.environ.get("BOOLEY_RUN_ID", "") or _PROCESS_RUN_TOKEN


def begin_run_log(
    work_dir: str | Path,
    *,
    flow: str,
    target: str,
    run: str | None = None,
) -> Path:
    """Open a FRESH ``<work_dir>/run.log`` for a run that is about to start.

    Truncates whatever the previous run left there and writes a one-line
    header naming this run (id, Flow, target, start time) plus the
    :data:`RUN_LOG_PENDING` marker. Without this the file keeps the PREVIOUS
    run's bytes for the whole duration of a run — :func:`write_run_log` only
    lands at the end — so a tail shows an old verdict as if it were current.

    Truncation is in place (not the atomic tmp+replace :func:`write_run_log`
    uses): a reader racing it sees an empty file at worst, never stale
    content, and an already-open ``tail -f`` follows the same inode.
    """
    path = Path(work_dir, RUN_LOG_NAME)
    started = utc_now_rfc3339()
    header = (
        f"{RUN_LOG_HEADER_PREFIX} run={run or current_run_token()} "
        f"flow={flow} target={target} started={started}\n"
    )
    path.write_text(f"{header}{RUN_LOG_PENDING}\n", encoding="utf-8")
    return path


def _read_run_log_head(path: Path) -> tuple[bytes, bytes]:
    """First two lines of *path*, or ``(b"", b"")`` when unreadable.

    Bounded reads: a run.log's first line is a header by construction, but a
    log written by something else can be one enormous line.
    """
    try:
        with path.open("rb") as fh:
            return fh.readline(4096), fh.readline(4096)
    except OSError:
        return b"", b""


def read_run_log_header(work_dir: str | Path) -> dict[str, str] | None:
    """Parse ``<work_dir>/run.log``'s header into its ``key=value`` fields.

    Returns None when the log is missing, unreadable, or carries no header
    (written by a path that never called :func:`begin_run_log`).
    """
    first, _ = _read_run_log_head(Path(work_dir, RUN_LOG_NAME))
    line = first.decode("utf-8", errors="replace")
    if not line.startswith(RUN_LOG_HEADER_PREFIX) or not line.endswith("\n"):
        return None
    fields: dict[str, str] = {}
    for token in line[len(RUN_LOG_HEADER_PREFIX) :].split():
        key, sep, value = token.partition("=")
        if sep and value:
            fields[key] = value
    return fields


def run_log_is_current(work_dir: str | Path, run: str | None = None) -> bool:
    """True when ``<work_dir>/run.log`` holds THIS run's FINISHED output.

    The single freshness notion behind every "see run.log" pointer: the log
    must carry the header this invocation wrote (:func:`begin_run_log`) and
    must no longer be sitting on the :data:`RUN_LOG_PENDING` marker. A
    header-less log is somebody else's — never vouched for. This replaces the
    old mtime-versus-start-time heuristic, which could not tell a run that
    never wrote its log from one that did.
    """
    header = read_run_log_header(work_dir)
    if header is None or header.get("run") != (run or current_run_token()):
        return False
    _, second = _read_run_log_head(Path(work_dir, RUN_LOG_NAME))
    body = second.decode("utf-8", errors="replace").rstrip("\n")
    return not body.startswith(RUN_LOG_IN_PROGRESS_PREFIX)


def _cap_log_bytes(data: bytes, max_bytes: int) -> bytes:
    """Clamp *data* to *max_bytes*, keeping the TAIL behind a marker line.

    The tail is where the verdict sentinel, assertion failures and ``$fatal``
    wording live. Never starts mid-line unless the tail is one unbroken line.
    """
    if len(data) <= max_bytes:
        return data
    marker = (
        f"[RUN_LOG TRUNCATED] original size {len(data)} bytes > cap {max_bytes}; keeping tail\n"
    ).encode()
    keep = max(0, max_bytes - len(marker))
    tail = data[-keep:] if keep else b""
    cut = tail.find(b"\n")
    if 0 <= cut < len(tail) - 1:
        tail = tail[cut + 1 :]
    return marker + tail


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace *path*'s contents with *data* in one step (PID-suffixed tmp).

    The ``_job_records`` idiom: a concurrent reader (an agent tailing run.log
    mid-run) never sees a torn file, only the old bytes or the new ones.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # don't leave a stray tmp behind
        raise


#: How much of the live tail :func:`write_run_log_progress` keeps. Small on
#: purpose: this file is rewritten every few seconds for the whole run, and the
#: point is "is it moving / what is it saying now", not the full transcript —
#: which :func:`write_run_log` lands in one piece at the end anyway.
RUN_LOG_PROGRESS_MAX_BYTES = 200_000


def write_run_log_progress(
    work_dir: str | Path,
    tail_text: str,
    *,
    elapsed_s: float,
    line_count: int,
    idle_s: float | None = None,
    max_bytes: int = RUN_LOG_PROGRESS_MAX_BYTES,
) -> Path:
    """Refresh ``<work_dir>/run.log`` with a live tail while the run is STILL going.

    Without this, run.log holds :data:`RUN_LOG_PENDING` for the whole duration
    of a run — so a healthy 6-minute simulation and a wedged one are
    indistinguishable until the timeout fires, and the only way to tell them
    apart is to wait out ``timeout_ms`` (fpu F-18).

    The body line stays inside :data:`RUN_LOG_IN_PROGRESS_PREFIX`, so every
    freshness check (:func:`run_log_is_current`) still reads this as "not
    finished" — a live tail must never be mistaken for a verdict. *idle_s* (how
    long since the sim last printed anything) is the hang-versus-slow signal:
    a sim that has printed nothing for minutes is stuck, not merely slow
    (fpu F-21, where an ``$fopen`` on a directory spun silently to the timeout).

    Best-effort by contract — callers ignore ``OSError``; losing a progress
    refresh must never fail a run that is otherwise fine.
    """
    path = Path(work_dir, RUN_LOG_NAME)
    first, _ = _read_run_log_head(path)
    is_header = first.startswith(RUN_LOG_HEADER_PREFIX.encode()) and first.endswith(b"\n")
    header = first if is_header else b""
    idle = "" if idle_s is None else f", last output {idle_s:.0f}s ago"
    status = (
        f"{RUN_LOG_IN_PROGRESS_PREFIX} — {elapsed_s:.0f}s elapsed, "
        f"{line_count} output line(s){idle}; live tail below, replaced by the "
        f"full output when the run finishes)\n"
    ).encode()
    body = tail_text.encode("utf-8", errors="replace")
    data = header + status + _cap_log_bytes(body, max(0, max_bytes - len(status)))
    _atomic_write(path, data)
    return path


def write_run_log(
    work_dir: str | Path,
    text: str,
    max_bytes: int | None = 10_000_000,
) -> Path:
    """Persist the raw simulator output to ``<work_dir>/run.log``.

    Companion to :func:`write_result_json`, written into the same directory:
    result.json carries only the verdict plus a 500-char ``first_error``, so
    when the MCP layer truncates the Flow's stdout the full simulator output
    is otherwise gone forever. run.log is the durable copy an agent can
    always read back after the fact.

    Oversized output keeps the TAIL (that is where the verdict sentinel,
    assertion failures and ``$fatal`` wording live) and prepends a one-line
    truncation marker recording the original size; the written file never
    exceeds *max_bytes*. Pass ``None`` when the caller's contract requires an
    unabridged archival copy. The write is atomic (PID-suffixed tmp + rename,
    the ``_job_records`` idiom) so a concurrent reader never sees a torn log.

    An existing :func:`begin_run_log` header is carried over verbatim — the
    run-halves write this file from their own child process and know nothing
    about the run identity the prepare half stamped in, so preserving the
    line here is what keeps :func:`run_log_is_current` answerable.
    """
    path = Path(work_dir, RUN_LOG_NAME)
    first, _ = _read_run_log_head(path)
    is_header = first.startswith(RUN_LOG_HEADER_PREFIX.encode()) and first.endswith(b"\n")
    header = first if is_header else b""
    # errors="replace": runner output is usually already decoded with
    # errors="replace" upstream, but a TB can still smuggle lone surrogates
    # through — never let an encode error lose the whole log.
    body = text.encode("utf-8", errors="replace")
    data = (
        header + body
        if max_bytes is None
        else header
        + _cap_log_bytes(
            body,
            max(0, max_bytes - len(header)),
        )
    )
    _atomic_write(path, data)
    return path


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
