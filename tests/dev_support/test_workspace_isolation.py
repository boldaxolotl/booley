"""Tests for workspace_isolation -- the single-owner isolation module."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.dev_support import workspace_isolation
from booley.dev_support.workspace_isolation import (
    CATEGORY_DIRS_DEFAULT,
    build_category_deny_patterns,
    clean_sim_artifacts,
    filter_state_file_for_category,
    get_category_dirs,
    heal_stranded_stashes,
    hide_opposite_sources,
    hide_specific_files,
    project_state_for_category,
    remove_shadow_package,
    validate_scope_category,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_core(
    work_dir: Path,
    *,
    rtl_dirs: list[str] | None = None,
    tb_dirs: list[str] | None = None,
) -> Path:
    """Author a flat ``.core`` under *work_dir* whose filesets place RTL sources
    under *rtl_dirs* and tb-tagged sources under *tb_dirs*.

    ADR 0026 follow-through: the ``.core`` ``tags:[tb]`` partition (read by
    ``source_dirs_from_core``) replaces ``[sources.*]`` in ``booley.toml`` as the
    category-dir source of truth. Each entry is a subdir file, so the derived dir
    is its parent directory.
    """
    rtl = rtl_dirs or ["rtl"]
    tb = tb_dirs or ["tb"]
    work_dir.mkdir(parents=True, exist_ok=True)
    rtl_files = "\n".join(f"      - {d}/dut.sv: {{file_type: systemVerilogSource}}" for d in rtl)
    tb_files = "\n".join(f"      - {d}/tb.sv: {{file_type: systemVerilogSource}}" for d in tb)
    core_path = work_dir / "design.core"
    core_path.write_text(
        "CAPI=2:\n"
        "name: ::demo\n"
        "filesets:\n"
        "  rtl:\n"
        "    files:\n"
        f"{rtl_files}\n"
        "  tb:\n"
        "    files:\n"
        f"{tb_files}\n"
        "    tags: [tb]\n"
        "targets:\n"
        "  sim: {filesets: [rtl, tb], toplevel: tb}\n",
        encoding="utf-8",
    )
    return core_path


# ---------------------------------------------------------------------------
# Category directory resolution
# ---------------------------------------------------------------------------


class TestCategoryDirs:
    def test_defaults_without_toml(self, tmp_path):
        result = get_category_dirs(tmp_path)
        assert set(result["rtl"]) == set(CATEGORY_DIRS_DEFAULT["rtl"])
        assert set(result["tb"]) == set(CATEGORY_DIRS_DEFAULT["tb"])

    def test_reads_from_core(self, tmp_path):
        _write_core(tmp_path, rtl_dirs=["rtl", "hw"], tb_dirs=["verif"])
        result = get_category_dirs(tmp_path)
        assert "hw/" in result["rtl"]
        assert "verif/" in result["tb"]

    def test_merges_with_defaults(self, tmp_path):
        _write_core(tmp_path, tb_dirs=["verif"])
        result = get_category_dirs(tmp_path)
        assert "tb/" in result["tb"]
        assert "verif/" in result["tb"]

    def test_none_work_dir_uses_fallback(self):
        result = get_category_dirs(None)
        assert "rtl/" in result["rtl"]
        assert "tb/" in result["tb"]

    def test_flat_repo_files_are_exact_entries(self, tmp_path):
        (tmp_path / "picorv32.v").write_text("module picorv32; endmodule\n")
        (tmp_path / "testbench.v").write_text("module testbench; endmodule\n")
        (tmp_path / "design.core").write_text(
            "CAPI=2:\n"
            "name: ::demo\n"
            "filesets:\n"
            "  rtl: {files: [picorv32.v]}\n"
            "  tb: {files: [testbench.v], tags: [tb]}\n"
            "targets:\n"
            "  sim: {filesets: [rtl, tb], toplevel: testbench}\n"
        )

        result = get_category_dirs(tmp_path)
        assert "picorv32.v" in result["rtl"]
        assert "picorv32.v/" not in result["rtl"]
        assert "testbench.v" in result["tb"]


# ---------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------


class TestValidateScopeCategory:
    def test_rtl_scope_rtl_category_ok(self):
        err = validate_scope_category(["rtl/mod_a.sv", "rtl/mod_b.sv"], "rtl")
        assert err is None

    def test_fw_scope_rtl_category_ok(self):
        err = validate_scope_category(["fw/boot.s"], "rtl")
        assert err is None

    def test_tb_scope_tb_category_ok(self):
        err = validate_scope_category(["tb/mod_a_tb.sv"], "tb")
        assert err is None

    def test_verif_scope_tb_category_ok(self, tmp_path):
        _write_core(tmp_path, tb_dirs=["verif"])
        err = validate_scope_category(["verif/tb_top.sv"], "tb", work_dir=tmp_path)
        assert err is None

    def test_verif_scope_rtl_category_error(self, tmp_path):
        _write_core(tmp_path, tb_dirs=["verif"])
        err = validate_scope_category(["verif/tb_top.sv"], "rtl", work_dir=tmp_path)
        assert err is not None
        assert "verif/tb_top.sv" in err

    def test_rtl_scope_tb_category_error(self):
        err = validate_scope_category(["rtl/mod_a.sv"], "tb")
        assert err is not None
        assert "rtl/mod_a.sv" in err

    def test_tb_scope_rtl_category_error(self):
        err = validate_scope_category(["tb/mod_a_tb.sv"], "rtl")
        assert err is not None
        assert "tb/mod_a_tb.sv" in err

    def test_mixed_scope_rtl_category_error(self):
        err = validate_scope_category(["rtl/mod_a.sv", "tb/mod_a_tb.sv"], "rtl")
        assert err is not None
        assert "tb/mod_a_tb.sv" in err

    def test_flat_repo_opposite_file_gets_bash_deny_pattern(self, tmp_path):
        (tmp_path / "picorv32.v").write_text("module picorv32; endmodule\n")
        (tmp_path / "testbench.v").write_text("module testbench; endmodule\n")
        (tmp_path / "design.core").write_text(
            "CAPI=2:\n"
            "name: ::demo\n"
            "filesets:\n"
            "  rtl: {files: [picorv32.v]}\n"
            "  tb: {files: [testbench.v], tags: [tb]}\n"
            "targets:\n"
            "  sim: {filesets: [rtl, tb], toplevel: testbench}\n"
        )

        patterns = build_category_deny_patterns("rtl", tmp_path)
        assert "Bash(*testbench.v*)" in patterns


# ---------------------------------------------------------------------------
# Source hiding
# ---------------------------------------------------------------------------


class TestHideOppositeSources:
    """Verify the context manager hides/restores dirs correctly."""

    def _make_worktree(self, tmp_path: Path) -> Path:
        """Create a fake worktree with rtl/, tb/, fw/, and data/ dirs."""
        wt = tmp_path / "worktree"
        for d in ("rtl", "tb", "fw", "data"):
            (wt / d).mkdir(parents=True)
            (wt / d / "stub.sv").write_text(f"// {d}\n", encoding="utf-8")
        return wt

    def test_tb_category_hides_rtl_dirs(self, tmp_path):
        wt = self._make_worktree(tmp_path)
        with hide_opposite_sources(wt, "tb") as hidden:
            assert not (wt / "rtl").exists()
            assert not (wt / "fw").exists()
            assert (wt / "data").exists()
            assert (wt / "tb").exists()
            assert set(hidden) >= {"rtl", "fw"}
        assert (wt / "rtl").is_dir()
        assert (wt / "fw").is_dir()
        assert (wt / "rtl" / "stub.sv").read_text(encoding="utf-8") == "// rtl\n"

    def test_rtl_category_hides_tb_dir(self, tmp_path):
        wt = self._make_worktree(tmp_path)
        with hide_opposite_sources(wt, "rtl") as hidden:
            assert not (wt / "tb").exists()
            assert (wt / "rtl").exists()
            assert (wt / "data").exists()
            assert "tb" in hidden
        assert (wt / "tb").is_dir()
        assert (wt / "tb" / "stub.sv").exists()

    def test_flat_repo_opposite_file_is_hidden_and_restored(self, tmp_path):
        wt = tmp_path / "worktree"
        wt.mkdir()
        rtl = wt / "picorv32.v"
        tb = wt / "testbench.v"
        rtl.write_text("module picorv32; endmodule\n")
        tb.write_text("module testbench; endmodule\n")
        category_dirs = {"rtl": ("picorv32.v",), "tb": ("testbench.v",)}

        with hide_opposite_sources(wt, "rtl", category_dirs=category_dirs) as hidden:
            assert rtl.is_file()
            assert not tb.exists()
            assert hidden == ["testbench.v"]
        assert tb.read_text() == "module testbench; endmodule\n"

    def test_rtl_category_hides_verif_dir(self, tmp_path):
        """verif/ hidden from RTL agents when booley.toml declares it as TB."""
        wt = self._make_worktree(tmp_path)
        _write_core(wt, tb_dirs=["verif"])
        verif = wt / "verif"
        verif.mkdir()
        (verif / "tb_top.sv").write_text("// tb\n", encoding="utf-8")
        with hide_opposite_sources(wt, "rtl") as hidden:
            assert not verif.exists(), "verif/ should be hidden from RTL agent"
            assert "verif" in hidden
            assert (wt / "rtl").exists()
        assert verif.is_dir()
        assert (verif / "tb_top.sv").read_text(encoding="utf-8") == "// tb\n"

    def test_stealth_cores_multicomponent_prefix(self, tmp_path):
        """Stealth-cores (ADR 0036) opposite dirs are multi-component symlinks.

        Regression: ``get_category_dirs`` resolves a project whose ``.core``
        files live under ``.booley_project/cores/`` to source-dir prefixes like
        ``.booley_project/cores/sim/`` -- themselves discovery symlinks pointing
        back at the real sources. The top-level stash loop built
        ``stash_dir/.booley_project/cores/sim`` without creating its parent, so
        ``shutil.move`` died recreating the symlink with FileNotFoundError,
        making every reviewer/specialist run crash deterministically.
        """
        wt = tmp_path / "worktree"
        (wt / "rtl").mkdir(parents=True)
        (wt / "rtl" / "top.sv").write_text("module top; endmodule\n", encoding="utf-8")
        (wt / "tb").mkdir()
        (wt / "tb" / "tb_top.sv").write_text("// tb\n", encoding="utf-8")
        (wt / ".booley_project" / "sim").mkdir(parents=True)
        (wt / ".booley_project" / "sim" / "model.py").write_text("# sim\n", encoding="utf-8")
        cores = wt / ".booley_project" / "cores"
        cores.mkdir()
        # The discovery symlinks Booley plants for stealth cores.
        (cores / "rtl").symlink_to("../../rtl", target_is_directory=True)
        (cores / "tb").symlink_to("../../tb", target_is_directory=True)
        (cores / "sim").symlink_to("../sim", target_is_directory=True)

        # Category dirs as get_category_dirs resolves them for this layout.
        stealth_dirs = {
            "rtl": (".booley_project/cores/rtl/", "rtl/"),
            "tb": (".booley_project/cores/sim/", ".booley_project/cores/tb/", "tb/"),
        }
        with (
            patch(
                "booley.dev_support.workspace_isolation.get_category_dirs",
                return_value=stealth_dirs,
            ),
            hide_opposite_sources(wt, "rtl") as hidden,
        ):
            # No crash, and the tb-side symlinks + repo tb/ are gone.
            assert not (cores / "sim").is_symlink()
            assert not (cores / "tb").is_symlink()
            assert not (wt / "tb").exists()
            assert "sim" in hidden and "tb" in hidden
            # Same-category rtl stays visible.
            assert (cores / "rtl").is_symlink()
            assert (wt / "rtl" / "top.sv").exists()

        # Everything restored verbatim, symlinks pointing where they did.
        assert (cores / "sim").is_symlink()
        assert (cores / "sim").readlink() == Path("../sim")
        assert (cores / "tb").readlink() == Path("../../tb")
        assert (wt / "tb" / "tb_top.sv").read_text(encoding="utf-8") == "// tb\n"
        assert (wt / ".booley_project" / "sim" / "model.py").exists()

    def test_restore_on_exception(self, tmp_path):
        wt = self._make_worktree(tmp_path)
        with pytest.raises(RuntimeError, match="boom"), hide_opposite_sources(wt, "tb"):
            assert not (wt / "rtl").exists()
            raise RuntimeError("boom")
        assert (wt / "rtl").is_dir()
        assert (wt / "fw").is_dir()

    def test_missing_dirs_skipped(self, tmp_path):
        """If the opposite dirs don't exist, nothing crashes."""
        wt = tmp_path / "worktree"
        (wt / "rtl").mkdir(parents=True)
        with hide_opposite_sources(wt, "rtl") as hidden:
            assert hidden == []
            assert (wt / "rtl").exists()

    def test_no_stash_dir_leaked(self, tmp_path):
        """Temp stash directory is cleaned up after restore."""
        wt = self._make_worktree(tmp_path)
        before = set(tmp_path.parent.glob("booley_isolation_*"))
        with hide_opposite_sources(wt, "tb"):
            pass
        after = set(tmp_path.parent.glob("booley_isolation_*"))
        assert before == after

    def test_nested_booley_project_dirs_hidden(self, tmp_path):
        """RTL dirs nested under .booley_project/ are also hidden for TB."""
        wt = self._make_worktree(tmp_path)
        nested_rtl = wt / ".booley_project" / "scratch" / "some_case" / "rtl"
        nested_rtl.mkdir(parents=True)
        (nested_rtl / "leaked.sv").write_text("// leaked\n", encoding="utf-8")
        nested_fw = wt / ".booley_project" / "scratch" / "some_case" / "fw"
        nested_fw.mkdir(parents=True)
        (nested_fw / "leaked.c").write_text("// leaked\n", encoding="utf-8")
        with hide_opposite_sources(wt, "tb") as _hidden:
            assert not nested_rtl.exists()
            assert not nested_fw.exists()
            assert (wt / ".booley_project").exists()
        assert nested_rtl.is_dir()
        assert (nested_rtl / "leaked.sv").read_text(encoding="utf-8") == "// leaked\n"
        assert nested_fw.is_dir()

    def test_nested_dirs_restored_on_exception(self, tmp_path):
        """Nested .booley_project dirs restored even on crash."""
        wt = self._make_worktree(tmp_path)
        nested_rtl = wt / ".booley_project" / "scratch" / "t1" / "rtl"
        nested_rtl.mkdir(parents=True)
        (nested_rtl / "mod.sv").write_text("// mod\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="boom"), hide_opposite_sources(wt, "tb"):
            assert not nested_rtl.exists()
            raise RuntimeError("boom")
        assert nested_rtl.is_dir()
        assert (nested_rtl / "mod.sv").exists()

    def test_restore_quarantines_recreated_dir_instead_of_nesting(self, tmp_path):
        """If a tool recreates rtl/ while hidden, restore original exactly."""
        wt = self._make_worktree(tmp_path)
        with hide_opposite_sources(wt, "tb"):
            recreated = wt / "rtl"
            recreated.mkdir()
            (recreated / "new.sv").write_text("// recreated\n", encoding="utf-8")
        assert (wt / "rtl" / "stub.sv").read_text(encoding="utf-8") == "// rtl\n"
        assert not (wt / "rtl" / "rtl").exists()
        conflicts = list((wt / ".booley_project" / "isolation_conflicts").glob("*/rtl/new.sv"))
        assert conflicts

    def test_byte_identical_recreated_tree_is_dropped_not_quarantined(self, tmp_path):
        """SETUP-F-43: FuseSoC re-stages sources during the run, so the hidden
        tree comes back byte-identical every time. Quarantining that copy grew
        isolation_conflicts/ on every run for zero information."""
        wt = self._make_worktree(tmp_path)
        with hide_opposite_sources(wt, "tb"):
            recreated = wt / "rtl"
            recreated.mkdir()
            (recreated / "stub.sv").write_text("// rtl\n", encoding="utf-8")

        assert (wt / "rtl" / "stub.sv").read_text(encoding="utf-8") == "// rtl\n"
        assert not (wt / ".booley_project" / "isolation_conflicts").exists()

    def test_differing_recreated_tree_is_still_quarantined(self, tmp_path):
        """Same name, different bytes — that IS new information; keep it."""
        wt = self._make_worktree(tmp_path)
        with hide_opposite_sources(wt, "tb"):
            recreated = wt / "rtl"
            recreated.mkdir()
            (recreated / "stub.sv").write_text("// EDITED\n", encoding="utf-8")

        assert (wt / "rtl" / "stub.sv").read_text(encoding="utf-8") == "// rtl\n"
        conflicts = list((wt / ".booley_project" / "isolation_conflicts").glob("*/rtl/stub.sv"))
        assert conflicts
        assert conflicts[0].read_text(encoding="utf-8") == "// EDITED\n"

    def test_artifact_roots_not_destructively_hidden(self, tmp_path):
        """Copied eval/worktree artifacts should not be moved by isolation."""
        wt = self._make_worktree(tmp_path)
        nested_rtl = wt / ".booley_project" / "eval" / "old_case" / "rtl"
        nested_rtl.mkdir(parents=True)
        (nested_rtl / "old.sv").write_text("// old\n", encoding="utf-8")
        with hide_opposite_sources(wt, "tb"):
            assert nested_rtl.is_dir()
            assert (nested_rtl / "old.sv").exists()

    def test_build_runtime_tree_not_hidden(self, tmp_path):
        """The relocated build tree (.booley_project/.runtime/) is an artifact
        root: edalize copies rtl/ filesets into its work dirs, so the nested
        opposite-category scan must NOT stash them (would corrupt the cached
        build and break incremental runs)."""
        wt = self._make_worktree(tmp_path)
        # Mimic an edalize work dir holding a copied rtl/ fileset.
        build_rtl = wt / ".booley_project" / ".runtime" / "edalize" / "sim" / "cfg" / "rtl"
        build_rtl.mkdir(parents=True)
        (build_rtl / "copied.sv").write_text("// copied\n", encoding="utf-8")
        with hide_opposite_sources(wt, "tb"):
            assert build_rtl.is_dir()
            assert (build_rtl / "copied.sv").exists()


# ---------------------------------------------------------------------------
# File hiding
# ---------------------------------------------------------------------------


class TestHideSpecificFiles:
    def test_hides_and_restores_files(self, tmp_path):
        wt = tmp_path / "worktree"
        (wt / "tb").mkdir(parents=True)
        (wt / "tb" / "tb1.sv").write_text("// tb1\n", encoding="utf-8")
        with hide_specific_files(wt, ["tb/tb1.sv"]) as hidden:
            assert "tb1.sv" in hidden
            assert not (wt / "tb" / "tb1.sv").exists()
        assert (wt / "tb" / "tb1.sv").read_text(encoding="utf-8") == "// tb1\n"

    def test_restore_on_exception(self, tmp_path):
        wt = tmp_path / "worktree"
        (wt / "tb").mkdir(parents=True)
        (wt / "tb" / "tb1.sv").write_text("// tb1\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="boom"), hide_specific_files(wt, ["tb/tb1.sv"]):
            assert not (wt / "tb" / "tb1.sv").exists()
            raise RuntimeError("boom")
        assert (wt / "tb" / "tb1.sv").exists()

    def test_missing_files_skipped(self, tmp_path):
        wt = tmp_path / "worktree"
        wt.mkdir()
        with hide_specific_files(wt, ["nonexistent.sv"]) as hidden:
            assert hidden == []

    def test_no_stash_leaked(self, tmp_path):
        wt = tmp_path / "worktree"
        (wt / "tb").mkdir(parents=True)
        (wt / "tb" / "tb1.sv").write_text("// tb1\n", encoding="utf-8")
        before = set(tmp_path.parent.glob("booley_hide_files_*"))
        with hide_specific_files(wt, ["tb/tb1.sv"]):
            pass
        after = set(tmp_path.parent.glob("booley_hide_files_*"))
        assert before == after


# ---------------------------------------------------------------------------
# Stash crash safety (SETUP-F-34)
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    """Return a PID that names no running process."""
    for candidate in range(4_000_000, 4_000_100):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except OSError:
            continue
    pytest.skip("no free PID found to simulate a dead process")
    raise AssertionError  # pragma: no cover


def _write_manifest(
    stash_dir: Path,
    work_dir: Path,
    moved: list[tuple[Path, Path]],
    *,
    pid: int | None = None,
    created: float | None = -1.0,
) -> Path:
    """Hand-write a stash manifest the way a since-killed run would have.

    ``created`` defaults to "just now"; pass ``None`` to omit the field.
    """
    payload: dict = {
        "pid": _dead_pid() if pid is None else pid,
        "work_dir": str(work_dir),
        "stash_dir": str(stash_dir),
        "moved": [[str(src), str(dst)] for src, dst in moved],
    }
    if created is not None:
        payload["created"] = time.time() if created == -1.0 else created
    manifest = workspace_isolation._manifest_path(stash_dir)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


@pytest.fixture
def isolated_tmp(tmp_path, monkeypatch):
    """Point tempfile (and therefore stashes + healing) at a private dir.

    Without this, healing globs the machine's real /tmp and the test would
    see (and try to heal) stashes belonging to other runs.
    """
    stash_root = tmp_path / "tmpdir"
    stash_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(stash_root))
    yield stash_root
    # Stashes are chmod 000; a test that leaves one locked (heal failed on
    # purpose) would otherwise make pytest's tmp_path cleanup warn.
    for path in stash_root.rglob("*"):
        with contextlib.suppress(OSError):
            if path.is_dir():
                path.chmod(0o755)


class TestStashCrashSafety:
    """A killed run must not leave the worktree missing tracked files."""

    def _make_worktree(self, tmp_path: Path) -> Path:
        wt = tmp_path / "worktree"
        for d in ("rtl", "tb"):
            (wt / d).mkdir(parents=True)
            (wt / d / "stub.sv").write_text(f"// {d}\n", encoding="utf-8")
        return wt

    def _manifests(self, stash_root: Path) -> list[Path]:
        return sorted(stash_root.glob("*.manifest.json"))

    def test_manifest_tracks_stash_and_is_cleared_on_restore(self, tmp_path, isolated_tmp):
        wt = self._make_worktree(tmp_path)
        with hide_opposite_sources(wt, "tb"):
            manifests = self._manifests(isolated_tmp)
            assert len(manifests) == 1
            data = json.loads(manifests[0].read_text(encoding="utf-8"))
            assert data["pid"] == os.getpid()
            assert data["work_dir"] == str(wt)
            assert [Path(src).name for src, _ in data["moved"]] == ["rtl"]
        assert self._manifests(isolated_tmp) == []

    def test_next_run_heals_a_stranded_stash(self, tmp_path, isolated_tmp):
        """The F-34 scenario: killed run, files still deleted, next run repairs it."""
        wt = self._make_worktree(tmp_path)
        moved: list[tuple[Path, Path]] = []
        stash_dir = workspace_isolation._stash_opposite_dirs(wt, "tb", {"rtl/"}, moved)
        assert stash_dir is not None
        workspace_isolation._lock_stash(stash_dir)
        # Pretend the owning process was SIGKILLed: manifest survives, the
        # in-process bookkeeping does not.
        manifest = workspace_isolation._manifest_path(stash_dir)
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["pid"] = _dead_pid()
        manifest.write_text(json.dumps(data), encoding="utf-8")
        workspace_isolation._ACTIVE_MANIFESTS.discard(manifest)
        assert not (wt / "rtl").exists()

        with hide_opposite_sources(wt, "rtl"):
            # The rtl/ tree is back (this run hides tb/, not rtl/).
            assert (wt / "rtl" / "stub.sv").read_text(encoding="utf-8") == "// rtl\n"
        assert not manifest.exists()
        assert not stash_dir.exists()
        assert (wt / "rtl" / "stub.sv").read_text(encoding="utf-8") == "// rtl\n"

    def test_heal_leaves_live_and_foreign_stashes_alone(self, tmp_path, isolated_tmp):
        wt = self._make_worktree(tmp_path)
        other = tmp_path / "other_worktree"
        other.mkdir()

        live = isolated_tmp / "booley_isolation_live.manifest.json"
        live.write_text(
            json.dumps(
                {
                    "pid": os.getppid(),  # a real, running process
                    "work_dir": str(wt),
                    "stash_dir": str(isolated_tmp / "booley_isolation_live"),
                    "moved": [],
                }
            ),
            encoding="utf-8",
        )
        foreign = isolated_tmp / "booley_isolation_foreign.manifest.json"
        foreign.write_text(
            json.dumps(
                {
                    "pid": _dead_pid(),
                    "work_dir": str(other),  # different worktree
                    "stash_dir": str(isolated_tmp / "booley_isolation_foreign"),
                    "moved": [],
                }
            ),
            encoding="utf-8",
        )

        assert heal_stranded_stashes(wt) == []
        assert live.exists()
        assert foreign.exists()

    def test_sigterm_restores_before_the_process_dies(self, tmp_path, isolated_tmp, monkeypatch):
        """SIGTERM skips ``finally`` — the handler must restore, then re-raise."""
        wt = self._make_worktree(tmp_path)
        killed: list[tuple[int, int]] = []

        with hide_opposite_sources(wt, "tb"):
            assert not (wt / "rtl").exists()
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            monkeypatch.setattr(
                workspace_isolation.os,
                "kill",
                lambda pid, sig: killed.append((pid, sig)),
            )
            handler(signal.SIGTERM, None)
            # Restored byte-identically, mid-context, before the re-raise.
            assert (wt / "rtl" / "stub.sv").read_text(encoding="utf-8") == "// rtl\n"
            assert killed == [(os.getpid(), signal.SIGTERM)]
        # The context's own finally must not double-restore or explode.
        assert (wt / "rtl" / "stub.sv").read_text(encoding="utf-8") == "// rtl\n"
        assert self._manifests(isolated_tmp) == []

    def test_hide_specific_files_is_healed_too(self, tmp_path, isolated_tmp):
        wt = tmp_path / "worktree"
        (wt / "tb").mkdir(parents=True)
        (wt / "tb" / "tb1.sv").write_text("// tb1\n", encoding="utf-8")

        stash_dir = Path(tempfile.mkdtemp(prefix="booley_hide_files_"))
        dst = stash_dir / "tb" / "tb1.sv"
        dst.parent.mkdir(parents=True)
        (wt / "tb" / "tb1.sv").rename(dst)
        manifest = _write_manifest(stash_dir, wt, [(wt / "tb" / "tb1.sv", dst)])

        assert heal_stranded_stashes(wt)
        assert (wt / "tb" / "tb1.sv").read_text(encoding="utf-8") == "// tb1\n"
        assert not manifest.exists()


class TestHealingIsBestEffort:
    """Healing must never be able to break the run it is trying to help."""

    def _stranded(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """Worktree with rtl/ stashed away and the owning pid dead."""
        wt = tmp_path / "worktree"
        for d in ("rtl", "tb"):
            (wt / d).mkdir(parents=True)
            (wt / d / "stub.sv").write_text(f"// {d}\n", encoding="utf-8")
        moved: list[tuple[Path, Path]] = []
        stash_dir = workspace_isolation._stash_opposite_dirs(wt, "tb", {"rtl/"}, moved)
        assert stash_dir is not None
        workspace_isolation._lock_stash(stash_dir)
        manifest = workspace_isolation._manifest_path(stash_dir)
        workspace_isolation._ACTIVE_MANIFESTS.discard(manifest)
        _write_manifest(stash_dir, wt, moved)
        return wt, stash_dir, manifest

    def test_a_failing_restore_does_not_propagate(self, tmp_path, isolated_tmp, monkeypatch):
        """An OSError mid-restore used to escape and exit-2 the whole tool run."""
        wt, _stash, manifest = self._stranded(tmp_path)
        monkeypatch.setattr(
            workspace_isolation,
            "_heal_restore",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("read-only file system")),
        )

        assert heal_stranded_stashes(wt) == []
        # Counted, not lost: the next run may well succeed.
        assert json.loads(manifest.read_text(encoding="utf-8"))["heal_failures"] == 1

    def test_the_run_that_heals_still_starts(self, tmp_path, isolated_tmp, monkeypatch):
        """The real regression: hide_opposite_sources must survive a bad manifest."""
        wt, _stash, _manifest = self._stranded(tmp_path)
        monkeypatch.setattr(
            workspace_isolation,
            "_heal_restore",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")),
        )

        with hide_opposite_sources(wt, "rtl") as hidden:
            assert hidden == ["tb"]
        assert (wt / "tb" / "stub.sv").exists()

    def test_one_bad_manifest_does_not_strand_the_others(
        self,
        tmp_path,
        isolated_tmp,
        monkeypatch,
    ):
        wt, bad_stash, _bad = self._stranded(tmp_path)
        # A second, healthy stranded stash for the same worktree.
        (wt / "extra").mkdir()
        (wt / "extra" / "x.sv").write_text("// x\n", encoding="utf-8")
        good_stash = Path(tempfile.mkdtemp(prefix="booley_hide_files_"))
        good_dst = good_stash / "extra" / "x.sv"
        good_dst.parent.mkdir(parents=True)
        (wt / "extra" / "x.sv").rename(good_dst)
        _write_manifest(good_stash, wt, [(wt / "extra" / "x.sv", good_dst)])

        real_restore = workspace_isolation._heal_restore

        def _flaky(work_dir, stash_dir, moved):
            if stash_dir == bad_stash:
                raise OSError("boom")
            real_restore(work_dir, stash_dir, moved)

        monkeypatch.setattr(workspace_isolation, "_heal_restore", _flaky)

        assert heal_stranded_stashes(wt) == [good_stash]
        assert (wt / "extra" / "x.sv").read_text(encoding="utf-8") == "// x\n"

    def test_permanently_unhealable_manifest_is_retired(self, tmp_path, isolated_tmp, monkeypatch):
        """It must not wedge *every* future run — give up after a few tries."""
        wt, _stash, manifest = self._stranded(tmp_path)
        monkeypatch.setattr(
            workspace_isolation,
            "_heal_restore",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("read-only file system")),
        )

        for _ in range(workspace_isolation._HEAL_MAX_ATTEMPTS):
            assert heal_stranded_stashes(wt) == []

        assert not manifest.exists()
        assert manifest.with_name(manifest.name + ".retired").exists()
        # And the retired record is invisible to the heal glob from now on.
        with hide_opposite_sources(wt, "rtl"):
            pass


