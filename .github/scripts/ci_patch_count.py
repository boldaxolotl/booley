#!/usr/bin/env python3
"""Fail a pull request that adds patches of Booley names or commands to tests.

Coding principle 14. The patches in the ``.py`` files under ``tests/`` that the
pull request changes are counted at the merge base of ``--base`` and ``--head``
and at ``--head``. The check fails when the Booley-name patches (internal public
plus internal private) rise, or when the command patches rise.

* Patch: a call to a ``setattr`` method (``monkeypatch.setattr``), ``patch``
  or ``patch.object`` (also through ``mock.``) whose replaced name is a string
  literal: the second argument, or the last dotted component of a string
  first argument.  The owner is everything before that name.
* External patch: the owner is, or is reached through, a standard-library or
  third-party module or class (``subprocess.run``, ``doctor.subprocess.run``,
  ``Path.home``, ``sys.argv``), or the replaced name is itself an imported
  standard-library module (``doctor.shutil``).
* Internal patch: not external, and ``src/booley/`` defines the replaced name
  as a function, class, method, assignment target or attribute.  Public when
  the name has no leading underscore, private otherwise.
* Command patch: an external patch of ``subprocess`` (the module or its
  ``run``, ``Popen``, ``check_output``, ``check_call``, ``call``), of
  ``os.exec*``, ``os.fork``, ``os.posix_spawn*``, ``os.system``, or of
  ``shutil.which``; or an internal patch of ``run_captured_bounded``, which
  is both a public internal patch and a command patch.

A patch with a ``# boundary: <reason>`` comment, whose reason is not empty, on
any of the call's lines does not count. A pull request that carries the
``patch-count-exempt`` label passes.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tarfile
import tokenize
from collections import Counter
from dataclasses import dataclass

from ci_test_ratio import FileDiff, parse_diff

EXEMPT_LABEL = "patch-count-exempt"
LIMITS = {
    "Booley-name": frozenset({"internal-public", "internal-private"}),
    "command": frozenset({"command"}),
}
STDLIB = set(sys.stdlib_module_names)
SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "check_call", "call"}
OS_PROCESS_FUNCS = {"fork", "system", "posix_spawn", "posix_spawnp"}
_BOUNDARY = re.compile(r"# boundary:(.*)")
_FIX = (
    "Drive the public entry point, use the shared fake for the boundary "
    "(docs/internals/TEST_FAKES.md), or mark a real boundary patch with "
    "'# boundary: <reason>' on its line. A maintainer may add the "
    f"{EXEMPT_LABEL} label and re-run this job."
)


@dataclass
class SourceIndex:
    """Names and modules that ``src/booley/`` defines at one commit."""

    names: set[str]
    runners: set[str]
    modules: set[str]


@dataclass(frozen=True)
class Patch:
    """One counted patch call in a test file."""

    path: str
    line: int
    end_line: int
    target: str
    classes: frozenset[str]


def _git(repo: str, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=repo,
        check=True,
        timeout=120,
        capture_output=True,
    ).stdout


def index_source(repo: str, rev: str) -> SourceIndex:
    names: set[str] = set()
    runners: set[str] = {"run_captured_bounded"}
    modules: set[str] = set()
    data = _git(repo, "archive", rev, "src/booley")
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            if not member.name.endswith(".py"):
                continue
            dotted = member.name[len("src/") : -len(".py")].replace("/", ".")
            modules.add(dotted.removesuffix(".__init__"))
            extracted = archive.extractfile(member)
            if extracted is not None:
                names.update(_defined_names(ast.parse(extracted.read(), filename=member.name)))
    return SourceIndex(names, runners, modules)


def _defined_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for leaf in ast.walk(target):
                    if isinstance(leaf, ast.Name):
                        names.add(leaf.id)
                    elif isinstance(leaf, ast.Attribute):
                        names.add(leaf.attr)
    return names


def _chain(expr: ast.AST) -> list[str] | None:
    """Flatten ``a.b.c`` into its components; None for any other expression."""
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    parts.append(expr.id)
    return parts[::-1]


def _imports(tree: ast.AST) -> dict[str, str]:
    """Map each imported local name to the module it comes from."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                bound[local] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound[alias.asname or alias.name] = node.module
    return bound


