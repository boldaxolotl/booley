"""Canonical credential links and private per-invocation configuration lifetime."""

import json
import tomllib
from pathlib import Path

import pytest
from tests.runtime.scratch_support import assert_native_workspace_lifetimes


@pytest.fixture
def synthetic_nested_environment(tmp_path, monkeypatch):
    """Keep legacy fixed /tmp allocations and future registry hooks inside this test."""

    from booley.runtime import _codex_backend as cb

    original = tmp_path / "developer"
    canonical = original / ".codex"
    canonical.mkdir(parents=True)
    (canonical / "auth.json").write_text('{"synthetic": "credential"}')
    (canonical / "models_cache.json").write_text(json.dumps({"models": [{"slug": "chosen"}]}))
    monkeypatch.setenv("HOME", str(original))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("BOOLEY_SCRATCH_ROOT", str(tmp_path / "registry"))
    monkeypatch.setattr(cb, "_inside_container", lambda: True)
    return canonical


def test_nested_auth_resolves_to_canonical_file(synthetic_nested_environment):
    from booley.core.models import AgentCallParams
    from booley.runtime import _codex_backend as cb

    params = AgentCallParams(prompt="synthetic", model="chosen", cwd=synthetic_nested_environment)
    with cb._invocation_home() as home:
        cb._prepare_private_home(params, home)
        assert (home / ".codex/auth.json").resolve() == synthetic_nested_environment / "auth.json"


def test_text_only_auth_resolves_to_canonical_file(tmp_path, synthetic_nested_environment):
    from booley.core.models import AgentCallParams
    from booley.runtime.text_only_agent import prepare_codex_text_only

    params = AgentCallParams(prompt="synthetic", model="chosen", cwd=tmp_path, text_only=True)
    _, environment = prepare_codex_text_only(
        ["codex", "exec", "-"],
        params,
        {"CODEX_HOME": str(synthetic_nested_environment)},
        private=tmp_path / "private",
    )
    assert Path(environment["CODEX_HOME"], "auth.json").resolve() == (
        synthetic_nested_environment / "auth.json"
    )


@pytest.mark.asyncio
async def test_completed_cli_call_removes_private_home(tmp_path, synthetic_nested_environment):
    import sys

    from booley.core.models import AgentCallParams
    from booley.runtime import _codex_backend as cb

    report = tmp_path / "child-report.json"
    script = tmp_path / "fake_codex.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "sys.stdin.read()\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'home': os.environ['HOME'], "
        "'codex_home': os.environ.get('CODEX_HOME')}))\n"
        "print(json.dumps({'type': 'item.completed', 'item': "
        "{'type': 'agent_message', 'text': 'done'}}))\n"
    )
    params = AgentCallParams(prompt="synthetic", model="chosen", cwd=tmp_path)
    for _ in range(3):
        _stdout, _stderr, status = await cb._codex_run_subprocess(
            [sys.executable, str(script), str(report)],
            params,
            full_prompt="synthetic",
            on_event=None,
        )
        assert status == 0
        child = json.loads(report.read_text())
        assert_native_workspace_lifetimes([Path(child["home"])])


@pytest.mark.parametrize("preference", [True, False])
def test_fresh_homes_use_canonical_preference_without_user_tools(
    synthetic_nested_environment, preference
):
    from booley.core.models import AgentCallParams
    from booley.runtime import _codex_backend as cb

    (synthetic_nested_environment / "config.toml").write_text(
        f"suppress_unstable_features_warning={str(preference).lower()}\n"
        '[mcp_servers.foreign]\ncommand="untrusted"\n'
    )
    params = AgentCallParams(prompt="synthetic", model="chosen", cwd=synthetic_nested_environment)
    homes = []
    for _ in range(2):
        with cb._invocation_home() as home:
            homes.append(home)
            cb._prepare_private_home(params, home)
            config = tomllib.loads((home / ".codex/config.toml").read_text())
            assert config["suppress_unstable_features_warning"] is preference
            assert "foreign" not in config.get("mcp_servers", {})
        assert not home.exists()
    assert homes[0] != homes[1]


