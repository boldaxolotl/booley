"""Each checkout keeps its own bwave session store (issue #3483).

Registration and resolution run in child processes with an explicit working
directory and a private temporary directory, the way an agent shell and a traced
sim run reach the store. No native bwave binary is needed: registration calls
``register_trace`` (what ``bwave register`` calls after its binary probe) and
resolution calls ``_resolve_trace`` (what every query calls before the binary).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import booley
from tests.booley_checkouts_support import RUNTIMES, isolate_git, linked_worktree, main_project
from tests.subprocess_budget import run_bounded_child

_REGISTER = (
    "import sys; from pathlib import Path; from booley.bwave import cli; "
    "cli.register_trace(Path(sys.argv[1]), alias=sys.argv[2])"
)
# Moves the temporary directory after import: only a store resolved per call follows it.
_REGISTER_AFTER_TMPDIR_CHANGE = (
    "import sys, tempfile; from pathlib import Path; from booley.bwave import cli; "
    "tempfile.tempdir = sys.argv[3]; cli.register_trace(Path(sys.argv[1]), alias=sys.argv[2])"
)
_RESOLVE = "import sys; from booley.bwave import cli; print(cli._resolve_trace(sys.argv[1:]))"
_REGISTER_MANY = """
import sys
from pathlib import Path
from booley.bwave import sessions

for index in range(20):
    sessions.register_trace(Path(f"/traces/{sys.argv[1]}{index}.fst"), alias=f"{sys.argv[1]}{index}")
"""


def _env(tmp: Path) -> dict[str, str]:
    """A child environment with its own temporary directory, as one Sandbox has."""
    tmp.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "TMPDIR": str(tmp),
        "TEMP": str(tmp),
        "TMP": str(tmp),
        "PYTHONPATH": str(Path(booley.__file__).resolve().parents[1]),
        "BOOLEY_CONTAINER": "1",
    }
    env.pop("RTL_PROJECT_ROOT", None)
    return env


def _python(code: str, cwd: Path, env: dict[str, str], *argv: str | Path) -> tuple[int, str, str]:
    """Run *code* in a child of *cwd*; return its exit code, stdout and stderr."""
    import sys

    child = run_bounded_child([sys.executable, "-c", code, *map(str, argv)], cwd=cwd, env=env)
    return child.returncode, child.stdout, child.stderr


def _register(cwd: Path, env: dict[str, str], trace: Path, alias: str) -> None:
    code, _out, err = _python(_REGISTER, cwd, env, trace, alias)
    assert code == 0, err


def _resolve(cwd: Path, env: dict[str, str], *query: str) -> Path:
    """The trace a ``bwave`` query run from *cwd* resolves to."""
    code, out, err = _python(_RESOLVE, cwd, env, *query)
    assert code == 0, err
    return Path(out.strip()).resolve()


def _trace(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    trace = directory / name
    trace.write_bytes(b"waveform")
    return trace.resolve()


def _store(checkout: Path) -> Path:
    return checkout / ".booley" / ".runtime" / "bwave" / "bwave_sessions.json"


def _plain_project(root: Path) -> Path:
    """An initialized Project that is not a Git checkout."""
    (root / ".booley").mkdir(parents=True)
    (root / ".booley" / "booley.toml").write_text("", encoding="utf-8")
    return root


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_each_checkout_resolves_its_own_target_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    main, worktree = linked_worktree(tmp_path, monkeypatch, layout="snapshot", runtime=runtime)
    # One temporary directory for every child: the shared state of one Sandbox.
    env = _env(tmp_path / "shared-tmp")
    main_trace = _trace(main / "out", "main.fst")
    tree_trace = _trace(worktree / "out", "tree.fst")

    _register(main, env, main_trace, "dut")
    assert _store(main).is_file()
    main_store = _store(main).read_bytes()
    _register(worktree, env, tree_trace, "dut")

    assert _resolve(main, env, "@dut", "list") == main_trace
    assert _resolve(main, env, "list") == main_trace
    assert _resolve(worktree, env, "@dut", "list") == tree_trace
    assert _resolve(worktree, env, "list") == tree_trace
    assert _store(main).read_bytes() == main_store


def test_in_checkout_trace_resolves_after_the_checkout_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolate_git(tmp_path, monkeypatch)
    main = main_project(tmp_path)
    env = _env(tmp_path / "shared-tmp")
    _register(main, env, _trace(main / "out", "dut.fst"), "dut")
    outside = _trace(tmp_path / "elsewhere", "ext.fst")
    _register(main, env, outside, "ext")

    moved = main.rename(tmp_path / "moved")

    assert _resolve(moved, env, "@dut", "list") == (moved / "out" / "dut.fst").resolve()
    assert _resolve(moved, env, "@ext", "list") == outside


def test_subdirectory_of_a_non_git_project_reads_its_store(tmp_path: Path) -> None:
    project = _plain_project(tmp_path / "project")
    (project / "rtl").mkdir()
    env = _env(tmp_path / "shared-tmp")
    trace = _trace(project / "sim", "dut.fst")

    _register(project, env, trace, "dut")

    # The shell's temporary directory differs from the one the run registered in.
    shell_env = _env(tmp_path / "shell-tmp")
    assert _resolve(project / "rtl", shell_env, "@dut", "list") == trace


def test_outside_a_project_uses_the_temporary_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    plain = tmp_path / "plain"
    plain.mkdir()
    trace = _trace(plain, "dut.fst")
    imported_tmp = _env(tmp_path / "imported-tmp")
    later_tmp = tmp_path / "later-tmp"
    later_tmp.mkdir()

    code, _out, err = _python(
        _REGISTER_AFTER_TMPDIR_CHANGE, plain, imported_tmp, trace, "dut", later_tmp
    )

    assert code == 0, err
    assert (later_tmp / "bwave" / "bwave_sessions.json").is_file()
    assert _resolve(plain, _env(later_tmp), "@dut", "list") == trace
    assert not (plain / ".booley").exists()


def test_concurrent_registrations_keep_every_alias(tmp_path: Path) -> None:
    """Two traced runs finishing together must not drop each other's aliases."""
    import subprocess
    import sys

    project = _plain_project(tmp_path / "project")
    env = _env(tmp_path / "shared-tmp")
    children = [
        subprocess.Popen(
            [sys.executable, "-c", _REGISTER_MANY, prefix],
            cwd=project,
            env=env,
            stderr=subprocess.PIPE,
            text=True,
        )
        for prefix in ("a", "b")
    ]
    for child in children:
        # Kept on communicate: both children must run concurrently to race the store.
        _, stderr = child.communicate(timeout=120)
        assert child.returncode == 0, stderr

    aliases = set(json.loads(_store(project).read_text(encoding="utf-8")))
    assert {f"{prefix}{index}" for prefix in "ab" for index in range(20)} <= aliases
