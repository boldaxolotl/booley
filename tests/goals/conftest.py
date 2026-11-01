"""Goal Mode test support: a real Git repository laid out like a Booley Project.

The Main Booley Directory is ``<main>/.booley`` (excluded from
Git, as ``init`` does); the linked worktree lives under its ``worktrees/``
and carries a copied ``.booley`` snapshot. Only the Target catalog is
faked, so entry never needs FuseSoC.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.core.booley_dir import reset_cache
from booley.flows import target_test_plan_loading
from booley.flows.target_test_plan import TargetTestPlan
from booley.flows.test_runs import TestRun, build_test_run, test_run_document
from booley.goals import entry as entry_module
from booley.goals import test_run_freezing
from booley.goals.binding import GoalRunBinding, bind_run
from booley.goals.sim_baseline_store import family_for_schema
from booley.goals.store import GoalStore
from booley.mcp import (
    sim_baseline_jobs,
)
from booley.runtime import job_records
from booley.runtime.timefmt import utc_now_rfc3339
from booley.targets.domain import UnknownTargetError

GIT_TIMEOUT_S = 30
KNOWN_TARGETS = frozenset({"top", "base"})
DATE = "20261006"

# A valid, never-resolved set of entry input roots for hand-built Goal Records.
INPUT_PATHS: dict[str, Any] = {
    "rtl": {"path": "/work", "identity": [1, 2]},
    "project": {"path": "/work/.booley", "identity": [1, 3]},
}


def git(cwd: Path, *args: str) -> str:
    """Run git with a fixed identity and return its stripped output.

    Automatic maintenance is off: a detached repack started by one fixture
    commit would otherwise change the object store under a later assertion.
    """
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Goal Test",
            "-c",
            "user.email=goal@test.invalid",
            "-c",
            "core.autocrlf=false",
            "-c",
            "maintenance.auto=false",
            "-c",
            "gc.auto=0",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=GIT_TIMEOUT_S,
    )
    return result.stdout.strip()


def fake_target_identity(_work_dir: Path | str, target: str) -> str:
    """The identity of a known Target, written by name or as ``::name:0#name``."""
    name = target.rpartition("#")[2]
    if name not in KNOWN_TARGETS or target not in {name, f"::{name}:0#{name}"}:
        raise UnknownTargetError(target)
    return f"::{name}:0#{name}"


def fake_test_plan(work_dir: Path | str, target: str) -> TargetTestPlan:
    """The Test Run plan of a known Target: its ``tests.toml`` section on Verilator."""
    name = fake_target_identity(work_dir, target).rpartition("#")[2]
    return target_test_plan_loading.load_target_test_plan(
        work_dir, name, identity=name, adapter="verilator"
    )


class FakeCatalog:
    """Knows :data:`KNOWN_TARGETS`; any other name is unknown."""

    @classmethod
    def build(cls, _root: Path) -> SimpleNamespace:
        def select(name: str) -> SimpleNamespace:
            if name not in KNOWN_TARGETS:
                raise UnknownTargetError(name)
            return SimpleNamespace(selector=name)

        return SimpleNamespace(select=select)


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """A Project with a Main Booley Directory and one linked worktree."""
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    git(main, "config", "core.autocrlf", "false")
    (main / "booley.toml").write_text("# root config\n", encoding="utf-8")
    (main / "rtl.v").write_text("module top; endmodule\n", encoding="utf-8")
    (main / "docs").mkdir()
    (main / "docs" / "spec.md").write_text("spec\n", encoding="utf-8")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "base")
    (main / ".git" / "info" / "exclude").write_text("/.booley\n", encoding="utf-8")
    control = main / ".booley"
    write_project_files(control)
    worktree = control / "worktrees" / "wt"
    git(main, "worktree", "add", "-q", "-b", "work", str(worktree))
    write_project_files(worktree / ".booley")
    monkeypatch.setenv("BOOLEY_DIR", str(control))
    from booley.flows.endpoint_diff import _classify_files

    def classify_review_paths(paths, root):
        return {"rtl"} if list(paths) == ["rtl.v"] else _classify_files(paths, root)

    monkeypatch.setattr("booley.flows.review_scope._classify_files", classify_review_paths)
    monkeypatch.setattr(entry_module, "TargetCatalog", FakeCatalog)
    monkeypatch.setattr(target_test_plan_loading, "load_checkout_target_test_plan", fake_test_plan)
    monkeypatch.setattr(target_test_plan_loading, "checkout_target_identity", fake_target_identity)
    # A run being bound and a status read resolve pending slots without a seed source of
    # their own: give them a deterministic one.
    monkeypatch.setattr(test_run_freezing, "draw_seeds", counting_draw(501))
    stamps = iter(f"{DATE}T12{minute:02d}00Z" for minute in range(60))
    monkeypatch.setattr(entry_module, "compact_utc_now", lambda: next(stamps))
    # Active Goal calls schedule detached baseline Jobs. A real Job would run
    # without these fakes and race status reads, so keep it deterministically
    # pending; tests that need the real supervisor pass ``launch`` explicitly.
    monkeypatch.setattr(sim_baseline_jobs, "launch_baseline", pending_baseline_launch)
    reset_cache()
    yield SimpleNamespace(main=main, control=control, worktree=worktree)
    reset_cache()


