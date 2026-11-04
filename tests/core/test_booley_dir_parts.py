"""Booley Directory path classification: Common vs Checkout Booley State (ADR 0082)."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

import pytest

# One sample path per ADR 0082 appendix row, with the part the ADR assigns it.
ADR_0082_SAMPLES = {
    "booley.toml": "checkout",
    "tests.toml": "checkout",
    "criteria.toml": "checkout",
    "doctor-waivers.toml": "checkout",
    "configs.toml": "checkout",
    "cores/ip/ip.core": "checkout",
    "hooks/pre-commit": "checkout",
    "generators/gen.py": "checkout",
    "mcp_tools/tool.py": "checkout",
    "selftest/case.toml": "checkout",
    "AGENTS.md": "checkout",
    "FUSESOC_IGNORE": "checkout",
    "goals/history/G1.md": "checkout",
    "tickets/history/T1.md": "checkout",
    ".runtime/edalize/sim/.locks/a.lock": "checkout",
    ".runtime/edalize/sim/payload/x": "checkout",
    ".runtime/python-artifacts/x.whl": "checkout",
    "flow-reports/sim/1/report.json": "checkout",
    "mcp-tool-reports/t/1.json": "checkout",
    "tmp/setup/x": "checkout",
    "goals/G1/record.json": "common",
    "runtime/jobs/j1/state.json": "common",
    "runtime/job-handles/h": "common",
    "runtime/sessions/s1.json": "common",
    "runtime/session_activity/a": "common",
    "runtime/dashboard-task.lock": "common",
    ".interactive_logs/a.log": "common",
    "runtime/doctor_stamp.json": "common",
    "runtime/doctor_deep_stamp.json": "common",
    "runtime/upgrade_review.json": "common",
    ".runtime/compiler-cache/x": "common",
    "result-cache/ab/entry": "common",
    "worktrees/s1/file": "common",
    "worktrees/.locks/l": "common",
    "worktrees/FUSESOC_IGNORE": "common",
    ".baseline-wt-1-ab/x": "common",
    "directory-layout.lock": "common",
    ".directory-relocation.json": "common",
    "findings.jsonl": "common",
    "BOOLEY-FEEDBACK.md": "common",
    "setup-evidence/a/b.txt": "common",
    "reviewer-evidence/a.json": "common",
    "reviewer-state/s": "common",
    "handoffs/h.md": "common",
    "tickets/T1/state.json": "common",
    "goalsets/set.md": "common",
    "docker/Dockerfile": "common",
}


def test_adr_0082_paths_are_classified():
    from booley.core.booley_dir_parts import BOOLEY_DIR_PATHS, BooleyDirPart, booley_dir_part

    for path, expected in ADR_0082_SAMPLES.items():
        assert booley_dir_part(path) is BooleyDirPart(expected), path
    # Every pattern in the table carries a real part.
    assert set(BOOLEY_DIR_PATHS.values()) == set(BooleyDirPart)
    # The deepest matching pattern wins, and a Windows spelling classifies like POSIX.
    assert booley_dir_part(PurePosixPath("goals/history/x")) is BooleyDirPart.CHECKOUT
    assert booley_dir_part(PureWindowsPath("goals", "history", "x")) is BooleyDirPart.CHECKOUT
    assert booley_dir_part(PureWindowsPath("goals", "G1", "x")) is BooleyDirPart.COMMON


def test_unclassified_path_is_rejected():
    from booley.core.booley_dir_parts import UnclassifiedBooleyPathError, booley_dir_part

    for path in ("not-booley/x", "runtime", ".runtime", "tmp", "../booley.toml", "/booley.toml"):
        with pytest.raises(UnclassifiedBooleyPathError):
            booley_dir_part(path)
