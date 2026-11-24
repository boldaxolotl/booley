"""``booley review --verify`` and ``--status``: re-check findings, ask if a commit is Reviewed (#3273).

Every test drives the real entry point against a temporary Git repository and the real
Reviewer, whose model is the shared ``ScriptedAgent``, never a live model.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FeatureFlagHelper
from tests.mcp_tools.reviewer_support import ScriptedAgent
from tests.review.test_review_command import (
    A_V2,
    BUGS,
    SPEC,
    _environment,  # noqa: F401 - the autouse fixture
    agent,  # noqa: F401 - the fixture
    finding,
    project_on_main,
    review,
    stored_runs,
)
from tests.runtime.test_commit_export import commit_all, git, write

SPECIFIED = BUGS + 'spec = "docs/spec.md"\n'
RESET, ENABLE, NAMING, PRE = "Reset value", "Enable polarity", "Naming nit", "Old problem"
DISCOVERED = [
    finding(summary=RESET, line=2),
    finding(summary=ENABLE, line=3),
    finding(summary=NAMING, line=4),
    finding("pre_existing", summary=PRE, line=1),
]
VERDICT_FILE = {"file": "rtl/a.sv", "line": 1, "reason": "the line now settles it"}
FIX_1 = "module a(input logic x);\n  // fix 1\n  assign y = x;\nendmodule\n"
FIX_2 = "module a(input logic x);\n  // fix 2\n  assign y = x;\nendmodule\n"


def verdict(finding_id: str, status: str, ruling: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"finding_id": finding_id, "verdict": status}
    if status == "fixed":
        entry["evidence"] = VERDICT_FILE
    if ruling is not None:
        entry["waiver_ruling"] = ruling
    return entry


def discovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flags: FeatureFlagHelper,
    agent: ScriptedAgent,  # noqa: F811
    answer: Any,
    reviews: str = BUGS.format(resolve="major"),
) -> tuple[Path, str, dict[str, Any]]:
    """A Project with one discovery run of its commit ``A``: ``(root, A, run)``."""
    root = project_on_main(tmp_path, flags, reviews)
    write(root / "rtl" / "a.sv", A_V2)
    first = commit_all(root, "Edit a")
    agent.answer(answer)
    review(monkeypatch, root)
    runs = stored_runs(root, first)
    return root, first, runs[0] if runs else {}


def ids_of(run: dict[str, Any]) -> dict[str, str]:
    """Each introduced finding's ID, by summary."""
    (rtl,) = run["reviews"]
    return {row["summary"]: row["finding_id"] for row in rtl["introduced"]}


def assert_waivers(
    waivers: list[dict[str, Any]],
    ids: dict[str, str],
    accepted: dict[str, bool | None],
    *,
    errored: bool,
) -> None:
    """Every proposal is recorded with its justification, the Reviewer's decision and ruling."""
    by_id = {w["finding_id"]: w for w in waivers}
    assert set(by_id) == {ids[summary] for summary in accepted}
    for summary, expected in accepted.items():
        assert by_id[ids[summary]]["accepted"] is expected
    assert by_id[ids[ENABLE]]["justification"] == "the output follows the input"
    assert by_id[ids[ENABLE]]["ruling"] == (None if errored else "the reason holds")


def commit_source(root: Path, text: str, message: str) -> str:
    """Commit only ``rtl/a.sv``: the stored Review Runs stay out of the branch."""
    write(root / "rtl" / "a.sv", text)
    git(root, "add", "rtl/a.sv")
    return commit_all(root, message, staged_only=True)


def stored_markdown(root: Path, commit: str, run: dict[str, Any]) -> str:
    """The Markdown report stored beside the JSON of *run*."""
    path = root / ".booley" / "reviews" / commit / f"{run['run_id']}.md"
    return path.read_text(encoding="utf-8")


def assert_sections(
    shown: str, scenario: str, ids: dict[str, str], fixed_line_shown: bool
) -> None:
    """The report prints Waivers, Needs you and Fixed in that order, each when it has lines."""
    expected = {
        "leaves-one-open": ["Waivers", "Needs you", "Fixed"],
        "clears-all": ["Waivers", "Fixed"],
        "reviewer-error": ["Waivers", "Needs you"],
    }[scenario]
    titles = [title for title in ("Waivers", "Needs you", "Fixed") if title in shown]
    assert titles == expected
    assert [shown.index(title) for title in titles] == sorted(shown.index(t) for t in titles)
    assert fixed_line_shown == (scenario != "reviewer-error")
    assert (f"blocking: {NAMING}" in shown) == (scenario != "clears-all")
    assert (f"blocking: {ENABLE}" in shown) == (scenario == "reviewer-error")
    assert (f"blocking: {RESET}" in shown) == (scenario == "reviewer-error")


