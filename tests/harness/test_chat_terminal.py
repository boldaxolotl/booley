"""`booley chat` runs the agent CLI as a forked child with correct terminal handling.

Each test runs the real `chat_cmd.run` in a driver process on a real pty (or a
pipe pair), with a fake agent CLI that records its process identity, counts the
signals it receives and reads one line. A tiny job-control shell stands in for
bash where Ctrl-Z and `fg` matter. Every wait is a `select` or a file marker
against a deadline; the only sleeps are the bounded settles that let a
duplicate signal show up (the absence of an event cannot be awaited).
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import sysconfig
import time
from pathlib import Path
from typing import Any

import pytest

if sys.platform != "linux":
    pytest.skip(
        "pty, /proc process identity and POSIX job control require Linux",
        allow_module_level=True,
    )

import fcntl
import pty

from booley.runtime.chat_sessions import codex_chat_owner
from booley.runtime.handles import HandleStore
from booley.runtime.pid import capture_process_identity
from booley.runtime.session_activity import process_session_key
from tests.execution_test_support import wait_for

_REPO = Path(__file__).parents[2]
_HOOK = _REPO / "src" / "booley" / "data" / "docker" / "session_activity_hook.py"
_WAIT_SECONDS = 15.0
_SETTLE_SECONDS = 0.3
_AGENT_EXIT_CODE = 7

# The fake agent. Runs as the `claude` script and, in a copy of the interpreter
# named `codex`, via `-c`. Its identity (agent.json) is written before AGENT-READY.
_AGENT_SOURCE = r"""
import _thread, importlib.util, json, os, select, signal, subprocess, sys, threading, time
from pathlib import Path

OUT = Path(os.environ["AGENT_OUT"])
VARIANT = os.environ["AGENT_VARIANT"]
KIND = os.environ["AGENT_KIND"]
BOOLEY_DIR = Path(os.environ["AGENT_BOOLEY_DIR"])
DEADLINE = time.monotonic() + 40
SETTLE = 0.5
counts = {"INT": 0, "HUP": 0, "TERM": 0, "CONT": 0}
state = {"first": None, "first_name": None, "resumed": False}


def write_json(name, payload):
    scratch = OUT / (name + ".tmp")
    scratch.write_text(json.dumps(payload))
    os.replace(scratch, OUT / name)


def counter(name):
    def handler(number, frame):
        counts[name] += 1
        if name == "CONT":
            state["resumed"] = True
            try:
                os.write(1, b"AGENT-RESUMED\n")
            except OSError:
                pass
        elif state["first"] is None:
            state["first"], state["first_name"] = time.monotonic(), name
        write_json("counts.json", counts)
    return handler


def wait_until(predicate):
    while not predicate():
        if time.monotonic() > DEADLINE:
            sys.exit(99)
        time.sleep(0.02)


for signame, name in (("SIGINT", "INT"), ("SIGHUP", "HUP"), ("SIGTERM", "TERM"), ("SIGCONT", "CONT")):
    signal.signal(getattr(signal, signame), counter(name))

spec = importlib.util.spec_from_file_location("hook", os.environ["AGENT_HOOK"])
hook = importlib.util.module_from_spec(spec)
sys.modules["hook"] = hook
spec.loader.exec_module(hook)
key = hook._process_key(Path("/proc"), os.getpid())
if KIND == "claude":  # what the MCP server's registry row would be
    sessions = BOOLEY_DIR / "runtime" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / hook._signal_filename(key)).write_text("{}")
hook_run = subprocess.run(
    [os.environ["AGENT_HOOK_PYTHON"], "-I", "-c", os.environ["AGENT_HOOK_PROGRAM"],
     os.environ["AGENT_HOOK"], KIND, str(BOOLEY_DIR)],
    capture_output=True, text=True, timeout=30,
)
cmdline = Path("/proc/self/cmdline").read_bytes().split(b"\0")[:-1]
write_json("agent.json", {
    "pid": os.getpid(), "ppid": os.getppid(), "pgrp": os.getpgrp(),
    "fg": os.tcgetpgrp(0) if os.isatty(0) else None, "cwd": os.getcwd(),
    "cmdline": [part.decode() for part in cmdline], "key": key,
    "hook": json.loads(hook_run.stdout) if hook_run.returncode == 0 else hook_run.stderr,
})
os.write(1, b"AGENT-READY\n")


