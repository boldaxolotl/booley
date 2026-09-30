"""Compiler-cache contracts at policy and actual compiler-shell boundaries."""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from booley.flows.sim.build import PreparedSimulationBuild, simulation_build_script
from booley.flows.sim.compiler_cache import (
    CompilerCacheConfigurationError,
    CompilerCachePolicy,
    compose_environment,
    execution_environment,
    resolve_policy,
    validate_make_assignments,
)
from booley.runtime.compiler_cache import (
    COMPILER_CACHE_RELATIVE,
    COMPILER_CACHE_ROOT_ENV,
    ISSUED_COMPILER_CACHE_ROOT,
    IssuedCacheIdentity,
)
from booley.runtime.filesystem_utils import copy_booley_tree
from booley.targets.domain import TargetInspection

# A host process: nothing issued, not inside a Sandbox.
HOST = IssuedCacheIdentity(root=None, in_sandbox=False)
# A Sandbox issued before the shared compiler cache existed.
UNREFRESHED_SANDBOX = IssuedCacheIdentity(root=None, in_sandbox=True)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    data = root / ".booley_project"
    data.mkdir()
    monkeypatch.delenv(COMPILER_CACHE_ROOT_ENV, raising=False)
    return root, data


def test_defaults_are_pure_and_owner_is_explicit(project, tmp_path):
    root, data = project
    owner = tmp_path / "shared"
    owner.mkdir()
    policy = resolve_policy(root, issued=HOST, owner=owner)
    assert policy == CompilerCachePolicy(True, owner / COMPILER_CACHE_RELATIVE, "5G")
    assert not (owner / ".runtime").exists()
    (data / "pipeline.toml").write_text(
        '[flows.sim.compiler_cache]\nenabled=false\nmax_size="1Mi"\n'
    )
    policy = resolve_policy(root, issued=HOST, owner=owner)
    assert not policy.enabled
    assert policy.environment()["OBJCACHE"] == ""
    assert not policy.root.exists()


@pytest.mark.parametrize(
    "setting",
    [
        'enabled="yes"',
        "enabled=1",
        "max_size=0",
        'max_size="0G"',
        'max_size="1.5G"',
        'max_size="-1G"',
        'max_size="1T"',
        "max_size=[]",
        'max_size="5"',
        'max_size="1GB"',
    ],
)
def test_invalid_explicit_settings_fail(project, setting):
    root, data = project
    (data / "booley.toml").write_text("[flows.sim.compiler_cache]\n" + setting)
    with pytest.raises(CompilerCacheConfigurationError):
        resolve_policy(root, issued=HOST, owner=data)


@pytest.mark.parametrize(
    "content", ["flows=1", "[flows]\nsim=[]", "[flows.sim]\ncompiler_cache=false", "[broken"]
)
def test_selected_checkout_errors_do_not_fall_back(project, tmp_path, monkeypatch, content):
    root, data = project
    main = tmp_path / "main-data"
    main.mkdir()
    (main / "booley.toml").write_text('[flows.sim.compiler_cache]\nmax_size="2G"')
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(main))
    (data / "booley.toml").write_text(content)
    with pytest.raises(CompilerCacheConfigurationError):
        resolve_policy(root, issued=HOST, owner=main)


@pytest.mark.parametrize(
    "name",
    [
        "OBJCACHE",
        "CCACHE_DIR",
        "CCACHE_MAXSIZE",
        "CCACHE_COMPILERCHECK",
        "CCACHE_BASEDIR",
        "USER_CPPFLAGS",
        COMPILER_CACHE_ROOT_ENV,
    ],
)
@pytest.mark.parametrize("operator", ["=", ":=", "::=", ":::=", "?=", "+=", "!="])
@pytest.mark.parametrize(
    "source", ["modern", "legacy", "MAKEFLAGS", "GNUMAKEFLAGS", "MAKEOVERRIDES", "MFLAGS"]
)
def test_reserved_make_assignments_rejected(name, operator, source):
    assignment = f"-j2 {name}{operator}anything"
    inspection = cast(
        TargetInspection,
        SimpleNamespace(
            flow_options={"make_options": [assignment]} if source == "modern" else {},
            tool_options={"make_options": [assignment]} if source == "legacy" else {},
        ),
    )
    with pytest.raises(CompilerCacheConfigurationError, match="managed by Booley"):
        validate_make_assignments(inspection, {source: assignment})


