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
import time
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows.sim.adapter_transport import (
    AdapterTransportIdentity,
    partial_result_identity,
    read_adapter_result,
)
from booley.flows.sim.backends import cocotb as crun
from booley.flows.sim.backends import cocotb_results
from booley.flows.sim.backends.shared import RunTermination
from booley.flows.sim.backends.supervision import SimulatorLaunch
from booley.flows.sim.result import SIM_INFRA_ERROR_PREFIX

# ---------------------------------------------------------------------------
# G1 — filter builder
# ---------------------------------------------------------------------------


class TestBuildCocotbTestFilter:
    @pytest.mark.parametrize(
        ("module", "names", "expected"),
        [
            ("test_counter", ["a", "b"], r"^test_counter\.(a|b)$"),
            ("m", ["only"], r"^m\.(only)$"),
        ],
    )
    def test_names_are_anchored_to_the_module(self, module, names, expected):
        # Fully-qualified anchoring (spike S4): cocotb matches the filter
        # against "<module>.<test>", so bare-name anchors match zero tests.
        assert crun.build_cocotb_test_filter(module, names) == expected

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
# G5 -- golden argv / env through run_cocotb_sim (cocotb-config and vvp stubbed)
# ---------------------------------------------------------------------------

_LIBPYTHON = "/usr/lib/libpython3.13.so"
_GPI_BASE = f"{_LIBPYTHON};/site/cocotb/simulator.so,initialize"
_EXTENSIONLESS_LIB = "/site/cocotb/libs/libcocotbvpi_icarus"
_SO = f"{_EXTENSIONLESS_LIB}.so"
_VVP = ["{vvp}", "-n", "-M{build}", "-m"]
_IMG = "{build}/sim_image"


@pytest.fixture(autouse=True)
def vvp(cocotb_config, tool_stub, monkeypatch) -> Path:
    """A cocotb 2.1.0 ``cocotb-config`` and a ``vvp`` on PATH; no ambient cocotb variables."""
    for name in (
        "GPI_USERS",
        "BOOLEY_RUNTIME_DIR",
        "PYTHONPATH",
        "RANDOM_SEED",
        "COCOTB_RANDOM_SEED",
    ):
        monkeypatch.delenv(name, raising=False)
    cocotb_config()
    return tool_stub("vvp", "exit 0")


def _run(tmp_path: Path, eda_tool: str = "icarus", *, image: bool = True, **kwargs) -> int:
    """``run_cocotb_sim`` over ``tmp_path/build`` (the image exists unless *image* is False)."""
    build = tmp_path / "build"
    build.mkdir(exist_ok=True)
    if image:
        (build / ("sim_image.scr" if eda_tool == "icarus" else "Vtop")).write_text("")
    kwargs.setdefault("cocotb_module", "m")
    return crun.run_cocotb_sim(build_dir=build, eda_tool=eda_tool, **kwargs)


def _identity(tmp_path: Path, tests=("t",)) -> AdapterTransportIdentity:
    return AdapterTransportIdentity(
        "cocotb", "attempt", "acme:lib:dut:1#sim", tuple(tests), tmp_path / "adapter.json"
    )


@pytest.mark.parametrize("seed", [1, 2**31 - 1])
@pytest.mark.parametrize(
    ("version", "variable", "absent"),
    [
        ("2.1.0", "COCOTB_RANDOM_SEED", "RANDOM_SEED"),
        ("1.9.2", "RANDOM_SEED", "COCOTB_RANDOM_SEED"),
    ],
)
def test_a_seeded_run_sets_only_the_seed_variable_the_installed_cocotb_reads(
    tmp_path, cocotb_config, fake_launch, monkeypatch, seed, version, variable, absent
):
    """ADR 0072: an inherited value of either variable never survives a seeded run."""
    cocotb_config(version)
    monkeypatch.setenv("RANDOM_SEED", "111")
    monkeypatch.setenv("COCOTB_RANDOM_SEED", "222")
    _run(tmp_path, tests=["a"], seed=seed)
    env = fake_launch.launches[0].env
    assert env[variable] == str(seed)
    assert absent not in env


