"""Behavior tests for host-side Docker build progress."""

from __future__ import annotations

import os
import runpy
import shlex
import subprocess
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path
from queue import Queue

import pytest

from booley.runtime import docker_build, docker_capacity
from booley.runtime.docker_build import run_docker_build
from booley.runtime.docker_capacity import (
    BuildEstimateClass,
    DockerBuildPlan,
    DockerBuildRequest,
    DockerCacheEvidence,
)


class _RecordingOutput:
    def __init__(self, *, tty: bool = False) -> None:
        self._tty = tty
        self._condition = threading.Condition()
        self._text = ""

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        with self._condition:
            self._text += text
            self._condition.notify_all()
        return len(text)

    def flush(self) -> None:
        pass

    def wait_for(self, text: str, timeout: float = 5.0) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: text in self._text, timeout=timeout)

    @property
    def text(self) -> str:
        with self._condition:
            return self._text


class _BrokenOutput:
    def isatty(self) -> bool:
        return False

    def write(self, _text: str) -> int:
        raise OSError("redirect closed")

    def flush(self) -> None:
        raise AssertionError("flush should not follow a failed write")


class _FailedCapture:
    def __iter__(self):
        return self

    def __next__(self):
        raise OSError("capture pipe failed")

    def close(self) -> None:
        pass


class _ExitedProcess:
    def __init__(self, stdout=None) -> None:
        self.stdout = stdout
        self.returncode = 0

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout=None) -> int:
        return self.returncode


def _install_fake_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cached: bool,
    build_cache: str = "20GB",
    info_failure: bool = False,
) -> tuple[Path, str]:
    marker = tmp_path / "build-started"
    fake_docker = tmp_path / "fake_docker.py"
    fake_docker.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        f"marker = Path({str(marker)!r})\n"
        f"cache = {build_cache!r}\n"
        f"cached = {cached!r}\n"
        f"info_failure = {info_failure!r}\n"
        "command = sys.argv[1:]\n"
        "if command and command[0] == 'info':\n"
        "    if info_failure:\n"
        "        print('Docker daemon unavailable', file=sys.stderr)\n"
        "        raise SystemExit(1)\n"
        f"    print({str(tmp_path)!r})\n"
        "elif command and command[0] == 'image':\n"
        "    if cached:\n"
        "        print('sha256:cached')\n"
        "    else:\n"
        "        print('Error response from daemon: No such image', file=sys.stderr)\n"
        "        raise SystemExit(1)\n"
        "elif command and command[0] == 'system':\n"
        "    print(f'Build Cache\\t{cache}\\t18GB')\n"
        "elif command and command[0] == 'build':\n"
        "    marker.touch()\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        docker = tmp_path / "docker.cmd"
        docker.write_text(f'@"{sys.executable}" "{fake_docker}" %*\r\n', encoding="utf-8")
    else:
        docker = tmp_path / "docker"
        docker.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
            f'{shlex.quote(str(fake_docker))} "$@"\n',
            encoding="utf-8",
        )
        docker.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(tmp_path), os.environ["PATH"])))
    return marker, docker.name


def _set_free_space(monkeypatch: pytest.MonkeyPatch, gib: int) -> None:
    disk_usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("shutil.disk_usage", lambda _path: disk_usage(100, 88, gib * 2**30))


def test_redirected_progress_is_visible_before_build_completes(tmp_path: Path) -> None:
    release = tmp_path / "release"
    child = (
        "import pathlib, sys, time; "
        "gate = pathlib.Path(sys.argv[1]); "
        "print('>>> Building Yosys from source', flush=True); "
        "\nwhile not gate.exists(): time.sleep(0.01)"
    )
    output = _RecordingOutput()
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            run_docker_build(
                [sys.executable, "-c", child, str(release)],
                image="booley-sandbox",
                verbose=False,
                timeout=10,
                output=output,
            )
        )
    )

    worker.start()
    try:
        assert output.wait_for(">>> Building Yosys from source")
        assert worker.is_alive()
    finally:
        release.touch()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert results[0].returncode == 0


