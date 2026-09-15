"""Read-only preflight and verdict oracles for PicoRV32 run-owned stimuli.

These checks validate Scenario Operator inputs and retained reports. They never run a
Booley Flow or turn a fixture self-test into a product Check Result.
"""

import argparse
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
from collections import Counter
from pathlib import Path


class FixtureError(ValueError):
    """A stimulus or retained result cannot support the declared Check."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise FixtureError(message)


def _mapping(value: object, name: str) -> dict:
    _require(isinstance(value, dict), f"{name} must be a JSON object")
    return value


def _git_root(path: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    _require(result.returncode == 0, f"{path}: not a usable Git checkout")
    return Path(result.stdout.strip()).resolve()


def git_topology(root: Path, mode: str, outer_ref: str = "HEAD", inner_ref: str = "HEAD") -> dict:
    """Verify the real outer and nested Git topology before a Check attempt."""
    root = root.resolve()
    inner = root / ".booley_project"
    _require((root / ".git").exists(), "outer checkout .git marker missing")
    _require(_git_root(root) == root, "outer .git resolves to another checkout")
    _require(inner.is_dir(), "nested Project directory missing")
    marker = inner / ".git"
    _require(marker.exists(), "nested Project .git marker missing")
    _require(_git_root(inner) == inner, "nested .git resolves to another checkout")
    if mode == "ticket":
        _require(marker.is_dir(), "Ticket Create requires standalone nested .git directory")
    elif mode == "synth":
        _require(marker.is_file(), "paired synthesis fixture requires linked nested .git file")
    else:
        _require(mode == "inventory", f"unknown topology mode: {mode}")
    for checkout, ref in ((root, outer_ref), (inner, inner_ref)):
        result = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--verify", ref + "^{commit}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        _require(result.returncode == 0, f"{checkout}: destination ref {ref} missing")
    return {"outer": str(root), "nested": str(inner), "mode": mode,
            "outer_ref": outer_ref, "inner_ref": inner_ref}


def spike_elf(path: Path, ram_start: int, ram_end: int) -> dict:
    """Require every ELF PT_LOAD segment, including header padding, inside RAM."""
    data = path.read_bytes()
    _require(len(data) >= 6, "truncated ELF identification header")
    _require(data[:4] == b"\x7fELF" and data[4] in (1, 2), "not an ELF32/ELF64 file")
    _require(data[5] in (1, 2), "unsupported ELF byte order")
    endian = "<" if data[5] == 1 else ">"
    elf64 = data[4] == 2
    _require(len(data) >= (64 if elf64 else 52), "truncated ELF header")
    phoff = struct.unpack_from(endian + ("Q" if elf64 else "I"), data,
                               32 if elf64 else 28)[0]
    phentsize = struct.unpack_from(endian + "H", data, 54 if elf64 else 42)[0]
    phnum = struct.unpack_from(endian + "H", data, 56 if elf64 else 44)[0]
    _require(0 < phnum <= 64 and phoff + phnum * phentsize <= len(data),
             "invalid or missing ELF program headers")
    segments = []
    for index in range(phnum):
        offset = phoff + index * phentsize
        if elf64:
            kind, _, _, vaddr, paddr, _, memsz, _ = struct.unpack_from(endian + "IIQQQQQQ", data, offset)
        else:
            kind, _, vaddr, paddr, _, memsz, _, _ = struct.unpack_from(endian + "IIIIIIII", data, offset)
        if kind != 1:
            continue
        address = paddr or vaddr
        _require(memsz > 0 and ram_start <= address and address + memsz <= ram_end,
                 f"PT_LOAD {index} [{address:#x}, {address + memsz:#x}) outside Spike RAM")
        segments.append({"start": address, "end": address + memsz})
    _require(bool(segments), "ELF has no PT_LOAD segment")
    return {"segments": segments, "ram_start": ram_start, "ram_end": ram_end}


def _warning_keys(report: dict) -> list[tuple]:
    report = _mapping(report, "lint report")
    warnings = report.get("warnings", [])
    _require(isinstance(warnings, list), "lint warnings must be a list")
    keys = []
    for value in warnings:
        item = _mapping(value, "lint warning")
        _require(all(field in item for field in ("rule", "file", "line", "message")),
                 "lint warning lacks identity fields")
        keys.append((item["rule"], item["file"], item["line"], item["message"]))
    return keys


def lint_dedupe(first: dict, second: dict, combined: dict) -> dict:
    """Prove the same real warning occurs for both Targets and once in aggregate."""
    first_keys, second_keys, combined_keys = map(set, map(_warning_keys, (first, second, combined)))
    common = first_keys & second_keys
    _require(bool(common), "no common real warning in both Target reports")
    _require(common <= combined_keys, "aggregate report omitted the common warning")
    _require(len(combined.get("warnings", [])) == len(combined_keys),
             "aggregate report contains duplicate warning rows")
    combined = _mapping(combined, "combined lint report")
    target_results = combined.get("target_results", [])
    _require(isinstance(target_results, list)
             and all(isinstance(row, dict) and isinstance(row.get("target"), str)
                     for row in target_results),
             "aggregate report has invalid Target results")
    targets = {row["target"] for row in target_results}
    _require(len(targets) >= 2, "aggregate report lacks two distinct Target results")
    return {"common_warnings": len(common), "targets": sorted(targets)}


def lint_waiver(before: dict, after: dict, intended_rule: str,
                control_rule: str | None = None) -> dict:
    """Require only the intended native warning to disappear."""
    earlier = Counter(_warning_keys(before))
    later = Counter(_warning_keys(after))
    intended = Counter({key: count for key, count in earlier.items()
                        if key[0] == intended_rule})
    _require(bool(intended), "intended warning absent before waiver")
    _require(sum(intended.values()) == 1,
             "waiver stimulus must contain exactly one intended warning")
    if control_rule is not None:
        controls = Counter({key: count for key, count in earlier.items()
                            if key[0] == control_rule})
        _require(bool(controls) and all(later[key] == count for key, count in controls.items()),
                 "unrelated control warning did not survive waiver")
    _require(all(key[0] != intended_rule for key in later),
             "intended warning remains after waiver")
    _require(later == earlier - intended, "waiver changed another warning")
    return {"suppressed": intended_rule, "preserved": control_rule}


def _synth_result(report: dict, target: str) -> tuple[dict, dict]:
    detail = _mapping(report.get("detail"), "synthesis report detail")
    implementation = _mapping(detail.get("implementation"), "synthesis implementation")
    results = _mapping(implementation.get("results"), "synthesis Target results")
    result = _mapping(results.get(target), "synthesis candidate Target result")
    comparison = _mapping(result.get("comparison"), "synthesis comparison")
    _require(comparison.get("basis_valid") is True and comparison.get("basis_errors") == [],
             "synthesis report has an invalid comparison basis")
    return result, comparison


def _synth_revisions(result: dict, comparison: dict) -> tuple[str | None, str | None]:
    provenance = _mapping(result.get("provenance"), "synthesis candidate provenance")
    producer = _mapping(provenance.get("producer"), "synthesis candidate producer")
    baseline = _mapping(comparison.get("baseline"), "synthesis comparison baseline")
    baseline_provenance = _mapping(baseline.get("provenance"), "synthesis baseline provenance")
    baseline_producer = _mapping(baseline_provenance.get("producer"),
                                 "synthesis baseline producer")
    return producer.get("source_revision"), baseline_producer.get("source_revision")


def _synth_expectations(summary: dict, expected: dict) -> tuple[dict, str, str]:
    for field in ("candidate_target", "baseline_target", "candidate_identity",
                  "baseline_identity"):
        value = summary.get(field)
        _require(isinstance(value, str) and bool(value.strip()),
                 f"synthesis comparison lacks {field}")
        _require(value == expected.get(field), f"synthesis {field} differs from declared identity")
    baseline = _mapping(summary.get("baseline"), "synthesis baseline report")
    ref = baseline.get("ref")
    candidate_ref = expected.get("candidate_revision")
    baseline_ref = expected.get("baseline_revision")
    _require(isinstance(candidate_ref, str)
             and re.fullmatch(r"[0-9a-f]{40}", candidate_ref) is not None,
             "declared candidate revision is not a full commit ID")
    _require(isinstance(ref, str) and isinstance(baseline_ref, str)
             and bool(ref.strip()) and re.fullmatch(r"[0-9a-f]{40}", baseline_ref) is not None
             and baseline_ref.startswith(ref),
             "synthesis baseline ref differs from declared revision")
    for field in ("delta_pct", "timing_delta_pct"):
        value = summary.get(field)
        expected_value = expected.get(field)
        _require(type(value) in (int, float) and math.isfinite(value),
                 f"synthesis comparison lacks numeric {field}")
        _require(type(expected_value) in (int, float) and math.isfinite(expected_value)
                 and value == expected_value,
                 f"synthesis {field} differs from declared numeric result")
    return baseline, candidate_ref, baseline_ref


def synth_baseline(summary: dict, report: dict, expected: dict) -> dict:
    """Validate the successful paired synthesis comparison and its identities."""
    summary = _mapping(summary, "synthesis summary")
    report = _mapping(report, "synthesis report")
    expected = _mapping(expected, "declared synthesis result")
    _require(type(summary.get("flow_exit")) is int and summary["flow_exit"] == 0,
             "synthesis comparison did not exit successfully")
    _require(summary.get("infra_error") in (None, ""),
             "synthesis comparison has an infrastructure error")
    baseline, expected_candidate_ref, expected_ref = _synth_expectations(summary, expected)
    result, comparison = _synth_result(report, summary["candidate_target"])
    candidate_revision, baseline_revision = _synth_revisions(result, comparison)
    _require(candidate_revision == expected_candidate_ref,
             "synthesis candidate revision differs from declared commit")
    _require(baseline_revision == expected_ref,
             "synthesis baseline revision differs from declared commit")
    identity = _mapping(result.get("identity"), "synthesis candidate identity")
    _require(identity.get("target_identity") == summary["candidate_identity"]
             and comparison.get("candidate_target_identity") == summary["candidate_identity"]
             and comparison.get("baseline_target_identity") == summary["baseline_identity"],
             "synthesis report Target identities contradict the summary")
    deltas = _mapping(comparison.get("deltas"), "synthesis report deltas")
    area_key = "area_kge" if "area_kge" in baseline else "area_um2"
    area = _mapping(deltas.get(area_key), f"synthesis {area_key} delta")
    timing = _mapping(deltas.get("wns_ns"), "synthesis timing delta")
    _require(area.get("delta_pct") == summary["delta_pct"]
             and timing.get("delta_pct") == summary["timing_delta_pct"],
             "synthesis report deltas contradict the summary")
    return {"candidate_identity": summary["candidate_identity"],
            "baseline_identity": summary["baseline_identity"],
            "candidate_revision": candidate_revision,
            "baseline_revision": baseline_revision, "delta_pct": summary["delta_pct"],
            "timing_delta_pct": summary["timing_delta_pct"]}


def lint_command(argv: list[str]) -> dict:
    """Reject the session-enter typo before invoking the hard-error stimulus."""
    _require(argv[:4] == ["booley", "session", "enter", "--"],
             "expected booley session enter -- ...")
    _require(len(argv) > 4 and argv[4] == "booley", "session command omits booley executable")
    _require("lint" in argv[5:], "session command does not invoke lint")
    return {"argv": argv}


def vivado_executable(mount: Path, registered_source: Path) -> dict:
    """Find the mounted executable by release layout, not a host-specific path."""
    source = registered_source.resolve()
    _require(source.is_dir(), "registered source directory missing")
    candidates = (Path("bin/vivado"), Path("Vivado/bin/vivado"))
    suffix = next((item for item in candidates if (source / item).is_file()), None)
    _require(suffix is not None, "registered source has no Vivado executable")
    executable = mount / suffix
    _require(executable.is_file() and os.access(executable, os.X_OK),
             f"mounted Vivado executable missing: {executable}")
    return {"registered_source": str(source), "mount_executable": str(executable)}


def distance_rows(text: str, expected_distances: list[int]) -> dict:
    """Accept B-Wave's documented text rows and check their numeric relation."""
    rows = re.findall(r"@\s*(\d+)\s*->\s*@\s*(\d+)\s+d=(\d+)", text)
    _require(bool(rows), "no B-Wave '@ start -> @ end d=...' row")
    observed = [int(delta) for _, _, delta in rows]
    _require(all(int(end) - int(start) == int(delta) for start, end, delta in rows),
             "distance row contradicts its start/end relation")
    _require(observed == expected_distances, "distance rows contradict expected sequence")
    return {"rows": len(rows), "distances": observed}


