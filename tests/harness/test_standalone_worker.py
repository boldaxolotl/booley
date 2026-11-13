"""The detached Standalone Job worker runs a Flow or Specialist that outlives its submitter.

Each real-worker test submits from a child Python that exits while the run is
still in progress, so the worker is detached when the assertions look at it.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.config.jobs import SlotCaps
from booley.harness.dashboard.jobs import JobReader
from booley.harness.standalone_job import (
    standalone_jobs_dir,
    standalone_log_path,
    standalone_request_path,
)
from booley.mcp import sim_baseline_jobs
from booley.runtime import job_records, job_slots
from booley.runtime.job_records import _proc_cmdline
from booley.runtime.timefmt import utc_now_rfc3339
from tests.subprocess_budget import run_bounded_child

linux_only = pytest.mark.skipif(
    sys.platform != "linux",
    reason="detached workers are observed through /proc and POSIX sessions",
)

# Generous bound for a child process or a marker file; only a hang ever reaches it.
TIMEOUT_S = 60

# Seeded into the submitter: none may reach the worker.
GOAL_AND_SESSION_ENV = {
    "BOOLEY_GOAL_RUN_BINDING": '{"goal": "g1"}',
    "BOOLEY_GOAL_FILE": "/nonexistent/goal.json",
    "BOOLEY_EXECUTION_ID": "session-execution",
    "BOOLEY_RUNTIME_EXECUTION_ID": "session-runtime-execution",
    "BOOLEY_RUN_ID": "outer-run",
}

# Both stubs wait, bounded, for the test's ``release`` marker; ``wait`` is shared source.
STUB_WAIT = """
import os, time
from pathlib import Path

