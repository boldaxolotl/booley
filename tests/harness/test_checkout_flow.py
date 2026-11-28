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

from booley.harness.dashboard.jobs import JobReader
from booley.harness.standalone_job import (
    STANDALONE_TIMEOUT_S,
    standalone_jobs_dir,
    standalone_log_path,
)
from booley.runtime import booley_dir, job_records, job_slots
from booley.runtime.commit_export import export_commit
from booley.runtime.resource_history import load_records
from booley.runtime.runtime_context import RuntimeLocation
from tests.harness.process_fake import FakeProcess
from tests.sandbox_venue_fake import place
from tests.subprocess_budget import run_bounded_child
from tests.time_budget import hang_guard_s, scaled

if TYPE_CHECKING:
    from booley.harness.checkout_flow import (
        CheckoutFlowChild,
        CheckoutFlowRequest,
        CheckoutFlowResult,
    )

# The runner is imported inside each test, so its absence fails that test, not collection.
linux_only = pytest.mark.skipif(
    sys.platform != "linux", reason="checkout Flows are observed through /proc and POSIX sessions"
)

# Generous bound for a child process or a marker file; only a hang ever reaches it.
TIMEOUT_S = hang_guard_s()
# A run that must time out, and a preflight that must overrun its bound (real clock).
WAIT_DEADLINE_S = scaled(2)
PREFLIGHT_DEADLINE_S = scaled(20)
FLOW = "checkout_stub"

