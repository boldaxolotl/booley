# Python CI policy

Pull requests use pairwise compatibility coverage so the required gate can run
horizontally:

- Python 3.14 runs the complete suite on Windows in six duration-balanced
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
`test-verify` checking the exact node-ID sets from its six Windows shards. The
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
requests, pushes, and reusable-workflow calls use the selected six-shard
production policy. The generated matrix keeps the same Linux and Windows
compatibility legs, marker selection, four-worker
work-stealing scheduler, timing model, and exact-union verification for every
candidate count.

The current timing-model refresh uses ten complete four-shard artifact sets,
from runs `35614218828` through `35850009708`, with run `35850009708` as the
13,107-test reference set. Benchmark comparisons must record setup, collection,
execution, job queueing, required-gate elapsed time, and total runner minutes.

The September 23, 2026 experiment selected six shards as the production
default. See [the experiment record](windows-shard-experiment.md) for the raw
comparison and the post-change validation requirement.

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

### RISC-V image phase measurements

When `riscv_image` is selected, `bwave-smoke` retains `riscv-image-evidence-*`
with a `phases.json` summary, independent records for every native parallel
lane, distinct raw BuildKit progress logs and metadata for the RISC-V substrate
and wheel overlay, and the candidate/parent image inspections. The summary
records the run and attempt, candidate SHA, UTC boundaries, elapsed seconds,
outcome, cache observations, parallel completion order, the RISC-V lane's
completion gap after the latest-finishing other lane, and the post-group Ibex
duration. A transfer/load duration is reported only when raw BuildKit progress
exposes a direct daemon import boundary. Otherwise transfer/load is explicitly
`unavailable`, the successful command duration remains attributed to the
combined construction/export/transfer/load operation, and the sample is not
complete.

Use the `Tests` workflow's `riscv_measurement` dispatch input for controlled
samples. `warm` restores a tooling-input- and stable-base-scoped local BuildKit
cache through GitHub's branch-aware cache service; pull-request caches cannot
replace the trusted default-branch entry. A cache miss seeds a later rerun and
is marked non-representative. Only a restored cache for which BuildKit reports
an actual tooling cache hit is a warm sample. `cold` adds `--no-cache` to both
candidate builds. `automatic` retains the baseline
`--builder default --load` path, and the input does not broaden pull-request
path coverage. Before considering a reusable tooling carrier, collect at least
five complete representative runs, including two cold runs. Compare runs with
the same stable base path and report the median and range for every phase and
parallel lane, workflow queue time, critical-path elapsed time, runner minutes,
and rounded job minutes. Do not claim sustained savings until at least 20
comparable RISC-V-enabled runs have been reported. Recoverable time is capped
by the completion-time gap between the RISC-V lane and the latest-finishing
other parallel lane; add Ibex only after native group completion. Proceed only
when tooling construction plus directly measured avoidable load time predicts
at least two minutes and 20% of the RISC-V lane. After any later cache rollout,
keep cold correctness coverage and track warm and cold budgets separately so
the 1080-second cold ceiling cannot mask a warm regression.
