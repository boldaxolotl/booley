"""Reusable assertion helpers for E2E tests."""

from __future__ import annotations

import json
from pathlib import Path

from .mock_agent_router import MockAgentRouter
from .mock_tool_runner import MockToolRunner


def assert_final_status(tickets_dir: Path, slug: str, expected_dir: str) -> None:
    """Assert ticket ended up in the expected status directory.

    ticket_board stores tickets under board/<status>/.
    """
    ticket_file = tickets_dir / "board" / expected_dir / f"{slug}.md"
    assert ticket_file.exists(), (
        f"Expected ticket {slug} in board/{expected_dir}/, but not found. Checked: {ticket_file}"
    )


def assert_steps_completed(
    logs_dir: Path,
    slug: str,
    expected: list[str],
) -> None:
    """Assert that progress.json lists the expected completed steps."""
    progress_path = logs_dir / slug / "progress.json"
    assert progress_path.exists(), f"progress.json not found: {progress_path}"
    data = json.loads(progress_path.read_text(encoding="utf-8"))
    completed = data.get("steps_completed", [])
    assert completed == expected, (
        f"Step mismatch.\n  Expected: {expected}\n  Got:      {completed}"
    )


def assert_steps_completed_superset(
    logs_dir: Path,
    slug: str,
    must_include: list[str],
) -> None:
    """Assert that progress.json includes at least the given steps."""
    progress_path = logs_dir / slug / "progress.json"
    assert progress_path.exists(), f"progress.json not found: {progress_path}"
    data = json.loads(progress_path.read_text(encoding="utf-8"))
    completed = set(data.get("steps_completed", []))
    missing = set(must_include) - completed
    assert not missing, f"Missing expected steps: {missing}\n  Completed: {sorted(completed)}"


def assert_artifacts_exist(
    logs_dir: Path,
    slug: str,
    step_artifacts: dict[str, list[str]],
) -> None:
    """Assert per-step artifact files exist in logs/<slug>/stages/<step>/."""
    for step, files in step_artifacts.items():
        for fname in files:
            # Step dirs use NN-step-name format
            step_dir = _find_step_dir(logs_dir / slug / "stages", step)
            if step_dir is None:
                raise AssertionError(
                    f"Step directory for {step!r} not found under {logs_dir / slug / 'stages'}"
                )
            path = step_dir / fname
            assert path.exists(), f"Artifact missing: {path}"


def assert_metadata_keys(
    logs_dir: Path,
    slug: str,
    step: str,
    keys: list[str],
) -> None:
    """Assert that per-step meta.json contains the given keys."""
    step_dir = _find_step_dir(logs_dir / slug / "stages", step)
    assert step_dir is not None, f"Step dir for {step!r} not found"
    meta_path = step_dir / "meta.json"
    assert meta_path.exists(), f"meta.json not found: {meta_path}"
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    missing = [k for k in keys if k not in data]
    assert not missing, f"Missing metadata keys for {step}: {missing}\n  Have: {list(data.keys())}"


def assert_call_counts(
    agent_router: MockAgentRouter,
    tool_runner: MockToolRunner,
    *,
    agent: int | None = None,
    sim: int | None = None,
    command: int | None = None,
) -> None:
    """Assert mock call counts."""
    if agent is not None:
        assert agent_router.agent_call_count == agent, (
            f"Expected {agent} agent calls, got {agent_router.agent_call_count}"
        )
    if sim is not None:
        assert tool_runner.sim_call_count == sim, (
            f"Expected {sim} sim calls, got {tool_runner.sim_call_count}"
        )
    if command is not None:
        assert tool_runner.command_call_count == command, (
            f"Expected {command} command calls, got {tool_runner.command_call_count}"
        )


def _find_step_dir(stages_root: Path, step_name: str) -> Path | None:
    """Find the NN-step-name directory under stages/."""
    if not stages_root.exists():
        return None
    for d in stages_root.iterdir():
        if d.is_dir() and d.name.endswith(step_name):
            return d
    return None