def test_unrelated_make_options_preserved():
    inspection = cast(
        TargetInspection,
        SimpleNamespace(
            flow_options={
                "make_options": [
                    "-j2",
                    "VM_PARALLEL_BUILDS=1",
                    "USER_LDFLAGS=-Wl,-defsym,OBJCACHE=1",
                    "OTHER=CCACHE_DIR=/tmp",
                    "'OTHER=label OBJCACHE=example'",
                ]
            },
            tool_options={},
        ),
    )
    validate_make_assignments(inspection, {"MAKEFLAGS": "-j4 --jobserver-auth=3,4 OTHER=yes"})


@pytest.mark.parametrize(
    "option",
    [
        "CCACHE_DIR := /tmp",
        "--eval='override CCACHE_MAXSIZE=0'",
        "--eval=' override OBJCACHE=escape'",
        "--eval 'export OBJCACHE=escape'",
        "-E 'private CCACHE_DIR=/tmp'",
        "-EOBJCACHE=escape",
        "--eval='$(eval OBJCACHE=escape)'",
        "--eval='define OBJCACHE\nescape\nendef'",
        "--eval='variable=OBJCACHE\n$(eval $(variable)=escape)'",
        "--ev=OBJCACHE=escape",
        "--eva 'CCACHE_DIR=/tmp'",
        "-sEOBJCACHE=escape",
        "-rE 'CCACHE_MAXSIZE=0'",
    ],
)
def test_reserved_eval_and_spaced_assignments_rejected(option):
    inspection = cast(
        TargetInspection, SimpleNamespace(flow_options={"make_options": [option]}, tool_options={})
    )
    with pytest.raises(CompilerCacheConfigurationError, match="managed by Booley"):
        validate_make_assignments(inspection, {})


@pytest.mark.parametrize("tool_directory", ["caller", "compiler"])
def test_relative_path_availability_uses_compiler_directory(tmp_path, monkeypatch, tool_directory):
    caller = tmp_path / "caller"
    compiler = tmp_path / "compiler"
    caller.mkdir()
    compiler.mkdir()
    tools = (caller if tool_directory == "caller" else compiler) / "tools"
    tools.mkdir()
    executable = tools / ("ccache.exe" if os.name == "nt" else "ccache")
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.chdir(caller)
    policy = CompilerCachePolicy(True, tmp_path / COMPILER_CACHE_RELATIVE, "5G")
    environment = execution_environment(
        policy, {**policy.environment(), "PATH": "tools"}, ambient={}, build_root=compiler
    )
    assert environment["OBJCACHE"] == ("ccache" if tool_directory == "compiler" else "")


def test_effective_path_and_precedence(project, tmp_path, caplog):
    root, owner = project
    policy = resolve_policy(root, issued=HOST, owner=owner)
    tools = tmp_path / "tools"
    tools.mkdir()
    ccache = tools / ("ccache.exe" if os.name == "nt" else "ccache")
    ccache.write_text("#!/bin/sh\nexit 0\n")
    ccache.chmod(0o755)
    target = {"PATH": str(tools), "CCACHE_DIR": "unrelated", "CCACHE_MAXSIZE": "0", "OTHER": "yes"}
    environment = compose_environment(policy, target, ambient={})
    assert environment["OTHER"] == "yes"
    assert environment["CCACHE_DIR"] == str(policy.root)
    assert "conflicts" in caplog.text
    assert execution_environment(policy, environment, ambient={})["OBJCACHE"] == "ccache"
    assert policy.root.is_dir()
    environment["PATH"] = str(tmp_path / "missing")
    assert execution_environment(policy, environment, ambient={})["OBJCACHE"] == ""
    assert "unavailable" in caplog.text


