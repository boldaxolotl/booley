"""Tests for booley.runtime.runtime_context -- the Sandbox context guard (ADR 0028)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from booley.core import sandbox_detection
from booley.runtime import runtime_context

# ---------------------------------------------------------------------------
# Runtime simulation helpers. Tests run on a host machine today, but the suite
# could someday run inside CI containers -- so every test pins BOTH signals
# (env marker + filesystem probes) instead of trusting the real environment.
# ---------------------------------------------------------------------------


def _pin_runtime(
    monkeypatch: pytest.MonkeyPatch, *, env: str | None, marker_file: str | None = None
) -> None:
    """Pin the env marker to *env* (unset for ``None``) and the one probe file that exists."""
    if env is None:
        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    else:
        monkeypatch.setenv("BOOLEY_CONTAINER", env)
    # as_posix(): plain str() renders "\\.dockerenv" on Windows and the
    # comparison silently never matches (F-16).
    monkeypatch.setattr(
        Path, "exists", lambda self: marker_file is not None and self.as_posix() == marker_file
    )


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the host: env marker unset, no container marker files."""
    _pin_runtime(monkeypatch, env=None)


@pytest.fixture
def container_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the sandbox via the baked-in env marker (primary signal)."""
    _pin_runtime(monkeypatch, env="1")


# ===========================================================================
# inside_session_runtime()
# ===========================================================================


@pytest.mark.parametrize(
    ("env", "marker_file", "inside"),
    [
        (None, None, False),
        ("1", None, True),
        (None, "/.dockerenv", True),  # images built before the env var existed
        (None, "/run/.containerenv", True),  # Podman/OCI runtimes
    ],
    ids=["host", "env-marker", "dockerenv", "containerenv"],
)
def test_inside_session_runtime_reads_the_marker_then_the_probe_files(
    monkeypatch: pytest.MonkeyPatch, env: str | None, marker_file: str | None, inside: bool
):
    _pin_runtime(monkeypatch, env=env, marker_file=marker_file)
    assert runtime_context.inside_session_runtime() is inside


# ===========================================================================
# ensure_proxy_env()
# ===========================================================================

# The environment the Session's devcontainer spec sets (session_issuance remoteEnv)
# and ensure_proxy_env restores when a bare docker exec drops it.
PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
PROXY_URL = "http://booley-proxy:8080"


class TestEnsureProxyEnv:
    """docker-exec entry drops the spec's remoteEnv — the self-heal must
    default the proxy vars in the sandbox, and ONLY in the sandbox."""

    @pytest.fixture(autouse=True)
    def _no_inherited_proxy_config(self, monkeypatch):
        for var in (*PROXY_ENV_VARS, "NO_PROXY", "BOOLEY_EGRESS"):
            monkeypatch.delenv(var, raising=False)

    @pytest.mark.parametrize(
        "declared",
        [{}, {"BOOLEY_EGRESS": "proxy"}, {"CODESPACES": "true"}],
        ids=["unset", "proxy", "codespaces-signal"],
    )
    def test_defaults_all_proxy_vars_in_sandbox(self, container_env, monkeypatch, declared):
        for name, value in declared.items():
            monkeypatch.setenv(name, value)
        assert runtime_context.ensure_proxy_env() is True
        for var in PROXY_ENV_VARS:
            assert os.environ[var] == PROXY_URL
        assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"

    def test_unproxied_mode_sets_no_proxy_vars(self, container_env, monkeypatch):
        monkeypatch.setenv("BOOLEY_EGRESS", "unproxied")
        assert runtime_context.ensure_proxy_env() is False
        assert not any(var in os.environ for var in (*PROXY_ENV_VARS, "NO_PROXY"))

    @pytest.mark.parametrize("value", ["bogus", ""], ids=["bogus", "empty"])
    def test_invalid_mode_refuses(self, container_env, monkeypatch, value):
        monkeypatch.setenv("BOOLEY_EGRESS", value)
        with pytest.raises(runtime_context.EgressModeError, match=r"`proxy`.*`unproxied`"):
            runtime_context.ensure_proxy_env()

    @pytest.mark.parametrize(
        "marker_file", [None, "/.dockerenv"], ids=["host", "foreign-container"]
    )
    def test_noop_outside_the_sandbox(self, monkeypatch, marker_file):
        """/.dockerenv alone is NOT enough — a foreign container has no
        booley-proxy, and forcing a dead proxy on it would break egress; the host
        has none either."""
        _pin_runtime(monkeypatch, env=None, marker_file=marker_file)
        assert runtime_context.ensure_proxy_env() is False
        assert "HTTPS_PROXY" not in os.environ

    def test_existing_proxy_config_is_respected_wholesale(self, container_env, monkeypatch):
        """Any pre-set proxy var means the entry path DID deliver config —
        partially overwriting it could mix two proxy setups."""
        monkeypatch.setenv("https_proxy", "http://corp-proxy:3128")
        assert runtime_context.ensure_proxy_env() is False
        if os.name == "nt":
            # Windows environment keys are case-insensitive: HTTPS_PROXY and
            # https_proxy are the same entry.
            assert os.environ["HTTPS_PROXY"] == "http://corp-proxy:3128"
        else:
            assert "HTTPS_PROXY" not in os.environ
        assert os.environ["https_proxy"] == "http://corp-proxy:3128"


# ===========================================================================
# agent_session_app()
# ===========================================================================


class TestAgentSessionApp:
    """Which agent CLI spawned us — the other half of "host agent, no Booley Flows"."""

    def test_none_in_a_plain_shell(self):
        assert runtime_context.agent_session_app() is None

    @pytest.mark.parametrize(
        ("marker", "app"),
        [
            ("CLAUDECODE", "claude"),
            ("CLAUDE_CODE_ENTRYPOINT", "claude"),
            ("CODEX_SANDBOX", "codex"),
            ("CODEX_HOME", "codex"),
        ],
    )
    def test_markers_name_the_agent_cli(self, monkeypatch, marker, app):
        monkeypatch.setenv(marker, "1")
        assert runtime_context.agent_session_app() == app

    def test_empty_marker_does_not_count(self, monkeypatch):
        """An exported-but-empty var is not a session."""
        monkeypatch.setenv("CLAUDECODE", "")
        assert runtime_context.agent_session_app() is None


# ===========================================================================
# container_only_error() and host_only_error()
# ===========================================================================


@pytest.mark.parametrize(
    ("guard", "own_side", "other_side", "fixes"),
    [
        (
            runtime_context.container_only_error,
            "1",
            None,
            ("Reopen in Container", "booley session enter -- bwave"),
        ),
        (runtime_context.host_only_error, None, "1", ("HOST terminal", "BOOLEY_CONTAINER=0")),
    ],
    ids=["container-only", "host-only"],
)
def test_venue_guard_refuses_only_on_the_wrong_side(
    monkeypatch: pytest.MonkeyPatch,
    guard,
    own_side: str | None,
    other_side: str | None,
    fixes: tuple[str, ...],
):
    _pin_runtime(monkeypatch, env=own_side)
    assert guard("bwave") is None

    _pin_runtime(monkeypatch, env=other_side)
    message = guard("bwave")
    assert message is not None
    assert message.startswith("ERROR: `bwave` ")
    for fix in fixes:
        assert fix in message
    assert "`booley --help`" in message
    # The refusal points at the command-location source instead of listing commands (#1236);
    # the heading line names the guarded command, so only the instructions are checked.
    instructions = "\n".join(message.splitlines()[1:])
    assert re.findall(r"`booley ([a-z]+)`", instructions) == []


# ===========================================================================
# runtime_location() and booley_init_hint() (issue 2751)
# ===========================================================================


def _mountinfo_line(mount_point: str) -> str:
    """One /proc/self/mountinfo record whose fifth field is *mount_point*."""
    return f"36 35 98:0 /src {mount_point} rw,noatime master:1 - ext4 /dev/root rw\n"


def _mountinfo(*mount_points: str) -> str:
    return "".join(_mountinfo_line(point) for point in mount_points)


def _use_mountinfo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str | None) -> None:
    """Point the location probe at a mountinfo file holding *text* (absent for ``None``)."""
    mountinfo = tmp_path / "mountinfo"
    if text is not None:
        mountinfo.write_bytes(text.encode())
    # private: /proc/self/mountinfo is the kernel's mount table; no public input moves the probe.
    monkeypatch.setattr(sandbox_detection, "_MOUNTINFO", mountinfo)


ISSUED = runtime_context.RuntimeLocation.ISSUED_SANDBOX
HAND_WRITTEN = runtime_context.RuntimeLocation.HAND_WRITTEN_SANDBOX


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_mountinfo("/", "/work", "/work/.devcontainer"), ISSUED),
        (_mountinfo("/", "/booley"), ISSUED),
        (_mountinfo("/", "/work/.booley"), ISSUED),
        (_mountinfo("/", "/booley/masked"), ISSUED),
        (_mountinfo("/", "/work", "/workspaces/repo", "/work/.booleyx"), HAND_WRITTEN),
        ("", HAND_WRITTEN),
        # Short records are skipped.
        ("garbage\n\n" + _mountinfo("/mnt"), HAND_WRITTEN),
        # A false "issued" verdict only causes a refusal, so unreadable means issued.
        (None, ISSUED),
    ],
    ids=[
        "devcontainer",
        "booley",
        "work-booley",
        "under-booley",
        "lookalike",
        "empty",
        "short-records",
        "unreadable",
    ],
)
def test_runtime_location_reads_the_mounts(
    container_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    text: str | None,
    expected: runtime_context.RuntimeLocation,
) -> None:
    _use_mountinfo(monkeypatch, tmp_path, text)
    assert runtime_context.runtime_location() is expected


def test_runtime_location_on_host_never_reads_mountinfo(
    host: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A host whose mountinfo happens to list an issued target is still the host.
    _use_mountinfo(monkeypatch, tmp_path, _mountinfo("/work/.devcontainer"))
    assert runtime_context.runtime_location() is runtime_context.RuntimeLocation.HOST


@pytest.mark.parametrize(
    ("env", "mounts", "plain", "code"),
    [
        (None, ("/work/.devcontainer",), "booley init", "`booley init`"),
        ("1", ("/",), "booley init in this Sandbox", "`booley init` in this Sandbox"),
        ("1", ("/", "/booley"), "booley init on the host", "`booley init` on the host"),
    ],
    ids=["host", "hand-written-sandbox", "issued-sandbox"],
)
def test_booley_init_hint_names_the_location(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env: str | None,
    mounts: tuple[str, ...],
    plain: str,
    code: str,
) -> None:
    _pin_runtime(monkeypatch, env=env)
    _use_mountinfo(monkeypatch, tmp_path, _mountinfo(*mounts))
    assert runtime_context.booley_init_hint() == plain
    assert runtime_context.booley_init_hint(code=True) == code


@pytest.mark.parametrize(
    ("env", "marker_file", "mounts", "location"),
    [
        ("0", "/.dockerenv", ("/",), runtime_context.RuntimeLocation.HOST),
        ("0", "/.dockerenv", None, runtime_context.RuntimeLocation.HOST),
        ("0", None, ("/",), runtime_context.RuntimeLocation.HOST),
        (None, "/.dockerenv", ("/",), HAND_WRITTEN),
        ("0", "/.dockerenv", ("/", "/booley"), ISSUED),
        ("0", None, ("/", "/work/.devcontainer"), ISSUED),
    ],
    ids=[
        "override",
        "override-unreadable-mounts",
        "override-no-marker",
        "no-override",
        "override-in-issued-sandbox",
        "override-in-issued-sandbox-no-marker",
    ],
)
def test_container_host_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env: str | None,
    marker_file: str | None,
    mounts: tuple[str, ...] | None,
    location: runtime_context.RuntimeLocation,
) -> None:
    _pin_runtime(monkeypatch, env=env, marker_file=marker_file)
    _use_mountinfo(monkeypatch, tmp_path, None if mounts is None else _mountinfo(*mounts))
    is_host = location is runtime_context.RuntimeLocation.HOST
    assert runtime_context.runtime_location() is location
    assert runtime_context.inside_session_runtime() is not is_host
    assert (runtime_context.host_only_error("booley session") is None) is is_host
    assert (runtime_context.container_only_error("booley flow sim") is None) is not is_host
