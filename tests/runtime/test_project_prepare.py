"""Project preparation is shared by authoring, readiness, and execution."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S

import booley
from booley.runtime.project_prepare import prepare_project


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    ).stdout.strip()


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
    tmp_path: Path, monkeypatch, hook: str
) -> None:
    """A worktree hook sees the worktree's directory yet shares main's slots and handles."""
    from tests.booley_checkouts_support import linked_worktree

    from booley.runtime.booley_dir import reset_cache

    main, worktree = linked_worktree(tmp_path, monkeypatch, layout="snapshot", runtime="sandbox")
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
        assert prepare_project(main, worktree, slug="s", sim_flow_enabled=False).ok
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
    reset_cache()
