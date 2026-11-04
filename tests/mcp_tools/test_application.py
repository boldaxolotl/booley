"""Tests for the Booley-owned MCP application interface."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from jsonschema.exceptions import SchemaError

from booley.mcp.application import (
    McpApplication,
    McpDispatchResult,
    UnknownMcpToolError,
    _normalize_payload,
)


def _definition(name: str = "echo", **extra):
    return {
        "name": name,
        "description": "Echo text",
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        **extra,
    }


def _application(definitions, calls=None, errors=None):
    calls = calls if calls is not None else []
    errors = errors if errors is not None else []

    async def dispatch(name, arguments, source):
        calls.append((name, arguments, source))
        return [SimpleNamespace(text=arguments["text"])]

    return McpApplication(
        definitions,
        dispatch=dispatch,
        canonicalize=lambda name: {"run_echo": "echo"}.get(name, name),
        on_discovery_error=errors.append,
    )


def test_catalog_is_deterministic_and_dispatch_accepts_alias() -> None:
    calls: list[tuple] = []
    application = _application([_definition("zeta"), _definition()], calls=calls)

    assert [tool.name for tool in application.list_tools()] == ["echo", "zeta"]
    payload = asyncio.run(application.call_tool("run_echo", {"text": "hello"}))

    assert payload.content[0].text == "hello"
    assert calls[0][0:2] == ("echo", {"text": "hello"})


def test_invalid_arguments_return_tool_error_with_json_path() -> None:
    application = _application([_definition()])

    payload = asyncio.run(application.call_tool("echo", {"text": 7}))

    assert payload.is_error is True
    assert payload.content[0].text.startswith("Invalid tool arguments at $.text:")


def test_unknown_tool_is_a_distinct_protocol_error() -> None:
    application = _application([_definition()])

    with pytest.raises(UnknownMcpToolError, match="missing"):
        asyncio.run(application.call_tool("missing", {}))


def test_duplicate_canonical_alias_fails_catalog_construction() -> None:
    with pytest.raises(ValueError, match="alias"):
        _application([_definition("echo"), _definition("run_echo")])


def test_invalid_builtin_schema_fails_startup() -> None:
    broken = _definition()
    broken["schema"] = {"type": "definitely-not-a-json-schema-type"}

    with pytest.raises(SchemaError):
        _application([broken])


def test_invalid_custom_schema_is_excluded_with_actionable_error() -> None:
    errors: list[str] = []
    broken = _definition(is_custom=True)
    broken["schema"] = {"type": "definitely-not-a-json-schema-type"}

    application = _application([broken, _definition("valid")], errors=errors)

    assert [tool.name for tool in application.list_tools()] == ["valid"]
    assert errors and errors[0].startswith("INVALID CUSTOM MCP SCHEMA: echo:")


@pytest.mark.parametrize("schema", [None, "not-a-schema", 7])
def test_non_mapping_custom_input_schema_is_excluded(schema: object) -> None:
    errors: list[str] = []
    broken = _definition(is_custom=True)
    broken["schema"] = schema

    application = _application([broken, _definition("valid")], errors=errors)

    assert [tool.name for tool in application.list_tools()] == ["valid"]
    assert errors == [
        f"INVALID CUSTOM MCP SCHEMA: echo: MCP tool 'echo' input schema "
        f"must be a mapping, got {type(schema).__name__}"
    ]


def test_non_mapping_custom_output_schema_is_excluded() -> None:
    errors: list[str] = []
    broken = _definition(is_custom=True, output_schema="not-a-schema")

    application = _application([broken, _definition("valid")], errors=errors)

    assert [tool.name for tool in application.list_tools()] == ["valid"]
    assert errors == [
        "INVALID CUSTOM MCP SCHEMA: echo: MCP tool 'echo' output schema must be a mapping, got str"
    ]


def test_non_mapping_builtin_schema_fails_startup() -> None:
    broken = _definition()
    broken["schema"] = None

    with pytest.raises(ValueError, match="input schema must be a mapping"):
        _application([broken])


def test_valid_output_schema_is_advertised() -> None:
    output_schema = {"type": "object", "properties": {"answer": {"type": "string"}}}

    application = _application([_definition(output_schema=output_schema)])

    assert application.list_tools()[0].output_schema == output_schema


def test_structured_result_normalization_validates_tuple_shape() -> None:
    with pytest.raises(TypeError, match="invalid structured result"):
        _normalize_payload(([], None))

    payload = _normalize_payload(([], {"answer": "ok"}))

    assert payload.structured_content == {"answer": "ok"}


def test_dispatch_error_preserves_text_and_structured_content() -> None:
    result = McpDispatchResult(
        value=([SimpleNamespace(text="EXIT_CODE: 2")], {"exit_code": 2}),
        is_error=True,
    )

    payload = _normalize_payload(result)

    assert payload.content[0].text == "EXIT_CODE: 2"
    assert payload.structured_content == {"exit_code": 2}
    assert payload.is_error is True


def test_explicit_older_schema_dialect_is_respected() -> None:
    definition = _definition()
    definition["schema"]["$schema"] = "http://json-schema.org/draft-07/schema#"
    application = _application([definition])

    payload = asyncio.run(application.call_tool("echo", {"text": "valid"}))

    assert payload.is_error is False


@pytest.mark.parametrize(
    "alternatives", [[False, False], [{"properties": {"family": False}}, False]]
)
def test_boolean_schema_alternatives_return_argument_errors(alternatives):
    application = _application([_definition(schema={"oneOf": alternatives}, is_custom=True)])
    payload = asyncio.run(application.call_tool("echo", {"family": "invalid"}))
    assert payload.is_error
    assert payload.content[0].text.startswith("Invalid tool arguments at $:")


def test_replacement_validates_before_publishing_and_preserves_dispatch():
    calls = []
    application = _application([_definition()], calls=calls)
    original = application.list_tools()
    with pytest.raises(ValueError, match="Duplicate"):
        application.replace_tools([_definition("second"), _definition("second")])
    assert application.list_tools() == original
    application.replace_tools([_definition("second")])
    asyncio.run(application.call_tool("second", {"text": "new"}))
    assert calls[0][0] == "second"


def test_inflight_dispatch_keeps_one_generation_metadata():
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def dispatch(name, arguments, source):
        entered.set()
        await release.wait()
        seen.append(source["description"])
        return [SimpleNamespace(text="done")]

    application = McpApplication(
        [_definition()],
        dispatch=dispatch,
        canonicalize=lambda name: name,
        on_discovery_error=lambda message: None,
    )

    async def exercise():
        task = asyncio.create_task(application.call_tool("echo", {"text": "old"}))
        await entered.wait()
        application.replace_tools([_definition(description="New description")])
        release.set()
        await task
        assert seen == ["Echo text"]
        assert application.list_tools()[0].description == "New description"

    asyncio.run(exercise())


def test_live_status_preserves_independent_names_and_observation_precedes_import(
    tmp_path, monkeypatch
):
    from booley.mcp import live_catalog, server
    from booley.runtime.booley_dir import reset_cache
    from tests.mcp_tools.test_live_catalog import write_endpoint

    directory = tmp_path / ".booley"
    directory.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_DIR", str(directory))
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    for key in ("BOOLEY_NESTED_AGENT", "BOOLEY_MCP_TOOLS", "BOOLEY_COVERAGE_CAMPAIGN"):
        monkeypatch.delenv(key, raising=False)
    reset_cache()
    seen, imports = [], []

    async def status(name, arguments, jobs, names, *, caller=None):
        seen.append(set(names))
        return [SimpleNamespace(text="ready")]

    def worker(paths, cancel):
        imports.append(paths)
        return [], []

    monkeypatch.setattr(server, "_dispatch_special_mcp_tool", status)
    monkeypatch.setattr(live_catalog, "run_schema_worker", worker)
    application, _, _ = server._build_mcp_catalog(server._McpLifetime(None, None))

    async def exercise():
        await application.call_tool("booley_status", {})
        write_endpoint(directory)
        (directory / "booley.toml").write_text("[flows.sim]\nenabled = false\n")
        assert await application.live_catalog.observe_change()
        assert imports == []
        await application.live_catalog.refresh()
        assert len(imports) == 1
        await application.call_tool("booley_status", {})
        assert "bwave" in seen[0]
        assert seen[1] == seen[0] - {"sim"}

    try:
        asyncio.run(exercise())
    finally:
        reset_cache()


def test_duplicate_custom_names_reject_complete_live_and_startup_candidates(tmp_path, monkeypatch):
    from booley.mcp import server
    from booley.runtime.booley_dir import reset_cache
    from tests.mcp_tools.test_live_catalog import write_endpoint

    directory = tmp_path / ".booley"
    directory.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_DIR", str(directory))
    reset_cache()
    application, _, _ = server._build_mcp_catalog(server._McpLifetime(None, None))
    original = application.list_tools()
    write_endpoint(directory, "first")
    second = write_endpoint(directory, "second")
    second.write_text(second.read_text().replace("name = 'second'", "name = 'first'"))
    try:
        rejected = False
        try:
            asyncio.run(application.live_catalog.refresh())
        except ValueError:
            rejected = True
        assert rejected, "duplicate custom names must reject the complete candidate"
        assert application.list_tools() == original
        with pytest.raises(ValueError, match="Duplicate"):
            server._build_mcp_catalog(server._McpLifetime(None, None))
    finally:
        reset_cache()


@pytest.mark.parametrize("default", ["Path('build')", "object()"])
def test_schema_worker_preserves_valid_siblings_and_wire_defaults(tmp_path, default):
    from booley.mcp.catalog_worker import run_schema_worker
    from tests.mcp_tools.test_live_catalog import write_endpoint

    path = write_endpoint(tmp_path, option="artifact")
    path.write_text(
        "from pathlib import Path\n"
        + path.read_text().replace(
            "parser.add_argument('--artifact', help='Current option')",
            f"parser.add_argument('--artifact', type=Path, default={default})",
        )
    )
    sibling = write_endpoint(tmp_path, name="valid_sibling")
    try:
        definitions, errors = run_schema_worker({"local_check": path, "valid_sibling": sibling})
    except RuntimeError:
        definitions, errors = [], []
    by_name = {item["name"]: item for item in definitions}
    assert "valid_sibling" in by_name, "schema worker must preserve valid endpoint definitions"
    if default.startswith("Path"):
        assert by_name["local_check"]["schema"]["properties"]["artifact"]["default"] == "build"
        assert not errors
    else:
        assert "local_check" not in by_name
        assert errors and "local_check" in errors[0]


def test_replacement_logs_custom_schema_errors_without_retaining_startup_errors(caplog):
    startup_errors = []
    application = _application([_definition()], errors=startup_errors)
    broken = _definition("broken", is_custom=True, schema={"type": "invalid"})
    with caplog.at_level("WARNING"):
        application.replace_tools([broken, _definition("valid")])
    assert "INVALID CUSTOM MCP SCHEMA: broken" in caplog.text, (
        "live catalog must log rejected custom schemas"
    )
    assert not startup_errors
    assert [item.name for item in application.list_tools()] == ["valid"]


def test_worker_failure_retains_bounded_importer_diagnostic(tmp_path):
    from booley.mcp.catalog_worker import run_schema_worker
    from tests.mcp_tools.test_live_catalog import write_endpoint

    path = write_endpoint(tmp_path)
    path.write_text("print('schema importer diagnostic', flush=True)\nraise SystemExit(7)\n")
    with pytest.raises(RuntimeError) as raised:
        run_schema_worker({"local_check": path})
    assert "schema importer diagnostic" in str(raised.value), (
        "worker failure must retain importer diagnostic"
    )
    assert "exited 7" in str(raised.value)


@pytest.mark.parametrize("delay, error", [(8.25, RuntimeError), (0, ValueError)])
def test_cancelled_schema_discovery_owns_worker_until_cleanup(monkeypatch, delay, error):
    import threading

    from booley.mcp import live_catalog

    entered, release, reaped = (threading.Event() for _ in range(3))

    def worker(paths, cancel):
        entered.set()
        assert cancel.wait(3)
        assert release.wait(12)
        reaped.set()
        raise error("worker cleanup completed")

    monkeypatch.setattr(live_catalog, "run_schema_worker", worker)

    async def exercise():
        task = asyncio.create_task(live_catalog._async_schemas({}))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        try:
            await asyncio.sleep(delay)
            premature = delay > 0 and task.done()
        finally:
            release.set()
        outcome = None
        try:
            await task
        except (asyncio.CancelledError, RuntimeError, ValueError) as exc:
            outcome = exc
        assert not premature and isinstance(outcome, asyncio.CancelledError) and reaped.is_set(), (
            "cancelled schema discovery must retain ownership until worker cleanup finishes"
        )

    asyncio.run(exercise())
