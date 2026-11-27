"""Behavior tests for host-side Docker build progress."""

from __future__ import annotations

import runpy
import subprocess
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path
from queue import Queue

import pytest
from tests.fake_docker_executable import FakeDocker, install_fake_docker
from tests.time_budget import hang_guard_s, scaled

from booley.runtime import docker_build, docker_capacity
from booley.runtime.docker_build import DockerBuildResult, run_docker_build
from booley.runtime.docker_capacity import (
    BuildEstimateClass,
    DockerBuildPlan,
    DockerBuildRequest,
    DockerCacheEvidence,
)

_GUIDANCE = (
    "Docker storage filled during the build; free unused cache with "
    "`docker builder prune` only if appropriate; pruning may evict layers "
    "the retry would otherwise reuse."
)


class _RecordingOutput:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._captured = ""

    def write(self, text: str) -> int:
        with self._condition:
            self._captured += text
            self._condition.notify_all()
        return len(text)

    def flush(self) -> None:
        pass

    def wait_for(self, text: str, timeout: float = hang_guard_s()) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: text in self._captured, timeout=timeout)

    @property
    def text(self) -> str:
        with self._condition:
            return self._captured


class _BrokenOutput:
    def __init__(self) -> None:
        self.attempts = 0

    def write(self, _text: str) -> int:
        self.attempts += 1
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


def _docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cached: bool = False,
    build_cache: str = "20GB",
    **replies: tuple[int, str],
) -> FakeDocker:
    """The fake Docker CLI answering the capacity probes; *replies* override its table."""
    table = {
        "info": (0, str(tmp_path)),
        "image": (0, "sha256:cached")
        if cached
        else (1, "Error response from daemon: No such image"),
        "system": (0, f"Build Cache\t{build_cache}\t18GB"),
    }
    return install_fake_docker(tmp_path, monkeypatch, table | replies)


def _build(docker: FakeDocker | str, image: str = "booley-sandbox", **kwargs) -> DockerBuildResult:
    name = docker if isinstance(docker, str) else docker.name
    return run_docker_build(
        [name, "build", "-t", image, "."],
        image=image,
        verbose=False,
        timeout=hang_guard_s(),
        output=_RecordingOutput(),
        **kwargs,
    )


def _set_free_space(monkeypatch: pytest.MonkeyPatch, gib: int) -> None:
    disk_usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("shutil.disk_usage", lambda _path: disk_usage(100, 88, gib * 2**30))


@pytest.mark.parametrize(
    ("announce", "chatter"), [(True, ""), (True, "compiler chatter"), (False, "")]
)
def test_live_build_shows_progress_and_heartbeat_before_it_ends(
    tmp_path: Path, monkeypatch, announce: bool, chatter: str
) -> None:
    release = tmp_path / "release"
    child = (
        "import pathlib, sys, time; "
        "gate = pathlib.Path(sys.argv[1])\n"
        "if sys.argv[3]: print('>>> Building Yosys from source', flush=True)\n"
        "while not gate.exists():\n"
        "    if sys.argv[2]: print(sys.argv[2], flush=True)\n"
        "    time.sleep(0.01)"
    )
    output = _RecordingOutput()
    results = []
    monkeypatch.setattr(docker_build, "HEARTBEAT_INTERVAL_S", 0.05)
    worker = threading.Thread(
        target=lambda: results.append(
            run_docker_build(
                [sys.executable, "-c", child, str(release), chatter, "1" if announce else ""],
                image="booley-sandbox",
                verbose=False,
                timeout=2 * hang_guard_s(),
                output=output,
            )
        )
    )

    worker.start()
    try:
        if announce:
            assert output.wait_for(">>> Building Yosys from source")
        assert output.wait_for("[booley-sandbox build] elapsed:")
        assert worker.is_alive()
        assert "compiler chatter" not in output.text
    finally:
        release.touch()
        worker.join(timeout=hang_guard_s())

    assert not worker.is_alive()
    assert results[0].returncode == 0


def test_fake_build_inherits_the_layers_of_a_base_given_by_id(tmp_path: Path, monkeypatch) -> None:
    docker = install_fake_docker(tmp_path / "bin", monkeypatch, {}, images={"base:v1": "sha256:a"})
    recipe = tmp_path / "Dockerfile"
    recipe.write_bytes(b"FROM base:v1\n")

    subprocess.run(["docker", "build", "-f", str(recipe), "-t", "out:v1", "."], check=True)

    assert docker.images["out:v1"]["Layers"] == ["sha256:a"]