@pytest.mark.parametrize(
    "relative", [".runtime", ".runtime/compiler-cache", COMPILER_CACHE_RELATIVE]
)
def test_symlink_ancestors_fall_back_without_writing(project, tmp_path, monkeypatch, relative):
    root, owner = project
    destination = tmp_path / "outside"
    destination.mkdir()
    link = owner / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    from tests.conftest import symlink_or_skip

    symlink_or_skip(link, destination, target_is_directory=True)
    monkeypatch.setattr("booley.flows.sim.compiler_cache.shutil.which", lambda *a, **kw: "/ccache")
    policy = resolve_policy(root, issued=HOST, owner=owner)
    assert execution_environment(policy, policy.environment(), ambient={})["OBJCACHE"] == ""
    assert list(destination.iterdir()) == []


def test_build_exports_do_not_leak_to_run(project, tmp_path, monkeypatch):
    root, owner = project
    policy = resolve_policy(root, issued=HOST, owner=owner)
    assert not policy.root.exists(), "resolution alone must not create storage"
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "ccache").write_text("#!/bin/sh\nexit 0\n")
    (tools / "ccache").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("CCACHE_MAXSIZE", raising=False)
    prepared = PreparedSimulationBuild(
        "sim",
        "id",
        cast(object, None),
        root,
        root,
        "verilator",
        "top",
        ("sh", "-c", 'test "$OBJCACHE" = ccache && test "$CCACHE_MAXSIZE" = 5G'),
        policy.environment(),
        compiler_cache=policy,
    )
    script = simulation_build_script(
        prepared,
        "a" * 32,
        run_line='test "$OBJCACHE" = run-value && test -z "$CCACHE_MAXSIZE"',
        run_environment={"OBJCACHE": "run-value"},
    )
    result = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "rc=0" in result.stdout
    assert policy.root.is_dir()


