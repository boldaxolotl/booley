"""Freeze accepted RTL bytes and resolve only explicitly committed literal includes."""

import hashlib
import re
import subprocess
from pathlib import Path

INCLUDE = re.compile(r'(?m)^\s*`include\s+"([^"\r\n]+)"[^\r\n]*')
COMMENTS = re.compile(r"/\*.*?\*/|//[^\r\n]*", re.DOTALL)


def committed_bytes(root: Path, commit: str, item: dict) -> tuple[Path, bytes]:
    relative = Path(item["path"])
    path = (root / relative).resolve(strict=True)
    if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(root):
        raise ValueError("Candidate input must be contained and relative")
    content = path.read_bytes()
    committed = subprocess.run(
        ["git", "-C", str(root), "show", f"{commit}:{relative.as_posix()}"],
        check=True,
        capture_output=True,
        timeout=10,
    ).stdout
    if content != committed or hashlib.sha256(content).hexdigest() != item["sha256"]:
        raise ValueError(f"Candidate input differs from accepted committed digest: {relative}")
    return relative, content


def include_target(name: str, source: Path, files: dict) -> Path:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name:
        raise ValueError(f"Include must be a contained literal path: {name}")
    choices = {source.parent / relative, relative}.intersection(files)
    if len(choices) != 1:
        raise ValueError(f"Include must resolve to one explicitly hashed input: {name}")
    return choices.pop()


def snapshot(candidate: dict, evaluator: Path, destination: Path) -> list[Path]:
    """Compile an owned snapshot, never the subsequently mutable candidate checkout."""
    root = Path(candidate["root"]).resolve(strict=True)
    if root.is_relative_to(evaluator) or evaluator.is_relative_to(root):
        raise ValueError("Candidate Project and operator evaluator must be disjoint")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if head != candidate["commit"]:
        raise ValueError("Candidate checkout does not match accepted commit")
    sources = candidate["sources"]
    includes = candidate.get("include_files", [])
    if not sources:
        raise ValueError("Expected a nonempty explicit RTL source list")
    files = dict(committed_bytes(root, head, item) for item in sources + includes)
    if len(files) != len(sources + includes):
        raise ValueError("Duplicate candidate input path")
    for item in sources:
        if Path(item["path"]).suffix not in [".v", ".sv"]:
            raise ValueError("Compilation source must be .v or .sv")
    for path in files:
        if path.suffix not in [".v", ".sv", ".vh", ".svh"]:
            raise ValueError("Only explicit RTL and header files are supported")
    rewritten = {
        path: resolve_includes(path, content, files, destination)
        for path, content in files.items()
    }
    destination.mkdir(parents=True, exist_ok=False)
    for path, content in rewritten.items():
        output = destination / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
    return [destination / item["path"] for item in sources]


def resolve_includes(path: Path, content: bytes, files: dict, destination: Path) -> str:
    text = content.decode("utf-8")
    uncommented = COMMENTS.sub(lambda match: re.sub(r"[^\r\n]", " ", match[0]), text)
    matches = list(INCLUDE.finditer(uncommented))
    if uncommented.count("`include") != len(matches):
        raise ValueError("Macro or nonliteral includes need an explicit reproducible closure")
    for match in reversed(matches):
        target = include_target(match[1], path, files)
        directive = f'`include "{(destination / target).as_posix()}"'
        text = text[: match.start()] + directive + text[match.end() :]
    return text
