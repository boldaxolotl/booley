"""Human CLI observation contracts without licensed EDA prerequisites."""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.flows.builtin_cli import build_cli_parser, build_parser, execute_cli, parse_request
from booley.flows.cli_selection import INVOCATION_ORIGIN_ENV
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintFlow
from booley.flows.observed_process import communicate_observed
from booley.flows.sim.flow import SimulateFlow
from booley.flows.source_paths import ProjectSourcePaths
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.flows.terminal_progress import (
    ProgressText,
    TerminalProgress,
    announce_unit,
    current_progress,
    observe_source_names,
    progress_lane,
)
from booley.runtime.endpoint_execution import EndpointOutcome, ExecutionResult
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S, run_bounded_child
from tests.time_budget import hang_guard_s, scaled

BUILTINS = (AsicSynthesizeFlow, FpgaImplFlow, SimulateFlow, LintFlow)
LINT_TARGETS = ("lint_a", "lint_b")
BUILD_NAMES = {"src/c/top.v": "rtl/top.v"}


class Screen(io.StringIO):
    def __init__(self, *, tty=False):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


@pytest.fixture(autouse=True)
def _terminal(monkeypatch):
    """A capable terminal without colors; non-TTY observers ignore both settings."""
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr("booley.presentation.colors.COLORS_ENABLED", False)


def observer(
    root,
    *,
    flow="sim",
    stream=None,
    tty=False,
    clock=None,
    width=120,
    height=24,
    target="sim_demo",
    **kwargs,
):
    """One constructor; a TTY or a clock also pins the clock and the window size."""
    if tty or clock is not None:
        kwargs["clock"] = clock or (lambda: 0.0)
        kwargs.setdefault("dimensions", lambda: os.terminal_size((width, height)))
    return TerminalProgress(flow, target, root, stream=stream or Screen(tty=tty), **kwargs)


def boundary(flow, command, **options):
    """Launch *command* through the Flow's boundary, as every built-in Flow does."""
    # Private seam: a built-in Flow launches a tool only from its run step, which needs that
    # Flow's EDA tool (Verilator, Yosys, Vivado, a simulator); the boundary is the shared
    # launcher every Flow uses.
    return flow._execute_boundary(command, **options)


def run_observed(sink, script, *, timeout=SUBPROCESS_TIMEOUT_S, encoding=None, **popen):
    """Run a Python child through ``communicate_observed``; returns its capture and process."""
    sink.begin_command()
    with subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding=encoding,
        start_new_session=os.name != "nt",
        **popen,
    ) as process:
        return communicate_observed(process, sink, timeout=timeout), process


@contextmanager
def frozen_worker(sink):
    """Install *sink* with its worker stopped; yields the pump that renders its queue."""
    # Private seam: TerminalProgress renders on its own worker thread and has no public
    # synchronous flush; the test stops the worker and pumps it, so records and frames
    # come in a fixed order.
    with patch.object(sink, "_run"), sink.installed():
        yield sink._poll
        sink._drain_end()


def progress_logs(text):
    return [
        (match[1], match[2], Path(match[3]))
        for match in re.finditer(r"Target=(\S+) stage=(.*?) elapsed=\d+s log=(\S+)", text)
    ]


@pytest.mark.parametrize(
    "flow_type",
    [LintFlow, SimulateFlow, AsicSynthesizeFlow, FpgaImplFlow],
    ids=["lint", "sim", "synth", "fpga"],
)
def test_builtin_run_log_registration_preserves_target(tmp_path, flow_type):
    flow = flow_type()
    sink = observer(tmp_path, flow=flow.name)
    sink.stage_changed("candidate: configure", target="owner")
    sink.stage_changed("baseline: checking", target="other")
    with sink.installed():
        # Private seam: the prepare half opens run.log only inside a Flow run, which needs the
        # Flow's EDA tool; lint reaches it through the CLI in
        # test_two_target_lint_progress_log_ownership.
        flow._open_run_log("owner", tmp_path / "owner")
    records = progress_logs(sink.stream.getvalue())
    assert [(owner, path) for owner, _, path in records] == [
        ("owner", tmp_path / "owner" / "run.log")
    ], "run log owner mismatch"
    assert records[0][1] == "candidate: configure"
    assert (sink.target, sink.stage) == ("other", "baseline: checking")


def test_progress_footer_preserves_each_log_owner(tmp_path):
    sink = observer(tmp_path)
    paths = {target: tmp_path / target / "run.log" for target in ("a", "b")}
    for target, path in paths.items():
        path.parent.mkdir()
        path.touch()
        sink.stage_changed(
            ProgressText("candidate: 1m 01s", record_text="candidate: 61.2s"), target=target
        )
        sink.log_path_known(path)
    sink.stage_changed("baseline: configure", target="a")
    sink.stage_changed("baseline: checking", target="b")
    sink.stream.seek(0)
    sink.stream.truncate()
    sink.footer()
    records = progress_logs(sink.stream.getvalue())
    assert [(target, path) for target, _, path in records] == list(paths.items()), (
        "footer log owner mismatch"
    )
    assert [stage for _, stage, _ in records] == ["candidate: 61.2s"] * 2
    assert (sink.target, sink.stage) == ("b", "baseline: checking")


@pytest.fixture
def lint_pair(tmp_path, monkeypatch):
    """Two lint Targets resolved without Verilator; returns ``build(boundary) -> LintFlow``."""
    from booley.fusesoc import fusesoc_registry

    monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
    monkeypatch.delenv(INVOCATION_ORIGIN_ENV, raising=False)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setenv("LINES", "24")
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n")
    (tmp_path / "demo.core").write_text(
        "CAPI=2:\nname: ::demo:0\nfilesets:\n  rtl:\n"
        "    files: [top.sv]\n    file_type: systemVerilogSource\ntargets:\n"
        + "".join(
            f"  {target}:\n    flow: lint\n    default_tool: verilator\n"
            "    flow_options: {tool: verilator}\n    filesets: [rtl]\n    toplevel: top\n"
            for target in LINT_TARGETS
        )
    )

    def resolve(target, *, build_root, library_plan=None, **_kwargs):
        return fusesoc_registry.ResolvedTarget(
            name=target.selector,
            vlnv=target.vlnv,
            toplevel="top",
            eda_tool="verilator",
            configured_eda_tool="verilator",
            parameters={},
            build_root=build_root,
            edam_path=build_root / "demo.eda.yml",
            files=(fusesoc_registry.ResolvedFile(str(source), "systemVerilogSource"),),
        )

    monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", resolve)

    def build(boundary):
        flow = LintFlow()
        # Private seam: Verilator is not on the test host; the boundary command is the lint
        # Flow's only tool launch, and the CLI run around it is public.
        monkeypatch.setattr(flow, "_execute_boundary", boundary)
        return flow

    return build


