"""Architecture guard: tests must not patch the shared ``time`` module clock.

Replacing ``time.monotonic`` or ``time.time`` on the shared ``time`` module is
visible to every thread and every module in the xdist worker, so one test's
fake clock leaks into unrelated code (heartbeat threads, locks, deadlines).
A module that needs a fakeable clock owns the name instead
(``from time import monotonic`` or ``from time import time as wall_clock``)
and tests patch that module-owned name, or inject the clock as an argument.

Three forms are flagged, in any call to ``setattr``, ``patch`` or ``patch.object``:

  * form A: ``monkeypatch.setattr(mod.time, "monotonic", fake)``
    (any ``<expr>.time`` target is flagged, erring toward flagging);
  * form B: ``patch("pkg.mod.time.monotonic")`` or
    ``monkeypatch.setattr("pkg.mod.time.time", fake)`` (dotted string target);
  * form C: ``monkeypatch.setattr(time, "time", fake)``,
    ``patch.object(time, "monotonic")`` or ``patch("time.monotonic")``
    (direct patch of the ``time`` module, including ``import time as clock``).

Not covered, because no test or module uses them: keyword-argument spellings,
``patch.multiple``, and a module-level ``import time as _time`` patched as
``mod._time.monotonic``.

Replacing a module's own ``time`` binding (``setattr(mod, "time", ns)``) is
allowed: it does not touch the shared module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Anchored on this file: the lint conftest chdirs into ``tmp_path``.
_TESTS_ROOT = Path(__file__).resolve().parents[1]
_PATCH_CALLEES = {"setattr", "patch", "object"}
_CLOCK_FUNCTIONS = {"monotonic", "time"}


def _callee_name(call: ast.Call) -> str | None:
    """Return the called function's bare name, or ``None`` for other callees."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _time_module_names(tree: ast.Module) -> set[str]:
    """Names bound to the shared ``time`` module: ``time`` and ``import time as X``."""
    names = {"time"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(
                alias.asname for alias in node.names if alias.name == "time" and alias.asname
            )
    return names


def _is_clock_string_target(node: ast.expr) -> bool:
    """True for a dotted string like ``"pkg.mod.time.monotonic"`` or ``"time.time"``."""
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    parts = node.value.split(".")
    return len(parts) >= 2 and parts[-2] == "time" and parts[-1] in _CLOCK_FUNCTIONS


def _is_clock_attribute_target(call: ast.Call, time_names: set[str]) -> bool:
    """True for ``(<time module or X.time>, "monotonic"|"time")`` argument pairs."""
    if len(call.args) < 2:
        return False
    target, attribute = call.args[0], call.args[1]
    if not (isinstance(attribute, ast.Constant) and attribute.value in _CLOCK_FUNCTIONS):
        return False
    if isinstance(target, ast.Name):
        return target.id in time_names
    return isinstance(target, ast.Attribute) and target.attr == "time"


def _shared_clock_patches(source: str, path: str) -> list[str]:
    """Return ``"<path>:<line>"`` for each patch of the shared clock in *source*."""
    tree = ast.parse(source, filename=path)
    time_names = _time_module_names(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee_name(node) not in _PATCH_CALLEES:
            continue
        if (node.args and _is_clock_string_target(node.args[0])) or _is_clock_attribute_target(
            node, time_names
        ):
            hits.append(f"{path}:{node.lineno}")
    return hits


def test_tests_do_not_patch_shared_clock() -> None:
    """No test patches ``monotonic``/``time`` on the shared ``time`` module."""
    scanned: set[Path] = set()
    hits: list[str] = []
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        scanned.add(path.resolve())
        relative = path.relative_to(_TESTS_ROOT).as_posix()
        if relative.startswith("goals/"):
            continue  # CI does not run Goal Mode unit tests (#3329), so it cannot gate them
        hits.extend(_shared_clock_patches(path.read_text(encoding="utf-8"), f"tests/{relative}"))
    assert Path(__file__).resolve() in scanned, "scan did not discover the guard's own file"
    assert not hits, (
        "tests patch the shared time module clock; patch a name the module owns "
        "(`from time import monotonic`) or inject a clock instead:\n  " + "\n  ".join(hits)
    )


@pytest.mark.parametrize(
    "statement",
    [
        'monkeypatch.setattr(mod.time, "monotonic", f)',
        'patch("pkg.mod.time.time")',
        'monkeypatch.setattr(time, "time", f)',
        'patch.object(time, "monotonic")',
        'mock.patch("time.monotonic")',
        'monkeypatch.setattr("pkg.mod.time.monotonic", f)',
        'monkeypatch.setattr(clock, "time", f)',
    ],
)
def test_guard_flags_each_shared_clock_form(statement: str) -> None:
    """Every shared-clock spelling is reported with file and line."""
    source = f"import time as clock\n\n{statement}\n"
    assert _shared_clock_patches(source, "seed.py") == ["seed.py:3"]


def test_guard_accepts_module_owned_clock_patch() -> None:
    """Patching a name the module owns, or its own ``time`` binding, is allowed."""
    source = (
        'monkeypatch.setattr(mod, "monotonic", f)\n'
        'patch("pkg.mod.wall_clock")\n'
        'monkeypatch.setattr(mod, "time", ns)\n'
    )
    assert _shared_clock_patches(source, "seed.py") == []
