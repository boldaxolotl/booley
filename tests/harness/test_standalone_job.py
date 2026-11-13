"""Shell Flow and Specialist runs are Standalone Jobs: a Job record, a ``job.log``, no Goal evidence."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from booley.harness import booley as cli
from booley.harness.dashboard.jobs import JobReader
from booley.mcp.registry import McpToolInfo
from booley.runtime import job_records
from booley.runtime.handles import job_handle_expired
from tests.conftest import symlink_or_skip
from tests.goals import conftest as goal_fixtures

layout = goal_fixtures.layout
goal_mode = goal_fixtures.goal_mode

# Where the Dashboard already scans for Job records: the common Booley Directory's standalone log
# root, with machine-owned records under ``.runtime/jobs`` and each run's output beside them.
STANDALONE_ROOT = Path(".interactive_logs") / "standalone"
JOBS_ROOT = STANDALONE_ROOT / ".runtime" / "jobs"

# Generous bound for a child process or a marker file; only a hang ever reaches it.
CHILD_TIMEOUT_S = 60


class StubEndpoint:
    """A Flow or Specialist stand-in that prints on both streams and exits with ``outcome``."""

    name = "stub"
    outcome: object = 0
    seen_run_id: str | None = None

    def main(self, argv):
        type(self).seen_run_id = os.environ.get("BOOLEY_RUN_ID")
        print("\x1b[31mred\x1b[0m line")
        print("err line", file=sys.stderr)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """A shell run starts outside any Job and outside any MCP transport."""
    monkeypatch.delenv("BOOLEY_RUN_ID", raising=False)
    monkeypatch.delenv("_BOOLEY_CLI_INVOCATION_ORIGIN", raising=False)
    StubEndpoint.outcome = 0
    StubEndpoint.seen_run_id = None


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A Project directory with its Booley Directory."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    root = tmp_path / "proj"
    (root / ".booley").mkdir(parents=True)
    return root


def command(argv):
    parser = cli._build_parser()
    return cli._normalize_args(parser, parser.parse_args(argv))


def shell_run(monkeypatch, project, kind, argv=("--target", "t1"), make=StubEndpoint):
    """Run ``booley <kind> stub <argv>`` through the real CLI dispatch with *make* as the endpoint."""
    info = McpToolInfo(name="stub", path="stub.py", description="stub", kind=kind)
    monkeypatch.setattr(cli, "_discover_project_mcp_tools", lambda _root: [info])
    monkeypatch.setattr("booley.mcp.registry.discover_mcp_tools", lambda **_kw: [info])
    monkeypatch.setattr(cli, "_load_mcp_tool_class", lambda _info: make)
    run = cli._cmd_flow if kind == "flow" else cli._cmd_specialist
    return run(command([kind, "stub", *argv]), project)


def records(project):
    return job_records.list_records(project / ".booley" / JOBS_ROOT)


def job_log(project, rec):
    return (project / ".booley" / STANDALONE_ROOT / rec.run_id / "job.log").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("kind", ["flow", "specialist"])
def test_shell_run_writes_standalone_job(monkeypatch, project, kind):
    for outcome in (0, 3):
        StubEndpoint.outcome = outcome
        assert shell_run(monkeypatch, project, kind) == outcome
    done, failed = sorted(records(project), key=lambda rec: rec.exit_code)
    for rec, outcome, status in ((done, 0, "done"), (failed, 3, "failed")):
        assert (rec.status, rec.exit_code) == (status, outcome)
        assert rec.session_key is None
        assert rec.binding is None
        assert rec.endpoint == "stub"
        assert rec.argv == ["booley", kind, "stub", "--target", "t1"]
        assert rec.work_dir == str(project)
        assert rec.pid == os.getpid()
        assert rec.started_at
        assert rec.ended_at
    assert StubEndpoint.seen_run_id == failed.run_id  # the endpoint ran under its Job's run id
    assert "BOOLEY_RUN_ID" not in os.environ


def test_job_log_copies_output_and_terminal_is_unchanged(monkeypatch, project, capfd):
    monkeypatch.setenv("_BOOLEY_CLI_INVOCATION_ORIGIN", "transport")  # unrecorded baseline
    shell_run(monkeypatch, project, "flow")
    unrecorded = capfd.readouterr()
    assert records(project) == []
    monkeypatch.delenv("_BOOLEY_CLI_INVOCATION_ORIGIN")

    shell_run(monkeypatch, project, "flow")

    assert capfd.readouterr() == unrecorded
    (rec,) = records(project)
    assert (
        job_log(project, rec) == "red line\nerr line\n"
    )  # no ANSI styling, both streams in order


def test_ctrl_c_records_cancelled(monkeypatch, project):
    """The interrupt still reaches the caller and the Job is recorded ``cancelled`` / 130."""
    for kind in ("flow", "specialist"):
        StubEndpoint.outcome = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            shell_run(monkeypatch, project, kind)
        assert "BOOLEY_RUN_ID" not in os.environ
    recs = records(project)
    assert len(recs) == 2
    for rec in recs:
        assert (rec.status, rec.exit_code) == ("cancelled", 130)
        assert rec.ended_at


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [(RuntimeError("boom"), 1), (SystemExit(2), 2), (SystemExit(None), 0), (SystemExit("msg"), 1)],
    ids=["crash", "usage-error", "clean-exit", "message-exit"],
)
def test_crash_and_exit_are_recorded_and_reraised(monkeypatch, project, outcome, expected):
    StubEndpoint.outcome = outcome
    with pytest.raises(type(outcome)):
        shell_run(monkeypatch, project, "flow")
    (rec,) = records(project)
    assert rec.exit_code == expected
    assert rec.status == ("done" if expected == 0 else "failed")


@pytest.mark.parametrize("case", ["transport", "nested", "no-booley-dir"])
def test_run_outside_standalone_scope_is_unrecorded(monkeypatch, project, tmp_path, case):
    target = project
    if case == "transport":
        monkeypatch.setenv("_BOOLEY_CLI_INVOCATION_ORIGIN", "transport")
    elif case == "nested":
        monkeypatch.setenv("BOOLEY_RUN_ID", "sim-outer-1")
    else:
        target = tmp_path / "bare"
        target.mkdir()
    StubEndpoint.outcome = 3
    assert shell_run(monkeypatch, target, "flow") == 3
    assert records(project) == []
    assert not (target / ".booley" / ".interactive_logs").exists()


def test_unwritable_record_warns_and_runs_unrecorded(monkeypatch, project, capfd):
    (project / ".booley" / ".interactive_logs").write_bytes(b"in the way")
    StubEndpoint.outcome = 3
    assert shell_run(monkeypatch, project, "flow") == 3
    err = capfd.readouterr().err
    assert "WARN: Standalone Job record unavailable:" in err
    assert "err line" in err
    assert "BOOLEY_RUN_ID" not in os.environ


def test_custom_endpoint_help_is_not_a_job(monkeypatch, project):
    from booley.runtime import runtime_context

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(runtime_context, "container_only_error", lambda _command: None)
    monkeypatch.setattr(runtime_context, "ensure_proxy_env", lambda: None)
    assert shell_run(monkeypatch, project, "flow", argv=("--help",)) == 0
    assert records(project) == []


def test_goal_worktree_shell_run_leaves_goal_evidence_unchanged(goal_mode, monkeypatch):
    def tree(root):
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    before = tree(goal_mode.control / "goals")
    StubEndpoint.outcome = 3
    assert shell_run(monkeypatch, goal_mode.worktree, "flow") == 3

    assert tree(goal_mode.control / "goals") == before
    (rec,) = job_records.list_records(goal_mode.control / JOBS_ROOT)
    assert rec.binding is None
    assert rec.work_dir == str(goal_mode.worktree)


CHILD = """
import signal, sys, time
from pathlib import Path
from booley.harness.standalone_job import StandaloneJob

