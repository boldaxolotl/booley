"""Independent arithmetic and native-record oracle; never imports Booley."""

import argparse
import gzip
import hashlib
import json
import re
from collections import Counter
from fractions import Fraction
from itertools import pairwise
from pathlib import Path

SOURCE_METRICS = {"line", "branch", "expression", "toggle"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_campaign(path: Path) -> tuple[dict, list[dict]]:
    """Read the bounded public pair and independently verify its integrity."""
    require(path.stat().st_size <= 16 * 1024 * 1024, "manifest size ceiling")
    manifest = json.loads(path.read_text())
    require(manifest["$schema"] == "booley.coverage-campaign/v3", "expected V3")
    ref = manifest["point_store"]
    require(ref["path"] == "coverage-points.jsonl.gz", "noncanonical point path")
    points_path = path.parent / ref["path"]
    require(not points_path.is_symlink(), "symlink point store")
    require(points_path.stat().st_size <= 256 * 1024 * 1024, "compressed ceiling")
    compressed = points_path.read_bytes()
    require(len(compressed) == ref["bytes"], "compressed bytes mismatch")
    digest = "sha256:" + hashlib.sha256(compressed).hexdigest()
    require(digest == ref["sha256"], "point digest mismatch")
    with gzip.open(points_path, "rb") as stream:
        raw = stream.read(512 * 1024 * 1024 + 1)
    require(len(raw) <= 512 * 1024 * 1024, "uncompressed ceiling")
    require(len(raw) == ref["uncompressed_bytes"], "uncompressed bytes mismatch")
    records = [json.loads(line) for line in raw.splitlines()]
    header, *points = records
    require(header["$schema"] == "booley.coverage-points/v1", "point schema")
    require(header["campaign_id"] == manifest["campaign_id"], "Campaign binding")
    require(header["target_identity"] == manifest["target"]["identity"], "Target binding")
    require(len(points) == ref["point_count"], "point count mismatch")
    require(len({p["id"] for p in points}) == len(points), "duplicate points")
    return manifest, points


def counts(points: list[dict]) -> tuple[int, int, int, int]:
    """Return total, eligible, covered and waived counts from sparse incidence."""
    eligible = [p for p in points if p["disposition"]["kind"] == "eligible"]
    for point in points:
        require(
            all(type(n) is int and n > 0 for n in point["hits_by_run"].values()),
            "incidence must contain positive integer counts only",
        )
    return (
        len(points),
        len(eligible),
        sum(bool(p["hits_by_run"]) for p in eligible),
        sum(p["disposition"]["kind"] == "waived" for p in points),
    )


def verify_rollups(
    rollups: list[dict], points: list[dict], metrics: set[str] | None = None
) -> None:
    """Verify counts and presentation separately from exact policy arithmetic."""
    expected_metrics = (
        metrics if metrics is not None else {p["identity"]["metric"] for p in points}
    )
    require(len(rollups) == len(expected_metrics), "rollup metric inventory mismatch")
    require({r["metric"] for r in rollups} == expected_metrics, "rollup metric inventory mismatch")
    for rollup in rollups:
        population = [p for p in points if p["identity"]["metric"] == rollup["metric"]]
        total, eligible, covered, waived = counts(population)
        actual = tuple(
            rollup[k]
            for k in ("total_points", "eligible_points", "covered_points", "waived_points")
        )
        require(actual == (total, eligible, covered, waived), "rollup count mismatch")
        expected = round(100 * covered / eligible, 2) if eligible else None
        require(rollup["percent"] == expected, "rollup percentage mismatch")


def verify_sources(manifest: dict, points: list[dict]) -> None:
    """Property metrics intentionally have no source-file rollup contract."""
    sources = {p["identity"]["location"]["source"] for p in points}
    rows = manifest["source_rollups"]
    require(len(rows) == len(sources), "source inventory mismatch")
    require({row["source"] for row in rows} == sources, "source inventory mismatch")
    for source in rows:
        require(
            {r["metric"] for r in source["rollups"]} == SOURCE_METRICS,
            "unexpected per-source metric inventory",
        )
        population = [p for p in points if p["identity"]["location"]["source"] == source["source"]]
        verify_rollups(source["rollups"], population, SOURCE_METRICS)


def native_records(path: Path) -> dict[str, int]:
    """Parse the pinned native text format without product parser reuse."""
    require(path.stat().st_size <= 256 * 1024 * 1024, "native size ceiling")
    records = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"C '(.*)' ([0-9]+)", line)
        require(match is not None, "malformed native record")
        key, count = match.groups()
        require(key not in records, "duplicate native record")
        records[key] = int(count)
    return records


