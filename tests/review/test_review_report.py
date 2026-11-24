"""The Review Report: Markdown and terminal views of a Review Run chain (#5455).

The chains are built by hand with literal values, so each golden file shows exactly what a
reader of the report sees.
"""

from __future__ import annotations

from typing import Any

import pytest

from booley.presentation import colors
from tests.golden.conftest import assert_matches_golden

BASE = "b" * 40
DISCOVERED_AT, C1, C2, C3 = ("a" * 40, "1" * 40, "2" * 40, "3" * 40)
MAJOR_OPEN, MINOR_OPEN, CRITICAL_FIXED, WAIVED_OK, WAIVED_REFUSED, ADVISORY = (
    "f-major-open",
    "f-minor-open",
    "f-critical-fixed",
    "f-waived-ok",
    "f-waived-refused",
    "f-advisory",
)
SPEC_MINOR = "f-spec-minor"


def row(finding_id: str, severity: str, status: str = "current", line: int = 1) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "severity": severity,
        "status": status,
        "file": "rtl/a.sv",
        "line": line,
        "summary": f"{finding_id} summary",
    }


def review(
    focus: str, resolve: str, outcome: str, introduced: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    return {
        "role": "tb" if focus == "quality" else "rtl",
        "focus": focus,
        "spec": None,
        "resolve": resolve,
        "outcome": outcome,
        "introduced": introduced,
        "pre_existing": [],
        "waivers": [],
        "error": None,
        **extra,
    }


def run(kind: str, run_id: str, commit: str, reviews: list[dict[str, Any]], **extra: Any) -> Any:
    seen = {r["outcome"] for r in reviews}
    outcome = next((o for o in ("error", "open", "pass") if o in seen), "nothing-to-review")
    return {
        "kind": kind,
        "run_id": run_id,
        "commit": commit,
        "base": {"commit": BASE, "choice": "merge-base with local main", "requested": None},
        "message": "Edit a\n",
        "rules_changed": [],
        "reviews": reviews,
        "outcome": outcome,
        **extra,
    }


def discovered() -> dict[str, Any]:
    """Review A (resolve major) and B (resolve minor) find; review C is not needed."""
    found = review(
        "bugs",
        "major",
        "open",
        [
            row(MAJOR_OPEN, "MAJOR", line=2),
            row(MINOR_OPEN, "MINOR", line=3),
            row(CRITICAL_FIXED, "CRITICAL", line=4),
            row(WAIVED_OK, "MAJOR", line=5),
            row(WAIVED_REFUSED, "MAJOR", line=6),
            row(ADVISORY, "MAJOR", "advisory", line=7),
        ],
    )
    spec = review("spec", "minor", "open", [row(SPEC_MINOR, "MINOR", line=8)])
    spec["pre_existing"] = [row("f-old", "CRITICAL", line=9)]
    return run(
        "discovery",
        "20260101T000000-aaaaaa",
        DISCOVERED_AT,
        [found, spec, review("quality", "major", "not-needed", [])],
        message="Edit a\n\nFirst body line.\nSecond body line.\n",
    )


def verify(
    run_id: str,
    commit: str,
    commits: list[str],
    statuses: dict[str, str],
    outcome: str = "open",
    **extra: Any,
) -> dict[str, Any]:
    """A verify run: review A re-checked with *statuses* over the discovery rows, B untouched."""
    base = {found["finding_id"]: found for found in discovered()["reviews"][0]["introduced"]}
    rows = [{**base[key], "status": status} for key, status in statuses.items()]
    again = review("bugs", "major", outcome, rows, **extra)
    spec = review("spec", "minor", "open", [row(SPEC_MINOR, "MINOR", "still_open", line=8)])
    document = run("verify", run_id, commit, [again, spec])
    document.update({"discovery": {"run_id": "20260101T000000-aaaaaa"}, "commits": commits})
    return document


def verify_chain() -> list[dict[str, Any]]:
    """Discovery, fix of the CRITICAL at C1, an errored re-check at C2, waivers judged at C3."""
    still = {
        MAJOR_OPEN: "still_open",
        MINOR_OPEN: "still_open",
        CRITICAL_FIXED: "fixed",
        WAIVED_OK: "still_open",
        WAIVED_REFUSED: "still_open",
        ADVISORY: "advisory",
    }
    first = verify("20260102T000000-bbbbbb", C1, [C1], still)
    errored = verify(
        "20260103T000000-cccccc",
        C2,
        [C1, C2],
        {**still, CRITICAL_FIXED: "current"},
        outcome="error",
        error="model unavailable",
        waivers=[
            {
                "finding_id": WAIVED_OK,
                "justification": "the output follows the input",
                "accepted": None,
                "ruling": None,
            }
        ],
    )
    last = verify(
        "20260104T000000-dddddd",
        C3,
        [C1, C2, C3],
        {**still, WAIVED_OK: "waived"},
        waivers=[
            {
                "finding_id": WAIVED_OK,
                "justification": "the output follows the input",
                "accepted": True,
                "ruling": "the reason holds",
            },
            {
                "finding_id": WAIVED_REFUSED,
                "justification": "cosmetic",
                "accepted": False,
                "ruling": None,
            },
        ],
    )
    last["rules_changed"] = ["reviews.toml"]
    last["message"] = "Fix 3\n\nRun `make` *twice*.\n"
    return [discovered(), first, errored, last]


def discovery_chain() -> list[dict[str, Any]]:
    document = discovered()
    document["rules_changed"] = ["docs/spec.md"]
    return [document]


@pytest.mark.parametrize("case", ["discovery", "verify"])
def test_report_matches_its_golden_files(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1-AC4: sections in order; blocking exactly at or above resolve; rules changes shown."""
    from booley.review.review_report import markdown_report, terminal_report

    chain = {"discovery": discovery_chain, "verify": verify_chain}[case]()

    assert_matches_golden(f"review/{case}.markdown", markdown_report(chain))
    assert_matches_golden(f"review/{case}.txt", terminal_report(chain))

    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    painted = terminal_report(chain)
    blocking = (
        f"`{SPEC_MINOR}` rtl/a.sv:8 MINOR blocking: {SPEC_MINOR} summary (rtl spec, resolve minor)"
    )
    assert "  " + colors.red(blocking) in painted
    changed = "rules changed since the Base: " + ", ".join(chain[-1]["rules_changed"])
    assert "  " + colors.amber(changed) in painted
    fixed = (
        f"`{CRITICAL_FIXED}` rtl/a.sv:4 CRITICAL fixed in `{C1[:12]}`: {CRITICAL_FIXED} summary"
    )
    assert ("  " + colors.green(fixed) in painted) == (case == "verify")


@pytest.mark.parametrize(
    ("case", "state"),
    [
        ("passed", "Reviewed"),
        ("nothing", "Reviewed: nothing to review: no Declared Review has a changed file"),
        ("target", "Not Reviewed: not stored (--target)"),
        ("failed", "Not Reviewed: a review failed"),
        ("one-open", "Not Reviewed: 1 finding needs you"),
    ],
)
def test_the_state_line_tells_whether_anything_needs_the_user(case: str, state: str) -> None:
    """AC3: pass, nothing to review, a --target run and a failed review each read right."""
    from booley.review.review_report import markdown_report, terminal_report

    reviews = {
        "passed": [review("bugs", "major", "pass", [row(MINOR_OPEN, "MINOR")])],
        "nothing": [review("quality", "major", "not-needed", [])],
        "target": [review("bugs", "major", "pass", [])],
        "failed": [review("bugs", "major", "error", [], error="down")],
        "one-open": [review("bugs", "major", "open", [row(MAJOR_OPEN, "MAJOR")])],
    }[case]
    chain = [run("discovery", "20260101T000000-aaaaaa", DISCOVERED_AT, reviews)]

    shown = terminal_report(chain, stored=case != "target")

    assert shown.splitlines()[1].startswith(state)
    if case != "target":  # the Markdown file exists only for a stored run
        assert f"**{state}" in markdown_report(chain)
    assert "Needs you" in shown if case == "one-open" else "Needs you" not in shown
