"""A Booley Flow runs in an exported checkout with shared state in the Main Booley Directory.

Each run starts a real child process. The live Project is a Git repository whose
Main Booley Directory is its own ``.booley``; the checkout is an export of its
commit, so it holds no ``.git`` and no shared state.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

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
TIMEOUT_S = 60
FLOW = "checkout_stub"

# The stub Flow's behavior follows ``--target``: ``pass`` runs one real command,
# ``fail`` returns a failing outcome, ``wait`` waits (bounded) for the ``release``
# marker and ``env`` records what it saw. Its control directory is written into the source.
STUB_FLOW = """
import json, os, time
from pathlib import Path
from booley.flows.base import BooleyFlow
from booley.runtime.endpoint_execution import EndpointOutcome

CTL = Path({ctl!r})

class CheckoutStub(BooleyFlow):
    name = "checkout_stub"
    description = "Checkout Flow runner fixture"
    JOB_CLASS = "heavy"
    target_required = False

    def _add_args(self, parser):
        pass

    def _run(self):
        seen = {{
            "env": dict(os.environ),
            "home_is_dir": os.path.isdir(os.environ["HOME"]),
            "tmp_is_dir": os.path.isdir(os.environ["TMPDIR"]),
            "work_dir": str(self.args.work_dir),
        }}
        (CTL / "seen.json").write_text(json.dumps(seen), encoding="utf-8")
        (CTL / "started").write_text("1", encoding="utf-8")
        target = self.args.target
        if target == "fail":
            return EndpointOutcome(exit_code=1)
        if target == "wait":
            deadline = time.monotonic() + 60
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
    "CALLER_ONLY_VARIABLE": "x",
}
KEPT_ENV = {"HTTP_PROXY": "http://proxy.invalid:3128", "LANG": "C.UTF-8"}


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


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """A committed Project whose uncommitted settings differ from its checkout's."""
    # PYTHONPATH stays: the suite sets it so that child interpreters import this checkout.
    for name in ("BOOLEY_DIR", *CALLER_ENV.keys() - {"PYTHONPATH"}):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("_BOOLEY_CLI_INVOCATION_ORIGIN", raising=False)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    live = tmp_path / "live"
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    tools = live / ".booley" / "mcp_tools"
    tools.mkdir(parents=True)
    (live / ".booley" / "booley.toml").write_bytes(b"[jobs]\nmax_heavy = 4\n")
    (tools / f"{FLOW}.py").write_bytes(STUB_FLOW.format(ctl=str(ctl)).encode())
    git(live, "init", "-q", "-b", "main")
    git(live, "add", "-A", "-f")
    git(live, "commit", "-q", "-m", "stub")
    commit = git(live, "rev-parse", "HEAD")
    # Main's settings now differ from the checkout's committed ones.
    (live / ".booley" / "booley.toml").write_bytes(b"[jobs]\nmax_heavy = 1\n")
    checkout = tmp_path / "scratch" / "checkout"
    export_commit(live, commit, checkout)
    booley_dir.reset_cache()
    yield World(live, live / ".booley", checkout, tmp_path / "reports", ctl)
    booley_dir.reset_cache()
    # A failed assertion must not leave a Flow running into other tests.
    for record in job_records.list_records(
        standalone_jobs_dir(live / ".booley"), skip_malformed=True
    ):
        if record.pid is not None and record.status == job_records.STATUS_RUNNING:
            with contextlib.suppress(OSError):
                os.kill(record.pid, signal.SIGKILL)


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


def refusal_requests(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, CheckoutFlowRequest]:
    """One request per refusal rule; each names the path it is refused for."""
    plain_main, env_dir = tmp_path / "main-bd", tmp_path / "env-bd"
    live_checkout = world.live / "exported"
    live_checkout.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(env_dir))
    return {
        "live-tree": world.request("pass", checkout=live_checkout),
        "main-booley-dir": world.request(
            "pass", checkout=plain_main / "co", main_booley_dir=plain_main
        ),
        "booley-dir-env": world.request("pass", checkout=env_dir / "co"),
        "report-in-checkout": world.request("pass", report_dir=world.checkout / "reports"),
    }


@linux_only
@pytest.mark.parametrize(
    "case", ["live-tree", "main-booley-dir", "booley-dir-env", "report-in-checkout"]
)
def test_checkout_flow_refuses_live_state_paths(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    request = refusal_requests(world, tmp_path, monkeypatch)[case]
    request.checkout.mkdir(parents=True, exist_ok=True)
    offending = request.report_dir if case == "report-in-checkout" else request.checkout

    with pytest.raises(CheckoutFlowError) as refused:
        run_checkout_flow(request)

    assert str(offending.resolve()) in str(refused.value)
    # Refused before anything was created: no Job record, no environment directory.
    assert not standalone_jobs_dir(request.main_booley_dir).exists()
    assert not list(request.checkout.parent.glob(".*env-*"))
    assert not (world.ctl / "started").exists()


@linux_only
@pytest.mark.parametrize("case", ["missing-checkout", "bad-flow-name"])
def test_checkout_flow_refuses_a_malformed_request(world: World, case: str) -> None:
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    request = {
        "missing-checkout": world.request("pass", checkout=world.checkout / "gone"),
        "bad-flow-name": world.request("pass", flow="../escape"),
    }[case]

    with pytest.raises(CheckoutFlowError, match=r"not a directory|invalid Flow name"):
        run_checkout_flow(request)

    assert not standalone_jobs_dir(world.main).exists()


@linux_only
def test_checkout_flow_start_failure_leaves_a_failed_record(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import checkout_flow
    from booley.harness.checkout_flow import CheckoutFlowError, run_checkout_flow

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(2, "no interpreter")

    monkeypatch.setattr(checkout_flow.subprocess, "Popen", refuse)

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
    store = job_slots.SlotStore(slots_root, SlotCaps(max_heavy=1))
    # A Flow of the session's own checkout holds Main's only heavy slot.
    holder = store.acquire(
        job_slots.CLASS_HEAVY, pid=os.getpid(), argv=_proc_cmdline(os.getpid()) or []
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
    # The checkout gained at most its own build cache: no shared state, no bytecode.
    added = {path.relative_to(world.checkout) for path in world.checkout.rglob("*")} - before
    cache = Path(".booley/.runtime/python-artifacts")
    stray = {
        path for path in added if path not in {cache.parent, cache} and cache not in path.parents
    }
    assert stray == set()
    assert not [path for path in added if "__pycache__" in path.parts]


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
        if path.is_file() and path.name != "job.log" and "resources" not in path.parts:
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
    shutil.rmtree(world.checkout)

    assert_no_record_names(world.main, world.checkout)
    assert not (record.work_dir or "").startswith(str(world.checkout))
    # The reports are where the caller asked for them, and still readable.
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
    assert final.status != job_records.STATUS_RUNNING and final.ended_at
    assert not list(world.checkout.parent.glob(".*env-*"))


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


def test_child_main_without_a_record_reports_it(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.harness import checkout_flow

    assert checkout_flow.child_main([str(world.main), "missing-1", FLOW]) == 2
    assert "record not found: missing-1" in capsys.readouterr().err
