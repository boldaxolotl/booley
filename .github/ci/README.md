# CI policy

Why the `Tests` workflow (`.github/workflows/test.yml`) runs what it runs. The
job selection itself is code: read `.github/scripts/ci_changes.py` (categories
and `required_jobs`), `ci_flake_stress.py`, `ci_pytest_shard.py`,
`ci_pytest_rerun.py`, `ci_flake_ledger.py`, and `ci_flaky_quarantine.py` for the
mechanics. Local checks
and the path from branch to `main` are in
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

## OS-specific tests

A test module is OS-specific when its subject depends on OS behavior: paths,
file locks, process trees, signals, subprocess launching, console encoding,
symlinks, or a platform branch. Register it by that rule in
`tests/os_specific.py`, the only place that applies the `os_specific` marker.
The marker skips nothing, and no CI job selects on it. The registry prepares for
[#4414](https://github.com/boldaxolotl/booley/issues/4414), which narrows the
Windows lane to tests whose result can differ by OS.

`tests/architecture/test_os_specific_tests.py` fails on:

- a `test_*.py` module under `tests/` that branches on the platform and is not
  registered;
- a module under `src/booley/` that branches on the platform with no registered
  test covering it and no exemption;
- an exemption whose module no longer branches on the platform, or that has no
  reason;
- a registered path that does not exist, or the marker written outside
  `tests/os_specific.py`.

A test covers a module when it imports it or names it in a string, itself or
through a support module; `conftest.py` and `tests/architecture/` do not count.
A platform branch is a platform check or an import of a listed platform-specific
or OS-inspecting module. `tests/architecture/platform_branches.py` lists what
counts and holds the exemptions, `PLATFORM_EXEMPTIONS`, each with its reason.

The guard has these blind spots:

- Detection is syntactic and names a fixed list. These pass unnoticed: feature
  probes (`hasattr(os, "getuid")`, `getattr(signal, "SIGKILL", None)`), an
  aliased or member-imported `sys`, `os`, or `ctypes` name
  (`from sys import platform`), any other platform flag, and `IS_WINDOWS`
  imported any other way.
- A test that fakes the platform (`monkeypatch.setattr(sys, "platform", ...)`)
  is not detected; register it by its subject.
- Coverage is per module: one registered test of a module satisfies every branch
  in it, and a registered test counts even when it is Sandbox-only and Windows
  skips it.
- A test module's own code is all that is checked; a branch or skip marker it
  takes from a support module, `conftest.py`, or another test module is not
  attributed to it.

## Docker CLI guard

A unit test that starts the host's `docker` is slow, load-sensitive, and depends
on the host. `tests/docker_cli_guard_plugin.py` therefore fails every test
outside `tests/docker/` and `tests/smoke/` that starts a `docker` process: at
once, naming the command line, or at teardown when the failure was swallowed, as
on a worker thread or by a broad `except`.

A test opts out in one of two ways:

- It carries the `docker_cli` marker, on the test or as a module `pytestmark`.
- It installs a shared fake that puts a `docker` program on `PATH`:
  `install_fake_docker`, `install_sandbox_exec`, or `PathToolFake.install` for
  `docker`. Fakes that replace a Booley runner start no process and leave the
  guard on.

The guard watches `subprocess.Popen` in the test process. It does not see a
`docker` started by a child process the test runs, a shell command line
(`shell=True`), `os.system`, `os.posix_spawn`, or `os.exec*`.

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

## Rerun of failed tests

The merge queue retries nothing, so one rare test failure would eject a PR or
batch. The pytest steps of the `docs-check`, `test`, and `coverage-shards` jobs
therefore run through `.github/scripts/ci_pytest_rerun.py`; other jobs do not
rerun. When a run ends with test failures or errors (a timeout-headroom overrun
is one), it reruns only the failed test ids once, in a separate serial pytest
process (`-n 0`, no shard options). The rerun keeps the mark expression,
per-test timeout, and time scale. The job passes when every rerun passes, or
when only [quarantined](#quarantine-of-flaky-tests) tests fail again.

- A test that fails once and passes on the rerun is recorded in the step
  summary ("Passed on rerun") and in `junit.xml` as `flakyFailure` or
  `flakyError` inside its `testcase`, with the failure counts reduced. A test
  that fails again keeps its failure and gains `rerunFailure` or `rerunError`;
  the log shows both failures. A quarantined test's failure becomes
  `quarantinedFailure` or `quarantinedError`, with the failure counts reduced.
- No rerun starts, and the step summary says why, after a collection error, a
  shard-selection error or any other exit that is not a test failure, a missing
  or malformed JUnit report,
  or more than `MAX_RERUN_FAILURES` (5, in `ci_pytest_rerun.py`) failed tests,
  which likely means a deterministic break. A coverage shard also skips the
  rerun after a crashed worker, because that worker's coverage data is lost.
- The rerun's coverage data is combined into the shard's one `.coverage.<index>`
  file, so lines only the failed attempt ran stay counted.
- `flake-stress` never reruns: its job is to expose flakiness.
- The rerun gets no extra time: it counts against the same step or job
  `timeout-minutes` as the first run.

## Flake ledger

A green rerun or a quarantine hides a flake from the PR that hit it, so
`.github/scripts/ci_flake_ledger.py` collects these hits across runs. The
`.github/actions/publish-pytest-evidence` action uploads a `flaky-<label>`
artifact, a copy of `junit.xml`, only when that report holds a `flakyFailure`,
`flakyError`, `quarantinedFailure`, or `quarantinedError`. The script downloads
only those artifacts, from the completed `Tests` runs created in the window, and
rebuilds the ledger on every call without storing anything.

```bash
PYTHONPATH=src python .github/scripts/ci_flake_ledger.py [--days N] [--json]
```

- Run it from a checkout, with `gh` signed in; the window is 7 days unless
  `--days` says otherwise.
- A hit is one test ID in one workflow run: several jobs or attempts of that run
  count once. A test ID with 2 or more hits in the window is a filing candidate.
- The script also reports which test IDs meet the
  [quarantine bar](#quarantine-of-flaky-tests). It reads the open `flaky` issues
  for this, and only hits inside the window count.
- A test whose module no longer exists in the checkout is listed by its JUnit
  name, `<classname>::<name>`.
- The default output is readable text. `--json` prints only a document with
  `window`, `min_hits`, `runs_read`, `ledger` (each `test_id` with its `hits`:
  run ID and URL, date, branch, jobs, first line of the failure message),
  `candidates`, which add a draft issue `title` and `body`, `quarantine_bar`,
  and `quarantine` (`test_id`, the owner `issue` number or null, `reasons`).
- It exits 1 with no ledger, naming the run or the listing (runs or issues),
  when the API cannot be read in full, a `flaky-*` artifact is expired or
  unreadable, or a job ran its tests but its evidence upload failed or was
  cancelled. A job that published nothing because its pytest step failed or
  timed out counts as having no hit.
- In-progress runs are not read. Runs of a commit without the `flaky-<label>`
  upload carry no marker, so their hits are not counted.
- No workflow here runs the script or files issues. The external issue pipeline
  runs it and files or comments on `flaky` issues as in
  [Flaky-test issues](../../docs/internals/agents/issue-tracker.md#flaky-test-issues).

## Quarantine of flaky tests

A repeat flaky test can be quarantined so that it stops blocking unrelated PRs.
It is not skipped: it runs in every job that selects it, and it stays visible
until its issue is fixed.

- In the jobs that [rerun](#rerun-of-failed-tests), a quarantined test that
  fails on the run and does not pass the rerun does not fail the job. The log,
  the step summary, and a warning annotation name the test ID and owner issue,
  and `junit.xml` records `quarantinedFailure` or `quarantinedError`, which the
  [flake ledger](#flake-ledger) counts as a hit.
- The job still fails when no rerun starts, for example after more than
  `MAX_RERUN_FAILURES` failures, when the rerun process ends other than with
  test results, or when its coverage data cannot be combined.
- `flake-stress` does not apply the quarantine, so a PR that adds or modifies
  the test's module must pass it there.
- The registry is `tests/flaky_quarantine.py`. Each entry is the exact pytest
  node ID, parameter case included, and the number of its owner issue: the open
  [`flaky` issue](../../docs/internals/agents/issue-tracker.md#flaky-test-issues)
  whose title names that ID.
- Add an entry only for a test ID the flake ledger reports as meeting the
  quarantine bar: at least 3 hits within 7 days, or a hit from a run created
  after its open `flaky` issue was opened. Adding one is a reviewed PR; no
  workflow does it.
- The `lint` job runs `.github/scripts/ci_flaky_quarantine.py`. It fails an
  entry with no owner issue, with a test ID pytest does not collect, or listed
  twice, and one whose owner issue is closed, is a PR, lacks the `flaky` label,
  or has a title that does not name the test ID. The PR that fixes the test must
  remove its entry, or later `lint` runs fail once the issue closes.

## Test time budgets

A test waits on real time in two ways, and both come from
`tests/time_budget.py`:

- A deadline the test asserts on names its own base budget and scales it:
  for example `scaled(5)`.
- A wait that only stops a hang uses the hang guard, `hang_guard_s()`: 30 s
  times the factor, rounded up to whole seconds. `SUBPROCESS_TIMEOUT_S` in
  `tests/subprocess_budget.py` is that value.

The factor is `BOOLEY_TEST_TIME_SCALE`. Unset means 1; a value that is not a
positive finite number fails the pytest session at startup. The Windows legs
of every pytest job (the test job, flake-stress, and the weekly full matrix)
set 2 and run `--timeout=120`. Across the 161 tests that both the Windows and
Linux timing models contain, the Windows to Linux time ratio has
median 1.06, 90th percentile 1.55, and 95th percentile 2.12, so a factor of 2
covers the usual Windows tail. Ubuntu legs keep factor 1 and `--timeout=60`.

The per-test limit must stay at least twice the hang guard, and at least 60 s:
after a guard expires the helper still kills the process tree and drains its
pipes, which takes up to about 32 s more on Windows, so the rule is sized for
the Windows factor 2. `tests/conftest.py`
refuses a session whose `--timeout` is positive and below that minimum, and
raises the default limit (120 s) to the minimum when a factor above 2 would
otherwise push the minimum past it. The check does not cover
`@pytest.mark.timeout(N)` markers, which size one test explicitly.

The step limit of `Run duration-balanced shard` is 15 minutes: the slowest
shard step measured over the 47 latest `Tests` runs (2026-11-15 to
2026-11-16) took 11.3 minutes, and 1.3 times that rounds up to 15, the coverage
shards' limit. The 12-minute limit of the focused compatibility step stays: it
is not a shard, and its slowest run took 1.15 minutes. To re-derive, list runs
with `gh run list --workflow test.yml --limit 47`, read each job's step times
from `gh api repos/{owner}/{repo}/actions/runs/<id>/jobs`, and take the largest
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
