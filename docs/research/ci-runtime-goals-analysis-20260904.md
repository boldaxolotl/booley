# CI runtime goals analysis — 2026-09-04

This note explains the failed Phase 9 runtime gates and defines a credible path
to the original targets. It analyzes the frozen Phase 7 comparator
[`4ab0e406a0622728af265b8ae98b9390cc156318`](https://github.com/boldaxolotl/booley/commit/4ab0e406a0622728af265b8ae98b9390cc156318),
the controlled evidence retained under
`/tmp/booley-ci-performance-phase9-20260904`, and the merged
[Phase 9 report](https://github.com/boldaxolotl/booley/blob/3ec4fe9e65737e49ca1bba9e36bfa3ed274f0459/docs/research/ci-performance-final-evaluation-20260904.md).
It does not change workflow behavior and does not revisit the three one-off
pytest failures except as assurance constraints.

## Decision

The six-minute target is not reachable for the frozen forced-full workflow by
removing queueing or collecting more Phase 4/5-sized savings. All six runs that
completed the full workflow missed it, with a descriptive median of **15m58s**.
Their common critical path contained a median **4m53s** candidate build/load,
then a median **7m34s** uncached RISC-V build, **1m18s** of RISC-V validation,
and **1m19s** of native validation. The minimum observed RISC-V build alone was
6m14s. Run 6's 302-second queue was on Ubuntu 3.11, not on the critical path;
excluding that run still produced the protocol's 10m35s unqueued median. The
Phase 9 verdict therefore remains failure: 10m35s unqueued median and 16m13s
all-run nearest-rank p95.

The original plan did deliver real local wins: Phase 4 removed 80 seconds and
Phase 5 removed a former 98-second sidecar segment from the critical path.
What it did not maintain was a closed performance budget. The final workload
added a seven-to-eight-minute RISC-V build after the original observation,
the reported JUnit workload changed from a representative 8,894 cases to
56,631 executed tests per run, the Windows scheduling experiment missed its
25% gate, and
"cache restored" was treated as a proxy for time saved without a hit-rate or
build/load gate. Those effects are larger than the accepted savings.

A credible full-workload route to six minutes requires all four of these
changes together:

1. make RISC-V tooling an immutable, prebuilt input instead of compiling Spike
   on every fresh runner;
2. make the exact candidate cache effective and hold candidate build plus local
   image load near two minutes;
3. overlap the RISC-V lane with native validation; and
4. shard or otherwise shorten every required Windows compatibility leg without
   reducing its tests.

Even then, a backward budget must reserve about two minutes for classification,
packaging, setup, contracts, uploads, and the final aggregate. A separate fast
PR status can be useful, but it is a different user journey: with the frozen
Windows matrix and `bwave-smoke` omitted, the observed proxy was still about
10m25s before aggregation. It cannot be relabeled as achievement of the
forced-full Phase 9 gate.

## What the metric measured

The [fixed protocol](https://github.com/boldaxolotl/booley/blob/96a0b3f38f32f8ce1d7184973045c376f49d271f/docs/research/ci-performance-baseline-20260901.md#L146-L205)
defined end-to-end time as workflow creation through `ci-required` completion.
Manual dispatch forced all conditional jobs, and the frozen classifier made all
nine jobs required. The exact run log records
`required_jobs=changes,docs-check,lint,test,rust-test,bwave-integration,package-artifacts,sidecar-smoke,bwave-smoke`;
the source contract is
[`ci_changes.py`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/scripts/ci_changes.py#L141-L184).
Thus Phase 9 measured **forced full manual validation**, not typical PR feedback.

The available evidence supports these distinct statements:

| User journey | Evidence-backed duration | Meaning |
| --- | ---: | --- |
| Frozen forced-full manual completion | Full runs 1–6: median 15m58s, nearest-rank p95/max 17m52s | The correct full-workload diagnostic; only six observations, so not a replacement 20-run gate |
| Original Phase 9 gate cohort | Unqueued median 10m35s; all-run p95 16m13s | The protocol result, retaining all 14 digest-guard failures |
| Full required set except `bwave-smoke` | Counterfactual 10m21s–10m44s, median 10m31.5s, including each run's observed aggregate latency | Fast-status proxy dominated by Windows; not independently measured as a selected PR cohort |
| Test matrix completion | Last matrix job: 10m15s–10m38s, median 10m25s | All six matrix legs and 56,631 executed tests; excludes aggregate and image assurance |
| Sidecar-only completion proxy | Counterfactual 1m59s–2m15s, median 2m12s, including aggregate latency | Relevant only to a sidecar-only classified PR |
| Docs-only completion proxy | Counterfactual 22–31 seconds, median 24.5s, including aggregate latency | Relevant only to a docs-only classified PR |

There is no single measured "ordinary PR runtime" in this cohort. The
classifier makes it path-dependent: a docs-only PR requires `changes` and
`docs-check`; ordinary Python tests require `lint` and the six-leg matrix;
Python source, Rust, Docker, image-test, packaging, workflow, release, or
unknown/full changes can require the image path. A workload-weighted ordinary
PR median needs a separate manifest of real PR path classes. A full-change PR
on the frozen workflow has essentially the same required DAG as the manual
cohort.

Current main must also not be substituted silently for the frozen workload.
After the comparator, commit
[`bfb46e6b`](https://github.com/boldaxolotl/booley/commit/bfb46e6b8cc4b0dbbb8e8dc2cddbf48710ff9d6b)
path-gated RISC-V work, commit
[`9899f516`](https://github.com/boldaxolotl/booley/commit/9899f516ce885dd3a0a3ffba4307651e8ab6f0f9)
overlapped that lane with native validation, and commit
[`753c69b1`](https://github.com/boldaxolotl/booley/commit/753c69b1f7e16046625a64f4a8fe82803e47249a)
added duration budgets. Current manual/main classification deliberately does
not force `riscv_image`, so a new ordinary manual dispatch is a narrower
workload than Phase 9 unless the replacement protocol explicitly selects the
RISC-V lane.

## Exact critical paths for the six complete runs

At the frozen SHA, `package-artifacts` needs `changes`, `bwave-smoke` needs both
`changes` and `package-artifacts`, and `ci-required` waits for every
classifier-controlled job
([frozen workflow](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L505-L515),
[aggregate](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L876-L900)).
In every completed run, `bwave-smoke` finished after all parallel siblings, so
the exact job chain was:

```text
workflow created
  -> changes
  -> package-artifacts
  -> bwave-smoke
  -> ci-required
```

The following table is reconstructed from each run's `run.json` and
`jobs.json`. A chain cell includes scheduler/dependency gap, that job's queue,
and execution. `CP queue` is only the sum of GitHub's
`started_at - created_at` for the four critical jobs; it excludes dependency
waiting and off-path queues.

| Run | Start + `changes` | `package-artifacts` | `bwave-smoke` | `ci-required` | CP queue | End-to-end |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| [1 / 33803625175](https://github.com/boldaxolotl/booley/actions/runs/33803625175) | 8s | 57s | 14m21s | 7s | 8s | 15m33s |
| [2 / 33805157770](https://github.com/boldaxolotl/booley/actions/runs/33805157770) | 11s | 1m08s | 16m26s | 7s | 9s | 17m52s |
| [3 / 33806872614](https://github.com/boldaxolotl/booley/actions/runs/33806872614) | 7s | 1m05s | 14m46s | 7s | 13s | 16m05s |
| [4 / 33808379022](https://github.com/boldaxolotl/booley/actions/runs/33808379022) | 10s | 1m03s | 14m52s | 8s | 10s | 16m13s |
| [5 / 33809870946](https://github.com/boldaxolotl/booley/actions/runs/33809870946) | 8s | 57s | 14m39s | 7s | 4s | 15m51s |
| [6 / 33811269466](https://github.com/boldaxolotl/booley/actions/runs/33811269466) | 9s | 1m06s | 14m24s | 9s | 15s | 15m48s |

Run 6's maximum eligible-job queue was 302 seconds, but it belonged to Ubuntu
3.11. That job still completed at 22:13:01 UTC, more than seven minutes before
`bwave-smoke` completed at 22:20:16 UTC. Queueing therefore affected resource
availability and the protocol cohort, but not this run's final critical chain.
Across the six runs, critical-path runner queue was only 4–15 seconds; setting
those queues to zero changes the six-run median from 958 to 949.5 seconds, an
8.5-second reduction.

Inside `bwave-smoke`, the frozen steps were serial through RISC-V construction
and validation; only the final eight native validations formed a parallel
group. The table uses contiguous intervals so every second of the job is
accounted for. `Pre` includes checkout, setup, dependency/artifact download,
Buildx, base contract, and base selection. `Std` is cache/layer evidence plus
the standard-image contract/upload. `Prep` is image measurement and Pico
contract preparation; `native` is the internally parallel eight-check group.
All values are seconds.

| Run | Total | Pre | Candidate build/load | Std | RISC-V build | RISC-V contract | Prep | Pico | Native prep | Native | Teardown/post |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 859 | 16 | 212 | 13 | 445 | 13 | 7 | 64 | 2 | 78 | 9 |
| 2 | 983 | 25 | 298 | 14 | 467 | 13 | 7 | 65 | 3 | 79 | 12 |
| 3 | 884 | 32 | 293 | 13 | 389 | 13 | 6 | 52 | 3 | 74 | 9 |
| 4 | 889 | 17 | 214 | 13 | 470 | 13 | 6 | 66 | 2 | 80 | 8 |
| 5 | 877 | 17 | 217 | 13 | 454 | 13 | 7 | 65 | 2 | 80 | 9 |
| 6 | 862 | 27 | 294 | 14 | 374 | 12 | 4 | 52 | 3 | 74 | 8 |

These rows preserve the merged report's warm full-path distributions:
candidate build/load/cache median 4m53s and p95 4m58s; RISC-V build median
7m34s and p95 7m50s; RISC-V validation median 1m18s; native final validation
median 1m19s; and total `bwave-smoke` median 14m44s and p95 16m23s. Runs 7–20
failed the stable-base digest guard in 16–38 seconds and do not enter this
full-path table.

## Why the cache did not create the assumed saving

The trusted-writer policy behaved as designed. All six candidate builds used
`cache-from: type=gha,scope=sandbox`, imported a manifest, emitted no
`cache-to`, and reported no cache error. The evaluation ref created zero cache
entries; Phase 8 remained at a 50 GB ceiling, seven-day retention, and an
owner-confirmed $3 budget. Those facts establish trust and billing behavior,
not cache effectiveness
([frozen cache contract](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L601-L620)).

The retained BuildKit logs explain the remaining 3m32s–4m58s:

- importing a cache manifest took less than a second, but the selected shared
  manifest did not supply all exact fixed-SHA layers;
- on representative run 2, materializing the stable-base-backed candidate
  layer continued to about 175.7 seconds, and exporting/loading the resulting
  image into Docker took 112.2 seconds;
- across the six runs, the final Docker image export/load alone took about
  110–144 seconds; `load: true` is necessary because later steps use
  `docker run`;
- the B-Wave Rust builder compiled again: the imported manifest lacked its
  exact reusable result, and restore-only evaluation runs could not replenish
  it. The single moving `sandbox` scope is consistent with that miss, but the
  retained logs do not distinguish scope replacement from every possible
  cache-key or record-coverage cause. Only two or three terminal operations
  reported `CACHED` per run, while Cargo compilation still took about 43–68
  seconds.

The RISC-V build was more direct: the frozen command used
`docker buildx build --builder default --load` with no external `cache-from`.
Every fresh runner downloaded the toolchain and built the pinned Spike commit
from source. Spike alone took 404.5, 422.3, 345.0, 428.7, 413.2, and 336.4
seconds—88.7%–91.2% of each RISC-V build. The cache-capacity change could not
accelerate a build that never consumed that cache.

Therefore the missing plan gate was **time on a cache hit**, not merely
"manifest imported." A useful gate must record per-layer cached/executed
status, bytes restored, image hydration/load time, and total build step time.

## Wrong or incomplete assumptions

| Assumption | Evidence | Correction |
| --- | --- | --- |
| The historical 10m04s was a normalized before value | The 17-run manifest is incomplete; its representative artifacts contained 8,894 cases, while every Phase 9 run had 57,093 collected and 56,631 executed across six legs | Retain it only as an observational anchor; no percentage improvement/regression claim is normalized |
| Accepted phase savings would add to the desired end state | The 80-second Phase 4 and 98-second Phase 5 savings totaled 178 seconds, while the later RISC-V build added a 374–470-second serial stage before its 64–79-second validation | Recompute the whole critical path after every phase and reject budget-consuming scope growth without an offset |
| More cache capacity plus a trusted writer implied a fast reader | Six imports succeeded, yet candidate time remained 212–298 seconds; RISC-V had no remote cache at all | Gate effective hit rate and wall time per consumer |
| Windows scheduling would provide at least 25% | Phase 3 measured only 6.7% improvement by per-attempt medians and 9.5% pooled for `worksteal`; `load` was 21% slower | Preserve the failed gate; the later `worksteal` adoption bought timeout headroom, not the promised speedup |
| Queueing was the primary median problem | Five complete runs had small maximum queues and the six-run median was 15m58s; critical-path queue totaled only 4–15 seconds, and run 6's 302-second queue was off-path | Treat capacity as a p95/burst concern, separate from serial execution |
| Freezing the repository SHA froze the evaluation | `booley-sandbox-base:main` moved between runs 6 and 7, changing its resolved digest and correctly tripping the source-contract guard | Freeze OCI identities and cache seeds as well as Git/workflow identities |
| "Full CI" was a stable workload definition | The RISC-V candidate contract was added after the initial study; current main later path-gated it and no longer forces it in ordinary manual/main classification | Name and version each user journey and required job set |

The test-count comparison does not prove a literal 6.37× increase in CPU work:
case duration, parametrization, xdist reporting, and platform mix can differ.
It does prove that the cohorts are not count-normalized, and the frozen
workflow also added the candidate RISC-V image contract. That is enough to
reject the 10m04s median as a controlled before value.

## Frozen-DAG lower bound and six-minute budget

Under the frozen implementation, an ideal hit existed only for the candidate's
configured GHA cache; the RISC-V build had no external cache on a fresh runner.
Its observed minimum was 374 seconds, already above the entire six-minute
target before candidate construction, validation, packaging, or aggregation.
Thus zero queue and a perfect candidate-cache hit cannot make the frozen image
lane meet six minutes on the observed runner class.

There is a stronger whole-DAG bound. If every runner queue is set to zero **and
both candidate and RISC-V build steps are assigned zero duration**, the frozen
six-leg matrix becomes the longest required predecessor. Replaying each run's
observed job execution/dependencies and then adding only its observed
`ci-required` execution gives optimistic lower bounds of 612, 625, 639, 630,
628, and 616 seconds: median **626.5 seconds (10m26.5s)**. This intentionally
gives the image builds an impossible advantage and still misses six minutes.
The frozen DAG therefore cannot meet the target through queue and cache fixes
alone; both the image lane and Windows matrix lane must change.

Local image materialization also remains important within the image lane. The
candidate export/load had a roughly 113-second median, and frozen validation
then serially required about 78 seconds for RISC-V and 79 seconds for native
checks. An effective cache must be combined with overlap and a prebuilt
RISC-V input, not treated as a complete solution.

A six-minute median budget should be allocated before implementation. Because
the matrix and image work fan out after classification, this is a parallel-lane
budget, not a sum of every runner's work:

| Budget | Median ceiling | Required change |
| --- | ---: | --- |
| Workflow creation/classification, scheduler gaps, final aggregate | 20s | Keep critical queues small and make aggregation immediate |
| Longest required lane after classification | 330s | Both matrix and image lanes must independently fit |
| Contingency | 10s | Target stages below their ceilings rather than accepting exactly 360s |
| Windows matrix lane | ≤330s | Deterministic sharding or an independently demonstrated scheduler/runner improvement, with unchanged summed tests |
| Image lane: package artifact | ≤45s | Reduce setup or duplication without weakening artifact validation |
| Image lane: `bwave-smoke` setup/contracts/post | ≤35s | Keep immutable resolution local and measure uploads separately |
| Image lane: candidate construction/local availability | ≤120s | Exact input-addressed cache and explicit image-load optimization |
| Image lane: concurrent native plus RISC-V construction/validation | ≤130s | Prebuilt immutable RISC-V tooling plus overlap against the exact candidate |

For the strict p95 below 12 minutes, use a separate 680-second execution
budget: at most 90 seconds of outer orchestration plus a longest-lane ceiling
of 590 seconds (within the image lane, for example 70 seconds package/setup,
180 seconds candidate, and 340 seconds concurrent final work). This leaves
strictly less than 40 seconds for percentile-level queue and scheduler delay.
The median budget is more demanding. Neither budget can be validated with the
fourteen early failures.

## Ranked technical options and falsification experiments

Savings below are engineering estimates against the warm full-path medians and
p95s, not measured results. They are deliberately paired with falsification
criteria.

| Rank | Change / current status | Expected saving | Assurance and cost risk | Experiment that falsifies the estimate |
| ---: | --- | ---: | --- | --- |
| 1 | Publish an immutable RISC-V tooling base keyed by Dockerfile/input digests, then apply the same exact candidate wheel/binary overlay to the standard and RISC-V bases | Reduce RISC-V build from 454s median / 470s p95 to at most 90s / 120s: **364s median, 350s p95** | A reusable tooling base must not substitute a stale application image; preserve base ancestry, contract, size, Pico, checksum, and offline tests. Adds registry/cache storage | Ten complete relevant runs on one frozen input; fail if build/load exceeds 90s median or 120s p95, any layer is mutable/unpinned, or either candidate overlay differs |
| 2 | Make candidate cache scopes input-addressed and durable, seeded only by trusted main; build the B-Wave binary once per run as a verified artifact instead of recompiling it in the image; retain local-load timing as its own gate | Reduce 293s median / 298s p95 to at most 150s / 180s: **143s median, 118s p95** | More scopes may raise storage churn/cost; untrusted refs must remain read-only; exact-SHA reuse must not serve stale wheels/binaries; artifact provenance must bind to the same SHA | Cold seed plus ten restore-only runs; fail if every required layer/artifact is not explainably bound, median/p95 exceed ceilings, the ref exports, or storage projection exceeds $3/month |
| 3 | Shard each Windows compatibility leg without reducing tests | Bring the observed 625s median / 638s p95 matrix-completion proxy below 330s: **about 295s median / 308s p95** once the matrix becomes the long pole | Doubles setup/artifacts and can expose ordering or shared-state assumptions; must retain all 56,631 executions and flake accounting | Ten frozen repeats with deterministic shard manifests; fail if summed node IDs/counts differ, duplicate/missing cases appear, or completion exceeds 330s median/p95 |
| 4 | Overlap RISC-V work and native validation; **already landed after the frozen SHA** in `9899f516` | No-contention model hides the 79s warm native median, at most **about 79s median / 80s p95** on RISC-V-relevant runs | Concurrent Docker/CPU/I/O work can lengthen Spike or native checks; log and cleanup isolation must remain intact | Compare at least ten serial and ten overlapped runs on the same immutable inputs; fail if the critical group does not shrink by at least 60s or flakes/resource failures increase |
| 5 | Path-gate RISC-V work; **already landed after the frozen SHA** in `bfb46e6b` | On a non-RISC-V `bwave-smoke` path, avoids about **532s median / 549s observed p95** (published warm build plus validation); **0s** when RISC-V assurance is selected | Misclassification could skip required candidate coverage. It narrows the workload and therefore cannot satisfy the old forced-full metric | Build a path truth table plus relevant/unrelated PR probes; fail on any false negative. Report RISC-V-selected and unselected cohorts separately |
| 6 | Split a fast PR feedback status from broad image assurance, only if merge queue/main still blocks on the broad lane | For warm runs, omitting `bwave-smoke` moves full-run feedback from 965s median to about 632s: **about 333s median**, still above six minutes until Windows is fixed | Highest semantic risk: "feedback available" is not "safe to merge." Ruleset and `ci-required` semantics must be explicit | Shadow statuses for 20 PRs; fail if any fast-pass later broad-fails for a defect the required policy promises to catch, or if branch protection permits merge before broad assurance |

Option 5 is valuable for ordinary PR feedback but cannot be counted in the
forced-full budget. Option 3 alone also cannot close the gap. Options 1 and 2
attack actual repeated work and must precede any claim that the original full
target is feasible. Sharding changes placement, not test coverage; reducing
the OS/Python matrix or running fewer tests would violate the Phase 9
executed-test constraint unless the assurance policy is separately changed.

A combined warm-median model illustrates the dependency. Whole-workflow
end-to-end is roughly 965 seconds minus 364 (immutable RISC-V), 143 (effective
candidate cache), and about 79 (overlap), or approximately 379 seconds. But
without Windows work the whole required DAG would still wait about 632 seconds
for the counterfactual non-image status. Sharding the matrix below 330 seconds
makes the 379-second image-led path visible again; another 19 seconds of
image/setup improvement is then needed. The proposal is therefore a hypothesis
near the boundary, not a promise.

## Replacement controlled evaluation

The replacement must use a new branch/cohort and preserve failures. It should
not reuse or recreate the completed Phase 9 evaluation branch.

1. Declare two versioned workloads before dispatch: **full assurance**
   (explicitly includes RISC-V regardless of the current manual default) and,
   if desired, **ordinary fast PR feedback** (the exact required jobs and path
   class are named). Keep the original ≤6-minute gate attached to full
   assurance unless governance explicitly changes it.
2. Freeze the repository SHA, workflow blob, action SHAs, contract files, test
   node IDs/counts, and the full classifier output. Verify those identities in
   every accepted run.
3. Resolve every OCI tag before run 1 and dispatch with immutable manifest
   digests. In particular replace the semantic input
   `ghcr.io/boldaxolotl/booley-sandbox-base:main` with its recorded
   `@sha256:...` identity; record the platform manifest/config digest and
   embedded Booley source/contract labels. Do the same for any prebuilt RISC-V
   tooling image.
4. Seed input-addressed BuildKit scopes from a trusted exact-source build,
   record the imported cache manifest/artifact IDs, prevent all cohort refs
   from exporting, and freeze or namespace the seed so unrelated `main`
   writers cannot replace it during the cohort. Snapshot bytes, entries, and
   cost before/after as Phase 9 did.
5. Retain checksums/full SHAs for every fetched toolchain, release asset, Python
   dependency lock, Rust dependency lock, and Git source. A checksum mismatch
   is a valid failed observation, but it must not mutate the workload.
6. GitHub-hosted runner images cannot be pinned by content digest. Use explicit
   labels instead of `*-latest` where available, capture each job's runner-image
   release/provisioner metadata from setup logs, and report drift. If exact OS
   immutability is required, that needs a separately reviewed immutable runner
   image and trust model.
7. Run a cold seed observation separately, then at least 20 sequential complete
   full-assurance runs for the specified nearest-rank p95. Also run a distinct
   multi-ref burst cohort for queue p95; do not infer burst behavior from the
   sequential cohort.
8. Retain every failed run, all six JUnit artifacts, per-layer cache status,
   bytes restored, candidate hydration/load, RISC-V construction/load,
   validation groups, queue/dependency waits, conclusions, confidential-content
   assurance, and the zero-reduced-test gate.
9. Accept only if the full-assurance unqueued median is at most 360 seconds,
   the all-run p95 is strictly below 720 seconds, required/confidential checks
   remain equivalent, failures/flakes do not regress, all 56,631-or-newer
   declared executions remain accounted for, and incremental cache cost stays
   within the owner-confirmed budget.

## Scope of the conclusion

Runs 7–20 remain valid protocol observations: they prove that the mutable OCI
input invalidated a frozen Git evaluation and they correctly contribute to the
official gate statistics. They are not full-workload speed improvements. Runs
1–6 independently prove that the speed target had already been missed before
that external change. The one-off pytest failures remain assurance failures
for any future design, but their diagnosis belongs to the separate flake work.

No Actions run or cache entry was deleted, no workflow behavior was changed,
and neither the immutable Phase 1 baseline nor merged Phase 9 report was
rewritten for this analysis.