def test_two_target_lint_progress_log_ownership(tmp_path, lint_pair, capsys):
    from booley.core.build_paths import work_root_for
    from booley.flows.base import SubprocessResult

    flow = lint_pair(lambda _: SubprocessResult(0))
    result = execute_cli(flow, ["--work-dir", str(tmp_path), "--target", "lint_a,lint_b"])
    assert result.exit_code == 0, result.outcome.report_text
    records = progress_logs(capsys.readouterr().err)
    expected = {
        work_root_for(tmp_path, "lint", target) / "run.log": target for target in LINT_TARGETS
    }
    assert records
    assert all(owner == expected[path] for owner, _, path in records), (
        "progress log target mismatch"
    )
    for target in LINT_TARGETS:
        stages = [stage for owner, stage, _ in records if owner == target]
        assert stages == ["preparing", "configure", "configure"]
        artifact = result.outcome.detail["artifacts"][f"log_{target}"]
        assert Path(artifact).parts[-2:] == (target, "run.log")
        assert (tmp_path / artifact).is_file()


def wait_until(predicate, *, timeout=SUBPROCESS_TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("live observation deadline expired")


@pytest.mark.parametrize("source", ["pipe", "file"])
def test_live_output_and_transcript_visible_before_exit(tmp_path, source):
    """Output reaches the screen and transcript while the child runs; a stale log is ignored."""
    release = tmp_path / "release"
    log = tmp_path / "yosys.log"
    log.write_text("old verdict\n")
    line = "live first line" if source == "pipe" else "fresh synthesis line"
    show = (
        f"print({line!r},flush=True)"
        if source == "pipe"
        else f"pathlib.Path({str(log)!r}).write_text({line + chr(10)!r})"
    )
    script = (
        f"import pathlib,time; {show}; p=pathlib.Path({str(release)!r}); "
        "exec('while not p.exists(): time.sleep(0.01)')"
    )
    console = Screen()
    sink = observer(
        tmp_path, flow="sim" if source == "pipe" else "synth", stream=console, tool_output=True
    )
    if source == "file":
        sink.watch([log])
    results = []
    with sink.installed():
        sink.begin_command("simulation")
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        thread = threading.Thread(
            target=lambda: results.append(
                communicate_observed(process, sink, timeout=SUBPROCESS_TIMEOUT_S)
            )
        )
        thread.start()
        try:
            wait_until(lambda: line in console.getvalue())
            assert "old verdict" not in console.getvalue()
            assert process.poll() is None
            assert sink.log_path is not None
            assert line.encode() in sink.log_path.read_bytes()
        finally:
            release.touch()
            thread.join(timeout=hang_guard_s())
            process.stdout.close()
            process.stderr.close()
    assert not thread.is_alive()
    assert results == [(line + "\n" if source == "pipe" else "", "", False)]
    assert current_progress() is None
    assert not (sink.directory / "active").exists()


@pytest.mark.parametrize("returncode", [0, 7])
def test_separate_flood_captures_match_communicate(tmp_path, returncode):
    script = (
        "import os,sys,threading; "
        "a=('λ\\r\\né\\rb\\n'*20000).encode(); b=b'err\\n'*40000; "
        "t=threading.Thread(target=lambda: os.write(2,b)); t.start(); "
        f"os.write(1,a); t.join(); sys.exit({returncode})"
    )
    command = [sys.executable, "-c", script]
    expected = run_bounded_child(command, timeout=SUBPROCESS_TIMEOUT_S)
    sink = observer(tmp_path)
    with sink.installed():
        (stdout, stderr, timed_out), process = run_observed(
            sink, script, timeout=SUBPROCESS_TIMEOUT_S, encoding="utf-8"
        )
    assert (stdout, stderr, process.returncode, timed_out) == (
        expected.stdout,
        expected.stderr,
        returncode,
        False,
    )


@pytest.mark.parametrize("encoding", ["utf-8", "latin-1"])
def test_split_unicode_newlines_and_partial_capture(tmp_path, encoding):
    raw = "café\r\nline\rtail".encode(encoding)
    script = f"import os,time; data={raw!r}; exec('for byte in data: os.write(1,bytes([byte])); time.sleep(0.001)')"
    sink = observer(tmp_path, tool_output=True)
    with sink.installed():
        (stdout, stderr, timed_out), _ = run_observed(sink, script, encoding=encoding)
    assert stdout == "café\nline\ntail"
    assert stderr == ""
    assert not timed_out
    assert "tail" in sink.stream.getvalue()


def test_invalid_capture_bytes_raise_and_reap(tmp_path):
    sink = observer(tmp_path)
    with sink.installed():
        sink.begin_command()
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"import os,time; os.write(1,b'\\xff'); time.sleep({scaled(30):g})",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        ) as process:
            with pytest.raises(UnicodeDecodeError):
                communicate_observed(process, sink, timeout=scaled(3))
            assert process.poll() is not None


@pytest.mark.parametrize("startup_delay", [0, 0.5])
def test_timeout_preserves_partial_output_and_reaps(tmp_path, startup_delay):
    ready = tmp_path / "child-ready"
    script = (
        "import os,pathlib,time; "
        f"time.sleep({startup_delay}); os.write(1,b'partial'); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
    )
    sink = observer(tmp_path)
    with sink.installed():
        sink.begin_command()
        with subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            try:
                wait_until(ready.exists)
                stdout, stderr, timed_out = communicate_observed(
                    process, sink, timeout=scaled(0.3)
                )
                assert process.poll() is not None
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=SUBPROCESS_TIMEOUT_S)
    assert (stdout, stderr, timed_out) == ("partial", "", True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX inherited pipe descriptor test")
def test_descendant_held_pipe_does_not_wait_forever(tmp_path):
    sink = observer(tmp_path)
    script = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c','import time; time.sleep({scaled(30):g})']); "
        "print('parent done',flush=True)"
    )
    started = time.monotonic()
    with sink.installed():
        # Generous timeout: a loaded CI runner can take over 0.3 s just to start
        # the interpreter. The held descendant sleeps 30 s (scaled), so the bound below
        # still proves the call does not wait for it.
        (stdout, _, timed_out), _ = run_observed(sink, script, timeout=scaled(3))
    assert time.monotonic() - started < scaled(15)
    assert stdout == "parent done\n"
    assert timed_out


@pytest.mark.parametrize("term,height", [("dumb", 24), ("xterm", 4)])
def test_incapable_terminal_uses_plain_records(tmp_path, monkeypatch, term, height):
    monkeypatch.setenv("TERM", term)
    now = [0.0]
    sink = observer(tmp_path, tty=True, clock=lambda: now[0], width=80, height=height)
    now[0] = 5
    sink.tick()
    assert "heartbeat" in sink.stream.getvalue()
    assert "\x1b" not in sink.stream.getvalue()


def test_controls_result_envelopes_and_oversized_partial_ui(tmp_path):
    console = Screen()
    sink = observer(tmp_path, stream=console, tool_output=True)
    sink.observe("stdout", b"\x1b[31mred\x1b[0m\x1b]0;title\x07\x00\n")
    sink.observe("stdout", b"[COCOTB_RESULTS] {}\nBOOLEY_BUILD_STAGE token=abc rc=0\n")
    for _ in range(20):
        sink.observe("stdout", b"x" * 65536)
    sink.end_command()
    value = console.getvalue()
    assert "red" in value
    assert "title" not in value
    assert "\x1b" not in value
    assert "COCOTB_RESULTS" not in value
    assert "BOOLEY_BUILD_STAGE" not in value
    assert max(map(len, value.splitlines())) < 2300


