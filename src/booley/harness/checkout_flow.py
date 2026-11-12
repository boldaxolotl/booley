"""Run one Booley Flow for one Target in an exported checkout (``booley measure``'s runner).

The checkout comes from :func:`booley.runtime.commit_export.export_commit`: a
Git-less directory that is the Project Root of the run. The Flow runs as a child
process whose ``BOOLEY_DIR`` is the caller's Main Booley Directory, so admission
and its settings, the Resource History and the Job record stay there, while
nothing shared is written into the checkout. Every input (configuration, cores,
tests) comes from the checkout's own Booley Directory, which the run refuses to
start without. The child gets the scrubbed
:func:`~booley.measurement.environment.measurement_environment`, a fresh ``HOME``
and ``TMPDIR`` beside the checkout, and its reports go to a directory the caller
names outside the checkout. Once the entry point returns, no Job record names a
path inside the checkout.

Before the Flow starts, two checks keep live inputs out of the run. A static scan
of the checkout refuses committed symlinks and absolute paths in ``.core`` files,
``fusesoc.conf`` and the Booley configuration that point into a live root (the
Sandbox's ``/work`` mount of the working tree, the Main Booley Directory and
``$BOOLEY_DIR``). A preflight child then resolves the Target the way the Flow will
(FuseSoC's ``--no-export`` mode, so each file keeps its original place), in the
measurement environment, and refuses a resolved file or core file inside a live
root; generators run there and see the scrubbed environment. The preflight runs
before the Job record and admission, so a refusal leaves no record, like the
resolution of ``booley targets <t>``; it costs one extra FuseSoC setup (generators
included) per run and is bounded by ``max(timeout, 60)`` seconds (the standalone
timeout without one), so the caller's bound caps the call at ``max(t, 60) + t``.
Outside ``ignore_native_cores`` Projects it prepares the Project's shared core view,
which projects stealth cores into the checkout exactly as the Flow's own resolution
does. It resolves the Target's own core: a Booley-generated overlay of a Flow is
derived from the same base core and names the same files.

The run's ``PATH`` also starts with a ``git`` that refuses every call (exit 128, one
line naming the command): the checkout has no repository, so a generator or Pre-Sim
Command that calls Git fails with a clear error instead of reading the live tree.
Git commands that need no repository fail too. An absolute ``/usr/bin/git`` is a
deliberate read and out of scope.

The caller owns the scratch location: a checkout under a directory named
``.booley`` (other than Main or ``$BOOLEY_DIR``) is rejected by the child's
discovery and ends as a failed run, not as an up-front refusal.

Limit: the child selects the checkout with the outer ``booley --project``, which
only endpoints on the shared CLI honour (built-in Flows and ``BooleyFlow`` Project
Flows); a Project Flow that overrides ``main`` is refused by the CLI with exit 2.

The child half (:func:`child_main`) runs with the checkout as its working
directory, so the checkout path is never part of the child's exec argv.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from booley.core.booley_layout import validate_environment
from booley.core.boundary import as_dict
from booley.flows.metric_declarations import FlowMetricValues
from booley.harness.standalone_job import (
    RUN_ID_ENV,
    STANDALONE_TIMEOUT_S,
    StandaloneJob,
    standalone_jobs_dir,
    standalone_log_path,
)
from booley.measurement.environment import measurement_environment, write_git_refusal
from booley.measurement.flow_metrics import report_metric_values
from booley.measurement.live_paths import LiveRoot, input_findings, resolved_findings
from booley.runtime import booley_dir, job_records, runtime_context, sandbox_layout
from booley.runtime.handles import register_job
from booley.runtime.process_group import (
    capture_process_group,
    force_group_termination,
    new_group_kwargs,
)
from booley.runtime.project_discovery import discover_project_root, has_git_worktree_marker
from booley.runtime.timefmt import compact_utc_now, utc_now_rfc3339
from booley.targets.flow_names import canonical

# The Flow's tools run in sessions of their own: SIGINT lets the Flow reap them
# before this grace period ends and the process group is killed.
_STOP_GRACE_S = 30
_START_FAILURE_EXIT_CODE = 2
_FLOW_NAME = re.compile(r"[A-Za-z0-9_-]+")
# The slot claim records this process's argv, so the bootstrap stays one short line.
_BOOTSTRAP = (
    "import sys;sys.path.insert(0,sys.argv.pop(1));"
    "from booley.harness.checkout_flow import child_main;raise SystemExit(child_main())"
)
_PREFLIGHT_BOOTSTRAP = (
    "import sys;sys.path.insert(0,sys.argv.pop(1));"
    "from booley.harness.checkout_flow import preflight_main;raise SystemExit(preflight_main())"
)
# The preflight's own bound when the request has a timeout: a short Flow budget must
# not be eaten by the interpreter start and a FuseSoC setup.
_PREFLIGHT_MIN_S = 60
_PREFLIGHT_LOG_LINES = 20
# Where ``import booley`` resolves from: located by argv, so ``PYTHONPATH`` stays out.
_BOOLEY_SOURCE_ROOT = Path(__file__).resolve().parents[2]


class CheckoutFlowError(ValueError):
    """The Flow was refused or could not be started; no Flow ran."""


class CheckoutFlowStatus(StrEnum):
    """How the Flow ended."""

    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class CheckoutFlowRequest:
    """One Flow run: *flow* for *target*, Project Root *checkout*, state in *main_booley_dir*."""

    checkout: Path
    main_booley_dir: Path
    report_dir: Path  # outside the checkout; the Flow's reports land under ``flow-reports/``
    flow: str
    target: str
    timeout_s: float | None = None  # wall clock of the run, admission queueing included


@dataclass(frozen=True)
class CheckoutFlowResult:
    """What the caller needs from the run."""

    run_id: str
    status: CheckoutFlowStatus
    exit_code: int
    reports: tuple[Path, ...]  # this run's ``report.json`` files, oldest first
    metrics: Mapping[str, FlowMetricValues]  # per Target; empty when the Flow declares none
    log: Path  # the Job's ``job.log`` in the Main Booley Directory


def _refuse_malformed_request(request: CheckoutFlowRequest) -> None:
    """Raise when the Sandbox, timeout, Flow name or Target name is unusable."""
    if not runtime_context.inside_session_runtime():
        raise CheckoutFlowError("a checkout Flow runs only inside the Sandbox")
    if request.timeout_s is not None and not 0 < request.timeout_s < math.inf:
        raise CheckoutFlowError(
            f"timeout must be a positive number of seconds: {request.timeout_s}"
        )
    if not _FLOW_NAME.fullmatch(request.flow):
        raise CheckoutFlowError(f"invalid Flow name: {request.flow!r}")
    if "\0" in request.target:
        raise CheckoutFlowError(f"invalid Target name: {request.target!r}")
    # One Flow for one Target: the Flow child would split a list and auto-select an
    # empty selector, resolving a Target the preflight never saw.
    if not request.target.strip() or "," in request.target:
        raise CheckoutFlowError(f"expected exactly one Target name: {request.target!r}")


def _refuse_unsafe(request: CheckoutFlowRequest) -> tuple[Path, Path, Path]:
    """Return the resolved checkout, Main Booley Directory and report directory.

    Raises before anything is created when the request is unsafe.
    """
    _refuse_malformed_request(request)
    checkout = request.checkout.resolve()
    if not checkout.is_dir():
        raise CheckoutFlowError(f"checkout is not a directory: {checkout}")
    # Explicit Project selection walks ancestors for ``.git``: a checkout at or below
    # any Git checkout (the live working tree above all) would capture the child.
    for candidate in (checkout, *checkout.parents):
        if has_git_worktree_marker(candidate):
            raise CheckoutFlowError(f"checkout {checkout} is inside the Git checkout {candidate}")
    main = request.main_booley_dir.resolve()
    state_dirs = [main]
    if os.environ.get("BOOLEY_DIR"):
        state_dirs.append(Path(os.environ["BOOLEY_DIR"]).resolve())
    for state_dir in state_dirs:
        # Identity too: the Sandbox mounts its Booley Directory under two spellings.
        if checkout.is_relative_to(state_dir) or booley_dir.contains(
            checkout, booley_dir=state_dir
        ):
            raise CheckoutFlowError(
                f"checkout {checkout} is inside the Booley Directory {state_dir}"
            )
    # The reverse too: shared state inside the checkout would vanish with it.
    if main.is_relative_to(checkout):
        raise CheckoutFlowError(f"Booley Directory {main} is inside the checkout {checkout}")
    report_dir = request.report_dir.resolve()
    if report_dir.is_relative_to(checkout):
        raise CheckoutFlowError(f"report directory {report_dir} is inside the checkout {checkout}")
    _require_own_booley_dir(checkout, main)
    return checkout, main, report_dir


def _require_own_booley_dir(checkout: Path, main: Path) -> None:
    """Refuse a checkout whose Booley configuration would not come from inside it.

    The child reads every input from the checkout's own Booley Directory. A commit
    without one would silently fall back to Main's (``BOOLEY_DIR``), and a
    ``[project].dir`` in an ancestor ``booley.toml`` would select a directory
    outside the checkout (or inside it, steered from outside). Either would make
    the run measure something other than the commit.
    """
    try:
        validate_environment()
    except (OSError, RuntimeError, ValueError) as exc:  # the runner's own environment
        raise CheckoutFlowError(f"the runner's environment is unusable: {exc}") from exc
    try:
        selected = booley_dir.resolve_authoritative_booley_dir(checkout)
        own = booley_dir.own_booley_dir(checkout)
    except (OSError, RuntimeError, ValueError) as exc:  # includes BooleyLayoutError
        raise CheckoutFlowError(
            f"checkout {checkout} has an unusable Booley configuration: {exc}"
        ) from exc
    if not selected.is_dir():
        # No Booley Directory in the commit: the child would fall back to Main's.
        raise CheckoutFlowError(
            f"checkout {checkout} has no Booley Directory of its own, so it would read "
            f"its Booley configuration from {main}; commit one inside the checkout"
        )
    selected = selected.resolve()
    if selected == checkout:
        raise CheckoutFlowError(
            f"checkout {checkout} selects its own root as its Booley Directory; "
            "use a directory inside the checkout"
        )
    # Only the checkout's own ``booley.toml`` may choose the directory: a selection
    # from an ancestor, even one landing inside the checkout, is steered from outside.
    if not selected.is_relative_to(checkout) or selected != own.resolve():
        raise CheckoutFlowError(
            f"checkout {checkout} would read its Booley configuration from {selected}, "
            "which its own booley.toml does not select inside it; commit a Booley "
            "Directory inside the checkout, remove any [project].dir above it and fix a "
            "[project].dir of its own that names a missing directory"
        )


def _live_roots(main: Path) -> list[LiveRoot]:
    """The directories that hold uncommitted work, most specific first.

    The runner runs only in the Sandbox, where the live working tree is the ``/work``
    mount and the Booley Directory a separate mount, so the live tree is not derivable
    from *main*; other working trees are not mounted there. A hand-written Sandbox has no
    ``/work``: its live tree is the Git checkout around the caller's working directory.
    """
    roots = [LiveRoot("Main Booley Directory", main)]
    if os.environ.get("BOOLEY_DIR"):
        roots.append(LiveRoot("BOOLEY_DIR", Path(os.environ["BOOLEY_DIR"]).resolve()))
    work_dir = Path(sandbox_layout.WORK_DIR)
    if not work_dir.is_dir():
        here = Path.cwd().resolve()
        work_dir = next((d for d in (here, *here.parents) if has_git_worktree_marker(d)), work_dir)
    roots.append(LiveRoot("live working tree", work_dir))
    return roots


def _refuse_live_inputs(checkout: Path, roots: list[LiveRoot]) -> None:
    """Raise when the checkout sits in a live root or one of its inputs reads from one."""
    for root in roots:
        # ``_refuse_unsafe`` walks ``.git`` markers, which an empty mounted one escapes.
        if booley_dir.contains(checkout, booley_dir=root.path) is not None:
            raise CheckoutFlowError(f"checkout {checkout} is inside the {root.label} {root.path}")
    findings = input_findings(checkout, roots)
    if findings:
        raise CheckoutFlowError("checkout run refused:\n" + "\n".join(findings))


def _prepare_scratch(checkout: Path) -> Path:
    """Create the run's directory beside the checkout: ``home/``, ``tmp/`` and ``bin/git``."""
    scratch = Path(tempfile.mkdtemp(dir=checkout.parent, prefix=f".{checkout.name}.env-"))
    try:
        for name in ("home", "tmp", "bin"):
            (scratch / name).mkdir()
        write_git_refusal(scratch / "bin")
    except BaseException:  # includes a Ctrl-C: the directory must not outlive the run
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return scratch


