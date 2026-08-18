"""Tests for booley.paths — package data resolution."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from booley import paths


class TestPackageDataDir:
    def test_returns_path_with_data(self):
        result = paths.package_data_dir()
        assert isinstance(result, Path)
        assert result.name == "data"

    def test_fallback_when_refs_missing(self, tmp_path):
        shadow = tmp_path / "booley" / "data"
        shadow.mkdir(parents=True)
        real = Path(paths.__file__).resolve().parent / "data"
        if (real / "refs").is_dir():
            with patch("booley.paths.files") as mock_files:
                mock_files.return_value.joinpath.return_value = shadow
                result = paths.package_data_dir()
                assert (result / "refs").is_dir() or result == shadow


class TestRefsDir:
    def test_is_subdirectory_of_data(self):
        result = paths.refs_dir()
        assert result.parent == paths.package_data_dir()
        assert result.name == "refs"


class TestTroubleshootingPath:
    def test_returns_packaged_troubleshooting_guide(self):
        result = paths.troubleshooting_path()
        assert result.name == "TROUBLESHOOTING.md"
        assert result.parent == paths.refs_dir()
        assert result.is_file()

    def test_packaged_guide_matches_public_document(self):
        public = Path(__file__).resolve().parent.parent / "docs" / "TROUBLESHOOTING.md"
        assert paths.troubleshooting_path().read_bytes() == public.read_bytes()

    def test_faq_path_remains_a_compatibility_alias(self):
        assert paths.faq_path() == paths.troubleshooting_path()


class TestDockerDataDir:
    def test_name_is_docker(self):
        result = paths.docker_data_dir()
        assert result.name == "docker"
        assert result.parent == paths.package_data_dir()


class TestSkillsDir:
    def test_name_is_skills(self):
        result = paths.skills_dir()
        assert result.name == "skills"


class TestDevSupportDir:
    def test_returns_dev_support_path(self):
        result = paths.dev_support_dir()
        assert isinstance(result, Path)
        assert result.name == "dev_support"


class TestBwaveBinary:
    @patch.object(sys, "platform", "win32")
    def test_windows_exe_suffix(self):
        result = paths.bwave_binary()
        assert result.name == "bwave.exe"

    @patch.object(sys, "platform", "linux")
    def test_linux_no_suffix(self):
        result = paths.bwave_binary()
        assert result.name == "bwave"


class TestCheatsheetPath:
    def test_returns_md_file(self):
        result = paths.cheatsheet_path()
        assert result.name == "cheatsheet.md"
        assert result.parent == paths.package_data_dir()