def _patch_target(call: ast.Call) -> tuple[list[str] | None, str] | None:
    """Return (owner components, replaced name) for a patch call, else None."""
    func = call.func
    text = ast.unparse(func)
    is_setattr = isinstance(func, ast.Attribute) and func.attr == "setattr"
    is_patch_object = text in {"patch.object", "mock.patch.object"}
    is_patch = text in {"patch", "mock.patch"}
    if not (is_setattr or is_patch_object or is_patch):
        return None
    args = call.args
    if (
        (is_setattr or is_patch_object)
        and len(args) >= 2
        and isinstance(name := args[1], ast.Constant)
        and isinstance(name.value, str)
        and not (is_setattr and len(args) == 2 and _is_str(args[0]))
    ):
        return _chain(args[0]), name.value
    if args and _is_str(args[0]) and (is_patch or (is_setattr and len(args) == 2)):
        owner, _, name = args[0].value.rpartition(".")
        return (["<str>", *owner.split(".")] if owner else ["<str>"]), name
    return None


def _is_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _owner_tail(
    owner: list[str] | None, imports: dict[str, str], index: SourceIndex
) -> tuple[bool, list[str]] | None:
    """Whether the owner is external, and its attribute chain; None for a ``tests`` string."""
    if owner and owner[0] == "<str>":
        path = owner[1:]
        if path and path[0] == "booley":
            # Strip the longest Booley module prefix; the rest is an attribute chain.
            cut = 0
            for size in range(len(path), 0, -1):
                if ".".join(path[:size]) in index.modules:
                    cut = size
                    break
            return False, path[cut:]
        if path and path[0] == "tests":
            return None
        return True, path
    if owner:
        origin = imports.get(owner[0], "")
        if origin and origin.split(".")[0] not in {"booley", "tests"}:
            return True, owner
        return False, owner[1:]
    return False, []


def classify(
    owner: list[str] | None, name: str, imports: dict[str, str], index: SourceIndex
) -> set[str]:
    """Return the classes a patch falls in: internal-public, internal-private,
    command, external, other."""
    resolved = _owner_tail(owner, imports, index)
    if resolved is None:
        return {"other"}
    external, tail = resolved
    if any(part in STDLIB for part in tail):
        external = True
    if name in STDLIB and name not in index.names:
        external = True  # a module alias such as doctor.shutil

    if external:
        last = tail[-1] if tail else ""
        return {"external", "command"} if _is_command(name, last) else {"external"}
    if name in index.names:
        classes = {"internal-private" if name.startswith("_") else "internal-public"}
        if name in index.runners:
            classes.add("command")
        return classes
    return {"other"}


def _is_command(name: str, last: str) -> bool:
    """Whether the external patch of *name*, on an owner ending in *last*, is a command."""
    return (
        name == "subprocess"
        or (last == "subprocess" and name in SUBPROCESS_FUNCS)
        or (last == "os" and (name.startswith("exec") or name in OS_PROCESS_FUNCS))
        or (last == "shutil" and name == "which")
    )


def _boundary_lines(data: bytes) -> set[int]:
    """Lines with a ``# boundary:`` comment that gives a reason."""
    lines: set[int] = set()
    for token in tokenize.tokenize(io.BytesIO(data).readline):
        if token.type == tokenize.COMMENT:
            marker = _BOUNDARY.search(token.string)
            if marker and marker.group(1).strip():
                lines.add(token.start[0])
    return lines


def _file_patches(path: str, data: bytes, index: SourceIndex) -> list[Patch]:
    tree = ast.parse(data, filename=path)
    imports = _imports(tree)
    boundaries = _boundary_lines(data)
    patches: list[Patch] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _patch_target(node)
        end = node.end_lineno or node.lineno
        if target is None or boundaries.intersection(range(node.lineno, end + 1)):
            continue
        owner, name = target
        shown = ".".join(owner[1:] if owner and owner[0] == "<str>" else owner or ["?"])
        classes = frozenset(classify(owner, name, imports, index))
        patches.append(Patch(path, node.lineno, end, f"{shown}.{name}", classes))
    return patches


