"""Tests for provider-independent read-only Specialist workspaces."""

from __future__ import annotations

import argparse
import subprocess
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.core.models import AgentCallParams, AgentResult
from booley.mcp_tools.reviewer import ReviewerSpecialist
from booley.mcp_tools.specialist_workspace import (
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
        patch("booley.mcp_tools.specialist._call_agent_sync", side_effect=fake_call),
        patch("booley.mcp_tools.specialist.parent_death_watchdog", return_value=nullcontext()),
    ):
        result = endpoint._invoke_agent(params, on_event=lambda _event: None)

    assert (repo / "tracked.sv").read_text(encoding="utf-8") == "old\n"
    assert (repo / "rtl" / "dut.sv").is_file()
    assert (repo / "tb" / "tb.sv").is_file()
    assert str(repo / "tracked.sv") in result.output