def _launch(
    request: CheckoutFlowRequest,
    checkout: Path,
    report_dir: Path,
    main: Path,
    run_id: str,
    log_path: Path,
    environment: dict[str, str],
) -> subprocess.Popen[Any]:
    """Start the Flow child in the checkout with the run's *environment*."""
    command = [
        sys.executable,
        "-c",
        _BOOTSTRAP,
        str(_BOOLEY_SOURCE_ROOT),
        str(main),
        run_id,
        request.flow,
        "--target",
        request.target,
        "--report-dir",
        str(report_dir / "flow-reports"),
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        return subprocess.Popen(
            command,
            cwd=checkout,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            **new_group_kwargs(),
        )


def _run_preflight_child(
    command: list[str],
    checkout: Path,
    environment: dict[str, str],
    bound_s: float,
    log_path: Path,
    target: str,
) -> None:
    """Run the preflight child to completion; raise when it cannot start, hangs or fails."""
    try:
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                command,
                cwd=checkout,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **new_group_kwargs(),
            )
    except OSError as exc:
        raise CheckoutFlowError(
            f"the preflight of Target {target} could not start: {exc}"
        ) from exc
    try:
        process.wait(timeout=bound_s)
    except subprocess.TimeoutExpired as exc:
        _kill(process)
        raise CheckoutFlowError(
            f"checkout run refused: the preflight did not finish within {bound_s:g} s"
        ) from exc
    except BaseException:  # a caller's Ctrl-C: never leave the preflight running
        _kill(process)
        raise
    if process.returncode != 0:
        tail = "\n".join(
            log_path.read_text(encoding="utf-8", errors="replace").splitlines()[
                -_PREFLIGHT_LOG_LINES:
            ]
        )
        raise CheckoutFlowError(
            f"checkout run refused: the preflight could not resolve Target {target}:\n{tail}"
        )


