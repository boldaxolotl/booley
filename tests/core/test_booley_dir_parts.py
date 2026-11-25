"""Booley Directory path classification: Common vs Checkout Booley State (ADR 0082)."""

from __future__ import annotations

import os
import shutil
import stat
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
    "t3-worktrees/work/t3-1a2b3c4d-0123456789abcdef/rtl/top.sv": "common",
    "runtime/jobs/j1/state.json": "common",
    "runtime/job-handles/h": "common",
    "runtime/sessions/s1.json": "common",
    "runtime/session_activity/a": "common",
    "runtime/dashboard-task.lock": "common",
    "job_logs/a.log": "common",
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
    "goalsets/set.md": "common",  # leftover directory of a removed feature
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
    ".runtime/mutation/x": "checkout",
    ".runtime/mutation_tester/x": "checkout",
    ".runtime/flow-reports/x": "checkout",
    ".runtime/mcp-tool-reports/x": "checkout",
    ".runtime/display.jsonl": "checkout",
    ".runtime/prompts/x": "checkout",
    ".runtime/bwave/bwave_sessions.json": "checkout",
    ".runtime/bwave/bwave_sessions.json.lock": "checkout",
    "tmp/fusesoc-isolated-cores/x": "checkout",
    "runtime/dashboard-task.json": "common",
    "runtime/upgrade_review.lock": "common",
    "runtime/doctor/x": "common",
    "runtime/developer_probe.json": "common",
    "runtime/synth_probe.json": "common",
    "runtime/chat-main.lock": "common",
    ".runtime/sandbox-mounts.json": "common",
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
    ".runtime/mutation/",
    ".runtime/mutation_tester/",
    ".runtime/flow-reports/",
    ".runtime/mcp-tool-reports/",
    ".runtime/display.jsonl",
    ".runtime/prompts/",
    ".runtime/bwave/",
    "tmp/fusesoc-isolated-cores/",
    "tmp/core-projection-publication/",
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


def test_copy_snapshot_reports_an_unreadable_source(tmp_path: Path) -> None:
    from booley.runtime.worktree_snapshot import SnapshotError, copy_snapshot

    with pytest.raises(SnapshotError, match="cannot list the Booley Directory for the Snapshot"):
        copy_snapshot(tmp_path / "missing", tmp_path / "copy")


def test_copy_snapshot_names_every_unclassified_path_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime.worktree_snapshot import copy_snapshot

    source = tmp_path / "data"
    (source / "eval_tmp").mkdir(parents=True)
    (source / "eval_tmp" / "x").write_text("x\n", encoding="utf-8")
    no_tools = tmp_path / "no-tools"
    no_tools.mkdir()
    monkeypatch.setenv("PATH", str(no_tools))

    assert copy_snapshot(source, tmp_path / "copy") == ["eval_tmp"]


def test_a_configured_directory_never_reclassifies_booley_state(tmp_path: Path) -> None:
    from booley.core.booley_dir_parts import (
        BOOLEY_DIR_PATHS,
        BooleyDirClass,
        iter_booley_dir,
        with_versioned_directory,
    )

    for relative in ("waivers/approved/a.toml", "waivers/other/x"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")

    rows = with_versioned_directory("waivers/approved")

    assert dict(iter_booley_dir(tmp_path, rows=rows)) == {
        "waivers/approved": BooleyDirClass.VERSIONED,
        "waivers/other": None,
    }
    for name in ("worktrees", "tmp", "cores/waivers"):
        assert with_versioned_directory(name) is BOOLEY_DIR_PATHS


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_copy_snapshot_names_unclassified_paths_and_skips_tool_state(tmp_path: Path) -> None:
    from booley.runtime.worktree_snapshot import copy_snapshot

    source = tmp_path / "data"
    for relative in (
        "eval_tmp/x",
        "cores/ip/x.core",
        "cores/ip/.git/HEAD",
        "cores/ip/rtl.v",
        "cores/ip/.venv/bin/python",
        "cores/ip/__pycache__/m.pyc",
    ):
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_text("x\n", encoding="utf-8")
    destination = tmp_path / "copy"

    assert copy_snapshot(source, destination) == ["eval_tmp"]

    copied = sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*"))
    assert [path for path in copied if path.startswith("cores/ip/")] == [
        "cores/ip/rtl.v",
        "cores/ip/x.core",
    ]


@pytest.mark.skipif(os.name == "nt", reason="needs symlinks and FIFOs")
def test_copy_snapshot_replaces_existing_entries_without_following_links(tmp_path: Path) -> None:
    from booley.runtime.worktree_snapshot import SnapshotError, copy_snapshot

    source, destination = tmp_path / "data", tmp_path / "copy"
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    for folder in (source / "cores", source / "tools", destination / "cores"):
        folder.mkdir(parents=True)
    (source / "cores" / "run").write_text("new\n", encoding="utf-8")
    (source / "cores" / "link").symlink_to("../rtl")
    os.mkfifo(source / "tools" / "channel")
    (destination / "cores" / "run").symlink_to(outside)
    (destination / "cores" / "link").symlink_to("elsewhere")

    copy_snapshot(source, destination)

    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert (destination / "cores" / "run").read_text(encoding="utf-8") == "new\n"
    assert not (destination / "cores" / "run").is_symlink()
    assert (destination / "cores" / "link").readlink() == Path("../rtl")
    assert stat.S_ISFIFO((destination / "tools" / "channel").lstat().st_mode)
    (destination / "cores" / "run").unlink()
    (destination / "cores" / "run").mkdir()
    with pytest.raises(SnapshotError, match="copying the Snapshot failed"):
        copy_snapshot(source, destination)
