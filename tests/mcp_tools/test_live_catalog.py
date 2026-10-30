"""A running server observes Project capability changes without losing dispatch state."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import Client, MCPError
from mcp.types import TextContent

from booley.mcp import server
from booley.runtime.booley_dir import reset_cache


@pytest.fixture
def project(tmp_path, monkeypatch):
    directory = tmp_path / ".booley"
    directory.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_DIR", str(directory))
    for name in ("BOOLEY_NESTED_AGENT", "BOOLEY_MCP_TOOLS", "BOOLEY_COVERAGE_CAMPAIGN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    monkeypatch.setattr(server, "_reconcile_orphaned_jobs", lambda: None)
    reset_cache()
    yield directory
    reset_cache()


def write_endpoint(directory: Path, name="local_check", *, family="McpTool", option="first"):
    custom = directory / "mcp_tools"
    custom.mkdir(exist_ok=True)
    imports = {
        "McpTool": "booley.mcp.base",
        "BooleyFlow": "booley.flows.base",
        "Specialist": "booley.specialists.specialist",
    }
    path = custom / f"{name}.py"
    path.write_text(
        f"from {imports[family]} import {family}\n"
        f"class Local({family}):\n"
        f"    name = {name!r}\n"
        "    description = 'Local capability'\n"
        "    def _add_args(self, parser):\n"
        f"        parser.add_argument('--{option}', help='Current option')\n"
        "    def _run(self, args): pass\n"
        "    def _build_prompt(self, args): return ''\n"
        "    def _interpret_output(self, output, args): pass\n"
    )
    return path


@asynccontextmanager
async def live_client(sdk):
    """Expose assertion failures after the SDK's task-group teardown completes."""
    failure = None
    try:
        async with Client(sdk, mode="2026-07-28") as client:
            yield client
    except* AssertionError as group:
        failure = group
    if failure is not None:
        while isinstance(failure, BaseExceptionGroup):
            assert len(failure.exceptions) == 1
            failure = failure.exceptions[0]
        raise failure from None


async def tools(client):
    return {item.name: item for item in (await client.list_tools()).tools}


def test_audited_capabilities_refresh_on_running_server(project):
    write_endpoint(project, "local_flow", family="BooleyFlow")
    write_endpoint(project, "local_specialist", family="Specialist")
    sdk, _ = server._build_server()

    async def exercise():
        async with live_client(sdk) as client:
            initial = await tools(client)
            from booley.mcp.registry import discover_mcp_tools

            endpoints = discover_mcp_tools(project_mcp_tools_dir=project / "mcp_tools")
            configurable = [item for item in endpoints if item.kind != "mcp_tool"]
            assert {"sim", "lint", "synth", "fpga", "local_flow", "local_specialist"} <= set(
                initial
            )
            config = project / "booley.toml"
            for item in configurable:
                if item.name not in initial:
                    continue
                table = "flows" if item.kind == "flow" else "specialists"
                config.write_text(f"[{table}.{item.name}]\nenabled = false\n")
                assert item.name not in await tools(client), (
                    "running catalog must hide currently disabled endpoint"
                )
                config.write_text(f"[{table}.{item.name}]\nenabled = true\n")
                assert (await tools(client))[item.name] == initial[item.name]

    asyncio.run(exercise())


def test_custom_endpoint_set_and_schema_refresh(project):
    sdk, _ = server._build_server()

    async def exercise():
        async with live_client(sdk) as client:
            for family in ("McpTool", "BooleyFlow", "Specialist"):
                name = "custom_" + family.lower()
                path = write_endpoint(project, name, family=family)
                listed = await tools(client)
                assert name in listed, (
                    "running catalog must rediscover current custom endpoint schema"
                )
                assert "first" in listed[name].input_schema["properties"]
                write_endpoint(project, name, family=family, option="second")
                listed = await tools(client)
                assert "second" in listed[name].input_schema["properties"]
                path.rename(path.with_name("_ignored.py"))
                assert name not in await tools(client)
                path.with_name("_ignored.py").unlink()

    asyncio.run(exercise())


