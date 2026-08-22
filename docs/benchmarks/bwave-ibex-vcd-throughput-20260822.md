# B-Wave Ibex VCD throughput — 2026-08-22

## Outcome

The parallel converter clears the core regular-file and FIFO throughput gates,
and serial/parallel query results are semantically equivalent on every captured
Ibex excerpt. The full acceptance set does **not** pass:

- regular-file FST size and query-time ratios fail for the repetitive Ibex
  `ordinary` and `high` replay profiles;
- FIFO `ordinary` stability is 1.143x, above the 1.10x limit;
- on three live OpenTitan pairs written directly to durable project storage,
  parallel median wall time is 1.058x serial, stability is 1.263x, and the FST
  is 1.300x the serial result.

The implementation should therefore remain a draft until the production
OpenTitan and output/query regressions are resolved or the acceptance contract
is explicitly changed.

## Environment and evidence

- Candidate binary: `bwave 0.2.0`, SHA-256
  `f865a8403f66dabbc68d919517430a101fa3919210808c524785c10c9270bbe1`.
- Candidate implementation commit used for the binary: `4070aab`. PR commit
  `d06ac6f` has the same production call paths; its delta adds Criterion
  workloads and scanner/lookup/stat helpers referenced only by benchmarks.
- Host: Linux x86-64, 24 logical CPUs.
- Session image: `cb6bb43fa203`; Verilator `5.046`.
- Workload: Ibex CoreMark, with every simulator-coupled run required to contain
  `// TEST PASSED //` and an executed-cycle count.
- Durable machine-readable evidence lives in the Ibex project at
  `.booley_project/handoffs/bwave-vcd-throughput-20260822/`.

The raw sources are stored compressed because the OpenTitan VCD expands to
91.35 GB. Both gzip streams passed full integrity checks. The checked-in
excerpt manifests retain the complete uncompressed byte count and SHA-256.

| Target | Stored bytes | Stored SHA-256 | Raw bytes | Raw SHA-256 |
|---|---:|---|---:|---|
| small | 3,023,159,464 | `ddf9f9816132a1c1ec165135846c981515c4409d569afc66d903844cee6fe9b3` | 17,763,580,128 | `599f59bbe8f9f0863ac739d0b2637b9fd3cf07b7f5ba6d748ea2201935c0cbc4` |
| opentitan | 19,937,988,980 | `97d96fe49007469d43d7a9d1818bb1e04613962befbb24b70c72494842e1be21` | 91,348,171,298 | `63a001723f0e0674c3dc3c7452889d3cac5896323cbeeb5189a2ab1dc0a13a03` |

## Checked-in semantic excerpts

| Profile | Target/window | Declarations / unique IDs | Raw excerpt | Selected events | Compressed SHA-256 |
|---|---|---:|---:|---:|---|
| initialization | small, ticks 0–5,000 | 4,243 / 1,768 | 11,047,585 B | 400,184 | `5a1218b08eff4c1d3029e7f79f3c6b8a5f7ced387cbe9d902b6286bf2c481d6e` |
| ordinary | small, ticks 1,000,000–1,010,000 | 4,243 / 1,768 | 22,335,154 B | 778,681 | `96eed8c7429475484b7ed75ac290cb6ea59364db90b2063a71a7ba3e01fdd104` |
| high | opentitan, ticks 0–20 | 12,207 / 6,142 | 903,935 B | 9,680 | `a343570f14d75dd5d77a61e1661ef72701a538983dae6ed393efa503d0e5c62e` |

The large-source capture path hashes and counts the full decompressed stream.
After the selected window, the trusted Verilator-tail mode aggregates complete
monotonic lines in 16 MiB blocks and rejects dump-control changes. Its source
statistics are regression-tested against the full semantic scan.

Each excerpt was decompressed, hashed, built with both engines, and compared by
JSON `list` and asynchronous all-signal `stats` queries. All comparisons pass.

## At-least-1-GiB replay benchmarks

Procedure: one warmup, five measured conversions, one asynchronous all-signal
query, serial first and parallel second. Rates use decimal bytes/second. Peak
RSS stayed below the default 1 GiB cap.

| Profile | Bytes | Serial median / slowest | Parallel median / slowest | Median speedup | Parallel peak RSS |
|---|---:|---:|---:|---:|---:|
| ordinary | 1,082,616,034 | 356.1 / 352.4 MB/s | 1.624 / 1.547 GB/s | 4.56x | 474 MiB |
| high | 1,073,748,019 | 436.1 / 432.0 MB/s | 1.756 / 1.658 GB/s | 4.03x | 377 MiB |
| xcelium | 1,073,758,940 | 208.4 / 207.1 MB/s | 1.250 / 1.210 GB/s | 6.00x | 718 MiB |

