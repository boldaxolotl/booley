#!/usr/bin/env python3
"""Classify the doc changes of a pull request: premerge, postreview, or none.

Usage: doc_change_class.py --base <rev> --head <rev> [--rules <file>] [--label-pr <n>]

The rules live in ``doc_change_rules.toml``; see
``docs/internals/agents/doc-change-classes.md``. Prints one JSON object on
stdout. Exit 2 names a malformed rules file or a Git failure; ``--label-pr``
exits 1 when a ``gh`` call fails. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

CLASSES = ("none", "postreview", "premerge")  # ascending rank
CHECKS = ("changed", "page-set", "headings", "cli-entries", "config-keys")
LABELS = {"premerge": "docs-premerge", "postreview": "docs-postreview"}
_STATUSES = {"A": "added", "D": "deleted", "M": "modified", "T": "modified", "R": "renamed"}
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*(\S*)")
_SPAN = re.compile(r"`([^`\n]+)`")
_WORD = re.compile(r"[a-z][a-z0-9_-]*")
_FLAG = re.compile(r"(?<![\w-])--?[A-Za-z][\w-]*")
_ATX = re.compile(r"^ {0,3}(#{1,2})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_TOML_TABLE = re.compile(r"^\s*(\[\[?[^\]]+\]\]?)\s*$")
_TOML_KEY = re.compile(r'^\s*#?\s*("[^"]+"|[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\s*=')
_CONFIG_SPAN = re.compile(r"^\[\[?[\w.-]+\]\]?(\.[\w-]+)?$")


class ClassifierError(Exception):
    """A malformed rules file or a failed Git call."""


@dataclass(frozen=True)
class Kind:
    name: str
    cls: str
    check: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class Rules:
    doc_paths: tuple[str, ...]
    default_class: str
    kinds: tuple[Kind, ...]


@dataclass(frozen=True)
class Change:
    path: str
    status: str
    old_path: str | None = None


def _strings(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    items: tuple[object, ...] = tuple(value)  # pyright: ignore[reportUnknownArgumentType]
    if not all(isinstance(item, str) and item for item in items):
        return None
    return tuple(str(item) for item in items)


def load_rules(path: Path) -> Rules:
    """Parse and validate the rules file; raise ClassifierError naming the problem."""

    def bad(message: str) -> ClassifierError:
        return ClassifierError(f"{path.name}: {message}")

    try:
        data: dict[str, object] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise bad(f"cannot read rules: {error}") from error
    if set(data) != {"doc_paths", "default_class", "kind"}:
        raise bad("top-level keys must be exactly doc_paths, default_class, kind")
    doc_paths = _strings(data["doc_paths"])
    if doc_paths is None:
        raise bad("doc_paths must be a non-empty list of non-empty strings")
    names = ", ".join(CLASSES[::-1])
    default_class = data["default_class"]
    if default_class not in CLASSES:
        raise bad(f"default_class {default_class!r} is not one of {names}")
    raw_kinds = data["kind"]
    if not isinstance(raw_kinds, list) or not raw_kinds:
        raise bad("kind must be a non-empty array of tables")
    kinds: list[Kind] = []
    for raw in raw_kinds:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(raw, dict) or set(raw) != {"name", "class", "check", "paths"}:  # pyright: ignore[reportUnknownArgumentType]
            raise bad("each kind needs exactly name, class, check, paths")
        item: dict[str, object] = dict(raw)  # pyright: ignore[reportUnknownArgumentType]
        name = item["name"]
        if not isinstance(name, str) or not name or name in {k.name for k in kinds}:
            raise bad(f"kind name {name!r} must be a unique non-empty string")
        if item["class"] not in CLASSES:
            raise bad(f"kind {name!r}: class {item['class']!r} is not one of {names}")
        if item["check"] not in CHECKS:
            raise bad(f"kind {name!r}: check {item['check']!r} is not one of {', '.join(CHECKS)}")
        paths = _strings(item["paths"])
        if paths is None:
            raise bad(f"kind {name!r}: paths must be a non-empty list of non-empty strings")
        kinds.append(Kind(name, str(item["class"]), str(item["check"]), paths))
    return Rules(doc_paths, str(default_class), tuple(kinds))


def git(*args: str) -> str:
    try:
        result = subprocess.run(["git", *args], capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ClassifierError(f"git {args[0]} failed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ClassifierError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.decode("utf-8", errors="replace")


def changes(base: str, head: str) -> tuple[list[Change], str]:
    """Return the changes since the merge base and the merge base itself."""
    merge_base = git("merge-base", base, head).strip()
    raw = git("diff", "--name-status", "-z", "-M", "--no-ext-diff", f"{base}...{head}")
    fields = raw.split("\0")
    found: list[Change] = []
    i = 0
    while i < len(fields) and fields[i]:
        code = fields[i]
        status = _STATUSES.get(code[0])
        if status is None:
            raise ClassifierError(f"unexpected git status {code!r}")
        if status == "renamed":
            found.append(Change(fields[i + 2], status, fields[i + 1]))
            i += 3
        else:
            found.append(Change(fields[i + 1], status))
            i += 2
    return found, merge_base


def _blob(rev: str, path: str) -> str:
    try:
        return git("show", f"{rev}:{path}")
    except ClassifierError:
        return ""


def _blocks(text: str) -> list[tuple[str | None, str]]:
    """Return (fence language or None, line) for every line outside front matter."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((n for n, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        lines = lines[end + 1 :] if end is not None else lines
    out: list[tuple[str | None, str]] = []
    fence: str | None = None
    lang = ""
    for line in lines:
        match = _FENCE.match(line)
        if fence is None and match:
            fence, lang = match.group(1), match.group(2).lower()
        elif (
            fence is not None
            and match
            and match.group(1)[0] == fence[0]
            and len(match.group(1)) >= len(fence)
            and not match.group(2)
        ):
            fence = None
        else:
            out.append((lang if fence is not None else None, line))
    return out


