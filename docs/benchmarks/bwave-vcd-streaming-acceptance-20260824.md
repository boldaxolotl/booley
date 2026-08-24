# B-Wave VCD streaming acceptance — 24 AUG 2026

## Outcome

The parser-produced fragment path passes the retained ordinary and
OpenTitan-high regular-file and FIFO acceptance campaigns. It sustains the
required throughput while staying below the CPU-work, stability, memory,
output-size, and query-time limits on both workload shapes.

The selected configuration uses six worker threads: four parsers, one
encoder, and two packers, with the encoder also participating in packing.
Timestamp-aligned VCD chunks target 4 MiB and estimated FST-stream sections
target 128 MiB. The production build engine remains serial pending integration
review; these results establish that the draft parallel engine is ready for
that review.

## Topology correction

The rejected small-replay campaign used `taskset -c 0-3` as a four-core mask.
On the reference i7-14650HX, logical CPUs 0/1 and 2/3 are sibling threads on
only two physical P-cores. That mask imposed the observed two-core ceiling.
Final measurements use `taskset -c 0-7`, which contains four physical P-cores
and their sibling threads.

Six worker threads do not mean six required physical cores. On this mask the
ordinary and high regular-file medians used 4.12 and 4.66 process cores while
remaining at 3.34 and 2.82 CPU-s/GB. The additional logical threads let the
reader, dispatcher, coordinator, and writer make progress without displacing
all parser work from a physical core.

## Retained changes

- Parser dispatch now assigns each timestamp chunk to the next idle parser.
  The former fixed round-robin dispatcher could block behind one busy parser
  while another parser was idle. A controlled alternating A/B on four distinct
  E-cores reduced pipeline wall time by approximately 6–8%, process work by
  approximately 3%, and peak RSS by approximately 20%.
- Post-target boundary reads are 64 KiB rather than 1 MiB. Initial reads still
  request the complete chunk target. This avoids copying a large suffix during
  both `split_off` and recycled-buffer activation. Alternating A/B trials cut
  reader CPU by approximately 8–10% and peak RSS by 8–9 MiB.
- The default timestamp chunk is 4 MiB. With dynamic dispatch, the extra chunk
  boundaries improve load balance enough to outweigh their fragment metadata.
  The FST section target stays 128 MiB.

All A/B configurations produced the same FST bytes for the retained 128 MiB
high-activity replay.

## One-GiB regular-file acceptance

Each profile used one warmup and five measured trials on tmpfs. Fresh
five-trial serial runs supplied the baselines.

| Metric | Ordinary | OpenTitan high |
|---|---:|---:|
| Parallel median | 1.224 GB/s | 1.641 GB/s |
| Parallel slowest | 1.179 GB/s | 1.619 GB/s |
| Median / slowest | 1.038x | 1.013x |
| Serial median | 0.352 GB/s | 0.448 GB/s |
| Median speedup | 3.472x | 3.661x |
| Median process CPU | 3.62 s | 3.03 s |
| CPU work | 3.34 CPU-s/GB | 2.82 CPU-s/GB |
| Median process cores | 4.12 | 4.66 |
| Peak RSS | 259,604 KiB | 214,936 KiB |
| Output size ratio | 1.017x | 1.000x |
| Query-time ratio | 1.051x | 0.986x |

The high-activity serial and parallel outputs are byte-identical: both are
1,022,258 bytes with SHA-256
`493e1b2b586090163948c71d50377295f717837f5f34f60215b625166e2c96bc`.
The ordinary parallel output uses two bounded FST sections instead of the
serial output's one, so its bytes differ; its 1.017x size and 1.051x query-time
ratios pass the semantic-equivalence gates.

## One-GiB FIFO acceptance

The standard FIFO harness ran one warmup and five measured producer proofs,
then one warmup and five measured conversions for each profile.

| Metric | Ordinary | OpenTitan high |
|---|---:|---:|
| Converter median | 1.152 GB/s | 1.558 GB/s |
| Converter slowest | 1.131 GB/s | 1.516 GB/s |
| Median / slowest | 1.019x | 1.028x |
| Producer median during conversion | 1.251 GB/s | 1.747 GB/s |
| Producer slowest during conversion | 1.183 GB/s | 1.612 GB/s |
| Independent producer slowest | 16.941 GB/s | 33.906 GB/s |
| Peak converter RSS | 259,604 KiB | 205,908 KiB |

Both FIFO runs passed the converter throughput, independent-producer,
stability, RSS, output, and query gates. Producer timing during conversion is
diagnostic because it is intentionally backpressured by the consumer; the
independent trivial-reader proof is the producer-speed gate.

## Four-GiB bounded-memory check

Single selected-configuration runs confirm that memory stays bounded as input
length grows.

| Metric | Ordinary | OpenTitan high |
|---|---:|---:|
| Input bytes | 4,307,637,044 | 4,295,175,559 |
| Wall time | 3.34 s | 2.49 s |
| Throughput | 1.290 GB/s | 1.725 GB/s |
| CPU work | 3.25 CPU-s/GB | 2.84 CPU-s/GB |
| Average process cores | 4.19 | 4.90 |
| Peak RSS | 308,804 KiB | 278,060 KiB |
| Output bytes | 76,891,219 | 3,759,816 |
| FST sections | 5 | 3 |

Both RSS results remain far below 1 GiB and grow only with bounded in-flight
chunks and sections, not total input length.

Machine-readable evidence is in
[`bwave-vcd-streaming-acceptance-20260824.json`](bwave-vcd-streaming-acceptance-20260824.json).
