"""MCP tool base class shared by Booley Flows and Specialists.

Exit code contract:
  0 — success (criterion met)
  1 — failure (criterion not met, but endpoint ran correctly)
  2 — error (endpoint itself failed, infrastructure problem)

Every MCP tool:
  - Parses CLI args via argparse
  - Loads/saves DevelopmentState
  - Writes a structured JSON report to the stage log directory
  - Classifies post-run git diffs as RTL or TB
  - Invalidates dependent criteria when code-modifying endpoints change files
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from booley.dev_support.criterion_categories import verification_fingerprint_categories
from booley.dev_support.development_state import (
    SOURCE_FINGERPRINT_DETAIL_KEY,
    CriterionChange,
    DevelopmentState,
)
from booley.flows import execution
from booley.flows.criterion_freshness import build_criterion_freshness
from booley.fusesoc.fusesoc_registry import FuseSocError
from booley.runtime import job_slots
from booley.runtime.job_records import _proc_cmdline
from booley.runtime.timefmt import utc_now_rfc3339
from booley.ticket_board.paths import ticket_runtime_dir

# moved out for SRP; re-imported for use + backward compat
from .diff_classify import (
    _RTL_DIRS,  # noqa: F401 — re-exported so tests can patch booley.dev_support.base._RTL_DIRS
    _TB_DIRS,  # noqa: F401 — re-exported so tests can patch booley.dev_support.base._TB_DIRS
    _classify_files,
    read_source_dirs_from_toml,  # noqa: F401 — public API re-export; base itself never calls it
)
from .events import (
    _emit_criteria_update,
    _endpoint_end_event,
    _endpoint_progress_event,
    _endpoint_start_event,
    _specialist_thinking_event,  # noqa: F401 — re-exported for specialist.py
    _write_display_event,
)
from .run_lock import (
    _as_pid,  # noqa: F401 — re-exported for booley.dev_support.base importers/tests
    _scan_endpoint_events,  # noqa: F401 — re-exported for booley.dev_support.base importers/tests
)

logger = logging.getLogger(__name__)

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_ERROR = 2

# A leading `C:`-style component. On Windows pathlib parses this as the drive of
# an ABSOLUTE path; on POSIX it is just an ordinary relative directory name.
_DRIVE_COMPONENT_RE = re.compile(r"^[A-Za-z]:$")


def _report_dir_arg(value: str) -> Path:
    """``--report-dir`` argparse type — rejects an MSYS-mangled host path.

    Git Bash / MSYS2 rewrites POSIX-looking argv into Windows paths when it
    spawns a native exe, so

        booley session enter -- python3 -m booley.flows.lint --report-dir /tmp/rep

    reaches the endpoint inside the Linux container as
    ``C:/Users/<you>/AppData/Local/Temp/rep`` — which POSIX pathlib reads as a
    *relative* path whose first component is literally ``C:``. Every
    ``report_dir.mkdir(parents=True)`` downstream then happily created
    ``/work/C:/Users/...`` inside the workspace, i.e. a junk ``C:`` directory in
    the user's repo (the bind mount renders the illegal colon as U+F03A), while
    the "See <path>" summary echoed the Windows path back and looked plausible.

    A relative path whose first component is a drive letter is never something a
    caller means, so refuse it and name the cause. The check keys off pathlib's
    own flavour rather than the host OS: on Windows ``C:/x`` is absolute and
    passes untouched (a real, legitimate host report dir); only the POSIX
    reading — the broken one — is caught.
    """
    path = Path(value)
    if not path.is_absolute() and path.parts and _DRIVE_COMPONENT_RE.match(path.parts[0]):
        raise argparse.ArgumentTypeError(
            f"--report-dir {value!r} is a Windows host path, but this endpoint "
            f"writes inside the container, where {path.parts[0]!r} is just a "
            "directory name — the reports would land in a junk "
            f"'{path.parts[0]}' folder in your workspace.\n"
            "Git Bash/MSYS rewrites '/tmp/...' into a Windows path when it "
            "launches booley. Re-run with MSYS_NO_PATHCONV=1 (or MSYS2_ARG_CONV_EXCL='*'), "
            "double the leading slash ('//tmp/rep'), or pass a path under the "
            "workspace instead."
        )
    return path


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


@dataclass
class McpToolResult:
    """Structured result from an MCP tool invocation."""

    exit_code: int = EXIT_SUCCESS
    criterion_key: str = ""
    criterion_met: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    report_text: str = ""
    # Agent token/cost tracking (populated by Specialists, zero for Flows)
    input_tokens: int = 0  # inclusive prompt total (uncached + cache reads + writes)
    output_tokens: int = 0
    cached_tokens: int = 0  # cache reads
    cache_create_tokens: int = 0  # cache writes
    cost_usd: float = 0.0
    # Git diff stats (populated by _finalize_result for code-modifying endpoints)
    lines_added: int = 0
    lines_removed: int = 0
    # One-line summaries displayed in the host terminal endpoint box
    display_lines: list[str] = field(default_factory=list)
    # One-line structured summary for Console history strip
    summary: str = ""


class McpTool(ABC):
    """Abstract base class for MCP endpoints.

    Subclasses implement ``_add_args``, ``_run``, and declare metadata
    via class attributes.
    """

    endpoint_kind: ClassVar[str] = "mcp_tool"

    # --- Class-level metadata (override in subclasses) ---
    name: str = ""
    description: str = ""
    # Does this endpoint modify code? If True, post-run git diff triggers invalidation.
    code_modifying: bool = False
    # Category of code this endpoint modifies (rtl, tb, or None for auto-detect)
    modifies_category: str | None = None
    # Does this endpoint operate per Target? False suppresses the Target in display headers.
    config_aware: bool = True
    # Target-aware deterministic Flows override this so both argparse and the
    # generated MCP schema require an explicit selection.
    target_required: bool = False
    # F-14: on a human/standalone (no-state-file) run, ``report_text`` is only
    # surfaced on *failure* — the PASS verdict lives in ``display_lines``, which
    # the harness UI renders but a bare CLI run drops. For an endpoint whose success
    # is otherwise indistinguishable from a no-op (fpga_impl: a passing route
    # prints zero bytes, exit 0), set this True to also print ``report_text`` on
    # success, so PASS is never silent.
    announce_success_report: bool = False
    # Class-overridable because argparse help is also the MCP ``target`` schema
    # description. The default is endpoint-neutral; a Flow that drives a sim
    # sub-loop can override it with narrower wording.
    target_help: str = (
        "FuseSoC .core Target name(s) this run applies to, comma-separated. "
        "Run with --target <name> (list them with `booley targets`)."
    )
    # Criteria this endpoint can satisfy (must match names in criteria.toml)
    satisfies: ClassVar[list[str]] = []
    # Per-criterion CLI args hint for the developer (criterion -> args string)
    satisfies_args: ClassVar[dict[str, str]] = {}

    def _flow_enabled(self) -> bool:
        """Return whether this Flow is enabled in the scoped config."""
        work_dir = Path(getattr(self.args, "work_dir", ".")) if self.args else None
        return execution.flow_enabled(self.name, work_dir)

    @property
    def _selected_target(self) -> str:
        """The selected Target name used by shared report/display plumbing."""
        return getattr(self.args, "target", "") or ""

    @property
    def display_tag(self) -> str | None:
        """Optional tag shown in the endpoint box header (e.g. "rtl", "tb").

        Overrides config_aware when set. Available after arg parsing.
        """
        return None

    def __init__(self) -> None:
        self._parser = argparse.ArgumentParser(
            prog=self.name or self.__class__.__name__,
            description=self.description,
        )
        self._add_common_args()
        self._add_args(self._parser)
        self._args: argparse.Namespace | None = None
        self._state: DevelopmentState | None = None
        self._start_time: float = 0.0
        self._pre_run_head: str | None = None
        self._raw_argv: list[str] | None = None
        self._invocation_id = uuid.uuid4().hex
        self._reserved_invocation_dir: Path | None = None
        # Set for the duration of _run(); read by _post_run to avoid echoing a
        # verdict block the endpoint already printed itself (F-28).
        self._stdout_witness: _StdoutWitness | None = None
        # The underlying EDA tool that actually ran (e.g. "verilator",
        # "verible", "yosys", "vivado"). Endpoints set this at target-resolution
        # time; write_report() emits it as ``eda_tool`` so reports say which
        # binary produced the result — distinct from the Booley Flow name.
        self._eda_tool: str | None = None

    # --- Argparse ---

    def _add_common_args(self) -> None:
        """Add args shared by all endpoints.

        Ticket context (slug, state-file, report-dir) comes from env vars
        set by the developer.  Endpoints work without them (human mode).
        """
        self._parser.add_argument(
            "--work-dir",
            type=Path,
            default=Path.cwd(),
            help="Working directory (worktree root)",
        )
        self._parser.add_argument(
            "--report-dir",
            type=_report_dir_arg,
            default=None,
            help="Directory for endpoint report output",
        )
        # Kept default="" (not argparse required) so each endpoint's validation can
        # produce specific guidance and discovery can represent no selection.
        self._parser.add_argument(
            "--target",
            required=self.target_required,
            help=self.target_help,
        )
        self._parser.add_argument(
            "--diagnostic",
            action="store_true",
            help=(
                "Run without satisfying Ticket criteria. In Ticket Mode this "
                "is required for a Flow/Target combination outside the sealed contract."
            ),
        )

    @abstractmethod
    def _add_args(self, parser: argparse.ArgumentParser) -> None:
        """Add endpoint-specific arguments."""

    def parse_args(self, argv: list[str] | None = None) -> argparse.Namespace:
        """Parse CLI arguments, filling ticket context from env vars."""
        self._raw_argv = argv if argv is not None else sys.argv[1:]
        self._args = self._parser.parse_args(argv)
        if hasattr(self._args, "steer") and isinstance(self._args.steer, list):
            if len(self._args.steer) == 0:
                self._args.steer = ""
            elif len(self._args.steer) == 1:
                self._args.steer = self._args.steer[0]
        # Ticket context from env vars (set by developer, absent in human mode)
        self._args.slug = os.environ.get("BOOLEY_SLUG", "")
        state_env = os.environ.get("BOOLEY_STATE_FILE", "")
        self._args.state_file = Path(state_env) if state_env else None
        # report-dir: CLI flag wins, then env var, then None
        if self._args.report_dir is None:
            logs_env = os.environ.get("BOOLEY_LOGS_DIR", "")
            runtime_env = os.environ.get("BOOLEY_RUNTIME_DIR", "")
            report_leaf = "flow-reports" if self.endpoint_kind == "flow" else "mcp-tool-reports"
            if runtime_env:
                self._args.report_dir = Path(runtime_env) / report_leaf
            elif logs_env:
                self._args.report_dir = ticket_runtime_dir(logs_env) / report_leaf
            else:
                self._args.report_dir = None
        return self._args

    @property
    def args(self) -> argparse.Namespace:
        if self._args is None:
            raise RuntimeError("parse_args() not called")
        return self._args

    # --- State access ---

    def read_state(self) -> DevelopmentState:
        """Load development state from disk.

        When state_file is None (human mode), returns an empty in-memory state.
        """
        sf = self.args.state_file
        if sf is None:
            self._state = DevelopmentState()  # no file path => save() is a no-op
        else:
            self._state = DevelopmentState.load(sf)
        return self._state

    @property
    def state(self) -> DevelopmentState:
        if self._state is None:
            raise RuntimeError("read_state() not called")
        return self._state

    def set_criterion(
        self,
        key: str,
        met: bool,
        *,
        detail: dict[str, Any] | None = None,
        source_target: str | None = None,
    ) -> None:
        """Set a criterion and persist state. No-op when state has no file."""
        if getattr(self.args, "diagnostic", False) and self.state.strict_criteria:
            logger.info("Diagnostic run: not recording criterion %s", key)
            return
        key = self._criterion_key_for_source(key, source_target)
        stamped_detail = self._stamp_source_fingerprint(
            key,
            met,
            detail,
            source_target=source_target,
        )
        changes = self.state.set_criterion(key, met, detail=stamped_detail)
        if self.state._file_path is not None:
            self._record_acceptance_changes(changes)
            self.state.save()
            _emit_criteria_update(self.state)

    def _record_acceptance_changes(self, changes: list[CriterionChange]) -> None:
        """Append normalized strict-Ticket outcomes before mutable state is saved."""
        if not changes or not self.state.strict_criteria:
            return
        raw_logs_dir = os.environ.get("BOOLEY_LOGS_DIR")
        if not raw_logs_dir:
            return
        target_contract: dict[str, Any] = {}
        raw_ticket_file = os.environ.get("BOOLEY_TICKET_FILE")
        if raw_ticket_file and Path(raw_ticket_file).is_file():
            from booley.ticket_board.target_contract import load_ticket_contract

            contract = load_ticket_contract(raw_ticket_file)
            if contract is not None:
                target_contract = contract.as_dict()
        from booley.ticket_board.acceptance_ledger import record_changes

        record_changes(
            Path(raw_logs_dir),
            self.state,
            changes,
            invocation_id=os.environ.get("BOOLEY_RUN_ID") or self._invocation_id,
            producer=self.name,
            execution_id=os.environ.get("BOOLEY_EXECUTION_ID", ""),
            target_contract=target_contract,
        )

    def _criterion_key_for_source(self, key: str, source_target: str | None) -> str:
        """Render a criterion key with the Target name, never a qualified selector."""
        if not source_target or not key.endswith(source_target):
            return key
        try:
            from booley.targets.target import select_target

            name = select_target(Path(self.args.work_dir), source_target).name
        except FuseSocError:
            return key
        return key[: -len(source_target)] + name

    def _stamp_source_fingerprint(
        self,
        key: str,
        met: bool,
        detail: dict[str, Any] | None,
        *,
        source_target: str | None,
    ) -> dict[str, Any] | None:
        """Attach source freshness metadata to verification criteria.

        Failed criteria retain actionable evidence, so every verification
        outcome receives the same atomic source/contract receipt.
        """
        categories = verification_fingerprint_categories(key)
        is_review = key.startswith(("review_rtl_", "review_tb_"))
        if not categories:
            return detail
        stamped = dict(detail or {})
        try:
            freshness = build_criterion_freshness(
                Path(self.args.work_dir),
                target=source_target,
                categories=categories,
            )
        except (OSError, FuseSocError) as exc:
            logger.warning(
                "Could not stamp source fingerprint for criterion %s, target %r: %s",
                key,
                source_target,
                exc,
            )
            return stamped
        source_detail = freshness.to_detail()
        if is_review and stamped.get("review_detail_version") == 3:
            from booley.dev_support.review_receipt import finalize_review_detail

            return finalize_review_detail(stamped, source_detail)
        stamped[SOURCE_FINGERPRINT_DETAIL_KEY] = source_detail
        return stamped

    def emit_progress(self, line: str) -> None:
        """Write a progress line to display.jsonl for live host terminal output."""
        _write_display_event(_endpoint_progress_event(self.name, line))

    def emit_completion(self, line: str, *, repeats_at_end: bool = False) -> None:
        """Render one completed unit immediately inside the open endpoint box."""
        _write_display_event(
            _endpoint_progress_event(
                self.name,
                line,
                completion=True,
                repeats_at_end=repeats_at_end,
            )
        )

    # --- Report ---

    def _next_invocation_dir(self, report_dir: Path) -> Path:
        """Atomically reserve the next numbered ``{endpoint_name}/{N}`` directory."""
        endpoint_dir = report_dir / self.name
        endpoint_dir.mkdir(parents=True, exist_ok=True)
        while True:
            existing = (
                int(d.name) for d in endpoint_dir.iterdir() if d.is_dir() and d.name.isdigit()
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

    def reserve_invocation_dir(self) -> Path | None:
        """Reserve the numbered report directory before ``write_report()``.

        A endpoint may stage artifacts (per-run logs, intermediate outputs) before
        the final McpToolResult exists. Reserving lets those artifacts and
        report.json live under the same ``flow-reports/<endpoint>/<N>/`` directory.
        """
        report_dir = self.args.report_dir
        if report_dir is None:
            return None
        report_dir.mkdir(parents=True, exist_ok=True)
        if self._reserved_invocation_dir is None:
            self._reserved_invocation_dir = self._next_invocation_dir(report_dir)
        return self._reserved_invocation_dir

    def write_report(self, result: McpToolResult) -> Path | None:
        """Write structured JSON report to report_dir/{endpoint_name}/{N}/report.json.

        Also writes a flat ``{endpoint_name}.json`` copy for backward compatibility
        (developer prompt rule 11, MCP ``_try_read_report``).
        """
        report_dir = self.args.report_dir
        if report_dir is None:
            return None
        report_dir.mkdir(parents=True, exist_ok=True)
        elapsed_s = round(time.monotonic() - self._start_time, 2)
        passed = result.exit_code == EXIT_SUCCESS
        identity_key = "flow" if self.endpoint_kind == "flow" else "mcp_tool"
        report: dict[str, Any] = {
            identity_key: self.name,
            "slug": self.args.slug or "",
            "target": self._selected_target,
            "exit_code": result.exit_code,
            "criterion_key": result.criterion_key,
            "criterion_met": result.criterion_met,
            "detail": result.detail,
            "timestamp": utc_now_rfc3339(),
            "elapsed_s": elapsed_s,
            "passed": passed,
        }
        if self._eda_tool:
            report["eda_tool"] = self._eda_tool
        # Job identity (ADR 0027): the MCP dispatch layer exports the run_id
        # it handed the agent, so a poll can match this report to ITS run
        # instead of trusting the last-writer-wins flat copy — concurrent
        # runs of the same endpoint (light-class specialists, heavy+host) would
        # otherwise cross-attribute results.
        run_id = os.environ.get("BOOLEY_RUN_ID", "")
        if run_id:
            report["run_id"] = run_id
        if self._raw_argv is not None:
            report["argv"] = self._raw_argv
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
        inv_dir = self._reserved_invocation_dir
        self._reserved_invocation_dir = None
        if inv_dir is None:
            inv_dir = self._next_invocation_dir(report_dir)
        inv_path = inv_dir / "report.json"
        inv_path.write_text(report_json, encoding="utf-8")
        # Flat copy for backward compat
        flat_path = report_dir / f"{self.name}.json"
        flat_path.write_text(report_json, encoding="utf-8")
        return inv_path

    def _warn_no_report_artifact(self) -> None:
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
            f"WARN: no --report-dir — this {self.name} verdict is printed only "
            "(no report.json is written for this run)",
            file=sys.stderr,
            flush=True,
        )

    # --- Git helpers ---

    def _get_head_sha(self) -> str | None:
        """Return current HEAD SHA, or None on failure."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.args.work_dir,
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

    # --- Git diff classification ---

    def classify_git_diff(self) -> set[str]:
        """Classify changes since pre-run HEAD as RTL and/or TB categories.

        Uses ``_pre_run_head`` (captured before ``_run()``) so that commits
        made during the endpoint run are included in the diff.

        Returns set of category strings (may be empty if no changes).
        """
        ref = getattr(self, "_pre_run_head", None) or "HEAD"
        work_dir = self.args.work_dir
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", ref],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                return set()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return set()
        return _classify_files(result.stdout.splitlines(), work_dir)

    def invalidate_dependent_criteria(self) -> list[str]:
        """Invalidate criteria based on what files changed.

        Only runs for code-modifying endpoints. Detects RTL vs TB changes
        from git diff and resets the appropriate criteria categories.
        """
        if not self.code_modifying:
            return []
        if self.modifies_category:
            return self.state.reset_category(self.modifies_category)
        categories = self.classify_git_diff()
        reset_keys: list[str] = []
        for cat in categories:
            reset_keys.extend(self.state.reset_category(cat))
        return reset_keys

    # --- Pre-run guardrails ---

    def _default_target_args(self) -> None:
        """Default well-known CLI arguments from the selected Target."""
        if self._state is None:
            return
        if hasattr(self.args, "tb_top") and not getattr(self.args, "tb_top", None):
            target = getattr(self.args, "target", "")
            if target:
                from booley.flows.flow_config import tb_top_for_target

                tb_top = tb_top_for_target(
                    target,
                    getattr(self.args, "work_dir", None),
                    resolved=None,
                )
                if tb_top:
                    self.args.tb_top = tb_top

    def _requested_targets(self) -> list[str]:
        """Return normalized Target tokens without resolving or invoking EDA."""
        raw = getattr(self.args, "target", "")
        values = raw if isinstance(raw, list) else [raw]
        targets: list[str] = []
        for value in values:
            targets.extend(part.strip() for part in str(value or "").split(",") if part.strip())
        return targets

    def _bound_criterion_keys(self, target: str) -> list[str]:
        """Return sealed criteria this endpoint/Target invocation can update."""
        criterion_target = target
        target_identity: str | None = None
        try:
            from booley.targets.target import select_target

            selected = select_target(Path(self.args.work_dir), target)
            criterion_target = selected.name
            target_identity = selected.identity
        except FuseSocError:
            pass
        selector = getattr(self.args, "test", None)
        detail = {
            "test_selector": selector or "all",
            "selected_tests": [selector] if selector else [],
        }
        bound: list[str] = []
        for family in self.satisfies:
            generic_key = f"{family}_{criterion_target}"
            if generic_key in self.state.criteria:
                bound.append(generic_key)
                continue
            for alias in self.state.flow_key_aliases.get(generic_key, []):
                if alias in self.state.criteria and self.state._alias_matches_run(alias, detail):
                    bound.append(alias)
            if family in self.state.criteria:
                bound.append(family)
            bound.extend(
                key
                for key, entry in self.state.criteria.items()
                if key.startswith(f"{family}_")
                and isinstance(entry.params, dict)
                and self._criterion_target_matches(
                    entry.params.get("target"), target, target_identity
                )
                and key not in bound
            )
        return bound

    def _criterion_target_matches(
        self,
        authored: Any,
        invoked: str,
        invoked_identity: str | None,
    ) -> bool:
        """Compare criterion and invocation Targets by identity when resolvable."""
        if not isinstance(authored, str):
            return False
        if authored == invoked:
            return True
        if invoked_identity is None:
            return False
        try:
            from booley.targets.target import select_target

            return select_target(Path(self.args.work_dir), authored).identity == invoked_identity
        except FuseSocError:
            return False

    def _criterion_binding_gate(self) -> McpToolResult | None:
        """Reject an unbound Ticket-mode Target before job admission/EDA."""
        if (
            not self.state.strict_criteria
            or not self.satisfies
            or getattr(self.args, "diagnostic", False)
        ):
            return None
        targets = self._requested_targets()
        if not targets:
            return None
        missing = [target for target in targets if not self._bound_criterion_keys(target)]
        if not missing:
            return None

        from booley.dev_support.criteria_actions import planned_invocation

        pending: list[str] = []
        for key, entry in self.state.criteria.items():
            if key.startswith("_") or not any(
                key == family or key.startswith(f"{family}_") for family in self.satisfies
            ):
                continue
            invocation = planned_invocation(key, entry)
            pending.append(f"  {key} -> {invocation or self.name}")
        pending_text = "\n".join(pending) if pending else "  (no compatible criterion declared)"
        return McpToolResult(
            exit_code=EXIT_ERROR,
            detail={
                "acceptance_effect": "rejected_unbound",
                "unbound_targets": missing,
            },
            report_text=(
                f"{self.name}: Target(s) {', '.join(missing)} do not bind a sealed "
                f"Ticket criterion.\nPending compatible criteria:\n{pending_text}\n"
                "Use --diagnostic only when this is intentionally a non-acceptance run."
            ),
        )

    def _apply_criterion_binding_gate(self, display_target: str | None) -> int | None:
        """Render and persist a binding rejection before job admission."""
        rejection = self._criterion_binding_gate()
        if rejection is None:
            return None
        if rejection.report_text:
            print(rejection.report_text, file=sys.stderr, flush=True)
        return self._finish_main(rejection, display_target, started=None)

    def steering_text(self) -> str:
        """Return steering text from repeated ``--steer`` values."""
        raw = getattr(self.args, "steer", None)
        if raw is None:
            return ""
        values = raw if isinstance(raw, list) else [raw]
        values = [str(v) for v in values]
        return "\n".join(v for v in values if v)

    # --- Main execution ---

    @abstractmethod
    def _run(self) -> McpToolResult:
        """Execute the endpoint's core logic. Implemented by subclasses."""

    def _finalize_result(self, result: McpToolResult) -> None:
        """Hook for subclasses to enrich an McpToolResult before reporting.

        Called after _run() completes. Base implementation stamps git diff
        stats for code-modifying endpoints. Specialist overrides this to also
        stamp accumulated token/cost data from sub-agent calls.
        """
        if getattr(self.args, "diagnostic", False):
            result.detail = dict(result.detail or {})
            result.detail["acceptance_effect"] = "diagnostic"
        if self.code_modifying:
            self._stamp_git_diff_stats(result)

    def _stamp_git_diff_stats(self, result: McpToolResult) -> None:
        """Compute lines_added/lines_removed from git diff --numstat."""
        ref = getattr(self, "_pre_run_head", None)
        if not ref:
            return
        try:
            proc = subprocess.run(
                ["git", "diff", "--numstat", ref, "HEAD"],
                cwd=self.args.work_dir,
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

    def _reject_with_gate(self, result: McpToolResult) -> int:
        """Record a gate rejection and return the exit code.

        Surfaces the rejection reason to stderr so MCP-wrapped Developer Agents
        (which see only the subprocess's stdout/stderr, not report.json) get
        the actionable message.  Without this, callers see only ambient
        startup noise on stderr and misattribute the cause.
        """
        self._start_time = time.monotonic()
        if result.report_text:
            print(result.report_text, file=sys.stderr, flush=True)
        self.write_report(result)
        self.state.record_mcp_tool_run(
            self.name,
            result.exit_code,
            endpoint_kind=self.endpoint_kind,
            duration_s=0.0,
        )
        if self.state._file_path is not None:
            self.state.save()
        return result.exit_code

    def _pre_state_gate(self) -> McpToolResult | None:
        """Reject before loading mutable ticket state; subclasses may override."""
        return None

    def _apply_pre_state_gate(self) -> int | None:
        """Render an early gate rejection without reading or writing ticket state."""
        result = self._pre_state_gate()
        if result is None:
            return None
        if result.report_text:
            print(result.report_text, file=sys.stderr, flush=True)
        return result.exit_code

    def main(self, argv: list[str] | None = None) -> int:
        """Full CLI entry point: parse args, load state, run, report, exit."""
        self.parse_args(argv)
        if (early_exit := self._apply_pre_state_gate()) is not None:
            return early_exit
        self.read_state()
        self._default_target_args()
        display_target = self._resolve_display_config()

        _write_display_event(_endpoint_start_event(self.name, display_target))
        if (binding_exit := self._apply_criterion_binding_gate(display_target)) is not None:
            return binding_exit
        slot_store: job_slots.SlotStore | None = None
        slot_token = None
        result = McpToolResult(exit_code=EXIT_ERROR)
        try:
            # Job admission (ADR 0028): claim a slot for this workload class,
            # or wait in queue order. Queue narration rides endpoint_progress
            # events so the Console/plain log render it with no new plumbing.
            try:
                slot_store, slot_token = self._acquire_job_slot()
            except job_slots.QueueFullError as exc:
                logger.error("Job admission refused: %s", exc)
                result = McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=f"BLOCKED: {exc}. Retry when queued work drains.",
                )
                return self._finish_main(result, display_target, started=None)
            except job_slots.ClaimLostError:
                logger.error("Queued %s run was cancelled before it started", self.name)
                result = McpToolResult(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"CANCELLED: this queued '{self.name}' run was "
                        f"withdrawn (booley_cancel) before it started."
                    ),
                )
                return self._finish_main(result, display_target, started=None)
            # Capture HEAD before _run() so classify_git_diff sees endpoint
            # commits; the clock starts after admission so duration measures
            # the run, not the queue wait.
            self._pre_run_head = self._get_head_sha()
            self._start_time = time.monotonic()
            # Watch stdout for the duration of the run so _post_run can tell a
            # verdict the endpoint already printed from one only it can surface.
            witness = _StdoutWitness(sys.stdout)
            self._stdout_witness = witness
            sys.stdout = witness  # type: ignore[assignment]
            try:
                result = self._run()
            except Exception:
                logger.exception("MCP endpoint %s failed with exception", self.name)
                result = McpToolResult(exit_code=EXIT_ERROR)
            finally:
                sys.stdout = witness.wrapped
                self._finalize_result(result)
            return self._finish_main(result, display_target, started=self._start_time)
        finally:
            if slot_store is not None and slot_token is not None:
                slot_store.release(slot_token)

    def _finish_main(
        self,
        result: McpToolResult,
        display_target: str | None,
        started: float | None,
    ) -> int:
        """Post-run bookkeeping + the endpoint_end event, shared by every exit path."""
        duration = (time.monotonic() - started) if started is not None else 0.0
        try:
            self._post_run(result, duration)
        finally:
            _write_display_event(
                _endpoint_end_event(
                    self.name,
                    display_target,
                    result,
                    duration,
                    dry_run=bool(getattr(self.args, "dry_run", False)),
                ),
            )
        return result.exit_code

    # Admission class for this endpoint. None = quick endpoint, no admission.
    JOB_CLASS: ClassVar[str | None] = None

    def _resolve_job_class(self) -> str | None:
        """The Job Class this call belongs to, or None for unclassed endpoints."""
        return self.JOB_CLASS

    def _acquire_job_slot(self) -> tuple[job_slots.SlotStore | None, object | None]:
        """Claim this run's admission slot, waiting in queue order if needed.

        Returns ``(store, token)`` to release in main()'s finally, or
        ``(None, None)`` when no admission applies: the endpoint is unclassed, or
        no runtime is configured (bare invocations outside a project keep
        working unguarded). Raises QueueFullError when the class queue is at
        ``queue_max`` — the only admission outcome surfaced as BLOCKED.
        """
        job_class = self._resolve_job_class()
        if job_class is None:
            return (None, None)  # unclassed: skip even the store lookup
        root = job_slots.slots_dir()
        if root is None:
            return (None, None)

        from booley.runtime.shared_infra import _load_rtl_config

        try:
            # Caps must come from the SAME project the slot store belongs to
            # (slots_dir → resolve_project_dir), never from work_dir: a
            # linked worktree can carry a diverged booley.toml, and two
            # claimants promoting under different caps overcommit the class.
            # None = the CWD/BOOLEY_PROJECT_DIR resolution path.
            cfg = _load_rtl_config(None)
        except Exception:  # noqa: BLE001 — best-effort; defaults are safe
            cfg = {}
        caps = job_slots.parse_caps(cfg or {})
        role = (
            job_slots.ROLE_TICKET
            if os.environ.get("BOOLEY_AGENT_ROLE") == "ticket"
            else job_slots.ROLE_INTERACTIVE
        )
        # Claim identity is this live process: record the argv exactly as
        # /proc reports it so the ghost guards can match it later.
        pid = os.getpid()
        argv = _proc_cmdline(pid) or []

        store = job_slots.SlotStore(root, caps)

        # Holder deadline for the reaper (job_slots._is_stale): the MCP
        # dispatch layer exports its real watchdog budget; 2x headroom keeps
        # the deadline a strict upper bound of any legitimate run (reaping a
        # LIVE holder frees an occupied slot → overcommit), while a wedged
        # unsupervised holder is still reclaimed eventually. Bare CLI runs
        # have no exported budget and keep no deadline — they are
        # user-supervised, and the PID guards still apply.
        timeout_s: float | None = None
        env_budget = os.environ.get("BOOLEY_SLOT_TIMEOUT_S", "")
        if env_budget:
            try:
                timeout_s = 2.0 * float(env_budget)
            except ValueError:
                logger.warning("Ignoring unparseable BOOLEY_SLOT_TIMEOUT_S=%r", env_budget)

        def _narrate(position: int) -> None:
            # stderr as well as the log: direct Flow diagnostics run in-process without
            # logging configured and without BOOLEY_LOGS_DIR, so both of the
            # other sinks are no-ops there — a queued run looked like a hang
            # with no hint that another job held the slot (F-27).
            held_by = store.describe_holders(job_class)
            line = f"waiting for {job_class} slot (position {position + 1}); held by {held_by}"
            logger.info("%s: %s", self.name, line)
            print(f"[slot] {self.name}: {line}", file=sys.stderr, flush=True)
            _write_display_event(_endpoint_progress_event(self.name, line))

        token = store.acquire(
            job_class,
            pid=pid,
            argv=argv,
            role=role,
            timeout_s=timeout_s,
            on_queued=_narrate,
        )
        return (store, token)

    def _resolve_display_config(self) -> str | None:
        """Resolve the config tag shown in display events."""
        return self.display_tag or ((self._selected_target or None) if self.config_aware else None)

    def _post_run(self, result: McpToolResult, duration: float) -> None:
        """Handle post-run bookkeeping: invalidation, timeline, report.

        Order matters: ``_pre_save_hook`` may flip ``result.exit_code`` and
        ``result.criterion_met`` (e.g. a specialist rejects when a required
        gate fails).  Running it BEFORE ``record_mcp_tool_run`` ensures the
        timeline entry reflects the final outcome instead of the pre-hook
        provisional success.
        """
        if self._state is not None and self._state._file_path is not None:
            self.state.work_dir = str(Path(self.args.work_dir).resolve())
            self._pre_save_hook(result)
        reset_keys: list[str] = []
        if result.exit_code == EXIT_SUCCESS and self.code_modifying:
            reset_keys = self.invalidate_dependent_criteria()
            self._record_acceptance_changes(
                [
                    CriterionChange(
                        key,
                        self.state.criteria[key].met,
                        "source-invalidated",
                        dict(self.state.criteria[key].detail),
                        self.state.criteria[key].mandatory,
                        dict(self.state.criteria[key].params),
                    )
                    for key in reset_keys
                ]
            )
        criteria_set = [result.criterion_key] if result.criterion_key else []
        if reset_keys:
            criteria_set.extend(f"~{k}" for k in reset_keys)
        # Extract key endpoint arguments for timeline filtering (e.g. --category)
        endpoint_args: dict[str, Any] | None = None
        if self._args:
            _ta: dict[str, Any] = {}
            for attr in ("category", "config", "reason"):
                val = getattr(self._args, attr, None)
                if val:
                    _ta[attr] = val
            endpoint_args = _ta or None
        self.state.record_mcp_tool_run(
            self.name,
            result.exit_code,
            endpoint_kind=self.endpoint_kind,
            duration_s=duration,
            criteria_set=criteria_set or None,
            cost_usd=result.cost_usd if result.cost_usd else None,
            args=endpoint_args,
        )
        if self._state is not None and self._state._file_path is not None:
            self.state.save()
            _emit_criteria_update(self.state)
        if self.write_report(result) is None:
            self._warn_no_report_artifact()
        # Human / standalone mode (no state file): the actionable diagnostic lives
        # in report_text, which is only persisted to report.json when --report-dir
        # is given. On failure that otherwise leaves a bare exit-1 with the real
        # reason trapped in a file that may not exist. Surface it on stderr so a
        # human — or an MCP wrapper that sees only stdout/stderr — gets the cause.
        # (Gate rejections take the _reject_with_gate path and already do this.)
        #
        # Skip the echo whenever the endpoint already printed the same text on
        # stdout. Callers commonly capture stdout/stderr separately and merge
        # them afterward; keying suppression on a shared OS sink duplicated the
        # complete verdict in that normal execution surface (Taxi F-32).
        human_mode = self._state is None or self._state._file_path is None
        witness = self._stdout_witness
        already_shown = witness is not None and witness.saw(result.report_text)
        if human_mode and result.report_text and not already_shown:
            failed = result.exit_code != EXIT_SUCCESS
            if failed:
                print(result.report_text, file=sys.stderr, flush=True)
            elif self.announce_success_report:
                # A passing run: put the verdict on stdout so it is not silent.
                print(result.report_text, flush=True)

    def _pre_save_hook(self, result: McpToolResult) -> None:
        """Hook for subclasses to mutate ``self.state`` (or *result*) just before
        ``state.save()`` runs.  Default no-op."""

    def cli(self) -> None:
        """Entry point for ``if __name__ == '__main__'`` usage."""
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        )
        sys.exit(self.main())