def test_docker_build_refuses_low_capacity_before_starting(tmp_path: Path, monkeypatch) -> None:
    marker, docker = _install_fake_docker(tmp_path, monkeypatch, cached=False)
    _set_free_space(monkeypatch, 12)

    with pytest.raises(OSError) as raised:
        run_docker_build(
            [docker, "build", "-t", "booley-sandbox", "."],
            image="booley-sandbox",
            verbose=False,
            timeout=10,
            output=_RecordingOutput(),
        )

    message = str(raised.value)
    assert "12.0 GiB available" in message
    assert "35.0 GiB required" in message
    assert "30.0 GiB cold temporary peak" in message
    assert "5.0 GiB safety reserve" in message
    assert "16.8 GiB reclaimable" in message
    assert "docker builder prune" in message
    assert "BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT=1" in message
    assert not marker.exists()


def test_sequence_capacity_refuses_before_first_build(tmp_path: Path, monkeypatch) -> None:
    marker, docker = _install_fake_docker(tmp_path, monkeypatch, cached=False)
    _set_free_space(monkeypatch, 39)
    runtime = DockerBuildRequest(
        "runtime base", "booley-runtime-base:local", BuildEstimateClass.HEAVYWEIGHT
    )
    sandbox = DockerBuildRequest("Sandbox Image", "booley-sandbox", BuildEstimateClass.HEAVYWEIGHT)
    plan = DockerBuildPlan((runtime, sandbox))

    with pytest.raises(OSError) as raised:
        run_docker_build(
            [docker, "build", "-t", runtime.output_tag, "."],
            image=runtime.output_tag,
            verbose=False,
            timeout=10,
            output=_RecordingOutput(),
            current_request=runtime,
            remaining_plan=plan,
        )

    message = str(raised.value)
    assert "runtime base -> Sandbox Image" in message
    assert "40.0 GiB required" in message
    assert "runtime base: 30.0 GiB cold temporary peak, 5.0 GiB retained growth" in message
    assert "Sandbox Image: 30.0 GiB cold temporary peak, 5.0 GiB retained growth" in message
    assert message.count("5.0 GiB safety reserve") == 1
    assert not marker.exists()


def test_remaining_tail_does_not_charge_completed_headroom(tmp_path: Path, monkeypatch) -> None:
    marker, docker = _install_fake_docker(tmp_path, monkeypatch, cached=False)
    observations = iter((35, 11))
    disk_usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "shutil.disk_usage",
        lambda _path: disk_usage(100, 0, next(observations) * 2**30),
    )
    base = DockerBuildRequest("runtime base", "base", BuildEstimateClass.HEAVYWEIGHT)
    overlay = DockerBuildRequest("wheel overlay", "candidate", BuildEstimateClass.THIN_OVERLAY)

    first = run_docker_build(
        [docker, "build", "-t", base.output_tag, "."],
        image=base.output_tag,
        verbose=False,
        timeout=10,
        output=_RecordingOutput(),
        current_request=base,
        remaining_plan=DockerBuildPlan((base, overlay)),
    )
    second = run_docker_build(
        [docker, "build", "-t", overlay.output_tag, "."],
        image=overlay.output_tag,
        verbose=False,
        timeout=10,
        output=_RecordingOutput(),
        current_request=overlay,
        remaining_plan=DockerBuildPlan((overlay,)),
    )

    assert first.returncode == second.returncode == 0
    assert marker.exists()


def test_candidate_overlay_uses_explicit_thin_estimate(tmp_path: Path, monkeypatch) -> None:
    marker, docker = _install_fake_docker(tmp_path, monkeypatch, cached=False)
    _set_free_space(monkeypatch, 10)
    request = DockerBuildRequest(
        "wheel overlay",
        "booley-lifecycle-transaction-wheel-overlay:candidate",
        BuildEstimateClass.THIN_OVERLAY,
    )

    result = run_docker_build(
        [docker, "build", "-t", request.output_tag, "."],
        image=request.output_tag,
        verbose=False,
        timeout=10,
        output=_RecordingOutput(),
        current_request=request,
        remaining_plan=DockerBuildPlan((request,)),
    )

    assert result.returncode == 0
    assert marker.exists()