All parallel median and slowest rates exceed 1.0 GB/s. The broader parallel
gate fails as follows:

| Profile | FST-size ratio | Query-time ratio | Result |
|---|---:|---:|---|
| ordinary | 1.675x | 1.121x | fail / fail |
| high | 2.995x | 1.712x | fail / fail |
| xcelium | 1.059x | 1.017x | pass / pass |

No control was tuned after observing these failures. The failed evidence file
was retained.

## FIFO replay benchmarks

Procedure: for each corpus, one warmup and five measured `/bin/cat` producer
proofs, then one warmup and five measured parallel B-Wave FIFO conversions.

| Profile | Slowest producer | Converter median / slowest | Stability | Peak converter RSS |
|---|---:|---:|---:|---:|
| ordinary | 34.35 GB/s | 1.568 / 1.371 GB/s | 1.143x | 443 MiB |
| high | 34.19 GB/s | 1.669 / 1.554 GB/s | 1.074x | 355 MiB |
| xcelium | 34.16 GB/s | 1.140 / 1.083 GB/s | 1.053x | 689 MiB |

Producer and median converter gates pass for every profile. `ordinary`
stability exceeds 1.10x, and its/high output and query ratios repeat the
regular-file failures.

## Live Ibex FIFO campaigns

Each primary target used one sink/serial/parallel warmup followed by three
alternating measured sink/serial/parallel pairs. Simulator and converter
wall/user/system time and RSS were collected independently. The sink controls
quantify live Verilator generation; inferred backpressure is simulator wall
time minus measured sink median.

These campaigns deliberately wrote the retained FST directly to durable Ibex
project storage. Booley's normal Flow instead converts in memory-backed
`/tmp/bwave` and publishes afterward, so the direct campaign also exposes
storage-path sensitivity.

| Target/mode | Median pipeline wall | Median rate | Slowest rate | Stability | Peak RSS | Inferred backpressure |
|---|---:|---:|---:|---:|---:|---:|
| small sink | 17.64 s | — | — | — | — | — |
| small serial | 153.86 s | 115.45 MB/s | 114.08 MB/s | 1.012x | 345 MiB | 135.66 s |
| small parallel | 70.94 s | 250.39 MB/s | 200.51 MB/s | 1.249x | 335 MiB | 53.19 s |
| opentitan sink | 79.31 s | — | — | — | — | — |
| opentitan serial | 846.07 s | 107.97 MB/s | 105.19 MB/s | 1.026x | 433 MiB | 766.42 s |
| opentitan parallel | 895.37 s | 102.02 MB/s | 80.77 MB/s | 1.263x | 353 MiB | 815.95 s |

Small parallel is 2.17x faster by median wall time, but misses the stability
gate. OpenTitan parallel is 5.8% slower by median wall time and misses
stability. All measured runs pass CoreMark: small at 4,149,572 cycles and
OpenTitan at 3,237,588 cycles.

The retained small serial/parallel FSTs are 547,537,453 / 573,981,730 bytes
(1.048x). OpenTitan serial/parallel are 2,295,480,774 / 2,984,056,610 bytes
(1.300x). Section counts are 10 / 498 and 50 / 2,526 respectively.

## Normal traced Booley Flows

The Flow resolver was checked directly and selected the candidate binary/hash
listed above. These runs convert into memory-backed `/tmp/bwave` before
publication.

| Target | Complete Flow | Build included | FST | Result |
|---|---:|---:|---:|---|
| small | 68.9 s | 48 s | 547.4 MiB | PASS, 0 SVA errors |
| opentitan | 207.0 s | 113 s | 2.8 GiB | PASS, 0 SVA errors |
| maxperf | 74.7 s | 54 s | 570.4 MiB | PASS, 0 SVA errors |
| maxperf-pmp-bmbalanced | 98.6 s | 72 s | 717.5 MiB | PASS, 0 SVA errors |

The secondary configurations show no functional regression. Their traced
runtime direction is consistent with `small`; the high-signal OpenTitan target
is the outlier in the durable-storage campaign.

## Validation

- `ruff check src/ tests/`: pass.
- `ruff check crates/bwave/tests/`: pass.
- `cargo test --all-targets`: 357 pass, 1 pre-existing ignored; property and
  serial/parallel differential tests pass; all Criterion smoke targets pass.
- `pytest crates/bwave/tests`: 38 pass, 50 environment-dependent skips.
- Checked-in Ibex fixture equivalence: 3/3 pass.

Instrumentation limitation: per-stage parser/encoder CPU and maximum in-flight
IR bytes are bounded by the implementation but are not exposed. FIFO blocked
time is inferred from the sink controls rather than directly instrumented.
