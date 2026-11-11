"""Where `booley chat` starts its agent: the main checkout or a worktree named after its handle.

The tests run the real `chat_cmd.run` in-process on a real temporary Git Project: the
forked child is replaced by an inline call and `os.execvp` by a recorder that notes where
the agent would have started. Real forks and terminals are in `test_chat_terminal.py`.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.core import file_lock
from booley.core.file_lock import acquire_file_lock, release_file_lock
from booley.harness import chat_cmd, worktree_cmd
from booley.mcp import session_registry
from booley.mcp.session_registry import Attribution, SessionRegistry
from booley.runtime.handles import HandleStore
from booley.runtime.pid import ProcessIdentity, capture_process_identity
from booley.runtime.session_activity import process_session_key
from tests.subprocess_budget import run_bounded_child

if sys.platform != "linux" or shutil.which("git") is None:
    pytest.skip(
        "booley chat is Sandbox-only: it needs Linux, flock and git", allow_module_level=True
    )

_NAMESPACE = "chat-checkout-test"


class _ExecCalledError(Exception):
    """Stop the launch where the agent would replace the process."""


@dataclass
class _Started:
    """What one launch did: its exit code, where and how the agent would start."""

    code: int | None
    exec_argv: list[str]
    cwd: Path | None
    lock_free: bool | None
    stderr: str


def _git(cwd: Path, *args: str) -> str:
    done = run_bounded_child(["git", *args], cwd=cwd, check=True)
    return done.stdout


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Git Project with a Booley Directory; the process works in its main checkout."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.com")
    (root / "README.md").write_bytes(b"hello\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    (root / ".booley").mkdir()
    _write_toml(root, "")
    monkeypatch.chdir(root)
    monkeypatch.setattr(session_registry, "namespace", lambda: _NAMESPACE)
    return root.resolve()


def _write_toml(project: Path, body: str) -> None:
    (project / ".booley" / "booley.toml").write_bytes(
        ("[submodules]\npaths = []\n" + body).encode()
    )


@pytest.fixture(autouse=True)
def _inline_claude_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the launch in-process as Claude; each launch is a distinct process identity."""
    monkeypatch.setattr(chat_cmd, "run_foreground_child", lambda launch, **_kw: launch())
    monkeypatch.setattr(chat_cmd, "load_agent_settings", lambda _root, *, booley_dir: None)
    monkeypatch.setattr(
        chat_cmd, "get_agent_settings", lambda: SimpleNamespace(default_cli="claude")
    )
    monkeypatch.setattr(shutil, "which", lambda name, mode=None: f"/usr/bin/{name}")
    launches = iter(range(5000, 5100))
    monkeypatch.setattr(
        chat_cmd,
        "capture_process_identity",
        lambda _pid: ProcessIdentity(next(launches), "fixture", 7),
        raising=False,
    )


def _lock_path(project: Path) -> Path:
    return project / ".booley" / "runtime" / "chat-main.lock"


@contextlib.contextmanager
def _lock_held_elsewhere(project: Path):
    """Hold the main-checkout lock as another `booley chat` would."""
    path = _lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        acquire_file_lock(handle)
        try:
            yield
        finally:
            release_file_lock(handle)


def _lock_is_free(project: Path) -> bool | None:
    """Whether the main-checkout lock could be taken right now; ``None`` if it cannot be opened."""
    path = _lock_path(project)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+")
    except OSError:
        return None
    with handle:
        try:
            acquire_file_lock(handle)
        except BlockingIOError:
            return False
        release_file_lock(handle)
        return True


def _start(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys, *, flag: str | None = None
) -> _Started:
    """Run `booley chat` to the point where the agent would start."""
    seen: list[_Started] = []

    def execvp(_executable: str, argv: list[str]) -> None:
        seen.append(_Started(None, argv, Path.cwd().resolve(), _lock_is_free(project), ""))
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execvp)
    args = argparse.Namespace(agent_args=[], chat_worktree=flag)
    capsys.readouterr()
    try:
        code = chat_cmd.run(args, project)
    except _ExecCalledError:
        code = None
    stderr = capsys.readouterr().err
    if seen:
        seen[0].stderr = stderr
        return seen[0]
    return _Started(code, [], None, None, stderr)


def _worktrees(project: Path) -> list[str]:
    root = project / ".booley" / "worktrees"
    return (
        sorted(p.name for p in root.iterdir() if not p.name.startswith("."))
        if root.is_dir()
        else []
    )