class TestHealingDoesNotClobberRecoveredWork:
    """SETUP-F-34 healing must repair damage, never roll the worktree back."""

    def _stranded_with_live_copy(self, tmp_path: Path, live_text: str) -> tuple[Path, Path, Path]:
        wt = tmp_path / "worktree"
        (wt / "tb").mkdir(parents=True)
        (wt / "tb" / "tb1.sv").write_text("// stashed\n", encoding="utf-8")
        stash_dir = Path(tempfile.mkdtemp(prefix="booley_hide_files_"))
        dst = stash_dir / "tb" / "tb1.sv"
        dst.parent.mkdir(parents=True)
        (wt / "tb" / "tb1.sv").rename(dst)
        manifest = _write_manifest(stash_dir, wt, [(wt / "tb" / "tb1.sv", dst)])
        # The user noticed the deletion, ran `git restore .`, then kept working.
        (wt / "tb" / "tb1.sv").write_text(live_text, encoding="utf-8")
        return wt, stash_dir, manifest

    def test_recovered_file_is_never_replaced_by_the_stash(self, tmp_path, isolated_tmp):
        wt, stash_dir, manifest = self._stranded_with_live_copy(tmp_path, "// two days of work\n")

        heal_stranded_stashes(wt)

        assert (wt / "tb" / "tb1.sv").read_text(encoding="utf-8") == "// two days of work\n"
        assert not manifest.exists()
        assert not stash_dir.exists()
        # The *stash* copy is what gets quarantined — never the live tree.
        quarantined = sorted((wt / ".booley_project" / "isolation_conflicts").rglob("tb1.sv"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text(encoding="utf-8") == "// stashed\n"

    def test_identical_recovered_file_is_not_quarantined(self, tmp_path, isolated_tmp):
        """The FuseSoC re-stage case (F-43): identical bytes, no conflict dir."""
        wt, stash_dir, manifest = self._stranded_with_live_copy(tmp_path, "// stashed\n")

        heal_stranded_stashes(wt)

        assert (wt / "tb" / "tb1.sv").read_text(encoding="utf-8") == "// stashed\n"
        assert not (wt / ".booley_project" / "isolation_conflicts").exists()
        assert not manifest.exists()
        assert not stash_dir.exists()

    def test_stale_manifest_is_retired_without_restoring(self, tmp_path, isolated_tmp):
        """A day-old stash is no longer evidence that the worktree is damaged."""
        wt = tmp_path / "worktree"
        (wt / "tb").mkdir(parents=True)
        (wt / "tb" / "tb1.sv").write_text("// tb1\n", encoding="utf-8")
        stash_dir = Path(tempfile.mkdtemp(prefix="booley_hide_files_"))
        dst = stash_dir / "tb" / "old.sv"
        dst.parent.mkdir(parents=True)
        dst.write_text("// ancient\n", encoding="utf-8")
        manifest = _write_manifest(
            stash_dir,
            wt,
            [(wt / "tb" / "old.sv", dst)],
            created=time.time() - workspace_isolation._HEAL_MAX_AGE_S - 60,
        )

        assert heal_stranded_stashes(wt) == []
        assert not (wt / "tb" / "old.sv").exists()
        assert manifest.with_name(manifest.name + ".retired").exists()

    def test_manifest_without_created_is_retired(self, tmp_path, isolated_tmp):
        """Unknown age ⇒ unknown damage: refuse rather than guess."""
        wt = tmp_path / "worktree"
        (wt / "tb").mkdir(parents=True)
        stash_dir = Path(tempfile.mkdtemp(prefix="booley_hide_files_"))
        dst = stash_dir / "tb" / "old.sv"
        dst.parent.mkdir(parents=True)
        dst.write_text("// ancient\n", encoding="utf-8")
        manifest = _write_manifest(
            stash_dir,
            wt,
            [(wt / "tb" / "old.sv", dst)],
            created=None,
        )

        assert heal_stranded_stashes(wt) == []
        assert not (wt / "tb" / "old.sv").exists()
        assert manifest.with_name(manifest.name + ".retired").exists()


class TestHealManifestContainment:
    """A world-writable /tmp must not become a write primitive into the worktree."""

    def test_src_outside_the_worktree_is_refused(self, tmp_path, isolated_tmp):
        wt = tmp_path / "worktree"
        wt.mkdir()
        victim = tmp_path / "outside" / "authorized_keys"
        victim.parent.mkdir()
        stash_dir = Path(tempfile.mkdtemp(prefix="booley_isolation_"))
        payload = stash_dir / "payload"
        payload.write_text("pwned\n", encoding="utf-8")
        manifest = _write_manifest(stash_dir, wt, [(victim, payload)])

        assert heal_stranded_stashes(wt) == []
        assert not victim.exists()
        assert manifest.with_name(manifest.name + ".retired").exists()

    def test_dotdot_escape_is_refused(self, tmp_path, isolated_tmp):
        wt = tmp_path / "worktree"
        wt.mkdir()
        stash_dir = Path(tempfile.mkdtemp(prefix="booley_isolation_"))
        payload = stash_dir / "payload"
        payload.write_text("pwned\n", encoding="utf-8")
        escaped = wt / ".." / "outside" / "x"
        manifest = _write_manifest(stash_dir, wt, [(escaped, payload)])

        assert heal_stranded_stashes(wt) == []
        assert not (tmp_path / "outside").exists()
        assert manifest.with_name(manifest.name + ".retired").exists()

    def test_dst_outside_the_stash_dir_is_refused(self, tmp_path, isolated_tmp):
        wt = tmp_path / "worktree"
        wt.mkdir()
        stash_dir = Path(tempfile.mkdtemp(prefix="booley_isolation_"))
        elsewhere = tmp_path / "attacker" / "payload"
        elsewhere.parent.mkdir()
        elsewhere.write_text("pwned\n", encoding="utf-8")
        manifest = _write_manifest(stash_dir, wt, [(wt / "rtl", elsewhere)])

        assert heal_stranded_stashes(wt) == []
        assert not (wt / "rtl").exists()
        assert manifest.with_name(manifest.name + ".retired").exists()

    def test_renamed_manifest_pointing_at_a_foreign_stash_is_refused(self, tmp_path, isolated_tmp):
        """stash_dir must be exactly the sibling the manifest name implies."""
        wt = tmp_path / "worktree"
        wt.mkdir()
        real_stash = Path(tempfile.mkdtemp(prefix="booley_isolation_"))
        (real_stash / "rtl").mkdir()
        manifest = isolated_tmp / "booley_isolation_decoy.manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "pid": _dead_pid(),
                    "work_dir": str(wt),
                    "stash_dir": str(real_stash),  # not <manifest-stem>
                    "created": time.time(),
                    "moved": [[str(wt / "rtl"), str(real_stash / "rtl")]],
                }
            ),
            encoding="utf-8",
        )

        assert heal_stranded_stashes(wt) == []
        assert not (wt / "rtl").exists()
        assert manifest.with_name(manifest.name + ".retired").exists()

    def test_symlinked_manifest_is_ignored(self, tmp_path, isolated_tmp):
        """A symlink in /tmp is someone aiming healing at a file they don't own."""
        wt = tmp_path / "worktree"
        wt.mkdir()
        stash_dir = Path(tempfile.mkdtemp(prefix="booley_isolation_"))
        payload = stash_dir / "payload"
        payload.write_text("pwned\n", encoding="utf-8")
        real = tmp_path / "real.json"
        real.write_text(
            json.dumps(
                {
                    "pid": _dead_pid(),
                    "work_dir": str(wt),
                    "stash_dir": str(stash_dir),
                    "created": time.time(),
                    "moved": [[str(wt / "payload"), str(payload)]],
                }
            ),
            encoding="utf-8",
        )
        link = workspace_isolation._manifest_path(stash_dir)
        link.symlink_to(real)

        assert heal_stranded_stashes(wt) == []
        assert not (wt / "payload").exists()


