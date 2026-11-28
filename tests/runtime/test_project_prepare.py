"""Project preparation is shared by authoring, readiness, and execution."""

from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests.git_support import git
from tests.pipe_holding_tree import (
    PARENT_SCRIPT,
    heartbeat_tree,
    start_deadline_after_tree_start,
    wait_for_tree_start,
    wait_until_heartbeat_stops,
)
from tests.time_budget import hang_guard_s, scaled

import booley
from booley.runtime import platform_paths
from booley.runtime.project_prepare import prepare_project

# ``timeout_s`` is printed raw by ``prepare_project``, so it stays an ``int``.
_HOOK_TIMEOUT_S = math.ceil(scaled(2))
_BACKGROUND_CHILD_TIMEOUT_S = math.ceil(scaled(50))
_BACKGROUND_CHILD_BOUND_S = scaled(30)


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "rtl"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / ".gitignore").write_text("/generated.hex\n", encoding="utf-8")
    project = root / ".booley"
    (project / "hooks").mkdir(parents=True)
    (project / "hooks" / "post-setup.sh").write_text(
        "#!/bin/sh\nprintf 'firmware\\n' > generated.hex\n",
        encoding="utf-8",
    )
    git(root, "add", "-A")
    git(root, "commit", "-m", "initial")
    return root, project


def test_prepare_project_is_idempotent_and_never_commits_generated_files(tmp_path: Path) -> None:
    root, _booley_dir = _project(tmp_path)
    before = git(root, "rev-parse", "HEAD")

    first = prepare_project(root, root, slug="demo", sim_flow_enabled=True)
    second = prepare_project(root, root, slug="demo", sim_flow_enabled=True)

    assert first.ok and second.ok
    assert (root / "generated.hex").read_text(encoding="utf-8") == "firmware\n"
    assert git(root, "rev-parse", "HEAD") == before
    assert git(root, "status", "--porcelain") == ""


@pytest.mark.parametrize("rc", [0, 7], ids=["succeeds", "fails"])
def test_captured_hook_output_that_is_not_utf8_does_not_abort(tmp_path: Path, rc: int) -> None:
    root, project = _project(tmp_path)
    (project / "hooks" / "post-setup.sh").write_bytes(
        f"#!/bin/sh\nprintf 'broken\\377' >&2\nexit {rc}\n".encode()
    )

    result = prepare_project(root, root, slug="demo", sim_flow_enabled=True)

    assert result.ok is (rc == 0)
    assert rc == 0 or (f"rc={rc}" in result.error and "broken" in result.error)


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
            timeout_s=hang_guard_s(),
            resource_flags=(),
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


def _heartbeat_hook_checkout(
    tmp_path: Path, paths: dict[str, Path], mode: str = "live-until-stop"
) -> Path:
    """A checkout whose hook is ``PARENT_SCRIPT`` in *mode*: it spawns a heartbeat grandchild."""
    argv = [str(paths["heartbeat"]), str(paths["stop"]), str(paths["done"]), mode]
    body = f"import sys\nprint('started', flush=True)\nsys.argv[1:] = {argv!r}\n{PARENT_SCRIPT}"
    return _hook_checkout(tmp_path, body)