def pending_baseline_launch(_path: Path, request: Mapping[str, Any]) -> None:
    """Record a live (this process) baseline Job without starting a worker."""
    job_records.write_record(
        job_records.JobRecord(
            request["job_id"],
            family_for_schema(request["schema"]).endpoint,
            utc_now_rfc3339(),
            sim_baseline_jobs.BASELINE_TIMEOUT_S,
            pid=os.getpid(),
            argv=[],
        ),
        Path(request["jobs_root"]),
    )


def write_project_files(booley_dir: Path) -> None:
    """A minimal Booley Directory: configuration and one custom MCP tool."""
    (booley_dir / "mcp_tools").mkdir(parents=True)
    (booley_dir / "booley.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (booley_dir / "mcp_tools" / "tool.py").write_text("# custom tool\n", encoding="utf-8")


def install_paired_project(layout: SimpleNamespace, tmp_path: Path) -> Path:
    """Replace the worktree's snapshot with a Paired Checkout.

    The paired branch has no upstream, like a Goal worktree's would.
    """
    project_repo = tmp_path / "project-repo"
    project_repo.mkdir()
    git(project_repo, "init", "-q", "-b", "main")
    git(project_repo, "config", "core.autocrlf", "false")
    (project_repo / "booley.toml").write_text("[project]\n", encoding="utf-8")
    git(project_repo, "add", "-A")
    git(project_repo, "commit", "-q", "-m", "project")
    snapshot = layout.worktree / ".booley"
    for path in sorted(snapshot.rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    snapshot.rmdir()
    git(project_repo, "worktree", "add", "-q", "--detach", str(snapshot))
    return snapshot


# ---------------------------------------------------------------------------
# An active Goal Mode (Phase 3 evidence tests)
# ---------------------------------------------------------------------------

TOP_CORE = (
    "CAPI=2:\n"
    "name: ::top:0\n"
    "filesets:\n"
    "  rtl: {files: [rtl.v], file_type: verilogSource}\n"
    "targets:\n"
    "  top: {filesets: [rtl], toplevel: top}\n"
)
#: The Goals the active Goal Mode fixture enters with.
GOAL_ARGS = (
    {"family": "LINT", "target": "top"},
    {"family": "SIM", "target": "top"},
)
LINT_KEY = "lint_clean_top"
SIM_KEY = "sim_pass_top"
#: The public name of the Goal :data:`SIM_KEY` is the key of.
SIM_NAME = "SIM:top (all selected tests pass)"


TESTS_TOML = b'[top]\ntests = ["smoke"]\n'


def counting_draw(first: int = 101) -> Any:
    """A seed source that hands out *first*, *first* + 1, ... and skips excluded seeds."""
    counter = iter(range(first, first + 10_000))

    def draw(count: int, *, exclude: Any = ()) -> tuple[int, ...]:
        drawn: list[int] = []
        while len(drawn) < count:
            seed = next(counter)
            if seed not in exclude:
                drawn.append(seed)
        return tuple(drawn)

    return draw


def entry_environment(layout: SimpleNamespace, **extra: Any) -> Any:
    """The entry environment of *layout*, with a deterministic seed source."""
    from booley.goals.entry import EntryEnvironment

    return EntryEnvironment(
        booley_dir=layout.control,
        draw_seeds=counting_draw(),
        **extra,
        routing_guidance="Explicit test routing guidance.",
    )


def write_tests_toml(layout: SimpleNamespace, content: bytes) -> None:
    """Rewrite the worktree's ``tests.toml`` (bytes: digests match on every platform)."""
    (layout.worktree / ".booley" / "tests.toml").write_bytes(content)


def enter_goals(
    layout: SimpleNamespace,
    goals: Sequence[Mapping[str, Any]] = GOAL_ARGS,
    *,
    tests_toml: bytes = TESTS_TOML,
    environment: Any = None,
) -> Any:
    """Commit a ``top`` core and a ``tests.toml`` in the worktree, then enter Goal Mode.

    *environment* replaces the default entry environment (for example, to freeze recipes).
    """
    from booley.goals.entry import EntryRequest, enter_goal_mode
    from booley.goals.model import parse_goal_args

    # Byte writes keep every digested file identical on every platform.
    (layout.worktree / "top.core").write_bytes(TOP_CORE.encode())
    (layout.worktree / "rtl.v").write_bytes(b"module top; endmodule\n")
    git(layout.worktree, "add", "top.core", "rtl.v")
    git(layout.worktree, "commit", "-q", "-m", "top core")
    write_tests_toml(layout, tests_toml)
    request = EntryRequest(
        work_dir=layout.worktree, slug="evidence", goals=parse_goal_args(list(goals))
    )
    return enter_goal_mode(request, environment or entry_environment(layout)).record


@pytest.fixture
def goal_mode(layout: SimpleNamespace) -> SimpleNamespace:
    """*layout* with an active Goal Mode on its worktree (lint and sim Goals on ``top``)."""
    record = enter_goals(layout)
    return SimpleNamespace(**vars(layout), record=record)


CAMPAIGN_ID = "12345678-1234-4234-9234-123456789abc"
#: The Test Run the ``goal_mode`` fixture's simulation Goal is frozen to.
SMOKE_RUN = build_test_run("smoke", seed=1)


def run_row(run: TestRun | str = SMOKE_RUN, grade: str = "pass") -> dict[str, Any]:
    """The Test Run row a campaign records for *run* (a name means Test at seed 1)."""
    if isinstance(run, str):
        run = build_test_run(run, seed=1)
    return {**test_run_document(run), "grade": grade}


def passing_sim(*runs: TestRun | str) -> dict[str, Any]:
    """Simulation detail of a campaign in which *runs* (default: ``smoke@1``) passed."""
    rows = [run_row(run) for run in runs or (SMOKE_RUN,)]
    return {"tests_passed": len(rows), "tests_total": len(rows), "test_runs": rows}


def campaign_facts(campaign_id: str = CAMPAIGN_ID) -> dict[str, Any]:
    """Simulation acceptance facts the ledger accepts (content is not interpreted)."""
    return {
        "$schema": "booley.simulation-acceptance-facts/v1",
        "campaign_id": campaign_id,
        "manifest_sha256": f"sha256:{'a' * 64}",
        "origin": {"execution_id": "b" * 32, "invocation_id": 7},
        "target": {"identity": "::top:0#top", "selector": "top"},
        "required_suite": {"names": ["smoke"], "default_invocation": False},
        "prerequisites": [],
        "consumed_results": [{"finished_at": "2026-10-07T10:00:00Z"}],
        "observations": [],
        "coverage_reference": None,
    }


def bind(layout: SimpleNamespace, invocation_id: str = "run-1") -> GoalRunBinding:
    """Bind a run in *layout*'s worktree to its active Goal Mode."""
    return bind_run(GoalStore(layout.control), layout.worktree, invocation_id)


def update_record(layout: SimpleNamespace, **changes: Any) -> None:
    """Save the worktree's occupying record with *changes*."""
    store = GoalStore(layout.control)
    record = store.active_for_worktree(layout.worktree)
    assert record is not None
    with store.record_lock(record.id) as lock:
        store.save(lock, replace(record, **changes))


def bump_spec(layout: SimpleNamespace, key: str) -> None:
    """Save the record with Goal *key* changed, as an applied Goal Change would."""
    store = GoalStore(layout.control)
    record = store.active_for_worktree(layout.worktree)
    assert record is not None
    goals = tuple(
        replace(goal, spec_revision=record.revision + 1) if goal.spec.key == key else goal
        for goal in record.goals
    )
    with store.record_lock(record.id) as lock:
        store.save(lock, replace(record, goals=goals))


def edit_protected(layout: SimpleNamespace, text: str = "[project]\nname = 'edited'\n") -> str:
    """Rewrite the Main Booley Directory's ``booley.toml`` (a Protected Input); return what it held."""
    path = layout.control / "booley.toml"
    before = path.read_text(encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    return before


def tree_digest(root: Path) -> dict[str, str]:
    """Every file beneath *root* by content, lock files excepted (they hold no evidence)."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name not in {"lock", ".sequence.lock"}
    }
