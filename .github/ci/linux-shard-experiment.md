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

Dispatch order rotated to spread runner load: A B C D, then B C D A, then
C D A B (the rerun below shifted the tail).

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
| A | 3 | 1030 s (982-1101) | 196.4 (187.9-196.7) | 997 s (954-1068) | 875 s (817-945) | 67 s (62-72) |
| B | 3 | 1171 s (1121-1186) | 186.2 (183.0-199.4) | 1087 s (949-1140) | 951 s (809-995) | 76 s (58-77) |
| C | 3 | 903 s (850-920) | 183.6 (176.2-185.9) | 576 s (563-618) | 454 s (452-489) | 68 s (67-75) |
| D | 2 | 881 s (874-888) | 193.6 (192.8-194.4) | 614 s (594-633) | 486 s (470-503) | 71 s (71-71) |

Maximum setup stayed between 27 s and 36 s in every arm; maximum queue was 3 to
5 s apart from the two outliers noted above and an 18 s value in one C run.

## Comparisons

Differences of medians (negative is faster or cheaper):

| Comparison | Effect | Gate | Runner min |
| --- | --- | ---: | ---: |
| Scheduler, one shard | B - A | +141 s | -10.2 |
| Scheduler, two shards | D - C | -22 s (2 runs for D) | +10.0 |
| Shard count, `loadscope` | C - A | -127 s | -12.8 |
| Shard count, `worksteal` | D - B | -290 s (2 runs for D) | +7.5 |

Shards cut the longest Ubuntu job from about 1,000 s to about 580-610 s, but the
gate moved much less because the latest job in every sharded run is a Windows
shard (826-890 s). That caps what any Ubuntu arm can show: with two shards the
Ubuntu legs are no longer the critical path, so the 3.11 and 3.14 gains beyond
this point are invisible at the gate. In the one-shard arms the 3.11 or 3.14
complete-suite job finished last.

Latency versus cost: C saves 127 s of median gate and 12.8 runner minutes, so
it costs nothing extra. The runner-minute figure is the unrounded one; the
rounded estimate puts C at 198 against 209 for A. `worksteal` on one shard was
slower and more variable (B), and with two shards it moved the median by
-22 s on two runs, inside the range noise.

Three runs per arm on a shared runner pool can show only large effects. The A
and C gate ranges do not overlap (A fastest 982 s, C slowest 920 s); the
scheduler effects are within noise.

## Per-shard setup totals (context)

Summed `test_phases` per shard artifact, setup / call / teardown seconds. Setup
is not in the model, and some shards carry much more of it; it explains some
call-time imbalance between shards of the same group:

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

By the pre-set rule a job timeout that recurs in the same arm makes the arm not
viable on this commit with no further reruns. Arm D therefore has two complete
runs and is not recommended. The timeouts were in Windows shards, whose
configuration is identical in all arms and whose slowest shard in the completed C and D runs finished in 826
to 890 s against the 900 s limit (C #1, 890 s, and D #1, 868 s,
included), so they are probably Windows runner variance, not an Ubuntu effect.
Whether to spend one more D run is the maintainer's decision. No other run
failed or was excluded. Fourteen runs were dispatched in total.

## Recommendation

Adopt two Ubuntu shards with `loadscope` (arm C). It is the only viable
Ubuntu change that measurably shortens the required gate: its slowest run
(920 s) beats the baseline's fastest (982 s), and its median gate is 127 s
lower (-12%). It also used 12.8 fewer runner minutes at the median, so the
extra jobs cost nothing here. `worksteal` is not recommended: it was 141 s
slower on one shard and showed no measurable gain on two. Gains beyond C are
capped by the Windows shards. The production default stays unchanged here
(one complete-suite job per Python version with `loadscope`); whether to
adopt C is the maintainer's decision.