@pytest.mark.parametrize("version", ["2.1.0", None])
def test_an_unseeded_run_leaves_the_environment_alone(
    tmp_path, cocotb_config, fake_launch, monkeypatch, version
):
    cocotb_config(version)  # None: a failed probe keeps the fallback dialect
    monkeypatch.setenv("RANDOM_SEED", "111")
    _run(tmp_path, tests=["a"])
    env = fake_launch.launches[0].env
    assert env["RANDOM_SEED"] == "111"
    assert "COCOTB_RANDOM_SEED" not in env
    assert "COCOTB_TEST_FILTER" in env


def test_a_failed_version_probe_is_infrastructure_for_a_seeded_run(
    tmp_path, cocotb_config, fake_launch, capsys
):
    cocotb_config("unknown")
    assert _run(tmp_path, tests=["a"], seed=5) == 1
    out = capsys.readouterr().out
    assert fake_launch.launches == []
    assert (
        "cannot seed the Cocotb run with 5: `cocotb-config --version` did not report "
        "a version, so the seed variable of the installed cocotb is unknown"
    ) in out
    assert "[SIM_INFRA_ERROR]" in out


def test_env_golden(tmp_path, fake_launch, monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR")  # no runtime root: artifacts stay beside the results file
    monkeypatch.setenv("PYTHONPATH", "/existing")
    monkeypatch.setenv("GPI_USERS", "/project/custom.so")
    _run(tmp_path, tests=["a", "b"], cocotb_module="test_counter")
    build = (tmp_path / "build").resolve()
    env = fake_launch.launches[0].env
    assert env["COCOTB_TEST_MODULES"] == "test_counter"
    assert env["MODULE"] == "test_counter"  # cocotb <2.0 compat
    assert env["COCOTB_TEST_FILTER"] == r"^test_counter\.(a|b)$"
    assert env["LIBPYTHON_LOC"] == _LIBPYTHON
    assert env["PYGPI_PYTHON_BIN"] == "/usr/bin/python3"
    assert env["GPI_USERS"] == f"{_GPI_BASE};/project/custom.so"
    # C1: the results path is explicit, never guessed.
    assert env["COCOTB_RESULTS_FILE"] == str(build / "results.xml")
    # Spike S1: the build dir leads PYTHONPATH so a project run_cwd cannot break the import.
    assert env["PYTHONPATH"] == f"{build}{os.pathsep}/existing"
    assert "PYTHONPYCACHEPREFIX" not in env
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    pytest_cache = Path(shlex.split(env["PYTEST_ADDOPTS"])[-1].removeprefix("cache_dir="))
    assert pytest_cache.parent == build / "python-artifacts" / "pytest"


def test_env_reuses_endpoint_runtime_for_pytest(tmp_path, fake_launch, monkeypatch):
    """#794: the pytest cache lives under the endpoint runtime, not the attempt directory."""
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime))
    _run(tmp_path, tests=["a"], cocotb_module="test_counter", work_dir=tmp_path / "attempt")
    env = fake_launch.launches[0].env
    pytest_cache = Path(shlex.split(env["PYTEST_ADDOPTS"])[-1].removeprefix("cache_dir="))
    assert pytest_cache.parent == runtime / "python-artifacts" / "pytest"
    assert str(tmp_path / "attempt") not in str(pytest_cache)