def trip_when_blocked(main_tid):
    # Self-test only: deliver a SIGINT the way the C handler does (flag the
    # signal, defer the Python handler to the main thread's next eval-breaker
    # check) once the main thread sleeps in the kernel waiting for input. A real
    # kernel signal would interrupt read(2); this one lands "just before" it.
    task = Path("/proc/self/task") / str(main_tid)
    stop = time.monotonic() + 10
    state_name, wchan = "?", "?"
    while time.monotonic() < stop:
        try:
            state_name = (task / "stat").read_text().rsplit(")", 1)[1].split()[0]
            wchan = (task / "wchan").read_text().strip()
        except OSError:
            wchan = "unreadable"
        if state_name == "S" and wchan not in ("0", "", "unreadable") and "futex" not in wchan:
            write_json("trip.json", {"tripped": True, "state": state_name, "wchan": wchan})
            _thread.interrupt_main(signal.SIGINT)
            return
        time.sleep(0.005)
    write_json("trip.json", {"tripped": False, "state": state_name, "wchan": wchan})


if VARIANT == "read-tripped":
    threading.Thread(
        target=trip_when_blocked, args=(threading.get_native_id(),), daemon=True
    ).start()

if VARIANT in ("read", "read-tripped"):
    # Poll for input instead of blocking in read(2): a signal the C handler caught
    # just before the read starts would otherwise defer its Python handler (and
    # the count file) until input arrives. Each poll returns to bytecode, so the
    # handler runs within one poll; the read then finds a line already waiting.
    wait_until(lambda: select.select([0], [], [], 0)[0])
    sys.stdin.readline()
elif VARIANT == "grandchild":
    # Outlives the agent and inherits every descriptor that is not close-on-exec.
    lingering = subprocess.Popen(["sleep", "60"], close_fds=False, start_new_session=True)
    write_json("grandchild.json", {"pid": lingering.pid})
elif VARIANT == "bg":
    wait_until(lambda: state["resumed"])
elif VARIANT == "bg-fg-read":
    wait_until(lambda: state["resumed"] and (OUT / "fg-done").exists())
    sys.stdin.readline()
elif VARIANT == "wait":
    wait_until(lambda: state["first"] is not None)
    wait_until(lambda: time.monotonic() > state["first"] + SETTLE)
    write_json("counts.json", counts)
    if state["first_name"] in ("HUP", "TERM"):
        number = getattr(signal, "SIG" + state["first_name"])
        signal.signal(number, signal.SIG_DFL)
        os.kill(os.getpid(), number)
        time.sleep(30)
sys.exit(7)
"""

# Runs as the hook does (isolated interpreter, hook loaded by path) from a
# grandchild of the agent, so the hook's real ancestor walk is exercised.
_HOOK_PROGRAM = r"""
import importlib.util, json, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("hook", sys.argv[1])
hook = importlib.util.module_from_spec(spec)
sys.modules["hook"] = hook
spec.loader.exec_module(hook)
booley_dir, proc = Path(sys.argv[3]), Path("/proc")
if sys.argv[2] == "claude":
    key = hook._claude_key({}, booley_dir, proc, os.getpid())
else:
    key = hook._codex_chat_key(booley_dir, proc, os.getpid())
print(json.dumps({"key": key}))
"""

# Runs the real `chat_cmd.run` with the Project configuration stubbed out.
_DRIVER_SOURCE = r"""
import json, os, sys
from pathlib import Path
from types import SimpleNamespace

provider = sys.argv[1]
os.chdir(os.environ["DRIVER_ROOT"])  # the shell the user launched `booley chat` from
OUT = Path(os.environ["DRIVER_OUT"])
(OUT / "driver.json").write_text(json.dumps({"pid": os.getpid(), "pgrp": os.getpgrp()}))

from booley.harness import chat_cmd, codex_chat

booley_dir = Path(os.environ["AGENT_BOOLEY_DIR"])
chat_cmd.resolve_checkout_booley_dir = lambda root: booley_dir
chat_cmd.resolve_common_booley_dir = lambda root: booley_dir
codex_chat.resolve_common_booley_dir = lambda root: booley_dir
chat_cmd.load_agent_settings = lambda root, *, booley_dir: None
chat_cmd.get_agent_settings = lambda: SimpleNamespace(default_cli=provider)

