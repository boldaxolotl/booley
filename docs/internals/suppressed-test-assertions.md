# Suppressed test assertions

Run the same static check locally and in CI's lint job:

```sh
python3 .github/scripts/check_suppressed_assertions.py
```

The checker parses every `tests/**/*.py` file, including fixtures and helpers,
without importing it. It prints the file count and sorted `path:line:column`
diagnostics. Violations, unreadable files, parse failures, an empty test tree,
and invalid or missing exemption data fail the command. CI runs it with Python
3.13, so tests must remain parseable on that interpreter.

## Rule

Keep contract assertions outside `contextlib.suppress`, even when it suppresses
only a narrow exception. An earlier exception can skip an assertion without
raising `AssertionError`. The checker rejects:

- `assert` inside suppression, including nested control flow and class bodies;
- assertion calls whose terminal name starts with `assert_`, `_assert_`,
  `check_`, or `_check_`, standard `assert` + uppercase unittest methods,
  plus calls named `fail` (including mock assertions). Subprocess
  `check_call`, `check_output`, and `check_returncode` are error checks, not
  assertion helpers;
- suppression of `Exception`, `BaseException`, `AssertionError`, or unittest
  `failureException` attributes (including `pytest.fail.Exception`), even with
  no visible assertion, because it can hide failures in arbitrary helpers;
- contextlib star imports, computed/unresolved suppression arguments, and suppression
  construction outside a direct `with` item, including `ExitStack.enter_context`
  and saving a context manager for later use. References passed through containers,
  factories, tuple unpacking, walrus expressions, or attribute assignments also
  fail; only direct calls and simple name aliases are supported.

Qualified imports, renamed imports, simple name assignment aliases, enclosing
imports that appear later in source, multiple context managers, and async
function bodies are recognized. Scope-wide possible bindings are conservative:
reassigning an imported alias may still produce a finding. Function and lambda
bodies are deferred scopes; their defaults and decorators execute in the current
scope. Class bodies execute immediately.

For expected exception behavior, require the exception with a specific match
and put the contract assertion after the block:

```python
with pytest.raises(RuntimeError, match="expected failure"):
    operation()
assert observed_result == expected_result
```

For best-effort cleanup, suppress only expected cleanup errors and place any
required assertions after the cleanup block. Moving an assertion alone does
not prove which operation raised or prevent checking stale state.

## Narrow exceptions

The manifest `.github/scripts/suppressed_assertion_exemptions.json` starts empty.
It lives under `.github/scripts/`, so changing only exemptions activates lint.
An exemption must describe intentional assertion input to a test of assertion
handling or assertion skipping. Ordinary contract assertions and broad cleanup
are not eligible. Prefer source-string checker fixtures rather than exemptions.

Each entry targets exactly one diagnostic with these fields:

```json
{
  "path": "tests/example.py",
  "line": 12,
  "column": 9,
  "scope": "TestHandler.test_assertion_input",
  "rule": "suppressed-assertion",
  "purpose": "intentional-assertion-input",
  "reason": "Describe why this assertion is deliberately test input.",
  "verification": "Identify the assertion outside suppression that checks the outcome."
}
```

Only `suppressed-assertion`, `suppressed-assertion-call`, and `broad-suppression`
can be exempted; intentional assertion handling may require separate entries
for both the broad suppression call and its assertion. Duplicate, stale,
malformed, or wildcard entries fail. A reviewer must verify the justification
and the independent outside assertion; the checker validates exact matching
and metadata, not the semantic truth of those statements.

## Static-analysis boundary and regression evidence

This is a deterministic lexical guard, not an interprocedural proof. Arbitrary
helpers under narrow suppression can still skip internal assertions. Dynamic
imports, reflective lookups, comprehension binding subtleties, and
`global`/`nonlocal` rebinding are outside
the supported name-resolution contract. Use explicit imports and direct
suppression for test code; review unsupported dynamic constructions manually.
Broad `try/except` suppression and assertions after a raising operation inside
`pytest.raises` are related hazards outside this contextlib-specific check.

`tests/ci/test_suppressed_assertions.py` executes reproductions that return green
with swallowed contradictory assertions or an assertion skipped by an earlier
exception, then proves the checker rejects them. The supplied report did not
identify the repaired tests or commits, and suppression-related main history
provided no recoverable pair. These fixtures are behavioral reproductions;
they are not represented as copies of the unidentified original tests.
