# Python CI policy

Pull requests use pairwise compatibility coverage so the required gate can run
horizontally:

- Python 3.14 runs the complete suite on Windows in eight duration-balanced
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
`test-verify` checking the exact node-ID sets from its eight Windows shards. The
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
requests, pushes, and reusable-workflow calls use the selected eight-shard
production policy. The generated matrix keeps the same Linux and Windows
compatibility legs, marker selection, four-worker
work-stealing scheduler, timing model, and exact-union verification for every
candidate count.

The current timing-model refresh uses ten complete four-shard artifact sets,
from runs `35614218828` through `35850009708`, with run `35850009708` as the
13,107-test reference set. Benchmark comparisons must record setup, collection,
execution, job queueing, required-gate elapsed time, and total runner minutes.

The September 23, 2026 experiment selected six shards as the production
default; on October 8, 2026 the grown suite moved the default to eight. See
[the experiment record](windows-shard-experiment.md) for the raw comparison,
the change, and the post-change validation requirement.

## Exhaustive recovery policy

Every code pull request retains representative before/after, repository-role,
and first/last checkpoint recovery cases. The full interruption permutation
set runs when Ticket Board code or tests, shared Git infrastructure, test
configuration, dependency configuration, or CI orchestration changes. Unknown
paths fail safe to the exhaustive set. Main pushes and the full scheduled
matrix also run every recovery permutation.

Goal code and tests and review code are release-sensitive inputs for the
standard image validation. They do not independently select exhaustive
Ticket Board recovery permutations.

## Performance telemetry

The fixed `ci-metrics` job runs after `ci-required` and records workflow queue
time, completed-job queue percentiles, consumed runner time at observation, a
rounded job-minute estimate, and workflow elapsed time. The only job absent
from that total is the metrics collector itself. It writes the measurements to
the job summary and keeps the JSON artifact for 90 days. Performance evaluation
uses at least 20 code-changing runs and includes queue time rather than
considering job runtime alone.

### Smoke diagnostic evidence

`bwave-smoke` always attempts to publish `junit-coverage-release-*` (JUnit XML
and incremental `test-timings.jsonl`), `bwave-smoke-phase-records-*` (raw phase
records), and `openroad-runtime-*` (logs, scripts, Yosys check reports, and placement
run directories, including partial evidence from failed probes).
Successful OpenROAD probes still fail if required evidence cannot be exported.
These artifacts have 14-day retention. Missing evidence warns when a producer never started or was terminated before writing it.

The incremental report closes each JSON line as a pytest setup, call, or
teardown report arrives. Each line contains the node ID, phase, outcome,
`duration_seconds`, and UTC `recorded_at`. Sum the phases for a test's total;
an interrupted test may have only setup evidence. Completed-test timings survive
process termination even when pytest cannot finish its JUnit XML. Runner loss
or a hard job timeout can still prevent the upload steps from running.

The upload contract tests cover explicit JUnit, diagnostic, and phase-record
outputs in `test.yml`, including timeout diagnostics, shard manifests and timings,
composite-action input gates, container mounts, and publication after producers.
CI-metrics publication also runs after collection failures, retaining any
completed telemetry file.
Caches, temporary test projects, and build products are not diagnostic outputs.

### RISC-V image phase measurements

When `riscv_image` is selected, `bwave-smoke` retains `riscv-image-evidence-*`
with a `phases.json` summary, independent records for every native parallel
lane, distinct raw BuildKit progress logs and metadata for the RISC-V substrate
and wheel overlay, and the candidate/parent image inspections. The summary
records the run and attempt, candidate SHA, UTC boundaries, elapsed seconds,
outcome, cache observations, parallel completion order, the RISC-V lane's
completion gap after the latest-finishing other lane, and the post-group Ibex
duration. Raw progress is BuildKit's `rawjson` stream: one SolveStatus snapshot
per line, with vertices merged by digest and statuses merged by vertex and ID.
The outer `exporting to image` vertex includes layer export, image metadata, and
naming, so it remains part of construction/export. Transfer/load is separated
only when BuildKit exposes a distinct daemon import, layer-load, or unpack
interval. Otherwise transfer/load is explicitly `unavailable`, the command
duration remains attributed to the combined construction/export/transfer/load
operation, and the sample is not complete.
The summary's `run.tooling` holds the lane's tooling source record (see
"RISC-V tooling stage"), so every run says whether it reused the published
tooling image. A registry hit adds an optional `riscv_tooling_compat` phase for
the compatibility check.

Use the `Tests` workflow's `riscv_measurement` dispatch input for controlled
samples. Main and manual runs otherwise skip the RISC-V lane unless the last
commit changed its inputs. `cold` forces the lane and builds the tooling stage
locally instead of asking the registry (for example `gh workflow run test.yml
--ref <branch> -f riscv_measurement=cold`); `automatic` keeps path gating and
registry reuse. Both arms use the hosted runner's own Docker and image store,
so a pair differs only in its tooling source. The input cannot be set on pull
requests. ADR 0070 retired the Phase-1 `baseline` and `warm` arms and their
containerd image store.

