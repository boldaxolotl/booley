# Booley Coding Principles

Design and correctness rules for Booley's own Python code, not the user's RTL
(see the [README](../../README.md) for what Booley is). Formatting is left to
Ruff, the Python linter and formatter. Docs follow
[DOCS_PRINCIPLES.md](DOCS_PRINCIPLES.md); environment setup and the
contribution workflow are in [CONTRIBUTING.md](CONTRIBUTING.md).

**Audience:** this document is written mainly for agents. Agents should apply
these principles to improve the codebase, both when writing new code and when
reviewing or refactoring existing code. Human contributors follow the same
principles when working on Booley itself.

Sources: NASA Power of Ten (Holzmann), SOLID (Martin), A Philosophy of
Software Design (Ousterhout).

These principles are enforced by review and automated checks where practical.
The repository's configuration and `.github/workflows/test.yml` are the source
of truth for the current tools, thresholds, and CI matrix.

---

## Complexity Control

### 1. Functions do one thing and fit on a screen

50 lines max. If you need a comment to separate "sections" inside a
function, those sections are separate functions.

### 2. Deep modules, shallow interfaces

A module's interface should be simple relative to the complexity it hides.
A function with 8 parameters that wraps 10 lines of logic is a net
negative: it moved complexity to every call site.

### 3. No magic

No metaclasses, no monkey-patching in production code, no dynamic
attribute generation. If `grep` can't find where a name is defined, the
code is too clever.

### 4. Bounded iteration

Every loop must have an obvious termination condition. `while True`
requires a visible bound: a counter, a timeout, or a retry limit.
Unbounded retries are bugs waiting for a trigger.

## Correctness

### 5. Validate at boundaries, trust internally

Validate all external input (CLI args, file content, API responses, user
data). Inside the module, trust your own types: don't re-validate what
you constructed.

Reach for the `booley.core.boundary` helpers (`src/booley/core/boundary.py`) —
`as_dict`/`require_dict`, `as_str_list`, `as_int`/`as_float`,
`require_finite_number`, `as_positive_int`, … — instead of hand-rolling another
`isinstance`/`float()`/try-except guard. Every numeric coercer there already
rejects NaN/inf and the `isinstance(True, int)` bool trap (in Python, `bool`
subclasses `int`, so a naive `isinstance(x, int)` wrongly accepts `True`/`False`).

### 6. Fail fast, fail loud

Raise exceptions on unexpected state. Don't return `None` and hope the
caller checks. Use assertions for invariants that "can't happen." A crash
with a stack trace is better than silent corruption.

### 7. Errors are part of the design, not afterthoughts

Define error cases before writing the happy path. Use specific exception
types. Never bare `except:`. Log context (what was attempted, with what
inputs) not just "something failed."

## Design

### 8. Keep related responsibilities together

Code that changes for the same reason belongs together. Split a module when its
parts change independently and the resulting interfaces become simpler—not
merely because the module performs more than one operation.

### 9. Keep domain policy independent of infrastructure

Domain logic does not import CLI, UI, agent-SDK, process, or container adapters.
Pass narrow data or behavior across those boundaries. Introduce an abstraction
for a real seam—multiple implementations, an external boundary, or deterministic
testing—not for a hypothetical future use.

### 10. State changes are atomic and recoverable

For persisted or external state, define what happens when execution stops after
each step. Validate before mutation, publish complete state atomically, and make
retries idempotent where the caller can repeat an operation. Never expose a
partially written state as successful.

## Analyzability

### 11. Type-annotate public interfaces

All public functions, methods, and class attributes get type annotations.
Internal helpers: use judgment. The goal is that a type checker can catch
misuse at module boundaries.

### 12. Zero linter warnings

Ruff must pass clean before merge. Suppressions (`# noqa`) require a
comment explaining why. See `[tool.ruff]` in `pyproject.toml` for the active rule set.

### 13. One date/time language

Machine timestamps are second-resolution UTC RFC 3339 with a `Z` suffix:
`2026-08-10T09:11:49Z`. Parsers also accept other UTC offsets and fractional
seconds, but new persisted values use the canonical form.

Human-visible dates use uppercase English three-letter months regardless of
process locale: `10 AUG 2026`. Use `booley.runtime.timefmt`; do not hand-roll
another format string. Persisted diagnostic log prefixes, and the same
prefixes echoed to the terminal, use the machine form instead, so events can be
correlated across files.

## Verification

### 14. Tests prove behavior

