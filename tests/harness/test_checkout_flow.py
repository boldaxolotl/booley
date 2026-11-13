"""A Booley Flow runs in an exported checkout with shared state in the Main Booley Directory.

Each run starts a real child process. The live Project is a Git repository whose
Main Booley Directory is its own ``.booley``; the checkout is an export of its
commit, so it holds no ``.git`` and no shared state.
"""

from __future__ import annotations

import _thread
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from booley.config.jobs import SlotCaps
from booley.harness.dashboard.jobs import JobReader
from booley.harness.standalone_job import standalone_jobs_dir, standalone_log_path
from booley.runtime import booley_dir, job_records, job_slots
from booley.runtime.commit_export import export_commit
from booley.runtime.job_records import _proc_cmdline
from booley.runtime.resource_history import load_records
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S, run_bounded_child

if TYPE_CHECKING:
    from booley.harness.checkout_flow import CheckoutFlowRequest, CheckoutFlowResult

# The runner is imported inside each test, so its absence fails that test, not collection.
linux_only = pytest.mark.skipif(
    sys.platform != "linux",
    reason="checkout Flows are observed through /proc and POSIX sessions",
)

# Generous bound for a child process or a marker file; only a hang ever reaches it.
TIMEOUT_S = 30
FLOW = "checkout_stub"

# The stub Flow's behavior follows ``--target``: ``pass`` runs one real command,
# ``fail`` returns a failing outcome, ``crash`` raises, ``abort`` exits at once with no
# report, ``wait`` waits (bounded) for the ``release``
# marker and ``env`` records what it saw. Its control directory is written into the source.
STUB_FLOW = """
import json, os, signal, time
from pathlib import Path
from booley.flows.base import BooleyFlow
from booley.runtime.endpoint_execution import EndpointOutcome

CTL = Path({ctl!r})

class CheckoutStub(BooleyFlow):
    name = "checkout_stub"
    description = "Checkout Flow runner fixture"
    JOB_CLASS = "light"

    def _memory_admitted(self):
        return False  # counted light slot: the test queues behind a plain slot holder
    target_required = False

    def _add_args(self, parser):
        parser.add_argument("--coverage", action="store_true")

    def _run(self):
        seen = {{
            "env": dict(os.environ),
            "home_is_dir": os.path.isdir(os.environ["HOME"]),
            "tmp_is_dir": os.path.isdir(os.environ["TMPDIR"]),
            "work_dir": str(self.args.work_dir),
            "coverage": self.args.coverage,
        }}
        (CTL / "seen.json").write_text(json.dumps(seen), encoding="utf-8")
        (CTL / "started").write_text("1", encoding="utf-8")
        target = self.args.target
        if target == "fail":
            return EndpointOutcome(exit_code=1)
        if target == "crash":
            raise RuntimeError("stub crashed")
        if target == "abort":
            print("stub aborted", flush=True)
            os._exit(3)  # dies without writing a report or a terminal record
        if target == "metrics":
            return EndpointOutcome(detail={{"marker": 7}})
        if target == "presim":
            # Pre-Sim Commands of core Target ``probe``, then the run evidence's Git lookup.
            from booley.flows.baseline_worktree import git_full_sha
            from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
            from booley.targets.catalog import TargetCatalog

            work = Path(self.args.work_dir)
            handle = TargetCatalog.build(work).select("probe")
            evidence = run_pre_sim_commands(
                handle, test_names=("t",), build_root=work / "b", eda_tool="icarus", timeout_s=30
            )
            (CTL / "presim.json").write_text(
                json.dumps(
                    {{
                        "status": evidence.status,
                        "returncode": evidence.returncode,
                        "stderr": evidence.stderr_tail,
                        "revision": git_full_sha("HEAD", work),
                    }}
                ),
                encoding="utf-8",
            )
            return EndpointOutcome()
        if target == "stubborn":
            signal.signal(signal.SIGINT, signal.SIG_IGN)  # only a kill ends this Flow
            (CTL / "ignoring").write_text("1", encoding="utf-8")
        if target in ("wait", "stubborn"):
            deadline = time.monotonic() + 45
            while not (CTL / "release").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
        else:
            self._execute(["true"])
        return EndpointOutcome()
"""

# What a caller's shell may carry; none of it may reach the child except as the allow-list says.
CALLER_ENV = {
    "PYTHONPATH": "/caller/pythonpath",
    "LD_LIBRARY_PATH": "/caller/lib",
    "LD_PRELOAD": "/caller/preload.so",
    "GIT_DIR": "/caller/.git",
    "RTL_PROJECT_ROOT": "/caller/project",
    "BOOLEY_GOAL_RUN_BINDING": '{"goal": "g1"}',
    "BOOLEY_GOAL_FILE": "/caller/goal.json",
    "BOOLEY_EXECUTION_ID": "session-execution",
    "BOOLEY_RUNTIME_EXECUTION_ID": "session-runtime-execution",
    "BOOLEY_STATE_FILE": "/caller/state.json",
    "BOOLEY_INHERITED_SLOT": "caller-slot",
    "BOOLEY_SLOTS_DIR": "/caller/slots",
    "BOOLEY_RUN_ID": "outer-run",
    "BOOLEY_INTERNAL_SELFTEST_KIND": "bad",  # Doctor's fail-path fixture must not reach a run
    "CALLER_ONLY_VARIABLE": "x",
}
KEPT_ENV = {
    "HTTP_PROXY": "http://proxy.invalid:3128",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",  # the ``LC_`` prefix
    "BOOLEY_FLEXNET_SERVER": "27000@license.invalid",  # the ``BOOLEY_FLEXNET_`` prefix
}


@dataclass(frozen=True)
class World:
    """The live Project, its checkout of HEAD and the caller's directories."""

    live: Path
    main: Path
    checkout: Path
    report_dir: Path
    ctl: Path

    def request(self, target: str, **changes: object) -> CheckoutFlowRequest:
        from booley.harness.checkout_flow import CheckoutFlowRequest

        request = CheckoutFlowRequest(
            checkout=self.checkout,
            main_booley_dir=self.main,
            report_dir=self.report_dir,
            flow=FLOW,
            target=target,
            timeout_s=TIMEOUT_S,
        )
        return replace(request, **changes)

    def seen(self) -> dict[str, object]:
        return json.loads((self.ctl / "seen.json").read_text(encoding="utf-8"))


def git(cwd: Path, *args: str) -> str:
    """Run Git with a fixed identity and no automatic maintenance; return stripped stdout."""
    command = [
        "git",
        "-c",
        "user.name=Checkout Test",
        "-c",
        "user.email=checkout@test.invalid",
        "-c",
        "maintenance.auto=false",
        "-c",
        "gc.auto=0",
        *args,
    ]
    return run_bounded_child(command, cwd=cwd, check=True).stdout.strip()


WorldBuilder = Callable[..., World]