# ---------------------------------------------------------------------------
# Sim artifact cleanup
# ---------------------------------------------------------------------------


class TestCleanSimArtifacts:
    """Verify clean_sim_artifacts removes compiled sim outputs."""

    def test_removes_icarus_work_dir(self, tmp_path):
        wt = tmp_path / "worktree"
        work = wt / "sim" / "work" / "default.tb_top"
        work.mkdir(parents=True)
        (work / "sim.vvp").write_text("compiled binary", encoding="utf-8")
        (work / "trace.vcd").write_text("vcd data", encoding="utf-8")
        with patch("booley.runtime.shared_infra.get_sim_output_dir", return_value=wt / "sim"):
            clean_sim_artifacts(wt)
        assert not (wt / "sim" / "work").exists()
        assert (wt / "sim").is_dir()

    def test_removes_verilator_obj_dir(self, tmp_path):
        wt = tmp_path / "worktree"
        obj = wt / "sim" / "obj_dir"
        obj.mkdir(parents=True)
        (obj / "Vtop.cpp").write_text("generated", encoding="utf-8")
        with patch("booley.runtime.shared_infra.get_sim_output_dir", return_value=wt / "sim"):
            clean_sim_artifacts(wt)
        assert not obj.exists()

    def test_noop_when_no_sim_dir(self, tmp_path):
        wt = tmp_path / "worktree"
        wt.mkdir()
        with patch("booley.runtime.shared_infra.get_sim_output_dir", return_value=wt / "sim"):
            clean_sim_artifacts(wt)

    def test_fallback_when_import_fails(self, tmp_path):
        wt = tmp_path / "worktree"
        work = wt / "sim" / "work" / "default.tb_mod"
        work.mkdir(parents=True)
        (work / "sim.vvp").write_text("binary", encoding="utf-8")
        with patch("booley.runtime.shared_infra.get_sim_output_dir", side_effect=ImportError):
            clean_sim_artifacts(wt)
        assert not (wt / "sim" / "work").exists()


