#!/usr/bin/env python3
"""Developer CI gate: validate every `bwave <cmd> --format json` payload
against the schema baked into the binary.

Runs `bwave schema` to fetch the schema, then runs the eight implemented
JSON subcommands, including clock-grid diagnostics against a representative
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
import tempfile
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


def query_cases(fixture: Path) -> list[tuple[str, list[str]]]:
    trace = str(fixture)
    return [
        ("list", ["list", trace, "--format", "json"]),
        ("value", ["value", trace, "--at", "1", "--format", "json"]),
        ("find", ["find", trace, "*", "rising", "--format", "json"]),
        ("stats", ["stats", trace, "--format", "json"]),
        ("signal", ["signal", trace, "--format", "json"]),
        ("wave", ["wave", trace, "--format", "json"]),
        ("distance", ["distance", trace, "*", "rising", "--format", "json"]),
        ("overview", ["overview", trace, "--format", "json"]),
    ]


def validate_cases(binary: str, validator: Draft202012Validator, fixture: Path) -> list[str]:
    errors: list[str] = []
    for name, argv in query_cases(fixture):
        rc, stdout, stderr = run_bwave(binary, argv)
        if rc != 0 or not stdout.strip():
            errors.append(f"{name}: exited {rc}, stderr={stderr!r}, stdout={stdout!r}")
            continue
        violations = validate(validator, name, stdout)
        errors.extend(violations)
        if not violations:
            print(f"  OK  {name}: {fixture.name}")
    return errors


def clock_fixture(binary: str, directory: Path, off_grid: bool) -> Path:
    label = "off-grid" if off_grid else "gated"
    vcd = directory / f"{label}.vcd"
    edge = 56 if off_grid else 55
    vcd.write_text(
        "$timescale 1ns $end\n$scope module tb $end\n"
        '$var wire 1 ! clk $end\n$var wire 1 " valid $end\n'
        "$upscope $end\n$enddefinitions $end\n"
        '#0\n0!\n1"\n#5\n1!\n#10\n0!\n#15\n1!\n#20\n0!\n'
        f"#{edge}\n1!\n#60\n0!\n"
    )
    store = vcd.with_suffix(".fst")
    rc, _, err = run_bwave(binary, ["build", str(vcd), "-o", str(store)])
    if rc != 0:
        raise RuntimeError(f"{label} build failed: {err}")
    return store


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    binary = find_binary()
    fixture = find_fixture(repo_root)
    rc, stdout, stderr = run_bwave(binary, ["schema"])
    if rc != 0:
        print(f"ERROR: schema exited {rc}: {stderr}", file=sys.stderr)
        return 1
    schema = json.loads(stdout)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    errors = validate_cases(binary, validator, fixture)
    with tempfile.TemporaryDirectory(prefix="bwave-schema-") as directory:
        for off_grid in [False, True]:
            store = clock_fixture(binary, Path(directory), off_grid)
            errors.extend(validate_cases(binary, validator, store))
            rc, payload, stderr = run_bwave(
                binary,
                [
                    "value",
                    str(store),
                    "--at",
                    "4",
                    "--sample-mode",
                    "pre-edge",
                    "--format",
                    "json",
                ],
            )
            if rc != 0:
                errors.append(f"pre-edge value: {stderr}")
            else:
                errors.extend(validate(validator, "pre-edge value", payload))
    if errors:
        print("FAIL: schema drift detected:\n" + "\n".join(errors), file=sys.stderr)
        return 1
    print("OK: all envelopes match schema, including gated/off-grid traces.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
