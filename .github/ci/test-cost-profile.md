# Test-cost profile (issue #1795)

A measured profile of where `Tests` workflow time goes, ranked by test family
and fixture phase, from ten complete `main` runs. It ranks costs and proposes
optimization candidates; it claims no saving and changes no test, fixture,
shard weight, or timing model. The maintainer files an optimization issue from
a candidate below. Policy lives in [the CI README](README.md); the sibling
record for shard counts is [the Windows experiment](windows-shard-experiment.md).

## Scope and exclusions

- **Excluded: the 6,000-report Job artifact test.**
  `tests/runtime/test_job_artifacts.py::test_real_combined_history_reuses_6000_reports`
  was removed from `main` by merge `231749bcd` (PR #1834, issue #1719) on
  2026-10-19, before the sample window. It is absent from every sampled
  artifact (the analysis also filtered that node name: zero matches in all 40
  platform-run inputs), so no family below contains it.
- Not profiled: the `windows-3.11-compatibility` and `windows-3.13-compatibility`
  jobs (about 2 minutes each), `bwave-smoke`, lint, Rust, and package jobs.
  They appear only in the runner-minute total.
- Out of scope: the existing issues #1600 and #1602 (not re-planned here).
- Platforms are never pooled. Windows 3.14 (ten shards), Ubuntu 3.11 and Ubuntu
  3.14 (one complete-suite job each) are ranked separately; the Ubuntu 3.13
  coverage shards (three) are reported in their own section because coverage
  tracing inflates their times.

## Sample

Ten successful, first-attempt `main` push runs of `Tests`, newest ten at
sampling time, all after merge `50993722d` (PR #1917, 2026-10-20), which added
per-phase timing. SHA range `0c4b5acbe..4fe5edfe7` (55 commits),
2026-10-22T17:07Z to 2026-10-23T11:29Z. Each run has all ten Windows 3.14
shard artifacts, both Ubuntu complete-suite timing artifacts, all three
coverage shard artifacts, and `ci-metrics`. No workflow was dispatched and no
branch was pushed for this record; every number below was read from artifacts
already retained (expire 2027-01-21).

| Run | SHA | Created (UTC) | Gate s | Max job queue s | Eligible tests (Windows / Ubuntu) |
| --- | --- | --- | --- | --- | --- |
| 39819890936 | `0c4b5acbe` | 2026-10-22T17:07:23Z | 1109 | 4 | 22259 / 22271 |
| 39841559123 | `c6b992edb` | 2026-10-22T19:36:22Z | 1149 | 55 | 22257 / 22269 |
| 39847003704 | `7d9531aac` | 2026-10-22T20:13:45Z | 1253 | 89 | 22272 / 22284 |
| 39852297352 | `48628c14e` | 2026-10-22T20:50:29Z | 1657 | 653 | 22264 / 22276 |
| 39861147741 | `fc25628c3` | 2026-10-22T21:56:03Z | 944 | 7 | 22259 / 22271 |
| 39865101274 | `456eab84e` | 2026-10-22T22:27:30Z | 1024 | 4 | 22281 / 22293 |
| 39883264094 | `27a495906` | 2026-10-23T01:18:40Z | 1090 | 3 | 22296 / 22308 |
| 39929370554 | `0c99a85b6` | 2026-10-23T09:05:49Z | 1120 | 5 | 22265 / 22277 |
| 39935665483 | `0402565cc` | 2026-10-23T09:57:51Z | 1098 | 3 | 22269 / 22281 |
| 39946472177 | `4fe5edfe7` | 2026-10-23T11:29:50Z | 1182 | 3 | 22271 / 22283 |

Comparability, checked per platform from the artifacts:

- Configuration is the production one in every run: ten Windows shards
  (`-n 4 --dist=worksteal`), Ubuntu complete suites `-n 4 --dist=loadscope`,
  three coverage shards `-n 4 --dist=loadscope`. Across the SHA range only the
  `bwave-smoke` job and its budget text changed in `.github/workflows/test.yml`
  (`a467ff36c`); no pytest job, shard weight, or timing model changed.
- Eligible-test count (`len(test_phases)`, summed over shards): Windows median
  22,267 (range 22,257-22,296), Ubuntu and coverage median 22,279 (22,269-22,308).
  Every run is within 0.1% of the median, inside the 5% limit. No run was
  replaced. The Windows count is lower than Ubuntu by 12 nodes that Windows
  does not collect.
- Per-run node counts of the leading families (Windows, then Ubuntu/coverage;
  the last four digits of the run ID head each column) are in the tables
  under "Leading families". Every leading family has the same node count in
  every run, so a family did not grow or shrink between SHAs.
- `main` pushes force every job (`FORCE_ALL`, `.github/workflows/test.yml:110-111`).
  The gate and runner minutes below are therefore the `main` job set, not an
  ordinary pull request's.

## Method

1. Data: `test-timings.json` per shard or complete-suite job
   (`phases`, `test_phases` with `setup`/`call`/`teardown` seconds), the
   `ci-metrics` artifact, and job timestamps from the Actions jobs API. Reads
   only; scripts and downloads stay local and uncommitted.
2. A **family** is a test module (node ID before the first `::`). For each
   platform and run, each phase is summed over the family's nodes and over all
   shards of the platform. A module- or class-scoped fixture's setup is charged
   to the first test that uses it on each worker and its teardown to the last,
   so module-level sums keep both with the owning family.
3. Fixtures: `pytest --fixtures -v tests/` (covers conftest fixtures and the
   `tests.timeout_evidence` / `tests.timeout_headroom` plugins loaded through
   `pytest_plugins`) and `--fixtures-per-test` on a sample of the setup
   outliers (phase of one second or more in at least three runs of a
   platform). Fixture closures of all 22,311 collected nodes were dumped by a
   collection-only scratch plugin.
4. **Share of capacity** for a family = its summed seconds inside the slowest
   shard (Windows, coverage; largest `execution_seconds` of that run) or the
   Ubuntu job, divided by that shard's or job's `execution_seconds x 4`
   workers. All-shard totals rank only and are never divided by one shard's
   time.
5. Local checks (suspected-cost section) ran one node per family on a Linux
   workstation, with a scratch `-p` plugin that wraps `subprocess.Popen` to
   count launches and time launch to `wait`/`communicate` return by command,
   plus `cProfile`. Those numbers are labeled **local** and are never mixed
   into the CI medians.

All table cells are `median (min-max)` over the ten runs, in seconds unless
stated.

## Elapsed and runner quantities

These are separate quantities. No elapsed saving is derived by adding
concurrent test time: summed test time is CPU-like work spread over four
workers per job and ten concurrent Windows jobs.

| Quantity | Windows 3.14 (10 shards) | Ubuntu 3.11 | Ubuntu 3.14 | Coverage (3 shards) |
| --- | --- | --- | --- | --- |
| Summed test time (all `test_phases` seconds, all shards) | 21,410.9 (19,658.3-22,243.5) | 3,590.5 (3,067.7-3,816.9) | 3,040.3 (2,293.4-3,991.1) | 5,187.8 (4,141.5-5,971.6) |
| Collection (`collection_seconds`, slowest worker; slowest shard) | 88.5 (84.5-104.1) | 65.6 (60.2-70.2) | 61.7 (41.0-75.7) | 141.9 (127.1-145.3) |
| Execution wall (`execution_seconds`; slowest shard) | 668.2 (652.8-714.0) | 910.5 (778.8-968.6) | 771.1 (581.3-1,012.5) | 510.3 (479.9-537.9) |
| Fastest shard execution | 363.7 (339.3-430.7) | n/a | n/a | n/a |
| Slowest minus fastest shard execution | 307.0 (240.2-347.2) | n/a | n/a | 185.8 (6.1-251.7) |
| Average non-test worker time (`execution_seconds - summed/4`, mean over shards) | 13.7 (10.1-15.2) | 12.2 (9.7-14.4) | 11.0 (8.0-14.7) | 9.0 (8.3-11.5) |
| Job elapsed (`completed_at - started_at`; slowest job of the platform) | 849.5 (825.0-886.0) | 1,036.0 (912.0-1,088.0) | 908.5 (682.0-1,150.0) | 705.0 (662.0-732.0) |

- **Average non-test worker time** estimates idle tail, inter-test gaps, and
  xdist overhead. It is biased up (idle tail of the slowest worker counts in
  full) and down (it averages workers, so the slowest worker's tail is
  larger). There is no per-worker data to bound it.
- Median Windows shard job elapsed: 752.5 (652.5-801.5). The platform's
  slowest job in the workflow is an Ubuntu complete suite in all ten runs
  (3.11 in six, 3.14 in four): 1,074.0 (912.0-1,150.0). The Windows shards do
  not bound the gate in this sample.
- **Required-gate elapsed time** (earliest job `created_at` in the attempt to
  `ci-required` `completed_at`, the definition in
  [the Windows record](windows-shard-experiment.md)): 1,114.5 (944-1,657).
  `ci-metrics` `workflow_elapsed_seconds`: 1,168 (978-1,667). Run 39852297352
  (1,657 s) queued its heavy jobs 525-780 s on runner availability (`ci-metrics`
  maximum job queue 653 s); the other nine runs queued at most 89 s.
- **Runner minutes** (`ci-metrics` `runner_minutes`, whole workflow):
  208.2 (199.6-220.0). Per-platform job minutes are the sum of job elapsed
  over that platform's jobs, with no GitHub per-job rounding and no OS billing
  multiplier: Windows 3.14 shards 117.9 (110.4-123.5), Ubuntu complete suites
  32.2 (27.7-36.3), coverage shards 30.6 (24.8-35.2); the remaining jobs are
  not itemised. The sum of all job elapsed (208.3, range 199.8-220.1) matches
  `ci-metrics`.

## Family ranking by platform

Top ten families by median total (setup + call + teardown) over the ten runs.
"Share" is the slowest-shard or slowest-job worker-capacity share defined in
Method. "Nodes (runs present)" is the median node count and the number of runs
with at least one node.

### Windows 3.14 (sum of ten shards)

| Rank | Family (module) | Total s | Setup s | Call s | Teardown s | Nodes (runs present) | Share of slowest shard/job capacity % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `tests/goals/test_finish.py` | 1317.4 (1245.6-1454.0) | 152.3 (142.8-167.2) | 1163.2 (1096.7-1302.7) | 0.0 (0.0-0.1) | 54 (10) | 7.4 (6.1-9.4) |
| 2 | `tests/goals/test_generated_inputs.py` | 1151.4 (1053.2-1301.7) | 24.3 (22.7-27.4) | 1126.7 (1030.5-1274.3) | 0.0 (0.0-0.1) | 61 (10) | 6.1 (4.3-7.3) |
| 3 | `tests/goals/test_r1_lifecycle.py` | 994.0 (924.3-1035.6) | 204.7 (193.6-215.9) | 791.7 (717.6-827.0) | 0.0 (0.0-0.1) | 46 (10) | 4.6 (2.7-6.4) |
| 4 | `tests/goals/test_finish_authority.py` | 966.1 (887.0-1038.9) | 295.6 (258.3-316.5) | 666.5 (622.2-722.2) | 0.1 (0.1-0.1) | 48 (10) | 4.5 (3.2-7.6) |
| 5 | `tests/harness/test_goal_cli.py` | 791.1 (729.9-850.0) | 374.0 (347.5-407.8) | 417.3 (382.3-442.1) | 0.1 (0.1-0.1) | 83 (10) | 3.6 (2.9-4.8) |
| 6 | `tests/goals/test_goal_waivers.py` | 781.5 (680.8-821.1) | 17.0 (14.4-18.0) | 764.7 (666.3-803.5) | 0.0 (0.0-0.0) | 40 (10) | 3.3 (2.3-4.0) |
| 7 | `tests/goals/test_mcp_routing.py` | 761.3 (686.6-833.5) | 144.8 (133.0-156.3) | 613.0 (553.6-682.7) | 0.1 (0.0-0.1) | 55 (10) | 4.1 (1.9-5.2) |
| 8 | `tests/goals/test_sim_baseline.py` | 703.0 (648.3-753.3) | 209.1 (194.3-231.4) | 491.5 (454.0-521.9) | 0.0 (0.0-0.0) | 57 (10) | 3.2 (2.4-4.1) |
| 9 | `tests/goals/test_test_run_evidence.py` | 565.4 (513.5-603.8) | 112.2 (102.7-118.9) | 453.1 (410.8-484.9) | 0.0 (0.0-0.0) | 29 (10) | 2.6 (2.1-3.5) |
| 10 | `tests/goals/test_completion_package.py` | 531.9 (438.5-589.0) | 23.9 (21.3-26.6) | 507.7 (417.2-564.7) | 0.0 (0.0-0.0) | 59 (10) | 2.0 (1.0-4.7) |

### Ubuntu 3.11 complete suite

| Rank | Family (module) | Total s | Setup s | Call s | Teardown s | Nodes (runs present) | Share of slowest shard/job capacity % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `tests/mcp_tools/test_live_catalog.py` | 111.8 (99.4-126.3) | 0.2 (0.1-0.3) | 111.6 (99.2-126.1) | 0.0 (0.0-0.0) | 21 (10) | 3.1 (3.0-3.3) |
| 2 | `tests/goals/test_finish.py` | 106.8 (87.6-112.4) | 11.7 (9.6-12.6) | 94.8 (77.9-99.7) | 0.1 (0.1-0.1) | 54 (10) | 2.9 (2.8-3.0) |
| 3 | `tests/goals/test_generated_inputs.py` | 101.7 (82.5-104.8) | 2.2 (1.8-2.4) | 99.2 (80.6-102.5) | 0.1 (0.1-0.1) | 61 (10) | 2.8 (2.6-2.8) |
| 4 | `tests/flows/sim/test_verilator_build_reuse_campaign.py` | 89.2 (79.1-100.9) | 0.1 (0.1-0.1) | 89.0 (79.0-100.7) | 0.1 (0.0-0.1) | 11 (10) | 2.5 (2.4-2.6) |
| 5 | `tests/goals/test_r1_lifecycle.py` | 88.5 (73.2-94.2) | 16.9 (14.0-17.7) | 71.1 (59.2-76.5) | 0.1 (0.1-0.1) | 46 (10) | 2.4 (2.3-2.5) |
| 6 | `tests/harness/test_doctor.py` | 88.1 (75.9-92.4) | 3.3 (2.8-3.7) | 84.3 (72.7-88.2) | 0.4 (0.3-0.5) | 568 (10) | 2.4 (2.4-2.5) |
| 7 | `tests/goals/test_finish_authority.py` | 81.8 (67.2-88.2) | 22.8 (18.6-24.3) | 58.8 (48.5-63.9) | 0.1 (0.1-0.1) | 48 (10) | 2.3 (2.2-2.3) |
| 8 | `tests/harness/test_cli_help_wording.py` | 77.0 (66.2-84.2) | 0.5 (0.4-0.6) | 76.5 (65.7-83.6) | 0.1 (0.1-0.1) | 114 (10) | 2.1 (2.0-2.3) |
| 9 | `tests/goals/test_finish_inputs.py` | 66.7 (56.3-73.1) | 1.3 (1.0-1.3) | 65.3 (55.2-71.7) | 0.1 (0.0-0.1) | 34 (10) | 1.9 (1.8-1.9) |
| 10 | `tests/flows/sim/test_endpoint_campaign_lifecycle.py` | 66.4 (58.3-70.4) | 0.3 (0.2-0.4) | 66.0 (57.8-69.9) | 0.1 (0.1-0.2) | 66 (10) | 1.8 (1.7-1.9) |

### Ubuntu 3.14 complete suite

| Rank | Family (module) | Total s | Setup s | Call s | Teardown s | Nodes (runs present) | Share of slowest shard/job capacity % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `tests/goals/test_finish.py` | 93.1 (71.3-121.5) | 10.2 (8.0-13.5) | 83.0 (62.9-107.9) | 0.1 (0.0-0.1) | 54 (10) | 3.0 (2.8-3.2) |
| 2 | `tests/goals/test_generated_inputs.py` | 86.3 (65.6-112.6) | 2.1 (1.5-2.3) | 84.3 (64.0-110.2) | 0.1 (0.1-0.1) | 61 (10) | 2.8 (2.7-3.0) |
| 3 | `tests/mcp_tools/test_live_catalog.py` | 82.5 (62.0-110.8) | 0.1 (0.1-0.2) | 82.4 (61.9-110.6) | 0.0 (0.0-0.0) | 21 (10) | 2.7 (2.4-2.9) |
| 4 | `tests/flows/sim/test_verilator_build_reuse_campaign.py` | 82.3 (59.4-112.1) | 0.1 (0.1-0.1) | 82.2 (59.3-112.0) | 0.0 (0.0-0.1) | 11 (10) | 2.7 (2.4-2.8) |
| 5 | `tests/goals/test_r1_lifecycle.py` | 77.7 (59.5-102.6) | 14.2 (11.1-19.8) | 63.5 (48.4-82.7) | 0.0 (0.0-0.1) | 46 (10) | 2.5 (2.4-2.6) |
| 6 | `tests/goals/test_finish_authority.py` | 71.4 (54.8-94.8) | 19.8 (15.5-26.2) | 51.5 (39.3-68.6) | 0.0 (0.0-0.1) | 48 (10) | 2.3 (2.2-2.4) |
| 7 | `tests/flows/sim/test_endpoint_campaign_lifecycle.py` | 64.8 (43.8-85.2) | 0.2 (0.2-0.4) | 64.4 (43.5-84.7) | 0.1 (0.1-0.1) | 66 (10) | 2.1 (1.9-2.1) |
| 8 | `tests/flows/sim/test_coverage_flow.py` | 63.5 (43.8-82.8) | 0.3 (0.2-0.4) | 63.0 (43.5-82.3) | 0.1 (0.1-0.2) | 113 (10) | 2.0 (1.9-2.1) |
| 9 | `tests/harness/test_doctor.py` | 63.0 (45.6-90.2) | 2.7 (2.0-3.8) | 60.0 (43.3-85.9) | 0.3 (0.2-0.4) | 568 (10) | 2.1 (1.9-2.2) |
| 10 | `tests/flows/sim/test_execution_engine.py` | 61.1 (43.5-81.6) | 0.4 (0.3-0.6) | 60.6 (43.2-80.9) | 0.1 (0.1-0.1) | 123 (10) | 2.0 (1.9-2.1) |

### Reading the rankings

- **Windows is dominated by one subsystem.** All ten leading Windows families
  are Goal Mode modules (`tests/goals/*`, `tests/harness/test_goal_cli.py`),
  and Windows costs them roughly 11-14 times what Ubuntu 3.11 does for the
  same node set (for example `test_finish.py` 1,317 s versus 107 s). No single
  family exceeds 7.4% of a slowest Windows shard's capacity; the cost is
  spread across many modules.
- **Ubuntu is flatter.** Its top families are 62-112 s each, 2-3% of the job.
  `test_live_catalog.py` (in-process MCP server and client, no subprocess) and
  `test_verilator_build_reuse_campaign.py` (fake Verilator toolchain; skipped
  on Windows by `pytestmark`) lead there but are small on Windows.
- **Setup is concentrated in the Goal Mode `layout` fixture.** No setup or
  teardown outlier (a phase of 1 s or more in at least 3 runs) exists on
  Ubuntu 3.11 or 3.14 except one node on 3.14
  (`tests/qa/test_coverage_fault_boundaries.py`). On Windows there are 549
  setup outlier nodes and no teardown outlier; on coverage, 63 setup outlier
  nodes and no teardown outlier. `--fixtures-per-test` on a 23-node sample of
  those outliers (every 25th of 553) resolved 21 through
  `tests/goals/conftest.py::layout` (line 103) or `::goal_mode` (line 263); the
  other two (`test_target_less_parent_ignore_does_not_hide_paired_constraint`,
  `test_actual_local_credential_transport`) have their own fixtures and are
  not in a leading family.
- **No shared-fixture family exists.** The repository defines no session- or
  package-scoped fixture; the only `session` fixtures are pytest/plugin built-ins
  (`tmp_path_factory`, `pytestconfig`, `worker_id`, and similar). The module-scoped
  ones (`tests/bwave/test_contract.py`, `tests/qa/*`, `tests/ci/test_goal_mode_driver.py`,
  Docker and Verilator smoke fixtures) belong to modules outside the top ranks.
  So the rule that moves a shared fixture's setup and teardown into its own
  family selected nothing. The cross-module cost is function-scoped:
  `layout` is built per test by 1,313 nodes in 60 modules (it includes nodes
  that reach it through `goal_mode` or other fixtures). Summed over those
  nodes it is 3,105 s setup + 12,955 s call on Windows (75.2% of Windows summed
  test time), 230 + 1,085 s on Ubuntu 3.11 (36.7%), and 295 + 1,515 s on
  coverage (34.9%). `goal_mode` (348 nodes, 23 modules) adds 1,605 s of
  Windows setup. These are not shared state, so they are listed as context for
  the candidates, not as a family.

## Coverage shards (Ubuntu 3.13, sum of three shards)

Reported separately: coverage tracing inflates these times and the shards are
balanced by the Windows timing model. Coverage totals run 1.4-1.8x Ubuntu 3.11
for the same families.

| Rank | Family (module) | Total s | Setup s | Call s | Teardown s | Nodes (runs present) | Share of slowest shard/job capacity % |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `tests/flows/sim/test_verilator_build_reuse_campaign.py` | 158.5 (114.3-176.3) | 0.1 (0.1-0.1) | 158.3 (114.2-176.1) | 0.1 (0.1-0.1) | 11 (10) | 3.0 (2.4-3.4) |
| 2 | `tests/mcp_tools/test_live_catalog.py` | 154.6 (117.7-176.7) | 0.1 (0.1-0.2) | 154.4 (117.6-176.5) | 0.0 (0.0-0.0) | 21 (10) | 3.6 (1.3-4.2) |
| 3 | `tests/goals/test_finish.py` | 147.8 (119.9-170.4) | 15.6 (12.7-17.7) | 132.5 (106.7-152.5) | 0.1 (0.1-0.1) | 54 (10) | 2.5 (2.1-3.5) |
| 4 | `tests/goals/test_generated_inputs.py` | 140.2 (114.2-163.5) | 2.2 (2.1-2.5) | 137.8 (111.9-160.8) | 0.1 (0.1-0.2) | 61 (10) | 2.7 (2.4-2.9) |
| 5 | `tests/goals/test_r1_lifecycle.py` | 128.6 (100.4-143.2) | 24.3 (17.4-26.4) | 104.1 (83.0-116.7) | 0.1 (0.1-0.1) | 46 (10) | 2.1 (1.9-3.1) |
| 6 | `tests/harness/test_doctor.py` | 121.8 (87.3-140.5) | 3.1 (2.3-3.6) | 118.0 (84.5-136.1) | 0.7 (0.5-0.8) | 568 (10) | 2.5 (1.6-3.0) |
| 7 | `tests/goals/test_finish_authority.py` | 119.4 (92.4-136.1) | 31.8 (26.1-36.1) | 87.2 (66.3-99.9) | 0.1 (0.1-0.1) | 48 (10) | 2.5 (1.6-2.9) |
| 8 | `tests/flows/sim/test_endpoint_campaign_lifecycle.py` | 114.4 (85.1-132.4) | 0.3 (0.3-0.4) | 114.0 (84.7-131.8) | 0.2 (0.1-0.2) | 66 (10) | 1.8 (1.7-2.6) |
| 9 | `tests/flows/sim/test_execution_engine.py` | 111.6 (87.1-131.3) | 0.6 (0.4-0.7) | 110.8 (86.5-130.4) | 0.2 (0.2-0.2) | 123 (10) | 2.5 (1.4-2.8) |
| 10 | `tests/harness/setup/test_scaffold.py` | 108.8 (79.5-128.2) | 0.4 (0.3-0.4) | 108.3 (79.2-127.7) | 0.1 (0.1-0.1) | 71 (10) | 2.2 (1.9-2.4) |

## Leading families

Leading = any family in the top five by median total on a non-coverage
platform, plus any shared-fixture family (none, above). Seven families qualify
(within the cap of ten).

| Family | Windows rank | Ubuntu 3.11 rank | Ubuntu 3.14 rank | Coverage rank |
| --- | --- | --- | --- | --- |
| `tests/goals/test_finish.py` | 1 | 2 | 1 | 3 |
| `tests/goals/test_generated_inputs.py` | 2 | 3 | 2 | 4 |
| `tests/goals/test_r1_lifecycle.py` | 3 | 5 | 5 | 5 |
| `tests/goals/test_finish_authority.py` | 4 | 7 | 6 | 7 |
| `tests/harness/test_goal_cli.py` | 5 | 16 | 15 | 19 |
| `tests/mcp_tools/test_live_catalog.py` | 42 | 1 | 3 | 2 |
| `tests/flows/sim/test_verilator_build_reuse_campaign.py` | 656 (skipped) | 4 | 4 | 1 |

Baseline of each (total = setup + call + teardown, seconds):

| Family | Windows 3.14 (10 shards) | Ubuntu 3.11 | Ubuntu 3.14 | Coverage (3 shards, Ubuntu 3.13) |
| --- | --- | --- | --- | --- |
| `tests/goals/test_finish.py` | 1317.4 (1245.6-1454.0) = 152.3 (142.8-167.2) + 1163.2 (1096.7-1302.7) + 0.0 (0.0-0.1) | 106.8 (87.6-112.4) = 11.7 (9.6-12.6) + 94.8 (77.9-99.7) + 0.1 (0.1-0.1) | 93.1 (71.3-121.5) = 10.2 (8.0-13.5) + 83.0 (62.9-107.9) + 0.1 (0.0-0.1) | 147.8 (119.9-170.4) = 15.6 (12.7-17.7) + 132.5 (106.7-152.5) + 0.1 (0.1-0.1) |
| `tests/goals/test_generated_inputs.py` | 1151.4 (1053.2-1301.7) = 24.3 (22.7-27.4) + 1126.7 (1030.5-1274.3) + 0.0 (0.0-0.1) | 101.7 (82.5-104.8) = 2.2 (1.8-2.4) + 99.2 (80.6-102.5) + 0.1 (0.1-0.1) | 86.3 (65.6-112.6) = 2.1 (1.5-2.3) + 84.3 (64.0-110.2) + 0.1 (0.1-0.1) | 140.2 (114.2-163.5) = 2.2 (2.1-2.5) + 137.8 (111.9-160.8) + 0.1 (0.1-0.2) |
| `tests/goals/test_r1_lifecycle.py` | 994.0 (924.3-1035.6) = 204.7 (193.6-215.9) + 791.7 (717.6-827.0) + 0.0 (0.0-0.1) | 88.5 (73.2-94.2) = 16.9 (14.0-17.7) + 71.1 (59.2-76.5) + 0.1 (0.1-0.1) | 77.7 (59.5-102.6) = 14.2 (11.1-19.8) + 63.5 (48.4-82.7) + 0.0 (0.0-0.1) | 128.6 (100.4-143.2) = 24.3 (17.4-26.4) + 104.1 (83.0-116.7) + 0.1 (0.1-0.1) |
| `tests/goals/test_finish_authority.py` | 966.1 (887.0-1038.9) = 295.6 (258.3-316.5) + 666.5 (622.2-722.2) + 0.1 (0.1-0.1) | 81.8 (67.2-88.2) = 22.8 (18.6-24.3) + 58.8 (48.5-63.9) + 0.1 (0.1-0.1) | 71.4 (54.8-94.8) = 19.8 (15.5-26.2) + 51.5 (39.3-68.6) + 0.0 (0.0-0.1) | 119.4 (92.4-136.1) = 31.8 (26.1-36.1) + 87.2 (66.3-99.9) + 0.1 (0.1-0.1) |
| `tests/harness/test_goal_cli.py` | 791.1 (729.9-850.0) = 374.0 (347.5-407.8) + 417.3 (382.3-442.1) + 0.1 (0.1-0.1) | 58.8 (48.5-60.8) = 28.2 (23.6-30.2) + 30.3 (24.7-31.5) + 0.1 (0.1-0.1) | 51.1 (39.8-65.2) = 26.5 (19.6-31.2) + 24.4 (20.1-33.9) + 0.1 (0.1-0.1) | 76.1 (63.9-88.9) = 35.4 (30.2-41.4) + 40.7 (33.6-47.3) + 0.2 (0.1-0.2) |
| `tests/mcp_tools/test_live_catalog.py` | 120.1 (104.8-128.1) = 0.1 (0.1-0.1) + 120.0 (104.8-128.1) + 0.0 (0.0-0.0) | 111.8 (99.4-126.3) = 0.2 (0.1-0.3) + 111.6 (99.2-126.1) + 0.0 (0.0-0.0) | 82.5 (62.0-110.8) = 0.1 (0.1-0.2) + 82.4 (61.9-110.6) + 0.0 (0.0-0.0) | 154.6 (117.7-176.7) = 0.1 (0.1-0.2) + 154.4 (117.6-176.5) + 0.0 (0.0-0.0) |
| `tests/flows/sim/test_verilator_build_reuse_campaign.py` | 0.0 (0.0-0.0) = 0.0 (0.0-0.0) + 0.0 (0.0-0.0) + 0.0 (0.0-0.0) | 89.2 (79.1-100.9) = 0.1 (0.1-0.1) + 89.0 (79.0-100.7) + 0.1 (0.0-0.1) | 82.3 (59.4-112.1) = 0.1 (0.1-0.1) + 82.2 (59.3-112.0) + 0.0 (0.0-0.1) | 158.5 (114.3-176.3) = 0.1 (0.1-0.1) + 158.3 (114.2-176.1) + 0.1 (0.1-0.1) |

Node counts per run (columns are the last four digits of each run ID, oldest
first), Windows:

| Family | 0936 | 9123 | 3704 | 7352 | 7741 | 1274 | 4094 | 0554 | 5483 | 2177 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `tests/goals/test_finish.py` | 54 | 54 | 54 | 54 | 54 | 54 | 54 | 54 | 54 | 54 |
| `tests/goals/test_generated_inputs.py` | 61 | 61 | 61 | 61 | 61 | 61 | 61 | 61 | 61 | 61 |
| `tests/goals/test_r1_lifecycle.py` | 46 | 46 | 46 | 46 | 46 | 46 | 46 | 46 | 46 | 46 |
| `tests/goals/test_finish_authority.py` | 48 | 48 | 48 | 48 | 48 | 48 | 48 | 48 | 48 | 48 |
| `tests/harness/test_goal_cli.py` | 83 | 83 | 83 | 83 | 83 | 83 | 83 | 83 | 83 | 83 |
| `tests/mcp_tools/test_live_catalog.py` | 21 | 21 | 21 | 21 | 21 | 21 | 21 | 21 | 21 | 21 |
| `tests/flows/sim/test_verilator_build_reuse_campaign.py` | 11 | 11 | 11 | 11 | 11 | 11 | 11 | 11 | 11 | 11 |

Ubuntu 3.11 (Ubuntu 3.14 and coverage are identical to it):

| Family | 0936 | 9123 | 3704 | 7352 | 7741 | 1274 | 4094 | 0554 | 5483 | 2177 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `tests/goals/test_finish.py` | 54 | 54 | 54 | 54 | 54 | 54 | 54 | 54 | 54 | 54 |
| `tests/goals/test_generated_inputs.py` | 61 | 61 | 61 | 61 | 61 | 61 | 61 | 61 | 61 | 61 |
| `tests/goals/test_r1_lifecycle.py` | 46 | 46 | 46 | 46 | 46 | 46 | 46 | 46 | 46 | 46 |
| `tests/goals/test_finish_authority.py` | 48 | 48 | 48 | 48 | 48 | 48 | 48 | 48 | 48 | 48 |
| `tests/harness/test_goal_cli.py` | 83 | 83 | 83 | 83 | 83 | 83 | 83 | 83 | 83 | 83 |
| `tests/mcp_tools/test_live_catalog.py` | 21 | 21 | 21 | 21 | 21 | 21 | 21 | 21 | 21 | 21 |
| `tests/flows/sim/test_verilator_build_reuse_campaign.py` | 11 | 11 | 11 | 11 | 11 | 11 | 11 | 11 | 11 | 11 |

## Suspected-cost checks

Each leading family was read for the suspected costs, and one representative
(the heaviest Ubuntu 3.11 node and the median node of each family) was
profiled locally. Local launches and launch-to-return seconds below come from
the `Popen` plugin; they are **local** values, not CI medians. Result per cell:
**found** (present and measured), **not found**, or **found, not leading**.

| Family | `git init` / `git worktree add` | Report or record files | Child interpreter / process start | Simulation harness |
| --- | --- | --- | --- | --- |
| `test_finish.py` | found: per test via `layout` (init, commit, `worktree add`); heaviest node 46 `git init`, 1,157 process launches | found: Goal records and publication files (`write_record`/`.json`, 6 sites); durable but small | not found in the file; git launches only (404 and 1,157 per profiled node) | not found |
| `test_generated_inputs.py` | found via `layout`; 600 launches in the heaviest node (11 `git init`) | found (4 sites) | found, one helper: `_real_disclosure` uses `_subprocess_invoker` | found, one helper: production `SimulationExecution.ordinary_group` planning disclosure |
| `test_r1_lifecycle.py` | found via `layout`; 651 launches in the heaviest node | found: owned summary, staging, and publication inodes (23 sites) | found: `test_hard_process_death_recovers_only_durably_owned_publication_inodes` runs `sys.executable -c` | not found |
| `test_finish_authority.py` | found via `layout`; 392 launches in a mid node | found: persisted attempt records (20 sites) | not found | not found |
| `test_goal_cli.py` | found: `goal_mode` enters Goal Mode per test (83 of 83 nodes), 323 launches in the heaviest node | not found | not found (the CLI `main()` is called in-process) | not found |
| `test_live_catalog.py` | not found (0 launches) | not found | found, one node: `test_worker_timeout_reaps_importer_and_descendants` starts `sys.executable` plus a sleeping child, by design | found in effect: an in-process MCP server and client; local profile of the heaviest node: 10.2 s waiting on thread locks, 2,364 `compile` calls (AST discovery of built-in endpoints) |
| `test_verilator_build_reuse_campaign.py` | not found | found: `build-execution.json` retained-image record | found: 6 `fusesoc` launches per node (2.4-2.6 s of 7.8-8.6 s local), a fake `verilator`, 76-114 fake compiler launches | found: real `SimulateFlow` path with a fake Verilator toolchain |

What the measurements show:

- **Process launches track the Windows cost.** The profiled Goal Mode nodes
  launch 160-1,157 child processes (mostly `git rev-parse`, `git ls-tree`,
  `git cat-file`; the heaviest node has 623 `git rev-parse` alone). The CI
  Windows median of those same nodes divided by their local launch count is
  65-71 ms per launch (nodes 1, 2, 3, 5, 7, 9: 75.9 s/1,157, 28.7 s/404,
  42.3 s/600, 42.4 s/651, 26.6 s/392, 22.5 s/323), versus 4.4-5.6 ms on Ubuntu 3.11. This
  is an inference: it attributes the whole node to launches and mixes a local
  count with a CI time, so it bounds the launch share from above rather than
  measuring it. The uniform ratio across five modules does make process
  start the strongest candidate cost on Windows.
- Local launch-to-return time is small on Linux (2.0 s of a 3.8 s node for the
  heaviest `test_finish.py` node), so the same candidates barely register
  locally; the cost is Windows process creation.
- `test_live_catalog.py` has no subprocess cost to remove in the profiled
  nodes; its cost is in-process work, and it costs the same on Windows (120 s)
  as on Ubuntu 3.11 (112 s).
- `cProfile` alone sees only `fork_exec`/`poll` for launches (0.26 s and 1.1 s
  for the heaviest `test_finish.py` node), which is why launch counts come from
  the `Popen` plugin.

## Sampling limitations

- **Fixture-scope attribution.** Module-level sums charge a module's fixture
  setup to the first user on each worker and teardown to the last; a
  module-scoped fixture used by a family therefore inflates that family's
  first test, and residual error remains where one module's tests are split
  across workers. No session or package fixture exists to reassign (above), so
  the shared-fixture reassignment rule changed nothing.
- **Scheduler placement.** Ubuntu and coverage use `loadscope` (a module's
  tests stay on one worker, so module setup is paid once per worker); Windows
  uses `worksteal` (a module's tests spread across workers, so per-worker
  module setup repeats). Per-shard placement and setup repetition therefore
  differ by platform and platform rankings are not comparable seconds for
  seconds.
- **Runner variance.** Hosted runners are shared. Ranges are reported next to
  medians: Ubuntu 3.14 summed test time ranges 2,293-3,991 s (56% of the median) across runs.
- **Sample size.** Ten runs in a one-day window; the suite changed between SHAs
  (a few new nodes, 22,257-22,308). This profile ranks costs and claims no
  saving. The 20-run, queue-time-included rule for performance claims
  (`.github/ci/README.md:119-121`) governs the follow-up measurements of the
  candidates, not this ranking.
- **`main` pushes run everything.** Every job and every recovery permutation
  runs on a `main` push (`.github/workflows/test.yml:110-111`,
  `.github/ci/README.md:103-104`), so recovery-family ranks, runner minutes,
  and gate time are larger than on an ordinary pull request.
- **Coverage tracing** inflates the coverage-shard times.
- **No per-worker data.** `phases` is per shard or job; the non-test worker
  time is an estimate with the biases stated above.
- **Local profile.** One Linux workstation, one Python (3.14.6), warm caches;
  the launch-to-time ratio is an inference, not a measurement of Windows.
- **Retention.** The artifacts expire 2027-01-21; the raw inputs are not
  committed.

## Optimization candidates

Each candidate is written so it can be pasted into an issue. The baseline is
the table above; each saving is an **estimate** with the stated reasoning, not
a measurement. The bound table converts an assumed family saving into a job
saving the way the CI shape allows: a family saving `S` seconds (summed over
workers) shortens a job by at most `S` divided by its four workers, and the
gate only when that job is on its critical path.

Gate relevance, from this sample: the two Ubuntu complete suites are the
slowest job in all ten runs, so only a saving in **both** Ubuntu 3.11 and 3.14
shortens the gate (by roughly the smaller of the two). Windows shards,
coverage shards, and the rest are not on the critical path (slowest Windows
shard 849.5 s versus slowest job 1,074.0 s), so a Windows-only saving lowers
runner minutes but not the gate unless Windows becomes the slowest job. The
measured gate baseline is the `main` job set (forced); the before/after gate
comparison uses the ordinary Python-change required-job set, run through the
`windows_shard_benchmark` / `linux_shard_benchmark` dispatch inputs
(`.github/ci/README.md:56-59,88-90`).

**Common measurement conditions (every candidate).** Same runner OS and Python
version, 4 workers, the platform's scheduler (`loadscope` on Ubuntu and
coverage, `worksteal` on Windows), the production shard count and the
checked-in timing model unchanged, the same eligible-test set verified by
`test-verify`, and at least 20 ordinary code-changing runs before and after
with queue time included (`.github/ci/README.md:119-121`) before a durable
saving is claimed.

**Common retained coverage (every candidate).** Real integration cases stay
real for filesystem identity (inode, mode, and path identity), process
behavior (child start, death, and reaping), caching and invalidation,
recovery after a crash or tamper, and production simulation reuse. No shared
mutable fixture state across tests, and no copied linked Git worktrees: each
test keeps building its own repository and worktree. A candidate may reduce
the *number* of redundant process launches or in-process rediscovery inside a
test, not the number of real cases.

Assumed savings and bounds (estimate; ranges are the low and high assumed
family saving applied to the baseline medians):

| Candidate family | Assumed family saving (estimate) | Windows family total saved s | Windows slowest shard shorter by s | Ubuntu 3.11 family saved s | Ubuntu 3.11 job shorter by s | Ubuntu 3.14 job shorter by s |
| --- | --- | --- | --- | --- | --- | --- |
| `tests/goals/test_finish.py` | 30-50% | 395-659 | 15-25 | 32-53 | 8-13 | 7-12 |
| `tests/goals/test_generated_inputs.py` | 30-50% | 345-576 | 12-20 | 30-51 | 8-13 | 6-11 |
| `tests/goals/test_r1_lifecycle.py` | 30-50% | 298-497 | 9-15 | 27-44 | 7-11 | 6-10 |
| `tests/goals/test_finish_authority.py` | 30-50% | 290-483 | 9-15 | 25-41 | 6-10 | 5-9 |
| `tests/harness/test_goal_cli.py` | 14-24% | 111-190 | 3-6 | 8-14 | 2-4 | 2-3 |
| `tests/mcp_tools/test_live_catalog.py` | 20-40% | 24-48 | 0-1 | 22-45 | 6-11 | 4-8 |
| `tests/flows/sim/test_verilator_build_reuse_campaign.py` | 15-30% | 0-0 | 0-0 | 13-27 | 3-7 | 3-6 |

### C1. Cut redundant git launches in `tests/goals/test_finish.py`

- **Baseline.** Windows 1,317.4 s (1,245.6-1,454.0) = setup 152.3 + call 1,163.2
  + teardown 0.0; 7.4% of a slowest Windows shard. Ubuntu 3.11 106.8 s,
  Ubuntu 3.14 93.1 s, coverage 147.8 s. 54 nodes, 54 using `layout`.
- **Cost.** Heaviest node: 1,157 launches (623 `git rev-parse`, 201
  `git ls-tree`), local.
- **Expected.** Family 30-50% faster on the assumption that launches halve
  (the 65-71 ms per launch inference). Windows slowest shard shorter by about
  15-25 s; Ubuntu 3.11 job by 8-13 s, Ubuntu 3.14 by 7-12 s. Gate: on the
  critical path (Ubuntu), so up to the smaller Ubuntu figure, about 7-12 s of a
  1,114 s gate.
- **Conditions.** Common block; verify with `--durations` on the same node set
  on a Windows shard.
- **Must stay real.** `test_nonexcluded_pair_finish_retry_and_next_entry_stay_local`
  (retry and next-entry against a real repository),
  `test_stealth_completion_is_frozen_and_makes_zero_git_writes` (asserts zero
  git writes), `test_recovery_refuses_substituted_immutable_publication_proof`
  (recovery from a tampered record), `test_two_identical_finish_calls_share_one_frozen_result`.

### C2. Cut redundant git launches in `tests/goals/test_generated_inputs.py`

- **Baseline.** Windows 1,151.4 s (1,053.2-1,301.7) = setup 24.3 + call 1,126.7;
  Ubuntu 3.11 101.7 s, 3.14 86.3 s, coverage 140.2 s. 61 nodes via `layout`.
  Setup is small here (24 s), so the cost is in the call phase.
- **Expected.** 30-50%; same reasoning and bound shape as C1 (see table).
  Gate relevance as C1.
- **Conditions.** Common block.
- **Must stay real.** `test_ignored_consumed_project_snapshot_keeps_its_independent_completion_proof`
  (ignored-path identity), `test_copyto_protected_destination_refuses_finish`
  (protected destination on the real filesystem), and the one
  `_real_disclosure` use of the production `SimulationExecution` planning path.

### C3. Cut redundant git launches in `tests/goals/test_r1_lifecycle.py`

- **Baseline.** Windows 994.0 s (924.3-1,035.6) = setup 204.7 + call 791.7;
  Ubuntu 3.11 88.5 s, 3.14 77.7 s, coverage 128.6 s. 46 nodes.
- **Expected.** 30-50% of the launch-bound nodes; this family also writes
  many staging and record files (23 sites), which launch reduction does not
  touch, so the lower figure is more likely.
- **Conditions.** Common block.
- **Must stay real.** `test_hard_process_death_recovers_only_durably_owned_publication_inodes`
  (child interpreter killed mid-publication),
  `test_old_owned_untracked_output_does_not_wedge_new_attempt_and_canonical_is_success`,
  `test_same_git_mode_0664_is_accepted_without_chmod` (file mode identity;
  skipped on Windows), `test_owned_summary_staging_tamper_never_materializes_public_destination`.

### C4. Cut redundant git launches in `tests/goals/test_finish_authority.py`

- **Baseline.** Windows 966.1 s (887.0-1,038.9) = setup 295.6 + call 666.5;
  Ubuntu 3.11 81.8 s, 3.14 71.4 s, coverage 119.4 s. 48 nodes. Setup is 31%
  of the Windows family, so a cheaper `layout`/enter step helps this family
  most.
- **Expected.** 30-50%; same bound shape.
- **Conditions.** Common block.
- **Must stay real.** `test_legacy_unpaired_record_can_freeze_and_retry_with_current_authority`,
  `test_package_reader_refuses_missing_required_facts`,
  `test_all_persisted_attempt_fields_are_bound_to_independent_authority`
  (persisted records read back from a real repository),
  `test_fatal_ignore_inspection_refuses_before_artifacts_or_git_writes`.

### C5. Cheaper Goal Mode entry per test in `tests/harness/test_goal_cli.py`

- **Baseline.** Windows 791.1 s (729.9-850.0) = setup 374.0 + call 417.3
  (47% setup); Ubuntu 3.11 58.8 s (setup 28.2). 83 nodes, all through
  `goal_mode`, which commits a core and enters Goal Mode for every test.
- **Expected.** Cutting 30-50% of the setup half (where the repeated entry
  lives), about 112-187 s on Windows with the call phase unchanged, is 14-24%
  of the family; the bound table uses that 14-24%. Gate: not on the critical
  path (Windows mostly; the Ubuntu 3.11 family is 1.6% of the job).
- **Conditions.** Common block.
- **Must stay real.** `test_goal_status_project_selection_no_option` (real
  Project and worktree selection), `test_goal_status_complete_bytes` (byte
  output from a real entered Goal), the occupying-goal and error-byte cases.

### C6. Reduce in-process rediscovery in `tests/mcp_tools/test_live_catalog.py`

- **Baseline.** Ubuntu 3.11 111.8 s (99.4-126.3) all call; Ubuntu 3.14 82.5 s;
  coverage 154.6 s; Windows 120.1 s. 21 nodes, no `layout`. 3.1% of the Ubuntu
  3.11 job, the largest single Ubuntu family. Heaviest node
  (`test_config_only_refresh_reuses_unfiltered_discovery`): 20.7 s on CI
  Ubuntu 3.11, 21.3 s local, 10.2 s of it waiting on thread locks.
- **Expected.** 20-40% by reusing built-in endpoint discovery across a
  module's cases where the case is not about discovery (low confidence: the
  profile shows wait time in an in-process server, not an obvious duplicate
  scan). See the bound table for the Ubuntu job figures. Gate: on the critical path,
  so the gate could shorten by the smaller Ubuntu figure.
