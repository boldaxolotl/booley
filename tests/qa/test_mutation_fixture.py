"""Check the mutation QA fixture's hand-written oracle against the fixture text.

The oracle (`qa/shared/mutation/expected.json`) lists every Mutant site by hand.
These tests only read files: they keep the oracle and the fixture from drifting
apart, and never run the Mutation Flow or any simulator.
"""

import json
import math
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

MUTATION_ROOT = Path(__file__).resolve().parents[2] / "qa" / "shared" / "mutation"
PROJECT = MUTATION_ROOT / "project"
OPERATORS = (
    "relational",
    "arithmetic-logical",
    "condition",
    "constant",
    "bit-select",
    "deletion",
)
# Replacements the user doc names for each operator whose set is closed.
RELATIONAL_SWAPS = {"<=", "!="}
ARITHMETIC_SWAPS = {"-", "|", ">>"}
DELETION_PLACEHOLDERS = {"/* deleted */", "begin end"}
FILE_TAGS = {"tb", "vendored", "generated"}


def _oracle() -> dict[str, Any]:
    path = MUTATION_ROOT / "expected.json"
    assert path.is_file(), "qa/shared/mutation/expected.json is missing"
    oracle: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return oracle


def _candidates(oracle: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return (operator, candidate) pairs in oracle order."""
    return [
        (operator, candidate)
        for operator, body in oracle["operators"].items()
        for candidate in body["candidates"]
    ]


def _key(candidate: dict[str, Any]) -> str:
    """Name a Mutant as `file:line:site->replacement`, the oracle's identity."""
    return (
        f"{candidate['file']}:{candidate['line']}:{candidate['site']}->{candidate['replacement']}"
    )


def _line(file: str, number: int) -> str:
    return (PROJECT / file).read_text(encoding="utf-8").splitlines()[number - 1]


def test_oracle_candidates_anchor_to_fixture_text() -> None:
    oracle = _oracle()
    assert list(oracle["operators"]) == list(OPERATORS)
    for operator, body in oracle["operators"].items():
        assert body["count"] == len(body["candidates"]), operator
    pairs = _candidates(oracle)
    keys = [_key(candidate) for _, candidate in pairs]
    assert len(keys) == len(set(keys)), "a candidate is listed twice"
    for operator, candidate in pairs:
        key = _key(candidate)
        assert candidate["file"] in oracle["default_scope"], key
        site, replacement = candidate["site"], candidate["replacement"]
        # The report shows the minimal site: its text occurs once on its line.
        assert _line(candidate["file"], candidate["line"]).count(site) == 1, key
        assert candidate["rule"], key
        if operator == "relational":
            assert replacement in RELATIONAL_SWAPS, key
        elif operator == "arithmetic-logical":
            assert replacement in ARITHMETIC_SWAPS, key
        elif operator == "condition":
            assert replacement in {f"!({site})", "1'b1", "1'b0"}, key
        elif operator == "constant":
            assert site.isdigit() and replacement.isdigit(), key
        elif operator == "bit-select":
            assert not set("[]") & set(site + replacement), key
        else:
            assert site.endswith(";") and replacement in DELETION_PLACEHOLDERS, key
    # `--count auto` sizes from the scope's line count: keep it equal to the files.
    lines = 0
    for name in oracle["default_scope"]:
        data = (PROJECT / name).read_bytes()
        lines += data.count(b"\n") + int(bool(data) and not data.endswith(b"\n"))
    assert oracle["auto"]["scope_lines"] == lines


def test_oracle_outcomes_name_listed_candidates() -> None:
    oracle = _oracle()
    pairs = _candidates(oracle)
    keys = {_key(candidate) for _, candidate in pairs}
    outcomes = oracle["outcomes"]
    survivors, equivalent = set(outcomes["survivors"]), set(outcomes["equivalent"])
    hanging, does_not_build = set(outcomes["hanging"]), set(outcomes["does_not_build"])
    for group in (survivors, equivalent, hanging, does_not_build):
        assert group <= keys, sorted(group - keys)
    assert equivalent <= survivors
    assert not survivors & hanging
    assert not survivors & does_not_build
    assert not hanging & does_not_build
    # Per-file tallies and the exhaustive totals follow from the lists above.
    for name, tally in oracle["per_file"].items():
        in_file = {_key(c) for _, c in pairs if c["file"] == name}
        assert tally == {
            "candidates": len(in_file),
            "survivors": len(survivors & in_file),
            "does_not_build": len(does_not_build & in_file),
            "hanging": len(hanging & in_file),
        }, name
    total = len(keys)
    exhaustive = oracle["totals"]["exhaustive"]
    assert oracle["totals"]["candidates"] == total == exhaustive["command_count"]
    assert exhaustive["not_detected"] == len(survivors)
    assert exhaustive["does_not_build"] == len(does_not_build)
    assert exhaustive["detected_by_timeout"] == len(hanging)
    # Candidates that do not build are replaced; none is left to replace them here,
    # so the run scores the rest and the default threshold follows that number.
    scored = total - len(does_not_build)
    assert exhaustive["scored"] == scored
    assert exhaustive["shortfall"] == f"only {scored} Mutants could be scored ({total} requested)"
    assert exhaustive["detected"] == scored - len(survivors)
    threshold = math.ceil(0.8 * scored)  # default: 80 % of the Mutants scored, rounded up
    assert exhaustive["threshold"] == threshold
    assert (exhaustive["verdict"], exhaustive["exit"]) == (
        ("PASS", 0) if exhaustive["detected"] >= threshold else ("FAIL", 1)
    )
    boundary = oracle["totals"]["boundary"]
    assert boundary["min_detected"] == exhaustive["detected"] + 1
    assert (boundary["verdict"], boundary["exit"]) == ("FAIL", 1)
    for count, needed in oracle["totals"]["default_threshold_by_count"].items():
        assert needed == math.ceil(0.8 * int(count)), count
    _assert_selection_and_auto_follow_counts(oracle)


def _assert_selection_and_auto_follow_counts(oracle: dict[str, Any]) -> None:
    """Round-robin order and `--count auto` cases must follow the candidate counts."""
    counts = {name: body["count"] for name, body in oracle["operators"].items()}
    selection = oracle["selection"]
    assert selection["operator_counts"] == counts

    def picks(count: int) -> list[str]:
        order: list[str] = []
        taken = dict.fromkeys(OPERATORS, 0)
        while len(order) < count and any(taken[name] < counts[name] for name in OPERATORS):
            for name in OPERATORS:
                if taken[name] < counts[name] and len(order) < count:
                    taken[name] += 1
                    order.append(name)
        return order

    for count, sequence in selection["round_robin"].items():
        assert sequence == picks(int(count)), count
    for count, per_operator in selection["picks_per_operator"].items():
        chosen = picks(int(count))
        assert per_operator == {name: chosen.count(name) for name in OPERATORS}, count
    shortfall = selection["shortfall"]
    assert shortfall["candidates"] == sum(counts.values())
    assert shortfall["run"] == shortfall["candidates"] - len(oracle["outcomes"]["does_not_build"])
    assert shortfall["message"] == (
        f"only {shortfall['run']} Mutants could be scored ({shortfall['count']} requested)"
    )
    assert f"ceil(0.8 x {shortfall['run']}) = " in shortfall["threshold_by_doc"]
    auto = oracle["auto"]
    formula = max(3, min(25, round(math.sqrt(auto["scope_lines"]))))
    assert auto["size_formula_count"] == formula
    for case in auto["cases"]:
        assert case["count"] == min(formula, case["time_cap"]), case["timeout"]
        assert f"{case['count']} Mutants" in case["progress_line"]


def test_decoys_are_never_candidates() -> None:
    oracle = _oracle()
    pairs = _candidates(oracle)
    for decoy in oracle["decoys"]:
        where = f"{decoy['file']}:{decoy['line']}"
        assert decoy["text"] in _line(decoy["file"], decoy["line"]), where
        assert decoy["reason"], where
        same_line = [
            candidate
            for _, candidate in pairs
            if (candidate["file"], candidate["line"]) == (decoy["file"], decoy["line"])
        ]
        if decoy["never"] == "*":
            assert not same_line, f"{where} is a decoy but has candidates"
        else:
            forbidden = [
                c
                for c in same_line
                if (c["site"], c["replacement"]) == (decoy["text"], decoy["never"])
            ]
            assert not forbidden, f"{where} lists the forbidden change {decoy['never']!r}"


def _filesets() -> dict[str, dict[str, Any]]:
    core = yaml.safe_load((PROJECT / "mutation.core").read_text(encoding="utf-8"))
    filesets: dict[str, dict[str, Any]] = core["filesets"]
    return filesets


def _fileset_files(fileset: dict[str, Any]) -> list[str]:
    """Return a fileset's file paths; an entry is a path or a one-key mapping."""
    return [next(iter(entry)) if isinstance(entry, dict) else entry for entry in fileset["files"]]


def test_unmutated_files_carry_their_tags() -> None:
    oracle = _oracle()
    filesets = _filesets()
    tags_by_file = {
        path: set(fileset.get("tags", []))
        for fileset in filesets.values()
        for path in _fileset_files(fileset)
    }
    candidate_files = {candidate["file"] for _, candidate in _candidates(oracle)}
    for entry in oracle["unmutated"]:
        path = entry["path"]
        assert (PROJECT / path).is_file(), path
        assert entry["tag"] in FILE_TAGS and entry["tag"] in tags_by_file[path], path
        assert path not in oracle["default_scope"], path
        assert path not in candidate_files, path
    # The default scope is every untagged HDL file of the Target, in Target order.
    untagged = [
        path for path in _fileset_files(filesets["rtl"]) if not FILE_TAGS & tags_by_file[path]
    ]
    assert oracle["default_scope"] == untagged


@pytest.mark.parametrize("target", ["sim_icarus", "sim_cocotb"])
def test_both_targets_share_the_rtl_filesets(target: str) -> None:
    core = yaml.safe_load((PROJECT / "mutation.core").read_text(encoding="utf-8"))
    assert core["targets"][target]["filesets"][:3] == ["rtl", "vendored", "generated"]
    registry = (PROJECT / "project-data" / "tests.toml").read_text(encoding="utf-8")
    assert re.search(rf"\[{target}\]\ntests = \[\"full\", \"smoke\"\]", registry)
