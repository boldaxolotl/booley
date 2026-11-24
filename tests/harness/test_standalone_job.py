"""Shell Flow and Specialist runs are Standalone Jobs: a Job record, a ``job.log``."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from booley.harness import booley as cli
from booley.harness.dashboard.jobs import JobReader
from booley.harness.standalone_job import StandaloneJob, checkout_report_root
from booley.runtime import job_records
from booley.runtime.handles import job_handle_expired
from booley.runtime.pid import DEAD, ProcessIdentity, ProcessObservation
from tests.conftest import symlink_or_skip
from tests.time_budget import hang_guard_s

# Where the Dashboard already scans for Job records: the common Booley Directory's standalone log
# root, with machine-owned records under ``.runtime/jobs`` and each run's output beside them.
STANDALONE_ROOT = Path("job_logs") / "standalone"
JOBS_ROOT = STANDALONE_ROOT / ".runtime" / "jobs"

# Generous bound for a child process or a marker file; only a hang ever reaches it.
CHILD_TIMEOUT_S = 2 * hang_guard_s()
# Each wait on the child is the hang guard and ends before the child's own timeout, so a hang
# fails with the child's output.
CHILD_WAIT_S = hang_guard_s()

# A real Project endpoint file per kind, discovered and loaded by the CLI like a user's own. Its
# ``main`` prints on both streams, records the run id it ran under in ``$STUB_SEEN``, then acts
# on ``--outcome``: an exit code, ``crash``, ``interrupt``, ``usage``, ``clean``, ``message``, or
# ``block`` (sleep ``$STUB_BLOCK_S`` seconds).
STUB = """
import os, sys, time
from pathlib import Path
from {module} import {base}

OUTCOMES = dict(
    crash=RuntimeError("boom"), interrupt=KeyboardInterrupt(), usage=SystemExit(2),
    clean=SystemExit(None), message=SystemExit("msg"),
)

def run(argv):
    print("\\x1b[31mred\\x1b[0m line")
    print("err line", file=sys.stderr, flush=True)
    Path(os.environ["STUB_SEEN"]).write_text(os.environ.get("BOOLEY_RUN_ID", ""), encoding="utf-8")
    outcome = argv[argv.index("--outcome") + 1] if "--outcome" in argv else "0"
    if outcome == "block":
        time.sleep(float(os.environ["STUB_BLOCK_S"]))
        return 0
    if outcome in OUTCOMES:
        raise OUTCOMES[outcome]
    return int(outcome)

class Stub({base}):
    name = "stub"
    description = "Standalone Job fixture"
{abstract}
    def main(self, argv, **_kwargs):
        return run(argv)
"""
KINDS = {
    "flow": (
        "booley.flows.base",
        "BooleyFlow",
        "    def _add_args(self, parser): pass\n    def _run(self): raise AssertionError\n",
    ),
    "specialist": (
        "booley.specialists.specialist",
        "Specialist",
        "    def _build_prompt(self): return ''\n    def _interpret_output(self, *_): return None\n",
    ),
}


def write_project(root: Path, booley_dir: Path) -> None:
    """Fill *booley_dir* with one Flow and one Specialist endpoint, mark *root* a Project, enter it."""
    tools = booley_dir / "mcp_tools"
    tools.mkdir(parents=True)
    for kind, (module, base, abstract) in KINDS.items():
        source = STUB.format(module=module, base=base, abstract=abstract)
        (tools / f"stub_{kind}.py").write_bytes(source.encode("utf-8"))
    (root / ".git").mkdir(exist_ok=True)
    os.chdir(root)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A Project in a Sandbox, with its Booley Directory and a shell run outside any Job."""
    for name in ("BOOLEY_DIR", "BOOLEY_RUN_ID", "RTL_PROJECT_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("_BOOLEY_CLI_INVOCATION_ORIGIN", raising=False)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("BOOLEY_EGRESS", "unproxied")
    monkeypatch.setenv("STUB_SEEN", str(tmp_path / "seen"))
    # The real CLI reads per-user config: keep it away from the developer's own home.
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path / "home"))
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)  # ``main`` replaces it on Ctrl-C
    monkeypatch.chdir(tmp_path)  # restored on teardown, whatever ``write_project`` enters
    root = tmp_path / "proj"
    root.mkdir()
    write_project(root, root / ".booley")
    return root


def booley(monkeypatch, *argv):
    """Run ``booley <argv>`` through the real ``main`` and return its exit code."""
    monkeypatch.setattr(sys, "argv", ["booley", *argv])
    return cli.main()


