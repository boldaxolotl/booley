"""Owned scratch directories with conservative, bounded crash recovery."""

from __future__ import annotations

import contextlib
import contextvars
import ctypes
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_NATIVE_RUN = subprocess.run
ROOT_ENV = "BOOLEY_SCRATCH_ROOT"
_MARKER = ".booley-lease.json"
_GUARD = ".booley-lease.lock"
_LIMIT = 128
_MAX_MARKER = 65536
_KINDS = frozenset({"scratch", "codex-home", "specialist", "baseline"})
_ACTIVE: contextvars.ContextVar[tuple[WorkspaceLease, ...]] = contextvars.ContextVar(
    "booley_scratch_leases", default=()
)


def process_identity(pid: int) -> dict[str, Any] | None:
    """Capture Linux boot, namespace and process creation identity, never PID alone."""
    if sys.platform != "linux":
        return None
    try:
        namespace = Path("/proc/self/ns/pid").stat()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {
            "pid": pid,
            "start": int(fields[19]),
            "boot": boot,
            "namespace": [namespace.st_dev, namespace.st_ino],
        }
    except (OSError, ValueError, IndexError):
        return None


def owner_alive(identity: dict[str, Any] | None) -> bool | None:
    """Return unknown when namespace or process identity cannot be established."""
    current = process_identity(os.getpid())
    if not _valid_process_identity(identity) or current is None:
        return None
    if any(identity.get(key) != current[key] for key in ("boot", "namespace")):
        return None
    pid = identity.get("pid")
    if type(pid) is not int or pid <= 0 or type(identity.get("start")) is not int:
        return None
    observed = process_identity(pid)
    if observed is not None:
        return observed == identity
    try:
        Path(f"/proc/{pid}").stat()
    except FileNotFoundError:
        return False
    except OSError:
        pass
    return None


def _directory(path: Path, *, private: bool) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path != path.resolve():
        raise ValueError(f"scratch directory crosses a link: {path}")
    if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError(f"scratch directory is not private: {path}")
    return info


def registry_root() -> Path:
    """Resolve the explicitly scoped registry or a private per-user temporary root."""
    if sys.platform != "linux":
        return _unsupported_registry()
    value = os.environ.get(ROOT_ENV)
    root = Path(value) if value else Path(tempfile.gettempdir()) / f"booley-scratch-{os.getuid()}"
    root = root.absolute()
    parent = _directory(root.parent, private=False)
    if parent.st_uid not in {0, os.getuid()}:
        raise ValueError("scratch registry parent belongs to another user")
    if parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX:
        raise ValueError("scratch registry parent is writable by another user")
    with contextlib.suppress(FileExistsError):
        root.mkdir(mode=0o700)
    _directory(root, private=True)
    return root


