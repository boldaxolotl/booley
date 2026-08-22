# Implementation plan — timestamp-chunked, multi-core VCD-to-FST conversion

**Created:** 2026-08-21
**Revised:** 2026-08-22
**Owner:** unassigned
**Scope:** `crates/bwave` VCD-to-FST build path, its FIFO integration, and
throughput/correctness validation
**Primary production evidence:** pending the retained Ibex trace follow-up

## Objective

Make B-Wave consume simulator-produced VCD at a sustained rate of at least
**1.0 GB/s** on the reference host, with **1.2 GB/s** as the stretch target that
keeps the converter ahead of the measured Ibex VCD producer.

This is the portable path for simulators that cannot emit FST. Native FST is
explicitly out of scope: it does not remove the need for a fast converter for
the other supported and planned simulators.

The implementation direction is fixed by the PR 35 attribution:

| PR 35 path on the reference i7-14650HX | Sustained input rate |
|---|---:|
| specialized scanner with null sink | 930–944 MiB/s |
| scanner plus production ID lookup | 770–784 MiB/s |
| scanner plus normalization | 310–324 MiB/s |
| complete VCD-to-FST build | 273–331 MiB/s |
| complete build of a generated 1 GiB VCD | 278 MiB/s (3.68 s) |

The earlier Ibex production attribution establishes why 1.0 GB/s is the
relevant end-to-end target:

| Production path | Sustained input rate |
|---|---:|
| FIFO with a trivial reader | about 1.14 GB/s |
| FIFO into B-Wave | about 300–314 MB/s |

At producer speed, the traced configurations move toward the already measured
FIFO-only floors, although those floors still include Verilator's VCD
generation and FIFO transport:

| Ibex configuration | Current pipeline | FIFO-only floor |
|---|---:|---:|
| `small` | 56.433 s | 15.397 s |
| `opentitan` | about 302.8 s | 80.288 s |

Final FST compression and writing account for much less time than parsing and
encoding on those workloads. More buffer tuning, lookup specialization, or
compression-only parallelism cannot close the remaining roughly 3.4× gap. The
scanner, normalization, current-value tracking, and encoding must execute
across multiple cores.

## Architectural decision

Build a bounded streaming pipeline whose unit of parallel work is a
timestamp-aligned VCD body chunk:

```text
                    bounded, sequence-numbered work

 VCD file/FIFO -> timestamp chunker -> parallel parsers -> ordered state
                                                        reconciliation
                                                               |
                                                               v
 ordered FST file <- ordered section writer <- parallel section encoders
```

The reader and final writer remain ordered. They should perform bulk byte I/O,
not per-event work. Parsing, ID resolution, normalization, duplicate detection,
bit packing, and section compression run in worker pools.

The existing serial converter remains available until rollout as:

- the semantic oracle for differential tests;
- the small-input path if parallel startup costs are not worthwhile; and
- a diagnostic fallback selected explicitly, never silently after a parallel
  conversion has consumed a non-seekable FIFO.

## Ground rules

- Preserve VCD compatibility across Verilator, Icarus, and the retained
  Xcelium dialect fixture.
- Preserve hierarchy, declarations, aliases, event order within a timestamp,
  values, and the full trace interval. Byte-identical FST output is not
  required.
- Preserve the current behavior for repeated and decreasing timestamps,
  `$dumpvars`, `$dumpoff`, `$dumpon`, short and over-wide values, X/Z values,
  reals, CRLF, and an unterminated final line.
- Propagate input, output, worker, and cancellation errors. Never publish a
  truncated FST as a successful conversion.
- Keep memory bounded independently of total trace length.
- Preserve deterministic hierarchy, section, signal, and transition ordering
  regardless of worker scheduling.
- Do not hardcode Ibex or any external project path in framework code. Fixture
  tools take source paths as explicit developer inputs; checked-in tests use
  repository-local fixtures.
- Read `docs/CODING_PRINCIPLES.md` before Python changes and run
  `ruff check src/ tests/` whenever Python source or tests change.
- Do not narrow the default trace scope or window to claim a throughput win.

