"""Reviewer ``--base``: review only the changes since a base commit (issue #3270).

Every test drives the real Reviewer against an isolated Git repository with a
scripted agent response (``_invoke_agent`` is a ``MagicMock``), never a live model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from booley.criteria.state import DevelopmentState
from booley.mcp.base import McpToolResult
from booley.specialists.reviewer import ReviewerSpecialist
from tests.project_runtime_git_support import git

CLEAN_KEY = "review_rtl_bugs_clean"
DONE_KEY = "review_rtl_bugs_done"

A_SV = "module a(input logic x);\nendmodule\n"
B_SV = "module b;\n  a u_a (.x(1'b0));\nendmodule\n"
C_SV = "module c;\nendmodule\n"
E_SV = "module e;\nendmodule\n"


def finding(
    file: str = "rtl/a.sv",
    line: int = 2,
    summary: str = "Wrong reset value",
    origin: str | None = "introduced",
) -> dict[str, Any]:
    """One schema-valid scripted Reviewer finding."""
    row: dict[str, Any] = {
        "severity": "MAJOR",
        "confidence": "HIGH",
        "category": "bugs",
        "file": file,
        "line": line,
        "summary": summary,
        "kind": "code_defect",
        "disposition": "current",
        "ticket_clause": "Reset clears output",
    }
    if origin is not None:
        row["origin"] = origin
    return row


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Committed baseline on ``main`` plus a checked-out ``feature`` branch."""
    config = tmp_path / "git-config"
    config.mkdir()
    empty = config / "empty"
    empty.touch()
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("HOME", str(config))
    root = tmp_path / "repo"
    (root / "rtl").mkdir(parents=True)
    for name, text in {"a": A_SV, "b": B_SV, "c": C_SV, "e": E_SV}.items():
        (root / "rtl" / f"{name}.sv").write_text(text, newline="\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "core.excludesFile", str(empty))
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    git(root, "checkout", "-q", "-b", "feature")
    state_file = tmp_path / "state.json"
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_file))
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    DevelopmentState.load(state_file).save()
    return root


def commit_edit(root: Path, name: str, text: str, message: str) -> None:
    """Commit *text* as the content of ``rtl/<name>.sv`` on the current branch."""
    (root / "rtl" / name).write_text(text, newline="\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", message)


def review(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    issues: list[dict[str, Any]] | None = None,
) -> tuple[McpToolResult, ReviewerSpecialist, MagicMock]:
    """Run one discovery review with a scripted response; return result, endpoint, mock."""
    reviewer = ReviewerSpecialist()
    reviewer.parse_args(["--work-dir", str(root), "--category", "rtl", "--focus", "bugs", *args])
    reviewer.read_state()
    invoke = MagicMock(
        return_value=MagicMock(
            output=json.dumps({"issues": issues or []}),
            captured_agent_capability_calls={},
        )
    )
    monkeypatch.setattr(reviewer, "_invoke_agent", invoke)
    return reviewer._run(), reviewer, invoke


def prompt_of(invoke: MagicMock) -> str:
    return invoke.call_args.args[0].prompt


def review_list(prompt: str) -> list[str]:
    """Files named in the 'Review the following files' list, before any other section."""
    head = prompt.split("## Focus", 1)[0]
    return [line.strip()[2:] for line in head.splitlines() if line.strip().startswith("- ")]


def test_base_reviews_only_changed_scope_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit_edit(repo, "a.sv", A_SV + "// committed edit\n", "Edit a")
    (repo / "rtl/e.sv").write_text(E_SV + "// uncommitted, outside scope\n", newline="\n")
    (repo / "rtl/d.sv").write_text("module d;\nendmodule\n", newline="\n")
    scope = "rtl/a.sv,rtl/c.sv,rtl/d.sv"

    result, reviewer, invoke = review(repo, monkeypatch, ["--scope", scope, "--base", "main"])

    assert result.exit_code == 0
    assert review_list(prompt_of(invoke)) == ["rtl/a.sv", "rtl/d.sv"]
    assert "rtl/c.sv" not in prompt_of(invoke)
    assert "rtl/e.sv" not in prompt_of(invoke)
    assert result.detail["contract"]["scope"] == ["rtl/a.sv", "rtl/d.sv"]
    assert result.report_text.startswith("Reviewing changes since main")
    assert "2 file(s)" in result.report_text
    assert '"origin"' in invoke.call_args.args[0].system_prompt
    assert reviewer._workspace_isolation_scope() == ("rtl/a.sv", "rtl/c.sv", "rtl/d.sv")


