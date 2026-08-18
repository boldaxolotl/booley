"""Canonical external-command subprocess runner.

One primitive for invoking external commands and EDA tools (synth/PnR/sim binaries, git,
docker, …) that ALWAYS captures stdout+stderr and the real exit code. The
"swallowed subprocess error" bug class — where a command failed and the report
guessed at the cause because stderr was thrown away — shipped twice
(79a7749 asic_synthesize, 6c86f9d tooling). Both fixes were one-off patches at
a single call site; this is the shared fix so the mistake has a single, obvious
alternative and cannot silently recur in new code.

Stdlib-only, so it sits at the bottom of the import graph with no cycle risk
(mirrors ``core.config_paths``).

Typical use::

    run = run_command(["yosys", "-s", script], cwd=work_dir, timeout=600)
    if not run.ok:
        report.failure_output = run.failure_excerpt()  # stderr is never lost

    # or, to fail loudly with the real cause embedded in the exception:
    run_command(["git", "diff", "--cached"], check=True)
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CommandRun:
    """The full outcome of one external-command invocation."""

    argv: list[str]
    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        """True only on a clean exit (rc 0, not timed out)."""
        return self.returncode == 0 and not self.timed_out

    def failure_excerpt(self, limit: int = 4000) -> str:
        """A human-facing excerpt of *why* the command failed, stderr first.

        Never returns the empty string for a real failure: falls back to the
        timeout note or a bare returncode line so callers always have something
        concrete to surface instead of a guess. Tail-trimmed to ``limit`` chars.
        """
        parts: list[str] = []
        if self.timed_out:
            parts.append(f"[timed out after {self.duration_s:.1f}s]")
        err = (self.stderr or "").strip()
        out = (self.stdout or "").strip()
        if err:
            parts.append(err)
        if out:
            parts.append(out)
        blob = "\n".join(parts).strip()
        if not blob:
            blob = f"[no output; exit code {self.returncode}]"
        return blob[-limit:]


class CommandError(RuntimeError):
    """Raised by ``run_command(..., check=True)`` on a non-clean exit.

    Unlike ``subprocess.CalledProcessError`` (whose ``stderr`` is ``None`` when
    the caller forgot ``capture_output=True`` — the exact footgun behind this
    bug class), the failure detail is always captured and rendered into the
    message and available via ``.run``.
    """

    def __init__(self, run: CommandRun) -> None:
        self.run = run
        cmd = " ".join(run.argv)
        status = "timed out" if run.timed_out else f"exit {run.returncode}"
        super().__init__(f"command failed ({status}): {cmd}\n{run.failure_excerpt()}")


def run_command(
    argv: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float | None = None,
    input_text: str | None = None,
    check: bool = False,
) -> CommandRun:
    """Run ``argv`` as an external command, always capturing stdout+stderr+rc.

    Args:
        argv: command + arguments (never a shell string — no ``shell=True``).
        cwd: working directory; ``None`` inherits the current one.
        env: full environment override; ``None`` inherits ``os.environ``.
        extra_env: overlay applied on top of the inherited/`env` environment
            (convenience for the common "add a couple of vars" case).
        timeout: seconds before the child is killed and ``timed_out`` set.
        input_text: text piped to the child's stdin.
        check: when True, raise :class:`CommandError` (with stderr embedded) on any
            non-clean exit instead of returning it.

    Returns:
        A :class:`CommandRun` — inspect ``.ok`` / ``.returncode`` and, on failure,
        ``.stderr`` or ``.failure_excerpt()``. A missing executable yields
        ``returncode=127`` with the reason in ``stderr`` (not a raised
        ``FileNotFoundError``), so callers have one uniform failure shape.
    """
    full_env: dict[str, str] | None = None
    if env is not None or extra_env:
        full_env = dict(env) if env is not None else os.environ.copy()
        if extra_env:
            full_env.update(extra_env)

    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=full_env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        run = CommandRun(
            argv=list(argv),
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration_s=time.monotonic() - start,
        )
    except subprocess.TimeoutExpired as exc:
        run = CommandRun(
            argv=list(argv),
            returncode=-1,
            stdout=_decode(exc.stdout),
            stderr=_decode(exc.stderr),
            timed_out=True,
            duration_s=time.monotonic() - start,
        )
    except FileNotFoundError as exc:
        run = CommandRun(
            argv=list(argv),
            returncode=127,
            stderr=f"executable not found: {argv[0] if argv else '<empty argv>'} ({exc})",
            duration_s=time.monotonic() - start,
        )

    if check and not run.ok:
        raise CommandError(run)
    return run


def _decode(data: str | bytes | None) -> str:
    """Decode subprocess output that may arrive as str, bytes, or None."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data
