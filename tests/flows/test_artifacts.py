"""Tests for the shared ``artifacts`` report block (``flows/artifacts.py``).

The block is the agent's route from a verdict to the files behind it, so the
properties worth pinning are the ones that make a pointer trustworthy: it is
project-relative, and it is absent rather than wrong.
"""

from __future__ import annotations

from pathlib import Path

from booley.flows import artifacts


class TestRelative:
    def test_absolute_path_becomes_project_relative(self, tmp_path: Path):
        log = tmp_path / "build" / "run.log"
        log.parent.mkdir(parents=True)
        log.write_text("output\n", encoding="utf-8")

        assert artifacts.relative(log, tmp_path) == "build/run.log"

    def test_relative_input_is_anchored_on_work_dir_not_cwd(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        """A caller may pass a path already in the form this function returns.

        Anchoring the existence check on the process cwd would drop every such
        pointer whenever the Flow does not happen to run from the work dir —
        which is the normal case under MCP dispatch.
        """
        log = tmp_path / "build" / "run.log"
        log.parent.mkdir(parents=True)
        log.write_text("output\n", encoding="utf-8")
        elsewhere = tmp_path / "somewhere-else"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        assert artifacts.relative("build/run.log", tmp_path) == "build/run.log"

    def test_missing_file_yields_none(self, tmp_path: Path):
        assert artifacts.relative(tmp_path / "nope.log", tmp_path) is None

    def test_empty_and_none_inputs_yield_none(self, tmp_path: Path):
        assert artifacts.relative(None, tmp_path) is None
        assert artifacts.relative("", tmp_path) is None

    def test_path_outside_work_dir_still_resolves(self, tmp_path: Path):
        """A ``..`` form is legitimate — a baseline worktree lives beside the
        work dir — so it must not be dropped."""
        outside = tmp_path / "outside.log"
        outside.write_text("x", encoding="utf-8")
        inner = tmp_path / "project"
        inner.mkdir()

        assert artifacts.relative(outside, inner) == "../outside.log"


class TestArtifactsBlock:
    def test_drops_absent_keys_keeps_present_ones(self, tmp_path: Path):
        present = tmp_path / "run.log"
        present.write_text("x", encoding="utf-8")

        block = artifacts.artifacts_block(
            tmp_path,
            log=present,
            trace=tmp_path / "trace.fst",
            result=None,
        )

        assert block == {"log": "run.log"}

    def test_empty_when_nothing_landed(self, tmp_path: Path):
        assert artifacts.artifacts_block(tmp_path, log=tmp_path / "gone.log") == {}

    def test_dirs_are_nested_under_their_own_key(self, tmp_path: Path):
        log = tmp_path / "run.log"
        log.write_text("x", encoding="utf-8")
        build = tmp_path / "build"
        build.mkdir()

        block = artifacts.artifacts_block(tmp_path, log=log, dirs={"build": build})

        assert block == {"log": "run.log", "dirs": {"build": "build"}}

    def test_absent_dirs_are_dropped_like_files(self, tmp_path: Path):
        """The drop-what-is-absent rule applies to directories too — an agent
        must never be sent to list a path that is not there."""
        build = tmp_path / "build"
        build.mkdir()

        block = artifacts.artifacts_block(
            tmp_path,
            dirs={"build": build, "timing": tmp_path / "build" / "reports" / "timing"},
        )

        assert block == {"dirs": {"build": "build"}}

    def test_dirs_key_omitted_when_none_resolved(self, tmp_path: Path):
        block = artifacts.artifacts_block(tmp_path, dirs={"build": tmp_path / "gone"})
        assert block == {}

    def test_a_file_passed_as_a_dir_still_resolves(self, tmp_path: Path):
        """``relative`` checks existence, not kind — callers decide semantics,
        so this is documented behaviour rather than a silent trap."""
        f = tmp_path / "run.log"
        f.write_text("x", encoding="utf-8")

        assert artifacts.artifacts_block(tmp_path, dirs={"build": f}) == {
            "dirs": {"build": "run.log"}
        }


class TestMergeArtifacts:
    def test_merges_into_existing_key(self):
        detail: dict = {"artifacts": {"log": "a/run.log"}}

        artifacts.merge_artifacts(detail, {"report": "b/report.json"})

        assert detail["artifacts"] == {"log": "a/run.log", "report": "b/report.json"}

    def test_creates_the_key_when_absent(self):
        detail: dict = {}

        artifacts.merge_artifacts(detail, {"log": "a/run.log"})

        assert detail["artifacts"] == {"log": "a/run.log"}

    def test_empty_block_adds_nothing(self):
        """A run that produced no durable file carries no empty ``artifacts``
        key — an empty mapping reads as "checked, found nothing", which is a
        different claim from "not applicable"."""
        detail: dict = {}

        artifacts.merge_artifacts(detail, {})

        assert detail == {}

    def test_dirs_merge_key_by_key_rather_than_replacing(self):
        """A caller adding directories in two passes must not lose the first."""
        detail: dict = {"artifacts": {"dirs": {"build": "a"}}}

        artifacts.merge_artifacts(detail, {"dirs": {"timing": "b"}})

        assert detail["artifacts"]["dirs"] == {"build": "a", "timing": "b"}
