"""Every Doctor probe stays reachable from ``run_doctor``.

``doctor.py`` wires its probes into orchestrators. A probe dropped from every
orchestrator keeps its own tests green while never running for a real user.
This test parses ``doctor.py`` with ``ast`` (no Doctor code runs) and fails
when a module-level ``_check_*`` probe or ``_run_*`` orchestrator is no longer
reachable from ``run_doctor`` through the module's call graph.
"""

from __future__ import annotations

import ast
from pathlib import Path

from booley.harness import doctor


def test_every_probe_and_orchestrator_is_reachable_from_run_doctor() -> None:
    """A dropped call leaves the probe, or a whole group of probes, unreachable."""
    tree = ast.parse(Path(doctor.__file__).read_text(encoding="utf-8"))
    funcs = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    graph = {
        name: {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name) and sub.id in funcs}
        for name, node in funcs.items()
    }
    reachable: set[str] = set()
    stack = ["run_doctor"]
    while stack:
        current = stack.pop()
        if current not in reachable:
            reachable.add(current)
            stack.extend(graph[current])

    probes = {name for name in funcs if name.startswith(("_check_", "_run_"))}
    assert not sorted(probes - reachable), (
        f"{sorted(probes - reachable)} no longer run for any user: wire each into an "
        "orchestrator or delete it"
    )
