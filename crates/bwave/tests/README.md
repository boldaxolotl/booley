# bwave test suite

All tests for the `bwave` crate live here, crate-local by design — they share the
fixtures in this directory and validate the Rust binary built from `../src`. They are
**not** collected by the repository's top-level `pytest` run (`testpaths = ["tests"]`
in the root `pyproject.toml`), nor by anything in the top-level `tests/` tree.

## Layout

| Path                              | Kind                | How to run |
|-----------------------------------|---------------------|------------|
| `integration_test.rs`             | Rust integration    | `cargo test` |
| `cli_issues.rs`                   | Rust regression     | `cargo test` |
| `property_tests.rs`               | Rust property tests (VCD→FST→query round-trip vs an independent model) | `cargo test` |
| `parallel_build.rs`               | Serial/parallel fixture-matrix differential and determinism checks      | `cargo test --test parallel_build` |
| `simulator_ground_truth_test.py`  | Python cross-check (Icarus/Verilator oracle vs. bwave) | `pytest tests/simulator_ground_truth_test.py` |
| `test_real_trace_fixtures.py`     | Python fixture check (compressed real VCDs)            | `pytest tests/test_real_trace_fixtures.py` |
| `test_vcd_corpus.py`              | Excerpt/replay correctness and determinism              | `pytest tests/test_vcd_corpus.py` |
| `test_benchmark_vcd.py`           | Benchmark schema, metrics, and acceptance-gate behavior | `pytest tests/test_benchmark_vcd.py` |
| `test_benchmark_vcd_fifo.py`      | FIFO producer proof, converter metrics, and gates       | `pytest tests/test_benchmark_vcd_fifo.py` |
| `native_fst_verilator_test.py`    | Native Verilator FST vs VCD+convert differential        | `pytest tests/native_fst_verilator_test.py` |
| `vcd_corpus.py`                   | Capture real timestamp windows and replay benchmark corpora | see below |
| `benchmark_vcd.py`                | Run named serial corpora and write a JSON result record | see below |
| `benchmark_vcd_fifo.py`           | Prove producer speed and benchmark the same corpus through a FIFO | see below |
| `gen_test_vcds.py`                | Fixture generator (writes synthetic VCDs to `fixtures/`) | run directly |
| `fixtures/`                       | Tracked `.vcd` test inputs (incl. the `test_*.vcd` used by the Rust tests) | — |
| `rtl_fixtures/`                   | Verilog DUT + testbench sources for the ground-truth oracle | — |

## Why these aren't in the repo's `tests/` tree

The Python cross-validation tests are tightly coupled to this crate's fixture layout
(`fixtures/`, `rtl_fixtures/`, resolved relative to each file) and require RTL
simulators (Icarus/Verilator) plus the built `bwave` binary. They are run on demand
during bwave development, not as part of the Booley Python unit suite. Keeping them
beside the crate they exercise avoids splitting the fixtures and keeps the EDA tool
self-contained.

## Real-trace corpus tool

Capture named initialization and steady-state windows separately. The source
label is deliberately a safe name rather than a path, so the generated manifest
does not disclose a developer workspace:

```console
python3 tests/vcd_corpus.py capture /path/to/ibex.vcd \
  --output tests/fixtures/real_trace/ibex-ordinary.vcd.gz \
  --start 100000 --end 200000 --profile ordinary \
  --source-label ibex-small-retained

python3 tests/vcd_corpus.py capture /path/to/ibex.vcd \
  --output tests/fixtures/real_trace/ibex-high.vcd.gz \
  --start 400000 --end 500000 --profile high \
  --source-label ibex-small-retained
```

Each excerpt preserves the complete source header and original event lines. At
the first selected timestamp it emits the incoming value of every unique VCD ID
before replaying that timestamp's events. The deterministic gzip and adjacent
`.manifest.json` record source/output hashes, widths, line classes, and activity.
For very large, known-monotonic Verilator traces without dump-control changes,
`--trusted-verilator-tail` counts and hashes the post-window tail in bulk after
the semantic excerpt is complete. The manifest records that scan mode, and the
tool rejects a trusted tail if it encounters `$dumpoff` or `$dumpon`.

Expand an excerpt locally without checking the large output into Git:

```console
python3 tests/vcd_corpus.py replay \
  tests/fixtures/real_trace/ibex-ordinary.vcd.gz \
  --output /tmp/ibex-ordinary-1g.vcd --target-bytes 1073741824
```