def _branches(project: Path) -> list[str]:
    return _git(project, "branch", "--list", "booley/*", "--format=%(refname:short)").split()


def test_worktree_launch_names_the_worktree_after_the_session_handle(project, monkeypatch, capsys):
    # Bug caught: the worktree and the tab handle numbered separately, or no banner.
    with _lock_held_elsewhere(project):
        started = _start(project, monkeypatch, capsys)

    worktree = project / ".booley" / "worktrees" / "s1"
    assert started.cwd == worktree.resolve()
    assert started.exec_argv == ["S1", "-n", "S1"]
    assert list(HandleStore(project / ".booley").load().labels().values()) == ["S1"]
    assert _worktrees(project) == ["s1"]
    assert _branches(project) == ["booley/s1"]
    assert (
        f"Booley session S1 works in worktree {worktree} on branch booley/s1 "
        "(the main checkout is in use by another booley chat).\n"
    ) in started.stderr
    assert started.stderr.rstrip().endswith("Booley is starting the claude interactive agent.")


def test_worktree_launch_waits_for_a_busy_session_writer(project, monkeypatch, capsys):
    # Bug caught: the handle reservation gave up on the first lock contention, so the
    # launch fell back to an unnamed tab (or no worktree) while an MCP upsert held the lock.
    writer = project / ".booley" / "runtime" / "sessions" / ".lock"
    writer.parent.mkdir(parents=True)
    holder = writer.open("a+")
    acquire_file_lock(holder)
    waits: list[float] = []

    def release_on_first_wait(seconds: float) -> None:
        waits.append(seconds)
        release_file_lock(holder)

    monkeypatch.setattr(file_lock, "time", SimpleNamespace(sleep=release_on_first_wait))
    try:
        started = _start(project, monkeypatch, capsys, flag="always")
    finally:
        holder.close()

    assert waits, "the launch never waited for the busy Session writer"
    assert started.cwd == (project / ".booley" / "worktrees" / "s1").resolve()
    assert started.exec_argv == ["S1", "-n", "S1"]


def test_worktree_launch_without_a_handle_creates_nothing(project, monkeypatch, capsys):
    # Bug caught: a worktree made for a session that has no handle to name it.
    def unavailable(*_args, **_kwargs):
        raise OSError("session writer is busy")

    monkeypatch.setattr(chat_cmd, "reserve_session_handle", unavailable, raising=False)

    started = _start(project, monkeypatch, capsys, flag="always")

    assert started.code == 2
    assert started.exec_argv == []
    assert started.stderr == (
        "ERROR: cannot name a worktree for this session: session writer is busy. "
        "Run booley chat --here to use the main checkout.\n"
    )
    assert _worktrees(project) == []
    assert _branches(project) == []


def test_failed_worktree_setup_rolls_back_and_never_starts_the_agent(project, monkeypatch, capsys):
    # Bug caught: the agent started in main (or in a half-made worktree) after a failed setup.
    hook = project / ".booley" / "hooks" / "post-setup.py"
    hook.parent.mkdir()
    hook.write_bytes(b"import sys\nprint('hook failed')\nsys.exit(3)\n")

    started = _start(project, monkeypatch, capsys, flag="always")

    assert started.code == 1
    assert started.exec_argv == []
    assert "hook failed" in started.stderr
    assert "Booley is starting" not in started.stderr
    assert _worktrees(project) == []
    assert _branches(project) == []


def _live_session_in_main(project: Path, *, alive: bool) -> None:
    """A registry row (a plain `claude` tab, say) whose process works in the main checkout."""
    common = project / ".booley"
    if alive:
        identity = capture_process_identity(os.getpid())
    else:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        identity = capture_process_identity(child.pid)
        child.wait()
    assert identity is not None
    key = process_session_key(identity)
    HandleStore(common).reserve(key, "S")
    SessionRegistry(common, quiet_after=3600).upsert(
        Attribution(key, "process", str(project), "key", process=identity, namespace=_NAMESPACE),
        "sim",
        "completed",
        now=time.time(),
    )


