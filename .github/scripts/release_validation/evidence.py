"""Write a small, uniform evidence envelope for a release validation gate."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _named_value(value: str) -> tuple[str, str]:
    name, separator, detail = value.partition("=")
    if not separator or not name or not detail:
        raise argparse.ArgumentTypeError("value must be NAME=VALUE")
    return name, detail


def _unique_values(values: list[tuple[str, str]], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in values:
        if name in result:
            raise ValueError(f"duplicate {label}: {name}")
        result[name] = value
    return result


def report(
    *,
    candidate_sha: str,
    images: dict[str, str],
    check_ids: list[str],
    uid: int | None = None,
    gid: int | None = None,
    cleanup: list[str] | None = None,
) -> dict[str, object]:
    if not candidate_sha:
        raise ValueError("candidate SHA must not be empty")
    if not images:
        raise ValueError("at least one image identity is required")
    if not check_ids or any(not check_id for check_id in check_ids):
        raise ValueError("at least one non-empty check ID is required")
    if len(set(check_ids)) != len(check_ids):
        raise ValueError("check IDs must be unique")
    if (uid is None) != (gid is None):
        raise ValueError("uid and gid must be provided together")
    payload: dict[str, object] = {
        "schema": 1,
        "candidate": {"sha": candidate_sha, "images": images},
        "checks": [{"id": check_id, "status": "pass"} for check_id in check_ids],
    }
    if uid is not None and gid is not None:
        payload["identity"] = {"uid": uid, "gid": gid}
    if cleanup:
        payload["cleanup"] = dict.fromkeys(cleanup, True)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--image", action="append", required=True, type=_named_value)
    parser.add_argument("--check", action="append", required=True)
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    parser.add_argument("--cleanup", action="append", default=[])
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    payload = report(
        candidate_sha=args.candidate_sha,
        images=_unique_values(args.image, "image name"),
        check_ids=args.check,
        uid=args.uid,
        gid=args.gid,
        cleanup=args.cleanup,
    )
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