#568 claims sustained savings only from two cohorts, reported on the issue:

- **Controlled pairs (attribution).** On one temporary measurement branch at
  one SHA, alternate `automatic` (registry) and `cold` (local) dispatches with
  the same pinned published stable base. Pin `PUBLISHED_BASE` to an immutable
  digest, because `:main` moves on every stable-base merge, and avoid `main`,
  where `tests-${{ github.ref }}` concurrency cancels dispatches. Collect at
  least three complete registry runs and two complete local runs.
- **Sustained cohort (at least 20 runs).** Count automatic `pull_request` and
  `push` runs only when `riscv_image` was selected, the stable base was the
  published one (`runtime-base.build == false`), the job set, Docker version,
  storage driver (from the image-size environment evidence), and platform
  match, the job succeeded, and `run.tooling.source` is `registry`. Report
  local-base runs, misses, and compatibility fallbacks separately, with the
  hit rate over all RISC-V-lane runs.

Compare the sustained cohort with automatic classic-store runs from the 30 days
before the consumer change, using only aggregate totals: RISC-V lane,
`bwave-smoke` critical path, required gate, queue time, and runner minutes.
Classic-store phase evidence has no transfer attribution, so report medians and
ranges for both cohorts and state that the controlled pairs bound the
cross-period difference without proving it.

### RISC-V tooling stage

`Dockerfile.riscv` builds the xPack GCC toolchain, Spike, and the offline
RISC-V specifications in a `riscv-tooling` stage that starts from the same
digest-pinned Ubuntu image as `Dockerfile.base` and depends on no Booley image
(ADR 0070). The final stage copies `/opt/riscv` and `/opt/riscv-docs` from it
onto the exact standard substrate, so every candidate still runs the full
RISC-V contract, size, and demo checks.

The tooling key is a hash of that stage's exact text, a schema version, and the
platform: `python .github/scripts/riscv_tooling.py key`. Any edit inside the
stage, including a comment, mints a new key. The script fails closed when the
stage gains a build-context `COPY`/`ADD`, a `RUN --mount`, an undigested
`FROM`, or a global `ARG`, because any of those would change the tooling
without changing its key.

`riscv-tooling-publish.yml` runs on `main` when the stage or the key
derivation changes. It builds `--target riscv-tooling` and pushes the
candidate by digest only, so failed runs leave no tag behind. It verifies the
candidate's labels, then runs the session runtime contract's `[riscv]` probes
and tooling paths against it (`riscv_tooling.py checks`): toolchain,
multilibs, hard links, Spike, and the shared-library closure of every host ELF
under `/opt/riscv`. It records the builder's `libc6`, `libstdc++6`,
`libgcc-s1`, and `g++` versions, and only then creates
`ghcr.io/boldaxolotl/booley-sandbox-base:riscv-tooling-<key>` with those
versions as an index annotation. `riscv_tooling.py promoted` then confirms the
tag serves the verified image manifest. An existing key tag with matching
labels ends the run; one with different labels fails it. Only buildx's exact
missing-tag error counts as absence; any other registry error fails the run.
Runs serialize per key. The publisher never overwrites a key tag, and no other
workflow writes one. Check a key with
`python .github/scripts/riscv_tooling.py published`.

`bwave-smoke` consumes the published image. When `riscv_image` is selected,
`riscv_tooling.py resolve` computes the key, resolves the key's tag to a
digest, checks its role and key labels, and writes the **tooling source
record** `riscv-image-evidence/tooling-source.json`:

| Source | When | Lane behavior |
| --- | --- | --- |
| `registry` | The key's tag exists with matching labels | Builds `Dockerfile.riscv` with `--build-context riscv-tooling=docker-image://<repository>@<digest>`, then runs the compatibility check |
| `local` | The tag is absent, the registry errors, the lookup exceeds 120 s, or the arm is `cold` | Builds the stage from source; the record's `reason` says why |
| `local-compat-fallback` | A registry hit whose composed candidate failed to build or failed the compatibility check | Moves the rejected attempt's evidence to `registry-attempt/`, then builds the stage from source |

A tag with the wrong labels fails the job: that is an integrity failure, not a
miss. Only the label-verified digest reaches BuildKit. The compatibility check
runs `riscv_tooling.py checks` (the contract's `[riscv]` probes, including the
shared-library closure of every host ELF under `/opt/riscv`) in the composed
RISC-V substrate before the wheel overlay. A fallback is a warning, not a
failure, and the full contract, size, and demo checks still judge whichever
image the lane finishes with. Repeated fallbacks for one key mean the
published image has drifted from the runtime base; republish it by editing
the stage, which mints a new key. Local and release builds always build the
stage from source.

The `bwave-smoke` duration budget follows the recorded source. Registry hits
and local builds (including fallbacks) each get their own budget, derived from
measured runs of that source, so a slow hit cannot hide behind the
local-build ceiling. A lane that stopped before writing its record is judged
against the local budget.