def test_missing_ccache_builds_uncached_without_storage(project, monkeypatch):
    root, owner = project
    policy = resolve_policy(root, issued=HOST, owner=owner)
    monkeypatch.setattr("booley.flows.sim.compiler_cache.shutil.which", lambda *a, **kw: None)
    prepared = PreparedSimulationBuild(
        "sim",
        "id",
        cast(object, None),
        root,
        root,
        "verilator",
        "top",
        ("sh", "-c", 'test -z "$OBJCACHE"'),
        policy.environment(),
        compiler_cache=policy,
    )
    result = subprocess.run(
        ["sh", "-c", simulation_build_script(prepared, "b" * 32)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert not policy.root.exists()


def test_legacy_snapshot_excludes_only_cache_subtree(tmp_path):
    src = tmp_path / ".booley"
    cache = src / "project" / COMPILER_CACHE_RELATIVE
    cache.mkdir(parents=True)
    (cache / "entry").write_text("cache")
    (cache.parent.parent / "other").write_text("operational neighbor")
    (src / "compiler-cache").mkdir()
    (src / "compiler-cache" / "authored").write_text("keep")
    dst = tmp_path / "copy"
    copy_booley_tree(src, dst)
    assert not (dst / "project" / ".runtime" / "compiler-cache").exists()
    assert (dst / "project" / ".runtime" / "other").read_text() == "operational neighbor"
    assert (dst / "compiler-cache" / "authored").read_text() == "keep"


def _prepare_real_verilator_target(root: Path, options: dict, *, legacy: bool = False):
    """Prepare a one-file Verilator Target through real FuseSoC/Edalize resolution."""
    import sys
    from unittest.mock import patch

    import yaml

    from booley.flows.sim.build import prepare_simulation_build
    from booley.fusesoc import fusesoc_registry
    from booley.targets.catalog import TargetCatalog

    (root / "top.sv").write_text("module top; endmodule\n")
    target = {"filesets": ["rtl"], "toplevel": "top"}
    if legacy:
        target.update(default_tool="verilator", tools={"verilator": options})
    else:
        target.update(flow="sim", flow_options={"tool": "verilator", **options})
    core = {
        "name": "booley:test:cache:1",
        "filesets": {"rtl": {"files": [{"top.sv": {"file_type": "systemVerilogSource"}}]}},
        "targets": {"sim": target},
    }
    (root / "test.core").write_text("CAPI=2:\n" + yaml.safe_dump(core))
    handle = TargetCatalog.build(root).select("sim", for_flow="sim")
    real_resolve = fusesoc_registry.resolve_target_handle
    with patch.object(
        fusesoc_registry,
        "resolve_target_handle",
        side_effect=lambda *args, **kwargs: real_resolve(
            *args,
            **kwargs,
            fusesoc_cmd=[sys.executable, "-c", "from fusesoc.main import main; main()"],
        ),
    ):
        return prepare_simulation_build(handle)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("source", ["authored", "GNUMAKEFLAGS"])
@pytest.mark.parametrize(
    "assignment", ["OBJCACHE=ccache", "CCACHE_MAXSIZE=0", "CCACHE_DIR=/elsewhere"]
)
def test_real_edalize_preparation_rejects_target_make_overrides(
    project, monkeypatch, legacy, assignment, source
):
    from booley.flows.sim.build import SimulationBuildPreparationError
    from booley.runtime.project_dir import reset_cache

    root, data = project
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    reset_cache()
    options = {"make_options": ["-j2", assignment] if source == "authored" else ["-j2"]}
    if source == "GNUMAKEFLAGS":
        monkeypatch.setenv(source, assignment)
    with pytest.raises(SimulationBuildPreparationError, match="managed by Booley"):
        _prepare_real_verilator_target(root, options, legacy=legacy)
    assert not (data / ".runtime" / "compiler-cache").exists()


def test_real_preparation_in_unrefreshed_sandbox_builds_uncached(project, monkeypatch):
    from booley.runtime.project_dir import reset_cache

    root, data = project
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    reset_cache()
    monkeypatch.setattr(
        "booley.flows.sim.build.read_issued_identity", lambda _environ: UNREFRESHED_SANDBOX
    )
    prepared = _prepare_real_verilator_target(root, {"make_options": ["-j2"]})
    assert prepared.compiler_cache is not None
    assert prepared.compiler_cache.root is None
    assert prepared.environment["OBJCACHE"] == ""
    assert "CCACHE_DIR" not in prepared.environment


@pytest.mark.parametrize("issued", ["relative/cache", "/other/.runtime/compiler-cache/ccache"])
def test_issued_root_cannot_widen_authority(project, caplog, issued):
    root, owner = project
    policy = resolve_policy(
        root, issued=IssuedCacheIdentity(root=issued, in_sandbox=True), owner=owner
    )
    assert policy.root is None
    assert "authorized Project mount" in policy.unavailable_reason
    environment = execution_environment(policy, policy.environment(), ambient={})
    assert environment["OBJCACHE"] == ""
    assert "CCACHE_DIR" not in environment
    assert "compiling uncached" in caplog.text


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_unrefreshed_sandbox_compiles_uncached(project, caplog, enabled):
    root, data = project
    (data / "booley.toml").write_text(f"[flows.sim.compiler_cache]\nenabled={enabled}\n")
    policy = resolve_policy(root, issued=UNREFRESHED_SANDBOX, owner=data)
    assert policy.root is None
    assert not policy.active
    environment = execution_environment(
        policy, compose_environment(policy, {}, ambient={}), ambient={}
    )
    assert environment["OBJCACHE"] == ""
    assert "CCACHE_DIR" not in environment
    assert ("booley session refresh" in caplog.text) is (enabled == "true")
    assert not (data / ".runtime").exists()


def test_disabled_policy_exports_no_cache_location(project):
    root, data = project
    (data / "booley.toml").write_text("[flows.sim.compiler_cache]\nenabled=false\n")
    environment = resolve_policy(root, issued=HOST, owner=data).environment()
    assert environment["OBJCACHE"] == ""
    assert not {"CCACHE_DIR", "CCACHE_MAXSIZE", "CCACHE_BASEDIR"} & environment.keys()


def test_issued_owner_survives_ticket_project_dir(project, tmp_path, monkeypatch):
    root, data = project
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(tmp_path / "ticket-data"))
    (data / "booley.toml").write_text('[flows.sim.compiler_cache]\nmax_size="1Gi"')
    issued = IssuedCacheIdentity(root=ISSUED_COMPILER_CACHE_ROOT, in_sandbox=True)
    policy = resolve_policy(root, issued=issued)
    assert policy.root == Path("/booley-project/.runtime/compiler-cache/ccache")
    assert policy.environment()[COMPILER_CACHE_ROOT_ENV] == ISSUED_COMPILER_CACHE_ROOT
    assert policy.max_size == "1Gi"


def test_storage_failure_uses_uncached_build(project, monkeypatch, caplog):
    root, owner = project
    policy = resolve_policy(root, issued=HOST, owner=owner)
    monkeypatch.setattr("booley.flows.sim.compiler_cache.shutil.which", lambda *a, **kw: "/ccache")

    def denied(_root):
        raise PermissionError("denied")

    monkeypatch.setattr("booley.flows.sim.compiler_cache._prepare_directory", denied)
    assert execution_environment(policy, policy.environment(), ambient={})["OBJCACHE"] == ""
    assert "unavailable" in caplog.text
    assert not policy.root.exists()


def test_disabled_cache_preserves_existing_entries(project):
    _root, data = project
    policy = CompilerCachePolicy(False, data / COMPILER_CACHE_RELATIVE, "5G")
    policy.root.mkdir(parents=True)
    entry = policy.root / "entry"
    entry.write_text("keep")
    assert execution_environment(policy, policy.environment(), ambient={})["OBJCACHE"] == ""
    assert entry.read_text() == "keep"


def test_debug_mapping_is_limited_to_generated_build_and_preserves_flags(project):
    root, owner = project
    policy = resolve_policy(root, issued=HOST, owner=owner)
    generated = root / "build directory$literal"
    environment = compose_environment(
        policy, {"USER_CPPFLAGS": "-DUSER=1"}, ambient={}, build_root=generated
    )
    assert environment["CCACHE_BASEDIR"] == str(generated)
    assert environment["USER_CPPFLAGS"].startswith("-DUSER=1 ")
    assert "-fdebug-prefix-map=" in environment["USER_CPPFLAGS"]
    assert "-ffile-prefix-map=" not in environment["USER_CPPFLAGS"]
    assert "$$literal" in environment["USER_CPPFLAGS"]
    disabled = replace(policy, enabled=False)
    assert (
        compose_environment(
            disabled, {"USER_CPPFLAGS": "-DUSER=1"}, ambient={}, build_root=generated
        )["USER_CPPFLAGS"]
        == "-DUSER=1"
    )


@pytest.mark.parametrize("name", ["USER_CPPFLAGS", "CCACHE_BASEDIR"])
def test_make_cannot_override_debug_normalization(name):
    inspection = cast(
        TargetInspection,
        SimpleNamespace(flow_options={"make_options": [name + "=override"]}, tool_options={}),
    )
    with pytest.raises(CompilerCacheConfigurationError, match="managed by Booley"):
        validate_make_assignments(inspection, {})


@pytest.mark.parametrize("filename", ["booley.toml", "pipeline.toml"])
def test_broken_selected_config_is_not_absent(project, filename):
    from tests.conftest import symlink_or_skip

    root, data = project
    symlink_or_skip(data / filename, data / "missing.toml")
    with pytest.raises(CompilerCacheConfigurationError, match="broken link"):
        resolve_policy(root, issued=HOST, owner=data)
