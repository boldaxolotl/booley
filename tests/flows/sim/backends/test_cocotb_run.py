"""Unit tests for the cocotb run-half (booley.flows.sim.backends.cocotb — G1, G5).

In the pattern of test_iverilog_run.py: pure run-half logic with no real
simulator — the filter builder's escaping property (G1), golden argv/env for
icarus and verilator with ``cocotb-config`` stubbed (G5), the explicit
results-file path, the D3 stale-image error, and the work-document mapping. The real
vvp/Vtop runs are exercised end-to-end in the Sandbox e2e.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from booley.flows.sim.adapter_transport import (
    AdapterTestResult,
    AdapterTransportIdentity,
    partial_result_identity,
    read_adapter_result,
)
from booley.flows.sim.backends import cocotb as crun
from booley.flows.sim.backends import cocotb_results
from booley.flows.sim.backends.cocotb_results import STATE_OK, CocotbResults, CocotbTest
from booley.flows.sim.backends.shared import RunTermination
from booley.flows.sim.result import SIM_INFRA_ERROR_PREFIX

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
    ("--version",): "2.1.0",
    ("--libpython",): "/usr/lib/libpython3.13.so",
    ("--python-bin",): "/usr/bin/python3",
    ("--lib-name-path", "vpi", "icarus"): "/site/cocotb/libs/libcocotbvpi_icarus.so",
    ("--pygpi-entry-point",): "/site/cocotb/simulator.so,initialize",
}


def _stub_cocotb_config(arg_sets):
    return [_CONFIG_ANSWERS[tuple(args)] for args in arg_sets]


def _stub_cocotb_config_v1(arg_sets):
    """A cocotb-1.9.2 sandbox (project image pinning a legacy stack)."""
    return [
        "1.9.2" if tuple(args) == ("--version",) else _CONFIG_ANSWERS[tuple(args)]
        for args in arg_sets
    ]


def _stub_cocotb_config_v2_0(arg_sets):
    """Cocotb 2.0 uses an extensionless path that vvp resolves to ``.vpl``."""
    return [
        (
            "2.0.1"
            if tuple(args) == ("--version",)
            else "/site/cocotb/libs/libcocotbvpi_icarus"
            if tuple(args) == ("--lib-name-path", "vpi", "icarus")
            else _CONFIG_ANSWERS[tuple(args)]
        )
        for args in arg_sets
    ]


def _stub_cocotb_config_no_version(arg_sets):
    """A cocotb-config whose ``--version`` prints nothing parseable."""
    return [
        "unknown" if tuple(args) == ("--version",) else _CONFIG_ANSWERS[tuple(args)]
        for args in arg_sets
    ]


class TestCocotbSeedEnvironment:
    """One seed variable is set: the one the installed cocotb reads (ADR 0072)."""

    def _env(self, tmp_path: Path, stub, seed: int | None, ambient: dict[str, str]):
        with (
            patch.dict(os.environ, ambient, clear=True),
            patch.object(crun, "_cocotb_config", side_effect=stub),
        ):
            return crun._build_cocotb_env(tmp_path, "m", ["a"], tmp_path / "r.xml", seed)

    @pytest.mark.parametrize("seed", [1, 2**31 - 1])
    def test_cocotb2_sets_cocotb_random_seed(self, tmp_path: Path, seed: int):
        env = self._env(tmp_path, _stub_cocotb_config, seed, {})
        assert env["COCOTB_RANDOM_SEED"] == str(seed)
        assert "RANDOM_SEED" not in env

    @pytest.mark.parametrize("seed", [1, 2**31 - 1])
    def test_cocotb1_sets_random_seed(self, tmp_path: Path, seed: int):
        env = self._env(tmp_path, _stub_cocotb_config_v1, seed, {})
        assert env["RANDOM_SEED"] == str(seed)
        assert "COCOTB_RANDOM_SEED" not in env

    def test_an_inherited_seed_never_survives_a_seeded_run(self, tmp_path: Path):
        ambient = {"RANDOM_SEED": "111", "COCOTB_RANDOM_SEED": "222"}
        env2 = self._env(tmp_path, _stub_cocotb_config, 5, ambient)
        assert (env2["COCOTB_RANDOM_SEED"], env2.get("RANDOM_SEED")) == ("5", None)
        env1 = self._env(tmp_path, _stub_cocotb_config_v1, 5, ambient)
        assert (env1["RANDOM_SEED"], env1.get("COCOTB_RANDOM_SEED")) == ("5", None)

    def test_an_unseeded_run_leaves_the_environment_alone(self, tmp_path: Path):
        env = self._env(tmp_path, _stub_cocotb_config, None, {"RANDOM_SEED": "111"})
        assert env["RANDOM_SEED"] == "111"
        assert "COCOTB_RANDOM_SEED" not in env

    def test_a_failed_version_probe_is_an_error_for_a_seeded_run(self, tmp_path: Path):
        with pytest.raises(crun.CocotbSeedError) as excinfo:
            self._env(tmp_path, _stub_cocotb_config_no_version, 5, {})
        assert str(excinfo.value) == (
            "cannot seed the Cocotb run with 5: `cocotb-config --version` did not report "
            "a version, so the seed variable of the installed cocotb is unknown"
        )

    def test_a_failed_version_probe_keeps_the_fallback_for_an_unseeded_run(self, tmp_path: Path):
        env = self._env(tmp_path, _stub_cocotb_config_no_version, None, {})
        assert "COCOTB_TEST_FILTER" in env

    def test_a_failed_probe_is_reported_as_infrastructure_not_a_verdict(
        self, tmp_path: Path, capsys
    ):
        with patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config_no_version):
            prepared = crun._prepare_invocation(
                build_dir=tmp_path,
                eda_tool="icarus",
                cocotb_module="m",
                tests=["a"],
                results_file=tmp_path / "r.xml",
                work_dir=tmp_path,
                plusargs=[],
                vcd=False,
                seed=5,
            )
        assert isinstance(prepared, str) and prepared.startswith("cannot seed the Cocotb run")
        assert "[SIM_INFRA_ERROR]" in capsys.readouterr().out


class TestBuildCocotbEnv:
    def test_env_golden(self, tmp_path: Path):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config),
        ):
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
        assert env["GPI_USERS"] == (
            "/usr/lib/libpython3.13.so;/site/cocotb/simulator.so,initialize"
        )
        # C1: the results path is explicit — never guessed.
        assert env["COCOTB_RESULTS_FILE"] == str(tmp_path / "results.xml")
        # Spike S1: the build dir is pinned on PYTHONPATH so a project
        # run_cwd cannot break the module import.
        assert env["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path)
        cache_root = tmp_path / "python-artifacts"
        assert "PYTHONPYCACHEPREFIX" not in env
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        pytest_cache = Path(shlex.split(env["PYTEST_ADDOPTS"])[-1].removeprefix("cache_dir="))
        assert pytest_cache.parent == cache_root / "pytest"

    def test_env_reuses_endpoint_runtime_for_pytest(self, tmp_path: Path):
        runtime = tmp_path / "runtime"
        with (
            patch.dict(os.environ, {"BOOLEY_RUNTIME_DIR": str(runtime)}, clear=True),
            patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config),
        ):
            env = crun._build_cocotb_env(
                tmp_path / "build",
                "test_counter",
                ["a"],
                tmp_path / "attempt" / "results.xml",
            )

        pytest_cache = Path(shlex.split(env["PYTEST_ADDOPTS"])[-1].removeprefix("cache_dir="))
        assert pytest_cache.parent == runtime / "python-artifacts" / "pytest"
        assert str(tmp_path / "attempt") not in str(pytest_cache)

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
        assert "GPI_USERS" not in env

    def test_cocotb2_0_retains_libpython_loc_contract(self, tmp_path: Path):
        with patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config_v2_0):
            env = crun._build_cocotb_env(
                tmp_path,
                "test_counter",
                ["a"],
                tmp_path / "results.xml",
            )

        assert env["LIBPYTHON_LOC"] == "/usr/lib/libpython3.13.so"
        assert "GPI_USERS" not in env

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

    def test_cocotb2_1_preserves_additional_gpi_users(self, tmp_path: Path):
        with (
            patch.dict(os.environ, {"GPI_USERS": "/project/custom.so"}),
            patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config),
        ):
            env = crun._build_cocotb_env(tmp_path, "m", [], tmp_path / "r.xml")

        assert env["GPI_USERS"] == (
            "/usr/lib/libpython3.13.so;/site/cocotb/simulator.so,initialize;/project/custom.so"
        )


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
            "-m",
            "/site/cocotb/libs/libcocotbvpi_icarus.so",
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

    def test_icarus_cocotb_2_0_extensionless_library_path(self, tmp_path: Path):
        (tmp_path / "sim_image.scr").write_text("")
        with (
            patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config_v2_0),
            patch("shutil.which", return_value="vvp"),
        ):
            cmd = crun._build_run_cmd(
                eda_tool="icarus",
                build_dir=tmp_path,
                plusargs=[],
                vcd=False,
            )

        assert cmd is not None
        assert cmd[cmd.index("-m") + 1] == "/site/cocotb/libs/libcocotbvpi_icarus"

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
    assert "booley bootstrap --update" in out
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
# Work document -> run_cocotb_sim
# ---------------------------------------------------------------------------


def test_document_fields_reach_run_cocotb_sim(tmp_path: Path, monkeypatch):
    from booley.flows.sim.adapter_contract import (
        PreparedSimulationWork,
        encode_simulation_work,
    )

    seen: dict[str, object] = {}

    def record(**kwargs: object) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(crun, "run_cocotb_sim", record)
    result_path = tmp_path / "adapter-result.json"
    traced = PreparedSimulationWork(
        adapter="cocotb",
        build_dir=str(tmp_path / "build"),
        run_cwd=str(tmp_path / "util"),
        timeout_s=120,
        # Never forwarded: the Cocotb adapter derives its own output directory.
        work_dir=str(tmp_path / "out"),
        eda_tool="icarus",
        cocotb_module="test_counter",
        tests=("b", "a"),
        cocotb_seed=42,
        max_rundir_bytes=1000,
        plusargs=("verbose",),
        trace=True,
        trace_scope="counter",
        trace_convert_timeout_s=90,
        result_verbosity="full",
        sim_time_grace_s=2.5,
        runtime_inputs=("firmware.hex",),
        adapter_result_path=str(result_path),
        attempt_token="attempt-1",
        target_identity="acme:lib:core:1#sim",
    )

    assert crun.main([encode_simulation_work(traced)]) == 0

    assert seen == {
        "build_dir": tmp_path / "build",
        "eda_tool": "icarus",
        "cocotb_module": "test_counter",
        "tests": ["b", "a"],  # order kept
        "run_cwd": tmp_path / "util",
        "vcd": True,
        "plusargs": ["verbose"],
        "timeout": 120,
        "max_rundir_bytes": 1000,
        "sim_time_grace_s": 2.5,
        "result_verbosity": "full",
        "expected_trace_scope": "counter",
        "runtime_inputs": ("firmware.hex",),
        "transport": AdapterTransportIdentity(
            adapter="cocotb",
            attempt_token="attempt-1",
            target_identity="acme:lib:core:1#sim",
            selected_tests=("b", "a"),
            result_path=result_path,
        ),
        "seed": 42,
        "trace_convert_timeout": 90,
    }  # fmt: skip
    assert "work_dir" not in seen

    # An untraced launch expects no trace scope, whatever scope the work names;
    # an unseeded, untransported one carries neither.
    seen.clear()
    untraced = PreparedSimulationWork(
        adapter="cocotb",
        build_dir=str(tmp_path / "build"),
        run_cwd="",
        timeout_s=120,
        eda_tool="verilator",
        cocotb_module="m",
        trace_scope="counter",
    )
    assert crun.main([encode_simulation_work(untraced)]) == 0
    assert seen["expected_trace_scope"] == ""
    assert seen["vcd"] is False
    assert seen["seed"] is None
    assert seen["transport"] is None
    assert seen["run_cwd"] is None
    assert seen["tests"] == []


# ---------------------------------------------------------------------------
# F-25 — frozen simulator clock watchdog wiring
# ---------------------------------------------------------------------------


def test_stall_diagnosis_replaces_the_useless_missing_xml_detail(tmp_path: Path, capsys):
    """F-25: a frozen run loop names itself instead of 'results.xml not found'.

    The guard killed the sim before cocotb wrote any XML, so the parser can only
    report the symptom. The abort reason in the captured output is the cause.
    """
    from booley.flows.sim.run_guard import format_sim_time_stall

    output = (
        "     0.00ns INFO     cocotb.regression   running test_ravenoc\n"
        f"ERROR: {format_sim_time_stall(180)}\n"
    )
    combined, passed, _ = crun._evaluate_verdict(
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


def test_partial_cocotb_abort_preserves_completed_test_and_publishes_termination(
    tmp_path: Path,
) -> None:
    identity = AdapterTransportIdentity(
        "cocotb", "attempt", "acme:lib:dut:1#sim", ("done", "later"), tmp_path / "adapter.json"
    )
    results = CocotbResults(
        state=STATE_OK, tests=(CocotbTest("done", "test_demo", "pass", elapsed_s=0.1),)
    )
    termination = RunTermination(
        "sim_time_stall", "simulator time did not advance", "infrastructure"
    )

    crun._publish_adapter_result(
        identity,
        "",
        False,
        results=results,
        termination=termination,
        simulator_returncode=-15,
    )

    result = read_adapter_result(identity)
    assert result.termination == "sim_time_stall"
    assert result.inconclusive is False
    assert result.simulator_returncode == -15
    assert [(test.name, test.verdict, test.termination) for test in result.test_results] == [
        ("done", "pass", "completed"),
        ("later", "fail", "sim_time_stall"),
    ]


def test_live_exception_log_before_cocotb_child_completes(tmp_path, monkeypatch):
    """Real streaming writes diagnostics to the evidence root while the child waits."""
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from booley.flows.run_log import begin_run_log
    from booley.flows.sim.backends import shared

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    cwd = tmp_path / "child"
    cwd.mkdir()
    log = begin_run_log(evidence, flow="sim", target="cocotb", run="live-test")
    observed = Event()
    children = []
    real_popen = subprocess.Popen

    def spawn(*args, **kwargs):
        child = real_popen(*args, **kwargs, stdin=subprocess.PIPE)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(shared, "RUN_LOG_PROGRESS_INTERVAL_S", 0)
    monkeypatch.setattr(crun, "_partial_result_publisher", lambda *_: lambda _: observed.set())
    run = crun._CocotbRun(
        evidence,
        cwd,
        evidence,
        evidence / "results.xml",
        ["selected"],
        os.environ.copy(),
        [
            sys.executable,
            "-u",
            "-c",
            "import sys; print('RuntimeError: live failure'); sys.stdin.readline(); print('final diagnostic')",
        ],
        None,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(crun._execute_cocotb_run, run, 5, 0, 0, None, ())
        try:
            assert observed.wait(5), "child output never reached the streaming callback"
            assert not future.done()
            contents = log.read_text()
            assert "RuntimeError: live failure" in contents
            assert "run=live-test" in contents and "run in progress" in contents
            assert not (cwd / "run.log").exists()
        finally:
            if children:
                children[0].stdin.write("release\n")
                children[0].stdin.flush()
                children[0].stdin.close()
            _lines, proc, termination = future.result(timeout=6)
    assert proc.returncode == 0
    assert not termination.aborted
    assert "final diagnostic" in log.read_text()


def _child_run(tmp_path: Path, script: str, tests: list[str], env=None) -> crun._CocotbRun:
    """A `_CocotbRun` whose simulator is a tiny unbuffered Python child running *script*."""
    command = [sys.executable, "-u", "-c", script]
    return crun._CocotbRun(
        *(tmp_path,) * 3, tmp_path / "results.xml", tests, env or os.environ.copy(), command, None
    )


def test_execute_passes_guard_inputs_to_the_runner(tmp_path: Path, monkeypatch) -> None:
    identity = AdapterTransportIdentity(
        "cocotb", "a", "acme:lib:dut:1#sim", ("t1",), tmp_path / "r"
    )
    run = _child_run(tmp_path, "pass", ["t1"], env={"COCOTB_TEST_FILTER": "t1"})
    from booley.flows.sim.backends.supervision import SimulatorLaunch

    launches: list[SimulatorLaunch] = []
    monkeypatch.setattr(crun, "run_supervised", launches.append)

    crun._execute_cocotb_run(run, 77, 1234, 9.5, identity, ("mem.hex",))

    (launch,) = launches
    assert callable(launch.on_line)
    assert replace(launch, on_line=None) == SimulatorLaunch(
        "cocotb", run.command, tmp_path, run.env, 77, tmp_path, 1234, ("mem.hex",), 9.5
    )


def test_printing_cocotb_run_is_stopped_at_the_deadline(
    tmp_path: Path, never_firing_timer
) -> None:
    """Only the per-line deadline check can end this run: the timer never fires."""
    script = "import time\nfor i in range(1500):\n    print('tick', i, flush=True)\n    time.sleep(0.01)\n"

    started = time.monotonic()
    _, _, termination = crun._execute_cocotb_run(
        _child_run(tmp_path, script, ["t"]), 1, 0, 0, None, ()
    )

    assert termination.kind == "timeout"
    assert time.monotonic() - started < 20
    assert "last output" in termination.detail


def test_sim_time_stall_ends_cocotb_run_and_beats_timeout(
    tmp_path: Path, monkeypatch, instant_timer
) -> None:
    from booley.flows.sim.backends import supervision

    real_guard = supervision.SimTimeStallGuard

    def tripped_guard(*args):
        guard = real_guard(*args)
        guard.tripped = True
        return guard

    monkeypatch.setattr(supervision, "SimTimeStallGuard", tripped_guard)
    run = _child_run(tmp_path, "import time; time.sleep(60)", ["t"])

    _, _, termination = crun._execute_cocotb_run(run, 30, 0, 5.0, None, ())

    assert termination.kind == "sim_time_stall"
    assert termination.failure_kind == "infrastructure"


def test_killed_cocotb_run_publishes_partial_results(tmp_path: Path) -> None:
    """Without the per-line hook only the initial publish runs and every test reads timeout."""
    identity = AdapterTransportIdentity(
        "cocotb", "a", "acme:lib:dut:1#sim", ("t1", "t2"), tmp_path / "adapter.json"
    )
    script = (
        "import time\n"
        "print('0.00ns INFO cocotb.regression running t1 (1/2)', flush=True)\n"
        "time.sleep(0.3)\n"
        "print('1.00ns INFO cocotb.regression t1 passed', flush=True)\n"
        "print('1.00ns INFO cocotb.regression running t2 (2/2)', flush=True)\n"
        "time.sleep(60)\n"
    )

    _, _, termination = crun._execute_cocotb_run(
        _child_run(tmp_path, script, ["t1", "t2"]), 5, 0, 0, identity, ()
    )

    assert termination.kind == "timeout"
    partial = read_adapter_result(partial_result_identity(identity))
    assert [(t.name, t.verdict) for t in partial.test_results] == [
        ("t1", "pass"),
        ("t2", "timeout"),
    ]


@pytest.mark.parametrize("eda_tool,backend", [("icarus", "iverilog"), ("verilator", "verilator")])
def test_traced_cocotb_status_reports_simulator(tmp_path: Path, eda_tool: str, backend: str):
    build = tmp_path / "build"
    build.mkdir()
    (build / ("sim_image.scr" if eda_tool == "icarus" else "Vtop")).write_text("")
    with (
        patch.object(crun, "_cocotb_config", side_effect=_stub_cocotb_config),
        patch("shutil.which", return_value="/usr/bin/vvp"),
    ):
        run = crun._prepare_cocotb_run(
            build_dir=build,
            run_cwd=None,
            work_dir=tmp_path / "work",
            eda_tool=eda_tool,
            module="test_counter",
            tests=["test_count"],
            plusargs=[],
            vcd=True,
            trace_scope="counter",
        )
    assert not isinstance(run, str)
    if eda_tool == "icarus":
        assert run.command[0] == "/usr/bin/vvp"
        assert str(build / "sim_image") in run.command
        assert run.command[-1] == "+trace"
    else:
        assert run.command[0] == str(build / "Vtop")
        assert run.command[-3:] == ["--trace", "--trace-file", "dump.vcd"]
    assert run.current is not None
    trace = run.current.session
    assert not trace.manifest_path.exists()
    trace.record_attempt("vcd_postprocess", "started")
    status = json.loads(trace.manifest_path.read_text())
    assert status["backend"] == backend
    assert status["trace_scope"] == "counter"


def test_rtl_fatal_failure_text_names_assertion_fatal_messages(capsys):
    """Cocotb failure text counts ``$fatal`` lines as messages, not SVA assertions (#2807)."""
    (detailed,) = crun._apply_design_evidence((AdapterTestResult("t", "pass"),), 1, "", "")
    assert detailed.detail == "1 assertion/fatal error message"

    assessment = crun._VerdictAssessment(
        selected=("t",),
        verdicts=(("t", "pass", ""),),
        sva_errors=2,
        passed=False,
        inconclusive=False,
        infrastructure_error="",
    )
    # The failure branch never reads the parsed results.
    crun._print_verdict(
        assessment, crun.CocotbResults.__new__(crun.CocotbResults), RunTermination(), 0
    )

    out = capsys.readouterr().out
    assert "cocotb sim FAILED (0/1 tests failed, 2 assertion/fatal error messages)" in out
    assert "SVA" not in out


# --- adapter result and transports are byte-stable (issue #3002) -------------

_GOLDEN_XML = (
    "<testsuites><testsuite>"
    '<testcase name="done" classname="test_demo" time="0.25"/>'
    '<testcase name="boom" classname="test_demo" time="0.5"><failure message="boom failed"/>'
    "</testcase>"
    '<testcase name="skip_me" classname="test_demo" time="0"><skipped/></testcase>'
    "</testsuite></testsuites>"
)
_DONE = '{"name":"done","module":"test_demo","status":"pass","failure":"","elapsed_s":0.25}'
_BOOM = (
    '{"name":"boom","module":"test_demo","status":"fail","failure":"boom failed","elapsed_s":0.5}'
)
_SKIP = '{"name":"skip_me","module":"test_demo","status":"skipped","failure":"","elapsed_s":0.0}'
_LINE_FULL = (
    f'[COCOTB_RESULTS] {{"state":"ok","detail":"","tests":[{_DONE},{_BOOM},{_SKIP}],'
    '"skipped_unselected":0}'
)
_LINE_FOCUSED_COMPACT = (
    f'[COCOTB_RESULTS] {{"state":"ok","detail":"","tests":[{_DONE},{_BOOM}],'
    '"skipped_unselected":1,"reported_names":["done","boom","skip_me"]}'
)
_RESULTS_JSON = """\
{
  "state": "ok",
  "detail": "",
  "tests": [
    {
      "name": "done",
      "module": "test_demo",
      "status": "pass",
      "failure": "",
      "elapsed_s": 0.25
    },
    {
      "name": "boom",
      "module": "test_demo",
      "status": "fail",
      "failure": "boom failed",
      "elapsed_s": 0.5
    },
    {
      "name": "skip_me",
      "module": "test_demo",
      "status": "skipped",
      "failure": "",
      "elapsed_s": 0.0
    }
  ],
  "skipped_unselected": 0
}
"""
_FOCUSED = ["done", "missing"]
_MISSING_ID = (
    "missing",
    "inconclusive",
    'no matching test ID "missing" in cocotb results. cocotb reported test IDs: '
    '"done", "boom", "skip_me". Check tests.toml against the rendered IDs in results.xml.',
)
_DIAGNOSTIC = "results.xml reports extra non-selected test(s): {} — logged, not verdict-bearing"
_UNFOCUSED_TESTS = (
    ("done", "pass", ""),
    ("boom", "fail", "boom failed"),
    (
        "skip_me",
        "inconclusive",
        "selected but reported skipped by cocotb — the test filter did not match it "
        "(name/filter mismatch)",
    ),
)

# case -> (xml, selection, verbosity, stdout line, failure_kind, per-test verdicts, diagnostics)
_GOLDEN_CASES = {
    "focused-compact": (
        _GOLDEN_XML,
        _FOCUSED,
        "compact",
        _LINE_FOCUSED_COMPACT,
        "inconclusive",
        (("done", "pass", ""), _MISSING_ID),
        (_DIAGNOSTIC.format("boom"),),
    ),
    "focused-full": (
        _GOLDEN_XML,
        _FOCUSED,
        "full",
        _LINE_FULL,
        "inconclusive",
        (("done", "pass", ""), _MISSING_ID),
        (_DIAGNOSTIC.format("boom, skip_me"),),
    ),
    "unfocused-compact": (_GOLDEN_XML, [], "compact", _LINE_FULL, "design", _UNFOCUSED_TESTS, ()),
    "unfocused-full": (_GOLDEN_XML, [], "full", _LINE_FULL, "design", _UNFOCUSED_TESTS, ()),
    # A non-finite time makes the transported payload undecodable: both selected tests
    # become inconclusive for missing results (the line itself is not pinned here).
    "nan-focused-compact": (
        _GOLDEN_XML.replace('time="0.25"', 'time="nan"'),
        _FOCUSED,
        "compact",
        None,
        "inconclusive",
        (
            ("done", "inconclusive", "missing results"),
            ("missing", "inconclusive", "missing results"),
        ),
        (),
    ),
}


def test_adapter_result_and_transports_match_pre_refactor_goldens(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """Adapter result, stdout line and ``cocotb_results.json`` stay byte-stable.

    Characterization: the literals were captured from the code that parsed the
    adapter's own stdout line back into results, so this proves the refactor
    changes nothing observable (selected-but-unreported, unselected-ran and
    unselected-skipped tests; compact and full verbosity; a non-finite time).
    The adapter must also build its result without parsing that line.
    """

    def forbidden_parse(output: str) -> None:
        raise AssertionError("adapter parsed its own stdout line")

    # Patch the defining module and the adapter's own namespace: either call style fails.
    monkeypatch.setattr(cocotb_results, "parse_results_line", forbidden_parse)
    monkeypatch.setattr(crun, "parse_results_line", forbidden_parse, raising=False)
    for case, (
        xml,
        selection,
        verbosity,
        line,
        failure_kind,
        verdicts,
        notes,
    ) in _GOLDEN_CASES.items():
        work = tmp_path / case
        work.mkdir()
        results_file = work / "results.xml"
        results_file.write_text(xml, encoding="utf-8")
        identity = AdapterTransportIdentity(
            "cocotb", "attempt", "acme:lib:dut:1#sim", tuple(selection), work / "adapter.json"
        )
        run = crun._CocotbRun(work, work, work, results_file, list(selection), {}, [], None)

        crun._complete_cocotb_run(
            run, [], SimpleNamespace(returncode=0), RunTermination(), verbosity, identity
        )

        result = read_adapter_result(identity)
        flags = (result.passed, result.inconclusive, result.failure_kind)
        assert flags == (False, True, failure_kind), case
        assert result.tests == tuple(selection or ("done", "boom", "skip_me")), case
        assert tuple((t.name, t.verdict, t.detail) for t in result.test_results) == verdicts, case
        assert result.diagnostics == notes, case
        stdout_lines = capsys.readouterr().out.splitlines()
        if line is not None:
            assert [row for row in stdout_lines if row.startswith("[COCOTB_RESULTS]")] == [line], (
                case
            )
            assert (work / "cocotb_results.json").read_text(encoding="utf-8") == _RESULTS_JSON, (
                case
            )