real_command = codex_chat._command


def command_running_the_fake(executable):
    # Keep the real validation, handle and --no-daemon; only add the fake's program.
    program, before, after = real_command(executable)
    return program, before, ["-c", os.environ["AGENT_CODEX_PROGRAM"], *after]


codex_chat._command = command_running_the_fake
if provider == "codex":  # scoped to the launch: the driver itself starts normally
    os.environ.update(json.loads(os.environ["DRIVER_CODEX_ENV"]))
sys.stdout.flush()
args = SimpleNamespace(agent_args=[], chat_worktree=os.environ.get("DRIVER_FLAG") or None)
raise SystemExit(chat_cmd.run(args, Path(os.environ["DRIVER_ROOT"])))
"""

# A job-control shell: runs the driver as a foreground job, reacts to its stop.
_SHELL_SOURCE = r"""
import json, os, signal, sys, time
from pathlib import Path

mode, driver, provider = sys.argv[1:4]
OUT = Path(os.environ["DRIVER_OUT"])
signal.signal(signal.SIGTTOU, signal.SIG_IGN)
job = os.fork()
if job == 0:
    os.setpgid(0, 0)
    os.tcsetpgrp(0, os.getpgrp())
    signal.signal(signal.SIGTTOU, signal.SIG_DFL)
    os.execv(sys.executable, [sys.executable, driver, provider])
try:  # both sides set the group, as shells do; EACCES means the job already exec'd
    os.setpgid(job, job)
except PermissionError:
    pass
os.tcsetpgrp(0, job)
stops = 0
while True:
    _, status = os.waitpid(job, os.WUNTRACED)
    if not os.WIFSTOPPED(status):
        break
    stops += 1
    if stops > 1:  # a re-stop is the failure under test
        os.killpg(job, signal.SIGKILL)
        continue
    os.tcsetpgrp(0, os.getpgrp())
    os.write(1, b"SHELL-STOPPED\n")
    if mode == "fg":
        os.tcsetpgrp(0, job)
        os.killpg(job, signal.SIGCONT)
        continue
    os.killpg(job, signal.SIGCONT)  # `bg`
    if mode == "bg-fg":  # a later `fg` of the running job: no SIGCONT
        deadline = time.monotonic() + 30
        while not (OUT / "trigger").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        os.tcsetpgrp(0, job)
        (OUT / "fg-done").write_text("")
owner_is_shell = os.tcgetpgrp(0) == os.getpgrp()
(OUT / "shell.json").write_text(json.dumps({
    "stops": stops, "code": os.waitstatus_to_exitcode(status), "owner_is_shell": owner_is_shell,
}))
"""


class _Terminal:
    """A child started on a fresh pty as session leader, read with deadlines."""

    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        self.pid, self._fd = pty.fork()
        if self.pid == 0:
            os.execve(sys.executable, argv, env)
        self.output = ""
        self._cursor = 0
        self.status: int | None = None
        self.groups: list[int] = []  # process groups to kill on cleanup

    def write(self, text: str) -> None:
        os.write(self._fd, text.encode())

    def expect(self, marker: str) -> None:
        """Wait until *marker* appears in output not yet matched."""
        deadline = time.monotonic() + _WAIT_SECONDS
        while (found := self.output.find(marker, self._cursor)) < 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self._fd], [], [], remaining)[0]:
                raise AssertionError(f"timed out waiting for {marker!r}; saw {self.output!r}")
            try:
                self.output += os.read(self._fd, 4096).decode(errors="replace")
            except OSError as exc:
                raise AssertionError(
                    f"terminal closed before {marker!r}: {self.output!r}"
                ) from exc
        self._cursor = found + len(marker)

    def drain(self) -> None:
        """Read what the exited child left in the terminal."""
        while select.select([self._fd], [], [], 0.5)[0]:
            try:
                chunk = os.read(self._fd, 4096)
            except OSError:  # EIO: the slave side is closed
                break
            if not chunk:
                break
            self.output += chunk.decode(errors="replace")

    def hang_up(self) -> None:
        """Close the master, as closing the terminal window does."""
        os.close(self._fd)
        self._fd = -1

    def wait_exit(self) -> int:
        """Return the child's wait status once it has exited."""

        def reaped() -> bool:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = status
            return bool(pid)

        wait_for(
            reaped, failure=f"child did not exit; saw {self.output!r}", timeout_s=_WAIT_SECONDS
        )
        assert self.status is not None
        return self.status

    def close(self) -> None:
        """Kill everything the test started and reap the child."""
        for group in {self.pid, *self.groups}:
            _kill_group(group)
        if self._fd >= 0:
            os.close(self._fd)
        if self.status is None:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(self.pid, 0)