@pytest.mark.parametrize("alive", [True, False])
def test_auto_uses_a_worktree_while_a_live_session_works_in_main(
    project, monkeypatch, capsys, alive
):
    # Bug caught: the registry ignored, so `auto` starts a second agent in a busy main checkout;
    # or a dead Session's leftover row sends every launch to a worktree.
    _live_session_in_main(project, alive=alive)

    started = _start(project, monkeypatch, capsys)

    if alive:
        assert started.cwd == (project / ".booley" / "worktrees" / "s2").resolve()
        assert "(Session S1 is working in the main checkout)." in started.stderr
        assert started.lock_free, "auto kept the main-checkout lock though it is not in main"
    else:
        assert started.cwd == project
        assert _worktrees(project) == []
        assert not started.lock_free
        assert "works in worktree" not in started.stderr


def test_always_creates_a_worktree_and_holds_a_free_lock(project, monkeypatch, capsys):
    # Bug caught: `always` starting in main, or leaving the free main-checkout lock untaken.
    _write_toml(project, '[chat]\nworktree = "always"\n')

    started = _start(project, monkeypatch, capsys)

    assert started.cwd == (project / ".booley" / "worktrees" / "s1").resolve()
    assert '([chat] worktree = "always").' in started.stderr
    assert not started.lock_free
    assert _lock_is_free(project), "the lock outlived the launch"


def test_never_and_here_use_main_without_waiting_for_a_held_lock(project, monkeypatch, capsys):
    # Bug caught: `never`/`--here` ignored, not taking the free lock, or waiting for a held one.
    _write_toml(project, '[chat]\nworktree = "never"\n')
    free = _start(project, monkeypatch, capsys)
    assert free.cwd == project
    assert not free.lock_free, "never must take the lock when it is free"

    _write_toml(project, '[chat]\nworktree = "always"\n')
    with _lock_held_elsewhere(project):
        started_at = time.monotonic()
        busy = _start(project, monkeypatch, capsys, flag="never")
    assert busy.cwd == project
    assert time.monotonic() - started_at < 5
    assert "works in worktree" not in busy.stderr
    assert _worktrees(project) == []


def test_new_worktree_flag_overrides_the_never_knob(project, monkeypatch, capsys):
    _write_toml(project, '[chat]\nworktree = "never"\n')

    started = _start(project, monkeypatch, capsys, flag="always")

    assert started.cwd == (project / ".booley" / "worktrees" / "s1").resolve()
    assert "on branch booley/s1 (--new-worktree)." in started.stderr


