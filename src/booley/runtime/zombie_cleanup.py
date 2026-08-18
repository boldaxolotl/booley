"""Kill leftover EDA-tool processes that could lock files.

Kills both native EDA-tool executables and orphaned Booley Flow wrapper processes
(run_yosys_syn.py, etc.) that survive when a parent
session is interrupted.  Child processes are often not killed when the
parent dies (no process group on Windows, detached children on Unix),
so these orphans accumulate.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
from pathlib import Path

# Command-line fragments identifying an orphaned Booley wrapper. The run-halves
# are shipped as ``python3 -m booley.sim.<name>`` (ADR 0019), so their cmdline
# never contains the ``<name>.py`` spelling — matching only on that missed every
# real orphan this module exists to reap, including the verilator_run supervisor
# that survived a timeout kill at 99.9 % CPU for 38 minutes (fpu F-13). Both
# spellings are listed: the module form is what runs today, the script form
# still matches a directly-invoked file.
_SIM_SCRIPT_MARKERS = [
    "booley.sim.verilator_run",
    "booley.sim.iverilog_run",
    "booley.sim.cocotb_run",
    "iverilog_run.py",
    "verilator_run.py",
]
_SYN_SCRIPT_MARKERS = ["booley.yosys.run_yosys_syn", "run_yosys_syn.py"]


def _kill_eda_tools_by_image_name(flow: str) -> None:
    """Kill EDA-tool executables for *flow* by image name (Windows)."""
    eda_tool_processes = {
        "sim": ["xsim.exe", "xelab.exe", "xvlog.exe", "vivado.exe"],
        "syn": ["yosys.exe", "yosys-abc.exe", "abc.exe"],
    }
    for proc in eda_tool_processes.get(flow, []):
        subprocess.run(
            ["taskkill", "/F", "/IM", proc],
            capture_output=True,
            check=False,
        )


def _find_pids_by_marker_windows(marker: str, roots: list[Path]) -> list[int]:
    """Find Python PIDs matching a script marker via PowerShell.

    Returns pid *and* command line so the scope filter can run in Python: a
    marker hit alone says nothing about whose run it is (see
    :func:`_scope_roots`), and Windows has no ``/proc/<pid>/cwd`` to fall back
    on, so the command line is the only evidence available.
    """
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-CimInstance Win32_Process -Filter "
                f"\"Name='python.exe' AND CommandLine LIKE '%{marker}%'\" "
                f'| ForEach-Object {{ "$($_.ProcessId)`t$($_.CommandLine)" }}',
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return []
        pids = []
        for raw_line in result.stdout.splitlines():
            pid_text, _, cmdline = raw_line.strip().partition("\t")
            if not pid_text:
                continue
            if not any(str(root) in cmdline for root in roots):
                continue  # another project's run — never ours to kill
            with contextlib.suppress(ValueError):
                pids.append(int(pid_text))
        return pids
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def _kill_zombies_windows(flow: str) -> None:
    """Windows implementation: taskkill + PowerShell Get-CimInstance."""
    _kill_eda_tools_by_image_name(flow)

    flow_wrapper_markers = {
        "sim": _SIM_SCRIPT_MARKERS,
        "syn": _SYN_SCRIPT_MARKERS,
    }
    my_pid = os.getpid()
    scope_roots = _scope_roots()
    for marker in flow_wrapper_markers.get(flow, []):
        for pid in _find_pids_by_marker_windows(marker, scope_roots):
            if pid == my_pid:
                continue
            logging.getLogger(__name__).info(
                "Zombie cleanup: killing PID %d (marker=%s)",
                pid,
                marker,
            )
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )


def _read_cmdline(pid: int, limit: int = 200) -> str:
    """Read /proc/<pid>/cmdline (Unix only). Returns '' on failure.

    *limit* caps the result for log lines; pass 0 for the full command line
    (:func:`_pid_in_scope` matches paths that sit well past 200 characters).
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        text = raw.replace(b"\x00", b" ").decode(errors="replace").strip()
    except (OSError, ValueError):
        return ""
    return text[:limit] if limit else text


def _parse_ppid(stat: str) -> int | None:
    """Parent PID out of one ``/proc/<pid>/stat`` line; None when unparseable.

    ``comm`` is the one field that may contain spaces *and* parentheses, and it
    is delimited by the LAST ``)`` of the prefix — so the split has to be an
    rsplit on ``)``. Splitting on the first ``") "`` mis-parses a process named
    e.g. ``foo) bar`` and silently drops it, which in :func:`_ancestor_pids`
    means a truncated ancestor chain: the very set that stops the reaper from
    SIGKILLing its own caller.
    """
    try:
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (ValueError, IndexError):
        return None


