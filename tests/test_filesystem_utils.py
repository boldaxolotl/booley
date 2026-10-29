"""Tests for filesystem_utils: safe_rmtree and replace_file."""

from __future__ import annotations

import errno
import stat
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from booley.runtime.filesystem_utils import replace_file, safe_rmtree, unlink_file

# ---------------------------------------------------------------------------
# safe_rmtree
# ---------------------------------------------------------------------------


class TestSafeRmtree:
    def test_removes_simple_tree(self, tmp_path: Path):
        d = tmp_path / "target"
        d.mkdir()
        (d / "file.txt").write_text("hello")
        (d / "sub").mkdir()
        (d / "sub" / "nested.txt").write_text("world")

        safe_rmtree(d)
        assert not d.exists()

    def test_noop_when_path_does_not_exist(self, tmp_path: Path):
        """Should not raise when path doesn't exist."""
        safe_rmtree(tmp_path / "nonexistent")

    def test_refuses_git_root_by_default(self, tmp_path: Path):
        """protect_git_root=True should block removal of a real git dir."""
        d = tmp_path / "repo"
        d.mkdir()
        git = d / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main")

        with pytest.raises(ValueError, match="refusing to remove git root"):
            safe_rmtree(d)
        assert d.exists()

    def test_allows_git_root_when_protection_off(self, tmp_path: Path):
        d = tmp_path / "repo"
        d.mkdir()
        git = d / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main")

        safe_rmtree(d, protect_git_root=False)
        assert not d.exists()

    def test_handles_read_only_files(self, tmp_path: Path):
        """Read-only files (e.g. git objects) should still be removed."""
        d = tmp_path / "target"
        d.mkdir()
        f = d / "readonly.txt"
        f.write_text("locked")
        f.chmod(stat.S_IREAD)

        safe_rmtree(d)
        assert not d.exists()

    def test_accepts_string_path(self, tmp_path: Path):
        d = tmp_path / "strpath"
        d.mkdir()
        (d / "x.txt").write_text("data")
        safe_rmtree(str(d))
        assert not d.exists()


# ---------------------------------------------------------------------------
# replace_file
# ---------------------------------------------------------------------------


def _windows_permission_error(winerror: int) -> PermissionError:
    """Build the PermissionError Windows raises; POSIX lacks the winerror field."""
    exc = PermissionError(13, "Access is denied")
    exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


class _FlakyReplace:
    """Stand-in for Path.replace that fails a fixed number of times first."""

    def __init__(self, failures: list[PermissionError]) -> None:
        self.failures = failures
        self.calls = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_replace = Path.replace

        # A plain function (not this instance) so Path binds it as a method.
        def flaky_replace(source: Path, destination: Path) -> Path:
            self.calls += 1
            if self.failures:
                raise self.failures.pop(0)
            return real_replace(source, destination)

        monkeypatch.setattr(Path, "replace", flaky_replace)


class TestReplaceFile:
    def test_replaces_destination(self, tmp_path: Path):
        source, destination = tmp_path / "new", tmp_path / "record.json"
        source.write_text("new")
        destination.write_text("old")
        replace_file(source, destination)
        assert destination.read_text() == "new"
        assert not source.exists()

    def test_retries_transient_windows_sharing_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        source, destination = tmp_path / "new", tmp_path / "record.json"
        source.write_text("new")
        flaky = _FlakyReplace([_windows_permission_error(32)] * 3)
        flaky.install(monkeypatch)
        delays: list[float] = []
        replace_file(source, destination, sleep=delays.append)
        assert destination.read_text() == "new"
        assert flaky.calls == 4
        assert len(delays) == 3

    def test_gives_up_after_bounded_retries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        source, destination = tmp_path / "new", tmp_path / "record.json"
        source.write_text("new")
        flaky = _FlakyReplace([_windows_permission_error(32)] * 100)
        flaky.install(monkeypatch)
        delays: list[float] = []
        with pytest.raises(PermissionError):
            replace_file(source, destination, sleep=delays.append)
        assert flaky.calls == len(delays) + 1
        assert sum(delays) < 2.0

    def test_does_not_retry_posix_permission_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        source, destination = tmp_path / "new", tmp_path / "record.json"
        source.write_text("new")
        flaky = _FlakyReplace([PermissionError(13, "Permission denied")])
        flaky.install(monkeypatch)
        delays: list[float] = []
        with pytest.raises(PermissionError):
            replace_file(source, destination, sleep=delays.append)
        assert flaky.calls == 1
        assert delays == []

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing semantics only")
    def test_waits_out_a_real_open_reader_on_windows(self, tmp_path: Path):
        source, destination = tmp_path / "new", tmp_path / "record.json"
        source.write_text("new")
        destination.write_text("old")
        reader = destination.open("rb")
        closer = threading.Timer(0.1, reader.close)
        closer.start()
        try:
            replace_file(source, destination)
        finally:
            closer.join(timeout=5)
            reader.close()
        assert destination.read_text() == "new"


class _FlakyUnlink:
    """Stand-in for Path.unlink that fails a fixed number of times first."""

    def __init__(self, failures: list[OSError]) -> None:
        self.failures = failures
        self.calls = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_unlink = Path.unlink

        def flaky_unlink(path: Path, *args, **kwargs) -> None:
            self.calls += 1
            if self.failures:
                raise self.failures.pop(0)
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", flaky_unlink)


