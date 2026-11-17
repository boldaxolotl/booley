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
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure src/ is importable (fallback when not installed via pip install -e .)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from booley.core.build_paths import work_root_for
from booley.flows.edam import (
    EdamFile,
    EdamSecurityError,
    WorkRootLeaseError,
    build_edam,
    configure,
    try_work_root_lease,
    work_root_lease,
)
from booley.flows.target_parameters import ParameterValue
from tests.conftest import symlink_or_skip
from tests.subprocess_budget import run_bounded_child


def _hold_work_root(path: str, acquired, release) -> None:
    with work_root_lease(path, timeout_s=5.0):
        acquired.set()
        if not release.wait(5.0):
            raise TimeoutError("test did not release held work root")


def _wait_for_work_root(path: str, waiting, acquired) -> None:
    with work_root_lease(path, timeout_s=5.0, on_wait=waiting.set):
        acquired.set()


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


def _sv(path: Path) -> EdamFile:
    return EdamFile(path, "systemVerilogSource")


def _edam(ws: Path, **overrides: Any) -> dict[str, Any]:
    """``build_edam`` for the workspace's ``top.sv`` with *overrides* applied."""
    options: dict[str, Any] = {
        "name": "d",
        "files": [_sv(ws / "rtl" / "top.sv")],
        "toplevel": "top",
        "eda_tool": "verilator",
        "workspace_root": ws,
    }
    options.update(overrides)
    return build_edam(**options)


def _param(datatype: str, paramtype: str, default: Any = None) -> dict[str, Any]:
    expected: dict[str, Any] = {"datatype": datatype, "paramtype": paramtype}
    if default is not None:
        expected["default"] = default
    return expected


class TestBuildEdam:
    def test_files_keep_declared_type_and_are_absolute(self, ws: Path):
        # pkg.v is declared SystemVerilog: the declared type wins over the suffix.
        edam = _edam(
            ws,
            name="design",
            files=[EdamFile(ws / "rtl" / "top.sv", "verilogSource"), _sv(ws / "rtl" / "pkg.v")],
        )
        assert edam["toplevel"] == "top"
        names = {e["name"]: e["file_type"] for e in edam["files"]}
        assert names[str((ws / "rtl" / "top.sv").resolve())] == "verilogSource"
        assert names[str((ws / "rtl" / "pkg.v").resolve())] == "systemVerilogSource"

    def test_name_sanitized(self, ws: Path):
        assert _edam(ws, name="my design/cfg:1")["name"] == "my_design_cfg_1"

    @pytest.mark.parametrize(
        ("option", "value", "expected"),
        [
            pytest.param(
                "defines",
                ["SYNTHESIS"],
                {"SYNTHESIS": _param("bool", "vlogdefine", True)},
                id="bare-define-is-boolean",
            ),
            pytest.param(
                "defines",
                ["WIDTH=8", "MODE=fast"],
                {
                    "WIDTH": _param("int", "vlogdefine", 8),
                    "MODE": _param("str", "vlogdefine", "fast"),
                },
                id="valued-define-int-vs-str",
            ),
            pytest.param("defines", ["", "  "], {}, id="empty-defines-skipped"),
            pytest.param(
                "vlogparams",
                {"DEPTH": "16", "NAME": "abc"},
                {
                    "DEPTH": _param("int", "vlogparam", 16),
                    "NAME": _param("str", "vlogparam", "abc"),
                },
                id="vlogparams",
            ),
            pytest.param(
                "plusargs",
                {"TESTID": 3, "VERBOSE": None},
                {
                    "TESTID": _param("int", "plusarg", 3),
                    # declare-only: no default key
                    "VERBOSE": _param("str", "plusarg"),
                },
                id="plusargs-value-and-declare-only",
            ),
            pytest.param(
                "vlogparams",
                {
                    "S": ParameterValue("str", "123"),
                    "F": ParameterValue("file", "a b"),
                    "B": ParameterValue("bool", "1"),
                    "I": ParameterValue("int", "12"),
                    "R": ParameterValue("real", "1.25"),
                },
                {
                    "S": _param("str", "vlogparam", "123"),
                    "F": _param("file", "vlogparam", "a b"),
                    "B": _param("bool", "vlogparam", True),
                    "I": _param("int", "vlogparam", 12),
                    "R": _param("real", "vlogparam", 1.25),
                },
                id="explicit-target-datatypes-survive",
            ),
        ],
    )
    def test_parameters_keep_their_kind_and_type(
        self, ws: Path, option: str, value: Any, expected: dict[str, Any]
    ):
        assert _edam(ws, **{option: value})["parameters"] == expected


