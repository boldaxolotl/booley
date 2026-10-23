# Expensive test inventory

Issue #1797 audits repository-wide scans, duplicate scenarios, and permutation
families in the Python suite. This is a measured record. Every disposition is a
proposal. It changes no test, tier, classifier rule, coverage threshold, or
workflow. After the maintainer approves a row, a follow-up issue implements it
with its own before/after measurement; this record's figures never stand in for
that measurement.

## Scope and exclusions

Reviewed: nine candidates (C1-C9, below), chosen because each is a
repository-wide scan, a test of CI or repository configuration, or a
parametrized recovery or authority family whose cost is large on at least one
platform. C6 goes beyond the issue's examples because it is the same kind of
whole-tree scan. Goal family selection (C7-C9) is rule-based, not hand-picked:
parametrized test functions under `tests/goals/` with at least four cases whose
function name contains `recover`, `authority`, `crash`, `interrupt`, or
`tamper`, ranked by the larger of the Ubuntu 3.14 and Windows 3.14 (ten-shard
sum) medians, keeping every function at or above 5 s, at most twelve. More than
twelve qualified; the top twelve by rank are reviewed. The next three
(`test_abandon.py::test_interrupted_entering_is_classified_and_retained`, about
17 s on Windows 3.14 and 1 s on Ubuntu 3.14, and two `test_finish_inputs.py`
functions about 16 s on Ubuntu 3.14 and absent from the Windows timings) fall
under the cap and are not reviewed.