@pytest.mark.parametrize("base", ["no-such-ref", "--version"])
def test_bad_base_is_refused_without_model_call(
    repo: Path, monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    result, _, invoke = review(repo, monkeypatch, ["--scope", "rtl/a.sv", f"--base={base}"])

    assert result.exit_code == 2
    assert f"--base {base}" in result.report_text
    invoke.assert_not_called()


def test_base_without_changes_in_scope_exits_zero_without_model_call(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "rtl/e.sv").write_text(E_SV + "// outside scope\n", newline="\n")

    result, _, invoke = review(
        repo, monkeypatch, ["--scope", "rtl/a.sv,rtl/c.sv", "--base", "main"]
    )

    assert result.exit_code == 0
    assert "No changes in scope since main" in result.report_text
    invoke.assert_not_called()


def test_missing_spec_is_refused_even_when_nothing_changed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _, invoke = review(
        repo, monkeypatch, ["--scope", "rtl/a.sv", "--focus", "spec", "--base", "main"]
    )

    assert result.exit_code == 2
    assert "needs a spec" in result.report_text
    invoke.assert_not_called()


def test_base_without_changes_writes_no_state(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, reviewer, invoke = review(repo, monkeypatch, ["--scope", "rtl/a.sv", "--base", "main"])

    assert result.exit_code == 0
    invoke.assert_not_called()
    assert DONE_KEY not in reviewer.state.criteria
    assert DONE_KEY not in DevelopmentState.load(Path(reviewer.args.state_file)).criteria


def test_base_ignores_changes_that_reached_base_after_merge_base(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(repo, "checkout", "-q", "main")
    commit_edit(repo, "c.sv", C_SV + "// main-only-change\n", "Main moves on")
    git(repo, "checkout", "-q", "feature")
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")

    result, _, invoke = review(
        repo, monkeypatch, ["--scope", "rtl/a.sv,rtl/c.sv", "--base", "main"]
    )

    assert result.exit_code == 0
    prompt = prompt_of(invoke)
    assert review_list(prompt) == ["rtl/a.sv"]
    assert "rtl/c.sv" not in prompt
    assert "main-only-change" not in prompt
    assert "Main moves on" not in prompt


def test_unchanged_users_are_context_not_reviewed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")

    result, _, invoke = review(
        repo,
        monkeypatch,
        ["--scope", "rtl/a.sv,rtl/b.sv,rtl/c.sv", "--base", "main"],
        [finding("rtl/b.sv", 2, "Bad connection at the instance")],
    )

    prompt = prompt_of(invoke)
    reviewed, change_section = prompt.split("## Change under review", 1)
    assert review_list(prompt) == ["rtl/a.sv"]
    assert "rtl/b.sv" not in reviewed
    assert "rtl/b.sv" in change_section
    assert "rtl/c.sv" not in prompt
    assert result.detail["issues"] == 0
    assert result.detail["issue_list"] == []
    assert [row["reason"] for row in result.detail["filtered"]] == ["source_scope"]


def test_commit_messages_and_diff_are_in_prompt(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit_edit(repo, "a.sv", A_SV + "// introduced marker\n", "Tighten reset of a\n\nBody line.")
    commit_edit(repo, "a.sv", A_SV + "// introduced marker\n// second\n", "Second commit")

    _, _, invoke = review(repo, monkeypatch, ["--scope", "rtl/a.sv", "--base", "main"])

    prompt = prompt_of(invoke)
    assert "Tighten reset of a" in prompt
    assert "Body line." in prompt
    assert "Second commit" in prompt
    assert "+// introduced marker" in prompt
    assert "+// second" in prompt


def test_untracked_only_change_says_no_commits(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "rtl/d.sv").write_text("module d;\nendmodule\n", newline="\n")

    _, _, invoke = review(repo, monkeypatch, ["--scope", "rtl/c.sv,rtl/d.sv", "--base", "main"])

    prompt = prompt_of(invoke)
    assert "No commits in range; uncommitted changes only." in prompt
    assert "new untracked file: rtl/d.sv" in prompt


def test_pre_existing_findings_are_observations(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")
    args = ["--scope", "rtl/a.sv", "--base", "main"]
    introduced = finding(line=3, summary="Introduced bug")
    old = finding(line=1, summary="Old bug", origin="pre_existing")

    result, _, _ = review(repo, monkeypatch, args, [introduced, old])

    assert result.exit_code == 0
    assert result.detail["issues"] == 1
    assert result.detail["MAJOR"] == 1
    assert result.detail["review_outcome"] == "corrective"
    text = result.report_text
    header = "PRE-EXISTING (non-blocking; present before this change):"
    before, after = text.split(header, 1)
    assert "Introduced bug" in before
    assert "Old bug" not in before
    assert "Old bug" in after
    assert "Introduced bug" not in after

    # --verify re-checks only the introduced finding and keeps the observation.
    introduced_id = next(
        row["finding_id"] for row in result.detail["issue_list"] if row["line"] == 3
    )
    reviewer = ReviewerSpecialist()
    reviewer.parse_args(["--work-dir", str(repo), "--verify"])
    reviewer.read_state()
    invoke = MagicMock(
        return_value=MagicMock(
            output=json.dumps({"findings": [{"index": 1, "status": "FIXED", "evidence": "gone"}]}),
            captured_agent_capability_calls={},
        )
    )
    monkeypatch.setattr(reviewer, "_invoke_agent", invoke)
    verified = reviewer._run()

    prompt = prompt_of(invoke)
    assert introduced_id in prompt
    assert "Old bug" not in prompt
    rows = verified.detail["issue_list"]
    assert [row["summary"] for row in rows if row.get("origin") == "pre_existing"] == ["Old bug"]
    assert [row["status"] for row in rows if row["summary"] == "Introduced bug"] == ["fixed"]


def test_same_finding_reported_as_introduced_and_pre_existing_stays_open(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")
    args = ["--scope", "rtl/a.sv", "--base", "main"]
    twin = finding(line=3, summary="Twin")

    result, _, _ = review(repo, monkeypatch, args, [twin, {**twin, "origin": "pre_existing"}])

    assert result.detail["issues"] == 1
    assert sorted(row["origin"] for row in result.detail["issue_list"]) == [
        "introduced",
        "pre_existing",
    ]


def test_pre_existing_findings_do_not_fail_clean_gate(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = DevelopmentState.load(repo.parent / "state.json")
    state.init_criteria({CLEAN_KEY: True})
    state.save()
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")

    result, _, _ = review(
        repo,
        monkeypatch,
        ["--scope", "rtl/a.sv", "--base", "main"],
        [finding(line=1, summary="Old bug", origin="pre_existing")],
    )

    assert result.exit_code == 0
    assert result.criterion_met is True
    assert result.detail["pending"] == []
    assert [row["summary"] for row in result.detail["observations"]] == ["Old bug"]
    assert "PRE-EXISTING (non-blocking; present before this change):" in result.report_text


def test_diff_review_rejects_finding_without_origin(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")

    result, _, _ = review(
        repo,
        monkeypatch,
        ["--scope", "rtl/a.sv", "--base", "main"],
        [finding(origin=None)],
    )

    assert result.exit_code == 2
    assert "failed the schema" in result.report_text


def test_origin_is_ignored_without_base(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, _, invoke = review(
        repo,
        monkeypatch,
        ["--scope", "rtl/a.sv"],
        [finding(origin="pre_existing")],
    )

    assert result.exit_code == 0
    assert result.detail["issues"] == 1
    assert "PRE-EXISTING" not in result.report_text
    assert "origin" not in invoke.call_args.args[0].system_prompt
    assert "Change under review" not in prompt_of(invoke)
    assert all("origin" not in row for row in result.detail["issue_list"])


def test_clean_mode_info_counts_pre_existing_observations_separately(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = DevelopmentState.load(repo.parent / "state.json")
    state.init_criteria({CLEAN_KEY: True})
    state.save()
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")
    advisory = {**finding(line=2, summary="Style nit"), "disposition": "advisory"}

    result, _, _ = review(
        repo,
        monkeypatch,
        ["--scope", "rtl/a.sv", "--base", "main"],
        [advisory, finding(line=1, summary="Old bug", origin="pre_existing")],
    )

    assert result.exit_code == 0
    assert "preserved 1 advisory/deferred observation(s)" in result.report_text
    assert "preserved 1 pre-existing observation(s)" in result.report_text


def test_dry_run_with_base_previews_only_changed_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")

    result, _, invoke = review(
        repo, monkeypatch, ["--scope", "rtl/a.sv,rtl/c.sv", "--base", "main", "--dry-run"]
    )

    assert result.exit_code == 0
    assert result.detail["scope"] == ["rtl/a.sv"]
    invoke.assert_not_called()
