"""Reviewer ``--base``: review only the changes since a base commit (issue #3270).

Every test drives the real Reviewer against an isolated Git repository with a
scripted agent (the shared ``ScriptedAgent``), never a live model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from booley.runtime.endpoint_execution import EndpointOutcome
from booley.specialists.reviewer import ReviewerSpecialist
from tests.mcp_tools.reviewer_support import (
    ScriptedAgent,
    guide_of,
    install_agent,
    issue,
    open_findings,
    run_review,
)
from tests.project_runtime_git_support import git

A_SV = "module a(input logic x);\nendmodule\n"
B_SV = "module b;\n  a u_a (.x(1'b0));\nendmodule\n"
C_SV = "module c;\nendmodule\n"
E_SV = "module e;\nendmodule\n"


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> ScriptedAgent:
    return install_agent(monkeypatch)


def finding(origin: str | None = "introduced", **overrides: Any) -> dict[str, Any]:
    """One schema-valid scripted finding in ``rtl/a.sv`` (no ``origin`` key when None)."""
    defaults = {"file": "rtl/a.sv", "line": 2, "ticket_clause": "Reset clears output"}
    row = issue(**{**defaults, **overrides})
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
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    return root


def commit_edit(root: Path, name: str, text: str, message: str) -> None:
    """Commit *text* as the content of ``rtl/<name>.sv`` on the current branch."""
    (root / "rtl" / name).write_text(text, newline="\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", message)


def base_argv(root: Path, *args: str) -> list[str]:
    return ["--work-dir", str(root), "--category", "rtl", "--focus", "bugs", *args]


def review(
    root: Path,
    agent: ScriptedAgent,
    args: list[str],
    issues: list[dict[str, Any]] | None = None,
) -> EndpointOutcome:
    """Run one discovery review whose agent reports *issues*."""
    agent.answer({"issues": issues or []})
    return run_review(base_argv(root, *args))[0]


def prompt_of(agent: ScriptedAgent) -> str:
    return agent.calls[0].prompt


def review_list(prompt: str) -> list[str]:
    """Files named in the 'Review the following files' list, before any other section."""
    head = prompt.split("## Focus", 1)[0]
    return [line.strip()[2:] for line in head.splitlines() if line.strip().startswith("- ")]


def test_base_reviews_only_changed_scope_files(repo: Path, agent: ScriptedAgent) -> None:
    commit_edit(repo, "a.sv", A_SV + "// committed edit\n", "Edit a")
    (repo / "rtl/e.sv").write_text(E_SV + "// uncommitted, outside scope\n", newline="\n")
    (repo / "rtl/d.sv").write_text("module d;\nendmodule\n", newline="\n")
    scope = "rtl/a.sv,rtl/c.sv,rtl/d.sv"

    result, reviewer = run_review(base_argv(repo, "--scope", scope, "--base", "main"))

    assert result.exit_code == 0
    assert review_list(prompt_of(agent)) == ["rtl/a.sv", "rtl/d.sv"]
    assert "rtl/c.sv" not in prompt_of(agent)
    assert "rtl/e.sv" not in prompt_of(agent)
    assert result.detail["contract"]["scope"] == ["rtl/a.sv", "rtl/d.sv"]
    assert result.report_text.startswith("Reviewing changes since main")
    assert "2 file(s)" in result.report_text
    assert '"origin"' in agent.calls[0].system_prompt
    # Private seam: the snapshot scope is visible only inside a real snapshot, which this
    # module skips.
    assert reviewer._workspace_isolation_scope() == ("rtl/a.sv", "rtl/c.sv", "rtl/d.sv")


@pytest.mark.parametrize("base", ["no-such-ref", "--version"])
def test_bad_base_is_refused_without_model_call(
    repo: Path, agent: ScriptedAgent, base: str
) -> None:
    result = review(repo, agent, ["--scope", "rtl/a.sv", f"--base={base}"])

    assert result.exit_code == 2
    assert f"--base {base}" in result.report_text
    assert agent.calls == []


@pytest.mark.parametrize(
    ("mode", "exit_code", "report"),
    [
        (["--base", "main"], 0, "No changes in scope since main"),
        (["--dry-run"], 0, "reviewer dry-run: rtl/bugs; 1 file(s)"),
        ([], 0, "RESULT: REVIEWED WITH FINDINGS"),
        (["--fail-on", "major"], 1, "RESULT: REVIEWED WITH FINDINGS"),
    ],
    ids=["no_change", "dry_run", "completed", "fail_on"],
)
def test_cli_prints_the_report_once(
    repo: Path,
    agent: ScriptedAgent,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: list[str],
    exit_code: int,
    report: str,
) -> None:
    monkeypatch.setattr("booley.runtime.runtime_context.inside_session_runtime", lambda: True)
    monkeypatch.delenv("BOOLEY_DIR")  # the endpoints conftest points it at a non-Project
    (repo / ".booley").mkdir()
    (repo / "rtl/e.sv").write_text(E_SV + "// outside scope\n", newline="\n")
    agent.answer({"issues": [finding()]})
    argv = ["--project", str(repo), "--category", "rtl", "--focus", "bugs", "--scope", "rtl/a.sv"]

    assert ReviewerSpecialist().main([*argv, *mode]) == exit_code

    shown = capsys.readouterr()
    assert (shown.out + shown.err).count(report) == 1
    assert report in (shown.out if exit_code == 0 else shown.err)


def test_base_without_changes_in_scope_exits_zero_without_model_call(
    repo: Path, agent: ScriptedAgent
) -> None:
    (repo / "rtl/e.sv").write_text(E_SV + "// outside scope\n", newline="\n")

    result = review(repo, agent, ["--scope", "rtl/a.sv,rtl/c.sv", "--base", "main"])

    assert result.exit_code == 0
    assert "No changes in scope since main" in result.report_text
    assert agent.calls == []


def test_missing_spec_is_refused_even_when_nothing_changed(
    repo: Path, agent: ScriptedAgent
) -> None:
    result = review(repo, agent, ["--scope", "rtl/a.sv", "--focus", "spec", "--base", "main"])

    assert result.exit_code == 2
    assert "needs a spec" in result.report_text
    assert agent.calls == []


def test_base_without_changes_makes_no_call(repo: Path, agent: ScriptedAgent) -> None:
    result = review(repo, agent, ["--scope", "rtl/a.sv", "--base", "main"])

    assert result.exit_code == 0
    assert agent.calls == []
    assert "audit_evidence" not in result.detail


def test_base_ignores_changes_that_reached_base_after_merge_base(
    repo: Path, agent: ScriptedAgent
) -> None:
    git(repo, "checkout", "-q", "main")
    commit_edit(repo, "c.sv", C_SV + "// main-only-change\n", "Main moves on")
    git(repo, "checkout", "-q", "feature")
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")

    result = review(repo, agent, ["--scope", "rtl/a.sv,rtl/c.sv", "--base", "main"])

    assert result.exit_code == 0
    prompt = prompt_of(agent)
    assert review_list(prompt) == ["rtl/a.sv"]
    assert "rtl/c.sv" not in prompt
    assert "main-only-change" not in prompt
    assert "Main moves on" not in prompt


def test_unchanged_users_are_context_not_reviewed(repo: Path, agent: ScriptedAgent) -> None:
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")

    result = review(
        repo,
        agent,
        ["--scope", "rtl/a.sv,rtl/b.sv,rtl/c.sv", "--base", "main"],
        [finding(file="rtl/b.sv", summary="Bad connection at the instance")],
    )

    prompt = prompt_of(agent)
    reviewed, change_section = prompt.split("## Change under review", 1)
    assert review_list(prompt) == ["rtl/a.sv"]
    assert "rtl/b.sv" not in reviewed
    assert "rtl/b.sv" in change_section
    assert "rtl/c.sv" not in prompt
    assert result.detail["issues"] == 0
    assert result.detail["issue_list"] == []
    # The bugs review's two guide calls each report the out-of-scope finding.
    assert [row["reason"] for row in result.detail["filtered"]] == ["source_scope"] * 2


def test_commit_messages_and_diff_are_in_prompt(repo: Path, agent: ScriptedAgent) -> None:
    commit_edit(repo, "a.sv", A_SV + "// introduced marker\n", "Tighten reset of a\n\nBody line.")
    commit_edit(repo, "a.sv", A_SV + "// introduced marker\n// second\n", "Second commit")

    review(repo, agent, ["--scope", "rtl/a.sv", "--base", "main"])

    prompt = prompt_of(agent)
    assert "Tighten reset of a" in prompt
    assert "Body line." in prompt
    assert "Second commit" in prompt
    assert "+// introduced marker" in prompt
    assert "+// second" in prompt


def test_untracked_only_change_says_no_commits(repo: Path, agent: ScriptedAgent) -> None:
    (repo / "rtl/d.sv").write_text("module d;\nendmodule\n", newline="\n")

    review(repo, agent, ["--scope", "rtl/c.sv,rtl/d.sv", "--base", "main"])

    prompt = prompt_of(agent)
    assert "No commits in range; uncommitted changes only." in prompt
    assert "new untracked file: rtl/d.sv" in prompt


def test_pre_existing_findings_are_observations(repo: Path, agent: ScriptedAgent) -> None:
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")
    args = ["--scope", "rtl/a.sv", "--base", "main"]
    introduced = finding(line=3, summary="Introduced bug")
    old = finding(line=1, summary="Old bug", origin="pre_existing")

    result = review(repo, agent, args, [introduced, old])

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


def test_same_finding_reported_as_introduced_and_pre_existing_stays_open(
    repo: Path, agent: ScriptedAgent
) -> None:
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")
    args = ["--scope", "rtl/a.sv", "--base", "main"]
    twin = finding(line=3, summary="Twin")

    result = review(repo, agent, args, [twin, {**twin, "origin": "pre_existing"}])

    assert result.detail["issues"] == 1
    assert sorted(row["origin"] for row in result.detail["issue_list"]) == [
        "introduced",
        "pre_existing",
    ]


def test_pre_existing_findings_do_not_fail_the_review(repo: Path, agent: ScriptedAgent) -> None:
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")

    result = review(
        repo,
        agent,
        ["--scope", "rtl/a.sv", "--base", "main"],
        [finding(line=1, summary="Old bug", origin="pre_existing")],
    )

    assert result.exit_code == 0
    assert open_findings(result.detail) == []
    assert [row["summary"] for row in result.detail["issue_list"]] == ["Old bug"]
    assert "PRE-EXISTING (non-blocking; present before this change):" in result.report_text


def test_diff_review_rejects_finding_without_origin(repo: Path, agent: ScriptedAgent) -> None:
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")

    result = review(
        repo,
        agent,
        ["--scope", "rtl/a.sv", "--base", "main"],
        [finding(origin=None)],
    )

    assert result.exit_code == 2
    assert "failed the schema" in result.report_text


def test_origin_is_ignored_without_base(repo: Path, agent: ScriptedAgent) -> None:
    result = review(
        repo,
        agent,
        ["--scope", "rtl/a.sv"],
        [finding(origin="pre_existing")],
    )

    assert result.exit_code == 0
    assert result.detail["issues"] == 1
    assert "PRE-EXISTING" not in result.report_text
    assert "origin" not in agent.calls[0].system_prompt
    assert "Change under review" not in prompt_of(agent)
    assert all("origin" not in row for row in result.detail["issue_list"])


def test_dry_run_with_base_previews_only_changed_files(repo: Path, agent: ScriptedAgent) -> None:
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")

    result = review(repo, agent, ["--scope", "rtl/a.sv,rtl/c.sv", "--base", "main", "--dry-run"])

    assert result.exit_code == 0
    assert result.detail["scope"] == ["rtl/a.sv"]
    assert agent.calls == []


def test_base_gives_every_guide_call_the_same_change_section(
    repo: Path, agent: ScriptedAgent
) -> None:
    """The bugs review's checklist and hunt calls share one change section and origin rule."""
    commit_edit(repo, "a.sv", A_SV + "// feature edit\n", "Feature edit")
    agent.answer(
        {
            "bugs-checklist": {"issues": [finding(line=3, summary="Introduced bug")]},
            "bugs-hunt": {"issues": [finding(line=1, summary="Old bug", origin="pre_existing")]},
        }
    )

    result = run_review(base_argv(repo, "--scope", "rtl/a.sv", "--base", "main"))[0]

    assert result.exit_code == 0, result.report_text
    assert {guide_of(call) for call in agent.calls} == {"bugs-checklist", "bugs-hunt"}
    (prompt,) = {call.prompt for call in agent.calls}
    assert "## Change under review" in prompt
    assert "Feature edit" in prompt
    for call in agent.calls:
        assert '"origin"' in call.system_prompt  # the origin rule reaches every call
    # A hunt finding that predates the change stays a non-blocking observation.
    rows = {row["summary"]: row for row in result.detail["issue_list"]}
    assert rows["Old bug"]["status"] == "reported"
    assert rows["Old bug"]["guide_kinds"] == ["hunt"]
    assert result.detail["issues"] == 1
    assert [row["summary"] for row in open_findings(result.detail)] == ["Introduced bug"]