def _preflight(
    request: CheckoutFlowRequest,
    checkout: Path,
    roots: list[LiveRoot],
    scratch: Path,
    environment: dict[str, str],
) -> None:
    """Resolve the Target in the measurement environment; raise when it reads a live root."""
    target = request.target.strip()
    result_path, log_path = scratch / "preflight.json", scratch / "preflight.log"
    command = [
        sys.executable,
        "-c",
        _PREFLIGHT_BOOTSTRAP,
        str(_BOOLEY_SOURCE_ROOT),
        target,
        str(scratch / "tmp" / "preflight-build"),
        str(result_path),
    ]
    if request.timeout_s is None:
        bound_s: float = STANDALONE_TIMEOUT_S
    else:
        bound_s = max(request.timeout_s, _PREFLIGHT_MIN_S)
    _run_preflight_child(command, checkout, environment, bound_s, log_path, target)
    document = as_dict(json.loads(result_path.read_bytes())) or {}
    files = document.get("files")
    if isinstance(files, list):
        findings = resolved_findings(
            target,
            [str(entry) for entry in files],
            [str(entry) for entry in document.get("cores") or ()],
            checkout,
            roots,
        )
        if findings:
            raise CheckoutFlowError("checkout run refused:\n" + "\n".join(findings))


def _kill(process: subprocess.Popen[Any]) -> None:
    """End the child's whole process group and reap it."""
    force_group_termination(process, capture_process_group(process))
    process.wait()


