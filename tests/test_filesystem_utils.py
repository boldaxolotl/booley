"""Tests for filesystem_utils: safe_rmtree and replace_file."""

from __future__ import annotations

import errno
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from booley.core.sharing_retry import link_file, write_file_text
from booley.runtime.filesystem_utils import (
    RemovedDirectoryError,
    make_directories,
    open_with_sharing_retry,
    read_file_bytes,
    replace_file,
    run_roots,
    safe_rmtree,
    unlink_file,
)
from tests.host_platform_fake import use_sys_platform
from tests.sharing_error_support import flaky
from tests.time_budget import hang_guard_s

# ---------------------------------------------------------------------------
# safe_rmtree
# ---------------------------------------------------------------------------


class TestSafeRmtree:
    def test_removes_a_nested_tree_with_read_only_files_and_ignores_a_missing_path(
        self, tmp_path: Path
    ):
        d = tmp_path / "target"
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "nested.txt").write_text("world")
        locked = d / "readonly.txt"  # read-only files (e.g. git objects) still go
        locked.write_text("locked")
        locked.chmod(stat.S_IREAD)

        safe_rmtree(str(d))
        assert not d.exists()
        safe_rmtree(str(d))

    def test_refuses_a_git_root_unless_protection_is_off(self, tmp_path: Path):
        d = tmp_path / "repo"
        git = d / ".git"
        git.mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main")

        with pytest.raises(ValueError, match="refusing to remove git root"):
            safe_rmtree(d)
        assert d.exists()

        safe_rmtree(d, protect_git_root=False)
        assert not d.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks")
    def test_error_handler_never_chmods_a_symlink_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A failed unlink of a link must not change the mode of what it points at."""
        real = tmp_path / "real-login"
        real.write_text("secret")
        real.chmod(0o600)
        d = tmp_path / "target"
        d.mkdir()
        link = d / "auth.json"
        link.symlink_to(real)

        def failing_rmtree(path, onerror):
            onerror(Path.unlink, str(link), None)

        monkeypatch.setattr("booley.runtime.filesystem_utils.shutil.rmtree", failing_rmtree)
        # The handler retries ``func`` (unlink), which removes the link itself.
        safe_rmtree(d)

        assert stat.S_IMODE(real.stat().st_mode) == 0o600
        assert real.exists()


# ---------------------------------------------------------------------------
# replace_file
# ---------------------------------------------------------------------------


class TestMakeDirectories:
    def test_creates_subdirectories_in_an_existing_booley_directory(self, tmp_path):
        booley_dir = tmp_path / "checkout" / ".booley"
        booley_dir.mkdir(parents=True)
        target = booley_dir / "flow-reports" / "lint" / "1"

        with run_roots(tmp_path / "checkout", booley_dir):
            make_directories(target)

        assert target.is_dir()

    def test_creates_a_booley_directory_inside_an_existing_checkout(self, tmp_path):
        target = tmp_path / "checkout" / ".booley" / ".runtime" / "edalize"
        target.parent.parent.parent.mkdir()

        make_directories(target)

        assert target.is_dir()

    def test_refuses_to_recreate_a_removed_checkout(self, tmp_path):
        checkout = tmp_path / "checkout"

        with pytest.raises(RemovedDirectoryError) as caught:
            make_directories(checkout / ".booley" / "flow-reports")

        assert caught.value.directory == checkout
        assert str(checkout) in str(caught.value)
        assert not checkout.exists()

    def test_refuses_a_removed_run_root_only_while_the_run_lasts(self, tmp_path):
        checkout = tmp_path / "checkout"
        booley_dir = checkout / "data"
        checkout.mkdir()

        with run_roots(checkout, booley_dir), pytest.raises(RemovedDirectoryError) as caught:
            make_directories(booley_dir / "flow-reports")

        assert caught.value.directory == booley_dir
        assert not booley_dir.exists()
        make_directories(booley_dir / "flow-reports")
        assert (booley_dir / "flow-reports").is_dir()

    def test_durable_publication_refuses_to_recreate_a_removed_checkout(self, tmp_path):
        from booley.flows.artifact_durability import durable_create

        checkout = tmp_path / "checkout"

        with pytest.raises(RemovedDirectoryError):
            durable_create(checkout / ".booley" / "flow-reports" / "run.log", b"x")

        assert not checkout.exists()

    def test_a_file_in_the_way_still_fails(self, tmp_path):
        (tmp_path / "taken").write_bytes(b"")

        with pytest.raises(FileExistsError):
            make_directories(tmp_path / "taken")


def _windows_permission_error(winerror: int) -> PermissionError:
    """Build the PermissionError Windows raises; POSIX lacks the winerror field."""
    exc = PermissionError(13, "Access is denied")
    exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


def _fail_first(
    monkeypatch: pytest.MonkeyPatch, method: str, path: Path, failures: list[OSError]
) -> list[int]:
    """Make ``Path.<method>`` on *path* raise each of *failures* in turn, then run for real.

    Returns one entry per call made on *path*.
    """
    real = getattr(Path, method)
    calls: list[int] = []

    # A plain function so Path binds it as a method.
    def flaky_method(self: Path, *args: object, **kwargs: object):
        if self != path:
            return real(self, *args, **kwargs)
        calls.append(1)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, flaky_method)
    return calls


def _operation(
    op: str, tmp_path: Path, delays: list[float]
) -> tuple[str, Path, Callable[[], object], Callable[[object], bool]]:
    """Return the ``Path`` method *op* uses, the path it runs on, the call, and its effect check."""
    if op == "replace":
        source, destination = tmp_path / "new", tmp_path / "record.json"
        source.write_bytes(b"new")
        return (
            "replace",
            source,
            lambda: replace_file(source, destination, sleep=delays.append),
            lambda _: destination.read_bytes() == b"new",
        )
    if op == "unlink":
        entry = tmp_path / "entry"
        entry.write_bytes(b"x")
        return (
            "unlink",
            entry,
            lambda: unlink_file(entry, sleep=delays.append),
            lambda _: not entry.exists(),
        )
    record = tmp_path / "record.json"
    if op == "write":
        return (
            "write_text",
            record,
            lambda: write_file_text(record, "payload", sleep=delays.append),
            lambda _: record.read_bytes() == b"payload",
        )
    record.write_bytes(b"payload")
    if op == "link":
        target = tmp_path / "gate"
        return (
            "hardlink_to",
            target,
            lambda: link_file(target, record, sleep=delays.append),
            lambda _: target.read_bytes() == b"payload",
        )
    return (
        "open",
        record,
        lambda: read_file_bytes(record, sleep=delays.append),
        lambda result: result == b"payload",
    )


OPERATIONS = pytest.mark.parametrize("op", ["replace", "unlink", "read", "write", "link"])


@OPERATIONS
def test_retries_transient_sharing_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, op: str
):
    delays: list[float] = []
    method, path, run, effect = _operation(op, tmp_path, delays)
    errors = [_windows_permission_error(code) for code in (5, 32, 32)]
    calls = _fail_first(monkeypatch, method, path, errors)

    assert effect(run())
    assert len(calls) == 4
    assert len(delays) == 3


@OPERATIONS
def test_gives_up_after_bounded_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, op: str):
    delays: list[float] = []
    method, path, run, _ = _operation(op, tmp_path, delays)
    calls = _fail_first(monkeypatch, method, path, [_windows_permission_error(32)] * 100)

    with pytest.raises(PermissionError):
        run()
    assert len(calls) == len(delays) + 1
    assert sum(delays) < 2.0


@OPERATIONS
def test_does_not_retry_posix_permission_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, op: str
):
    delays: list[float] = []
    method, path, run, _ = _operation(op, tmp_path, delays)
    calls = _fail_first(monkeypatch, method, path, [PermissionError(13, "Permission denied")])

    with monkeypatch.context() as host:
        if op in ("read", "write"):
            # Only the open paths treat a bare EACCES as a transient C-runtime sharing error
            # on Windows; replace and unlink run on the real host.
            use_sys_platform(host, windows=False)
        with pytest.raises(PermissionError):
            run()
    assert len(calls) == 1
    assert delays == []


def test_read_file_bytes_keeps_a_missing_file_missing(tmp_path: Path):
    delays: list[float] = []
    with pytest.raises(FileNotFoundError):
        read_file_bytes(tmp_path / "missing.json", sleep=delays.append)
    assert delays == []


def test_retries_bare_eacces_from_the_windows_c_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Windows open() goes through the C runtime, which reports a sharing
    violation as plain EACCES with no winerror (#2352)."""
    path = tmp_path / "record.json"
    path.write_bytes(b"payload")
    bare = PermissionError(errno.EACCES, "Permission denied")
    delays: list[float] = []
    with monkeypatch.context() as windows:
        use_sys_platform(windows, windows=True)
        calls = _fail_first(windows, "open", path, [bare] * 3)
        assert read_file_bytes(path, sleep=delays.append) == b"payload"
    assert len(calls) == 4
    assert len(delays) == 3

    delays.clear()
    with monkeypatch.context() as windows:
        use_sys_platform(windows, windows=True)
        persistent = _fail_first(windows, "open", path, [bare] * 100)
        with pytest.raises(PermissionError):
            read_file_bytes(path, sleep=delays.append)
    assert len(persistent) == len(delays) + 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing semantics only")
