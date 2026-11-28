"""Xcelium (xmsim) VCD dialect tests for B-wave, on a synthetic fixture.

The fixture (fixtures/test_xcelium_dialect.vcd, regenerable via
gen_xcelium_dialect_vcd.py) replicates the *format* of a real
`xmsim(64) 21.03-s001` runtime dump; the design content is invented.
The format was frozen from a real dump of an internal design on 2026-07-03,
where bwave was validated against VcdOracle with 0 mismatches over 6,902
transition counts and 48,314 async value probes — that dump is proprietary
and cannot be committed, so this fixture guards the dialect instead.

Xcelium dialect features exercised (absent from Icarus/Verilator fixtures):
  - `$var parameter` entries, including very wide string parameters (992 bit)
  - `$var integer` entries, with array-element names like `cnt_arr[0]`
  - bit-blasted wide nets: per-bit 1-bit vars named `wide_bus [1343]` (space
    before the index, 4-digit indices) instead of one ranged vector
  - `$timescale 1 ns` (space between magnitude and unit)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from simulator_ground_truth_test import (
    VcdOracle,
    _parse_scope_from_stderr,
    cache_transition_count,
    normalize_value,
    oracle_value_to_hex,
    parse_at_cycle_output,
    parse_list_signals_output,
    parse_stats_output,
)

BWAVE_SRC = THIS_DIR.parent
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
BWAVE_BIN = BWAVE_SRC / "target" / "debug" / f"bwave{EXE_SUFFIX}"

FIXTURE = THIS_DIR / "fixtures" / "test_xcelium_dialect.vcd"
# one bwave signal per $var: 8 tb-level + 3 dut-level + 1344 blasted bits
VAR_COUNT = 1355

pytestmark = pytest.mark.skipif(not BWAVE_BIN.exists(), reason="bwave not built")


def bwave(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [str(BWAVE_BIN), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, (
        f"bwave {' '.join(args)} failed rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


@pytest.fixture(scope="module")
def bwave_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    assert FIXTURE.exists(), f"fixture missing: {FIXTURE}"
    out = tmp_path_factory.mktemp("xcelium") / "dialect.fst"
    bwave(["build", str(FIXTURE), "-o", str(out)])
    assert out.stat().st_size > 0
    return out


@pytest.fixture(scope="module")
def oracle() -> VcdOracle:
    return VcdOracle(str(FIXTURE))


def test_header_dialect_features() -> None:
    """The fixture actually contains the Xcelium dialect constructs."""
    header = FIXTURE.read_text().split("$enddefinitions", 1)[0]
    assert "TOOL:\txmsim(64)" in header
    assert "$var parameter" in header
    assert "$var integer" in header
    assert "\n    1 ns\n" in header  # spaced timescale
    assert " wide_bus [1343] $end" in header  # bit-blast, 4-digit index
    assert " chunk_ofs [0] $end" in header  # single-element blast
    assert "cnt_arr[0]" in header  # integer array element


def test_list_preserves_every_var(bwave_cache: Path) -> None:
    """One signal per $var: params, integers, and bit-blasted bits all kept."""
    result = bwave(["list", str(bwave_cache), "-s", "*"])
    signals = parse_list_signals_output(result.stdout)
    assert len(signals) == VAR_COUNT
    assert "cnt_arr[0]" in signals  # $var integer element
    assert "COUNT" in signals  # $var parameter
    assert any(n.endswith("wide_bus[1343]") for n in signals)  # blasted bit
    assert any(n.endswith("chunk_ofs[0]") for n in signals)
    # widths survive the dialect
    assert signals["COUNT"] == 32
    wide_param = next(n for n in signals if n.startswith("vector_names"))
    assert signals[wide_param] == 992


def test_stats_match_oracle(bwave_cache: Path, oracle: VcdOracle) -> None:
    """Transition counts agree with the independent reference parser."""
    result = bwave(["stats", str(bwave_cache), "--async", "-s", "*"], timeout=300)
    stats = parse_stats_output(result.stdout)
    scope = _parse_scope_from_stderr(result.stderr)
    checked = 0
    for name, count in stats.items():
        full_name = scope + name if scope else name
        try:
            expected = cache_transition_count(oracle, full_name)
        except KeyError:
            # oracle collapses bit-blasted `name [N]` vars into one name
            continue
        assert count == expected, f"{name}: bwave={count} oracle={expected}"
        checked += 1
    assert checked >= 8, f"only {checked} signals cross-checked"


def test_async_values_match_oracle(bwave_cache: Path, oracle: VcdOracle) -> None:
    """Value snapshots at raw timestamps agree with the reference parser."""
    ts = oracle.timestamps()
    probes = [ts[1], ts[len(ts) // 2], ts[-2]]
    checked = 0
    for t in probes:
        result = bwave(["value", str(bwave_cache), "--async", "--at", f"{t}t"], timeout=300)
        values = parse_at_cycle_output(result.stdout)
        scope = _parse_scope_from_stderr(result.stderr)
        for name, val in values.items():
            full_name = scope + name if scope else name
            try:
                raw = oracle.value_at_time(full_name, t)
            except KeyError:
                continue
            width = oracle.signals().get(full_name, 1)
            expected = oracle_value_to_hex(raw, width)
            assert normalize_value(val) == normalize_value(expected), (
                f"t={t} {name}: bwave='{val}' oracle='{expected}'"
            )
            checked += 1
    assert checked >= 24, f"only {checked} values cross-checked"
