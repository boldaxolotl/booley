"""A fake of the ``gh api`` command that CI scripts read the GitHub REST API with."""

from __future__ import annotations

import json
import subprocess
from typing import Any

_ROOT = "repos/{owner}/{repo}/actions/"
_ISSUES = "repos/{owner}/{repo}/issues"


class GhApiFake:
    """Answers run, artifact, job, download, and issue-listing requests from canned data."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.artifacts: dict[int, list[dict[str, Any]]] = {}
        self.jobs: dict[int, list[dict[str, Any]]] = {}
        self.zips: dict[int, bytes] = {}
        self.failures: dict[str, str] = {}
        self.totals: dict[str, int] = {}
        self.calls: list[str] = []

    def add_run(
        self,
        run_id: int,
        created_at: str,
        branch: str = "codex/x",
        artifacts: tuple[tuple[str, bytes, bool], ...] = (),
        jobs: tuple[dict[str, Any], ...] = (),
        conclusion: str = "success",
        attempt: int = 1,
    ) -> None:
        url = f"https://github.com/o/r/actions/runs/{run_id}"
        self.runs.append(
            {"id": run_id, "html_url": url, "created_at": created_at}
            | {"head_branch": branch, "conclusion": conclusion, "run_attempt": attempt}
        )
        self.jobs[run_id] = list(jobs)
        listed = self.artifacts.setdefault(run_id, [])
        for name, data, expired in artifacts:
            artifact_id = len(self.zips) + 1
            self.zips[artifact_id] = data
            listed.append({"id": artifact_id, "name": name, "expired": expired})

    def add_issue(
        self,
        number: int,
        title: str,
        created_at: str,
        state: str = "open",
        label: str = "flaky",
        pull_request: bool = False,
    ) -> None:
        issue = {"number": number, "title": title, "created_at": created_at}
        issue |= {"state": state, "labels": [{"name": label}]}
        self.issues.append(issue | ({"pull_request": {}} if pull_request else {}))

    def fail(self, path_end: str, stderr: str) -> None:
        self.failures[path_end] = stderr

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[bytes]:
        assert args[:3] == ["api", "--method", "GET"], args
        path = args[3]
        fields = dict(value.split("=", 1) for value in args[5::2])
        self.calls.append(path)
        for end, stderr in self.failures.items():
            if path.endswith(end):
                return subprocess.CompletedProcess(args, 1, b"", stderr.encode())
        if path == _ISSUES:
            listed = [
                issue
                for issue in self.issues
                if issue["state"] == fields["state"]
                and {"name": fields["labels"]} in issue["labels"]
            ]
            page, size = int(fields["page"]), int(fields["per_page"])
            data = json.dumps(listed[(page - 1) * size : page * size]).encode()
            return subprocess.CompletedProcess(args, 0, data, b"")
        parts = path.removeprefix(_ROOT).split("/")
        if parts[0] == "artifacts":
            return subprocess.CompletedProcess(args, 0, self.zips[int(parts[1])], b"")
        if parts[0] == "workflows":
            key, first, last = "workflow_runs", *fields["created"].split("..")
            items = [r for r in self.runs if first <= r["created_at"][:10] <= last]
        else:
            key = parts[2]
            items = (self.artifacts if key == "artifacts" else self.jobs)[int(parts[1])]
        page, size = int(fields["page"]), int(fields["per_page"])
        total = self.totals.get(key, len(items))
        body = {"total_count": total, key: items[(page - 1) * size : page * size]}
        return subprocess.CompletedProcess(args, 0, json.dumps(body).encode(), b"")