# The stub Flow's behavior follows ``--target``: ``pass`` runs one real command,
# ``fail`` returns a failing outcome, ``crash`` raises, ``abort`` exits at once with no
# report, ``wait`` (or any target starting with ``wait``) waits (bounded) for the ``release``
# marker, ``linger`` is a ``wait`` that takes 7 s to stop once interrupted, and ``env`` records
# what it saw. Every run appends its target to ``order`` when it
# starts, and a target starting with ``metrics`` returns a typed detail. Its control
# directory is written into the source.
STUB_FLOW = """
import json, os, signal, time
from pathlib import Path
from booley.flows.base import BooleyFlow
from booley.runtime.endpoint_execution import EndpointOutcome

CTL = Path({ctl!r})

class CheckoutStub(BooleyFlow):
    name = "checkout_stub"
    description = "Checkout Flow runner fixture"
    JOB_CLASS = "memory"  # admitted by Memory Reservation, so a run-alone holder blocks it
    target_required = False

    def _add_args(self, parser):
        parser.add_argument("--coverage", action="store_true")
        parser.add_argument("--count", type=int)
        parser.add_argument("--seed", type=int)

    def _run(self):
        seen = {{
            "env": dict(os.environ),
            "home_is_dir": os.path.isdir(os.environ["HOME"]),
            "tmp_is_dir": os.path.isdir(os.environ["TMPDIR"]),
            "work_dir": str(self.args.work_dir),
            "coverage": self.args.coverage,
            "count": self.args.count,
            "seed": self.args.seed,
        }}
        (CTL / "seen.json").write_text(json.dumps(seen), encoding="utf-8")
        (CTL / "started").write_text("1", encoding="utf-8")
        target = self.args.target
        with (CTL / "order").open("a", encoding="utf-8") as order:
            order.write(target + "\\n")
        if target == "fail":
            return EndpointOutcome(exit_code=1)
        if target == "liberty":
            # The ASIC Synthesis Flow's own Liberty discovery, as a ``synth`` run reaches it.
            from booley.flows.synth.backends.yosys.discovery import resolve_liberty

            (CTL / "liberty").write_text(str(resolve_liberty()), encoding="utf-8")
        if target == "crash":
            raise RuntimeError("stub crashed")
        if target == "abort":
            print("stub aborted", flush=True)
            os._exit(3)  # dies without writing a report or a terminal record
        if target.startswith("metrics"):
            return EndpointOutcome(detail={{"marker": 7}})
        if target == "presim":
            # Pre-Sim Commands of core Target ``probe``, then the run evidence's Git lookup.
            from booley.flows.baseline_worktree import git_full_sha
            from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
            from booley.targets.catalog import TargetCatalog

            work = Path(self.args.work_dir)
            handle = TargetCatalog.build(work).select("probe")
            evidence = run_pre_sim_commands(
                handle, test_names=("t",), build_root=work / "b", eda_tool="icarus", timeout_s={hang_s}, resource_flags=()
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
        if target == "linger":
            def stop(*_):  # a stop takes 7 s, longer than the MCP default cancel grace of 5 s
                (CTL / "stopping").write_text("1", encoding="utf-8")
                time.sleep(7)
                os._exit(130)
            signal.signal(signal.SIGINT, stop)
            (CTL / "lingering").write_text("1", encoding="utf-8")  # the handler is installed
        if target == "stubborn":
            signal.signal(signal.SIGINT, signal.SIG_IGN)  # only a kill ends this Flow
            (CTL / "ignoring").write_text("1", encoding="utf-8")
        if target.startswith("wait") or target in ("stubborn", "linger"):
            deadline = time.monotonic() + {outer_s}
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
    from booley.core import sandbox_layout

    built: list[World] = []
    # The tests' verdict must not depend on the mounts of the host that runs them.
    place(monkeypatch, RuntimeLocation.HAND_WRITTEN_SANDBOX)

    def build(
        files: Mapping[str, str] | None = None,
        symlinks: Mapping[str, str] | None = None,
        config: str = "",
    ) -> World:
        # PYTHONPATH stays: the suite sets it so that child interpreters import this checkout.
        for name in ("BOOLEY_DIR", "PRJ_LIB_DIR", *CALLER_ENV.keys() - {"PYTHONPATH"}):
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
        settings = "[jobs]\nmax_tickets = SLOTS\n" + config.replace("{live}", str(live))
        (live / ".booley" / "booley.toml").write_bytes(settings.replace("SLOTS", "4").encode())
        (tools / f"{FLOW}.py").write_bytes(
            STUB_FLOW.format(
                ctl=str(ctl), hang_s=hang_guard_s(), outer_s=2 * hang_guard_s()
            ).encode()
        )
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
    [pytest.param("pass", "passed", 0, id="pass"), pytest.param("fail", "failed", 1, id="fail")],
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
    assert result.elapsed_s is not None and result.elapsed_s == document["elapsed_s"]


@linux_only
def test_coverage_and_settings_reach_the_child_as_its_options(world: World) -> None:
    """Each setting of the request is one ``--<name> <value>`` option of the Flow child."""
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow

    plain = run_checkout_flow(world.request("pass"))
    assert plain.status is CheckoutFlowStatus.PASSED
    assert world.seen()["coverage"] is False
    assert record_of(world, plain).argv == ["booley", "flow", FLOW, "--target", "pass"]

    result = run_checkout_flow(
        world.request("pass", coverage=True, settings={"count": 4, "seed": 3})
    )

    assert result.status is CheckoutFlowStatus.PASSED
    seen = world.seen()
    assert (seen["coverage"], seen["count"], seen["seed"]) == (True, 4, 3)
    assert record_of(world, result).argv[3:] == [
        "--target",
        "pass",
        "--count",
        "4",
        "--seed",
        "3",
        "--coverage",
    ]


@linux_only
def test_failed_crashed_or_timed_out_flow_returns_an_excerpt(world: World) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow

    cases = [
        ("crash", {}, CheckoutFlowStatus.FAILED, "RuntimeError: stub crashed"),
        ("abort", {}, CheckoutFlowStatus.FAILED, "stub aborted"),
        (
            "wait",
            {"timeout_s": WAIT_DEADLINE_S},
            CheckoutFlowStatus.TIMED_OUT,
            f"the run timed out after {WAIT_DEADLINE_S:g} s",
        ),
    ]
    for target, changes, status, expected in cases:
        result = run_checkout_flow(world.request(target, **changes))

        assert result.status is status, target
        assert result.metrics == {}, target
        assert result.excerpt is not None and expected in result.excerpt, target
        if status is CheckoutFlowStatus.TIMED_OUT:
            assert result.excerpt.startswith(expected)


def refusal_requests(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, CheckoutFlowRequest]:
    """One request per refusal rule; each names the path it is refused for."""
    from booley.core import sandbox_layout

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
    place(monkeypatch, RuntimeLocation.ISSUED_SANDBOX)
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
        "unusable-config",
    ],
)
def test_checkout_flow_refuses_booley_config_outside_the_checkout(
    world: World, tmp_path: Path, case: str
) -> None:
    """A checkout whose Booley configuration is not inside it is refused, naming the directory."""
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    checkout = world.checkout
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
        (checkout.parent / "booley.toml").write_bytes(
            f'[project]\ndir = "{offending.as_posix()}"\n'.encode()
        )
    elif case == "ancestor-selects-inside":
        # Even a directory inside the checkout is refused when an ancestor chose it.
        offending = checkout / "steered"
        offending.mkdir()
        (checkout.parent / "booley.toml").write_bytes(
            f'[project]\ndir = "{checkout.name}/steered"\n'.encode()
        )
    elif case == "own-root":
        # The checkout's own ``booley.toml`` selects the checkout root itself.
        offending = checkout
        (checkout / "booley.toml").write_bytes(b'[project]\ndir = "."\n')
    elif case == "unusable-config":
        # A Booley Directory that is not a directory is the checkout's problem.
        offending = checkout
        shutil.rmtree(checkout / ".booley")
        (checkout / ".booley").write_bytes(b"not a directory\n")
    else:
        # The checkout's own ``booley.toml`` selects a directory above it.
        offending = checkout.parent / "selected"
        offending.mkdir()
        (checkout / "booley.toml").write_bytes(b'[project]\ndir = "../selected"\n')
    request = world.request("pass", checkout=checkout)

    with pytest.raises(CheckoutFlowError) as refused:
        run_checkout_flow(request)

    assert str(offending.resolve()) in str(refused.value)
    if case in ("own-root", "unusable-config"):
        assert {"own-root": "selects its own root", "unusable-config": "unusable Booley"}[
            case
        ] in str(refused.value)
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
        "outside-sandbox",
        "runner-env",
        "unwritable-record",
    ],
)
def test_checkout_flow_refuses_an_unusable_request_or_runner(
    world: World, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from booley.core.booley_layout import RETIRED_ENV
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    def unwritable(*_args: object, **_kwargs: object) -> None:
        raise OSError(30, "read-only file system")

    if case == "outside-sandbox":
        place(monkeypatch, RuntimeLocation.HOST)
    if case == "runner-env":
        # A retired selection variable in the runner's environment is not the checkout's fault.
        monkeypatch.setenv(RETIRED_ENV, "")
    if case == "unwritable-record":
        monkeypatch.setattr(checkout_flow.job_records, "write_record_strict", unwritable)
    request, message = {
        "missing-checkout": (world.request("pass", checkout=world.checkout / "gone"), "not a dir"),
        "bad-flow-name": (world.request("pass", flow="../escape"), "invalid Flow name"),
        "bad-target": (world.request("bad\0target"), "invalid Target"),
        "empty-target": (world.request("  "), "exactly one Target"),
        "several-targets": (world.request("a,b"), "exactly one Target"),
        "nan-timeout": (world.request("pass", timeout_s=float("nan")), "positive"),
        "outside-sandbox": (world.request("pass"), "only inside the Sandbox"),
        "runner-env": (world.request("pass"), "runner's environment is unusable"),
        "unwritable-record": (world.request("pass"), "read-only file system"),
    }[case]

    with pytest.raises(CheckoutFlowError, match=message):
        run_checkout_flow(request)

    assert not standalone_jobs_dir(world.main).exists()
    assert not (world.ctl / "started").exists()


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
    store = job_slots.SlotStore(slots_root)
    # A Flow of the session's own checkout runs alone in Main's memory class.
    holder = store.acquire(
        job_slots.CLASS_MEMORY,
        pid=os.getpid(),
        argv=list(sys.orig_argv),
        memory=job_slots.MemoryClaim(budget_mb=1024, run_alone=True),
    )
    reader = JobReader(world.main, slots_root=slots_root)
    with ThreadPoolExecutor(max_workers=1) as pool:
        run = pool.submit(run_checkout_flow, world.request("pass"))
        try:
            poll(
                lambda: [view.state for view in reader.read().jobs] == ["queued"],
                "the checkout Flow to queue",
            )
            assert not (world.ctl / "started").exists()
        finally:
            store.release(holder)
        result = run.result(timeout=TIMEOUT_S)

    assert result.status is CheckoutFlowStatus.PASSED
    assert (world.ctl / "started").exists()


@linux_only
def test_checkout_flow_keeps_shared_state_in_main(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow

    before = {path.relative_to(world.checkout) for path in world.checkout.rglob("*")}
    # A relative Main: the child runs in the checkout, where ".booley" is another place.
    monkeypatch.chdir(world.live)

    result = run_checkout_flow(world.request("pass", main_booley_dir=Path(".booley")))

    assert result.status is CheckoutFlowStatus.PASSED
    # Main gained the Flow's Resource History record and its Job record.
    assert [row["flow"] for row in load_records(world.main)] == [FLOW]
    assert record_of(world, result).status == "done"
    # The checkout gained nothing: no shared state, no build cache, no bytecode.
    added = {path.relative_to(world.checkout) for path in world.checkout.rglob("*")} - before
    assert added == set()


@linux_only
def test_checkout_flow_job_keeps_the_session_of_its_request(world: World) -> None:
    """#5310: a measuring session's Flow Job stays its Job after the Flow rewrote the record."""
    from booley.harness.checkout_flow import run_checkout_flow

    # No timeout: the Job records the standalone bound, and the preflight runs within it.
    result = run_checkout_flow(world.request("pass", session_key="codex:t1", timeout_s=None))

    record = record_of(world, result)
    assert record.session_key == "codex:t1"
    assert record.timeout_s == STANDALONE_TIMEOUT_S