@pytest.fixture
def make_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[WorldBuilder]:
    """Build a committed Project whose uncommitted settings differ from its checkout's.

    ``files`` and ``symlinks`` (relative path to text or link target; ``{live}`` and
    ``{ctl}`` name the live tree and the control directory) are committed too, and
    ``config`` is appended to the committed and to Main's ``booley.toml``.
    """
    from booley.runtime import runtime_context, sandbox_layout

    built: list[World] = []
    # The tests' verdict must not depend on the mounts of the host that runs them.
    monkeypatch.setattr(
        runtime_context,
        "runtime_location",
        lambda: runtime_context.RuntimeLocation.HAND_WRITTEN_SANDBOX,
    )

    def build(
        files: Mapping[str, str] | None = None,
        symlinks: Mapping[str, str] | None = None,
        config: str = "",
    ) -> World:
        # PYTHONPATH stays: the suite sets it so that child interpreters import this checkout.
        for name in ("BOOLEY_DIR", *CALLER_ENV.keys() - {"PYTHONPATH"}):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("_BOOLEY_CLI_INVOCATION_ORIGIN", raising=False)
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        live = (tmp_path / "live").resolve()
        # The Sandbox mounts the live working tree at ``/work``; the host has no such path.
        monkeypatch.setattr(sandbox_layout, "WORK_DIR", str(live))
        ctl = tmp_path / "ctl"
        ctl.mkdir()
        tools = live / ".booley" / "mcp_tools"
        tools.mkdir(parents=True)
        settings = "[jobs]\nmax_light = SLOTS\n" + config.replace("{live}", str(live))
        (live / ".booley" / "booley.toml").write_bytes(settings.replace("SLOTS", "4").encode())
        (tools / f"{FLOW}.py").write_bytes(STUB_FLOW.format(ctl=str(ctl)).encode())
        for relative, text in (files or {}).items():
            path = live / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.replace("{live}", str(live)).replace("{ctl}", str(ctl)).encode())
        for relative, target in (symlinks or {}).items():
            (live / relative).parent.mkdir(parents=True, exist_ok=True)
            (live / relative).symlink_to(target.replace("{live}", str(live)))
        git(live, "init", "-q", "-b", "main")
        git(live, "add", "-A", "-f")
        git(live, "commit", "-q", "-m", "stub")
        commit = git(live, "rev-parse", "HEAD")
        # Main's settings now differ from the checkout's committed ones.
        (live / ".booley" / "booley.toml").write_bytes(settings.replace("SLOTS", "1").encode())
        checkout = tmp_path / "scratch" / "checkout"
        export_commit(live, commit, checkout)
        booley_dir.reset_cache()
        world = World(live, live / ".booley", checkout, tmp_path / "reports", ctl)
        built.append(world)
        return world

    yield build
    booley_dir.reset_cache()
    # A failed assertion must not leave a Flow running into other tests.
    for world in built:
        for record in job_records.list_records(
            standalone_jobs_dir(world.main), skip_malformed=True
        ):
            if record.pid is not None and record.status == job_records.STATUS_RUNNING:
                with contextlib.suppress(OSError):
                    os.kill(record.pid, signal.SIGKILL)


@pytest.fixture
def world(make_world: WorldBuilder) -> World:
    """The plain committed Project: the stub Flow and settings only."""
    return make_world()


def poll(condition: Callable[[], object], what: str) -> None:
    """Wait for *condition* against a deadline, never a fixed sleep."""
    deadline = time.monotonic() + TIMEOUT_S
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


def records_in(main: Path) -> list[job_records.JobRecord]:
    return job_records.list_records(standalone_jobs_dir(main), skip_malformed=True)


def record_of(world: World, result: CheckoutFlowResult) -> job_records.JobRecord:
    record = job_records.read_record(result.run_id, standalone_jobs_dir(world.main))
    assert record is not None
    return record


