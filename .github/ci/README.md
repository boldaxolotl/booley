# CI policy

How the `Tests` workflow (`.github/workflows/test.yml`) picks the jobs a change
runs, how it splits the Python suite across runners, and why. Read it before
you change a workflow, the change classifier (`.github/scripts/ci_changes.py`),
a pytest marker, a shard setting, or a file in this directory. It is written
for contributors and agents. Local checks and the path from branch to `main`
are in [CONTRIBUTING.md](../../docs/internals/CONTRIBUTING.md), the merge queue
in [merge-queue.md](../../docs/internals/agents/merge-queue.md), Renovate in
[dependency-updates.md](../../docs/internals/agents/dependency-updates.md), and
what the image checks require in
[SANDBOX-IMAGE-CONTRACT.md](../../docs/internals/SANDBOX-IMAGE-CONTRACT.md).

## Which Python and OS runs which tests

A pull request covers each Python version and each OS at least once, not every
combination. That keeps the required gate to many short parallel jobs instead
of a full OS-by-Python matrix:

- Python 3.14 on Windows runs the suite in four duration-balanced shards,
  skipping [Sandbox-only tests](#sandbox-only-tests).
- Python 3.14 on Ubuntu runs the complete suite under branch coverage, in six
  `coverage-shards` jobs. They use coverage.py's `sysmon` core
  (`COVERAGE_CORE=sysmon`, PEP 669), which keeps tracing cheap enough that one
  instrumented run replaces a plain run plus a separate coverage run.
- Python 3.11 on Ubuntu runs the complete suite in two duration-balanced shards.
- Python 3.11 and 3.13 on Windows run a fixed list of focused modules:
  `tests/architecture`, `tests/config`, and four runtime and simulation modules
  named in the `Run focused Windows compatibility suite` step.

The weekly `Full Python compatibility matrix` workflow
(`full-python-matrix.yml`, Sundays 03:17 UTC, or dispatched by hand) runs the
complete suite on Ubuntu and Windows for Python 3.11, 3.13, and 3.14. It is the
only run of the complete suite on Python 3.13 and on Windows 3.11.

Every complete-suite run uses `-m "not native_bwave"`; the `bwave-integration`
job runs the `native_bwave` tests, which need the compiled B-Wave binary. No
marker or classifier category selects any other subset. `pyproject.toml`
registers only the `string_parameter_eda`, `native_bwave`, `slow`, and
`sandbox_only` markers.

## Which jobs a change selects

The `changes` job runs `ci_changes.py`, which sorts each changed path into
categories and maps the categories to jobs (`required_jobs`). `ci-required`
then checks that every selected job passed. Draft pull requests skip every job
until marked ready; Mergify's queue drafts run.

- A path classified `workflow`, `packaging`, or `release`, or a path no rule
  classifies, selects every job except `flake-stress` and `riscv-image`, which
  [Flake stress](#flake-stress) and the [RISC-V image lane](#risc-v-image-lane)
  select on their own. `.github/ci/constraints.txt` is a packaging file, so a
  constraints-only change runs every job too.
- A path classified `python_source` or `python_tests` selects `lint`, `test`,
  `test-verify`, `coverage-shards`, and `coverage`; Python source also selects
  `package-artifacts`.
- `ci_tooling` is `.github/ci/requirements-*.txt`, the pins for CI-only tools
  that workflows install instead of pinning them inline. It selects the jobs
  that install those pins and the Python lane whose tests keep them in step
  with `pyproject.toml`, but no docs, site, or Rust job.
- README, website, and documentation-only changes run `docs-check`:
  `git diff --check` plus the content tests that
  `.github/scripts/ci_docs_tests.py` derives from the tests that read those
  files. `README.md` and `docs/user/INSTALL.md` add `package-artifacts` (with
  `build-dist`), which renders the README and runs the install guide's
  commands. `website/` and `docs/user/` add `site-build` and `lint`.
- Goal and review code (`src/booley/goals/`, `src/booley/review/`) select both
  image jobs, `bwave-smoke` and `riscv-image`.

### Goal Mode unit tests

Temporary, until Goal Mode is removed
([#3276](https://github.com/boldaxolotl/booley/issues/3276)). Collecting
`tests/` skips `tests/goals/` (`collect_ignore` in `tests/conftest.py`); run
them by naming the path, for example `pytest tests/goals`. Goal source
(`src/booley/goals/`) is omitted from coverage, and `docs-check` and
`flake-stress` never select those modules. A `tests/goals/` change selects only
`lint`, unless a test outside `tests/goals/` imports that file; then it is an
ordinary test change. The Goal Mode image smoke
(`tests/smoke/test_goal_mode_image_smoke.py`) runs only when
`BOOLEY_GOAL_MODE_SMOKE=1` is set.

## Main pushes and the nightly run

A push to `main` runs only `changes`, `release-semantic`, `build-dist`, and
`bwave-smoke`, then `ci-required` and `ci-metrics` (`MAIN_PUSH_JOBS` in
`ci_changes.py`). Mergify merges a batch's pull requests one by one. The last
merge reproduces the tree its queue candidate already passed, so the suite
would only repeat that result; the commits before it are intermediate trees
that no run tests on their own, and the nightly run covers `main`'s tip.
`bwave-smoke` stays because only a `main` push may write the shared
`sandbox-substrate` build cache, and it installs the wheel `build-dist` builds.
`package-artifacts` is left out because a queue candidate whose paths select it
already ran its isolated host install, wheel, and sdist checks on the same
tree; drift in the unconstrained host install surfaces in the nightly run.

The nightly run (01:47 UTC on `main`) selects every job except `riscv-image`,
which stays path-gated on every event. It catches what the queue cannot: a
classifier that skipped a needed job, a maintainer's manual incident merge, and
drift in runners, toolchains, or unpinned dependencies. It also refreshes the
`main`-scoped pip and Rust caches and produces the complete-run artifacts that
the [timing models](#timing-models) are refreshed from. Because a `main` push
does not refresh the shared pip cache (keyed on `pyproject.toml` and
`constraints.txt`), a pull request's first run after a dependency merge, until
that night's run, restores an older entry by prefix, purges it, and reinstalls
(see [pip cache](#pip-cache)).

Manual runs (except shard benchmarks) and the release tag's `publish.yml` call
also select every job. The concurrency group includes the event, so a `main`
push never cancels the nightly or a manual run.

### Built distributions

`build-dist` builds the wheel and sdist once and uploads them as `pr-dist`. The
classifier selects it whenever `package-artifacts`, `bwave-smoke`, or
`riscv-image` runs. The image jobs start from it directly instead of waiting
for the install and sdist checks in `package-artifacts`, so they finish
sooner; the cost is that a wheel that later fails those checks still spends
image-job minutes.

## Flake stress

The `flake-stress` job repeats the test modules a pull request adds or
modifies, on Ubuntu and Windows with Python 3.14 (`pytest --count=N -n 4
--dist=load`, with Windows keeping its timeout-headroom check). Any failed
repetition fails `ci-required`, so a flaky test fails here instead of in the
merge queue. Only ordinary pull requests run it: pushes, scheduled and manual
runs, and Mergify queue drafts skip it.

`.github/scripts/ci_flake_stress.py` picks the modules and sizes `N` (at most
10) from the Windows timing model, within a budget of Windows test time. A
broad change falls back to stressing only its added modules, or skips the job;
a notice on the `changes` job names each module left out. Reproduce a failure
locally with `pytest <module> --count=10 -n 4 --dist=load`.

## Sandbox-only tests

Windows runs Booley only as a host. Goal Mode, Booley Flows, Specialists, MCP
tools, the Dashboard, chat, and worktree code run in the
[Sandbox](../../docs/GLOSSARY.md#using-booley), a Linux container, so their
tests would only find Windows failures in code that never runs there. The
`sandbox_only` marker, registered in `pyproject.toml`, names those tests. On
Windows a marked test is skipped with the reason `sandbox_only: exercises
Sandbox-only code, which never runs on a Windows host; runs on Linux CI`. On
Linux nothing changes, and no workflow may mention `sandbox_only`
(`test_ci_mark_expressions_never_deselect_sandbox_only_tests`), so no mark
expression can drop these tests on Linux.

Only `tests/sandbox_only.py` applies the marker, by directory or by test
module, never per test; a guard test rejects any other use. Add a test module
there when its subject (the module it exercises) runs only in the Sandbox. A
test of code a host command can import stays unmarked, even in a Sandbox-heavy
directory: list it in `WINDOWS_HOST_TESTS` in that file.

`tests/architecture/test_sandbox_only_tests.py` keeps the list honest. It
builds the import graph of `src/booley` (`tests/architecture/sandbox_reach.py`,
on the analyzer behind the
[source dependency contract](../../docs/internals/SOURCE-DEPENDENCY-CONTRACT.md))
and takes as host roots the console scripts other than `booley-mcp`, the host
side of `booley` (everything `main` can reach except the `SESSION_RUNTIME`
handlers in `COMMAND_LOCATIONS`), and the built-in Flow and Specialist modules,
which endpoint help imports on the host. The test fails when a module reachable
from a root is covered only by Sandbox-only tests; its message names the
module, its tests, and the import path from the root. A test covers a module it
imports or names in a string such as a `mock.patch` target. Tests under
`tests/architecture` do not count.

The few imports declared in `SANDBOX_ONLY_LAZY_IMPORTS` run only on the Sandbox
MCP call path and are left out of the graph. Each records the functions that
hold it, the modules that reference them, and the functions inside the source
module that call them, so a new direct caller fails the test until someone
re-reads it. The guard has two blind spots: it counts a test as Windows
coverage even if a `skipif` or the `native_bwave` marker keeps it off Windows,
and it works per module, not per function, so a host-reachable function tested
only by Sandbox-only tests passes if another test covers its module. Keep
tests of help text, argument parsing, and config loaders on Windows.

Shard balance weighs the skipped tests at zero. `ci_pytest_shard.py` passes
nodes with an unconditional `skip` marker to `assign_shards`, which spreads
them round-robin, so Windows shards balance the work that actually runs.
`windows-test-timings.json` keeps the Sandbox-only entries because
`ci_flake_stress.py` sizes one repeat count for both operating systems from
it. A test keeps the Windows 3.11 and 3.13 compatibility list free of
Sandbox-only paths, so a leg cannot pass by skipping everything.

## Shard safety

`.github/scripts/ci_pytest_shard.py` collects the eligible tests on every
runner. Timings influence balance only: a new or unknown test is always
assigned to a shard, at the model's one-second default weight. The `test` job
runs the Windows shards, the Ubuntu 3.11 shards, and the compatibility legs;
`test-verify` checks the exact node-ID sets of the Windows and Ubuntu 3.11
shard groups. The independent `coverage-shards` job runs the Ubuntu 3.14 suite
balanced by `linux-test-timings.json`, and `coverage` verifies its six shard
selections before combining their raw data and enforcing the global (80%) and,
on pull requests, changed-line (90%) thresholds. `ci-required` waits for both
branches. Each verifier fails if a test is omitted, duplicated, or collected
differently by two shards.

### Timing models

`windows-test-timings.json` and `linux-test-timings.json` hold call-time
weights. Each stored weight is the median of the observed call times of a
currently eligible test, kept only when it exceeds the one-second default;
missing or unobserved tests use the default, and stale entries are harmless.
Neither model may list a `tests/goals/` test (`tests/ci/test_ci_test_shards.py`).

Every shard writes `test-timings.json` with three parts:

- `phases`: the slowest worker's collection time, controller wall time, and
  execution wall time. GitHub job-step timestamps add runner setup and package
  install time around them.
- `tests`: call seconds per node ID. Shard balance and model refreshes read
  only this.
- `test_phases`: the `setup`, `call`, and `teardown` seconds pytest reported
  per node. Nothing is zero-filled, so a skipped test or one that errors in
  setup lists only the phases it reached and is absent from `tests`.

Weights stay call-only because setup is dominated by module and session
fixtures charged to whichever test first triggers them on each xdist worker,
so it moves with scheduling and is not a stable per-test weight.

Shard timing files are in the `shard-<group>-<index>` artifacts: the Ubuntu
3.11 files in `shard-linux-3.11-<index>`, the Windows ones in
`shard-windows-<index>`, and the Ubuntu 3.14 ones in `shard-coverage-<index>`.
The coverage files include tracing overhead, so refresh
`linux-test-timings.json` from the 3.11 files. A manual run with one Ubuntu
shard publishes its complete-suite file as `pytest-timing-<leg label>`, on
every outcome including failed test runs.

## Shard counts and benchmarks

Manual `Tests` runs accept dispatch inputs that change the shard layout, so a
candidate layout can be measured without a code change. Pull requests, pushes,
and reusable-workflow calls always use the defaults; `ci_changes.py` rejects a
non-default Ubuntu count or scheduler, or a benchmark flag, outside
`workflow_dispatch`.

| Input | Values (default first) | Effect |
| --- | --- | --- |
| `windows_shard_count` | 4, 6, 8, 10 | Windows Python 3.14 shard count |
| `linux_shard_count` | 2, 1, 3 | Ubuntu Python 3.11 shard count; 1 runs one complete-suite job |
| `linux_scheduler` | `loadscope`, `worksteal` | `--dist` for the Ubuntu 3.11 legs; Windows always uses `worksteal` |
| `windows_shard_benchmark`, `linux_shard_benchmark` | false, true | Select the ordinary Python source change's job set, skipping unrelated image work; may be combined with each other, not with a RISC-V measurement arm |

Every candidate keeps the same compatibility legs, marker selection,
four-worker scheduler, timing model, and exact-union verification.

The defaults come from measured runs:

- **Four Windows shards.** Skipping Sandbox-only tests removed about 62% of the
  summed Windows test time. Four shards keep every shard's pytest step at least
  as far from its 12-minute step limit as the slowest ten-shard step was
  ([#3338](https://github.com/boldaxolotl/booley/pull/3338)).
- **Two Ubuntu 3.11 shards with `loadscope`.** In a controlled comparison on
  one commit, two shards cut the median required gate by 127 s against one
  complete-suite job, and `worksteal` showed no gain
  ([#1792](https://github.com/boldaxolotl/booley/issues/1792)). Over 349
  ordinary passing runs afterwards, the median gate fell by about 150 s for
  about 9-12 more runner minutes per run
  ([#1793](https://github.com/boldaxolotl/booley/issues/1793)).
- **Six coverage shards.** They keep a shard plus the `coverage` combine step
  off the merge queue's critical path, which `bwave-smoke` sets at about 14
  minutes. The `coverage` job's verifier must use the same count.

## Performance telemetry

The `ci-metrics` job runs after `ci-required` and records workflow queue time,
completed-job queue percentiles, consumed runner time at observation, a rounded
job-minute estimate, and workflow elapsed time. Only jobs that completed in the
current attempt and were not skipped count, so the collector itself, still
running, never does. It writes the measurements to the job summary and keeps
the JSON artifact for 90 days, even after a collection failure.

A performance claim about CI needs at least 20 ordinary code-changing runs and
includes queue time, not job runtime alone, because hosted runners are shared
and a single run's timing varies widely. A benchmark comparison records setup,
collection, execution, job queueing, required-gate elapsed time, and total
runner minutes.

## Smoke diagnostic evidence

`bwave-smoke` always attempts to publish `junit-coverage-release-*` (JUnit XML
and incremental `test-timings.jsonl`) and `openroad-runtime-*` (logs, scripts,
Yosys check reports, and placement run directories, including partial evidence
from failed probes). A successful OpenROAD probe still fails if its required
evidence cannot be exported. These artifacts are kept for 14 days. Missing
evidence warns when a producer never started or was terminated before writing
it.

The incremental report writes one complete JSON line as each pytest setup,
call, or teardown report arrives, so completed-test timings survive process
termination even when pytest cannot finish its JUnit XML. Each line holds the
node ID, phase, outcome, `duration_seconds`, and UTC `recorded_at`. Sum the
phases for a test's total; an interrupted test may have only setup evidence.
Runner loss or a hard job timeout can still prevent the upload steps from
running.

`tests/ci/test_evidence_uploads.py` checks that `test.yml` uploads the JUnit
and diagnostic outputs: timeout diagnostics, shard manifests and timings,
complete-suite phase timings, composite-action input gates, container mounts,
and publication after producers. Caches, temporary test projects, and build
products are not diagnostic outputs.

## RISC-V image lane

The `riscv-image` job builds and checks the RISC-V
[Sandbox Image](../../docs/GLOSSARY.md#using-booley). It is selected only by
its own inputs on every event: `RISCV_IMAGE_PREFIXES` and `RISCV_IMAGE_FILES`
in `.github/scripts/picorv32_ci_inputs.py` (the demo, Goal, review, and
evidence code among them) and any Dockerfile or image data change. Nightly and
manual runs select it only when the last commit changed those inputs, and a
`main` push never does, because its queue candidate already ran it.

### Measurement arms

The `Tests` workflow's `riscv_measurement` dispatch input accepts `automatic`
(the default: path gating and registry reuse) and `cold`, which forces the
lane and builds the tooling locally:
`gh workflow run test.yml --ref <branch> -f riscv_measurement=cold`. Any arm
other than `automatic` requires `workflow_dispatch`.

### Tooling stage

`Dockerfile.riscv` builds the xPack GCC toolchain, Spike, and the offline
RISC-V specifications in a `riscv-tooling` stage that starts from the same
digest-pinned Ubuntu image as `Dockerfile.base` and depends on no Booley image
([ADR 0070](../../docs/adr/0070-reuse-riscv-tooling-from-a-published-tooling-stage.md)).
The final stage copies `/opt/riscv` and `/opt/riscv-docs` from it onto the
exact standard substrate, so every candidate runs the full RISC-V contract,
size, and demo checks.

The tooling key is a hash of that stage's exact text, a schema version, and the
platform: `python .github/scripts/riscv_tooling.py key`. Any edit inside the
stage, including a comment, mints a new key. The script fails closed when the
stage gains a build-context `COPY`/`ADD`, a `RUN --mount`, an undigested
`FROM`, or a global `ARG`, because any of those would change the tooling
without changing its key.

`riscv-tooling-publish.yml` runs on `main` when `Dockerfile.riscv`, the key
derivation, or the workflow itself changes, or by hand. It builds
`--target riscv-tooling` and pushes the candidate by digest only, so failed
runs leave no tag behind. It verifies the candidate's labels, then runs the
`[riscv]` probes of the Sandbox Image contract
(`.github/contracts/session-runtime.toml`) and the tooling paths against it
(`riscv_tooling.py checks`): toolchain, multilibs, hard links, Spike, and the
shared-library closure of every host ELF under `/opt/riscv`. It records the
builder's `libc6`, `libstdc++6`, `libgcc-s1`, and `g++` versions, and only then
creates `ghcr.io/boldaxolotl/booley-sandbox-base:riscv-tooling-<key>` with
those versions as an index annotation. `riscv_tooling.py promoted` then
confirms the tag serves the verified image manifest. An existing key tag with
matching labels ends the run; one with different labels fails it. Only
buildx's exact missing-tag error counts as absence; any other registry error
fails the run. Runs serialize per key. The publisher never overwrites a key
tag, and no other workflow writes one. Check a key with
`python .github/scripts/riscv_tooling.py published`.

The `riscv-image` job consumes the published image. `riscv_tooling.py resolve`
computes the key, resolves the key's tag to a digest, checks its role and key
labels, and writes the **tooling source record**
`riscv-image-evidence/tooling-source.json`:

| Source | When | Lane behavior |
| --- | --- | --- |
| `registry` | The key's tag exists with matching labels | Builds `Dockerfile.riscv` with `--build-context riscv-tooling=docker-image://<repository>@<digest>`, then runs the compatibility check |
| `local` | The tag is absent, the registry errors, the lookup exceeds 120 s, or the arm is `cold` | Builds the stage from source; the record's `reason` says why |
| `local-compat-fallback` | A registry hit whose composed candidate failed to build or failed the compatibility check | Builds the stage from source; any compatibility log remains at the evidence root |

A tag with the wrong labels fails the job: that is an integrity failure, not a
miss. Only the label-verified digest reaches BuildKit. The compatibility check
runs `riscv_tooling.py checks` in the composed RISC-V substrate before the
wheel overlay. A fallback is a warning, not a failure, and the full contract,
size, and demo checks still judge whichever image the lane finishes with.
Repeated fallbacks for one key mean the published image has drifted from the
runtime base; republish it by editing the stage, which mints a new key. Local
and release builds always build the stage from source.

### Duration budgets

The `bwave-smoke` duration budget times the test phase only: its clock starts
after the standard candidate image and its wheel overlay are built, so runner
and cache-network build speed cannot fail it. The RISC-V lane runs as its own
job so it neither delays `bwave-smoke` nor competes with its validations for
CPU. When the stable base is unchanged, `riscv-image` builds its standard
substrate from the trusted cache and then starts its clock; when the base
changed, it builds the runtime base locally and skips the budget. The RISC-V
image build counts against the budget, which follows the recorded source:
registry hits and local builds (fallbacks included) each get their own budget,
derived from measured runs of that source, so a slow hit cannot hide behind
the local-build ceiling. A lane that stopped before writing its record is
judged against the local budget.

## Dependency constraints

`.github/ci/constraints.txt` pins every package the project and its extras can
install, for Linux and Windows on every CI Python version (one universal,
unhashed `uv pip compile`; pip rejects hashed constraints with an editable
install). Every CI install of the project passes `-c`, and the pip cache of
each such job keys on it. Regenerate it from the repository root with the
command in its header:

```bash
uv pip compile pyproject.toml --all-extras --universal --python-version 3.11 -o .github/ci/constraints.txt
```

- `release-semantic` runs `ci_constraints.py` without installing the project,
  so a stale file fails there with the regenerate command. The other jobs do
  not wait for it and may also fail with pip's `ResolutionImpossible`.
- The check catches an added, removed, or re-pinned direct dependency and a
  wrong compile command. It does not notice a new extra or marker on an
  existing direct requirement.
- Unconstrained: the setuptools build backend in pip's isolated build
  environment, pip, and uv. Built-wheel installs (`publish.yml`,
  `docker-publish.yml`, `isolated_host_install_smoke.py`) and image Dockerfile
  installs stay unconstrained on purpose: they verify what users get.

## pip cache

Every `actions/setup-python` step with `cache: pip` has `id: setup-python` and
is followed by a `Drop pip cache restored from an older dependency set` step.
On an exact key hit (`cache-hit == 'true'`) the step is skipped and nothing is
saved. On a miss, setup-python restores the newest entry for the same OS and
Python patch version by key prefix and saves a new entry after the job. pip
never prunes its cache, so without the purge each new entry would carry every
older wheel forward. Windows Python 3.11 is frozen at 3.11.9, the last 3.11
release with a Windows installer, so its prefix never resets: unpurged, its
entries reached 2 GB each, against about 180 MB for a fresh install, and pushed
other caches out of the repository's 50 GB limit. With the purge, a new entry
holds only the current dependency set.
`test_pip_caches_start_empty_after_a_prefix_restore` in
`tests/ci/test_ci_requirements.py` requires the step after every such cache.
An existing bloated entry stays until its key changes or it is deleted.