def _open_regular(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError(f"scratch metadata is not private regular storage: {path}")
    return fd


@contextlib.contextmanager
def _exclusion(parent: Path) -> Iterator[None]:
    import fcntl

    fd = _open_regular(parent / ".booley-registry.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_marker(root: Path) -> tuple[dict[str, Any], tuple[int, int]]:
    fd = _open_regular(root / _MARKER, os.O_RDONLY)
    try:
        info = os.fstat(fd)
        if info.st_size > _MAX_MARKER:
            raise ValueError("oversized scratch marker")
        data = json.loads(os.read(fd, _MAX_MARKER + 1))
        if not isinstance(data, dict):
            raise ValueError("scratch marker is not an object")
        return data, (info.st_dev, info.st_ino)
    finally:
        os.close(fd)


def _valid_process_identity(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"pid", "start", "boot", "namespace"}
        and type(value["pid"]) is int
        and value["pid"] > 0
        and type(value["start"]) is int
        and value["start"] >= 0
        and isinstance(value["boot"], str)
        and len(value["boot"]) == 36
        and isinstance(value["namespace"], list)
        and len(value["namespace"]) == 2
        and all(type(number) is int and number >= 0 for number in value["namespace"])
    )


def _valid_launch_state(value: Any) -> bool:
    if value in ({"state": "pending"}, {"state": "complete"}):
        return True
    return (
        isinstance(value, dict)
        and set(value) == {"state", "supervisor"}
        and value["state"] == "running"
        and _valid_process_identity(value["supervisor"])
    )


def _validated_marker(root: Path) -> tuple[dict[str, Any], tuple[int, int]]:
    parent = _directory(root.parent, private=False)
    info = _directory(root, private=True)
    data, marker_id = _read_marker(root)
    expected = {"schema", "kind", "lease", "uid", "parent", "root", "owner", "launches", "guard"}
    if data.get("kind") == "baseline":
        expected.add("baseline")
    if (
        set(data) != expected
        or type(data["schema"]) is not int
        or data["schema"] != 1
        or data["kind"] not in _KINDS
    ):
        raise ValueError("unknown scratch marker schema or kind")
    if (
        type(data["uid"]) is not int
        or data["uid"] != os.getuid()
        or data["parent"] != [parent.st_dev, parent.st_ino]
        or data["root"] != [info.st_dev, info.st_ino]
        or not isinstance(data["lease"], str)
        or len(data["lease"]) != 32
        or not _valid_process_identity(data["owner"])
        or not isinstance(data["launches"], dict)
        or len(data["launches"]) > 64
        or any(not _valid_launch_state(state) for state in data["launches"].values())
        or parent.st_uid not in {0, os.getuid()}
        or data["guard"] != [(root / _GUARD).lstat().st_dev, (root / _GUARD).lstat().st_ino]
        or (root / _GUARD).is_symlink()
    ):
        raise ValueError("scratch marker identity mismatch")
    return data, marker_id


def _write_marker(root: Path, data: dict[str, Any]) -> None:
    payload = json.dumps(data, separators=(",", ":")).encode()
    if len(payload) > _MAX_MARKER:
        raise ValueError("scratch marker exceeds its size bound")
    destination = root / _MARKER
    fd = _open_regular(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
    except BaseException:
        raise


def _delete_owned(root: Path, *, require_dead: bool) -> bool:
    import fcntl

    with _exclusion(root.parent):
        data, marker_id = _validated_marker(root)
        if any(value != {"state": "complete"} for value in data["launches"].values()):
            return False
        if require_dead and owner_alive(data["owner"]) is not False:
            return False
        fd = _open_regular(root / _GUARD, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
    try:
        if data["kind"] == "baseline":
            from booley.flows.baseline_worktree import cleanup_owned_baseline

            with inherited_workspace_guard(root, fd):
                if not cleanup_owned_baseline(root, data, _BASELINE_PROJECT.get()):
                    return False
        return _claim_and_remove(root, data, marker_id)
    finally:
        os.close(fd)


def _claim_and_remove(root: Path, data: dict[str, Any], marker_id: tuple[int, int]) -> bool:
    with _exclusion(root.parent):
        current, current_id = _validated_marker(root)
        if current != data or current_id != marker_id:
            return False
        if not shutil.rmtree.avoids_symlink_attacks:
            logger.warning("Retaining scratch: safe recursive deletion unavailable: %s", root)
            return False
        claimed = root.with_name(".booley-reaping-" + uuid.uuid4().hex)
        root.rename(claimed)
        try:
            claimed_data, claimed_marker = _validated_marker(claimed)
            if claimed_data != data or claimed_marker != marker_id:
                raise ValueError("scratch root changed while claiming deletion")
        except (OSError, ValueError, TypeError):
            if not root.exists() and not root.is_symlink():
                claimed.rename(root)
            raise
    shutil.rmtree(claimed)
    return True


@contextlib.contextmanager
def inherited_workspace_guard(root: Path, fd: int) -> Iterator[None]:
    """Carry an already claimed root through cleanup child supervision."""
    token = _ACTIVE.set((*_ACTIVE.get(), WorkspaceLease(root, fd)))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


_BASELINE_PROJECT: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "booley_scratch_baseline_project", default=None
)


@contextlib.contextmanager
def baseline_project_scope(project_root: Path) -> Iterator[None]:
    """Supply trusted current Project authority independently of marker content."""
    token = _BASELINE_PROJECT.set(project_root.resolve())
    try:
        yield
    finally:
        _BASELINE_PROJECT.reset(token)


def register_baseline(root: Path, metadata: dict[str, Any]) -> None:
    """Publish trusted baseline repository identities before any worktree add."""
    if any(lease.root == root and lease.guard_fd < 0 for lease in _ACTIVE.get()):
        data = json.loads((root / _MARKER).read_text())
        data["baseline"] = metadata
        (root / _MARKER).write_text(json.dumps(data), encoding="utf-8")
        return
    with _exclusion(root.parent):
        data, _identity = _validated_marker(root)
        if data["kind"] != "baseline":
            raise ValueError("baseline metadata requires the baseline scratch kind")
        data["baseline"] = metadata
        _write_marker(root, data)


def sweep_stale_workspaces(*, parent: Path | None = None) -> None:
    """Sweep one trusted direct-child namespace; preserve every ambiguous artifact."""
    if sys.platform != "linux":
        logger.warning("Retaining crash scratch: process ownership proof requires Linux")
        return
    try:
        selected = registry_root() if parent is None else parent.absolute()
        _directory(selected, private=parent is None)
        _sweep_entries(selected)
    except (OSError, ValueError) as exc:
        logger.warning("Scratch cleanup preserved uncertain ownership: %s", exc)


class _LinuxDirent(ctypes.Structure):
    _fields_ = [
        ("inode", ctypes.c_uint64),
        ("offset", ctypes.c_int64),
        ("record_length", ctypes.c_ushort),
        ("kind", ctypes.c_ubyte),
        ("name", ctypes.c_char * 256),
    ]


def _scan_cookie(parent: Path) -> int:
    path = parent / ".booley-registry.cursor"
    try:
        fd = _open_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        return 0
    try:
        if os.fstat(fd).st_size > 1024:
            raise ValueError("oversized scratch cursor")
        data = json.loads(os.read(fd, 1025))
        info = parent.stat()
        if (
            not isinstance(data, dict)
            or set(data) != {"schema", "parent", "offset"}
            or data["schema"] != 1
            or data["parent"] != [info.st_dev, info.st_ino]
            or type(data["offset"]) is not int
            or not 0 <= data["offset"] < 2**63
        ):
            raise ValueError("unknown scratch cursor identity")
        return data["offset"]
    finally:
        os.close(fd)


def _scan_batch(parent: Path, cookie: int) -> tuple[list[str], int]:
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ValueError("bounded directory cursor requires supported Linux 64-bit dirent")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.opendir.argtypes, libc.opendir.restype = [ctypes.c_char_p], ctypes.c_void_p
    libc.readdir.argtypes, libc.readdir.restype = [ctypes.c_void_p], ctypes.POINTER(_LinuxDirent)
    libc.seekdir.argtypes = [ctypes.c_void_p, ctypes.c_long]
    libc.closedir.argtypes = [ctypes.c_void_p]
    stream = libc.opendir(os.fsencode(parent))
    if not stream:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    names: list[str] = []
    try:
        libc.seekdir(stream, cookie)
        for _ in range(_LIMIT):
            ctypes.set_errno(0)
            entry = libc.readdir(stream)
            if not entry:
                error = ctypes.get_errno()
                if error:
                    raise OSError(error, os.strerror(error))
                return names, 0
            value = entry.contents
            cookie = value.offset
            name = os.fsdecode(bytes(value.name))
            if name.startswith(("booley-owned-", ".booley-reaping-")):
                names.append(name)
        return names, cookie
    finally:
        libc.closedir(stream)


def _sweep_entries(parent: Path) -> None:
    with _exclusion(parent):
        names, cookie = _scan_batch(parent, _scan_cookie(parent))
        info = parent.stat()
        cursor = {"schema": 1, "parent": [info.st_dev, info.st_ino], "offset": cookie}
        fd = _open_regular(
            parent / ".booley-registry.cursor", os.O_CREAT | os.O_WRONLY | os.O_TRUNC
        )
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(cursor, output)
    for name in names:
        try:
            _delete_owned(parent / name, require_dead=True)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Retaining uncertain scratch %s: %s", parent / name, exc)


@dataclass(frozen=True)
class WorkspaceLease:
    """One marked payload and its inherited shared flock description."""

    root: Path
    guard_fd: int


def active_guard_fds() -> tuple[int, ...]:
    """Return plain descriptors for explicit propagation across infrastructure seams."""
    return tuple(lease.guard_fd for lease in _ACTIVE.get() if lease.guard_fd >= 0)


@contextlib.contextmanager
def temporary_workspace(
    *, prefix: str = "scratch-", parent: str | Path | None = None, kind: str = "scratch"
) -> Iterator[str]:
    """Allocate private marked scratch; normal cleanup also respects child guards."""
    if kind not in _KINDS:
        raise ValueError(f"unknown scratch kind: {kind}")
    if sys.platform != "linux" or process_identity(os.getpid()) is None:
        with _unsupported_workspace(prefix, parent, kind) as root:
            yield root
        return
    parent = registry_root() if parent is None else Path(parent).absolute()
    sweep_stale_workspaces(parent=parent)
    lease = _allocate(parent, prefix, kind)
    token = _ACTIVE.set((*_ACTIVE.get(), lease))
    try:
        yield str(lease.root)
    finally:
        _ACTIVE.reset(token)
        os.close(lease.guard_fd)
        try:
            _delete_owned(lease.root, require_dead=False)
        except (OSError, ValueError) as exc:
            logger.warning("Scratch cleanup retained %s: %s", lease.root, exc)


def _unsupported_registry() -> Path:
    active = _ACTIVE.get()
    if active:
        return active[0].root.parent
    scoped = os.environ.get(ROOT_ENV)
    if scoped:
        root = Path(scoped).absolute()
        if root != root.resolve() or root.is_symlink():
            raise ValueError("scratch registry crosses a link")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return root
    return Path(tempfile.mkdtemp(prefix="booley-scratch-registry-"))


@contextlib.contextmanager
def _unsupported_workspace(prefix: str, parent: str | Path | None, kind: str) -> Iterator[str]:
    selected = registry_root() if parent is None else Path(parent).absolute()
    root = Path(tempfile.mkdtemp(prefix="booley-owned-" + prefix, dir=selected))
    info, parent_info = root.stat(), selected.stat()
    marker = {
        "schema": 1,
        "kind": kind,
        "lease": uuid.uuid4().hex,
        "uid": info.st_uid,
        "parent": [parent_info.st_dev, parent_info.st_ino],
        "root": [info.st_dev, info.st_ino],
        "owner": None,
        "launches": {},
    }
    (root / _MARKER).write_text(json.dumps(marker), encoding="utf-8")
    token = _ACTIVE.set((*_ACTIVE.get(), WorkspaceLease(root, -1)))
    try:
        yield str(root)
    finally:
        _ACTIVE.reset(token)
        logger.warning(
            "Retaining marked scratch: descendant ownership is unsupported on %s: %s",
            sys.platform,
            root,
        )


def _allocate(parent: Path, prefix: str, kind: str) -> WorkspaceLease:
    import fcntl

    _directory(parent, private=False)
    with _exclusion(parent):
        root = Path(tempfile.mkdtemp(prefix="booley-owned-" + prefix, dir=parent))
        info, parent_info = root.stat(), parent.stat()
        fd = _open_regular(root / _GUARD, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_SH)
        data = {
            "schema": 1,
            "kind": kind,
            "lease": uuid.uuid4().hex,
            "uid": os.getuid(),
            "parent": [parent_info.st_dev, parent_info.st_ino],
            "root": [info.st_dev, info.st_ino],
            "owner": process_identity(os.getpid()),
            "guard": [os.fstat(fd).st_dev, os.fstat(fd).st_ino],
            "launches": {},
        }
        if kind == "baseline":
            data["baseline"] = None
        _write_marker(root, data)
        return WorkspaceLease(root, fd)


@dataclass(frozen=True)
class LeaseLaunch:
    """A pending handoff that remains conservative if its parent is killed."""

    command: list[str]
    kwargs: dict[str, Any]
    roots: tuple[Path, ...]
    token: str


def prepare_launch(command: list[str], guard_fds: tuple[int, ...] = ()) -> LeaseLaunch:
    """Wrap an actual native launch, retaining a pending marker before spawn."""
    descriptors = tuple(dict.fromkeys((*active_guard_fds(), *guard_fds)))
    if not descriptors:
        return LeaseLaunch(command, {}, (), "")
    roots = tuple(_guard_root(fd) for fd in descriptors)
    token = uuid.uuid4().hex
    changed: list[Path] = []
    try:
        for root in roots:
            with _exclusion(root.parent):
                data, _identity = _validated_marker(root)
                if len(data["launches"]) >= 64:
                    raise RuntimeError("scratch launch concurrency limit exceeded")
                data["launches"][token] = {"state": "pending"}
                _write_marker(root, data)
                changed.append(root)
    except BaseException:
        _clear_launch(tuple(changed), token, pending_only=True)
        raise
    payload = json.dumps(
        {"roots": [str(root) for root in roots], "token": token, "fds": list(descriptors)}
    )
    launcher = [sys.executable, str(Path(__file__).with_name("lease_exec.py")), payload, *command]
    return LeaseLaunch(launcher, {"pass_fds": descriptors}, roots, token)


def _guard_root(fd: int) -> Path:
    root = Path(f"/proc/self/fd/{fd}").readlink().parent
    data, _identity = _validated_marker(root)
    observed = os.fstat(fd)
    expected = (root / _GUARD).lstat()
    if (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError("scratch descriptor no longer points to its registered guard")
    if data["uid"] != os.getuid():
        raise ValueError("scratch guard belongs to another user")
    return root


def _clear_launch(roots: tuple[Path, ...], token: str, *, pending_only: bool) -> None:
    for root in roots:
        with _exclusion(root.parent):
            data, _identity = _validated_marker(root)
            launch = data["launches"].get(token)
            if launch is not None and (not pending_only or launch == {"state": "pending"}):
                del data["launches"][token]
                _write_marker(root, data)


def launch_failed(launch: LeaseLaunch) -> None:
    """Clear a handoff only when the infrastructure proved no process was spawned."""
    _clear_launch(launch.roots, launch.token, pending_only=True)


def launch_finished(launch: LeaseLaunch) -> None:
    """Acknowledge only supervisor-proven completion; lost guardians remain marked."""
    for root in launch.roots:
        with _exclusion(root.parent):
            data, _identity = _validated_marker(root)
            state = data["launches"].get(launch.token)
            if isinstance(state, dict) and state.get("state") == "complete":
                del data["launches"][launch.token]
                _write_marker(root, data)


def register_launch(
    roots: tuple[Path, ...], token: str, inherited: tuple[int, ...] = ()
) -> tuple[int, ...]:
    """Reacquire shared guards before publishing the pending-to-running handoff."""
    import fcntl

    descriptors: list[int] = []
    try:
        for root in roots:
            with _exclusion(root.parent):
                data, _identity = _validated_marker(root)
                if data["launches"].get(token) != {"state": "pending"}:
                    raise ValueError("scratch launch is not a pending registered handoff")
                existing = next((fd for fd in inherited if _guard_root(fd) == root), None)
                fd = (
                    os.dup(existing)
                    if existing is not None
                    else _open_regular(root / _GUARD, os.O_RDWR)
                )
                descriptors.append(fd)
                if existing is None:
                    fcntl.flock(fd, fcntl.LOCK_SH)
                data["launches"][token] = {
                    "state": "running",
                    "supervisor": process_identity(os.getpid()),
                }
                _write_marker(root, data)
        return tuple(descriptors)
    except BaseException:
        for fd in descriptors:
            os.close(fd)
        raise


def complete_launch(roots: tuple[Path, ...], token: str) -> None:
    """Publish terminal ownership only after the subreaper has no children left."""
    for root in roots:
        with _exclusion(root.parent):
            data, _identity = _validated_marker(root)
            state = data["launches"].get(token)
            if not isinstance(state, dict) or state.get("supervisor") != process_identity(
                os.getpid()
            ):
                raise ValueError("scratch supervisor identity changed")
            data["launches"][token] = {"state": "complete"}
            _write_marker(root, data)


def run_guarded(command: list[str], *, runner=None, **kwargs: Any):
    """Run a synchronous infrastructure command with active scratch tree guards."""
    import subprocess

    selected = subprocess.run if runner is None else runner
    if active_guard_fds() and selected is _NATIVE_RUN:
        validate_native_command(command, kwargs)
    launch = prepare_launch(command)
    if not launch.roots:
        return selected(command, **kwargs)
    inherited = tuple(kwargs.pop("pass_fds", ()))
    spawn_kwargs = {**kwargs, **launch.kwargs}
    spawn_kwargs["pass_fds"] = tuple(dict.fromkeys((*inherited, *launch.kwargs["pass_fds"])))
    try:
        if selected is _NATIVE_RUN:
            return _run_supervised(launch, command, spawn_kwargs)
        return selected(launch.command, **spawn_kwargs)
    finally:
        launch_finished(launch)


def validate_native_command(command: list[str], kwargs: dict[str, Any]) -> None:
    """Preserve native lookup errors before starting a scratch supervisor."""
    executable = str(kwargs.get("executable") or command[0])
    environment = os.environ if kwargs.get("env") is None else kwargs["env"]
    cwd = Path(kwargs.get("cwd") or Path.cwd())
    candidate = (
        cwd / executable
        if "/" in executable
        else shutil.which(executable, path=environment.get("PATH", os.defpath))
    )
    if candidate is None or not Path(candidate).exists():
        raise FileNotFoundError(2, os.strerror(2), executable)
    if not os.access(candidate, os.X_OK) or Path(candidate).is_dir():
        raise PermissionError(13, os.strerror(13), executable)


def _run_supervised(launch: LeaseLaunch, original: list[str], kwargs: dict[str, Any]):
    import subprocess

    timeout = kwargs.pop("timeout", None)
    input_value = kwargs.pop("input", None)
    check = kwargs.pop("check", False)
    capture = kwargs.pop("capture_output", False)
    if input_value is not None:
        kwargs["stdin"] = subprocess.PIPE
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        process = subprocess.Popen(launch.command, **kwargs)
    except OSError:
        launch_failed(launch)
        raise
    with process:
        try:
            stdout, stderr = process.communicate(input_value, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=1)
            raise subprocess.TimeoutExpired(
                original, timeout, output=stdout, stderr=stderr
            ) from exc
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
            raise
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, original, stdout, stderr)
        return subprocess.CompletedProcess(original, process.returncode, stdout, stderr)


async def settle_launch(launch: LeaseLaunch) -> None:
    """Boundedly terminate registered guardians before releasing provider workspaces."""
    import asyncio
    import signal
    import time

    supervisors: dict[int, dict[str, Any]] = {}
    for root in launch.roots:
        with _exclusion(root.parent):
            data, _marker = _validated_marker(root)
            state = data["launches"].get(launch.token)
            if isinstance(state, dict) and state.get("state") == "running":
                identity = state.get("supervisor")
                if isinstance(identity, dict) and owner_alive(identity) is True:
                    supervisors[identity["pid"]] = identity
    for pid, identity in supervisors.items():
        if owner_alive(identity) is True:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while supervisors and time.monotonic() < deadline:
        supervisors = {
            pid: identity for pid, identity in supervisors.items() if owner_alive(identity) is True
        }
        if supervisors:
            await asyncio.sleep(0.02)
    if supervisors:
        logger.warning("Retaining scratch after guardian shutdown could not be proven")
    launch_failed(launch)
    launch_finished(launch)
