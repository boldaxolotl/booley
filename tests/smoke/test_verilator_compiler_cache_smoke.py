"""Real Verilator + ccache builds through Booley's managed compiler-cache policy.

Runs inside the Booley Sandbox Image (CI coverage-release step). Proves the
issue #879 acceptance criteria automatically: an RTL edit is reflected in
simulation behavior (no stale reuse) while unchanged generated C++ hits the
Project cache across build generations, and concurrent builds sharing one
cache all succeed with their own results.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from booley.flows.base import SubprocessResult
from booley.flows.sim.compiler_cache import (
    compose_environment,
    execution_environment,
    resolve_policy,
)
from booley.flows.sim.execution import NamedTests, SimulationExecution, SimulationOptions
from booley.runtime.compiler_cache import COMPILER_CACHE_RELATIVE, IssuedCacheIdentity
from booley.runtime.project_dir import reset_cache
from booley.targets.catalog import TargetCatalog

pytestmark = pytest.mark.skipif(
    shutil.which("verilator") is None or shutil.which("ccache") is None,
    reason="real smoke runs inside the pinned Booley Sandbox Image",
)
_TOOL_TIMEOUT_S = 300
_HOST = IssuedCacheIdentity(root=None, in_sandbox=False)


def _write_project(root: Path, revision: int) -> Path:
    """Write a one-Target Verilator Project whose RTL drives *revision*."""
    data = root / ".booley_project"
    data.mkdir(parents=True, exist_ok=True)
    (data / "booley.toml").write_text('[project]\nname="compiler-cache-smoke"\n')
    (data / "tests.toml").write_text('[sim]\ntests=["check"]\n')
    _write_rtl(root, revision)
    (root / "top.sv").write_text(
        "module top;\n"
        "  wire [7:0] value;\n"
        "  dut d(.value(value));\n"
        "  initial begin\n"
        '    #1 $display("VALUE=%0d", value);\n'
        '    $display("[SIM_RESULT] PASSED");\n'
        "    $finish;\n"
        "  end\n"
        "endmodule\n"
    )
    core = {
        "name": "booley:smoke:compiler_cache:1",
        "filesets": {
            "rtl": {"files": [{"dut.sv": {"file_type": "systemVerilogSource"}}]},
            "tb": {"files": [{"top.sv": {"file_type": "systemVerilogSource"}}], "tags": ["tb"]},
        },
        "targets": {
            "sim": {
                "flow": "sim",
                "filesets": ["rtl", "tb"],
                "toplevel": "top",
                "flow_options": {
                    "tool": "verilator",
                    "make_options": ["-j2"],
                    "verilator_options": ["--timing", "-Wno-fatal", "--main", "--exe"],
                },
            }
        },
    }
    (root / "smoke.core").write_text("CAPI=2:\n" + yaml.safe_dump(core))
    return data


def _write_rtl(root: Path, revision: int) -> None:
    (root / "dut.sv").write_text(
        f"module dut(output logic [7:0] value);\n  assign value = 8'd{revision};\nendmodule\n"
    )


def _cache_hits(cache_dir: Path) -> int:
    """Return ccache's cumulative direct + preprocessed hit count for *cache_dir*."""
    result = subprocess.run(
        ["ccache", "--print-stats"],
        env={**os.environ, "CCACHE_DIR": str(cache_dir)},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    stats = {
        parts[0]: int(parts[1])
        for line in result.stdout.splitlines()
        if len(parts := line.split()) == 2 and parts[1].isdigit()
    }
    return stats.get("direct_cache_hit", 0) + stats.get("preprocessed_cache_hit", 0)


def _simulate(root: Path) -> str:
    """Run the ``check`` test through the real Simulation Flow; return all output."""
    outputs: list[str] = []

    def invoke(command: list[str], *, timeout: int) -> SubprocessResult:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=min(timeout + 120, _TOOL_TIMEOUT_S),
            check=False,
        )
        outputs.append(result.stdout + result.stderr)
        return SubprocessResult(
            returncode=result.returncode, stdout=result.stdout, stderr=result.stderr
        )

    handle = TargetCatalog.build(root).select("sim", for_flow="sim")
    result = SimulationExecution(invoke=invoke, options=SimulationOptions(timeout_ms=30000)).run(
        handle, NamedTests(("check",))
    )
    output = "\n".join(outputs)
    assert result.passed, f"{result}\n{output}"
    return output


def test_rtl_edit_is_reflected_while_unchanged_objects_hit_cache(tmp_path, monkeypatch):
    root = tmp_path / "project"
    data = _write_project(root, revision=1)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(data))
    monkeypatch.delenv("BOOLEY_COMPILER_CACHE_ROOT", raising=False)
    reset_cache()
    cache_dir = data / COMPILER_CACHE_RELATIVE

    assert re.findall(r"VALUE=(\d+)", _simulate(root)) == ["1"]
    assert cache_dir.is_dir(), "enabled build must populate the Project cache"
    hits_before_edit = _cache_hits(cache_dir)

    _write_rtl(root, revision=2)
    # A stale object would still print VALUE=1; ccache keys on content.
    assert re.findall(r"VALUE=(\d+)", _simulate(root)) == ["2"]
    assert _cache_hits(cache_dir) > hits_before_edit, (
        "unchanged generated C++ must hit across build generations"
    )


def _cached_build(root: Path, revision: int, cache_owner: Path) -> str:
    """Build and run one design with Booley's managed cache environment."""
    (root / ".booley_project").mkdir(parents=True)
    source = root / "dut.sv"
    source.write_text(
        "module top;\n"
        f'  initial begin $display("VALUE=%0d", {revision}); $finish; end\n'
        "endmodule\n"
    )
    build_root = root / "obj_dir"
    policy = resolve_policy(root, issued=_HOST, owner=cache_owner)
    managed = compose_environment(policy, {}, ambient=os.environ, build_root=build_root)
    environment = {
        **os.environ,
        **execution_environment(policy, managed, ambient=os.environ, build_root=build_root),
    }
    assert environment["OBJCACHE"] == "ccache"
    subprocess.run(
        [
            "verilator",
            "--binary",
            "-Wno-fatal",
            "--Mdir",
            str(build_root),
            "-o",
            "sim",
            str(source),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=_TOOL_TIMEOUT_S,
        check=True,
    )
    return subprocess.run(
        [str(build_root / "sim")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout


def test_concurrent_builds_share_cache_without_corruption(tmp_path):
    owner = tmp_path / "shared-project-data"
    owner.mkdir()
    revisions = range(1, 5)
    with ThreadPoolExecutor(max_workers=len(revisions)) as pool:
        outputs = list(
            pool.map(
                lambda revision: _cached_build(tmp_path / f"sandbox-{revision}", revision, owner),
                revisions,
            )
        )
    assert [re.findall(r"VALUE=(\d+)", output) for output in outputs] == [
        [str(revision)] for revision in revisions
    ]
    cache_dir = owner / COMPILER_CACHE_RELATIVE
    hits_before = _cache_hits(cache_dir)
    # After the concurrent writers, the shared cache still serves correct objects.
    assert re.findall(r"VALUE=(\d+)", _cached_build(tmp_path / "later", 9, owner)) == ["9"]
    assert _cache_hits(cache_dir) > hits_before
