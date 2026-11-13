"""Project preparation is shared by authoring, readiness, and execution."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests.pipe_holding_tree import (
    PARENT_SCRIPT,
    heartbeat_tree,
    wait_until_heartbeat_stops,
)
from tests.subprocess_budget import run_bounded_child

import booley
from booley.runtime.project_prepare import prepare_project


def _git(repo: Path, *args: str) -> str:
    return run_bounded_child(["git", *args], cwd=repo, check=True).stdout.strip()


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "rtl"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / ".gitignore").write_text("/generated.hex\n", encoding="utf-8")
    project = root / ".booley"
    (project / "hooks").mkdir(parents=True)
    (project / "hooks" / "post-setup.sh").write_text(
        "#!/bin/sh\nprintf 'firmware\\n' > generated.hex\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    return root, project


def test_prepare_project_is_idempotent_and_never_commits_generated_files(tmp_path: Path) -> None:
    root, _booley_dir = _project(tmp_path)
    before = _git(root, "rev-parse", "HEAD")

    first = prepare_project(root, root, slug="demo", sim_flow_enabled=True)
    second = prepare_project(root, root, slug="demo", sim_flow_enabled=True)

    assert first.ok and second.ok
    assert (root / "generated.hex").read_text(encoding="utf-8") == "firmware\n"
    assert _git(root, "rev-parse", "HEAD") == before
    assert _git(root, "status", "--porcelain") == ""


def test_prepare_project_returns_hook_failure(tmp_path: Path) -> None:
    root, project = _project(tmp_path)
    (project / "hooks" / "post-setup.sh").write_text(
        "#!/bin/sh\necho broken >&2\nexit 7\n", encoding="utf-8"
    )

    result = prepare_project(root, root, slug="demo", sim_flow_enabled=True)

    assert not result.ok
    assert "rc=7" in result.error
    assert "broken" in result.error


#: A Booley command in miniature: report where Booley would put slots and handles.
_REPORT_ROOTS = """\
import json, os, sys
from booley.runtime import job_slots
from booley.runtime.booley_dir import resolve_common_booley_dir

with open(sys.argv[1], "w", encoding="utf-8") as out:
    json.dump(
        {
            "booley_dir": os.environ["BOOLEY_DIR"],
            "slots": str(job_slots.slots_dir()),
            "handles": str(resolve_common_booley_dir()),
        },
        out,
    )
