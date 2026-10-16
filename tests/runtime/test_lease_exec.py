"""Actual native subprocess lifetime protection, including detached descendants."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests.runtime.scratch_support import reap_guardian
from tests.runtime.scratch_support import wait as _wait

from booley.core import temporary_workspace as tw

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="requires Linux subreaper/flock")


@pytest.fixture
def scratch_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv(tw.ROOT_ENV, str(tmp_path / "registry"))
    monkeypatch.setenv("HOME", str(tmp_path / "synthetic-home"))
    return tmp_path


def test_guardian_waits_for_detached_native_descendant(scratch_namespace):
    root = scratch_namespace
    release, ready = root / "release", root / "ready"
    command = root / "native.py"
    command.write_text(
        "import os, pathlib, sys, time\n"
        "release, ready = map(pathlib.Path, sys.argv[1:])\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    ready.write_text(str(os.getpid()))\n"
        "    deadline = time.monotonic() + 10\n"
        "    while not release.exists() and time.monotonic() < deadline: time.sleep(.01)\n"
        "    os._exit(0)\n"
    )
    with tw.temporary_workspace() as directory:
        launch = tw.prepare_launch([sys.executable, str(command), str(release), str(ready)])
        process = subprocess.Popen(launch.command, **launch.kwargs)
        try:
            _wait(ready.exists)
            assert process.poll() is None
            assert Path(directory).is_dir()
            release.touch()
            assert process.wait(timeout=10) == 0
            tw.launch_finished(launch)
        finally:
            release.touch()
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
    assert not Path(directory).exists()


@pytest.mark.parametrize("backend", ["sync", "sdk"])
def test_killed_owner_preserves_detached_child_until_guardian_completes(
    scratch_namespace, backend, linux_subreaper
):
    root = scratch_namespace
    ready, release, report = root / "ready", root / "release", root / "owner.json"
    native = root / "native.py"
    native.write_text(_detached_native_source(sys, ready, release))
    native.chmod(0o700)
    worker_code = _SYNC_OWNER_WORKER
    if backend == "sdk":
        worker_code = _SDK_OWNER_WORKER
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    worker = subprocess.Popen(
        [sys.executable, "-c", worker_code, str(native), str(ready), str(release), str(report)],
        env=environment,
    )
    guardian = None
    try:
        _wait(lambda: report.exists() and ready.exists())
        directory, guardian = json.loads(report.read_text())
        worker.kill()
        worker.wait(timeout=10)
        tw.sweep_stale_workspaces()
        assert Path(directory).is_dir(), (
            "dead owner is insufficient while detached child uses scratch"
        )
        release.touch()
        reap_guardian(guardian)
        guardian = None
        tw.sweep_stale_workspaces()
        assert not Path(directory).exists()
    finally:
        release.touch()
        if worker.poll() is None:
            worker.kill()
        worker.wait(timeout=10)
        if guardian is not None:
            reap_guardian(guardian)


def test_synchronous_runner_timeout_reaps_detached_children(scratch_namespace):
    ready = scratch_namespace / "ready"
    native = scratch_namespace / "native-timeout.py"
    native.write_text(
        "import os, pathlib, sys, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "    time.sleep(10)\n"
        "else: time.sleep(10)\n"
    )
    with tw.temporary_workspace() as directory:
        with pytest.raises(subprocess.TimeoutExpired):
            tw.run_guarded(
                [sys.executable, str(native), str(ready)],
                timeout=0.3,
                capture_output=True,
                text=True,
            )
        assert ready.exists()
        assert not Path(f"/proc/{ready.read_text()}").exists()
    assert not Path(directory).exists()


def test_synchronous_runner_preserves_io_and_exit_contract(scratch_namespace):
    with tw.temporary_workspace() as directory:
        result = tw.run_guarded(
            [
                sys.executable,
                "-c",
                "import sys; print(sys.stdin.read()); print('err', file=sys.stderr)",
            ],
            input="synthetic input",
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        assert result.stdout == "synthetic input\n"
        assert result.stderr == "err\n"
        assert result.returncode == 0
    assert not Path(directory).exists()


@pytest.mark.asyncio
async def test_actual_sdk_version_probe_and_main_share_guarded_workspace(
    scratch_namespace, monkeypatch
):
    import asyncio

    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    from booley.runtime._claude_backend import _guarded_sdk_options

    root = scratch_namespace
    ready, release, launches = root / "sdk-ready", root / "sdk-release", root / "sdk-launches"
    native = root / "fake-claude"
    native.write_text(
        _actual_sdk_version_probe_and_main_share_guarded_workspace_source_4(
            sys, launches, release, ready
        )
    )
    native.chmod(0o700)
    monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
    transport = None
    with tw.temporary_workspace() as directory:
        options = ClaudeAgentOptions(cli_path=str(native), cwd=directory)
        async with _guarded_sdk_options(options, tw.active_guard_fds()) as guarded:
            assert guarded.cli_path != str(native)
            transport = SubprocessCLITransport(prompt="synthetic", options=guarded)
            try:
                await transport.connect()
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert ready.exists()
                assert len(launches.read_text().splitlines()) == 2
                assert launches.read_text().splitlines()[0] == "['-v']"
                closing = asyncio.create_task(transport.close())
                await asyncio.sleep(0.05)
                assert Path(directory).is_dir()
                release.touch()
                await asyncio.wait_for(closing, timeout=10)
            finally:
                release.touch()
                await transport.close()
        assert not Path(guarded.cli_path).exists()
    assert not Path(directory).exists()


def test_synchronous_runner_missing_native_raises_and_cleans(scratch_namespace):
    with tw.temporary_workspace() as directory:
        with pytest.raises(FileNotFoundError):
            tw.run_guarded([str(scratch_namespace / "missing-native")], timeout=5)
        assert json.loads((Path(directory) / tw._MARKER).read_text())["launches"] == {}
    assert not Path(directory).exists()


def test_failed_unproven_runner_preserves_pending_handoff(scratch_namespace):
    def unknown_runner(*args, **kwargs):
        raise RuntimeError("unknown launch outcome")

    with (
        tw.temporary_workspace() as directory,
        pytest.raises(RuntimeError, match="unknown launch outcome"),
    ):
        tw.run_guarded([sys.executable], runner=unknown_runner)
    assert Path(directory).exists()
    tw.sweep_stale_workspaces()
    assert Path(directory).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["failed_probe", "skipped_probe", "cancelled_main"])
async def test_actual_sdk_probe_variants_and_cancellation_cleanup(
    scratch_namespace, monkeypatch, mode
):
    import asyncio

    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    from booley.runtime._claude_backend import _guarded_sdk_options

    ready, launches = scratch_namespace / "ready", scratch_namespace / "launches"
    native = scratch_namespace / "fake-claude"
    native.write_text(
        _actual_sdk_probe_variants_and_cancellation_cleanup_source_5(sys, launches, ready, mode)
    )
    native.chmod(0o700)
    if mode == "skipped_probe":
        monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    else:
        monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
    with tw.temporary_workspace() as directory:
        options = ClaudeAgentOptions(cli_path=str(native), cwd=directory)

        async def invoke():
            async with _guarded_sdk_options(options, tw.active_guard_fds()) as guarded:
                transport = SubprocessCLITransport(prompt="synthetic", options=guarded)
                try:
                    await transport.connect()
                    await asyncio.Event().wait()
                finally:
                    await transport.close()

        task = asyncio.create_task(invoke())
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert ready.exists()
            expected = 1 if mode == "skipped_probe" else 2
            assert len(launches.read_text().splitlines()) == expected
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=8)
        assert options.cli_path == str(native), (
            "per-attempt wrapper must not mutate caller options"
        )
    assert not Path(directory).exists()


def test_native_sdk_resolution_prefers_explicit_bundled_then_path(tmp_path, monkeypatch):
    import claude_agent_sdk
    from claude_agent_sdk import ClaudeAgentOptions

    from booley.runtime._claude_backend import _native_sdk_cli

    monkeypatch.setattr(claude_agent_sdk, "__file__", str(tmp_path / "__init__.py"))
    monkeypatch.setattr("shutil.which", lambda _: "/sdk/path-cli")
    options = ClaudeAgentOptions(cli_path="/explicit/synthetic-cli")
    assert _native_sdk_cli(options) == "/explicit/synthetic-cli"
    bundled = tmp_path / "_bundled/claude"
    bundled.parent.mkdir()
    bundled.touch()
    assert _native_sdk_cli(ClaudeAgentOptions()) == str(bundled)
    bundled.unlink()
    assert _native_sdk_cli(ClaudeAgentOptions()) == "/sdk/path-cli"


_SYNC_OWNER_WORKER = (
    "import json, pathlib, signal, subprocess, sys\n"
    "from booley.core import temporary_workspace as tw\n"
    "native, ready, release, report = sys.argv[1:]\n"
    "with tw.temporary_workspace() as root:\n"
    "    launch = tw.prepare_launch([sys.executable, native, ready, release])\n"
    "    child = subprocess.Popen(launch.command, **launch.kwargs)\n"
    "    pathlib.Path(report).write_text(json.dumps([root, child.pid]))\n"
    "    signal.pause()\n"
)


def _detached_native_source(sys, ready, release):
    return (
        f"#!{sys.executable}\n"
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "if sys.argv[1:] == ['-v']: print('2.1.0'); raise SystemExit(0)\n"
        "guardian = os.getppid()\n"
        f"ready, release = pathlib.Path({str(ready)!r}), pathlib.Path({str(release)!r})\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    ready.write_text(str(guardian))\n"
        "    deadline = time.monotonic() + 10\n"
        "    while not release.exists() and time.monotonic() < deadline: time.sleep(.01)\n"
        "    os._exit(0)\n"
    )


_SDK_OWNER_WORKER = (
    "import asyncio, json, pathlib, sys\n"
    "from claude_agent_sdk import ClaudeAgentOptions\n"
    "from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport\n"
    "from booley.core import temporary_workspace as tw\n"
    "from booley.runtime._claude_backend import _guarded_sdk_options\n"
    "async def main():\n"
    "    native, ready, release, report = sys.argv[1:]\n"
    "    with tw.temporary_workspace() as root:\n"
    "        options = ClaudeAgentOptions(cli_path=native, cwd=root)\n"
    "        async with _guarded_sdk_options(options, tw.active_guard_fds()) as guarded:\n"
    "            transport = SubprocessCLITransport(prompt='synthetic', options=guarded)\n"
    "            await transport.connect()\n"
    "            end = asyncio.get_running_loop().time() + 5\n"
    "            while not pathlib.Path(ready).exists() and asyncio.get_running_loop().time() < end: await asyncio.sleep(.01)\n"
    "            guardian = int(pathlib.Path(ready).read_text())\n"
    "            relay = int(pathlib.Path(f'/proc/{guardian}/stat').read_text().rsplit(')',1)[1].split()[1])\n"
    "            pathlib.Path(report).write_text(json.dumps([root, relay]))\n"
    "            await asyncio.Event().wait()\n"
    "asyncio.run(main())\n"
)


def _actual_sdk_version_probe_and_main_share_guarded_workspace_source_4(
    sys, launches, release, ready
):
    return (
        f"#!{sys.executable}\n"
        "import os, pathlib, sys, time\n"
        f"launches = pathlib.Path({str(launches)!r})\n"
        "with launches.open('a') as output: output.write(repr(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:] == ['-v']:\n"
        "    print('2.1.0')\n"
        "    raise SystemExit(0)\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        f"    release = pathlib.Path({str(release)!r})\n"
        "    deadline = time.monotonic() + 10\n"
        "    while not release.exists() and time.monotonic() < deadline: time.sleep(.01)\n"
        "    os._exit(0)\n"
        f"pathlib.Path({str(ready)!r}).touch()\n"
        "sys.stdin.read()\n"
    )


def _actual_sdk_probe_variants_and_cancellation_cleanup_source_5(sys, launches, ready, mode):
    return (
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f'with pathlib.Path({str(launches)!r}).open("a") as output: output.write(repr(sys.argv[1:]) + "\\n")\n'
        'if sys.argv[1:] == ["-v"]:\n'
        f"    print({'invalid version' if mode == 'failed_probe' else '2.1.0'!r})\n"
        f"    raise SystemExit({1 if mode == 'failed_probe' else 0})\n"
        f"pathlib.Path({str(ready)!r}).touch()\n"
        "sys.stdin.read()\n"
    )
