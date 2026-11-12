"""Booley Directory path classification: Common vs Checkout Booley State (ADR 0082)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

# One sample path per ADR 0082 appendix row, with the part the ADR assigns it.
ADR_0082_SAMPLES = {
    "booley.toml": "checkout",
    "tests.toml": "checkout",
    "metrics.toml": "checkout",
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
    "measurements/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/r.json": "common",
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


# The maintainer's grouping of Checkout rows: copied into a Snapshot (versioned
# with the RTL) or tied to one checkout's sources. A new Checkout row must be
# placed in one of them here, which is how the test forces that decision.
VERSIONED_PATTERNS = {
    "booley.toml",
    "tests.toml",
    "metrics.toml",
    "criteria.toml",
    "doctor-waivers.toml",
    "configs.toml",
    "cores/",
    "hooks/",
    "generators/",
    "mcp_tools/",
    "selftest/",
    "AGENTS.md",
    "FUSESOC_IGNORE",
    "goals/history/",
    "tickets/history/",
    "pipeline.toml",
    "tools/",
    "rtl_style_guide.md",
    "tb_style_guide.md",
    ".gitignore",
    ".gitattributes",
    ".managed/",
}
SOURCE_TIED_PATTERNS = {
    "tmp/doctor/",
    "runtime/doctor/core-resolve/",
    ".runtime/edalize/",
    ".runtime/python-artifacts/",
    "flow-reports/",
    "mcp-tool-reports/",
    "tmp/setup/",
    ".git/",
    ".runtime/syn/",
    ".runtime/simulation-run-locks/",
    ".runtime/flow-console/",
    ".runtime/mutation_tester/",
    ".runtime/flow-reports/",
    ".runtime/mcp-tool-reports/",
    ".runtime/display.jsonl",
    ".runtime/prompts/",
    "tmp/fusesoc-isolated-cores/",
}


def test_every_checkout_row_says_whether_it_is_versioned():
    from booley.core.booley_dir_parts import BOOLEY_DIR_PATHS, BooleyDirClass, BooleyDirPart

    assert all(isinstance(value, BooleyDirClass) for value in BOOLEY_DIR_PATHS.values())
    by_class = {
        booley_class: {p for p, value in BOOLEY_DIR_PATHS.items() if value is booley_class}
        for booley_class in BooleyDirClass
    }
    assert by_class[BooleyDirClass.VERSIONED] == VERSIONED_PATTERNS
    assert by_class[BooleyDirClass.SOURCE_TIED] == SOURCE_TIED_PATTERNS
    assert BooleyDirClass.COMMON.part is BooleyDirPart.COMMON
    assert BooleyDirClass.VERSIONED.part is BooleyDirPart.CHECKOUT
    assert BooleyDirClass.SOURCE_TIED.part is BooleyDirPart.CHECKOUT


def test_walk_yields_topmost_uniform_entries(tmp_path):
    from booley.core.booley_dir_parts import BooleyDirClass, iter_booley_dir

    def touch(relative: str) -> None:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")

    for relative in (
        "cores/ip/deep/file.core",  # One entry: the whole subtree is versioned.
        "goals/G1/record.json",  # goals/ is Common but goals/history/ is not.
        "goals/history/H1.md",
        "runtime/jobs/j1/state.json",  # runtime/ is a container.
        "runtime/unknown.json",
        "runtime/doctor/core-resolve/x",
        "runtime/doctor/stale.lock",
        "eval_tmp/x",  # Unclassified, no rows below: one entry, not walked.
    ):
        touch(relative)
    (tmp_path / "linked").symlink_to(tmp_path / "cores", target_is_directory=True)
    (tmp_path / "tickets").mkdir()
    (tmp_path / "tickets" / "history").mkdir()

    walked = dict(iter_booley_dir(tmp_path))

    assert walked == {
        "cores": BooleyDirClass.VERSIONED,
        "eval_tmp": None,
        "goals/G1": BooleyDirClass.COMMON,
        "goals/history": BooleyDirClass.VERSIONED,
        "linked": None,  # A symlink is a leaf, never followed.
        "runtime/doctor/core-resolve": BooleyDirClass.SOURCE_TIED,
        "runtime/doctor/stale.lock": BooleyDirClass.COMMON,
        "runtime/jobs": BooleyDirClass.COMMON,
        "runtime/unknown.json": None,
        "tickets/history": BooleyDirClass.VERSIONED,
    }
    assert list(walked) == sorted(walked)


def test_snapshot_main_reports_an_unreadable_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime import worktree_snapshot

    status = worktree_snapshot.main([str(tmp_path / "missing"), str(tmp_path / "list")])

    assert status == 1
    assert "cannot list the Booley Directory for the Snapshot" in capsys.readouterr().err