def test_standard_sequence_uses_one_peak_instead_of_summed_peaks() -> None:
    plan = DockerBuildPlan(
        (
            DockerBuildRequest("runtime base", "base"),
            DockerBuildRequest("standard substrate", "substrate"),
            DockerBuildRequest("wheel overlay", "sandbox", BuildEstimateClass.THIN_OVERLAY),
        )
    )

    assert docker_capacity.required_sequence_headroom(plan) == 40 * 2**30
    assert docker_capacity.required_sequence_headroom(plan) < 3 * 30 * 2**30


def test_capacity_plan_rejects_empty_and_unknown_tail() -> None:
    with pytest.raises(ValueError, match="at least one request"):
        DockerBuildPlan(())

    plan = DockerBuildPlan((DockerBuildRequest("base", "base"),))
    with pytest.raises(ValueError, match="no request for output tag"):
        plan.tail_from_output("missing")


def test_sequence_headroom_requires_one_warm_state_per_request() -> None:
    plan = DockerBuildPlan((DockerBuildRequest("base", "base"),))

    with pytest.raises(ValueError, match="warm state count"):
        docker_capacity.required_sequence_headroom(plan, warm=())


def test_capacity_preflight_rejects_inconsistent_request_arguments() -> None:
    base = DockerBuildRequest("base", "base")
    overlay = DockerBuildRequest("overlay", "overlay")

    with pytest.raises(ValueError, match="image or current_request"):
        docker_capacity.ensure_docker_build_capacity(["docker", "build"])
    with pytest.raises(ValueError, match="must be first"):
        docker_capacity.ensure_docker_build_capacity(
            ["docker", "build"],
            current_request=base,
            remaining_plan=DockerBuildPlan((overlay,)),
        )
    with pytest.raises(ValueError, match="match the current request"):
        docker_capacity.ensure_docker_build_capacity(
            ["docker", "build"], image="other", current_request=base
        )


def test_cached_docker_build_uses_smaller_headroom(tmp_path: Path, monkeypatch) -> None:
    marker, docker = _install_fake_docker(tmp_path, monkeypatch, cached=True)
    _set_free_space(monkeypatch, 16)

    request = DockerBuildRequest(
        "booley-sandbox",
        "booley-sandbox",
        cache_evidence=DockerCacheEvidence("booley-sandbox", (("expected", "sha256:cached"),)),
    )
    result = run_docker_build(
        [docker, "build", "-t", "booley-sandbox", "."],
        image="booley-sandbox",
        verbose=False,
        timeout=10,
        output=_RecordingOutput(),
        current_request=request,
        remaining_plan=DockerBuildPlan((request,)),
    )

    assert result.returncode == 0
    assert marker.exists()


def test_cached_docker_build_preserves_five_gib_safety_reserve(
    tmp_path: Path, monkeypatch
) -> None:
    marker, docker = _install_fake_docker(tmp_path, monkeypatch, cached=True)
    _set_free_space(monkeypatch, 14)
    request = DockerBuildRequest(
        "booley-sandbox",
        "booley-sandbox",
        cache_evidence=DockerCacheEvidence("booley-sandbox", (("expected", "sha256:cached"),)),
    )

    with pytest.raises(OSError, match=r"15\.0 GiB required") as raised:
        run_docker_build(
            [docker, "build", "-t", "booley-sandbox", "."],
            image="booley-sandbox",
            verbose=False,
            timeout=10,
            output=_RecordingOutput(),
            current_request=request,
            remaining_plan=DockerBuildPlan((request,)),
        )

    assert "10.0 GiB warm temporary peak" in str(raised.value)
    assert not marker.exists()


