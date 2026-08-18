"""Tests for shared_infra: path resolution, config helpers, and file listing."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestSourceDirPrefixes:
    """ADR 0026: source_dirs entries may be files (exact prefix) or dirs."""

    def test_directory_entry_gets_trailing_separators(self, tmp_path: Path):
        from booley.shared_infra import source_dir_prefixes

        (tmp_path / "rtl").mkdir()
        assert source_dir_prefixes(["rtl"], tmp_path) == ("rtl/", "rtl\\")

    def test_file_entry_gets_exact_path_prefix(self, tmp_path: Path):
        from booley.shared_infra import source_dir_prefixes

        # A root-level file: '/' and '\\' forms are identical -> one prefix.
        (tmp_path / "picorv32.v").write_text("//\n", encoding="utf-8")
        assert source_dir_prefixes(["picorv32.v"], tmp_path) == ("picorv32.v",)

    def test_nested_file_entry_gets_both_separators(self, tmp_path: Path):
        from booley.shared_infra import source_dir_prefixes

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "top.v").write_text("//\n", encoding="utf-8")
        assert source_dir_prefixes(["src/top.v"], tmp_path) == (
            "src/top.v",
            "src\\top.v",
        )

    def test_unstatable_base_treats_all_as_dirs(self):
        from booley.shared_infra import source_dir_prefixes

        # base_dir None (legacy CWD path) -> every entry is a directory prefix.
        assert source_dir_prefixes(["picorv32.v"], None) == (
            "picorv32.v/",
            "picorv32.v\\",
        )

    def test_mixed_and_deduped(self, tmp_path: Path):
        from booley.shared_infra import source_dir_prefixes

        (tmp_path / "rtl").mkdir()
        (tmp_path / "top.v").write_text("//\n", encoding="utf-8")
        out = source_dir_prefixes(["rtl", "rtl", "top.v"], tmp_path)
        assert out == ("rtl/", "rtl\\", "top.v")


class TestSourcePathMatches:
    def test_directory_prefix_matches_descendants(self):
        from booley.shared_infra import source_path_matches

        assert source_path_matches("./rtl/top.sv", ("rtl/",))
        assert not source_path_matches("rtl_extra/top.sv", ("rtl/",))

    def test_file_prefix_is_exact(self):
        from booley.shared_infra import source_path_matches

        assert source_path_matches("./picorv32.v", ("picorv32.v",))
        assert not source_path_matches("picorv32.v.bak", ("picorv32.v",))

    def test_parent_segments_cannot_borrow_category(self):
        from booley.shared_infra import source_path_matches

        assert not source_path_matches("rtl/../tb/top.sv", ("rtl/",))


# ---------------------------------------------------------------------------
# Test resolve_project_root (can be tested without importing module globals)
# ---------------------------------------------------------------------------


class TestResolveProjectRoot:
    def test_from_env_var(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("RTL_PROJECT_ROOT", str(tmp_path))
        # Import after setting env so module-level PROJECT_ROOT picks it up
        from booley.shared_infra import resolve_project_root

        result = resolve_project_root()
        assert result == tmp_path.resolve()

    def test_cwd_walkup_beats_fallback(self, tmp_path: Path, monkeypatch):
        """CWD walk-up should take priority over fallback_dir."""
        monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
        from booley.shared_infra import resolve_project_root

        # Create a fake project root with .git
        project = tmp_path / "project"
        project.mkdir()
        (project / ".git").mkdir()
        (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        monkeypatch.chdir(project)
        fallback = tmp_path / "wrong_fallback"
        result = resolve_project_root(fallback_dir=fallback)
        assert result == project.resolve()

    def test_fallback_used_when_no_git(self, tmp_path: Path, monkeypatch):
        """Fallback used when CWD has no .git ancestor."""
        monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
        from booley.shared_infra import resolve_project_root

        # tmp_path has no .git above it
        monkeypatch.chdir(tmp_path)
        fallback = Path("/some/fallback")
        result = resolve_project_root(fallback_dir=fallback)
        assert result == fallback.resolve()

    def test_empty_git_sentinel_at_ancestor_is_ignored(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
        from booley.shared_infra import resolve_project_root

        (tmp_path / ".git").mkdir()
        child = tmp_path / "child"
        child.mkdir()
        monkeypatch.chdir(child)
        fallback = tmp_path / "fallback"

        assert resolve_project_root(fallback_dir=fallback) == fallback.resolve()


# ---------------------------------------------------------------------------
# Test _sort_packages_first
# ---------------------------------------------------------------------------

# _sort_packages_first / build_file_list were removed with the legacy
# configs.toml registry path (ADR 0022 decision 23 retired); FuseSoC now owns
# fileset ordering. Their tests were deleted with them.


# ---------------------------------------------------------------------------
# Test derive_work_dir
# ---------------------------------------------------------------------------


class TestDeriveWorkDir:
    @patch("booley.shared_infra.get_sim_output_dir")
    def test_sim_basic(self, mock_sim_dir, tmp_path: Path):
        mock_sim_dir.return_value = tmp_path / "util" / "sim"
        from booley.shared_infra import derive_work_dir

        result = derive_work_dir(tmp_path, "sim", "config_a")
        assert result == tmp_path / "util" / "sim" / "work" / "config_a"

    @patch("booley.shared_infra.get_syn_output_dir")
    def test_syn_basic(self, mock_syn_dir, tmp_path: Path):
        mock_syn_dir.return_value = tmp_path / "util" / "syn"
        from booley.shared_infra import derive_work_dir

        result = derive_work_dir(tmp_path, "syn", "config_b")
        assert result == tmp_path / "util" / "syn" / "syn_result" / "config_b"

    @patch("booley.shared_infra.get_sim_output_dir")
    def test_with_top_module(self, mock_sim_dir, tmp_path: Path):
        mock_sim_dir.return_value = tmp_path / "util" / "sim"
        from booley.shared_infra import derive_work_dir

        result = derive_work_dir(tmp_path, "sim", "config_a", top_module="my_top")
        assert result.name == "config_a.my_top"

    @patch("booley.shared_infra.get_sim_output_dir")
    def test_with_test_name(self, mock_sim_dir, tmp_path: Path):
        mock_sim_dir.return_value = tmp_path / "util" / "sim"
        from booley.shared_infra import derive_work_dir

        result = derive_work_dir(tmp_path, "sim", "config_a", test_name="test_foo")
        assert "test_foo" in result.name

    @patch("booley.shared_infra.get_sim_output_dir")
    def test_with_test_id_legacy(self, mock_sim_dir, tmp_path: Path):
        mock_sim_dir.return_value = tmp_path / "util" / "sim"
        from booley.shared_infra import derive_work_dir

        result = derive_work_dir(tmp_path, "sim", "config_a", test_id=3)
        assert "t3" in result.name


# ---------------------------------------------------------------------------
# Test check_paths (captures stdout + sys.exit)
# ---------------------------------------------------------------------------


class TestCheckPaths:
    def test_all_pass(self, tmp_path: Path, capsys, monkeypatch):
        monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
        from booley.shared_infra import check_paths

        rtl = tmp_path / "rtl"
        rtl.mkdir()
        check_paths(tmp_path, {"rtl": rtl})
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["pass"] is True

    def test_missing_dir_exits(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
        from booley.shared_infra import check_paths

        with pytest.raises(SystemExit) as exc:
            check_paths(tmp_path, {"missing": tmp_path / "nonexistent"})
        assert exc.value.code == 1

    def test_extra_checks_merged(self, tmp_path: Path, capsys, monkeypatch):
        monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
        from booley.shared_infra import check_paths

        rtl = tmp_path / "rtl"
        rtl.mkdir()
        check_paths(tmp_path, {"rtl": rtl}, extra_checks={"dpi": {"exists": True}})
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["dpi"]["exists"] is True


# ---------------------------------------------------------------------------
# Test config-leakage guard: project_root scoping
# ---------------------------------------------------------------------------


class TestProjectRootScoping:
    """Verify source-dir accessors don't leak CWD-cached config across projects.

    Regression guard for the bug class addressed by commits
    0c27f06 / 0f89dafd / 255bbb0: when a caller passes an explicit
    project_root, the config must come from that root — never from the
    module-level CWD cache, which may belong to a different project.

    ADR 0026 follow-through: the source of truth is now each project's ``.core``
    ``tags:[tb]`` partition (read fresh per root by ``source_dirs_from_core``),
    not ``[sources.*]`` in a gitignored ``.booley_project/booley.toml``. The
    scoping property is unchanged; only the fixture shape moved to ``.core``.
    """

    @staticmethod
    def _make_project(root: Path, rtl_dirs: list[str], tb_dirs: list[str]) -> None:
        """Author a flat ``.core`` at *root* placing RTL sources under *rtl_dirs*
        and tb-tagged sources under *tb_dirs* (subdir files, so
        ``source_dirs_from_core`` yields the parent dir of each)."""
        root.mkdir(parents=True, exist_ok=True)
        rtl_files = "\n".join(
            f"      - {d}/dut.sv: {{file_type: systemVerilogSource}}" for d in rtl_dirs
        )
        tb_files = "\n".join(
            f"      - {d}/tb.sv: {{file_type: systemVerilogSource}}" for d in tb_dirs
        )
        (root / "design.core").write_text(
            "CAPI=2:\n"
            "name: ::proj\n"
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

    def test_explicit_project_root_isolates_from_cwd_cache(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        """A scoped call must not leak the CWD-cached config from another project."""
        import booley.shared_infra as si

        proj_a = tmp_path / "proj_a"
        proj_b = tmp_path / "proj_b"
        self._make_project(proj_a, ["rtl"], ["tb"])
        self._make_project(proj_b, ["src"], ["verif"])

        # Prime the module-level CWD cache with a stale (proj_a-shaped) config —
        # the scoped .core read for proj_b must ignore it entirely.
        proj_a_cfg = {
            "sources": {
                "rtl": {"source_dirs": ["rtl"]},
                "testbench": {"source_dirs": ["tb"]},
            },
        }
        monkeypatch.setattr(si, "_TOML_CACHE", proj_a_cfg)

        # Now scope a call to proj_b — must return proj_b's .core dirs, NOT proj_a's.
        rtl_b = si.get_rtl_source_dirs(proj_b)
        tb_b = si.get_tb_source_dirs(proj_b)
        assert rtl_b == ["src"], f"leak detected: {rtl_b}"
        assert tb_b == ["verif"], f"leak detected: {tb_b}"

    def test_missing_config_under_explicit_root_returns_defaults_not_cache(
        self,
        tmp_path: Path,
        monkeypatch,
    ):
        """When project_root lacks a ``.core``, accessors must NOT fall back to the cache."""
        import booley.shared_infra as si

        empty = tmp_path / "empty_project"
        empty.mkdir()

        # Prime the CWD cache as if the harness had loaded a different project.
        cached = {
            "sources": {
                "rtl": {"source_dirs": ["custom_rtl"]},
                "testbench": {"source_dirs": ["custom_tb"]},
            },
        }
        monkeypatch.setattr(si, "_TOML_CACHE", cached)

        # Scoped to empty/, which has no .core. Must fall back to the hardcoded
        # .core-era defaults (["rtl", "fw"] / ["tb"]), NOT the cached values.
        assert si.get_rtl_source_dirs(empty) == ["rtl", "fw"]
        assert si.get_tb_source_dirs(empty) == ["tb"]

    def test_worktree_reads_its_own_tracked_core(self, tmp_path: Path, monkeypatch):
        """A git worktree resolves source dirs from its own checked-out ``.core``.

        Premise reframed for the ``.core`` era: the pre-ADR-0026 test asserted a
        worktree WALKS BACK to the main repo because its ``.booley_project/``
        (holding ``booley.toml``) was gitignored and absent from the checkout.
        ``.core`` files are *tracked*, so ``git worktree add`` materializes them
        directly in the worktree — no walk-back needed, and a stale main-repo
        config can never shadow the worktree's own. We assert that guarantee.
        """
        import booley.shared_infra as si

        main = tmp_path / "main"
        main.mkdir()
        self._make_project(main, ["main_rtl"], ["main_tb"])
        # git worktree internals: main has .git/ dir; wt has a .git file.
        git_dir = main / ".git"
        git_dir.mkdir()
        worktrees = git_dir / "worktrees" / "wt"
        worktrees.mkdir(parents=True)

        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {worktrees}\n", encoding="utf-8")
        # The tracked .core materializes in the worktree checkout with its own dirs.
        self._make_project(wt, ["worktree_rtl"], ["worktree_tb"])

        monkeypatch.setattr(si, "_TOML_CACHE", None)
        assert si.get_rtl_source_dirs(wt) == ["worktree_rtl"]
        assert si.get_tb_source_dirs(wt) == ["worktree_tb"]

    def test_explicit_call_does_not_pollute_cwd_cache(self, tmp_path: Path, monkeypatch):
        """Scoped reads must NOT write into the module-level CWD cache."""
        import booley.shared_infra as si

        proj_b = tmp_path / "proj_b"
        self._make_project(proj_b, ["src"], ["verif"])
        monkeypatch.setattr(si, "_TOML_CACHE", None)

        _ = si.get_rtl_source_dirs(proj_b)
        # Cache must remain unpopulated — the scoped read should bypass it.
        assert si._TOML_CACHE is None, "scoped read polluted the CWD cache"
