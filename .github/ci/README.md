# Python CI policy

Pull requests use pairwise compatibility coverage so the required gate can run
horizontally:

- Python 3.14 runs the complete suite on Windows in ten duration-balanced
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
`test-verify` checking the exact node-ID sets from its ten Windows shards and,
when a manual run shards Ubuntu, from each Linux group. The
independent `coverage-shards` job owns coverage execution; `coverage` verifies
its three exact shard selections before combining their raw data and enforcing
the global and changed-line thresholds. `ci-required` waits for and validates
both branches. Each verifier fails if a test is omitted, duplicated, or
collected differently by two shards.

The checked-in Windows timing model contains the slow observations from a
recent set of `main` runs. Each stored weight is the median of available
observations for a currently eligible test whose median exceeds the
conservative one-second default. Every shard, and each Ubuntu complete-suite
leg, emits fresh exact-node timing evidence for future model refreshes. Stale
entries are harmless and missing or unobserved tests use the default weight.

The timing evidence also separates the slowest worker's collection time from
controller and execution wall time. GitHub job-step timestamps supply runner
setup and package-install time around those pytest phases.

Each timing file also carries `test_phases`: node ID to the `setup`, `call`,
and `teardown` seconds pytest reported for that test. Nothing is zero-filled, so
a skipped test or one that errors in setup lists only the phases it reached and
is absent from `tests`. `tests` stays call-only, and shard balance and the model
refresh read only `tests`: setup is dominated by module and session fixtures
charged to whichever test first triggers them on each xdist worker, so it is not
a stable per-test weight and would move with scheduling. The Ubuntu
complete-suite legs publish the file as `pytest-timing-<leg label>` on every
outcome, including failed test runs. On a sharded manual
Ubuntu run, the Ubuntu timing files, `test_phases` included, are in the
`shard-linux-<python>-<index>` artifacts instead.

## Windows shard-count experiment

Manual `Tests` workflow runs accept four, six, eight, or ten Windows shards. The
`windows_shard_benchmark` option selects the same required jobs as an ordinary
Python source change, avoiding unrelated image work in required-gate timing. Pull
requests, pushes, and reusable-workflow calls use the selected ten-shard
production policy. The generated matrix keeps the same Linux and Windows
compatibility legs, marker selection, four-worker
work-stealing scheduler, timing model, and exact-union verification for every
candidate count.

The current timing-model refresh uses ten complete four-shard artifact sets,
from runs `35614218828` through `35850009708`, with run `35850009708` as the
13,107-test reference set. Benchmark comparisons must record setup, collection,
execution, job queueing, required-gate elapsed time, and total runner minutes.

The September 23, 2026 experiment selected six shards as the production
default; on October 8, 2026 the grown suite moved the default to eight, and on
October 9, 2026 to ten. See [the experiment record](windows-shard-experiment.md)
for the raw comparison, the changes, and the post-change validation
requirement.

## Ubuntu shard-count and scheduler experiment

Manual `Tests` workflow runs accept three dispatch inputs for the Ubuntu legs:

- `linux_shard_count` (1, 2, or 3; default 1). One keeps a complete-suite job
  per Python version. Two or three shard each of Python 3.11 and 3.14 into group
  `linux-<python>`, balanced by `linux-test-timings.json`, a model of measured
  call-time medians for tests above one second; any other test weighs its
  `default_seconds`. The measurement behind the model and the shard and
  scheduler comparison are in [linux-shard-experiment.md](linux-shard-experiment.md).
- `linux_scheduler` (`loadscope` or `worksteal`; default `loadscope`). It sets
  `--dist` for the Ubuntu legs only. Windows keeps its work-stealing scheduler.
- `linux_shard_benchmark` selects the same required jobs as an ordinary Python
  source change. It works only on `workflow_dispatch`, cannot be combined with a
  RISC-V measurement arm, and may be combined with `windows_shard_benchmark`.

A non-default count or scheduler is rejected outside `workflow_dispatch`, so
pull requests, pushes, and reusable-workflow calls keep one complete-suite job
per Python version with `loadscope`; that default is unchanged pending the
maintainer's decision on the experiment's recommendation. When Ubuntu is
sharded, `test-verify` also verifies each Linux group with the dispatched
count, using the same exact-union check as the Windows shards.

## Exhaustive recovery policy

Every code pull request retains representative before/after, repository-role,
and first/last checkpoint recovery cases. The full interruption permutation
set runs when Ticket Board code or tests, shared Git infrastructure, test
configuration, dependency configuration, or CI orchestration changes. Unknown
paths fail safe to the exhaustive set. Main pushes and the full scheduled
matrix also run every recovery permutation.

Goal and review code select standard and RISC-V image validation. Goal tests
(`tests/goals/`) select standard image validation only. The RISC-V prefixes are
`.github/actions/prepare-picorv32-demo/`, `demo/`, `src/booley/goals/`,
`src/booley/review/`, `src/booley/evidence/`, and
`tests/fixtures/goal_mode_smoke/`. These paths do not independently select
exhaustive Ticket Board recovery permutations.

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
and incremental `test-timings.jsonl`) and `openroad-runtime-*` (logs, scripts, Yosys check reports, and placement
run directories, including partial evidence from failed probes).
Successful OpenROAD probes still fail if required evidence cannot be exported.
These artifacts have 14-day retention. Missing evidence warns when a producer never started or was terminated before writing it.

The incremental report closes each JSON line as a pytest setup, call, or
teardown report arrives. Each line contains the node ID, phase, outcome,
`duration_seconds`, and UTC `recorded_at`. Sum the phases for a test's total;
an interrupted test may have only setup evidence. Completed-test timings survive
process termination even when pytest cannot finish its JUnit XML. Runner loss
or a hard job timeout can still prevent the upload steps from running.

The upload contract tests cover explicit JUnit and diagnostic
outputs in `test.yml`, including timeout diagnostics, shard manifests and timings,
complete-suite phase timings, composite-action input gates, container mounts,
and publication after producers.
CI-metrics publication also runs after collection failures, retaining any
completed telemetry file.
Caches, temporary test projects, and build products are not diagnostic outputs.

### RISC-V on-demand builds

The `Tests` workflow's `riscv_measurement` dispatch input accepts `cold` and `automatic`.
`cold` forces the RISC-V lane and builds tooling locally: `gh workflow run test.yml --ref <branch> -f riscv_measurement=cold`.
`automatic` retains path gating and registry reuse; main and manual runs otherwise select the lane only when the last commit changed its inputs.
The input cannot be set on pull requests.

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
| `local-compat-fallback` | A registry hit whose composed candidate failed to build or failed the compatibility check | Builds the stage from source; any compatibility log remains at the evidence root |

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

The `bwave-smoke` duration budget times the test phase only: its clock starts
after the standard candidate image and its wheel overlay are built, so runner
and cache-network build speed cannot fail it. The RISC-V lane builds its image
inside the validation group, so that build stays on the clock, and the budget
follows the recorded source. Registry hits and local builds (including
fallbacks) each get their own budget, derived from measured runs of that
source, so a slow hit cannot hide behind the local-build ceiling. A lane that
stopped before writing its record is judged against the local budget.
