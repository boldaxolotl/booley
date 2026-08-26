"""Tests for the shared Unix process-tree walk."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from booley.runtime import process_tree


@pytest.mark.skipif(sys.platform == "win32", reason="Unix-only /proc walk")
class TestProcStatParsing:
    def test_plain_comm(self):
        assert process_tree._parse_ppid("42 (bash) S 7 42 42 0 -1 4194304 100") == 7

    def test_comm_containing_a_close_paren_and_space(self):
        assert process_tree._parse_ppid("42 (weird) name) S 7 42 42 0 -1 4194304 100") == 7

    def test_garbage_is_none_not_an_exception(self):
        assert process_tree._parse_ppid("not a stat line") is None
        assert process_tree._parse_ppid("") is None


@pytest.mark.skipif(sys.platform == "win32", reason="Unix-only /proc walk")
class TestDescendantPids:
    def test_finds_a_grandchild_in_its_own_session(self):
        src = textwrap.dedent(
            """
            import subprocess, sys, time
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True,
            )
            print(child.pid, flush=True)
            time.sleep(30)
            """
        )
        proc = subprocess.Popen([sys.executable, "-c", src], stdout=subprocess.PIPE, text=True)
        grandchild: int | None = None
        try:
            assert proc.stdout is not None
            grandchild = int(proc.stdout.readline().strip())
            time.sleep(0.2)
            found = process_tree.descendant_pids(os.getpid())
            assert proc.pid in found
            assert grandchild in found
            assert found.index(grandchild) < found.index(proc.pid)
        finally:
            if grandchild is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(grandchild, signal.SIGKILL)
            proc.kill()
            proc.wait(timeout=10)
            if proc.stdout is not None:
                proc.stdout.close()

    def test_no_descendants_is_empty(self):
        assert process_tree.descendant_pids(999_999) == []
