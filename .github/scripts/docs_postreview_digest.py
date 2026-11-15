#!/usr/bin/env python3
"""Open the weekly "Docs post-review" digest issue.

Usage: docs_postreview_digest.py [--cutoff <RFC 3339 UTC>] [--dry-run]

Lists the ``docs-postreview`` pull requests merged in the window
(previous digest cut-off, cut-off] with their doc files and matched rules, and
opens one issue with two checkboxes per pull request. See
``docs/internals/agents/docs-postreview-digest.md``. Reads ``GH_REPO``
(``owner/name``) or asks ``gh``. Run from a full-history checkout, with
``PYTHONPATH=src`` so ``booley.runtime.timefmt`` imports. Exit 1 names a
failure; a failed run opens no issue. ``--dry-run`` prints and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from doc_change_class import ClassifierError, classify, load_rules

from booley.core.boundary import require_dict, require_int, require_list, require_str
from booley.runtime.timefmt import format_human_date, parse_timestamp, rfc3339_from_datetime

LABEL = "docs-postreview-digest"
MAX_PAGES = 100  # 100 nodes per page: far above any real count, so a loop cannot run away
_GH_TIMEOUT_SECONDS = 120
_CUTOFF = re.compile(r"<!-- docs-postreview-digest cutoff=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) -->")
_ENTRY = re.compile(r"<!-- docs-postreview-digest-entry pr=(\d+) kinds=(\S*) -->")
_REVIEWED = re.compile(r"^- \[([ xX])\] reviewed$", re.MULTILINE)
_NEEDED = re.compile(r"^- \[([ xX])\] needed edits$", re.MULTILINE)

_PR_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: MERGED, labels: ["docs-postreview"], baseRefName: "main", first: 100,
        after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes { number title url mergedAt mergeCommit { oid } }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
_ISSUE_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(labels: ["docs-postreview-digest"], states: [OPEN, CLOSED], first: 100, after: $cursor) {
      nodes { number state body }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

Gh = Callable[[list[str], str | None], str]
ClassifyPr = Callable[[str], dict[str, object]]


class DigestError(Exception):
    """A read, parse, or write failed; no digest was opened."""


@dataclass(frozen=True)
class Entry:
    pr: int
    kinds: tuple[str, ...]
    reviewed: bool
    needed: bool


@dataclass(frozen=True)
class Digest:
    number: int
    closed: bool
    cutoff: datetime
    entries: tuple[Entry, ...]


@dataclass(frozen=True)
class Pr:
    number: int
    title: str
    url: str
    merged_at: datetime
    sha: str


def run_gh(args: list[str], stdin: str | None = None) -> str:
    try:
        result = subprocess.run(
            [shutil.which("gh") or "gh", *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DigestError(f"gh {args[0]} failed: {error}") from error
    if result.returncode != 0:
        raise DigestError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout


def _nodes(gh: Gh, repo: str, query: str, field: str) -> list[dict[str, Any]]:
    """Read every node of a GraphQL connection, following its item cursors."""
    owner, _, name = repo.partition("/")
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        args = ["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}"]
        args += ["-f", f"name={name}"] + (["-f", f"cursor={cursor}"] if cursor else [])
        try:
            data = require_dict(json.loads(gh(args, None)), field="response")
            repository = require_dict(
                require_dict(data.get("data"), field="data").get("repository")
            )
            connection = require_dict(repository.get(field), field=field)
            found += [
                require_dict(n, field=f"{field} node")
                for n in require_list(connection.get("nodes"))
            ]
            page = require_dict(connection.get("pageInfo"), field="pageInfo")
            more, cursor = page.get("hasNextPage") is True, page.get("endCursor")
            if more and not isinstance(cursor, str):
                raise ValueError("pageInfo.endCursor must be a string while hasNextPage")
        except ValueError as error:
            raise DigestError(f"unreadable GitHub {field} response: {error}") from error
        if not more:
            return found
    raise DigestError(f"GitHub {field} has more than {MAX_PAGES} pages")


def parse_digest(node: dict[str, Any]) -> Digest:
    """Read an earlier digest issue; raise DigestError when it cannot be read."""
    number = require_int(node.get("number"), field="issue number")
    body = str(node.get("body") or "")

    def bad(message: str) -> DigestError:
        return DigestError(f"issue #{number}: {message}")

    cutoff = _CUTOFF.search(body)
    if cutoff is None:
        raise bad("no digest cut-off marker")
    markers = list(_ENTRY.finditer(body))
    if len(markers) != body.count("<!-- docs-postreview-digest-entry"):
        raise bad("an entry marker is malformed")
    if len(_REVIEWED.findall(body)) != len(markers) or len(_NEEDED.findall(body)) != len(markers):
        raise bad("a checkbox is not under an entry marker")
    entries: list[Entry] = []
    for i, marker in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(body)
        segment = body[marker.end() : end]
        reviewed, needed = _REVIEWED.findall(segment), _NEEDED.findall(segment)
        if len(reviewed) != 1 or len(needed) != 1:
            raise bad(f"entry for #{marker.group(1)} needs one reviewed and one needed edits box")
        kinds = tuple(k for k in marker.group(2).split(",") if k)
        entries.append(Entry(int(marker.group(1)), kinds, reviewed[0] != " ", needed[0] != " "))
    return Digest(
        number, node.get("state") == "CLOSED", parse_timestamp(cutoff.group(1)), tuple(entries)
    )


def edit_rates(digests: list[Digest]) -> dict[str, tuple[int, int]]:
    """Per kind, (reviewed pull requests, those that needed edits) from closed digests."""
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for digest in digests:
        for entry in digest.entries if digest.closed else ():
            for kind in entry.kinds if entry.reviewed else ():
                counts[kind][0] += 1
                counts[kind][1] += entry.needed
    return {kind: (reviewed, needed) for kind, (reviewed, needed) in sorted(counts.items())}


def _pr(node: dict[str, Any]) -> Pr:
    commit = require_dict(node.get("mergeCommit"), field="mergeCommit")
    return Pr(
        require_int(node.get("number"), field="number"),
        str(node.get("title") or ""),
        require_str(node, "url"),
        parse_timestamp(require_str(node, "mergedAt")),
        require_str(commit, "oid"),
    )


def merged_prs(gh: Gh, repo: str, previous: datetime | None, cutoff: datetime) -> list[Pr]:
    """The labeled merged PRs with previous < mergedAt <= cutoff, oldest first."""
    try:
        prs = [_pr(node) for node in _nodes(gh, repo, _PR_QUERY, "pullRequests")]
    except ValueError as error:
        raise DigestError(f"unreadable pull request: {error}") from error
    kept = [
        p for p in prs if (previous is None or previous < p.merged_at) and p.merged_at <= cutoff
    ]
    return sorted(kept, key=lambda p: (p.merged_at, p.number))


def _files(result: dict[str, object]) -> list[tuple[str, str, str]]:
    try:
        rows = [require_dict(f) for f in require_list(result.get("files"), field="files")]
        return [
            (require_str(f, "path"), require_str(f, "class"), require_str(f, "rule")) for f in rows
        ]
    except ValueError as error:
        raise DigestError(f"unreadable classifier result: {error}") from error


def _text(value: str) -> str:
    """One line with no comment opener, so it cannot forge a digest marker."""
    return " ".join(value.split()).replace("<", "&lt;")


def _entry(pr: Pr, files: list[tuple[str, str, str]]) -> str:
    kinds = sorted({rule for _, cls, rule in files if cls != "none"})
    lines = [
        f"### #{pr.number} {_text(pr.title)}",
        f"<!-- docs-postreview-digest-entry pr={pr.number} kinds={','.join(kinds)} -->",
        pr.url,
        "",
        *(f"- `{_text(path)}`: `{rule}`" for path, _, rule in sorted(files)),
        "",
        "- [ ] reviewed",
        "- [ ] needed edits",
    ]
    return "\n".join(lines) + "\n"


def _rate_section(rates: dict[str, tuple[int, int]]) -> str:
    if not rates:
        return "No closed digest has a reviewed pull request yet.\n"
    rows = ["| Kind | Reviewed PRs | Needed edits | Share |", "| --- | --- | --- | --- |"]
    for kind, (reviewed, needed) in rates.items():
        share = (200 * needed + reviewed) // (2 * reviewed)  # round half up
        rows.append(f"| `{kind}` | {reviewed} | {needed} | {share}% |")
    return "\n".join(rows) + "\n"


def _day(value: datetime) -> str:
    return format_human_date(value.astimezone(UTC).date())


def render(
    entries: list[tuple[Pr, list[tuple[str, str, str]]]],
    previous: datetime | None,
    cutoff: datetime,
    rates: dict[str, tuple[int, int]],
) -> tuple[str, str]:
    """Return the digest issue's title and body."""
    start = previous or entries[0][0].merged_at
    since = rfc3339_from_datetime(previous) if previous else "the first labeled merge"
    cut = rfc3339_from_datetime(cutoff)
    body = [
        f"<!-- docs-postreview-digest cutoff={cut} -->",
        "Docs of the pull requests below merged without the maintainer's pre-merge "
        f"sign-off between {since} and {cut}. How to work through them: "
        "docs/internals/agents/docs-postreview-digest.md",
        "",
        "## Edit rate from closed digests",
        "",
        _rate_section(rates),
        "## Pull requests",
        "",
        "\n".join(_entry(pr, files) for pr, files in entries),
    ]
    return f"Docs post-review: {_day(start)} to {_day(cutoff)}", "\n".join(body)


