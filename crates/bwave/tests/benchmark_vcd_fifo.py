#!/usr/bin/env python3
"""Benchmark a VCD producer through a FIFO with trivial and B-Wave consumers."""

from __future__ import annotations

import argparse
import errno
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

import benchmark_vcd as regular


@dataclass
class TimedProcess:
    process: subprocess.Popen[bytes]
    started: float
    metrics_path: Path
    stderr_path: Path


def _start_timed(
    command: list[str],
    time_bin: Path,
    scratch: Path,
    label: str,
    stdout: int | BinaryIO,
) -> TimedProcess:
    metrics_path = scratch / f"{label}.time"
    stderr_path = scratch / f"{label}.stderr"
    stderr = stderr_path.open("wb")
    try:
        process = subprocess.Popen(
            [str(time_bin), "-f", regular.TIME_FORMAT, "-o", str(metrics_path), *command],
            stdout=stdout,
            stderr=stderr,
        )
    finally:
        stderr.close()
    return TimedProcess(process, time.perf_counter(), metrics_path, stderr_path)


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _collect(timed: TimedProcess, deadline: float) -> regular.ProcessMetrics:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise regular.BenchmarkError("FIFO trial timed out")
    try:
        returncode = timed.process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as error:
        raise regular.BenchmarkError("FIFO trial timed out") from error
    wall_seconds = time.perf_counter() - timed.started
    if returncode != 0:
        detail = timed.stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        raise regular.BenchmarkError(f"FIFO command failed ({returncode}): {detail.strip()}")
    return regular._parse_time_metrics(timed.metrics_path, wall_seconds)


def _open_fifo_writer(path: Path, consumer: subprocess.Popen[bytes], timeout: float) -> int:
    deadline = time.monotonic() + timeout
    for _ in range(max(1, int(timeout * 100))):
        if consumer.poll() is not None:
            raise regular.BenchmarkError("FIFO consumer exited before opening the pipe")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            os.set_blocking(descriptor, True)
            return descriptor
        except OSError as error:
            if error.errno != errno.ENXIO:
                raise
        if time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    raise regular.BenchmarkError("FIFO consumer did not open the pipe before timeout")


def _run_pair(
    args: argparse.Namespace,
    corpus: regular.Corpus,
    scratch: Path,
    label: str,
    consumer_command: list[str],
) -> tuple[regular.ProcessMetrics, regular.ProcessMetrics]:
    fifo_path = scratch / f"{label}.fifo"
    os.mkfifo(fifo_path)
    command = [part.replace("{fifo}", str(fifo_path)) for part in consumer_command]
    consumer = _start_timed(
        command, args.time_bin, scratch, f"{label}-consumer", subprocess.DEVNULL
    )
    producer: TimedProcess | None = None
    try:
        descriptor = _open_fifo_writer(fifo_path, consumer.process, args.timeout_seconds)
        with os.fdopen(descriptor, "wb", buffering=0) as fifo:
            producer = _start_timed(
                [str(args.producer_bin), str(corpus.path)],
                args.time_bin,
                scratch,
                f"{label}-producer",
                fifo,
            )
        deadline = time.monotonic() + args.timeout_seconds
        producer_metrics = _collect(producer, deadline)
        consumer_metrics = _collect(consumer, deadline)
        return producer_metrics, consumer_metrics
    finally:
        if producer is not None:
            _stop(producer.process)
        _stop(consumer.process)
        fifo_path.unlink(missing_ok=True)


def _pair_record(
    corpus: regular.Corpus,
    producer: regular.ProcessMetrics,
    consumer: regular.ProcessMetrics,
) -> dict[str, object]:
    return {
        "producer": {
            **asdict(producer),
            "input_bytes_per_second": corpus.input_bytes / producer.wall_seconds,
        },
        "consumer": {
            **asdict(consumer),
            "input_bytes_per_second": corpus.input_bytes / consumer.wall_seconds,
        },
    }


