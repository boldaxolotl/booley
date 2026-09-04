# CI performance final evaluation — 2026-09-04

This report records the controlled Phase 9 evaluation of Booley's CI
performance plan. The final comparator is frozen at repository commit
[`4ab0e406a0622728af265b8ae98b9390cc156318`](https://github.com/boldaxolotl/booley/commit/4ab0e406a0622728af265b8ae98b9390cc156318),
the merge commit of [PR #309](https://github.com/boldaxolotl/booley/pull/309).
The protocol is the **Controlled 20-run final protocol** in the immutable
[Phase 1 baseline](https://github.com/boldaxolotl/booley/blob/96a0b3f38f32f8ce1d7184973045c376f49d271f/docs/research/ci-performance-baseline-20260901.md#L146-L205).
This report supplements that baseline; it does not rewrite it.

The historical comparison is observational, not a controlled before cohort.
It contains 17 successful, unqueued `Tests` runs with a 10m04s median and an
8m34s–12m30s range. Its incomplete manifest and provenance limits are retained
in the [baseline report](https://github.com/boldaxolotl/booley/blob/96a0b3f38f32f8ce1d7184973045c376f49d271f/docs/research/ci-performance-baseline-20260901.md#L126-L145).

## Decision

**Phase 9 failed.** The unqueued median was 10m35s, above the 6-minute
ceiling, and the all-run nearest-rank p95 was 16m13s, above the strict
12-minute ceiling. Required-check assurance also failed in 14 runs after an
unrelated trusted `main` publication changed the mutable stable-base image
beneath the frozen workflow. Three runs contained one test failure apiece, so
the no-new-flakes gate failed. The executed-test and incremental cache-storage
gates passed: all 20 runs executed 56,631 tests, and the evaluation ref created
no cache entries or bytes.

The first six runs completed the full workflow and had a 15m58s median. Runs
7–20 stopped the `bwave-smoke` path at its digest-contract guard, so their
shorter durations are protocol-valid failed observations, not evidence that
the full workload became faster. No failed or slow run was dropped.

## Frozen identity and protocol compliance

The frozen `Tests` workflow is Git blob
[`de9f36e179520f0b67badfc99706dc46cc1691e9`](https://api.github.com/repos/boldaxolotl/booley/git/blobs/de9f36e179520f0b67badfc99706dc46cc1691e9).
At the frozen commit it exposes manual dispatch and cancels overlapping runs on
the same ref
([`test.yml:3-19`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L3-L19)).
Manual dispatch sets `FORCE_ALL`, and the classifier consequently selects every
conditional job except an unnecessary stable-base rebuild
([`test.yml:48-57`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L48-L57),
[`ci_changes.py:150-184`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/scripts/ci_changes.py#L150-L184)).

| Identity field | Controlled value |
| --- | --- |
| Repository | `boldaxolotl/booley` |
| Frozen repository SHA | `4ab0e406a0622728af265b8ae98b9390cc156318` |
| `test.yml` blob SHA | `de9f36e179520f0b67badfc99706dc46cc1691e9` |
| Evaluation branch | `eval/ci-performance-final-20260904-0040` |
| Cohort UTC start | `2026-09-03T20:40:20Z` |
| Cohort UTC end | `2026-09-04T00:52:38Z` |
| Account plan at start | GitHub Pro, retained owner-confirmed Phase 2 state; authenticated `GET /user` again omitted `plan` |
| Cache storage ceiling | 50 GB, live API readback using version `2026-03-10` |
| Cache retention | 7 days, live API readback using version `2026-03-10` |
| Cache budget | Repository-scoped $3 Actions Cache Storage budget, with 75/90/100 alerts; owner-confirmed billing UI evidence |
| Ruleset | Active `main_protection`, ID `20994145`; strict `confidential-content` and `ci-required`, no bypass actors |

The active protection source was read before and after the cohort from
[`rulesets/20994145`](https://api.github.com/repos/boldaxolotl/booley/rulesets/20994145).
The baseline requires strict `ci-required` and `confidential-content` assurance,
with no classic branch-protection substitute
([baseline lines 23–44](https://github.com/boldaxolotl/booley/blob/96a0b3f38f32f8ce1d7184973045c376f49d271f/docs/research/ci-performance-baseline-20260901.md#L23-L44)).

### Cohort inclusion and exclusions

Accept the first 20 completed sequential dispatches whose `head_sha` is the
frozen SHA and whose `run_attempt` is 1. Keep genuine failures. A deliberate
cancellation is a protocol error and may be replaced, but it must not remove a
completed failure from the cohort
([protocol steps 2–3](https://github.com/boldaxolotl/booley/blob/96a0b3f38f32f8ce1d7184973045c376f49d271f/docs/research/ci-performance-baseline-20260901.md#L158-L167)).

Every row below had the full frozen SHA, `run_attempt: 1`, and was included.
End-to-end time ends at the row's `ci-required` completion.

| Position | Run | Conclusion | End-to-end | Maximum queue | `ci-required` |
| ---: | --- | --- | ---: | ---: | --- |
| 1 | [`33803625175`](https://github.com/boldaxolotl/booley/actions/runs/33803625175) | success | 15m33s | 5s | success |
| 2 | [`33805157770`](https://github.com/boldaxolotl/booley/actions/runs/33805157770) | success | 17m52s | 26s | success |
| 3 | [`33806872614`](https://github.com/boldaxolotl/booley/actions/runs/33806872614) | success | 16m05s | 8s | success |
| 4 | [`33808379022`](https://github.com/boldaxolotl/booley/actions/runs/33808379022) | success | 16m13s | 8s | success |
| 5 | [`33809870946`](https://github.com/boldaxolotl/booley/actions/runs/33809870946) | success | 15m51s | 2s | success |
| 6 | [`33811269466`](https://github.com/boldaxolotl/booley/actions/runs/33811269466) | success | 15m48s | 5m02s | success |
| 7 | [`33812602544`](https://github.com/boldaxolotl/booley/actions/runs/33812602544) | failure | 10m06s | 16s | failure |
| 8 | [`33813459388`](https://github.com/boldaxolotl/booley/actions/runs/33813459388) | failure | 9m45s | 8s | failure |
| 9 | [`33814263929`](https://github.com/boldaxolotl/booley/actions/runs/33814263929) | failure | 10m42s | 8s | failure |
| 10 | [`33815125924`](https://github.com/boldaxolotl/booley/actions/runs/33815125924) | failure | 9m31s | 8s | failure |
| 11 | [`33815872651`](https://github.com/boldaxolotl/booley/actions/runs/33815872651) | failure | 10m07s | 8s | failure |
| 12 | [`33816683880`](https://github.com/boldaxolotl/booley/actions/runs/33816683880) | failure | 10m38s | 6s | failure |
| 13 | [`33817504255`](https://github.com/boldaxolotl/booley/actions/runs/33817504255) | failure | 9m46s | 21s | failure |
| 14 | [`33818245239`](https://github.com/boldaxolotl/booley/actions/runs/33818245239) | failure | 10m16s | 10s | failure |
| 15 | [`33819019626`](https://github.com/boldaxolotl/booley/actions/runs/33819019626) | failure | 10m15s | 3s | failure |
| 16 | [`33819752352`](https://github.com/boldaxolotl/booley/actions/runs/33819752352) | failure | 10m21s | 14s | failure |
| 17 | [`33820547125`](https://github.com/boldaxolotl/booley/actions/runs/33820547125) | failure | 10m47s | 8s | failure |
| 18 | [`33821341003`](https://github.com/boldaxolotl/booley/actions/runs/33821341003) | failure | 10m35s | 9s | failure |
| 19 | [`33822096423`](https://github.com/boldaxolotl/booley/actions/runs/33822096423) | failure | 10m47s | 8s | failure |
| 20 | [`33822854840`](https://github.com/boldaxolotl/booley/actions/runs/33822854840) | failure | 9m43s | 9s | failure |

Two earlier dispatches at the frozen SHA are deliberately excluded from every
statistic: exploratory successful run
[`33787563455`](https://github.com/boldaxolotl/booley/actions/runs/33787563455)
completed before the fresh cohort began, and run
[`33789140839`](https://github.com/boldaxolotl/booley/actions/runs/33789140839)
was deliberately cancelled during prior scope correction. No wrong-SHA or
rerun attempt has entered the controlled branch cohort.

## Timing results

For every included run, retain the workflow ID and creation time; every job's
creation, start, completion, conclusion, and runner labels; and every step's
timestamps and conclusion. Job queue time is `started_at - created_at`.
End-to-end time is workflow `created_at` through `ci-required.completed_at`
([protocol step 4](https://github.com/boldaxolotl/booley/blob/96a0b3f38f32f8ce1d7184973045c376f49d271f/docs/research/ci-performance-baseline-20260901.md#L168-L172)).

For 20 observations, median is the mean of sorted observations 10 and 11, and
nearest-rank p95 is sorted observation 19. Report the complete cohort and the
subset whose maximum eligible-job queue is at most 30 seconds
([protocol step 8](https://github.com/boldaxolotl/booley/blob/96a0b3f38f32f8ce1d7184973045c376f49d271f/docs/research/ci-performance-baseline-20260901.md#L189-L193)).

| Cohort | N | Conclusions | Median end-to-end | Nearest-rank p95 | Range | Maximum eligible-job queue |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| All controlled runs | 20 | 6 success / 14 failure | 10m36.5s | 16m13s | 9m31s–17m52s | 5m02s |
| Run 1, reported separately | 1 | success | 15m33s | 15m33s | 15m33s | 5s |
| Warm runs 2–20 | 19 | 5 success / 14 failure | 10m35s | 17m52s | 9m31s–17m52s | 5m02s |
| Maximum eligible-job queue ≤30s | 19 | 5 success / 14 failure | 10m35s | 17m52s | 9m31s–17m52s | 26s |

The single excluded unqueued observation was successful run 6, whose maximum
eligible-job queue was 302 seconds. The six full successful runs, reported
descriptively rather than substituted for either gate cohort, had a 15m58s
median and 17m52s p95.

### Critical jobs and segments

The frozen workflow uses a six-leg Python matrix with a 15-minute job timeout
([`test.yml:178-190`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L178-L190)).
Windows uses xdist `worksteal`; Linux retains `loadscope`
([`test.yml:201-229`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L201-L229)).

The five protocol segment families are stable-base selection, candidate
build/load/cache, sidecar build/audit, sidecar behavior proofs, and final image
validation. Phase 5 moved the two sidecar segments into an independent job
([`test.yml:445-504`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L445-L504)).
The frozen workflow also contains a serial RISC-V image build and validation
before its native parallel validation group; retain it separately because it
can be the final-state long pole
([`test.yml:647-715`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L647-L715),
[`test.yml:735-855`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L735-L855)).

| Job or segment | Run 1 | Warm median | Warm p95 | Failures / not reached | Observation |
| --- | ---: | ---: | ---: | ---: | --- |
| Windows pytest, Python 3.11 | 8m41s | 8m43s | 9m08s | 2 failures | All 20 reached it |
| Windows pytest, Python 3.13 | 8m30s | 7m47s | 9m02s | 0 | All 20 reached it |
| Windows pytest, Python 3.14 | 8m51s | 8m16s | 9m06s | 0 | All 20 reached it |
| Stable-base selection | 1s | 2s | 3s | 14 failures | Digest guard stopped runs 7–20 |
| Candidate build/load/cache | 3m32s | 4m53s | 4m58s | 14 not reached | Full-path warm N=5 |
| Sidecar build/audit | 17s | 15s | 18s | 0 | Independent path, warm N=19 |
| Sidecar behavior proofs | 1m16s | 1m15s | 1m17s | 0 | Independent path, warm N=19 |
| Candidate RISC-V build | 7m25s | 7m34s | 7m50s | 14 not reached | Full-path warm N=5 |
| Candidate RISC-V validation | 1m17s | 1m18s | 1m19s | 14 not reached | Contract plus Pico, warm N=5 |
| Native final image validation group | 1m18s | 1m19s | 1m20s | 14 not reached | Full-path warm N=5 |
| `bwave-smoke` total | 14m19s | 14m44s | 16m23s | 14 failed early | Full-path warm N=5 |

Warm statistics for segments not reached after the digest mismatch use only the
five warm runs that completed that segment. The RISC-V build remained the
largest serial segment in the full path. For the fourteen guarded failures,
the 16–38 second `bwave-smoke` durations are failure latency and are not mixed
into the full-path segment medians above.

## Test and required-check assurance

Each run must yield all six matrix artifacts named
`junit-{ubuntu-latest,windows-latest}-py{3.11,3.13,3.14}`
([`test.yml:257-263`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L257-L263)).
Record tests, failures, errors, and skips per leg plus summed executed tests. A
missing or malformed artifact is an assurance failure
([protocol step 5](https://github.com/boldaxolotl/booley/blob/96a0b3f38f32f8ce1d7184973045c376f49d271f/docs/research/ci-performance-baseline-20260901.md#L173-L176)).
The checked-in parser defines the four XML counts directly
([`assert_junit.py:20-31`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/scripts/assert_junit.py#L20-L31)).

| OS / Python leg | Artifacts present | Tests distribution | Failures | Errors | Skips | Executed distribution |
| --- | --- | --- | ---: | ---: | --- | --- |
| Ubuntu / 3.11 | 20/20 | 9,518 ×20 | 0 ×20 | 0 ×20 | 28 ×20 | 9,490 ×20 |
| Ubuntu / 3.13 | 20/20 | 9,518 ×20 | 0 ×19; 1 ×1 | 0 ×20 | 28 ×20 | 9,490 ×20 |
| Ubuntu / 3.14 | 20/20 | 9,518 ×20 | 0 ×20 | 0 ×20 | 28 ×20 | 9,490 ×20 |
| Windows / 3.11 | 20/20 | 9,513 ×20 | 0 ×18; 1 ×2 | 0 ×20 | 126 ×20 | 9,387 ×20 |
| Windows / 3.13 | 20/20 | 9,513 ×20 | 0 ×20 | 0 ×20 | 126 ×20 | 9,387 ×20 |
| Windows / 3.14 | 20/20 | 9,513 ×20 | 0 ×20 | 0 ×20 | 126 ×20 | 9,387 ×20 |
| Per-run summed executed tests | 120/120 | 57,093 ×20 | 0 ×17; 1 ×3 | 0 ×20 | 462 ×20 | 56,631 ×20 |

The stable aggregate waits for every classifier-controlled job and resolves the
required subset
([`test.yml:876-900`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L876-L900),
[`ci_required.py:32-48`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/scripts/ci_required.py#L32-L48)).

- `ci-required` succeeded for runs 1–6 and failed for runs 7–20; each exact run
  URL and conclusion is retained in the manifest above.
- Every run completed without cancellation or rerun. All fourteen later runs
  failed `bwave-smoke` after the stable-base contract expected digest
  `76ccd1f3d5b230ea8954beb748e2ce5efe9935dce0c09ce54cf144685ef3a5dc`
  but observed
  `1db6dc329c0e198ed3e53960c4a770c3ae3d9b3fb19fc89c6a31c8354360aaa0`.
- Run 9 additionally failed Windows 3.11
  `test_follow_mode_pauses_on_single_up_and_resumes_at_tail`; run 12 failed
  Ubuntu 3.13 `test_second_sigint_forces_cleanup`; and run 14 failed Windows
  3.11 `test_resize_preserves_follow_mode_or_paused_neighborhood`. These three
  one-off failures are retained as new flakes.
- All 20 runs collected and executed identical counts. The 56,631 executed
  tests per run exceed the historical artifact's 8,894 cases, so there is no
  test-count reduction. This count verdict does not erase the three failures.

### Confidential-content assurance

`Tests` dispatch does not exercise the privileged `pull_request_target` path.
The separate frozen workflow checks out its trusted scanner, scans PR content,
and publishes the required status on the exact candidate SHA
([`confidential-content.yml:3-8`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/confidential-content.yml#L3-L8),
[`confidential-content.yml:20-55`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/confidential-content.yml#L20-L55),
[`confidential-content.yml:70-106`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/confidential-content.yml#L70-L106)).

- Frozen workflow blob: `e40b12d4a61be5555dab173db54fdb2a2bcfa3a1`.
- Exact-SHA manual assurance run:
  [`33787128552`](https://github.com/boldaxolotl/booley/actions/runs/33787128552),
  success on the frozen SHA with a 3m33s job runtime. Its frozen workflow blob
  is unchanged, so the protocol permits reuse instead of another dispatch.
- Privileged PR-event assurance: the required exact-head status was successful
  for [PR #235](https://github.com/boldaxolotl/booley/actions/runs/33504144066),
  [PR #241](https://github.com/boldaxolotl/booley/actions/runs/33605421739),
  [PR #243](https://github.com/boldaxolotl/booley/actions/runs/33614822021),
  [PR #256](https://github.com/boldaxolotl/booley/actions/runs/33622019438),
  [PR #274](https://github.com/boldaxolotl/booley/actions/runs/33630733885),
  [PR #304](https://github.com/boldaxolotl/booley/actions/runs/33737389817),
  [PR #305](https://github.com/boldaxolotl/booley/actions/runs/33740756486), and
  [PR #309](https://github.com/boldaxolotl/booley/actions/runs/33753931593).
- Workflow/guard diff and ruleset verdict: passed. The frozen confidential
  workflow blob remained `e40b12d4a61be5555dab173db54fdb2a2bcfa3a1`, and
  the live ruleset still required strict `confidential-content` and
  `ci-required` checks with no bypass actors before the cohort.

## Cache and billing observations

Take complete cache usage and paginated-list snapshots before run 1 and after
runs 1, 2, 5, 10, and 20. Cache reads are not transactional, so usage/list
count differences must be retained rather than silently reconciled. Do not
delete caches or reset expiry
([protocol step 7](https://github.com/boldaxolotl/booley/blob/96a0b3f38f32f8ce1d7184973045c376f49d271f/docs/research/ci-performance-baseline-20260901.md#L185-L188)).

| Snapshot | UTC | Usage entries | List entries | Usage bytes | List bytes | Duplicate groups / entries / bytes | Additions / evictions |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| Before run 1 | 2026-09-03 20:40:20–22Z | 276 | 277 | 10,700,285,831 | 10,700,285,831 | 9 / 18 / 4,293,773,987 | initial |
| After run 1 | 2026-09-03 20:56:22–24Z | 276 | 277 | 10,700,285,831 | 10,700,285,831 | 9 / 18 / 4,293,773,987 | 0 / 1 temporarily absent |
| After run 2 | 2026-09-03 21:14:38–39Z | 276 | 277 | 10,700,285,831 | 10,700,285,831 | 9 / 18 / 4,293,773,987 | 1 reappeared / 0 |
| After run 5 | 2026-09-03 22:04:33–35Z | 289 | 290 | 12,619,795,623 | 12,619,795,623 | 9 / 18 / 4,293,773,987 | 13 / 0 |
| After run 10 | 2026-09-03 23:03:14–16Z | 324 | 325 | 16,552,085,579 | 16,552,085,579 | 15 / 33 / 9,345,548,939 | 35 / 0 |
| After run 20 | 2026-09-04 00:52:36–38Z | 352 | 353 | 16,591,786,608 | 16,591,786,608 | 15 / 33 / 9,345,548,939 | 28 / 0 |

The frozen candidate build always restores `scope=sandbox`, but only a trusted
push to `main` exports it
([`test.yml:601-620`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L601-L620)).
A manual evaluation dispatch should therefore be restore-only and should not
create an evaluation-ref cache entry.

- Cache restore observations: all six runs that reached the candidate build
  selected `scope=sandbox`, imported a cache manifest, and reported no cache
  error. Runs 7–20 stopped before this step.
- Cache export observations: none of the six candidate-build logs contained a
  `cache-to` export; the other fourteen never reached the build. No cache
  export error occurred.
- Evaluation-ref cache entries: zero entries and zero bytes in all six
  snapshots.
- Cache churn: after run 1, paginated listing repeated ID `7300551658` and
  temporarily omitted ID `7286441183`; the ID reappeared after run 2. This is
  retained as non-transactional pagination evidence, not claimed as an
  eviction. The next 13 additions were seven `main` entries (9,924,573 bytes)
  and six PR #327 entries (1,909,585,219 bytes); the next 35 were 32 `main`
  entries (2,699,714,931 bytes) and three PR #326 entries (1,232,575,025
  bytes); and the final 28 were `main` entries (39,701,029 bytes). None belonged
  to the evaluation ref. Repository-wide usage grew by 5,891,500,777 bytes
  during the cohort because unrelated traffic continued.
- Live storage ceiling and retention endpoints, using GitHub API version
  `2026-03-10`: 50 GB and seven days before and after the cohort
  ([storage-limit](https://api.github.com/repos/boldaxolotl/booley/actions/cache/storage-limit),
  [retention-limit](https://api.github.com/repos/boldaxolotl/booley/actions/cache/retention-limit)).
- User-confirmed Actions Cache Storage budget and alert thresholds: repository
  $3 budget with 75/90/100 alerts, after resolving the overlapping $0 Actions
  stop-usage budget. This remains dashboard evidence, not an API readback.
- Incremental monthly cache-cost calculation: 0 attributable evaluation bytes
  × any storage rate = $0/month. The ≤$3/month gate therefore passed. GitHub's
  run-timing endpoint also returned zero billable milliseconds for all 20
  public-repository runs; it is reported separately from cache cost.

## Phase-specific gates

### Phase 1 — fixed baseline and provenance

The gate required the supplement to merge through its isolated PR, retain the
account-plan and budget snapshot, and preserve PR #229's immutable commit as the
historical source
([baseline lines 207–215](https://github.com/boldaxolotl/booley/blob/96a0b3f38f32f8ce1d7184973045c376f49d271f/docs/research/ci-performance-baseline-20260901.md#L207-L215)).

**Prior disposition:** passed by [PR #235](https://github.com/boldaxolotl/booley/pull/235).

**Final-evaluation check:** passed. The immutable historical comparator, frozen
Phase 7 commit, workflow blobs, controlled run manifest, and excluded-run
provenance are all stated separately.

### Phase 2 — account concurrency

The separate burst probe must expose more than 20 simultaneously eligible jobs
from distinct frozen refs and prove that jobs 21–40 start before the first wave
completes
([baseline lines 200–205](https://github.com/boldaxolotl/booley/blob/96a0b3f38f32f8ce1d7184973045c376f49d271f/docs/research/ci-performance-baseline-20260901.md#L200-L205)).
The sequential Phase 9 cohort deliberately prevents cancellation and is not a
Pro concurrency test.

**Prior disposition:** passed. Three distinct frozen refs exposed 42 workflow
jobs in runs
[`33604270074`](https://github.com/boldaxolotl/booley/actions/runs/33604270074),
[`33604272029`](https://github.com/boldaxolotl/booley/actions/runs/33604272029),
and
[`33604273131`](https://github.com/boldaxolotl/booley/actions/runs/33604273131).
Their first classification jobs and the following waves placed jobs 21–40 on
runners before the initial wave completed. The runs were intentionally stopped
after proving capacity and are not part of Phase 9.

**Final-evaluation check:** passed on retained evidence. The authenticated REST
`GET /user` response still omitted `plan`, so this report does not present the
GitHub Pro plan as a new API readback and does not reinterpret the sequential
Phase 9 queue observations as a burst probe.

### Phase 3 — Windows pytest scheduling

Temporary [PR #241](https://github.com/boldaxolotl/booley/pull/241) required five
attempts on one frozen SHA, identical test/skip/error counts, zero scheduler
failures, and at least 25% better median Windows test time. All scheduler jobs
were safe, but `worksteal` improved only 6.7% by per-attempt medians and 9.5%
by the pooled median; `load` was 21.0% slower. The performance gate therefore
failed and the temporary PR closed unmerged.

This failed gate must not be rewritten as a success. Later
[PR #305](https://github.com/boldaxolotl/booley/pull/305) introduced Windows
`worksteal` and a 15-minute compatibility-job ceiling after ordinary Windows
legs reached the former timeout. That later operational decision is present in
the frozen workflow, but it does not retroactively satisfy PR #241's 25%
performance gate.

**Final-state timing and test-integrity verdict:** the historical Phase 3 gate
remains failed. In the final cohort, warm Windows medians were 8m43s, 7m47s,
and 8m16s for Python 3.11, 3.13, and 3.14. Counts were identical across all
runs, but two Windows 3.11 one-off failures mean final test integrity did not
remain flake-free.

### Phase 4 — remote OCI base resolution

[PR #243](https://github.com/boldaxolotl/booley/pull/243) first shadow-compared
remote and pull-based resolution without removing the pull. After all three
callers agreed on immutable digests, [PR #256](https://github.com/boldaxolotl/booley/pull/256)
required at least 60 seconds saved in base selection, no transfer of the saving
into the following BuildKit build/load, and unchanged green required and
confidential-content checks. Its refreshed acceptance recorded a 2-second
selector, 268-second build/load, and 270-second combined duration against a
350-second shadow baseline: 80 seconds saved.

The frozen `Tests` caller explicitly selects the remote resolver
([`test.yml:541-560`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L541-L560)).

**Controlled-cohort verdict:** the resolver itself remained fast at a 2-second
median and 3-second p95. The digest guard then exposed an operational weakness:
between runs 6 and 7, a trusted `main` publication changed the mutable
`ghcr.io/boldaxolotl/booley-sandbox-base:main` image. Runs 7–20 correctly
rejected the new image because its embedded source digest no longer matched
the frozen workflow's expected digest. The historical Phase 4 timing gate
remains passed, but the final required-assurance gate failed.

### Phase 5 — sidecar extraction

[PR #274](https://github.com/boldaxolotl/booley/pull/274) removed the former
sidecar steps from the `bwave-smoke` critical path. Its accepted result was
98 seconds to zero seconds retained, a 100% reduction above the 81.6% gate. It
also required an independent required sidecar job, at least 86 collected tests,
zero skips/failures/errors, exact pinned source digests, and green
`ci-required` and confidential-content assurance. The frozen workflow enforces
the 86-test and zero-skip floor
([`test.yml:445-504`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/test.yml#L445-L504)).

**Controlled-cohort verdict:** passed. Every run completed the independent
sidecar job with 86 collected, zero skipped, zero failed, and zero errors. The
warm medians were 15 seconds for build/audit and 75 seconds for behavior
proofs, with no sidecar work restored to the `bwave-smoke` critical path.

### Phase 6 — PicoRV32 path gating

[PR #304](https://github.com/boldaxolotl/booley/pull/304) required unrelated PR
paths to skip the informative PicoRV32 workflow while relevant PR paths and
unconditional `main` push, merge queue, nightly, and manual coverage remained.
The frozen event/path contract preserves those cases
([`picorv32-demo.yml:3-24`](https://github.com/boldaxolotl/booley/blob/4ab0e406a0622728af265b8ae98b9390cc156318/.github/workflows/picorv32-demo.yml#L3-L24)).
PicoRV32 is not a required ruleset status, and a `Tests` dispatch does not run
this separate workflow.

**Final source/retained-assurance verdict:** passed. The frozen source preserves
the accepted event and path filters, and PR #304's successful exact-head
required and confidential-content results remain the retained privileged
assurance. The separate PicoRV32 workflow was intentionally not triggered by
the `Tests` cohort.

### Phase 7 — trusted BuildKit cache writer

[PR #309](https://github.com/boldaxolotl/booley/pull/309) required every
validation context to restore the shared sandbox cache, only trusted `main`
pushes to export it, PR/tag/manual contexts to remain restore-only, the first
merged-main run to export successfully, and a repeated main run to show no
slowdown. The frozen cache contract is the exact PR #309 workflow blob cited
above.

**Controlled restore/no-export/performance verdict:** passed for every reached
path. Runs 1–6 restored `scope=sandbox`, imported a cache manifest, emitted no
`cache-to`, and had no cache errors; runs 7–20 stopped at the preceding base
guard. All six snapshots contained zero evaluation-ref cache entries. Retained
trusted-main [writer run
`33791748814`](https://github.com/boldaxolotl/booley/actions/runs/33791748814)
successfully exported the GHA cache. Its candidate build took 284 seconds,
versus 286 seconds in preceding [main run
`33790597623`](https://github.com/boldaxolotl/booley/actions/runs/33790597623),
so no slowdown was observed.

### Phase 8 — paid cache settings and budget

The required live settings are a 50 GB repository cache ceiling and seven-day
retention. The repository-scoped $3 Actions Cache Storage budget and its alert
thresholds are owner-dashboard evidence and must be labeled separately from
API-readable settings.

**Live readback and no-drift verdict:** passed. Immediately before and after
the cohort, the versioned API returned 50 GB and seven days. The $3 repository
budget with 75/90/100 alerts remains owner-confirmed dashboard evidence; it is
not exposed by the repository REST API. Phase 8 was not repeated or changed.

### Phase 9 — controlled final gates

The protocol's exact gates are an unqueued median of at most 6 minutes, an
all-run nearest-rank p95 below 12 minutes, unchanged required-check and
confidential-content assurance, no new flakes or reduced executed tests, and
incremental cache storage of at most $3/month
([baseline lines 194–198](https://github.com/boldaxolotl/booley/blob/96a0b3f38f32f8ce1d7184973045c376f49d271f/docs/research/ci-performance-baseline-20260901.md#L194-L198)).

| Final gate | Measured result | Verdict |
| --- | --- | --- |
| Unqueued median ≤6m | 10m35s across 19 runs | **Failed** |
| All-run nearest-rank p95 <12m | 16m13s across 20 runs | **Failed** |
| Required-check and confidential-content assurance unchanged | `ci-required`: 6 success / 14 failure; confidential-content reused exact-SHA success | **Failed** |
| No new flakes | Three one-off test failures in runs 9, 12, and 14 | **Failed** |
| No reduced executed tests | 56,631 executed in every run; all 120 artifacts present | **Passed** |
| Incremental cache storage ≤$3/month | 0 evaluation-ref bytes, estimated $0/month | **Passed** |

## Limitations and external validity

- The historical comparator is observational and does not have a complete
  manifest. Any before/after statement must retain that qualification.
- Cache usage and cache-list endpoints are independent, non-transactional
  reads. Differences between them are churn evidence, not arithmetic errors.
- Sequential dispatch prevents same-ref cancellation but cannot validate the
  Phase 2 concurrency increase.
- Normal PR traffic has classifier-selected jobs and moving SHAs. It is useful
  external-validity evidence but must not be mixed into controlled statistics.
- User-confirmed billing-dashboard state is not independently API-readable and
  must remain attributed as such.
- Fourteen runs did not measure the full `bwave-smoke` path because the
  stable-base contract guard failed before candidate build and validation.
  Their timings remain in protocol statistics but cannot describe full-path
  throughput.
- GitHub-hosted runner labels and all job/step timestamps were captured, but
  runner assignment and unrelated repository activity were not controlled.
- GitHub's run-timing endpoint reported zero billable milliseconds for this
  public repository; that response is not a general estimate of runner cost.
- Actions artifacts and logs follow GitHub's retention policy. Run pages are
  the durable report citations; the evaluation also downloaded all six JUnit
  artifacts and complete logs while they were available.

## Reproduction sources

The primary API surfaces are:

```shell
gh api repos/boldaxolotl/booley/rulesets/20994145
gh api -H 'X-GitHub-Api-Version: 2026-03-10' \
  repos/boldaxolotl/booley/actions/cache/storage-limit
gh api -H 'X-GitHub-Api-Version: 2026-03-10' \
  repos/boldaxolotl/booley/actions/cache/retention-limit
gh api repos/boldaxolotl/booley/actions/cache/usage
gh api --paginate \
  'repos/boldaxolotl/booley/actions/caches?per_page=100&sort=size_in_bytes&direction=desc'
```

The controlled manifest above links every exact run. Each run's API record,
all-job response with step timestamps and runner labels, complete log archive,
timing response, artifact metadata, and six named JUnit archives were captured
while the cohort ran. Exact retained assurance sources are linked in the
confidential-content and phase sections. The six cache snapshots record both
the usage response and every page of cache IDs, keys, refs, sizes, creation
times, and last-access times; because those endpoints expose mutable repository
state, their acquisition windows are included in the cache table instead of a
misleading mutable URL citation.

## Actions deliberately not taken

No Actions run or cache entry is deleted as part of the evaluation. The frozen
evaluation branch is not moved during the cohort. The Phase 1 baseline is not
edited, and current `main` is not substituted for the frozen comparator.
