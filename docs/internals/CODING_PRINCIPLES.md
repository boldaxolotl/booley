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

### 8. One owner for each decision

Each rule, default, state transition, and data conversion has one authoritative
implementation. Other modules call it or derive projections from it. When two
places would have to stay synchronized, introduce a shared owner before adding
another copy.

### 9. Dependencies point toward policy

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
process locale: `10 AUG 2026`. Combined timestamps use the user's local time
as `HH:MM[:SS] · DD MMM YYYY`. Use `booley.runtime.timefmt`; do not hand-roll another
format string.

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
