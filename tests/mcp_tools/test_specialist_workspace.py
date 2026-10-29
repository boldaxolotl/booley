"""Tests for provider-independent read-only Specialist workspaces."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.core.models import AgentCallParams, AgentResult
from booley.specialists.reviewer import ReviewerSpecialist
from booley.specialists.specialist_workspace import (
    isolated_agent_workspace,
    restore_result_paths,
)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "--quiet"], repo)
    (repo / "tracked.sv").write_text("old\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    _git(["add", "tracked.sv", ".gitignore"], repo)
    _git(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--quiet",
            "-m",
            "initial",
        ],
        repo,
    )
    return repo


def test_read_only_snapshot_reflects_live_tree_and_discards_writes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tracked.sv").write_text("current\n", encoding="utf-8")
    (repo / "new.sv").write_text("new\n", encoding="utf-8")
    (repo / "ignored.log").write_text("large output\n", encoding="utf-8")
    params = AgentCallParams(
        prompt=f"Review {repo / 'tracked.sv'}",
        model="test",
        cwd=repo,
    )

    with isolated_agent_workspace(params, "read_only") as (isolated, snapshot):
        assert snapshot is not None
        isolated_root = Path(isolated.cwd)
        assert isolated_root != repo
        assert (isolated_root / "tracked.sv").read_text(encoding="utf-8") == "current\n"
        assert (isolated_root / "new.sv").read_text(encoding="utf-8") == "new\n"
        assert not (isolated_root / "ignored.log").exists()
        assert str(isolated_root) in isolated.prompt
        (isolated_root / "tracked.sv").write_text("agent edit\n", encoding="utf-8")

    assert (repo / "tracked.sv").read_text(encoding="utf-8") == "current\n"


def test_read_write_uses_original_workspace(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    params = AgentCallParams(prompt="work", model="test", cwd=repo)

    with isolated_agent_workspace(params, "read_write") as (actual, snapshot):
        assert actual is params
        assert snapshot is None


@pytest.mark.parametrize(
    ("category", "hidden_dir", "visible_dir"),
    [("rtl", "tb", "rtl"), ("tb", "rtl", "tb")],
)
def test_category_isolation_only_hides_snapshot_sources(
    tmp_path: Path,
    category: str,
    hidden_dir: str,
    visible_dir: str,
) -> None:
    """A concurrent tool always retains every source tree in the live worktree."""
    repo = _repo(tmp_path)
    for directory in ("rtl", "tb"):
        source = repo / directory / "source.sv"
        source.parent.mkdir()
        source.write_text(f"// {directory}\n", encoding="utf-8")
    (repo / "design.core").write_text(
        "CAPI=2:\nname: ::snapshot:0\nfilesets:\n  rtl: {files: [rtl/source.sv]}\n  tb: {files: [tb/source.sv], tags: [tb]}\n"
    )
    _git(["add", "rtl/source.sv", "tb/source.sv"], repo)
    params = AgentCallParams(prompt="review", model="test", cwd=repo)

    with isolated_agent_workspace(params, "read_only", category) as (isolated, _snapshot):
        isolated_root = Path(isolated.cwd)
        assert not (isolated_root / hidden_dir / "source.sv").exists()
        assert (isolated_root / visible_dir / "source.sv").is_file()
        assert (repo / "rtl" / "source.sv").is_file()
        assert (repo / "tb" / "source.sv").is_file()
        history = subprocess.run(
            ["git", "show", f"HEAD:{hidden_dir}/source.sv"],
            cwd=isolated_root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert history.returncode != 0
        assert f"// {hidden_dir}" not in history.stdout
        _git(["show", f"HEAD:{visible_dir}/source.sv"], isolated_root)

    assert (repo / "rtl" / "source.sv").is_file()
    assert (repo / "tb" / "source.sv").is_file()


def test_snapshot_does_not_expose_external_symlink_target(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    external = tmp_path / "external.txt"
    external.write_text("host data\n", encoding="utf-8")
    (repo / "external-link").symlink_to(external)
    _git(["add", "external-link"], repo)
    params = AgentCallParams(prompt="review", model="test", cwd=repo)

    with isolated_agent_workspace(params, "read_only") as (isolated, _snapshot):
        assert not (Path(isolated.cwd) / "external-link").exists()

    assert external.read_text(encoding="utf-8") == "host data\n"


def test_unknown_access_fails_loud(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    params = AgentCallParams(prompt="work", model="test", cwd=repo)

    with (
        pytest.raises(ValueError, match="workspace_access"),
        isolated_agent_workspace(params, "typo"),  # type: ignore[arg-type]
    ):
        pass


def test_result_paths_are_restored(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    params = AgentCallParams(prompt="work", model="test", cwd=repo)

    with isolated_agent_workspace(params, "read_only") as (_, snapshot):
        assert snapshot is not None
        temporary = str(snapshot.snapshot_root / "tracked.sv")
        result = AgentResult(
            output=f"issue in {temporary}",
            structured={"path": temporary},
            captured_agent_capability_calls={"ReportFindings": [{"path": temporary}]},
        )
        restored = restore_result_paths(result, snapshot)

    expected = str(repo / "tracked.sv")
    assert expected in restored.output
    assert restored.structured == {"path": expected}
    assert restored.captured_agent_capability_calls["ReportFindings"] == [{"path": expected}]


def test_reviewer_invokes_agent_in_snapshot(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    (repo / "rtl").mkdir()
    (repo / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    (repo / "tb").mkdir()
    (repo / "tb" / "tb.sv").write_text("module tb; endmodule\n", encoding="utf-8")
    from tests.agent_workspace.test_isolation import _write_core

    _write_core(repo)
    _git(["add", "rtl/dut.sv", "tb/tb.sv"], repo)
    caller = repo / "caller"
    (caller / "out").mkdir(parents=True)
    (caller / "out/report.md").write_text("embedded opposite source\n")
    (repo / "out").mkdir()
    (repo / "out/keep.md").write_text("authored support\n")
    monkeypatch.chdir(caller)
    params = AgentCallParams(prompt="review", model="test", cwd=repo)

    def fake_call(call_params: AgentCallParams, *, on_event) -> AgentResult:
        assert on_event is not None
        assert not (Path(call_params.cwd) / "caller/out").exists()
        assert (Path(call_params.cwd) / "out/keep.md").is_file()
        isolated_file = Path(call_params.cwd) / "tracked.sv"
        assert Path(call_params.cwd) != repo
        assert (Path(call_params.cwd) / "rtl" / "dut.sv").is_file()
        assert not (Path(call_params.cwd) / "tb/tb.sv").exists()
        assert (repo / "rtl" / "dut.sv").is_file()
        assert (repo / "tb" / "tb.sv").is_file()
        isolated_file.write_text("agent edit\n", encoding="utf-8")
        return AgentResult(output=f"finding in {isolated_file}")

    endpoint = ReviewerSpecialist()
    endpoint._args = argparse.Namespace(category="rtl", scope="tracked.sv", report_dir=Path("out"))
    with (
        patch("booley.specialists.specialist._call_agent_sync", side_effect=fake_call),
        patch("booley.specialists.specialist.parent_death_watchdog", return_value=nullcontext()),
    ):
        result = endpoint._invoke_agent(params, on_event=lambda _event: None)

    assert (repo / "tracked.sv").read_text(encoding="utf-8") == "old\n"
    assert (repo / "rtl" / "dut.sv").is_file()
    assert (repo / "tb" / "tb.sv").is_file()
    assert str(repo / "tracked.sv") in result.output


def test_snapshot_omits_ignored_managed_bundle_and_retains_authored_inputs(tmp_path):
    from booley.harness.setup.project_git_hook_bundle import build_project_git_hook_bundle
    from booley.runtime.project_gitignore import PROJECT_GITIGNORE

    repo = _repo(tmp_path)
    data = repo / ".booley"
    (data / "hooks").mkdir(parents=True)
    (data / ".gitignore").write_text(PROJECT_GITIGNORE)
    (data / "booley.toml").write_text("[project]\nname='fixture'\n")
    (data / "hooks/check.py").write_text("# authored lifecycle hook\n")
    bundle = data / ".managed/project-git-hooks.pyz"
    bundle.parent.mkdir()
    bundle.write_bytes(build_project_git_hook_bundle().content)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "add",
            "-f",
            ".booley/.gitignore",
            ".booley/booley.toml",
            ".booley/hooks/check.py",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    params = AgentCallParams(prompt="review", model="test", cwd=repo)
    with isolated_agent_workspace(params, "read_only") as (isolated, _snapshot):
        copied = Path(isolated.cwd) / ".booley"
        assert not (copied / ".managed/project-git-hooks.pyz").exists()
        assert (copied / "booley.toml").read_bytes() == (data / "booley.toml").read_bytes()
        assert (copied / "hooks/check.py").read_bytes() == (data / "hooks/check.py").read_bytes()
    assert bundle.is_file()


def _mutation_endpoint(repo: Path, monkeypatch):
    from booley.specialists.mutation_tester import MutationTesterSpecialist

    endpoint = MutationTesterSpecialist()
    endpoint.parse_args(["--work-dir", str(repo), "--target", "sim", "--scope", "rtl/dut.sv"])
    monkeypatch.setattr(endpoint, "_resolve_model", lambda: "test")
    monkeypatch.setattr(endpoint, "_session_file_path", lambda _key: repo.parent / "session")
    monkeypatch.setattr(
        "booley.specialists.specialist.parent_death_watchdog", lambda *_: nullcontext()
    )
    return endpoint


def _mutation_repo(tmp_path: Path, *, tb_dir="tb"):
    from tests.agent_workspace.test_isolation import _write_core

    repo = _repo(tmp_path)
    _write_core(repo, tb_dirs=[tb_dir])
    for rel, text in [
        ("rtl/dut.sv", "assign x = a + b;\n"),
        (f"{tb_dir}/tb.sv", "// testbench\n"),
    ]:
        path = repo / rel
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
    _git(["add", "."], repo)
    _git(
        ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "sources"],
        repo,
    )
    (repo / "rtl/dut.sv").write_text("assign x = live + b;\n")
    (repo / "rtl/new.sv").write_text("// untracked RTL\n")
    return repo


def _mutation_output(path: str):
    import json

    return json.dumps(
        {
            "mutations": [
                {
                    "index": 1,
                    "category": "operator",
                    "file": path,
                    "line": 1,
                    "original_code": "live + b",
                    "mutated_code": "live - b",
                }
            ]
        }
    )


def _mutation_plan(repo):
    from booley.specialists.mutation_tester import MutationRunPlan

    return MutationRunPlan(
        scope_files=["rtl/dut.sv"],
        scope_hashes={},
        work_dir=repo,
        target="sim",
        tb_top="tb",
        report_dir=None,
        min_detected=1,
        count=1,
        auto_mode=False,
        formula_count=1,
        source_size_budget=None,
    )


@pytest.mark.parametrize("tb_dir", ["tb", "verification"])
@pytest.mark.parametrize("absolute", [False, True])
def test_mutation_snapshot_proposals_resolve_live_wip(tmp_path, monkeypatch, tb_dir, absolute):
    repo = _mutation_repo(tmp_path, tb_dir=tb_dir)
    endpoint = _mutation_endpoint(repo, monkeypatch)
    snapshots = []
    before = sorted(str(p.relative_to(repo)) for p in repo.rglob("*") if ".git" not in p.parts)

    def provider(params, *, on_event):
        root = Path(params.cwd)
        snapshots.append(root)
        assert root != repo
        assert (repo / tb_dir / "tb.sv").is_file()
        assert (
            sorted(str(p.relative_to(repo)) for p in repo.rglob("*") if ".git" not in p.parts)
            == before
        )
        assert not (root / tb_dir / "tb.sv").exists()
        assert not (root / "rtl/new.sv").exists()  # unscoped and unregistered
        assert (root / "rtl/dut.sv").read_text() == "assign x = live + b;\n"
        assert params.allowed_agent_capabilities == ["Read", "Grep", "Glob"]
        assert str(root / "rtl/dut.sv") in params.prompt
        (root / "rtl/dut.sv").write_text("snapshot edit\n")
        # A concurrent author must survive proposer cleanup too.
        (repo / "tracked.sv").write_text("concurrent author\n")
        path = str(root / "rtl/dut.sv") if absolute else "rtl/dut.sv"
        return AgentResult(output=_mutation_output(path), session_id="resume-id")

    monkeypatch.setattr("booley.specialists.specialist._call_agent_sync", provider)
    specs, _, error = endpoint._proposal_round(f"Read {repo / 'rtl/dut.sv'}", 1)
    assert error is None
    assert specs[0].file == "rtl/dut.sv"
    resolved, problem = endpoint._resolve_proposals(specs, _mutation_plan(repo))
    assert problem == "" and resolved is not None
    assert resolved.mutations[0].original == b"live + b"
    assert resolved.mutations[0].line == 1
    assert (repo / "rtl/dut.sv").read_text() == "assign x = live + b;\n"
    assert (repo / "tracked.sv").read_text() == "concurrent author\n"
    assert all(not root.exists() for root in snapshots)


@pytest.mark.parametrize("returned_path", ["current", "stale", "outside"])
def test_mutation_snapshot_resume_paths(tmp_path, monkeypatch, returned_path):
    repo = _mutation_repo(tmp_path)
    endpoint = _mutation_endpoint(repo, monkeypatch)
    snapshots = []

    def provider(params, *, on_event):
        root = Path(params.cwd)
        assert root != repo
        assert (repo / "tb/tb.sv").is_file()
        assert not (root / "tb/tb.sv").exists()
        if snapshots:
            assert params.resume_session and params.session_id == "resume-id"
            assert not snapshots[0].exists()
            assert root != snapshots[0]
        snapshots.append(root)
        path = root / "rtl/dut.sv"
        if len(snapshots) == 2 and returned_path == "stale":
            path = snapshots[0] / "rtl/dut.sv"
        elif len(snapshots) == 2 and returned_path == "outside":
            path = tmp_path / "outside.sv"
        return AgentResult(output=_mutation_output(str(path)), session_id="resume-id")

    monkeypatch.setattr("booley.specialists.specialist._call_agent_sync", provider)
    for round_idx in (1, 2):
        specs, _, error = endpoint._proposal_round("propose", round_idx)
        assert error is None
        resolved, problem = endpoint._resolve_proposals(specs, _mutation_plan(repo))
        if round_idx == 2 and returned_path != "current":
            assert resolved is None and "escapes scope" in problem
        else:
            assert resolved is not None and problem == ""
    assert all(not root.exists() for root in snapshots)


def test_mutation_snapshot_cleanup_on_provider_exception(tmp_path, monkeypatch):
    repo = _mutation_repo(tmp_path)
    endpoint = _mutation_endpoint(repo, monkeypatch)
    snapshots = []

    def provider(params, *, on_event):
        snapshots.append(Path(params.cwd))
        assert Path(params.cwd) != repo
        assert (repo / "tb/tb.sv").is_file()
        raise RuntimeError("provider failed")

    monkeypatch.setattr("booley.specialists.specialist._call_agent_sync", provider)
    specs, elapsed, error = endpoint._proposal_round("propose", 1)
    assert specs == [] and elapsed == 0 and error.exit_code == 2
    assert (repo / "tb/tb.sv").is_file()
    assert all(not root.exists() for root in snapshots)


def test_result_json_windows_snapshot_paths_are_restored():
    import json

    from booley.specialists.specialist_workspace import WorkspaceSnapshot

    snapshot = WorkspaceSnapshot(
        real_root=Path(r"C:\live"), snapshot_root=Path(r"D:\temp\snapshot")
    )
    result = AgentResult(output=json.dumps({"file": str(snapshot.snapshot_root) + r"\rtl\dut.sv"}))
    restored = restore_result_paths(result, snapshot)
    assert json.loads(restored.output)["file"] == str(snapshot.real_root) + r"\rtl\dut.sv"


@pytest.mark.parametrize("category", ["rtl", "tb"])
def test_shared_directory_snapshot_removes_exact_opposite_sources(tmp_path, category):
    repo = _repo(tmp_path)
    (repo / "shared").mkdir()
    for name in ("dut.sv", "tb.sv", "sim_main.cpp"):
        (repo / "shared" / name).write_text("// source\n")
    (repo / "design.core").write_text("""CAPI=2:
name: ::isolation:0
filesets:
  rtl:
    files: [shared/dut.sv]
    file_type: systemVerilogSource
  tb:
    files: [shared/tb.sv]
    tags: [tb]
    file_type: systemVerilogSource
  harness:
    files: [shared/sim_main.cpp]
    file_type: cppSource
targets:
  sim:
    default_tool: verilator
    filesets: [rtl, tb, harness]
    toplevel: tb
""")
    params = AgentCallParams(prompt="review", model="test", cwd=repo)
    with isolated_agent_workspace(params, "read_only", category) as (isolated, _):
        root = Path(isolated.cwd)
        assert (root / "shared/dut.sv").exists() == (category == "rtl")
        assert (root / "shared/tb.sv").exists() == (category == "tb")
        assert (root / "shared/sim_main.cpp").exists() == (category == "tb")


def test_category_refuses_no_core_but_diagnostic_snapshot_remains_usable(tmp_path):
    from booley.fusesoc.fusesoc_registry import FuseSocError

    repo = _repo(tmp_path)
    params = AgentCallParams(prompt="review", model="test", cwd=repo)
    with (
        pytest.raises(FuseSocError, match=r"usable .core"),
        isolated_agent_workspace(params, "read_only", "rtl"),
    ):
        pytest.fail("launched without classification")
    with isolated_agent_workspace(params, "read_only") as (isolated, _):
        assert (Path(isolated.cwd) / "tracked.sv").is_file()


@pytest.mark.parametrize("category", ["rtl", "tb"])
@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("config_name", ["booley.toml", "pipeline.toml"])
def test_snapshot_sanitizes_resolved_copy_roots_and_unregistered_scope(
    tmp_path, monkeypatch, category, external, caplog, config_name
):
    from booley.runtime.booley_dir import reset_cache
    from tests.test_target_catalog import _category_sim_core

    repo = _repo(tmp_path)
    _category_sim_core(repo)
    data = tmp_path / "external-state" if external else repo / "project-state"
    data.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(repo / "custom-logs"))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(repo / "custom-runtime"))
    reset_cache()
    (data / config_name).write_text(
        '[project]\nname="fixture"\n[flows.sim]\noutput_dir="custom-output"\n'
    )
    roots = [
        data / "selftest/sim/bad-overlay",
        data / ".baseline-wt-stale",
        data / "worktrees/case",
        data / ".runtime/mutation_tester/lock/variants",
        repo / "custom-logs/reverted",
        repo / "custom-runtime",
        repo / "custom-output",
        repo / "current-report",
    ]
    for root in roots:
        root.mkdir(parents=True)
        (root / "copied.sv").write_text("module private_copy; endmodule\n")
        (root / "result.md").write_text("embedded private source\n")
    (repo / "shared/new.sv").write_text("module new; endmodule\n")
    scope = ("shared/new.sv",)
    params = AgentCallParams(prompt="review", model="test", cwd=repo)
    caplog.set_level("INFO")
    with isolated_agent_workspace(
        params, "read_only", category, scope=scope, excluded_roots=(Path("current-report"),)
    ) as (isolated, _):
        root = Path(isolated.cwd)
        assert (root / "shared/new.sv").is_file()
        assert (root / "shared/dut.sv").exists() == (category == "rtl")
        assert (root / "shared/main.cpp").exists() == (category == "tb")
        for source in roots:
            if source.is_relative_to(repo):
                relative = source.relative_to(repo)
                assert not (root / relative).exists()
                assert (relative / "result.md").as_posix() in caplog.text
    for source in roots:
        assert (source / "copied.sv").is_file()
    reset_cache()


def test_snapshot_refuses_tracked_file_beneath_external_symlink_parent(tmp_path):
    from tests.conftest import symlink_or_skip
    from tests.test_target_catalog import _category_sim_core

    repo = _repo(tmp_path)
    _category_sim_core(repo)
    external = tmp_path / "external"
    external.mkdir()
    (external / "evidence.md").write_text("external private source copy\n")
    parent = repo / "replaced"
    parent.mkdir()
    (parent / "evidence.md").write_text("original\n")
    _git(["add", "replaced/evidence.md"], repo)
    (parent / "evidence.md").unlink()
    parent.rmdir()
    symlink_or_skip(parent, external, target_is_directory=True)
    params = AgentCallParams(prompt="review", model="test", cwd=repo)
    with (
        pytest.raises(ValueError, match=r"source parent escapes.*replaced[\\/]evidence\.md"),
        isolated_agent_workspace(params, "read_only", "rtl"),
    ):
        pytest.fail("provider must not receive an externally populated snapshot")
    assert (external / "evidence.md").read_text() == "external private source copy\n"


def test_snapshot_rebases_relative_reentry_symlink_without_writing_live_files(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager

    from tests.conftest import symlink_or_skip
    from tests.test_target_catalog import _category_sim_core

    live = tmp_path / "live"
    live.mkdir()
    repo = _repo(live)
    _category_sim_core(repo)
    parent = repo / "alias"
    parent.mkdir()
    (parent / "dut.sv").write_text("original\n")
    _git(["add", "alias/dut.sv"], repo)
    (parent / "dut.sv").unlink()
    parent.rmdir()
    symlink_or_skip(parent, "../repo/shared", target_is_directory=True)
    # The snapshot root is <owned>/workspace, so "../repo/shared" escapes to
    # <owned>/repo/shared: plant the outsider exactly there.
    owned = tmp_path / "owned"
    outsider = owned / "repo/shared"
    outsider.mkdir(parents=True)
    (outsider / "dut.sv").write_text("must remain unchanged\n")

    @contextmanager
    def fixed_owned_dir(_prefix):
        yield owned

    monkeypatch.setattr("booley.specialists.specialist_workspace.owned_tempdir", fixed_owned_dir)
    params = AgentCallParams(prompt="review", model="test", cwd=repo)
    with isolated_agent_workspace(params, "read_only", "rtl"):
        assert (outsider / "dut.sv").read_text() == "must remain unchanged\n"
    assert (outsider / "dut.sv").read_text() == "must remain unchanged\n"


def test_snapshot_sanitizes_checkout_and_goal_copies_despite_external_selection(
    tmp_path, monkeypatch
):
    from booley.runtime.booley_dir import reset_cache
    from tests.test_target_catalog import _category_sim_core

    repo = _repo(tmp_path)
    _category_sim_core(repo)
    external = tmp_path / "external-state"
    external.mkdir()
    (external / "booley.toml").write_text('[project]\nname="external"\n')
    monkeypatch.setenv("BOOLEY_DIR", str(external))
    reset_cache()
    local = repo / ".booley"
    local.mkdir()
    (local / "booley.toml").write_text('[project]\nname="checkout"\n')
    goal = local / "goals/case-20261015T000000Z"
    roots = [local / "selftest", goal / "logs", goal / ".runtime/flow-reports"]
    for root in roots:
        root.mkdir(parents=True)
        (root / "source-copy.md").write_text("embedded opposite source\n")
    history = local / "goals/history"
    history.mkdir()
    (history / "summary.md").write_text("authored summary\n")
    _git(["add", "--force", ".booley"], repo)
    params = AgentCallParams(prompt="review", model="test", cwd=repo)
    with isolated_agent_workspace(params, "read_only", "rtl") as (isolated, _):
        for root in roots:
            assert not (Path(isolated.cwd) / root.relative_to(repo)).exists()
        assert (Path(isolated.cwd) / history.relative_to(repo) / "summary.md").is_file()
    assert all((root / "source-copy.md").is_file() for root in roots)
    reset_cache()


_CPP_REVIEW_CORE = """CAPI=2:
name: ::snapshot:0
filesets:
  rtl:
    files: [shared/dut.sv, {shared/rtl.c: {file_type: cSource}}]
    tags: [rtl]
  cpp:
    file_type: cppSource
    files:
      - shared/main.cpp
      - shared/api.h: {is_include_file: true}
      - shared/api.hpp: {is_include_file: true}
      - shared/override.hh: {file_type: cSource, is_include_file: true}
  c:
    file_type: cSource
    files: [shared/support.c]
  tagged:
    tags: [tb]
    files: [shared/tagged.hxx]