# ---------------------------------------------------------------------------
# Shadow package removal
# ---------------------------------------------------------------------------


class TestRemoveShadowPackage:
    def test_removes_shadow_dir(self, tmp_path):
        shadow = tmp_path / "booley"
        shadow.mkdir()
        (shadow / "some_file.py").write_text("", encoding="utf-8")
        remove_shadow_package(tmp_path)
        assert not shadow.exists()

    def test_preserves_real_package(self, tmp_path):
        real = tmp_path / "booley" / "data" / "refs"
        real.mkdir(parents=True)
        remove_shadow_package(tmp_path)
        assert (tmp_path / "booley").is_dir()

    def test_noop_when_no_shadow(self, tmp_path):
        remove_shadow_package(tmp_path)  # should not raise

    def test_handles_permission_error(self, tmp_path):
        shadow = tmp_path / "booley"
        shadow.mkdir()
        with patch(
            "booley.dev_support.workspace_isolation.shutil.rmtree", side_effect=OSError("perm")
        ):
            remove_shadow_package(tmp_path)  # logs warning, doesn't raise
        assert shadow.is_dir()


# ---------------------------------------------------------------------------
# State projection — opposite-category review detail stripping
# ---------------------------------------------------------------------------


def _sample_state() -> dict:
    """Fixture: state with both RTL-side and TB-side review findings."""
    return {
        "slug": "demo-0001",
        "criteria": {
            "review_rtl_code_style_clean": {
                "met": False,
                "mandatory": True,
                "detail": {
                    "issues": 1,
                    "CRITICAL": 1,
                    "MAJOR": 0,
                    "verify_attempts": 0,
                    "pending": [
                        {
                            "file": "rtl/dut.v",
                            "line": 42,
                            "summary": "secret RTL implementation note",
                            "fix_suggestion": "rename port foo_i to bar_i",
                        },
                    ],
                    "resolved": [],
                    "checks": ["c1", "c2"],
                },
            },
            "review_tb_quality_clean": {
                "met": False,
                "mandatory": True,
                "detail": {
                    "issues": 2,
                    "MAJOR": 2,
                    "pending": [
                        {"file": "verif/tb.sv", "summary": "tb gap"},
                    ],
                },
            },
            "lint_clean_default": {
                "met": True,
                "mandatory": False,
                "detail": {
                    "warnings": 0,
                    "checks": ["lint_rule_naming_rtl_internals"],
                },
            },
            "sim_pass_default": {
                "met": False,
                "mandatory": True,
                "detail": {"tb_path": "verif/tb.sv"},
            },
            "_report_submitted": {"met": False, "mandatory": True},
        },
        "timeline": [
            {"mcp_tool": "coder", "endpoint_kind": "mcp_tool", "args": {"category": "rtl"}}
        ],
    }