def test_waits_out_a_real_open_reader_on_windows(tmp_path: Path):
    source, destination = tmp_path / "new", tmp_path / "record.json"
    source.write_text("new")
    destination.write_text("old")
    reader = destination.open("rb")
    closer = threading.Timer(0.1, reader.close)
    closer.start()
    try:
        replace_file(source, destination)
    finally:
        closer.join(timeout=hang_guard_s())
        reader.close()
    assert destination.read_text() == "new"


def test_open_with_sharing_retry_retries_transient_errors():
    opener, calls = flaky(lambda: "handle", 3)
    delays: list[float] = []
    assert open_with_sharing_retry(opener, Path("held.json"), sleep=delays.append) == "handle"
    assert calls == [4]
    assert len(delays) == 3


def test_open_with_sharing_retry_raises_lasting_error_and_keeps_other_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    path = Path("held.json")
    opener, calls = flaky(lambda: "handle", 1000)
    delays: list[float] = []
    with pytest.raises(PermissionError) as lasting:
        open_with_sharing_retry(opener, path, sleep=delays.append)
    assert lasting.value.winerror == 32  # type: ignore[attr-defined]
    assert calls[0] == len(delays) + 1 > 1

    use_sys_platform(monkeypatch, windows=False)
    for error in (
        FileNotFoundError(errno.ENOENT, "missing"),
        OSError(errno.ELOOP, "symlink"),
        PermissionError(errno.EACCES, "Permission denied"),
    ):
        attempts: list[int] = []

        def failing(error: OSError = error, attempts: list[int] = attempts):
            attempts.append(1)
            raise error

        with pytest.raises(type(error)):
            open_with_sharing_retry(failing, path, sleep=delays.append)
        assert attempts == [1]


def test_unlink_file_missing_ok_ignores_absent_path(tmp_path: Path):
    absent = tmp_path / "absent"
    unlink_file(absent, missing_ok=True)
    with pytest.raises(FileNotFoundError):
        unlink_file(absent)
