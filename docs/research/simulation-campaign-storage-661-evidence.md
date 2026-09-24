# Issue #661 dependency comparison and Campaign storage evidence

Recorded: 24 SEP 2026.

Issue [#661](https://github.com/boldaxolotl/booley/issues/661) adds an
informational dependency-diff diagnostic and protects Simulation Campaign
storage behind the public Campaign package. This report records the exact clean
implementation evidence used for the pull request. The later evidence-only
documentation commit does not change the analyzed production or analyzer files.

## Identities and reproduction

- Base source: `8063cb870c312df9dbde5976e451dddbaf908b89`
- Base source digest:
  `sha256:465a2a42d0d54d73531c29968ccb7e41e2313c8377f6d851e32ea880c921c25d`
- Implementation source: `ccf34bfc0b46bbf7f6599de4a7c6af716a79ceb1`
- Implementation source digest:
  `sha256:046078e03fed9a24b93332b5a352529929bbfe572b232821628ed52a32d5c83a`
- Analyzer repository commit:
  `ccf34bfc0b46bbf7f6599de4a7c6af716a79ceb1`
- Analyzer/comparison semantics digest:
  `sha256:d7349b67a2b80a9b77f8a31a143ddac1d95fd8692d4a951984a5b6d8b9522297`

Both exact commits are archived and analyzed in one process with the same
analyzer bytes:

```console
python3 tests/architecture/compare_report.py \
  --before-ref 8063cb870c312df9dbde5976e451dddbaf908b89 \
  --after-ref ccf34bfc0b46bbf7f6599de4a7c6af716a79ceb1 \
  --repo-root .
```

## Graph result

| Diagnostic | Base | Implementation |
| --- | ---: | ---: |
| Parsed Python modules | 548 | 549 |
| Located dependency facts | 2,828 | 2,832 |
| Unique module edges | 2,344 | 2,347 |
| Direct mutual package pairs | 8 | 8 |
| Nontrivial SCC sizes | 11 and 2 | 11 and 2 |
| Direct `campaign.store` importers | 5 | 3 |
| External `campaign.store` importers | 3 | 0 |

There are no mutual-pair additions/removals, SCC membership transitions, or
changed named-hotspot fan-out values.

Added edges:

```text
booley.flows.sim.campaign -> booley.flows.sim.campaign.inspection
booley.flows.sim.campaign.inspection -> booley.flows.sim.campaign.codec
booley.flows.sim.campaign.inspection -> booley.flows.sim.campaign.model
booley.flows.sim.campaign.inspection -> booley.flows.sim.campaign.planning
booley.flows.sim.campaign.inspection -> booley.flows.sim.campaign.store
booley.flows.sim.campaign_retention -> booley.flows.sim.campaign
booley.flows.sim.coverage_analysis_input -> booley.flows.sim.campaign
booley.flows.sim.coverage_reference -> booley.flows.sim.campaign
```

Removed edges:

```text
booley.flows.sim.campaign_retention -> booley.flows.sim.campaign.store
booley.flows.sim.coverage_analysis_input -> booley.flows.sim.campaign.planning
booley.flows.sim.coverage_analysis_input -> booley.flows.sim.campaign.store
booley.flows.sim.coverage_reference -> booley.flows.sim.campaign.planning
booley.flows.sim.coverage_reference -> booley.flows.sim.campaign.store
```

Affected fan-out:

| Module | Base | Implementation |
| --- | ---: | ---: |
| `booley.flows.sim.campaign_retention` | 6 | 6 |
| `booley.flows.sim.coverage_reference` | 6 | 5 |
| `booley.flows.sim.coverage_analysis_input` | 11 | 10 |
| `booley.flows.sim.campaign.coordinator` | 15 | 15 |
| `booley.flows.sim.campaign.child_protocol` | 12 | 12 |
| `booley.flows.sim.campaign.inspection` | absent | 4 |

## Campaign interface and caller behavior

`authenticate_work_item()` accepts the exact `campaign/manifest.json` path and
one work-item identity. It returns an immutable `CampaignWorkItemEvidence` with
the canonical manifest path and digest, decoded immutable manifest, exact
manifest work item, and authenticated terminal `SimulationResult`. An absent,
duplicate, or incomplete selection raises `SimulationCampaignWorkItemError`;
other corrupt storage raises `SimulationCampaignIntegrityError`.

`inspect_retained_campaign()` returns immutable manifest/summary paths and
digest, completed/pending/interrupted identities, and typed indications of
whether the existing summary's `complete` and `completed` fields agree with
authoritative recovery. It loads only existing records and never regenerates a
summary.

Retention keeps its policy and diagnostic distinctions: incomplete recovery,
summary/projection mismatch, and invalid Campaign storage remain separate.
Coverage reference authentication retains nested Campaign, Target, attempt,
producer, artifact path, size, and digest checks. Coverage analysis consumes the
returned evidence for projection authentication and no longer constructs a
second store or reloads the manifest outside the Campaign interface. Simulation
acceptance and native/full retention continue to call the same Coverage
authentication seam and preserve their public error translation.

## Enforced private seam

D31 forbids every production `booley` source from importing
`campaign.store` or `campaign.inspection`, with four exact live permissions:

| Permission | Exact edge |
| --- | --- |
| C10 | `campaign.coordinator -> campaign.store` |
| C11 | `campaign.child_protocol -> campaign.store` |
| C12 | `campaign.inspection -> campaign.store` |
| C13 | `campaign -> campaign.inspection` |

The production gate asserts every permission is a current exact edge. A separate
AST ownership test rejects static access to `CampaignStore`, `CampaignRecovery`,
`WorkItemRecovery`, and `SharedBuildRecovery` through any Campaign module outside
the exact store owners. Seeded tests cover direct and aliased symbol imports,
module attribute access, deferred and conditional imports, and `TYPE_CHECKING`.
Arbitrary dynamic imports and dynamic-name access remain outside the static
claim.

## Verification

The implementation passed:

- complete architecture suite: 239 passed;
- focused Campaign reports/retention, Coverage reference/analysis, Simulation
  acceptance, and MCP Coverage Analyst selection: 148 passed;
- additional Campaign interface/integrity/manifest regression selection: 76
  passed;
- repository Ruff agent gate and CI check: passed; and
- Ruff formatting check: 1,429 files already formatted after applying the
  formatter to the six changed files it identified.

The broad suite completed with 13,179 passed, 67 skipped, and four unrelated
environment failures. The failures are the same local runner problems recorded
by prerequisite PR #662: the `python` subprocess launcher is unavailable, the
cached FuseSoC entry point names a deleted staging interpreter, and direct QA
subprocesses lack the source-tree `PYTHONPATH`. None is in a changed module or an
affected-path suite.