class TestUnlinkFile:
    def test_removes_file(self, tmp_path: Path):
        target = tmp_path / "entry"
        target.write_text("x")
        unlink_file(target)
        assert not target.exists()

    def test_retries_transient_windows_sharing_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        target = tmp_path / "entry"
        target.write_text("x")
        flaky = _FlakyUnlink([_windows_permission_error(32)] * 3)
        flaky.install(monkeypatch)
        delays: list[float] = []
        unlink_file(target, sleep=delays.append)
        assert not target.exists()
        assert flaky.calls == 4
        assert len(delays) == 3

    def test_gives_up_after_bounded_retries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        target = tmp_path / "entry"
        target.write_text("x")
        flaky = _FlakyUnlink([_windows_permission_error(32)] * 100)
        flaky.install(monkeypatch)
        delays: list[float] = []
        with pytest.raises(PermissionError):
            unlink_file(target, sleep=delays.append)
        assert flaky.calls == len(delays) + 1
        assert sum(delays) < 2.0


class _FlakyOpen:
    """Stand-in for Path.open that fails the first reads of one path."""

    def __init__(self, target: Path, failures: list[OSError]) -> None:
        self.target = target
        self.failures = failures
        self.calls = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_open = Path.open

        # A plain function (not this instance) so Path binds it as a method.
        def flaky_open(path: Path, *args: object, **kwargs: object):
            if path == self.target:
                self.calls += 1
                if self.failures:
                    raise self.failures.pop(0)
            return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "open", flaky_open)


class TestReadFileBytes:
    def test_retries_transient_windows_sharing_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from booley.runtime.filesystem_utils import read_file_bytes

        path = tmp_path / "record.json"
        path.write_bytes(b"payload")
        flaky = _FlakyOpen(
            path,
            [
                _windows_permission_error(5),
                _windows_permission_error(32),
                _windows_permission_error(5),
            ],
        )
        flaky.install(monkeypatch)
        delays: list[float] = []
        assert read_file_bytes(path, sleep=delays.append) == b"payload"
        assert flaky.calls == 4
        assert len(delays) == 3

    def test_gives_up_after_bounded_retries_and_keeps_missing_and_posix_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from booley.runtime.filesystem_utils import read_file_bytes

        path = tmp_path / "record.json"
        path.write_bytes(b"payload")
        flaky = _FlakyOpen(path, [_windows_permission_error(32)] * 100)
        flaky.install(monkeypatch)
        delays: list[float] = []
        with pytest.raises(PermissionError):
            read_file_bytes(path, sleep=delays.append)
        assert flaky.calls == len(delays) + 1
        assert sum(delays) < 2.0

        posix = _FlakyOpen(path, [PermissionError(13, "Permission denied")])
        posix.install(monkeypatch)
        delays.clear()
        # Pin POSIX: on Windows a bare EACCES is a transient C-runtime sharing error.
        with monkeypatch.context() as posix_host:
            posix_host.setattr(sys, "platform", "linux")
            with pytest.raises(PermissionError):
                read_file_bytes(path, sleep=delays.append)
        assert posix.calls == 1
        assert delays == []

        with pytest.raises(FileNotFoundError):
            read_file_bytes(tmp_path / "missing.json", sleep=delays.append)
        assert delays == []

    def test_retries_bare_eacces_from_the_windows_c_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Windows open() goes through the C runtime, which reports a sharing
        violation as plain EACCES with no winerror (#2352)."""
        from booley.runtime.filesystem_utils import read_file_bytes

        path = tmp_path / "record.json"
        path.write_bytes(b"payload")
        flaky = _FlakyOpen(path, [PermissionError(errno.EACCES, "Permission denied")] * 3)
        flaky.install(monkeypatch)
        delays: list[float] = []
        with monkeypatch.context() as windows:
            windows.setattr(sys, "platform", "win32")
            assert read_file_bytes(path, sleep=delays.append) == b"payload"
        assert flaky.calls == 4
        assert len(delays) == 3

        persistent = _FlakyOpen(path, [PermissionError(errno.EACCES, "Permission denied")] * 100)
        persistent.install(monkeypatch)
        delays.clear()
        with monkeypatch.context() as windows:
            windows.setattr(sys, "platform", "win32")
            with pytest.raises(PermissionError):
                read_file_bytes(path, sleep=delays.append)
        assert persistent.calls == len(delays) + 1


def test_open_with_sharing_retry_retries_transient_errors():
    from booley.runtime.filesystem_utils import open_with_sharing_retry
    from tests.sharing_error_support import flaky

    opener, calls = flaky(lambda: "handle", 3)
    delays: list[float] = []
    assert open_with_sharing_retry(opener, Path("held.json"), sleep=delays.append) == "handle"
    assert calls == [4]
    assert len(delays) == 3


def test_open_with_sharing_retry_raises_lasting_error_and_keeps_other_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    from booley.runtime.filesystem_utils import open_with_sharing_retry
    from tests.sharing_error_support import flaky

    path = Path("held.json")
    opener, calls = flaky(lambda: "handle", 1000)
    delays: list[float] = []
    with pytest.raises(PermissionError) as lasting:
        open_with_sharing_retry(opener, path, sleep=delays.append)
    assert lasting.value.winerror == 32  # type: ignore[attr-defined]
    assert calls[0] == len(delays) + 1 > 1

    monkeypatch.setattr(sys, "platform", "linux")
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
