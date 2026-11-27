"""Ratchet: only ``booley.core.file_hash`` hashes one file by path.

A function counts when it reads a file, hashes it with ``hashlib`` (directly or
through a same-module hasher) and returns the digest, looping over nothing but
that file's chunks.  Loops over several paths are fingerprints, not copies.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import booley

_SOURCE = Path(booley.__file__).parent
_HELPER = _SOURCE / "core" / "file_hash.py"
_LOOPS = (ast.For, ast.AsyncFor, ast.While, ast.comprehension)


def _name(node: ast.AST) -> str:
    return node.id if isinstance(node, ast.Name) else ""


def _calls(node: ast.AST) -> list[ast.Call]:
    return [child for child in ast.walk(node) if isinstance(child, ast.Call)]


def _hashes(call: ast.Call, hashers: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and _name(func.value) == "hashlib":
        return func.attr in {"sha256", "file_digest", "new"}
    return _name(func) in hashers


def _reads(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr in {"read_bytes", "open"}
    return _name(func) in {"open", "read_file_bytes"}


def _is_digest(node: ast.AST, tainted: set[str], hashers: set[str]) -> bool:
    """Whether *node* is a digest, or a name, prefix, f-string or tuple holding one."""
    if isinstance(node, ast.Call):
        func = node.func
        method = isinstance(func, ast.Attribute) and func.attr in {"hexdigest", "digest"}
        return method or _name(func) in hashers
    if isinstance(node, ast.Name):
        return node.id in tainted
    wrappers = (ast.BinOp, ast.JoinedStr, ast.FormattedValue, ast.Tuple)
    return isinstance(node, wrappers) and any(
        _is_digest(child, tainted, hashers) for child in ast.iter_child_nodes(node)
    )


def _returns_digest(function: ast.AST, hashers: set[str]) -> bool:
    nodes = list(ast.walk(function))
    tainted: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Assign) and _is_digest(node.value, tainted, hashers):
            tainted.update(_name(target) for target in node.targets)
    return any(
        isinstance(node, ast.Return)
        and node.value is not None
        and _is_digest(node.value, tainted, hashers)
        for node in nodes
    )


def _chunks_only(function: ast.AST) -> bool:
    loops = [node for node in ast.walk(function) if isinstance(node, _LOOPS)]
    return all(
        not isinstance(loop, ast.While)
        and isinstance(loop.iter, ast.Call)
        and _name(loop.iter.func) == "iter"
        for loop in loops
    )


def _file_hash_functions(source: str) -> list[str]:
    tree = ast.parse(source)
    functions = [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    hashers = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(_hashes(call, set()) for call in _calls(node))
        and _returns_digest(node, set())
    }
    return [
        f"{function.lineno} {function.name}"
        for function in functions
        if any(_hashes(call, hashers - {function.name}) for call in _calls(function))
        and any(_reads(call) for call in _calls(function))
        and _returns_digest(function, hashers - {function.name})
        and _chunks_only(function)
    ]


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("def f(p):\n    return hashlib.sha256(p.read_bytes()).hexdigest()\n", True),
        (
            "def f(p):\n    d = hashlib.sha256()\n    with p.open('rb') as s:\n"
            "        for c in iter(lambda: s.read(9), b''):\n            d.update(c)\n"
            "    return 'sha256:' + d.hexdigest()\n",
            True,
        ),
        ("def f(p):\n    r = hashlib.sha256(p.read_bytes()).hexdigest()\n    return r\n", True),
        (
            "def _d(b):\n    return hashlib.sha256(b).hexdigest()\n"
            "def f(p):\n    return _d(p.read_bytes())\n",
            True,
        ),
        (
            "def f(paths):\n    d = hashlib.sha256()\n    for p in paths:\n"
            "        d.update(p.read_bytes())\n    return d.hexdigest()\n",
            False,
        ),
        ("def f(content):\n    return hashlib.sha256(content).hexdigest()\n", False),
        (
            "def f(p):\n    raw = p.read_bytes()\n"
            "    return {'text': raw[:9], 'sha': hashlib.sha256(raw).hexdigest()}\n",
            False,
        ),
        (_HELPER, True),  # the scan sees the real thing, not only these snippets
    ],
    ids=["read", "chunks", "assigned", "local-hasher", "paths", "bytes", "excerpt", "helper"],
)
def test_scan_flags_a_file_hash_function_and_not_a_fingerprint(
    source: str | Path, flagged: bool
) -> None:
    if isinstance(source, Path):
        source = source.read_text(encoding="utf-8")

    assert bool(_file_hash_functions(source)) is flagged


def test_no_file_hash_function_outside_the_helper() -> None:
    offenders = [
        f"{path.relative_to(_SOURCE).as_posix()}:{hit}"
        for path in sorted(_SOURCE.rglob("*.py"))
        if path != _HELPER
        for hit in _file_hash_functions(path.read_text(encoding="utf-8"))
    ]
    # Ceiling zero: a new copy must call booley.core.file_hash instead.
    assert offenders == [], f"use booley.core.file_hash instead of: {offenders}"