@pytest.mark.parametrize(
    ("scenario", "exit_code", "statuses", "accepted", "status_exit"),
    [
        (
            "leaves-one-open",
            1,
            {RESET: "fixed", ENABLE: "waived", NAMING: "still_open"},
            {ENABLE: True, NAMING: False},
            1,
        ),
        (
            "clears-all",
            0,
            {RESET: "fixed", ENABLE: "waived", NAMING: "fixed"},
            {ENABLE: True},
            0,
        ),
        (
            "reviewer-error",
            2,
            {RESET: "current", ENABLE: "current", NAMING: "current"},
            {ENABLE: None, NAMING: None},
            1,
        ),
    ],
    ids=["leaves-one-open", "clears-all", "reviewer-error"],
)
def test_verify_rechecks_open_findings_and_links_its_discovery_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    agent: ScriptedAgent,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    scenario: str,
    exit_code: int,
    statuses: dict[str, str],
    accepted: dict[str, bool | None],
    status_exit: int,
) -> None:
    """AC1, AC3, AC4, AC5, AC6: each prior finding is fixed, waived or open; none is added."""
    root, first, run = discovered(
        tmp_path, monkeypatch, feature_flags, agent, {"issues": DISCOVERED}
    )
    ids = ids_of(run)
    write(root / "rtl" / "a.sv", FIX_1)
    first_fix = commit_all(root, "Fix 1")
    write(root / "rtl" / "a.sv", FIX_2)
    second_fix = commit_all(root, "Fix 2")
    write(root / "rtl" / "a.sv", "module a; // UNCOMMITTED\nendmodule\n")
    seen: list[str] = []
    agent.on_call = lambda params: seen.append(
        (Path(params.cwd) / "rtl" / "a.sv").read_text("utf-8")
    )
    answer: Any = {
        "verdicts": [
            verdict(ids[RESET], "fixed"),
            verdict(ids[ENABLE], "waived", "the reason holds"),
            verdict(ids[NAMING], "fixed" if scenario == "clears-all" else "still_open", "weak"),
            verdict("not-a-finding", "fixed"),
        ]
    }
    agent.answer([RuntimeError("down") if scenario == "reviewer-error" else answer])
    calls = len(agent.calls)
    capsys.readouterr()
    waive = ["--waive", ids[ENABLE], "the output follows the input"]
    if scenario != "clears-all":
        waive += ["--waive", ids[NAMING], "cosmetic"]

    assert review(monkeypatch, root, "--verify", *waive) == exit_code

    assert len(agent.calls) == calls + 1
    prompt = agent.calls[-1].prompt
    assert all(ids[summary] in prompt for summary in (RESET, ENABLE, NAMING))
    assert PRE not in prompt
    assert seen[-1] == FIX_2
    (verify,) = stored_runs(root, second_fix)
    assert verify["kind"] == "verify"
    assert verify["discovery"] == {"commit": first, "run_id": run["run_id"]}
    assert verify["commits"] == [first_fix, second_fix]
    assert verify["outcome"] == {0: "pass", 1: "open", 2: "error"}[exit_code]
    (rtl,) = verify["reviews"]
    assert {row["finding_id"] for row in rtl["introduced"]} == set(ids.values())
    assert {row["summary"]: row["status"] for row in rtl["introduced"]} == statuses
    assert_waivers(rtl["waivers"], ids, accepted, errored=exit_code == 2)
    out = capsys.readouterr()
    fixed = f"`{ids[RESET]}` rtl/a.sv:2 MAJOR fixed in `{first_fix[:12]}`, `{second_fix[:12]}`"
    assert_sections(out.out, scenario, ids, fixed in out.out)
    assert ("re-check failed" in out.err) == (exit_code == 2)
    assert "## Waivers" in stored_markdown(root, second_fix, verify)
    assert len(stored_runs(root, first)) == 1
    assert review(monkeypatch, root, "--status", second_fix) == status_exit
    assert ("not Reviewed" in capsys.readouterr().out) == bool(status_exit)