def required_subjects(pdf_text: dict[str, str], subjects: list[str]) -> dict:
    """Check required PDF subjects by extracted text, regardless of file count."""
    pdf_text = _mapping(pdf_text, "PDF text")
    _require(bool(pdf_text) and bool(subjects), "PDF text or subjects missing")
    _require(all(isinstance(value, str) for value in pdf_text.values())
             and isinstance(subjects, list)
             and all(isinstance(subject, str) and bool(subject) for subject in subjects),
             "PDF text or subjects have invalid types")
    contents = "\n".join(pdf_text.values()).casefold()
    missing = [subject for subject in subjects if subject.casefold() not in contents]
    _require(not missing, f"offline PDF subjects missing: {missing}")
    return {"files": len(pdf_text), "subjects": subjects}


def pdf_subjects(paths: list[Path], subjects: list[str]) -> dict:
    """Extract offline PDF text and check subjects without a filename-count rule."""
    _require(bool(paths), "no offline PDFs supplied")
    extracted = {}
    for path in paths:
        _require(path.is_file() and path.suffix.lower() == ".pdf", f"not a PDF: {path}")
        result = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                                capture_output=True, text=True, timeout=30, check=False)
        _require(result.returncode == 0, f"PDF text extraction failed: {path}")
        extracted[str(path)] = result.stdout
    return required_subjects(extracted, subjects)