def preflight_main(argv: list[str] | None = None) -> int:
    """The preflight child: resolve the Target in the cwd and write the paths it reads.

    *argv* is ``<target> <build root> <result file>``. The result is JSON: ``files`` is
    null when no FuseSoC Target has that name (a Project Flow takes none), else the
    resolved file paths, and ``cores`` the core files that contributed, both lexical
    and absolute.
    """
    import yaml

    from booley.fusesoc import fusesoc_registry
    from booley.targets.catalog import TargetCatalog
    from booley.targets.domain import UnknownTargetError

    target, build_dir, result_file = sys.argv[1:] if argv is None else argv
    root = discover_project_root(Path.cwd(), explicit_selection=True) or Path.cwd()
    result: dict[str, object] = {"files": None, "cores": []}
    try:
        handle = TargetCatalog.build(root).select(target)
    except UnknownTargetError:
        pass
    except fusesoc_registry.FuseSocError as exc:
        print(exc, file=sys.stderr)
        return _START_FAILURE_EXIT_CODE
    else:
        try:
            resolved = fusesoc_registry.resolve_target_handle(
                handle,
                build_root=build_dir,
                library_plan=fusesoc_registry.build_library_plan(root, build_dir),
                export=False,
            )
        except fusesoc_registry.FuseSocError as exc:
            print(exc, file=sys.stderr)
            return _START_FAILURE_EXIT_CODE
        edam = as_dict(yaml.safe_load(resolved.edam_path.read_text(encoding="utf-8"))) or {}
        cores = as_dict(edam.get("cores")) or {}
        result = {
            "files": [os.path.normpath(resolved.build_root / f.name) for f in resolved.files],
            "cores": [
                os.path.normpath(resolved.build_root / str(entry["core_file"]))
                for entry in (as_dict(item) or {} for item in cores.values())
                if entry.get("core_file")
            ],
        }
    Path(result_file).write_text(json.dumps(result), encoding="utf-8")
    return 0