class TestProjectStateForCategory:
    def test_tb_strips_rtl_review_findings(self):
        st = _sample_state()
        out = project_state_for_category(st, "tb")
        rtl_detail = out["criteria"]["review_rtl_code_style_clean"]["detail"]
        # Summary counts survive
        assert rtl_detail["CRITICAL"] == 1
        assert rtl_detail["issues"] == 1
        assert rtl_detail["verify_attempts"] == 0
        # Prose payload stripped
        assert "pending" not in rtl_detail
        assert "resolved" not in rtl_detail
        assert "checks" not in rtl_detail

    def test_tb_strips_lint_detail(self):
        # lint findings carry RTL signal/file names too
        out = project_state_for_category(_sample_state(), "tb")
        lint_detail = out["criteria"]["lint_clean_default"]["detail"]
        assert lint_detail["warnings"] == 0
        assert "checks" not in lint_detail

    def test_tb_preserves_own_review_findings(self):
        out = project_state_for_category(_sample_state(), "tb")
        tb_detail = out["criteria"]["review_tb_quality_clean"]["detail"]
        assert "pending" in tb_detail
        assert tb_detail["pending"][0]["summary"] == "tb gap"

    def test_rtl_strips_tb_review_findings(self):
        out = project_state_for_category(_sample_state(), "rtl")
        tb_detail = out["criteria"]["review_tb_quality_clean"]["detail"]
        assert "pending" not in tb_detail
        assert tb_detail["MAJOR"] == 2

    def test_rtl_preserves_rtl_review_findings(self):
        out = project_state_for_category(_sample_state(), "rtl")
        rtl_detail = out["criteria"]["review_rtl_code_style_clean"]["detail"]
        # RTL Specialists see their own findings in full
        assert "pending" in rtl_detail
        assert rtl_detail["pending"][0]["file"] == "rtl/dut.v"

    def test_preserves_timeline(self):
        out = project_state_for_category(_sample_state(), "tb")
        assert out["timeline"][0]["mcp_tool"] == "coder"

    def test_preserves_sim_and_underscore_criteria(self):
        # Cross-category and developer-internal criteria pass through.
        out = project_state_for_category(_sample_state(), "tb")
        assert out["criteria"]["sim_pass_default"]["detail"]["tb_path"] == "verif/tb.sv"
        assert "_report_submitted" in out["criteria"]

    def test_unknown_category_is_noop(self):
        st = _sample_state()
        out = project_state_for_category(st, "neither")
        assert out is st

    def test_non_dict_passes_through(self):
        assert project_state_for_category([], "tb") == []
        assert project_state_for_category("nope", "tb") == "nope"

    def test_idempotent(self):
        once = project_state_for_category(_sample_state(), "tb")
        twice = project_state_for_category(once, "tb")
        assert once == twice

    def test_original_state_unchanged(self):
        # Projection must not mutate the input — caller may still hold it.
        st = _sample_state()
        project_state_for_category(st, "tb")
        assert "pending" in st["criteria"]["review_rtl_code_style_clean"]["detail"]