def test_target_without_build_cache_uses_cold_build_headroom(tmp_path: Path, monkeypatch) -> None:
    marker, docker = _install_fake_docker(tmp_path, monkeypatch, cached=True, build_cache="0B")
    _set_free_space(monkeypatch, 16)

    with pytest.raises(OSError, match=r"35\.0 GiB required") as raised:
        run_docker_build(
            [docker, "build", "-t", "booley-sandbox", "."],
            image="booley-sandbox",
            verbose=False,
            timeout=10,
            output=_RecordingOutput(),
        )

    assert "30.0 GiB cold temporary peak" in str(raised.value)
    assert not marker.exists()


def test_disk_preflight_override_allows_expert_build(tmp_path: Path, monkeypatch) -> None:
    marker, docker = _install_fake_docker(tmp_path, monkeypatch, cached=False)
    _set_free_space(monkeypatch, 1)
    monkeypatch.setenv("BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT", "1")

    result = run_docker_build(
        [docker, "build", "-t", "booley-sandbox", "."],
        image="booley-sandbox",
        verbose=False,
        timeout=10,
        output=_RecordingOutput(),
    )

    assert result.returncode == 0
    assert marker.exists()


def test_disk_preflight_override_accepts_only_one(tmp_path: Path, monkeypatch) -> None:
    marker, docker = _install_fake_docker(tmp_path, monkeypatch, cached=False)
    _set_free_space(monkeypatch, 1)
    monkeypatch.setenv("BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT", "true")

    with pytest.raises(OSError, match="Insufficient disk capacity"):
        run_docker_build(
            [docker, "build", "-t", "booley-sandbox", "."],
            image="booley-sandbox",
            verbose=False,
            timeout=10,
            output=_RecordingOutput(),
        )

    assert not marker.exists()


def test_redirected_silent_build_emits_bounded_heartbeat(tmp_path: Path, monkeypatch) -> None:
    release = tmp_path / "release"
    child = (
        "import pathlib, sys, time; "
        "gate = pathlib.Path(sys.argv[1]); "
        "\nwhile not gate.exists(): time.sleep(0.01)"
    )
    output = _RecordingOutput()
    results = []
    monkeypatch.setattr(docker_build, "HEARTBEAT_INTERVAL_S", 0.05, raising=False)
    worker = threading.Thread(
        target=lambda: results.append(
            run_docker_build(
                [sys.executable, "-c", child, str(release)],
                image="booley-sandbox",
                verbose=False,
                timeout=10,
                output=output,
            )
        )
    )

    worker.start()
    try:
        assert output.wait_for("[booley-sandbox build] elapsed:")
        assert worker.is_alive()
    finally:
        release.touch()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert results[0].returncode == 0


def test_tty_silent_build_emits_bounded_heartbeat(tmp_path: Path, monkeypatch) -> None:
    release = tmp_path / "release"
    child = (
        "import pathlib, sys, time; "
        "gate = pathlib.Path(sys.argv[1]); "
        "\nwhile not gate.exists(): time.sleep(0.01)"
    )
    output = _RecordingOutput(tty=True)
    results = []
    monkeypatch.setattr(docker_build, "HEARTBEAT_INTERVAL_S", 0.05)
    worker = threading.Thread(
        target=lambda: results.append(
            run_docker_build(
                [sys.executable, "-c", child, str(release)],
                image="booley-sandbox",
                verbose=False,
                timeout=10,
                output=output,
            )
        )
    )

    worker.start()
    try:
        assert output.wait_for("[booley-sandbox build] elapsed:")
        assert worker.is_alive()
    finally:
        release.touch()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert results[0].returncode == 0


def test_pump_deadline_bounds_an_exited_process_without_eof() -> None:
    records: Queue[object] = Queue()
    output = _RecordingOutput()
    now = time.monotonic()
    state = docker_build._ProgressState(
        "booley-sandbox", False, output, now - 1, now - 0.5, now - 1
    )
    results = []
    worker = threading.Thread(
        target=lambda: results.append(docker_build._pump(_ExitedProcess(), records, state))
    )

    worker.start()
    worker.join(timeout=0.1)
    try:
        assert not worker.is_alive()
    finally:
        records.put(docker_build._EOF)
        worker.join(timeout=1)

    assert results == [False]