def _stop(process: subprocess.Popen[Any]) -> None:
    """Interrupt the child so the Flow reaps its tools; kill the group if it does not end."""
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=_STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        _kill(process)
    except BaseException:  # a second Ctrl-C: never leave the Flow running
        _kill(process)
        raise


def _wait(process: subprocess.Popen[Any], timeout_s: float | None) -> bool:
    """Wait for the child; return whether the budget expired. A caller's Ctrl-C stops it first."""
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _stop(process)
        return True
    except BaseException:
        _stop(process)
        raise
    return False


def _shell_status(returncode: int) -> int:
    """A shell's exit status for a process that died by signal."""
    return returncode if returncode >= 0 else 128 - returncode


def _read_reports(
    report_dir: Path, flow: str, run_id: str
) -> list[tuple[Path, dict[str, object]]]:
    """This run's numbered reports with their documents, oldest first; unreadable ones are skipped."""
    found: list[tuple[int, Path, dict[str, object]]] = []
    for path in (report_dir / "flow-reports" / flow).glob("*/report.json"):
        try:
            document = as_dict(json.loads(path.read_bytes()))
        except (OSError, ValueError):
            continue
        if (
            document is not None
            and document.get("run_id") == run_id
            and path.parent.name.isdigit()
        ):
            found.append((int(path.parent.name), path, document))
    return [(path, document) for _, path, document in sorted(found)]


def _settle_record(
    record: job_records.JobRecord, jobs_root: Path, returncode: int | None, *, timed_out: bool
) -> job_records.JobRecord:
    """Return the final record, failing one the child left running or that timed out."""
    try:
        final = job_records.read_record(record.run_id, jobs_root) or record
    except OSError:  # unreadable record: settle what this process knows
        final = record
    code = final.exit_code
    if code is None:
        code = _START_FAILURE_EXIT_CODE if returncode is None else _shell_status(returncode)
    # A child that finished ``done`` before the stop landed keeps its record.
    if final.status == job_records.STATUS_RUNNING or (
        timed_out and final.status != job_records.STATUS_DONE
    ):
        final.status = job_records.STATUS_FAILED
        final.exit_code = code
        final.ended_at = final.ended_at or utc_now_rfc3339()
        job_records.write_record(final, jobs_root)
    return final


def _result_from(
    request: CheckoutFlowRequest,
    run_id: str,
    final: job_records.JobRecord,
    log_path: Path,
    report_dir: Path,
    *,
    timed_out: bool,
) -> CheckoutFlowResult:
    """The caller's view of a settled run: outcome, this run's reports and their typed metrics."""
    reports = _read_reports(report_dir, request.flow, run_id)
    if timed_out and final.status != job_records.STATUS_DONE:
        status = CheckoutFlowStatus.TIMED_OUT
    elif final.status == job_records.STATUS_CANCELLED:
        status = CheckoutFlowStatus.CANCELLED
    else:
        # The record decides: a child that exited 0 but left no ``done`` record failed.
        status = (
            CheckoutFlowStatus.PASSED
            if final.status == job_records.STATUS_DONE
            else CheckoutFlowStatus.FAILED
        )
    detail = as_dict(reports[-1][1].get("detail")) if reports else None
    return CheckoutFlowResult(
        run_id=run_id,
        status=status,
        exit_code=final.exit_code if final.exit_code is not None else _START_FAILURE_EXIT_CODE,
        reports=tuple(path for path, _ in reports),
        metrics={} if detail is None else report_metric_values(request.flow, detail),
        log=log_path,
    )


