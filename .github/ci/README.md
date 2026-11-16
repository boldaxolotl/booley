# CI policy

Why the `Tests` workflow (`.github/workflows/test.yml`) runs what it runs. The
job selection itself is code: read `.github/scripts/ci_changes.py` (categories
and `required_jobs`), `ci_flake_stress.py`, and `ci_pytest_shard.py` for the
mechanics. Local checks and the path from branch to `main` are in
[CONTRIBUTING.md](../../docs/internals/CONTRIBUTING.md), the merge queue in
[merge-queue.md](../../docs/internals/agents/merge-queue.md), Renovate in
[dependency-updates.md](../../docs/internals/agents/dependency-updates.md), and
the image checks in
[session-runtime.toml](../contracts/session-runtime.toml).

## Which Python and OS runs which tests

A pull request covers each Python version and each OS at least once, not every
combination. That keeps the required gate to many short parallel jobs instead
of a full OS-by-Python matrix:

- Python 3.14 on Windows runs the suite in four duration-balanced shards,
  skipping [Sandbox-only tests](#sandbox-only-tests).
- Python 3.14 on Ubuntu runs the complete suite under branch coverage in six
  `coverage-shards` jobs, using coverage.py's `sysmon` core (PEP 669) so one
  instrumented run replaces a plain run plus a coverage run.
- Python 3.11 on Ubuntu runs the complete suite in two duration-balanced shards.
- Python 3.11 and 3.13 on Windows run a fixed list of focused modules (the
  `Run focused Windows compatibility suite` step).

The weekly `full-python-matrix.yml` runs the complete suite on Ubuntu and
Windows for Python 3.11, 3.13, and 3.14. It is the only complete-suite run on
Python 3.13 and on Windows 3.11.

Every complete-suite run uses `-m "not native_bwave"`; the `bwave-integration`
job runs the `native_bwave` tests, which need the compiled B-Wave binary.

## Main pushes and the nightly run

A push to `main` runs only the jobs in `MAIN_PUSH_JOBS` (`ci_changes.py`, which
says why). The suite is not repeated there because the queue candidate already
passed on the same tree; the nightly run covers `main`'s tip.

The nightly run (01:47 UTC on `main`) selects every job except `riscv-image`,
which stays path-gated on every event. It catches what the queue cannot: a
classifier that skipped a needed job, a manual incident merge, and drift in
runners, toolchains, or unpinned dependencies. `main` evidence comes from it.
It also refreshes the `main`-scoped pip and Rust caches and produces the
complete-run artifacts the [timing models](#shard-safety-and-timing-models) are refreshed from.
Until that night's run, a pull request's first run after a dependency merge
restores an older pip entry by prefix, purges it, and reinstalls (see
[pip cache](#pip-cache)).

## Sandbox-only tests

Windows runs Booley only as a host, so tests of code that runs only in the
[Sandbox](../../docs/GLOSSARY.md#using-booley) carry the `sandbox_only` marker,
which skips them on Windows. Where a test goes is the rule in
`tests/sandbox_only.py`; `tests/architecture/test_sandbox_only_tests.py` fails
with the module, its tests, and the import path when a host-reachable module is
covered only by Sandbox-only tests. No workflow may mention `sandbox_only`, so
no mark expression can drop these tests on Linux.

The guard has two blind spots. It counts a test as Windows coverage even if a
`skipif` or the `native_bwave` marker keeps it off Windows, and it works per
module, not per function, so a host-reachable function tested only by
Sandbox-only tests passes if another test covers its module. Keep tests of help
text, argument parsing, and config loaders on Windows.

## Shard safety and timing models

`ci_pytest_shard.py` collects the eligible tests on every runner. Timings
influence balance only: a new or unknown test always lands in a shard. Each
verifier fails if a test is omitted, duplicated, or collected differently by two
shards.

`windows-test-timings.json` and `linux-test-timings.json` hold call-time weights
(the median call time of an eligible test, kept only above the one-second
default). Shard timing files are in the `shard-<group>-<index>` artifacts. The
coverage run's files include tracing overhead, so refresh
`linux-test-timings.json` from the Ubuntu 3.11 files (`shard-linux-3.11-<index>`),
not the coverage ones.

## Shard counts and benchmarks

Manual `Tests` runs accept dispatch inputs that change the shard layout, so a
candidate layout can be measured without a code change; `ci_changes.py` rejects
them outside `workflow_dispatch`. The defaults (four Windows shards, two Ubuntu
3.11 shards with `loadscope`, six coverage shards) come from measured runs; the
inputs and their allowed values are in `ci_changes.py`.

## Test time budgets

A test waits on real time in two ways, and both come from
`tests/time_budget.py`:

- A deadline the test asserts on names its own base budget and scales it:
  `_HOOK_DEADLINE_S = scaled(5)`.
- A wait that only stops a hang uses the hang guard, `hang_guard_s()`: 30 s
  times the factor, rounded up to whole seconds. `SUBPROCESS_TIMEOUT_S` in
  `tests/subprocess_budget.py` is that value.

The factor is `BOOLEY_TEST_TIME_SCALE`. Unset means 1; a value that is not a
positive finite number fails the pytest session at startup. The Windows legs
of every pytest job (the test job, flake-stress, and the weekly full matrix)
set 2 and run `--timeout=120`. Across the 161 tests that the Windows and Linux
timing models both measure at 1 s or more, the Windows to Linux time ratio has
median 1.06, 90th percentile 1.55, and 95th percentile 2.12, so a factor of 2
covers the usual Windows tail. Ubuntu legs keep factor 1 and `--timeout=60`.

The per-test limit must stay at least twice the hang guard, and at least 60 s:
after a guard expires the helper still kills the process tree and drains its
pipes, which takes up to about 32 s more on Windows. `tests/conftest.py`
refuses a session whose `--timeout` is positive and below that minimum, and
raises the default limit (120 s) to the minimum when a factor above 2 would
otherwise push the minimum past it. The check does not cover
`@pytest.mark.timeout(N)` markers, which size one test explicitly.

The step limit of `Run duration-balanced shard` is 15 minutes: the slowest
shard step measured over the 47 latest `Tests` runs (2026-11-15 to
2026-11-16) took 11.3 minutes, and 1.3 times that rounds up to 15, the coverage
shards' limit. The 12-minute limit of the focused compatibility step stays: it
is not a shard, and its slowest run took 1.15 minutes. To re-derive, list runs
with `gh run list --workflow test.yml`, read each job's step times from
`gh api repos/{owner}/{repo}/actions/runs/<id>/jobs`, and take the largest
`completed_at - started_at` of that step.

## Performance telemetry

The `ci-metrics` job runs after `ci-required` and records queue time, runner
time, and elapsed time to the job summary and a JSON artifact. A performance
claim about CI needs at least 20 ordinary code-changing runs and includes queue
time, not job runtime alone, because hosted runners are shared and a single
run's timing varies widely.

## RISC-V image lane

The `riscv-image` job builds and checks the RISC-V
[Sandbox Image](../../docs/GLOSSARY.md#using-booley). It is selected only by its
own inputs (`.github/scripts/picorv32_ci_inputs.py`, any Dockerfile or image
data change). A `main` push never selects it, because its queue candidate
already ran it.

The toolchain lives in a `riscv-tooling` stage published by
`riscv-tooling-publish.yml` and reused by key
([ADR 0070](../../docs/adr/0070-reuse-riscv-tooling-from-a-published-tooling-stage.md);
mechanics in `.github/scripts/riscv_tooling.py`). The key hashes the stage's
exact text, so any edit inside the stage, a comment included, mints a new key.
Repeated `local-compat-fallback` records for one key mean the published image
has drifted from the runtime base: republish it by editing the stage.

## Dependency constraints

`.github/ci/constraints.txt` pins every package the project and its extras can
install; regenerate it with the command in its header, and see
`.github/scripts/ci_constraints.py` for what the check catches. These installs
stay unconstrained on purpose: the setuptools build backend in pip's isolated
build environment, pip, uv, built-wheel installs (`publish.yml`,
`docker-publish.yml`, `isolated_host_install_smoke.py`), and image Dockerfile
installs. They verify what users get.

## pip cache

Every `actions/setup-python` step with `cache: pip` has `id: setup-python` and
is followed by a `Drop pip cache restored from an older dependency set` step.
On a miss, setup-python restores the newest entry for the same OS and Python
patch version by key prefix, and pip never prunes, so without the purge each new
entry would carry every older wheel forward. Windows Python 3.11 is frozen at
3.11.9, so its prefix never resets: unpurged, its entries reached 2 GB each
against about 180 MB fresh and pushed other caches out of the repository's 50 GB
limit. `test_pip_caches_start_empty_after_a_prefix_restore` in
`tests/ci/test_ci_requirements.py` requires the step.
