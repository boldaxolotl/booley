"""Scope-relative ``-s`` globs: ``dut.*`` finds what ``bwave list`` prints under ``dut``.

``bwave list`` strips the store's common scope (``# scope: TOP.tb``) from the
names it prints, so users copy relative names such as ``dut.*`` into ``-s``.
These tests run the *built binary* (and the ``gui`` resolver on top of it) on
one store and pin that such a glob selects the same signals ``list`` shows,
that full-path globs and bare names keep their meaning, and that a total miss
names the anchored form.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from booley.bwave import cli
from tests.bwave.native_binary import require_native_binary as _native_bwave_binary

pytestmark = pytest.mark.native_bwave

SCOPE = "TOP.tb"

# Common scope TOP.tb. `dut` appears twice: under tb (the relative tree users
# copy from `list`) and under tb.other (a suffix match, never a relative one).
# The clock has rising edges at ticks 5, 15 and 25; every dut / other.dut
# signal takes value 1 by cycle 2 so `find PATTERN 1` reports each of them.
VCD = """\
$timescale 1ns $end
$scope module TOP $end
$scope module tb $end
$var wire 1 ! clk $end
$var wire 8 " count [7:0] $end
$var wire 8 % data_bus [7:0] $end
$scope module dut $end
$var wire 8 $ count [7:0] $end
$var wire 1 & valid $end
$scope module u_core $end
$var wire 4 ' state [3:0] $end
$upscope $end
$upscope $end
$scope module other $end
$scope module dut $end
$var wire 1 ( flag $end
$upscope $end
$upscope $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
0!
b0 "
b0 %
b0 $
0&
b0 '
0(
#5
1!
#10
0!
b1 $
1&
b1 "
b11 %
b1 '
1(
#15
1!
#20
0!
b10 $
0&
#25
1!
#30
0!
"""

# The signals `list` prints under `dut` (scope prefix TOP.tb. + relative name).
DUT_TREE = {
    "TOP.tb.dut.count[7:0]",
    "TOP.tb.dut.valid",
    "TOP.tb.dut.u_core.state[3:0]",
}
ALL_DUT_NAMED = DUT_TREE | {"TOP.tb.other.dut.flag"}


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_native_bwave_binary()), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One real store built from the fixture VCD."""
    directory = tmp_path_factory.mktemp("scope-relative")
    vcd = directory / "scope.vcd"
    vcd.write_text(VCD, encoding="utf-8")
    out = directory / "scope.fst"
    result = _run("build", str(vcd), "-o", str(out))
    assert result.returncode == 0, result.stderr
    return out


def _full(prefix: str, names: list[str]) -> set[str]:
    return {f"{prefix}.{name}" if prefix else name for name in names}


def _command_args(command: str, store: Path, glob: str) -> list[str]:
    """The command line that selects signals with ``-s glob`` (or ``glob`` itself)."""
    s = str(store)
    return {
        "list": ["list", s, "-s", glob, "--format", "json"],
        "value": ["value", s, "--at", "2", "-s", glob, "--format", "json"],
        "find": ["find", s, glob, "1", "--format", "json"],
        # Trigger on a bare name that matches on any build, so only -s differs.
        "sample": ["sample", s, "dut.valid", "1", "-s", glob],
        "stats": ["stats", s, "-s", glob, "--format", "json"],
    }[command]


def _names(command: str, result: subprocess.CompletedProcess[str]) -> set[str]:
    """Full signal names a successful ``command`` run selected."""
    if command == "sample":
        scope = next(
            line.removeprefix("# scope:").strip()
            for line in result.stderr.splitlines()
            if line.startswith("# scope:")
        )
        rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
        return _full(scope, [row[1] for row in rows])
    data = json.loads(result.stdout)["data"]
    if command == "stats":
        return {s["name"] for s in data["signals"]}
    key = "matches" if command == "find" else "signals"
    return _full(data["scope_prefix"], [entry["name"] for entry in data[key]])


def _selected(command: str, store: Path, glob: str) -> tuple[int, set[str]]:
    """Return (exit code, full names) one command selects for ``-s glob``."""
    result = _run(*_command_args(command, store, glob))
    return result.returncode, (_names(command, result) if result.returncode == 0 else set())


def _list_tree(store: Path) -> set[str]:
    """Oracle: what the unfiltered ``list`` prints under ``dut``."""
    result = _run("list", str(store), "--format", "json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)["data"]
    assert data["scope_prefix"] == SCOPE
    names = _full(data["scope_prefix"], [s["name"] for s in data["signals"]])
    return {name for name in names if name.startswith(f"{SCOPE}.dut.")}


@pytest.mark.parametrize("command", ["list", "value", "find", "sample", "stats"])
def test_relative_glob_selects_the_list_tree(store: Path, command: str) -> None:
    """AC1/AC3: ``dut.*`` is what ``list`` prints under ``dut`` and nothing else."""
    assert _list_tree(store) == DUT_TREE
    code, selected = _selected(command, store, "dut.*")
    assert code == 0
    assert selected == DUT_TREE  # excludes TOP.tb.other.dut.flag: anchored, not a suffix


def test_gui_resolves_relative_glob(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1/AC3: ``gui --signals dut.*`` resolves the same tree."""
    monkeypatch.setattr(cli, "native_bwave_binary", _native_bwave_binary)
    try:
        signals, _listing = cli._resolve_signal_globs(str(store), ["dut.*"], 100)
    except SystemExit as error:
        outcome: object = error.code
    else:
        outcome = {signal.bwave_name for signal in signals}
    assert outcome == DUT_TREE


@pytest.mark.parametrize("glob", ["u_core.*", "tb.*"])
def test_glob_total_miss_suggests_anchored_form(store: Path, glob: str) -> None:
    """AC5: a total miss names the anchored form; the exit code stays 2."""
    anchored = {"u_core.*": "TOP.tb.dut.u_core.*", "tb.*": "TOP.tb.*"}[glob]
    result = _run("value", str(store), "--at", "2", "-s", glob)
    assert result.returncode == 2
    assert f"did you mean '{anchored}'?" in result.stderr
    # The suggestion precedes the existing hint.
    assert result.stderr.index("did you mean") < result.stderr.index("signals in store")


def test_gui_total_miss_suggests_anchored_form(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC5: ``gui`` relays the suggestion in its own total-miss error."""
    monkeypatch.setattr(cli, "native_bwave_binary", _native_bwave_binary)
    with pytest.raises(SystemExit) as raised:
        cli._resolve_signal_globs(str(store), ["u_core.*"], 100)
    assert raised.value.code == 2
    assert "did you mean 'TOP.tb.dut.u_core.*'?" in capsys.readouterr().err


@pytest.mark.parametrize("glob", ["TOP.tb.dut.*", "*dut.*"])
def test_anchored_globs_select_as_before(store: Path, glob: str) -> None:
    """AC2: full-path and ``*``-led globs keep their meaning, in every command."""
    expected = DUT_TREE if glob == "TOP.tb.dut.*" else ALL_DUT_NAMED
    for command in ("list", "value", "sample", "stats"):
        assert _selected(command, store, glob) == (0, expected), command


def test_bare_name_suffix_match_unchanged(store: Path) -> None:
    """AC4: a bare name is a suffix match: ``bus`` finds ``data_bus``."""
    assert _selected("list", store, "bus") == (0, {"TOP.tb.data_bus[7:0]"})
    assert _selected("list", store, "count") == (
        0,
        {"TOP.tb.count[7:0]", "TOP.tb.dut.count[7:0]"},
    )