@pytest.mark.parametrize("logged", [False, True], ids=["captured", "logged"])
def test_hook_timeout_stops_the_hook_tree(tmp_path: Path, monkeypatch, logged: bool) -> None:
    log = tmp_path / "h.log" if logged else None
    with heartbeat_tree(tmp_path) as paths:
        root = _heartbeat_hook_checkout(tmp_path, paths)
        began = time.monotonic()
        if logged:
            # The logged path starts its deadline before it spawns the hook, so only
            # its tree kill can wait for the tree to start.  The wait is timed apart
            # from the timeout, and the kill still runs when the tree never started.
            real_kill, waited = platform_paths.kill_process_tree, []

            def kill(process: subprocess.Popen) -> None:
                wait_began = time.monotonic()
                try:
                    if not waited:  # the exception cleanup calls the kill a second time
                        wait_for_tree_start(paths)
                finally:
                    waited.append(time.monotonic() - wait_began)
                    real_kill(process)

            monkeypatch.setattr(platform_paths, "kill_process_tree", kill)
        else:
            started = start_deadline_after_tree_start(monkeypatch, paths)

        result = prepare_project(
            root, root, slug="s", sim_flow_enabled=False, timeout_s=_HOOK_TIMEOUT_S, log=log
        )

        # The timeout plus the helper's fixed 32 s Windows margin, under a scaled
        # bound below the grandchild's self-exit
        tree_started = began + waited[0] if logged else started[0]
        assert time.monotonic() - tree_started < scaled(38)
        assert not result.ok
        assert result.error == f"post-setup hook timed out ({_HOOK_TIMEOUT_S}s)"
        assert wait_until_heartbeat_stops(paths["heartbeat"]), "the descendant was left running"
        assert log is None or "started" in log.read_text(encoding="utf-8")


@pytest.mark.parametrize("logged", [False, True], ids=["captured", "logged"])
def test_hook_start_failure_is_reported(tmp_path: Path, monkeypatch, logged: bool) -> None:
    log = tmp_path / "h.log" if logged else None
    root = _hook_checkout(tmp_path, "pass\n")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "absent-python"))

    result = prepare_project(root, root, slug="s", sim_flow_enabled=False, log=log)

    assert not result.ok
    assert "OS error" in result.error


def test_captured_hook_is_stopped_with_its_descendants_when_interrupted(
    tmp_path: Path, monkeypatch
) -> None:
    real_communicate = subprocess.Popen.communicate

    def interrupt_once_started(self, *args, **kwargs):
        # Only the hook's own process: Windows tree-kill runs taskkill through Popen too.
        if any("post-setup" in str(arg) for arg in self.args):
            deadline = time.monotonic() + hang_guard_s()
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


def test_logged_hook_background_child_does_not_hold_success(tmp_path: Path) -> None:
    """A background child that keeps the log open neither delays a hook that exited nor dies."""
    with heartbeat_tree(tmp_path) as paths:
        root = _heartbeat_hook_checkout(tmp_path, paths, "exit")
        started = time.monotonic()
        result = prepare_project(
            root,
            root,
            slug="s",
            sim_flow_enabled=False,
            timeout_s=_BACKGROUND_CHILD_TIMEOUT_S,
            log=tmp_path / "h.log",
        )
        elapsed = time.monotonic() - started

        assert result.ok
        assert elapsed < _BACKGROUND_CHILD_BOUND_S
        wait_for_tree_start(paths)
        # A child killed at release could still land one beat: wait for several.
        size = paths["heartbeat"].stat().st_size
        deadline = time.monotonic() + hang_guard_s()
        while paths["heartbeat"].stat().st_size < size + 3:
            assert time.monotonic() < deadline, "the background child was stopped"
            time.sleep(0.05)


def test_logged_hook_is_stopped_with_its_descendants_when_interrupted(
    tmp_path: Path, monkeypatch
) -> None:
    from booley.runtime import project_prepare

    def interrupt_once_started() -> None:
        # `_wait_tick`: the only point inside the logged wait loop to inject an interrupt.
        if paths["heartbeat"].exists():
            raise KeyboardInterrupt("injected interrupt")
        time.sleep(0.05)

    monkeypatch.setattr(project_prepare, "_wait_tick", interrupt_once_started)
    with heartbeat_tree(tmp_path) as paths:
        root = _heartbeat_hook_checkout(tmp_path, paths)

        with pytest.raises(KeyboardInterrupt, match="injected interrupt"):
            prepare_project(root, root, slug="s", sim_flow_enabled=False, log=tmp_path / "h.log")

        assert wait_until_heartbeat_stops(paths["heartbeat"]), "the descendant was left running"
