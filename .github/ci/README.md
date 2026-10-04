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
duration. Raw progress is BuildKit's `rawjson` stream: one SolveStatus snapshot
per line, with vertices merged by digest and statuses merged by vertex and ID.
The outer `exporting to image` vertex includes layer export, image metadata, and
naming, so it remains part of construction/export. Transfer/load is separated
only when BuildKit exposes a distinct daemon import, layer-load, or unpack
interval. Otherwise transfer/load is explicitly `unavailable`, the command
duration remains attributed to the combined construction/export/transfer/load
operation, and the sample is not complete.
A tooling cache hit means every Dockerfile step vertex was cached; the named
parent context is always reported cached and does not count.

Use the `Tests` workflow's `riscv_measurement` dispatch input for controlled
samples. Main and manual runs otherwise skip the RISC-V lane unless the last
commit changed its inputs, so every arm except `automatic` forces the lane on
dispatch (for example `gh workflow run test.yml --ref main -f
riscv_measurement=cold`). All controlled arms (`baseline`, `warm`, and `cold`)
use pinned Docker v28.0.4 with the containerd image store and the daemon-backed
`--builder default --load` path. This gives every arm the same image-store/load
boundary and retains access to the exact local candidate parent.

`baseline` adds no explicit RISC-V cache import/export. `warm` restores a
tooling-input-, stable-base-, and standard-parent-content-scoped local BuildKit cache through GitHub's
branch-aware cache service. Pull-request caches cannot replace the trusted
default-branch entry. The cache key includes the standard candidate
runtime-content fingerprint: changing its configuration or layer ancestry gets a
new key rather than an immutable stale cache hit. The fingerprint hashes the
inspected runtime configuration, RootFS layer diff IDs, architecture, and OS.
It excludes the containerd index's timestamped attestations, which can change
between otherwise identical builds, and does not depend on exporter-specific
config-digest metadata. The candidate's inspected image ID remains
in the retained provenance evidence. A cache miss seeds a later rerun and is
marked non-representative. Only a restored cache for which BuildKit reports an actual
tooling cache hit is a warm sample. `cold` adds `--no-cache` to both candidate
builds and does not restore or export the RISC-V cache. `automatic` retains the
hosted-runner Docker setup, `--builder default --load` path, and path gating;
the measurement input cannot be set on pull requests.

For the decision sample, use the published stable-base path and verify the
same standard parent runtime-content fingerprint across runs. A locally rebuilt stable
base stamps a fresh creation time and can change parent identity on every run;
warm dispatches on that path fail before building/exporting an unusable cache.
Use `cold` to exercise that path's correctness.
Compare controlled arms at the same candidate SHA, stable-base digest, Docker
version, storage driver, and runner architecture; retain the image-size report's
Docker/storage environment evidence with each sample. Alternate cold and warm
runs to limit runner/time drift, excluding warm seed runs and any sample with
incomplete phase evidence. Use at least two controlled cold runs and three
restored warm runs for the five-run decision sample. The controlled `baseline`
is an uncached-import reference on containerd, not the classic-store production
baseline. Analyze `automatic` runs separately; do not attribute an image-store
switch to tooling-cache savings.

The controlled experiment establishes cache effects within containerd. Before
using it to justify a production carrier rollout, separately retain ordinary
`automatic` run totals and critical-path headroom on the classic store. Missing
classic-store transfer attribution does not invalidate those aggregate totals,
but it prevents a phase-level load claim. Bound the production prediction by
observed classic-store headroom, report the Docker setup/store effect separately,
and obtain a matched classic/containerd comparison before extrapolating the
controlled cache savings to production. If that comparison cannot isolate the
store effect, the production go/no-go remains unresolved.

Before considering a reusable tooling carrier, collect at least
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
derivation changes. It builds `--target riscv-tooling`, pushes a
run-scoped candidate tag, verifies its labels, toolchain, multilibs, hard
links, and shared-library closure, records the builder's `libc6`,
`libstdc++6`, `libgcc-s1`, and `g++` versions, and only then creates
`ghcr.io/boldaxolotl/booley-sandbox-base:riscv-tooling-<key>` with those
versions as an index annotation. An existing key tag with matching labels ends
the run; one with different labels fails it. The publisher never overwrites a
key tag, and no other workflow writes one. Check a key with
`python .github/scripts/riscv_tooling.py published`.

The `Tests` workflow does not consume the published image yet; its RISC-V lane
still builds the stage locally. Consumption, its compatibility fallback, and
source-dependent duration budgets follow in the next #568 change.