## Acceptance contract

### Performance corpus

Measure all of the following:

1. A 1 GiB Ibex-derived mixed-width, ordinary-activity VCD.
2. A 1 GiB Ibex-derived high-activity VCD.
3. A many-signal workload covering approximately 4,000 and 11,000 declared
   signals and multi-character IDs.
4. The retained Xcelium dialect fixture amplified to a useful benchmark size.
5. A regular file on memory-backed storage, isolating converter CPU.
6. A FIFO driven by a producer proven independently to exceed 1.2 GB/s.

Record input bytes divided by wall time, user/system time, CPU utilization,
peak RSS, output bytes, FST section count, and query timings. Run one warmup and
at least five measured trials; report the median and slowest trial.

### Proposed completion gates

- Median complete-build throughput is at least 1,000,000,000 input bytes/s on
  every primary Ibex-derived workload.
- The slowest measured trial is within 10% of the median.
- The FIFO converter does not pace a producer sustaining 1.0 GB/s.
- Peak RSS stays below 1 GiB with default settings and does not grow with total
  input length.
- Default FST output is no more than 10% larger than the serial result.
- Representative range and multi-signal query timings regress by no more than
  10% from the serially sectioned FST.
- Parallel and serial output are semantically equivalent under the complete
  compatibility matrix below.

The performance harness should fail explicitly when a configured acceptance
threshold is missed. Normal shared CI may track a wider regression threshold;
the 1 GB/s release gate runs on named, pinned hardware.

## Ibex fixture and benchmark strategy

Use real Ibex VCD syntax and activity rather than relying solely on the
existing regular synthetic generator.

The visible Ibex workspace currently contains a small `trace.fst`, while the
historical attribution campaign streamed its large VCDs through FIFOs and did
not retain them in the visible runtime tree. Before implementation:

1. Locate the original `small` and `opentitan` VCD artifacts if they are still
   retained outside the project runtime, or rerun those two traced simulations
   with an explicitly retained VCD file.
2. Record source configuration, simulator/version, byte size, signal count,
   width distribution, line-class counts, activity distribution, and SHA-256
   in a developer-only capture manifest.
3. Do not commit the multi-gigabyte originals.

Add a deterministic developer tool under `crates/bwave/tests/` that accepts an
explicit source VCD and produces two kinds of repository-safe artifacts:

### Checked-in semantic excerpts

- Preserve the complete original header.
- Choose contiguous time windows representative of initialization and steady
  execution.
- Compute the value of every declared signal at the selected start time and
  emit that state at the excerpt's first timestamp before replaying the
  selected body events.
- Keep the original IDs, widths, hierarchy, value spellings, and relative
  activity.
- Compress the excerpts under
  `crates/bwave/tests/fixtures/real_trace/` and add a provenance manifest that
  contains no absolute developer path.
- Inspect the generated fixtures for confidential or environment-specific
  content before committing them and run the confidential-content guard.

Target a few MiB compressed at most. The excerpts are correctness and
microbenchmark fixtures, not the sustained-throughput corpus.

### Generated large corpora

Generate large inputs locally by replaying an extracted steady-state Ibex event
window while monotonically offsetting timestamps. This preserves real line
lengths, ID distribution, widths, value forms, and signal activity without
checking in a 1 GiB artifact. Emit the header and a valid initial value state
once, then repeat the event window until the requested byte target is reached.

Provide named profiles for ordinary and high activity, plus options for target
bytes and output path. Generation must be deterministic from the checked-in
excerpt and parameters. The benchmark records the generated file's SHA-256.

Keep small hand-authored fixtures for rare boundary behavior such as dump
control, malformed input, and decreasing timestamps. Do not contort an Ibex
excerpt to cover dialect cases it does not contain.

## Implementation status — complete locally, 2026-08-22

The bounded regular-file/FIFO pipeline is complete and the parallel converter
is now the default. The hidden `--engine serial` control remains available as
the semantic oracle, benchmark baseline, and diagnostic fallback. Regenerating
the unavailable primary Ibex artifacts and recording production measurements
has been split into a focused follow-up handoff.

