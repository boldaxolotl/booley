"""Unit tests for the cocotb run-half (booley.sim.cocotb_run — G1, G5).

In the pattern of test_iverilog_run.py: pure run-half logic with no real
simulator — the filter builder's escaping property (G1), golden argv/env for
icarus and verilator with ``cocotb-config`` stubbed (G5), the explicit
results-file path, the D3 stale-image error, and the arg round-trip. The real
vvp/Vtop runs are exercised end-to-end in the Sandbox e2e.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.sim import cocotb_run as crun
from booley.sim.sim_result import SIM_INFRA_ERROR_PREFIX

# ---------------------------------------------------------------------------
# G1 — filter builder
# ---------------------------------------------------------------------------


class TestBuildCocotbTestFilter:
    def test_plain_names(self):
        # Fully-qualified anchoring (spike S4): cocotb matches the filter
        # against "<module>.<test>", so bare-name anchors match zero tests.
        assert crun.build_cocotb_test_filter("test_counter", ["a", "b"]) == (
            r"^test_counter\.(a|b)$"
        )

    def test_single_test(self):
        assert crun.build_cocotb_test_filter("m", ["only"]) == r"^m\.(only)$"

    def test_full_set(self):
        names = ["test_reset", "test_count", "test_overflow"]
        pattern = re.compile(crun.build_cocotb_test_filter("mod", names))
        for name in names:
            assert pattern.match(f"mod.{name}")

    @pytest.mark.parametrize(
        "tricky",
        ["t.est", "te+st", "te[a]st", "t(es)t", "te|st"],
    )
    def test_metacharacter_names_match_only_themselves(self, tricky: str):
        # A test named with regex metacharacters must not widen the match.
        pattern = re.compile(crun.build_cocotb_test_filter("mod", [tricky, "plain"]))
        assert pattern.match(f"mod.{tricky}")
        assert pattern.match("mod.plain")
        assert not pattern.match("mod.tXest")  # '.' escaped, not a wildcard
        assert not pattern.match(f"other.{tricky}")  # module anchored

    def test_module_with_metacharacters_is_escaped(self):
        pattern = re.compile(crun.build_cocotb_test_filter("pkg.mod", ["t"]))
        assert pattern.match("pkg.mod.t")
        assert not pattern.match("pkgXmod.t")

    def test_property_non_selected_names_never_match(self):
        """G1 property: for every selected subset, unselected names never match."""
        names = [
            "test_reset",
            "test.count",
            "a+b",
            "x[0]",
            "t(1)",
            "p|q",
            "^anchor$",
            "back\\slash",
            "star*",
            "quest?",
        ]
        module = "test_mod"
        for i in range(1, len(names)):
            selected, rest = names[:i], names[i:]
            pattern = re.compile(crun.build_cocotb_test_filter(module, selected))
            for name in selected:
                assert pattern.match(f"{module}.{name}"), (selected, name)
            for name in rest:
                assert not pattern.match(f"{module}.{name}"), (selected, name)


# ---------------------------------------------------------------------------
# G5 — golden argv / env (cocotb-config stubbed)
# ---------------------------------------------------------------------------

_CONFIG_ANSWERS = {
    ("--version",): "2.0.1",
    ("--libpython",): "/usr/lib/libpython3.13.so",
    ("--python-bin",): "/usr/bin/python3",
    ("--lib-dir",): "/site/cocotb/libs",
    ("--lib-name", "vpi", "icarus"): "cocotbvpi_icarus",
}


def _stub_cocotb_config(arg_sets):
    return [_CONFIG_ANSWERS[tuple(args)] for args in arg_sets]


def _stub_cocotb_config_v1(arg_sets):
    """A cocotb-1.9.2 sandbox (project image pinning a legacy stack)."""
    return [
        "1.9.2" if tuple(args) == ("--version",) else _CONFIG_ANSWERS[tuple(args)]
        for args in arg_sets
    ]


class TestBuildCocotbEnv:
    def test_env_golden(self, tmp_path: Path):
        with patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config):
            env = crun._build_cocotb_env(
                tmp_path,
                "test_counter",
                ["a", "b"],
                tmp_path / "results.xml",
            )
        assert env["COCOTB_TEST_MODULES"] == "test_counter"
        assert env["MODULE"] == "test_counter"  # cocotb <2.0 compat
        assert env["COCOTB_TEST_FILTER"] == r"^test_counter\.(a|b)$"
        assert env["LIBPYTHON_LOC"] == "/usr/lib/libpython3.13.so"
        assert env["PYGPI_PYTHON_BIN"] == "/usr/bin/python3"
        # C1: the results path is explicit — never guessed.
        assert env["COCOTB_RESULTS_FILE"] == str(tmp_path / "results.xml")
        # Spike S1: the build dir is pinned on PYTHONPATH so a project
        # run_cwd cannot break the module import.
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path)

    def test_2x_dialect_sets_no_testcase(self, tmp_path: Path):
        # cocotb 2.x removed TESTCASE — the 2.x dialect must never set it.
        with patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config):
            env = crun._build_cocotb_env(
                tmp_path,
                "test_counter",
                ["a", "b"],
                tmp_path / "results.xml",
            )
        assert "TESTCASE" not in env

    def test_cocotb1_uses_testcase_dialect(self, tmp_path: Path):
        # A project image pinning cocotb 1.x (legacy TestFactory TBs) selects
        # via TESTCASE — 1.x silently ignores COCOTB_TEST_FILTER.
        with patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config_v1):
            env = crun._build_cocotb_env(
                tmp_path,
                "test_counter",
                ["run_test_001", "run_test_002"],
                tmp_path / "results.xml",
            )
        assert env["TESTCASE"] == "run_test_001,run_test_002"
        assert "COCOTB_TEST_FILTER" not in env
        assert env["MODULE"] == "test_counter"  # 1.x module selector

    def test_version_probe_failure_defaults_to_2x(self, tmp_path: Path):
        # An old cocotb-config without --version (or any probe hiccup) must
        # fall back to the base-image 2.x dialect, not crash the run.
        def _no_version(arg_sets):
            if any(tuple(args) == ("--version",) for args in arg_sets):
                raise subprocess.CalledProcessError(2, "cocotb-config")
            return _stub_cocotb_config(arg_sets)

        with patch.object(crun, "_cocotb_config", side_effect=_no_version):
            env = crun._build_cocotb_env(
                tmp_path,
                "m",
                ["a"],
                tmp_path / "results.xml",
            )
        assert env["COCOTB_TEST_FILTER"] == r"^m\.(a)$"
        assert "TESTCASE" not in env

    def test_no_tests_means_no_filter(self, tmp_path: Path):
        # An empty selection runs the whole module — no COCOTB_TEST_FILTER,
        # matching "no test list declared" semantics.
        with patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config):
            env = crun._build_cocotb_env(
                tmp_path,
                "m",
                [],
                tmp_path / "results.xml",
            )
        assert "COCOTB_TEST_FILTER" not in env
        assert "TESTCASE" not in env

    def test_existing_pythonpath_preserved(self, tmp_path: Path):
        with (
            patch.dict(os.environ, {"PYTHONPATH": "/existing"}),
            patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config),
        ):
            env = crun._build_cocotb_env(tmp_path, "m", [], tmp_path / "r.xml")
        assert env["PYTHONPATH"] == f"{tmp_path}{os.pathsep}/existing"


class TestBuildRunCmd:
    def test_icarus_golden_argv(self, tmp_path: Path):
        (tmp_path / "sim_image.scr").write_text("")
        with (
            patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config),
            patch("shutil.which", return_value="/usr/bin/vvp"),
        ):
            cmd = crun._build_run_cmd(
                eda_tool="icarus",
                build_dir=tmp_path,
                plusargs=["verbose", "+already"],
                vcd=False,
            )
        assert cmd == [
            "/usr/bin/vvp",
            "-n",
            f"-M{tmp_path}",
            "-M",
            "/site/cocotb/libs",
            "-m",
            "cocotbvpi_icarus",
            str(tmp_path / "sim_image"),
            "+verbose",
            "+already",
        ]

    def test_icarus_trace_adds_plusarg(self, tmp_path: Path):
        (tmp_path / "img.scr").write_text("")
        with (
            patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config),
            patch("shutil.which", return_value="vvp"),
        ):
            cmd = crun._build_run_cmd(
                eda_tool="icarus",
                build_dir=tmp_path,
                plusargs=[],
                vcd=True,
            )
        assert cmd[-1] == "+trace"

    def test_icarus_missing_image_returns_none(self, tmp_path: Path):
        with patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config):
            assert (
                crun._build_run_cmd(
                    eda_tool="icarus",
                    build_dir=tmp_path,
                    plusargs=[],
                    vcd=False,
                )
                is None
            )

    def test_verilator_runs_vtop_not_vtoplevel(self, tmp_path: Path):
        # Edalize forces --prefix Vtop for cocotb builds: the binary is Vtop,
        # never V<toplevel> (spike exit note).
        (tmp_path / "Vtop").write_text("")
        cmd = crun._build_run_cmd(
            eda_tool="verilator",
            build_dir=tmp_path,
            plusargs=["+p"],
            vcd=False,
        )
        assert cmd == [str(tmp_path / "Vtop"), "+p"]

    def test_verilator_trace_uses_cocotb_main_flags(self, tmp_path: Path):
        # Spike S3: cocotb's verilator.cpp main owns tracing via getopt flags.
        (tmp_path / "Vtop").write_text("")
        cmd = crun._build_run_cmd(
            eda_tool="verilator",
            build_dir=tmp_path,
            plusargs=[],
            vcd=True,
        )
        assert cmd[-3:] == ["--trace", "--trace-file", "dump.vcd"]

    def test_verilator_missing_binary_returns_none(self, tmp_path: Path):
        assert (
            crun._build_run_cmd(
                eda_tool="verilator",
                build_dir=tmp_path,
                plusargs=[],
                vcd=False,
            )
            is None
        )


# ---------------------------------------------------------------------------
# D3 — stale-image error (cocotb-config absent)
# ---------------------------------------------------------------------------


def test_missing_cocotb_config_names_cause_and_fix(tmp_path: Path, capsys):
    with patch.object(crun, "_cocotb_config", side_effect=FileNotFoundError):
        rc = crun.run_cocotb_sim(
            build_dir=tmp_path,
            eda_tool="icarus",
            cocotb_module="m",
        )
    assert rc == 1
    out = capsys.readouterr().out
    assert "sandbox image predates cocotb support" in out
    assert "rebuild the sandbox image" in out
    # result.json records the inconclusive verdict (never a silent pass).
    result = (tmp_path / "result.json").read_text(encoding="utf-8")
    assert '"passed": false' in result
    assert '"inconclusive": true' in result


def test_unbuilt_dir_fails_with_message(tmp_path: Path, capsys):
    with patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config):
        rc = crun.run_cocotb_sim(
            build_dir=tmp_path,
            eda_tool="verilator",
            cocotb_module="m",
        )
    assert rc == 1
    assert "no built cocotb sim" in capsys.readouterr().out


@pytest.mark.parametrize("stub", [_stub_cocotb_config, FileNotFoundError])
def test_prerun_failures_carry_the_infra_marker(tmp_path: Path, capsys, stub):
    """SETUP-F-41b: nothing ran, so the nonzero exit is not a verdict. Both
    pre-run abort paths (unbuilt dir, absent cocotb-config) say so explicitly
    so a grading caller cannot score them as a killed mutant."""
    side_effect = stub if stub is not FileNotFoundError else FileNotFoundError()
    with patch.object(crun, "_cocotb_config", side_effect=side_effect):
        rc = crun.run_cocotb_sim(build_dir=tmp_path, eda_tool="verilator", cocotb_module="m")
    assert rc == 1
    out = capsys.readouterr().out
    assert SIM_INFRA_ERROR_PREFIX in out
    assert SIM_INFRA_ERROR_PREFIX in (tmp_path / "run.log").read_text(encoding="utf-8")


def test_stale_results_xml_removed_before_run(tmp_path: Path):
    # A stale results.xml from a previous run must never feed verdicts: the
    # run-half deletes it before launching (missing binary aborts right after).
    stale = tmp_path / crun.RESULTS_XML_NAME
    stale.write_text("<testsuites/>")
    stale_json = tmp_path / crun.FULL_RESULTS_JSON_NAME
    stale_json.write_text('{"state":"ok","tests":[{"name":"old_pass"}]}')
    with patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config):
        crun.run_cocotb_sim(
            build_dir=tmp_path,
            eda_tool="verilator",
            cocotb_module="m",
        )
    assert not stale.exists()
    assert not stale_json.exists()


# ---------------------------------------------------------------------------
# CLI arg round-trip
# ---------------------------------------------------------------------------


def test_parse_args_round_trip():
    args = crun._parse_args(
        [
            "--build-dir",
            "build/sim",
            "--eda-tool",
            "icarus",
            "--cocotb-module",
            "test_counter",
            "--test",
            "a",
            "--test",
            "b",
            "--run-cwd",
            "util/sim",
            "--work-dir",
            "out",
            "--timeout",
            "120",
            "--max-rundir-bytes",
            "1000",
            "--trace",
            "--expected-trace-scope",
            "counter",
            "--plusarg",
            "verbose",
            "--result-verbosity",
            "full",
        ]
    )
    assert args.build_dir == "build/sim"
    assert args.eda_tool == "icarus"
    assert args.cocotb_module == "test_counter"
    assert args.tests == ["a", "b"]
    assert args.run_cwd == "util/sim"
    assert args.work_dir == "out"
    assert args.timeout == 120
    assert args.max_rundir_bytes == 1000
    assert args.trace is True
    assert args.expected_trace_scope == "counter"
    assert args.plusargs == ["verbose"]
    assert args.result_verbosity == "full"


def test_parse_args_requires_eda_tool_and_module():
    with pytest.raises(SystemExit):
        crun._parse_args(["--build-dir", "b"])


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_parse_args_rejects_non_positive_timeout(bad: str):
    with pytest.raises(SystemExit):
        crun._parse_args(
            [
                "--build-dir",
                "b",
                "--eda-tool",
                "icarus",
                "--cocotb-module",
                "m",
                "--timeout",
                bad,
            ]
        )


# ---------------------------------------------------------------------------
# F-25 — frozen simulator clock watchdog wiring
# ---------------------------------------------------------------------------


def test_sim_time_grace_defaults_and_round_trips():
    from booley.sim.run_guard import DEFAULT_SIM_TIME_GRACE_S

    args = crun._parse_args(
        ["--build-dir", "b", "--eda-tool", "verilator", "--cocotb-module", "m"]
    )
    assert args.sim_time_grace_s == DEFAULT_SIM_TIME_GRACE_S

    args = crun._parse_args(
        [
            "--build-dir",
            "b",
            "--eda-tool",
            "verilator",
            "--cocotb-module",
            "m",
            "--sim-time-grace",
            "0",
        ]
    )
    assert args.sim_time_grace_s == 0.0


def test_parse_args_rejects_negative_sim_time_grace():
    with pytest.raises(SystemExit):
        crun._parse_args(
            [
                "--build-dir",
                "b",
                "--eda-tool",
                "verilator",
                "--cocotb-module",
                "m",
                "--sim-time-grace",
                "-1",
            ]
        )


def test_stall_diagnosis_replaces_the_useless_missing_xml_detail(tmp_path: Path, capsys):
    """F-25: a frozen run loop names itself instead of 'results.xml not found'.

    The guard killed the sim before cocotb wrote any XML, so the parser can only
    report the symptom. The abort reason in the captured output is the cause.
    """
    from booley.sim.run_guard import format_sim_time_stall

    output = (
        "     0.00ns INFO     cocotb.regression   running test_ravenoc\n"
        f"ERROR: {format_sim_time_stall(180)}\n"
    )
    combined, passed = crun._evaluate_verdict(
        output,
        returncode=0,
        timed_out=False,
        work_dir=tmp_path,
        results_file=tmp_path / "results.xml",
        tests=["test_ravenoc"],
    )
    assert passed is False
    assert "run-loop version mismatch" in combined
    # The verdict line quotes the promoted detail, not "results.xml not found".
    assert "results.xml not found" not in capsys.readouterr().out.split("INCONCLUSIVE")[-1]


def test_disk_baseline_is_taken_before_the_spawn(tmp_path: Path, monkeypatch):
    """A3: the pre-existing-bytes snapshot must predate the simulator.

    Same reason as the other two run-halves: ``dir_size_bytes`` is a recursive
    walk that takes seconds on a multi-GB run dir, and a baseline taken after
    the Popen quietly absorbs whatever the sim wrote during it.
    """
    import sys

    import booley.sim.run_guard as rg

    order: list[str] = []
    real_snapshot = rg.snapshot_dir_baseline
    real_popen = subprocess.Popen

    def _snapshot(rundir, budget):
        order.append("baseline")
        return real_snapshot(rundir, budget)

    def _popen(*args, **kwargs):
        order.append("spawn")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(rg, "snapshot_dir_baseline", _snapshot)
    monkeypatch.setattr(subprocess, "Popen", _popen)

    run = tmp_path / "run"
    run.mkdir()
    crun._stream_output(
        [sys.executable, "-c", "pass"],
        run,
        os.environ.copy(),
        30,
        max_rundir_bytes=1 << 20,
    )
    assert order == ["baseline", "spawn"]