Excluded, already owned elsewhere: the `check_suppressed_assertions` pytest
duplicate (#1796, removed), the 6,000-report Job artifact test (#1719, removed
by `231749bcd`), fixture and body speed-ups (#1601; #1795's profile), and
Ubuntu sharding (#1600). Open Slim issues checked at implementation time
(`gh issue list --state open --search "Slim in:title"`): #1644, #1646, #1659,
#1689, #1701. None rewrites C1-C9.

### Not reviewed

Non-Goal recovery and crash families are outside this record because the issue
names the Goal families. They are measured here so "every reviewed candidate"
is not read as "every expensive family". Reviewing them is a follow-up row.

| Not reviewed module | Nodes | Ubuntu 3.11 | Ubuntu 3.14 | Ubuntu 3.13 coverage | Windows 3.14 |
| --- | ---: | --- | --- | --- | --- |
| `tests/flows/sim/test_campaign_crash_matrix.py` | 60 | 3.5 (2.7-4.7); 0.4 | 3.7 (2.1-6.0); 0.3 | 7.6 (5.1-13.3); 0.4 | 5.9 (4.9-9.2); 0.3 |
| `tests/harness/test_session_refresh_recovery.py` | 60 | 0.3 (0.2-0.3); 0.3 | 0.3 (0.2-1.1); 0.3 | 0.4 (0.3-0.5); 0.4 | 0.5 (0.4-5.2); 0.3 |
| `tests/runtime/test_session_issuance.py` | 248 | 2.7 (1.7-2.9); 2.8 | 2.5 (1.6-3.0); 2.4 | 4.8 (3.5-5.6); 5.1 | 5.5 (4.7-6.2); 5.4 |

## Sample and method

The 20 newest successful, first-attempt `main` push `Tests` runs after
`50993722d` (per-phase timing, PR #1917), SHA range `17a2f1800..4fe5edfe7`,
2026-10-21T19:16Z to 2026-10-23T11:29Z, satisfying the 20-run rule in
`README.md` ("Performance telemetry"). Every run has all artifacts used here
(`pytest-timing-ubuntu-3.11-full`, `pytest-timing-ubuntu-3.14-full`,
`shard-windows-0..9`, `shard-coverage-0..2`,
`junit-windows-3.11-compatibility`, `junit-windows-3.13-compatibility`,
`ci-metrics-<run>-1`). Data came from `gh run download <run> -n <artifact>` and
`gh api repos/boldaxolotl/booley/actions/runs/<run>/jobs`; artifacts were read
with `python3 -I` from separate directories.

| # | Run | SHA | Required gate (s) | Ubuntu 3.11 (s) | Ubuntu 3.14 (s) | Slowest Windows shard (s) | Slowest coverage shard (s) | Runner min | Workflow elapsed (s) |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 39946472177 | 4fe5edfe7 | 1182 | 1030 | 1150 | 844 | 732 | 220.0 | 1279 |
| 2 | 39935665483 | 0402565cc | 1098 | 1042 | 1068 | 848 | 732 | 203.9 | 1178 |
| 3 | 39929370554 | 0c99a85b6 | 1120 | 1088 | 743 | 879 | 665 | 207.2 | 1204 |
| 4 | 39883264094 | 27a495906 | 1090 | 1065 | 938 | 840 | 689 | 209.4 | 1100 |
| 5 | 39865101274 | 456eab84e | 1024 | 995 | 775 | 851 | 712 | 199.6 | 1032 |
| 6 | 39861147741 | fc25628c3 | 944 | 912 | 879 | 840 | 717 | 210.3 | 978 |
| 7 | 39852297352 | 48628c14e | 1657 | 982 | 682 | 871 | 702 | 205.0 | 1667 |
| 8 | 39847003704 | 7d9531aac | 1253 | 981 | 1101 | 886 | 662 | 211.5 | 1264 |
| 9 | 39841559123 | c6b992edb | 1149 | 1052 | 1111 | 825 | 682 | 209.1 | 1158 |
| 10 | 39819890936 | 0c4b5acbe | 1109 | 1080 | 787 | 864 | 708 | 205.5 | 1118 |
| 11 | 39814013201 | 69bbf0661 | 932 | 902 | 858 | 831 | 688 | n/a (see note) | 942 |
| 12 | 39788276412 | c5eba0d06 | 1118 | 1088 | 708 | 856 | 693 | 202.9 | 1126 |
| 13 | 39783464340 | 4941ad405 | 1024 | 992 | 785 | 848 | 704 | 210.1 | 1032 |
| 14 | 39775647416 | d5287881a | 1123 | 1020 | 1092 | 837 | 708 | 224.4 | 1194 |
| 15 | 39769222765 | 68cfd9b78 | 1153 | 1077 | 1124 | 853 | 720 | 211.4 | 1267 |
| 16 | 39766062925 | 6ceaf7b1b | 1111 | 1048 | 1081 | 797 | 703 | 203.0 | 1120 |
| 17 | 39762751706 | beda16c53 | 1085 | 1051 | 714 | 820 | 747 | 210.5 | 1092 |
| 18 | 39693461241 | dcf45499e | 1145 | 742 | 1114 | 827 | 719 | 199.9 | 1228 |
| 19 | 39672962583 | 528cbadda | 1587 | 994 | 975 | 843 | 723 | 222.4 | 1667 |
| 20 | 39660136688 | 17a2f1800 | 1148 | 744 | 1114 | 868 | 711 | 217.7 | 1258 |

Limitation: `main` pushes force every job (`FORCE_ALL`, `test.yml:110-111`), so
this baseline is the `main` job set. The ordinary code-PR gate is measured
separately below from the three dispatched benchmark runs and is never pooled
with these 20.

Cost per candidate is the sum of its nodes' `test_phases` per leg per run, then
the median and min-max over the 20 runs: Ubuntu 3.11 and 3.14 complete suites;
Windows 3.14 as the sum of the ten shards (each node runs in one shard); Ubuntu
3.13 coverage as the sum of three shards (tracing inflates it, so it is its own
column). Cells read `call median (min-max); setup+teardown median`, in seconds.
Setup is charged to the first user of a fixture and moves with scheduling
(`README.md`, "Shard safety"), so it is never counted as saved unless a whole
module and its module fixtures leave the leg. The Windows 3.11/3.13 compatibility
legs run `tests/architecture` and `tests/config` plus four runtime/sim modules
(`test.yml:328-336`); they report JUnit `time` only, labeled JUnit, not
per-phase. Sums of concurrent seconds are test cost, never elapsed time. Where a
candidate is absent, the cell says so.

## Before baseline (20 `main` runs)

| Measure | Median (min-max) |
| --- | ---: |
| Required-gate elapsed, earliest job creation to `ci-required` completion (s) | 1119 (932-1657) |
| Ubuntu 3.11 complete job (s) | 1025 (742-1088) |
| Ubuntu 3.14 complete job (s) | 956 (682-1150) |
| Slowest Windows 3.14 shard (s) | 846 (797-886) |
| Fastest Windows 3.14 shard (s) | 480 (453-639) |
| Slowest Ubuntu 3.13 coverage shard (s) | 708 (662-747) |
| Windows 3.11 / 3.13 compatibility job (s) | 126 (99-163) / 106 (79-129) |
| `ci-metrics` runner minutes (19 runs, see note) | 209 (200-224) |
| `ci-metrics` workflow elapsed (s) | 1168 (942-1667) |

On `main` the slowest Python test job was an Ubuntu complete suite in all 20
runs (`ubuntu-3.11-full` 11 times, `ubuntu-3.14-full` 9 times), and `test-verify`,
which needs them, started last before `ci-required` in all 20. On those runs a
saving that only shortens Windows shards (about 14 minutes at the median) or the
coverage shards cannot shorten the gate. The PR-shaped runs below differ, so
the critical job must be named per measurement and not assumed. Note: run
39814013201 reported `ci-metrics` `completed_jobs` 12 of 29 and `runner_minutes`
32.5, an under-count at observation time, so it is excluded from the
runner-minute figures only; its job timings are used.

## PR-shaped gate baseline (3 dispatched runs, indicative)

Three `Tests` runs were dispatched one at a time on the ticket branch with
`gh workflow run Tests --ref codex/issue-1797-ci-cost-inventory -f windows_shard_benchmark=true`
(production ten Windows shards, Ubuntu defaults; maintainer decision d1797-1
approved three). The benchmark option selects the ordinary Python-change job
set (`ci_changes.py:495-496`), so image, Rust, sidecar, and `bwave-smoke` jobs
are skipped. The branch head was `4fe5edfe7`, the same tree as `main`. All three
succeeded; none was cancelled or rerun for infrastructure. They are reported
separately and never pooled with the 20 `main` runs.

| # | Run | SHA | Required gate (s) | Critical job (last Python job before `test-verify`) | Its queue / run (s) | Max job queue (s) | Ubuntu 3.11 / 3.14 run (s) | Slowest Windows shard run (s) | Runner min | Workflow elapsed (s) |
| ---: | --- | --- | ---: | --- | --- | ---: | --- | ---: | ---: | ---: |
| 1 | 40015199657 | 4fe5edfe7 | 1388 | test / windows-3.14-shard-5-of-10 | 408 / 841 | 408 | 742 / 729 | 857 | 186.4 | 1576 |
| 2 | 40018653026 | 4fe5edfe7 | 1106 | test / windows-3.14-shard-3-of-10 | 193 / 809 | 193 | 749 / 694 | 847 | 181.5 | 1117 |
| 3 | 40021121972 | 4fe5edfe7 | 1390 | test / ubuntu-3.14-full | 240 / 1120 | 459 | 748 / 1120 | 813 | 183.9 | 1400 |

Observations, each from the table or the runs' job lists:

- Required gate was 1106 to 1390 s (median 1388 s). The critical job's queue
  time was 408, 193, and 240 s: gate elapsed here is queue plus the slowest job,
  and queueing depends on runner availability when dispatched, not on test cost.
- The critical job was a Windows shard in two of three runs and
  `ubuntu-3.14-full` in the third (1120 s against 694 to 749 s in the other
  runs, one slow outlier). The Ubuntu complete jobs ran 694 to 749 s here against
  medians of 1025 s (3.11) and 956 s (3.14) on `main`, so a PR-shaped gate is not Ubuntu-bound the
  way the `main` baseline is; a Windows saving can move it.
- Runner minutes were 181.5 to 186.4, against a 209 median on `main`, because
  the skipped jobs do not run.

Consequence for estimates: Windows seconds saved shorten the gate only through
the slowest shard. On `worksteal` with four workers per shard and ten shards,
a total saving S moves a shard by about S / 40 if the timing model rebalances
(P1: about 19 s); Ubuntu `loadscope` legs move between zero and the module
saving. Both are bounds, not predictions.

## The exhaustive-recovery pattern as it was implemented

`.github/ci/README.md` ("Exhaustive recovery policy") and
`docs/internals/CONTRIBUTING.md` ("Agent source-development checks") describe a
mechanism that no longer exists. Commit `e384f7122` (2026-09-06) added it:
`pyproject.toml` marker `exhaustive_recovery`;
`_EXHAUSTIVE_RECOVERY_PREFIXES` (`.github/ci/`, `.github/scripts/`,
`.github/workflows/`, `src/booley/ticket_board/`, `tests/ticket_board/`) and
`_EXHAUSTIVE_RECOVERY_FILES` (`pyproject.toml`,
`src/booley/flows/baseline_worktree.py`, `src/booley/runtime/git.py`,
`src/booley/core/scope_matching.py`, `tests/conftest.py`) with
`_recovery_categories` in `ci_changes.py`; an `exhaustive_recovery` classifier
output; the mark expression `not native_bwave and not exhaustive_recovery` in
`test.yml` when the category was absent; classifier tests in
`tests/ci/test_change_classifier.py`
(`test_recovery_sensitive_changes_request_exhaustive_permutations`,
`test_unknown_path_fails_safe_to_exhaustive_recovery`); and
`test_exhaustive_recovery_marker_retains_a_pr_subset` in
`tests/test_pytest_configuration.py`. Commit `1460ad065` (2026-10-11) removed
it with the Ticket Board (see `git show 1460ad065^:.github/scripts/ci_changes.py`).
Today
`pyproject.toml:261-265` has no such marker, `ci_changes.py` has no such
category, and `test.yml:299,376` run `-m 'not native_bwave'`, so every Goal
recovery and authority permutation runs in full on every code PR. The policy
text is stale; Q2 of the plan leaves its rewrite to the maintainer, and the
summary table carries it as row S4.

Pattern a tier must follow: representative before/after, repository-role, and
first/last checkpoint cases run on every code PR; the complete set runs when a
named dependency changes, when a path is unclassified (`ci_changes.py:248-255`
makes unknown paths `full`), on `main` pushes (`FORCE_ALL`), and in
`full-python-matrix.yml` (Sunday 03:17 UTC, Ubuntu and Windows x 3.11/3.13/3.14,
complete suite, `-m "not native_bwave"`).

## Inventory

Legs per code PR that run a candidate: a full-suite test runs on four legs
(Ubuntu 3.11, Ubuntu 3.14, Windows 3.14 in one shard, Ubuntu 3.13 coverage in
one shard); `tests/architecture` also runs on both Windows compatibility legs,
six in total. `main` and the weekly matrix add Windows/Ubuntu 3.11/3.13/3.14.

| Candidate | Nodes | Ubuntu 3.11 | Ubuntu 3.14 | Ubuntu 3.13 coverage (3 shards) | Windows 3.14 (10 shards) | Windows 3.11 / 3.13 compat. |
| --- | ---: | --- | --- | --- | --- | --- |
| C1 `tests/ci/test_confidential_content_guard.py` | 59 | 20.1 (12.9-21.0); 0.3 | 20.2 (13.8-24.8); 0.3 | 32.9 (26.4-37.3); 0.4 | 72.8 (63.5-81.0); 0.2 | not run / not run |
| C2a `tests/architecture/test_source_dependency_contract.py` | 74 | 48.4 (35.3-51.7); 0.3 | 35.0 (23.4-49.5); 0.3 | 81.4 (63.6-100.3); 0.4 | 45.1 (37.1-52.0); 0.2 | 27.7 (17.4-30.1) JUnit, n=74 / 25.7 (18.9-29.5) JUnit, n=74 |
| C2b `tests/architecture/test_config_eda_runtime_separation.py` | 33 | 7.0 (5.6-8.4); 0.2 | 4.5 (3.1-7.0); 0.2 | 11.3 (6.2-13.8); 0.2 | 7.1 (3.4-9.1); 0.1 | 5.6 (3.7-6.0) JUnit, n=33 / 5.3 (3.7-5.8) JUnit, n=33 |
| C2c `tests/test_dependency_direction.py` | 6 | 3.1 (2.2-4.1); 0.1 | 2.8 (1.6-4.0); 0.1 | 5.6 (4.1-8.0); 0.0 | 2.7 (1.7-3.8); 0.0 | not run / not run |
| C3 `tests/ci/test_agent_readiness.py` | 60 | 14.4 (8.4-17.1); 0.3 | 10.2 (6.9-14.3); 0.3 | 11.1 (7.3-13.9); 0.4 | 18.8 (13.4-20.2); 0.2 | not run / not run |
| C3 node `test_schema_one_receipt_is_rebuilt_with_working_console_scripts` | 1 | 13.7 (7.9-16.4); 0.0 | 9.6 (6.5-13.6); 0.0 | 9.9 (6.0-12.5); 0.0 | 15.9 (10.6-17.0); 0.0 | not run / not run |
| C4a `tests/ci/test_test_profiles.py` | 30 | 15.5 (10.2-18.4); 0.2 | 15.7 (10.5-22.1); 0.2 | 25.5 (19.8-30.0); 0.2 | 23.0 (22.0-24.7); 0.1 | not run / not run |
| C4b `tests/test_pytest_configuration.py` | 59 | 10.3 (5.9-11.4); 0.3 | 9.3 (6.2-13.1); 0.2 | 19.8 (15.2-24.7); 0.3 | 14.7 (13.0-17.3); 0.3 | not run / not run |
| C4 node `test_suite_default_timeout_preserves_explicit_settings` | 8 | 5.2 (3.3-5.6); 0.0 | 5.4 (3.7-7.1); 0.0 | 8.8 (6.4-10.2); 0.0 | 8.6 (7.8-10.4); 0.0 | not run / not run |
| C5 `tests/docs/` five modules | 145 | 20.8 (17.4-22.3); 0.8 | 14.7 (9.3-17.8); 0.8 | 26.4 (19.6-33.9); 0.8 | 22.9 (20.9-28.7); 0.5 | not run / not run |
| C5 `test_links.py` | 10 | 2.4 (1.9-3.5); 0.1 | 2.1 (1.2-3.3); 0.1 | 8.0 (3.9-9.2); 0.1 | 3.6 (2.4-4.7); 0.0 | not run / not run |
| C5 `test_documented_commands.py` | 16 | 3.2 (2.7-3.9); 0.1 | 2.5 (1.4-3.3); 0.1 | 3.9 (2.6-5.3); 0.1 | 4.7 (4.1-6.0); 0.1 | not run / not run |
| C5 `test_current_version_docs.py` | 79 | 4.0 (3.1-4.6); 0.3 | 2.4 (1.6-3.9); 0.3 | 4.0 (2.7-5.0); 0.4 | 3.9 (3.0-5.1); 0.3 | not run / not run |
| C5 `test_guidance_guard.py` | 4 | 3.4 (2.5-4.6); 0.0 | 2.1 (1.4-3.0); 0.0 | 3.7 (3.1-6.9); 0.0 | 4.3 (3.1-6.3); 0.0 | not run / not run |
| C5 `test_goal_work_dir_guidance.py` | 5 | 7.1 (5.6-8.6); 0.1 | 5.1 (3.0-6.7); 0.1 | 6.8 (4.8-10.1); 0.0 | 6.4 (5.1-8.5); 0.0 | not run / not run |
| C6a `test_production_has_no_retired_host_execution_symbols` | 1 | 8.4 (5.9-10.6); 0.0 | 6.2 (4.2-10.4); 0.0 | 21.3 (10.4-23.4); 0.0 | 8.4 (5.6-12.6); 0.0 | not run / not run |
| C6b `test_production_strings_use_current_vocabulary` (9 of 20 runs) | 1 | 7.2 (6.1-7.8); 0.0 | 6.1 (4.2-7.3); 0.0 | 11.1 (5.9-13.1); 0.0 | 8.6 (6.3-11.3); 0.0 | not run / not run |

| Selected function | Cases | Ubuntu 3.11 | Ubuntu 3.14 | Ubuntu 3.13 coverage | Windows 3.14 |
| --- | ---: | --- | --- | --- | --- |
| `test_finish_authority.py::test_saved_attempt_path_tamper_refuses_before_any_effect` | 18 | 27.0 (17.6-29.6); 0.6 | 23.4 (18.1-31.9); 0.6 | 40.3 (31.2-46.4); 0.7 | 325.8 (304.4-359.4); 7.2 |
| `test_finish.py::test_every_publication_crash_boundary_recovers_exactly_once` | 8 | 19.7 (13.0-21.2); 0.3 | 19.6 (13.1-22.7); 0.3 | 27.3 (20.9-32.0); 0.3 | 263.1 (229.8-288.1); 3.4 |
| `test_goal_waivers.py::test_each_waiver_effect_recovers_without_revalidating_mutable_inputs` | 8 | 13.0 (8.0-14.5); 0.3 | 12.9 (8.5-16.3); 0.3 | 18.6 (15.1-21.4); 0.3 | 183.4 (134.9-222.5); 3.5 |
| `test_finish_authority.py::test_all_persisted_attempt_fields_are_bound_to_independent_authority` | 9 | 7.2 (4.5-7.9); 7.2 | 6.6 (4.8-8.3); 6.8 | 10.4 (7.2-12.4); 9.8 | 73.0 (56.0-86.9); 93.9 |
| `test_apply_authority.py::test_existing_intent_must_equal_complete_captured_entry_before_recovery` | 12 | 11.6 (7.2-13.0); 0.4 | 11.3 (7.5-13.4); 0.4 | 17.1 (14.1-19.9); 0.5 | 157.9 (132.3-186.9); 4.9 |
| `test_r1_lifecycle.py::test_hard_process_death_recovers_only_durably_owned_publication_inodes` | 4 | 12.9 (8.6-14.6); 0.1 | 11.8 (8.7-15.0); 0.1 | 19.0 (14.1-22.2); 0.2 | 135.5 (90.9-155.0); 1.6 |
| `test_abandon.py::test_abandon_recovers_finishing_through_original_exact_publication` | 4 | 8.6 (4.7-10.7); 0.1 | 7.4 (4.5-10.9); 0.1 | 13.4 (9.8-16.4); 0.2 | 130.0 (88.2-165.2); 1.7 |
| `test_finish_review_fixes.py::test_pruned_publication_without_exact_authority_refuses_without_git_writes` | 4 | 8.4 (5.8-10.2); 0.2 | 8.3 (6.0-9.6); 0.2 | 12.1 (9.1-14.1); 0.2 | 97.0 (78.3-108.7); 1.7 |
| `test_finish.py::test_recovery_refuses_substituted_immutable_publication_proof` | 4 | 7.4 (5.0-8.3); 0.1 | 7.2 (4.4-8.8); 0.1 | 10.4 (8.4-12.5); 0.2 | 95.8 (70.7-111.8); 1.6 |
| `test_recorder.py::test_interrupted_goal_transaction_recovers` | 8 | 5.4 (3.7-6.5); 2.5 | 5.0 (3.4-6.2); 2.5 | 7.9 (6.2-9.5); 3.4 | 58.7 (48.5-65.8); 36.8 |
| `test_entry.py::test_a_crash_at_every_boundary_is_rolled_back_by_the_next_entry` | 6 | 2.6 (1.8-3.1); 0.2 | 2.2 (1.5-3.1); 0.2 | 3.5 (2.6-3.9); 0.2 | 38.3 (30.6-52.2); 2.6 |
| `test_r1_lifecycle.py::test_abandonment_closure_requires_terminal_authority_and_recovers_crashes` | 4 | 1.8 (1.5-2.1); 0.2 | 1.9 (1.2-2.3); 0.2 | 2.7 (1.7-2.9); 0.2 | 23.0 (17.0-26.8); 1.8 |

Group totals of the median call seconds of the selected functions, and the part that belongs to the cases a tier would relocate (T1, below), with the min-max of the relocatable part:

| Group | Cases / kept as representatives | Ubuntu 3.11 | Ubuntu 3.14 | Ubuntu 3.13 coverage | Windows 3.14 |
| --- | --- | --- | --- | --- | --- |
| C7 Finish (5 functions) | 43 / 11 | 69.6 total, 50.9 relocatable (33.2-55.2) | 66.2 total, 47.5 relocatable (34.1-59.0) | 99.5 total, 74.2 relocatable (57.2-86.3) | 852.9 total, 633.3 relocatable (580.2-689.2) |
| C8 Apply/Change (1 function) | 12 / 2 | 11.6 total, 9.6 relocatable (5.9-11.0) | 11.3 total, 9.5 relocatable (6.2-11.3) | 17.1 total, 14.1 relocatable (11.5-16.5) | 157.9 total, 132.3 relocatable (112.4-153.8) |
| C9 other Goal recovery (6 functions) | 34 / 12 | 44.2 total, 27.7 relocatable (17.9-31.4) | 42.0 total, 26.8 relocatable (17.5-32.5) | 63.7 total, 38.8 relocatable (32.8-45.6) | 570.5 total, 357.5 relocatable (292.2-390.8) |
| All twelve | 89 / 25 | 126.2 total, 88.4 relocatable (57.0-97.6) | 119.5 total, 83.7 relocatable (57.8-102.3) | 179.2 total, 127.4 relocatable (103.5-148.4) | 1577.4 total, 1118.1 relocatable (1018.4-1217.5) |

Context: the twelve selected functions have 89 cases in total. In the table
above the Windows column is 10 to 20 times the Ubuntu column for most of them;
the cause was not investigated here.

### Audit rows

| # | Protected contract (cite) | Existing duplicate enforcement | Python legs | OS / Python independent? Coverage-leg need? | Proposed disposition and retained coverage |
| --- | --- | --- | --- | --- | --- |
| C1 | The leak scanner blocks banned terms and unapproved identities in outgoing commits, history, and PR text without echoing terms, fails closed on a missing key or tampered vocabulary, and trusts only Mergify identities (`tests/ci/test_confidential_content_guard.py:266,427,522,663,676,877,931,1077`); two tests read the workflow text (`:1316,:1327`). | `confidential-content.yml:40-85` scans real PR commits and full `main` history with the real key. Inputs differ (real history and key against synthetic repos and a sentinel term), so it enforces the repository's content while the tests enforce the scanner: not a duplicate. | 4 | Python-version: yes (stdlib, Git, one crypto package). OS: not shown; tests cover symlink and rename handling (`:618,:631`). Coverage: runs `.github/scripts/`, not `src/booley`, so no `src` line coverage. | Keep. No evidence that any case is redundant; it protects a security gate. |
| C2a | The production import graph satisfies the approved dependency contract and its derived rules: whole contract (`tests/architecture/test_source_dependency_contract.py:28`), no Job/Report path to the MCP adapter (`:36`), exact Campaign storage edges (`:57`) and types (`:68`), low-level Target mechanics (`:279`), shared health (`:467,:489`). | None dedicated. Pyright in `lint` (`test.yml:222-288`) checks types and include scope: a different property. | 6 | OS and Python-version independent in principle (pure AST over the source tree); it runs on the compatibility legs by design (`test.yml:334`). Coverage: reads source as data, so no `src` line coverage. | Consolidate (P3). Five tests each rebuild the whole-source graph (`analyze_imports(_SOURCE_ROOT)` at `:29,:37,:58,:469,:494`) and two more parse every source file themselves (`:68`, `:279`). The contract and every assertion stay. |
| C2b | Config, EDA, and runtime direction rules D23/D24 and cyclic-group tightening on synthetic trees (`tests/architecture/test_config_eda_runtime_separation.py:18,45,51,70,97`). | Overlaps the contract in intent, but tests synthetic trees that prove the evaluator catches each import form; the real tree is checked by C2a. | 6 | As C2a. | Keep: it is the evaluator's fault-injection proof, which a consolidation of C2a needs. |
| C2c | Five package-direction checks (`tests/test_dependency_direction.py:41,49,56,64,71`) plus the session-issuance facade check (`:79`). | The five checks are subsumed by contract rules D1, D3, D4, D5, and D6 (`tests/architecture/booley_contract.py`, `DirectionRule` entries), which also cover subpackages, relative imports, and deferred imports; the standalone checks read only top-level `*.py` files and absolute imports, and share their two exemptions (`mcp/server.py`, `incontainer_register`) with the contract's C2 and C8 permissions. The session-issuance check (`:79`) is not covered by the contract. Fault injection on a copy of `src/booley` during this audit: a `from booley.mcp import base` added to `config/limits.py` failed both; a relative `from ..specialists import specialist` added to `runtime/atomic_files.py` failed contract rule D5 and passed the standalone runtime check. | 4 (not on compatibility legs) | As C2a. | Delete the five superseded direction tests (P2); keep `:79`. Retained: rules D1, D3, D4, D5, D6 in C2a. |
| C3 | The read-only Agent Readiness Check and its tools bootstrap: commands, receipts, schema migration, console scripts (`tests/ci/test_agent_readiness.py`, for example `:415`). | None: no workflow runs `agent_readiness.py` or `bootstrap_agent_tools.py` (searched `.github/workflows/`). | 4 | OS: no (launchers and venv layout differ). Coverage: executes `booley.dev_support.agent_readiness`, so it contributes `src` lines. | Keep. The 10-16 s receipt-rebuild case is the one cost outlier, and no evidence says another case covers launcher repair. |
| C4 | Test-profile prerequisites and fail-fast pytest controller behavior (`tests/ci/test_test_profiles.py`, child pytest runs) and the repository's pytest, marker, and CI-matrix configuration (`tests/test_pytest_configuration.py`, for example `:1234`). | `test-verify` and `coverage` check that the shard union equals the eligible set; `assert_junit.py --min-tests` guards the weekly matrix. Different inputs and failure modes (a run's artifacts against the configuration's source). | 4 | OS: no (asserts Windows path forms and workflow command tokens, for example `ntpath` and `shlex` use). Coverage: `test_test_profiles.py` executes `booley.dev_support.test_profiles`. | Keep. A tier is not proposed: it would gate the tests that protect the gate. |
| C5 | Published Markdown links resolve (`tests/docs/test_links.py:36-52`), documented commands match the registered CLI trees (`test_documented_commands.py`), current-version package text (`test_current_version_docs.py`), tool names in guidance (`test_guidance_guard.py`, `test_goal_work_dir_guidance.py`). | `docs-check` runs only `git diff --check` (`test.yml:168-181`); no other job validates links or commands. | 4 | Mostly independent; the CLI help checks import `booley` and cover some `src` lines. | Keep. Each module guards a distinct property and the total is 15-26 s per leg. |
| C6 | No retired host-execution imports or symbols remain in production (`tests/test_no_retired_host_execution.py:57-79`), and no retired Ticket wording remains in production strings (`tests/test_retired_ticket_wording.py:129-131`). | None. Ruff does not search for retired symbols or wording. | 4 | Independent (source text). Coverage: none. | Keep. Both scans re-parse the same tree as C2a; if P3 lands, a shared parsed-source cache could serve them (follow-up of P3, not a separate row). |
| C7 | Goal Finish: a saved attempt's path, fields, and publication proof are bound to independent authority, and every publication crash boundary recovers exactly once (`tests/goals/test_finish.py`, `test_finish_authority.py`, `test_finish_review_fixes.py`). | None; no tier exists (see "The exhaustive-recovery pattern"). | 4 | OS: no (Git process behavior). Coverage: executes `src/booley/goals/finish*.py` recovery paths; representatives keep every function and its first and last checkpoint. | Relocate non-representative cases to a tier (P1). |
| C8 | Goal Apply/Change: an existing intent must equal the captured entry before recovery (`tests/goals/test_apply_authority.py`). | None. | 4 | As C7. | Relocate to the same tier (P1). |
| C9 | Other Goal recovery families: waiver effects (`test_goal_waivers.py`), hard process death (`test_r1_lifecycle.py`), Abandon (`test_abandon.py`), recorder transactions (`test_recorder.py`), Entry crash rollback (`test_entry.py`). | None. | 4 | As C7. | Relocate to the same tier (P1). |

## Proposed tiers

### T1: Goal recovery and authority permutations

Representative cases kept on every code PR (25 of 89 cases, two or three per
function; the first and last checkpoint of each family, plus one case per path
form for the tamper family):

- `tests/goals/test_finish_authority.py::test_saved_attempt_path_tamper_refuses_before_any_effect`: `absolute-normal-finishing`, `different-literal-stealth-attempt-frozen`, `traversal-excluded-attempt-frozen`
- `tests/goals/test_finish.py::test_every_publication_crash_boundary_recovers_exactly_once`: `attempt-frozen`, `finished`
- `tests/goals/test_goal_waivers.py::test_each_waiver_effect_recovers_without_revalidating_mutable_inputs`: `intent`, `applied`
- `tests/goals/test_finish_authority.py::test_all_persisted_attempt_fields_are_bound_to_independent_authority`: `branch`, `package`
- `tests/goals/test_apply_authority.py::test_existing_intent_must_equal_complete_captured_entry_before_recovery`: `after-intent`, `transaction_ref-applied`
- `tests/goals/test_r1_lifecycle.py::test_hard_process_death_recovers_only_durably_owned_publication_inodes`: `index-lock-acquired`, `summary-linked`
- `tests/goals/test_abandon.py::test_abandon_recovers_finishing_through_original_exact_publication`: `finishing-finished`, `finished-finished`
- `tests/goals/test_finish_review_fixes.py::test_pruned_publication_without_exact_authority_refuses_without_git_writes`: `missing-pack`, `physical-control`
- `tests/goals/test_finish.py::test_recovery_refuses_substituted_immutable_publication_proof`: `blob`, `package`
- `tests/goals/test_recorder.py::test_interrupted_goal_transaction_recovers`: `before:acceptance_intent`, `after:acceptance_commit`
- `tests/goals/test_entry.py::test_a_crash_at_every_boundary_is_rolled_back_by_the_next_entry`: `created`, `state_saved`
- `tests/goals/test_r1_lifecycle.py::test_abandonment_closure_requires_terminal_authority_and_recovers_crashes`: `intent-write`, `terminal-save`

Relocated to the tier: the other 64 cases of the same twelve functions. The
cases stay in the files; they are skipped by marker selection, not deleted.

Explicit path dependencies that select the complete set (no filename-pattern
guesses; each is a path whose change can alter the behavior these cases check):

- `src/booley/goals/` and `tests/goals/` (any file);
- the first-party modules the twelve functions' test modules import outside
  `booley.goals`:
  - `src/booley/criteria/evidence_ledger.py`
  - `src/booley/criteria/freshness.py`
  - `src/booley/criteria/state.py`
  - `src/booley/criteria/templates.py`
  - `src/booley/evidence/acceptance.py`
  - `src/booley/evidence/fields.py`
  - `src/booley/evidence/review_receipt.py`
  - `src/booley/flows/baseline_pins.py`
  - `src/booley/flows/baseline_worktree.py`
  - `src/booley/flows/endpoint_diff.py`
  - `src/booley/flows/endpoint_state.py`
  - `src/booley/flows/execution_persistence.py`
  - `src/booley/flows/sim/coverage_campaign.py`
  - `src/booley/flows/sim/coverage_campaign_store.py`
  - `src/booley/flows/sim/coverage_policy.py`
  - `src/booley/flows/sim/coverage_reference.py`
  - `src/booley/flows/sim/coverage_waiver_application.py`
  - `src/booley/flows/sim/coverage_waiver_promotion.py`
  - `src/booley/flows/sim/coverage_waivers.py`
  - `src/booley/flows/sim/waiver_candidates.py`
  - `src/booley/flows/source_fingerprint.py`
  - `src/booley/flows/target_test_plan.py`
  - `src/booley/flows/target_test_plan_loading.py`
  - `src/booley/flows/test_runs.py`
  - `src/booley/fusesoc/fusesoc_registry.py`
  - `src/booley/harness/__init__.py`
  - `src/booley/harness/doctor.py`
  - `src/booley/mcp/base.py`
  - `src/booley/mcp/goal_changes.py`
  - `src/booley/mcp/goal_completion.py`
  - `src/booley/mcp/goal_freshness.py`
  - `src/booley/mcp/goal_waivers.py`
  - `src/booley/mcp/sim_baseline_jobs.py`
  - `src/booley/review/goal_package.py`
  - `src/booley/review/goal_presentation_v1.py`
  - `src/booley/runtime/history_commit.py`
  - `src/booley/runtime/job_records.py`
  - `src/booley/runtime/job_wait.py`
  - `src/booley/runtime/pinned_history.py`
  - `src/booley/runtime/project_gitignore.py`
  - `src/booley/runtime/timefmt.py`
  - `src/booley/specialists/reviewer.py`
  - `src/booley/targets/catalog.py`
  - `src/booley/targets/domain.py`
  - `src/booley/targets/goal_diff.py`
- helper tests: `tests/conftest.py`, `tests/criteria/ledger_support.py`,
  `tests/golden/conftest.py`, `tests/flows/sim/test_coverage_campaign.py`;
- `pyproject.toml`, `.github/ci/`, `.github/scripts/`, and
  `.github/workflows/` (test configuration and CI orchestration; Markdown
  excluded); any unclassified path selects `full` as today.

A static import closure was tried and rejected: it reaches 481 of 613 source
modules because Goal code imports the harness and runtime, so a closure rule
would select the full set for nearly every change.

Pattern: the complete set also runs on `main` pushes and in
`full-python-matrix.yml`. Caveat: that workflow is weekly and does not block
merge, so a failure that only it catches surfaces after merge, up to a week
later. This is the real cost of the tier; the dependency list is what keeps it
small.

Machinery a follow-up must introduce (it was removed in `1460ad065`):
a registered marker in `pyproject.toml`; a classifier category and output in
`ci_changes.py` with the path list above; the mark expression in the test and
coverage jobs of `test.yml` and in `full-python-matrix.yml`; classifier tests in
`tests/ci/test_change_classifier.py`; and a marker-subset test like the removed
`test_exhaustive_recovery_marker_retains_a_pr_subset`. The shard-union
verification in `test-verify` and `coverage` must use the same expression so a
deselected case is not reported missing.

Share of PRs that run the complete set: applying the dependency list to the
file lists of the 50 most recent merged PRs (2026-10-21T09:19Z to
2026-10-23T19:18Z, `gh pr list --state merged -L 50 --json number,files`),
49 select Python tests and 16 of those 49 (33%) match a dependency or select
`full`; the other 33 (67%) would run representatives only. The 16 are PRs
#1966, #1967, #1975, #1989, #1990, #1992, #1996, #2008, #2027, #2053, #2054, #2058, #2064, #2078, #2112, #2116. The other PR (#2112) changes only Markdown (docs-only).

## Summary of proposed dispositions

Ordered by estimated saving. Every figure is an estimate from the 20-run
sample: seconds of test call time per code-PR run unless stated, tier rows
scaled by (1 - 0.33) for the share of PRs that still run the complete set.
Runner minutes are the per-platform sum divided by 60. A real saving needs the
follow-up's 20-run before/after; none of these figures is that.

| ID | Proposed issue title | Candidate | Disposition | Retained coverage | Required proof from the follow-up | Estimate (per code-PR run) | Measurement rule |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P1 | CI: run Goal recovery and authority permutations as a path-triggered tier | C7, C8, C9: 64 relocatable cases of 12 functions (89 cases) | Relocate to a tier following the exhaustive-recovery pattern | 25 representative cases on every code PR; complete set on the T1 dependencies, unclassified paths, `main` pushes, and `full-python-matrix.yml` (weekly, post-merge detection) | (1) Historical regression: show the 25 representatives fail on the parent of `276dc774b` or `0cf4262b7` (Goal recovery fixes), or fault-inject `src/booley/goals/finish*.py` and `apply*.py` and show a representative fails. (2) Backstop: a `full-python-matrix.yml` run (or a dispatch) in which the relocated cases execute and a deliberately failing case is reported. (3) Marker-subset and classifier tests, and `test-verify` and `coverage` shard-union checks pass under the new expression. (4) Coverage report before and after; the 80% floor and 90% diff-cover unchanged | Call seconds, estimate: Ubuntu 3.11 about 59 (range 38-66), Ubuntu 3.14 about 56 (39-69), coverage about 86 (70-100), Windows 3.14 about 753 (685-819); about 15.9 runner minutes (7.6% of the 209-minute median). Gate: between 0 and about 59 s via Ubuntu 3.11 (the slowest job in 11 of 20 `main` runs), and about 19 s via a Windows shard (critical in 2 of 3 dispatched PR-shaped runs) | 20 ordinary code-PR runs before and after: required-gate elapsed, Ubuntu complete job times, runner minutes; report the share of PRs that ran the complete set |
| P2 | Tests: delete the five package-direction tests the architecture contract supersedes | C2c: 5 of 6 tests in `tests/test_dependency_direction.py` | Delete as obsolete | Contract rules D1, D3, D4, D5, D6 in `test_production_source_dependencies_obey_approved_contract`; the session-issuance test stays | Fault injection per rule: add one forbidden import (absolute and relative) to a module in each of config/fusesoc/targets, specialists, mcp infrastructure, runtime, and shared runtime, and show the contract test fails for each (done once for D1 and D5 in this audit); the five deleted tests listed with the rule that replaces each | Call seconds, estimate: Ubuntu 3.11 3.1, Ubuntu 3.14 2.8, coverage 5.6, Windows 3.14 2.7; about 0.24 runner minutes. Gate: between 0 and 3.1 s | 20-run before/after of the Ubuntu complete jobs; expect no visible change, so judge by the call-time medians of the module |
| P3 | Tests: build the production import graph once for the architecture contract | C2a: 7 whole-source tests | Consolidate (session or module cache of the immutable analysis result) | Every assertion and the contract unchanged | Same fault-injection set as P2 passes; no test mutates the cached result; module call time before and after on 20 runs | Call seconds, estimate between a lower bound of 6 builds x 1.6 s (local unloaded build) and an upper bound of 6/7 of the seven tests' call time: Ubuntu 3.11 10-41, Ubuntu 3.14 10-30, coverage 10-70, Windows 3.14 10-38, Windows compat. 3.11/3.13 10-24 / 10-22; 1.0-3.7 runner minutes. Gate: between 0 and the Ubuntu figure (a module runs on one `loadscope` worker) | 20-run before/after of `test_source_dependency_contract.py` call medians and Ubuntu job times |
| P4 | Docs: restore or rewrite the exhaustive-recovery policy text | `.github/ci/README.md` "Exhaustive recovery policy", `docs/internals/CONTRIBUTING.md` "Agent source-development checks" | Correct the text: P1's PR rewrites it if P1 is approved, else a docs-only fix stating every recovery permutation runs on every code PR | The records above | The text matches `ci_changes.py`, `test.yml`, and `pyproject.toml` at the PR's head | No saving | Not applicable |

Keep rows (no change proposed): C1, C2b, C3, C4, C5, C6.

## Limitations

- Ubuntu job elapsed is only bounded: `loadscope` assigns a module to one
  worker and `test-timings.json` has no per-worker spans
  (`ci_pytest_shard.py:111-149`), so a saving inside one module moves the job
  between none and the full module saving. Windows shards use `worksteal` with
  four workers, so elapsed saving there is about saving / 4 per shard set, and
  the gate does not depend on it.
- Estimates assume the relocated cases cost the same on other days; the min-max
  ranges above show how much they moved across 20 runs.
- Coverage: no row claims line coverage is unaffected. Each non-keep row
  requires a coverage comparison in its follow-up.
- Tier savings assume the 50-PR window is typical; it spans three days.
