# CI performance fixed-baseline supplement — 2026-09-01

This is the Phase 1 supplement to
[`CI speed options`](https://github.com/boldaxolotl/booley/blob/23e4c7b579005dee49b38ee5150bdf6d33188081/docs/research/ci-speed-options-20260901.md)
in [PR #229](https://github.com/boldaxolotl/booley/pull/229), which remains the
canonical performance study and recommendation at its immutable commit. PR
#229 was closed unmerged on 2026-09-01 with no closure comment; it was not
reopened or changed during this baseline. This note does not repeat that
analysis. It freezes repository/settings evidence, identifies provenance gaps
in the historical timing cohort, and specifies the controlled 20-run final
protocol. No workflow, repository setting, cache, or billing setting was
changed while collecting it.

## Fixed identity and protection

| Field | Baseline | Evidence |
| --- | --- | --- |
| Repository | `boldaxolotl/booley`, public, owned by a personal `User` | [Repository API](https://api.github.com/repos/boldaxolotl/booley) |
| Repository revision | `f288f87a40fe097d5799e7919048d29a5a7d309f` | Local `HEAD` and `origin/main`; immutable [commit](https://github.com/boldaxolotl/booley/commit/f288f87a40fe097d5799e7919048d29a5a7d309f) |
| Research branch | `research/ci-performance-baseline-20260901` | Clean isolated worktree before this note was added |
| Settings/cache observation | `2026-09-01T10:41:41Z` | UTC clock immediately before the final cache-list query |

The active public ruleset is
[`main_protection` (ID 20994145)](https://api.github.com/repos/boldaxolotl/booley/rulesets/20994145).
It applies to `~DEFAULT_BRANCH`, has no bypass actors, blocks deletion and
non-fast-forward updates, requires a pull request, and requires strict GitHub
Actions status checks for exactly `confidential-content` and `ci-required`.
It was last updated at `2026-08-30T11:35:53.982Z`.

The checked-in aggregate agrees: `ci-required` waits for every
classifier-controlled job and resolves the required subset
([`.github/workflows/test.yml`](../../.github/workflows/test.yml), lines
705–728). The classifier's current job contract is in
`.github/scripts/ci_changes.py:29-38,141-157`. These rules and checked-in
contracts are assurance invariants for every performance phase.

After authentication, the classic branch-protection endpoint returned `404
Branch not protected`. Protection is implemented exclusively by active ruleset
20994145. Re-read both surfaces before any phase that changes required checks:

```shell
gh api repos/boldaxolotl/booley/branches/main/protection
gh api repos/boldaxolotl/booley/rulesets/20994145
```

## Account, cache, and budget state

The authenticated REST `GET /user` response does not include a `plan` field.
The owner confirmed **GitHub Free** in the personal billing UI. Authentication
was restored successfully for `boldaxolotl`; the token value, billing amounts,
and screenshot were not recorded.

The remaining settings snapshot is explicit:

| Field | State | Exact source after login |
| --- | --- | --- |
| Account plan | **GitHub Free** | Owner-confirmed personal billing UI; authenticated REST omits the field |
| Classic `main` protection | **None** (`404 Branch not protected`); ruleset 20994145 is the sole protection | Authenticated branch-protection and ruleset APIs |
| Repository cache storage ceiling | **Unavailable until billing is enabled**; endpoint returned HTTP 402 | `GET /actions/cache/storage-limit`, API `2026-03-10` |
| Cache retention limit | **Unavailable until billing is enabled**; endpoint returned HTTP 402 | `GET /actions/cache/retention-limit`, API `2026-03-10` |
| Payment method | **No valid payment method on file**, per both cache-setting endpoints | Authenticated HTTP 402 response; no payment details recorded |
| Existing budgets | Five account-level **$0 stop-usage** budgets: Codespaces, Packages, Actions, Git LFS, and All AI Credit SKUs | Owner-confirmed personal billing budgets UI |
| Existing `actions_cache_storage` budget | **None** | Owner-confirmed personal billing budgets UI |
| Included-usage alerts | **On** | Owner-confirmed personal billing budgets UI |

Authentication, the API checks below, and the owner-facing billing review were
completed before buying Pro:

```shell
gh auth status -h github.com
gh api user --jq '{login, plan}'
gh api repos/boldaxolotl/booley/branches/main/protection
gh api -H 'X-GitHub-Api-Version: 2026-03-10' \
  repos/boldaxolotl/booley/actions/cache/storage-limit
gh api -H 'X-GitHub-Api-Version: 2026-03-10' \
  repos/boldaxolotl/booley/actions/cache/retention-limit
```

Only the non-secret plan and budget fields above were transcribed from
[personal billing](https://github.com/settings/billing) and
[billing budgets](https://github.com/settings/billing/budgets). Payment details,
tokens, invoices, usage amounts, and screenshots were not retained. Before
Phase 8 enables paid cache storage, reconcile the broad $0 Actions stop-usage
budget with the planned repository-scoped $3 `actions_cache_storage` budget;
do not assume the narrower budget overrides the broader stop condition.

### Current cache inventory

The fixed itemized snapshot at `2026-09-01T10:41:41Z` contained **84 entries
and 10,310,455,603 bytes (9.602 GiB)**. Its first-party sources are the
[usage endpoint](https://api.github.com/repos/boldaxolotl/booley/actions/cache/usage)
and [size-sorted inventory](https://api.github.com/repos/boldaxolotl/booley/actions/caches?per_page=100&sort=size_in_bytes&direction=desc).

| Scope or family | Entries | Bytes |
| --- | ---: | ---: |
| `refs/heads/main` | 59 | 8,181,118,433 |
| `refs/pull/228/merge` | 4 | 2,036,961,337 |
| `refs/pull/226/merge` | 21 | 92,375,833 |
| BuildKit blobs | 67 | 4,803,771,062 |
| setup-python/pip | 10 | 5,269,038,780 |
| Rust | 2 | 237,551,760 |
| BuildKit indexes/other | 5 | 94,001 |

The largest object was main-scoped BuildKit cache ID `7197706957`, key
`buildkit-blob-1-sha256:5a54fd31f88b75cb801df1641786d26a1c014d1e168610ceb46fbec926c864e1`,
at 1,864,214,976 bytes. Six exact BuildKit keys occurred under both `main` and
PR 226; the two material pairs consumed 73,902,272 and 72,307,028 bytes across
their two copies. At the baseline SHA, `bwave-smoke` restores and exports the
`sandbox` BuildKit scope even on PRs (`.github/workflows/test.yml:486-503`).

The inventory was actively changing. One usage query observed 61 entries and
10,218,066,716 bytes; the later list query produced the 84-entry snapshot.
PR #229 had recorded 121 entries and 10,488,758,535 bytes at 10:21 UTC.
These non-transactional reads demonstrate churn; they are not one
self-consistent inventory. The 84-entry list is the fixed itemized baseline.
No cache was deleted or altered.

Reproduction after authentication:

```shell
gh api repos/boldaxolotl/booley/actions/cache/usage
gh api --paginate \
  'repos/boldaxolotl/booley/actions/caches?per_page=100&sort=size_in_bytes&direction=desc'
```

## Preserved timing cohorts and provenance

The following values are preserved from PR #229 rather than recomputed here:

| Cohort | Preserved result | First-party anchors |
| --- | --- | --- |
| Successful, unqueued `Tests` | 17 runs; median 10m04s; range 8m34s–12m30s | Representative run [`33494169982`](https://github.com/boldaxolotl/booley/actions/runs/33494169982) was 9m19s. Run [`33492219693`](https://github.com/boldaxolotl/booley/actions/runs/33492219693) independently anchors 10m04s exactly. |
| Saturation | 20m51s and 27m04s | Run [`33493356997`](https://github.com/boldaxolotl/booley/actions/runs/33493356997) anchors 20m51s, including 566s before workflow start. Main-push run [`33462046692`](https://github.com/boldaxolotl/booley/actions/runs/33462046692) anchors 27m04s, including 1,172s before workflow start. |
| Windows pytest, run `33494169982` | Python 3.11/3.13/3.14: 431s / 367s / 287s | Job-step timestamps and JUnit artifact IDs `9795277907`, `9795241735`, `9795196406` |
| Windows slow module, same run | `tests.ticket_board.test_completion`: 390s / 337s / 261s; six JUnit files contained 8,894 cases | PR #229's analysis of the run's six JUnit artifacts |
| `bwave-smoke`, run `33494169982` | total 7m56s; stable-base pull 92s; build/load/cache 184s; sidecar builds 18s; sidecar proofs 70s; final validation 79s | Job-step timestamps |
| Second `bwave-smoke` sample | 107s / 189s / 23s / 69s / 75s for the same five segments | Run [`33492358263`](https://github.com/boldaxolotl/booley/actions/runs/33492358263) |

One provenance gap remains in PR #229: it did not retain the complete 17-run
manifest. Artifact download also now returns `401` without valid
authentication, so the 8,894-case and slow-module values could not be re-read.
Preserve the published values as a historical observational cohort, but do not
present it as the controlled final comparator. After login, download the six
JUnit artifacts for run `33494169982` and retain their parsed summary.

## Controlled 20-run final protocol

The fixed historical 17-run cohort above is the pre-change observational
baseline. Run this protocol once on the frozen final post-plan SHA. This matches
the plan's requirement for 20 comparable final runs without spending a second
20-run campaign reconstructing a baseline that has already passed. Any
before/after claim must identify the historical baseline as observational.

1. Create a temporary remote evaluation branch pointing at the exact SHA.
   Record branch, repository SHA, workflow blob SHA, UTC window, account plan,
   cache ceiling, and cache/budget snapshot. Do not move the branch during its
   cohort.
2. Dispatch `.github/workflows/test.yml` with `workflow_dispatch` on that
   branch. Manual dispatch sets `FORCE_ALL`, so every conditional job runs
   (`.github/workflows/test.yml:47-56` and
   `.github/scripts/ci_changes.py:125-157`). Dispatch sequentially and wait for
   completion before starting the next: the same-ref concurrency group would
   otherwise cancel the preceding run (`.github/workflows/test.yml:16-19`).
3. Accept the first 20 completed dispatches whose `head_sha` is the frozen SHA
   and `run_attempt` is 1. Keep failed runs. Never replace a test, contract, or
   infrastructure failure with a success. Record deliberate cancellation as a
   protocol error and rerun it without removing completed failures.
4. For each run, retain the run ID plus every job's `created_at`, `started_at`,
   `completed_at`, conclusion, runner labels, and step timestamps. Define job
   queue as `started_at - created_at`. Define end-to-end time as workflow
   `created_at` through `ci-required.completed_at`. Retain each Windows pytest
   step and the five `bwave-smoke` segments above.
5. Download all six JUnit artifacts. Record tests, failures, errors, and skips
   per OS/Python leg and summed executed tests. A missing/malformed artifact is
   an assurance failure. Retain the `ci-required` conclusion and URL for every
   dispatch.
6. Test confidential-content assurance separately because dispatching `Tests`
   does not trigger the `pull_request_target` workflow. Dispatch
   `.github/workflows/confidential-content.yml` once on the frozen evaluation
   branch and require its status on the exact SHA to succeed. Also retain the
   required `confidential-content` result from every implementation PR, which
   exercises the privileged PR event path
   (`.github/workflows/confidential-content.yml:3-8,20-50,62-98`). Re-read
   ruleset 20994145 and diff the workflow/guard contracts against this baseline.
7. Snapshot cache usage/list before run 1 and after runs 1, 2, 5, 10, and 20.
   Report run 1 separately from warm runs 2–20. Do not delete caches or reset
   expiry. Retain cache restore/export timings and errors, bytes/entries,
   duplicate keys, eviction, and billing.
8. Define median as the mean of sorted observations 10 and 11. Define p95 by
   nearest rank: sorted observation 19 of 20. Report all 20 runs including
   queueing and the subset whose maximum eligible-job queue is at most 30s.
   Report conclusions and executed-test distributions alongside timing; never
   calculate timing from successes only.
9. Apply the plan's exact final gates: unqueued median at most 6m; all-run burst
   p95 below 12m; unchanged required-check/confidential-content assurance; no
   new flakes or reduced executed tests; incremental cache storage at most
   $3/month. Report phase-specific gates separately so an unrelated speedup
   cannot hide a regression.

Normal PR traffic remains an external-validity cohort, but classifier-selected
job sets and moving SHAs must not be mixed into these controlled statistics.
The sequential cohort intentionally prevents cancellation and is not a Pro
concurrency test. Phase 2 needs a separate burst probe: dispatch full `Tests`
from enough distinct frozen refs to make more than 20 jobs simultaneously
eligible, then prove jobs 21–40 start before the first wave completes.

## Phase 1 completion gate

Phase 1 is complete when:

- this supplement is merged through its isolated baseline PR;
- the account-plan and budget snapshot above remains attached to this baseline;
  and
- PR #229's immutable commit remains the cited source for the historical
  observational cohort. The closed PR itself does not need to be reopened.

Only after those items should the owner perform the manual GitHub Pro purchase.