Every bug fix includes a regression test that fails without the fix. New
behavior is tested at the closest stable boundary, including meaningful error
cases and edge cases. Prefer tests of observable contracts over tests coupled
to implementation details.

A test earns its place only if a plausible bug in production code makes it
fail. Before adding one, name the bug it catches. These catch none; do not
write them:

- **Copied logic.** Test-file helpers that reimplement production logic, or
  expected values computed the way the code computes them (the same `max()`,
  f-string, or path helper). Call the production code and assert a literal
  expected value.
- **Mock echo.** A stub returns a sentinel and the test asserts the sentinel
  came back. Assert what the code does with collaborator output or what it
  passes in, or use a real collaborator (a temporary Git repository, a real
  store).
- **Language checks.** Dataclass field storage or defaults, `frozen=True`
  raising, `__init__` storing its arguments, `isinstance(X(), X)`, a
  constant's type. Python already guarantees these.
- **Constant mirrors.** `assert CONST == <literal copied from source>`. Test
  the behavior the constant drives instead: a bound rejected, a path used. Pin
  a value only when an external contract (persisted format, public CLI, ADR)
  fixes it, and name that contract in the test.
- **Unfailable tests.** No assertion, or mocks that make every outcome
  succeed. A "does not raise" test is valid only when raising is the plausible
  bug.
- **Dead setup.** Setup the code under test never sees, such as an
  environment built and then discarded. If the test name promises a behavior,
  the test exercises it.
- **Duplicates.** A test that can only fail when another test also fails,
  including wrappers that re-call a parametrized test with its own parameters.
- **Tests that keep dead code alive.** If only a test calls a function,
  delete both.
- **Repository pins.** Reading a repository file (a workflow, a Dockerfile,
  `.github/renovate.json`, `.mergify.yml`, a skill, a doc) and asserting its
  literal text, keys, step order, or layout. Such a test fails when someone
  edits the file on purpose, not when Booley breaks. Instead, rely on the
  owning tool's own validation (Renovate's config validator, Mergify's
  check on its own config, actionlint), run the workflow or script, or
  leave the text to review. Five kinds of check stay, because each catches
  a break that neither the tool nor a run reports:
  - **Supply chain.** What a build or release pulls in, and what untrusted
    code can reach: every download verified by digest or checksum, no
    mutable tag (`latest`, a branch name), attestation and provenance on
    what is published, nothing promoted to a shared tag before it passes
    verification, and no secret or write token reachable by code from a
    pull request or a Renovate or Mergify branch. Assert that property
    alone, not other step positions or step names.
  - **Generator or source sync.** Text derived from a source, such as a
    generated file or a reference doc that lists the CLI's options: the
    test regenerates it or reads the source (the generator, the parser, the
    other file) and asserts they agree. An expected value typed into the
    test is a constant mirror, not a sync check.
  - **Ratchets.** A count that may only shrink, such as the Pyright
    exclusion list: the test fails when the count exceeds a ceiling that
    each fix lowers. A limit the next change may raise is a pin.
  - **Link and command resolution.** The test follows each link, or runs
    each documented command, and asserts that it resolves. Asserting that a
    doc names a given link or command is a pin.
  - **Required-check integrity.** What keeps a required check from going
    green without doing its work, in the spirit of principle 15: a skipped
    job never counts as a pass, and every evidence file a job declares
    reaches an upload that runs even when the job fails. Assert that
    property, such as the merge rule demanding an explicit success.

  A test that mixes pins with these checks keeps only the checks.

A test replaces only what lies outside Booley: a command, Docker, the network,
the clock, the host environment. Booley's code between the entry point the
test calls and that boundary runs for real, because replaced code is untested
code. When a test needs something replaced:

- **Do not patch Booley's own logic, public or private.** The test keeps
  passing after production stops calling the patched function. Moving a patch
  from `_helper` to a public function keeps the mock. Call the public entry
  point (a module's public function, the CLI, an MCP tool) and assert its
  result.
- **Prefer the real collaborator**: a temporary Git repository, a real store,
  a file under `tmp_path`.
- **Patch a Booley name only where it is the boundary**: the one function that
  runs the command, reads the clock (principle 16), or tells the host from the
  Sandbox. Mark a patch of such a boundary with `# boundary: <reason>` on any
  line of the patch call, the reason non-empty and naming the boundary. Any
  other patch of a Booley name carries a comment that says why no public entry
  point reaches the behavior; it still counts toward the patch check below.
- **One shared fake per boundary.** The fake lives in a support module under
  `tests/`, not in a `test_*.py` file, and every test of that boundary uses
  it. A test does not patch `subprocess` or a command runner itself: copies
  of a stand-in drift apart, and away from the tool they imitate. The
  [test fakes catalog](TEST_FAKES.md) names the fake for each boundary: extend
  it rather than add another. A new fake module, named with `fake` or
  `double`, needs a catalog row for a boundary no fake covers yet.
- **Cross a boundary in one place.** New code that runs a command or reads
  the clock does it through one function (principle 9), so a test has one
  name to replace.

The `test-ratio` job in the `Tests` workflow, part of `ci-required`, counts
patches in the `.py` files under `tests/` that a pull request adds, changes or
deletes, test modules and support modules alike
([exact definitions](../../.github/scripts/ci_patch_count.py)). It fails when
the pull request raises the patches of Booley names (public plus private) or,
separately, the patches of commands (`subprocess` or its `run`, `Popen`,
`check_output`, `check_call` or `call`; `os.exec*`, `os.fork`,
`os.posix_spawn*`, `os.system`, `shutil.which`, `run_captured_bounded`), and
names each added patch. Moving a patch between files does not raise the total.
A patch marked as above does not count; reviewers judge the reason. A
maintainer exempts a pull request with the `patch-count-exempt` label; the job
reads the label each time it is re-run.

To check a new test, break the code it covers: flip a condition, swap `min`
for `max`, drop a call. If the test still passes, it catches nothing.

### 15. A green check must prove it ran

Required CI checks pass before merge. CI and test-infrastructure changes must
demonstrate that the intended files and tests were exercised; a job that
silently analyzes nothing, collects no relevant tests, or skips a required
suite is a failure even when its process exits successfully.

### 16. Verification is deterministic and bounded

Control time, randomness, concurrency, and external state in automated tests.
Subprocesses, retries, and integration tests have explicit time bounds. When an
optional tool or licensed environment is unavailable, skip explicitly with the
missing prerequisite; required suites assert their expected execution rather
than accepting silent skips.

Tests that touch time, processes, sockets, or the filesystem also:

- Order concurrent steps with events, handshakes, or polling a condition
  against a deadline. A fixed `sleep` before an assertion, or a stand-in that
  waits a fixed time before answering, is a race.
- Size inner deadlines (`subprocess.run(timeout=...)`, poll loops) with the
  shared helper in [`tests/time_budget.py`](../../tests/time_budget.py): a
  named base budget passed to `scaled`, or `hang_guard_s()` for a wait that only
  stops a hang (see
  [Test time budgets](../../.github/ci/README.md#test-time-budgets)).
  Windows CI fails any test that uses more than 80% of its timeout (see
  [`tests/timeout_headroom.py`](../../tests/timeout_headroom.py)).
- Bind port 0 and read back the assigned port.
- Pass on Windows or skip there with the reason: guard POSIX-only signals
  (`SIGKILL`, `SIGHUP`), file modes, and symlinks, and close handles before
  deleting or renaming files.
- **Leave nothing running.** A test ends with every thread, subprocess, and
  detached job it started stopped, including work that tools start on their
  own, such as Git auto-maintenance. Code that starts a background thread
  stops and joins it on every way its owner exits.
- **Patch time narrowly.** Inject a clock, or patch a name the module under
  test owns. Never replace `time.monotonic` or `time.time` on the shared
  `time` module, whether directly, through a module's `time` alias, or by a
  dotted string ending in `.time.monotonic` or `.time.time`: every thread in
  the worker sees it.
- **The checkout is live.** A test that needs a copy of the repository copies
  only the files it needs, such as the tracked files `git ls-files` lists. It
  never snapshots the working directory, where other processes create and
  delete caches and coverage files.
- **Fake a process identity fully.** A fake PID in the real range (such as
  `4242`) can belong to a live host process, so a test that stubs
  `is_pid_alive` but leaves `/proc/<pid>` reads real sees a foreign process.
  Use a PID above the kernel limit (`4_194_305`) or fake every check.
- **Ready means ready to observe.** A child that prints "ready" and then blocks
  in `read()` can lose a signal sent in between: Python runs its handler only
  between bytecodes, so the read blocks anyway. Wait with `select` on
  `signal.set_wakeup_fd`, or with a timeout.
- Exercise new process, signal, or cancellation code with a repeated run of
  its test modules (`pytest <module> --count=10 -n 4 --dist=load`) before
  review. The `flake-stress` CI job repeats only the test modules a pull
  request adds or modifies; see `.github/ci/README.md`.

## Proportion

### 17. No bloat

Write the smallest change that fully solves the stated problem. Every line
traces to an acceptance criterion or a demonstrated failure; "it might happen"
is not a reason. Before adding code, delete or reuse what exists.

These are bloat. Do not write them:

- **Repeated checks.** Validate once at the boundary (principle 5). Do not
  re-check the same fact at each layer, before and after each step, or on every
  read.
- **Distrusting your own output.** A record Booley publishes atomically
  (principle 10) needs a parse and a schema-version check on read, not a
  re-proof of every field, count, digest, or ownership claim.
- **Impossible-state defenses.** No guards for states nothing in the system
  can produce: concurrent writers that cannot exist, files nobody else writes,
  inputs a previous step already rejected.
- **Compatibility with unreleased data.** A format no release tag contains
  has no users. Change it in place; never migrate, replay, or test it as
  legacy.
- **Single-use seams.** No Protocol, facade, registry, adapter, or option with
  one implementation and no real seam (principle 9).
- **Test-only shims.** No re-exports, aliases, or wrappers kept only so
  existing tests can patch private names. Update the tests.
- **Reimplementing tools.** Use the tool's own interface (Git, FuseSoC,
  argparse, a TOML library) instead of re-deriving its parsing, discovery, or
  precedence rules.
- **Hardened presentation.** Labels, titles, progress text, and hints are not
  security state. Show a fallback when they are missing or malformed.
- **Test sprawl.** One test per behavior and per meaningful error class
  (principle 14). Do not enumerate exotic inputs for one validator, pin
  private constants, or name test files after review rounds.

Review findings follow the same rule. Fix a finding that shows a reachable
failure. Reject one that only adds a defense against an unreachable state, and
say why.

### 18. Pull requests stay small

A pull request delivers one coherent change that merges on its own (see
[One PR per issue](agents/agent-brief.md#one-pr-per-issue)). Count additions
plus deletions:

- **400 lines** of non-test code and configuration (`src/`, `crates/`,
  `.github/`, scripts).
- **1,000 lines** in total, including tests and docs.

Tests count toward the total. More than two test lines per changed source line
means duplicated cases (principle 17).

The `test-ratio` job in the `Tests` workflow, part of `ci-required`, fails a
pull request whose added test lines exceed twice its changed source lines,
unless it removes more test lines than it adds. Test lines are `.py` and `.rs`
files under test directories and inline `#[cfg(test)]` items in Rust; data
files under test directories (`.vcd`, `.json`) do not count. A maintainer
exempts a pull request with the `test-ratio-exempt` label; the job reads the
label each time it is re-run. The same job runs principle 14's patch count.

A PR that removes more lines than it adds counts only its additions. The caps
exist to keep new code reviewable; deleting duplicated or obsolete code shrinks
what reviewers must hold in their heads, so it should not force a split. Check
each cap on its own: non-test code and the total each qualify only when their
own net change is negative. Every addition still counts, so a cleanup cannot
carry a large rewrite in under it.

Not counted: deletions of whole files or dead code, mechanical renames or
moves with no logic change, files regenerated by a committed generator,
lockfiles, and vendored upstream sources. Say in the PR body what was
excluded and why. A net-reduction PR states its added and deleted line counts
in its body.

A rename of a term or identifier with no behavior change stays in one PR,
with its docs, however many lines the mechanical replacement touches. Split
by layer (code, messages, docs), the names disagree between merges and each
part rebases over the others. Only its hand-written edits count toward the
caps; a rename whose hand-written edits exceed them still splits.

Work over a cap is split before implementation into sub-issues that each merge
on their own. When a change truly cannot be split, the PR body explains why,
and the maintainer approves the size before review starts. A PR that is over
the cap because of review rounds is a sign of bloat: remove code, do not add
more.

## Comments

### 19. Comments say why

A comment states what the code cannot: an external tool's behavior, a rejected
alternative, an invariant, a unit, or the bug a test catches. Keep those.

Change the code instead when a comment:

- explains what the code does (rename or extract);
- justifies a `cast`, or relies on a rule in another module (put the fact in
  an existing type or interface; do not add a Protocol or wrapper for it,
  principle 17);
- labels a section of a file (check principle 8: split only if the parts
  change independently; section comments inside a function are principle 1).

Delete a comment that restates the code.

The principle applies to code you write or touch. Do not sweep untouched files.