def canonical_registration(requested: Path, registered: Path) -> dict:
    """Compare Vivado source identity after canonical path resolution."""
    source = requested.resolve(strict=True)
    _require(source == registered.resolve(strict=True), "registration source differs")
    return {"canonical_source": str(source)}


def vivado_implementation(report: dict, artifacts: dict, before: dict,
                           retained_dir: Path) -> dict:
    """Grade the declared fresh routed output without inventing a bitstream rule."""
    report = _mapping(report, "Vivado report")
    artifacts = _mapping(artifacts, "Vivado artifacts")
    before = _mapping(before, "prior Vivado artifacts")
    _require(type(report.get("exit_code")) is int and report["exit_code"] == 0,
             "Vivado Flow did not exit successfully")
    detail = _mapping(report.get("detail"), "Vivado report detail")
    implementation = _mapping(detail.get("implementation"), "Vivado implementation")
    _require(implementation.get("grade") == "pass" and implementation.get("passed") is True,
             "Vivado implementation did not report PASS")
    _require(bool(implementation.get("results")), "Vivado report lacks Target results")
    cache = _mapping(detail.get("cache"), "Vivado cache report")
    _require(bool(cache) and all(isinstance(item, dict)
                                 and item.get("cached") is False for item in cache.values()),
             "Vivado implementation was cached")
    required = ("routed-checkpoint.dcp", "routed-timing.rpt", "routed-utilization.rpt")
    for name in required:
        current = artifacts.get(name)
        _require(isinstance(current, dict) and current.get("size", 0) > 0
                 and current.get("sha256") and current.get("mtime_ns"),
                 f"missing declared routed artifact: {name}")
        prior = before.get(name)
        _require(prior is None or isinstance(prior, dict),
                 f"invalid prior routed artifact: {name}")
        _require(prior is None or current["mtime_ns"] > prior.get("mtime_ns", 0),
                 f"routed artifact is not fresh: {name}")
        retained = retained_dir / name
        _require(retained.is_file() and retained.stat().st_size == current["size"],
                 f"retained routed artifact missing or changed: {name}")
        _require(hashlib.sha256(retained.read_bytes()).hexdigest() == current["sha256"],
                 f"retained routed artifact digest mismatch: {name}")
    return {"grade": "pass", "artifacts": list(required)}


