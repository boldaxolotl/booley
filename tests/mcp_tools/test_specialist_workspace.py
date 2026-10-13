"""Tests for provider-independent read-only Specialist workspaces."""

from __future__ import annotations

import argparse
import subprocess
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
    _git(["add", "rtl/source.sv", "tb/source.sv"], repo)
    params = AgentCallParams(prompt="review", model="test", cwd=repo)

    with isolated_agent_workspace(params, "read_only", category) as (isolated, _snapshot):
        isolated_root = Path(isolated.cwd)
        assert not (isolated_root / hidden_dir).exists()
        assert (isolated_root / visible_dir / "source.sv").is_file()
        assert (repo / "rtl" / "source.sv").is_file()
        assert (repo / "tb" / "source.sv").is_file()

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


def test_reviewer_invokes_agent_in_snapshot(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "rtl").mkdir()
    (repo / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    (repo / "tb").mkdir()
    (repo / "tb" / "tb.sv").write_text("module tb; endmodule\n", encoding="utf-8")
    _git(["add", "rtl/dut.sv", "tb/tb.sv"], repo)
    params = AgentCallParams(prompt="review", model="test", cwd=repo)

    def fake_call(call_params: AgentCallParams, *, on_event) -> AgentResult:
        assert on_event is not None
        isolated_file = Path(call_params.cwd) / "tracked.sv"
        assert Path(call_params.cwd) != repo
        assert (Path(call_params.cwd) / "rtl" / "dut.sv").is_file()
        assert not (Path(call_params.cwd) / "tb").exists()
        assert (repo / "rtl" / "dut.sv").is_file()
        assert (repo / "tb" / "tb.sv").is_file()
        isolated_file.write_text("agent edit\n", encoding="utf-8")
        return AgentResult(output=f"finding in {isolated_file}")

    endpoint = ReviewerSpecialist()
    endpoint._args = argparse.Namespace(category="rtl")
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
    data = repo / ".booley_project"
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
            ".booley_project/.gitignore",
            ".booley_project/booley.toml",
            ".booley_project/hooks/check.py",
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    params = AgentCallParams(prompt="review", model="test", cwd=repo)
    with isolated_agent_workspace(params, "read_only") as (isolated, _snapshot):
        copied = Path(isolated.cwd) / ".booley_project"
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
        assert not (root / tb_dir).exists()
        assert (root / "rtl/new.sv").read_text() == "// untracked RTL\n"
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
        assert not (root / "tb").exists()
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
