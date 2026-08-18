# Booley Coding Principles

Concise engineering discipline rules for all Python code in Booley
(Booley's own Python codebase, not the user's RTL; see the
[README](../README.md) for what Booley is). These are language-agnostic
principles applied to Python: they govern design and correctness, not
formatting (Ruff, the Python linter/formatter, handles that).

**Audience:** this document is written mainly for agents. Agents should apply
these principles to improve the codebase, both when writing new code and when
reviewing or refactoring existing code. Human contributors follow the same
principles when working on Booley itself.

Sources: NASA Power of Ten (Holzmann), SOLID (Martin), A Philosophy of
Software Design (Ousterhout).

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

### 8. Single Responsibility

Every module/class has one reason to change. If describing what a module
does requires "and," it does too much.

### 9. Depend on abstractions, not concretions

High-level development code should not import low-level implementation
details. Pass behavior in (callbacks, protocols, configuration), don't
reach down.

### 10. Design it twice

Before implementing a non-trivial component, consider at least two
approaches. You don't have to build both, but if you can't articulate why
your approach is better than an alternative, you haven't thought enough.

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
