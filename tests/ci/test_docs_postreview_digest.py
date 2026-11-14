"""Contracts for the weekly Docs post-review digest."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.subprocess_budget import run_bounded_child

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

from doc_change_class import ClassifierError, classify, load_rules


class _Digest:
    """Imports the script on first use, so a missing script fails the test, not collection."""

    def __getattr__(self, name: str) -> Any:
        import docs_postreview_digest

        return getattr(docs_postreview_digest, name)


digest = _Digest()
_RULES = _ROOT / ".github/scripts/doc_change_rules.toml"
_WORKFLOW = _ROOT / ".github/workflows/docs-postreview-digest.yml"
_CUTOFF = "2026-11-16T06:00:00Z"
_PREVIOUS = "2026-11-09T06:00:00Z"


def _pr(number: int, merged_at: str, title: str | None = None) -> dict[str, Any]:
    return {
        "number": number,
        "title": title or f"Change {number}",
        "url": f"https://github.com/o/r/pull/{number}",
        "mergedAt": merged_at,
        "mergeCommit": {"oid": f"sha{number}"},
    }


def _issue(number: int, state: str, body: str) -> dict[str, Any]:
    return {"number": number, "state": state, "body": body}


def _body(cutoff: str, entries: list[tuple[int, str, str, str]]) -> str:
    """An earlier digest body: entries are (pr, kinds, reviewed mark, needed mark)."""
    lines = [f"<!-- docs-postreview-digest cutoff={cutoff} -->", "", "## Pull requests", ""]
    for pr, kinds, reviewed, needed in entries:
        lines += [
            f"### #{pr} Title",
            f"<!-- docs-postreview-digest-entry pr={pr} kinds={kinds} -->",
            "",
            f"- [{reviewed}] reviewed",
            f"- [{needed}] needed edits",
            "",
        ]
    return "\n".join(lines)


class FakeGh:
    """Answers the digest's gh calls from canned data and records every call."""

    def __init__(
        self,
        prs: list[list[dict[str, Any]]] | None = None,
        issues: list[dict[str, Any]] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.prs = prs or [[]]
        self.issues = issues or []
        self.fail_on = fail_on
        self.calls: list[tuple[list[str], str | None]] = []
        self.pr_cursors: list[str | None] = []

    def __call__(self, args: list[str], stdin: str | None = None) -> str:
        self.calls.append((args, stdin))
        if self.fail_on and self.fail_on in " ".join(args):
            raise digest.DigestError(f"gh {args[0]} failed: boom")
        if args[:2] == ["api", "graphql"]:
            return self._graphql(args)
        return ""

    def _graphql(self, args: list[str]) -> str:
        fields = dict(a.split("=", 1) for a in args if "=" in a and not a.startswith("query="))
        query = next(a for a in args if a.startswith("query="))
        if "pullRequests" in query:
            cursor = fields.get("cursor")
            self.pr_cursors.append(cursor)
            index = 0 if cursor is None else int(cursor.removeprefix("c"))
            more = index + 1 < len(self.prs)
            connection = {
                "nodes": self.prs[index],
                "pageInfo": {"hasNextPage": more, "endCursor": f"c{index + 1}" if more else None},
            }
            repository = {"pullRequests": connection}
        else:
            connection = {"nodes": self.issues, "pageInfo": {"hasNextPage": False}}
            repository = {"issues": connection}
        return json.dumps({"data": {"repository": repository}})

    @property
    def writes(self) -> list[tuple[list[str], str | None]]:
        return [call for call in self.calls if call[0][0] != "api" or "--method" in call[0]]

    def created_issue(self) -> dict[str, Any]:
        posts = [stdin for args, stdin in self.writes if args[0] == "api"]
        assert len(posts) == 1 and posts[0] is not None
        return json.loads(posts[0])


def _files(*files: tuple[str, str, str]) -> Any:
    def classifier(sha: str) -> dict[str, object]:
        return {
            "class": "postreview",
            "files": [
                {"path": p, "status": "modified", "class": c, "rule": r} for p, c, r in files
            ],
        }

    return classifier


_DEFAULT = _files(("docs/internals/ARCHITECTURE.md", "postreview", "default"))


def _main(
    gh: FakeGh,
    monkeypatch: pytest.MonkeyPatch,
    *argv: str,
    classifier: Any = _DEFAULT,
) -> int:
    monkeypatch.setenv("GH_REPO", "o/r")
    return digest.main(["--cutoff", _CUTOFF, *argv], gh=gh, classify_pr=classifier)


def test_window_takes_prs_after_previous_cutoff_through_cutoff(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh(
        prs=[
            [
                _pr(1, "2026-11-08T12:00:00Z"),
                _pr(2, _PREVIOUS),
                _pr(3, "2026-11-09T06:00:01Z"),
                _pr(4, _CUTOFF),
                _pr(5, "2026-11-16T06:00:01Z"),
            ]
        ],
        issues=[_issue(7, "CLOSED", _body(_PREVIOUS, []))],
    )
    assert _main(gh, monkeypatch) == 0
    issue = gh.created_issue()
    assert issue["title"] == "Docs post-review: 09 NOV 2026 to 16 NOV 2026"
    assert issue["labels"] == ["docs-postreview-digest"]
    body = issue["body"]
    assert body.startswith(f"<!-- docs-postreview-digest cutoff={_CUTOFF} -->")
    assert "### #3 Change 3" in body
    assert "### #4 Change 4" in body
    for excluded in ("#1 ", "#2 ", "#5 "):
        assert f"### {excluded}" not in body
    assert gh.writes[0][0][:3] == ["label", "create", "docs-postreview-digest"]


def test_previous_cutoff_is_the_latest_of_open_and_closed_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = FakeGh(
        prs=[[_pr(1, "2026-11-02T12:00:00Z"), _pr(2, "2026-11-10T12:00:00Z")]],
        issues=[
            _issue(7, "OPEN", _body(_PREVIOUS, [])),
            _issue(6, "CLOSED", _body("2026-11-02T06:00:00Z", [])),
        ],
    )
    assert _main(gh, monkeypatch) == 0
    body = gh.created_issue()["body"]
    assert "### #2 " in body
    assert "### #1 " not in body


def test_first_run_lists_every_labeled_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = FakeGh(prs=[[_pr(1, "2026-10-20T08:00:00Z")], [_pr(2, "2026-11-12T08:00:00Z")]])
    assert _main(gh, monkeypatch) == 0
    issue = gh.created_issue()
    assert issue["title"] == "Docs post-review: 20 OCT 2026 to 16 NOV 2026"
    assert "### #1 " in issue["body"]
    assert "### #2 " in issue["body"]
    assert gh.pr_cursors == [None, "c1"]


def test_empty_window_opens_no_issue(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh(
        prs=[[_pr(1, "2026-11-01T00:00:00Z")]], issues=[_issue(7, "CLOSED", _body(_PREVIOUS, []))]
    )
    assert _main(gh, monkeypatch) == 0
    assert gh.writes == []
    assert capsys.readouterr().out.strip() == (
        f"No docs-postreview pull request merged after {_PREVIOUS} up to {_CUTOFF}; "
        "no digest opened."
    )


def test_empty_first_run_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh()
    assert _main(gh, monkeypatch) == 0
    assert gh.writes == []
    assert capsys.readouterr().out.strip() == (
        f"No docs-postreview pull request merged up to {_CUTOFF}; no digest opened."
    )


def test_workflow_runs_weekly_and_by_hand_with_dry_run() -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow[True]  # PyYAML reads the key `on` as the boolean True
    assert triggers["schedule"] == [{"cron": "0 6 * * 1"}]
    dry_run = triggers["workflow_dispatch"]["inputs"]["dry_run"]
    assert dry_run["type"] == "boolean"
    assert dry_run["default"] is True
    assert workflow["permissions"]["issues"] == "write"
    assert workflow["concurrency"]["group"] == "docs-postreview-digest"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    steps = workflow["jobs"]["digest"]["steps"]
    run = next(s for s in steps if "docs_postreview_digest.py" in s.get("run", ""))
    assert '--cutoff "$CUTOFF"' in run["run"]
    assert '"$DRY_RUN" = true' in run["run"]
    assert "${{" not in run["run"]
    assert run["env"]["DRY_RUN"] == "${{ inputs.dry_run }}"
    first = steps[0]
    assert "date -u" in first["run"]
    assert "uses" in steps[1] and steps[1]["with"]["fetch-depth"] == 0


def _merge_repo(tmp_path: Path) -> tuple[Path, str]:
    """A main with a merge commit that brings a feature branch's doc edits."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = os.environ | {
        "GIT_AUTHOR_NAME": "CI Test",
        "GIT_AUTHOR_EMAIL": "ci@example.invalid",
        "GIT_COMMITTER_NAME": "CI Test",
        "GIT_COMMITTER_EMAIL": "ci@example.invalid",
    }

    def git(*args: str) -> str:
        result = run_bounded_child(["git", "-C", str(repo), *args], check=True, env=env)
        return result.stdout.strip()

    def write(name: str, text: str) -> None:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(text.encode("utf-8"))

    git("init", "-b", "main")
    write("CHANGELOG.md", "# Changelog\n")
    write("docs/internals/ARCHITECTURE.md", "# Architecture\n")
    write("src/x.py", "x = 1\n")
    git("add", "-A")
    git("commit", "-m", "base")
    git("checkout", "-b", "feature")
    write("CHANGELOG.md", "# Changelog\n\n- entry\n")
    write("docs/internals/ARCHITECTURE.md", "# Architecture\n\nMore\n")
    write("src/x.py", "x = 2\n")
    git("add", "-A")
    git("commit", "-m", "feature")
    git("checkout", "main")
    write("src/y.py", "y = 1\n")
    git("add", "-A")
    git("commit", "-m", "main moves")
    git("merge", "--no-ff", "feature", "-m", "Merge pull request #9")
    return repo, git("rev-parse", "HEAD")


def test_entry_lists_files_rules_and_checkboxes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, merge = _merge_repo(tmp_path)
    monkeypatch.chdir(repo)
    pr = _pr(9, "2026-11-12T08:00:00Z", "Explain <!-- the --> thing")
    pr["mergeCommit"] = {"oid": merge}
    gh = FakeGh(prs=[[pr]])

    def classifier(sha: str) -> dict[str, object]:
        return classify(load_rules(_RULES), f"{sha}^1", sha)

    assert _main(gh, monkeypatch, classifier=classifier) == 0
    body = gh.created_issue()["body"]
    entry = body[body.index("### #9") :]
    assert entry == (
        "### #9 Explain &lt;!-- the --> thing\n"
        "<!-- docs-postreview-digest-entry pr=9 kinds=default -->\n"
        "https://github.com/o/r/pull/9\n"
        "\n"
        "- `CHANGELOG.md`: `changelog`\n"
        "- `docs/internals/ARCHITECTURE.md`: `default`\n"
        "\n"
        "- [ ] reviewed\n"
        "- [ ] needed edits\n"
    )


def test_edit_rate_counts_closed_digests_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = _body(
        "2026-11-02T06:00:00Z",
        [
            (1, "default", "x", "x"),
            (2, "default", "X", " "),
            (3, "default", "x", " "),
            (4, "default,glossary", "x", "x"),
            (5, "glossary", " ", "x"),
            (6, "", "x", "x"),
        ],
    )
    open_digest = _body(_PREVIOUS, [(7, "default", "x", "x"), (8, "adr", "x", "x")])
    gh = FakeGh(
        prs=[[_pr(10, "2026-11-12T08:00:00Z")]],
        issues=[_issue(5, "CLOSED", closed), _issue(6, "OPEN", open_digest)],
    )
    assert _main(gh, monkeypatch) == 0
    body = gh.created_issue()["body"]
    assert "| `default` | 4 | 2 | 50% |" in body
    assert "| `glossary` | 1 | 1 | 100% |" in body
    assert "`adr`" not in body


def test_edit_rate_rounds_to_whole_percent_and_handles_no_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [(1, "a", "x", "x"), (2, "a", "x", " "), (3, "a", "x", " ")]
    gh = FakeGh(
        prs=[[_pr(10, "2026-11-12T08:00:00Z")]],
        issues=[_issue(5, "CLOSED", _body("2026-11-02T06:00:00Z", entries))],
    )
    assert _main(gh, monkeypatch) == 0
    assert "| `a` | 3 | 1 | 33% |" in gh.created_issue()["body"]
    none = FakeGh(prs=[[_pr(10, "2026-11-12T08:00:00Z")]])
    assert _main(none, monkeypatch) == 0
    assert "No closed digest has a reviewed pull request yet." in none.created_issue()["body"]


def test_dry_run_prints_digest_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh(prs=[[_pr(3, "2026-11-12T08:00:00Z")]])
    assert _main(gh, monkeypatch, "--dry-run") == 0
    assert gh.writes == []
    out = capsys.readouterr().out
    assert "Docs post-review: 12 NOV 2026 to 16 NOV 2026" in out
    assert "### #3 Change 3" in out


def test_failure_opens_no_issue_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(sha: str) -> dict[str, object]:
        raise ClassifierError("git diff failed")

    for failure in ("listing", "digest", "classifier", "label", "post"):
        issues = [_issue(7, "CLOSED", "no marker here")] if failure == "digest" else []
        fail_on = {"listing": "pullRequests", "label": "label create", "post": "--method"}
        gh = FakeGh(
            prs=[[_pr(3, "2026-11-12T08:00:00Z")]], issues=issues, fail_on=fail_on.get(failure)
        )
        classifier = broken if failure == "classifier" else _DEFAULT
        assert _main(gh, monkeypatch, classifier=classifier) == 1, failure
        assert capsys.readouterr().err.startswith("docs_postreview_digest: "), failure
        posts = [args for args, _ in gh.writes if args[0] == "api"]
        assert len(posts) == (1 if failure == "post" else 0), failure


def test_classifier_failure_names_the_pull_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(sha: str) -> dict[str, object]:
        raise ClassifierError("git diff failed")

    assert (
        _main(FakeGh(prs=[[_pr(3, "2026-11-12T08:00:00Z")]]), monkeypatch, classifier=broken) == 1
    )
    assert "pull request #3: git diff failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "body",
    [
        "<!-- docs-postreview-digest cutoff=yesterday -->",
        _body(_CUTOFF, [(1, "a", "x", "x")]).replace("- [x] needed edits", ""),
        _body(_CUTOFF, [(1, "a", "x", "x")]).replace("kinds=a", "kinds=a b"),
        _body(_CUTOFF, [(1, "a", "x", "x"), (2, "a", "x", " ")]).replace(
            "<!-- docs-postreview-digest-entry pr=1 kinds=a -->\n", ""
        ),
    ],
    ids=["no-marker", "missing-box", "bad-kinds", "orphaned-entry"],
)
def test_unreadable_earlier_digest_fails(
    body: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh(prs=[[_pr(3, "2026-11-12T08:00:00Z")]], issues=[_issue(7, "CLOSED", body)])
    assert _main(gh, monkeypatch) == 1
    assert "issue #7" in capsys.readouterr().err
    assert gh.writes == []


@pytest.mark.parametrize(
    "response",
    ["not json", '{"data": null}', '{"data": {"repository": {"pullRequests": {"nodes": 1}}}}'],
    ids=["not-json", "null-data", "bad-nodes"],
)
def test_malformed_response_fails(
    response: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def gh(args: list[str], stdin: str | None = None) -> str:
        return response

    monkeypatch.setenv("GH_REPO", "o/r")
    assert digest.main(["--cutoff", _CUTOFF], gh=gh, classify_pr=_DEFAULT) == 1
    assert "docs_postreview_digest:" in capsys.readouterr().err


def test_pull_request_without_merge_commit_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pr = _pr(3, "2026-11-12T08:00:00Z")
    pr["mergeCommit"] = None
    assert _main(FakeGh(prs=[[pr]]), monkeypatch) == 1
    assert "docs_postreview_digest:" in capsys.readouterr().err


def test_runaway_paging_stops_at_the_page_bound(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("docs_postreview_digest.MAX_PAGES", 2)
    gh = FakeGh(prs=[[], [], []])
    assert _main(gh, monkeypatch) == 1
    assert "more than 2 pages" in capsys.readouterr().err