- **Conditions.** Common block.
- **Must stay real.** The live server and client round trip, plus
  `test_worker_timeout_reaps_importer_and_descendants` (child process
  reaping), `test_unchanged_catalog_does_not_rescan_installed_sources` and
  `test_config_only_refresh_reuses_unfiltered_discovery` (these assert the
  caching being optimized), `test_retired_custom_directory_change_refuses_then_recovers`.

### C7. Reduce fake-toolchain launches in `tests/flows/sim/test_verilator_build_reuse_campaign.py`

- **Baseline.** Ubuntu 3.11 89.2 s (79.1-100.9); Ubuntu 3.14 82.3 s; coverage
  158.5 s (rank 1 there); not run on Windows. 11 nodes, 8 s each. Local:
  6 `fusesoc` launches per node cost 2.4-2.6 s of 7.8-8.6 s.
- **Expected.** 15-30% by launching `fusesoc` fewer times per node (for
  example one configuration pass shared by the miss and hit calls of a single
  test). Job figures are in the bound table. Gate: on the critical path
  (both Ubuntu jobs).
- **Conditions.** Common block, Ubuntu only (the module is skipped on Windows).
- **Must stay real.** Everything the module pins: `test_second_identical_flow_sim_reuses_retained_image`,
  `test_flow_sim_input_edit_misses_then_hits` (invalidation),
  `test_flow_sim_pre_sim_firmware_in_run_cwd_keeps_reuse`,
  `test_flow_sim_legacy_pre_sim_change_to_generation_forces_build`, and the
  production `verilator_identity` and read-closure checks; the real
  `SimulateFlow` path stays the path under test.
