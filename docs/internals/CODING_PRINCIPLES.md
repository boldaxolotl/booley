# Booley Coding Principles

Concise engineering discipline rules for all Python code in Booley
(Booley's own Python codebase, not the user's RTL; see the
[README](../../README.md) for what Booley is). These are language-agnostic
principles applied to Python: they govern design and correctness, not
formatting (Ruff, the Python linter/formatter, handles that).

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
`2026-08-10T09:11:49Z`. Parsers remain liberal enough to read legacy offsets
and fractional seconds, but new persisted values use the canonical form.

Human-visible dates use uppercase English three-letter months regardless of
process locale: `10 AUG 2026`. Use `booley.runtime.timefmt`; do not hand-roll
another format string. Persisted diagnostic log prefixes and their mirrored Console
prefixes instead use canonical UTC RFC 3339 `Z` timestamps so events can be
correlated across files.

### Time and duration display

Dashboard and CLI presentation uses `format_clock`, `format_duration`, and
`format_age` from `booley.runtime.timefmt`. Absolute clocks use the user's local
timezone: same day `HH:MM:SS`, another day in the same year `Oct 03 14:44`,
another year `YYYY-MM-DD HH:MM`. Compare local calendar dates; clock months
are fixed English title case, while date-only months stay uppercase.

Choose duration buckets from the original input. Nonpositive values display
`0s`; every positive subsecond value uses one decimal, including `0.0s`,
`0.4s`, and rounded `1.0s` (`0.999` seconds). Exactly one second displays `1s`.
Floor whole seconds thereafter: `59s`, `1m 00s`, `59m 59s`, `1h 00m`,
`23h 59m`, `1d 00h`. Relative ages are `just now` below five seconds, then
`5s ago`, `1m ago`, `1h ago`, `1d ago` with the leading unit floored.
Future ages clamp to `just now`; invalid or naive instants and invalid
durations display `unknown`. Keep meaningful absent-field markers.

This policy owns Dashboard/CLI presentation. Existing document/report text,
JSON, MCP diagnostic representations, redirected progress records (including
elapsed tokens), and UTC diagnostic prefixes retain their representations.
Printed report excerpts remain exact copies of the stored report.

## Verification

### 14. Tests prove behavior

Every bug fix includes a regression test that fails without the fix. New
behavior is tested at the closest stable boundary, including meaningful error
cases and edge cases. Prefer tests of observable contracts over tests coupled
to implementation details.

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

Work over a cap is split before implementation into sub-issues that each merge
on their own. When a change truly cannot be split, the PR body explains why,
and the maintainer approves the size before review starts. A PR that is over
the cap because of review rounds is a sign of bloat: remove code, do not add
more.