def prepare(root, endpoint="stub", **kwargs):
    """Prepare a Job for *endpoint* in *root*, as a shell run does."""
    return StandaloneJob.prepare(
        root, endpoint, ["booley", endpoint], report_root=checkout_report_root(root), **kwargs
    )


def records(project):
    return job_records.list_records(project / ".booley" / JOBS_ROOT)


def job_log(project, rec):
    return (project / ".booley" / STANDALONE_ROOT / rec.run_id / "job.log").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("kind", ["flow", "specialist"])
def test_shell_run_writes_standalone_job(monkeypatch, project, kind):
    for outcome in (0, 3):
        assert booley(monkeypatch, kind, "stub", "--outcome", str(outcome)) == outcome
    done, failed = sorted(records(project), key=lambda rec: rec.exit_code)
    for rec, outcome, status in ((done, 0, "done"), (failed, 3, "failed")):
        assert (rec.status, rec.exit_code) == (status, outcome)
        assert rec.session_key is None
        assert rec.endpoint == "stub"
        assert rec.argv == ["booley", kind, "stub", "--outcome", str(outcome)]
        assert rec.work_dir == str(project)
        assert rec.pid == os.getpid()
        assert rec.started_at
        assert rec.ended_at
    assert (project.parent / "seen").read_text(
        encoding="utf-8"
    ) == failed.run_id  # the endpoint ran under its Job's run id
    assert "BOOLEY_RUN_ID" not in os.environ


def test_job_log_copies_output_and_terminal_is_unchanged(monkeypatch, project, capfd):
    monkeypatch.setenv("_BOOLEY_CLI_INVOCATION_ORIGIN", "transport")  # unrecorded baseline
    booley(monkeypatch, "flow", "stub")
    unrecorded = capfd.readouterr()
    assert records(project) == []
    monkeypatch.delenv("_BOOLEY_CLI_INVOCATION_ORIGIN")

    booley(monkeypatch, "flow", "stub")

    assert capfd.readouterr() == unrecorded
    (rec,) = records(project)
    assert (
        job_log(project, rec) == "red line\nerr line\n"
    )  # no ANSI styling, both streams in order


@pytest.mark.parametrize("kind", ["flow", "specialist"])
def test_ctrl_c_records_cancelled_and_reaches_main_as_a_plain_interrupt(
    monkeypatch, project, capsys, kind
):
    """Python 3.11 dies by SIGINT only for the base class: ``main`` re-raises exactly that."""
    with pytest.raises(KeyboardInterrupt) as raised:
        booley(monkeypatch, kind, "stub", "--outcome", "interrupt")
    assert type(raised.value) is KeyboardInterrupt
    assert "BOOLEY_RUN_ID" not in os.environ
    (rec,) = records(project)
    assert (rec.status, rec.exit_code) == ("cancelled", 130)
    assert rec.ended_at

    capsys.readouterr()
    sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
    assert capsys.readouterr().err == ""
    sys.excepthook(RuntimeError, RuntimeError("boom"), None)
    assert "RuntimeError: boom" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("outcome", "raised", "expected"),
    [
        ("crash", RuntimeError, 1),
        ("usage", SystemExit, 2),
        ("clean", SystemExit, 0),
        ("message", SystemExit, 1),
    ],
)
def test_crash_and_exit_are_recorded_and_reraised(monkeypatch, project, outcome, raised, expected):
    with pytest.raises(raised):
        booley(monkeypatch, "flow", "stub", "--outcome", outcome)
    (rec,) = records(project)
    assert rec.exit_code == expected
    assert rec.status == ("done" if expected == 0 else "failed")


@pytest.mark.parametrize("case", ["transport", "nested"])
def test_run_outside_standalone_scope_is_unrecorded(monkeypatch, project, case):
    if case == "transport":
        monkeypatch.setenv("_BOOLEY_CLI_INVOCATION_ORIGIN", "transport")
    else:
        monkeypatch.setenv("BOOLEY_RUN_ID", "sim-outer-1")
    assert booley(monkeypatch, "flow", "stub", "--outcome", "3") == 3
    assert records(project) == []
    assert not (project / ".booley" / "job_logs").exists()


