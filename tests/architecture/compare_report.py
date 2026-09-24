"""Compare reproducible diagnostics for two exact Booley source trees."""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from import_graph import (
    DependencyGraphDiff,
    DependencyGraphSnapshot,
    compare_dependency_graphs,
    dependency_graph_snapshot,
)

_OBJECT_ID = re.compile(r"[0-9a-f]{40,64}\Z")
_SEMANTICS_FILES = (Path(__file__).with_name("import_graph.py"), Path(__file__))


@dataclass(frozen=True)
class _Source:
    identity: str
    root: Path
    digest: str


def main() -> int:
    """Run the comparison command and return its process status."""
    parser = _parser()
    args = parser.parse_args()
    try:
        mode = _mode(args, parser)
        with ExitStack() as stack:
            before, after = _sources(args, mode, stack)
            before_snapshot = dependency_graph_snapshot(before.root)
            after_snapshot = dependency_graph_snapshot(after.root)
            diff = compare_dependency_graphs(before_snapshot, after_snapshot)
            print(
                _render(
                    before,
                    after,
                    before_snapshot,
                    after_snapshot,
                    diff,
                    _analyzer_commit(),
                    _tree_digest(_SEMANTICS_FILES, common_root=Path(__file__).parent),
                )
            )
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(f"comparison failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-ref")
    parser.add_argument("--after-ref")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--before-root", type=Path)
    parser.add_argument("--after-root", type=Path)
    parser.add_argument("--before-label")
    parser.add_argument("--after-label")
    return parser


def _mode(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    refs = (args.before_ref, args.after_ref)
    roots = (args.before_root, args.after_root)
    labels = (args.before_label, args.after_label)
    if any(refs) and (any(roots) or any(labels)):
        parser.error("ref mode and root mode cannot be mixed")
    if any(refs):
        if not all(refs):
            parser.error("--before-ref and --after-ref are required together")
        return "ref"
    if not all(roots):
        parser.error("--before-root and --after-root are required together")
    if not all(labels):
        parser.error("root mode requires --before-label and --after-label")
    return "root"


def _sources(args: argparse.Namespace, mode: str, stack: ExitStack) -> tuple[_Source, _Source]:
    if mode == "root":
        return (
            _root_source(args.before_root, args.before_label),
            _root_source(args.after_root, args.after_label),
        )
    repo_root = args.repo_root.resolve()
    before_id = _resolve_ref(repo_root, args.before_ref)
    after_id = _resolve_ref(repo_root, args.after_ref)
    before_root = stack.enter_context(_archived_source(repo_root, before_id))
    after_root = stack.enter_context(_archived_source(repo_root, after_id))
    return (
        _Source(before_id, before_root, _source_digest(before_root)),
        _Source(after_id, after_root, _source_digest(after_root)),
    )


def _root_source(root: Path, label: str) -> _Source:
    resolved = root.resolve()
    return _Source(label, resolved, _source_digest(resolved))


def _resolve_ref(repo_root: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    object_id = result.stdout.strip()
    if result.returncode or not _OBJECT_ID.fullmatch(object_id):
        detail = result.stderr.strip() or "not a commit"
        raise ValueError(f"cannot resolve ref {ref!r}: {detail}")
    return object_id


@contextmanager
def _archived_source(repo_root: Path, object_id: str) -> Iterator[Path]:
    result = subprocess.run(
        ["git", "archive", "--format=tar", object_id, "--", "src/booley"],
        cwd=repo_root,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip() or "git archive failed"
        raise ValueError(f"cannot archive {object_id}: {detail}")
    with tempfile.TemporaryDirectory(prefix="booley-dependency-compare-") as temporary:
        destination = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
            members = archive.getmembers()
            for member in members:
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or (path.parts != ("src",) and path.parts[:2] != ("src", "booley"))
                    or member.issym()
                    or member.islnk()
                    or not (member.isdir() or member.isfile())
                ):
                    raise ValueError(f"unsafe archive member: {member.name}")
            archive.extractall(destination, members=members, filter="data")
        yield destination / "src" / "booley"


def _source_digest(root: Path) -> str:
    paths = tuple(sorted(root.rglob("*.py")))
    if not (root / "__init__.py").is_file():
        raise ValueError(f"source root is not a Python package: {root}")
    return _tree_digest(paths, common_root=root)


def _tree_digest(paths: tuple[Path, ...], *, common_root: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(common_root).as_posix().encode()
        raw = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return "sha256:" + digest.hexdigest()


def _analyzer_commit() -> str:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    commit = result.stdout.strip()
    return commit if not result.returncode and _OBJECT_ID.fullmatch(commit) else "unavailable"


def _render(
    before: _Source,
    after: _Source,
    before_snapshot: DependencyGraphSnapshot,
    after_snapshot: DependencyGraphSnapshot,
    diff: DependencyGraphDiff,
    analyzer_commit: str,
    analyzer_digest: str,
) -> str:
    lines = [
        f"Before source: {before.identity}",
        f"Before source digest: {before.digest}",
        f"After source: {after.identity}",
        f"After source digest: {after.digest}",
        f"Analyzer commit: {analyzer_commit}",
        f"Analyzer digest: {analyzer_digest}",
        "",
        _snapshot_line("Before", before_snapshot),
        _snapshot_line("After", after_snapshot),
    ]
    _edge_section(lines, "Added edges", diff.added_edges, " -> ")
    _edge_section(lines, "Removed edges", diff.removed_edges, " -> ")
    _edge_section(lines, "Added mutual pairs", diff.added_mutual_pairs, " <-> ")
    _edge_section(lines, "Removed mutual pairs", diff.removed_mutual_pairs, " <-> ")
    lines.extend(("", "SCC membership changes:"))
    if not diff.scc_transitions:
        lines.append("(none)")
    for item in diff.scc_transitions:
        lines.append(f"- {item.package}: {_membership(item.before)} -> {_membership(item.after)}")
    lines.extend(("", "Changed named-hotspot fan-out:"))
    if not diff.fan_out_transitions:
        lines.append("(none)")
    for item in diff.fan_out_transitions:
        lines.append(f"- {item.source}: {item.before} -> {item.after} ({item.delta:+d})")
    return "\n".join(lines)


def _snapshot_line(label: str, snapshot: DependencyGraphSnapshot) -> str:
    return (
        f"{label} snapshot: {snapshot.parsed_module_count} modules, "
        f"{snapshot.dependency_fact_count} facts, {len(snapshot.edges)} unique edges"
    )


def _edge_section(
    lines: list[str], title: str, edges: tuple[tuple[str, str], ...], separator: str
) -> None:
    lines.extend(("", f"{title}:"))
    lines.extend(f"- {source}{separator}{target}" for source, target in edges)
    if not edges:
        lines.append("(none)")


def _membership(members: tuple[str, ...] | None) -> str:
    return "absent" if members is None else "{" + ", ".join(members) + "}"


if __name__ == "__main__":
    raise SystemExit(main())
