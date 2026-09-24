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
