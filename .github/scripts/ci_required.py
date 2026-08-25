#!/usr/bin/env python3
"""Resolve the stable aggregate CI check from conditional job results."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def _result(entry: Any) -> str:
    if not isinstance(entry, dict) or not isinstance(entry.get("result"), str):
        return "missing"
    return entry["result"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--required", required=True)
    parser.add_argument("--needs-json", required=True)
    args = parser.parse_args()
    try:
        needs = json.loads(args.needs_json)
    except json.JSONDecodeError as error:
        print(f"error: invalid needs JSON: {error}", file=sys.stderr)
        return 2
    if not isinstance(needs, dict):
        print("error: needs JSON must be an object", file=sys.stderr)
        return 2

    required = set(filter(None, args.required.split(",")))
    failures = {
        job: _result(needs.get(job)) for job in required if _result(needs.get(job)) != "success"
    }
    failures.update(
        {
            job: _result(entry)
            for job, entry in needs.items()
            if _result(entry) not in {"success", "skipped"}
        }
    )
    if failures:
        rendered = ", ".join(f"{job}={result}" for job, result in sorted(failures.items()))
        print(f"required CI did not pass: {rendered}", file=sys.stderr)
        return 1
    print(f"Required CI passed: {','.join(sorted(required))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
