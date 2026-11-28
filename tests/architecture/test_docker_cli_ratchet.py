"""Ratchet on Docker CLI plumbing outside ``booley.core.docker_cli``.

Three patterns are counted, and each count may only shrink: argument lists that
spell ``inspect``, stderr-else-stdout failure details in modules that run Docker,
and typed copies of the label names the shared module owns.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from booley.core import docker_cli

_SOURCE_ROOT = Path(__file__).parents[2] / "src" / "booley"
# The shared module, and the idle reaper, which is stdlib-only by design.
_OWNERS = frozenset({"core/docker_cli.py", "docker/reaper.py"})
_LABELS = tuple(value for name, value in vars(docker_cli).items() if name.endswith("_LABEL"))

CEILINGS = {"inspect": 3, "fallback": 7, "label": 0}


def _is_text(node: ast.AST, text: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == text


def _runs_docker(tree: ast.Module) -> bool:
    """Whether the module names the ``docker`` executable or imports the shared module."""
    for node in ast.walk(tree):
        if _is_text(node, "docker"):
            return True
        if isinstance(node, ast.ImportFrom):
            names = {f"{node.module}.{alias.name}" for alias in node.names}
            if node.module == "booley.core.docker_cli" or "booley.core.docker_cli" in names:
                return True
        if isinstance(node, ast.Import) and any(
            alias.name == "booley.core.docker_cli" for alias in node.names
        ):
            return True
    return False


def _reads(node: ast.AST, attribute: str) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == attribute for n in ast.walk(node))


def _is_fallback(node: ast.AST) -> bool:
    """An ``or`` chain that reads ``.stderr`` first and ``.stdout`` after it."""
    if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
        return False
    return any(
        _reads(value, "stderr") and any(_reads(v, "stdout") for v in node.values[index + 1 :])
        for index, value in enumerate(node.values)
    )


def _is_label(node: ast.AST) -> bool:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    text = node.value.removeprefix("--").removeprefix("label=")
    return any(
        text == label or text.startswith(f"{label}=") or f'"{label}"' in text for label in _LABELS
    )


def _sites(source: str) -> Iterator[tuple[str, int]]:
    """Yield ``(pattern, line)`` for each counted site in one module."""
    tree = ast.parse(source)
    runs_docker = _runs_docker(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            if any(_is_text(element, "inspect") for element in node.elts):
                yield "inspect", node.lineno
        elif runs_docker and _is_fallback(node):
            yield "fallback", getattr(node, "lineno", 0)
        elif _is_label(node):
            yield "label", getattr(node, "lineno", 0)


@pytest.mark.parametrize("pattern", list(CEILINGS))
def test_docker_cli_plumbing_stays_at_its_ceiling(pattern: str) -> None:
    sites: list[str] = []
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(_SOURCE_ROOT).as_posix()
        if relative not in _OWNERS:
            found = _sites(path.read_text(encoding="utf-8"))
            sites.extend(f"{relative}:{line}" for kind, line in found if kind == pattern)

    ceiling = CEILINGS[pattern]
    assert len(sites) <= ceiling, (
        f"{len(sites)} {pattern} sites, ceiling {ceiling}. "
        "Use booley.core.docker_cli instead:\n" + "\n".join(sites)
    )
    assert len(sites) == ceiling, f"lower CEILINGS[{pattern!r}] to {len(sites)}"


_IMPORT = "from booley.core import docker_cli\n"


@pytest.mark.parametrize(
    ("source", "pattern", "count"),
    [
        ('["image", "inspect", n]\n', "inspect", 1),
        ('MODE = "inspect"\n', "inspect", 0),
        ('shutil.which("docker")\nd = r.stderr or r.stdout\n', "fallback", 1),
        (_IMPORT + "d = r.stderr.strip() or r.stdout.strip()\n", "fallback", 1),
        ("d = r.stderr or r.stdout\n", "fallback", 0),
        (_IMPORT + 'd = (r.stderr or "").strip() or "git failed"\n', "fallback", 0),
        ('x = "booley.role"\n', "label", 1),
        ('x = f"booley.project={n}"\n', "label", 1),
        ('["--filter", "label=booley.project-id=p"]\n', "label", 1),
        ('["--label=booley.project=p"]\n', "label", 1),
        ("x = '{{index .Config.Labels \"booley.role\"}}'\n", "label", 1),
        ('x = "booley.projects.cli"\n', "label", 0),
    ],
    ids=[
        "inspect-argv",
        "inspect-word",
        "docker-word",
        "docker-import",
        "no-docker",
        "no-stdout",
        "label",
        "label-fstring",
        "label-filter",
        "label-flag",
        "label-template",
        "other-name",
    ],
)
def test_detector_counts_docker_cli_plumbing(source: str, pattern: str, count: int) -> None:
    assert sum(kind == pattern for kind, _line in _sites(source)) == count