def _execute(
    request: CheckoutFlowRequest,
    checkout: Path,
    main: Path,
    report_dir: Path,
    roots: list[LiveRoot],
    scratch: Path,
) -> CheckoutFlowResult:
    """Preflight, record, start and settle the Flow; *scratch* is the caller's to remove."""
    environment = measurement_environment(
        os.environ,
        main_booley_dir=main,
        home=scratch / "home",
        tmp=scratch / "tmp",
        git_refusal=scratch / "bin",
    )
    _preflight(request, checkout, roots, scratch, environment)
    run_id = job_records.make_run_id(request.flow, compact_utc_now(), 1)
    log_path = standalone_log_path(main, run_id)
    jobs_root = standalone_jobs_dir(main)
    record = job_records.JobRecord(
        run_id=run_id,
        endpoint=request.flow,
        started_at=utc_now_rfc3339(),
        timeout_s=STANDALONE_TIMEOUT_S
        if request.timeout_s is None
        else math.ceil(request.timeout_s),
        argv=["booley", "flow", request.flow, "--target", request.target],
        report_root=str(report_dir),
    )
    try:
        job_records.write_record_strict(record, jobs_root)
        register_job(main, run_id, request.flow, None)
        process = _launch(request, checkout, report_dir, main, run_id, log_path, environment)
    except OSError as exc:
        # Best effort: an unwritable log must not stop the record turning failed.
        with contextlib.suppress(OSError), log_path.open("ab") as log:
            log.write(f"Checkout Flow not started: {exc}\n".encode())
        record.status = job_records.STATUS_FAILED
        record.exit_code = _START_FAILURE_EXIT_CODE
        record.ended_at = utc_now_rfc3339()
        job_records.write_record(record, jobs_root)
        raise CheckoutFlowError(f"Flow {request.flow!r} could not be started: {exc}") from exc
    timed_out = False
    try:
        timed_out = _wait(process, request.timeout_s)
    finally:
        final = _settle_record(record, jobs_root, process.returncode, timed_out=timed_out)
    return _result_from(request, run_id, final, log_path, report_dir, timed_out=timed_out)


def run_checkout_flow(request: CheckoutFlowRequest) -> CheckoutFlowResult:
    """Run the Flow in the checkout and return its outcome, reports and typed metrics.

    Raises:
        CheckoutFlowError: the request is refused, an input reads a live root, or the
            child cannot be started. A refusal leaves no Job record.
    """
    checkout, main, report_dir = _refuse_unsafe(request)
    request = replace(request, flow=canonical(request.flow))  # the CLI reports under this name
    roots = _live_roots(main)
    _refuse_live_inputs(checkout, roots)
    scratch = _prepare_scratch(checkout)
    try:
        return _execute(request, checkout, main, report_dir, roots, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run_flow_cli(flow: str, flow_argv: list[str]) -> int:
    """Run ``booley flow`` with the working directory as the explicitly selected Project Root."""
    from booley.harness import booley as booley_cli

    sys.argv = ["booley", "--project", str(Path.cwd()), "flow", flow, *flow_argv]
    return booley_cli.main()


def child_main(argv: list[str] | None = None) -> int:
    """The child half: adopt the Job record the parent wrote and run the Flow in the cwd.

    *argv* is ``<main booley dir> <run id> <flow> [flow arguments...]``.
    """
    main_dir, run_id, flow, *flow_argv = sys.argv[1:] if argv is None else argv
    job = StandaloneJob.adopt(Path(main_dir), run_id)
    if job is None:
        print(f"ERROR: Checkout Flow record not found: {run_id}", file=sys.stderr)
        return _START_FAILURE_EXIT_CODE
    # Its own run id: the CLI then sees a nested run and writes no second record.
    os.environ[RUN_ID_ENV] = run_id
    # fd 1 is the block-buffered ``job.log``: a live log would stay empty.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True)
    return job.run(lambda: _run_flow_cli(flow, flow_argv))
