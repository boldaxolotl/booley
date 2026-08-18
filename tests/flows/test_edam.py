"""Unit tests for the Edalize invocation layer (booley.flows.edam, ADR 0019).

Two tiers:
  * pure EDAM construction + security (no Edalize import) — always run;
  * flow ``configure()`` + generated command-string assertions —
    ``importorskip("edalize")`` so they run wherever Edalize is installed
    (sandbox image / CI) and skip on a bare dev env.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure src/ is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from tests.conftest import symlink_or_skip

from booley.flows.edam import (
    EdamSecurityError,
    build_edam,
    configure,
    file_type_for,
    make_command,
    relpath_for_make,
    work_root_for,
)

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
# Relocatable (relative) paths for the host/sandbox boundary
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
        # the .vc carries a relative path, not an absolute host path
        assert str(ws) not in vc
        assert "../" in vc
        assert "--lint-only" in vc  # added by the Lint flow itself


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