@pytest.mark.parametrize(
    ("version", "libpath", "tests", "present", "absent"),
    [
        (
            "2.1.0",
            None,
            ["a", "b"],
            {"COCOTB_TEST_FILTER": r"^m\.(a|b)$", "GPI_USERS": _GPI_BASE},
            ("TESTCASE",),  # cocotb 2.x removed TESTCASE
        ),
        (
            "1.9.2",  # 1.x silently ignores COCOTB_TEST_FILTER
            None,
            ["run_test_001", "run_test_002"],
            {"TESTCASE": "run_test_001,run_test_002", "MODULE": "m"},
            ("COCOTB_TEST_FILTER", "GPI_USERS"),
        ),
        ("2.0.1", _EXTENSIONLESS_LIB, ["a"], {"LIBPYTHON_LOC": _LIBPYTHON}, ("GPI_USERS",)),
        (None, None, ["a"], {"COCOTB_TEST_FILTER": r"^m\.(a)$"}, ("TESTCASE",)),
        ("2.1.0", None, [], {}, ("COCOTB_TEST_FILTER", "TESTCASE")),  # no tests: whole module
    ],
    ids=["2.1", "1.x", "2.0", "probe-failure", "no-tests"],
)
def test_selection_dialect_follows_the_installed_cocotb(
    tmp_path, cocotb_config, fake_launch, version, libpath, tests, present, absent
):
    cocotb_config(version, libpath)
    _run(tmp_path, tests=tests)
    env = fake_launch.launches[0].env
    assert {key: env.get(key) for key in present} == present
    assert not set(absent) & set(env)


@pytest.mark.parametrize(
    ("eda_tool", "libpath", "plusargs", "vcd", "expected"),
    [
        (
            "icarus",
            None,
            ["verbose", "+already"],
            False,
            [*_VVP, _SO, _IMG, "+verbose", "+already"],
        ),
        ("icarus", None, [], True, [*_VVP, _SO, _IMG, "+trace"]),
        (
            "icarus",
            _EXTENSIONLESS_LIB,  # cocotb 2.0: vvp resolves the extension itself
            [],
            False,
            [*_VVP, _EXTENSIONLESS_LIB, _IMG],
        ),
        ("verilator", None, ["+p"], False, ["{build}/Vtop", "+p"]),  # Vtop, never V<toplevel>
        ("verilator", None, [], True, ["{build}/Vtop", "--trace", "--trace-file", "dump.vcd"]),
    ],
    ids=["icarus", "icarus-trace", "icarus-2.0", "verilator", "verilator-trace"],
)
def test_run_command_golden(
    tmp_path, cocotb_config, fake_launch, vvp, eda_tool, libpath, plusargs, vcd, expected
):
    cocotb_config("2.0.1" if libpath else "2.1.0", libpath)
    _run(tmp_path, eda_tool, plusargs=plusargs, vcd=vcd, expected_trace_scope="tb")
    build = (tmp_path / "build").resolve()
    assert fake_launch.launches[0].command == [
        part.format(vvp=vvp, build=build) for part in expected
    ]


@pytest.mark.parametrize(
    ("eda_tool", "path_empty", "messages"),
    [
        ("icarus", True, ("sandbox image predates cocotb support", "booley bootstrap --update")),
        ("icarus", False, ("no built cocotb sim",)),
        ("verilator", False, ("no built cocotb sim",)),
    ],
    ids=["missing-config", "unbuilt-icarus", "unbuilt-verilator"],
)
def test_prerun_failures_are_infrastructure_not_a_verdict(
    tmp_path, monkeypatch, fake_launch, capsys, eda_tool, path_empty, messages
):
    """SETUP-F-41b: nothing ran, so the nonzero exit is not a verdict a grader may score."""
    if path_empty:
        (tmp_path / "empty").mkdir()
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert _run(tmp_path, eda_tool, image=path_empty) == 1
    out = capsys.readouterr().out
    assert all(message in out for message in messages)
    assert SIM_INFRA_ERROR_PREFIX in out
    build = tmp_path / "build"
    assert SIM_INFRA_ERROR_PREFIX in (build / "run.log").read_text(encoding="utf-8")
    # result.json records the inconclusive verdict (never a silent pass).
    result = (build / "result.json").read_text(encoding="utf-8")
    assert '"passed": false' in result
    assert '"inconclusive": true' in result
    assert fake_launch.launches == []