def test_a_project_without_a_booley_directory_is_not_a_job(project, tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert prepare(bare) is None


def test_unwritable_record_warns_and_runs_unrecorded(monkeypatch, project, capfd):
    (project / ".booley" / "job_logs").write_bytes(b"in the way")
    assert booley(monkeypatch, "flow", "stub", "--outcome", "3") == 3
    err = capfd.readouterr().err
    assert "WARN: Standalone Job record unavailable:" in err
    assert "err line" in err
    assert "BOOLEY_RUN_ID" not in os.environ


def test_custom_endpoint_help_is_not_a_job(monkeypatch, project):
    assert booley(monkeypatch, "flow", "stub", "--help") == 0
    assert records(project) == []


CHILD = """
import os, signal, sys, time
from pathlib import Path
from booley.flows.progress_lifecycle import (
    ProgressLifecycle,
    progress_document,
    write_progress_json,
)
from booley.harness.standalone_job import StandaloneJob, checkout_report_root

# A runner that started pytest under nohup passes SIGHUP ignored down; the job
# keeps a signal's existing handler, so start from the default disposition.
signal.signal(signal.SIGHUP, signal.SIG_DFL)

def publish(phase):
    write_progress_json(Path(sys.argv[3]), progress_document(
        flow="stub", run_id=os.environ["BOOLEY_RUN_ID"], phase=phase, targets=["t1", "t2"],
        completed_targets=["t1"], detail=dict(),
    ))

def block():
    with ProgressLifecycle(publish):
        publish("running")
        Path(sys.argv[2]).write_text("ready")  # the handler is installed before the run starts
        time.sleep({timeout})
    return 0

job = StandaloneJob.prepare(
    Path(sys.argv[1]), "stub", ["booley", "flow", "stub", "--target", "t1"], report_root=checkout_report_root(Path(sys.argv[1]))
)
sys.exit(job.run(block))
"""


def run_child_until_signalled(script, args, marker, signum):
    """Run *script* with *args* in a child, signal it once *marker* exists, return ``(exit code, stderr)``."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    for name in ("BOOLEY_DIR", "PYTHONWARNINGS", "PYTHONDEVMODE"):
        env.pop(name, None)  # stderr must hold only what Booley itself writes
    env["STUB_BLOCK_S"] = str(CHILD_TIMEOUT_S)
    child = subprocess.Popen(
        [sys.executable, "-c", script.format(timeout=CHILD_TIMEOUT_S), *map(str, args)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + CHILD_WAIT_S
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not marker.exists():
            child.kill()
            out, err = child.communicate()
            pytest.fail(f"child never reached its run (exit {child.returncode}):\n{out}\n{err}")
        child.send_signal(signum)
        # Kept on subprocess.Popen: test drives the process and signals it
        try:
            _, stderr = child.communicate(timeout=CHILD_WAIT_S)
        except subprocess.TimeoutExpired:
            child.kill()
            out, err = child.communicate()
            pytest.fail(f"child did not exit {CHILD_WAIT_S}s after signal {signum}:\n{out}\n{err}")
        return child.returncode, stderr
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


# The real ``main``, as a shell user runs it, started where a terminal's Ctrl-C finds Python's handler.
CLI_CHILD = """
import signal, sys
from booley.harness.booley import main

# A background runner may pass SIGINT ignored; a terminal Ctrl-C finds Python's handler.
signal.signal(signal.SIGINT, signal.default_int_handler)
signal.signal(signal.SIGHUP, signal.SIG_DFL)
sys.exit(main())
"""

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")


@posix_only
@pytest.mark.parametrize("name", ["sigterm", "sighup", "sigkill"])
def test_signal_leaves_terminal_record(project, tmp_path, name):
    signum = getattr(signal, name.upper())
    progress_path = tmp_path / "reports" / "stub" / "1" / "progress.json"
    returncode, _stderr = run_child_until_signalled(
        CHILD, (project, tmp_path / "ready", progress_path), tmp_path / "ready", signum
    )
    assert returncode == -signum  # dies by the signal
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    (rec,) = records(project)
    if name != "sigkill":
        assert (rec.status, rec.exit_code) == ("cancelled", 128 + signum)
        assert rec.ended_at
        # The process dies without unwinding: the handler publishes the terminal progress.
        assert (progress["phase"], progress["complete"]) == ("aborted", True)
        assert progress["pending_targets"] == ["t2"]
        return
    assert progress["phase"] == "running"  # SIGKILL is out of scope for progress
    # Nothing runs on SIGKILL: the record stays running until someone observes the dead process.
    if sys.platform != "linux":
        pytest.skip("process identity observes /proc")
    assert rec.status == "running"
    (view,) = JobReader(project / ".booley").read().jobs
    assert view.state == "failed"  # the Dashboard sees the dead process

    job = prepare(project)
    assert job is not None
    try:
        assert job.run(lambda: 0) == 0  # the next shell run repairs the dead record
    finally:
        job.discard()
    repaired = job_records.read_record(rec.run_id, project / ".booley" / JOBS_ROOT)
    assert repaired.status == "failed"
    assert repaired.ended_at is None
    assert job_handle_expired(repaired, time.time() + 10 * 365 * 24 * 3600)


def test_cleanup_keeps_a_record_that_finished_after_it_was_listed(monkeypatch, project):
    """A run that ends between the cleanup's listing and its rewrite keeps its own outcome."""
    from booley.harness import standalone_job

    root = project / ".booley" / JOBS_ROOT
    job = prepare(project)
    assert job is not None
    job.discard()
    other = job_records.JobRecord(
        run_id="stub-other-1",
        endpoint="stub",
        started_at="2026-01-01T00:00:00Z",
        timeout_s=60,
        argv=["booley", "flow", "stub"],
        pid=1,
        process_identity=ProcessIdentity(pid=1, identity_scope="test", start_token=7).to_payload(),
    )
    job_records.write_record_strict(other, root)

    def finish_then_die(_identity):
        other.status, other.exit_code = "done", 0  # the other run writes its end, then exits
        job_records.write_record(other, root)
        return ProcessObservation(DEAD)

    monkeypatch.setattr(standalone_job, "observe_process", finish_then_die)
    assert job.run(lambda: 0) == 0
    kept = job_records.read_record("stub-other-1", root)
    assert kept is not None
    assert (kept.status, kept.exit_code) == ("done", 0)


def test_closed_standard_streams_do_not_fail_the_run(monkeypatch, project):
    """With fd 1 or 2 closed, Python leaves the stream None; print() drops silently as before."""
    job = prepare(project)
    assert job is not None
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    try:
        assert job.run(lambda: print("dropped") or 0) == 0
    finally:
        job.discard()
    (rec,) = records(project)
    assert rec.status == "done"


def test_report_root_is_the_resolved_booley_directory(monkeypatch, project, tmp_path):
    """A symlinked ``.booley`` is recorded by its target: that is where the Dashboard looks."""
    real = tmp_path / "elsewhere" / "booley-dir"
    real.mkdir(parents=True)
    root = tmp_path / "linked"
    root.mkdir()
    symlink_or_skip(root / ".booley", real, target_is_directory=True)
    write_project(root, real)
    assert booley(monkeypatch, "flow", "stub") == 0
    (rec,) = job_records.list_records(real / JOBS_ROOT)
    assert rec.report_root == str(real.resolve())


@posix_only
@pytest.mark.parametrize("kind", ["flow", "specialist"])
def test_ctrl_c_prints_one_line_and_dies_by_sigint(project, tmp_path, kind):
    """Ctrl-C: no traceback, one line, and death by SIGINT (not ``exit(130)``) so a caller stops."""
    returncode, stderr = run_child_until_signalled(
        CLI_CHILD, (kind, "stub", "--outcome", "block"), tmp_path / "seen", signal.SIGINT
    )
    assert returncode == -signal.SIGINT, stderr
    assert "Traceback" not in stderr, stderr
    assert "KeyboardInterrupt" not in stderr, stderr
    lines = [line for line in stderr.splitlines() if line and line != "err line"]
    assert lines == ["interrupted; run cancelled"], stderr
    (rec,) = records(project)
    assert (rec.status, rec.exit_code) == ("cancelled", 130)


@pytest.mark.parametrize("inherited", ["outer-run", None])
def test_an_own_run_job_ignores_an_inherited_run_id_and_restores_it(
    monkeypatch, project, inherited
):
    """``own_run`` gives a run inside another Job its own record; the caller's id comes back."""
    if inherited is not None:
        monkeypatch.setenv("BOOLEY_RUN_ID", inherited)
    monkeypatch.setenv("_BOOLEY_CLI_INVOCATION_ORIGIN", "transport")

    job = prepare(project, "measure", own_run=True)
    assert job is not None
    try:
        assert os.environ["BOOLEY_RUN_ID"] == job.run_id != inherited
        assert job.run(lambda: 0) == 0
    finally:
        job.discard()

    assert os.environ.get("BOOLEY_RUN_ID") == inherited
    (rec,) = records(project)
    assert (rec.run_id, rec.endpoint, rec.status) == (job.run_id, "measure", "done")
    assert rec.argv == ["booley", "measure"]


def test_progress_is_recorded_while_the_job_runs_and_never_after_it_ends(project):
    """``record_progress`` rewrites the running record; a finished Job's record stays final."""
    job = prepare(project, "measure")
    assert job is not None
    seen: list[dict[str, int] | None] = []

    def run() -> int:
        job.record_progress(1, 3)
        (running,) = records(project)
        seen.append(running.progress)
        return 0

    try:
        assert job.run(run) == 0
        job.record_progress(2, 3)
    finally:
        job.discard()

    assert seen == [{"finished": 1, "total": 3}]
    (rec,) = records(project)
    assert (rec.status, rec.progress) == ("done", {"finished": 1, "total": 3})