def test_docker_build_refuses_low_capacity_before_starting(tmp_path: Path, monkeypatch) -> None:
    docker = _docker(tmp_path, monkeypatch)
    _set_free_space(monkeypatch, 12)

    with pytest.raises(OSError) as raised:
        _build(docker)

    message = str(raised.value)
    assert "12.0 GiB available" in message
    assert "35.0 GiB required" in message
    assert "30.0 GiB cold temporary peak" in message
    assert "5.0 GiB safety reserve" in message
    assert "16.8 GiB reclaimable" in message
    assert "docker builder prune" in message
    assert "BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT=1" in message
    assert not docker.built


def test_sequence_capacity_refuses_before_first_build(tmp_path: Path, monkeypatch) -> None:
    docker = _docker(tmp_path, monkeypatch)
    _set_free_space(monkeypatch, 39)
    runtime = DockerBuildRequest(
        "runtime base", "booley-runtime-base:local", BuildEstimateClass.HEAVYWEIGHT
    )
    sandbox = DockerBuildRequest("Sandbox Image", "booley-sandbox", BuildEstimateClass.HEAVYWEIGHT)

    with pytest.raises(OSError) as raised:
        _build(
            docker,
            runtime.output_tag,
            current_request=runtime,
            remaining_plan=DockerBuildPlan((runtime, sandbox)),
        )

    message = str(raised.value)
    assert "runtime base -> Sandbox Image" in message
    assert "40.0 GiB required" in message
    assert "runtime base: 30.0 GiB cold temporary peak, 5.0 GiB retained growth" in message
    assert "Sandbox Image: 30.0 GiB cold temporary peak, 5.0 GiB retained growth" in message
    assert message.count("5.0 GiB safety reserve") == 1
    assert not docker.built


def test_candidate_overlay_uses_explicit_thin_estimate(tmp_path: Path, monkeypatch) -> None:
    docker = _docker(tmp_path, monkeypatch)
    _set_free_space(monkeypatch, 10)
    request = DockerBuildRequest(
        "wheel overlay",
        "booley-lifecycle-transaction-wheel-overlay:candidate",
        BuildEstimateClass.THIN_OVERLAY,
    )

    result = _build(
        docker,
        request.output_tag,
        current_request=request,
        remaining_plan=DockerBuildPlan((request,)),
    )

    assert result.returncode == 0
    assert docker.built


def test_standard_sequence_uses_one_peak_instead_of_summed_peaks() -> None:
    plan = DockerBuildPlan(
        (
            DockerBuildRequest("runtime base", "base"),
            DockerBuildRequest("standard substrate", "substrate"),
            DockerBuildRequest("wheel overlay", "sandbox", BuildEstimateClass.THIN_OVERLAY),
        )
    )

    assert docker_capacity.required_sequence_headroom(plan) == 40 * 2**30


