"""Host-side runtime Git captures go through the bounded helper (#3860).

``subprocess.run(..., timeout=...)`` kills only the direct child on timeout and, on
Windows, then waits for the output pipes without limit, so a Git helper process that
keeps them open hangs the caller. ``platform_paths.run_captured_bounded`` kills the
whole tree and gives up on the pipes.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

_RUNTIME_ROOT = Path(__file__).parents[2] / "src" / "booley" / "runtime"
# Keywords that take a call out of "text capture of Git output" (stdin or redirected streams).
_NON_CAPTURE_KEYWORDS = {"input", "stdin", "stdout", "stderr"}


def _is_git_list(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.List)
        and bool(node.elts)
        and isinstance(node.elts[0], ast.Constant)
        and node.elts[0].value == "git"
    )


def _function_builds_git_list(scope: ast.AST) -> bool:
    return any(_is_git_list(node) for node in ast.walk(scope))


def _is_git_argv(argv: ast.AST, scope: ast.AST) -> bool:
    """Whether *argv* is a Git command: a ``["git", ...]`` list, a ``*git*()`` call, or a
    name in a function that builds a ``["git", ...]`` list."""
    if _is_git_list(argv):
        return True
    if isinstance(argv, ast.Call):
        name = ast.unparse(argv.func).lower()
        return "git" in name
    return isinstance(argv, ast.Name) and _function_builds_git_list(scope)


def _keyword_is_true(call: ast.Call, name: str) -> bool:
    return any(
        keyword.arg == name
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )


def _calls_with_scope(node: ast.AST, scope: ast.AST) -> Iterator[tuple[ast.Call, ast.AST]]:
    """Every call under *node* with its outermost enclosing function (or the module)."""
    for child in ast.iter_child_nodes(node):
        child_scope = scope
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(
            scope, ast.Module
        ):
            child_scope = child
        if isinstance(child, ast.Call):
            yield child, child_scope
        yield from _calls_with_scope(child, child_scope)


def _is_text_mode(call: ast.Call) -> bool:
    """Whether the call decodes output: ``text``/``universal_newlines`` or an explicit codec."""
    keywords = {keyword.arg for keyword in call.keywords}
    return (
        _keyword_is_true(call, "text")
        or _keyword_is_true(call, "universal_newlines")
        or bool(keywords & {"encoding", "errors"})
    )


def _unbounded_git_captures(source: str) -> list[int]:
    """Line numbers of timeout-bound, text-captured ``subprocess.run`` Git calls.

    Scope: ``subprocess.run`` with ``capture_output=True`` (``check_output`` and
    ``Popen`` pipes are not matched; none exists in the runtime today).
    """
    tree = ast.parse(source)
    lines: list[int] = []
    for call, scope in _calls_with_scope(tree, tree):
        if ast.unparse(call.func) != "subprocess.run":
            continue
        keywords = {keyword.arg for keyword in call.keywords}
        if (
            "timeout" in keywords
            and _keyword_is_true(call, "capture_output")
            and _is_text_mode(call)
            and not keywords & _NON_CAPTURE_KEYWORDS
            and call.args
            and _is_git_argv(call.args[0], scope)
        ):
            lines.append(call.lineno)
    return sorted(lines)


def test_host_runtime_git_captures_use_the_bounded_helper() -> None:
    flagged = {
        "list": 'subprocess.run(["git", "status"], capture_output=True, text=True, timeout=5)',
        "call": "subprocess.run(_git_command(x), capture_output=True, text=True, timeout=5)",
        "name": (
            "def f(wt):\n"
            "    cmd = ['git', 'status']\n"
            "    return subprocess.run(cmd, capture_output=True, text=True, timeout=5)\n"
        ),
        "encoding": (
            'subprocess.run(["git", "x"], capture_output=True, encoding="utf-8", timeout=5)'
        ),
    }
    for shape, source in flagged.items():
        assert _unbounded_git_captures(source), f"detector missed the {shape} argv shape"
    ignored = {
        "stdin": (
            'subprocess.run(["git", "x"], capture_output=True, text=True, timeout=5, input="")'
        ),
        "no timeout": 'subprocess.run(["git", "x"], capture_output=True, text=True)',
        "bytes": 'subprocess.run(["git", "x"], capture_output=True, timeout=5)',
        "not git": 'subprocess.run(["docker", "x"], capture_output=True, text=True, timeout=5)',
    }
    for shape, source in ignored.items():
        assert not _unbounded_git_captures(source), f"detector flagged the {shape} shape"

    offenders = [
        f"{path.relative_to(_RUNTIME_ROOT).as_posix()}:{line}"
        for path in sorted(_RUNTIME_ROOT.rglob("*.py"))
        if not path.name.startswith("incontainer_")  # runs only inside the Linux Sandbox
        for line in _unbounded_git_captures(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"use platform_paths.run_captured_bounded for timed Git captures: {offenders}"
    )