def test_private_links_follow_canonical_replacement_and_write_through(
    synthetic_nested_environment, tmp_path
):
    from booley.runtime import _codex_backend as cb

    homes = [tmp_path / "first", tmp_path / "second"]
    for home in homes:
        cb._populate_private_codex_home(home, {}, [], canonical=synthetic_nested_environment)
    shared = synthetic_nested_environment / "auth.json"
    (homes[0] / "auth.json").write_text('{"synthetic":"updated"}')
    assert (homes[1] / "auth.json").read_bytes() == shared.read_bytes()
    replacement = synthetic_nested_environment / "replacement"
    replacement.write_text('{"synthetic":"replacement"}')
    replacement.replace(shared)
    assert all((home / "auth.json").read_bytes() == shared.read_bytes() for home in homes)


def test_private_destination_update_preserves_boolean_preference(tmp_path):
    from booley.runtime import _codex_backend as cb

    private = tmp_path / "private"
    cb._populate_private_codex_home(private, {}, [])
    (private / "config.toml").write_text("suppress_unstable_features_warning=false\n")
    cb._populate_private_codex_home(private, {}, [])
    assert (
        tomllib.loads((private / "config.toml").read_text())["suppress_unstable_features_warning"]
        is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["timeout", "cancel", "reader_error"])
async def test_actual_cli_failure_reaps_and_removes_home(
    tmp_path, synthetic_nested_environment, monkeypatch, termination
):
    import asyncio
    import sys

    from booley.core.models import AgentCallParams
    from booley.runtime import _codex_backend as cb
    from booley.runtime.agent_errors import AgentTimeoutError

    report = tmp_path / "failure-home"
    script = tmp_path / "fake-hanging-codex.py"
    script.write_text(_HANGING_CODEX_SCRIPT)
    params = AgentCallParams(prompt="synthetic", model="chosen", cwd=tmp_path, timeout_seconds=1)
    if termination == "reader_error":

        async def broken_read(*args):
            deadline = asyncio.get_running_loop().time() + 5
            while not report.exists() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            raise ValueError("synthetic stream error")

        monkeypatch.setattr(cb, "_read_stdout", broken_read)
    task = asyncio.create_task(
        cb._codex_run_subprocess(
            [sys.executable, str(script), str(report)],
            params,
            full_prompt="synthetic",
            on_event=None,
        )
    )
    deadline = asyncio.get_running_loop().time() + 5
    while not report.exists() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert report.exists()
    home = Path(report.read_text())
    assert home.is_dir()
    if termination == "cancel":
        task.cancel()
        expected = asyncio.CancelledError
    elif termination == "reader_error":
        expected = ValueError
    else:
        expected = AgentTimeoutError
    with pytest.raises(expected):
        await asyncio.wait_for(task, timeout=8)
    assert_native_workspace_lifetimes([home])
    assert set((tmp_path / "registry").glob("booley-owned-codex-home-*")) == (
        {home} if home.exists() else set()
    )


@pytest.mark.asyncio
async def test_concurrent_calls_cleanup_only_the_completed_home(
    tmp_path, synthetic_nested_environment
):
    import asyncio
    import sys

    from booley.core.models import AgentCallParams
    from booley.runtime import _codex_backend as cb

    script = tmp_path / "fake-concurrent-codex.py"
    script.write_text(_CONCURRENT_CODEX_SCRIPT)
    reports = [tmp_path / f"home-{index}" for index in range(2)]
    releases = [tmp_path / f"release-{index}" for index in range(2)]
    tasks = [
        asyncio.create_task(
            cb._codex_run_subprocess(
                [sys.executable, str(script), str(report), str(release)],
                AgentCallParams(prompt="synthetic", model="chosen", cwd=tmp_path),
                full_prompt="synthetic",
                on_event=None,
            )
        )
        for report, release in zip(reports, releases, strict=True)
    ]
    try:
        deadline = asyncio.get_running_loop().time() + 5
        while (
            not all(report.exists() for report in reports)
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)
        assert all(report.exists() for report in reports)
        homes = [Path(report.read_text()) for report in reports]
        assert homes[0] != homes[1]
        releases[0].touch()
        await asyncio.wait_for(tasks[0], timeout=5)
        assert_native_workspace_lifetimes([homes[0]])
        assert homes[1].is_dir()
        releases[1].touch()
        await asyncio.wait_for(tasks[1], timeout=5)
        assert_native_workspace_lifetimes([homes[1]])
    finally:
        for release in releases:
            release.touch()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=8)