### Implemented

- The serial oracle has strict typed read/timestamp errors, checked `u64`
  timestamps, exact offsets, huge-line support, finalization error propagation,
  and partial-output/progress cleanup.
- `tests/vcd_corpus.py` captures deterministic semantic excerpts with incoming
  state and redacted provenance, then replays complete real-activity windows to
  an explicit byte target with monotonic timestamps.
- Regular-file and FIFO benchmark runners record hashes, wall/user/system time,
  CPU, RSS, output bytes, section count, query time, medians, slowest trials,
  host data, and all configured gates. Both enforce the 1 GiB RSS ceiling; a
  serial baseline also enables automatic 1.10× output-size and query-time
  gates. The FIFO runner independently proves producer speed first.
- The FST writer is split into independent header, section encoder, ordered
  append, and final-patch ownership. First-timestamp-only sections, compressed
  initial frames, exact-width values, independent frame-seeded sections, and
  ordered append are covered by reader round trips.
- `VcdChunkSource` uses bulk reads, exact timestamp boundaries, sequence/input
  ranges, huge-line handling, bounded buffer recycling, cancellation, and the
  same semantics for files and FIFOs. A Criterion run measured 5.54 GiB/s for
  1 MiB timestamp-aligned chunks.
- Parser workers produce contiguous compact IR: eight-byte operations, inline
  scalar values, timestamp/value arenas, dump-state summaries, sparse
  first/last assignment summaries, maximum timestamps, and exact enabled-only
  warning counts. Two- and three-character printable VCD IDs use dense lookup;
  this reduced the measured parse stage about 24% on the retained trace.
- Ordered reconciliation consumes summaries rather than rescanning events,
  carries the incoming full frame/time/dump state, and submits coalesced output
  sections as soon as their predecessors are resolved.
- Parser and section-encoder pools run concurrently. A one-section encoder
  handoff, bounded completion reorder map, chunk-by-chunk IR release, and a
  four-buffer raw recycle pool keep memory independent of input length.
- Encoder panics become typed worker failures. Downstream failure cancels a
  nonblocking FIFO body read, removes the partial FST, and closes the consumer
  so a live producer observes `BrokenPipe`.
- Per-signal value-change streams use the FST-standard zlib pack type at level
  1. This retains independent-section parallelism while recovering the
  compression ratio that LZ4 lost when its history reset at every section.
- Measured parallel defaults are 1 MiB parse chunks, 34 MiB FST sections, and
  an approximately 70/30 parser/encoder split capped at 24 workers (17/7 on
  the 24-logical-CPU reference host).

### Current evidence

The available real trace is a 307,985,605-byte PicoRV32 VCD, not a primary Ibex
artifact. A deterministic 1,078,240,241-byte replay of a contiguous 51,291,711-
byte steady-state activity window provides sustained local evidence. The source
excerpt is 6,022,896 bytes compressed and retains all 496 declarations, 428
unique IDs, mixed widths through 1,024 bits, and 3,920,217 selected events.

With 1 MiB parse chunks, 34 MiB sections, and the selected 17/7 split:

| Path | Median | Slowest | Peak RSS | Sections |
|---|---:|---:|---:|---:|
| regular file | 1.148 GB/s | 1.138 GB/s | 830 MiB | 31 |
| FIFO | 1.087 GB/s | 1.080 GB/s | 817 MiB | 31 |

During the FIFO conversion the producer sustained a 1.401 GB/s median and
1.320 GB/s slowest trial; the independent trivial-reader producer proof
exceeded 16.9 GB/s. A 2,150,036,342-byte FIFO replay sustained 1.233 GB/s and
peaked at 802 MiB, showing that RSS remains bounded as total input grows.

The parallel FST is 1.0748× the two-section serial result. A full asynchronous
all-signal stats query took 19.36 s on the parallel FST versus 19.41 s on the
serial FST, a 0.9972× ratio. Thus throughput, stability, FIFO, RSS, output-size,
and representative query gates all pass on the available sustained workload.
The earlier LZ4 prototype failed the repeated-window size gate (1.221× even
with a 51 MB replay window); level-1 zlib closes it without changing section
count or exceeding the CPU/RSS budgets.