def test_custom_endpoint_refresh_in_redirected_booley_directory(project, monkeypatch):
    monkeypatch.delenv("BOOLEY_DIR")
    redirected = project.parent / "selected"
    redirected.mkdir()
    (project.parent / "booley.toml").write_text('[project]\ndir = "selected"\n')
    project.rmdir()
    reset_cache()
    sdk, _ = server._build_server()

    async def exercise():
        async with live_client(sdk) as client:
            path = write_endpoint(redirected)
            assert "local_check" in await tools(client), (
                "running catalog must rediscover endpoints in the selected Booley Directory"
            )
            path.unlink()
            assert "local_check" not in await tools(client)

    asyncio.run(exercise())


def test_retired_custom_directory_change_refuses_then_recovers(project):
    sdk, _ = server._build_server()

    async def exercise():
        async with live_client(sdk) as client:
            retired = project / "tools"
            retired.mkdir()
            rejected = False
            try:
                await client.list_tools()
            except MCPError:
                rejected = True
            assert rejected, "running catalog must reject retired endpoint directory"
            retired.rmdir()
            assert "sim" in await tools(client)

    asyncio.run(exercise())


def test_config_is_read_even_with_preserved_mtime(project):
    config = project / "booley.toml"
    config.write_text("[flows.sim]\nenabled = true\n")
    before = config.stat()
    sdk, _ = server._build_server()

    async def exercise():
        async with live_client(sdk) as client:
            config.write_text("[flows.sim]\nenabled = false\n")
            os.utime(config, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert "sim" not in await tools(client), (
                "running catalog must hide currently disabled endpoint"
            )

    asyncio.run(exercise())


def test_invalid_config_is_not_served_as_stale_enabled_catalog(project):
    sdk, _ = server._build_server()

    async def exercise():
        async with live_client(sdk) as client:
            for invalid in ("[flows", "[tools.sim]\nenabled = false\n"):
                (project / "booley.toml").write_text(invalid)
                rejected = False
                try:
                    await client.list_tools()
                except MCPError:
                    rejected = True
                assert rejected, "running catalog must reject invalid configuration"
            (project / "booley.toml").write_text("[flows.sim]\nenabled = false\n")
            assert "sim" not in await tools(client)
            assert (await client.call_tool("booley_poll", {"run_id": "missing"})).content

    asyncio.run(exercise())


def test_refresh_preserves_job_and_dispatch_state(project, monkeypatch):
    from booley.mcp.call_context import resolve_call_context

    seen, runs = [], []
    entered = asyncio.Event()
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(project / "logs"))

    async def child(*args, **kwargs):
        kwargs["on_spawn"](os.getpid())
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "_run_subprocess", child)

    async def dispatch(name, arguments, definition, counts, jobs, **kwargs):
        counts[name] += 1
        seen.append((jobs, counts, counts[name]))
        if not runs:
            runs.append(jobs.submit(name, [], 60, context=resolve_call_context({})))
            await entered.wait()
        return [TextContent(type="text", text="accepted")]

    monkeypatch.setattr(server, "_dispatch_booley_mcp_tool", dispatch)
    monkeypatch.setattr(server, "_prepend_changed_health_alert", lambda result: result)
    sdk, _ = server._build_server()

    async def exercise():
        async with live_client(sdk) as client:
            await client.call_tool("sim", {"target": "smoke"})
            write_endpoint(project)
            assert "local_check" in await tools(client), (
                "running catalog must rediscover current custom endpoint schema"
            )
            await client.call_tool("sim", {"target": "smoke"})
            assert seen[0][0] is seen[1][0]
            assert seen[0][1] is seen[1][1]
            assert [item[2] for item in seen] == [1, 2]
            polled = await client.call_tool("booley_poll", {"run_id": runs[0], "wait_seconds": 0})
            assert runs[0] in polled.content[0].text
            cancelled = await client.call_tool("booley_cancel", {"run_id": runs[0]})
            assert "CANCELLED" in cancelled.content[0].text

    asyncio.run(exercise())


