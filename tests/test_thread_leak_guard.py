"""Exercise the thread-leak guard through real pytest and xdist runs.

Inner threads block on an ``Event`` and are daemons, so an inner process always
exits even when the guard under test misbehaves.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from textwrap import dedent

import pytest

from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S

_TESTS_DIR = Path(__file__).parent
_SOURCE_DIR = _TESTS_DIR.parent / "src"

_LEAKING_MODULE = """
    import threading

    _never = threading.Event()

    def _block():
        _never.wait()

    def test_leaks():
        threading.Thread(target=_block, name="leaky-worker", daemon=True).start()

    def test_after():
        pass
"""


def _run_inner(
    tmp_path: Path,
    module_source: str,
    *,
    workers: int = 0,
    conftest_source: str | None = None,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run ``test_case.py`` in a private pytest session that loads only the guard."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_case.py").write_text(dedent(module_source), encoding="utf-8")
    if conftest_source is not None:
        (tmp_path / "conftest.py").write_text(dedent(conftest_source), encoding="utf-8")
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")
    }
    environment["PYTHONPATH"] = os.pathsep.join([str(_TESTS_DIR), str(_SOURCE_DIR)])
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    arguments = [
        "-p", "xdist.plugin",
        "-p", "thread_leak_guard",
        "-p", "no:cacheprovider",
        f"--basetemp={tmp_path / 'inner'}",
        *extra_args,
        "-n", str(workers),
    ]  # fmt: skip
    return subprocess.run(
        [sys.executable, "-m", "pytest", "test_case.py", "-q", *arguments],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        check=False,
    )


def _counts(output: str) -> dict[str, int]:
    """Parse the pytest summary line, e.g. ``1 passed, 1 error in 2.0s``."""
    summary = [
        line for line in output.splitlines() if re.search(r"\d+ (passed|error|failed)", line)
    ][-1]
    return {
        name: int(count) for count, name in re.findall(r"(\d+) (passed|error|failed)", summary)
    }


@pytest.mark.parametrize("workers", [0, 1])
def test_leaked_thread_fails_the_leaking_test_by_name(tmp_path: Path, workers: int) -> None:
    result = _run_inner(tmp_path, _LEAKING_MODULE, workers=workers)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    # The leaking test passed its call phase, then errored at teardown.
    assert _counts(output) == {"passed": 2, "error": 1}, output
    assert "ERROR at teardown of test_leaks" in output
    assert "test_case.py::test_leaks left 1 thread(s) running" in output
    assert "leaky-worker" in output
    assert "test_case._block" in output
    # The failure belongs to the leaking test, never to the one that runs next.
    assert "ERROR at teardown of test_after" not in output


def test_joined_thread_and_preexisting_thread_pass(tmp_path: Path) -> None:
    # A thread alive before the test starts, and pytest-timeout's per-test
    # timer (the Windows CI method), belong to the baseline.
    conftest = """
        import threading

        _never = threading.Event()
        threading.Thread(target=_never.wait, name="session-thread", daemon=True).start()
    """
    module = """
        import threading

        def test_joins():
            worker = threading.Thread(target=lambda: None, name="joined-worker")
            worker.start()
            worker.join()
    """
    result = _run_inner(
        tmp_path,
        module,
        conftest_source=conftest,
        extra_args=("-p", "pytest_timeout", "--timeout=60", "--timeout-method=thread"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _counts(result.stdout) == {"passed": 1}


def test_thread_finishing_within_grace_passes(tmp_path: Path) -> None:
    # The thread is alive when the guard looks (it waits for a finalizer) and
    # ends 0.3 s later, inside the grace period, without being joined.
    module = """
        import threading
        import time

        import pytest

        @pytest.fixture
        def release():
            gate = threading.Event()
            yield gate
            gate.set()

        def test_lingers(release):
            def linger():
                release.wait()
                time.sleep(0.3)

            threading.Thread(target=linger, name="lingering-worker", daemon=True).start()
    """
    result = _run_inner(tmp_path, module)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _counts(result.stdout) == {"passed": 1}


def test_thread_leaked_by_a_function_fixture_fails_the_test(tmp_path: Path) -> None:
    # The snapshot precedes fixture setup, so a fixture's thread is the test's.
    module = """
        import threading

        import pytest

        @pytest.fixture
        def server():
            threading.Thread(
                target=threading.Event().wait, name="fixture-server", daemon=True
            ).start()

        def test_uses_fixture(server):
            pass
    """
    result = _run_inner(tmp_path, module)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "test_case.py::test_uses_fixture left 1 thread(s) running" in output
    assert "fixture-server" in output


def test_foreign_thread_that_cannot_be_joined_is_ignored(tmp_path: Path) -> None:
    # A thread made outside threading that calls current_thread() appears as a
    # _DummyThread, whose join() raises.
    module = """
        import _thread
        import threading

        def test_foreign_thread():
            ready = threading.Event()
            stop = _thread.allocate_lock()
            stop.acquire()

            def foreign():
                threading.current_thread()
                ready.set()
                stop.acquire()

            _thread.start_new_thread(foreign, ())
            ready.wait()
            assert any(type(t).__name__ == "_DummyThread" for t in threading.enumerate())
    """
    result = _run_inner(tmp_path, module)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _counts(result.stdout) == {"passed": 1}


def _blocked_thread(name: str, stop: threading.Event) -> threading.Thread:
    thread = threading.Thread(target=stop.wait, name=name, daemon=True)
    thread.start()
    return thread


def test_allowlisted_leak_is_ignored_and_unlisted_leak_reported() -> None:
    from tests.thread_leak_guard import AllowEntry, unexpected_leaks

    stop = threading.Event()
    threads = [_blocked_thread(name, stop) for name in ("one", "two")]
    try:
        listed_by_thread = AllowEntry(thread="*Event.wait", test=None, reason="r", issue=None)
        listed_by_both = AllowEntry(
            thread="*Event.wait", test="tests/x.py::test_x", reason="r", issue=None
        )
        wrong_test = AllowEntry(
            thread="*Event.wait", test="tests/x.py::test_other", reason="r", issue=None
        )
        other_thread = AllowEntry(thread="*Nothing.here", test=None, reason="r", issue=None)

        assert unexpected_leaks("tests/x.py::test_x", threads, [listed_by_thread]) == []
        # Parametrized and pytest-repeat copies match the parameter-free pattern.
        for nodeid in ("tests/x.py::test_x[a]", "tests/x.py::test_x[3-10]", "tests/x.py::test_x"):
            assert unexpected_leaks(nodeid, threads, [listed_by_both]) == []
        assert unexpected_leaks("tests/x.py::test_x", threads, [wrong_test]) == threads
        assert unexpected_leaks("tests/x.py::test_x", threads, [other_thread]) == threads
        assert unexpected_leaks("tests/x.py::test_x", threads, []) == threads
    finally:
        stop.set()
        for thread in threads:
            thread.join()


@pytest.mark.parametrize(
    ("toml", "fragment"),
    [
        pytest.param('[[leak]]\nthread = "a.b"\n', "reason", id="no-reason"),
        pytest.param('[[leak]]\nreason = "because"\n', "thread", id="no-pattern"),
        pytest.param('[[leak]]\nthread = "a.b"\nreason = "  "\n', "reason", id="blank-reason"),
        pytest.param(
            '[[leak]]\nthread = "a.b"\nreason = "r"\nissue = 0\n', "issue", id="bad-issue"
        ),
        pytest.param(
            '[[leak]]\nthread = "a.b"\nreason = "r"\nbogus = 1\n', "bogus", id="unknown-key"
        ),
        pytest.param("leak = 1\n", "array of tables", id="leak-not-a-list"),
        pytest.param("leak = [1]\n", "must be a table", id="entry-not-a-table"),
    ],
)
def test_invalid_allowlist_entry_is_rejected(tmp_path: Path, toml: str, fragment: str) -> None:
    from tests.thread_leak_guard import load_allowlist

    path = tmp_path / "allow.toml"
    path.write_bytes(toml.encode("utf-8"))

    with pytest.raises(pytest.UsageError, match=fragment):
        load_allowlist(path)


def test_valid_allowlist_loads(tmp_path: Path) -> None:
    from tests.thread_leak_guard import AllowEntry, load_allowlist

    path = tmp_path / "allow.toml"
    path.write_bytes(
        b'[[leak]]\nthread = "a.b"\ntest = "t.py::test_x"\nreason = "why"\nissue = 7\n'
    )

    assert load_allowlist(path) == [
        AllowEntry(thread="a.b", test="t.py::test_x", reason="why", issue=7)
    ]


def test_shipped_allowlist_is_valid_and_points_the_stall_monitor_at_its_issue() -> None:
    from tests.thread_leak_guard import load_allowlist

    entries = load_allowlist(_TESTS_DIR / "thread_leak_allowlist.toml")

    stall = [entry for entry in entries if entry.thread and "_monitor_stalls" in entry.thread]
    assert [entry.issue for entry in stall] == [3139]


def test_describe_names_the_callable_the_timer_or_the_class() -> None:
    from tests.thread_leak_guard import describe

    class Custom(threading.Thread):
        def run(self) -> None:
            pass

    stop = threading.Event()
    targeted = _blocked_thread("targeted", stop)
    timer = threading.Timer(60, print)
    try:
        assert describe(targeted) == "threading.Event.wait"
        assert describe(timer) == "builtins.print"
        assert describe(Custom()) == (
            "tests.test_thread_leak_guard."
            "test_describe_names_the_callable_the_timer_or_the_class.<locals>.Custom"
        )
    finally:
        stop.set()
        targeted.join()