_BASE = DockerBuildRequest("base", "base")
_OVERLAY = DockerBuildRequest("overlay", "overlay")


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: DockerBuildPlan(()), "at least one request"),
        (
            lambda: DockerBuildPlan((_BASE,)).tail_from_output("missing"),
            "no request for output tag",
        ),
        (
            lambda: docker_capacity.required_sequence_headroom(DockerBuildPlan((_BASE,)), warm=()),
            "warm state count",
        ),
        (
            lambda: docker_capacity.ensure_docker_build_capacity(["docker", "build"]),
            "image or current_request",
        ),
        (
            lambda: docker_capacity.ensure_docker_build_capacity(
                ["docker", "build"],
                current_request=_BASE,
                remaining_plan=DockerBuildPlan((_OVERLAY,)),
            ),
            "must be first",
        ),
        (
            lambda: docker_capacity.ensure_docker_build_capacity(
                ["docker", "build"], image="other", current_request=_BASE
            ),
            "match the current request",
        ),
    ],
)
def test_capacity_planning_rejects_inconsistent_inputs(call, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        call()


@pytest.mark.parametrize("free_gib", [16, 14])
def test_warm_cache_uses_smaller_headroom_and_keeps_the_safety_reserve(
    tmp_path: Path, monkeypatch, free_gib: int
) -> None:
    docker = _docker(tmp_path, monkeypatch, cached=True)
    _set_free_space(monkeypatch, free_gib)
    request = DockerBuildRequest(
        "booley-sandbox",
        "booley-sandbox",
        cache_evidence=DockerCacheEvidence("booley-sandbox", (("expected", "sha256:cached"),)),
    )
    plan = DockerBuildPlan((request,))

    if free_gib == 16:
        assert _build(docker, current_request=request, remaining_plan=plan).returncode == 0
        assert docker.built
        return
    with pytest.raises(OSError, match=r"15\.0 GiB required") as raised:
        _build(docker, current_request=request, remaining_plan=plan)
    assert "10.0 GiB warm temporary peak" in str(raised.value)
    assert not docker.built


def test_target_without_build_cache_uses_cold_build_headroom(tmp_path: Path, monkeypatch) -> None:
    docker = _docker(tmp_path, monkeypatch, cached=True, build_cache="0B")
    _set_free_space(monkeypatch, 16)

    with pytest.raises(OSError, match=r"35\.0 GiB required") as raised:
        _build(docker)

    assert "30.0 GiB cold temporary peak" in str(raised.value)
    assert not docker.built


@pytest.mark.parametrize(("value", "builds"), [("1", True), ("true", False)])
def test_disk_preflight_override_accepts_only_one(
    tmp_path: Path, monkeypatch, value: str, builds: bool
) -> None:
    docker = _docker(tmp_path, monkeypatch)
    _set_free_space(monkeypatch, 1)
    monkeypatch.setenv("BOOLEY_SKIP_IMAGE_DISK_PREFLIGHT", value)

    if builds:
        assert _build(docker).returncode == 0
    else:
        with pytest.raises(OSError, match="Insufficient disk capacity"):
            _build(docker)

    assert docker.built is builds


def test_pump_deadline_bounds_an_exited_process_without_eof() -> None:
    # Private seam on purpose: through run_docker_build a build whose parent exits while a
    # descendant holds the output pipe returns only when that descendant ends, because
    # _close_capture closes the pipe the reader thread is blocked on, so no public call
    # reaches the deadline path alone. Follow-up bug: run_docker_build outlives its timeout.
    records: Queue[object] = Queue()
    output = _RecordingOutput()
    now = time.monotonic()
    state = docker_build._ProgressState(  # private: see the comment above
        "booley-sandbox", False, output, now - 1, now - 0.5, now - 1
    )
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            docker_build._pump(_ExitedProcess(), records, state)  # private: see above
        )
    )

    worker.start()
    worker.join(timeout=scaled(0.1))
    try:
        assert not worker.is_alive()
    finally:
        records.put(docker_build._EOF)  # private: see above
        worker.join(timeout=hang_guard_s())

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
        timeout=hang_guard_s(),
        output=_RecordingOutput(),
    )

    assert result.returncode == 1
    assert "ERROR: package checksum mismatch" in result.diagnostics
    assert len(result.diagnostics) <= 120


@pytest.mark.parametrize("verbose", [False, True])
def test_capacity_exhaustion_after_preflight_has_cleanup_guidance(verbose: bool) -> None:
    result = run_docker_build(
        [sys.executable, "-c", "print('ERROR: no space left on device'); raise SystemExit(1)"],
        image="booley-sandbox",
        verbose=verbose,
        timeout=hang_guard_s(),
        output=_RecordingOutput(),
    )

    assert result.returncode == 1
    expected = (_GUIDANCE,) if verbose else ("ERROR: no space left on device", _GUIDANCE)
    assert result.diagnostics == expected


@pytest.mark.parametrize(
    ("replies", "fault", "message"),
    [
        ({"info": (1, "Docker daemon unavailable")}, "", "could not query Docker storage root"),
        ({"info": (0, "relative")}, "", "invalid storage root"),
        ({"system": (1, "daemon down")}, "", "could not query Docker build cache"),
        ({"system": (0, "Images\t1GB\t0B")}, "", "did not report build-cache"),
        ({"system": (0, "Build Cache\tunknown\t0B")}, "", "invalid size"),
        ({}, "blocked", "could not run Docker capacity probe"),
        ({}, "usage", "could not inspect Docker storage"),
    ],
)
def test_capacity_probe_failure_stops_build_before_starting(
    tmp_path: Path, monkeypatch, replies, fault: str, message: str
) -> None:
    docker = _docker(tmp_path, monkeypatch, **replies)
    program = docker.name
    if fault == "blocked":
        # A directory cannot be started: an OSError other than FileNotFoundError.
        program = str(tmp_path / "blocked" / "docker")
        Path(program).mkdir(parents=True)
    if fault == "usage":

        def unavailable(_path):
            raise OSError("usage unavailable")

        monkeypatch.setattr("shutil.disk_usage", unavailable)

    with pytest.raises(docker_capacity.DockerCapacityError, match=message):
        _build(program)

    assert not docker.built