@linux_only
@pytest.mark.parametrize(
    ("target", "status", "exit_code"),
    [
        pytest.param("pass", "passed", 0, id="pass"),
        pytest.param("fail", "failed", 1, id="fail"),
    ],
)
def test_checkout_flow_returns_outcome_and_reports(
    world: World, target: str, status: str, exit_code: int
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow

    result = run_checkout_flow(world.request(target))

    assert (result.status, result.exit_code) == (CheckoutFlowStatus(status), exit_code)
    assert result.metrics == {}  # the stub declares no metrics
    (report,) = result.reports
    assert report == world.report_dir.resolve() / "flow-reports" / FLOW / "1" / "report.json"
    document = json.loads(report.read_text(encoding="utf-8"))
    assert (document["run_id"], document["exit_code"]) == (result.run_id, exit_code)
    assert result.log.is_file()
    record = record_of(world, result)
    assert (record.status, record.exit_code) == (
        ("done" if exit_code == 0 else "failed"),
        exit_code,
    )
    assert record.report_root == str(world.report_dir.resolve())
    assert len(records_in(world.main)) == 1  # the CLI wrote no second record


@linux_only
def test_checkout_flow_returns_typed_metrics_of_its_report(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.flows.metric_declarations import FlowMetricValues
    from booley.harness.checkout_flow import run_checkout_flow
    from booley.measurement import flow_metrics

    def reader(detail: dict[str, object]) -> dict[str, FlowMetricValues]:
        return {
            "top": FlowMetricValues(plain={"cell_count": int(str(detail["marker"]))}, per_clock={})
        }

    monkeypatch.setitem(flow_metrics._READERS_BY_FLOW, FLOW, reader)

    result = run_checkout_flow(world.request("metrics"))

    assert result.metrics == {"top": FlowMetricValues(plain={"cell_count": 7}, per_clock={})}


@linux_only
def test_coverage_request_runs_the_flow_with_coverage_collection(world: World) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow

    plain = run_checkout_flow(world.request("pass"))
    assert plain.status is CheckoutFlowStatus.PASSED
    assert world.seen()["coverage"] is False
    assert record_of(world, plain).argv == ["booley", "flow", FLOW, "--target", "pass"]

    result = run_checkout_flow(world.request("pass", coverage=True))

    assert result.status is CheckoutFlowStatus.PASSED
    assert world.seen()["coverage"] is True
    assert record_of(world, result).argv[-1] == "--coverage"


@linux_only
def test_failed_crashed_or_timed_out_flow_returns_an_excerpt_that_errors_every_verdict(
    world: World,
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow
    from booley.measurement.metrics_file import Bound, TableRules
    from booley.measurement.run_document import Verdict
    from booley.measurement.verdicts import (
        FlowFailed,
        evaluate_table,
        measure_exit_code,
    )

    rules = TableRules(
        "t", "vlnv:x#t", "lint", (Bound("errors_max", "errors", "max", 0, None, None, None),), ()
    )
    cases = [
        ("crash", {}, CheckoutFlowStatus.FAILED, "RuntimeError: stub crashed"),
        ("abort", {}, CheckoutFlowStatus.FAILED, "stub aborted"),
        ("wait", {"timeout_s": 2}, CheckoutFlowStatus.TIMED_OUT, "the run timed out after 2 s"),
    ]
    for target, changes, status, expected in cases:
        result = run_checkout_flow(world.request(target, **changes))

        assert result.status is status, target
        assert result.metrics == {}, target
        assert result.excerpt is not None and expected in result.excerpt, target
        if status is CheckoutFlowStatus.TIMED_OUT:
            assert result.excerpt.startswith(expected)
        verdicts = evaluate_table(rules, FlowFailed(result.excerpt))
        assert [(v.verdict, v.detail) for v in verdicts] == [(Verdict.ERROR, result.excerpt)]
        assert measure_exit_code(v.verdict for v in verdicts) == 2


def refusal_requests(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, CheckoutFlowRequest]:
    """One request per refusal rule; each names the path it is refused for."""
    from booley.runtime import runtime_context, sandbox_layout

    plain_main, env_dir = tmp_path / "main-bd", tmp_path / "env-bd"
    live_checkout = world.live / "exported"
    sandbox_work = tmp_path / "sandbox-work"

    def exported(destination: Path) -> Path:
        """A real export: an empty directory fails the own-Booley-Directory rule first."""
        shutil.copytree(world.checkout, destination, symlinks=True)
        return destination

    # The Sandbox's ``/work`` mount: a live tree whose ``.git`` marker the walk cannot see.
    monkeypatch.setattr(sandbox_layout, "WORK_DIR", str(sandbox_work))
    # Only the issued Sandbox trusts ``/work`` without a ``.git`` marker.
    monkeypatch.setattr(
        runtime_context,
        "runtime_location",
        lambda: runtime_context.RuntimeLocation.ISSUED_SANDBOX,
    )
    monkeypatch.setenv("BOOLEY_DIR", str(env_dir))
    return {
        "work-dir": world.request("pass", checkout=exported(sandbox_work / "co")),
        "live-tree": world.request("pass", checkout=live_checkout),
        "main-booley-dir": world.request(
            "pass", checkout=plain_main / "co", main_booley_dir=plain_main
        ),
        "booley-dir-env": world.request("pass", checkout=env_dir / "co"),
        "report-in-checkout": world.request("pass", report_dir=world.checkout / "reports"),
        "main-in-checkout": world.request("pass", main_booley_dir=world.checkout / ".booley"),
    }


@linux_only
@pytest.mark.parametrize(
    "case",
    [
        "work-dir",
        "live-tree",
        "main-booley-dir",
        "booley-dir-env",
        "report-in-checkout",
        "main-in-checkout",
    ],
)
def test_checkout_flow_refuses_live_state_paths(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    request = refusal_requests(world, tmp_path, monkeypatch)[case]
    request.checkout.mkdir(parents=True, exist_ok=True)
    offending = {
        "report-in-checkout": request.report_dir,
        "main-in-checkout": request.main_booley_dir,
    }.get(case, request.checkout)

    with pytest.raises(CheckoutFlowError) as refused:
        run_checkout_flow(request)

    assert str(offending.resolve()) in str(refused.value)
    if case == "work-dir":
        # The ``/work`` rule refused it, not the rule for a checkout without its own directory.
        assert "is inside the live working tree" in str(refused.value)
    # Refused before anything was created: no Job record, no environment directory.
    assert not standalone_jobs_dir(request.main_booley_dir).exists()
    assert not list(request.checkout.parent.glob(".*env-*"))
    assert not (world.ctl / "started").exists()


@linux_only
@pytest.mark.parametrize(
    "case",
    [
        "no-own-dir",
        "ancestor-selects",
        "ancestor-selects-inside",
        "own-root",
        "own-selects-outside",
    ],
)
def test_checkout_flow_refuses_booley_config_outside_the_checkout(
    world: World, tmp_path: Path, case: str
) -> None:
    """A checkout whose Booley configuration is not inside it is refused, naming the directory."""
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    if case == "no-own-dir":
        # No Booley Directory in the commit: the child would fall back to Main's.
        checkout = tmp_path / "bare" / "checkout"
        checkout.mkdir(parents=True)
        (checkout / "dut.sv").write_bytes(b"module dut; endmodule\n")
        offending = world.main
    elif case == "ancestor-selects":
        # An ancestor's ``[project].dir`` selects a directory outside the checkout.
        offending = tmp_path / "selected"
        offending.mkdir()
        checkout = world.checkout
        (checkout.parent / "booley.toml").write_bytes(
            f'[project]\ndir = "{offending.as_posix()}"\n'.encode()
        )
    elif case == "ancestor-selects-inside":
        # Even a directory inside the checkout is refused when an ancestor chose it.
        checkout = world.checkout
        offending = checkout / "steered"
        offending.mkdir()
        (checkout.parent / "booley.toml").write_bytes(
            f'[project]\ndir = "{checkout.name}/steered"\n'.encode()
        )
    elif case == "own-root":
        # The checkout's own ``booley.toml`` selects the checkout root itself.
        checkout = world.checkout
        offending = checkout
        (checkout / "booley.toml").write_bytes(b'[project]\ndir = "."\n')
    else:
        # The checkout's own ``booley.toml`` selects a directory above it.
        checkout = world.checkout
        offending = checkout.parent / "selected"
        offending.mkdir()
        (checkout / "booley.toml").write_bytes(b'[project]\ndir = "../selected"\n')
    request = world.request("pass", checkout=checkout)

    with pytest.raises(CheckoutFlowError) as refused:
        run_checkout_flow(request)

    assert str(offending.resolve()) in str(refused.value)
    if case == "own-root":
        assert "selects its own root" in str(refused.value)
    # Refused before anything was created: no Job record, no environment directory.
    assert not standalone_jobs_dir(world.main).exists()
    assert not list(checkout.parent.glob(".*env-*"))
    assert not (world.ctl / "started").exists()


@linux_only
@pytest.mark.parametrize(
    "case",
    [
        "missing-checkout",
        "bad-flow-name",
        "bad-target",
        "empty-target",
        "several-targets",
        "nan-timeout",
    ],
)
def test_checkout_flow_refuses_a_malformed_request(world: World, case: str) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    request = {
        "missing-checkout": world.request("pass", checkout=world.checkout / "gone"),
        "bad-flow-name": world.request("pass", flow="../escape"),
        "bad-target": world.request("bad\0target"),
        "empty-target": world.request("  "),
        "several-targets": world.request("a,b"),
        "nan-timeout": world.request("pass", timeout_s=float("nan")),
    }[case]

    with pytest.raises(
        CheckoutFlowError,
        match=r"not a directory|invalid Flow name|invalid Target|exactly one Target|positive",
    ):
        run_checkout_flow(request)

    assert not standalone_jobs_dir(world.main).exists()


@linux_only
def test_checkout_flow_refuses_outside_the_sandbox(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    monkeypatch.setattr(checkout_flow.runtime_context, "inside_session_runtime", lambda: False)

    with pytest.raises(CheckoutFlowError, match="only inside the Sandbox"):
        run_checkout_flow(world.request("pass"))

    assert not standalone_jobs_dir(world.main).exists()


@linux_only
def test_checkout_flow_reports_an_unwritable_record_as_a_refusal(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    def unwritable(*_args: object, **_kwargs: object) -> None:
        raise OSError(30, "read-only file system")

    monkeypatch.setattr(checkout_flow.job_records, "write_record_strict", unwritable)

    with pytest.raises(CheckoutFlowError, match="read-only file system"):
        run_checkout_flow(world.request("pass"))

    assert not (world.ctl / "started").exists()


@linux_only
def test_checkout_flow_resolves_a_relative_main_booley_dir(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow

    monkeypatch.chdir(
        world.live
    )  # the child runs in the checkout, where ".booley" is another place

    result = run_checkout_flow(world.request("pass", main_booley_dir=Path(".booley")))

    assert result.status is CheckoutFlowStatus.PASSED
    assert len(records_in(world.main)) == 1


@linux_only
def test_checkout_flow_start_failure_leaves_a_failed_record(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    real_popen = subprocess.Popen

    def refuse_flow_child(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        if "child_main" in command[2]:  # the preflight child starts first and must run
            raise OSError(2, "no interpreter")
        return real_popen(command, **kwargs)

    monkeypatch.setattr(checkout_flow.subprocess, "Popen", refuse_flow_child)

    with pytest.raises(CheckoutFlowError, match="no interpreter"):
        run_checkout_flow(world.request("pass"))

    (record,) = records_in(world.main)
    assert (record.status, record.exit_code) == ("failed", 2)
    assert "no interpreter" in standalone_log_path(world.main, record.run_id).read_text(
        encoding="utf-8"
    )
    assert not list(world.checkout.parent.glob(".*env-*"))  # the environment was removed


@linux_only
def test_checkout_flow_queues_behind_live_flow_in_main_slot_store(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow

    # The caller's own slot and store must not be inherited by the child.
    monkeypatch.setenv("BOOLEY_INHERITED_SLOT", "caller-slot")
    monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(world.live / "elsewhere"))
    slots_root = world.main / "runtime" / "jobs" / "slots"
    store = job_slots.SlotStore(slots_root, SlotCaps(max_light=1))
    # A Flow of the session's own checkout holds Main's only light slot.
    holder = store.acquire(
        job_slots.CLASS_LIGHT, pid=os.getpid(), argv=_proc_cmdline(os.getpid()) or []
    )
    reader = JobReader(world.main, slots_root=slots_root)
    released = False
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            run = pool.submit(run_checkout_flow, world.request("pass"))

            def queued() -> bool:
                snapshot = reader.read([])
                return [view.state for view in snapshot.jobs] == ["queued"]

            poll(queued, "the checkout Flow to queue")
            assert not (world.ctl / "started").exists()
            store.release(holder)
            released = True
            result = run.result(timeout=TIMEOUT_S)
        finally:
            if not released:
                store.release(holder)

    assert result.status is CheckoutFlowStatus.PASSED
    assert (world.ctl / "started").exists()


@linux_only
def test_checkout_flow_keeps_shared_state_in_main(world: World) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow

    before = {path.relative_to(world.checkout) for path in world.checkout.rglob("*")}

    result = run_checkout_flow(world.request("pass"))

    assert result.status is CheckoutFlowStatus.PASSED
    # Main gained the Flow's Resource History record and its Job record.
    assert [row["flow"] for row in load_records(world.main)] == [FLOW]
    assert record_of(world, result).status == "done"
    # The checkout gained nothing: no shared state, no build cache, no bytecode.
    added = {path.relative_to(world.checkout) for path in world.checkout.rglob("*")} - before
    assert added == set()


@linux_only
def test_checkout_flow_environment_is_scrubbed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import run_checkout_flow

    for name, value in (CALLER_ENV | KEPT_ENV).items():
        monkeypatch.setenv(name, value)
    before = set(world.checkout.parent.iterdir())

    result = run_checkout_flow(world.request("pass"))

    seen = world.seen()
    env = seen["env"]
    assert isinstance(env, dict)
    assert not (CALLER_ENV.keys() - {"BOOLEY_RUN_ID"}) & env.keys()
    assert env["BOOLEY_RUN_ID"] == result.run_id  # its own run, not the caller's
    assert (env["PYTHONSAFEPATH"], env["PYTHONNOUSERSITE"]) == ("1", "1")
    assert env["BOOLEY_DIR"] == str(world.main)
    assert env["PYTHONPYCACHEPREFIX"].startswith(env["TMPDIR"])
    assert {name: env[name] for name in KEPT_ENV} == KEPT_ENV
    # HOME and TMPDIR existed during the run, in a directory that did not exist before it.
    assert seen["home_is_dir"] and seen["tmp_is_dir"]
    scratch = Path(env["HOME"]).parent
    assert scratch.parent == world.checkout.parent and scratch not in before
    assert not scratch.exists()
    assert set(world.checkout.parent.iterdir()) == before


def assert_no_record_names(main: Path, checkout: Path) -> None:
    """No Job record or handle under *main* names a path inside *checkout*."""
    needles = {str(checkout), str(checkout.resolve())}
    for path in main.rglob("*"):
        # Logs and the history are output, not records.
        if path.is_file() and path.name != "job.log":
            text = path.read_text(encoding="utf-8", errors="replace")
            assert not any(needle in text for needle in needles), path


@linux_only
@pytest.mark.parametrize("outcome", ["passed", "failed", "timed_out", "cancelled"])
def test_deleted_checkout_is_named_by_no_job_record(world: World, outcome: str) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow

    if outcome == "cancelled":
        # The Dashboard cancels a Job by signalling the pid its record names.
        with ThreadPoolExecutor(max_workers=1) as pool:
            run = pool.submit(run_checkout_flow, world.request("wait"))
            poll((world.ctl / "started").exists, "the Flow to start")
            (running,) = records_in(world.main)
            assert running.pid is not None
            os.kill(running.pid, signal.SIGTERM)
            result = run.result(timeout=TIMEOUT_S)
    else:
        target, timeout_s = {
            "passed": ("pass", TIMEOUT_S),
            "failed": ("fail", TIMEOUT_S),
            "timed_out": ("wait", 2),
        }[outcome]
        result = run_checkout_flow(world.request(target, timeout_s=timeout_s))

    assert result.status is CheckoutFlowStatus(outcome)
    record = record_of(world, result)
    assert record.status != job_records.STATUS_RUNNING and record.ended_at
    if outcome == "timed_out":
        assert record.status == job_records.STATUS_FAILED
    shutil.rmtree(world.checkout)

    assert_no_record_names(world.main, world.checkout)
    assert not (record.work_dir or "").startswith(str(world.checkout))
    # The reports are where the caller asked for them, and still readable.
    if outcome in {"passed", "failed"}:
        assert result.reports
    for report in result.reports:
        assert json.loads(report.read_text(encoding="utf-8"))["run_id"] == result.run_id


# Runs the entry point in its own process so the test can interrupt it like a Ctrl-C.
CALLER = """
import json, sys
from pathlib import Path
from booley.harness.checkout_flow import CheckoutFlowRequest, run_checkout_flow

checkout, main, reports = (Path(arg) for arg in sys.argv[1:4])
run_checkout_flow(CheckoutFlowRequest(checkout, main, reports, "checkout_stub", "wait", 60))
"""


@linux_only
def test_interrupted_caller_stops_the_flow(world: World) -> None:
    caller = subprocess.Popen(
        [
            sys.executable,
            "-c",
            CALLER,
            str(world.checkout),
            str(world.main),
            str(world.report_dir),
        ],
        env=os.environ | {"BOOLEY_CONTAINER": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        poll(lambda: (world.ctl / "started").exists() or caller.poll() is not None, "the Flow")
        assert caller.poll() is None, caller.communicate()[1]
        (running,) = records_in(world.main)
        assert running.pid is not None
        caller.send_signal(signal.SIGINT)
        caller.communicate(timeout=SUBPROCESS_TIMEOUT_S)
    finally:
        if caller.poll() is None:
            caller.kill()
            caller.communicate()

    assert caller.returncode != 0  # the Ctrl-C reached the caller
    poll(lambda: not Path(f"/proc/{running.pid}").exists(), "the Flow to be gone")
    (final,) = records_in(world.main)
    assert final.status not in {job_records.STATUS_RUNNING, job_records.STATUS_DONE}
    assert final.ended_at
    assert not list(world.checkout.parent.glob(".*env-*"))


@linux_only
def test_child_main_runs_the_adopted_record_with_the_checkout_selected(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import checkout_flow

    run_id = "checkout_stub-20261107T000000Z-1-abcd1234"
    jobs_root = standalone_jobs_dir(world.main)
    job_records.write_record_strict(
        job_records.JobRecord(run_id, FLOW, "2026-11-07T00:00:00Z", 60), jobs_root
    )
    monkeypatch.chdir(world.checkout)
    monkeypatch.setenv("BOOLEY_DIR", str(world.main))
    # Written into the process environment by the Flow's CLI; restored on teardown.
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("BOOLEY_RUN_ID", "outer-run")
    monkeypatch.setattr(sys, "argv", ["pytest"])
    booley_dir.reset_cache()

    code = checkout_flow.child_main(
        [str(world.main), run_id, FLOW, "--target", "pass", "--report-dir", str(world.report_dir)]
    )

    assert code == 0
    # The checkout, not an ancestor, was the Project Root; the CLI wrote no second record.
    assert world.seen()["work_dir"] == str(world.checkout.resolve())
    (record,) = records_in(world.main)
    assert (record.run_id, record.status, record.exit_code) == (run_id, "done", 0)


@linux_only
def test_child_main_without_a_record_reports_it(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.harness import checkout_flow

    assert checkout_flow.child_main([str(world.main), "missing-1", FLOW]) == 2
    assert "record not found: missing-1" in capsys.readouterr().err


@linux_only
def test_child_killed_before_its_terminal_write_fails_the_record(world: World) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow

    with ThreadPoolExecutor(max_workers=1) as pool:
        run = pool.submit(run_checkout_flow, world.request("wait"))
        poll((world.ctl / "started").exists, "the Flow to start")
        (running,) = records_in(world.main)
        assert running.pid is not None
        os.kill(running.pid, signal.SIGKILL)
        result = run.result(timeout=TIMEOUT_S)

    assert (result.status, result.exit_code) == (CheckoutFlowStatus.FAILED, 137)
    record = record_of(world, result)
    assert (record.status, record.exit_code) == ("failed", 137)
    assert not list(world.checkout.parent.glob(".*env-*"))


@linux_only
def test_flow_that_ignores_the_stop_is_killed_after_the_grace(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import run_checkout_flow

    monkeypatch.setattr(checkout_flow, "_STOP_GRACE_S", 1)

    def interrupt_once_ignoring() -> None:
        poll((world.ctl / "ignoring").exists, "the Flow to ignore SIGINT")
        _thread.interrupt_main()  # the caller's Ctrl-C

    with ThreadPoolExecutor(max_workers=1) as pool:
        watcher = pool.submit(interrupt_once_ignoring)
        with pytest.raises(KeyboardInterrupt):
            run_checkout_flow(world.request("stubborn"))
        watcher.result(timeout=TIMEOUT_S)

    (record,) = records_in(world.main)
    assert record.status == "failed" and record.pid is not None
    assert not Path(f"/proc/{record.pid}").exists()


class StubbornProcess:
    """A child that ends only when killed; the first wait is interrupted like a second Ctrl-C."""

    def __init__(self) -> None:
        self.signals: list[int] = []
        self.killed = False
        self.waits = 0

    def send_signal(self, signum: int) -> None:
        self.signals.append(signum)

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.waits == 1:
            raise KeyboardInterrupt
        return -9


def test_second_interrupt_during_the_stop_still_kills_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.harness import checkout_flow

    process = StubbornProcess()
    monkeypatch.setattr(checkout_flow, "capture_process_group", lambda _process: "group")
    monkeypatch.setattr(
        checkout_flow,
        "force_group_termination",
        lambda _process, group: setattr(process, "killed", group == "group"),
    )

    with pytest.raises(KeyboardInterrupt):
        checkout_flow._stop(process)  # type: ignore[arg-type]  # a fake with Popen's used surface

    assert process.signals == [signal.SIGINT]
    assert process.killed
    assert process.waits == 2  # the kill was reaped before the interrupt went on


def test_a_child_that_finished_before_the_stop_keeps_its_done_record(tmp_path: Path) -> None:
    from booley.harness import checkout_flow

    record = job_records.JobRecord("flow-1", FLOW, "2026-11-07T00:00:00Z", 60)
    record.status, record.exit_code, record.ended_at = "done", 0, "2026-11-07T00:00:01Z"
    job_records.write_record_strict(record, tmp_path)

    final = checkout_flow._settle_record(record, tmp_path, 0, timed_out=True)

    assert (final.status, final.exit_code) == ("done", 0)
    result = checkout_flow._result_from(
        checkout_flow.CheckoutFlowRequest(tmp_path, tmp_path, tmp_path, FLOW, "pass"),
        "flow-1",
        final,
        tmp_path / "log",
        tmp_path,
        timed_out=True,
    )
    assert (result.status, result.exit_code) == (checkout_flow.CheckoutFlowStatus.PASSED, 0)


@linux_only
def test_legacy_flow_spelling_is_reported_under_its_canonical_name(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow
    from booley.targets import flow_names

    monkeypatch.setitem(flow_names.LEGACY_TO_CANONICAL, "checkout_legacy", FLOW)

    result = run_checkout_flow(world.request("pass", flow="checkout_legacy"))

    assert result.status is CheckoutFlowStatus.PASSED
    (report,) = result.reports
    assert report.parent.parent.name == FLOW


@linux_only
def test_child_selects_the_checkout_explicitly(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import booley as booley_cli
    from booley.harness import checkout_flow

    seen: list[list[str]] = []

    def main() -> int:
        seen.append(list(sys.argv))
        return 0

    monkeypatch.chdir(world.checkout)
    monkeypatch.setattr(booley_cli, "main", main)

    assert checkout_flow._run_flow_cli(FLOW, ["--target", "pass"]) == 0

    # ``--project`` beats discovery, so no ancestor of the checkout can become the root.
    assert seen == [
        ["booley", "--project", str(world.checkout.resolve()), "flow", FLOW, "--target", "pass"]
    ]


def test_unreadable_record_settles_from_the_one_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import checkout_flow

    def unreadable(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(checkout_flow.job_records, "read_record", unreadable)
    record = job_records.JobRecord("flow-1", FLOW, "2026-11-07T00:00:00Z", 60)

    final = checkout_flow._settle_record(record, tmp_path, 3, timed_out=False)

    assert (final.status, final.exit_code) == ("failed", 3)


def test_child_that_exited_zero_without_a_done_record_is_reported_failed(tmp_path: Path) -> None:
    from booley.harness import checkout_flow

    # Exit 0 but the child's terminal record write was lost: the record still says running.
    record = job_records.JobRecord("flow-1", FLOW, "2026-11-07T00:00:00Z", 60)
    final = checkout_flow._settle_record(record, tmp_path, 0, timed_out=False)

    result = checkout_flow._result_from(
        checkout_flow.CheckoutFlowRequest(tmp_path, tmp_path, tmp_path, FLOW, "pass"),
        "flow-1",
        final,
        tmp_path / "log",
        tmp_path,
        timed_out=False,
    )

    assert final.status == "failed"
    assert result.status is checkout_flow.CheckoutFlowStatus.FAILED


@linux_only
def test_checkout_flow_refuses_an_unusable_booley_configuration(world: World) -> None:
    """A Booley Directory that is not a directory is refused as the checkout's problem."""
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    shutil.rmtree(world.checkout / ".booley")
    (world.checkout / ".booley").write_bytes(b"not a directory\n")

    with pytest.raises(CheckoutFlowError, match="unusable Booley configuration") as refused:
        run_checkout_flow(world.request("pass"))

    assert str(world.checkout.resolve()) in str(refused.value)


@linux_only
def test_checkout_flow_blames_the_runner_environment_for_a_bad_selection_variable(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retired selection variable in the runner's environment is not the checkout's fault."""
    from booley.core.booley_layout import RETIRED_ENV
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    monkeypatch.setenv(RETIRED_ENV, "")

    with pytest.raises(CheckoutFlowError, match="runner's environment is unusable"):
        run_checkout_flow(world.request("pass"))


# ---- Preflight: no Flow input may come from the live tree or the Main Booley Directory.

PROBE_CORE = """CAPI=2:
name: acme:ip:probe:0.1
filesets:
  rtl:
    files: [{files}]
    file_type: verilogSource
targets:
  probe:
    filesets: [rtl]
    toplevel: top
    default_tool: icarus
"""

# A generator that records what it saw and writes a minimal core, optionally waiting first.
GENERATOR_CORE = """CAPI=2:
name: acme:ip:gen:0.1
generators:
  record:
    interpreter: python3
    command: gen.py
filesets:
  rtl:
    files: [top.v]
    file_type: verilogSource
generate:
  made:
    generator: record
targets:
  probe:
    filesets: [rtl]
    generate: [made]
    toplevel: top
    default_tool: icarus
"""
GENERATOR = """import json, os, subprocess, time
from pathlib import Path

ctl = Path("{ctl}")
git = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
(ctl / "generator.json").write_text(
    json.dumps({{"env": dict(os.environ), "rc": git.returncode, "stderr": git.stderr.strip()}})
)
(ctl / "generator.pid").write_text(str(os.getpid()))
deadline = time.monotonic() + {wait}
while {wait} and not (ctl / "release").exists() and time.monotonic() < deadline:
    time.sleep(0.02)
Path("made.core").write_text("CAPI=2:\\nname: acme:ip:made:0.1\\n")
"""


def use_fusesoc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the tools environment first on ``PATH``: the allow-list keeps it for the child."""
    tools = str(Path(sys.executable).parent)
    monkeypatch.setenv("PATH", tools + os.pathsep + os.environ["PATH"])
    # A declared dependency of Booley: absent means a broken environment, not an optional tool.
    assert shutil.which("fusesoc"), "fusesoc is not installed next to the test interpreter"


def assert_refused_before_any_run(world: World) -> None:
    """A refusal leaves no Job record, no environment directory and no started Flow."""
    assert not standalone_jobs_dir(world.main).exists()
    assert not list(world.checkout.parent.glob(".*env-*"))
    assert not (world.ctl / "started").exists()


@linux_only
@pytest.mark.parametrize(
    "case", ["core", "booley-toml", "symlink", "core-symlink", "config-symlink"]
)
def test_checkout_flow_refuses_live_paths_in_inputs(make_world: WorldBuilder, case: str) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    inputs: dict[str, tuple[dict[str, Any], str, str]] = {
        "core": (
            {"files": {"ip.core": PROBE_CORE.format(files="{live}/rtl/top.v")}},
            "ip.core",
            "{live}/rtl/top.v",
        ),
        "booley-toml": (
            {"config": '[flows.sim]\npre_run_commands = ["cat {live}/data.txt"]\n'},
            ".booley/booley.toml",
            "{live}/data.txt",
        ),
        "symlink": (
            {"files": {"rtl/other.v": ""}, "symlinks": {"rtl/shared.v": "{live}/data/shared.v"}},
            "rtl/shared.v",
            "{live}/data/shared.v",
        ),
        # A link inside the checkout is read by the Flow, so its target is scanned too.
        "core-symlink": (
            {
                "files": {"cores/ip.txt": PROBE_CORE.format(files="{live}/rtl/top.v")},
                "symlinks": {"ip.core": "cores/ip.txt"},
            },
            "ip.core",
            "{live}/rtl/top.v",
        ),
        "config-symlink": (
            {
                "files": {"cfg/tests.toml": 'tests = "{live}/data.txt"\n'},
                "symlinks": {".booley/tests.toml": "../cfg/tests.toml"},
            },
            ".booley/tests.toml",
            "{live}/data.txt",
        ),
    }
    build_arguments, offending, named_path = inputs[case]
    world = make_world(**build_arguments)

    with pytest.raises(CheckoutFlowError) as refused:
        run_checkout_flow(world.request("pass"))

    message = str(refused.value)
    assert offending in message
    assert named_path.replace("{live}", str(world.live)) in message
    assert f"inside the live working tree {world.live}" in message
    assert_refused_before_any_run(world)


@linux_only
def test_checkout_flow_finds_the_live_tree_without_a_work_mount(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow
    from booley.runtime import sandbox_layout

    # Without a ``/work`` mount the live tree is the Git checkout around Main, whatever
    # directory the caller stands in.
    world = make_world(files={"ip.core": PROBE_CORE.format(files="{live}/rtl/top.v")})
    monkeypatch.setattr(sandbox_layout, "WORK_DIR", str(world.live / "missing"))
    monkeypatch.chdir(world.checkout.parent)

    with pytest.raises(CheckoutFlowError, match=r"ip\.core names .*live working tree"):
        run_checkout_flow(world.request("pass"))

    assert_refused_before_any_run(world)


@linux_only
def test_checkout_flow_derives_the_live_tree_from_the_working_directory(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dataclasses import replace

    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow
    from booley.runtime import sandbox_layout

    # A Booley Directory outside every Git checkout: only the caller's directory shows the tree.
    world = make_world(files={"ip.core": PROBE_CORE.format(files="{live}/rtl/top.v")})
    detached_main = tmp_path / "detached-main"
    detached_main.mkdir()
    monkeypatch.setattr(sandbox_layout, "WORK_DIR", str(world.live / "missing"))
    monkeypatch.chdir(world.live)

    with pytest.raises(CheckoutFlowError, match=r"ip\.core names .*live working tree"):
        run_checkout_flow(replace(world.request("pass"), main_booley_dir=detached_main))


@linux_only
def test_checkout_flow_does_not_trust_an_empty_work_directory_outside_the_issued_sandbox(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow
    from booley.runtime import sandbox_layout

    # The image's ``WORKDIR /work`` exists in a hand-written Sandbox without being the tree.
    world = make_world(files={"ip.core": PROBE_CORE.format(files="{live}/rtl/top.v")})
    empty_work = tmp_path / "empty-work"
    empty_work.mkdir()
    monkeypatch.setattr(sandbox_layout, "WORK_DIR", str(empty_work))

    with pytest.raises(CheckoutFlowError, match=r"ip\.core names .*live working tree"):
        run_checkout_flow(world.request("pass"))

    assert_refused_before_any_run(world)


@linux_only
def test_checkout_flow_refuses_a_path_into_the_booley_dir_variable(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    env_dir = tmp_path / "env-bd"
    env_dir.mkdir()
    world = make_world(files={"ip.core": PROBE_CORE.format(files=f"{env_dir}/rtl/top.v")})
    monkeypatch.setenv("BOOLEY_DIR", str(env_dir))

    with pytest.raises(CheckoutFlowError, match=rf"{env_dir}/rtl/top\.v, inside the BOOLEY_DIR"):
        run_checkout_flow(world.request("pass"))

    assert_refused_before_any_run(world)


@linux_only
def test_checkout_flow_takes_a_work_directory_with_a_git_marker_as_the_live_tree(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dataclasses import replace

    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    # Main is outside every checkout and the caller stands outside the tree: only a ``/work``
    # that is a Git checkout (``WORK_DIR`` is the live tree here) shows it.
    world = make_world(files={"ip.core": PROBE_CORE.format(files="{live}/rtl/top.v")})
    detached_main = tmp_path / "detached-main"
    detached_main.mkdir()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(CheckoutFlowError, match=r"ip\.core names .*live working tree"):
        run_checkout_flow(replace(world.request("pass"), main_booley_dir=detached_main))


@linux_only
def test_checkout_flow_ignores_a_path_under_an_empty_work_directory(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow
    from booley.runtime import sandbox_layout

    # The image's ``WORKDIR /work`` exists in a hand-written Sandbox without being the
    # tree: naming a path under it is not a read of uncommitted work.
    empty_work = tmp_path / "empty-work"
    empty_work.mkdir()
    world = make_world(files={"ip.core": PROBE_CORE.format(files=f"{empty_work}/rtl/top.v")})
    monkeypatch.setattr(sandbox_layout, "WORK_DIR", str(empty_work))

    assert run_checkout_flow(world.request("pass")).status is CheckoutFlowStatus.PASSED


@linux_only
def test_checkout_flow_skips_a_booley_directory_that_is_its_own_checkout(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow
    from booley.runtime import sandbox_layout

    # A linked Booley Directory with its own repository is not the live tree around it.
    world = make_world(files={"ip.core": PROBE_CORE.format(files="{live}/rtl/top.v")})
    git(world.main, "init", "-q")
    monkeypatch.setattr(sandbox_layout, "WORK_DIR", str(world.live / "missing"))
    monkeypatch.chdir(world.checkout.parent)

    with pytest.raises(CheckoutFlowError, match=r"ip\.core names .*live working tree"):
        run_checkout_flow(world.request("pass"))

    assert_refused_before_any_run(world)


@linux_only
@pytest.mark.parametrize("case", ["live-path", "clean", "no-mount"])
def test_checkout_flow_in_the_issued_sandbox_trusts_the_work_mount_alone(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import (
        CheckoutFlowError,
        CheckoutFlowStatus,
        run_checkout_flow,
    )
    from booley.runtime import runtime_context, sandbox_layout

    files = {"ip.core": PROBE_CORE.format(files="{live}/rtl/top.v")} if case == "live-path" else {}
    world = make_world(files=files)
    issued = runtime_context.RuntimeLocation.ISSUED_SANDBOX
    monkeypatch.setattr(runtime_context, "runtime_location", lambda: issued)
    # The issued Sandbox's Main has an empty ``.git`` sentinel, not a repository around it.
    monkeypatch.setattr(checkout_flow, "has_git_worktree_marker", lambda _path: False)
    monkeypatch.chdir(world.checkout.parent)
    if case == "no-mount":
        monkeypatch.setattr(sandbox_layout, "WORK_DIR", str(world.live / "missing"))

    if case == "clean":
        assert run_checkout_flow(world.request("pass")).status is CheckoutFlowStatus.PASSED
        return
    message = r"ip\.core names .*live working tree" if case == "live-path" else "is not mounted"
    with pytest.raises(CheckoutFlowError, match=message):
        run_checkout_flow(world.request("pass"))
    assert_refused_before_any_run(world)


@linux_only
def test_checkout_flow_refuses_when_no_live_working_tree_is_found(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow
    from booley.runtime import sandbox_layout

    monkeypatch.setattr(sandbox_layout, "WORK_DIR", str(world.live / "missing"))
    monkeypatch.setattr(checkout_flow, "has_git_worktree_marker", lambda _path: False)

    with pytest.raises(CheckoutFlowError, match="no live working tree found"):
        run_checkout_flow(world.request("pass"))

    assert_refused_before_any_run(world)


@linux_only
def test_checkout_flow_runs_with_a_relative_path_spelling_a_live_root(
    make_world: WorldBuilder,
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow

    # ``sim`` + the live path is one relative token, not an absolute path into the live tree.
    world = make_world(config='[flows.sim]\npre_run_commands = ["make -C sim{live}"]\n')

    assert run_checkout_flow(world.request("pass")).status is CheckoutFlowStatus.PASSED


@linux_only
def test_checkout_flow_refuses_a_resolved_file_in_the_live_booley_dir(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    use_fusesoc(monkeypatch)
    # Relative, so the static scan passes; FuseSoC resolves it into the live Booley Directory.
    shared = "../../live/.booley/shared.v"
    world = make_world(files={"probe.core": PROBE_CORE.format(files=shared)})
    (world.main / "shared.v").write_bytes(b"module top; endmodule\n")

    with pytest.raises(CheckoutFlowError) as refused:
        run_checkout_flow(world.request("probe"))

    message = str(refused.value)
    assert "Target probe" in message
    assert str(world.main / "shared.v") in message and "Main Booley Directory" in message
    assert_refused_before_any_run(world)


@linux_only
def test_checkout_flow_refuses_an_include_path_in_the_live_booley_dir(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    use_fusesoc(monkeypatch)
    # A relative include directory: only the resolved EDAM shows where it points.
    include = "{inc/defs.vh: {is_include_file: true, include_path: ../../live/.booley/inc}}"
    world = make_world(
        files={
            "probe.core": PROBE_CORE.format(files=f"top.v, {include}"),
            "top.v": "module top; endmodule\n",
            "inc/defs.vh": "",
        }
    )

    with pytest.raises(CheckoutFlowError) as refused:
        run_checkout_flow(world.request("probe"))

    assert str(world.main / "inc") in str(refused.value)
    assert_refused_before_any_run(world)


@linux_only
def test_preflight_resolution_runs_in_the_measurement_environment(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow

    use_fusesoc(monkeypatch)
    world = make_world(
        files={
            "probe.core": GENERATOR_CORE,
            "gen.py": GENERATOR.format(ctl="{ctl}", wait=0),
            "top.v": "module top; endmodule\n",
        }
    )
    # After ``make_world``, which clears the caller variables it knows of.
    monkeypatch.setenv("CALLER_ONLY_VARIABLE", "x")

    # The stub Flow resolves nothing, so the generator's record comes from the preflight.
    result = run_checkout_flow(world.request("probe"))

    assert result.status is CheckoutFlowStatus.PASSED
    seen = json.loads((world.ctl / "generator.json").read_text(encoding="utf-8"))
    assert "CALLER_ONLY_VARIABLE" not in seen["env"]
    flow_env = world.seen()["env"]
    assert isinstance(flow_env, dict)
    scratch = Path(flow_env["HOME"]).parent
    assert Path(seen["env"]["HOME"]).parent == scratch  # the run's own HOME, as the Flow saw
    assert scratch.parent == world.checkout.parent and scratch.name.startswith(".checkout.env-")
    assert seen["rc"] == 128
    assert seen["stderr"] == (
        "booley: git rev-parse HEAD: the measurement checkout is not a git repository"
    )


@linux_only
def test_pre_sim_git_call_fails_with_the_checkout_message(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import run_checkout_flow

    use_fusesoc(monkeypatch)
    world = make_world(
        files={"probe.core": PROBE_CORE.format(files="top.v"), "top.v": "module top; endmodule\n"},
        config='[flows.sim]\npre_run_commands = ["git rev-parse HEAD"]\n',
    )

    run_checkout_flow(world.request("presim"))

    presim = json.loads((world.ctl / "presim.json").read_text(encoding="utf-8"))
    assert (presim["status"], presim["returncode"]) == ("failed", 128)
    assert presim["stderr"].strip() == (
        "booley: git rev-parse HEAD: the measurement checkout is not a git repository"
    )
    # Run evidence reads "unversioned", as for any non-git Project.
    assert presim["revision"] is None


@linux_only
def test_checkout_flow_refuses_when_the_preflight_cannot_resolve(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    use_fusesoc(monkeypatch)
    world = make_world(files={"probe.core": PROBE_CORE.format(files="missing.v")})

    with pytest.raises(CheckoutFlowError, match=r"could not resolve Target probe"):
        run_checkout_flow(world.request("probe"))

    assert_refused_before_any_run(world)


@linux_only
def test_checkout_flow_refuses_when_the_preflight_does_not_finish(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    use_fusesoc(monkeypatch)
    # The request's bound applies; it is generous, FuseSoC starts slowly on a loaded host.
    monkeypatch.setattr(checkout_flow, "_PREFLIGHT_MIN_S", 0)
    world = make_world(
        files={
            "probe.core": GENERATOR_CORE,
            # Outlives the bound and the poll below: only a group kill ends it in time.
            "gen.py": GENERATOR.format(ctl="{ctl}", wait=120),
            "top.v": "module top; endmodule\n",
        }
    )

    with pytest.raises(CheckoutFlowError, match=r"did not finish within 20 s"):
        run_checkout_flow(world.request("probe", timeout_s=20))

    assert_refused_before_any_run(world)
    assert (world.ctl / "generator.pid").exists(), "the generator never started within the bound"
    pid = int((world.ctl / "generator.pid").read_text(encoding="utf-8"))
    poll(lambda: not Path(f"/proc/{pid}").exists(), "the generator to be gone")


@linux_only
def test_checkout_flow_refuses_when_the_preflight_cannot_start(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(2, "no interpreter")

    monkeypatch.setattr(checkout_flow.subprocess, "Popen", refuse)

    with pytest.raises(CheckoutFlowError, match=r"preflight of Target pass could not start"):
        run_checkout_flow(world.request("pass"))

    assert_refused_before_any_run(world)


@linux_only
def test_checkout_flow_refuses_a_generated_core_naming_a_live_path(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    use_fusesoc(monkeypatch)
    generator = GENERATOR.format(ctl="{ctl}", wait=0).replace(
        'name: acme:ip:made:0.1\\n"', 'name: acme:ip:made:0.1\\n# {live}/data\\n"'
    )
    world = make_world(
        files={
            "probe.core": GENERATOR_CORE,
            "gen.py": generator,
            "top.v": "module top; endmodule\n",
        }
    )

    with pytest.raises(CheckoutFlowError) as refused:
        run_checkout_flow(world.request("probe"))

    assert "made.core names " + str(world.live) in str(refused.value)
    assert_refused_before_any_run(world)


@linux_only
@pytest.mark.parametrize("target", ["probe", "pass"])
def test_preflight_main_writes_the_paths_a_fusesoc_target_reads(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str
) -> None:
    from booley.harness.checkout_flow import preflight_main

    # In process, so the changed lines are measured: the Flow's child runs it in a subprocess.
    use_fusesoc(monkeypatch)
    world = make_world(
        files={"probe.core": PROBE_CORE.format(files="top.v"), "top.v": "module top; endmodule\n"}
    )
    monkeypatch.chdir(world.checkout)
    result_file = tmp_path / "preflight.json"

    assert preflight_main([target, str(tmp_path / "build"), str(result_file)]) == 0

    document = json.loads(result_file.read_text(encoding="utf-8"))
    if target == "pass":  # a Project Flow takes no FuseSoC Target
        assert document == {"files": None, "cores": []}
    else:
        assert document["files"] == [str(world.checkout.resolve() / "top.v")]
        assert document["cores"] == [str(world.checkout.resolve() / "probe.core")]


@linux_only
@pytest.mark.parametrize("failing", ["select", "resolve"])
def test_preflight_main_exits_with_the_start_failure_code_on_a_fusesoc_error(
    make_world: WorldBuilder,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failing: str,
) -> None:
    from booley.fusesoc import fusesoc_registry
    from booley.harness.checkout_flow import preflight_main
    from booley.targets.catalog import TargetCatalog

    use_fusesoc(monkeypatch)
    world = make_world(
        files={"probe.core": PROBE_CORE.format(files="top.v"), "top.v": "module top; endmodule\n"}
    )
    monkeypatch.chdir(world.checkout)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise fusesoc_registry.FuseSocError("boom")

    if failing == "select":
        monkeypatch.setattr(TargetCatalog, "select", fail)
    else:
        monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", fail)

    assert preflight_main(["probe", str(tmp_path / "build"), str(tmp_path / "r.json")]) == 2
    assert "boom" in capsys.readouterr().err
    assert not (tmp_path / "r.json").exists()


@linux_only
def test_prepare_scratch_removes_its_directory_when_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import checkout_flow

    def fail(_directory: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(checkout_flow, "write_git_refusal", fail)
    checkout = tmp_path / "scratch" / "checkout"
    checkout.mkdir(parents=True)

    with pytest.raises(OSError, match="disk full"):
        checkout_flow._prepare_scratch(checkout)

    assert not list(checkout.parent.glob(".*env-*"))


@linux_only
def test_a_callers_interrupt_ends_the_preflight_child(tmp_path: Path) -> None:
    import threading

    from booley.harness.checkout_flow import _run_preflight_child

    pid_file = tmp_path / "pid"
    command = ["sh", "-c", f"echo $$ > {pid_file}; sleep 60"]
    threading.Timer(1.0, _thread.interrupt_main).start()

    with pytest.raises(KeyboardInterrupt):
        _run_preflight_child(command, tmp_path, dict(os.environ), 30.0, tmp_path / "log", "t")

    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


@linux_only
def test_the_preflight_of_a_run_without_a_timeout_uses_the_standalone_bound(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from booley.harness import checkout_flow

    world = make_world()
    bounds: list[float] = []

    def child(
        command: list[str], _c: Path, _e: dict[str, str], bound_s: float, *_r: object
    ) -> None:
        bounds.append(bound_s)
        Path(command[-1]).write_text('{"files": null, "cores": []}', encoding="utf-8")

    monkeypatch.setattr(checkout_flow, "_run_preflight_child", child)
    scratch = tmp_path / "scratch-dir"
    (scratch / "tmp").mkdir(parents=True)

    checkout_flow._preflight(
        world.request("pass", timeout_s=None), world.checkout, [], scratch, {}
    )

    assert bounds == [checkout_flow.STANDALONE_TIMEOUT_S]
