"""List Python files whose only changes are terminology renames.

Two rules qualify a file for the changed-line coverage exclusion:

- ``terminology_only``: Sandbox terminology changed inside string constants.
- ``identifier_rename_only``: identifiers renamed from a retired term to its
  replacement (``_IDENTIFIER_RENAMES``) with the syntax otherwise unchanged.
"""

import argparse
import ast
import re
import subprocess
from collections import Counter
from pathlib import Path

_RENAMES = (
    ("session runtimes", "sandboxes"),
    ("session runtime", "sandbox"),
    ("session containers", "sandboxes"),
    ("session container", "sandbox"),
    ("runtime images", "sandbox images"),
    ("runtime image", "sandbox image"),
    ("runtime attachments", "sandbox attachments"),
    ("runtime attachment", "sandbox attachment"),
    ("runtime policy", "sandbox policy"),
    ("runtime", "sandbox"),
    ("session", "sandbox"),
)
_PATTERNS = tuple(
    (re.compile(r"(?<![\w./])" + re.escape(old) + r"(?![\w/])", re.IGNORECASE), new)
    for old, new in _RENAMES
)
_TERM = re.compile(r"session|runtime|sandbox", re.IGNORECASE)

# (retired term, replacement) pairs accepted in identifier renames. Add the pair
# of each later rename part here.
_IDENTIFIER_RENAMES = (("campaign", "record"),)
# AST fields holding a lexically scoped name: the old -> new mapping must be a
# bijection per file.
_LEXICAL_FIELDS = (
    (ast.Name, "id"),
    (ast.FunctionDef, "name"),
    (ast.AsyncFunctionDef, "name"),
    (ast.ClassDef, "name"),
    (ast.arg, "arg"),
    (ast.ExceptHandler, "name"),
    (ast.Global, "names"),
    (ast.Nonlocal, "names"),
    (ast.alias, "asname"),
)
# AST fields holding a member name: one spelling can belong to several types, so
# each pair is checked alone.
_MEMBER_FIELDS = ((ast.Attribute, "attr"), (ast.keyword, "arg"))
# An imported or exported name: (name, asname) for an alias, (string, None) for
# an ``__all__`` member.
_Entry = tuple[str, str | None]