def test_output_capture_failure_is_reported_with_build_context(monkeypatch) -> None:
    process = _ExitedProcess(_FailedCapture())
    monkeypatch.setattr(docker_build.subprocess, "Popen", lambda *_args, **_kwargs: process)

    with pytest.raises(OSError, match="booley-sandbox Docker build output capture failed"):
        run_docker_build(
            [sys.executable, "-c", "pass"],
            image="booley-sandbox",
            verbose=False,
            timeout=10,
            output=_RecordingOutput(),
        )


def test_failure_retains_early_error_despite_noisy_cleanup() -> None:
    child = (
        "print('ERROR: package checksum mismatch', flush=True); "
        "[print(f'cleanup line {i}') for i in range(150)]; "
        "raise SystemExit(1)"
    )

    result = run_docker_build(
        [sys.executable, "-c", child],
        image="booley-sandbox",
        verbose=False,
        timeout=10,
        output=_RecordingOutput(),
    )

    assert result.returncode == 1
    assert "ERROR: package checksum mismatch" in result.diagnostics
    assert len(result.diagnostics) <= 120


def test_capacity_exhaustion_after_preflight_has_cleanup_guidance() -> None:
    result = run_docker_build(
        [
            sys.executable,
            "-c",
            "print('ERROR: no space left on device'); raise SystemExit(1)",
        ],
        image="booley-sandbox",
        verbose=False,
        timeout=10,
        output=_RecordingOutput(),
    )

    assert result.returncode == 1
    assert result.diagnostics[-1] == (
        "Docker storage filled during the build; free unused cache with "
        "`docker builder prune` only if appropriate; pruning may evict layers "
        "the retry would otherwise reuse."
    )


def test_capacity_exhaustion_in_verbose_tty_has_cleanup_guidance() -> None:
    output = _RecordingOutput(tty=True)
    result = run_docker_build(
        [
            sys.executable,
            "-c",
            "print('ERROR: no space left on device'); raise SystemExit(1)",
        ],
        image="booley-sandbox",
        verbose=True,
        timeout=10,
        output=output,
    )

    assert result.returncode == 1
    assert result.diagnostics == (
        "Docker storage filled during the build; free unused cache with "
        "`docker builder prune` only if appropriate; pruning may evict layers "
        "the retry would otherwise reuse.",
    )


def test_capacity_probe_failure_stops_build_before_starting(tmp_path: Path, monkeypatch) -> None:
    marker, docker = _install_fake_docker(tmp_path, monkeypatch, cached=False, info_failure=True)

    with pytest.raises(OSError, match="could not query Docker storage root"):
        run_docker_build(
            [docker, "build", "-t", "booley-sandbox", "."],
            image="booley-sandbox",
            verbose=False,
            timeout=10,
            output=_RecordingOutput(),
        )

    assert not marker.exists()