@pytest.mark.parametrize(
    ("scenario", "fragment"),
    [
        ("no-discovery", "no discovery Review Run"),
        ("rewritten", "no discovery Review Run"),
        ("nothing-open", "has no open finding to verify"),
        ("errored-discovery", "failed; run a new discovery"),
        ("declared-changed", "the Declared Reviews changed"),
        ("spec-changed", "the Declared Reviews changed"),
        ("file-added", "the Declared Reviews changed"),
        ("spec-outside", "is not a file of the Project"),
        ("unknown-waiver", "--waive nope: not an open finding"),
        ("blank-justification", "the justification is empty"),
        ("duplicate-waiver", "proposed more than once"),
        ("waive-without-verify", "--waive needs --verify"),
        ("target-with-verify", "--target cannot be used with --verify"),
        ("base-with-status", "--base cannot be used with --status"),
        ("json-with-status", "--json cannot be used with --status"),
    ],
    ids=[
        "no-discovery",
        "rewritten",
        "nothing-open",
        "errored-discovery",
        "declared-changed",
        "spec-changed",
        "file-added",
        "spec-outside",
        "unknown-waiver",
        "blank-justification",
        "duplicate-waiver",
        "waive-without-verify",
        "target-with-verify",
        "base-with-status",
        "json-with-status",
    ],
)
def test_verify_refusals_exit_two_and_store_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    agent: ScriptedAgent,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    scenario: str,
    fragment: str,
) -> None:
    """AC2, AC3, AC6: a refusal exits 2, names its reason, stores nothing and calls no model."""
    reviews = SPECIFIED.format(resolve="major")
    answer: Any = {"issues": [] if scenario == "nothing-open" else DISCOVERED}
    if scenario == "errored-discovery":
        answer = RuntimeError("model unavailable")
    root, _first, run = discovered(tmp_path, monkeypatch, feature_flags, agent, answer, reviews)
    if scenario == "no-discovery":
        root = project_on_main(tmp_path / "other", feature_flags, reviews)
    ids = ids_of(run) if run and run["reviews"][0]["introduced"] else {RESET: "x"}
    first_id = ids.get(RESET, "x")
    write(root / "rtl" / "a.sv", FIX_1)
    changes = {
        "declared-changed": (root / ".booley" / "reviews.toml", reviews.replace("major", "minor")),
        "spec-changed": (root / "docs" / "spec.md", SPEC + "SPEC-TEXT-V2\n"),
        "file-added": (root / "rtl" / "d.sv", "module d;\nendmodule\n"),
    }
    if scenario in changes:
        write(*changes[scenario])
    if scenario == "file-added":
        core = (root / "sim.core").read_text("utf-8")
        write(root / "sim.core", core.replace("rtl/gen/c.sv]", "rtl/gen/c.sv, rtl/d.sv]"))
    if scenario == "spec-outside":
        if sys.platform == "win32":
            pytest.skip("needs symlinks")
        outside = tmp_path / "outside.md"
        outside.write_text(SPEC, encoding="utf-8")
        (root / "docs" / "spec.md").unlink()
        (root / "docs" / "spec.md").symlink_to(outside)
    if scenario == "rewritten":
        git(root, "commit", "-q", "--amend", "-m", "Rewritten")  # the discovery commit
    commit_all(root, "Fix")
    calls = len(agent.calls)
    arguments = {
        "unknown-waiver": ("--verify", "--waive", "nope", "reason"),
        "blank-justification": ("--verify", "--waive", first_id, "   "),
        "duplicate-waiver": ("--verify", "--waive", first_id, "a", "--waive", first_id, "b"),
        "waive-without-verify": ("--waive", first_id, "reason"),
        "target-with-verify": ("--verify", "--target", "sim_core"),
        "base-with-status": ("--status", "--base", "main"),
        "json-with-status": ("--status", "--json"),
    }.get(scenario, ("--verify",))
    capsys.readouterr()

    assert review(monkeypatch, root, *arguments) == 2

    assert fragment in capsys.readouterr().err
    assert len(agent.calls) == calls
    assert len(stored_runs(root)) == (0 if scenario == "no-discovery" else 1)