def stealth_native(paths: list[str]) -> dict:
    """Exclude all dot-prefixed projected paths from native-file counts."""
    _require(isinstance(paths, list) and all(isinstance(path, str) and bool(path)
                                             for path in paths),
             "projected paths must be a list of nonempty strings")
    native = [path for path in paths if all(not part.startswith(".")
                                             for part in Path(path).parts)]
    return {"raw_count": len(paths), "native_count": len(native), "native": native}


def same_bwave_mode(child: dict, replay: dict) -> dict:
    """Require an exact replay before declaring a child's B-Wave divergence."""
    child = _mapping(child, "B-Wave child query")
    replay = _mapping(replay, "B-Wave replay query")
    fields = ("trace", "argv", "signals", "clock", "reset", "sampling")
    for record, name in ((child, "child"), (replay, "replay")):
        _require(all(field in record for field in fields),
                 f"B-Wave {name} query is incomplete")
        _require(isinstance(record["trace"], str) and bool(record["trace"]),
                 f"B-Wave {name} trace is invalid")
        for field in ("argv", "signals"):
            value = record[field]
            _require(isinstance(value, list) and bool(value)
                     and all(isinstance(item, str) and bool(item) for item in value),
                     f"B-Wave {name} {field} is invalid")
        for field in ("clock", "reset"):
            _require(record[field] is None
                     or (isinstance(record[field], str) and bool(record[field])),
                     f"B-Wave {name} {field} is invalid")
        _require(isinstance(record["sampling"], str) and bool(record["sampling"]),
                 f"B-Wave {name} sampling is invalid")
    _require(all(child.get(field) == replay.get(field) for field in fields),
             "B-Wave replay differs from child query or defaults")
    return {"exact_replay": True}