def wait(ctl):
    deadline = time.monotonic() + 60
    while not (ctl / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
"""

STUB_FLOW = (
    STUB_WAIT
    + """
import json
from booley.flows.base import BooleyFlow
from booley.runtime.endpoint_execution import EndpointOutcome

class StubFlow(BooleyFlow):
    name = "stub_flow"
    description = "Detached worker fixture"
    JOB_CLASS = "light"

    def _memory_admitted(self):
        return False  # counted light slot: the test queues behind a plain slot holder
    target_required = False

    def _add_args(self, parser):
        parser.add_argument("--ctl", required=True)

    def _run(self):
        ctl = Path(self.args.ctl)
        print("flow output line")
        (ctl / "env.json").write_text(json.dumps(dict(os.environ)), encoding="utf-8")
        (ctl / "started").write_text("1", encoding="utf-8")
        wait(ctl)
        return EndpointOutcome()
"""
)

STUB_SPECIALIST = (
    STUB_WAIT
    + """
import json
from booley.specialists.specialist import Specialist, parent_death_watchdog

class StubSpecialist(Specialist):
    name = "stub_specialist"
    description = "Detached worker fixture"

    def _build_prompt(self):
        return "unused"

    def _interpret_output(self, output, structured):
        return None

    def main(self, argv, **kwargs):
        from booley.config.settings import get_agent_settings

        ctl = Path(argv[argv.index("--ctl") + 1])
        settings = get_agent_settings()
        (ctl / "seen.json").write_text(
            json.dumps(
                {
                    "argv": list(argv),
                    "model": settings.model_for_specialist("reviewer"),
                    "default_cli": settings.default_cli,
                    "claude_auth": settings.auth_for("claude"),
                    "codex_auth": settings.auth_for("codex"),
                }
            ),
            encoding="utf-8",
        )
        with parent_death_watchdog(self.name, interval=0.02):
            parent = os.getppid()
            (ctl / "armed").write_text("1", encoding="utf-8")
            deadline = time.monotonic() + 60
            while os.getppid() == parent and time.monotonic() < deadline:  # submitter exits
                time.sleep(0.02)
            time.sleep(0.5)  # 25 polls: an armed watchdog has aborted this run by now
            (ctl / "survived").write_text("1", encoding="utf-8")
            wait(ctl)
        return 0
"""
)

# Runs in the submitter: submit, report the run id, wait for the stub's marker, then exit.
SUBMITTER = """
import json, sys, time
from pathlib import Path
from booley.harness.standalone_worker import StandaloneRequest, submit

booley_dir, request, marker = Path(sys.argv[1]), json.loads(sys.argv[2]), sys.argv[3]
run_id = submit(
    booley_dir,
    StandaloneRequest(Path(request["worktree"]), request["kind"], request["name"], tuple(request["argv"])),
)
print(run_id, flush=True)
deadline = time.monotonic() + 60
while marker and not Path(marker).exists() and time.monotonic() < deadline:
    time.sleep(0.02)
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A Project whose Booley Directory has one Flow, one Specialist and one light slot."""
    for name in ("BOOLEY_DIR", "BOOLEY_SLOTS_DIR", *GOAL_AND_SESSION_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("_BOOLEY_CLI_INVOCATION_ORIGIN", raising=False)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    root = tmp_path / "proj"
    tools = root / ".booley" / "mcp_tools"
    tools.mkdir(parents=True)
    # The default CLI is named so a Specialist resolves one inside a container too.
    (root / ".booley" / "booley.toml").write_text(
        '[agent]\ndefault = "claude"\n\n[jobs]\nmax_light = 1\n', encoding="utf-8"
    )
    (tools / "stub_flow.py").write_text(STUB_FLOW, encoding="utf-8")
    (tools / "stub_specialist.py").write_text(STUB_SPECIALIST, encoding="utf-8")
    yield root
    # A failed assertion must not leave a detached worker running into other tests.
    for record in job_records.list_records(
        standalone_jobs_dir(root / ".booley"), skip_malformed=True
    ):
        if record.pid is not None and record.status == job_records.STATUS_RUNNING:
            with contextlib.suppress(OSError):
                os.kill(record.pid, signal.SIGKILL)


@pytest.fixture
def ctl(tmp_path: Path) -> Path:
    """Marker directory the stubs and the test use to order their steps."""
    directory = tmp_path / "ctl"
    directory.mkdir()
    return directory


def booley_dir_of(project: Path) -> Path:
    return project / ".booley"


def jobs_root_of(project: Path) -> Path:
    return standalone_jobs_dir(booley_dir_of(project))


def poll(condition: Callable[[], object], what: str) -> None:
    """Wait for *condition* against a deadline, never a fixed sleep."""
    deadline = time.monotonic() + TIMEOUT_S
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


def submit_and_exit(
    project: Path,
    kind: str,
    name: str,
    argv: tuple[str, ...] = (),
    *,
    wait_for: Path | None = None,
    env: dict[str, str] | None = None,
    worktree: Path | None = None,
) -> str:
    """Submit in a child that exits once *wait_for* exists; return the run id."""
    request = {
        "worktree": str(project if worktree is None else worktree),
        "kind": kind,
        "name": name,
        "argv": list(argv),
    }
    result = run_bounded_child(
        [
            sys.executable,
            "-c",
            SUBMITTER,
            str(booley_dir_of(project)),
            json.dumps(request),
            "" if wait_for is None else str(wait_for),
        ],
        env=os.environ | (env or {}),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def terminal_record(project: Path, run_id: str) -> job_records.JobRecord:
    """Wait for the Job to end and return its final record."""

    def ended() -> bool:
        record = job_records.read_record(run_id, jobs_root_of(project))
        return record is not None and record.status != job_records.STATUS_RUNNING

    poll(ended, f"Job {run_id} to end")
    record = job_records.read_record(run_id, jobs_root_of(project))
    assert record is not None
    return record


def job_log(project: Path, run_id: str) -> str:
    return standalone_log_path(booley_dir_of(project), run_id).read_text(encoding="utf-8")


def dashboard_states(project: Path, run_id: str) -> list[str]:
    """What the Dashboard's Job reader shows for *run_id*."""
    snapshot = JobReader(
        booley_dir_of(project), slots_root=booley_dir_of(project) / "runtime/jobs/slots"
    ).read([])
    return [view.state for view in snapshot.jobs if view.record.run_id == run_id]


@linux_only
def test_flow_outlives_its_submitter(project: Path, ctl: Path) -> None:
    run_id = submit_and_exit(
        project, "flow", "stub_flow", ("--ctl", str(ctl)), wait_for=ctl / "started"
    )

    # The submitter is gone and the Flow is mid-run; its output is already in the live log.
    running = job_records.read_record(run_id, jobs_root_of(project))
    assert running is not None and running.status == job_records.STATUS_RUNNING
    assert running.pid is not None and running.pid != os.getpid()
    assert "flow output line" in job_log(project, run_id)

    (ctl / "release").write_text("1", encoding="utf-8")
    final = terminal_record(project, run_id)
    assert (final.status, final.exit_code) == ("done", 0)
    assert final.pid == running.pid
    assert final.ended_at


@linux_only
def test_specialist_survives_submitter_exit(project: Path, ctl: Path) -> None:
    run_id = submit_and_exit(
        project, "specialist", "stub_specialist", ("--ctl", str(ctl)), wait_for=ctl / "armed"
    )

    def survived_or_ended() -> bool:
        record = job_records.read_record(run_id, jobs_root_of(project))
        return (ctl / "survived").exists() or (
            record is not None and record.status != job_records.STATUS_RUNNING
        )

    poll(survived_or_ended, "the Specialist to outlive its submitter")
    # The watchdog aborts a run whose parent changed; this one is still going.
    assert (ctl / "survived").exists()
    # An unclassed Specialist holds no slot; the record alone must show it running.
    assert dashboard_states(project, run_id) == ["running"]
    (ctl / "release").write_text("1", encoding="utf-8")
    final = terminal_record(project, run_id)
    assert (final.status, final.exit_code) == ("done", 0)


# Sandbox hand-off variables a worker inherits; the tests set only what they assert.
AGENT_ENV_RESET = dict.fromkeys(
    ("BOOLEY_PRIMARY_PROVIDER", "BOOLEY_PRIMARY_AUTH", "BOOLEY_AGENT_APP", "BOOLEY_AGENT_CLIS"), ""
)


def run_stub_specialist(
    project: Path, ctl: Path, *, worktree: Path | None = None, env: dict[str, str] | None = None
) -> tuple[str, dict[str, object]]:
    """Run the stub Specialist to its end; return the run id and what it saw."""
    (ctl / "release").write_text("1", encoding="utf-8")
    run_id = submit_and_exit(
        project,
        "specialist",
        "stub_specialist",
        ("--ctl", str(ctl)),
        wait_for=ctl / "armed",  # the stub arms its watchdog while the submitter is alive
        env=AGENT_ENV_RESET | (env or {}),
        worktree=worktree,
    )
    final = terminal_record(project, run_id)
    assert (final.status, final.exit_code) == ("done", 0), job_log(project, run_id)
    return run_id, json.loads((ctl / "seen.json").read_text(encoding="utf-8"))


@linux_only
def test_specialist_job_uses_worktree_agent_settings(project: Path, ctl: Path) -> None:
    # The Project pins conflicting values: only the worktree's toml may decide.
    (booley_dir_of(project) / "booley.toml").write_text(
        '[agent]\ndefault = "claude"\n[agent.auth]\nclaude = "api_key"\n'
        '[models]\nreviewer = "claude-project-pin"\n',
        encoding="utf-8",
    )
    worktree = project.parent / "worktree"
    tools = worktree / ".booley" / "mcp_tools"
    tools.mkdir(parents=True)
    (tools / "stub_specialist.py").write_text(STUB_SPECIALIST, encoding="utf-8")
    (worktree / ".booley" / "booley.toml").write_text(
        '[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n'
        '[agent.auth]\nclaude = "subscription"\n[models]\nreviewer = "claude-pinned-test"\n',
        encoding="utf-8",
    )

    _, seen = run_stub_specialist(
        project, ctl, worktree=worktree, env={"BOOLEY_AGENT_CLIS": "codex=api_key"}
    )

    assert seen["model"] == "claude-pinned-test"
    assert seen["default_cli"] == "claude"
    assert seen["claude_auth"] == "subscription"
    assert seen["codex_auth"] == "api_key"


@linux_only
def test_specialist_job_gets_a_transcript_dir_and_keeps_its_command(
    project: Path, ctl: Path
) -> None:
    run_id, seen = run_stub_specialist(project, ctl)

    transcripts = (
        booley_dir_of(project) / ".interactive_logs" / "standalone" / run_id / "transcripts"
    )
    assert seen["argv"] == ["--ctl", str(ctl), "--transcript-dir", str(transcripts)]
    record = job_records.read_record(run_id, jobs_root_of(project))
    assert record is not None
    assert record.argv == ["booley", "specialist", "stub_specialist", "--ctl", str(ctl)]
    request = json.loads(
        standalone_request_path(booley_dir_of(project), run_id).read_text(encoding="utf-8")
    )
    assert request["argv"] == ["--ctl", str(ctl)]


@linux_only
def test_worker_environment_has_no_goal_or_session_identity(project: Path, ctl: Path) -> None:
    (ctl / "release").write_text("1", encoding="utf-8")
    run_id = submit_and_exit(
        project, "flow", "stub_flow", ("--ctl", str(ctl)), env=GOAL_AND_SESSION_ENV
    )
    final = terminal_record(project, run_id)

    worker_env = json.loads((ctl / "env.json").read_text(encoding="utf-8"))
    for name in GOAL_AND_SESSION_ENV.keys() - {"BOOLEY_RUN_ID"}:
        assert name not in worker_env
    assert worker_env["BOOLEY_RUN_ID"] == run_id  # its own run, not the submitter's
    assert final.binding is None and final.session_key is None


START_FAILURE_REASONS = {
    "unknown-flow": "no_such_flow",
    "missing-worktree": "worktree is not a directory",
    "bad-kind": "kind must be one of",
    "bad-name": "bad name",
    "bad-models": "names no known Agent CLI",
}


def bad_models_worktree(project: Path) -> Path:
    """A checkout whose ``[models]`` pins a model no Agent CLI owns."""
    worktree = project.parent / "bad-models"
    (worktree / ".booley").mkdir(parents=True)
    (worktree / ".booley" / "booley.toml").write_text(
        '[agent]\ndefault = "claude"\n\n[models]\nreviewer = "bogus"\n', encoding="utf-8"
    )
    return worktree


@linux_only
@pytest.mark.parametrize("case", START_FAILURE_REASONS)
def test_start_failure_leaves_failed_record(project: Path, case: str) -> None:
    kind, name, worktree = {
        "unknown-flow": ("flow", "no_such_flow", project),
        "missing-worktree": ("flow", "stub_flow", project / "gone"),
        "bad-kind": ("bogus", "stub_flow", project),
        "bad-name": ("flow", "bad name", project),
        "bad-models": ("specialist", "stub_specialist", bad_models_worktree(project)),
    }[case]
    run_id = submit_and_exit(project, kind, name, worktree=worktree)
    final = terminal_record(project, run_id)

    assert (final.status, final.exit_code) == ("failed", 2)
    assert final.ended_at
    assert START_FAILURE_REASONS[case] in job_log(project, run_id)
    assert dashboard_states(project, run_id) == ["failed"]


def test_worker_exit_before_initialization_is_failed(tmp_path: Path) -> None:
    from booley.runtime import detached_job

    jobs_root = tmp_path / "jobs"
    log_path = tmp_path / "job.log"
    job_records.write_record(
        job_records.JobRecord("stub-1", "stub", utc_now_rfc3339(), 60), jobs_root
    )

    detached_job._reap_unstarted(
        SimpleNamespace(wait=lambda: 3), log_path, jobs_root, "stub-1", None
    )

    record = job_records.read_record("stub-1", jobs_root)
    assert record is not None
    assert (record.status, record.exit_code) == ("failed", 3)
    assert record.ended_at
    assert "worker exited before initialization (exit 3)" in log_path.read_text(encoding="utf-8")


def test_spawn_failure_leaves_failed_record(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import standalone_worker as worker
    from booley.runtime import detached_job

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(2, "no interpreter")

    monkeypatch.setattr(detached_job.subprocess, "Popen", refuse)
    request = worker.StandaloneRequest(project, "flow", "stub_flow")

    run_id = worker.submit(booley_dir_of(project), request)

    record = job_records.read_record(run_id, jobs_root_of(project))
    assert record is not None
    assert (record.status, record.exit_code) == ("failed", 2)
    assert record.ended_at
    assert "no interpreter" in job_log(project, run_id)


def test_unwritable_log_still_leaves_failed_record(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.harness import standalone_worker as worker

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(13, "log not writable")

    monkeypatch.setattr(worker.detached_job, "spawn", refuse)
    # A directory cannot be opened for append, so the reason line cannot be written.
    monkeypatch.setattr(worker, "standalone_log_path", lambda *_args: tmp_path)

    run_id = worker.submit(booley_dir_of(project), worker.StandaloneRequest(project, "flow", "x"))

    record = job_records.read_record(run_id, jobs_root_of(project))
    assert record is not None
    assert (record.status, record.exit_code) == ("failed", 2)


def test_goal_baseline_launches_the_generic_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime import detached_job

    spawned: list[list[str]] = []

    def popen(command: list[str], **_kwargs: object) -> object:
        spawned.append(command)
        return SimpleNamespace()

    monkeypatch.setattr(detached_job.subprocess, "Popen", popen)
    monkeypatch.setattr(
        detached_job.threading, "Thread", lambda **_kw: SimpleNamespace(start=lambda: None)
    )
    path = tmp_path / "request.json"
    request = {
        "job_id": "baseline-one",
        "booley_dir": str(tmp_path),
        "jobs_root": str(tmp_path / "jobs"),
        "record": {"worktree_path": "/work", "session_key": "codex:one"},
    }
    sim_baseline_jobs._publish_job_record(path, request)
    sim_baseline_jobs.launch_baseline(path, request)

    expected = [
        sys.executable,
        "-m",
        "booley.harness.standalone_worker",
        "goal-baseline",
        str(path),
    ]
    recorded = job_records.read_record("baseline-one", tmp_path / "jobs")
    assert recorded is not None
    assert spawned == [expected]
    assert recorded.argv == expected


def test_worker_rejects_unknown_kinds(capsys: pytest.CaptureFixture[str]) -> None:
    from booley.harness import standalone_worker as worker

    assert worker.main(["unknown", "x"]) == 2
    assert worker.main(["goal-baseline"]) == 2
    assert worker.main(["standalone", "x"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_worker_without_a_record_reports_it(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.harness import standalone_worker as worker

    assert worker.main(["standalone", str(booley_dir_of(project)), "missing-1"]) == 2
    assert "record not found: missing-1" in capsys.readouterr().err


@linux_only
def test_worker_job_queues_in_the_shell_slot_store(project: Path, ctl: Path) -> None:
    slots_root = booley_dir_of(project) / "runtime" / "jobs" / "slots"
    store = job_slots.SlotStore(slots_root, SlotCaps(max_light=1))
    # A shell run holds the only light slot.
    holder = store.acquire(
        job_slots.CLASS_LIGHT, pid=os.getpid(), argv=_proc_cmdline(os.getpid()) or []
    )
    try:
        (ctl / "release").write_text("1", encoding="utf-8")
        run_id = submit_and_exit(project, "flow", "stub_flow", ("--ctl", str(ctl)))
        poll(lambda: dashboard_states(project, run_id) == ["queued"], "the worker Job to queue")
        assert not (ctl / "started").exists()
    finally:
        store.release(holder)

    final = terminal_record(project, run_id)
    assert (final.status, final.exit_code) == ("done", 0)
    assert (ctl / "started").exists()