def test_a_crashed_lock_holder_does_not_block_the_next_launch(project, monkeypatch, capsys):
    # Bug caught: a lock file that outlives its holder (a sentinel, a pid file) blocks main forever.
    lock = _lock_path(project)
    lock.parent.mkdir(parents=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time\n"
            f"h = open({str(lock)!r}, 'a+')\n"
            "fcntl.flock(h, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(60)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == "held\n"
        assert not _lock_is_free(project)
        holder.send_signal(signal.SIGKILL)
        # Kept on subprocess.Popen: test drives the process (reads a line, SIGKILLs it)
        holder.wait(timeout=30)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
        if holder.stdout is not None:
            holder.stdout.close()

    started = _start(project, monkeypatch, capsys)

    assert started.cwd == project
    assert not started.lock_free


def test_launch_from_inside_a_linked_worktree_runs_in_place(project, monkeypatch, capsys):
    # Bug caught: project discovery maps a worktree to main, so a launch inside `s9` took the
    # main-checkout lock or made another worktree; `--new-worktree` there is refused.
    assert worktree_cmd.run(argparse.Namespace(worktree_command="new", name="s9"), project) == 0
    inside = project / ".booley" / "worktrees" / "s9"
    monkeypatch.chdir(inside)
    _write_toml(project, '[chat]\nworktree = "always"\n')

    started = _start(project, monkeypatch, capsys)

    assert started.cwd == inside.resolve()
    assert started.lock_free, "a launch inside a worktree must not take the main-checkout lock"
    assert _worktrees(project) == ["s9"]
    refused = _start(project, monkeypatch, capsys, flag="always")
    assert refused.code == 2
    assert refused.stderr == (
        f"ERROR: booley chat --new-worktree runs only from the main checkout {project}.\n"
    )
    assert _worktrees(project) == ["s9"]


def test_unopenable_lock_still_starts_here_in_main(project, monkeypatch, capsys):
    # Bug caught: storage trouble under runtime/ stopping `--here`, or crashing with a traceback.
    (project / ".booley" / "runtime").write_bytes(b"not a directory\n")

    started = _start(project, monkeypatch, capsys, flag="never")

    assert started.cwd == project
    assert "NOTE: main-checkout lock unavailable:" in started.stderr
    assert "works in worktree" not in started.stderr


def test_unreadable_registry_does_not_send_auto_to_a_worktree(project, monkeypatch, capsys):
    # Bug caught: a malformed [goals] table (read for the quiet window) crashing the launch.
    _write_toml(project, "[goals]\nquiet_after = 1\n")

    started = _start(project, monkeypatch, capsys)

    assert started.cwd == project
    assert _worktrees(project) == []


def test_invalid_worktree_knob_stops_the_launch(project, monkeypatch, capsys):
    _write_toml(project, '[chat]\nworktree = "sometimes"\n')

    started = _start(project, monkeypatch, capsys)

    assert started.code == 2
    assert started.exec_argv == []
    assert started.stderr == (
        "ERROR: booley.toml [chat].worktree must be one of auto, always, never\n"
    )


@pytest.mark.parametrize("interrupt", ["SIGINT", "SIGTERM", "SIGHUP", "KeyboardInterrupt"])
def test_interrupt_during_worktree_setup_reports_one_line_and_restores_signals(
    project, monkeypatch, capsys, interrupt
):
    # Bug caught: SIGTERM/SIGHUP kill the child mid-setup without the rollback, or the handlers
    # installed for the setup stay installed in the agent.
    def interrupted(_project, _name):
        if interrupt == "KeyboardInterrupt":
            raise KeyboardInterrupt
        signal.raise_signal(getattr(signal, interrupt))
        raise AssertionError("the signal did not interrupt the setup")

    monkeypatch.setattr(worktree_cmd, "create_worktree", interrupted)
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}

    started = _start(project, monkeypatch, capsys, flag="always")

    number = signal.SIGINT if interrupt == "KeyboardInterrupt" else getattr(signal, interrupt)
    assert started.code == 128 + int(number)
    assert started.stderr == "Worktree setup interrupted; s1 was rolled back.\n"
    assert {s: signal.getsignal(s) for s in before} == before


def test_a_second_signal_cannot_abort_the_rollback(project, monkeypatch, capsys):
    # Bug caught: a second Ctrl-C or SIGHUP (a closing terminal sends two) lands in the
    # rollback the first one started, and stops it half done.
    def interrupted(_project, _name):
        try:
            signal.raise_signal(signal.SIGHUP)
        finally:
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGINT)

    monkeypatch.setattr(worktree_cmd, "create_worktree", interrupted)

    started = _start(project, monkeypatch, capsys, flag="always")

    assert started.code == 128 + int(signal.SIGHUP)


def test_setup_sees_sigint_held_by_the_launch_window_then_holds_it_again(monkeypatch):
    # Bug caught: the launch window keeps SIGINT blocked until the agent's exec, so a Ctrl-C
    # during worktree setup waited out the creation and then killed the launch, worktree made.
    held = {signal.SIGINT, signal.SIGQUIT}
    outer = signal.pthread_sigmask(signal.SIG_BLOCK, held)
    try:
        with (
            pytest.raises(chat_cmd._SetupInterruptedError),
            chat_cmd._signals_interrupt_setup(),
        ):
            signal.raise_signal(signal.SIGINT)
        assert held <= signal.pthread_sigmask(signal.SIG_BLOCK, [])
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, outer)


def test_an_interrupt_that_leaves_the_worktree_behind_says_so(project, monkeypatch, capsys):
    # Bug caught: "was rolled back" printed after a signal killed the creation script before
    # it could undo its work, leaving s1 and its branch for the user to find.
    def interrupted(root, name):
        worktree_cmd.worktree_path(root, name).mkdir(parents=True)
        signal.raise_signal(signal.SIGHUP)

    monkeypatch.setattr(worktree_cmd, "create_worktree", interrupted)

    started = _start(project, monkeypatch, capsys, flag="always")

    assert started.code == 128 + int(signal.SIGHUP)
    assert "s1 may be left behind" in started.stderr
    assert "was rolled back" not in started.stderr
    assert "branch -D booley/s1" in started.stderr