"""


@pytest.mark.parametrize("hook", ["post-setup", "pre-sim"])
def test_booley_commands_from_checkout_hooks_reach_main_common_state(
    tmp_path: Path, monkeypatch, request: pytest.FixtureRequest, hook: str
) -> None:
    """A worktree hook sees the worktree's directory yet shares main's slots and handles."""
    from tests.booley_checkouts_support import linked_worktree

    from booley.runtime.booley_dir import reset_cache

    main, worktree = linked_worktree(tmp_path, monkeypatch, layout="snapshot", runtime="sandbox")
    request.addfinalizer(reset_cache)  # also on failure: no stale resolver cache
    monkeypatch.delenv("BOOLEY_SLOTS_DIR", raising=False)
    # The child interpreter must import the Booley under test, installed or not.
    monkeypatch.setenv("PYTHONPATH", str(Path(booley.__file__).resolve().parent.parent))
    report = tmp_path / "roots.json"
    script = tmp_path / "report_roots.py"
    script.write_text(_REPORT_ROOTS, encoding="utf-8")
    reset_cache()

    if hook == "post-setup":
        hooks = worktree / ".booley" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "post-setup.py").write_text(
            "import runpy, sys\n"
            f"sys.argv = ['report', {str(report)!r}]\n"
            f"runpy.run_path({str(script)!r}, run_name='__main__')\n",
            encoding="utf-8",
        )
        assert prepare_project(worktree, worktree, slug="s", sim_flow_enabled=False).ok
    else:
        from types import SimpleNamespace
        from typing import cast

        from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
        from booley.targets.domain import TargetHandle

        handle = cast(TargetHandle, SimpleNamespace(project_root=worktree.resolve(), selector="t"))
        evidence = run_pre_sim_commands(
            handle,
            test_names=("smoke",),
            build_root=tmp_path / "build",
            eda_tool="verilator",
            timeout_s=60,
            commands=(f'"{sys.executable}" "{script}" "{report}"',),
        )
        assert evidence is not None and evidence.status == "passed", evidence

    seen = json.loads(report.read_text(encoding="utf-8"))
    common = (main / ".booley").resolve()
    assert Path(seen["booley_dir"]) == worktree / ".booley"
    assert Path(seen["slots"]).resolve().is_relative_to(common)
    assert Path(seen["handles"]).resolve() == common


def _hook_checkout(tmp_path: Path, body: str) -> Path:
    """A plain checkout whose ``.booley/hooks/post-setup.py`` runs *body*."""
    root = tmp_path / "logged"
    hooks = root / ".booley" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "post-setup.py").write_text(body, encoding="utf-8")
    return root


def _heartbeat_hook_checkout(tmp_path: Path, paths: dict[str, Path]) -> Path:
    """A checkout whose hook is ``PARENT_SCRIPT``: it spawns a heartbeat grandchild and waits."""
    argv = [str(paths["heartbeat"]), str(paths["stop"]), str(paths["done"]), "live-until-stop"]
    return _hook_checkout(tmp_path, f"import sys\nsys.argv[1:] = {argv!r}\n{PARENT_SCRIPT}")


def test_captured_hook_timeout_stops_the_hook_tree(tmp_path: Path) -> None:
    with heartbeat_tree(tmp_path) as paths:
        root = _heartbeat_hook_checkout(tmp_path, paths)
        started = time.monotonic()

        result = prepare_project(root, root, slug="s", sim_flow_enabled=False, timeout_s=2)

        # 2 s + the helper's 32 s Windows margin, below the grandchild's 40 s self-exit
        assert time.monotonic() - started < 38
        assert not result.ok and result.error == "post-setup hook timed out (2s)"
        assert wait_until_heartbeat_stops(paths["heartbeat"]), "the descendant was left running"


def test_captured_hook_start_failure_is_reported(tmp_path: Path, monkeypatch) -> None:
    root = _hook_checkout(tmp_path, "pass\n")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "absent-python"))

    result = prepare_project(root, root, slug="s", sim_flow_enabled=False)

    assert not result.ok
    assert "OS error" in result.error


def test_captured_hook_is_stopped_with_its_descendants_when_interrupted(
    tmp_path: Path, monkeypatch
) -> None:
    real_communicate = subprocess.Popen.communicate

    def interrupt_once_started(self, *args, **kwargs):
        # Only the hook's own process: Windows tree-kill runs taskkill through Popen too.
        if any("post-setup" in str(arg) for arg in self.args):
            deadline = time.monotonic() + 15
            while not paths["heartbeat"].exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            raise KeyboardInterrupt("injected interrupt")
        return real_communicate(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupt_once_started)
    with heartbeat_tree(tmp_path) as paths:
        root = _heartbeat_hook_checkout(tmp_path, paths)

        with pytest.raises(KeyboardInterrupt, match="injected interrupt"):
            prepare_project(root, root, slug="s", sim_flow_enabled=False)

        assert paths["heartbeat"].exists()
        assert wait_until_heartbeat_stops(paths["heartbeat"]), "the descendant was left running"


def test_logged_hook_output_reaches_log_and_stderr(tmp_path: Path, capsys) -> None:
    root = _hook_checkout(tmp_path, "import sys\nprint('out')\nprint('err', file=sys.stderr)\n")
    log = tmp_path / "logs" / "hook.log"

    result = prepare_project(root, root, slug="s", sim_flow_enabled=False, log=log)

    assert result.ok
    assert sorted(log.read_text(encoding="utf-8").split()) == ["err", "out"]
    assert sorted(capsys.readouterr().err.split()) == ["err", "out"]


def test_logged_hook_failure_reports_rc_without_output_tail(tmp_path: Path) -> None:
    root = _hook_checkout(tmp_path, "import sys\nprint('broken')\nsys.exit(4)\n")

    result = prepare_project(root, root, slug="s", sim_flow_enabled=False, log=tmp_path / "h.log")

    assert not result.ok
    assert result.error == "post-setup hook failed (rc=4)"


def test_logged_hook_start_failure_is_reported(tmp_path: Path, monkeypatch) -> None:
    root = _hook_checkout(tmp_path, "pass\n")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "absent-python"))

    result = prepare_project(root, root, slug="s", sim_flow_enabled=False, log=tmp_path / "h.log")

    assert not result.ok
    assert "OS error" in result.error


def test_logged_hook_timeout_kills_the_hook(tmp_path: Path) -> None:
    root = _hook_checkout(tmp_path, "import time\nprint('started', flush=True)\ntime.sleep(60)\n")
    log = tmp_path / "h.log"
    started = time.monotonic()

    result = prepare_project(root, root, slug="s", sim_flow_enabled=False, timeout_s=1, log=log)

    assert not result.ok and "timed out" in result.error
    assert time.monotonic() - started < 30
    assert "started" in log.read_text(encoding="utf-8")


def test_logged_hook_background_child_does_not_hold_success(tmp_path: Path) -> None:
    """A detached child that keeps the log open must not delay a hook that already exited."""
    child_pid_file = tmp_path / "child.pid"
    root = _hook_checkout(
        tmp_path,
        "import pathlib, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid), encoding='utf-8')\n"
        "print('parent done')\n",
    )
    started = time.monotonic()
    try:
        result = prepare_project(
            root, root, slug="s", sim_flow_enabled=False, timeout_s=50, log=tmp_path / "h.log"
        )
        elapsed = time.monotonic() - started
    finally:
        _stop_process(child_pid_file)

    assert result.ok
    assert elapsed < 30


def _stop_process(pid_file: Path) -> None:
    """Terminate the process whose PID the test's hook recorded, if it got that far."""
    if not pid_file.exists():
        return
    pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while os.name != "nt" and time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)


def test_logged_hook_is_killed_when_interrupted(tmp_path: Path, monkeypatch) -> None:
    from booley.runtime import project_prepare

    pid_file = tmp_path / "hook.pid"
    root = _hook_checkout(
        tmp_path,
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(60)\n",
    )

    def interrupt_once_started() -> None:
        if pid_file.exists() and pid_file.read_text(encoding="utf-8"):
            raise KeyboardInterrupt("injected interrupt")
        time.sleep(0.05)

    monkeypatch.setattr(project_prepare, "_wait_tick", interrupt_once_started)

    with pytest.raises(KeyboardInterrupt, match="injected interrupt"):
        prepare_project(root, root, slug="s", sim_flow_enabled=False, log=tmp_path / "h.log")

    pid = int(pid_file.read_text(encoding="utf-8"))
    if os.name == "nt":
        pytest.skip("the exit probe uses POSIX signals")
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_unlogged_hook_is_found_without_running_it(tmp_path: Path) -> None:
    from booley.runtime.project_prepare import find_post_setup_hook

    root = _hook_checkout(tmp_path, "raise SystemExit(9)\n")

    assert find_post_setup_hook(root) == root / ".booley" / "hooks" / "post-setup.py"
    bare = tmp_path / "bare"
    (bare / ".booley").mkdir(parents=True)
    assert find_post_setup_hook(bare) is None