### Validation and external follow-up

- `cargo test --all-targets`: 357 tests passed, one pre-existing real-value
  query test ignored; all Criterion smoke targets passed.
- Serial/parallel differential coverage: eleven Verilator/Xcelium/edge
  fixtures, deterministic repeated builds, tiny boundary targets, aliases,
  dump controls, reals, wide/X/Z values, clock metadata, and 48 randomized
  generated cases.
- Crate-local Python: 18 passed. The broader repository B-Wave, simulator,
  simulation-flow, native-resolution, and validation-target suite: 739 passed
  with local-loopback permission. `ruff check src/ tests/` passes.

The earlier Ibex attribution handoff and the original retained Ibex
`small`/`opentitan` VCDs are not present on this host. Consequently Phase 0's
checked-in Ibex excerpts and pinned-host baseline, both primary 1 GiB workload
gates, and end-to-end Ibex `small`/`opentitan` production validation remain an
external evidence follow-up. Do not substitute the PicoRV32 results above for
those primary acceptance results. The default switch was explicitly approved
from the complete local correctness suite and the sustained regular-file/FIFO
evidence while that production campaign is handed off separately.

## Phase 0 — land the oracle, fixtures, and trustworthy benchmark

### Work

1. Preserve the PR 35 specialized scanner, dense-ID lookup, normalization fast
   paths, and stage benchmarks that survived correctness review.
2. Fix the serial scanner's error contract before using it as an oracle:
   return and propagate read errors instead of treating them as EOF; parse
   timestamps with checked arithmetic and reject trailing junk.
3. Move benchmark attribution behind test/benchmark-only interfaces rather
   than public production methods.
4. Add the Ibex capture/excerpt/replay tooling and checked-in semantic
   excerpts described above.
5. Add one command that runs serial conversion over every benchmark profile
   and writes a machine-readable result record.
6. Capture the pre-parallel baseline on the pinned reference host.

### Exit gate

- The exact 1 GiB and FIFO benchmark procedures are reproducible.
- The checked-in excerpts pass existing FST/query differential tests.
- Serial failures cannot be mistaken for clean EOF.
- The baseline reproduces the production bottleneck closely enough that a
  local architectural win can be checked against Ibex.

## Phase 1 — split the converter into testable stages

Refactor the serial implementation through the following internal interfaces
without adding threads yet:

```text
VcdChunkSource
    -> ChunkParser
    -> StateReconciler
    -> FstSectionEncoder
    -> OrderedFstWriter
```

### Work

1. Separate immutable header-derived signal schema from mutable conversion
   state. The schema owns signal index, FST handle, width, type, aliases, and
   scope filtering.
2. Make section encoding independent of the output file. It must be possible
   to encode a complete value-change section into a cursor/owned byte buffer.
3. Split writer ownership into:
   - header/hierarchy/geometry creation;
   - ordered section append; and
   - final header patching after all sections succeed.
4. Define typed errors that retain input offset, chunk sequence, timestamp,
   signal context where available, and source error.
5. Route the serial engine through the new interfaces and prove semantic
   equivalence before introducing concurrency.

### Exit gate

- No throughput claim is required, but the refactor does not regress serial
  throughput by more than 5%.
- An independently encoded section can be appended and read by `fst-reader`.
- All current Rust tests and the Ibex excerpt differential suite pass.

## Phase 2 — implement bounded timestamp-aligned chunking

Define an owned `VcdChunk` with a sequence number, byte buffer, input byte
range, and boundary metadata.

### Boundary algorithm

1. The first chunk begins at the body position returned by header parsing.
2. Fill a reusable buffer to the target size, initially 32 MiB.
3. Continue to the next complete line whose first byte is `#`; end the current
   chunk immediately before that timestamp line.
4. The following chunk therefore begins with a timestamp and can establish its
   local time without scanning a preceding arbitrary body fragment.
5. A line larger than the target size is accepted as one oversized chunk; it
   must not cause an unbounded retry or truncate the line.

