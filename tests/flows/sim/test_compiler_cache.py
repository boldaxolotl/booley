"""Compiler-cache contracts at policy and actual compiler-shell boundaries."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import cast

import pytest

from booley.flows.sim.build import PreparedSimulationBuild, simulation_build_script
from booley.flows.sim.compiler_cache import (
    CompilerCacheConfigurationError,
    CompilerCachePolicy,
    compose_environment,
    execution_environment,
    resolve_policy,
)
from booley.runtime.compiler_cache import COMPILER_CACHE_ROOT_ENV

MANAGED = {"OBJCACHE", "CCACHE_DIR", "CCACHE_MAXSIZE", "CCACHE_COMPILERCHECK"}


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    data = root / ".booley"
    data.mkdir()
    monkeypatch.delenv(COMPILER_CACHE_ROOT_ENV, raising=False)
    return root, data


def fake_ccache(directory: Path) -> Path:
    """Put an executable ``ccache`` stub in *directory*; return the directory."""
    directory.mkdir(exist_ok=True)
    stub = directory / ("ccache.exe" if os.name == "nt" else "ccache")
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return directory


def prepared_build(root: Path, policy: CompilerCachePolicy, check: str):
    """A prepared build whose make command runs the shell *check*."""
    return PreparedSimulationBuild(
        "sim",
        "id",
        cast(object, None),
        root,
        root,
        "verilator",
        "top",
        ("sh", "-c", check),
        policy.environment(),
        compiler_cache=policy,
    )


def test_defaults_use_issued_root_without_creating_storage(project, tmp_path):
    root, data = project
    cache = tmp_path / "issued" / "ccache"
    policy = resolve_policy(root, {COMPILER_CACHE_ROOT_ENV: str(cache)})
    assert policy == CompilerCachePolicy(True, cache, "5G")
    assert policy.environment().keys() == MANAGED
    assert not cache.parent.exists()
    (data / "booley.toml").write_text('[flows.sim.compiler_cache]\nmax_size="1Gi"\n')
    assert resolve_policy(root, {COMPILER_CACHE_ROOT_ENV: str(cache)}).max_size == "1Gi"


INVALID_SETTINGS = (
    '[flows.sim.compiler_cache]\nenabled="yes"',
    '[flows.sim.compiler_cache]\nmax_size="0G"',
    '[flows.sim.compiler_cache]\nmax_size="1GB"',
    "[flows.sim.compiler_cache]\nmax_size=5",
    "[flows.sim]\ncompiler_cache=false",
)


def test_invalid_settings_fail(project):
    root, data = project
    for content in INVALID_SETTINGS:
        (data / "booley.toml").write_text(content)
        with pytest.raises(CompilerCacheConfigurationError):
            resolve_policy(root, {})


@pytest.mark.timeout(180 if os.name == "nt" else 60)
def test_missing_ccache_builds_uncached_without_storage(project, tmp_path, monkeypatch):
    root, _data = project
    cache = tmp_path / "cache"
    policy = resolve_policy(root, {COMPILER_CACHE_ROOT_ENV: str(cache)})
    monkeypatch.setattr("booley.flows.sim.compiler_cache.shutil.which", lambda *a, **kw: None)
    prepared = prepared_build(root, policy, 'test -z "$OBJCACHE"')
    result = subprocess.run(
        ["sh", "-c", simulation_build_script(prepared, "b" * 32)],
        capture_output=True,
        text=True,
        timeout=60 if os.name == "nt" else 10,
        check=False,
    )
    assert result.returncode == 0
    assert not cache.exists()


def test_unset_root_builds_uncached_and_names_refresh(project, caplog):
    root, data = project
    policy = resolve_policy(root, {})
    environment = execution_environment(
        policy, compose_environment(policy, {}, ambient={}), ambient={}
    )
    assert environment["OBJCACHE"] == ""
    assert "CCACHE_DIR" not in environment
    assert caplog.text.count("booley session refresh") == 1
    assert not (data / ".runtime").exists()


def test_storage_error_builds_uncached(project, tmp_path, caplog):
    root, _data = project
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    policy = resolve_policy(root, {COMPILER_CACHE_ROOT_ENV: str(blocker / "ccache")})
    environment = {**policy.environment(), "PATH": str(fake_ccache(tmp_path / "tools"))}
    assert execution_environment(policy, environment, ambient={})["OBJCACHE"] == ""
    assert "unavailable" in caplog.text


def test_disabled_exports_no_cache_location_and_keeps_entries(project, tmp_path):
    root, data = project
    (data / "booley.toml").write_text("[flows.sim.compiler_cache]\nenabled=false\n")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "entry").write_text("keep")
    policy = resolve_policy(root, {COMPILER_CACHE_ROOT_ENV: str(cache)})
    environment = execution_environment(
        policy, compose_environment(policy, {}, ambient={}, build_root=root), ambient={}
    )
    assert environment["OBJCACHE"] == ""
    assert not {"CCACHE_DIR", "CCACHE_MAXSIZE", "CCACHE_BASEDIR"} & environment.keys()
    assert (cache / "entry").read_text() == "keep"


def test_debug_prefix_map_keeps_authored_user_cppflags(project, tmp_path, caplog):
    root, _data = project
    policy = resolve_policy(root, {COMPILER_CACHE_ROOT_ENV: str(tmp_path / "cache")})
    generated = root / "build directory$literal"
    environment = compose_environment(
        policy,
        {"USER_CPPFLAGS": "-DUSER=1", "CCACHE_DIR": "unrelated"},
        ambient={},
        build_root=generated,
    )
    assert environment["CCACHE_BASEDIR"] == str(generated)
    assert environment["USER_CPPFLAGS"].startswith("-DUSER=1 ")
    assert "-fdebug-prefix-map=" in environment["USER_CPPFLAGS"]
    assert "$$literal" in environment["USER_CPPFLAGS"]
    assert environment["CCACHE_DIR"] == str(policy.root)
    assert "yields to Booley" in caplog.text


def test_build_exports_do_not_leak_to_run(project, tmp_path, monkeypatch):
    root, _data = project
    cache = tmp_path / "cache"
    policy = resolve_policy(root, {COMPILER_CACHE_ROOT_ENV: str(cache)})
    assert not cache.exists(), "resolution alone must not create storage"
    tools = fake_ccache(tmp_path / "tools")
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("CCACHE_MAXSIZE", raising=False)
    prepared = prepared_build(
        root, policy, 'test "$OBJCACHE" = ccache && test "$CCACHE_MAXSIZE" = 5G'
    )
    script = simulation_build_script(
        prepared,
        "a" * 32,
        run_line='test "$OBJCACHE" = run-value && test -z "$CCACHE_MAXSIZE"',
        run_environment={"OBJCACHE": "run-value"},
    )
    # Git for Windows starts several shell processes here on loaded CI workers.
    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=60 if os.name == "nt" else 10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "rc=0" in result.stdout
    assert cache.is_dir()


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


@pytest.mark.parametrize("legacy", [False, True], ids=["flow", "tool"])
@pytest.mark.parametrize(
    "source", ["authored", "MAKEFLAGS", "GNUMAKEFLAGS", "MAKEOVERRIDES", "MFLAGS"]
)
@pytest.mark.parametrize(
    "assignment", ["OBJCACHE=", "--eval=CCACHE_DIR:=x"], ids=["assign", "eval"]
)
def test_real_edalize_preparation_preserves_make_overrides(
    project, tmp_path, monkeypatch, legacy, assignment, source
):
    root, _data = project
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv(COMPILER_CACHE_ROOT_ENV, str(cache_dir))
    for name in ("MAKEFLAGS", "GNUMAKEFLAGS", "MAKEOVERRIDES", "MFLAGS"):
        monkeypatch.delenv(name, raising=False)
    options = {"make_options": ["-j2", "OTHER=yes"]}
    if source == "authored":
        options["make_options"].append(assignment)
    else:
        monkeypatch.setenv(source, assignment)
    prepared = _prepare_real_verilator_target(root, options, legacy=legacy)
    recipe = (prepared.build_root / ("config.mk" if legacy else "Makefile")).read_text()
    assert all(option in recipe for option in options["make_options"])
    assert prepared.environment["OBJCACHE"] == "ccache"
    assert prepared.environment["CCACHE_DIR"] == str(cache_dir)
    assert not cache_dir.exists()
