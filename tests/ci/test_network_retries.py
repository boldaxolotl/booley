"""CI commands that download from the network retry a transient failure (#4420)."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.subprocess_budget import run_bounded_child

_GITHUB = Path(__file__).parents[2] / ".github"
_RETRY = _GITHUB / "scripts" / "retry.sh"
_PULL = re.compile(r"\bdocker pull\b.*?\"\$\{(\w+)\}\"")
_RUN = re.compile(r"\bdocker run\b.*?\"\$\{(\w+)\}\"")


def _commands(text: str) -> list[str]:
    """Join backslash continuations and drop comment lines."""
    joined = re.sub(r"\\\n\s*", " ", text)
    return [
        line for line in joined.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


def _units() -> Iterator[tuple[str, dict[str, Any], str, list[tuple[str, str]]]]:
    """Yield (name, env, kind, [(step key, run text)]) for each workflow job, action, script."""
    for path in sorted((_GITHUB / "workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job, body in workflow["jobs"].items():
            runs = [
                (f"{job}/{s.get('name', '')}", s["run"])
                for s in body.get("steps") or []
                if "run" in s
            ]
            yield f"{path.name}:{job}", workflow.get("env") or {}, "workflow", runs
    for path in sorted((_GITHUB / "actions").glob("*/action.yml")):
        action = yaml.safe_load(path.read_text(encoding="utf-8"))
        runs = [(s.get("name", ""), s["run"]) for s in action["runs"]["steps"] if "run" in s]
        yield path.parent.name, {}, "action", runs
    for path in sorted((_GITHUB / "scripts").glob("*.sh")):
        yield path.name, {}, "script", [(path.name, path.read_text(encoding="utf-8"))]


def _line_issues(line: str, kind: str) -> list[str]:
    """Name each retry the command line lacks."""
    issues: list[str] = []
    if re.search(r"\bdocker pull\b|\bgit fetch\b", line) and "retry.sh" not in line:
        issues.append("lacks retry.sh")
    if "nangate_pdk" in line and "retry.sh" not in line:
        issues.append("lacks retry.sh for the PDK download")
    if re.search(r"\bcurl ", line) and not ("--retry 8" in line and "--retry-delay 30" in line):
        issues.append("lacks curl retries")
    if re.search(r"\bapt-get (update|install)", line) and "Acquire::Retries=" not in line:
        issues.append("lacks Acquire::Retries")
    if "pip install" in line and kind == "action" and "--retries" not in line:
        issues.append("lacks --retries")
    return issues


def test_workflow_network_commands_retry() -> None:
    """Each download in a workflow, action, or script retries; each pull precedes its run."""
    problems: list[str] = []
    for name, env, kind, runs in _units():
        job_pulls = {m[1] for _, text in runs for m in _PULL.finditer("\n".join(_commands(text)))}
        if name.startswith("renovate.yml:"):
            job_pulls.add("IMAGE")
        earlier = ""
        for step, text in runs:
            for line in _commands(text):
                where = f"{name} [{step}]: {line.strip()[:70]}"
                problems.extend(f"{where} {issue}" for issue in _line_issues(line, kind))
            joined_step = "\n".join(_commands(text))
            for match in _RUN.finditer(joined_step):
                before = earlier + joined_step[: match.start()]
                if match[1] in job_pulls and not re.search(
                    rf'docker pull\b.*"\$\{{{match[1]}\}}"', before
                ):
                    problems.append(f"{name} [{step}]: docker run of {match[1]} before its pull")
            earlier += joined_step + "\n"
        joined = "\n".join(text for _, text in runs)
        if kind == "workflow" and "pip install" in joined and "PIP_RETRIES" not in env:
            problems.append(f"{name}: pip without PIP_RETRIES")
        if kind == "workflow" and re.search(r"\bcargo ", joined) and "CARGO_NET_RETRY" not in env:
            problems.append(f"{name}: cargo without CARGO_NET_RETRY")
    smoke = (_GITHUB / "scripts" / "isolated_host_install_smoke.py").read_text(encoding="utf-8")
    if "retry.sh" not in smoke or "Acquire::Retries" not in smoke:
        problems.append("isolated_host_install_smoke.py lacks pull or apt retries")
    assert problems == []


@pytest.mark.skipif(sys.platform == "win32", reason="workflow steps run under Linux bash")
def test_retry_helper_bounds_attempts(tmp_path: Path) -> None:
    """The helper retries up to eight times, 30 s apart, and keeps the last exit status."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "sleep").write_bytes(b'#!/bin/sh\necho "$1" >> "$COUNT_DIR/sleeps"\n')
    (bin_dir / "flaky").write_bytes(
        b'#!/bin/sh\necho x >> "$COUNT_DIR/runs"\n'
        b'test "$(wc -l < "$COUNT_DIR/runs")" -gt "$FAILURES" || exit 7\n'
    )
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    for failures, runs, sleeps, status in ((2, 3, 2, 0), (99, 9, 8, 7)):
        count_dir = tmp_path / str(failures)
        count_dir.mkdir()
        result = run_bounded_child(
            ["bash", str(_RETRY), "flaky"],
            cwd=tmp_path,
            env={
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "COUNT_DIR": str(count_dir),
                "FAILURES": str(failures),
            },
            check=False,
        )

        assert result.returncode == status, result.stdout + result.stderr
        assert len((count_dir / "runs").read_text(encoding="utf-8").splitlines()) == runs
        assert (count_dir / "sleeps").read_text(encoding="utf-8").splitlines() == ["30"] * sleeps
        assert result.stderr.count("::warning::") == sleeps
