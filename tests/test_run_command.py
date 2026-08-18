"""Unit tests for booley.core.run_command."""

from __future__ import annotations

import sys

import pytest

from booley.core.run_command import CommandError, CommandRun, run_command

PY = sys.executable


class TestRunCommand:
    def test_success_captures_stdout(self):
        run = run_command([PY, "-c", "print('hello')"])
        assert run.ok
        assert run.returncode == 0
        assert "hello" in run.stdout
        assert run.stderr == ""

    def test_failure_captures_stderr_and_code(self):
        run = run_command([PY, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])
        assert not run.ok
        assert run.returncode == 3
        assert "boom" in run.stderr
        # The whole point: stderr survives instead of being thrown away.
        assert "boom" in run.failure_excerpt()

    def test_missing_executable_is_uniform_failure(self):
        # No raised FileNotFoundError — one uniform failure shape.
        run = run_command(["definitely-not-a-real-binary-xyz"])
        assert not run.ok
        assert run.returncode == 127
        assert "not found" in run.stderr

    def test_check_raises_toolerror_with_detail(self):
        with pytest.raises(CommandError) as ei:
            run_command([PY, "-c", "import sys; sys.stderr.write('why'); sys.exit(1)"], check=True)
        assert "why" in str(ei.value)
        assert ei.value.run.returncode == 1

    def test_check_passes_through_on_success(self):
        run = run_command([PY, "-c", "pass"], check=True)
        assert run.ok

    def test_timeout_sets_timed_out(self):
        run = run_command([PY, "-c", "import time; time.sleep(5)"], timeout=0.2)
        assert run.timed_out
        assert not run.ok
        assert "timed out" in run.failure_excerpt()

    def test_input_text_piped_to_stdin(self):
        run = run_command(
            [PY, "-c", "import sys; sys.stdout.write(sys.stdin.read())"], input_text="echoed"
        )
        assert run.stdout == "echoed"

    def test_extra_env_overlays_environment(self):
        run = run_command(
            [PY, "-c", "import os; print(os.environ.get('BOOLEY_TEST_X'))"],
            extra_env={"BOOLEY_TEST_X": "42"},
        )
        assert "42" in run.stdout

    def test_cwd_is_respected(self, tmp_path):
        run = run_command([PY, "-c", "import os; print(os.getcwd())"], cwd=tmp_path)
        assert str(tmp_path) in run.stdout


class TestFailureExcerpt:
    def test_never_empty_on_bare_failure(self):
        # No stdout/stderr at all, just a bad code -> still concrete.
        run = CommandRun(argv=["x"], returncode=2)
        exc = run.failure_excerpt()
        assert exc
        assert "2" in exc

    def test_stderr_first(self):
        run = CommandRun(argv=["x"], returncode=1, stdout="out", stderr="err")
        excerpt = run.failure_excerpt()
        assert excerpt.index("err") < excerpt.index("out")

    def test_tail_trimmed_to_limit(self):
        run = CommandRun(argv=["x"], returncode=1, stderr="A" * 10000)
        assert len(run.failure_excerpt(limit=100)) == 100
