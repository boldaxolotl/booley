# B-Wave parser-produced chunk fragments — 24 AUG 2026

> **Superseded:** dynamic parser dispatch, narrower boundary reads, and a
> corrected four-physical-core affinity pass the sustained acceptance gates.
> The parallel engine is now the production default; the decision below is a
> historical checkpoint.
> See
> [B-Wave VCD streaming acceptance](bwave-vcd-streaming-acceptance-20260824.md).

## Decision

Retain the parser-produced fragment implementation in the draft parallel
engine, but keep the production build engine serial. The redesign passes the
small-replay CPU and memory predictions and preserves exact output bytes. It
does not predict the required throughput or utilization, so it does not justify
a 1 GiB, 4 GiB, five-trial, or live OpenTitan acceptance run.

## CPU attribution before redesign

The retained 128 MiB high-activity replay was first rerun with reader and
dispatcher thread CPU added to the profile. For 133,805,569 body bytes, the
profile measured 0.065 CPU-seconds in reader/chunk production, 0.264 in parsing,
0.183 in signal packing, 0.017 in compression, and 0.003 in coordination. GNU
`time` measured 0.57 process CPU-seconds, leaving about 0.038 CPU-seconds in
header/finalization, allocation outside the measured regions, scheduling, and
other process work. Packing was therefore the only removable cost large enough
to close the CPU gate; reader work was not.

## Representation

Timestamp-led chunks without `$dumpoff` or `$dumpon` are parsed into one
fragment per touched signal. A fragment keeps its first two distinct values
explicit because the first transition depends on incoming section state. Later
distinct values are locally deduplicated and written immediately in final FST
signal-stream encoding. The packer compares the first value to incoming state,
emits the second value when present, then appends the encoded tail directly.

The parser's signal-slot table maps each owned signal directly to its fragment,
so packer ranges do not rescan chronological records or route records during
parsing. Exact repeated vector text is rejected before normalization, while
canonical comparison still catches textually different equivalent values.
Fragment stream capacities are recycled and initially reserve 128 bytes, close
to the measured mean fragment stream, to avoid repeated small allocations.

The existing chronological record path remains the fallback for chunks with
dump controls or content before a leading timestamp. Non-affine time maps from
duplicate/non-monotonic or cross-chunk timestamps rewrite only the fragment
tail's varint headers. Initial-frame changes, real values, x/z vectors, aliases,
overwide values, cancellation, and deterministic signal-order assembly retain
their existing behavior.

## Retained small-replay result

Configuration: CPUs 0-3, jobs 4, parse jobs 2, encode jobs 1, pack jobs 2,
8 MiB chunks, 128 MiB sections, profile build, tmpfs input/output. The input is
134,443,328 bytes with SHA-256
`8675dce3ca337d6c0785e1cfb66b4d123d6dc08fd37f9d87a3a0979a67406441`.
One warmup and three measured builds produced:

| Metric | Result |
|---|---:|
| Median wall | 0.1980 s |
| Median throughput | 0.679 GB/s |
| Slowest throughput | 0.644 GB/s |
| Median process CPU | 0.43 s / 3.20 CPU-s/GB |
| Worst process CPU | 0.44 s / 3.27 CPU-s/GB |
| Median average process cores | 2.17 |
| Peak RSS | 136,824 KiB |
| Representation/body input | 0.2334 |
| Input events / retained transitions | 5,533,422 / 2,752,363 |
| Value arena bytes | 9,321,238 |
| Packer CPU in diagnostic trial | 0.0228 s |

The exact FST SHA-256 is
`b2276a914c41e203c1dd08c221be122557a127b65c4c62289c50a503c8ad0e46`,
identical to the pre-redesign parallel output and identical across repeated
builds. Relative to the attributed pre-redesign diagnostic, representation
falls from 0.6142 to 0.2334 bytes per body byte and pack CPU falls from 0.1829
to 0.0228 seconds. Parser CPU remains approximately flat after raw duplicate
rejection and capacity recycling.

## Promotion decision

The process CPU prediction passes the approximately 3.4 CPU-s/GB gate and RSS
passes comfortably. Median throughput remains far below 1.2 GB/s, and 2.17
average cores remains below the greater-than-3.5-core gate. The small replay is
also no faster in wall time than the previous design. Do not run a larger or
live acceptance campaign from this state. The next redesign must expose more
than two continuously useful parser cores without raising total parser work;
changing indexes, record routing, or chunk size did not do so.

Exact machine-readable measurements and rejected configuration trials are in
`bwave-opentitan-parser-fragments-early-20260824.json`.
