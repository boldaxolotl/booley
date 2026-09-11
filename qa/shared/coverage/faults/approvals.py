"""Bind only the two exact maintainer-approved fixture points to retained evidence."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def select(points: list[dict], spec: dict) -> list[dict]:
    selected = []
    for point in points:
        identity = point["identity"]
        selector = spec["selector"]
        if (
            identity["metric"] != selector["metric"]
            or identity["location"]["source"] != spec["source"]
        ):
            continue
        matched = (
            identity["location"]["start"]["line"] in selector["source_lines"]
            if "source_lines" in selector
            else identity["subject"]["signal_bit_direction"] in selector["subjects"]
        )
        if matched and point["disposition"]["kind"] == "eligible":
            selected.append(point)
    if len(selected) != 2 or any(p["hits_by_run"] for p in selected):
        raise ValueError(
            "exactly two unhit, eligible preapproved points required; no substitution"
        )
    return sorted(selected, key=lambda p: p["id"])


def bind(campaign: Path, project: Path, directory: Path, inputs: dict, reason: str) -> Path:
    """Write approval TOML without changing source, Campaign or approval semantics."""
    document = json.loads(campaign.read_text())
    spec = inputs[reason]
    if document["target"]["identity"] != spec["target"]:
        raise ValueError("approval Target identity mismatch")
    ref = document["point_store"]
    points_path = campaign.parent / ref["path"]
    if ref["path"] != "coverage-points.jsonl.gz" or points_path.is_symlink():
        raise ValueError("unsafe point store")
    if digest(points_path) != ref["sha256"]:
        raise ValueError("Campaign point store digest mismatch")
    points = [json.loads(line) for line in gzip.decompress(points_path.read_bytes()).splitlines()][
        1:
    ]
    source_hash = digest(project / spec["source"])
    recorded = {r["path"]: r["sha256"] for r in document["source_closure"]["rtl"]}
    if recorded.get(spec["source"]) != source_hash:
        raise ValueError("approved source differs from retained RTL closure")
    selected = select(points, spec)
    target = directory / (spec["source"] + ".toml")
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        'schema = "booley.coverage-waivers/v1"',
        "source = " + json.dumps(spec["source"]),
        "source_sha256 = " + json.dumps(source_hash),
    ]
    for index, point in enumerate(selected):
        fields = {
            "id": f"qa-{reason}-{index}",
            "target": spec["target"],
            "point_id": point["id"],
            "reason": reason,
            "justification": spec["justification"],
            **{k: inputs[k] for k in ("approved_by", "approved_at", "approval_ref")},
        }
        lines.extend(["", "[[approval]]", *[f"{k} = {json.dumps(v)}" for k, v in fields.items()]])
        if reason == "unreachable":
            proof = directory / spec["proof_reference"]
            if "SUCCESS" not in proof.read_text():
                raise ValueError("retain successful formal proof before binding")
            lines.extend(
                [
                    "[approval.proof]",
                    'kind = "formal"',
                    "reference = " + json.dumps(spec["proof_reference"]),
                    "sha256 = " + json.dumps(digest(proof)),
                ]
            )
    with target.open("x") as stream:
        stream.write("\n".join(lines) + "\n")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reason", choices=["excluded", "unreachable"])
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    args = parser.parse_args()
    print(
        bind(
            args.campaign,
            args.project,
            args.directory,
            json.loads(args.inputs.read_text()),
            args.reason,
        )
    )


if __name__ == "__main__":
    main()
