"""Single-field Campaign corruption on an explicitly marked disposable copy."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path


def rewrite_points(path: Path, document: dict, records: list[dict]) -> None:
    raw = b"".join(json.dumps(row, sort_keys=True).encode() + b"\n" for row in records)
    compressed = gzip.compress(raw, mtime=0)
    path.write_bytes(compressed)
    document["point_store"].update(
        bytes=len(compressed),
        uncompressed_bytes=len(raw),
        point_count=len(records) - 1,
        sha256="sha256:" + hashlib.sha256(compressed).hexdigest(),
    )


def corrupt_points(mode: str, path: Path, document: dict) -> None:
    if mode == "missing-points":
        path.unlink()
    elif mode == "changed-points":
        path.write_bytes(path.read_bytes() + b"changed")
    elif mode == "truncated-gzip":
        data = path.read_bytes()[:-8]
        path.write_bytes(data)
        document["point_store"].update(
            bytes=len(data), sha256="sha256:" + hashlib.sha256(data).hexdigest()
        )
    elif mode == "trailing-data":
        data = path.read_bytes() + b"not a gzip member"
        path.write_bytes(data)
        document["point_store"].update(
            bytes=len(data), sha256="sha256:" + hashlib.sha256(data).hexdigest()
        )
    else:
        records = [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
        if mode == "duplicate-point":
            records.append(records[-1])
        elif mode == "invalid-final-record":
            records[-1]["hits_by_run"] = {"wrong-run": -1}
        else:
            raise ValueError("unknown point fault")
        rewrite_points(path, document, records)


def regular_owned(path: Path, owned: Path) -> None:
    if (
        path.is_symlink()
        or not path.resolve(strict=True).is_relative_to(owned)
        or not path.is_file()
    ):
        raise ValueError("mutation destination must be a regular file inside the owned copy")
    if path.stat().st_nlink != 1:
        raise ValueError("mutation destination must not share a hard link")


def mutate(path: Path, owned: Path, mode: str) -> None:
    """Reject unmarked/outside paths; callers retain an untouched canonical archive."""
    owned = owned.resolve(strict=True)
    if not (owned / ".qa-coverage-fault-copy").is_file():
        raise ValueError("explicit disposable-copy marker required")
    if not path.resolve(strict=True).is_relative_to(owned) or path.is_symlink():
        raise ValueError("Campaign must be a regular file in the owned copy")
    regular_owned(path, owned)
    regular_owned(path.parent / "coverage-points.jsonl.gz", owned)
    document = json.loads(path.read_text())
    points = path.parent / "coverage-points.jsonl.gz"
    if mode in {
        "missing-points",
        "changed-points",
        "truncated-gzip",
        "trailing-data",
        "duplicate-point",
        "invalid-final-record",
    }:
        corrupt_points(mode, points, document)
    elif mode in {"v1", "v2"}:
        document["$schema"] = "booley.coverage-campaign/" + mode
        document.pop("source_rollups")
        if mode == "v1":
            document["points"] = [
                json.loads(line) for line in gzip.decompress(points.read_bytes()).splitlines()
            ][1:]
            document.pop("point_store")
    else:
        corrupt_manifest(mode, path, document, owned)
    path.write_text(json.dumps(document, sort_keys=True) + "\n")


def corrupt_manifest(mode: str, path: Path, document: dict, owned: Path) -> None:
    fields = {
        "point-count": "point_count",
        "compressed-size": "bytes",
        "uncompressed-size": "uncompressed_bytes",
    }
    if mode in fields:
        document["point_store"][fields[mode]] += 1
    elif mode == "point-digest":
        document["point_store"]["sha256"] = "sha256:" + "0" * 64
    elif mode in {"unsafe-relative-path", "absolute-path"}:
        document["point_store"]["path"] = (
            "../outside.gz" if mode.startswith("unsafe") else str(owned / "outside.gz")
        )
    elif mode == "symlink":
        points = path.parent / "coverage-points.jsonl.gz"
        outside = owned / "outside.gz"
        with outside.open("xb") as stream:
            stream.write(points.read_bytes())
        points.unlink()
        points.symlink_to(outside)
    elif mode == "wrong-rollup":
        document["rollups"][0]["covered_points"] += 1
    elif mode == "wrong-source-rollup":
        rows = document["source_rollups"]
        if len(rows) < 2 or rows[0]["rollups"] == rows[1]["rollups"]:
            raise ValueError("two sources with distinct distributions required")
        rows[0]["rollups"], rows[1]["rollups"] = rows[1]["rollups"], rows[0]["rollups"]
    elif mode == "wrong-evaluation":
        document["evaluation"]["status"] = (
            "pass" if document["evaluation"]["status"] != "pass" else "fail"
        )
    elif mode == "resource-ceiling":
        document["point_store"]["point_count"] = 1_000_001
    else:
        raise ValueError(f"unknown Campaign fault: {mode}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode")
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--owned", type=Path, required=True)
    args = parser.parse_args()
    mutate(args.campaign, args.owned, args.mode)


if __name__ == "__main__":
    main()