The chunker only performs bulk reads and searches near the target boundary. It
must not lex or dispatch every body line serially.

### File and FIFO sources

- Implement the buffered streaming source first so files and FIFOs share the
  same semantics.
- Reuse buffers through a bounded pool.
- Permit at most approximately `worker_count + 2` chunks in flight initially.
- Add memory mapping or positional file reads only if the shared source is a
  measured bottleneck.
- Preserve heartbeat progress while downstream stages work, using bytes read
  and chunks committed rather than FST file growth alone.

### Exit gate

- Chunk concatenation reproduces the exact original body bytes.
- Every non-first chunk begins at a timestamp line.
- Unit/property tests cover CRLF, EOF without newline, timestamp text split
  across reads, huge lines, empty bodies, read errors, and cancellation.
- Chunk production alone exceeds 2 GB/s on the reference host.

## Phase 3 — parse chunks independently into compact `ChunkIR`

Each parser worker owns one chunk and produces immutable intermediate data:

```text
ChunkIR
  sequence and input byte range
  local timestamp runs
  dump-control transitions
  compact changes grouped or indexable by signal
  first/last assignment summaries
  maximum observed timestamp
  validation warnings and counters
```

### Requirements

1. Resolve raw VCD IDs to integer signal indices inside the worker.
2. Normalize values into an output-oriented packed representation once.
3. Preserve source order for changes at the same timestamp.
4. Suppress duplicates after the first local assignment to a signal. Retain
   the first assignment because equality depends on the incoming frame.
5. Store scalar changes separately or compactly; a three-byte scalar line must
   not become a large heap-backed general event.
6. Use contiguous arenas and offset tables instead of one allocation per
   event.
7. Record enough summary data for the coordinator to determine incoming and
   outgoing state without rescanning every event.
8. Represent dump-control segments so the initial segment can be enabled or
   disabled after the preceding chunk's dump state is known.
9. Represent timestamp runs so a preceding global maximum can be applied to
   repeated/decreasing timestamps without reparsing text.

Benchmark alternative IR layouts with the Ibex-derived corpora before fixing
the public implementation. Track IR bytes per input byte as well as speed.

### Exit gate

- Four to eight workers construct `ChunkIR` above 1.5 GB/s aggregate on both
  Ibex-derived corpora.
- Peak live IR stays within the bounded pipeline's memory budget.
- Replaying the IR through a null semantic sink matches the serial event
  stream exactly.

If this gate fails, redesign the representation before changing FST output.
Do not hide an expanding IR behind more worker threads.

## Phase 4 — reconcile cross-chunk state in order

Parser results may arrive out of order. A single coordinator holds a bounded
sequence-numbered reorder map and consumes summaries in input order.

For each chunk it determines:

- incoming dump-control state;
- incoming global timestamp maximum;
- incoming full FST frame/current signal values;
- section start/end time;
- the effective first change for each touched signal; and
- outgoing signal state for the following chunk.

### Constraints

- The coordinator processes compact summaries and frame copies, not every
  event.
- Applying sparse last-assignment summaries must determine the next frame.
- Copying a full frame once per output section is acceptable initially; track
  its cost and change representation only if measured.
- Reconciliation sends an `EncodeChunk` containing the incoming frame and
  resolved boundary state to the encoder pool as soon as its predecessor has
  been reconciled. It does not wait for the whole input.
- Coordinator failure closes all queues and wakes blocked FIFO, parser,
  encoder, and writer participants.

### Exit gate

- Reconciled chunk streams match the serial converter for randomized chunk
  sizes and schedules.
- Boundary tests cover duplicates, dump control, same-timestamp order, and
  decreasing timestamps on both sides of a chunk boundary.
- Coordinator work remains below 10% of complete conversion wall time and its
  memory remains bounded.

## Phase 5 — encode FST sections in parallel and write them in order

Encoder workers receive reconciled chunks and independently:

1. apply cross-chunk duplicate suppression;
2. build the local timetable;
3. encode per-signal delta/value streams;
4. compress eligible signal streams and the timetable; and
5. return a self-contained section byte buffer plus metadata.

