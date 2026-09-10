# Split Coverage Campaign summary from point storage

A Coverage Campaign remains one immutable, lossless record, but its V2 persistence separates the
small `coverage.json` manifest from required `coverage-points.jsonl.gz` point evidence. The manifest
is published last as the crash-durable commit marker and binds the point store by exact path, schema,
byte counts, point count, and SHA-256; summary readers avoid point I/O, while policy, retention, and
analysis deep-load and validate the complete pair.

---
status: accepted
---

## Considered Options

- Keeping points inline was rejected because every percentage/status read scales with the complete
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

V1 Campaigns remain readable without rewriting immutable history. V2 format-wide resource limits
apply equally to publication and reading. Persistence schema belongs to the codecs and loaded
envelope rather than the in-memory Coverage Campaign domain value. The split does not itself reduce
the Coverage Analyst model payload; any bounded evidence selection requires a separate explicit
decision that discloses omitted points.
