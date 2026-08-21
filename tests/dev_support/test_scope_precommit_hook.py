"""Tests for scope_precommit_hook — hard scope and bookkeeping boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from booley.dev_support.scope_precommit_hook import (
    _load_scope,
    _matches_scope,
    _staged_files,
    main,
)

# ---------------------------------------------------------------------------
# _load_scope
# ---------------------------------------------------------------------------


class TestLoadScope:
    def test_missing_file(self, tmp_path: Path):
        assert _load_scope(tmp_path) is None

    def test_valid_scope(self, tmp_path: Path):
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": ["rtl/foo.sv", "tb/bar.sv"]}))
        result = _load_scope(tmp_path)
        assert result == ["rtl/foo.sv", "tb/bar.sv"]

    def test_no_scope_key(self, tmp_path: Path):
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"other": "data"}))
        assert _load_scope(tmp_path) is None

    def test_invalid_json(self, tmp_path: Path):
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text("not valid json{{{")
        assert _load_scope(tmp_path) is None

    def test_wildcard_scope(self, tmp_path: Path):
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": ["*"]}))
        result = _load_scope(tmp_path)
        assert result == ["*"]

    def test_empty_scope_list(self, tmp_path: Path):
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": []}))
        result = _load_scope(tmp_path)
        assert result == []

    def test_non_dict_root(self, tmp_path: Path):
        # A JSON array/scalar at the root has no .get() — must degrade, not crash.
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps(["rtl/foo.sv"]))
        assert _load_scope(tmp_path) is None

    def test_scope_is_bare_string(self, tmp_path: Path):
        # The classic bug: a string scope would iterate char-by-char downstream.
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": "rtl/foo.sv"}))
        assert _load_scope(tmp_path) is None

    def test_scope_with_non_string_entries(self, tmp_path: Path):
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": ["rtl/foo.sv", 42, None]}))
        assert _load_scope(tmp_path) is None


# ---------------------------------------------------------------------------
# _matches_scope
# ---------------------------------------------------------------------------


class TestMatchesScope:
    def test_exact_match(self):
        assert _matches_scope("rtl/foo.sv", ["rtl/foo.sv"])

    def test_no_match(self):
        assert not _matches_scope("rtl/bar.sv", ["rtl/foo.sv"])

    def test_glob_match(self):
        assert _matches_scope("rtl/foo.sv", ["rtl/*.sv"])

    def test_glob_no_match(self):
        assert not _matches_scope("tb/foo.sv", ["rtl/*.sv"])

    def test_question_mark_glob(self):
        assert _matches_scope("rtl/a.sv", ["rtl/?.sv"])
        assert not _matches_scope("rtl/ab.sv", ["rtl/?.sv"])

    def test_bracket_glob(self):
        assert _matches_scope("rtl/a.sv", ["rtl/[abc].sv"])
        assert not _matches_scope("rtl/d.sv", ["rtl/[abc].sv"])

    def test_multiple_scope_entries(self):
        scope = ["rtl/foo.sv", "tb/*.sv"]
        assert _matches_scope("rtl/foo.sv", scope)
        assert _matches_scope("tb/bar.sv", scope)
        assert not _matches_scope("docs/readme.md", scope)

    def test_empty_scope(self):
        assert not _matches_scope("rtl/foo.sv", [])


# ---------------------------------------------------------------------------
# _staged_files
# ---------------------------------------------------------------------------


class TestStagedFiles:
    def test_returns_file_list(self):
        with patch("booley.dev_support.scope_precommit_hook.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "rtl/foo.sv\0tb/bar.sv\0"
            result = _staged_files()
        assert result == ["rtl/foo.sv", "tb/bar.sv"]

    def test_empty_output(self):
        with patch("booley.dev_support.scope_precommit_hook.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            result = _staged_files()
        assert result == []

    def test_git_failure(self):
        with patch("booley.dev_support.scope_precommit_hook.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            result = _staged_files()
        assert result == []

    def test_filters_blank_lines(self):
        with patch("booley.dev_support.scope_precommit_hook.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "rtl/a.sv\0\0\0tb/b.sv\0"
            result = _staged_files()
        assert result == ["rtl/a.sv", "tb/b.sv"]


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMain:
    def test_no_scope_file_allows_commit(self, tmp_path: Path):
        with (
            patch("booley.dev_support.scope_precommit_hook.Path.cwd", return_value=tmp_path),
            patch(
                "booley.dev_support.scope_precommit_hook._staged_files",
                return_value=["any/file.sv"],
            ),
        ):
            assert main() == 0

    def test_wildcard_scope_owns_nothing_and_blocks_commit(self, tmp_path: Path, capsys):
        """The unknown-scope sentinel lost its blanket-permission meaning."""
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": ["*"]}))

        with (
            patch("booley.dev_support.scope_precommit_hook.Path.cwd", return_value=tmp_path),
            patch(
                "booley.dev_support.scope_precommit_hook._staged_files",
                return_value=["anything.sv"],
            ),
        ):
            assert main() == 1
        assert "anything.sv" in capsys.readouterr().err

    def test_in_scope_files_allowed(self, tmp_path: Path):
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": ["rtl/foo.sv"]}))

        with (
            patch("booley.dev_support.scope_precommit_hook.Path.cwd", return_value=tmp_path),
            patch(
                "booley.dev_support.scope_precommit_hook._staged_files",
                return_value=["rtl/foo.sv"],
            ),
        ):
            assert main() == 0

    def test_out_of_scope_files_block_commit(self, tmp_path: Path, capsys):
        """Scope is a hard commit boundary."""
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": ["rtl/foo.sv"]}))

        with (
            patch("booley.dev_support.scope_precommit_hook.Path.cwd", return_value=tmp_path),
            patch(
                "booley.dev_support.scope_precommit_hook._staged_files",
                return_value=["rtl/foo.sv", "docs/readme.md"],
            ),
        ):
            assert main() == 1
        assert "docs/readme.md" in capsys.readouterr().err

    def test_harness_bookkeeping_is_rejected(self, tmp_path: Path):
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": ["rtl/foo.sv"]}))

        with (
            patch("booley.dev_support.scope_precommit_hook.Path.cwd", return_value=tmp_path),
            patch(
                "booley.dev_support.scope_precommit_hook._staged_files",
                return_value=["rtl/foo.sv", ".booley_project/booley.toml"],
            ),
        ):
            assert main() == 1

    def test_manifest_selected_generator_is_rejected(self, tmp_path: Path):
        path = "scripts/build_recipe.py"
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": [path], "contract_control": [path]}))

        with (
            patch("booley.dev_support.scope_precommit_hook.Path.cwd", return_value=tmp_path),
            patch(
                "booley.dev_support.scope_precommit_hook._staged_files",
                return_value=[path],
            ),
        ):
            assert main() == 1

    def test_bookkeeping_check_runs_without_a_scope_file(self, tmp_path: Path):
        """The forbidden tier is scope-independent — no .scope.json, still blocked."""
        with (
            patch("booley.dev_support.scope_precommit_hook.Path.cwd", return_value=tmp_path),
            patch(
                "booley.dev_support.scope_precommit_hook._staged_files",
                return_value=[".scope.json"],
            ),
        ):
            assert main() == 1

    def test_stealth_cores_are_immutable_contract_inputs(self, tmp_path: Path):
        """Scope cannot override the sealed stealth Target contract."""
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": [".booley_project/cores/dut.core"]}))

        with (
            patch("booley.dev_support.scope_precommit_hook.Path.cwd", return_value=tmp_path),
            patch(
                "booley.dev_support.scope_precommit_hook._staged_files",
                return_value=[".booley_project/cores/dut.core"],
            ),
        ):
            assert main() == 1

    def test_project_docs_are_not_bookkeeping(self, tmp_path: Path):
        """Project docs may be explicit ticket outputs, not acceptance state."""
        path = ".booley_project/docs/fw/memory-map.md"
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": [path]}))

        with (
            patch("booley.dev_support.scope_precommit_hook.Path.cwd", return_value=tmp_path),
            patch("booley.dev_support.scope_precommit_hook._staged_files", return_value=[path]),
        ):
            assert main() == 0

    def test_no_staged_files_allowed(self, tmp_path: Path):
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": ["rtl/foo.sv"]}))

        with (
            patch("booley.dev_support.scope_precommit_hook.Path.cwd", return_value=tmp_path),
            patch("booley.dev_support.scope_precommit_hook._staged_files", return_value=[]),
        ):
            assert main() == 0

    def test_empty_scope_blocks_everything(self, tmp_path: Path, capsys):
        """An empty scope owns nothing, so every staged file is a deviation."""
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": []}))

        with (
            patch("booley.dev_support.scope_precommit_hook.Path.cwd", return_value=tmp_path),
            patch(
                "booley.dev_support.scope_precommit_hook._staged_files",
                return_value=["rtl/foo.sv"],
            ),
        ):
            assert main() == 1
        assert "rtl/foo.sv" in capsys.readouterr().err

    def test_glob_scope_matches(self, tmp_path: Path):
        scope_file = tmp_path / ".scope.json"
        scope_file.write_text(json.dumps({"scope": ["rtl/*.sv"]}))

        with (
            patch("booley.dev_support.scope_precommit_hook.Path.cwd", return_value=tmp_path),
            patch(
                "booley.dev_support.scope_precommit_hook._staged_files",
                return_value=["rtl/foo.sv", "rtl/bar.sv"],
            ),
        ):
            assert main() == 0
