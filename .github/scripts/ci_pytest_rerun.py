#!/usr/bin/env python3
"""Run pytest, then rerun its few failed tests once, serially, in a new process.

Usage: ``ci_pytest_rerun.py --junitxml J --basetemp B [--coverage-file F] pytest <args>``.
The wrapper owns ``--junitxml`` and ``--basetemp`` for both runs. The rerun
passes only when every failed test passes again; see ``.github/ci/README.md``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

#: A run with more failed tests than this is a likely deterministic break: no rerun.
MAX_RERUN_FAILURES = 5

_FAILURE_TAGS = {"failure": "flakyFailure", "error": "flakyError"}
_RERUN_TAGS = {"failure": "rerunFailure", "error": "rerunError"}
_Key = tuple[str, str]


class RerunRefusedError(Exception):
    """The first run's failure is not a few test results, so no rerun starts."""


def _failure_children(case: ElementTree.Element) -> list[ElementTree.Element]:
    return [child for child in case if child.tag in _FAILURE_TAGS]


def _node_id(classname: str, name: str, root: Path) -> str:
    parts = classname.split(".")
    for size in range(len(parts), 0, -1):
        module = "/".join(parts[:size]) + ".py"
        if (root / module).is_file():
            return "::".join([module, *parts[size:], name])
    raise RerunRefusedError(f"cannot map JUnit test {classname}::{name} to a file")


def failed_ids(
    exit_code: int, junit: Path, root: Path, *, coverage: bool = False
) -> dict[_Key, str]:
    """Map each failed JUnit test key to its node id, or refuse the rerun."""
    if exit_code != 1:
        raise RerunRefusedError(
            f"pytest exited {exit_code}, not with test failures (interrupted, internal "
            "error, usage or shard-selection error, or no tests)"
        )
    try:
        tree = ElementTree.parse(junit)
    except FileNotFoundError:
        raise RerunRefusedError(f"no JUnit report at {junit}") from None
    except ElementTree.ParseError as exc:
        raise RerunRefusedError(f"malformed JUnit report at {junit}: {exc}") from None
    failed = [case for case in tree.iter("testcase") if _failure_children(case)]
    broken = [case.get("name", "") for case in failed if not case.get("classname")]
    if broken:
        raise RerunRefusedError(f"collection failed: {', '.join(broken)}")
    keys = {(case.get("classname", ""), case.get("name", "")) for case in failed}
    if not keys:
        raise RerunRefusedError("pytest exited 1 but the JUnit report records no failed test")
    if len(keys) > MAX_RERUN_FAILURES:
        raise RerunRefusedError(
            f"{len(keys)} tests failed, more than {MAX_RERUN_FAILURES} "
            "(MAX_RERUN_FAILURES): likely a deterministic break"
        )
    if coverage and any(
        "crashed while running" in child.get("message", "")
        for case in failed
        for child in _failure_children(case)
    ):
        raise RerunRefusedError(
            "worker crashed: its coverage data is lost, so a rerun cannot restore the "
            "shard's coverage"
        )
    return {key: _node_id(*key, root) for key in sorted(keys)}


def rerun_arguments(first: list[str], ids: list[str]) -> list[str]:
    """Rebuild the pytest arguments for a serial run of *ids* without xdist or shard options."""
    rest = list(first)
    while rest and not rest[0].startswith("-"):
        rest.pop(0)
    kept: list[str] = []
    index = 0
    while index < len(rest):
        arg = rest[index]
        value = rest[index + 1 : index + 2]
        separate = arg in {"-n", "--dist"} or (arg.startswith("--ci-") and "=" not in arg)
        if separate or (arg == "-p" and value == ["ci_pytest_shard"]):
            index += 2
        elif arg.startswith(("--dist=", "--ci-")):
            index += 1
        else:
            kept.append(arg)
            index += 1
    return [*kept, "-n", "0", *ids]