def test_initially_disabled_enable_and_helper_bytes_refresh_without_repeat_import(project):
    config = project / "booley.toml"
    config.write_text("[flows.local_flow]\nenabled = false\n")
    path = write_endpoint(project, "local_flow", family="BooleyFlow")
    helper = path.parent / "_schema_helper.py"
    helper.write_text("OPTION = 'first'\n")
    marker = project / "imports.txt"
    source = path.read_text().replace(
        "from booley.flows.base", "from _schema_helper import OPTION\nfrom booley.flows.base"
    )
    source = source.replace("'--first'", "'--' + OPTION")
    source += f"\nfrom pathlib import Path\nwith Path({str(marker)!r}).open('a') as output:\n    output.write('import\\n')\n"
    path.write_text(source)
    sdk, _ = server._build_server()

    async def exercise():
        async with live_client(sdk) as client:
            assert "local_flow" not in await tools(client)
            config.write_text("[flows.local_flow]\nenabled = true\n")
            assert "first" in (await tools(client))["local_flow"].input_schema["properties"]
            assert marker.read_text().splitlines() == ["import"]
            await tools(client)
            assert marker.read_text().splitlines() == ["import"]
            config.write_text("[flows.local_flow]\nenabled = true\n[agent]\nmax_turns = 3\n")
            await tools(client)
            assert marker.read_text().splitlines() == ["import", "import"]
            before = helper.stat()
            helper.write_text("OPTION = 'other'\n")
            os.utime(helper, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert "other" in (await tools(client))["local_flow"].input_schema["properties"]
            assert marker.read_text().splitlines() == ["import", "import", "import"]

    asyncio.run(exercise())


def test_hidden_and_removed_calls_refresh_before_dispatch(project, monkeypatch):
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("hidden endpoint reached dispatch")

    monkeypatch.setattr(server, "_dispatch_booley_mcp_tool", forbidden)
    sdk, _ = server._build_server()

    async def exercise():
        async with live_client(sdk) as client:
            (project / "booley.toml").write_text("[flows.sim]\nenabled = false\n")
            with pytest.raises(MCPError, match="Unknown MCP tool"):
                await client.call_tool("sim", {"target": "smoke"})
            path = write_endpoint(project)
            await tools(client)
            path.unlink()
            with pytest.raises(MCPError, match="Unknown MCP tool"):
                await client.call_tool("local_check", {})
            assert calls == []

    asyncio.run(exercise())


def test_new_custom_call_refreshes_and_dispatches_published_source(project, monkeypatch):
    seen = []

    async def dispatch(name, arguments, source, *args, **kwargs):
        seen.append((name, arguments, source["custom_path"]))
        return [TextContent(type="text", text="new endpoint dispatched")]

    monkeypatch.setattr(server, "_dispatch_booley_mcp_tool", dispatch)
    sdk, _ = server._build_server()

    async def exercise():
        async with live_client(sdk) as client:
            path = write_endpoint(project)
            result = await client.call_tool("local_check", {"first": "hello"})
            assert not result.is_error
            assert "new endpoint dispatched" in result.content[0].text
            assert seen == [("local_check", {"first": "hello"}, str(path))]

    asyncio.run(exercise())


def test_duplicate_refresh_is_atomic_and_corrected_import_recovers(project):
    application, _, _ = server._build_mcp_catalog(server._McpLifetime(None, None))
    catalog = application.live_catalog
    original = application.list_tools()

    async def exercise():
        path = write_endpoint(project, "sim")
        with pytest.raises(ValueError, match="Duplicate"):
            await catalog.refresh()
        assert application.list_tools() == original
        path.unlink()
        path = write_endpoint(project)
        path.write_text(path.read_text() + "\nraise RuntimeError('broken custom import')\n")
        await catalog.refresh()
        assert "local_check" not in {item.name for item in application.list_tools()}
        write_endpoint(project)
        await catalog.refresh()
        assert "local_check" in {item.name for item in application.list_tools()}

    asyncio.run(exercise())


def test_poll_and_cancel_bypass_failed_and_waiting_catalog_refresh(project):
    application, _, _ = server._build_mcp_catalog(server._McpLifetime(None, None))
    catalog = application.live_catalog
    sdk = server._build_sdk_server(application, server._McpLifetime(None, None))

    async def exercise():
        async with live_client(sdk) as client:
            await client.list_tools()
            async with catalog._lock:
                result = await asyncio.wait_for(
                    client.call_tool("booley_poll", {"run_id": "absent"}), 1
                )
                assert result.content
                result = await asyncio.wait_for(
                    client.call_tool("booley_cancel", {"run_id": "absent"}), 1
                )
                assert result.content
            (project / "booley.toml").write_text("[flows")
            with pytest.raises(MCPError):
                await client.list_tools()
            assert (await client.call_tool("booley_poll", {"run_id": "absent"})).content

    asyncio.run(exercise())


def test_observation_generation_is_independent_and_root_selector_does_not_relocate(
    project, monkeypatch
):
    monkeypatch.delenv("BOOLEY_DIR")
    selector = project.parent / "booley.toml"
    selector.write_text('[project]\ndir = ".booley"\n')
    reset_cache()
    application, _, _ = server._build_mcp_catalog(server._McpLifetime(None, None))
    catalog = application.live_catalog

    async def exercise():
        assert not await catalog.observe_change()
        (project.parent / "other").mkdir()
        selector.write_text('[project]\ndir = "other"\n')
        custom = write_endpoint(project)
        custom.write_text(
            custom.read_text()
            + "\nfrom booley.runtime.booley_dir import resolve_booley_dir\n"
            + f"assert str(resolve_booley_dir()) == {str(project)!r}\n"
        )
        await catalog.refresh()
        assert "local_check" in {item.name for item in application.list_tools()}
        assert catalog.directory == project
        assert await catalog.observe_change()
        assert not await catalog.observe_change()
        (project / "booley.toml").write_text("[flows")
        assert await catalog.observe_change()
        assert not await catalog.observe_change()
        (project / "booley.toml").write_text("")
        assert await catalog.observe_change()

    asyncio.run(exercise())


def test_unchanged_catalog_does_not_rescan_installed_sources(project, monkeypatch):
    from booley.mcp import live_catalog

    application, _, _ = server._build_mcp_catalog(server._McpLifetime(None, None))
    discover = live_catalog.discover_mcp_tools
    scans = []

    def observed_discovery(*args, **kwargs):
        scans.append(True)
        return discover(*args, **kwargs)

    monkeypatch.setattr(live_catalog, "discover_mcp_tools", observed_discovery)

    async def exercise():
        await application.live_catalog.refresh()
        await application.live_catalog.refresh()

    asyncio.run(exercise())
    assert not scans, "unchanged catalog must not rescan installed endpoint sources"


def test_unchanged_catalog_retains_project_boundary_validation(project, monkeypatch):
    from booley.mcp import live_catalog

    application, _, _ = server._build_mcp_catalog(server._McpLifetime(None, None))

    def rejected_selection():
        raise RuntimeError("selected Project boundary rejected")

    monkeypatch.setattr(live_catalog, "resolve_booley_dir", rejected_selection)
    with pytest.raises(RuntimeError, match="selected Project boundary rejected"):
        asyncio.run(application.live_catalog.refresh())


def test_worker_timeout_reaps_importer_and_descendants(project, monkeypatch):
    from types import SimpleNamespace

    from booley.mcp import catalog_worker

    path = write_endpoint(project)
    pid_file = project / "worker-pids"
    path.write_text(
        "import os, subprocess, sys, time\nfrom pathlib import Path\n"
        "print('importer timeout diagnostic', flush=True)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()) + ' ' + str(child.pid))\n"
        "time.sleep(120)\n" + path.read_text()
    )
    ready_at = None

    def discovery_time():
        nonlocal ready_at
        now = time.monotonic()
        if pid_file.exists():
            ready_at = ready_at or now
            return now + catalog_worker.DISCOVERY_TIMEOUT_SECONDS
        return now

    # Expire discovery only after the real importer has spawned its child.
    # The production deadline still independently bounds importer startup.
    monkeypatch.setattr(catalog_worker, "time", SimpleNamespace(monotonic=discovery_time))
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out") as failure:
        catalog_worker.run_schema_worker({"local_check": path})
    assert "importer timeout diagnostic" in str(failure.value)
    assert pid_file.exists()
    assert ready_at is not None
    assert ready_at - started < catalog_worker.DISCOVERY_TIMEOUT_SECONDS
    # Two taskkill calls (10s each), 0.2s grace, and a 5s reap are bounded.
    assert time.monotonic() - ready_at < 26
    for pid in pid_file.read_text().split():
        assert_worker_pid_stopped(pid)


# Upper bound for a queued SIGKILL to take effect on a loaded host; it matches
# the worker's own 5 s reap bound and is only spent when the process lingers.
_KILL_LANDS_SECONDS = 5


def assert_worker_pid_stopped(pid):
    import csv
    import subprocess

    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert all(len(row) < 2 or row[1] != pid for row in csv.reader(result.stdout.splitlines()))
    else:
        # killpg() returns once SIGKILL is queued, not once the target has
        # exited: under CPU load a killed grandchild can still be runnable
        # ("R") for a moment before it becomes a zombie or vanishes. The worker
        # cannot reap it (it is not the worker's child), so wait for the kill to
        # land. A process that never stops still fails after the deadline.
        deadline = time.monotonic() + _KILL_LANDS_SECONDS
        while (state := _proc_state(pid)) not in (None, "Z"):
            assert time.monotonic() < deadline, f"pid {pid} still in state {state!r}"
            time.sleep(0.01)


def _proc_state(pid):
    """Return the /proc state letter of *pid*, or None once it has vanished."""
    # A stopped process may vanish while we look at it: before open() gives
    # FileNotFoundError, between open() and read() gives ProcessLookupError.
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    # The state field follows the (possibly paren-laden) comm; "Z" is a zombie.
    return stat_text.rsplit(")", 1)[1].split()[0]


@pytest.mark.skipif(os.name == "nt", reason="reads /proc")
@pytest.mark.parametrize("vanished", [FileNotFoundError, ProcessLookupError])
def test_pid_stopped_check_accepts_process_vanishing_mid_read(monkeypatch, vanished):
    def read_text(*_args, **_kwargs):
        raise vanished

    monkeypatch.setattr(Path, "read_text", read_text)
    # A live pid makes /proc/<pid>/stat exist, so only the read can fail.
    assert_worker_pid_stopped(str(os.getpid()))


def test_nested_exposure_is_preserved_across_refresh(project, monkeypatch):
    monkeypatch.setenv("BOOLEY_NESTED_AGENT", "1")
    monkeypatch.setenv("BOOLEY_NESTED_MCP_TOOLS", "sim")
    sdk, _ = server._build_server()

    async def exercise():
        async with live_client(sdk) as client:
            initial = set(await tools(client))
            write_endpoint(project, "local_specialist", family="Specialist")
            (project / "booley.toml").write_text("[flows.sim]\nenabled = false\n")
            assert set(await tools(client)) == initial - {"sim"}
            assert "booley_poll" in initial
            assert "goal_enter" not in initial

    asyncio.run(exercise())


def test_retired_layout_marker_invalidates_and_recovers(project):
    application, _, _ = server._build_mcp_catalog(server._McpLifetime(None, None))
    catalog = application.live_catalog

    async def exercise():
        marker = project.parent / ".booley_project"
        marker.mkdir()
        assert await catalog.observe_change()
        with pytest.raises(RuntimeError, match="both Booley Directories"):
            await catalog.refresh()
        marker.rmdir()
        assert await catalog.observe_change()
        await catalog.refresh()

    asyncio.run(exercise())


@pytest.mark.timeout(120)  # Windows CI: 34.4 s; 3x rounded up to 30 s.
def test_config_only_refresh_reuses_unfiltered_discovery(project, monkeypatch):
    from booley.mcp import registry

    scans = []
    original = registry.extract_mcp_tool_info

    def counted(*args, **kwargs):
        scans.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "extract_mcp_tool_info", counted)
    write_endpoint(project, "local_flow", family="BooleyFlow")
    write_endpoint(project, "local_specialist", family="Specialist")
    sdk, _ = server._build_server()
    endpoints = registry.discover_mcp_tools(project_mcp_tools_dir=project / "mcp_tools")
    startup_scans = len(scans)

    async def exercise():
        async with live_client(sdk) as client:
            initial = await tools(client)
            config = project / "booley.toml"
            for item in endpoints:
                if item.kind == "mcp_tool" or item.name not in initial:
                    continue
                table = "flows" if item.kind == "flow" else "specialists"
                config.write_text(f"[{table}.{item.name}]\nenabled = false\n")
                assert item.name not in await tools(client)
                config.write_text(f"[{table}.{item.name}]\nenabled = true\n")
                assert (await tools(client))[item.name] == initial[item.name]
            config.write_text('[unrelated]\nvalue = "new"\n')
            assert await tools(client) == initial
            assert len(scans) == startup_scans, (
                "config-only refresh must reuse unfiltered discovery"
            )

    asyncio.run(exercise())


