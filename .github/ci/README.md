# Python CI policy

Pull requests use pairwise compatibility coverage so the required gate can run
horizontally:

- Python 3.14 runs the complete suite on Windows in ten duration-balanced
  shards.
- Python 3.14 runs the complete suite on Ubuntu under branch coverage, in the
  six `coverage-shards` jobs. They use coverage.py's `sysmon` core
  (`COVERAGE_CORE=sysmon`, PEP 669), which keeps tracing cheap enough that one
  instrumented run replaces a plain 3.14 run plus a separate coverage run.
- Python 3.11 runs the complete suite on Ubuntu in two duration-balanced
  shards.
- Python 3.11 and 3.13 run focused installation, import, path, subprocess, and
  API compatibility checks on Windows. The complete Python 3.13 suite runs only
  in the weekly full matrix.

The scheduled and manually dispatchable `Full Python compatibility matrix`
workflow remains the complete OS-by-Python backstop. This is deliberately a
pairwise PR policy, not equivalent to running the complete suite on every
combination before merge.

## Main pushes and the nightly run

A push to `main` runs a narrow tier: `changes`, `release-semantic`,
`package-artifacts`, `bwave-smoke`, then `ci-required` and `ci-metrics`
(`MAIN_PUSH_JOBS` in `.github/scripts/ci_changes.py`). Mergify merges the exact
tree its queue candidate already passed, so the suite would only repeat that
result. `bwave-smoke` stays because only a `main` push may write the shared
`sandbox-substrate` build cache, and it installs the wheel `package-artifacts`
builds.

A scheduled run on `main` every night at 01:47 UTC selects every job. It
catches what the queue cannot: a classifier that skipped a needed job, a
maintainer's manual incident merge, and drift in runners, toolchains, or
unpinned dependencies. It also refreshes the `main`-scoped pip and Rust caches
after a dependency change and produces the complete-run artifacts that timing
models and cost profiles sample. Manual runs (except shard benchmarks) and the
release tag's `publish.yml` call also select every job. The concurrency group includes the
event, so a `main` push never cancels the nightly or a manual run.

## Flake stress

The `flake-stress` job repeats the test modules a pull request adds or
modifies on Ubuntu and Windows with Python 3.14 (`pytest --count=N -n 4
--dist=load`, Windows keeping its timeout-headroom gate). Any failed repetition
fails `ci-required`. Pushes, manual runs and Mergify queue drafts skip it.
`.github/scripts/ci_flake_stress.py` picks the modules and sizes `N` (at most
10) from this directory's Windows timing model. A broad change falls back to
its added modules or skips the job, and a notice on the `changes` job names
each module left out. Run `pytest <module> --count=10 -n 4 --dist=load`
to reproduce a failure locally.

## Shard safety

`.github/scripts/ci_pytest_shard.py` collects the eligible tests on every
runner. Historical timings influence balance only: a new or unknown test is
always assigned to a shard. The `test` job owns compatibility execution, with
`test-verify` checking the exact node-ID sets from its ten Windows shards and
from the sharded Ubuntu 3.11 group. The
independent `coverage-shards` job owns coverage execution and the Ubuntu 3.14
suite, balanced by `linux-test-timings.json`; `coverage` verifies
its six exact shard selections before combining their raw data and enforcing
the global and changed-line thresholds. `ci-required` waits for and validates
both branches. Each verifier fails if a test is omitted, duplicated, or
collected differently by two shards.

The checked-in Windows timing model contains the slow observations from a
recent set of `main` runs. Each stored weight is the median of available
observations for a currently eligible test whose median exceeds the
conservative one-second default. Every shard, and each Ubuntu complete-suite
leg of a manual one-shard run, emits fresh exact-node timing evidence for future model refreshes. Stale
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
outcome, including failed test runs. On a sharded run, the Ubuntu 3.11 timing
files, `test_phases` included, are in the `shard-linux-3.11-<index>` artifacts
instead. Ubuntu 3.14 timings are only in the `shard-coverage-<index>` artifacts
and include coverage overhead, so refresh `linux-test-timings.json` from the
3.11 files.

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

## Test-cost profile

[The test-cost profile](test-cost-profile.md) ranks test families and fixture
phases per platform from ten complete `main` runs (SHAs `0c4b5acbe..4fe5edfe7`,
October 22-23, 2026). It lists setup, call, and teardown medians and ranges,
keeps summed test time, job elapsed time, required-gate elapsed time, and runner
minutes as separate quantities, states its sampling limitations, and proposes
optimization candidates with the integration coverage each must keep.

## Ubuntu shard-count and scheduler experiment

Manual `Tests` workflow runs accept three dispatch inputs for the Ubuntu legs:

- `linux_shard_count` (1, 2, or 3; default 2). One keeps a complete-suite job
  for Python 3.11. Two or three shard it into group `linux-3.11`, balanced by
  `linux-test-timings.json`, a model of measured
  call-time medians for tests above one second; any other test weighs its
  `default_seconds`. The measurement behind the model and the shard and
  scheduler comparison are in [linux-shard-experiment.md](linux-shard-experiment.md).
- `linux_scheduler` (`loadscope` or `worksteal`; default `loadscope`). It sets
  `--dist` for the Ubuntu legs only. Windows keeps its work-stealing scheduler.
