# Split Coverage Campaign summary from point storage

A Coverage Campaign remains one immutable, lossless record, but its V3 persistence separates the
small `coverage.json` manifest from required `coverage-points.jsonl.gz` point evidence. The manifest
is published last as the crash-durable commit marker and binds the point store by exact path, schema,
byte counts, point count, and SHA-256; summary readers avoid point I/O, while policy, retention, and
analysis deep-load and validate the complete pair. V1/V2 persisted Campaigns are rejected with an
actionable recollection diagnostic after the V3 source-rollup migration in #483.

---
status: accepted
---

## Considered Options

- Keeping points inline in the current persisted schema was rejected because every
  percentage/status read scales with the complete
  point population and realistic Campaigns already produce tens of megabytes of JSON.
- Making points optional was rejected because exact identities and per-run incidence are required
  for deterministic rollups, Approved Waivers, and Coverage Analysis.
- A public lazy point iterator was rejected because digest, count, trailing-data, and final-record
  validity are unknown until complete consumption; no point-dependent effect may commit before deep
  validation succeeds.
- Changing `cp1:` identifiers in the same migration was rejected because approved waivers decode
  their complete source-bound identity. Compression addresses representation size without coupling
  identifier migration to storage layout.

## Consequences

V3 format-wide resource limits apply equally to publication and reading. Persistence schema belongs
to the codecs and loaded envelope rather than the in-memory Coverage Campaign domain value. The
V3-only cutoff deliberately favors one fully checked storage contract over compatibility readers
for unreleased formats; retained V1/V2 evidence must be recollected. The split does not itself
reduce the Coverage Analyst model payload. ADR 0063 separately binds analysis to budgeted evidence
views and records the retrieval scope so omitted points remain explicit.