# ---------------------------------------------------------------------------
# State file filtering — context manager round-trip
# ---------------------------------------------------------------------------


class TestFilterStateFileForCategory:
    def _write_state(self, path: Path) -> bytes:
        body = json.dumps(_sample_state(), indent=2).encode("utf-8")
        path.write_bytes(body)
        return body

    def test_filters_then_restores(self, tmp_path):
        sf = tmp_path / "booley_state.json"
        original = self._write_state(sf)

        with filter_state_file_for_category(sf, "tb") as applied:
            assert applied is True
            mid = json.loads(sf.read_text(encoding="utf-8"))
            # Filtered: RTL review pending stripped
            assert "pending" not in mid["criteria"]["review_rtl_code_style_clean"]["detail"]

        # Restored byte-for-byte
        assert sf.read_bytes() == original

    def test_restores_even_when_body_raises(self, tmp_path):
        sf = tmp_path / "booley_state.json"
        original = self._write_state(sf)

        with pytest.raises(RuntimeError), filter_state_file_for_category(sf, "tb"):
            raise RuntimeError("agent crashed")

        # Restored despite the exception
        assert sf.read_bytes() == original

    def test_restores_on_sigterm(self, tmp_path, monkeypatch):
        """A killed run must not leave the projected (lossy) state behind."""
        sf = tmp_path / "booley_state.json"
        original = self._write_state(sf)
        killed: list[tuple[int, int]] = []

        with filter_state_file_for_category(sf, "tb") as applied:
            assert applied is True
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            monkeypatch.setattr(
                workspace_isolation.os,
                "kill",
                lambda pid, sig: killed.append((pid, sig)),
            )
            handler(signal.SIGTERM, None)
            assert sf.read_bytes() == original
            assert killed == [(os.getpid(), signal.SIGTERM)]

        assert sf.read_bytes() == original

    def test_skips_when_path_missing(self, tmp_path):
        sf = tmp_path / "does_not_exist.json"
        with filter_state_file_for_category(sf, "tb") as applied:
            assert applied is False

    def test_skips_when_path_none(self):
        with filter_state_file_for_category(None, "tb") as applied:
            assert applied is False

    def test_skips_unknown_category(self, tmp_path):
        sf = tmp_path / "booley_state.json"
        original = self._write_state(sf)
        with filter_state_file_for_category(sf, "neither") as applied:
            assert applied is False
        assert sf.read_bytes() == original

    def test_corrupt_json_is_passthrough(self, tmp_path):
        sf = tmp_path / "booley_state.json"
        sf.write_bytes(b"not json at all {")
        with filter_state_file_for_category(sf, "tb") as applied:
            assert applied is False
        # File untouched
        assert sf.read_bytes() == b"not json at all {"
