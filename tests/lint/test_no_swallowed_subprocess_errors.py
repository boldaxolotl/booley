"""Architecture guard: external-tool errors must never be silently swallowed.

The "swallowed subprocess error" bug class — a tool fails and the report guesses
at the cause because stderr / the real return code was thrown away — shipped
twice (79a7749 asic_synthesize, 6c86f9d tooling). The structural fix is
``booley.core.run_command`` (always captures stdout+stderr+rc). This test stops the
mistake from creeping back in: every ``except subprocess.CalledProcessError``
handler in the source tree must SURFACE the failure detail, PROPAGATE it, or
re-raise — not discard it behind a bare "failed" message.

A handler passes if it does any of:
  * references the bound exception's ``.stderr`` / ``.stdout`` / ``.output``
    (surfaces the captured detail), or
  * references ``.returncode`` (propagates the real code, e.g.
    ``sys.exit(exc.returncode)``), or
  * contains a ``raise`` (re-raises / raises a richer error).

New swallow sites should adopt ``run_command`` instead. If a genuinely-benign
swallow is unavoidable, add it to ``_ALLOWLIST`` with a one-line reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

# (relative posix path under src/booley, enclosing function name) -> reason.
# Empty by design: every current CalledProcessError handler surfaces, propagates,
# or re-raises. Add an entry only for a genuinely-benign swallow that can't use
# run_command, with a one-line justification.
_ALLOWLIST: dict[tuple[str, str], str] = {}

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "booley"
_SURFACING_ATTRS = {"stderr", "stdout", "output", "returncode"}


def _is_called_process_error(node: ast.ExceptHandler) -> bool:
    """True if this handler catches CalledProcessError (bare or qualified)."""
    exc = node.type
    types = exc.elts if isinstance(exc, ast.Tuple) else [exc]
    for t in types:
        if isinstance(t, ast.Attribute) and t.attr == "CalledProcessError":
            return True
        if isinstance(t, ast.Name) and t.id == "CalledProcessError":
            return True
    return False


def _handler_surfaces_error(node: ast.ExceptHandler) -> bool:
    """True if the handler surfaces, propagates, or re-raises the error."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Raise):  # bare re-raise or richer raise
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in _SURFACING_ATTRS:
            return True
    return False


def _enclosing_func(tree: ast.Module, target: ast.ExceptHandler) -> str:
    """Name of the function lexically enclosing *target* ('<module>' if none)."""
    best = "<module>"
    for fn in ast.walk(tree):
        if isinstance(
            fn, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and fn.lineno <= target.lineno <= (fn.end_lineno or target.lineno):
            best = fn.name  # innermost wins (walk yields outer→inner by lineno span)
    return best


def _find_violations() -> list[str]:
    violations: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not _is_called_process_error(node):
                continue
            if _handler_surfaces_error(node):
                continue
            func = _enclosing_func(tree, node)
            if (rel, func) in _ALLOWLIST:
                continue
            violations.append(f"{rel}:{node.lineno} (in {func}())")
    return violations


def test_no_swallowed_subprocess_errors() -> None:
    violations = _find_violations()
    assert not violations, (
        "CalledProcessError handler(s) discard the failure detail — surface "
        "exc.stderr/.returncode, re-raise, or use booley.core.run_command "
        "(which always captures stderr):\n  " + "\n  ".join(violations)
    )


def test_allowlisted_sites_still_exist() -> None:
    """Keep the allowlist honest: a listed site that no longer swallows (or
    moved) should be removed so the entry can't mask a future real swallow."""
    live = {
        (rel, func)
        for rel, func in (
            (
                path.relative_to(_SRC_ROOT).as_posix(),
                _enclosing_func(ast.parse(path.read_text(encoding="utf-8")), node),
            )
            for path in _SRC_ROOT.rglob("*.py")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ExceptHandler)
            and _is_called_process_error(node)
            and not _handler_surfaces_error(node)
        )
    }
    stale = set(_ALLOWLIST) - live
    assert not stale, f"Allowlist entries no longer apply (remove them): {stale}"