# A runner that started pytest under nohup passes SIGHUP ignored down; the job
# keeps a signal's existing handler, so start from the default disposition.
signal.signal(signal.SIGHUP, signal.SIG_DFL)

def block():
    Path(sys.argv[2]).write_text("ready")  # the handler is installed before the run starts
    time.sleep({timeout})
    return 0

job = StandaloneJob.prepare(Path(sys.argv[1]), "flow", "stub", ["--target", "t1"])
sys.exit(job.run(block))
"""


def run_child_until_signalled(project, tmp_path, signum, script=CHILD, extra_args=()):
    """Start *script* in a child, signal it once it is blocked, and return ``(exit code, stderr)``."""
    marker = tmp_path / "ready"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    for name in ("BOOLEY_DIR", "PYTHONWARNINGS", "PYTHONDEVMODE"):
        env.pop(name, None)  # stderr must hold only what Booley itself writes
    # The real CLI reads per-user config: keep it away from the developer's own home.
    env["HOME"] = str(tmp_path / "home")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script.format(timeout=CHILD_TIMEOUT_S),
            str(project),
            str(marker),
            *extra_args,
        ],
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + CHILD_TIMEOUT_S
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "child never reached its run"
        child.send_signal(signum)
        # Kept on subprocess.Popen: test drives the process and signals it
        _, stderr = child.communicate(timeout=CHILD_TIMEOUT_S)
        return child.returncode, stderr
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


# The real ``cli.main()`` with discovery stubbed to one blocking endpoint: what a shell user runs.
CLI_CHILD = """
import signal, sys, time
from pathlib import Path
from unittest import mock

from booley.harness import booley as cli
from booley.mcp.registry import McpToolInfo
from booley.runtime import runtime_context