def _fifo_summary(trials: list[dict[str, object]]) -> dict[str, float | int]:
    producer_rates = [float(trial["producer"]["input_bytes_per_second"]) for trial in trials]
    consumer_rates = [float(trial["consumer"]["input_bytes_per_second"]) for trial in trials]
    consumer_walls = [float(trial["consumer"]["wall_seconds"]) for trial in trials]
    consumer_rss = [int(trial["consumer"]["peak_rss_kb"]) for trial in trials]
    return {
        "median_producer_bytes_per_second": statistics.median(producer_rates),
        "slowest_producer_bytes_per_second": min(producer_rates),
        "median_consumer_bytes_per_second": statistics.median(consumer_rates),
        "slowest_consumer_bytes_per_second": min(consumer_rates),
        "median_consumer_wall_seconds": statistics.median(consumer_walls),
        "slowest_consumer_wall_seconds": max(consumer_walls),
        "peak_consumer_rss_kb": max(consumer_rss),
    }


def _run_trials(
    args: argparse.Namespace,
    corpus: regular.Corpus,
    scratch: Path,
    kind: str,
    command: list[str],
) -> tuple[list[dict[str, object]], dict[str, float | int]]:
    for index in range(args.warmups):
        _run_pair(args, corpus, scratch, f"{corpus.profile}-{kind}-warmup-{index}", command)
    trials = []
    for index in range(args.trials):
        producer, consumer = _run_pair(
            args, corpus, scratch, f"{corpus.profile}-{kind}-{index}", command
        )
        trials.append(_pair_record(corpus, producer, consumer))
    return trials, _fifo_summary(trials)


def _gate_corpus(
    profile: str,
    trivial: dict[str, float | int],
    converter: dict[str, float | int],
    args: argparse.Namespace,
) -> list[str]:
    failures = []
    producer_rate = float(trivial["slowest_producer_bytes_per_second"])
    converter_rate = float(converter["median_consumer_bytes_per_second"])
    if producer_rate < args.min_producer_bytes_per_second:
        failures.append(
            f"{profile}: slowest trivial-reader producer rate {producer_rate:.0f} "
            f"is below {args.min_producer_bytes_per_second:.0f}"
        )
    if converter_rate < args.min_converter_bytes_per_second:
        failures.append(
            f"{profile}: median FIFO converter rate {converter_rate:.0f} "
            f"is below {args.min_converter_bytes_per_second:.0f}"
        )
    ratio = float(converter["slowest_consumer_wall_seconds"]) / float(
        converter["median_consumer_wall_seconds"]
    )
    if ratio > args.max_slowest_ratio:
        failures.append(
            f"{profile}: FIFO slowest/median wall ratio {ratio:.3f} "
            f"exceeds {args.max_slowest_ratio:.3f}"
        )
    peak_rss = int(converter["peak_consumer_rss_kb"])
    if peak_rss >= args.max_peak_rss_kb:
        failures.append(
            f"{profile}: peak converter RSS {peak_rss} KiB is not below "
            f"{args.max_peak_rss_kb} KiB"
        )
    return failures


def _benchmark_corpus(
    args: argparse.Namespace, corpus: regular.Corpus, scratch: Path
) -> tuple[dict[str, object], list[str]]:
    fst_path = scratch / f"{corpus.profile}.fst"
    trivial_trials, trivial_summary = _run_trials(
        args, corpus, scratch, "trivial", [str(args.reader_bin), "{fifo}"]
    )
    converter_trials, converter_summary = _run_trials(
        args,
        corpus,
        scratch,
        "converter",
        [
            str(args.bwave),
            "build",
            *regular._build_options(args),
            "--input",
            "{fifo}",
            "-o",
            str(fst_path),
        ],
    )
    queries = [
        regular._run_query(args.bwave, fst_path, args.query_pattern, args.time_bin, scratch)
        for _ in range(args.query_trials)
    ]
    converter_summary["output_bytes"] = fst_path.stat().st_size
    converter_summary["fst_section_count"] = regular._fst_section_count(fst_path)
    failures = _gate_corpus(corpus.profile, trivial_summary, converter_summary, args)
    record = {
        "profile": corpus.profile,
        "input": {"file": corpus.path.name, "bytes": corpus.input_bytes, "sha256": corpus.sha256},
        "trivial_reader": {"trials": trivial_trials, "summary": trivial_summary},
        "converter": {"trials": converter_trials, "summary": converter_summary},
        "query": {"command": "stats --async", "pattern": args.query_pattern, "trials": queries},
    }
    baseline = args.baseline_records.get(corpus.profile)
    candidate = {
        "trials": [{"output_bytes": converter_summary["output_bytes"]}],
        "query": record["query"],
    }
    failures.extend(
        regular._comparison_violations(
            corpus.profile,
            candidate,
            baseline,
            args.max_output_ratio,
            args.max_query_ratio,
        )
    )
    return record, failures


