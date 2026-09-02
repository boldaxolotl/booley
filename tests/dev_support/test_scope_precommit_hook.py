"""Tests for scope_precommit_hook — hard scope and bookkeeping boundaries."""

from __future__ import annotations

import json
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from booley.dev_support.scope_precommit_hook import (
    _load_scope,
    _matches_scope,
    _staged_files,
    main,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        timeout=10,
    )


def _vendor_scope_hook(hook_dir: Path) -> Path:
    hook_dir.mkdir()
    package = Path(__file__).resolve().parents[2] / "src" / "booley"
    sources = {
        "scope_precommit_hook.py": package / "dev_support" / "scope_precommit_hook.py",
        "commit_msg_utils.py": package / "dev_support" / "commit_msg_utils.py",
        "checkout_role.py": package / "runtime" / "checkout_role.py",
        "boundary.py": package / "core" / "boundary.py",
        "contract_path_policy.py": package / "ticket_board" / "contract_path_policy.py",
    }
    for destination, source in sources.items():
        shutil.copy2(source, hook_dir / destination)
    return hook_dir / "scope_precommit_hook.py"


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
    def test_module_imports_vendored_commit_helper(self, monkeypatch):
        """The standalone scope hook resolves its flat vendored dependency."""
        module_path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "booley"
            / "dev_support"
            / "scope_precommit_hook.py"
        )
        helper_dir = str(module_path.parent)
        monkeypatch.syspath_prepend(helper_dir)
        real_import = __import__

        def block_package_helper(name, *args, **kwargs):
            if name == "booley.dev_support.commit_msg_utils":
                raise ModuleNotFoundError(name)
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=block_package_helper):
            namespace = runpy.run_path(str(module_path))

        assert callable(namespace["source_checkout_policy_owner"])

    def test_stale_hook_allows_source_checkout_commit(self, tmp_path: Path):
        """A linked source worktree never inherits old Project scope policy."""
        (tmp_path / "pyproject.toml").write_text(
            "[tool.booley]\nsource_checkout = true\n",
            encoding="utf-8",
        )
        (tmp_path / ".scope.json").write_text(json.dumps({"scope": ["rtl/allowed.sv"]}))

        with (
            patch("booley.dev_support.scope_precommit_hook.Path.cwd", return_value=tmp_path),
            patch(
                "booley.dev_support.scope_precommit_hook._staged_files",
                return_value=["rtl/outside.sv"],
            ) as staged_files,
        ):
            assert main() == 0
        staged_files.assert_not_called()

    def test_standalone_stale_hook_allows_source_linked_worktree_commit(self, tmp_path: Path):
        source = tmp_path / "source"
        source.mkdir()
        (source / "pyproject.toml").write_text(
            "[tool.booley]\nsource_checkout = true\n",
            encoding="utf-8",
        )
        _git(tmp_path, "init", "-q", "-b", "main", str(source))
        _git(source, "add", "pyproject.toml")
        _git(
            source,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )
        linked = tmp_path / "linked"
        _git(source, "worktree", "add", "-q", "-b", "ticket", str(linked))
        (linked / ".scope.json").write_text(json.dumps({"scope": ["rtl/allowed.sv"]}))
        outside = linked / "rtl" / "outside.sv"
        outside.parent.mkdir()
        outside.write_text("module outside; endmodule\n", encoding="utf-8")
        _git(linked, "add", "rtl/outside.sv")
        hook = _vendor_scope_hook(tmp_path / "stale-hooks")

        result = subprocess.run(
            [sys.executable, "-S", str(hook)],
            cwd=linked,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

        assert result.returncode == 0, result.stderr

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
