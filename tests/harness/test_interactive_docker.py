"""Tests for the Interactive Mode Docker probes (ADR 0018).

The ``docker`` CLI is mocked at the single ``_run_docker`` seam, so these run
without Docker installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

import pytest

from booley.runtime import image_identity
from booley.runtime import interactive_docker as idk
from tests.pipe_holding_tree import heartbeat_tree, parent_argv, wait_until_heartbeat_stops


def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["docker"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class FakeDocker:
    """Records docker invocations and replies from a rules table.

    *rules* maps a matching predicate (callable on the args list) to a
    CompletedProcess. First match wins; unmatched calls return success.
    """

    def __init__(self, rules=None):
        self.calls: list[list[str]] = []
        self.rules = rules or []

    def __call__(self, args, *, timeout=30, executable="docker"):
        self.calls.append(list(args))
        for predicate, response in self.rules:
            if predicate(args):
                return response
        return _cp(0)

    def ran(self, *needles: str) -> bool:
        """True if any recorded call contains all *needles* as a subsequence-ish match."""
        return any(all(n in call for n in needles) for call in self.calls)


@pytest.fixture
def fake_docker(monkeypatch):
    def install(rules=None):
        fd = FakeDocker(rules)
        monkeypatch.setattr(idk, "_run_docker", fd)
        return fd

    return install


class TestIssuedImageKeepers:
    def test_image_id_returns_none_only_for_missing_image(self, fake_docker):
        fake = fake_docker(
            [
                (
                    lambda args: args[:2] == ["image", "inspect"],
                    _cp(1, stderr="Error response from daemon: No such image: keeper"),
                )
            ]
        )
        assert idk.image_id("keeper") is None
        assert fake.calls == [["image", "inspect", "keeper", "--format", "{{.Id}}"]]

        fake_docker(
            [
                (
                    lambda args: args[:2] == ["image", "inspect"],
                    _cp(0, stdout="sha256:" + "a" * 64 + "\n"),
                )
            ]
        )
        assert idk.image_id("keeper") == "sha256:" + "a" * 64

        # A daemon failure is not "absent": the same lookup must refuse to answer.
        fake_docker(
            [
                (
                    lambda args: args[:2] == ["image", "inspect"],
                    _cp(1, stderr="Cannot connect to the Docker daemon"),
                )
            ]
        )
        with pytest.raises(RuntimeError, match="Cannot connect") as caught:
            idk.image_id("keeper")
        assert isinstance(caught.value, idk.ImageInspectError)

    def test_image_id_raises_for_docker_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cases: list[tuple[Any, str]] = [
            (FileNotFoundError("docker"), "docker"),
            (subprocess.TimeoutExpired("docker", 30), "timed out"),
            (_cp(125, stderr="Cannot connect to the Docker daemon"), "Cannot connect"),
            (_cp(1), "exited 1"),
            (_cp(0), "no image ID"),
        ]
        timeouts: list[int] = []
        for failure, cause in cases:

            def fake(
                args: Any,
                *,
                timeout: int = 0,
                _failure: Any = failure,
                **_kwargs: Any,
            ) -> subprocess.CompletedProcess:
                timeouts.append(timeout)
                if isinstance(_failure, BaseException):
                    raise _failure
                return _failure

            monkeypatch.setattr(idk, "_run_docker", fake)

            with pytest.raises(RuntimeError, match=cause) as caught:
                idk.image_id("keeper:tag")

            assert isinstance(caught.value, idk.ImageInspectError)
            assert "'keeper:tag'" in str(caught.value)
        assert timeouts == [30] * len(cases)

    def test_image_id_raises_for_non_executable_docker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess:
            raise PermissionError("not executable")

        monkeypatch.setattr(idk, "_run_docker", fake)

        with pytest.raises(RuntimeError, match="not executable") as caught:
            idk.image_id("keeper:tag")

        assert isinstance(caught.value, idk.ImageInspectError)

    def test_remove_keeper_is_idempotent_but_surfaces_daemon_failure(self, fake_docker):
        fake_docker(
            [
                (
                    lambda args: args[:2] == ["image", "rm"],
                    _cp(1, stderr="Error response from daemon: No such image: keeper"),
                )
            ]
        )
        idk.remove_image_tag("keeper")

        fake_docker(
            [
                (
                    lambda args: args[:2] == ["image", "rm"],
                    _cp(1, stderr="Cannot connect to the Docker daemon"),
                )
            ]
        )
        with pytest.raises(RuntimeError, match="Cannot connect"):
            idk.remove_image_tag("keeper")

    def test_tag_image_surfaces_docker_failure(self, fake_docker):
        fake_docker([(lambda args: args[:2] == ["image", "tag"], _cp(1, stderr="no space"))])
        with pytest.raises(RuntimeError, match="no space"):
            idk.tag_image("sha256:abc", "booley-issued-" + "a" * 64 + ":session")

    def test_tag_image_surfaces_missing_docker(self, monkeypatch):
        def missing(*_args, **_kwargs):
            raise FileNotFoundError("docker")

        monkeypatch.setattr(idk, "_run_docker", missing)
        with pytest.raises(RuntimeError, match="cannot retain issued Sandbox Image"):
            idk.tag_image("sha256:abc", "booley-issued-" + "a" * 64 + ":session")

    def test_lists_only_well_formed_keeper_tags(self, fake_docker):
        keeper = "booley-issued-" + "a" * 64 + ":session"
        fake_docker(
            [
                (
                    lambda args: args[:2] == ["image", "ls"],
                    _cp(0, stdout=f"{keeper}\nubuntu:latest\nbooley-issued-bad:session\n"),
                )
            ]
        )
        assert idk.issued_image_tags() == (keeper,)


def test_external_selection_compares_exact_image_ids(monkeypatch) -> None:
    metadata = {
        "issued": image_identity.ImageMetadata("issued", "sha256:old", {}, {}, ()),
        "same": image_identity.ImageMetadata("same", "sha256:old", {}, {}, ()),
        "rebuilt": image_identity.ImageMetadata("rebuilt", "sha256:new", {}, {}, ()),
    }
    monkeypatch.setattr(
        idk,
        "inspect_image_metadata",
        lambda reference, **_kwargs: metadata.get(reference),
    )

    assert idk.compare_issued_reference("issued", "same").status is image_identity.Status.MATCH
    assert (
        idk.compare_issued_reference("issued", "rebuilt").status is image_identity.Status.MISMATCH
    )
    assert (
        idk.compare_issued_reference("issued", "missing").status is image_identity.Status.UNKNOWN
    )


# ===========================================================================
# Network
# ===========================================================================


class TestNetworkProbes:
    def test_network_is_internal_probe(self, fake_docker):
        fake_docker(
            [
                (lambda a: a[:2] == ["network", "inspect"], _cp(0, stdout="true\n")),
            ]
        )
        assert idk.network_is_internal() is True


class TestUnproxiedNetwork:
    def test_creates_routed_network_when_missing(self, fake_docker):
        fd = fake_docker([(lambda a: a[:2] == ["network", "inspect"], _cp(1))])
        assert idk.ensure_unproxied_network() is True
        assert ["network", "create", "--driver", "bridge", idk.UNPROXIED_NETWORK] in fd.calls

    def test_keeps_existing_routed_network(self, fake_docker):
        fd = fake_docker(
            [
                (
                    lambda a: a[:2] == ["network", "inspect"] and "{{.Internal}}" in a,
                    _cp(0, stdout="false\n"),
                ),
            ]
        )
        assert idk.ensure_unproxied_network() is False
        assert not fd.ran("network", "create")

    def test_rejects_internal_network_with_routed_name(self, fake_docker):
        fd = fake_docker([(lambda a: a[:2] == ["network", "inspect"], _cp(0, stdout="true\n"))])
        with pytest.raises(RuntimeError, match=f"docker network rm {idk.UNPROXIED_NETWORK}"):
            idk.ensure_unproxied_network()
        assert not fd.ran("network", "create")


# ===========================================================================
# State volumes (persistent interactive home-state)
# ===========================================================================


class TestStateVolumes:
    def test_filters_to_booley_state_volumes(self, fake_docker):
        listing = "\n".join(
            [
                "booley-claude-state-myproj",
                "booley-codex-state-other",
                "booley-cargo-registry",  # not a state volume
                "booley-pip-local",  # not a state volume
                "some-unrelated-volume",
            ]
        )
        fake_docker([(lambda a: a[:2] == ["volume", "ls"], _cp(0, stdout=listing))])
        assert idk.state_volumes() == [
            "booley-claude-state-myproj",
            "booley-codex-state-other",
        ]

    def test_empty_when_none(self, fake_docker):
        fake_docker([(lambda a: a[:2] == ["volume", "ls"], _cp(0, stdout="\n"))])
        assert idk.state_volumes() == []

    def test_empty_on_docker_error(self, fake_docker):
        fake_docker([(lambda a: a[:2] == ["volume", "ls"], _cp(1, stderr="boom"))])
        assert idk.state_volumes() == []


@pytest.mark.parametrize("output", ["short", "sha256:bad", "", "sha256:" + "A" * 64])
def test_keeper_cleanup_rejects_invalid_container_image(
    output: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def fake(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        value = "c" * 64 if args[:2] == ["container", "ls"] else output
        return subprocess.CompletedProcess(args, 0, value, "")

    monkeypatch.setattr(idk, "_run_docker", fake)
    with pytest.raises(RuntimeError, match="invalid immutable"):
        idk.container_image_ids_strict()


@pytest.mark.parametrize("operation", ["issued_image_tags", "container_image_ids_strict"])
def test_keeper_cleanup_daemon_failure_is_not_empty_inventory(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    monkeypatch.setattr(
        idk,
        "_run_docker",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "daemon unavailable"),
    )
    with pytest.raises(RuntimeError, match="daemon unavailable"):
        getattr(idk, operation)()


@pytest.mark.parametrize(
    "failure", [FileNotFoundError("docker"), subprocess.TimeoutExpired("docker", 30)]
)
@pytest.mark.parametrize("operation", ["issued_image_tags", "container_image_ids_strict"])
def test_keeper_cleanup_missing_docker_and_timeout_fail_closed(
    failure: Any, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr(idk, "_run_docker", fail)
    with pytest.raises(RuntimeError, match="cannot observe"):
        getattr(idk, operation)()


def test_keeper_cleanup_disappearing_container_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["container", "ls"]:
            return _cp(stdout="c" * 64)
        return _cp(1, stderr="No such container")

    monkeypatch.setattr(idk, "_run_docker", fake)
    with pytest.raises(RuntimeError, match="No such container"):
        idk.container_image_ids_strict()


def test_compare_issued_image_dispatch(monkeypatch, tmp_path):
    from booley.runtime import project_image

    compare = getattr(idk, "compare_issued_image", None)
    assert callable(compare), "shared image dispatch is missing"
    calls = []
    result = image_identity.Comparison(image_identity.Status.UNKNOWN)

    def selection(*args, **kwargs):
        calls.append(("selection", args, kwargs))
        return result

    def reference(*args, **kwargs):
        calls.append(("reference", args, kwargs))
        return result

    monkeypatch.setattr(idk, "compare_issued_selection", selection)
    monkeypatch.setattr(idk, "compare_issued_reference", reference)
    monkeypatch.setattr(
        project_image, "is_managed_sandbox_image", lambda root, name: name == "managed"
    )
    assert (
        compare("old-tag", "managed", project_root=tmp_path).status
        is image_identity.Status.MISMATCH
    )
    assert calls == []
    issued = "sha256:" + "a" * 64
    for configured in ("managed", "external"):
        assert (
            compare(issued, configured, project_root=tmp_path, executable="custom-docker")
            is result
        )
    assert calls == [
        (
            "selection",
            (issued, "managed"),
            {"project_root": tmp_path, "executable": "custom-docker"},
        ),
        ("reference", (issued, "external"), {"executable": "custom-docker"}),
    ]


def test_inspect_image_metadata_missing_image_is_none(fake_docker) -> None:
    for message in ("Error: No such image: gone", "Error: no such object: gone"):
        fake_docker([(lambda args: True, _cp(1, stderr=message))])

        assert idk.inspect_image_metadata("gone") is None

    # Only Docker's own "no such image" answer is absence.
    fake_docker([(lambda args: True, _cp(1, stderr="Error: No such container: gone"))])
    with pytest.raises(idk.ImageInspectError, match="No such container"):
        idk.inspect_image_metadata("gone")


def test_inspect_image_metadata_docker_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    failures: list[tuple[Any, str]] = [
        (_cp(125, stderr="Cannot connect to the Docker daemon"), "Cannot connect"),
        (_cp(1), "Docker exited 1"),
        (FileNotFoundError("docker"), "docker"),
        (subprocess.TimeoutExpired("docker", 15), "timed out"),
    ]
    for failure, cause in failures:

        def fake(
            *_args: Any, _failure: Any = failure, **_kwargs: Any
        ) -> subprocess.CompletedProcess:
            if isinstance(_failure, BaseException):
                raise _failure
            return _failure

        monkeypatch.setattr(idk, "_run_docker", fake)

        with pytest.raises(idk.ImageInspectError, match=cause) as caught:
            idk.inspect_image_metadata("keeper:tag")

        assert "'keeper:tag'" in str(caught.value)


def test_inspect_image_metadata_rejects_malformed_output(fake_docker) -> None:
    for stdout in ("not json", json.dumps([{"Id": "sha256:x", "RootFS": None}])):
        fake_docker([(lambda args: True, _cp(0, stdout=stdout))])

        with pytest.raises(idk.ImageInspectError, match="invalid metadata"):
            idk.inspect_image_metadata("keeper:tag")


def test_inspect_image_metadata_reads_layers_in_one_call(fake_docker) -> None:
    stdout = json.dumps([{"Id": "sha256:x", "RootFS": {"Layers": ["sha256:l"]}}])
    docker = fake_docker([(lambda args: True, _cp(0, stdout=stdout))])

    metadata = idk.inspect_image_metadata("keeper:tag")

    assert metadata is not None and metadata.layers == ("sha256:l",)
    assert docker.calls == [["image", "inspect", "keeper:tag"]]


def test_issued_comparisons_report_inspect_failure_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon_down = _cp(125, stderr="Cannot connect to the Docker daemon")
    monkeypatch.setattr(idk, "_run_docker", lambda *_args, **_kwargs: daemon_down)
    monkeypatch.setattr(
        image_identity,
        "current_host_build_identity",
        lambda: image_identity.BooleyBuildIdentity("1.0", None, None, None),
    )
    comparisons = {
        "build": lambda: idk.compare_issued_build("issued"),
        "reference": lambda: idk.compare_issued_reference("issued", "configured"),
        "selection": lambda: idk.compare_issued_selection("issued", "configured"),
    }

    for name, compare in comparisons.items():
        result = compare()

        assert result.status is image_identity.Status.UNKNOWN, name
        assert "Cannot connect to the Docker daemon" in result.detail, name
        assert "'issued'" in result.detail, name


def test_inspect_large_valid_metadata(monkeypatch):
    value = "x" * 1_048_577
    stdout = json.dumps([{"Id": "issued", "Config": {"Labels": {"large": value}}}])
    monkeypatch.setattr(idk, "_run_docker", lambda *_args, **_kwargs: _cp(stdout=stdout))
    result = idk.inspect_image_metadata("issued")
    assert result is not None
    assert result.labels == {"large": value}


# ---------------------------------------------------------------------------
# _run_docker timeout contract (real processes, no Docker needed)
# ---------------------------------------------------------------------------


class TestRunDockerTimeout:
    """``_run_docker`` must honor ``timeout`` and clean up what it started."""

    @pytest.fixture
    def grandchild(self, tmp_path):
        """Paths for the heartbeat grandchild; teardown stops it and waits."""
        with heartbeat_tree(tmp_path) as paths:
            yield paths

    @staticmethod
    def _run_timing_out(tmp_path, paths, mode: str) -> float:
        """Run the parent script through ``_run_docker``; return elapsed seconds."""
        # ``executable`` is the interpreter; the "docker args" are the script.
        python, *args = parent_argv(tmp_path, sys.executable, paths, mode)
        start = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            idk._run_docker(args, timeout=2, executable=python)
        return time.monotonic() - start

    def test_timeout_kills_the_whole_process_tree(self, tmp_path, grandchild):
        self._run_timing_out(tmp_path, grandchild, "live")
        heartbeat = grandchild["heartbeat"]
        assert heartbeat.exists(), "grandchild never started"
        assert wait_until_heartbeat_stops(heartbeat), "grandchild still running after the timeout"

    def test_timeout_returns_after_the_child_exits_while_a_grandchild_holds_the_pipe(
        self, tmp_path, grandchild
    ):
        elapsed = self._run_timing_out(tmp_path, grandchild, "exit")
        assert elapsed < 25, f"_run_docker hung {elapsed:.1f}s on a pipe held by a grandchild"

    @pytest.mark.skipif(os.name == "nt", reason="start_new_session is POSIX-only")
    def test_timeout_abandons_pipes_held_by_a_grandchild_that_escaped_the_group(
        self, tmp_path, grandchild
    ):
        elapsed = self._run_timing_out(tmp_path, grandchild, "escape")
        assert elapsed < 25, (
            f"_run_docker hung {elapsed:.1f}s on a pipe held by an escaped grandchild"
        )


class TestRunDockerProcess:
    """``_run_docker`` returns captured text and keeps its error contract."""

    @staticmethod
    def _python(code: str):
        return idk._run_docker(["-c", code], timeout=30, executable=sys.executable)

    def test_returns_text_output_for_success(self):
        result = self._python("import sys; print('out'); print('err', file=sys.stderr)")
        assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (
            0,
            "out",
            "err",
        )

    def test_non_zero_exit_is_returned_not_raised(self):
        result = self._python("import sys; sys.exit(3)")
        assert result.returncode == 3

    def test_missing_executable_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            idk._run_docker(["version"], timeout=5, executable=str(tmp_path / "no-such-docker"))
