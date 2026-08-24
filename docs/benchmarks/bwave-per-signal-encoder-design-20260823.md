# B-Wave per-signal encoder design — 23 AUG 2026

> **Superseded:** the selected fused section-local representation passed the
> sustained acceptance campaign, and the parallel engine is now the production
> default. The design and rejected prototype measurements below remain
> historical context; see
> [B-Wave VCD streaming acceptance](bwave-vcd-streaming-acceptance-20260824.md).

## Decision

Keep the production build engine serial. Preserve 128 MiB estimated FST-stream
sections in the draft parallel engine, but move concurrency inside each section.
The preferred implementation is a fused section-local event representation with
persistent signal packers. Parallel compression of the existing signal streams is
a useful stepping stone, not the final design: it shortens some wall time but does
not remove parser, reconciliation, or chronological replay work.

## Current hot path

The current parallel path performs four material passes before publication:

1. `ChunkParser` scans each timestamp-aligned VCD chunk and creates chronological
   `IrOp`, timestamp, and value arenas. On the retained high-activity replay this
   representation is 1.139 bytes per input byte.
2. The coordinator consumes every `ChunkIr` in order, walks per-chunk last-change
   indexes to reconstruct the outgoing frame, and forwards the chronological IR.
3. One active `ActiveSectionEncoder` walks every `IrOp`, maintains dump state,
   performs duplicate suppression, copies the value into `SignalBuffer.values`,
   and appends final bytes to one `Vec<u8>` per signal.
4. `write_value_changes` visits every signal stream, zlib-compresses it, and
   serially assembles the deterministic FST section, offset table, and time table.

The FST format does not require signal streams to be packed or compressed in
signal-number order. It requires only that the final streams and offset entries be
assembled in signal-number order and that every stream refer to the shared ordered
time table. That makes per-signal packing and compression independent work.

## Designs considered

### A. Parallel compression after chronological packing

Keep the existing IR and `SignalBuffer` packing. At section flush, compress all
nonempty signal streams on a bounded persistent pool, collect results in signal
order, then assemble the section serially. This is deterministic, bounded by the
section target, and low risk. It cannot reduce total encoding work because all
existing passes remain; it only overlaps independent compression calls.

The opt-in `--pack-jobs` prototype implements this shape. One trial with explicit
128 MiB sections and `taskset -c 0-3` showed:

| Profile/configuration | Wall | Process CPU | Interpretation |
|---|---:|---:|---|
| high, parse 3 / pack 1 | 2.02 s | 4.65 s | current path |
| high, parse 1 / pack 3 | 2.23 s | 3.40 s | four-worker prototype; CPU passes, throughput fails |
| ordinary, parse 1 / pack 3 | 2.54 s | 4.45 s | four-worker prototype; CPU and throughput fail |
| high, parse 3 / pack 3 | 1.95 s | 4.60 s | pinned-core diagnostic; worker-oversubscribed |

The prototype is therefore rejected as the primary redesign. It remains an
opt-in experiment while the fused representation is built; it does not change
the serial production default or the parallel defaults.

The fair four-worker high-activity trial attributes 1.70 CPU-seconds to its one
parser, 1.30 to chronological signal packing, 0.075 to compression, 0.006 to
the coordinator's useful work, and 0.004 to assembly. The coordinator spent
0.466 wall-seconds blocked on the encoder queue. Instrumented work averaged only
1.40 active cores. Compression is therefore too small to be the primary target;
removing parser-to-IR-to-packer work and exposing signal packing throughout the
section are the necessary next steps.

### B. Fused section-local per-signal events (selected)

Parse directly into one flat value arena plus signal-linked change records. Each
record contains signal number, local time-table ordinal, value offset/inline
value, dump-state classification, and the next record for that signal. The parser
maintains first/last record indexes per touched signal, so no sort and no second
chronological representation are required.

The coordinator performs only ordered control reconciliation:

- concatenate chunk time tables and assign each chunk its section time-index base;
- resolve changes before the first dump directive from the incoming dump state;
- preserve maximum-timestamp and final dump state semantics;
- splice each touched signal's chunk-local chain into the section-local chain;
- retain the incoming frame and final per-signal value needed at a section boundary.

Persistent packers own disjoint signal ranges. For every signal they walk its
chain once, suppress duplicates against the incoming value, encode time-index
deltas directly into the final signal buffer, update the outgoing value, and
compress the completed buffer. No lock is needed inside a range. The assembler
writes the frame, packed streams, offsets, and time table in signal-number order.

## Invariants and bounds

- Timestamp order is established only by the ordered coordinator. Packers receive
  immutable section time-index bases and cannot reorder time.
- Output is deterministic because range assignment and final assembly use stable
  signal-number order; worker completion order is irrelevant.
- Aliases continue to share one FST signal ID and therefore one chain.
- `$dumpoff` holds the last value, matching the current serial builder. Prefix
  changes are enabled or suppressed only after applying the incoming dump state.
- A section owns one value arena and one record array. It must not retain the old
  chronological `IrOp` array at the same time.
- Signal-chain indexes use checked `u32` conversion, matching the current arena
  bounds. Section rollover remains timestamp-aligned.
- Buffers are recycled by signal range. Peak live arenas plus packed/compressed
  data must remain below the 1 GiB process RSS gate.
- Packing errors are returned with section and signal context. Worker panic and
  cancellation behavior remains fail-fast.

## Profiling contract

Profile builds use thread CPU clocks for parser tasks, coordinator work, section
packing, compression tasks, and assembly. They also retain wall time, queue-block
time, input bytes, IR bytes, and peak batch bytes. Summed measured thread CPU
divided by pipeline wall is reported as measured active workers. GNU `time`
remains authoritative for whole-process CPU and peak RSS; the difference exposes
reader, allocator, scheduler, and uninstrumented overhead.

Before another live OpenTitan run, the fused prototype must pass the checked-in
fixture differential/determinism suite and demonstrate on the retained replays:
at least 1.2 GB/s over a 4 GiB sustained input, no more than 3.4 CPU-s/GB, more
than 3.5 average active cores, no more than 1.10x serial output/query time, and
less than 1 GiB peak RSS.