The writer accepts completed sections out of worker completion order, retains
only a bounded reorder window, appends bytes by chunk sequence, and patches the
FST header only after every section succeeds.

Start with one FST section per 32 MiB parse chunk. Measure section-count effects
on frame overhead and queries. If necessary, combine consecutive parsed chunks
into 128–256 MiB output sections without reducing parse parallelism.

### Exit gate

- Repeated parallel builds are deterministic in hierarchy, section ordering,
  output size, and query results.
- Output errors, encoder panic, cancellation, and early FIFO EOF terminate the
  whole pipeline promptly and leave no success marker.
- Complete regular-file conversion exceeds 1.0 GB/s on the primary corpora.
- FST size and query gates remain satisfied.

## Phase 6 — make the full pipeline stream through FIFOs

Connect the stages with bounded queues and explicit cancellation:

- one reader/chunker;
- a parse worker pool;
- one lightweight ordered state coordinator;
- an encode worker pool, shared with parsers initially if simpler; and
- one ordered writer.

### Work

1. Size queue capacities from the RSS budget rather than throughput alone.
2. Ensure a blocked reader wakes when any downstream error occurs.
3. Ensure a simulator writing the FIFO observes consumer failure rather than
   running indefinitely against an abandoned keepalive descriptor.
4. Report heartbeat progress for bytes read, chunks parsed, and sections
   committed without placing a clock read on every event.
5. Exercise sustained streams much larger than available memory.
6. Measure worker counts `1, 2, 4, 6, 8` and chunk sizes `16, 32, 64, 128 MiB`.
7. Select defaults from physical cores and the memory budget; keep temporary
   developer controls for experiments but avoid unnecessary permanent CLI
   surface.

### Exit gate

- The converter drains a synthetic FIFO producer sustaining at least 1.0 GB/s.
- RSS reaches a steady bound during a multi-gigabyte stream.
- Ibex `small` and `opentitan` traced runs show that B-Wave no longer
  materially paces the simulator.

## Phase 7 — optimize worker kernels only after pipeline attribution

Retain the validated PR 35 fast paths, then optimize only measured worker
costs:

- store two-state current values bit-packed rather than as ASCII;
- compare and pack exact-width two-state vectors in one traversal;
- use byte lookup tables before considering AVX2-specific code;
- keep explicit slow paths for X/Z, shortened/over-wide values, and reals;
- reuse chunk, arena, timetable, compression, and section-output buffers;
- retain bounded dense numeric-ID decoding with a legal-ID fallback;
- tune compression threshold only after parsing and encoding scale.

Compression-only wins are cleanup, not the architecture. Do not add SIMD or
unsafe code without a benchmark demonstrating material end-to-end benefit and
dedicated equivalence tests.

### Exit gate

- The preferred defaults meet the 1.0 GB/s completion target with margin on
  every primary workload.
- Each retained kernel optimization has isolated and complete-build evidence.
- One-worker mode remains semantically equivalent and useful for diagnosis.

## Correctness and compatibility matrix

Every phase that changes parsing, state, encoding, sectioning, or concurrency
must run:

1. Existing Rust unit, property, CLI, and integration tests in
   `crates/bwave`.
2. Existing simulator ground-truth tests.
3. Existing native-FST reader tests so writer changes do not regress the
   shared query surface.
4. Differential serial-versus-parallel checks covering:
   - Ibex-derived Verilator excerpts, Icarus, and Xcelium fixtures;
   - aliases and repeated declarations;
   - scalar, vector, real, parameter, integer, and wide signals;
   - X/Z, short values, over-wide values, and bit-blasted arrays;
   - `$dumpvars`, `$dumpoff`, `$dumpon`, CRLF, multiline headers, empty body,
     malformed/overflowing/repeated/decreasing timestamps, and truncated
     input;
   - every legal boundary position around timestamps, directives, and long
     lines; and
   - randomized chunk sizes, worker counts, and completion schedules.