def test_stale_results_xml_removed_before_run(tmp_path: Path):
    # A stale results.xml from a previous run must never feed verdicts: the
    # run-half deletes it before launching (missing binary aborts right after).
    build = tmp_path / "build"
    build.mkdir()
    stale = build / crun.RESULTS_XML_NAME
    stale.write_text("<testsuites/>")
    stale_json = build / crun.FULL_RESULTS_JSON_NAME
    stale_json.write_text('{"state":"ok","tests":[{"name":"old_pass"}]}')
    _run(tmp_path, "verilator", image=False)
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
        # A record attempt's evidence root: the adapter writes there, never into the bundle.
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
        "work_dir": tmp_path / "out",
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


def test_stall_diagnosis_replaces_the_useless_missing_xml_detail(tmp_path, fake_launch, capsys):
    """F-25: a frozen run loop names itself instead of 'results.xml not found'.

    The guard killed the sim before cocotb wrote any XML, so the parser can only
    report the symptom. The abort reason in the captured output is the cause.
    """
    from booley.flows.sim.run_guard import format_sim_time_stall

    fake_launch.output = [
        "     0.00ns INFO     cocotb.regression   running test_ravenoc\n",
        f"ERROR: {format_sim_time_stall(180)}\n",
    ]
    assert _run(tmp_path, tests=["test_ravenoc"]) == 1
    out = capsys.readouterr().out
    assert "run-loop version mismatch" in out
    # The verdict line quotes the promoted detail, not "results.xml not found".
    assert "results.xml not found" not in out.split("INCONCLUSIVE")[-1]


def test_the_launch_carries_the_command_environment_and_guard_inputs(tmp_path, fake_launch):
    identity = _identity(tmp_path, ("t1",))
    _run(
        tmp_path,
        "verilator",
        tests=["t1"],
        timeout=77,
        max_rundir_bytes=1234,
        sim_time_grace_s=9.5,
        runtime_inputs=("mem.hex",),
        transport=identity,
    )
    (launch,) = fake_launch.launches
    build = (tmp_path / "build").resolve()
    assert callable(launch.on_line)
    assert replace(launch, on_line=None) == SimulatorLaunch(
        "cocotb", [str(build / "Vtop")], build, launch.env, 77, build, 1234, ("mem.hex",), 9.5
    )
    assert launch.env["COCOTB_TEST_FILTER"] == r"^m\.(t1)$"


def test_partial_cocotb_abort_preserves_completed_test_and_publishes_termination(
    tmp_path, fake_launch
) -> None:
    """#838: an abort keeps the test that completed and marks the rest by the abort."""
    identity = _identity(tmp_path, ("done", "later"))

    def write_results(launch):
        Path(launch.env["COCOTB_RESULTS_FILE"]).write_text(
            '<testsuites><testsuite><testcase name="done" classname="test_demo" time="0.1"/>'
            "</testsuite></testsuites>",
            encoding="utf-8",
        )

    fake_launch.during = write_results
    fake_launch.returncode = -15
    fake_launch.termination = RunTermination(
        "sim_time_stall", "simulator time did not advance", "infrastructure"
    )
    _run(tmp_path, tests=["done", "later"], transport=identity)

    result = read_adapter_result(identity)
    assert result.termination == "sim_time_stall"
    assert result.inconclusive is False
    assert result.simulator_returncode == -15
    assert [(test.name, test.verdict, test.termination) for test in result.test_results] == [
        ("done", "pass", "completed"),
        ("later", "fail", "sim_time_stall"),
    ]


