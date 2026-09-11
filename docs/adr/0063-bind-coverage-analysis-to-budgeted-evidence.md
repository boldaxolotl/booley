# Bind Coverage Analysis to budgeted evidence

The Coverage Analyst model receives a compact immutable Campaign reference and one
Campaign-scoped, read-only `coverage_evidence` tool. The tool serves bounded `overview`,
filtered/paged `points`, and exact-point `source` views from a completely validated Campaign;
point responses preserve the complete disposition, including Approved Waiver provenance.
Each response and the whole analysis session have byte budgets, and the report records the
retrieval scope and exact point identifiers used.

---
status: accepted
---

## Considered Options

- Embedding the complete Campaign and verified source closure in the prompt was rejected because
  valid collected Campaigns can exceed the model backend's input limit before analysis starts.
- Passing a filesystem path without a scoped query interface was rejected because it grants the
  model more authority than analysis needs and does not bound retrieval.
- Paging directly from compressed V3 storage was rejected because point-dependent effects must not
  precede complete digest, count, trailing-data, and record validation.
- Summary-only analysis was rejected because recommendations and Waiver Candidates require exact,
  source-bound Coverage Point references.

## Consequences

The host deep-loads V3 evidence before model invocation, then binds the validated Campaign to an
isolated evidence session. Persisted Campaigns produce `booley.coverage-analysis/v2` reports that
retain the manifest and point-store digest instead of duplicating every point; the in-memory
compatibility seam produces `booley.coverage-analysis/v1`. After complete validation, the session
indexes exact IDs, derives overview counts once, caches one filtered match set for consecutive
pagination, and encodes only returned pages. The cache is bounded by the V3 point ceiling and dies
with the isolated session. These V1/V2 analysis-report contracts and their query semantics remain
compatible. The model has no general filesystem or execution tools. An exhausted
evidence or model-context budget fails with an actionable diagnostic, while the audit makes partial
analysis explicit rather than implying every point was inspected. Full Campaign loading remains a
linear cost and no constant-memory claim is made.