def verify_native(points: list[dict], native: dict[str, Path]) -> None:
    """Check every supplied per-run raw counter against normalized incidence."""
    keys = [p["identity"]["collector"]["native_key"] for p in points]
    require(len(keys) == len(set(keys)), "duplicate normalized native identity")
    for run_id, path in native.items():
        records = native_records(path)
        require(set(records) == set(keys), "native/normalized point inventory mismatch")
        for point in points:
            key = point["identity"]["collector"]["native_key"]
            require(key in records, "normalized point missing from native evidence")
            require(
                records[key] == point["hits_by_run"].get(run_id, 0),
                "native/normalized hit count mismatch",
            )


def symbol(point: dict) -> str:
    identity = point["identity"]
    if identity["metric"] == "toggle":
        label = identity["subject"]["signal_bit_direction"]
        match = re.search(r"value\[([0-3])\].*?([01])->([01])", label)
        require(match is not None, "unexpected toggle identity")
        bit, before, after = match.groups()
        return f"bit{bit}:{before}->{after}"
    return "line:" + str(identity["location"]["start"]["line"])


def expected_incidence(case: dict, test: str) -> dict[str, int]:
    if case["metric"] != "toggle":
        return dict.fromkeys(case["tests"][test], 1)
    sequence = case["sequences"][test]
    return dict(
        Counter(
            f"bit{bit}:{(before >> bit) & 1}->{(after >> bit) & 1}"
            for before, after in pairwise(sequence)
            for bit in range(4)
            if ((before ^ after) >> bit) & 1
        )
    )


def verify_known_answer(manifest: dict, points: list[dict], case: dict) -> dict:
    """Require a fixed population and exact per-test hit sets, not just a ratio."""
    population = [
        p
        for p in points
        if p["identity"]["metric"] == case["metric"] and p["disposition"]["kind"] == "eligible"
    ]
    require(
        manifest["target"]["identity"] == "booley:qa:coverage:1#" + case["target"],
        "wrong known-answer Target",
    )
    source = (
        "rtl/toggle.sv"
        if case["metric"] == "toggle"
        else "rtl/" + case["target"].removeprefix("sim_") + ".sv"
    )
    require(
        {p["identity"]["location"]["source"] for p in population} == {source},
        "wrong known-answer source",
    )
    actual = {symbol(p): p for p in population}
    require(len(actual) == len(population), "duplicate symbolic native point")
    require(set(actual) == set(case["population"]), "fixed denominator/identities mismatch")
    runs = {r["id"]: r["test"] for r in manifest["tests"]["runs"]}
    require(len(runs) == len(case["tests"]), "wrong selected run inventory")
    require(set(runs.values()) == set(case["tests"]), "wrong selected test suite")
    require(all(set(p["hits_by_run"]) <= set(runs) for p in points), "foreign run incidence")
    for run_id, test in runs.items():
        hits = {name for name, p in actual.items() if p["hits_by_run"].get(run_id, 0) > 0}
        require(hits == set(case["tests"][test]), f"wrong hit set for {test}")
        observed = {
            name: p["hits_by_run"][run_id]
            for name, p in actual.items()
            if run_id in p["hits_by_run"]
        }
        require(
            observed == expected_incidence(case, test), f"wrong transition/sample count for {test}"
        )
    covered = sum(bool(p["hits_by_run"]) for p in population)
    require((covered, len(population)) == tuple(case["fraction"]), "known fraction mismatch")
    verify_rollups(manifest["rollups"], points)
    verify_sources(manifest, points)
    ratio = Fraction(100 * covered, len(population))
    if "min_pct" in case:
        verdict = "pass" if ratio >= Fraction(str(case["min_pct"])) else "fail"
        require(manifest["evaluation"]["status"] == verdict, "exact policy verdict mismatch")
    return {"covered": covered, "eligible": len(population), "exact_percent": str(ratio)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--native", action="append", default=[], metavar="RUN_ID=PATH")
    args = parser.parse_args()
    manifest, points = read_campaign(args.campaign)
    native = dict(item.split("=", 1) for item in args.native)
    require(
        set(native) == {r["id"] for r in manifest["tests"]["runs"]},
        "provide native evidence for every run",
    )
    verify_native(points, {key: Path(value) for key, value in native.items()})
    case = json.loads(args.expected.read_text())[args.case]
    print(json.dumps(verify_known_answer(manifest, points, case), sort_keys=True))


if __name__ == "__main__":
    main()