@pytest.mark.parametrize(("free_gib", "status"), [(35, 0), (34, 1)])
def test_capacity_cli_handles_success_and_failure(
    tmp_path: Path, monkeypatch, capsys, free_gib: int, status: int
) -> None:
    docker = _docker(tmp_path, monkeypatch)
    _set_free_space(monkeypatch, free_gib)

    result = docker_capacity.main(
        ["--image", "booley-sandbox", "--", docker.name, "build", "-t", "booley-sandbox", "."]
    )

    assert result == status
    stderr = capsys.readouterr().err
    assert ("Docker image-build preflight failed: Insufficient disk capacity" in stderr) is bool(
        status
    )


def test_capacity_cli_passes_validated_remaining_plan(tmp_path: Path, monkeypatch, capsys) -> None:
    docker = _docker(tmp_path, monkeypatch)
    _set_free_space(monkeypatch, 36)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        '{"requests":['
        '{"managed_image":"base","output_tag":"base","estimate_class":"heavyweight"},'
        '{"managed_image":"overlay","output_tag":"overlay","estimate_class":"thin-overlay"},'
        '{"managed_image":"final","output_tag":"final","estimate_class":"heavyweight"}'
        "]}",
        encoding="utf-8",
    )

    result = docker_capacity.main(
        [
            *("--image", "overlay", "--plan-file", str(plan_file), "--current-index", "1"),
            *("--", docker.name, "build", "-t", "overlay", "."),
        ]
    )

    assert result == 1
    stderr = capsys.readouterr().err
    assert "overlay -> final" in stderr
    assert "37.0 GiB required" in stderr


@pytest.mark.parametrize(
    ("contents", "index", "image", "message"),
    [
        ("{", 0, "base", "invalid Docker build plan file"),
        ('{"requests":[]}', -1, "base", "current index must not be negative"),
        ('{"requests":[]}', 0, "base", "no request at the current index"),
        (
            '{"requests":[{"managed_image":"base","output_tag":"base",'
            '"estimate_class":"heavyweight"}]}',
            0,
            "different",
            "current plan output tag does not match",
        ),
    ],
)
def test_capacity_cli_rejects_invalid_plan_files(
    tmp_path: Path, capsys, contents: str, index: int, image: str, message: str
) -> None:
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(contents, encoding="utf-8")

    result = docker_capacity.main(
        [
            *("--image", image, "--plan-file", str(plan_file), "--current-index", str(index)),
            *("--", "docker", "build"),
        ]
    )

    assert result == 1
    assert message in capsys.readouterr().err


def test_capacity_cli_requires_a_build_command() -> None:
    with pytest.raises(SystemExit):
        docker_capacity.main(["--image", "image"])


def test_capacity_module_entrypoint_runs(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["docker_capacity.py", "--image", "image", "--", "echo"])
    with pytest.raises(SystemExit, match="0"):
        runpy.run_path(docker_capacity.__file__, run_name="__main__")


def test_closed_progress_sink_does_not_fail_a_healthy_build_or_retry_a_failed_write() -> None:
    header, resolved = _PARALLEL_PROGRESS_RECORDS[0].rstrip("\n"), _PARALLEL_PROGRESS_RECORDS[2]
    child = f"for _ in range(3): print({header!r}); print({resolved.rstrip()!r})"
    output = _BrokenOutput()

    result = run_docker_build(
        [sys.executable, "-c", child],
        image="booley-sandbox",
        verbose=False,
        timeout=hang_guard_s(),
        output=output,
    )

    assert result.returncode == 0
    assert output.attempts == 1