def test_capacity_probe_process_failure_is_fail_closed(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise OSError("probe unavailable")

    monkeypatch.setattr(docker_capacity.subprocess, "run", fail)

    with pytest.raises(
        docker_capacity.DockerCapacityError, match="could not run Docker capacity probe"
    ):
        docker_capacity._run_docker_probe(["docker", "info"])


def test_capacity_probe_rejects_invalid_storage_root(monkeypatch) -> None:
    result = subprocess.CompletedProcess(["docker"], 0, stdout="relative", stderr="")
    monkeypatch.setattr(docker_capacity, "_run_docker_probe", lambda _command: result)

    with pytest.raises(docker_capacity.DockerCapacityError, match="invalid storage root"):
        docker_capacity._docker_storage("docker")


def test_capacity_probe_rejects_malformed_external_sizes() -> None:
    with pytest.raises(docker_capacity.DockerCapacityError, match="invalid size"):
        docker_capacity._size_bytes("unknown")


def test_capacity_probe_rejects_cache_query_failures(monkeypatch) -> None:
    failed = subprocess.CompletedProcess(["docker"], 1, stdout="", stderr="daemon down")
    monkeypatch.setattr(docker_capacity, "_run_docker_probe", lambda _command: failed)

    with pytest.raises(
        docker_capacity.DockerCapacityError, match="could not query Docker build cache"
    ):
        docker_capacity._build_cache("docker")


def test_capacity_probe_rejects_missing_cache_report(monkeypatch) -> None:
    result = subprocess.CompletedProcess(["docker"], 0, stdout="Images\t1GB\t0B", stderr="")
    monkeypatch.setattr(docker_capacity, "_run_docker_probe", lambda _command: result)

    with pytest.raises(docker_capacity.DockerCapacityError, match="did not report build-cache"):
        docker_capacity._build_cache("docker")


def test_capacity_probe_reports_storage_usage_failure(monkeypatch) -> None:
    monkeypatch.delenv(docker_capacity.SKIP_PREFLIGHT_ENV, raising=False)
    monkeypatch.setattr(docker_capacity, "_docker_storage", lambda _docker: Path("/storage"))
    monkeypatch.setattr(
        docker_capacity.shutil,
        "disk_usage",
        lambda _path: (_ for _ in ()).throw(OSError("usage unavailable")),
    )

    with pytest.raises(
        docker_capacity.DockerCapacityError, match="could not inspect Docker storage"
    ):
        docker_capacity.ensure_docker_build_capacity(["docker", "build"], image="image")


def test_capacity_cli_handles_success_and_failure(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        docker_capacity, "ensure_docker_build_capacity", lambda *_args, **_kwargs: None
    )
    assert docker_capacity.main(["--image", "image", "--", "docker", "build"]) == 0

    def fail(*_args, **_kwargs):
        raise docker_capacity.DockerCapacityError("not enough space")

    monkeypatch.setattr(docker_capacity, "ensure_docker_build_capacity", fail)
    assert docker_capacity.main(["--image", "image", "--", "docker", "build"]) == 1
    assert "Docker image-build preflight failed: not enough space" in capsys.readouterr().err


def test_capacity_cli_passes_validated_remaining_plan(tmp_path: Path, monkeypatch) -> None:
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        '{"requests":['
        '{"managed_image":"base","output_tag":"base","estimate_class":"heavyweight"},'
        '{"managed_image":"overlay","output_tag":"overlay","estimate_class":"thin-overlay"}'
        "]}",
        encoding="utf-8",
    )
    observed = []
    monkeypatch.setattr(
        docker_capacity,
        "ensure_docker_build_capacity",
        lambda *_args, **kwargs: observed.extend(kwargs["remaining_plan"].requests),
    )

    result = docker_capacity.main(
        [
            "--image",
            "overlay",
            "--plan-file",
            str(plan_file),
            "--current-index",
            "1",
            "--",
            "docker",
            "build",
        ]
    )

    assert result == 0
    assert [request.managed_image for request in observed] == ["overlay"]


@pytest.mark.parametrize(
    ("contents", "index", "message"),
    [
        ("{", 0, "invalid Docker build plan file"),
        ('{"requests":[]}', -1, "current index must not be negative"),
        ('{"requests":[]}', 0, "no request at the current index"),
    ],
)
def test_capacity_plan_file_rejects_invalid_documents(
    tmp_path: Path, contents: str, index: int, message: str
) -> None:
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(contents, encoding="utf-8")

    with pytest.raises(docker_capacity.DockerCapacityError, match=message):
        docker_capacity._plan_from_file(plan_file, index)


def test_capacity_cli_rejects_plan_output_mismatch(tmp_path: Path, capsys) -> None:
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        '{"requests":['
        '{"managed_image":"base","output_tag":"base","estimate_class":"heavyweight"}'
        "]}",
        encoding="utf-8",
    )

    result = docker_capacity.main(
        [
            "--image",
            "different",
            "--plan-file",
            str(plan_file),
            "--",
            "docker",
            "build",
        ]
    )

    assert result == 1
    assert "current plan output tag does not match" in capsys.readouterr().err