def _ppid_of(pid: int) -> int | None:
    """Parent PID from ``/proc/<pid>/stat``; None when unreadable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, ValueError):
        return None
    return _parse_ppid(stat)


def _scope_roots() -> list[Path]:
    """Directories a matched process must be running under to be reapable.

    ``pgrep -f booley.sim.verilator_run`` is a HOST-WIDE match with no notion of
    whose run it found. Combined with the descendant walk below that is a loaded
    gun: a bisect in project A would SIGKILL project B's in-flight simulation
    and its entire subtree, because B's supervisor matches the marker
    and is nobody's ancestor. So a marker hit is only a *candidate* — it has to
    be shown to belong to THIS project before anything is signalled.

    Unprovable means "not ours". Failing to reap an orphan costs a busy core;
    reaping a stranger's simulation costs somebody their run.
    """
    roots: list[Path] = []
    for raw in (os.environ.get("BOOLEY_PROJECT_DIR"), str(Path.cwd())):
        if not raw:
            continue
        try:
            resolved = Path(raw).resolve()
        except OSError:  # pragma: no cover - unreadable cwd
            continue
        # "/" would scope to the whole machine, i.e. no scope at all.
        if resolved != resolved.parent and resolved not in roots:
            roots.append(resolved)
    return roots


def _pid_in_scope(pid: int, roots: list[Path]) -> bool:
    """True when *pid* is demonstrably running inside one of *roots* (Unix)."""
    if not roots:
        return False
    try:
        cwd: Path | None = Path(f"/proc/{pid}/cwd").readlink().resolve()
    except OSError:
        cwd = None  # gone, or another user's process — either way, not ours
    cmdline = _read_cmdline(pid, limit=0)
    for root in roots:
        if cwd is not None and (cwd == root or root in cwd.parents):
            return True
        # Run-halves are often handed relative paths, but the Flows that launch
        # them (and any `-C <dir>` style shim) usually name the root outright.
        if str(root) in cmdline:
            return True
    return False


def _descendant_pids(root: int) -> list[int]:
    """PIDs of *root*'s descendants, deepest first (Unix ``/proc`` walk).

    A run-half supervisor starts its simulator in its OWN session
    (``start_new_session``), so killing the supervisor's pid — or even its
    process group — leaves the simulator running and reparented. The orphaned
    ``V<top>`` burning a core after a timeout kill was exactly that (fpu F-13),
    so the reap has to follow the parent links instead.
    """
    children: dict[int, list[int]] = {}
    try:
        entries = [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()]
    except (OSError, ValueError):
        return []
    for pid in entries:
        ppid = _ppid_of(pid)
        if ppid is None:
            continue
        children.setdefault(ppid, []).append(pid)

    ordered: list[int] = []
    frontier = [root]
    while frontier:
        pid = frontier.pop()
        for child in children.get(pid, []):
            ordered.append(child)
            frontier.append(child)
    ordered.reverse()  # deepest first: kill the simulator before its supervisor
    return ordered


def _ancestor_pids() -> set[int]:
    """Collect all ancestor PIDs via /proc/<pid>/stat (Unix only)."""
    ancestors: set[int] = set()
    pid = os.getpid()
    while pid > 1:
        ancestors.add(pid)
        ppid = _ppid_of(pid)
        if ppid is None:
            break
        pid = ppid
    ancestors.add(1)
    return ancestors


def _kill_zombies_unix(flow: str) -> None:
    """Unix implementation: pkill EDA tools, then reap Flow wrappers."""
    import signal as _signal

    # Native EDA-tool process names (no .exe)
    eda_tool_processes = {
        "sim": ["xsim", "xelab", "xvlog", "vivado"],
        "syn": ["yosys", "yosys-abc", "abc"],
    }
    for proc in eda_tool_processes.get(flow, []):
        # -9 SIGKILL, -x exact name match
        subprocess.run(["pkill", "-9", "-x", proc], capture_output=True, check=False)

    flow_wrapper_markers = {
        "sim": _SIM_SCRIPT_MARKERS,
        "syn": _SYN_SCRIPT_MARKERS,
    }
    markers = flow_wrapper_markers.get(flow, [])
    safe_pids = _ancestor_pids()
    scope_roots = _scope_roots()

    for marker in markers:
        try:
            result = subprocess.run(
                ["pgrep", "-f", marker],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode not in (0, 1):  # 1 = no matches
                continue
            for raw_line in result.stdout.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    pid = int(line)
                except ValueError:
                    continue
                if pid in safe_pids:
                    continue
                if not _pid_in_scope(pid, scope_roots):
                    # Another project's (or another user's) run — see _scope_roots.
                    logging.getLogger(__name__).debug(
                        "Zombie cleanup: PID %d matches marker=%s but is not this "
                        "project's run; leaving it alone",
                        pid,
                        marker,
                    )
                    continue
                cmdline = _read_cmdline(pid)
                logging.getLogger(__name__).info(
                    "Zombie cleanup: killing PID %d (marker=%s, cmd=%s)",
                    pid,
                    marker,
                    cmdline,
                )
                # Descendants first: the simulator the wrapper spawned lives in
                # its own session and would otherwise survive its parent's death.
                for child in _descendant_pids(pid):
                    if child in safe_pids:
                        continue
                    with contextlib.suppress(OSError, ProcessLookupError):
                        os.kill(child, _signal.SIGKILL)
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.kill(pid, _signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            logging.getLogger(__name__).debug(
                "Zombie cleanup failed for marker=%s: %s",
                marker,
                exc,
            )


def _is_inside_docker() -> bool:
    """Detect if running inside a container."""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def kill_zombie_flow_processes(flow: str) -> None:
    """Kill leftover processes that could lock files.

    Kills both the EDA-tool executables and orphaned Booley Flow wrapper processes
    (run_yosys_syn.py, etc.) that survive when a parent
    session is interrupted. Child processes are often not killed when the
    parent dies (no process group on Windows, detached children on Unix),
    so these orphans accumulate.

    Skipped inside containers: fresh containers have no zombies, and
    pgrep -f matches the parent developer's command line (which embeds
    script names in the prompt), causing accidental SIGKILL of the
    developer process.

    On the host, a marker hit is only a candidate: it must also be shown to
    belong to THIS project (:func:`_scope_roots`) before anything is signalled,
    so a bisect in one checkout can never reach another session's live run.

    Args:
        flow: ``"sim"`` for simulation EDA tools, ``"syn"`` for synthesis
            EDA tools.
    """
    if _is_inside_docker():
        return
    if sys.platform == "win32":
        _kill_zombies_windows(flow)
    else:
        _kill_zombies_unix(flow)
