#!/usr/bin/env python3
"""Run reproducible serial B-Wave conversion benchmarks over named VCD corpora."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
TIME_FORMAT = "user_seconds=%U\nsystem_seconds=%S\ncpu_percent=%P\npeak_rss_kb=%M"


class BenchmarkError(ValueError):
    """Invalid benchmark configuration or failed benchmark subprocess."""


@dataclass(frozen=True)
class Corpus:
    profile: str
    path: Path
    input_bytes: int
    sha256: str


@dataclass(frozen=True)
class ProcessMetrics:
    wall_seconds: float
    user_seconds: float
    system_seconds: float
    cpu_percent: float
    peak_rss_kb: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _corpus(text: str) -> tuple[str, Path]:
    profile, separator, raw_path = text.partition("=")
    if not separator or not PROFILE_RE.fullmatch(profile):
        raise argparse.ArgumentTypeError("corpus must be SAFE_PROFILE=/path/to/input.vcd")
    if not raw_path:
        raise argparse.ArgumentTypeError("corpus path must not be empty")
    return profile, Path(raw_path)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from error
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def _nonnegative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from error
    if value < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return value


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}") from error
    if not 0 < value < float("inf"):
        raise argparse.ArgumentTypeError("value must be finite and positive")
    return value


def _load_corpora(values: list[tuple[str, Path]]) -> list[Corpus]:
    corpora: list[Corpus] = []
    seen: set[str] = set()
    for profile, path in values:
        if profile in seen:
            raise BenchmarkError(f"duplicate corpus profile: {profile}")
        if not path.is_file():
            raise BenchmarkError(f"corpus not found: {path}")
        if path.name.endswith(".gz"):
            raise BenchmarkError(f"benchmark corpus must be an uncompressed VCD: {path}")
        corpora.append(Corpus(profile, path, path.stat().st_size, _sha256(path)))
        seen.add(profile)
    return corpora


def _parse_time_metrics(path: Path, wall_seconds: float) -> ProcessMetrics:
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    try:
        return ProcessMetrics(
            wall_seconds=wall_seconds,
            user_seconds=float(fields["user_seconds"]),
            system_seconds=float(fields["system_seconds"]),
            cpu_percent=float(fields["cpu_percent"].rstrip("%")),
            peak_rss_kb=int(fields["peak_rss_kb"]),
        )
    except (KeyError, ValueError) as error:
        raise BenchmarkError(f"invalid GNU time metrics in {path}") from error


def _timed_command(command: list[str], time_bin: Path, scratch: Path) -> ProcessMetrics:
    metrics_path = scratch / "process.time"
    stderr_path = scratch / "process.stderr"
    started = time.perf_counter()
    with stderr_path.open("wb") as stderr:
        result = subprocess.run(
            [str(time_bin), "-f", TIME_FORMAT, "-o", str(metrics_path), *command],
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            check=False,
        )
    wall_seconds = time.perf_counter() - started
    if result.returncode != 0:
        detail = stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        raise BenchmarkError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{detail}"
        )
    return _parse_time_metrics(metrics_path, wall_seconds)


def _fst_section_count(path: Path) -> int:
    with path.open("rb") as stream:
        header = stream.read(73)
    if len(header) < 73 or header[0] != 0 or int.from_bytes(header[1:9], "big") != 329:
        raise BenchmarkError(f"output is not a complete FST file: {path}")
    return int.from_bytes(header[65:73], "big")


def _trial_record(metrics: ProcessMetrics, corpus: Corpus, fst_path: Path) -> dict[str, object]:
    record = asdict(metrics)
    record.update(
        {
            "input_bytes_per_second": corpus.input_bytes / metrics.wall_seconds,
            "output_bytes": fst_path.stat().st_size,
            "fst_section_count": _fst_section_count(fst_path),
        }
    )
    return record


def _run_build(
    args: argparse.Namespace, corpus: Corpus, fst_path: Path, scratch: Path
) -> dict[str, object]:
    metrics = _timed_command(
        [str(args.bwave), "build", *_build_options(args), str(corpus.path), "-o", str(fst_path)],
        args.time_bin,
        scratch,
    )
    return _trial_record(metrics, corpus, fst_path)


def _build_options(args: argparse.Namespace) -> list[str]:
    options = ["--engine", args.engine]
    if args.jobs is not None:
        options.extend(("--jobs", str(args.jobs)))
    if args.parse_jobs is not None:
        options.extend(("--parse-jobs", str(args.parse_jobs)))
    if args.encode_jobs is not None:
        options.extend(("--encode-jobs", str(args.encode_jobs)))
    if args.chunk_bytes is not None:
        options.extend(("--chunk-bytes", str(args.chunk_bytes)))
    if args.section_bytes is not None:
        options.extend(("--section-bytes", str(args.section_bytes)))
    return options


def _run_query(
    bwave: Path, fst_path: Path, pattern: str, time_bin: Path, scratch: Path
) -> dict[str, object]:
    metrics = _timed_command(
        [str(bwave), "stats", str(fst_path), "--async", "--format", "json", "-s", pattern],
        time_bin,
        scratch,
    )
    return asdict(metrics)


def _summary(trials: list[dict[str, object]]) -> dict[str, float | int]:
    rates = [float(trial["input_bytes_per_second"]) for trial in trials]
    walls = [float(trial["wall_seconds"]) for trial in trials]
    rss = [int(trial["peak_rss_kb"]) for trial in trials]
    return {
        "median_input_bytes_per_second": statistics.median(rates),
        "slowest_input_bytes_per_second": min(rates),
        "median_wall_seconds": statistics.median(walls),
        "slowest_wall_seconds": max(walls),
        "peak_rss_kb": max(rss),
    }


def _violations(
    profile: str,
    summary: dict[str, float | int],
    min_rate: float | None,
    max_slowest_ratio: float | None,
    max_peak_rss_kb: int | None,
) -> list[str]:
    failures: list[str] = []
    median_rate = float(summary["median_input_bytes_per_second"])
    slowest_wall = float(summary["slowest_wall_seconds"])
    median_wall = float(summary["median_wall_seconds"])
    if min_rate is not None and median_rate < min_rate:
        failures.append(f"{profile}: median rate {median_rate:.0f} is below {min_rate:.0f}")
    if max_slowest_ratio is not None and slowest_wall > median_wall * max_slowest_ratio:
        failures.append(
            f"{profile}: slowest/median wall ratio {slowest_wall / median_wall:.3f} "
            f"exceeds {max_slowest_ratio:.3f}"
        )
    if max_peak_rss_kb is not None and int(summary["peak_rss_kb"]) >= max_peak_rss_kb:
        failures.append(
            f"{profile}: peak RSS {int(summary['peak_rss_kb'])} KiB is not below "
            f"{max_peak_rss_kb} KiB"
        )
    return failures


def _load_baseline(path: Path | None) -> dict[str, dict[str, object]]:
    if path is None:
        return {}
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        records = report["corpora"]
        return {str(record["profile"]): record for record in records}
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise BenchmarkError(f"invalid serial baseline report: {path}") from error


def _comparison_violations(
    profile: str,
    record: dict[str, object],
    baseline: dict[str, object] | None,
    max_output_ratio: float,
    max_query_ratio: float,
) -> list[str]:
    if baseline is None:
        return []
    failures = []
    output_bytes = int(record["trials"][0]["output_bytes"])
    baseline_output = int(baseline["trials"][0]["output_bytes"])
    output_ratio = output_bytes / baseline_output
    if output_ratio > max_output_ratio:
        failures.append(
            f"{profile}: output-size ratio {output_ratio:.3f} exceeds {max_output_ratio:.3f}"
        )
    queries = record["query"]["trials"]
    baseline_queries = baseline["query"]["trials"]
    if queries and baseline_queries:
        query_ratio = statistics.median(float(item["wall_seconds"]) for item in queries) / (
            statistics.median(float(item["wall_seconds"]) for item in baseline_queries)
        )
        if query_ratio > max_query_ratio:
            failures.append(
                f"{profile}: query-time ratio {query_ratio:.3f} exceeds {max_query_ratio:.3f}"
            )
    elif queries or baseline_queries:
        failures.append(f"{profile}: candidate and baseline query trial counts are inconsistent")
    return failures


def _benchmark_corpus(
    args: argparse.Namespace,
    corpus: Corpus,
    scratch: Path,
    baseline: dict[str, object] | None,
) -> tuple[dict[str, object], list[str]]:
    fst_path = scratch / f"{corpus.profile}.fst"
    for _ in range(args.warmups):
        _run_build(args, corpus, fst_path, scratch)
    trials = [_run_build(args, corpus, fst_path, scratch) for _ in range(args.trials)]
    queries = [
        _run_query(args.bwave, fst_path, args.query_pattern, args.time_bin, scratch)
        for _ in range(args.query_trials)
    ]
    summary = _summary(trials)
    record = {
        "profile": corpus.profile,
        "input": {
            "file": corpus.path.name,
            "bytes": corpus.input_bytes,
            "sha256": corpus.sha256,
        },
        "trials": trials,
        "summary": summary,
        "query": {"command": "stats --async", "pattern": args.query_pattern, "trials": queries},
    }
    failures = _violations(
        corpus.profile,
        summary,
        args.min_bytes_per_second,
        args.max_slowest_ratio,
        args.max_peak_rss_kb,
    )
    failures.extend(
        _comparison_violations(
            corpus.profile,
            record,
            baseline,
            args.max_output_ratio,
            args.max_query_ratio,
        )
    )
    return record, failures


def _bwave_version(path: Path) -> str:
    result = subprocess.run([str(path), "--version"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise BenchmarkError(f"cannot execute B-Wave binary: {path}")
    return result.stdout.strip()


def benchmark(args: argparse.Namespace) -> tuple[dict[str, object], bool]:
    if not args.bwave.is_file():
        raise BenchmarkError(f"B-Wave binary not found: {args.bwave}")
    if not args.time_bin.is_file():
        raise BenchmarkError(f"GNU time binary not found: {args.time_bin}")
    corpora = _load_corpora(args.corpus)
    baseline = _load_baseline(args.baseline)
    missing_baselines = [
        corpus.profile
        for corpus in corpora
        if args.baseline is not None and corpus.profile not in baseline
    ]
    if missing_baselines:
        raise BenchmarkError(
            "serial baseline is missing profile(s): " + ", ".join(missing_baselines)
        )
    records: list[dict[str, object]] = []
    failures: list[str] = []
    scratch_parent = args.scratch_dir
    if scratch_parent is not None:
        scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bwave-benchmark-", dir=scratch_parent) as temp:
        scratch = Path(temp)
        for corpus in corpora:
            record, corpus_failures = _benchmark_corpus(
                args, corpus, scratch, baseline.get(corpus.profile)
            )
            records.append(record)
            failures.extend(corpus_failures)
    report = _report(args, records, failures)
    return report, not failures


def _report(
    args: argparse.Namespace, records: list[dict[str, object]], failures: list[str]
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": f"bwave_{args.engine}_vcd_benchmark",
        "bwave_version": _bwave_version(args.bwave),
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "logical_cpu_count": os.cpu_count(),
        },
        "settings": {
            "warmups": args.warmups,
            "trials": args.trials,
            "query_trials": args.query_trials,
            "engine": args.engine,
            "jobs": args.jobs,
            "parse_jobs": args.parse_jobs,
            "encode_jobs": args.encode_jobs,
            "chunk_bytes": args.chunk_bytes,
            "section_bytes": args.section_bytes,
            "min_bytes_per_second": args.min_bytes_per_second,
            "max_slowest_ratio": args.max_slowest_ratio,
            "max_peak_rss_kb": args.max_peak_rss_kb,
            "baseline": args.baseline.name if args.baseline is not None else None,
            "max_output_ratio": args.max_output_ratio,
            "max_query_ratio": args.max_query_ratio,
        },
        "corpora": records,
        "gate": {"passed": not failures, "violations": failures},
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bwave", type=Path, required=True)
    parser.add_argument("--corpus", type=_corpus, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warmups", type=_nonnegative_int, default=1)
    parser.add_argument("--trials", type=_positive_int, default=5)
    parser.add_argument("--query-trials", type=_nonnegative_int, default=1)
    parser.add_argument("--query-pattern", default="*")
    parser.add_argument("--engine", choices=("serial", "parallel"), default="parallel")
    parser.add_argument("--jobs", type=_positive_int)
    parser.add_argument("--parse-jobs", type=_positive_int)
    parser.add_argument("--encode-jobs", type=_positive_int)
    parser.add_argument("--chunk-bytes", type=_positive_int)
    parser.add_argument("--section-bytes", type=_positive_int)
    parser.add_argument("--min-bytes-per-second", type=_positive_float)
    parser.add_argument("--max-slowest-ratio", type=_positive_float)
    parser.add_argument("--max-peak-rss-kb", type=_positive_int, default=1024 * 1024)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--max-output-ratio", type=_positive_float, default=1.10)
    parser.add_argument("--max-query-ratio", type=_positive_float, default=1.10)
    parser.add_argument("--scratch-dir", type=Path)
    parser.add_argument("--time-bin", type=Path, default=Path("/usr/bin/time"))
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        report, passed = benchmark(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (BenchmarkError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if not passed:
        for violation in report["gate"]["violations"]:
            print(f"ERROR: acceptance gate missed: {violation}", file=sys.stderr)
        return 2
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
