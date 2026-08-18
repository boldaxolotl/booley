#!/usr/bin/env python3
"""Developer CI gate: validate every `bwave <cmd> --format json` payload
against the schema baked into the binary.

Runs `bwave schema` to fetch the schema, then runs the four implemented
subcommands (`list`, `value`, `find`, `stats`) against a representative
.fst fixture and validates each stdout against the schema.

Exits 0 on success, 1 on any drift (schema/envelope mismatch).

Requires: `jsonschema` (pip install jsonschema). On Windows the bundled
Python 3.14 has it; on MSYS2 Bash you'd need to use the project venv.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:
    print("ERROR: jsonschema not installed. `pip install jsonschema`", file=sys.stderr)
    sys.exit(2)


def find_binary() -> str:
    """Locate the bwave binary. Prefer whichever build is newer so a dev
    iteration on debug isn't shadowed by a stale release artifact."""
    repo_root = Path(__file__).resolve().parent.parent
    candidates = [
        repo_root / "target" / "debug" / ("bwave.exe" if os.name == "nt" else "bwave"),
        repo_root / "target" / "release" / ("bwave.exe" if os.name == "nt" else "bwave"),
    ]
    existing = [c for c in candidates if c.exists()]
    if existing:
        return str(max(existing, key=lambda p: p.stat().st_mtime))
    # Fall back to PATH lookup.
    return "bwave"


def find_fixture(repo_root: Path) -> Path:
    """Pick a small representative .fst store for validation."""
    fixtures = repo_root / "tests" / "fixtures"
    candidates = [
        fixtures / "test_basic.fst",
        fixtures / "test_many_signals.fst",
        fixtures / "test_wide_signals.fst",
    ]
    for c in candidates:
        if c.exists():
            return c
    # Fall back to any prebuilt .fst already sitting in the fixtures dir.
    for c in sorted(fixtures.glob("*.fst")):
        return c
    raise FileNotFoundError(
        f"No .fst fixture found in {fixtures}; build one with "
        f"`bwave build tests/fixtures/test_basic.vcd -o tests/fixtures/test_basic.fst`."
    )


def run_bwave(binary: str, args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def validate(validator: Draft202012Validator, command: str, payload: str) -> list[str]:
    """Return a list of schema-violation messages (empty on success)."""
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError as e:
        return [f"{command}: stdout is not valid JSON: {e}"]

    errors = sorted(validator.iter_errors(obj), key=lambda e: e.path)
    msgs = []
    for err in errors:
        path = "/".join(str(p) for p in err.absolute_path) or "<root>"
        msgs.append(f"{command}: at {path}: {err.message}")
    return msgs


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    binary = find_binary()
    fixture = find_fixture(repo_root)

    # 1) Fetch the schema from the binary itself. This is the contract.
    rc, schema_stdout, schema_stderr = run_bwave(binary, ["schema"])
    if rc != 0:
        print(f"ERROR: `{binary} schema` exited {rc}\n{schema_stderr}", file=sys.stderr)
        return 1
    try:
        schema = json.loads(schema_stdout)
    except json.JSONDecodeError as e:
        print(f"ERROR: schema is not valid JSON: {e}", file=sys.stderr)
        return 1

    validator = Draft202012Validator(schema)

    # 2) Run each implemented subcommand against the fixture and validate.
    cases: list[tuple[str, list[str]]] = [
        ("list", ["list", str(fixture), "--format", "json"]),
        ("value", ["value", str(fixture), "--at", "1", "--format", "json"]),
        ("find", ["find", str(fixture), "*", "rising", "--format", "json"]),
        ("stats", ["stats", str(fixture), "--format", "json"]),
    ]

    all_errors: list[str] = []
    for name, argv in cases:
        rc, stdout, stderr = run_bwave(binary, argv)
        if rc not in (0, 2):  # 2 = recoverable (e.g. bad --virtual)
            all_errors.append(f"{name}: `bwave {' '.join(argv)}` exited {rc}\nstderr: {stderr}")
            continue
        if not stdout.strip():
            all_errors.append(f"{name}: empty stdout (rc={rc}, stderr={stderr!r})")
            continue
        errs = validate(validator, name, stdout)
        if errs:
            all_errors.extend(errs)
        else:
            print(f"  OK  bwave {' '.join(argv[:2])} ...")

    if all_errors:
        print("\nFAIL: schema drift detected:", file=sys.stderr)
        for e in all_errors:
            print(f"  - {e}", file=sys.stderr)
        print(
            "\nHint: edit schema/bwave.json and/or src/output.rs to bring "
            "them back into sync, then rerun this check.",
            file=sys.stderr,
        )
        return 1

    print("\nOK: all envelopes match schema.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
