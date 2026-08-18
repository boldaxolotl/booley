#!/usr/bin/env python
"""
Lightweight AST-based mutation testing runner.
For each mutation: parses source, applies one change, writes mutated file,
runs pytest, restores original.

Usage:
    python run_mutmut.py                     # mutate all setup-stage modules
    python run_mutmut.py intake             # single file (substring match)
    python run_mutmut.py --results           # show cached results
    python run_mutmut.py --survivors         # list surviving mutants
    python run_mutmut.py --show <id>         # show a specific mutant diff
    python run_mutmut.py --clear-cache       # clear cache before running
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).parent.resolve()
STEPS_DIR = SCRIPTS_DIR / "harness" / "setup"
sys.path.insert(0, str(SCRIPTS_DIR))
from booley.runtime.platform_paths import venv_python

PYTHON = venv_python(SCRIPTS_DIR / "harness" / ".venv")
CACHE_FILE = SCRIPTS_DIR / ".mutmut-results-v2.json"

PYTEST_BASE = [
    str(PYTHON),
    "-m",
    "pytest",
    "--ignore=harness/tests/e2e",
    "-x",
    "-q",
    "--tb=no",
    "--no-header",
]

# Targeted test files per setup-stage module
STEP_TEST_MAP: dict[str, list[str]] = {
    "intake": ["tests/harness/test_setup_intake.py"],
    "workspace": ["tests/harness/test_setup_workspace.py"],
}

# ---------------------------------------------------------------------------
# Mutation operators
# ---------------------------------------------------------------------------

BINOP_SWAPS: dict[type, list[type]] = {
    ast.Add: [ast.Sub],
    ast.Sub: [ast.Add],
    ast.Mult: [ast.FloorDiv],
    ast.Div: [ast.Mult],
    ast.FloorDiv: [ast.Div],
    ast.Mod: [ast.FloorDiv],
    ast.Pow: [ast.Mult],
    ast.BitAnd: [ast.BitOr],
    ast.BitOr: [ast.BitAnd],
    ast.BitXor: [ast.BitAnd],
    ast.LShift: [ast.RShift],
    ast.RShift: [ast.LShift],
}


# Readable names for mutation descriptions (ASCII only for Windows compat)
def _op_name(cls: type) -> str:
    return cls.__name__


def _desc(orig: type, repl: type) -> str:
    return f"{_op_name(orig)}->{_op_name(repl)}"


CMPOP_SWAPS: dict[type, list[type]] = {
    ast.Eq: [ast.NotEq],
    ast.NotEq: [ast.Eq],
    ast.Lt: [ast.LtE],
    ast.LtE: [ast.Lt],
    ast.Gt: [ast.GtE],
    ast.GtE: [ast.Gt],
    ast.Is: [ast.IsNot],
    ast.IsNot: [ast.Is],
    ast.In: [ast.NotIn],
    ast.NotIn: [ast.In],
}

BOOLOP_SWAPS: dict[type, list[type]] = {
    ast.And: [ast.Or],
    ast.Or: [ast.And],
}


@dataclass
class Mutation:
    """One mutation = (file, name, mutated_source_code)."""

    file: Path
    name: str  # unique key, e.g. "intake:L42:binop:Add->Sub"
    line: int
    kind: str
    description: str
    mutated_code: str  # the full file source with this one mutation applied


# ---------------------------------------------------------------------------
# Mutation collection — one parse per mutation, no id() tricks
# ---------------------------------------------------------------------------


class _MutationCollector(ast.NodeVisitor):
    """Walk the AST once, for each mutable node: deep-copy tree, apply mutation,
    unparse, and store as a Mutation."""

    def __init__(self, filepath: Path, source: str, tree: ast.AST):
        self.filepath = filepath
        self.source = source
        self.tree = tree
        self.mutations: list[Mutation] = []
        # Assign a stable index to every node so we can find it in a copy
        self._index_map: dict[int, int] = {}
        self._all_nodes: list[ast.AST] = []
        for node in ast.walk(tree):
            idx = len(self._all_nodes)
            self._index_map[id(node)] = idx
            self._all_nodes.append(node)

    def _node_in_copy(self, copy_tree: ast.AST, orig_node: ast.AST) -> ast.AST | None:
        """Find the corresponding node in a deep-copied tree by walk-order index."""
        orig_idx = self._index_map.get(id(orig_node))
        if orig_idx is None:
            return None
        for i, node in enumerate(ast.walk(copy_tree)):
            if i == orig_idx:
                return node
        return None

    def _add(self, node: ast.AST, kind: str, desc: str, apply_fn):
        """Create a mutation: deep-copy tree, apply change, unparse."""
        # Skip lines marked with # pragma: no mutate
        if hasattr(node, "lineno"):
            line = (
                self.source.splitlines()[node.lineno - 1]
                if node.lineno <= len(self.source.splitlines())
                else ""
            )
            if "pragma: no mutate" in line:
                return
        tree_copy = ast.parse(self.source, filename=str(self.filepath))
        target = self._node_in_copy(tree_copy, node)
        if target is None:
            return
        apply_fn(target)
        ast.fix_missing_locations(tree_copy)
        try:
            mutated = ast.unparse(tree_copy)
        except (ValueError, TypeError, RecursionError):
            return
        name = f"{self.filepath.stem}:L{node.lineno}:{kind}:{desc}"
        # Deduplicate: same name means same mutation
        if any(m.name == name for m in self.mutations):
            return
        self.mutations.append(
            Mutation(
                file=self.filepath,
                name=name,
                line=node.lineno,
                kind=kind,
                description=desc,
                mutated_code=mutated,
            )
        )

    def visit_BinOp(self, node: ast.BinOp) -> None:
        for repl in BINOP_SWAPS.get(type(node.op), []):
            self._add(
                node,
                "binop",
                f"{type(node.op).__name__}->{repl.__name__}",
                lambda n, r=repl: setattr(n, "op", r()),
            )
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for i, op in enumerate(node.ops):
            for repl in CMPOP_SWAPS.get(type(op), []):

                def apply(n: ast.AST, idx: int = i, r: type = repl) -> None:
                    n.ops[idx] = r()

                self._add(node, "cmpop", f"{type(op).__name__}->{repl.__name__}", apply)
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        for repl in BOOLOP_SWAPS.get(type(node.op), []):
            self._add(
                node,
                "boolop",
                f"{type(node.op).__name__}->{repl.__name__}",
                lambda n, r=repl: setattr(n, "op", r()),
            )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        val = node.value
        if isinstance(val, bool):
            self._add(
                node, "const", f"{val}->{not val}", lambda n: setattr(n, "value", not n.value)
            )
        elif isinstance(val, int) and not isinstance(val, bool):
            nv = 1 if val == 0 else (2 if val == 1 else val + 1)
            self._add(node, "const", f"{val}->{nv}", lambda n, v=nv: setattr(n, "value", v))
        elif isinstance(val, float):
            nv = 1.0 if val == 0.0 else val + 1.0
            self._add(node, "const", f"{val}->{nv}", lambda n, v=nv: setattr(n, "value", v))
        elif isinstance(val, str) and val != "":
            safe_val = val[:20].encode("ascii", "replace").decode("ascii")
            self._add(
                node,
                "const_str",
                f'"{safe_val}"->mutated',
                lambda n, v=val: setattr(n, "value", f"XX{v}XX"),
            )
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self._add(
            node,
            "negate",
            "negate_if",
            lambda n: setattr(n, "test", ast.UnaryOp(op=ast.Not(), operand=n.test)),
        )
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None:
            self._add(
                node,
                "return",
                "return_None",
                lambda n: setattr(n, "value", ast.Constant(value=None)),
            )
        self.generic_visit(node)


def collect_mutations(filepath: Path) -> list[Mutation]:
    """Parse file and generate all single-point mutations."""
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as exc:
        print(f"  WARN: cannot parse {filepath.name} for mutations: {exc}")
        return []
    collector = _MutationCollector(filepath, source, tree)
    collector.visit(tree)
    return collector.mutations


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------


def find_target_files(pattern: str | None) -> list[Path]:
    files = sorted(f for f in STEPS_DIR.glob("*.py") if f.name != "__init__.py")
    if pattern:
        matches = [f for f in files if pattern in f.name]
        if not matches:
            print(f"No match for '{pattern}'")
            sys.exit(1)
        return matches
    return files


def load_results() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text())
    return {}


def save_results(results: dict) -> None:
    CACHE_FILE.write_text(json.dumps(results, indent=2))


def _test_paths_for_step(step_file: Path) -> list[str]:
    """Get targeted test files for a setup-stage module, or fall back to all tests."""
    stem = step_file.stem  # e.g. "intake"
    for key, value in STEP_TEST_MAP.items():
        if stem.startswith(key):
            return value
    return ["booley/tests/"]


def run_pytest(test_paths: list[str] | None = None, timeout: int = 60) -> int:
    cmd = list(PYTEST_BASE) + (test_paths or ["booley/tests/"])
    try:
        r = subprocess.run(
            cmd, capture_output=True, cwd=str(SCRIPTS_DIR), timeout=timeout, check=False
        )
        return r.returncode
    except subprocess.TimeoutExpired:
        return -1


def _test_one_mutant(mut: Mutation) -> str:
    """Apply mutation, run tests, restore original. Returns status string."""
    test_paths = _test_paths_for_step(mut.file)
    original_content = mut.file.read_text(encoding="utf-8")
    try:
        mut.file.write_text(mut.mutated_code, encoding="utf-8")
        rc = run_pytest(test_paths=test_paths, timeout=60)
        if rc == -1:
            return "timeout"
        return "killed" if rc != 0 else "survived"
    except (OSError, subprocess.SubprocessError):
        return "error"
    finally:
        try:
            mut.file.write_text(original_content, encoding="utf-8")
        except OSError:
            try:
                rel = mut.file.relative_to(SCRIPTS_DIR)
                subprocess.run(
                    ["git", "checkout", "--", str(rel)],
                    cwd=str(SCRIPTS_DIR),
                    timeout=10,
                    capture_output=True,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError, ValueError) as git_exc:
                print(f"  CRITICAL: failed to restore {mut.file.name} via git checkout: {git_exc}")


def _print_mutation_summary(
    files: list[Path], total: int, counters: dict[str, int], elapsed: float
) -> None:
    """Print final mutation testing results table."""
    killed = counters["killed"]
    survived = counters["survived"]
    timeouts = counters["timeout"]
    errors = counters["errors"]
    decided = killed + survived
    kill_rate = (killed / decided * 100) if decided > 0 else 0
    print(f"\n{'=' * 60}")
    print("  Mutation Testing Results")
    print(f"{'=' * 60}")
    print(f"  Files tested:  {len(files)}")
    print(f"  Total mutants: {total}")
    print(f"  Killed:        {killed}")
    print(f"  Survived:      {survived}")
    print(f"  Timeout:       {timeouts}")
    print(f"  Errors:        {errors}")
    print(f"  Kill rate:     {kill_rate:.1f}%")
    print(f"  Time:          {elapsed:.1f}s")
    print(f"{'=' * 60}")


def _collect_all_mutations(files: list[Path]) -> list[Mutation]:
    """Collect mutations from all target files, printing per-file counts."""
    print("Collecting mutations...")
    all_mutations: list[Mutation] = []
    for filepath in files:
        muts = collect_mutations(filepath)
        print(f"  {filepath.name}: {len(muts)} mutations")
        all_mutations.extend(muts)
    print(f"\nTotal mutations: {len(all_mutations)}")
    return all_mutations


def _tally_status(status: str, counters: dict[str, int]) -> None:
    """Increment the appropriate counter for a mutation result status."""
    if status in counters:
        counters[status] += 1
    else:
        counters["errors"] += 1


def _run_mutant_loop(
    all_mutations: list[Mutation],
    results: dict,
) -> dict[str, int]:
    """Execute each mutation, update *results* cache, return final counters."""
    counters = {"killed": 0, "survived": 0, "timeout": 0, "errors": 0}
    total = len(all_mutations)
    start_time = time.time()

    for i, mut in enumerate(all_mutations, 1):
        file_key = mut.file.name
        if file_key not in results:
            results[file_key] = {}

        if mut.name in results[file_key]:
            status = results[file_key][mut.name]
        else:
            status = _test_one_mutant(mut)
            results[file_key][mut.name] = status

        _tally_status(status, counters)
        _print_progress(i, total, counters, mut, start_time)
        if i % 10 == 0:
            save_results(results)

    print()
    save_results(results)
    return counters


def _print_progress(
    i: int,
    total: int,
    counters: dict[str, int],
    mut: Mutation,
    start_time: float,
) -> None:
    """Print single-line progress update for the current mutant."""
    elapsed = time.time() - start_time
    rate = i / elapsed if elapsed > 0 else 0
    display_name = mut.name[:65].encode("ascii", "replace").decode("ascii")
    k, s, t, e = counters["killed"], counters["survived"], counters["timeout"], counters["errors"]
    print(
        f"\r  [{i}/{total}] K:{k} S:{s} T:{t} E:{e} ({rate:.1f}/s) {display_name:65s}",
        end="",
        flush=True,
    )


def run_mutation_testing(files: list[Path]) -> None:
    results = load_results()
    all_mutations = _collect_all_mutations(files)

    print("\nVerifying clean tests pass...")
    rc = run_pytest()
    if rc != 0:
        print(f"ERROR: Clean tests fail (rc={rc}). Fix tests first.")
        return
    print("  OK\n")

    start_time = time.time()
    counters = _run_mutant_loop(all_mutations, results)
    elapsed = time.time() - start_time
    _print_mutation_summary(files, len(all_mutations), counters, elapsed)


def show_results() -> None:
    results = load_results()
    if not results:
        print("No results cached.")
        return

    total_k = total_s = total_t = total_e = 0
    for filename in sorted(results.keys()):
        mutants = results[filename]
        k = sum(1 for v in mutants.values() if v == "killed")
        s = sum(1 for v in mutants.values() if v == "survived")
        t = sum(1 for v in mutants.values() if v == "timeout")
        e = sum(1 for v in mutants.values() if v == "error")
        total_tested = k + s + t + e
        rate = (k / (k + s) * 100) if (k + s) > 0 else 0
        print(
            f"  {filename:40s}  {total_tested:3d} mutants  K:{k} S:{s} T:{t} E:{e}  ({rate:.1f}%)"
        )
        total_k += k
        total_s += s
        total_t += t
        total_e += e

    grand = total_k + total_s + total_t + total_e
    rate = (total_k / (total_k + total_s) * 100) if (total_k + total_s) > 0 else 0
    print(
        f"\n  {'TOTAL':40s}  {grand:3d} mutants  K:{total_k} S:{total_s} T:{total_t} E:{total_e}  ({rate:.1f}%)"
    )


def show_survivors() -> None:
    results = load_results()
    if not results:
        print("No results cached.")
        return
    idx = 0
    for filename in sorted(results.keys()):
        for name, status in sorted(results[filename].items()):
            if status == "survived":
                print(f"  [{idx}] {name}")
                idx += 1
    if idx == 0:
        print("No survivors!")


def show_mutant(mutant_id: str) -> None:
    results = load_results()
    survivors = []
    for filename in sorted(results.keys()):
        for name, status in sorted(results[filename].items()):
            if status == "survived":
                survivors.append((filename, name))

    try:
        idx = int(mutant_id)
        if 0 <= idx < len(survivors):
            filename, name = survivors[idx]
        else:
            print(f"Index {idx} out of range")
            return
    except ValueError:
        matches = [(f, n) for f, n in survivors if mutant_id in n]
        if not matches:
            print(f"No match for '{mutant_id}'")
            return
        filename, name = matches[0]

    print(f"  File:   {filename}")
    print(f"  Mutant: {name}")
    parts = name.split(":")
    if len(parts) >= 4:
        print(f"  Line:   {parts[1]}")
        print(f"  Kind:   {parts[2]}")
        print(f"  Change: {':'.join(parts[3:])}")

    # Show context from source
    filepath = STEPS_DIR / filename
    if filepath.exists():
        lines = filepath.read_text(encoding="utf-8").splitlines()
        line_no = int(parts[1][1:]) if len(parts) >= 2 else 0
        if line_no > 0:
            start = max(0, line_no - 3)
            end = min(len(lines), line_no + 2)
            print()
            for j in range(start, end):
                marker = ">>>" if j == line_no - 1 else "   "
                print(f"  {marker} {j + 1:4d} | {lines[j]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mutation testing for Booley")
    parser.add_argument(
        "target",
        nargs="?",
        help="file or directory to mutate (default: all pipeline Python files)",
    )
    parser.add_argument(
        "--results",
        action="store_true",
        help="show results summary from the last run",
    )
    parser.add_argument(
        "--survivors",
        action="store_true",
        help="list surviving mutants from the last run",
    )
    parser.add_argument(
        "--show",
        metavar="ID",
        help="display a specific mutant by ID with source context",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="delete the mutant cache before running",
    )

    args = parser.parse_args()

    if args.results:
        show_results()
        return
    if args.survivors:
        show_survivors()
        return
    if args.show is not None:
        show_mutant(args.show)
        return

    if args.clear_cache and CACHE_FILE.exists():
        CACHE_FILE.unlink()
        print("Cache cleared.")

    files = find_target_files(args.target)
    print(f"Mutation testing: {len(files)} file(s)")
    for f in files:
        print(f"  {f.name}")
    print()

    run_mutation_testing(files)


if __name__ == "__main__":
    main()