def _repo() -> str:
    return (
        os.environ.get("GH_REPO")
        or run_gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]).strip()
    )


def run(gh: Gh, classify_pr: ClassifyPr, repo: str, cutoff: datetime, *, dry_run: bool) -> str:
    """Read everything, then write once; return the text to print."""
    digests = [parse_digest(n) for n in _nodes(gh, repo, _ISSUE_QUERY, "issues")]
    previous = max((d.cutoff for d in digests), default=None)
    prs = merged_prs(gh, repo, previous, cutoff)
    shown = rfc3339_from_datetime(cutoff)
    if not prs:
        after = f"after {rfc3339_from_datetime(previous)} " if previous else ""
        return f"No docs-postreview pull request merged {after}up to {shown}; no digest opened."
    entries: list[tuple[Pr, list[tuple[str, str, str]]]] = []
    for pr in prs:
        try:
            entries.append((pr, _files(classify_pr(pr.sha))))
        except ClassifierError as error:
            raise DigestError(f"pull request #{pr.number}: {error}") from error
    title, body = render(entries, previous, cutoff, edit_rates(digests))
    if not dry_run:
        gh(
            [
                "label",
                "create",
                LABEL,
                "--force",
                "--repo",
                repo,
                "--color",
                "0e8a16",
                "--description",
                "Weekly Docs post-review digest",
            ],
            None,
        )
        payload = json.dumps({"title": title, "body": body, "labels": [LABEL]})
        gh(["api", "--method", "POST", f"repos/{repo}/issues", "--input", "-"], payload)
    return f"{title}\n\n{body}"


def main(
    argv: list[str] | None = None, *, gh: Gh = run_gh, classify_pr: ClassifyPr | None = None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", help="window end, RFC 3339 UTC (default: now)")
    parser.add_argument("--dry-run", action="store_true", help="print the digest, write nothing")
    args = parser.parse_args(argv)
    try:
        try:
            cutoff = parse_timestamp(args.cutoff) if args.cutoff else datetime.now(UTC)
        except ValueError as error:
            raise DigestError(f"--cutoff is not an RFC 3339 timestamp: {error}") from error
        rules = Path(__file__).with_name("doc_change_rules.toml")
        classifier = classify_pr or (lambda sha: classify(load_rules(rules), f"{sha}^1", sha))
        print(run(gh, classifier, _repo(), cutoff, dry_run=args.dry_run))
    except (DigestError, ClassifierError) as error:
        print(f"docs_postreview_digest: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
