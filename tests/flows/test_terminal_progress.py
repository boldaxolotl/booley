"""Human CLI observation contracts without licensed EDA prerequisites."""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.flows.builtin_cli import build_cli_parser, build_parser, execute_cli, parse_request
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintFlow
from booley.flows.observed_process import communicate_observed
from booley.flows.sim.flow import SimulateFlow
from booley.flows.synth.flow import AsicSynthesizeFlow
from booley.flows.terminal_progress import TerminalProgress, current_progress
from booley.runtime.endpoint_execution import EndpointOutcome, ExecutionResult

BUILTINS = (AsicSynthesizeFlow, FpgaImplFlow, SimulateFlow, LintFlow)


class Screen(io.StringIO):
    def __init__(self, *, tty=False):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


def observer(root, *, flow="sim", stream=None, **kwargs):
    return TerminalProgress(flow, "sim_demo", root, stream=stream or Screen(), **kwargs)


def wait_until(predicate, *, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("live observation deadline expired")


def test_pipe_output_and_transcript_visible_before_exit(tmp_path):
    release = tmp_path / "release"
    console = Screen()
    script = (
        "import pathlib,time; print('live first line',flush=True); "
        f"p=pathlib.Path({str(release)!r}); "
        "exec('while not p.exists(): time.sleep(0.01)')"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    sink = observer(tmp_path, stream=console, tool_output=True)
    results = []
    with sink.installed():
        sink.begin_command("simulation")
        thread = threading.Thread(
            target=lambda: results.append(communicate_observed(process, sink, timeout=5))
        )
        thread.start()
        try:
            wait_until(lambda: "live first line" in console.getvalue())
            assert process.poll() is None
            assert sink.log_path is not None
            assert b"live first line" in sink.log_path.read_bytes()
        finally:
            release.touch()
            thread.join(timeout=6)
            process.stdout.close()
            process.stderr.close()
    assert not thread.is_alive()
    assert results == [("live first line\n", "", False)]
    assert current_progress() is None
    assert not (sink.directory / "active").exists()


def test_file_only_live_output_before_exit_and_stale_log_ignored(tmp_path):
    log = tmp_path / "yosys.log"
    log.write_text("old verdict\n")
    release = tmp_path / "release"
    script = (
        "import pathlib,time; "
        f"pathlib.Path({str(log)!r}).write_text('fresh synthesis line\\n'); "
        f"p=pathlib.Path({str(release)!r}); "
        "exec('while not p.exists(): time.sleep(0.01)')"
    )
    console = Screen()
    sink = observer(tmp_path, flow="synth", stream=console, tool_output=True)
    sink.watch([log])
    with sink.installed():
        sink.begin_command("synthesis")
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        thread = threading.Thread(target=lambda: communicate_observed(process, sink, timeout=5))
        thread.start()
        try:
            wait_until(lambda: "fresh synthesis line" in console.getvalue())
            assert "old verdict" not in console.getvalue()
            assert process.poll() is None
            assert b"fresh synthesis line" in sink.log_path.read_bytes()
        finally:
            release.touch()
            thread.join(timeout=6)
            process.stdout.close()
            process.stderr.close()
    assert not thread.is_alive()


@pytest.mark.parametrize("returncode", [0, 7])
def test_separate_flood_captures_match_communicate(tmp_path, returncode):
    script = (
        "import os,sys,threading; "
        "a=('λ\\r\\né\\rb\\n'*20000).encode(); b=b'err\\n'*40000; "
        "t=threading.Thread(target=lambda: os.write(2,b)); t.start(); "
        f"os.write(1,a); t.join(); sys.exit({returncode})"
    )
    command = [sys.executable, "-c", script]
    expected = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    sink = observer(tmp_path)
    with sink.installed():
        sink.begin_command()
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            stdout, stderr, timed_out = communicate_observed(process, sink, timeout=5)
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
        sink.begin_command()
        with subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=encoding,
            start_new_session=True,
        ) as process:
            stdout, stderr, timed_out = communicate_observed(process, sink, timeout=3)
    assert stdout == "café\nline\ntail"
    assert stderr == ""
    assert not timed_out
    assert "tail" in sink.stream.getvalue()


def test_invalid_capture_bytes_raise_and_reap(tmp_path):
    sink = observer(tmp_path)
    with sink.installed():
        sink.begin_command()
        with subprocess.Popen(
            [sys.executable, "-c", "import os,time; os.write(1,b'\\xff'); time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        ) as process:
            with pytest.raises(UnicodeDecodeError):
                communicate_observed(process, sink, timeout=3)
            assert process.poll() is not None


def test_timeout_preserves_partial_output_and_reaps(tmp_path):
    sink = observer(tmp_path)
    with sink.installed():
        sink.begin_command()
        with subprocess.Popen(
            [sys.executable, "-c", "import os,time; os.write(1,b'partial'); time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            stdout, stderr, timed_out = communicate_observed(process, sink, timeout=0.3)
            assert process.poll() is not None
    assert (stdout, stderr, timed_out) == ("partial", "", True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX inherited pipe descriptor test")
def test_descendant_held_pipe_does_not_wait_forever(tmp_path):
    sink = observer(tmp_path)
    script = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print('parent done',flush=True)"
    started = time.monotonic()
    with sink.installed():
        sink.begin_command()
        with subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            stdout, _, timed_out = communicate_observed(process, sink, timeout=0.3)
    assert time.monotonic() - started < 3
    assert stdout == "parent done\n"
    assert timed_out


def test_silent_non_tty_heartbeats_and_bounded_new_lines(tmp_path):
    now = [100.0]
    console = Screen()
    sink = observer(tmp_path, stream=console, clock=lambda: now[0])
    sink.stage_changed("simulation")
    for index in range(10):
        sink.observe("stdout", f"line {index}\n".encode())
    # Public end-of-command drains pending observations independently of child output.
    now[0] += 5
    sink.end_command()
    first = console.getvalue()
    assert "heartbeat" in first
    assert "elapsed=5s" in first
    assert first.count("output:") == 6
    now[0] += 5
    sink.tick()
    second = console.getvalue()[len(first) :]
    assert "heartbeat" in second
    assert "output:" not in second
    assert "\x1b" not in console.getvalue()
    assert "\r" not in console.getvalue()


def test_tty_resize_rate_and_clear_before_footer(tmp_path, monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    now = [0.0]
    size = [os.terminal_size((60, 24))]
    screen = Screen(tty=True)
    sink = observer(tmp_path, stream=screen, clock=lambda: now[0], dimensions=lambda: size[0])
    sink.stage_changed("simulation")
    sink.tick()
    original = screen.getvalue()
    sink.tick()
    assert screen.getvalue() == original
    now[0] = 1
    size[0] = os.terminal_size((20, 24))
    sink.tick()
    assert "\x1b[" in screen.getvalue()
    sink.close()
    assert screen.getvalue().endswith("\x1b[J")
    assert sink.rows == 0


@pytest.mark.parametrize("term,height", [("dumb", 24), ("xterm", 4)])
def test_incapable_terminal_uses_plain_records(tmp_path, monkeypatch, term, height):
    monkeypatch.setenv("TERM", term)
    now = [0.0]
    sink = observer(
        tmp_path,
        stream=Screen(tty=True),
        clock=lambda: now[0],
        dimensions=lambda: os.terminal_size((80, height)),
    )
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


def test_unique_transcripts_and_disk_failure_is_advisory(tmp_path, monkeypatch):
    first, second = observer(tmp_path), observer(tmp_path)
    with first.installed(), second.installed():
        first.begin_command()
        second.begin_command()
        assert first.log_path != second.log_path
    failed = observer(tmp_path, tool_output=True)
    monkeypatch.setattr(
        "booley.flows.terminal_progress.runtime_dir",
        lambda _: (_ for _ in ()).throw(PermissionError()),
    )
    with failed.installed():
        failed.begin_command()
        failed.observe("stdout", b"still live\n")
        failed.end_command()
    failed.footer()
    assert "still live" in failed.stream.getvalue()
    assert failed.stream.getvalue().count("incomplete/unavailable") == 1
    assert "transcript=" not in failed.stream.getvalue()


@pytest.mark.parametrize("flow_type", BUILTINS)
def test_cli_flags_do_not_change_shared_schema(flow_type):
    flow = flow_type()
    before = [(action.dest, action.option_strings[:]) for action in build_parser(flow)._actions]
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
    assert before == [
        (action.dest, action.option_strings[:]) for action in build_parser(flow)._actions
    ]


@pytest.mark.parametrize("mode", ["human", "quiet", "runtime", "ticket", "dry-run"])
def test_cli_installs_only_at_human_adapter_boundary(tmp_path, monkeypatch, mode):
    from booley.flows.flow_session import FlowSession

    monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
    if mode == "runtime":
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
    args = ["--target", "demo", "--work-dir", str(tmp_path)]
    if mode == "quiet":
        args.append("-q")
    if mode == "dry-run":
        args.append("--dry-run")
    observed = []

    def execute(session):
        observed.append(current_progress())
        return ExecutionResult(0, EndpointOutcome())

    with patch.object(FlowSession, "execute_prepared", execute):
        execute_cli(LintFlow(), args, adapter=object() if mode == "ticket" else None)
    assert (observed[0] is not None) == (mode == "human")
    assert current_progress() is None
    assert not (tmp_path / ".runtime" / "flow-console").exists()


@pytest.mark.parametrize("flow_type", BUILTINS)
def test_all_builtin_boundary_commands_are_actually_observed(tmp_path, flow_type):
    flow = flow_type()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "demo"])
    sink = observer(tmp_path, flow=flow.name, tool_output=True)
    with sink.installed():
        result = flow._execute_boundary(
            [sys.executable, "-c", "print('observed boundary')"], timeout=3
        )
    assert result.returncode == 0
    assert result.stdout == "observed boundary\n"
    assert result.dispatched_unix > 0
    assert b"observed boundary" in sink.log_path.read_bytes()
    if flow.name != "synth":
        assert "observed boundary" in sink.stream.getvalue()


@pytest.mark.parametrize("flow_type", BUILTINS)
def test_module_entry_help_contains_cli_presentation_options(tmp_path, flow_type):
    result = subprocess.run(
        [sys.executable, "-m", f"booley.flows.{flow_type.name}", "--help"],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0
    assert "--quiet" in result.stdout
    assert "--tool-output" not in result.stdout
    assert result.stderr == ""


def test_pre_sim_observed_without_changing_evidence(tmp_path):
    from booley.flows.sim.execution.pre_sim import _run_pre_sim_process

    command = [
        sys.executable,
        "-c",
        "import os; os.write(1,b'hook out\\n'); os.write(2,b'hook err\\n')",
    ]
    expected = _run_pre_sim_process(command, cwd=tmp_path, env=os.environ, timeout=3)
    sink = observer(tmp_path, tool_output=True)
    with sink.installed():
        result = _run_pre_sim_process(command, cwd=tmp_path, env=os.environ, timeout=3)
    assert (result.stdout, result.stderr, result.returncode) == (
        expected.stdout,
        expected.stderr,
        expected.returncode,
    )
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
        sink.begin_command()
        with subprocess.Popen(
            [sys.executable, "-c", "print('authoritative capture')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            output = communicate_observed(process, sink, timeout=3)
    assert output == ("authoritative capture\n", "", False)
    assert sink.log_path is None


def test_synth_work_unit_observes_registered_stage_file(tmp_path, monkeypatch):
    from types import SimpleNamespace

    flow = AsicSynthesizeFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "demo"])
    build = tmp_path / "synth"
    build.mkdir()
    plan = SimpleNamespace(build_dir=build)
    monkeypatch.setattr(flow, "_resolve_synth_recipe", lambda _: SimpleNamespace(command=["spec"]))
    monkeypatch.setattr(flow, "_configure_synth", lambda *_: plan)
    script = f"import pathlib; pathlib.Path({str(build / 'yosys.log')!r}).write_text('BOOLEY_STAGE: yosys\\nreal file-only line\\n')"
    monkeypatch.setattr(flow, "_synth_boundary_cmd", lambda _: [sys.executable, "-c", script])
    monkeypatch.setattr(
        flow, "_interpret_boundary_run", lambda *args: (SimpleNamespace(), args[2].stdout)
    )
    sink = observer(tmp_path, flow="synth", tool_output=True)
    with sink.installed():
        flow._run_single_config("demo")
    assert "candidate: yosys" in sink.stream.getvalue()
    assert "real file-only line" in sink.stream.getvalue()
    assert b"real file-only line" in sink.log_path.read_bytes()


def test_fpga_work_unit_observes_pending_vivado_child_log(tmp_path, monkeypatch):
    from booley.flows.fpga.flow import _PreparedFpgaCommand

    flow = FpgaImplFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "demo"])
    log = tmp_path / "demo.runs" / "impl_1" / "runme.log"
    script = f"import pathlib; p=pathlib.Path({str(log)!r}); p.parent.mkdir(parents=True); p.write_text('fresh implementation line\\n')"
    prepared = _PreparedFpgaCommand(
        [sys.executable, "-c", script],
        tmp_path,
        "",
        False,
        console_logs=(tmp_path / "vivado.log", log),
    )
    monkeypatch.setattr(flow, "_prepare_fpga_command", lambda _: prepared)
    sink = observer(tmp_path, flow="fpga", tool_output=True)
    with sink.installed():
        flow._run_single_target("demo")
    assert "candidate: implementation" in sink.stream.getvalue()
    assert "fresh implementation line" in sink.stream.getvalue()
    assert b"fresh implementation line" in sink.log_path.read_bytes()


def test_keyboard_interrupt_reaps_and_preserves_original_exception(tmp_path, monkeypatch):
    from booley.flows import observed_process

    def interrupt(*_args):
        raise KeyboardInterrupt

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


def test_missing_executable_finishes_observation_and_can_run_again(tmp_path):
    flow = LintFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "demo"])
    sink = observer(tmp_path, flow="lint", tool_output=True)
    with sink.installed():
        assert flow._execute_boundary([str(tmp_path / "missing-tool")]).returncode == -1
        assert sink.worker is None
        assert (
            flow._execute_boundary([sys.executable, "-c", "print('second command')"]).returncode
            == 0
        )
    assert "second command" in sink.stream.getvalue()


def test_campaign_worker_inherits_invocation_observer(tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from booley.flows.sim.campaign.scheduler import BoundedCampaignScheduler, ScheduledAttempt

    capacity = SimpleNamespace(
        managed=False, shutdown_requested=False, outer_permit=nullcontext, slot_store=None
    )
    registry = SimpleNamespace(project_data=tmp_path, recover_unretired=lambda _: None)
    flow = SimulateFlow()
    flow.parse_args(["--work-dir", str(tmp_path), "--target", "demo"])

    def execute(attempt, *_args):
        assert current_progress() is sink
        result = flow._execute_boundary(
            [sys.executable, "-c", "print('campaign worker line')"], timeout=3
        )
        assert result.stdout == "campaign worker line\n"

    scheduler = BoundedCampaignScheduler(
        capacity,
        registry,
        allocate=lambda item: ScheduledAttempt(item, "attempt", 1, tmp_path, "demo"),
        execute=execute,
    )
    sink = observer(tmp_path, tool_output=True)
    with sink.installed():
        scheduler.run([{"test": "smoke"}])
    assert "campaign worker line" in sink.stream.getvalue()
    assert b"campaign worker line" in sink.log_path.read_bytes()


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


def test_blocked_transcript_writer_cannot_block_process_capture(tmp_path):
    release = threading.Event()
    entered = threading.Event()
    sink = observer(tmp_path)
    sink.begin_command()
    real_log = sink.log

    class SlowLog:
        def write(self, data):
            entered.set()
            release.wait(timeout=6)
            real_log.write(data)

        def close(self):
            real_log.close()

    sink.log = SlowLog()
    started = time.monotonic()
    try:
        with subprocess.Popen(
            [sys.executable, "-c", "print('complete capture')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            assert communicate_observed(process, sink, timeout=3) == (
                "complete capture\n",
                "",
                False,
            )
        assert entered.is_set()
        assert time.monotonic() - started < 3
        assert sink.log_path is None
        assert "incomplete/unavailable" in sink.stream.getvalue()
    finally:
        release.set()
        sink.worker.join(timeout=2)
        sink.close()
    assert not sink.worker.is_alive()
    assert not (sink.directory / "active").exists()


def test_sim_verbose_keeps_resume_semantics_with_quiet():
    from booley.flows.flow_session import FlowSession

    seen = []

    def execute(session):
        seen.append((session.args.verbose, current_progress()))
        return ExecutionResult(0, EndpointOutcome())

    with patch.object(FlowSession, "execute_prepared", execute):
        execute_cli(SimulateFlow(), ["--target", "demo", "--verbose", "--quiet"])
    assert seen == [(True, None)]


def test_plain_progress_has_canonical_utc_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "booley.flows.terminal_progress.utc_now_rfc3339", lambda: "2026-10-05T13:00:00Z"
    )
    sink = observer(tmp_path)
    sink.stage_changed("simulation")
    assert sink.stream.getvalue().startswith("[booley-progress] 2026-10-05T13:00:00Z ")


def test_concurrent_commands_keep_observing_after_one_finishes(tmp_path):
    sink = observer(tmp_path, tool_output=True)
    release = tmp_path / "release"
    script = (
        "import pathlib,time; print('second ready',flush=True); "
        f"p=pathlib.Path({str(release)!r}); deadline=time.monotonic()+5; "
        "exec('while not p.exists() and time.monotonic()<deadline: time.sleep(0.01)'); "
        "print('second still live',flush=True); time.sleep(0.5)"
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
            target=lambda: result.append(communicate_observed(second, sink, timeout=6))
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
                assert communicate_observed(first, sink, timeout=3)[0] == "first done\n"
            release.touch()
            wait_until(lambda: "second still live" in sink.stream.getvalue())
            assert second.poll() is None
        finally:
            release.touch()
            thread.join(timeout=7)
            second.stdout.close()
            second.stderr.close()
    assert not thread.is_alive()
    assert result == [("second ready\nsecond still live\n", "", False)]


def test_fast_output_backlog_preserves_every_transcript_line(tmp_path):
    sink = observer(tmp_path)
    # Freeze the consumer so the backlog deterministically exceeds the old limit.
    with sink.installed(), patch.object(sink, "_run"):
        sink.begin_command()
        chunk = b"unique-line\n" * 4000
        for _ in range(300):
            sink.observe("stdout", chunk)
        sink.end_command()
        sink._drain_end()
    assert sink.log_path.read_bytes().count(b"unique-line\n") == 1_200_000
    assert "incomplete/unavailable" not in sink.stream.getvalue()


def test_parallel_pipe_sources_do_not_merge_partial_lines(tmp_path):
    sink = observer(tmp_path, tool_output=True)
    sink.observe("stdout:1", b"first ")
    sink.observe("stdout:2", b"second ")
    sink.observe("stdout:1", b"line\n")
    sink.observe("stdout:2", b"line\n")
    sink.end_command()
    assert "stdout:1: first line" in sink.stream.getvalue()
    assert "stdout:2: second line" in sink.stream.getvalue()


@pytest.mark.parametrize("mode", ["runtime", "ticket"])
def test_agent_console_and_display_bytes_match_existing_transport(
    tmp_path, monkeypatch, capsys, mode
):
    from booley.flows.endpoint_events import (
        _endpoint_progress_event,
        _serialize_display_event,
        _write_display_event,
    )
    from booley.flows.flow_session import FlowSession

    def write_event(event):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(_serialize_display_event(event))

    if mode == "runtime":
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
    else:
        monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
        # Observe the existing transport without activating the runtime exclusion.
        monkeypatch.setattr("booley.flows.endpoint_reporting._write_display_event", write_event)
    monkeypatch.setenv("BOOLEY_DISPLAY_INVOCATION_ID", "fixed-invocation")
    monkeypatch.setattr(
        "booley.flows.endpoint_events.utc_now_rfc3339", lambda: "2026-10-05T13:00:00Z"
    )
    path = tmp_path / "display.jsonl"
    with patch.dict(os.environ, {"BOOLEY_RUNTIME_DIR": str(tmp_path)}):
        _write_display_event(_endpoint_progress_event("lint", "checking"))
        _write_display_event(
            _endpoint_progress_event("lint", "done", completion=True, repeats_at_end=False)
        )
    expected_events = path.read_bytes()
    path.unlink()

    def execute(session):
        assert session.terminal_progress is None
        assert current_progress() is None
        session.emit_progress("checking")
        session.emit_completion("done")
        print("verdict unchanged")
        return ExecutionResult(7, EndpointOutcome())

    adapter = object() if mode == "ticket" else None
    with patch.object(FlowSession, "execute_prepared", execute):
        result = execute_cli(LintFlow(), ["--target", "demo"], adapter=adapter)
    captured = capsys.readouterr()
    assert captured.out == "verdict unchanged\n"
    assert captured.err == ""
    assert result.exit_code == 7
    assert path.read_bytes() == expected_events


@pytest.mark.skipif(os.name != "nt", reason="Windows anonymous pipe compatibility")
def test_windows_capture_does_not_require_nonblocking_pipe_support(tmp_path, monkeypatch):
    def unavailable(*_args):
        raise OSError("nonblocking pipes unavailable on Python 3.11")

    monkeypatch.setattr(os, "set_blocking", unavailable)
    sink = observer(tmp_path)
    with sink.installed():
        sink.begin_command()
        with subprocess.Popen(
            [sys.executable, "-c", "print('Windows capture')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            assert communicate_observed(process, sink, timeout=3) == (
                "Windows capture\n",
                "",
                False,
            )


def test_real_fast_tool_keeps_complete_transcript(tmp_path):
    sink = observer(tmp_path)
    with sink.installed():
        sink.begin_command()
        with subprocess.Popen(
            [sys.executable, "-c", "import os; os.write(1,b'line\\n'*6000000)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as process:
            stdout, stderr, timed_out = communicate_observed(process, sink, timeout=5)
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
