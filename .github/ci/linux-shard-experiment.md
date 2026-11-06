# Ubuntu shard-count and scheduler experiment

Issue #1792 measured whether sharding the Ubuntu Python 3.11 and 3.14 legs, or
switching them from `loadscope` to `worksteal`, shortens the required `Tests`
gate. All controlled runs are manual `workflow_dispatch` runs of the `Tests`
workflow on one fixed commit, `e5bc6853f5f9614ce9eada544c473f6b6ec5fc23`, with
`linux_shard_benchmark=true` and every Windows input at its default (ten
shards). Each run below is a first attempt. Runs were dispatched one at a time,
never overlapping.

## Timing model

Commit `e5bc6853f` refreshed `.github/ci/linux-test-timings.json` from the
Ubuntu complete-suite legs (3.11 and 3.14) of three passing `main` push runs
whose `pytest-timing-ubuntu-<python>-full` artifacts carry `test_phases` with
`setup`, `call`, and `teardown` entries:

- [39929370554](https://github.com/boldaxolotl/booley/actions/runs/39929370554)
- [39935665483](https://github.com/boldaxolotl/booley/actions/runs/39935665483)
- [39946472177](https://github.com/boldaxolotl/booley/actions/runs/39946472177)

That is six observations per test. Each stored weight is the median call time
over the available observations, kept when it exceeds the one-second default
(869 of 22,090 observed tests; total 2,032 s). Weights are call-only, as
`.github/ci/README.md` requires: setup is dominated by fixtures charged to
whichever test first triggers them on a worker, so it is reported below as
context and is not a weight. Evidence came from `main` push runs rather than
dedicated runs because the maintainer approved only the twelve controlled
dispatches.

## Arms

| Arm | `linux_shard_count` | `linux_scheduler` |
| --- | ---: | --- |
| A Baseline | 1 | `loadscope` |
| B Scheduler only | 1 | `worksteal` |
| C Shards | 2 | `loadscope` |
| D Shards + scheduler | 2 | `worksteal` |

The planned rotation to spread runner load was A B C D, then B C D A, then
C D A B. The two cancelled arm D runs and one rerun (below) shifted the actual
order, by run ID: A B C D, B C D(cancelled) D, A C D(cancelled), A B.

## Per-run results

Required gate is the earliest job creation through `ci-required` completion.
Queue is the maximum `started - created` over completed jobs (run B #3 shows
184 s because `test-verify` waited for a runner). Setup is the longest Ubuntu
test-leg interval from job start to the pytest step. Collection and execution
are the longest values over Ubuntu legs or shards. Finished last is the latest
test job, Windows included, other than `test-verify`. Runner minutes are the
unrounded `ci-metrics` value; the rounded estimate is for reference. Exact-once
shows `<group> <eligible tests>/<shards>`, each verified by `test-verify`.

| Arm | Run | Required gate | Max queue | Max setup | Max collection | Max execution | Slowest Ubuntu job | Finished last | Runner min | Rounded est. | Exact-once |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | --- |
| A Baseline #1 | [39981238217](https://github.com/boldaxolotl/booley/actions/runs/39981238217) | 982 s | 12 s | 36 s | 72 s | 817 s | ubuntu-3.14-full 954 s | ubuntu-3.14-full 963 s | 196.4 | 209 | windows 22271/10 |
| A Baseline #2 | [40000275643](https://github.com/boldaxolotl/booley/actions/runs/40000275643) | 1101 s | 3 s | 27 s | 66 s | 945 s | ubuntu-3.11-full 1068 s | ubuntu-3.11-full 1079 s | 196.7 | 210 | windows 22271/10 |
| A Baseline #3 | [40007264009](https://github.com/boldaxolotl/booley/actions/runs/40007264009) | 1030 s | 3 s | 34 s | 62 s | 875 s | ubuntu-3.11-full 997 s | ubuntu-3.11-full 1009 s | 187.9 | 201 | windows 22271/10 |
| B Scheduler only #1 | [39983650602](https://github.com/boldaxolotl/booley/actions/runs/39983650602) | 1121 s | 4 s | 27 s | 77 s | 951 s | ubuntu-3.14-full 1087 s | ubuntu-3.14-full 1097 s | 199.4 | 212 | windows 22271/10 |
| B Scheduler only #2 | [39990791032](https://github.com/boldaxolotl/booley/actions/runs/39990791032) | 1171 s | 3 s | 33 s | 76 s | 995 s | ubuntu-3.14-full 1140 s | ubuntu-3.14-full 1149 s | 183.0 | 195 | windows 22271/10 |
| B Scheduler only #3 | [40009688331](https://github.com/boldaxolotl/booley/actions/runs/40009688331) | 1186 s | 184 s | 28 s | 58 s | 809 s | ubuntu-3.14-full 949 s | ubuntu-3.14-full 962 s | 186.2 | 198 | windows 22271/10 |
| C Shards #1 | [39986400640](https://github.com/boldaxolotl/booley/actions/runs/39986400640) | 920 s | 18 s | 30 s | 68 s | 454 s | ubuntu-3.11-shard-2-of-2 576 s | windows-3.14-shard-9-of-10 890 s | 185.9 | 201 | windows 22271/10, linux-3.11 22283/2, linux-3.14 22283/2 |
| C Shards #2 | [39993581999](https://github.com/boldaxolotl/booley/actions/runs/39993581999) | 903 s | 3 s | 31 s | 75 s | 489 s | ubuntu-3.14-shard-2-of-2 618 s | windows-3.14-shard-8-of-10 884 s | 176.2 | 190 | windows 22271/10, linux-3.11 22283/2, linux-3.14 22283/2 |
| C Shards #3 | [40002871638](https://github.com/boldaxolotl/booley/actions/runs/40002871638) | 850 s | 3 s | 29 s | 67 s | 452 s | ubuntu-3.11-shard-1-of-2 563 s | windows-3.14-shard-2-of-10 826 s | 183.6 | 198 | windows 22271/10, linux-3.11 22283/2, linux-3.14 22283/2 |
| D Shards + scheduler #1 | [39988629918](https://github.com/boldaxolotl/booley/actions/runs/39988629918) | 888 s | 5 s | 34 s | 71 s | 503 s | ubuntu-3.14-shard-1-of-2 633 s | windows-3.14-shard-9-of-10 868 s | 192.8 | 207 | windows 22271/10, linux-3.11 22283/2, linux-3.14 22283/2 |
| D Shards + scheduler #2 | [39998146343](https://github.com/boldaxolotl/booley/actions/runs/39998146343) | 874 s | 3 s | 30 s | 71 s | 469 s | ubuntu-3.14-shard-1-of-2 594 s | windows-3.14-shard-3-of-10 855 s | 194.4 | 210 | windows 22271/10, linux-3.11 22283/2, linux-3.14 22283/2 |

## Per-arm medians and ranges

| Arm | Complete runs | Gate median (range) | Runner min median (range) | Slowest Ubuntu job median (range) | Max execution median (range) | Max collection median (range) |
| --- | ---: | --- | --- | --- | --- | --- |
| A | 3 | 1030 s (982-1101) | 196.4 (187.9-196.7) | 997 s (954-1068) | 875 s (817-945) | 66 s (62-72) |
| B | 3 | 1171 s (1121-1186) | 186.2 (183.0-199.4) | 1087 s (949-1140) | 951 s (809-995) | 76 s (58-77) |
| C | 3 | 903 s (850-920) | 183.6 (176.2-185.9) | 576 s (563-618) | 454 s (452-489) | 68 s (67-75) |
| D | 2 | 881 s (874-888) | 193.6 (192.8-194.4) | 614 s (594-633) | 486 s (469-503) | 71 s (71-71) |

Maximum setup stayed between 27 s and 36 s in every arm; maximum queue was 3 to
5 s apart from the 184 s outlier noted above, a 12 s value in A #1, and an
18 s value in C #1.

## Comparisons

Differences of medians (negative is faster or cheaper):

| Comparison | Effect | Gate | Runner min |
| --- | --- | ---: | ---: |
| Scheduler, one shard | B - A | +141 s | -10.2 |
| Scheduler, two shards | D - C | -22 s (2 runs for D) | +10.0 |
| Shard count, `loadscope` | C - A | -127 s | -12.8 |
| Shard count, `worksteal` | D - B | -290 s (2 runs for D) | +7.4 |

Shards cut the longest Ubuntu job from about 1,000 s to about 580-610 s, but the
gate moved much less because the latest job in every sharded run is a Windows
shard (826-890 s). That caps what any Ubuntu arm can show: with two shards the
Ubuntu legs are no longer the critical path, so the 3.11 and 3.14 gains beyond
this point are invisible at the gate. In the one-shard arms the 3.11 or 3.14
complete-suite job finished last.

Latency versus cost: C saves 127 s of median gate, and its median runner
minutes are 12.8 lower, so no extra cost showed up. Two more Ubuntu jobs per
Python version repeat about 30 s of setup and 60-75 s of collection each, so
roughly 6-7 extra runner minutes would be expected; the measured saving is
about the size of the spread between A and B (-10.2), whose job layouts are
identical, so read it as "no measurable increase", not as a real saving. The
runner-minute figure is the unrounded one; the rounded estimate puts C at 198
against 209 for A. `worksteal` on one shard was
slower and more variable (B), and with two shards it moved the median by
-22 s on two runs, inside the range noise.

Three runs per arm on a shared runner pool can show only large effects. The A
and C gate ranges do not overlap (A fastest 982 s, C slowest 920 s); the
scheduler effects are within noise.

## Per-shard setup totals (context)

Summed `test_phases` per shard artifact, setup / call / teardown seconds. Setup
is not in the model, and some shards carry much more of it. It can explain some
call-time imbalance between shards of the same group, as can the one-second
default weight: only 869 of the 22,283 eligible tests have a measured weight,
so most of each shard is balanced by count:

- C #1: 3.11 shards 172/1580/9 and 180/1604/9; 3.14 shards 126/1119/5 and 109/1010/5.
- C #2: 3.11 167/1528/8 and 180/1601/9; 3.14 126/1101/5 and 193/1728/8.
- C #3: 3.11 175/1593/9 and 124/1112/5; 3.14 120/1055/5 and 129/1123/5.
- D #1: 3.11 174/1536/7 and 193/1709/9; 3.14 203/1773/9 and 188/1606/7.
- D #2: 3.11 179/1568/8 and 134/1162/5; 3.14 189/1660/8 and 131/1064/5.

## Excluded runs and viability

- [39995777428](https://github.com/boldaxolotl/booley/actions/runs/39995777428) (arm D): cancelled. `windows-3.14-shard-1-of-10`
  hit its 15-minute job limit in `Run duration-balanced shard`. No test result
  was recorded. Rerun once as a suspected flake (D #2).
- [40004940869](https://github.com/boldaxolotl/booley/actions/runs/40004940869) (arm D): cancelled the same way;
  `windows-3.14-shard-9-of-10` hit the job limit.

By the pre-set rule a job timeout that recurs in the same arm marks the arm not
viable on this commit with no further reruns, so D got no third complete run and
stays "not viable, not recommended". The maintainer accepted D as 2 of 3
complete runs (decision d1792-1). The cancellation reason is the same in both
lost runs: a Windows shard hit its 15-minute job limit. Windows shard
configuration is identical in all arms, and the latest Windows shard in the
completed C and D runs finished 826 to 890 s after the run started (a finish
time, not a job duration), so the timeouts look like Windows runner variance,
not an Ubuntu effect. If so, every arm carries the same timeout risk and D's
label reflects which runs happened to time out. The D figures above rest on two runs and are
indicative only. The adopt-C recommendation below does not depend on D: it
rests on the A and C runs, three each. No other run failed or was excluded.

Thirteen runs were dispatched in total: the twelve planned arm slots (the two
cancelled runs above filled two D slots, so D #3 never ran) plus one rerun
(D #2). The maintainer approved twelve controlled dispatches; the rerun was the
thirteenth.

## Recommendation

Adopt two Ubuntu shards with `loadscope` (arm C). It is the only viable
Ubuntu change that measurably shortens the required gate: its slowest run
(920 s) beats the baseline's fastest (982 s), and its median gate is 127 s
lower (-12%). Its median runner minutes were 12.8 lower, so no extra
cost was measurable (a saving that size is within the A-to-B spread). `worksteal` is not recommended: it was 141 s
slower on one shard and showed no measurable gain on two. Gains beyond C are
capped by the Windows shards, which finished last in every sharded run and
account for the only timeouts seen. The record left the production default
unchanged (one complete-suite job per Python version with `loadscope`) for the
maintainer to decide; the Decision section below records that outcome.

## Decision

On 2026-10-27 the maintainer adopted arm C: pull requests, pushes, and
reusable-workflow calls now run two Ubuntu shards per Python version with
`loadscope` (`DEFAULT_LINUX_SHARD_COUNT = 2`), and `test-verify` checks both
Linux groups on every such run. Manual runs can still choose one or three
shards.

## Sustained cohort

Issue #1793. This section is observational: it shows how the adopted default
(two Ubuntu shards per Python version, `loadscope`) behaved on ordinary
validations. It does not attribute the effect; only the controlled samples
above do. No drift-check runs were dispatched, and none of the runs below are
dispatches.

### Selection

Collected on 2026-11-06 from the GitHub API: attempt 1 of every completed
`push` or `pull_request` run of `Tests` (merge-queue runs included). A run is
comparable when the `test` job ran and the other jobs that can sit on the gate
path match exactly. Two such job sets are common on both sides of the change:

- **Full:** Windows 3.11/3.13 compatibility, ten Windows 3.14 shards, three
  Ubuntu 3.13 coverage shards and `coverage`, `bwave-smoke`,
  `bwave-integration`, `rust-test`, and `sidecar-smoke`.
- **Python-only:** the same without `bwave-integration`, `rust-test`, and
  `sidecar-smoke`.

Runs that also selected `flake-stress` or `scanner-smoke` (added after the
change) are left out. The before cohort uses the Ubuntu complete-suite jobs
(`ubuntu-<python>-full`); the after cohort uses the two-shard jobs and starts
with #2279's merge (2026-10-27 14:59 UTC). The before cohort starts on
2026-10-25: test speedups from #1601 landed on 2026-10-24 and cut the slowest
Windows shard from about 830 s to about 600 s, so earlier runs are not the same
shape. Metrics use the definitions of the per-run table above. Runner minutes
sum the attempt-1 runtime of every completed job except `ci-metrics`, as
`ci-metrics` does.

### Results

Passing runs only (`ci-required` succeeded on attempt 1):

| Job set | Cohort | Window (UTC) | Passing runs | Gate median (p25-p75) | Gate p90 | Gate minus max queue, median | Max queue median | Slowest Ubuntu job median (range) | Slowest Windows 3.14 shard median | Runner min median (p25-p75) |
| --- | --- | --- | ---: | --- | ---: | ---: | ---: | --- | ---: | --- |
| Full | Before | 10-25 06:56 to 10-27 14:31 | 44 | 1074 s (1031-1140) | 1353 s | 1036 s | 4 s | 1024 s (739-1115) | 603 s | 175.1 (168.7-180.9) |
| Full | After | 10-27 14:59 to 11-05 17:40 | 190 | 920 s (836-1110) | 1487 s | 830 s | 100 s | 598 s (492-715) | 621 s | 186.9 (182.2-190.9) |
| Python-only | Before | 10-25 06:39 to 10-27 10:14 | 46 | 1088 s (1020-1181) | 1442 s | 1008 s | 66 s | 991 s (737-1121) | 608 s | 168.3 (162.9-174.7) |
| Python-only | After | 10-27 15:15 to 11-06 09:52 | 159 | 943 s (830-1178) | 1480 s | 820 s | 103 s | 598 s (403-659) | 620 s | 177.5 (172.7-182.8) |

- **Gate:** the median fell by 154 s (full) and 145 s (Python-only), about
  13-14%, close to the controlled C - A effect of -127 s. With the longest
  queue wait subtracted, the median drops by 206 s and 188 s and the p90 by
  132 s (1110 to 978) and 126 s (1097 to 971). The raw p90 got worse, and so did
  the median queue (4-66 s before, 100-103 s after): the after cohort ran on a
  busier runner pool, and each run now asks for two more runners. The slow
  tail is queue, not test time.
- **Critical path:** before, an Ubuntu complete-suite job was the latest test
  job in 89 of 90 passing runs, and `test-verify` was the last job before
  `ci-required` in 83. After, a Windows 3.14 shard was the latest test job in
  274 of 349, and `bwave-smoke` (175) or `coverage` (149) finished last; an
  Ubuntu shard was latest in 75. More Ubuntu shards would rarely shorten the
  gate; `bwave-smoke`, `coverage`, and the Windows shards now set it.
- **Cost:** median runner minutes rose by 11.8 (full) and 9.2 (Python-only),
  about 5-7%. The controlled samples showed no measurable increase (C - A was
  -12.8, inside the A-to-B noise). The record predicted 6-7 extra minutes,
  because each extra Ubuntu job repeats about 30 s of setup and 60-75 s of
  collection; the sustained increase is a few minutes above that.

### Failures, reported separately

Runs whose `ci-required` did not pass on attempt 1, from the same windows:

| Cause | Full before (59 runs) | Full after (261) | Python-only before (68) | Python-only after (225) |
| --- | ---: | ---: | ---: | ---: |
| Superseded by a newer push (cancelled) | 14 | 43 | 9 | 29 |
| Windows test failure | 1 | 9 | 7 | 25 |
| Ubuntu test failure | 0 | 2 | 1 | 6 |
| Ubuntu shard hit its 15-minute limit | 0 | 2 | 0 | 1 |
| Windows shard hit its 15-minute limit | 0 | 1 | 0 | 0 |
| `bwave-smoke` failed | 0 | 11 | 0 | 4 |
| `coverage` failed | 0 | 1 | 2 | 1 |
| `lint` or `docs-check` failed | 0 | 2 | 3 | 0 |
| `test-verify` failed | 0 | 0 | 0 | 0 |

`test-verify` passed in every run that reached it, so the exact-once check for
both Linux groups never failed. The three Ubuntu shard timeouts:

- [42052210406](https://github.com/boldaxolotl/booley/actions/runs/42052210406):
  3.14 shard 1, 945 s, in the test step.
- [41032747128](https://github.com/boldaxolotl/booley/actions/runs/41032747128):
  3.11 shard 2, 930 s, in the test step after a timed-out cache restore.
- [42033479342](https://github.com/boldaxolotl/booley/actions/runs/42033479342):
  3.11 shard 2, 1,030 s, stuck installing test dependencies.

No Ubuntu complete-suite job hit its 30-minute limit before the change. A
shard now has 15 minutes for about 600 s of work, so a slow setup or a
hanging test reaches the limit sooner than it did with one 1,000 s job under
30 minutes. The Windows test and `bwave-smoke` failures are outside the Ubuntu
jobs; this cohort cannot tie them to the sharding.

### Per-run results (first 20 passing runs after the change, per job set)

The aggregates above use every passing run. The first 20 of each set are
listed here so the cohort's start can be checked against the controlled
samples. Shard numbers are folded to `N`.

#### Full job set

| Run | Event | Required gate | Max queue | Slowest Ubuntu shard | Latest test job | Last job before `ci-required` | Runner min |
| --- | --- | ---: | ---: | ---: | --- | --- | ---: |
| [40526483475](https://github.com/boldaxolotl/booley/actions/runs/40526483475) | push | 979 s | 5 s | 607 s | windows-3.14-shard-N-of-10 | bwave-smoke | 188.7 |
| [40532635264](https://github.com/boldaxolotl/booley/actions/runs/40532635264) | push | 792 s | 5 s | 591 s | windows-3.14-shard-N-of-10 | coverage | 188.7 |
| [40537646096](https://github.com/boldaxolotl/booley/actions/runs/40537646096) | push | 829 s | 5 s | 579 s | windows-3.14-shard-N-of-10 | coverage | 194.5 |
| [40545203857](https://github.com/boldaxolotl/booley/actions/runs/40545203857) | push | 895 s | 2 s | 560 s | windows-3.14-shard-N-of-10 | bwave-smoke | 192.5 |
| [40548118939](https://github.com/boldaxolotl/booley/actions/runs/40548118939) | pull_request | 813 s | 4 s | 574 s | windows-3.14-shard-N-of-10 | coverage | 181.3 |
| [40551209231](https://github.com/boldaxolotl/booley/actions/runs/40551209231) | push | 834 s | 4 s | 590 s | windows-3.14-shard-N-of-10 | coverage | 197.3 |
| [40657963122](https://github.com/boldaxolotl/booley/actions/runs/40657963122) | push | 819 s | 4 s | 582 s | windows-3.14-shard-N-of-10 | coverage | 188.4 |
| [40668755321](https://github.com/boldaxolotl/booley/actions/runs/40668755321) | pull_request | 792 s | 7 s | 606 s | windows-3.14-shard-N-of-10 | coverage | 177.8 |
| [40670083640](https://github.com/boldaxolotl/booley/actions/runs/40670083640) | pull_request | 993 s | 5 s | 563 s | windows-3.14-shard-N-of-10 | bwave-smoke | 175.4 |
| [40671596712](https://github.com/boldaxolotl/booley/actions/runs/40671596712) | push | 814 s | 3 s | 586 s | windows-3.14-shard-N-of-10 | coverage | 191.8 |
| [40674881153](https://github.com/boldaxolotl/booley/actions/runs/40674881153) | pull_request | 973 s | 3 s | 715 s | ubuntu-3.11-shard-N-of-2 | bwave-smoke | 190.4 |
| [40677093783](https://github.com/boldaxolotl/booley/actions/runs/40677093783) | push | 886 s | 4 s | 586 s | windows-3.14-shard-N-of-10 | bwave-smoke | 182.9 |
| [40734081457](https://github.com/boldaxolotl/booley/actions/runs/40734081457) | push | 1125 s | 205 s | 607 s | windows-3.14-shard-N-of-10 | bwave-smoke | 191.3 |
| [40744933599](https://github.com/boldaxolotl/booley/actions/runs/40744933599) | push | 973 s | 164 s | 577 s | windows-3.14-shard-N-of-10 | coverage | 181.9 |
| [40750878240](https://github.com/boldaxolotl/booley/actions/runs/40750878240) | pull_request | 828 s | 121 s | 556 s | windows-3.14-shard-N-of-10 | coverage | 189.7 |
| [40751605493](https://github.com/boldaxolotl/booley/actions/runs/40751605493) | push | 1071 s | 172 s | 609 s | ubuntu-3.14-shard-N-of-2 | bwave-smoke | 187.2 |
| [40758748798](https://github.com/boldaxolotl/booley/actions/runs/40758748798) | push | 861 s | 128 s | 594 s | windows-3.14-shard-N-of-10 | bwave-smoke | 190.9 |
| [40761498008](https://github.com/boldaxolotl/booley/actions/runs/40761498008) | pull_request | 1074 s | 299 s | 551 s | windows-3.14-shard-N-of-10 | test-verify | 176.4 |
| [40762591524](https://github.com/boldaxolotl/booley/actions/runs/40762591524) | push | 1064 s | 150 s | 599 s | windows-3.14-shard-N-of-10 | coverage | 185.1 |
| [40767430000](https://github.com/boldaxolotl/booley/actions/runs/40767430000) | push | 849 s | 231 s | 580 s | windows-3.14-shard-N-of-10 | test-verify | 166.7 |

#### Python-only job set

| Run | Event | Required gate | Max queue | Slowest Ubuntu shard | Latest test job | Last job before `ci-required` | Runner min |
| --- | --- | ---: | ---: | ---: | --- | --- | ---: |
| [40529140373](https://github.com/boldaxolotl/booley/actions/runs/40529140373) | pull_request | 850 s | 7 s | 565 s | windows-3.14-shard-N-of-10 | bwave-smoke | 170.9 |
| [40656111991](https://github.com/boldaxolotl/booley/actions/runs/40656111991) | pull_request | 798 s | 3 s | 612 s | ubuntu-3.14-shard-N-of-2 | coverage | 165.5 |
| [40734118102](https://github.com/boldaxolotl/booley/actions/runs/40734118102) | pull_request | 1449 s | 658 s | 567 s | windows-3.14-shard-N-of-10 | bwave-smoke | 186.1 |
| [40734687095](https://github.com/boldaxolotl/booley/actions/runs/40734687095) | pull_request | 1227 s | 417 s | 545 s | windows-3.14-shard-N-of-10 | bwave-smoke | 170.0 |
| [40738402837](https://github.com/boldaxolotl/booley/actions/runs/40738402837) | pull_request | 1032 s | 101 s | 544 s | windows-3.14-shard-N-of-10 | bwave-smoke | 184.6 |
| [40740347164](https://github.com/boldaxolotl/booley/actions/runs/40740347164) | pull_request | 1236 s | 323 s | 586 s | windows-3.14-shard-N-of-10 | bwave-smoke | 176.8 |
| [40740455893](https://github.com/boldaxolotl/booley/actions/runs/40740455893) | pull_request | 954 s | 138 s | 568 s | windows-3.14-shard-N-of-10 | bwave-smoke | 170.1 |
| [40741117757](https://github.com/boldaxolotl/booley/actions/runs/40741117757) | pull_request | 1026 s | 421 s | 620 s | windows-3.14-shard-N-of-10 | test-verify | 194.4 |
| [40741517911](https://github.com/boldaxolotl/booley/actions/runs/40741517911) | pull_request | 1275 s | 512 s | 583 s | windows-3.14-shard-N-of-10 | coverage | 185.5 |
| [40745067851](https://github.com/boldaxolotl/booley/actions/runs/40745067851) | pull_request | 1176 s | 278 s | 585 s | windows-3.14-shard-N-of-10 | coverage | 180.2 |
| [40745783833](https://github.com/boldaxolotl/booley/actions/runs/40745783833) | pull_request | 1480 s | 404 s | 538 s | windows-3.14-shard-N-of-10 | bwave-smoke | 178.7 |
| [40749422966](https://github.com/boldaxolotl/booley/actions/runs/40749422966) | pull_request | 829 s | 51 s | 591 s | windows-3.14-shard-N-of-10 | coverage | 182.2 |
| [40753705555](https://github.com/boldaxolotl/booley/actions/runs/40753705555) | pull_request | 876 s | 3 s | 554 s | windows-3.14-shard-N-of-10 | bwave-smoke | 170.4 |
| [40754428896](https://github.com/boldaxolotl/booley/actions/runs/40754428896) | pull_request | 846 s | 19 s | 496 s | windows-3.14-shard-N-of-10 | coverage | 175.9 |
| [40756641896](https://github.com/boldaxolotl/booley/actions/runs/40756641896) | pull_request | 815 s | 3 s | 554 s | windows-3.14-shard-N-of-10 | coverage | 166.7 |
| [40758094915](https://github.com/boldaxolotl/booley/actions/runs/40758094915) | pull_request | 886 s | 3 s | 566 s | windows-3.14-shard-N-of-10 | coverage | 179.3 |
| [40760511240](https://github.com/boldaxolotl/booley/actions/runs/40760511240) | pull_request | 787 s | 101 s | 605 s | windows-3.14-shard-N-of-10 | coverage | 181.4 |
| [40762789647](https://github.com/boldaxolotl/booley/actions/runs/40762789647) | pull_request | 1088 s | 273 s | 587 s | windows-3.14-shard-N-of-10 | coverage | 176.7 |
| [40763804837](https://github.com/boldaxolotl/booley/actions/runs/40763804837) | pull_request | 1578 s | 565 s | 554 s | windows-3.14-shard-N-of-10 | coverage | 165.7 |
| [40763928458](https://github.com/boldaxolotl/booley/actions/runs/40763928458) | pull_request | 1384 s | 529 s | 612 s | windows-3.14-shard-N-of-10 | bwave-smoke | 177.5 |

### Recommendation

Keep the two-shard default. Across 349 ordinary passing validations, it took
about 150 s (13-14%) off the median required gate, in line with the controlled
estimate, and the exact-once check never failed. It costs about 9-12 more
runner minutes per run (5-7%), which the controlled samples did not show. Most
of that is the expected setup and collection overhead of two more Ubuntu jobs.
Reverting would give up the 150 s to save those minutes.

Two follow-ups for the maintainer to consider; neither changes the default:

- The gate now ends on `bwave-smoke`, `coverage` (after the Ubuntu 3.13
  coverage shards), or a Windows 3.14 shard. Those, not the Ubuntu legs, are
  where any further latency gain is.
- The 15-minute shard limit leaves about 300 s of headroom. Three of 486 runs
  after the change lost a Ubuntu shard to it, one while installing
  dependencies. Watch this as the suite grows.