def verdict(expected: str, observed: str) -> dict:
    """Preserve declared and independent observed results even on mismatch."""
    _require(expected in ("pass", "fail", "denied"), "invalid declared expectation")
    _require(observed in ("pass", "fail", "denied"), "invalid observed result")
    return {"declared_expected": expected, "observed": observed,
            "matches": expected == observed}


def oracle(record: dict) -> dict:
    """Evaluate a retained JSON oracle input without modifying run evidence."""
    record = _mapping(record, "oracle input")
    kind = record["kind"]
    if kind == "lint-dedupe":
        result = lint_dedupe(record["first"], record["second"], record["combined"])
    elif kind == "lint-waiver":
        result = lint_waiver(record["before"], record["after"],
                             record["intended_rule"], record.get("control_rule"))
    elif kind == "synth-baseline":
        result = synth_baseline(record["summary"], record["report"], record["expected"])
    elif kind == "riscv-subjects":
        result = required_subjects(record["pdf_text"], record["subjects"])
    elif kind == "vivado-implementation":
        result = vivado_implementation(record["report"], record["artifacts"],
                                       record["before"], Path(record["retained_dir"]))
    elif kind == "stealth-native":
        result = stealth_native(record["paths"])
    elif kind == "bwave-mode":
        result = same_bwave_mode(record["child"], record["replay"])
    elif kind == "verdict":
        result = verdict(record["declared_expected"], record["observed"])
    else:
        raise FixtureError(f"unknown oracle: {kind}")
    return result


def _parser() -> argparse.ArgumentParser:
    """Build the small Scenario Operator-facing preflight CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="kind", required=True)
    topology = sub.add_parser("git-topology")
    topology.add_argument("root", type=Path)
    topology.add_argument("mode", choices=("inventory", "synth", "ticket"))
    topology.add_argument("--outer-ref", default="HEAD")
    topology.add_argument("--inner-ref", default="HEAD")
    elf = sub.add_parser("spike-elf")
    elf.add_argument("path", type=Path)
    elf.add_argument("--ram-start", type=lambda x: int(x, 0), required=True)
    elf.add_argument("--ram-end", type=lambda x: int(x, 0), required=True)
    command = sub.add_parser("lint-command")
    command.add_argument("argv", nargs=argparse.REMAINDER)
    mount = sub.add_parser("vivado-mount")
    mount.add_argument("mount", type=Path)
    mount.add_argument("registered_source", type=Path)
    distance = sub.add_parser("distance")
    distance.add_argument("path", type=Path)
    distance.add_argument("expected", help="comma-separated expected distance sequence")
    oracle_parser = sub.add_parser("oracle")
    oracle_parser.add_argument("path", type=Path)
    pdf = sub.add_parser("pdf-subjects")
    pdf.add_argument("--subject", action="append", required=True)
    pdf.add_argument("paths", nargs="+", type=Path)
    registration = sub.add_parser("vivado-registration")
    registration.add_argument("requested", type=Path)
    registration.add_argument("registered", type=Path)
    return parser


def _execute(args: argparse.Namespace) -> dict:
    """Dispatch one validated preflight operation."""
    if args.kind == "git-topology":
        result = git_topology(args.root, args.mode, args.outer_ref, args.inner_ref)
    elif args.kind == "spike-elf":
        result = spike_elf(args.path, args.ram_start, args.ram_end)
    elif args.kind == "lint-command":
        result = lint_command(args.argv)
    elif args.kind == "vivado-mount":
        result = vivado_executable(args.mount, args.registered_source)
    elif args.kind == "oracle":
        result = oracle(json.loads(args.path.read_text()))
    elif args.kind == "pdf-subjects":
        result = pdf_subjects(args.paths, args.subject)
    elif args.kind == "vivado-registration":
        result = canonical_registration(args.requested, args.registered)
    else:
        result = distance_rows(args.path.read_text(), [int(item) for item in args.expected.split(",")])
    return result


def main() -> None:
    parser = _parser()
    try:
        result = _execute(parser.parse_args())
    except (FixtureError, OSError, struct.error, KeyError, TypeError,
            subprocess.TimeoutExpired) as error:
        parser.exit(2, f"fixture preflight failed: {error}\n")
    print(json.dumps(result, sort_keys=True))
    if result.get("matches") is False:
        sys.exit(1)


if __name__ == "__main__":
    main()