def count_patches(repo: str, rev: str, paths: list[str]) -> list[Patch]:
    """Return the patches without a boundary marker in *paths* at *rev*."""
    index = index_source(repo, rev)
    return [
        patch
        for path in paths
        for patch in _file_patches(path, _git(repo, "show", f"{rev}:{path}"), index)
    ]


def _changed_tests(repo: str, base: str, head: str) -> list[FileDiff]:
    """Diffs of the ``.py`` files under ``tests/``; a move is a delete and an add."""
    options = ("-U0", "--no-renames", "--no-color", "--no-ext-diff")
    diff = _git(repo, "diff", *options, base, head, "--", "tests/")
    return [
        entry
        for entry in parse_diff(diff.decode("utf-8", errors="replace"))
        if any(path.endswith(".py") for path in (entry.old_path, entry.new_path))
    ]


def _added(base: list[Patch], head: list[Patch], added_lines: dict[str, set[int]]) -> list[Patch]:
    """The head patches to report as new, preferring those on lines the diff adds."""
    before = Counter((p.path, p.target, p.classes) for p in base)
    by_key: dict[tuple[str, str, frozenset[str]], list[Patch]] = {}
    for patch in head:
        by_key.setdefault((patch.path, patch.target, patch.classes), []).append(patch)
    new: list[Patch] = []
    for key, patches in by_key.items():
        lines = added_lines.get(key[0], set())
        patches.sort(key=lambda p: (not lines.intersection(range(p.line, p.end_line + 1)), p.line))
        new.extend(patches[: max(len(patches) - before[key], 0)])
    return sorted(new, key=lambda p: (p.path, p.line))


def _of(patches: list[Patch], group: str) -> list[Patch]:
    return [patch for patch in patches if patch.classes & LIMITS[group]]


def verdict(
    base: list[Patch], head: list[Patch], added_lines: dict[str, set[int]]
) -> tuple[int, str]:
    """Return the exit code and the message for the patches of both sides."""
    failures: list[str] = []
    for group in LIMITS:
        before, after = _of(base, group), _of(head, group)
        if len(after) <= len(before):
            continue
        failures.append(
            f"{group} patches rose from {len(before)} to {len(after)} (coding principle 14). "
            "New in this pull request:"
        )
        failures += [f"  {p.path}:{p.line} {p.target}" for p in _added(before, after, added_lines)]
    if not failures:
        return 0, ""
    return 1, "\n".join([*failures, _FIX])


def _count_pull_request(repo: str, base: str, head: str) -> tuple[int, str]:
    """Print the totals of the pull request and return its verdict."""
    merge_base = _git(repo, "merge-base", base, head).decode("utf-8").strip()
    entries = _changed_tests(repo, merge_base, head)
    old = [e.old_path for e in entries if e.old_path != "/dev/null"]
    new = [e.new_path for e in entries if e.new_path != "/dev/null"]
    before = count_patches(repo, merge_base, old)
    after = count_patches(repo, head, new)
    totals = ", ".join(
        f"{group} patches {len(_of(before, group))} -> {len(_of(after, group))}"
        for group in LIMITS
    )
    print(f"patch-count: {totals} in {len(entries)} changed test files.")
    return verdict(before, after, {e.new_path: set(e.added) for e in entries})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--labels", default="", help="newline-separated label names")
    args = parser.parse_args(argv)
    if EXEMPT_LABEL in args.labels.splitlines():
        print(f"exempt by the {EXEMPT_LABEL} label")
        return 0
    try:
        code, message = _count_pull_request(args.repo, args.base, args.head)
    except subprocess.CalledProcessError as error:
        print(f"error: {error.stderr.decode('utf-8', errors='replace')}", file=sys.stderr)
        return 2
    except SyntaxError as error:
        print(f"error: {error.filename}:{error.lineno}: {error.msg}", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
