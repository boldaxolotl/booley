"""Where `booley chat` starts its agent: the main checkout or a worktree named after its handle.

The tests run the real `chat_cmd.run` in-process on a real temporary Git Project: the
forked child is replaced by an inline call and `os.execvp` by a recorder that notes where
the agent would have started. Real forks and terminals are in `test_chat_terminal.py`.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import re
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
from booley.harness import chat_cmd, codex_chat, worktree_cmd
from booley.mcp import session_registry
from booley.mcp.session_registry import Attribution, SessionRegistry
from booley.runtime.chat_sessions import reserve_codex_chat
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
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    *,
    flag: str | None = None,
    agent_args: list[str] | None = None,
) -> _Started:
    """Run `booley chat` to the point where the agent would start."""
    seen: list[_Started] = []

    def execvp(_executable: str, argv: list[str]) -> None:
        seen.append(_Started(None, argv, Path.cwd().resolve(), _lock_is_free(project), ""))
        raise _ExecCalledError

    monkeypatch.setattr(chat_cmd.os, "execvp", execvp)
    args = argparse.Namespace(agent_args=list(agent_args or []), chat_worktree=flag)
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


def test_stealth_worktree_launch_uses_a_wt_branch(project, monkeypatch, capsys):
    # Bug caught (#3608): Stealth Mode still created booley/s1, a banned term in a branch name.
    _write_toml(project, "[stealth]\nenabled = true\n")
    with _lock_held_elsewhere(project):
        started = _start(project, monkeypatch, capsys)

    worktree = project / ".booley" / "worktrees" / "s1"
    assert _git(project, "branch", "--list", "wt/*", "--format=%(refname:short)").split() == [
        "wt/s1"
    ]
    assert _branches(project) == []
    assert f"works in worktree {worktree} on branch wt/s1 " in started.stderr


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


def _live_session_in_main(project: Path, *, alive: bool, work_dir: Path | None = None) -> None:
    """A registry row (a plain `claude` tab, say) whose process works in *work_dir* (main)."""
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
        Attribution(
            key, "process", str(work_dir or project), "key", process=identity, namespace=_NAMESPACE
        ),
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
    _write_toml(project, '[worktree]\nchat = "always"\n')

    started = _start(project, monkeypatch, capsys)

    assert started.cwd == (project / ".booley" / "worktrees" / "s1").resolve()
    assert '([worktree] chat = "always").' in started.stderr
    assert not started.lock_free
    assert _lock_is_free(project), "the lock outlived the launch"


def test_never_and_here_use_main_without_waiting_for_a_held_lock(project, monkeypatch, capsys):
    # Bug caught: `never`/`--here` ignored, not taking the free lock, or waiting for a held one.
    _write_toml(project, '[worktree]\nchat = "never"\n')
    free = _start(project, monkeypatch, capsys)
    assert free.cwd == project
    assert not free.lock_free, "never must take the lock when it is free"

    _write_toml(project, '[worktree]\nchat = "always"\n')
    with _lock_held_elsewhere(project):
        started_at = time.monotonic()
        busy = _start(project, monkeypatch, capsys, flag="never")
    assert busy.cwd == project
    assert time.monotonic() - started_at < 5
    assert "works in worktree" not in busy.stderr
    assert _worktrees(project) == []


def test_new_worktree_flag_overrides_the_never_knob(project, monkeypatch, capsys):
    _write_toml(project, '[worktree]\nchat = "never"\n')

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
    _write_toml(project, '[worktree]\nchat = "always"\n')

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
    _write_toml(project, '[worktree]\nchat = "sometimes"\n')

    started = _start(project, monkeypatch, capsys)

    assert started.code == 2
    assert started.exec_argv == []
    assert started.stderr == (
        "ERROR: booley.toml [worktree].chat must be one of auto, always, never\n"
    )


def test_invalid_worktree_chat_fails_doctor_validation(tmp_path):
    from booley.audit.diagnostic_results import Findings
    from booley.harness.setup import readiness

    findings = Findings()

    assert not readiness._validate_booley_toml(
        {"worktree": {"chat": "sometimes"}}, tmp_path, findings
    )

    (failure,) = [f for f in findings.report().findings if "[worktree].chat" in f.message]
    assert "[worktree].chat" in failure.fix


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


def test_a_stealth_interrupt_names_the_wt_branch(project, monkeypatch, capsys):
    # Bug caught (#3608): the notice looked for booley/s1 only, so a surviving wt/s1 was
    # reported as "rolled back".
    _write_toml(project, "[stealth]\nenabled = true\n")

    def interrupted(root, name):
        _git(root, "branch", f"wt/{name}")
        signal.raise_signal(signal.SIGHUP)

    monkeypatch.setattr(worktree_cmd, "create_worktree", interrupted)

    started = _start(project, monkeypatch, capsys, flag="always")

    assert started.code == 128 + int(signal.SIGHUP)
    assert "s1 may be left behind" in started.stderr
    assert "was rolled back" not in started.stderr
    assert "branch -D wt/s1" in started.stderr


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


# --- resuming a conversation reopens the worktree it ran in (#2614) ----------------------

# A process that is gone: the earlier session of every recorded conversation.
_OLD_IDENTITY = ProcessIdentity(990001, "fixture", 7)
_OLD_KEY = process_session_key(_OLD_IDENTITY)
_NEW_KEY = process_session_key(ProcessIdentity(5000, "fixture", 7))
_MAIN_MTIME = 1_800_000_000


@dataclass
class _Homes:
    """The CLIs' own storage, redirected into the test's temporary directory."""

    claude: Path
    codex: Path


@pytest.fixture(autouse=True)
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Homes:
    found = _Homes(tmp_path / "claude-home", tmp_path / "codex-home")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(found.claude))
    monkeypatch.setenv("CODEX_HOME", str(found.codex))
    return found


@pytest.fixture
def codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Launch Codex with the real handle reservation, as a native Linux install would."""
    launcher = tmp_path / "bin" / "codex"
    launcher.parent.mkdir()
    launcher.write_bytes(b"native\n")
    monkeypatch.setattr(
        chat_cmd, "get_agent_settings", lambda: SimpleNamespace(default_cli="codex")
    )
    others = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, **kwargs: str(launcher) if name == "codex" else others(name, **kwargs),
    )
    monkeypatch.setattr(codex_chat, "sys", SimpleNamespace(platform="linux", stderr=sys.stderr))
    launches = iter(range(6000, 6100))
    monkeypatch.setattr(
        codex_chat,
        "capture_process_identity",
        lambda _pid: ProcessIdentity(next(launches), "fixture", 7),
    )


