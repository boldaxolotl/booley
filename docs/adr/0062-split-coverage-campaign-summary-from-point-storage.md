# Split Coverage Campaign summary from point storage

A Coverage Campaign remains one immutable, lossless record, but its V2 persistence separates the
small `coverage.json` manifest from required `coverage-points.jsonl.gz` point evidence. The manifest
is published last as the crash-durable commit marker and records the point store's exact path, schema,
byte counts, point count, and SHA-256 as its identity; the manifest carries the summaries, and every
reader deep-loads the complete pair. Readers trust the published pair: they check the header schema,
the gzip and JSON format, and the uncompressed ceiling, then validate every point.

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

Readers accept only the V5 storage schema; any other schema fails with
`COV_SCHEMA_VERSION_UNSUPPORTED` naming the Campaign directory to delete or recollect.
One uncompressed point-store ceiling applies on write and read, plus the manifest ceiling. The writer no
longer caps the point count, so a Campaign over the Analyst session's 1M-point ceiling publishes and is
refused at analysis. Persistence schema belongs
to the codecs and loaded envelope rather than the in-memory Coverage Campaign domain value. The split does not itself reduce
the Coverage Analyst model payload. ADR 0063 separately binds analysis to budgeted evidence views
and records the retrieval scope so omitted points remain explicit.