targets:
  sim:
    default_tool: verilator
    filesets: [rtl, cpp, c, tagged]
"""


def _cpp_source_status(repo):
    return subprocess.check_output(
        ["git", "status", "--porcelain", "--", "shared", "tracked.sv"], cwd=repo
    )


def _cpp_review_project(tmp_path, monkeypatch, layout="flat"):
    from booley.runtime.booley_dir import reset_cache

    repo = _repo(tmp_path)
    if layout != "flat":
        repo = repo / "hw"
        repo.mkdir()
    data = repo / ".booley"
    data.mkdir()
    config = '[project]\nname="snapshot"\n'
    if layout == "nested-stealth":
        config += "[stealth]\nenabled=true\n"
    (data / "booley.toml").write_text(config)
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    monkeypatch.chdir(repo)
    reset_cache()
    visible = [
        "shared/main.cpp",
        "shared/support.c",
        "shared/api.h",
        "shared/api.hpp",
        "shared/override.hh",
        "shared/tagged.hxx",
        "shared/helper.CPP",
    ]
    hidden = [
        "shared/dut.sv",
        "shared/rtl.c",
        "shared/unknown.C",
        "shared/unknown.HXX",
        "shared/backup.HPP.bak",
        "tracked.sv",
    ]
    for name in visible + hidden:
        path = repo / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("// authored source\n")
    core_base = data / "cores" if layout == "nested-stealth" else repo
    core_base.mkdir(exist_ok=True)
    core_path = core_base / "design.core"
    core_path.write_text(_CPP_REVIEW_CORE)
    _git(["add", "--force", str(core_path.relative_to(repo))], repo)
    _git(["add", "shared/main.cpp", "shared/dut.sv", "shared/rtl.c"], repo)
    before = {name: (repo / name).read_bytes() for name in visible + hidden}
    status = _cpp_source_status(repo)
    return repo, visible, hidden, before, status


def _assert_cpp_review_snapshot(params, repo, visible, hidden, category):
    root = Path(params.cwd)
    assert root != repo
    for name in visible:
        assert (root / name).is_file(), name
        _git(["show", f"HEAD:./{name}"], root)
    for name in hidden:
        assert not (root / name).exists(), name
        assert (
            subprocess.run(
                ["git", "cat-file", "-e", f"HEAD:./{name}"],
                cwd=root,
                capture_output=True,
                check=False,
            ).returncode
            != 0
        )
    if category == "tb":
        assert "C/C++ testbench" in params.system_prompt
        assert "Canonical style guide for SystemVerilog" not in params.system_prompt
        assert "Cocotb" not in params.system_prompt
        assert "shared/helper.CPP" in params.prompt


def _cpp_review_result(root, category, finding_path):
    import json

    issues = [
        {
            "severity": "MINOR",
            "confidence": "HIGH",
            "category": "quality" if category == "tb" else "bugs",
            "file": str(root / path),
            "line": 1,
            "summary": summary,
            "kind": "code_defect",
            "disposition": "current",
            "ticket_clause": "testbench quality",
        }
        for path, summary in [
            (finding_path, "header finding"),
            ("shared/unknown.C", "outside finding"),
        ]
    ]
    return AgentResult(output=json.dumps({"issues": issues}))


@pytest.mark.parametrize("category", ["tb", "rtl"])
@pytest.mark.parametrize("layout", ["flat", "nested", "nested-stealth"])
def test_interactive_cpp_review_uses_real_snapshot(tmp_path, monkeypatch, category, layout):
    """Admission, prompt, isolation and finding filtering use the same exact scope."""
    repo, visible, hidden, before, status = _cpp_review_project(tmp_path, monkeypatch, layout)
    if category == "rtl":
        hidden = [
            *visible,
            "shared/unknown.C",
            "shared/unknown.HXX",
            "shared/backup.HPP.bak",
            "tracked.sv",
        ]
        visible = ["shared/dut.sv", "shared/rtl.c"]
    scope = visible if category == "tb" else ["shared/dut.sv"]
    finding_path = "shared/api.hpp" if category == "tb" else "shared/dut.sv"
    calls = []

    def fake_call(params, *, on_event):
        calls.append(params)
        _assert_cpp_review_snapshot(params, repo, visible, hidden, category)
        return _cpp_review_result(Path(params.cwd), category, finding_path)

    endpoint = ReviewerSpecialist()
    endpoint.parse_args(
        [
            "--work-dir",
            str(repo),
            "--category",
            category,
            "--focus",
            "quality" if category == "tb" else "bugs",
            "--scope",
            ",".join(scope),
            "--model",
            "light",
        ]
    )
    endpoint.read_state()
    with (
        patch("booley.specialists.specialist._call_agent_sync", side_effect=fake_call),
        patch("booley.specialists.specialist.parent_death_watchdog", return_value=nullcontext()),
    ):
        result = endpoint._run_review()
    assert calls, result.report_text
    assert result.exit_code != 2, result.report_text
    assert "header finding" in result.report_text
    assert "outside finding" not in result.report_text
    assert str(repo / finding_path) in result.report_text
    assert before == {name: (repo / name).read_bytes() for name in before}
    assert status == _cpp_source_status(repo)


@pytest.mark.parametrize("category", ["rtl", "tb"])
@pytest.mark.parametrize("suffix", ["sv", "cpp"])
@pytest.mark.parametrize("conditional", [False, True])
def test_spaced_source_cannot_gain_opposite_scope(tmp_path, category, suffix, conditional):
    from booley.specialists.reviewer import validate_scope_category
    from booley.targets.catalog import TargetCatalog

    repo = _repo(tmp_path)
    name = f"shared/source with space.{suffix}"
    source = repo / name
    source.parent.mkdir()
    source.write_text("authored source\n")
    opposite = "tb" if category == "rtl" else "rtl"
    expression = f"tool_verilator ? ({name})" if conditional else name
    kind = "cppSource" if suffix == "cpp" else "systemVerilogSource"
    (repo / "design.core").write_text(
        f'CAPI=2:\nname: ::spaced:0\nfilesets:\n  scoped:\n    files: ["{expression}"]\n'
        f"    tags: [{opposite}]\n    file_type: {kind}\n"
        "targets:\n  sim: {default_tool: verilator, filesets: [scoped], toplevel: dut}\n"
    )
    params = AgentCallParams(prompt="review", model="test", cwd=repo)
    with (
        pytest.raises(ValueError, match="opposite-category"),
        isolated_agent_workspace(params, "read_only", category, scope=(name,)),
    ):
        pytest.fail("declared opposite source reached provider snapshot")
    catalog = TargetCatalog.build(repo)
    inspection = catalog.inspect(catalog.select("sim"))
    partition = inspection.tb_files if opposite == "tb" else inspection.rtl_files
    assert name in partition
    assert validate_scope_category([name], category, repo)
    assert source.read_text() == "authored source\n"


@pytest.mark.skipif(sys.platform == "win32", reason="needs SIGKILL and the Linux Sandbox")
def test_specialist_launch_reclaims_killed_run_scratch(tmp_path, monkeypatch):
    """AC4/AC5: a launch removes the scratch of a SIGKILL-ed run and nothing else."""
    from tests.owned_scratch_support import (
        exited_process_payload,
        kill_child_with_scratch,
        write_marker,
    )

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    repo = _repo(tmp_path)
    killed = kill_child_with_scratch(repo, scratch, tmp_path / "home")
    live = scratch / "booley-specialist-ro-live"
    dead = scratch / "booley-specialist-ro-dead"
    unmarked = scratch / "booley-specialist-ro-unmarked"
    for directory in (live, dead, unmarked):
        directory.mkdir()
    from booley.runtime.pid import capture_process_identity

    own_identity = capture_process_identity(os.getpid())
    assert own_identity is not None
    write_marker(live, own_identity.to_payload())
    write_marker(dead, exited_process_payload())
    endpoint = ReviewerSpecialist()
    endpoint._args = argparse.Namespace(category=None, scope="", report_dir=None)
    params = AgentCallParams(prompt="review", model="test", cwd=repo)
    try:
        with (
            patch(
                "booley.specialists.specialist._call_agent_sync",
                return_value=AgentResult(output="ok"),
            ),
            patch(
                "booley.specialists.specialist.parent_death_watchdog", return_value=nullcontext()
            ),
        ):
            endpoint._invoke_agent(params, on_event=lambda _event: None)
        assert [path for path in (*killed, dead) if path.exists()] == []
        assert live.is_dir()
        assert unmarked.is_dir()
    finally:
        for path in killed:  # unfixed code leaves the nested home in the real /tmp
            shutil.rmtree(path, ignore_errors=True)