def test_mutation_native_sessions_survive_fresh_homes_and_lock_wipe(
    tmp_path, synthetic_nested_environment, monkeypatch
):
    from booley.core.models import AgentCallParams
    from booley.dev_support import mutation_lock
    from booley.runtime import _codex_backend as cb

    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    sessions = mutation_lock.lock_dir() / "native-codex-sessions"
    params = AgentCallParams(
        prompt="synthetic", model="chosen", cwd=tmp_path, session_state_dir=sessions
    )
    homes = []
    for index in range(2):
        with cb._invocation_home() as home:
            homes.append(home)
            cb._prepare_private_home(params, home)
            native = home / ".codex/sessions"
            assert native.resolve() == sessions
            if index == 0:
                (native / "synthetic-rollout.jsonl").write_text("synthetic native transcript")
            else:
                assert (
                    native / "synthetic-rollout.jsonl"
                ).read_text() == "synthetic native transcript"
        assert not home.exists()
    assert homes[0] != homes[1]
    assert not (synthetic_nested_environment / "sessions").exists()
    mutation_lock.wipe_lock()
    assert not sessions.exists()


def test_installed_native_file_writer_follows_auth_link(tmp_path, synthetic_nested_environment):
    import os
    import shutil
    import subprocess

    from booley.core.models import AgentCallParams
    from booley.runtime import _codex_backend as cb

    executable = shutil.which("codex")
    if executable is None:
        pytest.skip("native Codex executable unavailable for offline file-writer contract")
    with cb._invocation_home() as home:
        cb._prepare_private_home(AgentCallParams(prompt="", model="chosen", cwd=tmp_path), home)
        private = home / ".codex"
        env = {"PATH": os.defpath, "HOME": str(home), "CODEX_HOME": str(private)}
        result = subprocess.run(
            [executable, "login", "--with-api-key"],
            input="synthetic-offline-key\n",
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert (private / "auth.json").is_symlink()
        stored = json.loads((synthetic_nested_environment / "auth.json").read_text())
        assert stored["OPENAI_API_KEY"] == "synthetic-offline-key"


def test_installed_native_uuid_resume_finds_lock_owned_rollout_in_fresh_home(
    tmp_path, synthetic_nested_environment
):
    import os
    import shutil
    import subprocess
    import uuid

    from booley.core.models import AgentCallParams
    from booley.core.temporary_workspace import run_guarded
    from booley.runtime import _codex_backend as cb

    executable = shutil.which("codex")
    if executable is None:
        pytest.skip("native Codex executable unavailable for offline UUID lookup contract")
    sessions = tmp_path / "mutation-lock" / "native-codex-sessions"
    params = AgentCallParams(
        prompt="", model="synthetic", cwd=tmp_path, session_state_dir=sessions
    )
    identity = str(uuid.uuid4())
    with cb._invocation_home() as home:
        cb._prepare_private_home(params, home)
        rollout = _seed_native_rollout(home, identity, tmp_path)
    with cb._invocation_home() as fresh:
        cb._prepare_private_home(params, fresh)
        private = fresh / ".codex"
        (private / "config.toml").write_text(
            'model="synthetic"\nmodel_provider="offline"\n[model_providers.offline]\n'
            'name="offline"\nbase_url="http://127.0.0.1:1/v1"\nwire_api="responses"\n'
            "request_max_retries=0\nstream_max_retries=0\n"
        )
        env = _offline_codex_environment(fresh, private, os.defpath)
        # Native lookup emits thread.started before its forced loopback-only
        # request. Timeout bounds that request and the real guardian reaps it.
        with pytest.raises(subprocess.TimeoutExpired) as outcome:
            run_guarded(
                [executable, "exec", "resume", "--skip-git-repo-check", "--json", identity, "-"],
                input="synthetic offline continue",
                env=env,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=3,
            )
        records = [json.loads(line) for line in outcome.value.output.splitlines()]
        assert {"type": "thread.started", "thread_id": identity} in records
    assert_native_workspace_lifetimes([fresh])
    assert rollout.name in {path.name for path in sessions.iterdir()}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["preparation", "parse", "spawn"])