class _Scene:
    """One test's directories, programs and fake agent CLIs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.out = root / "out"
        self.booley_dir = root / ".booley"
        bin_dir = root / "bin"
        for directory in (self.out, self.booley_dir, bin_dir):
            directory.mkdir()
        self.driver = self._program("driver.py", _DRIVER_SOURCE)
        self.shell = self._program("shell.py", _SHELL_SOURCE)
        self.agent_file = self._program("agent.py", _AGENT_SOURCE)
        claude = bin_dir / "claude"
        claude.write_text(f"#!{sys.executable}\n{_AGENT_SOURCE}")
        claude.chmod(0o755)
        self.codex_env = self._install_codex_copy(bin_dir / "codex")
        self.terminals: list[_Terminal] = []
        self.strays: list[int] = []  # process groups a test left running on purpose
        self.processes: list[subprocess.Popen[bytes]] = []
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "BOOLEY_DIR")}
        env.update(
            PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            PYTHONPATH=os.pathsep.join([str(_REPO / "src"), env.get("PYTHONPATH", "")]),
            DRIVER_OUT=str(self.out),
            DRIVER_ROOT=str(root),
            DRIVER_CODEX_ENV=json.dumps(self.codex_env),
            AGENT_OUT=str(self.out),
            AGENT_BOOLEY_DIR=str(self.booley_dir),
            AGENT_HOOK=str(_HOOK),
            AGENT_HOOK_PYTHON=sys.executable,
            AGENT_HOOK_PROGRAM=_HOOK_PROGRAM,
            AGENT_CODEX_PROGRAM=f"exec(open({str(self.agent_file)!r}).read())",
        )
        self._env = env

    def _program(self, name: str, source: str) -> Path:
        path = self.root / name
        path.write_text(source)
        return path

    def _install_codex_copy(self, target: Path) -> dict[str, str]:
        """Copy the interpreter as `codex`: a native ELF the real launch code recognizes."""
        real = Path(sys.executable).resolve()
        shutil.copy2(real, target)
        prefixes = {sys.base_prefix, sys.base_exec_prefix}
        env = {
            "PYTHONHOME": os.pathsep.join(sorted(prefixes)),
            "LD_LIBRARY_PATH": sysconfig.get_config_var("LIBDIR") or "",
        }
        probe = subprocess.run(
            [str(target), "-c", "pass"],
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            check=False,
        )
        # A Python that cannot be relocated must fail loudly, not skip.
        assert probe.returncode == 0, f"relocated interpreter copy failed: {probe.stderr}"
        return env

    def env(self, variant: str, provider: str, slot: str = "", flag: str = "") -> dict[str, str]:
        """The environment of one launch; its markers go to `out/<slot>` (a second chat's own)."""
        out = self.out / slot
        out.mkdir(exist_ok=True)
        return {
            **self._env,
            "AGENT_VARIANT": variant,
            "AGENT_KIND": provider,
            "DRIVER_OUT": str(out),
            "AGENT_OUT": str(out),
            "DRIVER_FLAG": flag,
        }

    def start(
        self,
        provider: str,
        variant: str = "read",
        *,
        shell: str | None = None,
        slot: str = "",
        flag: str = "",
        ready: bool = True,
    ) -> _Terminal:
        """Start the driver (or the job-control shell around it) and wait for the agent."""
        argv = [sys.executable, str(self.driver), provider]
        if shell is not None:
            argv = [sys.executable, str(self.shell), shell, str(self.driver), provider]
        terminal = _Terminal(argv, self.env(variant, provider, slot, flag))
        self.terminals.append(terminal)
        if ready:
            terminal.expect("AGENT-READY")
            terminal.groups = self._known_groups(slot)
        return terminal

    def _known_groups(self, slot: str = "") -> list[int]:
        return [
            group
            for name in ("driver.json", "agent.json")
            if (group := self.read(name, slot).get("pgrp")) is not None
        ]

    def read(self, name: str, slot: str = "") -> dict[str, Any]:
        path = self.out / slot / name
        return json.loads(path.read_text()) if path.exists() else {}

    def git_project(self, *, hook: str | None = None) -> None:
        """Make the scene's root a Git Project, as `booley chat` finds it in the Sandbox."""
        for args in (
            ("init", "-q"),
            ("config", "user.name", "Test User"),
            ("config", "user.email", "test@example.com"),
            ("commit", "-q", "--allow-empty", "-m", "init"),
        ):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        (self.booley_dir / "booley.toml").write_bytes(b"[submodules]\npaths = []\n")
        if hook is not None:
            (self.booley_dir / "hooks").mkdir()
            (self.booley_dir / "hooks" / "post-setup.py").write_text(hook)

    def worktree_names(self) -> list[str]:
        """The worktrees Booley made, by directory, and by `booley/*` branch, as one list."""
        root = self.booley_dir / "worktrees"
        directories = (
            sorted(p.name for p in root.iterdir() if not p.name.startswith("."))
            if root.is_dir()
            else []
        )
        branches = subprocess.run(
            ["git", "branch", "--list", "booley/*", "--format=%(refname:short)"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        return directories + branches

    def counts(self) -> dict[str, int]:
        return self.read("counts.json")

    def wait_for_count(self, name: str, expected: int) -> None:
        wait_for(
            lambda: self.counts().get(name, 0) >= expected,
            failure=f"agent never counted {expected} x {name}: {self.counts()}",
            timeout_s=_WAIT_SECONDS,
        )

    def cleanup(self) -> None:
        for terminal in self.terminals:
            terminal.close()
        for group in self.strays:
            _kill_group(group)
        for process in self.processes:
            _kill_group(process.pid)
            process.wait(timeout=_WAIT_SECONDS)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()


def _kill_group(group: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, signal.SIGKILL)


@pytest.fixture
def scene(tmp_path: Path):
    scene = _Scene(tmp_path)
    try:
        yield scene
    finally:
        scene.cleanup()


def _assert_forked(scene: _Scene) -> tuple[dict[str, Any], dict[str, Any]]:
    """The agent is Booley's child, leads its own group and holds the terminal."""
    driver, agent = scene.read("driver.json"), scene.read("agent.json")
    assert agent["ppid"] == driver["pid"], "the agent must be a child of Booley, not Booley itself"
    assert agent["pgrp"] == agent["pid"] != driver["pgrp"]
    # The terminal's foreground group names the VS Code tab (`${process}`).
    assert agent["fg"] == agent["pgrp"]
    return driver, agent


def _finish_with_line(terminal: _Terminal) -> None:
    terminal.write("done\n")
    assert os.waitstatus_to_exitcode(terminal.wait_exit()) == _AGENT_EXIT_CODE


def _settle() -> None:
    time.sleep(_SETTLE_SECONDS)


def test_claude_agent_is_the_forked_child_that_owns_the_handle(scene: _Scene) -> None:
    terminal = scene.start("claude")
    _driver, agent = _assert_forked(scene)

    handle = agent["cmdline"][agent["cmdline"].index("-n") + 1]
    # Only the agent holds a handle; Booley's own key is not reserved.
    assert HandleStore(scene.booley_dir).load().entries == {agent["key"]: handle}
    assert agent["hook"]["key"] == agent["key"]
    _finish_with_line(terminal)


def test_codex_agent_is_the_forked_child_that_owns_the_marked_allocation(scene: _Scene) -> None:
    terminal = scene.start("codex")
    _driver, agent = _assert_forked(scene)

    handles = HandleStore(scene.booley_dir).load()
    assert handles.entries == {agent["key"]: agent["cmdline"][0]}
    assert handles.aliases["chat:codex:" + agent["key"]] == agent["key"]
    assert "--no-daemon" in agent["cmdline"]
    assert agent["hook"]["key"] == agent["key"]
    identity = capture_process_identity(agent["pid"])
    assert identity is not None
    owner = codex_chat_owner(scene.booley_dir, identity)
    assert owner is not None
    assert process_session_key(owner) == agent["key"]
    _finish_with_line(terminal)


def test_ctrl_c_reaches_only_the_agent_once(scene: _Scene) -> None:
    terminal = scene.start("claude")
    _assert_forked(scene)

    terminal.write("\x03")
    scene.wait_for_count("INT", 1)
    _settle()

    assert scene.counts()["INT"] == 1
    assert os.waitpid(terminal.pid, os.WNOHANG) == (0, 0), "Booley died of the Ctrl-C"
    _finish_with_line(terminal)


def test_ctrl_z_suspends_and_fg_resumes_the_agent(scene: _Scene) -> None:
    terminal = scene.start("claude", shell="fg")
    _assert_forked(scene)

    terminal.write("\x1a")
    terminal.expect("SHELL-STOPPED")
    terminal.expect("AGENT-RESUMED")
    terminal.write("done\n")
    terminal.wait_exit()

    assert scene.read("shell.json") == {"stops": 1, "code": 7, "owner_is_shell": False}
    assert scene.counts()["CONT"] == 1  # one SIGCONT per resume, not one per relay path


def test_ctrl_z_then_bg_leaves_the_terminal_with_the_shell(scene: _Scene) -> None:
    terminal = scene.start("claude", "bg", shell="bg")
    _assert_forked(scene)

    terminal.write("\x1a")
    terminal.expect("SHELL-STOPPED")
    terminal.expect("AGENT-RESUMED")
    terminal.wait_exit()

    # Booley must not take the terminal back from the shell's prompt on exit.
    assert scene.read("shell.json") == {"stops": 1, "code": 7, "owner_is_shell": True}


def _background_then_foreground(scene: _Scene, terminal: _Terminal) -> None:
    """Ctrl-Z, `bg`, then `fg` of the running job: the shell sends no SIGCONT."""
    terminal.write("\x1a")
    terminal.expect("SHELL-STOPPED")
    terminal.expect("AGENT-RESUMED")  # the parent has left its after-continue branch
    (scene.out / "trigger").write_text("")
    wait_for(
        (scene.out / "fg-done").exists,
        failure="the shell never moved the terminal back to the job",
        timeout_s=_WAIT_SECONDS,
    )


def test_fg_of_a_running_background_job_restores_ctrl_c_to_the_agent(scene: _Scene) -> None:
    terminal = scene.start("claude", "wait", shell="bg-fg")
    _assert_forked(scene)
    _background_then_foreground(scene, terminal)

    terminal.write("\x03")
    terminal.wait_exit()

    assert scene.counts()["INT"] == 1
    assert scene.read("shell.json")["stops"] == 1
    assert scene.read("shell.json")["code"] == _AGENT_EXIT_CODE


def test_fg_of_a_running_background_job_lets_the_agent_read(scene: _Scene) -> None:
    terminal = scene.start("claude", "bg-fg-read", shell="bg-fg")
    _assert_forked(scene)
    _background_then_foreground(scene, terminal)

    terminal.write("done\n")
    terminal.wait_exit()

    # The agent's SIGTTIN stop is repaired by Booley, not reported to the shell.
    assert scene.read("shell.json")["stops"] == 1
    assert scene.read("shell.json")["code"] == _AGENT_EXIT_CODE


def test_ctrl_z_without_job_control_resumes_the_agent(scene: _Scene) -> None:
    # The driver is the session leader: its group is orphaned, so the kernel
    # discards its self-stop and Ctrl-Z must degrade to an immediate resume.
    terminal = scene.start("claude")
    _assert_forked(scene)

    terminal.write("\x1a")
    terminal.expect("AGENT-RESUMED")

    _finish_with_line(terminal)


def test_closing_the_terminal_ends_the_agent_and_propagates_sighup(scene: _Scene) -> None:
    terminal = scene.start("claude", "wait")
    _driver, agent = _assert_forked(scene)

    terminal.hang_up()
    status = terminal.wait_exit()

    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGHUP
    assert scene.counts()["HUP"] == 1
    assert not Path(f"/proc/{agent['pid']}").exists()


def test_sigterm_is_forwarded_once_and_its_status_propagates(scene: _Scene) -> None:
    terminal = scene.start("claude", "wait")
    driver, _agent = _assert_forked(scene)

    os.kill(driver["pid"], signal.SIGTERM)
    status = terminal.wait_exit()

    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGTERM
    assert scene.counts()["TERM"] == 1


def _read_until(stream_fd: int, marker: bytes) -> bytes:
    seen = b""
    deadline = time.monotonic() + _WAIT_SECONDS
    while marker not in seen:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream_fd], [], [], remaining)[0]:
            raise AssertionError(f"timed out waiting for {marker!r}; saw {seen!r}")
        chunk = os.read(stream_fd, 4096)
        if not chunk:
            raise AssertionError(f"stream closed before {marker!r}; saw {seen!r}")
        seen += chunk
    return seen


def test_exit_status_propagates_without_a_terminal(scene: _Scene) -> None:
    process = subprocess.Popen(
        [sys.executable, str(scene.driver), "claude"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=scene.env("read", "claude"),
        process_group=0,
    )
    scene.processes.append(process)
    assert process.stdout is not None
    assert process.stdin is not None
    _read_until(process.stdout.fileno(), b"AGENT-READY")

    # Shared-group mode: the agent is a child of Booley in Booley's own group.
    driver, agent = scene.read("driver.json"), scene.read("agent.json")
    assert agent["ppid"] == driver["pid"] == process.pid, (
        "the agent must be a child of Booley, not Booley itself"
    )
    assert agent["pgrp"] == driver["pgrp"] == process.pid
    assert agent["pid"] != driver["pid"]

    # AGENT-READY is written after the agent installed its handlers, and the
    # shared group was asserted above: the INT cannot beat either.
    os.killpg(process.pid, signal.SIGINT)  # what the kernel does for a tty Ctrl-C
    scene.wait_for_count("INT", 1)
    _settle()
    assert scene.counts()["INT"] == 1
    assert process.poll() is None, "Booley died of a SIGINT meant for the agent"

    process.stdin.write(b"done\n")
    process.stdin.flush()
    assert process.wait(timeout=_WAIT_SECONDS) == _AGENT_EXIT_CODE


def test_fake_agent_counts_a_signal_caught_before_its_input_wait(scene: _Scene) -> None:
    """A handled signal flagged just before the agent blocks must not wait for input."""
    process = subprocess.Popen(
        [sys.executable, scene.agent_file],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=scene.env("read-tripped", "claude"),
        process_group=0,
    )
    scene.processes.append(process)
    assert process.stdout is not None
    assert process.stdin is not None
    _read_until(process.stdout.fileno(), b"AGENT-READY")

    wait_for(
        lambda: "tripped" in scene.read("trip.json"),
        failure="the fake agent never reported whether it tripped a signal",
        timeout_s=_WAIT_SECONDS,
    )
    trip = scene.read("trip.json")
    assert trip["tripped"], (
        f"never saw the agent blocked in a kernel wait (needs /proc wchan): {trip}"
    )

    scene.wait_for_count("INT", 1)
    _settle()
    assert scene.counts()["INT"] == 1
    assert process.poll() is None, "the handler must not end the agent's input wait"

    process.stdin.write(b"done\n")
    process.stdin.flush()
    assert process.wait(timeout=_WAIT_SECONDS) == _AGENT_EXIT_CODE


# ---------------------------------------------------------------------------
# Placement: a second concurrent chat works in the worktree named after its handle
# ---------------------------------------------------------------------------

_THREAD_ID = "019a0000-0000-7000-8000-000000000000"
_SLOW_HOOK = (
    "import pathlib, time\npathlib.Path({marker!r}).write_text('started')\ntime.sleep(60)\n"
)


def _main_lock_is_held(scene: _Scene) -> bool:
    """Whether some process holds the main-checkout lock right now."""
    path = scene.booley_dir / "runtime" / "chat-main.lock"
    if not path.exists():
        return False
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def _tab_handle(agent: dict[str, Any]) -> str:
    """The handle on the tab: Claude's `-n` name, or Codex's first argument."""
    cmdline = agent["cmdline"]
    return cmdline[cmdline.index("-n") + 1] if "-n" in cmdline else cmdline[0]


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_second_concurrent_chat_runs_in_the_worktree_named_after_its_handle(
    scene: _Scene, provider: str
) -> None:
    # Bug caught: the lock never taken (the second launch lands in main), or the worktree and
    # the handle numbered separately (tab, Dashboard and directory disagree).
    scene.git_project()
    first = scene.start(provider)
    second = scene.start(provider, slot="b")
    one, two = scene.read("agent.json"), scene.read("agent.json", "b")

    assert Path(one["cwd"]).resolve() == scene.root.resolve()
    handle = _tab_handle(two)
    assert handle == "S2" and _tab_handle(one) == "S1"
    worktree = scene.booley_dir / "worktrees" / handle.lower()
    assert Path(two["cwd"]).resolve() == worktree.resolve()
    # The Dashboard reads its label by the key the session hook computes for the agent process.
    assert HandleStore(scene.booley_dir).load().labels()[two["hook"]["key"]] == handle
    assert scene.worktree_names() == ["s2", "booley/s2"]
    assert (
        f"Booley session S2 works in worktree {worktree} on branch booley/s2 "
        "(the main checkout is in use by another booley chat)."
    ) in " ".join(second.output.split())
    _finish_with_line(first)
    _finish_with_line(second)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_worktree_session_calls_without_work_dir_route_to_its_worktree(
    scene: _Scene, provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug caught: the agent started in main (or its cwd was not the worktree root), so a call
    # without work_dir would run the main checkout's RTL.
    from booley.mcp import peer_checkout

    scene.git_project()
    first = scene.start(provider)
    second = scene.start(provider, slot="b")
    agent = scene.read("agent.json", "b")
    identity = capture_process_identity(agent["pid"])
    assert identity is not None
    monkeypatch.setattr(peer_checkout, "resolve_common_booley_dir", lambda: scene.booley_dir)
    client = {"client_name": "codex-mcp-client", "metadata": {"threadId": _THREAD_ID}}
    if provider == "claude":
        client = {"client_name": "claude-code", "metadata": {}}

    session = peer_checkout.session_process(identity, proc_root=Path("/proc"), **client)
    assert session is not None
    routed = peer_checkout.process_checkout(
        session, server_cwd=scene.root.resolve(), proc_root=Path("/proc")
    )

    assert routed == (scene.booley_dir / "worktrees" / "s2").resolve()
    _finish_with_line(first)
    _finish_with_line(second)


@pytest.mark.parametrize("interrupt", ["SIGINT", "SIGHUP"])
def test_interrupted_worktree_setup_rolls_back(scene: _Scene, interrupt: str) -> None:
    # Bug caught: a signal during setup leaves worktree s1 and branch booley/s1 behind (and
    # Ctrl-C then dumps a traceback), or lets the agent start in a half-made worktree.
    marker = scene.out / "hook-started"
    scene.git_project(hook=_SLOW_HOOK.format(marker=str(marker)))
    terminal = scene.start("claude", flag="always", ready=False)
    wait_for(marker.exists, failure="the post-setup hook never started", timeout_s=_WAIT_SECONDS)

    if interrupt == "SIGINT":
        terminal.write("\x03")
    else:
        os.kill(scene.read("driver.json")["pid"], signal.SIGHUP)
    code = os.waitstatus_to_exitcode(terminal.wait_exit())
    terminal.drain()

    assert code == 128 + int(getattr(signal, interrupt))
    assert "Worktree setup interrupted; s1 was rolled back." in terminal.output
    assert "Traceback" not in terminal.output
    assert "AGENT-READY" not in terminal.output
    assert scene.worktree_names() == []
    assert scene.read("agent.json") == {}


def test_ended_session_releases_the_lock_despite_a_lingering_grandchild(scene: _Scene) -> None:
    # Bug caught: the main-checkout lock inherited by a process the agent left running, so the
    # next launch is pushed into a worktree long after the session ended.
    scene.git_project()
    first = scene.start("claude", "grandchild")
    assert os.waitstatus_to_exitcode(first.wait_exit()) == _AGENT_EXIT_CODE
    grandchild = scene.read("grandchild.json")["pid"]
    scene.strays.append(grandchild)
    assert Path(f"/proc/{grandchild}").exists(), "the grandchild must outlive the session"

    second = scene.start("claude", slot="b")

    assert Path(scene.read("agent.json", "b")["cwd"]).resolve() == scene.root.resolve()
    assert scene.worktree_names() == []
    assert _main_lock_is_held(scene), "the next launch took main without holding the lock"
    _finish_with_line(second)