def test_live_exception_log_before_cocotb_child_completes(tmp_path, monkeypatch):
    """#1311: real streaming writes diagnostics to the evidence root while the child waits."""
    from concurrent.futures import ThreadPoolExecutor

    from booley.flows.run_log import begin_run_log
    from booley.flows.sim.backends import shared

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    cwd = tmp_path / "child"
    cwd.mkdir()
    release = tmp_path / "release"
    build = tmp_path / "build"
    build.mkdir()
    vtop = build / "Vtop"
    vtop.write_bytes(
        f'#!/bin/sh\necho "RuntimeError: live failure"\nwhile [ ! -e "{release}" ]; do sleep 0.05; done\n'
        'echo "final diagnostic"\n'.encode()
    )
    vtop.chmod(0o755)
    log = begin_run_log(evidence, flow="sim", target="cocotb", run="live-test")
    monkeypatch.setattr(shared, "RUN_LOG_PROGRESS_INTERVAL_S", 0)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _run,
            tmp_path,
            "verilator",
            image=False,
            work_dir=evidence,
            run_cwd=cwd,
            timeout=30,
            sim_time_grace_s=0,
        )
        try:
            deadline = time.monotonic() + 10
            while (
                "RuntimeError: live failure" not in log.read_text() and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            contents = log.read_text()
            assert "RuntimeError: live failure" in contents
            assert not future.done()
            assert "run=live-test" in contents and "run in progress" in contents
            assert not (cwd / "run.log").exists()
        finally:
            release.write_text("go")
            future.result(timeout=20)
    assert "final diagnostic" in log.read_text()


@pytest.mark.parametrize(
    ("eda_tool", "backend", "tail"),
    [
        ("icarus", "iverilog", ["+trace"]),
        ("verilator", "verilator", ["--trace", "--trace-file", "dump.vcd"]),
    ],
)
def test_traced_cocotb_status_reports_simulator(tmp_path, fake_launch, eda_tool, backend, tail):
    """#1435: the trace status names the simulator that ran, not a hard-coded one."""
    _run(tmp_path, eda_tool, vcd=True, expected_trace_scope="counter", tests=["test_count"])
    assert fake_launch.launches[0].command[-len(tail) :] == tail
    # No dump was produced, so finalize_trace wrote the incident status.
    status = json.loads((tmp_path / "build" / "trace_status.json").read_text())
    assert status["backend"] == backend
    assert status["trace_scope"] == "counter"


def test_rtl_fatal_failure_text_names_assertion_fatal_messages(tmp_path, fake_launch, capsys):
    """Cocotb failure text counts ``$fatal`` lines as messages, not SVA assertions (#2807)."""
    identity = _identity(tmp_path)
    fake_launch.during = lambda launch: Path(launch.env["COCOTB_RESULTS_FILE"]).write_text(
        '<testsuites><testsuite><testcase name="t" classname="m" time="0.1"/></testsuite>'
        "</testsuites>",
        encoding="utf-8",
    )
    fake_launch.output = ["%Fatal: assertion failed in tb\n"]
    assert _run(tmp_path, tests=["t"], transport=identity) == 1
    out = capsys.readouterr().out
    assert "cocotb sim FAILED (0/1 tests failed, 1 assertion/fatal error message)" in out
    assert "SVA" not in out
    (test,) = read_adapter_result(identity).test_results
    assert test.detail == "1 assertion/fatal error message"


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


@pytest.mark.parametrize("case", list(_GOLDEN_CASES))
def test_adapter_result_and_transports_match_pre_refactor_goldens(
    tmp_path, fake_launch, capsys, monkeypatch, case
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
    xml, selection, verbosity, line, failure_kind, verdicts, notes = _GOLDEN_CASES[case]
    fake_launch.output = []
    fake_launch.during = lambda launch: Path(launch.env["COCOTB_RESULTS_FILE"]).write_text(
        xml, encoding="utf-8"
    )
    identity = _identity(tmp_path, selection)
    work = tmp_path / "work"

    _run(tmp_path, tests=selection, result_verbosity=verbosity, transport=identity, work_dir=work)

    result = read_adapter_result(identity)
    assert (result.passed, result.inconclusive, result.failure_kind) == (False, True, failure_kind)
    assert result.tests == tuple(selection or ("done", "boom", "skip_me"))
    assert tuple((t.name, t.verdict, t.detail) for t in result.test_results) == verdicts
    assert result.diagnostics == notes
    stdout_lines = capsys.readouterr().out.splitlines()
    if line is not None:
        assert [row for row in stdout_lines if row.startswith("[COCOTB_RESULTS]")] == [line]
        assert (work / "cocotb_results.json").read_text(encoding="utf-8") == _RESULTS_JSON


@pytest.mark.parametrize("prefix", ["", "tests.counter."])
def test_cocotb_transport_preserves_partial_timeout_progress(
    tmp_path, fake_launch, prefix
) -> None:
    """PR #1465: a timed-out run publishes the progress its output reached."""
    identity = _identity(tmp_path, ("done", "active", "later"))
    fake_launch.output = [
        f"0.00ns INFO cocotb.regression running {prefix}done (1/3)\n",
        f"1.00ns INFO cocotb.regression {prefix}done passed\n",
        f"1.00ns INFO cocotb.regression running {prefix}active (2/3)\n",
    ]
    fake_launch.returncode = -15
    fake_launch.termination = RunTermination("timeout", "deadline expired", "timeout")

    _run(tmp_path, tests=["done", "active", "later"], transport=identity)

    result = read_adapter_result(partial_result_identity(identity))
    assert result.failure_kind == "timeout"
    assert [(item.name, item.verdict) for item in result.test_results] == [
        ("done", "pass"),
        ("active", "timeout"),
        ("later", "timeout"),
    ]


@pytest.mark.parametrize("prefix", ["", "tests.counter."])
def test_default_cocotb_partial_transport_discovers_current_attempt_names(
    tmp_path, fake_launch, prefix
) -> None:
    """PR #1465: with no selection, the partial names come from the current results.xml."""
    identity = _identity(tmp_path, ())
    fake_launch.during = lambda launch: Path(launch.env["COCOTB_RESULTS_FILE"]).write_text(
        "<testsuite><testcase name='done'/><testcase name='active'>"
        "<failure message='interrupted'/></testcase>"
        "<testcase name='later'><skipped/></testcase></testsuite>",
        encoding="utf-8",
    )
    fake_launch.output = [
        f"0.00ns INFO cocotb.regression running {prefix}done (1/3)\n",
        f"1.00ns INFO cocotb.regression {prefix}done passed\n",
        f"1.00ns INFO cocotb.regression running {prefix}active (2/3)\n",
    ]
    fake_launch.returncode = -15
    fake_launch.termination = RunTermination("timeout", "deadline expired", "timeout")

    _run(tmp_path, transport=identity)

    result = read_adapter_result(partial_result_identity(identity))
    assert result.tests == ("done", "active", "later")
    assert [test.verdict for test in result.test_results] == ["pass", "timeout", "timeout"]


@pytest.mark.parametrize(
    ("status", "output", "returncode", "verdict", "kind"),
    [
        ("pass", [], 0, "inconclusive", "artifact"),
        ("fail", [], 0, "fail", "design"),
        ("pass", ["$error assertion\n"], 0, "fail", "design"),
        ("pass", [], 1, "fail", "design"),
    ],
    ids=["pass", "fail", "sva-error", "nonzero-exit"],
)
def test_cocotb_trace_incident_preserves_functional_verdicts(
    tmp_path, fake_launch, status, output, returncode, verdict, kind
) -> None:
    """A missing waveform downgrades only a passing test, never a functional failure."""
    identity = _identity(tmp_path, ("reset",))
    failure = '<failure message="boom"/>' if status == "fail" else ""
    fake_launch.during = lambda launch: Path(launch.env["COCOTB_RESULTS_FILE"]).write_text(
        f'<testsuites><testsuite><testcase name="reset" classname="m" time="0.1">{failure}'
        "</testcase></testsuite></testsuites>",
        encoding="utf-8",
    )
    fake_launch.output = output
    fake_launch.returncode = returncode

    _run(tmp_path, vcd=True, expected_trace_scope="tb", tests=["reset"], transport=identity)

    result = read_adapter_result(identity)
    assert result.test_results[0].verdict == verdict
    assert result.failure_kind == kind