async def test_backend_call_failure_always_releases_private_home(
    tmp_path, synthetic_nested_environment, monkeypatch, failure
):
    import sys

    from booley.core.models import AgentCallParams
    from booley.runtime import _codex_backend as cb

    observed = []
    params = AgentCallParams(prompt="synthetic", model="chosen", cwd=tmp_path)

    def command(*args, **kwargs):
        observed.append(cb._PRIVATE_HOME.get())
        if failure == "preparation":
            raise ValueError("synthetic preparation failure")
        return [sys.executable, "-c", 'import sys; sys.stdin.read(); print("synthetic")'], None

    def parse(*args):
        raise ValueError("synthetic parse failure")

    async def spawn(*args, **kwargs):
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(cb, "_codex_build_cmd", command)
    if failure == "parse":
        monkeypatch.setattr(cb, "_codex_parse_events", parse)
    if failure == "spawn":
        monkeypatch.setattr(cb.asyncio, "create_subprocess_exec", spawn)
    expected = OSError if failure == "spawn" else ValueError
    with pytest.raises(expected, match=f"synthetic {failure} failure"):
        await cb.CodexBackend()._call_once(params, full_prompt="synthetic", transcript_path=None)
    assert observed
    if failure == "preparation":
        assert all(not home.exists() for home in observed)
    else:
        assert_native_workspace_lifetimes(observed)


def test_explicit_canonical_home_is_only_auth_source_and_metadata_is_private(
    tmp_path, synthetic_nested_environment, monkeypatch
):
    from booley.core.models import AgentCallParams
    from booley.runtime import _codex_backend as cb

    canonical = tmp_path / "explicit-canonical"
    canonical.mkdir()
    (canonical / "auth.json").write_text('{"synthetic":"explicit canonical"}')
    (canonical / "models_cache.json").write_text('{"models":[{"slug":"chosen"}]}')
    (canonical / "version.json").write_text('{"synthetic":"metadata"}')
    monkeypatch.setenv("CODEX_HOME", str(canonical))
    with cb._invocation_home() as home:
        env = cb._prepare_private_home(
            AgentCallParams(prompt="", model="chosen", cwd=tmp_path), home
        )
        private = Path(env["CODEX_HOME"])
        assert private != canonical
        assert (private / "auth.json").resolve() == canonical / "auth.json"
        assert (private / "auth.json").resolve() != synthetic_nested_environment / "auth.json"
        for name in ("models_cache.json", "version.json"):
            assert not (private / name).is_symlink()
            assert (private / name).read_bytes() == (canonical / name).read_bytes()
            (private / name).write_text("private metadata changed")
            assert (canonical / name).read_text() != "private metadata changed"


@pytest.mark.asyncio
async def test_actual_retry_uses_fresh_home_and_cleans_each_completed_attempt(
    tmp_path, synthetic_nested_environment, monkeypatch
):
    import sys

    from booley.core.models import AgentCallParams
    from booley.runtime import _codex_backend as cb

    script = tmp_path / "fake-retrying-codex.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "sys.stdin.read()\n"
        'pathlib.Path(sys.argv[1]).write_text(os.environ["HOME"])\n'
        'event = {"type":"error", "message":"connection reset"} if sys.argv[2] == "0" else '
        '{"type":"item.completed", "item":{"type":"agent_message", "text":"done"}}\n'
        "print(json.dumps(event))\n"
    )
    reports = []

    def command(*args, **kwargs):
        index = len(reports)
        for earlier in reports:
            assert_native_workspace_lifetimes([Path(earlier.read_text())])
        report = tmp_path / f"retry-home-{index}"
        reports.append(report)
        return [sys.executable, str(script), str(report), str(index)], None

    monkeypatch.setattr(cb, "_codex_build_cmd", command)
    monkeypatch.setattr(cb, "compute_backoff", lambda _: 0)
    result = await cb.CodexBackend().call(
        AgentCallParams(prompt="synthetic", model="chosen", cwd=tmp_path)
    )
    assert result.output == "done"
    assert len(reports) == 2
    homes = [Path(report.read_text()) for report in reports]
    assert homes[0] != homes[1]
    assert_native_workspace_lifetimes(homes)


