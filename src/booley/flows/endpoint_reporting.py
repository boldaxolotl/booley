"""Endpoint artifacts, display and completion publication."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booley.flows.endpoint_events import (
    _emit_criteria_update,
    _endpoint_end_event,
    _endpoint_progress_event,
    _write_display_event,
)
from booley.runtime.endpoint_execution import (
    EXIT_SUCCESS,
    EndpointOutcome,
)
from booley.runtime.timefmt import utc_now_rfc3339

if TYPE_CHECKING:
    from booley.flows.endpoint_state import EndpointState


logger = logging.getLogger(__name__)


class _BufferWitness:
    """Byte-level tee for :attr:`_StdoutWitness.buffer`.

    ``bwave._safe_print``'s legacy-Windows fallback writes encoded bytes
    straight to ``sys.stdout.buffer``, which bypasses ``write`` entirely.
    Decoding them back into the text witness keeps "did the endpoint already print
    this?" honest on that path instead of silently answering no.
    """

    def __init__(self, wrapped: Any, witness: _StdoutWitness) -> None:
        self.wrapped = wrapped
        self._witness = witness

    def _record(self, data: Any) -> None:
        if isinstance(data, (bytes, bytearray, memoryview)):
            encoding = getattr(self._witness.wrapped, "encoding", None) or "utf-8"
            self._witness._record(bytes(data).decode(encoding, errors="replace"))

    def write(self, data: Any) -> Any:
        self._record(data)
        return self.wrapped.write(data)

    def writelines(self, lines: Any) -> None:
        chunks = list(lines)
        for chunk in chunks:
            self._record(chunk)
        self.wrapped.writelines(chunks)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.wrapped, name)


class _StdoutWitness:
    """A tee that remembers the tail of what a endpoint wrote to ``sys.stdout``.

    Several Booley Flows end ``_run()`` with ``print(report_text)`` — the verdict
    block belongs on stdout, where a human running ``booley flow`` and an MCP
    wrapper both see it. ``_post_run`` *also* prints ``report_text``, on stderr,
    so a failure's reason is never trapped in a report.json that may not exist.
    On any console that merges the two streams every FAIL path therefore
    rendered its whole verdict block twice (fpu F-28).

    Fixing that per endpoint would need every one of them to cooperate with a flag
    and would silently regress the moment a new endpoint printed its own report.
    Instead the base layer observes what actually reached stdout and skips the
    echo when the text is already there — one fix, all endpoints, no contract to
    remember. This also covers callers that capture stdout/stderr separately
    and merge them afterward. Only the tail is retained: enough to cover any
    report_text an endpoint could plausibly print, bounded so a chatty Specialist
    cannot grow it.

    Every path stdout offers is teed — ``write``, ``writelines`` and the
    byte-level ``buffer`` — because an unwitnessed write reads as "the endpoint
    never printed this", and the wrong answer there costs a diagnostic.
    """

    _MAX_CHARS = 1 << 20  # 1 MiB of stdout tail

    def __init__(self, wrapped: Any) -> None:
        self.wrapped = wrapped
        self._chunks: list[str] = []
        self._size = 0
        # True once the ring buffer has dropped a chunk: the retained text no
        # longer starts at a line boundary, so `saw` must stop trusting
        # position 0 as one.
        self._truncated = False
        self._buffer: _BufferWitness | None = None

    def _record(self, text: str) -> None:
        """Remember *text* in the bounded tail (no forwarding)."""
        self._chunks.append(text)
        self._size += len(text)
        while self._size > self._MAX_CHARS and len(self._chunks) > 1:
            self._size -= len(self._chunks.pop(0))
            self._truncated = True

    def write(self, text: str) -> int:
        self._record(text)
        return self.wrapped.write(text)

    def writelines(self, lines: Any) -> None:
        # Not covered by ``write``: file objects implement writelines natively,
        # so delegating it through __getattr__ would let a whole verdict block
        # reach the terminal unwitnessed.
        chunks = list(lines)
        for chunk in chunks:
            self._record(chunk)
        self.wrapped.writelines(chunks)

    @property
    def buffer(self) -> Any:
        """The byte-level stream, teed — see :class:`_BufferWitness`.

        A real attribute so it wins over ``__getattr__``; raises
        ``AttributeError`` like the wrapped stream would when there is no
        binary layer (``hasattr(sys.stdout, "buffer")`` must stay truthful).
        """
        wrapped_buffer = getattr(self.wrapped, "buffer", None)
        if wrapped_buffer is None:
            raise AttributeError("buffer")
        if self._buffer is None or self._buffer.wrapped is not wrapped_buffer:
            self._buffer = _BufferWitness(wrapped_buffer, self)
        return self._buffer

    def __getattr__(self, name: str) -> Any:
        # flush/fileno/encoding/isatty/... belong to the real stream.
        return getattr(self.wrapped, name)

    def saw(self, text: str) -> bool:
        """Whether *text* was printed to stdout as a block of whole lines.

        Line-anchored on purpose. An unanchored substring scan over a megabyte
        of stdout tail is far too eager: a short report_text — tb_coder's bare
        ``"BLOCKED"``, or any ``str(exc)`` — matches the moment those
        characters appear anywhere inside a streamed agent log, and then the
        stderr echo, the only place that failure reason exists, disappears.
        Requiring the text to occupy whole lines still recognises the
        ``print(report_text)`` this exists for (F-28), while a mention inside a
        longer line no longer silences the diagnostic.
        """
        needle = (text or "").strip()
        if not needle:
            return False
        blob = "".join(self._chunks)
        start = 0
        while (idx := blob.find(needle, start)) >= 0:
            starts_line = blob[idx - 1] == "\n" if idx else not self._truncated
            end = idx + len(needle)
            ends_line = end == len(blob) or blob[end] == "\n"
            if starts_line and ends_line:
                return True
            start = idx + 1
        return False


def emit_progress(endpoint: EndpointState, line: str) -> None:
    """Write a progress line to display.jsonl for live host terminal output."""
    _write_display_event(_endpoint_progress_event(endpoint.name, line))


def emit_completion(endpoint: EndpointState, line: str, *, repeats_at_end: bool = False) -> None:
    """Render one completed unit immediately inside the open endpoint box."""
    _write_display_event(
        _endpoint_progress_event(
            endpoint.name,
            line,
            completion=True,
            repeats_at_end=repeats_at_end,
        )
    )


def _next_invocation_dir(endpoint: EndpointState, report_dir: Path) -> Path:
    """Atomically reserve the next numbered ``{endpoint_name}/{N}`` directory."""
    endpoint_dir = report_dir / endpoint.name
    endpoint_dir.mkdir(parents=True, exist_ok=True)
    while True:
        # Count observed names even if pruning renames a directory during this scan.
        # Rechecking is_dir() would discard the old name before seeing its tombstone.
        existing = (
            int(d.name.removeprefix(".pruned-"))
            for d in endpoint_dir.iterdir()
            if d.name.removeprefix(".pruned-").isdigit()
        )
        inv_dir = endpoint_dir / str(max(existing, default=0) + 1)
        try:
            # mkdir without exist_ok is the cross-process reservation.
            inv_dir.mkdir()
        except FileExistsError:
            # A concurrent same-endpoint writer claimed this number after our
            # scan. Re-scan and reserve the next one instead of failing.
            continue
        return inv_dir


def reserve_invocation_dir(endpoint: EndpointState) -> Path | None:
    """Reserve the numbered report directory before ``write_report()``.

    A endpoint may stage artifacts (per-run logs, intermediate outputs) before
    the final EndpointOutcome exists. Reserving lets those artifacts and
    report.json live under the same ``flow-reports/<endpoint>/<N>/`` directory.
    """
    report_dir = endpoint.args.report_dir
    if report_dir is None:
        return None
    report_dir.mkdir(parents=True, exist_ok=True)
    if endpoint._reserved_invocation_dir is None:
        endpoint._reserved_invocation_dir = endpoint._next_invocation_dir(report_dir)
    return endpoint._reserved_invocation_dir


def write_report(endpoint: EndpointState, result: EndpointOutcome) -> Path | None:
    """Write structured JSON report to report_dir/{endpoint_name}/{N}/report.json.

    Also writes a flat ``{endpoint_name}.json`` copy for backward compatibility
    (developer prompt rule 11, MCP ``_try_read_report``).
    """
    report_dir = endpoint.args.report_dir
    if report_dir is None:
        return None
    report_dir.mkdir(parents=True, exist_ok=True)
    elapsed_s = round(time.monotonic() - endpoint._start_time, 2)
    passed = result.exit_code == EXIT_SUCCESS
    identity_key = "flow" if endpoint.endpoint_kind == "flow" else "mcp_tool"
    report: dict[str, Any] = {
        identity_key: endpoint.name,
        "slug": endpoint.args.slug or "",
        "target": endpoint._selected_target,
        "exit_code": result.exit_code,
        "criterion_key": result.criterion_key,
        "criterion_met": result.criterion_met,
        "detail": result.detail,
        "timestamp": utc_now_rfc3339(),
        "elapsed_s": elapsed_s,
        "passed": passed,
    }
    mode = result.detail.get("mode")
    if isinstance(mode, str) and mode:
        report["mode"] = mode
    if endpoint._eda_tool:
        report["eda_tool"] = endpoint._eda_tool
    # Job identity (ADR 0027): the MCP dispatch layer exports the run_id
    # it handed the agent, so a poll can match this report to ITS run
    # instead of trusting the last-writer-wins flat copy — concurrent
    # runs of the same endpoint (light-class specialists, heavy+host) would
    # otherwise cross-attribute results.
    run_id = os.environ.get("BOOLEY_RUN_ID", "")
    if run_id:
        report["run_id"] = run_id
    if endpoint._raw_argv is not None:
        report["argv"] = endpoint._raw_argv
    if result.input_tokens or result.output_tokens:
        report["usage"] = {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cached_tokens": result.cached_tokens,
            "cache_create_tokens": result.cache_create_tokens,
            "cost_usd": round(result.cost_usd, 4),
        }
    if result.report_text:
        report["report_text"] = result.report_text
    report_json = json.dumps(report, indent=2)
    # Per-invocation numbered report
    inv_dir = endpoint._reserved_invocation_dir
    endpoint._reserved_invocation_dir = None
    if inv_dir is None:
        inv_dir = endpoint._next_invocation_dir(report_dir)
    inv_path = inv_dir / "report.json"
    inv_path.write_text(report_json, encoding="utf-8")
    # Flat copy for backward compat
    flat_path = report_dir / f"{endpoint.name}.json"
    flat_path.write_text(report_json, encoding="utf-8")
    return inv_path


def _warn_no_report_artifact(endpoint: EndpointState) -> None:
    """Say, once per run, that this run persisted no verdict artifact.

    Without ``--report-dir`` (outside a ticket, where the runtime fills it
    in) the verdict lives only in this process's stdout: no
    ``report.json``, nothing for a later poll or a triage sweep to read.
    The reviewer learned this the expensive way — a standalone review
    looked recorded when nothing was written (SETUP-F-39) — but the gap is
    every endpoint's, so the notice belongs here rather than in one endpoint.

    stderr, not stdout: the verdict block on stdout is the endpoint's product
    and may be piped/parsed; this is an operator note about the run.

    One channel, deliberately. ``logger.warning`` also lands on stderr (via
    ``cli()``'s basicConfig, or logging's last-resort handler when nothing
    configured it), so emitting both printed this same sentence twice —
    the exact defect the F-28 work above set out to remove. The plain
    ``print`` wins because it carries the operator wording verbatim and
    does not depend on how logging happens to be configured.
    """
    print(
        f"WARN: no --report-dir — this {endpoint.name} verdict is printed only "
        "(no report.json is written for this run)",
        file=sys.stderr,
        flush=True,
    )


def _get_head_sha(endpoint: EndpointState) -> str | None:
    """Return current HEAD SHA, or None on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=endpoint.args.work_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _finalize_result(endpoint: EndpointState, result: EndpointOutcome) -> None:
    """Hook for subclasses to enrich an EndpointOutcome before reporting.

    Called after _run() completes. Base implementation stamps git diff
    stats for code-modifying endpoints. Specialist overrides this to also
    stamp accumulated token/cost data from sub-agent calls.
    """
    if getattr(endpoint.args, "diagnostic", False):
        result.detail = dict(result.detail or {})
        result.detail["acceptance_effect"] = "diagnostic"
    if endpoint.code_modifying:
        endpoint._stamp_git_diff_stats(result)