def _earlier_session(
    project: Path, *, key: str = _OLD_KEY, client: str = "claude"
) -> tuple[Path, str]:
    """The worktree an earlier `booley chat` made and the handle its agent had."""
    common = project / ".booley"
    if client == "codex":
        label = reserve_codex_chat(common, _OLD_IDENTITY)
    else:
        label = HandleStore(common).reserve(key, "S")
    name = label.lower()
    assert worktree_cmd.run(argparse.Namespace(worktree_command="new", name=name), project) == 0
    return common / "worktrees" / name, label


def _record(
    project: Path,
    client: str,
    conversation: str,
    worktree: Path,
    *,
    key: str | None = _OLD_KEY,
    handle: str | None = None,
    git_stamp: int | None = None,
    cwd: Path | None = None,
) -> None:
    """Write the conversation record the hook leaves when a conversation starts in *worktree*."""
    stamp = git_stamp if git_stamp is not None else (worktree / ".git").stat().st_mtime_ns
    record = {
        "schema": 1,
        "client": client,
        "conversation_id": conversation,
        "cwd": str(cwd or worktree),
        "worktree": worktree.name,
        "handle": handle or worktree.name.upper(),
        "process_key": key,
        "at": "2026-11-11T10:00:00Z",
        "git_stamp": stamp,
    }
    digest = hashlib.sha256(f"{client}:{conversation}".encode()).hexdigest()
    store = project / ".booley" / "worktrees" / ".conversations"
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{digest}.json").write_bytes(json.dumps(record).encode())