@pytest.mark.parametrize("operation", ["append", "truncate", "replace", "rewrite"])
def test_log_freshness_and_final_backlog(tmp_path, operation):
    path = tmp_path / "eda.log"
    path.write_bytes(b"old evidence\n")
    sink = observer(tmp_path, tool_output=True)
    sink.watch([path])
    fresh = b"fresh evidence\n" * 20000
    if operation == "append":
        with path.open("ab") as stream:
            stream.write(fresh)
    elif operation == "replace":
        replacement = tmp_path / "replacement"
        replacement.write_bytes(fresh)
        replacement.replace(path)
    elif operation == "truncate":
        path.write_bytes(b"new\n")
        fresh = b"new\n"
    else:
        path.write_bytes(fresh)
    sink.end_command()
    text = sink.stream.getvalue()
    assert "old evidence" not in text
    assert text.count(
        ": fresh evidence\n" if operation != "truncate" else ": new\n"
    ) == fresh.count(b"\n")


def test_missing_pending_file_and_temporary_transcript_survives_deletion(tmp_path):
    path = tmp_path / "baseline.log"
    sink = observer(tmp_path, tool_output=True)
    sink.watch([path], temporary=True)
    with sink.installed():
        sink.begin_command()
        path.write_bytes(b"baseline bytes\n")
        sink.end_command()
        path.unlink()
    sink.footer()
    assert b"baseline bytes" in sink.log_path.read_bytes()
    assert "log=" + str(path) not in sink.stream.getvalue().splitlines()[-1]


def test_observers_sharing_a_root_get_distinct_transcripts(tmp_path):
    first, second = observer(tmp_path), observer(tmp_path)
    with first.installed(), second.installed():
        first.begin_command()
        second.begin_command()
        assert first.log_path != second.log_path


@pytest.mark.parametrize("flow_type", BUILTINS)
def test_cli_flags_do_not_change_shared_schema(flow_type):
    flow = flow_type()
    before = build_parser(flow).format_help()
    for flag in ("-q", "--quiet"):
        request = parse_request(flow, ["--target", "demo", flag])
        assert not any(name.startswith("_console") for name in vars(request))
        assert flag in build_cli_parser(flow).format_help()
    if flow.name == "sim":
        assert parse_request(flow, ["--target", "demo", "--verbose", "--quiet"]).verbose
    for flag in ("-v", "--tool-output"):
        with pytest.raises(SystemExit) as exc:
            parse_request(flow, ["--target", "demo", flag])
        assert exc.value.code == 2
    assert before == build_parser(flow).format_help()


@pytest.mark.parametrize("mode", ["human", "quiet", "runtime", "transport", "dry-run"])
def test_cli_installs_only_at_human_adapter_boundary(tmp_path, monkeypatch, mode):
    from booley.flows.base import BuiltinFlow

    monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
    monkeypatch.delenv(INVOCATION_ORIGIN_ENV, raising=False)
    if mode == "runtime":
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
    if mode == "transport":
        monkeypatch.setenv(INVOCATION_ORIGIN_ENV, "transport")
    args = ["--target", "demo", "--work-dir", str(tmp_path)]
    if mode == "quiet":
        args.append("-q")
    if mode == "dry-run":
        args.append("--dry-run")
    observed = []

    def execute(flow):
        observed.append(current_progress())
        return ExecutionResult(0, EndpointOutcome())

    with patch.object(BuiltinFlow, "execute_prepared", execute):
        execute_cli(LintFlow(), args)
    assert (observed[0] is not None) == (mode == "human")
    assert current_progress() is None
    assert not (tmp_path / ".runtime" / "flow-console").exists()


@pytest.mark.parametrize("flow_type", BUILTINS)
def test_all_builtin_boundary_commands_are_actually_observed(tmp_path, flow_type):
    flow = flow_type()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "demo"])
    sink = observer(tmp_path, flow=flow.name, tool_output=True)
    with sink.installed():
        result = boundary(
            flow,
            [sys.executable, "-c", "print('observed boundary')"],
            timeout=SUBPROCESS_TIMEOUT_S,
        )
    assert result.returncode == 0
    assert result.stdout == "observed boundary\n"
    assert result.dispatched_unix > 0
    assert b"observed boundary" in sink.log_path.read_bytes()
    if flow.name != "synth":
        assert "observed boundary" in sink.stream.getvalue()


@pytest.mark.parametrize("flow_type", BUILTINS)
def test_module_entry_help_contains_cli_presentation_options(tmp_path, flow_type):
    result = run_bounded_child(
        [sys.executable, "-m", f"booley.flows.{flow_type.name}", "--help"],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    )
    assert result.returncode == 0
    assert "--quiet" in result.stdout
    assert "--tool-output" not in result.stdout
    assert result.stderr == ""


def test_pre_sim_observed_without_changing_evidence(tmp_path):
    from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
    from tests.target_test_support import make_target_handle

    def fire():
        evidence = run_pre_sim_commands(
            make_target_handle(tmp_path, "sim_demo"),
            test_names=("smoke",),
            build_root=tmp_path / "build",
            eda_tool="icarus",
            timeout_s=SUBPROCESS_TIMEOUT_S,
            resource_flags=(),
            commands=("printf 'hook out\\n'; printf 'hook err\\n' >&2",),
        )
        assert evidence is not None
        return (evidence.status, evidence.returncode, evidence.stdout_tail, evidence.stderr_tail)

    expected = fire()
    sink = observer(tmp_path, tool_output=True)
    with sink.installed():
        observed = fire()
    assert observed == expected
    assert expected[2] == "hook out\n"
    assert "hook out" in sink.stream.getvalue()
    assert "hook err" in sink.stream.getvalue()


def test_oversized_result_envelope_remains_filtered_across_chunks(tmp_path):
    sink = observer(tmp_path, tool_output=True)
    sink.observe("stdout", b"[COCOTB_RESULTS] " + b"x" * 65000)
    sink.observe("stdout", b"x" * 65000 + b"\npublic line\n")
    sink.end_command()
    assert "COCOTB_RESULTS" not in sink.stream.getvalue()
    assert "xxxx" not in sink.stream.getvalue()
    assert "public line" in sink.stream.getvalue()


def test_observer_failure_never_changes_capture(tmp_path):
    class BrokenConsole(Screen):
        def write(self, text):
            raise OSError("terminal unavailable")

    sink = observer(tmp_path, stream=BrokenConsole(), tool_output=True)
    with sink.installed():
        output, _ = run_observed(sink, "print('authoritative capture')")
    assert output == ("authoritative capture\n", "", False)
    assert sink.log_path is None


def prepare_synth_unit(flow, tmp_path, monkeypatch):
    # Private seam: Yosys and Vivado are not on the test host; the work unit is driven past its
    # configure step with the tool command replaced.
    from types import SimpleNamespace

    flow.parse_args(["--work-dir", str(tmp_path), "--target", "demo"])
    build = tmp_path / "synth"
    build.mkdir()
    plan = SimpleNamespace(
        build_dir=build, spec=SimpleNamespace(timing=SimpleNamespace(mode="logical"))
    )
    recipe = SimpleNamespace(
        command=["spec"],
        resolved=SimpleNamespace(files=(), build_root=tmp_path),
        project_paths=ProjectSourcePaths(BUILD_NAMES),
    )
    monkeypatch.setattr(flow, "_resolve_synth_recipe", lambda _: recipe)
    monkeypatch.setattr(flow, "_configure_synth", lambda *_: plan)
    script = (
        f"import pathlib; pathlib.Path({str(build / 'yosys.log')!r})"
        ".write_text('BOOLEY_STAGE: yosys\\nsrc/c/top.v:3: real file-only line\\n')"
    )
    monkeypatch.setattr(flow, "_synth_boundary_cmd", lambda _: [sys.executable, "-c", script])
    monkeypatch.setattr(
        flow, "_interpret_boundary_run", lambda *args, **_: (SimpleNamespace(), args[2].stdout)
    )
    return flow._run_single_config, build / "yosys.log", "src/c/top.v:3: real file-only line"