def benchmark(args: argparse.Namespace) -> tuple[dict[str, object], bool]:
    for label, path in (
        ("B-Wave", args.bwave),
        ("GNU time", args.time_bin),
        ("producer", args.producer_bin),
        ("trivial reader", args.reader_bin),
    ):
        if not path.is_file():
            raise regular.BenchmarkError(f"{label} binary not found: {path}")
    corpora = regular._load_corpora(args.corpus)
    args.baseline_records = regular._load_baseline(args.baseline)
    missing_baselines = [
        corpus.profile
        for corpus in corpora
        if args.baseline is not None and corpus.profile not in args.baseline_records
    ]
    if missing_baselines:
        raise regular.BenchmarkError(
            "serial baseline is missing profile(s): " + ", ".join(missing_baselines)
        )
    records: list[dict[str, object]] = []
    failures: list[str] = []
    if args.scratch_dir is not None:
        args.scratch_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bwave-fifo-benchmark-", dir=args.scratch_dir) as temp:
        for corpus in corpora:
            record, corpus_failures = _benchmark_corpus(args, corpus, Path(temp))
            records.append(record)
            failures.extend(corpus_failures)
    return _report(args, records, failures), not failures


def _report(
    args: argparse.Namespace, records: list[dict[str, object]], failures: list[str]
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "bwave_fifo_vcd_benchmark",
        "bwave_version": regular._bwave_version(args.bwave),
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
            "timeout_seconds": args.timeout_seconds,
            "min_producer_bytes_per_second": args.min_producer_bytes_per_second,
            "min_converter_bytes_per_second": args.min_converter_bytes_per_second,
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
    parser.add_argument("--corpus", type=regular._corpus, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--warmups", type=regular._nonnegative_int, default=1)
    parser.add_argument("--trials", type=regular._positive_int, default=5)
    parser.add_argument("--query-trials", type=regular._nonnegative_int, default=1)
    parser.add_argument("--query-pattern", default="*")
    parser.add_argument("--engine", choices=("serial", "parallel"), default="parallel")
    parser.add_argument("--jobs", type=regular._positive_int)
    parser.add_argument("--parse-jobs", type=regular._positive_int)
    parser.add_argument("--encode-jobs", type=regular._positive_int)
    parser.add_argument("--chunk-bytes", type=regular._positive_int)
    parser.add_argument("--section-bytes", type=regular._positive_int)
    parser.add_argument("--timeout-seconds", type=regular._positive_float, default=600.0)
    parser.add_argument(
        "--min-producer-bytes-per-second", type=regular._positive_float, default=1.2e9
    )
    parser.add_argument(
        "--min-converter-bytes-per-second", type=regular._positive_float, default=1.0e9
    )
    parser.add_argument("--max-slowest-ratio", type=regular._positive_float, default=1.10)
    parser.add_argument("--max-peak-rss-kb", type=regular._positive_int, default=1024 * 1024)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--max-output-ratio", type=regular._positive_float, default=1.10)
    parser.add_argument("--max-query-ratio", type=regular._positive_float, default=1.10)
    parser.add_argument("--scratch-dir", type=Path)
    parser.add_argument("--time-bin", type=Path, default=Path("/usr/bin/time"))
    parser.add_argument("--producer-bin", type=Path, default=Path("/bin/cat"))
    parser.add_argument("--reader-bin", type=Path, default=Path("/bin/cat"))
    return parser


def main() -> int:
    if os.name != "posix":
        print("ERROR: FIFO benchmarks require a POSIX host", file=sys.stderr)
        return 1
    args = _parser().parse_args()
    try:
        report, passed = benchmark(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (regular.BenchmarkError, OSError) as error:
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