def _canonical_text(value: str) -> str:
    value = value.casefold()
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def terminology_only(before: str, after: str) -> bool:
    """Accept only unchanged Python syntax with renamed terminology in strings."""
    try:
        old_tree, new_tree = ast.parse(before), ast.parse(after)
    except SyntaxError:
        return False
    old_strings = [
        n for n in ast.walk(old_tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    new_strings = [
        n for n in ast.walk(new_tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    if len(old_strings) != len(new_strings):
        return False
    for old, new in zip(old_strings, new_strings, strict=True):
        if old.value != new.value and not (_TERM.search(old.value) or _TERM.search(new.value)):
            return False
        old.value = _canonical_text(old.value)
        new.value = _canonical_text(new.value)
    return ast.dump(old_tree, include_attributes=False) == ast.dump(
        new_tree, include_attributes=False
    )


def _field_kind(node: ast.AST, field: str) -> str | None:
    """Classify an identifier field as ``lexical`` or ``member``, else None."""
    if any(isinstance(node, kind) and field == name for kind, name in _LEXICAL_FIELDS):
        return "lexical"
    if any(isinstance(node, kind) and field == name for kind, name in _MEMBER_FIELDS):
        return "member"
    return None


def _terminology_rename(old: str, new: str) -> bool:
    """Accept only a rename that drops a retired term and adds its replacement."""
    old_fold, new_fold = old.casefold(), new.casefold()
    return any(
        retired in old_fold and retired not in new_fold and replacement in new_fold
        for retired, replacement in _IDENTIFIER_RENAMES
    )


def _all_members(node: ast.AST) -> list[str] | None:
    """Return the string members of a module-level ``__all__`` list or tuple."""
    if not isinstance(node, ast.Assign | ast.AnnAssign):
        return None
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
        return None
    if not isinstance(node.value, ast.List | ast.Tuple):
        return None
    members = [e.value for e in node.value.elts if isinstance(e, ast.Constant)]
    if len(members) != len(node.value.elts) or not all(isinstance(m, str) for m in members):
        return None
    return [m for m in members if isinstance(m, str)]


class _Pairing:
    """Walk two ASTs in parallel, recording identifier renames between them."""

    def __init__(self) -> None:
        self.forward: dict[str, str] = {}
        self.reverse: dict[str, str] = {}
        self.unordered: list[tuple[list[_Entry], list[_Entry]]] = []
        self.matches = True

    def _identifier(self, kind: str, old: str, new: str) -> bool:
        if old == new and kind == "member":
            return True
        if old != new and not _terminology_rename(old, new):
            return False
        if kind == "lexical":
            # A split or merge breaks the bijection and may hide a behavior change.
            return (
                self.forward.setdefault(old, new) == new
                and self.reverse.setdefault(new, old) == old
            )
        return True

    def _identifiers(self, kind: str, old: object, new: object) -> bool:
        if isinstance(old, list) and isinstance(new, list):
            return len(old) == len(new) and all(
                self._identifier(kind, o, n) for o, n in zip(old, new, strict=True)
            )
        return isinstance(old, str) and isinstance(new, str) and self._identifier(kind, old, new)

    def _unordered(self, old: ast.AST, new: ast.AST) -> bool | None:
        """Record an import or ``__all__`` pair compared as multisets, else None.

        Ruff re-sorts both after a rename, so positional pairing cannot match them.
        """
        if isinstance(old, ast.ImportFrom) and isinstance(new, ast.ImportFrom):
            self.unordered.append(
                (
                    [(a.name, a.asname) for a in old.names],
                    [(a.name, a.asname) for a in new.names],
                )
            )
            same_source = (old.module, old.level) == (new.module, new.level)
            return same_source and len(old.names) == len(new.names)
        old_all, new_all = _all_members(old), _all_members(new)
        if old_all is not None and new_all is not None:
            self.unordered.append(([(m, None) for m in old_all], [(m, None) for m in new_all]))
            return True
        return None

    def walk(self, old: object, new: object) -> bool:
        """Return whether two nodes match up to accepted identifier renames."""
        if type(old) is not type(new):
            return False
        if isinstance(old, list) and isinstance(new, list):
            return len(old) == len(new) and all(
                self.walk(o, n) for o, n in zip(old, new, strict=True)
            )
        if not (isinstance(old, ast.AST) and isinstance(new, ast.AST)):
            return old == new
        unordered = self._unordered(old, new)
        if unordered is not None:
            return unordered
        return all(self._field(old, new, field) for field in old._fields)

    def _field(self, old: ast.AST, new: ast.AST, field: str) -> bool:
        """Compare one field: identifiers by rename rules, everything else exactly."""
        old_value, new_value = getattr(old, field, None), getattr(new, field, None)
        kind = _field_kind(old, field)
        if kind is None:
            return self.walk(old_value, new_value)
        if old_value is None and new_value is None:
            return True
        return self._identifiers(kind, old_value, new_value)


def _pairing(before: str, after: str) -> _Pairing | None:
    """Return the identifier pairing of two sources, or None when they differ."""
    try:
        old_tree, new_tree = ast.parse(before), ast.parse(after)
    except SyntaxError:
        return None
    pairing = _Pairing()
    return pairing if pairing.walk(old_tree, new_tree) else None


def identifier_rename_only(before: str, after: str, known: dict[str, str]) -> bool:
    """Accept unchanged syntax whose only differences are terminology renames.

    ``known`` maps names defined in other changed files, so an import or
    re-export of a renamed name resolves without a positional pair here.
    """
    pairing = _pairing(before, after)
    if pairing is None:
        return False

    def mapped(name: str | None) -> str | None:
        return None if name is None else pairing.forward.get(name) or known.get(name, name)

    for old_entries, new_entries in pairing.unordered:
        expected = Counter((mapped(name), mapped(alias)) for name, alias in old_entries)
        if expected != Counter(new_entries):
            return False
        for name, alias in old_entries:
            for old in (name, alias):
                new = mapped(old)
                if old and new and new != old and not _terminology_rename(old, new):
                    return False
    return True


def excluded_paths(changes: dict[str, tuple[str, str]]) -> list[str]:
    """Return the changed paths that qualify under either exclusion rule.

    ``changes`` maps each changed path to its ``(before, after)`` sources. Renames
    defined in one file resolve the re-exports in another.
    """
    known: dict[str, str] = {}
    conflicting: set[str] = set()
    for before, after in changes.values():
        pairing = _pairing(before, after)
        if pairing is None:
            continue
        for old, new in pairing.forward.items():
            if old != new and known.setdefault(old, new) != new:
                conflicting.add(old)
    for old in conflicting:
        del known[old]
    return [
        path
        for path, (before, after) in changes.items()
        if terminology_only(before, after) or identifier_rename_only(before, after, known)
    ]


def diff_cover_path(path: Path) -> str:
    """Use the exact absolute path matched by the pinned diff-cover version."""
    return str(path.resolve())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    paths = subprocess.check_output(
        ["git", "diff", "--name-only", f"{args.base}...HEAD", "--", "src/booley/"],
        text=True,
    ).splitlines()
    changes: dict[str, tuple[str, str]] = {}
    for name in paths:
        path = Path(name)
        if path.suffix != ".py" or not path.is_file():
            continue
        before = subprocess.run(
            ["git", "show", f"{args.base}:{name}"], capture_output=True, text=True, check=False
        )
        if before.returncode == 0:
            changes[name] = (before.stdout, path.read_text())
    for name in excluded_paths(changes):
        print(diff_cover_path(Path(name)))


if __name__ == "__main__":
    main()