@linux_only
def test_checkout_flow_environment_is_scrubbed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness.checkout_flow import run_checkout_flow

    for name, value in (CALLER_ENV | KEPT_ENV).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PRJ_LIB_DIR", "")  # an empty value is as good as unset
    before = set(world.checkout.parent.iterdir())

    result = run_checkout_flow(world.request("pass"))

    seen = world.seen()
    env = seen["env"]
    assert isinstance(env, dict)
    assert not (CALLER_ENV.keys() - {"BOOLEY_RUN_ID"}) & env.keys()
    assert "PRJ_LIB_DIR" not in env
    assert env["BOOLEY_RUN_ID"] == result.run_id  # its own run, not the caller's
    assert (env["PYTHONSAFEPATH"], env["PYTHONNOUSERSITE"]) == ("1", "1")
    assert env["BOOLEY_DIR"] == str(world.main)
    assert env["PYTHONPYCACHEPREFIX"].startswith(env["TMPDIR"])
    assert {name: env[name] for name in KEPT_ENV} == KEPT_ENV
    # HOME and TMPDIR existed during the run, in a directory that did not exist before it.
    assert seen["home_is_dir"] and seen["tmp_is_dir"]
    scratch = Path(env["HOME"]).parent
    assert scratch.parent == world.checkout.parent and scratch not in before
    # The run's own runtime tree: no other child or live run shares its scratch (AC3).
    assert Path(env["BOOLEY_RUNTIME_DIR"]).parent == scratch
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
@pytest.mark.parametrize("outcome", ["passed", "failed", "timed_out", "cancelled", "killed"])
def test_deleted_checkout_is_named_by_no_job_record(world: World, outcome: str) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow

    if outcome in {"cancelled", "killed"}:
        # The Dashboard cancels a Job by signalling the pid its record names; a Flow killed
        # before its terminal write is failed with its shell status.
        with ThreadPoolExecutor(max_workers=1) as pool:
            run = pool.submit(run_checkout_flow, world.request("wait"))
            poll((world.ctl / "started").exists, "the Flow to start")
            (running,) = records_in(world.main)
            assert running.pid is not None
            os.kill(running.pid, signal.SIGKILL if outcome == "killed" else signal.SIGTERM)
            result = run.result(timeout=TIMEOUT_S)
    else:
        target, timeout_s = {
            "passed": ("pass", TIMEOUT_S),
            "failed": ("fail", TIMEOUT_S),
            "timed_out": ("wait", WAIT_DEADLINE_S),
        }[outcome]
        result = run_checkout_flow(world.request(target, timeout_s=timeout_s))

    assert result.status is (
        CheckoutFlowStatus.FAILED if outcome == "killed" else CheckoutFlowStatus(outcome)
    )
    record = record_of(world, result)
    assert record.status != job_records.STATUS_RUNNING and record.ended_at
    if outcome in {"timed_out", "killed"}:
        assert record.status == job_records.STATUS_FAILED
    if outcome == "killed":
        assert (result.exit_code, record.exit_code) == (137, 137)
    assert not list(world.checkout.parent.glob(".*env-*"))
    shutil.rmtree(world.checkout)

    assert_no_record_names(world.main, world.checkout)
    assert not (record.work_dir or "").startswith(str(world.checkout))
    # The reports are where the caller asked for them, and still readable.
    if outcome in {"passed", "failed"}:
        assert result.reports
    for report in result.reports:
        assert json.loads(report.read_text(encoding="utf-8"))["run_id"] == result.run_id


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
def test_flow_that_ignores_the_stop_is_killed_after_the_grace(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import run_checkout_flow

    monkeypatch.setattr(checkout_flow, "STOP_GRACE_S", scaled(1))

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
    assert not list(world.checkout.parent.glob(".*env-*"))


def flow_child(
    root: Path,
    process: Any,
    *,
    timeout_s: float | None = None,
    age_s: float = 0.0,
    record: job_records.JobRecord | None = None,
) -> CheckoutFlowChild:
    """A started child around *process* whose Job records live under ``root/jobs``."""
    from booley.harness.checkout_flow import CheckoutFlowChild, CheckoutFlowRequest

    record = record or job_records.JobRecord("flow-1", FLOW, "2026-11-07T00:00:00Z", 60)
    return CheckoutFlowChild(
        process=process,
        request=CheckoutFlowRequest(root, root, root, FLOW, "pass", timeout_s),
        run_id=record.run_id,
        record=record,
        jobs_root=root / "jobs",
        log_path=root / "job.log",
        report_dir=root,
        scratch=root / "scratch",
        started_at=time.monotonic() - age_s,
    )


class InterruptedGraceWait(subprocess.Popen[bytes]):
    """A real child whose grace wait is cut short by a second Ctrl-C."""

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            raise KeyboardInterrupt
        return super().wait(timeout=hang_guard_s())


@linux_only
def test_second_interrupt_during_the_stop_still_kills_the_group(tmp_path: Path) -> None:
    # A child that ignores SIGINT, like a Flow stuck in a tool: only the group kill ends it.
    script = 'trap "" INT; echo ready; exec sleep 600'
    process = InterruptedGraceWait(
        ["sh", "-c", script], stdout=subprocess.PIPE, start_new_session=True
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == b"ready\n"
        child = flow_child(tmp_path, process, timeout_s=1, age_s=10)

        with pytest.raises(KeyboardInterrupt):
            child.poll()

        assert process.returncode == -signal.SIGKILL
    finally:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        subprocess.Popen.wait(process, timeout=hang_guard_s())  # past the interrupting wait
        if process.stdout is not None:
            process.stdout.close()


@pytest.mark.parametrize(
    ("case", "returncode", "timed_out", "status", "exit_code"),
    [
        # A child that finished ``done`` before the stop landed keeps its record.
        pytest.param("done-before-stop", 0, True, "passed", 0, id="done-before-stop"),
        # Exit 0 but the child's terminal record write was lost: the record says running.
        pytest.param("exit-0-without-done", 0, False, "failed", 0, id="exit-0-without-done"),
        # A record that exists but cannot be read settles from the one in memory.
        pytest.param("unreadable", 3, False, "failed", 3, id="unreadable"),
    ],
)
def test_settle_decides_the_outcome_from_the_record(
    tmp_path: Path, case: str, returncode: int, timed_out: bool, status: str, exit_code: int
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowStatus

    record = job_records.JobRecord("flow-1", FLOW, "2026-11-07T00:00:00Z", 60)
    if case == "done-before-stop":
        record.status, record.exit_code, record.ended_at = "done", 0, "2026-11-07T00:00:01Z"
        job_records.write_record_strict(record, tmp_path / "jobs")
    if case == "unreadable":
        (tmp_path / "jobs" / "flow-1.json").mkdir(parents=True)  # a directory: read raises
    child = flow_child(tmp_path, FakeProcess(returncode=returncode), record=record)
    child.timed_out = timed_out

    result = child.settle()

    assert (result.status, result.exit_code) == (CheckoutFlowStatus(status), exit_code)


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
    # private: the public boundary cannot show the selection: every layout where discovery and
    # ``--project`` disagree is refused by the parent first (dropping ``--project`` left
    # ``child_main`` passing with an ancestor ``[project].dir`` and with RTL_PROJECT_ROOT
    # set), so only the argv handed to the CLI does.
    monkeypatch.setattr(booley_cli, "main", main)

    assert checkout_flow._run_flow_cli(FLOW, ["--target", "pass"]) == 0

    # ``--project`` beats discovery, so no ancestor of the checkout can become the root.
    assert seen == [
        ["booley", "--project", str(world.checkout.resolve()), "flow", FLOW, "--target", "pass"]
    ]


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
    "case", ["core", "booley-toml", "symlink", "core-symlink", "config-symlink", "booley-dir-env"]
)
def test_checkout_flow_refuses_live_paths_in_inputs(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    env_dir = tmp_path / "env-bd"
    env_dir.mkdir()
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
        "booley-dir-env": (
            {"files": {"ip.core": PROBE_CORE.format(files=f"{env_dir}/rtl/top.v")}},
            "ip.core",
            f"{env_dir}/rtl/top.v",
        ),
    }
    build_arguments, offending, named_path = inputs[case]
    world = make_world(**build_arguments)
    root = f"live working tree {world.live}"
    if case == "booley-dir-env":
        monkeypatch.setenv("BOOLEY_DIR", str(env_dir))  # after ``make_world`` clears it
        root = f"BOOLEY_DIR {env_dir.resolve()}"

    with pytest.raises(CheckoutFlowError) as refused:
        run_checkout_flow(world.request("pass"))

    message = str(refused.value)
    assert offending in message
    assert named_path.replace("{live}", str(world.live)) in message
    assert f"inside the {root}" in message
    assert_refused_before_any_run(world)


@linux_only
@pytest.mark.parametrize(
    "case", ["around-main", "around-cwd", "empty-work", "git-work", "main-repo", "nowhere"]
)
def test_checkout_flow_derives_the_live_tree(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    from booley.core import sandbox_layout
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    world = make_world(files={"ip.core": PROBE_CORE.format(files="{live}/rtl/top.v")})
    if case != "git-work":  # that case keeps the world's ``/work``, a Git checkout
        monkeypatch.setattr(sandbox_layout, "WORK_DIR", str(world.live / "missing"))
    detached_main = tmp_path / "detached-main"  # outside every Git checkout
    detached_main.mkdir()
    request = world.request("pass")
    if case == "around-main":
        # Without a ``/work`` mount the live tree is the Git checkout around Main.
        monkeypatch.chdir(world.checkout.parent)
    elif case == "around-cwd":
        # Only the caller's directory shows the tree.
        monkeypatch.chdir(world.live)
        request = replace(request, main_booley_dir=detached_main)
    elif case == "empty-work":
        # The image's ``WORKDIR /work`` exists in a hand-written Sandbox without being the tree.
        (tmp_path / "empty-work").mkdir()
        monkeypatch.setattr(sandbox_layout, "WORK_DIR", str(tmp_path / "empty-work"))
    elif case == "git-work":
        # Only a ``/work`` that is a Git checkout (``WORK_DIR`` is the live tree) shows it.
        monkeypatch.chdir(tmp_path)
        request = replace(request, main_booley_dir=detached_main)
    elif case == "nowhere":
        # Neither Main, the caller's directory nor ``/work`` is a Git checkout.
        monkeypatch.setattr(checkout_flow, "has_git_worktree_marker", lambda _path: False)
    else:
        # A linked Booley Directory with its own repository is not the live tree around it.
        git(world.main, "init", "-q")
        monkeypatch.chdir(world.checkout.parent)

    refusal = (
        "no live working tree found"
        if case == "nowhere"
        else r"ip\.core names .*live working tree"
    )
    with pytest.raises(CheckoutFlowError, match=refusal):
        run_checkout_flow(request)

    if request.main_booley_dir == world.main:
        assert_refused_before_any_run(world)


@linux_only
@pytest.mark.parametrize("spelling", ["absolute", "relative"])
def test_checkout_flow_passes_the_callers_pdk_root(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spelling: str
) -> None:
    from booley.harness.checkout_flow import run_checkout_flow

    real = tmp_path / "pdk-real"
    library = real / "cell" / "lib" / "NangateOpenCellLibrary_typical_ccs.lib"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"library (x) {}\n")
    (tmp_path / "pdk").symlink_to(real)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRJ_LIB_DIR", str(tmp_path / "pdk") if spelling == "absolute" else "pdk")

    run_checkout_flow(world.request("liberty"))

    env = world.seen()["env"]
    assert isinstance(env, dict)
    assert env["PRJ_LIB_DIR"] == str(real.resolve())
    expected = real.resolve() / "cell" / "lib" / "NangateOpenCellLibrary_typical_ccs.lib"
    assert (world.ctl / "liberty").read_text(encoding="utf-8") == str(expected)


@linux_only
@pytest.mark.parametrize("case", ["live-tree", "main-booley-dir", "booley-dir-env", "symlink"])
def test_checkout_flow_refuses_a_pdk_root_in_a_live_root(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    env_dir = tmp_path / "env-bd"
    env_dir.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(env_dir))
    (tmp_path / "link").symlink_to(world.live)
    named, label, root = {
        "live-tree": (world.live / "pdk", "live working tree", world.live),
        "main-booley-dir": (world.main / "pdk", "Main Booley Directory", world.main),
        "booley-dir-env": (env_dir / "pdk", "BOOLEY_DIR", env_dir.resolve()),
        "symlink": (tmp_path / "link" / "pdk", "live working tree", world.live),
    }[case]
    monkeypatch.setenv("PRJ_LIB_DIR", str(named))

    with pytest.raises(CheckoutFlowError) as refused:
        run_checkout_flow(world.request("liberty"))

    expected = f"PRJ_LIB_DIR names {world.live / 'pdk' if case == 'symlink' else named.resolve()}"
    assert expected in str(refused.value)
    assert f"inside the {label} {root}" in str(refused.value)
    assert_refused_before_any_run(world)


@linux_only
@pytest.mark.parametrize("case", ["live-path", "clean", "no-mount"])
def test_checkout_flow_in_the_issued_sandbox_trusts_the_work_mount_alone(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from booley.core import sandbox_layout
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import (
        CheckoutFlowError,
        CheckoutFlowStatus,
        run_checkout_flow,
    )

    files = {"ip.core": PROBE_CORE.format(files="{live}/rtl/top.v")} if case == "live-path" else {}
    world = make_world(files=files)
    place(monkeypatch, RuntimeLocation.ISSUED_SANDBOX)
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
@pytest.mark.parametrize("case", ["empty-work-path", "relative-spelling"])
def test_checkout_flow_runs_when_no_input_reads_the_live_tree(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    from booley.core import sandbox_layout
    from booley.harness.checkout_flow import CheckoutFlowStatus, run_checkout_flow

    if case == "empty-work-path":
        # The image's ``WORKDIR /work`` exists in a hand-written Sandbox without being the
        # tree: naming a path under it is not a read of uncommitted work.
        empty_work = tmp_path / "empty-work"
        empty_work.mkdir()
        world = make_world(files={"ip.core": PROBE_CORE.format(files=f"{empty_work}/rtl/top.v")})
        monkeypatch.setattr(sandbox_layout, "WORK_DIR", str(empty_work))
    else:
        # ``sim`` + the live path is one relative token, not an absolute path into the live tree.
        world = make_world(config='[flows.sim]\npre_run_commands = ["make -C sim{live}"]\n')

    assert run_checkout_flow(world.request("pass")).status is CheckoutFlowStatus.PASSED


@linux_only
@pytest.mark.parametrize("case", ["file", "include-path"])
def test_checkout_flow_refuses_a_resolved_path_in_the_live_booley_dir(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    use_fusesoc(monkeypatch)
    # Relative, so the static scan passes; only the resolved EDAM shows where it points.
    if case == "file":
        files = {"probe.core": PROBE_CORE.format(files="../../live/.booley/shared.v")}
        named = "shared.v"
    else:
        include = "{inc/defs.vh: {is_include_file: true, include_path: ../../live/.booley/inc}}"
        files = {
            "probe.core": PROBE_CORE.format(files=f"top.v, {include}"),
            "top.v": "module top; endmodule\n",
            "inc/defs.vh": "",
        }
        named = "inc"
    world = make_world(files=files)
    (world.main / "shared.v").write_bytes(b"module top; endmodule\n")

    with pytest.raises(CheckoutFlowError) as refused:
        run_checkout_flow(world.request("probe"))

    message = str(refused.value)
    assert "Target probe" in message and "Main Booley Directory" in message
    assert str(world.main / named) in message
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
    assert "PRJ_LIB_DIR" not in flow_env
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

    with pytest.raises(CheckoutFlowError, match=r"could not resolve Target probe") as refused:
        run_checkout_flow(world.request("probe"))

    assert "missing.v" in str(refused.value)
    assert "only through the post-setup hook" in str(refused.value)
    assert_refused_before_any_run(world)


@linux_only
@pytest.mark.parametrize("ending", ["timeout", "interrupt"])
def test_checkout_flow_refuses_when_the_preflight_does_not_finish(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    use_fusesoc(monkeypatch)
    # private: the floor is 60 s of real clock and no request input lowers it.
    monkeypatch.setattr(checkout_flow, "_PREFLIGHT_MIN_S", 0)
    world = make_world(
        files={
            "probe.core": GENERATOR_CORE,
            # Outlives the bound and the poll below: only a group kill ends it in time.
            "gen.py": GENERATOR.format(ctl="{ctl}", wait=6 * PREFLIGHT_DEADLINE_S),
            "top.v": "module top; endmodule\n",
        }
    )

    if ending == "interrupt":

        def interrupt_once_started() -> None:
            poll((world.ctl / "generator.pid").exists, "the generator to start")
            _thread.interrupt_main()  # the caller's Ctrl-C

        with ThreadPoolExecutor(max_workers=1) as pool:
            watcher = pool.submit(interrupt_once_started)
            with pytest.raises(KeyboardInterrupt):
                run_checkout_flow(world.request("probe", timeout_s=hang_guard_s()))
            watcher.result(timeout=TIMEOUT_S)
    else:
        with pytest.raises(
            CheckoutFlowError, match=rf"did not finish within {PREFLIGHT_DEADLINE_S:g} s"
        ):
            run_checkout_flow(world.request("probe", timeout_s=PREFLIGHT_DEADLINE_S))

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


@pytest.fixture
def probe_checkout(make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch) -> World:
    """A world with a FuseSoC core for Target ``probe``, the caller standing in its checkout."""
    use_fusesoc(monkeypatch)
    world = make_world(
        files={"probe.core": PROBE_CORE.format(files="top.v"), "top.v": "module top; endmodule\n"}
    )
    monkeypatch.chdir(world.checkout)
    return world


@linux_only
@pytest.mark.parametrize("target", ["probe", "pass"])
def test_preflight_main_writes_the_paths_a_fusesoc_target_reads(
    probe_checkout: World, tmp_path: Path, target: str
) -> None:
    from booley.harness.checkout_flow import preflight_main

    world = probe_checkout
    # In process, so the changed lines are measured: the Flow's child runs it in a subprocess.
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
@pytest.mark.usefixtures("probe_checkout")
def test_preflight_main_exits_with_the_start_failure_code_on_a_fusesoc_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failing: str,
) -> None:
    from booley.fusesoc import fusesoc_registry
    from booley.harness.checkout_flow import preflight_main
    from booley.targets.catalog import TargetCatalog

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
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import run_checkout_flow

    def fail(_directory: Path) -> None:
        raise OSError("disk full")

    # No request input makes writing into a fresh scratch directory fail.
    monkeypatch.setattr(checkout_flow, "write_git_refusal", fail)

    with pytest.raises(OSError, match="disk full"):
        run_checkout_flow(world.request("pass"))

    assert_refused_before_any_run(world)


def test_poll_stops_a_child_whose_budget_expired(tmp_path: Path) -> None:
    young, unbounded, expired = FakeProcess(), FakeProcess(), FakeProcess()
    children = (
        flow_child(tmp_path, young, timeout_s=100, age_s=1),
        flow_child(tmp_path, unbounded, timeout_s=None, age_s=10_000),
        flow_child(tmp_path, expired, timeout_s=5, age_s=10),
    )

    assert not children[0].poll() and not children[1].poll()
    assert not (children[0].timed_out or children[1].timed_out)
    assert young.signals == [] and unbounded.signals == []
    assert children[2].poll() and children[2].timed_out
    assert expired.signals == [signal.SIGINT]
