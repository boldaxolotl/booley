# Python CI policy

Pull requests use pairwise compatibility coverage so the required gate can run
horizontally:

- Python 3.14 runs the complete suite on Windows in four duration-balanced
  shards.
- Python 3.13 runs complete branch coverage on Ubuntu in three shards.
- Python 3.11 and 3.14 run the complete suite on Ubuntu.
- Python 3.11 and 3.13 run focused installation, import, path, subprocess, and
  API compatibility checks on Windows.

The scheduled and manually dispatchable `Full Python compatibility matrix`
workflow remains the complete OS-by-Python backstop. This is deliberately a
pairwise PR policy, not equivalent to running the complete suite on every
combination before merge.

## Shard safety

`.github/scripts/ci_pytest_shard.py` collects the eligible tests on every
runner. Historical timings influence balance only: a new or unknown test is
always assigned to a shard. The `test` job owns compatibility execution, with
`test-verify` checking the exact node-ID sets from its four Windows shards. The
independent `coverage-shards` job owns coverage execution; `coverage` verifies
its three exact shard selections before combining their raw data and enforcing
the global and changed-line thresholds. `ci-required` waits for and validates
both branches. Each verifier fails if a test is omitted, duplicated, or
collected differently by two shards.

The checked-in Windows timing model contains the slow observations from a
recent set of `main` runs. Each stored weight is the median of available
observations for a currently eligible test whose median exceeds the
conservative one-second default. Every shard emits fresh exact-node timing
evidence for future model refreshes. Stale entries are harmless and missing or
unobserved tests use the default weight.

The timing evidence also separates the slowest worker's collection time from
controller and execution wall time. GitHub job-step timestamps supply runner
setup and package-install time around those pytest phases.

## Windows shard-count experiment

Manual `Tests` workflow runs accept four, six, or eight Windows shards. The
`windows_shard_benchmark` option selects the same required jobs as an ordinary
Python source change, avoiding unrelated image work in required-gate timing. Pull
requests, pushes, and reusable-workflow calls retain four shards unless the
production policy is changed after measurement. The generated matrix keeps the
same Linux and Windows compatibility legs, marker selection, four-worker
work-stealing scheduler, timing model, and exact-union verification for every
candidate count.

The current timing-model refresh uses ten complete four-shard artifact sets,
from runs `35614218828` through `35850009708`, with run `35850009708` as the
13,107-test reference set. Benchmark comparisons must record setup, collection,
execution, job queueing, required-gate elapsed time, and total runner minutes.

## Exhaustive recovery policy

Every code pull request retains representative before/after, repository-role,
and first/last checkpoint recovery cases. The full interruption permutation
set runs when ticket-board code or tests, shared Git infrastructure, test
configuration, dependency configuration, or CI orchestration changes. Unknown
paths fail safe to the exhaustive set. Main pushes and the full scheduled
matrix also run every recovery permutation.

## Performance telemetry

The fixed `ci-metrics` job runs after `ci-required` and records workflow queue
time, completed-job queue percentiles, consumed runner time at observation, a
rounded job-minute estimate, and workflow elapsed time. The only job absent
from that total is the metrics collector itself. It writes the measurements to
the job summary and keeps the JSON artifact for 90 days. Performance evaluation
uses at least 20 code-changing runs and includes queue time rather than
considering job runtime alone.