# ---------------------------------------------------------------------------
# Security — workspace confinement + option whitelist
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_file_outside_workspace_rejected(self, ws: Path, tmp_path: Path):
        outside = tmp_path.parent / "evil.sv"
        with pytest.raises(EdamSecurityError, match="outside the workspace"):
            _edam(ws, files=[_sv(outside)])

    def test_file_behind_symlinked_booley_dir_entry_accepted(self, ws: Path, tmp_path: Path):
        """A Codespace links ``/work/.booley``; links below the entry stay refused."""
        # The fixture workspace is tmp_path itself, so both link targets live beside it.
        data = tmp_path.parent / f"{tmp_path.name}-linked-data"
        elsewhere = tmp_path.parent / f"{tmp_path.name}-elsewhere"
        (data / "build").mkdir(parents=True)
        elsewhere.mkdir()
        symlink_or_skip(ws / ".booley", data)
        symlink_or_skip(data / "linked", elsewhere)
        staged = data / "build" / "src" / "top.sv"
        staged.parent.mkdir()
        staged.write_text("module top; endmodule\n")
        (elsewhere / "top.sv").write_text("module top; endmodule\n")

        edam = _edam(ws, files=[_sv(ws / ".booley" / "build" / "src" / "top.sv")])
        assert edam["files"][0]["name"] == str(staged.resolve())
        with pytest.raises(EdamSecurityError, match="outside the workspace"):
            _edam(ws, files=[_sv(ws / ".booley" / "linked" / "top.sv")])

    def test_symlink_escape_rejected(self, ws: Path, tmp_path: Path):
        secret = tmp_path.parent / "secret.sv"
        secret.write_text("module secret; endmodule\n")
        link = ws / "rtl" / "link.sv"
        symlink_or_skip(link, secret)
        with pytest.raises(EdamSecurityError, match="outside the workspace"):
            _edam(ws, files=[_sv(link)])

    def test_non_whitelisted_flow_option_rejected(self, ws: Path):
        with pytest.raises(EdamSecurityError, match="not permitted"):
            _edam(
                ws,
                flow="lint",
                eda_tool_options={"iverilog_options": ["x"]},  # not allowed for lint
            )

    def test_unknown_flow_rejected(self, ws: Path):
        with pytest.raises(EdamSecurityError, match="unknown flow"):
            _edam(ws, flow="bogus")


# ---------------------------------------------------------------------------
# Relocatable paths inside the Sandbox workspace
# ---------------------------------------------------------------------------


class TestRelativePaths:
    def test_relative_still_confines(self, ws: Path, tmp_path: Path):
        outside = tmp_path.parent / "evil.sv"
        with pytest.raises(EdamSecurityError):
            _edam(ws, files=[_sv(outside)], relative_to=work_root_for(ws, "lint", "cfg"))

    def test_work_root_refuses_booley_source_checkout(self, tmp_path: Path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "pyproject.toml").write_text(
            "[tool.booley]\nsource_checkout = true\n",
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="cannot be initialized or used as a Project"):
            work_root_for(source, "lint", "cfg")
        assert not (source / ".booley").exists()

    def test_relative_configure_relocatable(self, ws: Path):
        pytest.importorskip("edalize")
        work_root = work_root_for(ws, "lint", "cfg")
        edam = _edam(
            ws,
            name="design",
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
            result = run_bounded_child(
                [sys.executable, "-c", probe, str(work_root)],
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
        edam = _edam(
            ws,
            name="design",
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
        edam = _edam(
            ws,
            name="design",
            flow="lint",
            eda_tool_options={"verilator_options": ["--lint-only", "-Wall"]},
        )
        work_root = tmp_path / "wr_lint"
        configure("lint", edam, work_root)
        assert (work_root / "Makefile").is_file()
        vc = (work_root / "design.vc").read_text()
        assert "--lint-only" in vc