5. Query equivalence for `list`, `value`, `signal`, `wave`, `stats`, `find`,
   `diff`, clock derivation, and randomized signal/timestamp samples.
6. Determinism checks across repeated parallel builds.
7. Multi-section traces large enough to wrap every bounded queue repeatedly.

Semantic comparison is authoritative. Compressed bytes may differ when
sectioning or compression scheduling changes.

## Production validation on Ibex

Use the pinned Ibex environment and the existing attribution procedure. For
`small` and `opentitan`, record:

- simulation verdict and cycle count;
- raw VCD byte count;
- simulator and B-Wave wall/user/system time;
- converter rate and CPU utilization by pipeline stage;
- FIFO backpressure or producer-blocked time if observable;
- wait after simulator exit;
- peak B-Wave RSS and maximum in-flight chunk/IR bytes;
- FST bytes and section count;
- intended signal count and randomized query equivalence; and
- complete traced Flow wall time.

Run one warmup and at least three alternating serial/parallel measured trials
per configuration. After selecting defaults, confirm `maxperf` and
`maxperf-pmp-bmbalanced` once each for correctness and performance direction.

Attach results to the implementation handoff or PR; do not rely on an
ephemeral `/tmp` report as the only final evidence.

## Delivery sequence

The implementation followed these validation checkpoints:

1. **Fixtures and benchmark:** Ibex excerpt/replay tooling, result schema,
   serial correctness fixes, and baseline.
2. **Conversion seams:** schema, chunk/parser/reconciler/encoder/writer
   interfaces running serially.
3. **Chunker and IR:** bounded chunk source plus parallel parse prototype.
4. **State reconciliation:** ordered boundary semantics and exhaustive tests.
5. **Parallel sections:** independent encoding, deterministic ordered writer,
   and file-path performance gate.
6. **FIFO integration:** bounded queues, cancellation, heartbeat, and Ibex
   production validation.
7. **Kernel tuning and rollout:** measured fast paths, defaults, documentation,
   and switch to the parallel engine for large traces.

The serial engine remains available to isolate regressions at any checkpoint.

## Likely code locations

- `crates/bwave/src/fst.rs` — current production scanner, ID resolution, and
  build handler; likely split into focused conversion modules.
- `crates/bwave/src/parser.rs` — shared VCD behavior and compatibility tests.
- `crates/bwave/vendor/fst-writer/src/buffer.rs` — signal state and encoded
  changes.
- `crates/bwave/vendor/fst-writer/src/io.rs` — section serialization,
  per-signal compression, timetable compression, and varints.
- `crates/bwave/vendor/fst-writer/src/writer.rs` — header, section append, and
  finalization ownership.
- `crates/bwave/benches/throughput.rs` — stage, corpus, and regression
  benchmarks.
- `crates/bwave/tests/` — Ibex extraction/replay tool, fixtures, property,
  dialect, differential, and end-to-end tests.
- `src/booley/sim/bwave_fifo.py` and `src/booley/sim/trace_session.py` — only
  where cancellation, heartbeat, or FIFO lifecycle contracts must change.

## Explicit non-goals

- Selecting native FST instead of fixing the portable VCD path.
- Increasing FIFO capacity as the throughput solution.
- Reducing default trace hierarchy, signal count, or time window.
- Replacing FST with a new primary store format in this campaign.
- Optimizing final FST publication or trace-build caching.
- Adding public tuning options before evidence shows users need them.
- Blindly splitting raw VCD at arbitrary byte offsets without timestamp and
  cross-chunk state reconciliation.

## Decisions to record during implementation

1. Exact Ibex source traces/excerpts and their provenance.
2. `ChunkIR` layout and bytes-per-input-byte evidence.
3. Default parse chunk size and maximum in-flight chunks.
4. State-frame representation and measured reconciliation cost.
5. Whether parse chunks map one-to-one to FST sections or are coalesced.
6. Parser and encoder worker counts and their selection policy.
7. Final FST size/query trade-off and compression thresholds.
8. Final achieved file and FIFO rates versus the 1.0/1.2 GB/s targets.
9. The next bottleneck if B-Wave no longer paces the simulator.
