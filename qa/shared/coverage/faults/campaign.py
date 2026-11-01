"""Single-field Campaign corruption on an explicitly marked disposable copy."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path

REFERENCE_SCHEMA = "booley.coverage-campaign-reference/v1"
#: Booley's uncompressed point-store ceiling (coverage_campaign_store.MAX_UNCOMPRESSED_BYTES).
POINT_CEILING_BYTES = 512 * 1024 * 1024


def canonical(value: object) -> bytes:
    """Encode exactly like Booley's point lines and reference: compact, sorted, UTF-8."""
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        + b"\n"
    )


def corrupt_points(mode: str, path: Path) -> None:
    """Booley trusts the recorded point-store identity, so the manifest stays as published."""
    if mode == "missing-points":
        path.unlink()
    elif mode == "truncated-gzip":
        path.write_bytes(path.read_bytes()[:-8])
    elif mode == "trailing-data":
        path.write_bytes(path.read_bytes() + b"not a gzip member")
    elif mode == "resource-ceiling":
        # Decompresses one byte past the ceiling; the reader must stop there.
        with gzip.GzipFile(path, "wb", compresslevel=1, mtime=0) as stream:
            for start in range(0, POINT_CEILING_BYTES + 1, 1 << 20):
                stream.write(b" " * min(1 << 20, POINT_CEILING_BYTES + 1 - start))
    else:
        records = [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
        if mode == "duplicate-point":
            records.append(records[-1])
        elif mode == "invalid-final-record":
            records[-1]["hits_by_run"] = {"wrong-run": -1}
        else:
            raise ValueError("unknown point fault")
        path.write_bytes(gzip.compress(b"".join(canonical(row) for row in records), mtime=0))


def regular_owned(path: Path, owned: Path) -> None:
    if (
        path.is_symlink()
        or not path.resolve(strict=True).is_relative_to(owned)
        or not path.is_file()
    ):
        raise ValueError("mutation destination must be a regular file inside the owned copy")
    if path.stat().st_nlink != 1:
        raise ValueError("mutation destination must not share a hard link")


def nested_campaign(reference: Path, owned: Path) -> tuple[Path, dict]:
    """Follow a Target reference to its nested Campaign manifest inside the owned copy."""
    document = json.loads(reference.read_text())
    if document.get("$schema") != REFERENCE_SCHEMA:
        raise ValueError(f"expected the Target coverage.json ({REFERENCE_SCHEMA})")
    nested = document["coverage_campaign"]
    if nested.get("path_base") != "origin_target":
        raise ValueError("unsupported reference path_base")
    path = reference.parent / nested["path"]
    regular_owned(path, owned)
    return path, document


def rebind_reference(reference: Path, document: dict, campaign: Path) -> None:
    """Update the reference envelope so validation reaches the mutated Campaign."""
    raw = campaign.read_bytes()
    document["coverage_campaign"].update(
        bytes=len(raw), sha256="sha256:" + hashlib.sha256(raw).hexdigest()
    )
    reference.write_bytes(canonical(document))


def mutate(reference: Path, owned: Path, mode: str) -> None:
    """Reject unmarked/outside paths; callers retain an untouched canonical archive."""
    owned = owned.resolve(strict=True)
    if not (owned / ".qa-coverage-fault-copy").is_file():
        raise ValueError("explicit disposable-copy marker required")
    if not reference.resolve(strict=True).is_relative_to(owned) or reference.is_symlink():
        raise ValueError("Campaign must be a regular file in the owned copy")
    regular_owned(reference, owned)
    path, reference_document = nested_campaign(reference, owned)
    regular_owned(path.parent / "coverage-points.jsonl.gz", owned)
    document = json.loads(path.read_text())
    points = path.parent / "coverage-points.jsonl.gz"
    if mode in {
        "missing-points",
        "truncated-gzip",
        "trailing-data",
        "duplicate-point",
        "invalid-final-record",
        "resource-ceiling",
    }:
        corrupt_points(mode, points)
    elif mode in {"v1", "v2", "v3"}:
        # The schema gate rejects an unsupported version before any decode.
        document["$schema"] = "booley.coverage-campaign/" + mode
    else:
        corrupt_manifest(mode, path, document, owned)
    path.write_bytes(canonical(document))
    rebind_reference(reference, reference_document, path)


def corrupt_manifest(mode: str, path: Path, document: dict, owned: Path) -> None:
    if mode == "symlink":
        points = path.parent / "coverage-points.jsonl.gz"
        outside = owned / "outside.gz"
        with outside.open("xb") as stream:
            stream.write(points.read_bytes())
        points.unlink()
        points.symlink_to(outside)
    elif mode == "wrong-rollup":
        document["rollups"][0]["covered_points"] += 1
    elif mode == "wrong-evaluation":
        document["evaluation"]["status"] = (
            "pass" if document["evaluation"]["status"] != "pass" else "fail"
        )
    else:
        raise ValueError(f"unknown Campaign fault: {mode}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode")
    parser.add_argument("campaign", type=Path, help="Target coverage.json reference in the copy")
    parser.add_argument("--owned", type=Path, required=True)
    args = parser.parse_args()
    mutate(args.campaign, args.owned, args.mode)


if __name__ == "__main__":
    main()