# A background runner may pass SIGINT ignored; a terminal Ctrl-C finds Python's handler.
signal.signal(signal.SIGINT, signal.default_int_handler)
signal.signal(signal.SIGHUP, signal.SIG_DFL)
project, marker, kind = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]


class BlockingEndpoint:
    name = "stub"

    def main(self, argv):
        print("err line", file=sys.stderr, flush=True)
        marker.write_text("ready")
        time.sleep({timeout})
        return 0


info = McpToolInfo(name="stub", path="stub.py", description="stub", kind=kind)
parser = cli._build_parser()
args = cli._normalize_args(parser, parser.parse_args([kind, "stub", "--target", "t1"]))
patches = [
    mock.patch.object(runtime_context, "inside_session_runtime", return_value=True),
    mock.patch.object(runtime_context, "ensure_proxy_env", return_value=False),
    mock.patch.object(cli, "_parse_cli", return_value=args),
    mock.patch.object(cli, "find_project_root", return_value=project),
    mock.patch.object(cli, "_reject_source_project_command", return_value=None),
    mock.patch.object(cli, "_require_command_project", return_value=None),
    mock.patch.object(cli, "_host_install_authority_error", return_value=None),
    mock.patch.object(cli, "_discover_project_mcp_tools", return_value=[info]),
    mock.patch("booley.mcp.registry.discover_mcp_tools", return_value=[info]),
    mock.patch.object(cli, "_load_mcp_tool_class", return_value=BlockingEndpoint),
]
for patch in patches:
    patch.start()
sys.exit(cli.main())
"""

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")


@posix_only
@pytest.mark.parametrize("name", ["sigterm", "sighup", "sigkill"])
def test_signal_leaves_terminal_record(project, tmp_path, name):
    signum = getattr(signal, name.upper())
    returncode, _stderr = run_child_until_signalled(project, tmp_path, signum)
    assert returncode == -signum  # dies by the signal
    (rec,) = records(project)
    if name != "sigkill":
        assert (rec.status, rec.exit_code) == ("cancelled", 128 + signum)
        assert rec.ended_at
        return
    # Nothing runs on SIGKILL: the record stays running until someone observes the dead process.
    if sys.platform != "linux":
        pytest.skip("process identity observes /proc")
    assert rec.status == "running"
    (view,) = JobReader(project / ".booley").read([]).jobs
    assert view.state == "failed"  # the Dashboard sees the dead process

    from booley.harness.standalone_job import StandaloneJob

    job = StandaloneJob.prepare(project, "flow", "stub", [])
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
    from booley.harness.standalone_job import StandaloneJob
    from booley.runtime.pid import DEAD, ProcessIdentity, ProcessObservation

    root = project / ".booley" / JOBS_ROOT
    job = StandaloneJob.prepare(project, "flow", "stub", [])
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
    from booley.harness.standalone_job import StandaloneJob

    job = StandaloneJob.prepare(project, "flow", "stub", [])
    assert job is not None
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    try:
        assert job.run(lambda: print("dropped") or 0) == 0
    finally:
        job.discard()
    (rec,) = records(project)
    assert rec.status == "done"


def test_report_root_is_the_resolved_booley_directory(monkeypatch, tmp_path):
    """A symlinked ``.booley`` is recorded by its target: that is where the Dashboard looks."""
    real = tmp_path / "elsewhere" / "booley-dir"
    real.mkdir(parents=True)
    root = tmp_path / "proj"
    root.mkdir()
    symlink_or_skip(root / ".booley", real, target_is_directory=True)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    assert shell_run(monkeypatch, root, "flow") == 0
    (rec,) = job_records.list_records(real / JOBS_ROOT)
    assert rec.report_root == str(real.resolve())


@posix_only
def test_ctrl_c_prints_one_line_and_dies_by_sigint(tmp_path):
    """Ctrl-C on a shell run: no traceback, one interrupted line, and the process dies by SIGINT.

    Dying by SIGINT (shell ``$?`` 130) rather than ``exit(130)`` keeps a calling shell loop or
    script stopping, as it does for any interrupted program.
    """
    for kind in ("flow", "specialist"):
        project = tmp_path / kind / "proj"
        (project / ".booley").mkdir(parents=True)
        returncode, stderr = run_child_until_signalled(
            project, tmp_path / kind, signal.SIGINT, script=CLI_CHILD, extra_args=(kind,)
        )
        assert returncode == -signal.SIGINT, kind
        assert "Traceback" not in stderr, stderr
        assert "KeyboardInterrupt" not in stderr, stderr
        lines = [line for line in stderr.splitlines() if line and line != "err line"]
        assert lines == ["interrupted; run cancelled"], stderr
        (rec,) = records(project)
        assert (rec.status, rec.exit_code) == ("cancelled", 130)