def merge_rerun(first: Path, rerun: Path, keys: dict[_Key, str]) -> tuple[list[str], list[str]]:
    """Record the rerun in the first report; return (passed ids, failed-again ids)."""
    tree = ElementTree.parse(first)
    after: dict[_Key, list[ElementTree.Element]] = {}
    try:
        for case in ElementTree.parse(rerun).iter("testcase"):
            after.setdefault((case.get("classname", ""), case.get("name", "")), []).append(case)
    except (FileNotFoundError, ElementTree.ParseError):
        pass
    passed: list[str] = []
    again: list[str] = []
    for suite in tree.iter("testsuite"):
        for case in list(suite.iter("testcase")):
            key = (case.get("classname", ""), case.get("name", ""))
            if key not in keys or not _failure_children(case):
                continue
            reruns = after.get(key, [])
            if reruns and not any(c.tag in {*_FAILURE_TAGS, "skipped"} for r in reruns for c in r):
                for child in _failure_children(case):
                    counter = "errors" if child.tag == "error" else "failures"
                    suite.set(counter, str(int(suite.get(counter, 0)) - 1))
                    child.tag = _FAILURE_TAGS[child.tag]
            else:
                _append_rerun_results(case, reruns)
    for key, node_id in keys.items():
        again_here = _still_failing(tree, key)
        (again if again_here else passed).append(node_id)
    tree.write(first, encoding="utf-8", xml_declaration=True)
    return passed, again


def _still_failing(tree: ElementTree.ElementTree, key: _Key) -> bool:
    return any(
        _failure_children(case)
        for case in tree.iter("testcase")
        if (case.get("classname", ""), case.get("name", "")) == key
    )


def _append_rerun_results(case: ElementTree.Element, reruns: list[ElementTree.Element]) -> None:
    added = False
    for rerun in reruns:
        for child in rerun:
            if child.tag == "skipped":
                message = f"skipped on rerun: {child.get('message', '')}"
                ElementTree.SubElement(case, "rerunFailure", {"message": message})
            elif child.tag in _FAILURE_TAGS:
                copy = ElementTree.SubElement(case, _RERUN_TAGS[child.tag], dict(child.attrib))
                copy.text = child.text
            else:
                continue
            added = True
    if not added:
        ElementTree.SubElement(case, "rerunError", {"message": "no result in the rerun report"})


def _run(argv: list[str], env: dict[str, str] | None = None) -> int:
    """Run ``python -m <argv>``, streaming its output to the job log."""
    return subprocess.run([sys.executable, "-m", *argv], env=env, check=False).returncode


def _say(text: str) -> None:
    print(text, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    split = arguments.index("pytest")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junitxml", type=Path, required=True)
    parser.add_argument("--basetemp", type=Path, required=True)
    parser.add_argument("--coverage-file", type=Path)
    own = parser.parse_args(arguments[:split])
    first_args = arguments[split + 1 :]
    junit: Path = own.junitxml
    code = _run(["pytest", *first_args, "--junitxml", str(junit), "--basetemp", str(own.basetemp)])
    if code == 0:
        return 0
    try:
        ids = failed_ids(code, junit, Path.cwd(), coverage=own.coverage_file is not None)
    except RerunRefusedError as refusal:
        _say(f"Failed tests not rerun: {refusal}")
        return code
    _say(
        f"Rerunning {len(ids)} failed tests serially in a new pytest process: {', '.join(ids.values())}"
    )
    rerun_junit = junit.with_name(f"{junit.stem}-rerun{junit.suffix}")
    env = dict(os.environ)
    if own.coverage_file is not None:
        env["COVERAGE_FILE"] = f"{own.coverage_file}.rerun"
    rerun_args = rerun_arguments(first_args, list(ids.values()))
    base = f"{own.basetemp}-rerun"
    rerun_code = _run(
        ["pytest", *rerun_args, "--junitxml", str(rerun_junit), "--basetemp", base], env
    )
    if own.coverage_file is not None:
        combine = ["coverage", "combine", "--append", f"--data-file={own.coverage_file}"]
        if _run([*combine, env["COVERAGE_FILE"]]) != 0:
            _say("Coverage data of the rerun could not be combined; failing the job.")
            return code
    passed, again = merge_rerun(junit, rerun_junit, ids)
    for node_id in passed:
        _say(f"Passed on rerun: `{node_id}`")
    for node_id in again:
        _say(f"Failed again: `{node_id}`")
    return 0 if rerun_code == 0 and not again else rerun_code or 1


if __name__ == "__main__":
    raise SystemExit(main())