def test_text_only_relative_auth_source_and_explicit_scratch_handoff(
    tmp_path, synthetic_nested_environment, monkeypatch
):
    from booley.core.models import AgentCallParams
    from booley.core.temporary_workspace import registry_root
    from booley.runtime.text_only_agent import prepare_codex_text_only

    monkeypatch.chdir(synthetic_nested_environment.parent)
    private = tmp_path / "private"
    params = AgentCallParams(
        prompt="",
        model="chosen",
        cwd=tmp_path,
        text_only=True,
        nested_mcp_tools=["coverage_evidence"],
    )
    _, env = prepare_codex_text_only(
        ["codex", "exec", "-"],
        params,
        {"CODEX_HOME": ".codex"},
        private=private,
    )
    assert (
        Path(env["CODEX_HOME"]) / "auth.json"
    ).resolve() == synthetic_nested_environment / "auth.json"
    config = tomllib.loads((private / "config.toml").read_text())
    assert config["mcp_servers"]["booley"]["env"]["BOOLEY_SCRATCH_ROOT"] == str(registry_root())


@pytest.mark.asyncio
async def test_missing_native_codex_preserves_lookup_error_and_ownership_policy(
    tmp_path, synthetic_nested_environment
):
    from booley.core.models import AgentCallParams
    from booley.runtime import _codex_backend as cb

    with pytest.raises(FileNotFoundError):
        await cb._codex_run_subprocess(
            [str(tmp_path / "missing-native-codex")],
            AgentCallParams(prompt="synthetic", model="chosen", cwd=tmp_path),
            full_prompt="synthetic",
            on_event=None,
        )
    homes = list((tmp_path / "registry").glob("booley-owned-codex-home-*"))
    from tests.runtime.scratch_support import scratch_process_ownership_supported

    assert len(homes) == (0 if scratch_process_ownership_supported() else 1)
    assert_native_workspace_lifetimes(homes)


def _seed_native_rollout(home: Path, identity: str, tmp_path: Path) -> Path:
    rollout = home / ".codex/sessions" / f"rollout-2026-10-15T00-00-00-{identity}.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-10-15T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": identity,
                    "timestamp": "2026-10-15T00:00:00Z",
                    "cwd": str(tmp_path),
                    "originator": "codex_cli_rs",
                    "cli_version": "0.160.0",
                    "source": "cli",
                    "model_provider": "offline",
                },
            }
        )
        + "\n"
    )
    return rollout


_HANGING_CODEX_SCRIPT = (
    "import os, pathlib, sys, time\n"
    "sys.stdin.read()\n"
    'pathlib.Path(sys.argv[1]).write_text(os.environ["HOME"])\n'
    "time.sleep(15)\n"
)


_CONCURRENT_CODEX_SCRIPT = (
    "import os, pathlib, sys, time\n"
    "sys.stdin.read()\n"
    "report, release = map(pathlib.Path, sys.argv[1:])\n"
    'report.write_text(os.environ["HOME"])\n'
    "end = time.monotonic() + 15\n"
    "while not release.exists() and time.monotonic() < end: time.sleep(.01)\n"
)


def _offline_codex_environment(fresh, private, path):
    return {
        "PATH": path,
        "HOME": str(fresh),
        "CODEX_HOME": str(private),
        "OPENAI_API_KEY": "synthetic-offline-key",
    }