def _claude_transcript(homes: _Homes, cwd: Path, conversation: str, mtime: float) -> None:
    """Claude's transcript of *conversation*: one file per directory slug, touched at *mtime*."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    path = homes.claude / "projects" / slug / f"{conversation}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{}\n")
    os.utime(path, (mtime, mtime))


def _codex_rollout(
    homes: _Homes,
    conversation: str,
    cwd: Path,
    *,
    created: int,
    updated: int,
    first_line: bytes | None = None,
) -> None:
    """Codex's rollout: filed under its creation time, touched when the conversation updates."""
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime(created))
    day = time.strftime("%Y/%m/%d", time.gmtime(created))
    path = homes.codex / "sessions" / day / f"rollout-{stamp}-{conversation}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"id": conversation, "cwd": str(cwd), "source": "cli"}
    line = first_line or json.dumps({"type": "session_meta", "payload": meta}).encode()
    path.write_bytes(line + b"\n")
    os.utime(path, (updated, updated))


def _new_launch_label(project: Path) -> str:
    """The handle the resumed launch (the first fixture process) ended up with."""
    return HandleStore(project / ".booley").load().labels()[_NEW_KEY]


@pytest.mark.parametrize("resume", [["--resume", "conv-1"], ["--resume=conv-1"], ["-r", "conv-1"]])
def test_claude_resume_by_id_reopens_the_recorded_worktree_with_its_handle(
    project, monkeypatch, capsys, resume
):
    # Bug caught: a resume from the main checkout started the agent in main (or a fresh
    # worktree), where Claude cannot find the conversation, and under a new handle.
    worktree, label = _earlier_session(project)
    _record(project, "claude", "conv-1", worktree)

    started = _start(project, monkeypatch, capsys, agent_args=resume)

    assert started.cwd == worktree.resolve()
    assert started.exec_argv == [label, "-n", label, *resume]
    assert _new_launch_label(project) == label
    assert started.lock_free, "the main checkout stays free for the next launch"
    assert _worktrees(project) == [worktree.name]
    assert f"Booley session {label} resumes conversation conv-1 in worktree {worktree}.\n" in (
        started.stderr
    )


def test_resume_starts_in_the_directory_the_conversation_had_inside_the_worktree(
    project, monkeypatch, capsys
):
    # Bug caught: a conversation started in a subdirectory resumed at the worktree root.
    worktree, _ = _earlier_session(project)
    (worktree / "sub").mkdir()
    _record(project, "claude", "conv-1", worktree, cwd=worktree / "sub")

    started = _start(project, monkeypatch, capsys, agent_args=["--resume", "conv-1"])

    assert started.cwd == (worktree / "sub").resolve()


@pytest.mark.parametrize("latest", [["-c"], ["--continue"]])
def test_claude_continue_reopens_the_worktree_of_the_most_recent_conversation(
    project, monkeypatch, capsys, homes, latest
):
    # Bug caught: `-c` from main continued main's own conversation although a worktree
    # conversation was more recent, or reopened the worktree without naming its conversation.
    worktree, label = _earlier_session(project)
    _record(project, "claude", "conv-1", worktree)
    _claude_transcript(homes, project, "main-conv", _MAIN_MTIME)
    _claude_transcript(homes, worktree, "conv-1", _MAIN_MTIME + 60)

    started = _start(project, monkeypatch, capsys, agent_args=latest)

    assert started.cwd == worktree.resolve()
    assert started.exec_argv == [label, "-n", label, "--resume", "conv-1"]
    assert _new_launch_label(project) == label


