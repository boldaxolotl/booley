# Withhold invalid Coverage Campaign scores

A Coverage Campaign publishes score summaries only when collection is complete and
the native format is compatible. V4 adds a required top-level `scoring` state. A
valid Campaign has `{status: valid, reason: null}` and exact overall/source
rollups. Every non-complete Campaign has `{status: invalid, reason:
<collection-status>}` and empty overall/source rollup inventories while retaining
its diagnostic Coverage Points, native and hook artifacts, findings, and point-store
integrity reference.

---
status: accepted
---

## Considered options

- Discarding points after a collector failure was rejected because parsed native
  observations and hook evidence explain the failure and remain useful diagnostics.
- Publishing point-derived percentages with a warning was rejected because those
  values look authoritative to summary-only consumers.
- Using `null` rollups was rejected because existing consumers iterate the arrays;
  empty arrays state that no score inventory was published.
- Rejecting every V3 Campaign was rejected because valid retained Simulation
  Campaigns must remain resumable, analyzable, and prunable.

## Consequences

New publication uses `booley.coverage-campaign/v4`. Readers accept valid V3 and
V4 Campaigns, synthesize V3 scoring state from collection status, and continue to
reject V1/V2. A historical V3 Campaign with non-complete collection and nonempty
overall or source rollups is rejected instead of re-publishing its invalid score.
Criteria short-circuit score-dependent checks when scoring is invalid. The Coverage
Analyst keeps its intentionally limited `incomplete` advisory mode, but rejects
collector-error and incompatible evidence before provider invocation.