def test_config_cache_preserves_unknown_specialist_rejection(project, monkeypatch):
    from booley.mcp import registry

    scans = []
    original = registry.extract_mcp_tool_info

    def counted(*args, **kwargs):
        scans.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "extract_mcp_tool_info", counted)
    write_endpoint(project, "local_specialist", family="Specialist")
    sdk, _ = server._build_server()
    startup_scans = len(scans)

    async def exercise():
        async with live_client(sdk) as client:
            initial = await tools(client)
            config = project / "booley.toml"
            config.write_text("[specialists.local_specialist]\nenabled = false\n")
            assert "local_specialist" not in await tools(client)
            config.write_text("[specialists.unknown_specialist]\nenabled = false\n")
            with pytest.raises(MCPError):
                await client.list_tools()
            config.write_text("[specialists.local_specialist]\nenabled = true\n")
            assert await tools(client) == initial
            assert len(scans) == startup_scans, (
                "config validation must reuse complete Specialist inventory"
            )

    asyncio.run(exercise())


def test_config_generation_reexecutes_custom_schema(project):
    path = write_endpoint(project)
    path.write_text(
        path.read_text().replace(
            "parser.add_argument('--first', help='Current option')",
            "import os; from pathlib import Path; import tomllib\n"
            "        config = tomllib.loads((Path(os.environ['BOOLEY_DIR']) / 'booley.toml').read_text())\n"
            "        parser.add_argument('--' + config['display']['option'])",
        )
    )
    config = project / "booley.toml"
    config.write_text('[display]\noption = "first"\n')
    sdk, _ = server._build_server()

    async def exercise():
        async with live_client(sdk) as client:
            assert "first" in (await tools(client))["local_check"].input_schema["properties"]
            config.write_text('[display]\noption = "other"\n')
            current = (await tools(client))["local_check"].input_schema["properties"]
            assert "other" in current
            assert "first" not in current

    asyncio.run(exercise())
