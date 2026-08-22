"""Tests for ticket Scope authorization and deviation reporting."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from booley.harness.scope_policy import (
    DEVIATION_REPORT_NAME,
    ScopeTier,
    classify_path,
    committed_deviations,
    is_forbidden_path,
    is_restore_artifact,
    write_deviation_report,
)

SCOPE = ["rtl/dut.sv"]


class TestTiers:
    def test_scope_match_is_owned(self):
        assert classify_path(SCOPE, "rtl/dut.sv") is ScopeTier.OWNED

    @pytest.mark.parametrize(
        "path",
        ["rtl/pkg.sv", "tb/tb_dut.sv", "docs/spec.md"],
    )
    def test_everything_else_is_advisory_not_forbidden(self, path):
        """Anything an agent might need to finish hardware work stays advisory."""
        assert classify_path(SCOPE, path) is ScopeTier.ADVISORY

    @pytest.mark.parametrize(
        "path",
        [
            ".scope.json",
            ".booley_project/booley.toml",
            ".booley_project/tickets/board/queue/t.md",
            ".git/config",
            # Legacy layout, still resolved by core.config_paths.
            ".booley/project/booley.toml",
            # Harness-vendored scripts synthesis actually executes.
            ".booley/src/yosys/run_yosys_syn.py",
            # The sealed execution contract is immutable during development.
            "dut.core",
            "constraints/dut.sdc",
            ".booley_project/cores/dut.core",
        ],
    )
    def test_harness_bookkeeping_is_forbidden(self, path):
        assert classify_path(SCOPE, path) is ScopeTier.FORBIDDEN
        assert is_forbidden_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            ".booley_project/adapters/thing.py",
            ".booley_project/docs/fw/memory-map.md",
        ],
    )
    def test_authored_project_content_is_carved_out(self, path):
        """Non-contract project adapters and docs remain ordinary authored content."""
        assert classify_path(SCOPE, path) is ScopeTier.ADVISORY

    def test_backslashes_and_dot_prefix_normalize(self):
        assert is_forbidden_path(r".booley_project\booley.toml")
        assert is_forbidden_path("./.scope.json")

    def test_wildcard_scope_owns_nothing(self):
        """A ticket that named nothing owns nothing -- everything is reported."""
        assert classify_path(["*"], "rtl/dut.sv") is ScopeTier.ADVISORY

    def test_empty_scope_owns_nothing(self):
        assert classify_path([], "rtl/dut.sv") is ScopeTier.ADVISORY

    def test_directory_entry_owns_its_subtree(self):
        assert classify_path(["rtl/verilog"], "rtl/verilog/dut.sv") is ScopeTier.OWNED


SCOPE_NEW = ["rtl/dut.sv", "verif/lane1/*.sv [new]"]


class TestRestoreArtifacts:
    def test_deletion_under_a_new_glob_is_an_artifact(self):
        assert is_restore_artifact(SCOPE_NEW, "verif/lane1/old_tb.sv", " D")

    def test_addition_under_a_new_glob_is_real_work(self):
        assert not is_restore_artifact(SCOPE_NEW, "verif/lane1/new_tb.sv", "??")

    def test_out_of_scope_deletion_is_real_work(self):
        """An outside deletion is ordinary triage work, not restore fallout."""
        assert not is_restore_artifact(SCOPE_NEW, "rtl/legacy.sv", " D")

    def test_deletion_owned_by_a_plain_entry_is_real_work(self):
        assert not is_restore_artifact(SCOPE_NEW, "rtl/dut.sv", " D")

    def test_a_plain_entry_wins_over_an_overlapping_new_glob(self):
        scope = ["verif/lane1/old_tb.sv", "verif/lane1/*.sv [new]"]
        assert not is_restore_artifact(scope, "verif/lane1/old_tb.sv", " D")


def _init_repo(path: Path) -> None:
    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)

    path.mkdir(parents=True, exist_ok=True)
    run("init", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (path / "rtl").mkdir()
    (path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "base")


class TestCommittedDeviations:
    def _branch_with(self, repo: Path, files: dict[str, str]) -> None:
        subprocess.run(
            ["git", "checkout", "-b", "feat"], cwd=repo, check=True, capture_output=True
        )
        for rel, text in files.items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "work"], cwd=repo, check=True, capture_output=True)

    def test_in_scope_only_yields_no_deviations(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        self._branch_with(repo, {"rtl/dut.sv": "module dut; wire a; endmodule\n"})
        assert committed_deviations(repo, "main", SCOPE) == ([], [])

    def test_out_of_scope_commit_is_reported(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        self._branch_with(
            repo,
            {
                "rtl/dut.sv": "module dut; wire a; endmodule\n",
                "rtl/pkg.sv": "package p; endpackage\n",
            },
        )
        assert committed_deviations(repo, "main", SCOPE) == (["rtl/pkg.sv"], [])

    def test_reads_the_branch_not_the_worktree(self, tmp_path):
        """Uncommitted noise is not a deviation -- only what the branch carries."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        self._branch_with(repo, {"rtl/dut.sv": "module dut; wire a; endmodule\n"})
        (repo / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")
        assert committed_deviations(repo, "main", SCOPE) == ([], [])

    def test_missing_base_branch_is_undecidable_not_clean(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        assert committed_deviations(repo, "no-such-branch", SCOPE) is None


class TestHarnessPathsAreSeparate:
    def test_a_leaked_forbidden_path_is_not_an_agent_deviation(self, tmp_path):
        """Setup's own --no-verify commit puts these on the branch."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        subprocess.run(
            ["git", "checkout", "-b", "feat"], cwd=repo, check=True, capture_output=True
        )
        (repo / ".booley_project").mkdir()
        (repo / ".booley_project" / "booley.toml").write_text("x\n", encoding="utf-8")
        (repo / "rtl" / "pkg.sv").write_text("package p; endpackage\n", encoding="utf-8")
        # -f: a real project usually gitignores the project dir, and the host's
        # global excludes may too. Force it so the leak this test models exists.
        subprocess.run(["git", "add", "-A", "-f"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--no-verify", "-m", "setup"],
            cwd=repo,
            check=True,
            capture_output=True,
        )

        deviations, harness_paths = committed_deviations(repo, "main", SCOPE)
        assert deviations == ["rtl/pkg.sv"]
        assert harness_paths == [".booley_project/booley.toml"]


class TestDeviationReport:
    def test_clean_run_still_writes_a_report(self, tmp_path):
        report = tmp_path / DEVIATION_REPORT_NAME
        write_deviation_report(report, slug="t", base_branch="main", scope=SCOPE, result=([], []))
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["decidable"] is True
        assert payload["deviations"] == []
        assert payload["harness_paths"] == []

    def test_undecidable_is_distinguishable_from_clean(self, tmp_path):
        report = tmp_path / DEVIATION_REPORT_NAME
        write_deviation_report(report, slug="t", base_branch="main", scope=SCOPE, result=None)
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["decidable"] is False
        assert payload["deviations"] == []

    def test_creates_missing_parents(self, tmp_path):
        report = tmp_path / "logs" / "slug" / ".runtime" / DEVIATION_REPORT_NAME
        write_deviation_report(
            report, slug="t", base_branch="main", scope=SCOPE, result=(["rtl/pkg.sv"], [])
        )
        assert json.loads(report.read_text(encoding="utf-8"))["deviations"] == ["rtl/pkg.sv"]

    def test_an_unwritable_report_never_raises(self, tmp_path):
        """Informational only -- it must not take down a run that succeeded."""
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory\n", encoding="utf-8")
        write_deviation_report(
            blocker / DEVIATION_REPORT_NAME,
            slug="t",
            base_branch="main",
            scope=SCOPE,
            result=([], []),
        )