Replay always writes complete windows, so the output can exceed the requested
byte target by less than one excerpt window. Timestamps are offset monotonically,
the initialized frame appears once, and the corpus manifest records the final
SHA-256. Inspect captured headers and manifests for confidential content before
committing them. Existing outputs are refused unless `--force` is explicit.

Run every generated profile through the serial converter in one command. GNU
`time` supplies per-process CPU and peak-RSS metrics; use a memory-backed
`--scratch-dir` when measuring converter CPU without storage effects:

```console
python3 tests/benchmark_vcd.py \
  --bwave target/release/bwave \
  --corpus ordinary=/tmp/ibex-ordinary-1g.vcd \
  --corpus high=/tmp/ibex-high-1g.vcd \
  --corpus xcelium=/tmp/xcelium-replay.vcd \
  --output /tmp/bwave-serial-baseline.json \
  --engine serial \
  --scratch-dir /dev/shm \
  --query-pattern '*' \
  --min-bytes-per-second 1000000000 \
  --max-slowest-ratio 1.10
```

Then pass the serial JSON to the parallel run so output size and query time
are gated as ratios rather than inspected manually:

```console
python3 tests/benchmark_vcd.py \
  --bwave target/release/bwave \
  --corpus ordinary=/tmp/ibex-ordinary-1g.vcd \
  --corpus high=/tmp/ibex-high-1g.vcd \
  --output /tmp/bwave-parallel.json \
  --engine parallel \
  --baseline /tmp/bwave-serial-baseline.json \
  --min-bytes-per-second 1000000000 \
  --max-slowest-ratio 1.10
```

Defaults are one warmup, five measured builds, and one asynchronous `stats`
query per profile. The JSON records input hashes, wall/user/system time, CPU,
peak RSS, output size, FST section count, query timing, median and slowest rates,
host characteristics, and all configured gates. Peak RSS must stay below 1 GiB
by default. With `--baseline`, output must be at most 1.10× the serial result and
the median query must be at most 1.10× the serial query; both ratios are
configurable. A missed gate still writes the evidence file, prints each
violation, and exits 2.

The benchmark runners and production `bwave build` default to the promoted
parallel converter. Pass `--engine serial` when collecting the semantic oracle,
a performance baseline, or diagnostic evidence. Optional
`--jobs`, optional `--parse-jobs`/`--encode-jobs` split overrides, the opt-in
`--pack-jobs` per-signal compression override, `--chunk-bytes`, and
`--section-bytes` are recorded in the evidence and
forwarded to hidden developer controls on `bwave build`; they are not supported
user surface. The promoted defaults cap the worker budget at six and select four
parsers, one encoder, and two packers when at least six logical CPUs are
available. Timestamp-aligned parse chunks target 4 MiB and estimated-uncompressed
FST sections target 128 MiB. Smaller hosts scale the worker topology down.
Experimental splits are rejected when parser plus packer concurrency exceeds
`--jobs`.
Section sizing follows the accumulated FST stream rather than raw VCD bytes, so
signal activity does not multiply the section count. The hidden `--engine
serial` control remains available for differential diagnosis and benchmark
baselines.

Run the matching named-pipe procedure with the same generated corpus:

```console
python3 tests/benchmark_vcd_fifo.py \
  --bwave target/release/bwave \
  --corpus ordinary=/tmp/ibex-ordinary-1g.vcd \
  --corpus high=/tmp/ibex-high-1g.vcd \
  --output /tmp/bwave-fifo-baseline.json \
  --engine parallel \
  --baseline /tmp/bwave-serial-baseline.json \
  --scratch-dir /dev/shm
```

For each profile, this first times `/bin/cat` writing the source through a
fresh FIFO to an independent trivial `/bin/cat` reader. The slowest producer
trial must exceed 1.2 GB/s by default. It then repeats with `bwave build
--input FIFO` as the consumer and requires a median 1.0 GB/s converter rate.
Both sides' wall/user/system time, CPU, and RSS are retained, together with FST
size, section count, query timing, stability, host data, and input hash. Defaults
are one warmup and five trials for both the proof and conversion. A per-trial
timeout ensures a failed consumer cannot leave the producer blocked forever.
The FIFO runner applies the same RSS and optional serial output/query gates as
the regular-file runner.