def test_codex_resume_by_id_reopens_the_recorded_worktree_with_its_handle(
    project, monkeypatch, capsys, codex
):
    # Bug caught: a Codex resume got a fresh handle, or its ownership marker pointed at the
    # old key so the new launch no longer owned its tab.
    worktree, label = _earlier_session(project, client="codex")
    _record(project, "codex", "conv-1", worktree)

    started = _start(project, monkeypatch, capsys, agent_args=["resume", "conv-1"])

    assert started.cwd == worktree.resolve()
    assert started.exec_argv == [label, "--no-daemon", "resume", "conv-1"]
    new_key = process_session_key(ProcessIdentity(6000, "fixture", 7))
    handles = HandleStore(project / ".booley").load()
    assert handles.labels()[new_key] == label
    assert handles.aliases["chat:codex:" + new_key] == new_key


def test_codex_resume_last_reopens_the_worktree_of_the_most_recent_conversation(
    project, monkeypatch, capsys, codex, homes
):
    # Bug caught: ordering by creation (the file's name) instead of last update: this
    # conversation was created long before main's but updated after it.
    worktree, label = _earlier_session(project, client="codex")
    _record(project, "codex", "conv-1", worktree)
    _codex_rollout(
        homes, "conv-1", worktree, created=_MAIN_MTIME - 86400, updated=_MAIN_MTIME + 60
    )
    _codex_rollout(homes, "main-conv", project, created=_MAIN_MTIME, updated=_MAIN_MTIME + 30)

    started = _start(project, monkeypatch, capsys, agent_args=["resume", "--last"])

    assert started.cwd == worktree.resolve()
    assert started.exec_argv == [label, "--no-daemon", "resume", "conv-1"]


def test_continue_skips_the_conversation_of_a_running_session(project, monkeypatch, capsys, homes):
    # Bug caught: `-c` reopening a worktree where another agent is running (its transcript is
    # always the newest), instead of the newest conversation the user can continue.
    running = capture_process_identity(os.getpid())
    assert running is not None
    busy, _ = _earlier_session(project, key=process_session_key(running))
    _record(project, "claude", "run-1", busy, key=process_session_key(running))
    _claude_transcript(homes, busy, "run-1", _MAIN_MTIME + 300)
    _claude_transcript(homes, project, "main-conv", _MAIN_MTIME)

    stays = _start(project, monkeypatch, capsys, agent_args=["-c"])

    assert (stays.cwd, stays.exec_argv) == (project, ["S2", "-n", "S2", "-c"])
    free, _ = _earlier_session(project)
    _record(project, "claude", "free-1", free)
    _claude_transcript(homes, free, "free-1", _MAIN_MTIME + 60)

    moves = _start(project, monkeypatch, capsys, agent_args=["-c"])

    assert moves.cwd == free.resolve()
    assert moves.exec_argv[-2:] == ["--resume", "free-1"]


def test_resume_into_an_occupied_worktree_refuses(project, monkeypatch, capsys):
    # Bug caught: a second agent started in a worktree whose session is still running.
    running = capture_process_identity(os.getpid())
    assert running is not None
    busy, label = _earlier_session(project, key=process_session_key(running))
    _record(project, "claude", "conv-1", busy, key=process_session_key(running), handle=label)

    started = _start(project, monkeypatch, capsys, agent_args=["--resume", "conv-1"])

    assert started.code == 2
    assert started.exec_argv == []
    assert started.stderr == (
        f"ERROR: conversation conv-1 belongs to worktree {busy.name}, where session {label} "
        "is still running; end that session first.\n"
    )
    assert _worktrees(project) == [busy.name]


@pytest.mark.parametrize("how", ["removed", "replaced"])
def test_resume_of_a_removed_worktree_starts_in_main_with_a_notice(
    project, monkeypatch, capsys, how
):
    # Bug caught: a record of a deleted worktree sent the resume to a path that is gone,
    # or silently started in main with no word about it.
    worktree, _ = _earlier_session(project)
    _record(project, "claude", "conv-1", worktree)
    stamp = (worktree / ".git").stat().st_mtime_ns
    shutil.rmtree(worktree)
    if how == "replaced":
        _git(project, "worktree", "prune")
        _git(project, "branch", "-D", "booley/s1")
        assert (
            worktree_cmd.run(argparse.Namespace(worktree_command="new", name="s1"), project) == 0
        )
        _record(project, "claude", "conv-1", worktree, git_stamp=stamp + 1)

    started = _start(project, monkeypatch, capsys, agent_args=["--resume", "conv-1"])

    assert started.cwd == project
    assert started.exec_argv[-2:] == ["--resume", "conv-1"]
    assert not started.lock_free
    assert (
        "NOTE: conversation conv-1 ran in worktree s1, which no longer exists; "
        "resuming in the main checkout.\n"
    ) in started.stderr