def test_ctrl_c_while_reserving_the_handle_prints_one_line(project, monkeypatch, capsys):
    # Bug caught: Ctrl-C during the wait for the Session writer lock dumping a traceback.
    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(chat_cmd, "_agent_command", interrupted)

    started = _start(project, monkeypatch, capsys)

    assert started.code == 128 + int(signal.SIGINT)
    assert started.stderr == "Launch interrupted.\n"


def test_a_launcher_that_cannot_carry_a_handle_refuses_a_worktree(project, monkeypatch, capsys):
    # Bug caught: a worktree named after nothing for an agent CLI without tab handles.
    monkeypatch.setattr(
        chat_cmd, "get_agent_settings", lambda: SimpleNamespace(default_cli="aider")
    )

    plain = _start(project, monkeypatch, capsys)
    refused = _start(project, monkeypatch, capsys, flag="always")

    assert plain.exec_argv == ["aider"]
    assert refused.code == 2
    assert refused.stderr == (
        "ERROR: cannot name a worktree for this session: aider cannot carry a handle. "
        "Run booley chat --here to use the main checkout.\n"
    )
    assert _worktrees(project) == []


def test_a_lock_error_other_than_contention_reads_as_not_held(project, monkeypatch, capsys):
    # Bug caught: a full disk or odd filesystem under runtime/ blocking `--here` with a traceback.
    def full(_handle):
        raise OSError(errno.ENOSPC, "no space left")

    monkeypatch.setattr("booley.harness.chat_checkout.acquire_file_lock", full)

    started = _start(project, monkeypatch, capsys, flag="never")

    assert started.cwd == project
    assert "NOTE: main-checkout lock unavailable: [Errno 28] no space left\n" in started.stderr


def test_an_unreadable_git_marker_runs_in_place(project, tmp_path, monkeypatch, capsys):
    # Bug caught: a malformed `.git` file in the launch directory crashing the launch.
    odd = tmp_path / "odd"
    odd.mkdir()
    (odd / ".git").write_bytes(b"garbage\n")
    monkeypatch.chdir(odd)

    started = _start(project, monkeypatch, capsys)

    assert started.cwd == odd.resolve()
    assert started.lock_free
    assert _worktrees(project) == []


# -- launch-time gc -------------------------------------------------------------------

_DAY = 86400


def _idle_worktree(project: Path, name: str, *, days: float | None) -> Path:
    """Register worktree *name* on `booley/<name>`, last used *days* ago (``None``: never)."""
    path = project / ".booley" / "worktrees" / name
    _git(project, "worktree", "add", "-q", "-b", f"booley/{name}", str(path), "HEAD")
    if days is not None:
        stamp = project / ".booley" / "worktrees" / ".last-used" / name
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_bytes(b"")
        then = time.time() - days * _DAY
        os.utime(stamp, (then, then))
    return path


def test_launch_gc_removes_an_idle_clean_worktree(project, monkeypatch, capsys):
    # Bug caught: no `gc` at launch (worktrees pile up), or its removal printed nowhere.
    path = _idle_worktree(project, "s1", days=10)

    started = _start(project, monkeypatch, capsys)

    assert not path.exists()
    assert "Removed worktree s1.\nBranches kept: booley/s1\n" in started.stderr
    assert started.cwd == project
    assert "booley/s1" in _branches(project)


def test_launch_gc_is_quiet_about_kept_worktrees_and_errors(project, monkeypatch, capsys):
    # Bug caught: launch-time `gc` printing `Kept worktree ...`, totals or an error for a
    # recent, a dirty, an unstamped worktree or a bad `gc_older_than`; or blocking the launch.
    recent = _idle_worktree(project, "s1", days=1)
    dirty = _idle_worktree(project, "s2", days=10)
    (dirty / "README.md").write_bytes(b"edited\n")
    unstamped = _idle_worktree(project, "s3", days=None)
    idle = _idle_worktree(project, "s4", days=10)
    starting = "Booley is starting the claude interactive agent.\n"

    started = _start(project, monkeypatch, capsys)

    # Only the removal is printed: no `Kept` lines, no totals.
    assert started.stderr == f"Removed worktree s4.\nBranches kept: booley/s4\n{starting}"
    assert not idle.exists()

    _write_toml(project, '[worktree]\ngc_older_than = "soon"\n')
    started = _start(project, monkeypatch, capsys)

    assert started.stderr == starting
    assert started.cwd == project
    assert all(path.is_dir() for path in (recent, dirty, unstamped))
