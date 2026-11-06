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
    from booley.core.booley_dir_parts import BooleyDirPart, booley_dir_part

    for path, expected in ADR_0082_SAMPLES.items():
        assert booley_dir_part(path) is BooleyDirPart(expected), path
    # The deepest matching pattern wins, and a Windows spelling classifies like POSIX.
    assert booley_dir_part(PurePosixPath("goals/history/x")) is BooleyDirPart.CHECKOUT
    assert booley_dir_part(PureWindowsPath("goals", "history", "x")) is BooleyDirPart.CHECKOUT
    assert booley_dir_part(PureWindowsPath("goals", "G1", "x")) is BooleyDirPart.COMMON


# Paths other Booley modules write, beyond the ADR appendix rows.
WRITTEN_PATH_SAMPLES = {
    "pipeline.toml": "checkout",
    "tools/t.toml": "checkout",
    "rtl_style_guide.md": "checkout",
    "tb_style_guide.md": "checkout",
    ".git/HEAD": "checkout",
    ".gitignore": "checkout",
    ".gitattributes": "checkout",
    ".managed/x": "checkout",
    ".runtime/syn/x": "checkout",
    ".runtime/simulation-run-locks/x": "checkout",
    ".runtime/flow-console/x": "checkout",
    ".runtime/mutation_tester/x": "checkout",
    ".runtime/flow-reports/x": "checkout",
    ".runtime/mcp-tool-reports/x": "checkout",
    ".runtime/display.jsonl": "checkout",
    ".runtime/prompts/x": "checkout",
    "tmp/fusesoc-isolated-cores/x": "checkout",
    "runtime/dashboard-task.json": "common",
    "runtime/upgrade_review.lock": "common",
    "runtime/doctor/x": "common",
    "runtime/developer_probe.json": "common",
    "runtime/synth_probe.json": "common",
    "tmp/doctor/x": "checkout",
    "runtime/doctor/core-resolve/x": "checkout",
    "runtime/resources/x": "common",
    ".runtime/executions/x": "common",
    ".runtime/jobs/x": "common",
    ".runtime/findings.lock": "common",
    ".runtime/sessions/x": "common",
    ".runtime/transcripts/x": "common",
    "findings.jsonl.tmp": "common",
    "SETUP-REPORT.md": "common",
    "FEEDBACK-REPORT.md": "common",
    "SETUP-PLAN.md": "common",
    "PARITY-REPORT.md": "common",
    "parity/x": "common",
    "plans/x": "common",
    "logs/x": "common",
    "human-logs/x.md": "common",
}

# Atomic-write temporaries take the part of the file they replace.
TEMP_FILE_SAMPLES = {
    "runtime/.doctor_stamp.json.1234.tmp": "common",
    "runtime/.upgrade_review.json.k3j2h1.tmp": "common",
    ".booley.toml.ab12cd.tmp": "checkout",
    ".gitignore.ab12cd.tmp": "checkout",
    ".directory-relocation.json.ab12cd.tmp": "common",
}


def test_written_paths_and_temp_files_are_classified():
    from booley.core.booley_dir_parts import BooleyDirPart, booley_dir_part

    for path, expected in {**WRITTEN_PATH_SAMPLES, **TEMP_FILE_SAMPLES}.items():
        assert booley_dir_part(path) is BooleyDirPart(expected), path


def test_unclassified_path_is_rejected():
    from booley.core.booley_dir_parts import UnclassifiedBooleyPathError, booley_dir_part

    for path in (
        "not-booley/x",
        "runtime",
        ".runtime",
        "tmp",
        "../booley.toml",
        "/booley.toml",
        ".unknown.1234.tmp",
    ):
        with pytest.raises(UnclassifiedBooleyPathError):
            booley_dir_part(path)