def _stamp_git_diff_stats(endpoint: EndpointState, result: EndpointOutcome) -> None:
    """Compute lines_added/lines_removed from git diff --numstat."""
    ref = getattr(endpoint, "_pre_run_head", None)
    if not ref:
        return
    try:
        proc = subprocess.run(
            ["git", "diff", "--numstat", ref, "HEAD"],
            cwd=endpoint.args.work_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            return
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                try:
                    result.lines_added += int(parts[0])
                    result.lines_removed += int(parts[1])
                except ValueError:
                    pass
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _resolve_display_config(endpoint: EndpointState) -> str | None:
    """Resolve the config tag shown in display events."""
    return endpoint.display_tag or (
        (endpoint._selected_target or None) if endpoint.config_aware else None
    )


def _resolve_display_label(endpoint: EndpointState) -> str | None:
    """Return an optional human label without changing endpoint semantics."""
    return None


def _post_run(endpoint: EndpointState, result: EndpointOutcome, duration: float) -> None:
    """Persist mutable run state and publish the report.

    The execution coordinator calls ``record_acceptance`` first. That step
    also runs ``_pre_save_hook`` so this timeline sees the final rather than
    provisional outcome before saving mutable state.
    """
    criteria_set = list(endpoint._pending_criteria_set or ())
    # Extract key endpoint arguments for timeline filtering (e.g. --category)
    endpoint_args: dict[str, Any] | None = None
    if endpoint._args:
        _ta: dict[str, Any] = {}
        for attr in ("category", "config", "reason", "dry_run"):
            val = getattr(endpoint._args, attr, None)
            if val:
                _ta[attr] = val
        endpoint_args = _ta or None
    endpoint.state.record_mcp_tool_run(
        endpoint.name,
        result.exit_code,
        endpoint_kind=endpoint.endpoint_kind,
        duration_s=duration,
        criteria_set=criteria_set or None,
        cost_usd=result.cost_usd if result.cost_usd else None,
        args=endpoint_args,
    )
    if endpoint._state is not None and endpoint._state._file_path is not None:
        endpoint.state.save()
        _emit_criteria_update(endpoint.state)
    if endpoint.write_report(result) is None:
        endpoint._warn_no_report_artifact()
    # Human / standalone mode (no state file): the actionable diagnostic lives
    # in report_text, which is only persisted to report.json when --report-dir
    # is given. On failure that otherwise leaves a bare exit-1 with the real
    # reason trapped in a file that may not exist. Surface it on stderr so a
    # human — or an MCP wrapper that sees only stdout/stderr — gets the cause.
    # Pre-state gate rejections exit before this point.
    #
    # Skip the echo whenever the endpoint already printed the same text on
    # stdout. Callers commonly capture stdout/stderr separately and merge
    # them afterward; keying suppression on a shared OS sink duplicated the
    # complete verdict in that normal execution surface (Taxi F-32).
    human_mode = endpoint._state is None or endpoint._state._file_path is None
    witness = endpoint._stdout_witness
    already_shown = witness is not None and witness.saw(result.report_text)
    if human_mode and result.report_text and not already_shown:
        failed = result.exit_code != EXIT_SUCCESS
        if failed:
            print(result.report_text, file=sys.stderr, flush=True)
        elif endpoint.announce_success_report:
            # A passing run: put the verdict on stdout so it is not silent.
            print(result.report_text, flush=True)


def _finish_main(
    endpoint: EndpointState,
    result: EndpointOutcome,
    display_target: str | None,
    display_label: str | None,
    started: float | None,
    *,
    acceptance_recorded: bool,
    dry_run: bool,
    non_persisting_dry_run: bool,
) -> int:
    """Post-run bookkeeping + the endpoint_end event, shared by every exit path."""
    duration = (time.monotonic() - started) if started is not None else 0.0
    try:
        if acceptance_recorded and not non_persisting_dry_run:
            endpoint._post_run(result, duration)
    finally:
        endpoint._pending_criteria_set = None
        _write_display_event(
            _endpoint_end_event(
                endpoint.name,
                display_target,
                result,
                duration,
                display_label=display_label,
                dry_run=dry_run,
            ),
        )
    return result.exit_code