def test_status_follows_the_stored_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    agent: ScriptedAgent,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC5, AC6: Reviewed means a passing latest verify or a discovery that left nothing open."""
    cases: dict[str, tuple[Any, int, str]] = {
        "clean-discovery": ({"issues": []}, 0, "Reviewed (discovery Review Run"),
        "below-resolve": ({"issues": [finding(severity="MINOR")]}, 0, "Reviewed (discovery"),
        "open-discovery": ({"issues": DISCOVERED}, 1, "no Review Run leaves nothing open"),
        "errored-discovery": (RuntimeError("down"), 1, "not Reviewed"),
    }
    for index, (name, (answer, expected, line)) in enumerate(cases.items()):
        root, commit, _ = discovered(
            tmp_path / name,
            monkeypatch,
            feature_flags,
            agent,
            answer,
            BUGS.format(resolve="major"),
        )
        capsys.readouterr()
        assert review(monkeypatch, root, "--status", commit) == expected, name
        assert line in capsys.readouterr().out, index
        write(root / "rtl" / "a.sv", FIX_1)
        child = commit_all(root, "Fix")
        assert review(monkeypatch, root, "--status") == 1
        assert f"{child[:12]}: not Reviewed (no Review Run)" in capsys.readouterr().out
    assert review(monkeypatch, root, "--status", "no-such-revision") == 2
    assert "is not a commit of this Project" in capsys.readouterr().err


def test_the_latest_verify_run_decides_and_a_bad_run_is_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    agent: ScriptedAgent,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC4, AC5: a waive-only verify on the discovery commit clears it; a later one reopens."""
    root, first, run = discovered(
        tmp_path, monkeypatch, feature_flags, agent, {"issues": [finding(summary=RESET)]}
    )
    (finding_id,) = ids_of(run).values()
    cleared = {"verdicts": [verdict(finding_id, "fixed")]}
    reopened = {"verdicts": [verdict(finding_id, "still_open")]}
    agent.answer([cleared])
    capsys.readouterr()

    assert review(monkeypatch, root, "--verify", first, "--json") == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["commits"] == []
    assert printed["discovery"]["run_id"] == run["run_id"]
    assert review(monkeypatch, root, "--status", first) == 0
    agent.answer([reopened])
    assert review(monkeypatch, root, "--verify", first) == 1
    assert "latest verify Review Run" not in capsys.readouterr().out
    assert review(monkeypatch, root, "--status", first) == 1
    assert "latest verify Review Run" in capsys.readouterr().out
    assert [item["kind"] for item in stored_runs(root, first)] == ["discovery", "verify", "verify"]

    (root / ".booley" / "reviews" / first / "0-broken.json").write_text("{", encoding="utf-8")

    assert review(monkeypatch, root, "--status", first) == 2
    assert "0-broken.json cannot be read" in capsys.readouterr().err


def test_the_report_chains_earlier_verify_runs_of_ancestors_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature_flags: FeatureFlagHelper,
    agent: ScriptedAgent,  # noqa: F811
) -> None:
    """AC4: the report of a fix lists the waivers of its ancestors' verify runs, not a sibling's."""
    root, _first, run = discovered(
        tmp_path, monkeypatch, feature_flags, agent, {"issues": [finding(summary=RESET, line=2)]}
    )
    finding_id = ids_of(run)[RESET]
    refused: Any = {"verdicts": [verdict(finding_id, "still_open", "weak")]}
    agent.answer([refused])
    assert review(monkeypatch, root, "--verify", "--waive", finding_id, "first reason") == 1
    git(root, "checkout", "-q", "-b", "side")
    sibling = commit_source(root, FIX_1, "Side work")
    agent.answer([refused])
    assert review(monkeypatch, root, "--verify", "--waive", finding_id, "sibling reason") == 1
    git(root, "checkout", "-q", "feature")
    fix = commit_source(root, FIX_2, "Fix on the feature branch")
    agent.answer([{"verdicts": [verdict(finding_id, "fixed")]}])

    assert review(monkeypatch, root, "--verify") == 0

    (stored,) = stored_runs(root, fix)
    report = stored_markdown(root, fix, stored)
    assert "first reason" in report.split("## Waivers")[1]
    assert "sibling reason" not in report
    assert f"fixed in `{fix[:12]}`:" in report
    assert sibling[:12] not in report
