# Windows shard-count experiment

Issue #567 compared four, six, and eight Windows Python 3.14 shards with the
same 13,083 eligible tests, four-worker work-stealing scheduler, timing model,
and ordinary Python-change required-job set. The artifact verifier confirmed
that every candidate ran the eligible set exactly once.

| Shards | Run | Required gate | Runner min | Max queue | Max setup | Max collection | Max execution |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | [35865334444](https://github.com/boldaxolotl/booley/actions/runs/35865334444) | 501 s | 63.8 | 38 s | 83 s | 50.5 s | 338.6 s |
| 6 | [35866413724, attempt 2](https://github.com/boldaxolotl/booley/actions/runs/35866413724/attempts/2) | 422 s | 67.3 | 4 s | 66 s | 62.9 s | 245.5 s |
| 8 | [35868445607](https://github.com/boldaxolotl/booley/actions/runs/35868445607) | 446 s | 73.3 | 30 s | 90 s | 54.1 s | 183.8 s |

Required-gate elapsed time is measured from the earliest job creation in the
candidate attempt through `ci-required` completion. Queue is the maximum across
all completed jobs. Setup is the longest Windows shard interval from runner
start to the pytest step. Collection and execution are the longest phase values
reported by a Windows shard. Runner minutes come from the workflow metrics
artifact.

Six shards won this sample: it reduced required-gate time by 79 seconds versus
four shards for 3.5 additional runner-minutes. Eight shards reduced pytest
execution further, but the required gate was 24 seconds slower than six and
used 6.0 more runner-minutes. The first six-shard attempt was excluded after an
existing Windows temporary-file rename race failed one shard; the complete
rerun is the recorded candidate.

The workflow therefore defaults to six shards. This is one successful run per
candidate, not evidence of sustained savings. Compare at least 20 ordinary,
code-changing runs using the same gate and runner-minute measurements before
claiming a durable improvement or removing the four/eight experiment options.

## October 8, 2026: default raised to eight

The suite grew from 13,083 to 21,287 eligible Windows tests, and six shards no
longer fit the 15-minute job limit. On `main`, push runs
[37645077103](https://github.com/boldaxolotl/booley/actions/runs/37645077103),
[37750380037](https://github.com/boldaxolotl/booley/actions/runs/37750380037),
and [37752689044](https://github.com/boldaxolotl/booley/actions/runs/37752689044)
ran Windows shards for 9 to 15 minutes, and run 37750380037 lost two shards
to the job deadline. Run setup and package installation take about 1 to 1.5
minutes per job, so the overrun is test execution. Pull request run
[37772516564](https://github.com/boldaxolotl/booley/actions/runs/37772516564)
added roughly 400 test-seconds per shard and lost four of six shards to the
deadline. Rebalancing the timing model could not help: the work exceeded the
combined six-shard capacity.

Eight shards spread the same work about 25% thinner per job without changing
the timing model, the four-worker scheduler, or the job limit. Revisit the
count with the 20-run comparison above once the suite size settles.

## October 9, 2026: default raised to ten

Eight shards were again close to the 15-minute job limit. On the five `main`
push runs from
[37811658032](https://github.com/boldaxolotl/booley/actions/runs/37811658032)
through [37875988225](https://github.com/boldaxolotl/booley/actions/runs/37875988225),
the slowest of the eight Windows shards ran 845 to 877 seconds against the
900-second limit, and the fastest ran 484 to 567 seconds.

Ten shards keep the timing model, the four-worker scheduler, and the job limit
unchanged. Manual runs can still select four, six, or eight shards. The wide gap
between the fastest and slowest shard suggests the timing model has drifted
from real durations; refreshing it is separate work.

The first ten-shard run, pull request run
[37897720629](https://github.com/boldaxolotl/booley/actions/runs/37897720629),
ran its slowest Windows shard in 743 seconds and its fastest in 413 seconds.

## November 10, 2026: default lowered to four

Windows CI skips [Sandbox-only tests](README.md#sandbox-only-tests), which carry
about 62% of the summed Windows test time, so four shards, the smallest
supported count, replace ten. The criterion is the existing timeout headroom: no
Windows shard's pytest step may come closer to its 12-minute step limit than the
slowest ten-shard step does today. Projection per run: the longest of four
longest-processing-time shards of the tests the registry keeps, divided by the
four workers, plus 20 seconds of worker overhead and the run's longest collection.

| Ten-shard run | Slowest step today | Kept test time | Slowest of four shards | Projected step |
| --- | ---: | ---: | ---: | ---: |
| [42888369399](https://github.com/boldaxolotl/booley/actions/runs/42888369399) | 573 s | 4,954 s | 1,239 s | 418 s |
| [42890283558](https://github.com/boldaxolotl/booley/actions/runs/42890283558) | 573 s | 4,780 s | 1,195 s | 406 s |
| [42889969548](https://github.com/boldaxolotl/booley/actions/runs/42889969548) | 543 s | 4,386 s | 1,097 s | 376 s |

All three projections are well under today's slowest step, so four shards stay
within the headroom. The timing model is unchanged; shard balance weighs skipped
tests at zero. Manual runs can still select six, eight, or ten shards.