@pytest.mark.parametrize("case", ["no-record", "main-is-newer"])
def test_resume_of_a_main_checkout_conversation_starts_in_main(
    project, monkeypatch, capsys, homes, case
):
    # Bug caught: `[worktree] chat = "always"` sent a main-checkout conversation to a new
    # worktree, where the CLI cannot find it.
    _write_toml(project, '[worktree]\nchat = "always"\n')
    if case == "no-record":
        arguments = ["--resume", "conv-9"]
    else:
        arguments = ["-c"]
        worktree, _ = _earlier_session(project)
        _record(project, "claude", "conv-1", worktree)
        _claude_transcript(homes, worktree, "conv-1", _MAIN_MTIME)
        _claude_transcript(homes, project, "main-conv", _MAIN_MTIME + 60)
    before = _worktrees(project)

    started = _start(project, monkeypatch, capsys, agent_args=arguments)

    assert started.cwd == project
    assert started.exec_argv[-len(arguments) :] == arguments
    assert not started.lock_free, "main is held for the life of the agent"
    assert _worktrees(project) == before
    assert "resumes conversation" not in started.stderr


def test_resume_into_a_busy_main_checkout_refuses_with_a_here_hint(project, monkeypatch, capsys):
    # Bug caught: today the launch goes to a fresh worktree where the CLI cannot find the
    # conversation, so the resume silently never worked.
    with _lock_held_elsewhere(project):
        started = _start(project, monkeypatch, capsys, agent_args=["--resume", "conv-9"])

    assert started.code == 2
    assert started.exec_argv == []
    assert started.stderr == (
        "ERROR: the main checkout is in use by another booley chat; conversation conv-9 "
        "resumes only there. Run booley chat --here ... to resume it beside that session.\n"
    )
    assert _worktrees(project) == []


def test_resume_into_an_occupied_worktree_without_a_process_key_refuses(
    project, monkeypatch, capsys
):
    # Bug caught: a Codex started by hand (its record has no process key) was invisible, so a
    # resume started a second agent beside it.
    busy, _ = _earlier_session(project)
    _record(project, "claude", "conv-1", busy, key=None)
    _live_session_in_main(project, alive=True, work_dir=busy / "sub")

    started = _start(project, monkeypatch, capsys, agent_args=["--resume", "conv-1"])

    assert started.code == 2
    assert started.exec_argv == []
    assert "where session S2 is still running" in started.stderr


@pytest.mark.parametrize("busy_by", ["lock", "session"])
def test_continue_with_a_busy_main_resumes_the_free_worktree_conversation(
    project, monkeypatch, capsys, homes, busy_by
):
    # Bug caught: the busy main's own (newest) transcript won `-c`, so the refusal hinted
    # `--here -c`, which continues the running session's conversation, and the free worktree
    # conversation could not be reached.
    free, _ = _earlier_session(project)
    _record(project, "claude", "free-1", free)
    _claude_transcript(homes, free, "free-1", _MAIN_MTIME)
    _claude_transcript(homes, project, "main-conv", _MAIN_MTIME + 300)
    if busy_by == "session":
        _live_session_in_main(project, alive=True)
        started = _start(project, monkeypatch, capsys, agent_args=["-c"])
    else:
        with _lock_held_elsewhere(project):
            started = _start(project, monkeypatch, capsys, agent_args=["-c"])

    assert started.cwd == free.resolve()
    assert started.exec_argv[-2:] == ["--resume", "free-1"]