@pytest.mark.parametrize(
    ("child", "verbose"),
    [
        ("import os, time; os.close(1); os.close(2); time.sleep(10)", False),
        ("import time; time.sleep(10)", True),
    ],
)
def test_timeout_uses_one_result_contract(child: str, verbose: bool) -> None:
    result = run_docker_build(
        [sys.executable, "-c", child],
        image="booley-sandbox",
        verbose=verbose,
        timeout=scaled(0.1),
        output=_RecordingOutput(),
    )

    assert result.timed_out
    assert result.returncode is None


def test_failure_diagnostics_do_not_repeat_visible_progress() -> None:
    child = "print('>>> compiling', flush=True); print('ERROR: compile failed'); exit(1)"
    output = _RecordingOutput()

    result = run_docker_build(
        [sys.executable, "-c", child],
        image="booley-sandbox",
        verbose=False,
        timeout=hang_guard_s(),
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
        timeout=hang_guard_s(),
        output=output,
    )

    assert result.returncode == 1
    assert output.text.splitlines() == ["build detail", "ERROR: compile failed"]
    assert result.diagnostics == ()


_PARALLEL_PROGRESS_RECORDS = (
    '#7 [yosys 1/1] RUN echo ">>> Building Yosys vX.Y..." \\\n',
    '#11 [runtime 1/1] RUN echo ">>> Installing runtime..." \\\n',
    "#7 0.1 >>> Building Yosys vX.Y...\n",
    "#11 0.1 >>> Installing runtime...\n",
)


@pytest.mark.parametrize("verbose", [False, True])
def test_parallel_stage_announcements_are_deduplicated_per_build(
    tmp_path: Path, verbose: bool
) -> None:
    records = (
        *_PARALLEL_PROGRESS_RECORDS,
        *_PARALLEL_PROGRESS_RECORDS[:2],
        "hidden compiler chatter\n",
        ">>> third\n",
        *_PARALLEL_PROGRESS_RECORDS,
        ">>> fourth\n",
        # Unresolved shell headers remain distinct from resolved announcements.
        '#7 RUN echo ">>> Building Yosys ${YOSYS_VERSION}..." \\\n',
        '#7 RUN echo ">>> Building Yosys ${YOSYS_VERSION}..." \\\n',
    )
    path = tmp_path / "records.txt"
    path.write_bytes("".join(records).encode("utf-8"))
    child = "import pathlib, sys; sys.stdout.write(pathlib.Path(sys.argv[1]).read_text('utf-8'))"
    first_seen = [
        ">>> Building Yosys vX.Y...",
        ">>> Installing runtime...",
        ">>> third",
        ">>> fourth",
        ">>> Building Yosys ${YOSYS_VERSION}...",
    ]

    # A second build with a fresh sink shows the same announcements again.
    for _ in range(1 if verbose else 2):
        output = _RecordingOutput()
        result = run_docker_build(
            [sys.executable, "-c", child, str(path)],
            image="booley-sandbox",
            verbose=verbose,
            timeout=hang_guard_s(),
            output=output,
        )
        assert result.returncode == 0
        if verbose:
            assert output.text == "".join(records)
        else:
            assert output.text.splitlines() == first_seen


def test_duplicate_announcements_do_not_postpone_heartbeat(monkeypatch) -> None:
    # Private seam on purpose: the heartbeat's minute boundary (59 s against 60 s) and its
    # "1m 00s" / "2m 40s" text need a clock controlled over minutes, which a live build
    # read through run_docker_build cannot provide.
    now = 0.0
    monkeypatch.setattr(docker_build, "monotonic", lambda: now)
    output = _RecordingOutput()
    state = docker_build._ProgressState("image", False, output, 0.0, 1000.0, 0.0)  # private: above
    for record in _PARALLEL_PROGRESS_RECORDS:
        state.accept(record)
    announcements = output.text
    for duplicate_time in (30.0, 59.0):
        now = duplicate_time
        for record in _PARALLEL_PROGRESS_RECORDS[:2]:
            state.accept(record)
    state.heartbeat(59.0)
    assert output.text == announcements
    state.heartbeat(60.0)
    assert output.text == announcements + "  * [image build] elapsed: 1m 00s\n"
    now = 100.0
    state.accept(">>> third\n")
    visible = output.text
    state.heartbeat(159.0)
    assert output.text == visible
    state.heartbeat(160.0)
    assert output.text == visible + "  * [image build] elapsed: 2m 40s\n"