def prepare_fpga_unit(flow, tmp_path, monkeypatch):
    # Private seam: same as prepare_synth_unit, for Vivado.
    from booley.flows.fpga.flow import _PreparedFpgaCommand

    flow.parse_args(["--work-dir", str(tmp_path), "--target", "demo"])
    log = tmp_path / "demo.runs" / "impl_1" / "runme.log"
    script = (
        f"import pathlib; p=pathlib.Path({str(log)!r}); p.parent.mkdir(parents=True);"
        " p.write_text('src/c/top.v:3: fresh implementation line\\n')"
    )
    prepared = _PreparedFpgaCommand(
        [sys.executable, "-c", script],
        tmp_path,
        "",
        False,
        None,
        console_logs=(tmp_path / "vivado.log", log),
        project_paths=ProjectSourcePaths(BUILD_NAMES),
    )
    monkeypatch.setattr(flow, "_prepare_fpga_command", lambda _: prepared)
    return flow._run_single_target, log, "src/c/top.v:3: fresh implementation line"


@pytest.mark.parametrize(
    ("flow_type", "prepare", "first", "stages", "unit_stage", "line"),
    [
        (
            AsicSynthesizeFlow,
            prepare_synth_unit,
            "candidate: configure",
            ["candidate: synthesis"] * 3,
            "candidate: yosys",
            "src/c/top.v:3: real file-only line",
        ),
        (
            FpgaImplFlow,
            prepare_fpga_unit,
            None,
            ["candidate: Vivado running"] * 2,
            "candidate: implementation",
            "src/c/top.v:3: fresh implementation line",
        ),
    ],
    ids=["synth", "fpga"],
)
def test_work_unit_observes_registered_log(
    tmp_path, monkeypatch, flow_type, prepare, first, stages, unit_stage, line
):
    from booley.flows import terminal_progress

    flow = flow_type()
    run_unit, _, _ = prepare(flow, tmp_path, monkeypatch)
    sink = observer(tmp_path, flow=flow.name, tool_output=True)
    watch = terminal_progress.observe_logs

    def register(paths, *, target, temporary):
        sink.stage_changed("candidate: unrelated", target="other")
        watch(paths, target=target, temporary=temporary)

    monkeypatch.setattr(terminal_progress, "observe_logs", register)
    with sink.installed():
        run_unit("demo")
    records = progress_logs(sink.stream.getvalue())
    assert all(target == "demo" for target, _, _ in records)
    if first:
        assert records[0][1] == first
    assert [stage for _, stage, _ in records[1:]] == stages
    assert unit_stage in sink.stream.getvalue()
    assert line.replace("src/c/", "rtl/") in sink.stream.getvalue()
    assert "src/c/" not in sink.stream.getvalue()
    assert line.encode() in sink.log_path.read_bytes()


def test_keyboard_interrupt_reaps_and_preserves_original_exception(tmp_path, monkeypatch):
    from booley.flows import observed_process

    def interrupt(*_args):
        raise KeyboardInterrupt

    # Private seam: the interrupt must land while the capture waits; replacing the wait is the
    # only deterministic place.
    monkeypatch.setattr(observed_process, "_wait", interrupt)
    sink = observer(tmp_path)
    with sink.installed():
        sink.begin_command()
        with subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            with pytest.raises(KeyboardInterrupt):
                communicate_observed(process, sink, timeout=3)
            assert process.poll() is not None
    assert sink.closed


def test_missing_executable_records_no_command_start(tmp_path):
    flow = LintFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "demo"])
    sink = observer(tmp_path, flow="lint", tool_output=True)
    with sink.installed():
        assert boundary(flow, [str(tmp_path / "missing-tool")]).returncode == -1
        assert "command started" not in sink.stream.getvalue(), (
            "unstarted command must not record a start"
        )
        assert sink.worker is None
        second = boundary(flow, [sys.executable, "-c", "print('second command')"])
        assert second.returncode == 0
    assert "second command" in sink.stream.getvalue()


