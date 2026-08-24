# B-Wave live Ibex acceptance — 24 AUG 2026

## Outcome

The final live simulator-coupled campaign passes on the promoted parallel
converter. Every sink, serial, and parallel CoreMark run passed. Parallel
conversion is approximately 2.9x faster than serial on both primary targets,
is stable across the three measured trials, stays below 1 GiB RSS, and keeps
FST size within 1.01x of the serial oracle.

The candidate binary SHA-256 is
`ea3a0504e7f6a2727407917c6a3d06568aefd5c8ff201c2524086c4ff9c395c0`.
The CoreMark firmware SHA-256 is
`0412183f498163639f3df4759a0828c5d92273750121155b03c41273397acdf3`.

## Primary campaign

Each target used one sink/serial/parallel warmup followed by three measured
sink/serial/parallel triplets. The simulator wrote VCD to a FIFO, conversion
used tmpfs scratch, and only compact JSON/log evidence was published to the
durable Ibex project handoff.

| Metric | small | OpenTitan |
|---|---:|---:|
| Raw VCD | 17,763,580,128 B | 91,348,171,298 B |
| Executed cycles | 4,149,572 | 3,237,588 |
| Sink median | 15.23 s | 79.93 s |
| Serial median | 69.61 s | 355.33 s |
| Parallel median | 23.99 s | 122.93 s |
| Parallel speedup | 2.902x | 2.891x |
| Parallel slowest / median | 1.003x | 1.000x |
| Parallel converter rate | 0.741 GB/s | 0.743 GB/s |
| Parallel peak RSS | 383,380 KiB | 755,056 KiB |
| Parallel / serial FST bytes | 1.004x | 1.007x |
| CoreMark | all pass | all pass |

The earlier live regression came from making the FIFO descriptor nonblocking
for cancellation and then sleeping 2 ms after every temporary empty read.
Verilator emits many small writes, so the fixed sleep caused repeated FIFO
block/wake cycles and inflated simulator system time. The final reader waits
on descriptor readiness instead: data wakes it immediately, while idle
cancellation remains bounded. A fixed small parallel trial fell from 120.6 s
to 24.1 s without changing its FST result.

## Secondary direction checks

Each secondary target used one counting sink, one serial conversion, and one
parallel conversion.

| Target | Raw VCD | Sink | Serial | Parallel | Speedup | Result |
|---|---:|---:|---:|---:|---:|---|
| maxperf | 18,543,697,579 B | 13.77 s | 70.11 s | 24.22 s | 2.895x | PASS |
| maxperf-pmp-bmbalanced | 21,989,343,779 B | 20.49 s | 89.24 s | 30.65 s | 2.912x | PASS |

Machine-readable summary evidence is in
[`bwave-ibex-live-acceptance-20260824.json`](bwave-ibex-live-acceptance-20260824.json).
Full per-trial JSON and simulator logs are retained under
`.booley_project/handoffs/bwave-vcd-throughput-20260824/live-results` in the
Ibex project.