- `linux_shard_benchmark` selects the same required jobs as an ordinary Python
  source change. It works only on `workflow_dispatch`, cannot be combined with a
  RISC-V measurement arm, and may be combined with `windows_shard_benchmark`.

A non-default count or scheduler is rejected outside `workflow_dispatch`, so
pull requests, pushes, and reusable-workflow calls run two Ubuntu shards per
Python 3.11 with `loadscope`. The maintainer adopted that default from the
experiment's recommendation (arm C). Its sustained behavior over ordinary
validations, kept apart from the controlled samples, is in the record's
[Sustained cohort](linux-shard-experiment.md#sustained-cohort) section. When Ubuntu is
sharded, `test-verify` also verifies the Linux group with the selected count,
using the same exact-union check as the Windows shards.

## Exhaustive recovery policy

No pytest marker or change-classifier category selects recovery tests.
`pyproject.toml` registers only the `string_parameter_eda`, `native_bwave`, and
`slow` markers, and every complete-suite run uses `-m "not native_bwave"`.
Whenever a pull request runs the `test` job, its Ubuntu 3.11 shards and the
Ubuntu 3.14 `coverage-shards` jobs each run every Goal recovery and authority
permutation, and its Windows 3.14 shards together run the same set except
POSIX-only cases; all of them use the same mark expression. The Windows 3.11
and 3.13 compatibility legs run a fixed focused subset. The change classifier
(`required_jobs` in `.github/scripts/ci_changes.py`) selects the `test` job for any path classified
`python_source`, `python_tests`, `workflow`, `packaging`, `release`, or
`ci_tooling`, and for unclassified paths. `ci_tooling` is
`.github/ci/requirements-*.txt`, where CI-only tool pins live instead of inline
in workflows; it runs the jobs that install those pins and the Python lane that
keeps them in step with `pyproject.toml`, but not `full`. README, website and
documentation-only changes run `docs-check` instead: `git diff --check` plus the
content tests that `.github/scripts/ci_docs_tests.py` derives from the files that read
documentation. They add `package-artifacts` when `README.md` changed, and
`site-build` and `lint` for `website/` and `docs/user/`. The nightly run on
`main` selects every job (a `main` push runs only its narrow tier), and the
weekly `full-python-matrix.yml` workflow runs the same mark expression.

Goal and review code select standard and RISC-V image validation. Goal tests
(`tests/goals/`) select standard image validation only. The RISC-V prefixes are
`.github/actions/prepare-picorv32-demo/`, `demo/`, `src/booley/goals/`,
`src/booley/review/`, `src/booley/evidence/`, and
`tests/fixtures/goal_mode_smoke/`.

## Expensive test inventory

[The expensive test inventory](expensive-test-inventory.md) is a measured
record of costly repository-wide scans, duplicate scenarios, and recovery
permutation families, from 20 successful `main` push runs between
`17a2f1800` and `4fe5edfe7` (October 21 to 23, 2026). Its dispositions are
proposals; each approved one is implemented by its own issue with its own
before/after measurement. The path-triggered recovery selection that the
previous section once described was removed with the Ticket Board in
`1460ad065`; the record describes that mechanism as it was and proposes how a
tier would restore it.

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

## Dependency constraints

`.github/ci/constraints.txt` pins every package the project and its extras can
install, for Linux and Windows on every CI Python version (one universal,
unhashed `uv pip compile`; pip rejects hashed constraints with an editable
install). Every CI install of the project passes `-c`, and each pip cache keys on
it. Regenerate it from the repository root with the command in its header:

```bash
uv pip compile pyproject.toml --all-extras --universal --python-version 3.11 -o .github/ci/constraints.txt
```

- `release-semantic` runs `ci_constraints.py` before its own project install, so a
  stale file fails there with the regenerate command. The other jobs do not wait
  for it and may also fail with pip's `ResolutionImpossible`.
- The check catches an added, removed, or re-pinned direct dependency and a wrong
  compile command. It does not notice a new extra or marker on an existing direct
  requirement.
- A constraints-only change is classified like `pyproject.toml`: every job runs,
  Windows legs included.
- Still unconstrained: the setuptools build backend in pip's isolated build
  environment, pip, and uv. Built-wheel installs (`publish.yml`,
  `docker-publish.yml`, `isolated_host_install_smoke.py`) and image Dockerfile
  installs stay unconstrained on purpose: they verify what users get.

## pip cache

Every `actions/setup-python` step with `cache: pip` has `id: setup-python` and is
followed by a `Drop pip cache restored from an older dependency set` step. On an
exact key hit (`cache-hit == 'true'`) the step is skipped and nothing is saved.
On a miss, setup-python restores the newest entry for the same OS and Python
patch version by key prefix and saves a new entry after the job. pip never
prunes its cache, so without the purge each new entry carried every older
wheel forward. Windows Python 3.11 is frozen at 3.11.9, the last 3.11 release
with a Windows installer, so its prefix never reset: its entries reached 2 GB
each, against about 180 MB for a fresh install, and pushed other caches out of
the repository's 50 GB limit. After the purge, a new entry holds only the
current dependency set. `test_pip_caches_start_empty_after_a_prefix_restore`
in `tests/ci/test_ci_requirements.py` requires the step after every such cache.
An existing bloated entry stays until its key changes or it is deleted.
