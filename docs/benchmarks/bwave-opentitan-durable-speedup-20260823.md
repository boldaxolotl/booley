# B-Wave OpenTitan durable-output speedup — 23 AUG 2026

> **Superseded:** parser-produced fragments and corrected CPU affinity later
> passed the sustained file, FIFO, memory, size, and query gates. The parallel
> engine is now the production default. This report remains the historical
> evidence for the rejected intermediate topology; see
> [B-Wave VCD streaming acceptance](bwave-vcd-streaming-acceptance-20260824.md).

## Outcome

Durable publication and structural section formation are implemented, but the
parallel converter remains a draft. Under a four-core/four-worker limit, the
retained 1 GiB replay gate passes stability, output size, query time, and RSS,
but reaches only 0.402 GB/s on `ordinary` and 0.519 GB/s on OpenTitan `high`.
Both are below the 1.0 GB/s diagnostic gate and the roughly 1.15 GB/s rate
needed to stay near the OpenTitan sink.

The machine-readable result is
[`bwave-opentitan-durable-speedup-20260823.json`](bwave-opentitan-durable-speedup-20260823.json).
The authoritative earlier evidence remains in the Ibex handoff named in the
implementation handoff; it was reused rather than copied.

## Changes evaluated

- Reclaimed 3.2 GiB of reproducible Cargo and Python cache output before work.
- Ported the candidate timestamp-aligned parallel parser and deterministic
  ordered writer.
- Replaced raw-VCD section sizing with accumulated estimated uncompressed FST
  stream sizing.
- Streamed reconciled IR chunks to section workers instead of retaining an
  expanded section-sized IR batch. This cut sweep RSS from 1.18–1.22 GiB to
  265–368 MiB while allowing large sections.
- Changed the four-worker default to three parser workers and one encoder
  worker, with 8 MiB VCD chunks and 128 MiB FST-stream sections.
- Added stage wall attribution, encoder-queue blocking, queue high-water, IR
  bytes, input bytes, writer time, process CPU/RSS, output bytes, section count,
  and effective CPU affinity.
- Added atomic durable publication through a preallocated sibling temporary
  file, 8 MiB sequential copies, `fdatasync`, atomic replace, and destination
  directory sync. Publication failures retain the valid memory-backed cache.

## Bounded sweep

Section targets 32, 64, 128, 256, and 512 MiB and viable four-worker splits
were sampled on the retained `ordinary` and `high` 1 GiB replays. Targets of
32 and 64 MiB exposed more encoder parallelism but failed the `high` size gate
at 1.294x and 1.150x serial. Targets at or above 128 MiB passed size and query
gates. The 128 MiB target was retained as the smallest passing target.

Increasing parse chunks from 8 to 16 or 32 MiB did not improve wall time and
raised peak RSS as high as 811 MiB, so 8 MiB remains the default.

## Repeated replay result

The originally published table in this section was collected with a 256 MiB
section default and was incorrectly attributed to the later 128 MiB default. It
is superseded by the result below. One warmup and five measured trials used the
same retained inputs, explicit `--section-bytes 134217728`, explicit worker and
chunk settings, and `taskset -c 0-3`. Output and queries used tmpfs in both
modes. A fresh five-trial serial oracle supplied the size and query comparisons.
This correction was collected immediately before the opt-in compression
prototype; its `pack_jobs=1` conversion path is equivalent.

| Profile | Serial median | Parallel median | Rate | Stability | RSS | Size | Query |
|---|---:|---:|---:|---:|---:|---:|---:|
| ordinary | 3.118 s | 2.692 s | 0.402 GB/s | 1.006x | 362 MiB | 1.017x | 0.998x |
| high | 2.484 s | 2.068 s | 0.519 GB/s | 1.011x | 296 MiB | 1.074x | 1.034x |

The retained settings satisfy the <=1.10x stability, size, and query gates and
the <1 GiB RSS gate. Both outputs contain two FST sections. They do not satisfy
throughput. The parallel CPU medians are 5.55 seconds (`ordinary`) and 4.74
seconds (`high`), or 5.13 and 4.41 CPU-s/GB. A fused per-signal packer is still
needed to remove the remaining parse-IR-encode traversal before live acceptance
is worth repeating.

The exact candidate command was:

```console
taskset -c 0-3 python3 tests/benchmark_vcd.py \
  --bwave target/release/bwave \
  --corpus ordinary=/tmp/ibex-small-ordinary-1g.vcd \
  --corpus high=/tmp/ibex-opentitan-high-1g.vcd \
  --engine parallel --jobs 4 --parse-jobs 3 --encode-jobs 1 \
  --chunk-bytes 8388608 --section-bytes 134217728 \
  --baseline /tmp/bwave-serial-128-correction-baseline.json \
  --scratch-dir /dev/shm --query-pattern '*' --min-bytes-per-second 1000000000 \
  --max-slowest-ratio 1.10 --output /tmp/bwave-parallel-explicit-128m.json
```

## Per-signal prototype

The design and invariants are recorded in
[`bwave-per-signal-encoder-design-20260823.md`](bwave-per-signal-encoder-design-20260823.md).
An opt-in `--pack-jobs` prototype compresses the already partitioned signal
streams concurrently and assembles them deterministically. With a fair
four-compute-worker split (`parse=1`, `pack=3`), one ordinary trial used 2.54
wall-seconds and 4.45 CPU-seconds; high activity used 2.23 wall-seconds and 3.40
CPU-seconds. The latter is 3.17 CPU-s/GB, but its 0.481 GB/s throughput and 1.40
measured active workers fail badly. Because the prototype does not expose enough
parallel work, it is not selected as a new default and does not justify live
acceptance. The next implementation step is the fused section-local signal-chain
representation described in the design. On the fair high trial, parser work was
1.70 CPU-seconds, chronological packing 1.30, and compression only 0.075; the
coordinator spent 0.466 wall-seconds blocked on the encoder queue.

## Durable publication

A real tmpfs-to-ext4 publication used the retained 2,295,480,774-byte serial
OpenTitan FST. Copy took 0.713 seconds, file sync 0.240 seconds, directory sync
0.0005 seconds, and combined publication 0.967 seconds. The temporary source
and published benchmark copy were removed afterward.

## Validation

- `cargo test --all-targets`: 357 passed, one pre-existing ignored; Criterion
  smoke targets passed.
- `pytest crates/bwave/tests`: 37 passed, 50 environment-dependent skips.
- Checked-in Ibex initialization/ordinary/high excerpts: serial/parallel list
  and asynchronous stats equivalence passed.
- Parallel fixture differential, deterministic output, property, malformed
  timestamp, FIFO cancellation, and read-error tests passed.
- `ruff check src/ tests/` and `ruff check crates/bwave/tests/`: passed.
- Python formatting and Rust formatting checks: passed.

Live alternating OpenTitan acceptance was not repeated after the bounded replay
proved the converter cannot meet the required feed rate. Per the handoff
decision rule, no promotion is warranted until the remaining CPU traversal is
removed and both live goals are measured under equivalent durability.
