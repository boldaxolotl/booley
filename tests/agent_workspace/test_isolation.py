"""Tests for workspace_isolation -- the single-owner isolation module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from booley.agent_workspace.isolation import (
    CATEGORY_DIRS_DEFAULT,
    get_category_dirs,
    remove_shadow_package,
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
# Shadow package cleanup
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
        with patch("booley.agent_workspace.isolation.shutil.rmtree", side_effect=OSError("perm")):
            remove_shadow_package(tmp_path)  # logs warning, doesn't raise
        assert shadow.is_dir()


@pytest.mark.parametrize(
    "content",
    [
        "not: [valid",
        "filesets: broken",
        "filesets: {rtl: {files: source.sv}}",
        "filesets: {rtl: {files: [42]}}",
    ],
)
def test_strict_inventory_refuses_malformed_discovered_core(tmp_path, content):
    from booley.fusesoc.fusesoc_registry import FuseSocError, classified_sources

    _write_core(tmp_path)
    (tmp_path / "broken.core").write_text(content)
    with pytest.raises(FuseSocError, match=r"broken.core"):
        classified_sources(tmp_path, strict=True)


def test_strict_inventory_refuses_escaping_declared_source(tmp_path):
    from booley.fusesoc.fusesoc_registry import FuseSocError, classified_sources

    core = _write_core(tmp_path)
    core.write_text(core.read_text().replace("rtl/dut.sv", "../outside.sv"))
    with pytest.raises(FuseSocError, match="escapes Project Root"):
        classified_sources(tmp_path, strict=True)
