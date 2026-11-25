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

from booley.presentation.colors import strip_ansi
from booley.runtime.endpoint_execution import (
    EXIT_SUCCESS,
    HOST_OOM_KILL_NOTE,
    OOM_KILL_NOTE,
    OOM_REQUEUE_NOTE,
    EndpointOutcome,
    normalize_completion_error,
)
from booley.runtime.execution_records import atomic_write_json
from booley.runtime.filesystem_utils import make_directories
from booley.runtime.timefmt import utc_now_rfc3339

if TYPE_CHECKING:
    from booley.flows.endpoint_state import EndpointState


logger = logging.getLogger(__name__)


def automatic_target_notice(flow: str, target: str) -> str:
    """Format the announcement for one implicitly selected canonical Target."""
    return f"{flow}: --target omitted; selected Target {target}."


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
    wrapper both see it. Completion publication also emits an otherwise unseen
    failure ``report_text`` on stderr, so its reason is never trapped in a
    report.json that may not exist.
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
        of stdout tail is far too eager: a short report_text — a bare
        ``"BLOCKED"``, or any ``str(exc)`` — matches the moment those
        characters appear anywhere inside a streamed agent log, and then the
        stderr echo, the only place that failure reason exists, disappears.
        Requiring the text to occupy whole lines still recognises the
        ``print(report_text)`` this exists for (F-28), while a mention inside a
        longer line no longer silences the diagnostic.

        The stdout tail is compared without ANSI codes: a Flow may print a
        coloured console copy of its plain report on a terminal, and that copy
        still counts as the report having been shown.
        """
        return _contains_whole_line_block(
            strip_ansi("".join(self._chunks)), text, leading_truncated=self._truncated
        )


def _contains_whole_line_block(blob: str, text: str, *, leading_truncated: bool = False) -> bool:
    """Return whether *text* appears in *blob* as complete lines.

    Leading indentation belongs to the needle: a block that begins with an
    indented line (a table header) matches only where that exact indentation
    starts a line, so an indented echo of a one-line verdict never counts.
    """
    needle = (text or "").lstrip("\r\n").rstrip()
    if not needle:
        return False
    start = 0
    while (idx := blob.find(needle, start)) >= 0:
        starts_line = blob[idx - 1] == "\n" if idx else not leading_truncated
        end = idx + len(needle)
        ends_line = end == len(blob) or blob[end] == "\n"
        if starts_line and ends_line:
            return True
        start = idx + 1
    return False


def emit_progress(endpoint: EndpointState, line: str) -> None:
    """Show one progress line on the optional CLI sink."""
    observer = endpoint.terminal_progress
    if observer is not None:
        observer.stage_changed(line)


def emit_completion(endpoint: EndpointState, line: str, *, repeats_at_end: bool = False) -> None:
    """Render one completed unit immediately inside the open endpoint box."""
    observer = endpoint.terminal_progress
    if observer is not None and not repeats_at_end:
        observer.stage_changed(line)


def _next_invocation_dir(endpoint: EndpointState, report_dir: Path) -> Path:
    """Atomically reserve the next numbered ``{endpoint_name}/{N}`` directory."""
    endpoint_dir = report_dir / endpoint.name
    make_directories(endpoint_dir)
    while True:
        # `.pruned-<N>` directories keep their number reserved.
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
    make_directories(report_dir)
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
    make_directories(report_dir)
    if not endpoint._skip_report_detail_refresh:
        endpoint.refresh_report_detail(result)
    report = _report_document(endpoint, result)
    inv_dir = endpoint._reserved_invocation_dir
    if inv_dir is None:
        inv_dir = endpoint._next_invocation_dir(report_dir)
        endpoint._reserved_invocation_dir = inv_dir
    inv_path = inv_dir / "report.json"
    atomic_write_json(inv_path, report)
    flat_path = report_dir / f"{endpoint.name}.json"
    atomic_write_json(flat_path, report)
    endpoint._reserved_invocation_dir = None
    return inv_path


def _process_resources() -> dict[str, float] | None:
    """Snapshot process-lifetime CPU and largest reaped process RSS in MiB."""
    try:
        import resource
    except ImportError:
        return None
    try:
        own = resource.getrusage(resource.RUSAGE_SELF)
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
    except OSError:
        logger.warning("Process resource accounting unavailable", exc_info=True)
        return None
    divisor = 1024**2 if sys.platform == "darwin" else 1024
    return {
        "cpu_user_s": own.ru_utime + children.ru_utime,
        "cpu_sys_s": own.ru_stime + children.ru_stime,
        "peak_rss_mb": max(own.ru_maxrss, children.ru_maxrss) / divisor,
    }


def _report_document(endpoint: EndpointState, result: EndpointOutcome) -> dict[str, Any]:
    elapsed_s = round(time.monotonic() - endpoint._start_time, 2)
    passed = result.exit_code == EXIT_SUCCESS
    identity_key = "flow" if endpoint.endpoint_kind == "flow" else "mcp_tool"
    report: dict[str, Any] = {
        identity_key: endpoint.name,
        "slug": endpoint.args.slug or "",
        "target": endpoint._selected_target,
        "exit_code": result.exit_code,
        "detail": result.detail,
        "timestamp": utc_now_rfc3339(),
        "elapsed_s": elapsed_s,
        "passed": passed,
    }
    if endpoint.report_schema is not None:
        report["$schema"] = endpoint.report_schema
    mode = result.detail.get("mode")
    if isinstance(mode, str) and mode:
        report["mode"] = mode
    tools = endpoint._eda_tools
    if len(tools) > 1 and (endpoint.endpoint_kind == "flow" or any(tools.values())):
        report["eda_tools"] = dict(tools)
    elif endpoint._eda_tool:
        report["eda_tool"] = endpoint._eda_tool
    resources = _process_resources()
    if resources is not None:
        report["resources"] = resources
    # Job identity (ADR 0027): the MCP dispatch layer exports the run_id
    # it handed the agent, so a poll can match this report to ITS run
    # instead of trusting the last-writer-wins flat copy — concurrent
    # runs of the same endpoint (Specialists, Flows) would
    # otherwise cross-attribute results.
    run_id = os.environ.get("BOOLEY_RUN_ID", "")
    if run_id:
        report["run_id"] = run_id
    if endpoint._raw_argv is not None:
        report["argv"] = endpoint._raw_argv
    if result.input_tokens or result.output_tokens:
        report["usage"] = _report_usage(result)
    if result.report_text:
        report["report_text"] = result.report_text
    report.update(json.loads(result.cycle_report_json))
    return report


def _report_usage(result: EndpointOutcome) -> dict[str, object]:
    return {
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cached_tokens": result.cached_tokens,
        "cache_create_tokens": result.cache_create_tokens,
        "cost_usd": round(result.cost_usd, 4),
    }


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
    if endpoint.code_modifying:
        endpoint._stamp_git_diff_stats(result)


def _stamp_git_diff_stats(endpoint: EndpointState, result: EndpointOutcome) -> None:
    """Compute lines_added/lines_removed from git diff --numstat."""
    ref = endpoint._pre_run_head
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


def _publish_report(endpoint: EndpointState, result: EndpointOutcome) -> None:
    if endpoint.write_report(result) is None:
        endpoint._warn_no_report_artifact()


def _publish_console_report(endpoint: EndpointState, result: EndpointOutcome) -> None:
    """Publish one CLI verdict without coupling it to durable persistence."""
    if not endpoint._console_publication_requested:
        return
    _publish_console_verdict(endpoint, result)
    notice = endpoint._automatic_target_notice
    if notice and not endpoint._automatic_target_notice_published:
        print(notice, file=sys.stderr, flush=True)
        endpoint._automatic_target_notice_published = True


def _publish_console_verdict(endpoint: EndpointState, result: EndpointOutcome) -> None:
    """Publish the original verdict body before any invocation notice."""
    if not result.report_text:
        return
    witness = endpoint._stdout_witness
    # The host line leads the result; the Flow's own verdict, printed while it ran, follows it.
    host_line, text = "", result.report_text
    if text.startswith(HOST_OOM_KILL_NOTE):
        host_line, _, text = text.partition("\n")
    if witness is not None and witness.saw(text):
        if host_line:
            print(host_line, file=sys.stderr, flush=True)
        return
    # A note appended after the report the witness already saw is all that is new.
    for opening in ("\nCompletion failure (", f"\n{OOM_REQUEUE_NOTE}", f"\n{OOM_KILL_NOTE}"):
        original, separator, rest = text.partition(opening)
        if separator and witness is not None and witness.saw(original):
            print(
                "\n".join(filter(None, (host_line, f"{opening[1:]}{rest}"))),
                file=sys.stderr,
                flush=True,
            )
            return
    if result.exit_code != EXIT_SUCCESS:
        print(result.report_text, file=sys.stderr, flush=True)
        return
    if endpoint.endpoint_kind == "flow" or endpoint.announce_success_report:
        print(result.report_text, flush=True)


def removed_root(endpoint: EndpointState) -> str | None:
    """Name the checkout or Booley Directory that vanished since the run started."""
    if endpoint._run_roots is None:
        return None
    checkout, booley_dir = endpoint._run_roots
    if not checkout.is_dir():
        return f"checkout {checkout}"
    if booley_dir is not None and not booley_dir.is_dir():
        return f"Booley Directory {booley_dir}"
    return None


def report_root_removed(endpoint: EndpointState) -> bool:
    """Whether the report directory lies under a checkout or Booley Directory that vanished."""
    report_dir = endpoint.args.report_dir
    if report_dir is None or endpoint._run_roots is None:
        return False
    report_dir = Path(report_dir).absolute()
    return any(
        root is not None and not root.is_dir() and report_dir.is_relative_to(root)
        for root in endpoint._run_roots
    )


def _finish_main(endpoint: EndpointState, result: EndpointOutcome, *, persist_report: bool) -> int:
    """Post-run bookkeeping shared by every exit path."""
    try:
        if persist_report:
            _publish_report_with_recovery(endpoint, result)
    finally:
        try:
            observer = endpoint.terminal_progress
            if observer is not None:
                observer.close()
            endpoint._publish_console_report(result)
            if observer is not None:
                sys.stdout.flush()
                observer.footer()
        except OSError as exc:
            logger.debug("Endpoint console publication failed", exc_info=True)
            normalize_completion_error(result, exc, "publish console diagnosis")
            _recover_report_publication(endpoint, result)
    return result.exit_code


def _publish_report_with_recovery(endpoint: EndpointState, result: EndpointOutcome) -> None:
    try:
        _publish_report(endpoint, result)
    except Exception as exc:
        logger.debug("Endpoint report publication failed", exc_info=True)
        normalize_completion_error(result, exc, "publish report.json")
        _recover_report_publication(endpoint, result)


def _recover_report_publication(endpoint: EndpointState, result: EndpointOutcome) -> None:
    previous = endpoint._skip_report_detail_refresh
    endpoint._skip_report_detail_refresh = True
    try:
        _publish_report(endpoint, result)
    except Exception as exc:
        logger.debug("Endpoint recovery report publication failed", exc_info=True)
        normalize_completion_error(result, exc, "publish report.json")
        endpoint._reserved_invocation_dir = None
    finally:
        endpoint._skip_report_detail_refresh = previous
