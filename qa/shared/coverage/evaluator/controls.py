"""Literal independent oracle controls, separate from product qualification evidence."""

import argparse
import gzip
import hashlib
import json
import tempfile
from pathlib import Path

import measurements


def specimen(root: Path) -> tuple[Path, dict]:
    """Construct the oracle's minimal public-field pair from literal 4/8 operands."""
    points = []
    for bit in range(4):
        for before in range(2):
            label = f"value[{bit}]:{before}->{1 - before}"
            points.append(
                {
                    "id": label,
                    "identity": {
                        "metric": "toggle",
                        "location": {"source": "rtl/toggle.sv", "start": {"line": 2}},
                        "subject": {"signal_bit_direction": label},
                    },
                    "disposition": {"kind": "eligible"},
                    "hits_by_run": {"r1": 1} if bit < 2 else {},
                }
            )
    rollup = {
        "metric": "toggle",
        "total_points": 8,
        "eligible_points": 8,
        "covered_points": 4,
        "waived_points": 0,
        "percent": 50,
    }
    source_metrics = [rollup] + [
        {
            "metric": metric,
            "total_points": 0,
            "eligible_points": 0,
            "covered_points": 0,
            "waived_points": 0,
            "percent": None,
        }
        for metric in ["line", "branch", "expression"]
    ]
    header = {
        "$schema": "booley.coverage-points/v1",
        "campaign_id": "qa-control",
        "target_identity": "booley:qa:coverage:1#sim_toggle",
    }
    raw = b"".join(json.dumps(row).encode() + b"\n" for row in [header, *points])
    compressed = gzip.compress(raw, mtime=0)
    (root / "coverage-points.jsonl.gz").write_bytes(compressed)
    manifest = {
        "$schema": "booley.coverage-campaign/v3",
        "campaign_id": "qa-control",
        "target": {"identity": header["target_identity"]},
        "tests": {"runs": [{"id": "r1", "test": "half"}]},
        "evaluation": {"status": "fail"},
        "rollups": [rollup],
        "source_rollups": [{"source": "rtl/toggle.sv", "rollups": source_metrics}],
        "point_store": {
            "path": "coverage-points.jsonl.gz",
            "bytes": len(compressed),
            "uncompressed_bytes": len(raw),
            "point_count": 8,
            "sha256": "sha256:" + hashlib.sha256(compressed).hexdigest(),
        },
    }
    path = root / "coverage.json"
    path.write_text(json.dumps(manifest))
    case = json.loads((Path(__file__).parents[1] / "expected.json").read_text())["threshold-above"]
    return path, case


def exercise(mode: str, root: Path) -> None:
    path, case = specimen(root)
    if mode == "digest":
        point_store = root / "coverage-points.jsonl.gz"
        corrupt = bytearray(point_store.read_bytes())
        corrupt[-1] ^= 1
        point_store.write_bytes(corrupt)
    manifest, points = measurements.read_campaign(path)
    if mode == "hit-count":
        points[0]["hits_by_run"]["r1"] = 3
    elif mode == "source-grouping":
        manifest["source_rollups"][0]["source"] = "rtl/other.sv"
    elif mode == "rational-verdict":
        manifest["evaluation"]["status"] = "pass"
    measurements.verify_known_answer(manifest, points, case)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=["positive", "hit-count", "source-grouping", "digest", "rational-verdict"]
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="coverage-oracle-") as directory:
        try:
            exercise(args.mode, Path(directory))
        except ValueError as error:
            if args.mode == "positive":
                raise
            print(json.dumps({"control": args.mode, "detected": str(error)}))
        else:
            if args.mode != "positive":
                raise ValueError("negative control escaped detection")
            print(json.dumps({"control": "positive", "exact_fraction": "4/8"}))


if __name__ == "__main__":
    main()