def test_capacity_cli_requires_a_build_command() -> None:
    with pytest.raises(SystemExit):
        docker_capacity.main(["--image", "image"])


def test_capacity_module_entrypoint_runs(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["docker_capacity.py", "--image", "image", "--", "echo"])
    with pytest.raises(SystemExit, match="0"):
        runpy.run_path(docker_capacity.__file__, run_name="__main__")


def test_closed_progress_sink_does_not_fail_a_healthy_build() -> None:
    result = run_docker_build(
        [sys.executable, "-c", "print('>>> Building image', flush=True)"],
        image="booley-sandbox",
        verbose=False,
        timeout=10,
        output=_BrokenOutput(),
    )

    assert result.returncode == 0


def test_timeout_still_applies_after_child_closes_output_pipe() -> None:
    child = "import os, time; os.close(1); os.close(2); time.sleep(10)"

    result = run_docker_build(
        [sys.executable, "-c", child],
        image="booley-sandbox",
        verbose=False,
        timeout=0.1,
        output=_RecordingOutput(),
    )

    assert result.timed_out
    assert result.returncode is None


def test_verbose_tty_timeout_uses_the_same_result_contract() -> None:
    result = run_docker_build(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        image="booley-sandbox",
        verbose=True,
        timeout=0.1,
        output=_RecordingOutput(tty=True),
    )

    assert result.timed_out
    assert result.returncode is None


def test_hidden_chatter_does_not_postpone_redirected_heartbeat(
    tmp_path: Path, monkeypatch
) -> None:
    release = tmp_path / "release"
    child = (
        "import pathlib, sys, time; "
        "gate = pathlib.Path(sys.argv[1]); "
        "\nwhile not gate.exists(): print('compiler chatter', flush=True); time.sleep(0.01)"
    )
    output = _RecordingOutput()
    monkeypatch.setattr(docker_build, "HEARTBEAT_INTERVAL_S", 0.05)
    worker = threading.Thread(
        target=lambda: run_docker_build(
            [sys.executable, "-c", child, str(release)],
            image="booley-sandbox",
            verbose=False,
            timeout=10,
            output=output,
        )
    )

    worker.start()
    try:
        assert output.wait_for("[booley-sandbox build] elapsed:")
        assert "compiler chatter" not in output.text
    finally:
        release.touch()
        worker.join(timeout=5)

    assert not worker.is_alive()


def test_tty_non_verbose_preserves_progress_order_without_consecutive_duplicates() -> None:
    child = (
        "print('>>> first', flush=True); print('>>> first', flush=True); "
        "print('hidden chatter', flush=True); print('>>> second', flush=True)"
    )
    output = _RecordingOutput(tty=True)

    result = run_docker_build(
        [sys.executable, "-c", child],
        image="booley-sandbox",
        verbose=False,
        timeout=10,
        output=output,
    )

    assert result.returncode == 0
    assert output.text.splitlines() == [">>> first", ">>> second"]


def test_failure_diagnostics_do_not_repeat_visible_progress() -> None:
    child = "print('>>> compiling', flush=True); print('ERROR: compile failed'); exit(1)"
    output = _RecordingOutput(tty=True)

    result = run_docker_build(
        [sys.executable, "-c", child],
        image="booley-sandbox",
        verbose=False,
        timeout=10,
        output=output,
    )

    assert output.text.splitlines() == [">>> compiling"]
    assert result.diagnostics == ("ERROR: compile failed",)


def test_redirected_verbose_failure_streams_complete_output_once() -> None:
    child = (
        "import sys; print('build detail', flush=True); "
        "print('ERROR: compile failed', file=sys.stderr, flush=True); exit(1)"
    )
    output = _RecordingOutput()

    result = run_docker_build(
        [sys.executable, "-c", child],
        image="booley-sandbox",
        verbose=True,
        timeout=10,
        output=output,
    )

    assert result.returncode == 1
    assert output.text.splitlines() == ["build detail", "ERROR: compile failed"]
    assert result.diagnostics == ()