def test_simulation_record_worker_inherits_invocation_observer(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from booley.flows.sim.record.scheduler import (
        BoundedSimulationRecordScheduler,
        ScheduledAttempt,
    )
    from booley.runtime.supervised_execution import SupervisedExecutionScope

    capacity = SimpleNamespace(
        managed=False,
        items_admitted=False,
        shutdown_requested=False,
        outer_permit=nullcontext,
        outer_scope=lambda stopped: SupervisedExecutionScope(None, tmp_path, stopped),
        cancellation_cause=lambda: None,
        work_item_claim=lambda _item, alone=False: None,
    )
    seen = []

    def execute(attempt, *_args):
        seen.append(current_progress())

    scheduler = BoundedSimulationRecordScheduler(
        capacity,
        allocate=lambda item: ScheduledAttempt(item, "attempt", 1, tmp_path, "demo"),
        execute=execute,
    )
    sink = observer(tmp_path, tool_output=True)
    with sink.installed():
        scheduler.run([{"test": "smoke"}])
    assert seen == [sink]


def test_internal_sim_result_records_filtered_but_retained_in_transcript(tmp_path):
    sink = observer(tmp_path, tool_output=True)
    data = (
        b'[SIM_SUMMARY] {"passed":true}\n[SIM_INFRA_ERROR] details\n'
        b"BOOLEY_BUILD_MILLISECONDS: 1\nBOOLEY_SIM_CPU_SECONDS: user=1\n"
        b"public line\n"
    )
    with sink.installed():
        sink.begin_command()
        sink.observe("stdout", data)
        sink.end_command()
    assert "SIM_SUMMARY" not in sink.stream.getvalue()
    assert "SIM_INFRA_ERROR" not in sink.stream.getvalue()
    assert "BOOLEY_BUILD" not in sink.stream.getvalue()
    assert "BOOLEY_SIM_CPU" not in sink.stream.getvalue()
    assert data in sink.log_path.read_bytes()


@pytest.mark.parametrize("lane", [False, True], ids=["serial", "lane"])
def test_relayed_line_names_project_path_and_transcript_keeps_raw(tmp_path, lane):
    raw = b"- src/acme_c_1/tb.v:266: Verilog $finish\nplain line\n"
    sink = observer(tmp_path)
    with sink.installed(), ExitStack() as stack:
        observe_source_names(ProjectSourcePaths({"src/acme_c_1/tb.v": "rtl/tb.v"}))
        if lane:
            # Labelled by a Target other than the one the names were registered under.
            stack.enter_context(progress_lane("t/1"))
            announce_unit("run", target="acme:c:1#sim")
        sink.begin_command()
        sink.observe("stdout", raw)
        sink.end_command()
    shown = sink.stream.getvalue()
    assert "rtl/tb.v:266: Verilog $finish" in shown
    assert "plain line" in shown
    assert "src/acme_c_1/" not in shown
    assert raw in sink.log_path.read_bytes()


def test_blocked_transcript_writer_cannot_block_process_capture(tmp_path):
    release = threading.Event()
    entered = threading.Event()
    sink = observer(tmp_path)
    sink.begin_command()
    real_log = sink.log

    class SlowLog:
        def write(self, data):
            entered.set()
            release.wait(timeout=hang_guard_s())
            real_log.write(data)

        def close(self):
            real_log.close()

    sink.log = SlowLog()
    deadline_s = scaled(3)
    started = time.monotonic()
    try:
        with subprocess.Popen(
            [sys.executable, "-c", "print('complete capture')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            assert communicate_observed(process, sink, timeout=deadline_s) == (
                "complete capture\n",
                "",
                False,
            )
        assert entered.is_set()
        assert time.monotonic() - started < deadline_s
        assert sink.log_path is None
        assert "incomplete/unavailable" in sink.stream.getvalue()
    finally:
        release.set()
        sink.worker.join(timeout=hang_guard_s())
        sink.close()
    assert not sink.worker.is_alive()
    assert not (sink.directory / "active").exists()


def test_concurrent_commands_keep_observing_after_one_finishes(tmp_path):
    sink = observer(tmp_path, tool_output=True)
    release = tmp_path / "release"
    script = (
        "import pathlib,time; print('second ready',flush=True); "
        f"p=pathlib.Path({str(release)!r}); deadline=time.monotonic()+{SUBPROCESS_TIMEOUT_S}; "
        "exec('while not p.exists() and time.monotonic()<deadline: time.sleep(0.01)'); "
        "print('second still live',flush=True); "
        f"p=pathlib.Path({str(release.with_name('done'))!r}); "
        "exec('while not p.exists() and time.monotonic()<deadline: time.sleep(0.01)')"
    )
    with sink.installed():
        sink.begin_command()
        second = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        result = []
        thread = threading.Thread(
            target=lambda: result.append(
                communicate_observed(second, sink, timeout=SUBPROCESS_TIMEOUT_S)
            )
        )
        thread.start()
        try:
            wait_until(lambda: "second ready" in sink.stream.getvalue())
            sink.begin_command()
            with subprocess.Popen(
                [sys.executable, "-c", "print('first done')"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            ) as first:
                assert (
                    communicate_observed(first, sink, timeout=SUBPROCESS_TIMEOUT_S)[0]
                    == "first done\n"
                )
            release.touch()
            wait_until(lambda: "second still live" in sink.stream.getvalue())
            assert second.poll() is None
        finally:
            release.with_name("done").touch()  # let the second command exit
            release.touch()
            thread.join(timeout=hang_guard_s())
            second.stdout.close()
            second.stderr.close()
    assert not thread.is_alive()
    assert result == [("second ready\nsecond still live\n", "", False)]


def test_parallel_pipe_sources_do_not_merge_partial_lines(tmp_path):
    sink = observer(tmp_path, tool_output=True)
    sink.observe("stdout:1", b"first ")
    sink.observe("stdout:2", b"second ")
    sink.observe("stdout:1", b"line\n")
    sink.observe("stdout:2", b"line\n")
    sink.end_command()
    assert "stdout:1: first line" in sink.stream.getvalue()
    assert "stdout:2: second line" in sink.stream.getvalue()


# Captured once from the existing transport writer with the fixed invocation id and timestamp.
DISPLAY_EVENTS = (
    b'{"type":"endpoint_progress","endpoint":"lint","line":"checking",'
    b'"timestamp":"2026-10-05T13:00:00Z","invocation_id":"fixed-invocation",'
    b'"display_scope":"developer"}\n'
    b'{"type":"endpoint_progress","endpoint":"lint","line":"done",'
    b'"timestamp":"2026-10-05T13:00:00Z","completion":true,'
    b'"invocation_id":"fixed-invocation","display_scope":"developer"}\n'
)


def test_agent_console_and_display_bytes_match_existing_transport(tmp_path, monkeypatch, capsys):
    from booley.flows.base import BuiltinFlow

    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("BOOLEY_DISPLAY_INVOCATION_ID", "fixed-invocation")
    monkeypatch.setattr(
        "booley.flows.endpoint_events.utc_now_rfc3339", lambda: "2026-10-05T13:00:00Z"
    )

    def execute(flow):
        assert flow.terminal_progress is None
        assert current_progress() is None
        flow.emit_progress("checking")
        flow.emit_completion("done")
        print("verdict unchanged")
        return ExecutionResult(7, EndpointOutcome())

    with patch.object(BuiltinFlow, "execute_prepared", execute):
        result = execute_cli(LintFlow(), ["--target", "demo"])
    captured = capsys.readouterr()
    assert captured.out == "verdict unchanged\n"
    assert captured.err == ""
    assert result.exit_code == 7
    # The writer opens the file in text mode, so Windows stores CRLF.
    written = (tmp_path / "display.jsonl").read_bytes().replace(b"\r\n", b"\n")
    assert written == DISPLAY_EVENTS


@pytest.mark.skipif(os.name != "nt", reason="Windows anonymous pipe compatibility")
def test_windows_capture_does_not_require_nonblocking_pipe_support(tmp_path, monkeypatch):
    def unavailable(*_args):
        raise OSError("nonblocking pipes unavailable on Python 3.11")

    monkeypatch.setattr(os, "set_blocking", unavailable)
    sink = observer(tmp_path)
    with sink.installed():
        output, _ = run_observed(sink, "print('Windows capture')")
    assert output == ("Windows capture\n", "", False)


def test_real_fast_tool_keeps_complete_transcript(tmp_path):
    sink = observer(tmp_path)
    # Freeze the writer to test real pipe backlog retention independently of
    # the advisory worker shutdown deadline and loaded-runner disk throughput.
    with frozen_worker(sink):
        (stdout, stderr, timed_out), _ = run_observed(
            sink, "import os; os.write(1,b'line\\n'*6000000)", timeout=SUBPROCESS_TIMEOUT_S
        )
    assert stderr == ""
    assert not timed_out
    assert sink.log_path is not None
    transcript = re.sub(
        rb"\n\[sim Target=sim_demo stage=preparing source=stdout:\d+\]\n",
        b"",
        sink.log_path.read_bytes(),
    )
    assert transcript == stdout.encode()


def test_coalesced_tail_keeps_stage_markers_inside_short_line_flood(tmp_path):
    sink = observer(tmp_path)
    sink.stage_changed("candidate: synthesis")
    sink.observe("stdout", b"line\n" * 1000 + b"BOOLEY_STAGE: yosys\n" + b"tail\n" * 1000)
    sink.end_command()
    assert sink.stage == "candidate: yosys"
    assert list(sink.tail) == ["tail"] * 6


@pytest.mark.parametrize("separator", ["/", "\\"])
@pytest.mark.parametrize(
    "directory,stage", [("synth_1", "synthesis"), ("impl_1", "implementation")]
)
def test_vivado_stage_identification_accepts_native_path_separators(
    tmp_path, separator, directory, stage
):
    sink = observer(tmp_path, flow="fpga")
    sink.stage_changed("candidate: Vivado running")
    source = separator.join(["build", "demo.runs", directory, "runme.log"])
    sink.observe(source, b"fresh Vivado output\n")
    sink.end_command()
    assert sink.stage == "candidate: " + stage


def test_tty_adaptive_intervals_use_monotonic_differences(tmp_path):
    now = [1000000.0]
    progress = observer(tmp_path, tty=True, clock=lambda: now[0], width=180, height=40)
    screen = progress.stream
    progress.last_output = now[0]
    now[0] += 3661
    progress.tick()
    assert "sim · sim_demo · preparing   1h 01m   last output 1h ago" in screen.getvalue()
    screen.seek(0)
    screen.truncate()
    progress.last_output = now[0] - 0.4
    now[0] += 0.3
    progress.tick()
    assert "1h 01m   last output just now" in screen.getvalue()


# --- Compact TTY status row (#2431) ---------------------------------------------------


def _cells(text):
    """Terminal cells of *text*, counted independently of production code."""
    import unicodedata

    return sum(
        0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
        for c in text
    )


def _tty_sink(tmp_path, monkeypatch, *, width=120, colors=False, target="top"):
    """A TTY observer with stubbed size, clock and color policy (no host dependence)."""
    monkeypatch.setattr("booley.presentation.colors.COLORS_ENABLED", colors)
    clock = [0.0]
    sink = observer(tmp_path, tty=True, clock=lambda: clock[0], width=width, target=target)
    return sink, clock


def _frame(sink):
    """Render one frame and return its physical rows (no clear prefix)."""
    sink.stream.seek(0)
    sink.stream.truncate()
    sink.rows = 0
    sink.tick()
    return sink.stream.getvalue().splitlines()


@pytest.mark.parametrize(
    ("last_output", "expected"),
    [
        (51.0, "sim · top · building   1m 03s   last output 12s ago"),
        (None, "sim · top · building   1m 03s"),
    ],
    ids=["output-seen", "no-output-yet"],
)
def test_tty_status_row_is_compact(tmp_path, monkeypatch, last_output, expected):
    sink, clock = _tty_sink(tmp_path, monkeypatch)
    sink.stage = "building"
    sink.last_output = last_output
    clock[0] = 63.0
    assert _frame(sink) == [expected]


def test_tty_status_row_styles_through_color_helpers(tmp_path, monkeypatch):
    from booley.presentation.colors import accent, amber, dim

    sink, clock = _tty_sink(tmp_path, monkeypatch, colors=True)
    sink.stage = "building"
    sink.last_output = 51.0
    clock[0] = 63.0
    assert _frame(sink) == [
        f"sim · {accent('top')} · {amber('building')}   {dim('1m 03s')}   last output 12s ago"
    ]


@pytest.mark.parametrize(
    ("width", "target"),
    [(80, "t" * 60), (30, "t" * 60), (60, "設計" * 20)],
    ids=["80", "30", "wide-chars"],
)
def test_tty_status_row_cuts_target_to_width(tmp_path, monkeypatch, width, target):
    sink, clock = _tty_sink(tmp_path, monkeypatch, width=width, target=target)
    sink.stage = "building"
    sink.last_output = 51.0
    clock[0] = 63.0
    (row,) = _frame(sink)
    assert _cells(row) <= width - 1
    assert row.startswith("sim · ")
    if width == 30:
        # Narrow terminals keep eight Target cells and drop trailing parts whole.
        assert row == "sim · ttttttt… · building"
    else:
        assert row.endswith("…" + " · building   1m 03s   last output 12s ago")
        assert _cells(row) == width - 1


@contextmanager
def _two_lanes(tmp_path, monkeypatch, *, colors, width=120, alpha="alpha/1", alpha_tail=b""):
    """Two lanes opened through the public API; yields the sink at 63 s with both open."""
    sink, clock = _tty_sink(tmp_path, monkeypatch, width=width, colors=colors, target="sim_demo")
    with frozen_worker(sink) as pump, progress_lane(alpha):
        announce_unit("simulation: alpha")
        clock[0] = 51.0
        sink.observe("stdout", b"alpha tail\n" + alpha_tail)
        pump()
        with progress_lane("beta_long/2"):
            announce_unit("building")
            sink.observe("stdout", b"beta tail\n")
            pump()
            clock[0] = 63.0
            yield sink


@pytest.mark.parametrize("colors", [False, True], ids=["plain", "colored"])
def test_tty_lane_rows_are_indented_and_aligned(tmp_path, monkeypatch, colors):
    from booley.presentation.colors import strip_ansi

    with _two_lanes(tmp_path, monkeypatch, colors=colors) as sink:
        frame = _frame(sink)
    assert [strip_ansi(row) for row in frame] == [
        "sim · sim_demo · 2 lanes   1m 03s   last output 12s ago",
        "  alpha/1       simulation: alpha   last output 12s ago",
        "    alpha tail",
        "  beta_long/2   building            last output 12s ago",
        "    beta tail",
    ]
    # Padding sits outside the styling, so colors never change the layout.
    assert any("\x1b[" in row for row in frame) is colors


def test_tty_lane_row_without_output_has_no_last_output_part(tmp_path, monkeypatch):
    sink, clock = _tty_sink(tmp_path, monkeypatch, target="sim_demo")
    with frozen_worker(sink), progress_lane("gamma/3"):
        announce_unit("building")
        clock[0] = 63.0
        rows = _frame(sink)
    assert rows[1] == "  gamma/3   building"


def test_tty_lane_names_are_cut_to_fit(tmp_path, monkeypatch):
    with _two_lanes(
        tmp_path, monkeypatch, colors=False, width=30, alpha="a" * 40, alpha_tail=b"x" * 60 + b"\n"
    ) as sink:
        rows = _frame(sink)
    assert all(_cells(row) <= 29 for row in rows), rows
    assert rows[1].startswith("  aaaaaaa…"), rows
    # Parts that do not fit are dropped whole, never cut mid-token.
    assert "last output" not in rows[1]
    assert rows[1].rstrip() == rows[1]


# Captured from the pre-#2431 implementation (extended on the #2885 base): redirected
# records must never change.
_RECORD = "[booley-progress] 2026-10-05T13:00:00Z sim Target=sim_demo"
# Records keep the full 32-hex attempt token that human text shortens (#2885).
_RESET_STAGE = "simulation: reset attempt=" + "0123456789abcdef" * 2
REDIRECTED_GOLDEN = "\n".join(
    [
        f"{_RECORD} stage=simulation elapsed=0s ",
        f"{_RECORD} stage=simulation elapsed=0s "
        "transcript=<ROOT>/.runtime/flow-console/invocation-ID/tool-output.log",
        f"{_RECORD} stage=simulation elapsed=0s command started",
        f"{_RECORD} stage=simulation elapsed=5s heartbeat (elapsed time; process health unknown)",
        f"{_RECORD} stage=simulation elapsed=5s output: serial one",
        f"{_RECORD} stage=simulation elapsed=5s output: serial two",
        f"{_RECORD} lane=alpha/1 stage=simulation: alpha elapsed=5s ",
        f"{_RECORD} lane=alpha/1 stage=simulation: alpha elapsed=5s command started",
        f"{_RECORD} stage=lanes=1 elapsed=10s heartbeat (elapsed time; process health unknown)",
        f"{_RECORD} lane=alpha/1 stage=simulation: alpha elapsed=10s output: lane line",
        f"{_RECORD} stage={_RESET_STAGE} elapsed=10s ",
        f"{_RECORD} stage={_RESET_STAGE} elapsed=10s log=<ROOT>/run.log",
        f"{_RECORD} stage={_RESET_STAGE} elapsed=10s "
        "transcript=<ROOT>/.runtime/flow-console/invocation-ID/tool-output.log",
        f"{_RECORD} stage={_RESET_STAGE} elapsed=10s log=<ROOT>/run.log",
        "",
    ]
)


def test_redirected_progress_records_are_byte_identical(tmp_path, monkeypatch):
    from booley.presentation.colors import strip_ansi

    monkeypatch.setattr("booley.presentation.colors.COLORS_ENABLED", True)
    monkeypatch.setattr(
        "booley.flows.terminal_progress.utc_now_rfc3339", lambda: "2026-10-05T13:00:00Z"
    )
    now = [0.0]
    sink = observer(tmp_path, clock=lambda: now[0], width=100)
    with frozen_worker(sink) as pump:
        announce_unit("simulation")
        sink.begin_command()
        sink.observe("stdout", b"serial one\nserial two\n")
        pump()
        now[0] = 5
        sink.tick()
        with progress_lane("alpha/1"):
            announce_unit("simulation: alpha")
            sink.begin_command()
            sink.observe("stdout", b"lane line\n")
            pump()
            now[0] = 10
            sink.tick()
        # A 32-hex attempt token, a registered log and a command end stay verbatim in records.
        announce_unit(f"simulation: reset attempt={'0123456789abcdef' * 2}")
        (tmp_path / "run.log").touch()
        sink.log_path_known(tmp_path / "run.log")
        sink.end_command()
    sink.footer()
    text = sink.stream.getvalue()
    assert "\x1b" not in text
    normalized = re.sub(
        r"invocation-\w+", "invocation-ID", text.replace(str(tmp_path), "<ROOT>")
    ).replace("\\", "/")
    assert normalized == REDIRECTED_GOLDEN
    # The same state on a terminal renders the compact row instead of the records.
    sink.stream = Screen(tty=True)
    sink.rows = 0
    sink.last_render = -1.0
    sink.tick()
    assert "sim · sim_demo · " in strip_ansi(sink.stream.getvalue())


# --- Compact permanent output on a terminal (#2885) ----------------------------------

TOKEN_40 = "0123456789abcdef0123456789abcdef01234567"
HEX_32 = re.compile(r"[0-9a-f]{32}")


def settled_lines(text):
    """Lines left on screen once every live-panel clear sequence has been applied."""
    lines = []
    for chunk in re.split(r"(\x1b\[\d+A\x1b\[J)", text):
        clear = re.fullmatch(r"\x1b\[(\d+)A\x1b\[J", chunk)
        if clear:
            del lines[-int(clear[1]) :]
        else:
            lines.extend(chunk.splitlines())
    return lines


def test_tty_sim_attempt_leaves_compact_summary(tmp_path):
    from booley.flows.base import SubprocessResult
    from booley.flows.sim.adapter_transport import AdapterTransportIdentity
    from booley.flows.sim.execution.attempt import AdapterAttemptRequest, execute_adapter_attempt

    sink = observer(tmp_path, tty=True)
    identity = AdapterTransportIdentity(
        "cocotb", TOKEN_40, "sim_demo", ("reset",), tmp_path / f"adapter-{TOKEN_40}.json"
    )
    request = AdapterAttemptRequest(("tool",), SUBPROCESS_TIMEOUT_S, identity, tmp_path)
    code = (
        "print('ALL TESTS PASSED.');"
        f"print('BOOLEY_EDA_FAILURE token={TOKEN_40} detail');"
        f"print('wrote ' + {str(identity.result_path)!r})"
    )

    def invoke(_command, *, timeout):
        (out, err, _), process = run_observed(sink, code, timeout=timeout)
        return SubprocessResult(process.returncode, out, err)

    with sink.installed():
        execute_adapter_attempt(invoke, request)
    sink.footer()

    text = sink.stream.getvalue()
    assert "[booley-progress]" not in text
    assert not HEX_32.search(text), "a full attempt token reached human text"
    short = TOKEN_40[:8]
    assert settled_lines(text) == [
        f"done   sim_demo · simulation: reset attempt={short}   0s",
        "    ALL TESTS PASSED.",
        f"    BOOLEY_EDA_FAILURE token={short} detail",
        f"    wrote {tmp_path / f'adapter-{short}.json'}",
        f"transcript: {sink.log_path}",
    ]
    assert TOKEN_40.encode() in sink.log_path.read_bytes(), "transcript must keep the full token"


def test_tty_crlf_child_output_leaves_no_blank_tail_rows(tmp_path):
    """A Windows child ends lines with CRLF; the terminal tail must not gain blank rows."""
    sink = observer(tmp_path, tty=True)
    code = "import sys; sys.stdout.buffer.write(b'one\\r\\n\\r\\ntwo\\r\\n'); sys.stdout.flush()"
    with sink.installed():
        run_observed(sink, code)
    sink.footer()

    assert settled_lines(sink.stream.getvalue())[1:3] == ["    one", "    two"]


def test_tty_stage_outside_command_shows_live_row(tmp_path):
    """Between commands a stage change writes no record and leaves no panel rows that
    later output would have to erase (the name is pinned by the #2885 plan)."""
    sink = observer(tmp_path, tty=True, target="top")
    sink.stage_changed("configure")
    sink.stream.write("verdict\n")  # the Flow prints its verdict before the observer closes
    sink.close()
    text = sink.stream.getvalue()
    assert "[booley-progress]" not in text
    assert settled_lines(text) == ["verdict"]


def test_tty_two_target_lint_leaves_compact_output(tmp_path, monkeypatch, lint_pair):
    from booley.core.build_paths import work_root_for
    from booley.flows.base import SubprocessResult

    def run_observed_child(_command):
        (out, err, _), process = run_observed(current_progress(), "print('lint tool ran')")
        return SubprocessResult(process.returncode, out, err)

    flow = lint_pair(run_observed_child)
    screen = Screen(tty=True)  # the Flow's stdout verdict and the observer share one terminal
    monkeypatch.setattr(sys, "stderr", screen)
    monkeypatch.setattr(sys, "stdout", screen)
    result = execute_cli(flow, ["--work-dir", str(tmp_path), "--target", "lint_a,lint_b"])
    assert result.exit_code == 0, result.outcome.report_text
    assert "[booley-progress]" not in screen.getvalue()
    logs = ", ".join(str(work_root_for(tmp_path, "lint", t) / "run.log") for t in LINT_TARGETS)
    lines = settled_lines(screen.getvalue())
    results = [
        line for line in lines if re.fullmatch(r"done   lint_[ab] · checking   [\d.]+s", line)
    ]
    assert [line.split()[1] for line in results] == ["lint_a", "lint_b"], lines
    assert lines.count("    lint tool ran") == 2
    assert "RESULT: PASS" in lines, "the stdout verdict must survive the observer closing"
    assert re.fullmatch(rf"transcript: .+ · logs: {re.escape(logs)}", lines[-1]), lines[-1]


@pytest.mark.parametrize(
    ("flow_type", "prepare"),
    [(AsicSynthesizeFlow, prepare_synth_unit), (FpgaImplFlow, prepare_fpga_unit)],
    ids=["synth", "fpga"],
)
def test_tty_watched_log_flows_leave_compact_output(tmp_path, monkeypatch, flow_type, prepare):
    flow = flow_type()
    run_unit, log, watched_line = prepare(flow, tmp_path, monkeypatch)
    sink = observer(tmp_path, tty=True, flow=flow.name, target="demo")
    with sink.installed():
        run_unit("demo")
    sink.footer()
    text = sink.stream.getvalue()
    assert "[booley-progress]" not in text
    lines = settled_lines(text)
    results = [line for line in lines if not line.startswith((" ", "transcript:"))]
    assert len(results) == 1, lines
    assert re.fullmatch(r"done   demo · candidate: [\w ]+   0s", results[0]), results
    assert f"    {watched_line}" in lines
    prefix = f"transcript: {sink.log_path} · logs: "
    assert lines[-1].startswith(prefix)
    assert lines[-1].removeprefix(prefix).split(", ")[-1] == str(log)


def scripted_session(sink):
    """Drive one serial command through the public hooks; returns after the footer."""
    with frozen_worker(sink) as pump:
        announce_unit(f"simulation: reset attempt={TOKEN_40}")
        sink.begin_command()
        sink.observe("stdout", f"BOOLEY_EDA_FAILURE token={TOKEN_40}\nplain line\n".encode())
        pump()
        sink.end_command()
    sink.footer()


def test_transcript_bytes_do_not_depend_on_terminal(tmp_path):
    transcripts = {}
    for name, tty in (("tty", True), ("redirected", False)):
        work = tmp_path / name
        work.mkdir()
        sink = observer(work, tty=tty, clock=lambda: 0.0)
        scripted_session(sink)
        transcripts[name] = sink.log_path.read_bytes()
    assert transcripts["tty"] == transcripts["redirected"]
    assert TOKEN_40.encode() in transcripts["tty"]


@pytest.mark.parametrize(
    ("script", "timeout", "status"),
    [
        ("print('ok')", SUBPROCESS_TIMEOUT_S, "done"),
        ("import sys; sys.exit(3)", SUBPROCESS_TIMEOUT_S, "exit 3"),
        ("import time; time.sleep(30)", scaled(0.3), "timed out"),
        (
            f"import os,time; os.write(1,b'\\xff'); time.sleep({scaled(30):g})",
            scaled(10),
            "stopped",
        ),
    ],
    ids=["done", "exit", "timeout", "capture-error"],
)
def test_tty_result_line_names_the_process_outcome(tmp_path, script, timeout, status):
    sink = observer(tmp_path, tty=True)
    sink.stage_changed("simulation")
    with sink.installed():
        if status == "stopped":  # invalid bytes on the pipe raise from the capture
            with pytest.raises(UnicodeDecodeError):
                run_observed(sink, script, timeout=timeout, encoding="utf-8")
        else:
            run_observed(sink, script, timeout=timeout, encoding="utf-8")
    assert settled_lines(sink.stream.getvalue())[0] == f"{status}   sim_demo · simulation   0s"


def test_tty_lane_keeps_its_first_failing_status(tmp_path):
    sink = observer(tmp_path, tty=True)
    with frozen_worker(sink), progress_lane("alpha/1"):
        for status in ("done", "exit 2", "timed out"):
            sink.begin_command()
            sink.end_command(status)
    assert settled_lines(sink.stream.getvalue()) == ["exit 2   alpha/1   0s"]


def test_tty_transcript_failure_prints_one_plain_line(tmp_path, monkeypatch):
    sink = observer(tmp_path, tty=True)
    monkeypatch.setattr(
        "booley.flows.terminal_progress.runtime_dir",
        lambda _: (_ for _ in ()).throw(PermissionError()),
    )
    with sink.installed():
        sink.begin_command()
        sink.end_command()
    sink.footer()
    text = sink.stream.getvalue()
    assert "[booley-progress]" not in text
    assert text.count("Observation transcript incomplete/unavailable") == 1
    assert settled_lines(text)[0].startswith("Observation transcript incomplete/unavailable")


def test_tty_window_shrink_keeps_compact_output(tmp_path):
    """A window that drops below the panel height mid-run draws nothing, not raw records."""
    now = [0.0]
    height = [24]
    sink = observer(
        tmp_path,
        tty=True,
        clock=lambda: now[0],
        dimensions=lambda: os.terminal_size((120, height[0])),
    )
    with frozen_worker(sink) as pump:
        sink.begin_command()
        sink.observe("stdout", b"kept line\n")
        pump()
        height[0], now[0] = 5, 10.0
        sink.tick()
        sink.end_command()
    text = sink.stream.getvalue()
    assert "[booley-progress]" not in text
    assert settled_lines(text) == ["done   sim_demo · preparing   10s", "    kept line"]


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (f"Paired Checkout -> {TOKEN_40}", f"Paired Checkout -> {TOKEN_40[:8]}"),
        ("data=" + "01" * 20, "data=" + "01" * 20),  # wide binary vector
        ("data=" + "0" * 40, "data=" + "0" * 40),  # zero vector
        ("data=" + "f" * 32, "data=" + "f" * 32),  # all-letter vector
    ],
    ids=["hash", "binary", "zero", "letters"],
)
def test_human_text_shortens_hashes_but_not_design_data(tmp_path, text, shown):
    sink = observer(tmp_path, tty=True)
    with sink.installed():
        announce_unit("baseline: " + text)  # the baseline stage reaches the live row
    sink.tick()
    assert "baseline: " + shown in sink.stream.getvalue()
    assert text == shown or text not in sink.stream.getvalue()


@pytest.mark.skipif(os.name == "nt", reason="needs a POSIX pty or FIFO")
@pytest.mark.parametrize("terminal", ["pty", "pipe"])
def test_raw_terminal_lines_reach_job_log_without_frames(tmp_path, monkeypatch, terminal):
    """Raw fd writes bypass a Standalone Job's tee: permanent lines are copied, redraws are not."""
    from booley.harness.standalone_job import JobLogStream

    monkeypatch.setenv("TERM", "xterm")
    reader, writer = os.openpty() if terminal == "pty" else os.pipe()
    log_path = tmp_path / "job.log"
    with os.fdopen(writer, "w") as screen, log_path.open("wb", buffering=0) as log:
        sink = observer(
            tmp_path,
            stream=JobLogStream(screen, log),
            clock=lambda: 10.0,
            dimensions=lambda: os.terminal_size((120, 24)),
        )
        sink.tick()  # a pty draws the live status panel; a pipe draws nothing
        assert (sink.rows > 0) == (terminal == "pty")
        # Private seam: the only public writers of a permanent line also redraw the panel or
        # print records, which the test must keep apart.
        sink._say("permanent \x1b[1mline\x1b[0m")
        sink.close()
    os.close(reader)
    assert log_path.read_text(encoding="utf-8") == "permanent line\n"