def headings(text: str) -> Counter[tuple[int, str]]:
    found: Counter[tuple[int, str]] = Counter()
    paragraph: list[str] = []
    for lang, line in _blocks(text):
        if lang is not None:
            continue
        atx = _ATX.match(line)
        stripped = line.strip()
        if atx:
            found[len(atx.group(1)), atx.group(2).strip()] += 1
            paragraph = []
        elif not stripped:
            paragraph = []
        elif paragraph and set(stripped) in ({"="}, {"-"}):
            if all("|" not in p and p[0] not in "#>-*+" for p in paragraph):
                found[1 if stripped[0] == "=" else 2, " ".join(paragraph)] += 1
            paragraph = []
        else:
            paragraph.append(stripped)
    return found


def _cli_entries(text: str) -> set[str]:
    entries: set[str] = set()
    snippets = [(lang is not None, line) for lang, line in _blocks(text) if lang is not None]
    snippets += [
        (False, span)
        for lang, line in _blocks(text)
        if lang is None
        for span in _SPAN.findall(line)
    ]
    for fenced, snippet in snippets:
        tokens = snippet.split()
        if not tokens or (fenced and tokens[0].startswith("#")):
            continue
        if not fenced and len(tokens) == 1 and _WORD.fullmatch(tokens[0]):
            entries.add(tokens[0])
        if tokens[0] != "booley":
            entries.update(_FLAG.findall(snippet))
            continue
        words = ["booley"]
        rest = tokens[1:]
        while rest and _WORD.fullmatch(rest[0]):
            words.append(rest.pop(0))
        path = " ".join(words)
        entries.add(path)
        for token in rest:
            if token == "--" or token.startswith("#"):
                break
            entries.update(f"{path} {flag}" for flag in _FLAG.findall(token))
    return entries


def _config_entries(text: str) -> set[str]:
    entries: set[str] = set()
    table = ""
    for lang, line in _blocks(text):
        if lang is None:
            for span in _SPAN.findall(line):
                if _CONFIG_SPAN.match(span) or _WORD.fullmatch(span):
                    entries.add(span)
        elif lang == "toml":
            header = _TOML_TABLE.match(line)
            key = _TOML_KEY.match(line)
            if header:
                table = header.group(1)
                entries.add(table)
            elif key:
                name = key.group(1).strip('"')
                entries.add(f"{table}.{name}")
    return entries


def _check(check: str, change: Change, base_rev: str, head_rev: str) -> bool:
    if check == "changed":
        return True
    if check == "page-set":
        return change.status in {"added", "deleted", "renamed"}
    old = "" if change.status == "added" else _blob(base_rev, change.old_path or change.path)
    new = "" if change.status == "deleted" else _blob(head_rev, change.path)
    if check == "headings":
        return headings(old) != headings(new)
    entries = _cli_entries if check == "cli-entries" else _config_entries
    return bool(entries(new) - entries(old))


def _matches(patterns: tuple[str, ...], change: Change, *, every_path: bool = False) -> bool:
    names = [change.path, *([change.old_path] if change.old_path else [])]
    hits = [any(fnmatchcase(name, pattern) for pattern in patterns) for name in names]
    return all(hits) if every_path else any(hits)


def classify(rules: Rules, base: str, head: str) -> dict[str, object]:
    found, merge_base = changes(base, head)
    files: list[dict[str, str]] = []
    for change in found:
        if not _matches(rules.doc_paths, change):
            continue
        hits = [
            kind
            for kind in rules.kinds
            if _matches(kind.paths, change, every_path=kind.cls == "none")
            and _check(kind.check, change, merge_base, head)
        ]
        cls = max([k.cls for k in hits] or [rules.default_class], key=CLASSES.index)
        rule = next((k.name for k in hits if k.cls == cls), "default")
        entry = {"path": change.path, "status": change.status}
        if change.old_path:
            entry["old_path"] = change.old_path
        files.append(entry | {"class": cls, "rule": rule})
    pr_class = max([f["class"] for f in files] or ["none"], key=CLASSES.index)
    return {"class": pr_class, "files": files}


def _gh(*args: str) -> str:
    try:
        result = subprocess.run(
            [shutil.which("gh") or "gh", "api", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"gh api {' '.join(args)} failed: {error}") from error
    if result.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def label_pr(number: int, pr_class: str) -> None:
    """Make the PR carry exactly the label for pr_class, calling gh only to change it."""
    base = f"repos/{{owner}}/{{repo}}/issues/{number}/labels"
    present = set(_gh("--paginate", base, "--jq", ".[].name").split())
    wanted = LABELS.get(pr_class)
    if wanted and wanted not in present:
        _gh("--method", "POST", base, "-f", f"labels[]={wanted}")
    for label in LABELS.values():
        if label != wanted and label in present:
            _gh("--method", "DELETE", f"{base}/{label}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument(
        "--rules", type=Path, default=Path(__file__).with_name("doc_change_rules.toml")
    )
    parser.add_argument("--label-pr", type=int)
    args = parser.parse_args(argv)
    try:
        result = classify(load_rules(args.rules), args.base, args.head)
    except ClassifierError as error:
        print(f"doc_change_class: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    if args.label_pr is not None:
        try:
            label_pr(args.label_pr, str(result["class"]))
        except RuntimeError as error:
            print(f"doc_change_class: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
