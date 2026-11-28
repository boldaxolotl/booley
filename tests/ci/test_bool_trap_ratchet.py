"""Hand-written bool traps in `src/booley` may only shrink (#5838)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

# Number of hand-written bool traps `src/booley` may hold outside `core/boundary.py`.
# Lower it in the same change that replaces a trap with a boundary helper; never raise
# it to add one.
BOOL_TRAP_CEILING = 17


def test_package_bool_traps_equal_the_ceiling() -> None:
    """A new trap anywhere in the package, or a stale ceiling, fails."""
    from bool_trap_scan import package_bool_traps, ratchet_violations

    sites = package_bool_traps(_ROOT / "src/booley")

    assert ratchet_violations(sites, BOOL_TRAP_CEILING) == []


@pytest.mark.parametrize(
    ("trap", "count"),
    [
        pytest.param("isinstance(v, bool) or not isinstance(v, int)", 1, id="bool-or-not-int"),
        pytest.param("isinstance(v, int) and not isinstance(v, bool)", 1, id="int-and-not-bool"),
        pytest.param("not isinstance(v, int | float) or isinstance(v, bool)", 1, id="union"),
        pytest.param("isinstance(v, bool) or not isinstance(v, (str, float))", 1, id="tuple"),
        pytest.param(
            "isinstance(v, int) and (not isinstance(v, bool) and v > 0)", 1, id="nested-chain"
        ),
        pytest.param(
            "isinstance(v, bool) or not isinstance(v, int)"
            " or isinstance(w, bool) or not isinstance(w, int)",
            2,
            id="two-values",
        ),
    ],
)
def test_new_bool_trap_fails_the_ratchet(trap: str, count: int) -> None:
    from bool_trap_scan import bool_traps, package_bool_traps, ratchet_violations

    added = bool_traps(f"ok = {trap}\n", "new.py")
    sites = package_bool_traps(_ROOT / "src/booley") + added
    violations = ratchet_violations(sites, BOOL_TRAP_CEILING)

    assert added == ["new.py:1"] * count
    assert len(violations) == 1
    assert "new.py:1" in violations[0]


def test_scan_skips_the_boundary_module(tmp_path: Path) -> None:
    """Also not traps: tests of two different values, and a type dispatch."""
    from bool_trap_scan import package_bool_traps

    trap = "ok = isinstance(v, bool) or not isinstance(v, int)\n"
    core = tmp_path / "core"
    core.mkdir()
    (core / "boundary.py").write_bytes(trap.encode())
    (core / "other.py").write_bytes(
        (
            "a = isinstance(v, bool) or not isinstance(w, int)\n"
            "b = (isinstance(v, bool) and a) or (isinstance(v, int) and b)\n" + trap
        ).encode()
    )

    assert package_bool_traps(tmp_path) == ["core/other.py:3"]


def test_ratchet_asks_to_lower_a_stale_ceiling() -> None:
    from bool_trap_scan import ratchet_violations

    violations = ratchet_violations([], 1)

    assert len(violations) == 1
    assert "lower BOOL_TRAP_CEILING to 0" in violations[0]