def test_continue_with_a_busy_main_and_no_free_conversation_refuses(project, monkeypatch, capsys):
    # Bug caught: the latest-form refusal wording was never exercised.
    with _lock_held_elsewhere(project):
        started = _start(project, monkeypatch, capsys, agent_args=["-c"])

    assert started.code == 2
    assert started.stderr == (
        "ERROR: the main checkout is in use by another booley chat; the latest conversation "
        "resumes only there. Run booley chat --here ... to resume it beside that session.\n"
    )
    assert _worktrees(project) == []


def test_resume_into_main_with_a_live_session_names_that_session(project, monkeypatch, capsys):
    # Bug caught: the refusal names no occupant when a plain tab (no lock) works in main.
    _live_session_in_main(project, alive=True)

    started = _start(project, monkeypatch, capsys, agent_args=["--resume", "conv-9"])

    assert started.code == 2
    assert "in use by Session S1; conversation conv-9 resumes only there." in started.stderr


def test_never_lets_a_resume_share_the_main_checkout(project, monkeypatch, capsys):
    # Bug caught: the refusal applying to `never`, whose users chose a shared main checkout.
    _write_toml(project, '[worktree]\nchat = "never"\n')
    with _lock_held_elsewhere(project):
        started = _start(project, monkeypatch, capsys, agent_args=["--resume", "conv-9"])

    assert started.cwd == project
    assert started.exec_argv[-2:] == ["--resume", "conv-9"]


@pytest.mark.parametrize("case", ["here", "linked-worktree", "codex-all", "picker"])
def test_resume_routing_leaves_these_launches_as_today(project, monkeypatch, capsys, homes, case):
    # Bug caught: resume routing overriding an explicit flag, a launch inside a worktree, or
    # a form that names no single conversation.
    worktree, _ = _earlier_session(project, client="codex" if case == "codex-all" else "claude")
    if case == "codex-all":
        monkeypatch.setattr(
            chat_cmd, "get_agent_settings", lambda: SimpleNamespace(default_cli="codex")
        )
        _record(project, "codex", "conv-1", worktree)
        _codex_rollout(homes, "conv-1", worktree, created=_MAIN_MTIME, updated=_MAIN_MTIME + 60)
        arguments, flag, expected = ["resume", "--last", "--all"], None, project
        monkeypatch.setattr(codex_chat, "command", lambda *_a, **_k: ("codex", ["codex"], None))
    else:
        _record(project, "claude", "conv-1", worktree)
        arguments, flag, expected = ["--resume", "conv-1"], None, project
    if case == "here":
        flag = "never"
    elif case == "linked-worktree":
        assert (
            worktree_cmd.run(argparse.Namespace(worktree_command="new", name="s9"), project) == 0
        )
        expected = project / ".booley" / "worktrees" / "s9"
        monkeypatch.chdir(expected)
    elif case == "picker":
        arguments = ["--resume"]

    started = _start(project, monkeypatch, capsys, flag=flag, agent_args=arguments)

    assert started.cwd == expected.resolve()
    assert started.exec_argv[-len(arguments) :] == arguments
    assert "resumes conversation" not in started.stderr


def test_codex_last_stays_in_main_when_main_recency_is_unknown(
    project, monkeypatch, capsys, codex, homes
):
    # Bug caught: a record winning because main's newer rollout could not be read; the
    # rewrite to a conversation id would then pick the wrong conversation.
    worktree, _ = _earlier_session(project, client="codex")
    _record(project, "codex", "conv-1", worktree)
    _codex_rollout(homes, "conv-1", worktree, created=_MAIN_MTIME, updated=_MAIN_MTIME)
    oversized = b'{"type":"session_meta","payload":{"cwd":"' + b"a" * (1 << 21) + b'"}}'
    _codex_rollout(
        homes,
        "main-conv",
        project,
        created=_MAIN_MTIME,
        updated=_MAIN_MTIME + 60,
        first_line=oversized,
    )

    started = _start(project, monkeypatch, capsys, agent_args=["resume", "--last"])

    assert started.cwd == project
    assert started.exec_argv[-2:] == ["resume", "--last"]


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
