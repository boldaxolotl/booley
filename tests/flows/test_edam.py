"""Unit tests for the Edalize invocation layer (booley.flows.edam, ADR 0019).

Two tiers:
  * pure EDAM construction + security (no Edalize import) — always run;
  * flow ``configure()`` + generated command-string assertions —
    ``importorskip("edalize")`` so they run wherever Edalize is installed
    (sandbox image / CI) and skip on a bare dev env.
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure src/ is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from booley.flows.edam import (
    EdamSecurityError,
    WorkRootLeaseError,
    build_edam,
    configure,
    file_type_for,
    make_command,
    relpath_for_make,
    try_work_root_lease,
    work_root_for,
    work_root_lease,
)
from tests.conftest import symlink_or_skip


def _hold_work_root(path: str, acquired, release) -> None:
    with work_root_lease(path, timeout_s=5.0):
        acquired.set()
        if not release.wait(5.0):
            raise TimeoutError("test did not release held work root")


def _wait_for_work_root(path: str, waiting, acquired) -> None:
    with work_root_lease(path, timeout_s=5.0, on_wait=waiting.set):
        acquired.set()


# ---------------------------------------------------------------------------
# file_type_for
# ---------------------------------------------------------------------------


class TestFileType:
    def test_known_suffixes(self):
        assert file_type_for("a.sv") == "systemVerilogSource"
        assert file_type_for("a.svh") == "systemVerilogSource"
        assert file_type_for("a.v") == "verilogSource"
        assert file_type_for("a.vh") == "verilogSource"
        assert file_type_for("waiver.vlt") == "vlt"
        assert file_type_for("c.xdc") == "xdc"
        assert file_type_for("c.sdc") == "SDC"

    def test_case_insensitive(self):
        assert file_type_for("A.SV") == "systemVerilogSource"

    def test_unknown_suffix_falls_through(self):
        assert file_type_for("notes.txt") == "user"


# ---------------------------------------------------------------------------
# build_edam — structure + parameter encoding
# ---------------------------------------------------------------------------


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    """A workspace with a couple of source files and an include dir."""
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "top.sv").write_text("module top; endmodule\n")
    (tmp_path / "rtl" / "pkg.v").write_text("// pkg\n")
    (tmp_path / "rtl" / "inc").mkdir()
    return tmp_path


class TestBuildEdam:
    def test_files_typed_and_absolute(self, ws: Path):
        edam = build_edam(
            name="design",
            files=[ws / "rtl" / "top.sv", ws / "rtl" / "pkg.v"],
            toplevel="top",
            eda_tool="verilator",
            workspace_root=ws,
        )
        assert edam["toplevel"] == "top"
        names = {e["name"]: e["file_type"] for e in edam["files"]}
        assert names[str((ws / "rtl" / "top.sv").resolve())] == "systemVerilogSource"
        assert names[str((ws / "rtl" / "pkg.v").resolve())] == "verilogSource"

    def test_include_dirs_marked(self, ws: Path):
        edam = build_edam(
            name="d",
            files=[ws / "rtl" / "top.sv"],
            toplevel="top",
            eda_tool="verilator",
            workspace_root=ws,
            include_dirs=[ws / "rtl" / "inc"],
        )
        inc = [e for e in edam["files"] if e.get("is_include_file")]
        assert len(inc) == 1
        assert inc[0]["name"] == str((ws / "rtl" / "inc").resolve())

    def test_name_sanitized(self, ws: Path):
        edam = build_edam(
            name="my design/cfg:1",
            files=[ws / "rtl" / "top.sv"],
            toplevel="top",
            eda_tool="verilator",
            workspace_root=ws,
        )
        assert edam["name"] == "my_design_cfg_1"

    def test_bare_define_is_boolean(self, ws: Path):
        edam = build_edam(
            name="d",
            files=[ws / "rtl" / "top.sv"],
            toplevel="top",
            eda_tool="verilator",
            workspace_root=ws,
            defines=["SYNTHESIS"],
        )
        p = edam["parameters"]["SYNTHESIS"]
        assert p == {"datatype": "bool", "paramtype": "vlogdefine", "default": True}

    def test_valued_define_int_vs_str(self, ws: Path):
        edam = build_edam(
            name="d",
            files=[ws / "rtl" / "top.sv"],
            toplevel="top",
            eda_tool="verilator",
            workspace_root=ws,
            defines=["WIDTH=8", "MODE=fast"],
        )
        assert edam["parameters"]["WIDTH"] == {
            "datatype": "int",
            "paramtype": "vlogdefine",
            "default": 8,
        }
        assert edam["parameters"]["MODE"] == {
            "datatype": "str",
            "paramtype": "vlogdefine",
            "default": "fast",
        }

    def test_vlogparams(self, ws: Path):
        edam = build_edam(
            name="d",
            files=[ws / "rtl" / "top.sv"],
            toplevel="top",
            eda_tool="verilator",
            workspace_root=ws,
            vlogparams={"DEPTH": "16", "NAME": "abc"},
        )
        assert edam["parameters"]["DEPTH"] == {
            "datatype": "int",
            "paramtype": "vlogparam",
            "default": 16,
        }
        assert edam["parameters"]["NAME"]["paramtype"] == "vlogparam"

    def test_plusargs_value_and_declare_only(self, ws: Path):
        edam = build_edam(
            name="d",
            files=[ws / "rtl" / "top.sv"],
            toplevel="top",
            eda_tool="verilator",
            workspace_root=ws,
            plusargs={"TESTID": 3, "VERBOSE": None},
        )
        assert edam["parameters"]["TESTID"] == {
            "datatype": "int",
            "paramtype": "plusarg",
            "default": 3,
        }
        # declare-only: no default key
        assert edam["parameters"]["VERBOSE"] == {
            "datatype": "str",
            "paramtype": "plusarg",
        }

    def test_empty_defines_skipped(self, ws: Path):
        edam = build_edam(
            name="d",
            files=[ws / "rtl" / "top.sv"],
            toplevel="top",
            eda_tool="verilator",
            workspace_root=ws,
            defines=["", "  "],
        )
        assert edam["parameters"] == {}

    def test_eda_tool_options_merged_into_flow_options(self, ws: Path):
        edam = build_edam(
            name="d",
            files=[ws / "rtl" / "top.sv"],
            toplevel="top",
            eda_tool="verilator",
            workspace_root=ws,
            flow="sim",
            eda_tool_options={"verilator_options": ["--trace", "--trace-depth", "3"]},
        )
        assert edam["flow_options"] == {
            "tool": "verilator",  # upstream Edalize schema field
            "verilator_options": ["--trace", "--trace-depth", "3"],
        }


# ---------------------------------------------------------------------------
# Security — workspace confinement + option whitelist
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_file_outside_workspace_rejected(self, ws: Path, tmp_path: Path):
        outside = tmp_path.parent / "evil.sv"
        with pytest.raises(EdamSecurityError, match="outside the workspace"):
            build_edam(
                name="d",
                files=[outside],
                toplevel="top",
                eda_tool="verilator",
                workspace_root=ws,
            )

    def test_symlink_escape_rejected(self, ws: Path, tmp_path: Path):
        secret = tmp_path.parent / "secret.sv"
        secret.write_text("module secret; endmodule\n")
        link = ws / "rtl" / "link.sv"
        symlink_or_skip(link, secret)
        with pytest.raises(EdamSecurityError, match="outside the workspace"):
            build_edam(
                name="d",
                files=[link],
                toplevel="top",
                eda_tool="verilator",
                workspace_root=ws,
            )

    def test_non_whitelisted_flow_option_rejected(self, ws: Path):
        with pytest.raises(EdamSecurityError, match="not permitted"):
            build_edam(
                name="d",
                files=[ws / "rtl" / "top.sv"],
                toplevel="top",
                eda_tool="verilator",
                workspace_root=ws,
                flow="lint",
                eda_tool_options={"iverilog_options": ["x"]},  # not allowed for lint
            )

    def test_missing_upstream_eda_tool_rejected(self, ws: Path):
        # build_edam always injects the EDA-tool field, so probe the validator via a raw call.
        from booley.flows.edam import _validate_flow_options

        with pytest.raises(EdamSecurityError, match="must name the upstream Edalize 'tool' field"):
            _validate_flow_options("sim", {"verilator_options": []})

    def test_unknown_flow_rejected(self, ws: Path):
        with pytest.raises(EdamSecurityError, match="unknown flow"):
            build_edam(
                name="d",
                files=[ws / "rtl" / "top.sv"],
                toplevel="top",
                eda_tool="verilator",
                workspace_root=ws,
                flow="bogus",
            )


# ---------------------------------------------------------------------------
# make_command
# ---------------------------------------------------------------------------


class TestMakeCommand:
    def test_default_target(self):
        assert make_command("/w") == ["make", "-C", "/w"]

    def test_run_target_with_vars(self):
        cmd = make_command("/w", target="run", make_vars={"EXTRA_OPTIONS": "+TESTID=3"})
        assert cmd == ["make", "-C", "/w", "run", "EXTRA_OPTIONS=+TESTID=3"]


# ---------------------------------------------------------------------------
# Relocatable paths inside the Session Runtime workspace
# ---------------------------------------------------------------------------


class TestRelativePaths:
    def test_relative_to_emits_relative_names(self, ws: Path):
        work_root = work_root_for(ws, "lint", "cfg")
        edam = build_edam(
            name="d",
            files=[ws / "rtl" / "top.sv"],
            toplevel="top",
            eda_tool="verilator",
            workspace_root=ws,
            flow="lint",
            relative_to=work_root,
        )
        name = edam["files"][0]["name"]
        assert not Path(name).is_absolute()
        # resolves back to the real file relative to the work_root
        assert (work_root / name).resolve() == (ws / "rtl" / "top.sv").resolve()

    def test_relative_still_confines(self, ws: Path, tmp_path: Path):
        outside = tmp_path.parent / "evil.sv"
        with pytest.raises(EdamSecurityError):
            build_edam(
                name="d",
                files=[outside],
                toplevel="top",
                eda_tool="verilator",
                workspace_root=ws,
                relative_to=work_root_for(ws, "lint", "cfg"),
            )

    def test_work_root_for_layout_and_variant(self, ws: Path):
        wr = work_root_for(ws, "lint", "cfg/a")
        assert wr == ws / ".booley_project" / ".runtime" / "edalize" / "lint" / "cfg_a"
        traced = work_root_for(ws, "sim", "cfg", variant="trace")
        assert traced.name == "cfg-trace"

    def test_work_root_refuses_booley_source_checkout(self, tmp_path: Path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "pyproject.toml").write_text(
            "[tool.booley]\nsource_checkout = true\n",
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="cannot be initialized or used as a Project"):
            work_root_for(source, "lint", "cfg")
        assert not (source / ".booley_project").exists()

    def test_relpath_for_make(self, ws: Path):
        wr = work_root_for(ws, "lint", "cfg")
        assert relpath_for_make(wr, ws) == ".booley_project/.runtime/edalize/lint/cfg"

    def test_relative_configure_relocatable(self, ws: Path):
        pytest.importorskip("edalize")
        work_root = work_root_for(ws, "lint", "cfg")
        edam = build_edam(
            name="design",
            files=[ws / "rtl" / "top.sv"],
            toplevel="top",
            eda_tool="verilator",
            workspace_root=ws,
            flow="lint",
            relative_to=work_root,
            eda_tool_options={"verilator_options": ["-Wall"]},
        )
        configure("lint", edam, work_root)
        vc = (work_root / "design.vc").read_text()
        # The .vc carries a relative path, not an absolute workspace path.
        assert str(ws) not in vc
        assert "../" in vc
        assert "--lint-only" in vc  # added by the Lint flow itself


# ---------------------------------------------------------------------------
# Work-root ownership
# ---------------------------------------------------------------------------


class TestWorkRootLease:
    def test_open_failure_is_an_infrastructure_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        def fail_open(*_args, **_kwargs):
            raise OSError("cannot open lock")

        monkeypatch.setattr(Path, "open", fail_open)

        with (
            pytest.raises(WorkRootLeaseError, match="cannot open lock"),
            try_work_root_lease(tmp_path / "synth" / "toy"),
        ):
            pytest.fail("unreachable")

    def test_acquisition_failure_is_an_infrastructure_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        def fail_acquire(_handle):
            raise OSError("cannot acquire lock")

        monkeypatch.setattr("booley.flows.edam.acquire_file_lock", fail_acquire)

        with (
            pytest.raises(WorkRootLeaseError, match="cannot acquire lock"),
            try_work_root_lease(tmp_path / "synth" / "toy"),
        ):
            pytest.fail("unreachable")

    def test_same_root_is_exclusive_across_processes(self, tmp_path: Path):
        work_root = tmp_path / "synth" / "toy"
        source_root = Path(__file__).resolve().parents[2] / "src"
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(source_root), env.get("PYTHONPATH", "")) if part
        )
        probe = (
            "from pathlib import Path; import sys; "
            "from booley.flows.edam import try_work_root_lease; "
            "ctx = try_work_root_lease(Path(sys.argv[1])); "
            "leased = ctx.__enter__(); "
            "print('acquired' if leased is not None else 'busy'); "
            "ctx.__exit__(None, None, None)"
        )

        with work_root_lease(work_root, timeout_s=1.0):
            result = subprocess.run(
                [sys.executable, "-c", probe, str(work_root)],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
                env=env,
            )

        assert result.stdout.strip() == "busy"

    def test_different_roots_remain_independent(self, tmp_path: Path):
        first = tmp_path / "synth" / "first"
        second = tmp_path / "synth" / "second"

        with (
            work_root_lease(first, timeout_s=1.0),
            try_work_root_lease(second) as leased,
        ):
            assert leased == second.resolve()

    def test_sanitized_target_collision_uses_one_lease(self, ws: Path):
        slash = work_root_for(ws, "synth", "toy/target")
        colon = work_root_for(ws, "synth", "toy:target")
        assert slash == colon

        with (
            work_root_lease(slash, timeout_s=1.0),
            try_work_root_lease(colon) as leased,
        ):
            assert leased is None

    def test_waiter_acquires_after_holder_releases(self, tmp_path: Path):
        ctx = multiprocessing.get_context("spawn")
        holder_acquired = ctx.Event()
        release_holder = ctx.Event()
        waiter_blocked = ctx.Event()
        waiter_acquired = ctx.Event()
        work_root = str(tmp_path / "synth" / "toy")
        holder = ctx.Process(
            target=_hold_work_root,
            args=(work_root, holder_acquired, release_holder),
        )
        waiter = ctx.Process(
            target=_wait_for_work_root,
            args=(work_root, waiter_blocked, waiter_acquired),
        )
        holder.start()
        try:
            assert holder_acquired.wait(5.0)
            waiter.start()
            assert waiter_blocked.wait(5.0)
            assert not waiter_acquired.is_set()
            release_holder.set()
            assert waiter_acquired.wait(5.0)
        finally:
            release_holder.set()
            holder.join(5.0)
            waiter.join(5.0)
            if holder.is_alive():
                holder.terminate()
                holder.join(5.0)
            if waiter.is_alive():
                waiter.terminate()
                waiter.join(5.0)

        assert holder.exitcode == 0
        assert waiter.exitcode == 0

    def test_owner_exit_releases_lease(self, tmp_path: Path):
        ctx = multiprocessing.get_context("spawn")
        acquired = ctx.Event()
        never_release = ctx.Event()
        work_root = tmp_path / "synth" / "toy"
        holder = ctx.Process(
            target=_hold_work_root,
            args=(str(work_root), acquired, never_release),
        )
        holder.start()
        assert acquired.wait(5.0)
        holder.terminate()
        holder.join(5.0)
        assert not holder.is_alive()

        with work_root_lease(work_root, timeout_s=1.0) as leased:
            assert leased == work_root.resolve()

    def test_body_exception_releases_lease(self, tmp_path: Path):
        work_root = tmp_path / "synth" / "toy"

        with (
            pytest.raises(RuntimeError, match="body failed"),
            work_root_lease(work_root, timeout_s=1.0),
        ):
            raise RuntimeError("body failed")

        with try_work_root_lease(work_root) as leased:
            assert leased == work_root.resolve()


# ---------------------------------------------------------------------------
# Flow configure() — needs real Edalize
# ---------------------------------------------------------------------------


class TestConfigure:
    def test_sim_configure_emits_makefile_and_vc(self, ws: Path, tmp_path: Path):
        pytest.importorskip("edalize")
        edam = build_edam(
            name="design",
            files=[ws / "rtl" / "top.sv"],
            toplevel="top",
            eda_tool="verilator",
            workspace_root=ws,
            flow="sim",
            defines=["MYDEF", "WIDTH=8"],
            vlogparams={"DEPTH": 16},
            eda_tool_options={"verilator_options": ["--trace", "--trace-depth", "3"]},
        )
        work_root = tmp_path / "wr"
        configure("sim", edam, work_root)
        makefile = (work_root / "Makefile").read_text()
        assert "verilator -f design.vc" in makefile
        vc = (work_root / "design.vc").read_text()
        # Trace overlay + param/define encoding land in the .vc
        assert "--trace" in vc
        assert "--top-module top" in vc
        assert "-DMYDEF=1" in vc
        assert "-DWIDTH=8" in vc
        assert "-GDEPTH=16" in vc

    def test_lint_configure(self, ws: Path, tmp_path: Path):
        pytest.importorskip("edalize")
        edam = build_edam(
            name="design",
            files=[ws / "rtl" / "top.sv"],
            toplevel="top",
            eda_tool="verilator",
            workspace_root=ws,
            flow="lint",
            eda_tool_options={"verilator_options": ["--lint-only", "-Wall"]},
        )
        work_root = tmp_path / "wr_lint"
        configure("lint", edam, work_root)
        assert (work_root / "Makefile").is_file()
        vc = (work_root / "design.vc").read_text()
        assert "--lint-only" in vc
